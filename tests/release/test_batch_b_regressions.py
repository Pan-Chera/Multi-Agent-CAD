"""Tracked runtime regression guards for Batch B fixes.

These tests exercise the runtime contracts behind the Batch B production
fixes -- they do not call any external model.

* BUG-013: ``_validate_mating_plan`` rejects multi-part briefs whose mate
  graph is empty / under-connected / disconnected / cyclic. Before the
  fix, an empty ``mates`` list plus an empty ``interfaces`` list passed
  validation for any ``len(parts)`` -- the assembler then emitted only
  the root part while QA reported PASS.

* BUG-016: ``_pair_collides`` returns ``None`` (UNVERIFIABLE) -- never
  ``False`` -- when either mesh has zero volume. Before the fix, the
  ``vol = frac * min(abs(mesh_a.volume), abs(mesh_b.volume))`` proxy
  collapsed to 0 and the pair reported "no collision" even when a flat
  plate clearly crossed a closed box.

* BUG-008: ``export_urdf`` fails closed (returns ``None``) when any link
  resolves to a zero-volume mesh, instead of emitting a URDF that skips
  ``<inertial>`` and lets PyBullet assign mass=1. The preflight runs
  before any link is emitted so no half-URDF is left on the final path.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest
import trimesh

from mac_assembly.schemas_assembly import (
    Anchor,
    AnchorKind,
    AssemblyBrief,
    MateSpec,
    MateType,
    MatingPlan,
    PartSpec,
)
from mac_assembly.nodes_assembly import _validate_mating_plan


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

def _brief(part_ids):
    """AssemblyBrief with N non-template parts and no interfaces."""
    return AssemblyBrief(
        assembly_name="t",
        user_request_raw="t",
        parts=[
            PartSpec(part_id=pid, part_name=pid.upper(), description=pid)
            for pid in part_ids
        ],
        interfaces=[],
    )


def _rigid_mate(mid, fixed, moving):
    """Minimal RIGID mate between two parts (AXIS_POINT anchors on Z)."""
    return MateSpec(
        mate_id=mid,
        mate_type=MateType.RIGID,
        fixed_part_id=fixed,
        moving_part_id=moving,
        fixed_anchor=Anchor(kind=AnchorKind.AXIS_POINT, axis="z", offset_mm=0.0),
        moving_anchor=Anchor(kind=AnchorKind.AXIS_POINT, axis="z", offset_mm=0.0),
    )


# ---------------------------------------------------------------------------
# BUG-013 -- _validate_mating_plan tree invariants
# ---------------------------------------------------------------------------

class TestValidateMatingPlanTreeInvariants(unittest.TestCase):
    """Cover the seven contract cases listed in the BUG-013 brief."""

    def test_single_part_zero_mates_is_valid(self):
        brief = _brief(["a"])
        plan = MatingPlan(assembly_name="t", mates=[])
        self.assertEqual(_validate_mating_plan(plan, brief), [])

    def test_two_parts_zero_mates_is_invalid(self):
        brief = _brief(["a", "b"])
        plan = MatingPlan(assembly_name="t", mates=[])
        errs = _validate_mating_plan(plan, brief)
        self.assertGreaterEqual(len(errs), 1)
        self.assertTrue(
            any("under-connected" in e for e in errs),
            f"expected an under-connected error, got {errs}",
        )

    def test_three_parts_zero_mates_is_invalid(self):
        brief = _brief(["a", "b", "c"])
        plan = MatingPlan(assembly_name="t", mates=[])
        errs = _validate_mating_plan(plan, brief)
        self.assertGreaterEqual(len(errs), 1)
        self.assertTrue(
            any("under-connected" in e for e in errs),
            f"expected an under-connected error, got {errs}",
        )

    def test_three_parts_one_edge_is_invalid(self):
        brief = _brief(["a", "b", "c"])
        plan = MatingPlan(
            assembly_name="t",
            mates=[_rigid_mate("m_ab", "a", "b")],
        )
        errs = _validate_mating_plan(plan, brief)
        self.assertGreaterEqual(len(errs), 1)
        # Either the edge-count gate or the multiple-roots gate (or both)
        # must fire -- both are legitimate signals of under-connection.
        self.assertTrue(
            any("under-connected" in e or "multiple unmoved roots" in e for e in errs),
            f"expected under-connected or multiple-roots error, got {errs}",
        )

    def test_three_parts_two_edges_valid_tree_is_valid(self):
        brief = _brief(["a", "b", "c"])
        plan = MatingPlan(
            assembly_name="t",
            mates=[
                _rigid_mate("m_ab", "a", "b"),
                _rigid_mate("m_bc", "b", "c"),
            ],
        )
        self.assertEqual(_validate_mating_plan(plan, brief), [])

    def test_disconnected_forest_is_invalid(self):
        # 4 parts, 2 mates forming two trees: a->b, c->d.
        # Edge count (2) < n-1 (3), AND unmoved = {a, c} (multiple roots).
        brief = _brief(["a", "b", "c", "d"])
        plan = MatingPlan(
            assembly_name="t",
            mates=[
                _rigid_mate("m_ab", "a", "b"),
                _rigid_mate("m_cd", "c", "d"),
            ],
        )
        errs = _validate_mating_plan(plan, brief)
        self.assertGreaterEqual(len(errs), 1)
        self.assertTrue(
            any("under-connected" in e or "multiple unmoved roots" in e for e in errs),
            f"expected forest rejection, got {errs}",
        )

    def test_cycle_is_invalid(self):
        # 3 parts, 3 mates forming a directed cycle a->b->c->a.
        # moved = {a,b,c}; unmoved = {} -> "need a fixed root".
        brief = _brief(["a", "b", "c"])
        plan = MatingPlan(
            assembly_name="t",
            mates=[
                _rigid_mate("m_ab", "a", "b"),
                _rigid_mate("m_bc", "b", "c"),
                _rigid_mate("m_ca", "c", "a"),
            ],
        )
        errs = _validate_mating_plan(plan, brief)
        self.assertGreaterEqual(len(errs), 1)
        self.assertTrue(
            any("fixed root" in e or "cycle" in e for e in errs),
            f"expected cycle / no-root rejection, got {errs}",
        )

    def test_cycle_with_orphan_is_invalid(self):
        # 4 parts, 3 mates forming a cycle on a,b,c plus orphan d.
        # unmoved = {d} (size 1) so the unmoved gate does NOT fire; the
        # toposort gate must catch the cyclic sub-component.
        brief = _brief(["a", "b", "c", "d"])
        plan = MatingPlan(
            assembly_name="t",
            mates=[
                _rigid_mate("m_ab", "a", "b"),
                _rigid_mate("m_bc", "b", "c"),
                _rigid_mate("m_ca", "c", "a"),
            ],
        )
        errs = _validate_mating_plan(plan, brief)
        self.assertGreaterEqual(len(errs), 1)
        self.assertTrue(
            any("cycle" in e for e in errs),
            f"expected cycle rejection from toposort, got {errs}",
        )

    def test_template_only_part_excluded_from_graph_count(self):
        # A template part that is never directly mated (only referenced
        # via reuses_part_id by an instance) must NOT count toward the
        # graph node total. 1 base + 1 template + 1 instance, 1 mate
        # between base and the instance -> valid (n_graph=2, mates=1).
        brief = AssemblyBrief(
            assembly_name="t",
            user_request_raw="t",
            parts=[
                PartSpec(part_id="base", part_name="Base", description="base"),
                PartSpec(part_id="arm_template", part_name="Arm Template",
                         description="arm template"),
                PartSpec(part_id="arm_l", part_name="Arm L",
                         description="arm left",
                         reuses_part_id="arm_template"),
            ],
            interfaces=[],
        )
        plan = MatingPlan(
            assembly_name="t",
            mates=[_rigid_mate("m_base_arm_l", "base", "arm_l")],
        )
        self.assertEqual(_validate_mating_plan(plan, brief), [])


# ---------------------------------------------------------------------------
# BUG-016 -- _pair_collides zero-volume mesh
# ---------------------------------------------------------------------------

class TestPairCollidesZeroVolumeMesh(unittest.TestCase):
    """Verify zero-volume meshes return UNVERIFIABLE, never a false PASS."""

    def setUp(self):
        # Closed 10mm box, volume=1000 mm^3.
        self.box = trimesh.creation.box(extents=[10, 10, 10])
        # Zero-volume plate: 4 verts, 2 triangles in the XY plane.
        self.plate = trimesh.Trimesh(
            vertices=[[-50, -50, 0], [50, -50, 0], [50, 50, 0], [-50, 50, 0]],
            faces=[[0, 1, 2], [0, 2, 3]],
        )
        # Sanity: the plate really is zero-volume.
        self.assertAlmostEqual(float(self.plate.volume), 0.0, places=12)

    def test_intersecting_zero_volume_plate_vs_closed_box_is_unverifiable(self):
        from mac_assembly.assembly_qa import _pair_collides
        result, detail, vol = _pair_collides(self.plate, self.box)
        self.assertIsNone(
            result,
            f"zero-volume plate vs closed box must return None, got {result} "
            f"(detail={detail!r})",
        )
        self.assertIn("zero-volume", detail.lower())
        self.assertEqual(vol, 0.0)

    def test_zero_volume_vs_zero_volume_is_unverifiable(self):
        from mac_assembly.assembly_qa import _pair_collides
        result, detail, _vol = _pair_collides(self.plate, self.plate)
        self.assertIsNone(
            result,
            f"zero-volume vs zero-volume must return None, got {result}",
        )
        self.assertIn("zero-volume", detail.lower())

    def test_overlapping_closed_meshes_still_detected_as_collision(self):
        from mac_assembly.assembly_qa import _pair_collides
        # Two boxes that overlap by 5mm in each axis -> must still be True.
        box_a = trimesh.creation.box(extents=[20, 20, 20])
        box_b = trimesh.creation.box(
            extents=[20, 20, 20],
            transform=trimesh.transformations.translation_matrix([10, 0, 0]),
        )
        result, _detail, _vol = _pair_collides(box_a, box_b)
        self.assertTrue(
            result is True,
            f"overlapping closed boxes must collide, got {result}",
        )

    def test_disjoint_closed_meshes_still_report_no_collision(self):
        from mac_assembly.assembly_qa import _pair_collides
        # Two boxes far apart -> must still be False (verified no collision).
        box_a = trimesh.creation.box(extents=[10, 10, 10])
        box_b = trimesh.creation.box(
            extents=[10, 10, 10],
            transform=trimesh.transformations.translation_matrix([100, 0, 0]),
        )
        result, _detail, _vol = _pair_collides(box_a, box_b)
        self.assertFalse(
            result,
            f"disjoint closed boxes must report no collision, got {result}",
        )

    def test_nan_volume_mesh_returns_unverifiable(self):
        # NaN volume bypasses ``<= eps`` (``nan <= x`` is False). Before the
        # isfinite guard, the proxy propagated NaN and produced a nonsense
        # collision verdict. The guard must fire and return None.
        from types import SimpleNamespace
        from mac_assembly.assembly_qa import _pair_collides
        nan_mesh = SimpleNamespace(volume=float("nan"))
        box = trimesh.creation.box(extents=[10, 10, 10])
        result, detail, _vol = _pair_collides(nan_mesh, box)
        self.assertIsNone(
            result,
            f"NaN-volume mesh must return None (UNVERIFIABLE), got {result}",
        )
        self.assertIn("zero-volume", detail.lower())

    def test_inf_volume_mesh_returns_unverifiable(self):
        # inf volume also bypasses ``<= eps`` and inflates the proxy to inf.
        from types import SimpleNamespace
        from mac_assembly.assembly_qa import _pair_collides
        inf_mesh = SimpleNamespace(volume=float("inf"))
        box = trimesh.creation.box(extents=[10, 10, 10])
        result, detail, _vol = _pair_collides(inf_mesh, box)
        self.assertIsNone(
            result,
            f"inf-volume mesh must return None (UNVERIFIABLE), got {result}",
        )
        self.assertIn("zero-volume", detail.lower())


# ---------------------------------------------------------------------------
# BUG-008 -- export_urdf zero-volume preflight
# ---------------------------------------------------------------------------

def _urdf_loc(xyz, rpy=(0, 0, 0)):
    return {"translation": list(xyz), "rotation_euler_xyz_deg": list(rpy)}


def _urdf_endpoint(part, xyz, rpy=(0, 0, 0)):
    return {"part": part, "position": list(xyz), "orientation": list(rpy)}


def _write_assembly_files(job: Path, labels, mate_pairs):
    """Write a minimal assembly_mates.json + assembly_manifest.json.

    ``labels`` is a list of (label, extents) tuples; ``mate_pairs`` is a
    list of (parent, child) tuples emitted as fixed revolute joints.
    """
    (job / "assembly_manifest.json").write_text(json.dumps([
        {"label": lbl, "location": _urdf_loc((0, 0, 0))}
        for lbl, _ in labels
    ]))
    mates = []
    for parent, child in mate_pairs:
        mates.append({
            "label": f"{parent}_to_{child}",
            "relation": "rigid",
            "fixed_endpoint": _urdf_endpoint(parent, (0, 0, 0)),
            "moving_endpoint": _urdf_endpoint(child, (0, 0, 0)),
        })
    (job / "assembly_mates.json").write_text(json.dumps(mates))
    for lbl, extents in labels:
        part_dir = job / "parts" / lbl
        part_dir.mkdir(parents=True, exist_ok=True)
        trimesh.creation.box(extents=extents).export(
            part_dir / "temp_output_0.stl"
        )


def _write_zero_volume_assembly_files(job: Path, labels, mate_pairs):
    """Like _write_assembly_files but emits a zero-volume plate for the
    first label in ``labels`` (the rest are closed boxes)."""
    (job / "assembly_manifest.json").write_text(json.dumps([
        {"label": lbl, "location": _urdf_loc((0, 0, 0))}
        for lbl, _ in labels
    ]))
    mates = []
    for parent, child in mate_pairs:
        mates.append({
            "label": f"{parent}_to_{child}",
            "relation": "rigid",
            "fixed_endpoint": _urdf_endpoint(parent, (0, 0, 0)),
            "moving_endpoint": _urdf_endpoint(child, (0, 0, 0)),
        })
    (job / "assembly_mates.json").write_text(json.dumps(mates))
    for i, (lbl, _extents) in enumerate(labels):
        part_dir = job / "parts" / lbl
        part_dir.mkdir(parents=True, exist_ok=True)
        if i == 0:
            # Zero-volume plate (4 verts, 2 triangles, planar).
            trimesh.Trimesh(
                vertices=[[-5, -5, 0], [5, -5, 0], [5, 5, 0], [-5, 5, 0]],
                faces=[[0, 1, 2], [0, 2, 3]],
            ).export(part_dir / "temp_output_0.stl")
        else:
            trimesh.creation.box(extents=_extents).export(
                part_dir / "temp_output_0.stl"
            )


class TestExportUrdfZeroVolumePreflight(unittest.TestCase):
    """Verify export_urdf rejects zero-volume meshes before emitting."""

    def test_valid_closed_meshes_export_normally(self):
        from mac_assembly import urdf_export as u
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            _write_assembly_files(
                job,
                labels=[("base", (10, 10, 10)), ("arm", (10, 10, 10))],
                mate_pairs=[("base", "arm")],
            )
            with patch.object(u, "_validate", return_value=("ok", "test")):
                output = u.export_urdf(job)
            self.assertIsNotNone(
                output,
                "closed-box assembly must export successfully",
            )
            self.assertTrue(Path(output).is_file())

    def test_zero_volume_mesh_rejected_with_clear_reason(self):
        from mac_assembly import urdf_export as u
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            _write_zero_volume_assembly_files(
                job,
                labels=[("base", (10, 10, 10)), ("arm", (10, 10, 10))],
                mate_pairs=[("base", "arm")],
            )
            captured = []
            with patch.object(
                u, "_validate", return_value=("ok", "test")
            ), patch(
                "builtins.print",
                side_effect=lambda *a, **k: captured.append(" ".join(str(x) for x in a)),
            ):
                output = u.export_urdf(job)
            self.assertIsNone(
                output,
                "zero-volume mesh must cause export_urdf to return None",
            )
            joined = "\n".join(captured)
            self.assertIn(
                "zero-volume",
                joined.lower(),
                f"expected 'zero-volume' in stdout, got: {joined!r}",
            )

    def test_zero_volume_rejection_leaves_no_final_urdf(self):
        from mac_assembly import urdf_export as u
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            _write_zero_volume_assembly_files(
                job,
                labels=[("base", (10, 10, 10)), ("arm", (10, 10, 10))],
                mate_pairs=[("base", "arm")],
            )
            urdf_dir = job / "urdf"
            urdf_dir.mkdir()
            # Plant a prior successful URDF -- the preflight must NOT
            # overwrite it with a half-emitted candidate, and the candidate
            # must not be promoted to the final path.
            prior = urdf_dir / f"{job.name}.urdf"
            prior.write_text("previous successful export", encoding="utf-8")
            with patch.object(u, "_validate", return_value=("ok", "test")):
                self.assertIsNone(u.export_urdf(job))
            # The prior URDF is untouched.
            self.assertEqual(
                prior.read_text(encoding="utf-8"),
                "previous successful export",
            )
            # No candidate file left behind.
            self.assertFalse(
                (urdf_dir / f".{job.name}.candidate.urdf").exists(),
                "candidate URDF must not be left on disk after preflight "
                "rejection",
            )

    def test_nan_volume_mesh_rejected(self):
        # NaN volume bypasses ``<= eps`` (``nan <= x`` is False). Before the
        # isfinite guard, the preflight let the mesh through and
        # ``_inertial_from_mesh`` computed ``mass = nan``; ``_emit_link``'s
        # ``mass > 0`` gate (``nan > 0`` is False) then skipped
        # ``<inertial>`` -- the exact BUG-008 regression. The preflight must
        # fail closed.
        from types import SimpleNamespace
        from mac_assembly import urdf_export as u
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            _write_assembly_files(
                job,
                labels=[("base", (10, 10, 10)), ("arm", (10, 10, 10))],
                mate_pairs=[("base", "arm")],
            )
            captured = []
            fake_nan_mesh = SimpleNamespace(is_empty=False, volume=float("nan"))
            with patch.object(u, "_validate", return_value=("ok", "test")), \
                 patch("trimesh.load", return_value=fake_nan_mesh), \
                 patch(
                    "builtins.print",
                    side_effect=lambda *a, **k: captured.append(
                        " ".join(str(x) for x in a)
                    ),
                ):
                output = u.export_urdf(job)
            self.assertIsNone(
                output,
                "NaN-volume mesh must cause export_urdf to return None",
            )
            joined = "\n".join(captured)
            self.assertIn(
                "zero-volume",
                joined.lower(),
                f"expected 'zero-volume' in stdout, got: {joined!r}",
            )

    def test_inf_volume_mesh_rejected(self):
        # inf volume also bypasses ``<= eps``; ``_inertial_from_mesh``
        # would compute ``mass = inf``, ``_emit_link``'s ``mass > 0`` gate
        # (``inf > 0`` is True) would emit ``<inertial>`` with non-finite
        # dynamics -- a URDF PyBullet/MuJoCo reject. Preflight must fail
        # closed.
        from types import SimpleNamespace
        from mac_assembly import urdf_export as u
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            _write_assembly_files(
                job,
                labels=[("base", (10, 10, 10)), ("arm", (10, 10, 10))],
                mate_pairs=[("base", "arm")],
            )
            captured = []
            fake_inf_mesh = SimpleNamespace(is_empty=False, volume=float("inf"))
            with patch.object(u, "_validate", return_value=("ok", "test")), \
                 patch("trimesh.load", return_value=fake_inf_mesh), \
                 patch(
                    "builtins.print",
                    side_effect=lambda *a, **k: captured.append(
                        " ".join(str(x) for x in a)
                    ),
                ):
                output = u.export_urdf(job)
            self.assertIsNone(
                output,
                "inf-volume mesh must cause export_urdf to return None",
            )
            joined = "\n".join(captured)
            self.assertIn(
                "zero-volume",
                joined.lower(),
                f"expected 'zero-volume' in stdout, got: {joined!r}",
            )


if __name__ == "__main__":
    unittest.main()
