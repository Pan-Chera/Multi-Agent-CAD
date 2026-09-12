"""BUG-003 (P0 revision): _check_kinematics used to allow new
collisions on the direct mate pair when the volume was below
INTERFERENCE_VOLUME_TOL_MM3 (5.0 mm³), even when the baseline had NO
contact. A 1.0 mm³ new collision at a non-zero sample, with baseline
volume 0.0, was treated as "within baseline + tol" (1.0 <= 0 + 5.0) and
silently PASSed.

Fix: track baseline_hit (bool) per mesh pair, separately from
baseline_vol. When baseline_hit is False (no contact at sample 0), ANY
hit at ANY sweep sample is a NEW collision and must FAIL, regardless
of volume. The volume tolerance only applies to deepening of existing
baseline contact.

Also: per-mesh-pair baseline tracking, not per-part-pair -- a
tessellated part exporting as multiple connected components must not
have one mesh pair's baseline contact exempt a NEW collision on
another mesh pair.

No external LLM calls; trimesh primitives + mocked _pair_collides for
deterministic collision sequencing.
"""
from __future__ import annotations

import unittest
from unittest import mock

import trimesh

from mac_assembly import assembly_qa as qa
from mac_assembly.schemas_assembly import (
    Anchor,
    AnchorKind,
    MateSpec,
    MateType,
)


def _box(center, size=4.0):
    return trimesh.creation.box(
        extents=(size, size, size),
        transform=trimesh.transformations.translation_matrix(center),
    )


def _revolute_mate(fixed_id, moving_id):
    return MateSpec(
        mate_id="m1",
        mate_type=MateType.REVOLUTE,
        fixed_part_id=fixed_id,
        moving_part_id=moving_id,
        fixed_anchor=Anchor(
            kind=AnchorKind.AXIS_POINT, axis="z", offset_mm=0.0,
        ),
        moving_anchor=Anchor(
            kind=AnchorKind.AXIS_POINT, axis="z", offset_mm=0.0,
        ),
    )


def _placed(meshes_by_label):
    import numpy as np
    placed = []
    for label, mesh_list in meshes_by_label.items():
        bb = np.vstack([m.bounds for m in mesh_list])
        placed.append((label, tuple(bb.min(axis=0)), tuple(bb.max(axis=0))))
    return placed


class TestKinematicNewCollisionLowVolumeFails(unittest.TestCase):
    """When the baseline probe at sample 0 reports NO collision, any
    subsequent hit at a non-zero sample must FAIL, even if the volume
    is small (e.g. 1.0 mm³, well below INTERFERENCE_VOLUME_TOL_MM3)."""

    def _run_qa(self, meshes_by_label, mate, pair_collides_fn):
        placed = _placed(meshes_by_label)
        with mock.patch.object(qa, "_pair_collides",
                               side_effect=pair_collides_fn):
            return qa._check_kinematics(
                placed, [mate],
                meshes_by_label=meshes_by_label,
                step_paths={},
                locations={},
            )

    def test_baseline_no_contact_then_small_volume_new_collision_fails(self):
        """Baseline (sample 0) reports no collision. Subsequent non-zero
        samples report hit=True with vol=1.0 (below the 5.0 mm³ tol).
        The previous code allowed this as "within baseline + tol".
        Now: baseline_hit=False means ANY hit is a new collision -> FAIL."""
        a = _box((0, 0, -2), size=4.0)
        b = _box((0, 0, 2), size=4.0)
        meshes = {"a": [a], "b": [b]}
        mate = _revolute_mate("a", "b")
        call_count = {"n": 0}
        def fake_pair_collides(moved, static):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Baseline probe at sample 0: no collision.
                return (False, "no collision", 0.0)
            # Non-zero sample: NEW collision with small volume.
            return (True, "new small collision", 1.0)
        checks = self._run_qa(meshes, mate, fake_pair_collides)
        self.assertTrue(checks)
        self.assertFalse(
            checks[0].passed,
            f"baseline-free + new collision (vol=1.0) must FAIL, got: "
            f"{checks[0].detail}"
        )
        # Detail should mention "new collision" (not "deepened").
        self.assertIn(
            "new collision",
            checks[0].detail.lower(),
            f"detail should say 'new collision', got: {checks[0].detail}"
        )

    def test_baseline_contact_no_deepening_passes(self):
        """Baseline (sample 0) reports contact with vol=1.0. Subsequent
        non-zero samples report the SAME vol=1.0 (no deepening). This
        is legitimate designed contact -- must PASS via the deepening
        tolerance."""
        a = _box((0, 0, -2), size=4.0)
        b = _box((0, 0, 2), size=4.0)
        meshes = {"a": [a], "b": [b]}
        mate = _revolute_mate("a", "b")
        def fake_pair_collides(moved, static):
            return (True, "baseline contact", 1.0)
        checks = self._run_qa(meshes, mate, fake_pair_collides)
        self.assertTrue(checks)
        self.assertTrue(
            checks[0].passed,
            f"baseline contact with no deepening must PASS, got: "
            f"{checks[0].detail}"
        )

    def test_per_mesh_pair_baseline_does_not_exempt_other_pair(self):
        """Two mesh pairs (multiple moving meshes vs one static mesh).
        Pair 1 has baseline contact (allowed via deepening tolerance).
        Pair 2 has NO baseline contact but reports a new collision at a
        non-zero sample. The previous per-part-pair baseline tracking
        exempted Pair 2's new collision via Pair 1's baseline. Now:
        per-mesh-pair tracking catches Pair 2's new collision -> FAIL."""
        # Two moving meshes (m1, m2) vs one static mesh (s).
        # Mate has fixed="a", moving="b", so 'b' holds the moving meshes.
        m1 = _box((0, 0, 2), size=2.0)
        m2 = _box((10, 0, 2), size=2.0)
        s = _box((0, 0, -2), size=4.0)
        meshes = {"a": [s], "b": [m1, m2]}
        mate = _revolute_mate("a", "b")
        call_log = {"n": 0}
        def fake_pair_collides(moved, static):
            call_log["n"] += 1
            # moved.bounds = [-1,-1,1] to [1,1,3] for m1
            #          = [9,-1,1] to [11,1,3] for m2
            # static.bounds = [-2,-2,-4] to [2,2,0] for s
            moved_xmin = moved.bounds[0, 0]
            is_m2 = moved_xmin > 5.0
            if call_log["n"] <= 2:
                # Baseline probe phase (sample 0): one call per
                # (moving_mesh, static_mesh) pair.
                if is_m2:
                    # Pair (m2, s) baseline: NO contact.
                    return (False, "no contact", 0.0)
                else:
                    # Pair (m1, s) baseline: contact with vol=1.0.
                    return (True, "baseline contact", 1.0)
            # Sweep phase: Pair (m2, s) reports a new collision (small vol).
            if is_m2:
                return (True, "new collision on m2 vs s", 1.0)
            return (True, "baseline contact continues", 1.0)
        checks = self._run_qa(meshes, mate, fake_pair_collides)
        self.assertTrue(checks)
        self.assertFalse(
            checks[0].passed,
            f"Pair (m2, s) new collision must FAIL even though Pair "
            f"(m1, s) has baseline contact, got: {checks[0].detail}"
        )


if __name__ == "__main__":
    unittest.main()
