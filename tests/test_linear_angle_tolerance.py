"""BUG-013 (revised): the previous `abs(angle_deg % 360) > 1e-6` check
misclassified small negative noise. In Python, `-1e-7 % 360 == 359.9999999`
(not 0), so a near-zero negative angle was treated as a non-zero angle
and rejected.

Fix: use `math.remainder(angle, 360.0)` which returns the SIGNED distance
to the nearest 360-multiple (so -1e-7 -> -1e-7, not 359.9999999).

No external LLM calls; pure validation test.
"""
from __future__ import annotations

import math
import unittest

from mac_assembly.nodes_assembly import _validate_mating_plan
from mac_assembly.schemas_assembly import (
    Anchor,
    AnchorKind,
    AssemblyBrief,
    FaceQuery,
    InterfaceType,
    FunctionalInterface,
    MateSpec,
    MateType,
    MatingPlan,
    PartSpec,
)


def _brief():
    return AssemblyBrief(
        assembly_name="t",
        parts=[
            PartSpec(part_id="a", part_name="a", description=""),
            PartSpec(part_id="b", part_name="b", description=""),
        ],
        interfaces=[
            FunctionalInterface(
                interface_id="itf", part_a="a", part_b="b",
                interface_type=InterfaceType.SLIDE, description="",
            ),
        ],
        user_request_raw="r",
    )


def _linear_mate(angle_deg: float, slide_axis: str = "x"):
    return MateSpec(
        mate_id="m1",
        mate_type=MateType.LINEAR,
        fixed_part_id="a",
        moving_part_id="b",
        fixed_anchor=Anchor(kind=AnchorKind.AXIS_POINT, axis=slide_axis,
                             offset_mm=0.0),
        moving_anchor=Anchor(kind=AnchorKind.AXIS_POINT, axis=slide_axis,
                             offset_mm=0.0),
        position_mm=0.0,
        slide_axis=slide_axis,
        angle_deg=angle_deg,
    )


class TestLinearAngleTolerance(unittest.TestCase):
    def _errors(self, angle_deg: float) -> list[str]:
        plan = MatingPlan(assembly_name="t", mates=[_linear_mate(angle_deg)])
        brief = _brief()
        return _validate_mating_plan(plan, brief)

    def _is_rejected(self, angle_deg: float) -> bool:
        errs = self._errors(angle_deg)
        return any("angle_deg" in e or "linear mates cannot" in e
                   for e in errs)

    def test_zero_ok(self):
        self.assertFalse(self._is_rejected(0.0))

    def test_positive_360_ok(self):
        self.assertFalse(self._is_rejected(360.0))

    def test_negative_360_ok(self):
        self.assertFalse(self._is_rejected(-360.0))

    def test_small_negative_noise_ok(self):
        """-1e-7 should be treated as 0 (within tolerance), not 359.99."""
        self.assertFalse(self._is_rejected(-1e-7))

    def test_small_positive_just_under_360_ok(self):
        """359.9999999 should be treated as 360 (within tolerance)."""
        self.assertFalse(self._is_rejected(359.9999999))

    def test_small_negative_just_under_negative_360_ok(self):
        """-360.0000001 should be treated as -360 (within tolerance)."""
        self.assertFalse(self._is_rejected(-360.0000001))

    def test_one_degree_rejected(self):
        """1 degree is a real angle, must be rejected for LINEAR."""
        self.assertTrue(self._is_rejected(1.0))


if __name__ == "__main__":
    unittest.main()
