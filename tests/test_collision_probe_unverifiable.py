"""BUG-004: _pair_collides used to swallow internal trimesh exceptions and
return collided=False, which the caller interpreted as a verified no-collision
(false PASS). The probe must be three-state: collided / no-collision /
UNVERIFIABLE, and UNVERIFIABLE must fail the QA report (passed=False) with a
detail string naming the failed stage.

No external LLM calls; trimesh internals are monkeypatched.
"""
from __future__ import annotations

import unittest
from unittest import mock

import trimesh


def _two_disjoint_boxes():
    """Two non-overlapping unit boxes; baseline no-collision case."""
    a = trimesh.creation.box(extents=(2, 2, 2))
    b = trimesh.creation.box(extents=(2, 2, 2))
    b.apply_translation([10, 0, 0])
    return a, b


def _two_overlapping_boxes():
    """Two boxes overlapping by a large volume; baseline collision case."""
    a = trimesh.creation.box(extents=(4, 4, 4))
    b = trimesh.creation.box(extents=(4, 4, 4))
    b.apply_translation([1, 0, 0])
    return a, b


class TestPairCollidesThreeState(unittest.TestCase):
    def _qa_with_pair(self, mesh_a, mesh_b):
        """Drive _check_interference with a single labeled pair."""
        from mac_assembly import assembly_qa as qa

        comp_labels = [("part_a", mesh_a), ("part_b", mesh_b)]
        return qa._check_interference(comp_labels, mates=None)

    def test_normal_no_collision_passes(self):
        a, b = _two_disjoint_boxes()
        checks = self._qa_with_pair(a, b)
        # Find our pair (skip the "*" sentinel that may appear on trimesh
        # missing -- in our environment trimesh IS available).
        pair = [c for c in checks if c.part_a == "part_a" and c.part_b == "part_b"]
        self.assertTrue(pair, f"expected a check for part_a/part_b, got {checks}")
        self.assertTrue(pair[0].passed, f"disjoint boxes must pass: {pair[0].detail}")

    def test_normal_collision_fails(self):
        a, b = _two_overlapping_boxes()
        checks = self._qa_with_pair(a, b)
        pair = [c for c in checks if c.part_a == "part_a" and c.part_b == "part_b"]
        self.assertTrue(pair)
        self.assertFalse(pair[0].passed, "overlapping boxes must fail")

    def test_sampling_exception_is_unverifiable(self):
        # Use AABB-overlapping boxes so the broad-phase does NOT skip the
        # pair -- _pair_collides must actually be invoked.
        a, b = _two_overlapping_boxes()
        with mock.patch(
            "mac_assembly.assembly_qa._sample_surface",
            side_effect=RuntimeError("simulated sampling crash"),
        ):
            checks = self._qa_with_pair(a, b)
        pair = [c for c in checks if c.part_a == "part_a" and c.part_b == "part_b"]
        self.assertTrue(pair)
        self.assertFalse(
            pair[0].passed,
            "sampling exception must NOT silently PASS -- fail closed",
        )
        self.assertIn("UNVERIFIABLE", pair[0].detail.upper())

    def test_contains_exception_is_unverifiable(self):
        # AABB-overlapping boxes so _pair_collides is reached; then break
        # Mesh.contains (the ray-parity containment test called inside
        # _deep_penetration_frac) to simulate a trimesh internal failure.
        a, b = _two_overlapping_boxes()
        with mock.patch.object(
            type(a), "contains", side_effect=RuntimeError("simulated contains crash")
        ):
            checks = self._qa_with_pair(a, b)
        pair = [c for c in checks if c.part_a == "part_a" and c.part_b == "part_b"]
        self.assertTrue(pair)
        self.assertFalse(pair[0].passed)
        self.assertIn("UNVERIFIABLE", pair[0].detail.upper())


if __name__ == "__main__":
    unittest.main()
