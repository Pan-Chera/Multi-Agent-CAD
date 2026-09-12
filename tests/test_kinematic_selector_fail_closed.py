"""P1-1: SELECTOR fail-closed in _check_kinematics.

The previous code used the legacy two-state ``_resolve_qa_anchor``
returning None on SELECTOR miss (and on probe error). When None, the
code fell through to the FACE/AXIS_POINT bbox fallback, sweeping about
the principal Z axis -- silently passing a Box part with a cylinder
SELECTOR query.

Fix: use the shared three-state ``_resolve_qa_anchor_3state`` (which
also distinguishes "no_selector" from "error" for SELECTOR + missing
step_path / missing selector_query). On ``not_found``, fail closed with
the selector-miss signature; on ``error``, fail closed with UNVERIFIABLE.

Also: ``_resolve_qa_point_3state`` now distinguishes SELECTOR + no
step_path / no selector_query as ``"error"`` (UNVERIFIABLE), not
``"no_selector"`` (which would fall through to bbox algebra).

No external LLM calls; trimesh primitives + build123d STEP files.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import trimesh
from build123d import Align, Box, Cylinder, Pos, export_step

from mac_assembly import assembly_qa as qa
from mac_assembly.schemas_assembly import (
    Anchor,
    AnchorKind,
    FaceQuery,
    MateSpec,
    MateType,
)


def _box_mesh(center, size=2.0):
    return trimesh.creation.box(
        extents=(size, size, size),
        transform=trimesh.transformations.translation_matrix(center),
    )


def _placed(meshes_by_label):
    placed = []
    for label, mesh_list in meshes_by_label.items():
        bb = np.vstack([m.bounds for m in mesh_list])
        placed.append((label, tuple(bb.min(axis=0)), tuple(bb.max(axis=0))))
    return placed


def _box_step_no_cylinder(path: Path):
    """A simple Box with NO cylindrical faces."""
    plate = Pos(0, 0, 5) * Box(20, 20, 10, align=(Align.CENTER,) * 3)
    export_step(plate, str(path))


def _shaft_step(path: Path, radius: float = 5.0, height: float = 8.0):
    """Solid shaft along Z."""
    shape = Cylinder(radius=radius, height=height, align=(Align.CENTER,) * 3)
    shape = Pos(0, 0, height / 2.0) * shape
    export_step(shape, str(path))


def _revolute_mate_selector(fixed_id, moving_id):
    """A REVOLUTE mate with a cylinder SELECTOR fixed anchor."""
    return MateSpec(
        mate_id="m1",
        mate_type=MateType.REVOLUTE,
        fixed_part_id=fixed_id,
        moving_part_id=moving_id,
        fixed_anchor=Anchor(
            kind=AnchorKind.SELECTOR,
            selector_query=FaceQuery(surface="cylinder", axis="z"),
        ),
        moving_anchor=Anchor(
            kind=AnchorKind.AXIS_POINT, axis="z", offset_mm=0.0,
        ),
    )


class TestKinematicSelectorFailClosed(unittest.TestCase):
    """_check_kinematics must fail closed on SELECTOR miss / probe error
    instead of falling through to bbox algebra."""

    def test_fixed_box_no_cylinder_with_cylinder_selector_kinematic_fails(self):
        """Counterexample: fixed is a Box with NO cylindrical faces, but
        the fixed anchor is a cylinder SELECTOR. The previous code's
        _resolve_qa_anchor returned None, fell through to bbox fallback,
        and swept about Z -- silently passing. Now: the 3state probe
        returns "not_found", the kinematic check fails with the
        selector-miss signature."""
        with tempfile.TemporaryDirectory() as tmp:
            fixed_step = Path(tmp) / "box.step"
            _box_step_no_cylinder(fixed_step)
            # Moving part is a shaft with a real cylinder -- but the
            # fixed SELECTOR miss should fail the kinematic check
            # before any sweep is attempted.
            meshes = {
                "fixed": [_box_mesh((0.0, 0.0, 0.0), 10.0)],
                "moving": [_box_mesh((0.0, 0.0, 0.0), 4.0)],
            }
            mate = _revolute_mate_selector("fixed", "moving")
            step_paths = {"fixed": str(fixed_step)}
            checks = qa._check_kinematics(
                _placed(meshes), [mate], meshes,
                step_paths=step_paths, locations={},
            )
        self.assertTrue(checks)
        self.assertFalse(
            checks[0].passed,
            f"fixed Box + cylinder SELECTOR must FAIL (not_found), "
            f"got: {checks[0].detail}"
        )
        self.assertIn(
            "SELECTOR anchor matched no face",
            checks[0].detail,
            f"detail should mention SELECTOR miss: {checks[0].detail}"
        )

    def test_selector_with_missing_step_path_is_unverifiable(self):
        """SELECTOR + missing step_path must be UNVERIFIABLE (not
        "no_selector" which falls through to bbox). P1-1 requirement 3.
        """
        meshes = {
            "fixed": [_box_mesh((0.0, 0.0, 0.0), 10.0)],
            "moving": [_box_mesh((0.0, 0.0, 0.0), 4.0)],
        }
        mate = _revolute_mate_selector("fixed", "moving")
        # step_paths is empty -> step_path is None for the fixed part.
        # _resolve_qa_anchor_3state returns ("error", None) for SELECTOR
        # + no step_path.
        checks = qa._check_kinematics(
            _placed(meshes), [mate], meshes,
            step_paths={}, locations={},
        )
        self.assertTrue(checks)
        self.assertFalse(
            checks[0].passed,
            f"SELECTOR + no step_path must FAIL (UNVERIFIABLE), got: "
            f"{checks[0].detail}"
        )
        self.assertIn(
            "UNVERIFIABLE",
            checks[0].detail.upper(),
            f"detail should mention UNVERIFIABLE: {checks[0].detail}"
        )

    def test_selector_probe_exception_is_unverifiable(self):
        """A probe error (helper returns "error") must be UNVERIFIABLE,
        not a silent PASS. This simulates a probe exception caught by
        _resolve_qa_anchor_3state (STEP unreadable, cadpy unavailable,
        or probe exception)."""
        meshes = {
            "fixed": [_box_mesh((0.0, 0.0, 0.0), 10.0)],
            "moving": [_box_mesh((0.0, 0.0, 0.0), 4.0)],
        }
        mate = _revolute_mate_selector("fixed", "moving")
        # Mock the 3state helper to return ("error", None) -- simulates
        # a probe exception caught by the helper's try/except.
        with mock.patch.object(
            qa, "_resolve_qa_anchor_3state",
            return_value=("error", None),
        ):
            checks = qa._check_kinematics(
                _placed(meshes), [mate], meshes,
                step_paths={"fixed": "/nonexistent.step"}, locations={},
            )
        self.assertTrue(checks)
        self.assertFalse(
            checks[0].passed,
            f"probe error must FAIL (UNVERIFIABLE), got: "
            f"{checks[0].detail}"
        )
        # The detail should mention UNVERIFIABLE.
        detail_upper = checks[0].detail.upper()
        self.assertIn(
            "UNVERIFIABLE",
            detail_upper,
            f"detail should mention UNVERIFIABLE: {checks[0].detail}"
        )

    def test_face_anchor_sweep_does_not_regress(self):
        """A FACE-anchored revolute mate (non-SELECTOR) must still
        sweep normally (no SELECTOR probe runs, no fail-closed)."""
        # Build a simple revolute with FACE anchors: the moving part
        # sweeps about Z (the FACE top normal). With no blocker, the
        # sweep should pass.
        meshes = {
            "fixed": [_box_mesh((0.0, 0.0, -2.0), 4.0)],
            "moving": [_box_mesh((5.0, 0.0, 2.0), 2.0)],
        }
        mate = MateSpec(
            mate_id="m1",
            mate_type=MateType.REVOLUTE,
            fixed_part_id="fixed",
            moving_part_id="moving",
            fixed_anchor=Anchor(
                kind=AnchorKind.FACE, face="top", offset_mm=0.0,
            ),
            moving_anchor=Anchor(
                kind=AnchorKind.AXIS_POINT, axis="z", offset_mm=0.0,
            ),
        )
        checks = qa._check_kinematics(
            _placed(meshes), [mate], meshes,
            step_paths={}, locations={},
        )
        self.assertTrue(checks)
        # With no blocker, the sweep should pass (the moving part
        # rotates about Z but its trajectory doesn't hit anything).
        self.assertTrue(
            checks[0].passed,
            f"FACE-anchored revolute sweep without blocker must pass, "
            f"got: {checks[0].detail}"
        )


if __name__ == "__main__":
    unittest.main()
