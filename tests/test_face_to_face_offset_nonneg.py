"""BUG-012: MateSpec.offset_mm description said ">=0 for stacking" but no
validator enforced it. A negative offset would physically mean the moving
part interpenetrates the fixed part by |offset| mm -- a face_to_face mate
with offset_mm=-2 produces 2mm penetration. Reject negative offset for
FACE_TO_FACE mates only; other mate types (coaxial/rigid) leave offset_mm
unused and axial_offset_mm is signed (so not constrained here).

No external LLM calls; pure schema validation.
"""
from __future__ import annotations

import unittest

from mac_assembly.schemas_assembly import (
    Anchor,
    AnchorKind,
    MateSpec,
    MateType,
)


def _face_to_face(offset_mm: float) -> MateSpec:
    return MateSpec(
        mate_id="m1",
        mate_type=MateType.FACE_TO_FACE,
        fixed_part_id="a",
        moving_part_id="b",
        fixed_anchor=Anchor(kind=AnchorKind.FACE, face="top"),
        moving_anchor=Anchor(kind=AnchorKind.FACE, face="bottom"),
        offset_mm=offset_mm,
    )


class TestFaceToFaceOffsetNonNeg(unittest.TestCase):
    def test_zero_offset_ok(self):
        m = _face_to_face(0.0)
        self.assertEqual(m.offset_mm, 0.0)

    def test_positive_offset_ok(self):
        m = _face_to_face(2.0)
        self.assertEqual(m.offset_mm, 2.0)

    def test_negative_offset_rejected(self):
        with self.assertRaises(Exception) as ctx:
            _face_to_face(-2.0)
        self.assertIn("offset_mm", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
