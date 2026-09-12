"""BUG-003 (revised): _check_kinematics used to unconditionally skip the
direct moving/fixed pair (the joint's own bearing/slider interface),
trusting mate delta to catch interface problems. But mate delta checks
anchor alignment, not volume penetration -- a slider ramming into the
end of its guide, or a rotating arm sweeping into its own pivot post,
went undetected.

Fix: run swept collision on direct pairs too. For legitimate
bearing/slider contact at the initial pose, use baseline comparison:
record the sample=0 penetration volume, then fail only if a non-zero
sample produces NEW or DEEPENED penetration beyond tolerance.

No external LLM calls; trimesh primitives + mocked _pair_collides for
deterministic collision sequencing.
"""
from __future__ import annotations

import math
import unittest
from unittest import mock

import numpy as np
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
    placed = []
    for label, mesh_list in meshes_by_label.items():
        bb = np.vstack([m.bounds for m in mesh_list])
        placed.append((label, tuple(bb.min(axis=0)), tuple(bb.max(axis=0))))
    return placed


class TestKinematicDirectPairCollision(unittest.TestCase):
    """The joint's own moving/fixed pair must be swept. Legitimate initial
    contact is allowed via baseline; new/deepened penetration fails."""

    def _run_qa(self, meshes_by_label, mate, locations=None,
                pair_collides_fn=None):
        placed = _placed(meshes_by_label)
        kwargs = {
            "meshes_by_label": meshes_by_label,
            "step_paths": {},
            "locations": locations or {},
        }
        if pair_collides_fn is not None:
            with mock.patch.object(qa, "_pair_collides",
                                   side_effect=pair_collides_fn):
                return qa._check_kinematics(placed, [mate], **kwargs)
        return qa._check_kinematics(placed, [mate], **kwargs)

    def test_direct_revolute_initial_contact_passes(self):
        """Bearing interface with designed contact at sample=0. Entire
        +/-30 deg sweep keeps the same contact (no new collision, no
        deepening) -> PASS via baseline exemption."""
        a = _box((0, 0, -2), size=4.0)
        b = _box((0, 0, 2), size=4.0)
        meshes = {"a": [a], "b": [b]}
        mate = _revolute_mate("a", "b")
        # Mock _pair_collides: at sample 0, designed contact (small
        # volume). At non-zero samples, same small volume -- baseline
        # contact preserved, no deepening.
        sample_holder = {"i": 0}
        samples = []
        def fake_pair_collides(moved, static):
            # Track call order to map to samples
            samples.append(sample_holder["i"])
            return (True, "baseline contact", 1.0)
        # Baseline at sample=0 is allowed; non-zero samples must not
        # deepen. With constant vol=1.0 and INTERFERENCE_VOLUME_TOL_MM3
        # =5.0, vol (1.0) <= baseline (1.0) + tol (5.0) = 6.0, so passes.
        checks = self._run_qa(meshes, mate, pair_collides_fn=fake_pair_collides)
        self.assertTrue(checks)
        self.assertTrue(
            checks[0].passed,
            f"baseline contact revolute pair should pass: "
            f"{checks[0].detail}"
        )

    def test_direct_revolute_new_collision_at_nonzero_fails(self):
        """Counterexample: NO collision at sample=0 (baseline=0), but NEW
        collision at non-zero sample. The blanket skip used to false-pass
        this. Now: baseline comparison detects the new collision (vol > 0
        + tol is fine, but it's a NEW collision since baseline=0 and the
        pair wasn't colliding at sample=0).
        """
        a = _box((0, 0, -2), size=4.0)
        b = _box((0, 0, 2), size=4.0)
        meshes = {"a": [a], "b": [b]}
        mate = _revolute_mate("a", "b")
        # First call (baseline at sample 0): no collision.
        # Subsequent calls (non-zero samples): collision detected.
        call_count = {"n": 0}
        def fake_pair_collides(moved, static):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Baseline probe at sample 0: no collision.
                return (False, "no collision", 0.0)
            # Non-zero sample: NEW collision.
            return (True, "new collision in sweep", 10.0)
        checks = self._run_qa(meshes, mate, pair_collides_fn=fake_pair_collides)
        self.assertTrue(checks)
        self.assertFalse(
            checks[0].passed,
            f"new collision at non-zero sample must FAIL, got: "
            f"{checks[0].detail}"
        )

    def test_direct_revolute_deepened_collision_fails(self):
        """Baseline contact at sample 0 (small vol). At non-zero sample,
        penetration deepens beyond baseline + tolerance -> FAIL."""
        a = _box((0, 0, -2), size=4.0)
        b = _box((0, 0, 2), size=4.0)
        meshes = {"a": [a], "b": [b]}
        mate = _revolute_mate("a", "b")
        call_count = {"n": 0}
        def fake_pair_collides(moved, static):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Baseline: small contact
                return (True, "baseline contact", 1.0)
            # Non-zero: deepened penetration (vol > baseline + tol)
            return (True, "deepened", 100.0)
        checks = self._run_qa(meshes, mate, pair_collides_fn=fake_pair_collides)
        self.assertTrue(checks)
        self.assertFalse(
            checks[0].passed,
            f"deepened penetration must FAIL, got: {checks[0].detail}"
        )


if __name__ == "__main__":
    unittest.main()
