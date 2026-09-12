"""BUG-003: _check_interference used to skip every directly-mated pair
unconditionally (passed=True), trusting mate/kinematic QA to catch interface
problems. But mate delta checks anchor alignment, not volume penetration -- a
mate with a wrong axial_offset_mm could embed the moving part 50mm into the
fixed part and still pass mate delta. The blanket skip turned that into a
false PASS.

Fix: directly-mated pairs are still tested with the depth-tolerant
`_pair_collides`. Legitimate face-to-face contact / press-fit at the mate
interface is handled by the same INTERFERENCE_DEPTH_TOL_MM mechanism
(`contains` excludes boundary points, so touching = frac 0). The skip is
removed; no special mate-aware exemption is needed.

No external LLM calls; trimesh primitives only.
"""
from __future__ import annotations

import unittest

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


def _face_to_face_mate(fixed_id, moving_id):
    return MateSpec(
        mate_id="m1",
        mate_type=MateType.FACE_TO_FACE,
        fixed_part_id=fixed_id,
        moving_part_id=moving_id,
        fixed_anchor=Anchor(kind=AnchorKind.FACE, face="top"),
        moving_anchor=Anchor(kind=AnchorKind.FACE, face="bottom"),
        offset_mm=0.0,
    )


class TestDirectMateInterference(unittest.TestCase):
    def test_direct_mate_face_to_face_contact_passes(self):
        # Two boxes stacked: top of A at z=2, bottom of B at z=2 -> touching.
        a = _box((0, 0, 0), size=4.0)   # spans z=-2..2
        b = _box((0, 0, 4), size=4.0)  # spans z=2..6
        mate = _face_to_face_mate("a", "b")
        checks = qa._check_interference(
            [("a", a), ("b", b)], mates=[mate]
        )
        pair = [c for c in checks if {c.part_a, c.part_b} == {"a", "b"}]
        self.assertTrue(pair, f"expected a check for a/b, got {checks}")
        self.assertTrue(
            pair[0].passed,
            f"face-to-face contact at a mate interface must pass: {pair[0].detail}",
        )

    def test_direct_mate_deep_penetration_fails(self):
        # Two boxes claimed as face_to_face mates but actually overlapping
        # by 50% -- this is a mate placement bug (wrong axial_offset), not a
        # legitimate contact. The blanket skip used to mark this passed=True.
        a = _box((0, 0, 0), size=4.0)   # spans z=-2..2
        b = _box((0, 0, 1), size=4.0)  # spans z=-1..3 -- 3mm overlap with a
        mate = _face_to_face_mate("a", "b")
        checks = qa._check_interference(
            [("a", a), ("b", b)], mates=[mate]
        )
        pair = [c for c in checks if {c.part_a, c.part_b} == {"a", "b"}]
        self.assertTrue(pair)
        self.assertFalse(
            pair[0].passed,
            f"deep penetration at a direct mate interface must NOT silently pass: "
            f"{pair[0].detail}",
        )


if __name__ == "__main__":
    unittest.main()
