"""Fix: _validate_mating_plan rejected large-offset axis_point anchors on
revolute/linear/coaxial/cylindrical mates ("requires both anchors to define
a principal axis ... got fixed=None"), even though the mating prompt's own
guidance tells the architect LLM to put datums at joint ends ("axis_point
offset = half the relevant extent") and its canonical revolute example uses
offsets 12.5 and -30.0. The <=1mm filter inside codegen's _get_anchor_axis
is an override heuristic for postprocess_axial_offsets (None there means
"leave the LLM's axial_offset_mm alone"), not an axis-validity rule.
Reusing it as a hard gate killed the telescopic-crane run on 2026-09-09:
both internal attempts failed on the same two mates, and the graph ENDs on
a first-pass mating failure.

No external LLM calls: _validate_mating_plan is deterministic.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from mac_assembly.assembly_codegen import generate_assembly_script
from mac_assembly.nodes_assembly import _anchor_axis_for_validation, _validate_mating_plan
from mac_assembly.schemas_assembly import (
    Anchor,
    AnchorKind,
    AssemblyBrief,
    FaceQuery,
    FunctionalInterface,
    InterfaceType,
    MateSpec,
    MateType,
    MatingPlan,
    PartSpec,
)


def _brief() -> AssemblyBrief:
    return AssemblyBrief(
        assembly_name="t",
        parts=[
            PartSpec(part_id="base", part_name="base", description="base"),
            PartSpec(part_id="boom", part_name="boom", description="boom"),
            PartSpec(part_id="carrier", part_name="carrier", description="carrier"),
        ],
        interfaces=[
            FunctionalInterface(
                interface_id="itf_base_boom", part_a="base", part_b="boom",
                interface_type=InterfaceType.HINGE, description="hinge",
            ),
            FunctionalInterface(
                interface_id="itf_boom_carrier", part_a="boom", part_b="carrier",
                interface_type=InterfaceType.SLIDE, description="slide",
            ),
        ],
        user_request_raw="r",
    )


def _plan(mates: list[MateSpec]) -> MatingPlan:
    return MatingPlan(assembly_name="t", mates=mates)


def _axis_point(axis: str, offset: float) -> Anchor:
    return Anchor(kind=AnchorKind.AXIS_POINT, axis=axis, offset_mm=offset)


def _plane_selector(axis: str) -> Anchor:
    return Anchor(kind=AnchorKind.SELECTOR, selector_query=FaceQuery(
        surface="plane", axis=axis))


def _cylinder_selector(axis: str, radius: float = 7.5) -> Anchor:
    return Anchor(kind=AnchorKind.SELECTOR, selector_query=FaceQuery(
        surface="cylinder", axis=axis, select="closest_to", value_mm=radius))


class TestAnchorAxisForValidation(unittest.TestCase):
    """The validation-specific axis resolver: axis_point counts at any offset."""

    def test_axis_point_any_offset_names_its_axis(self):
        self.assertEqual(_anchor_axis_for_validation(_axis_point("x", 210.0)), "x")
        self.assertEqual(_anchor_axis_for_validation(_axis_point("z", 12.5)), "z")
        self.assertEqual(_anchor_axis_for_validation(_axis_point("y", 0.0)), "y")

    def test_cylinder_selector_axis_still_resolved(self):
        a = Anchor(kind=AnchorKind.SELECTOR, selector_query=FaceQuery(
            surface="cylinder", axis="z"))
        self.assertEqual(_anchor_axis_for_validation(a), "z")

    def test_plane_selector_resolves_to_normal_axis(self):
        # Turntable pattern: a plane selector's normal is a valid hinge
        # axis (the resolver returns it as the SELECTOR axis direction).
        self.assertEqual(_anchor_axis_for_validation(_plane_selector("z")), "z")

    def test_face_anchor_normal_axis(self):
        a = Anchor(kind=AnchorKind.FACE, face="top")
        self.assertEqual(_anchor_axis_for_validation(a), "z")


class TestValidateMatingPlanAxisRule(unittest.TestCase):
    """The crane failure signature passes; genuine axis defects still fail."""

    def test_revolute_large_offset_axis_points_pass(self):
        # The mating prompt's canonical pattern: fixed datum at the joint
        # end of a long part (offset = half the extent), moving datum at
        # the compact part's bbox centre.
        plan = _plan([
            MateSpec(
                mate_id="boom_hinge", mate_type=MateType.REVOLUTE,
                fixed_part_id="base", moving_part_id="boom",
                fixed_anchor=_axis_point("z", 12.5),
                moving_anchor=_axis_point("z", -30.0),
            ),
            MateSpec(
                mate_id="carrier_slide", mate_type=MateType.LINEAR,
                fixed_part_id="boom", moving_part_id="carrier",
                fixed_anchor=_axis_point("x", 0.0),
                moving_anchor=_axis_point("x", 0.0),
                slide_axis="x",
            ),
        ])
        errors = _validate_mating_plan(plan, _brief())
        self.assertEqual(errors, [])

    def test_linear_large_offset_axis_points_pass(self):
        # The telescope_slide failure from the crane run: the boom-side
        # datum sits at the truss end (large offset), the carrier-side
        # datum at its centre.
        plan = _plan([
            MateSpec(
                mate_id="boom_hinge", mate_type=MateType.REVOLUTE,
                fixed_part_id="base", moving_part_id="boom",
                fixed_anchor=_axis_point("z", 0.0),
                moving_anchor=_axis_point("z", 0.0),
            ),
            MateSpec(
                mate_id="carrier_slide", mate_type=MateType.LINEAR,
                fixed_part_id="boom", moving_part_id="carrier",
                fixed_anchor=_axis_point("x", 210.0),
                moving_anchor=_axis_point("x", 0.0),
                slide_axis="x",
            ),
        ])
        errors = _validate_mating_plan(plan, _brief())
        self.assertEqual(errors, [])

    def test_turntable_revolute_on_plane_selector_passes(self):
        # azimuth_rotor revolutes about Z on the pedestal's top plane --
        # the pattern the mating LLM wrote twice on the crane run
        # (azimuth_joint_mate / gimbal_pan_mate, rejected as fixed=None).
        plan = _plan([
            MateSpec(
                mate_id="boom_hinge", mate_type=MateType.REVOLUTE,
                fixed_part_id="base", moving_part_id="boom",
                fixed_anchor=_plane_selector("z"),
                moving_anchor=_axis_point("z", 0.0),
            ),
            MateSpec(
                mate_id="carrier_slide", mate_type=MateType.LINEAR,
                fixed_part_id="boom", moving_part_id="carrier",
                fixed_anchor=_axis_point("x", 0.0),
                moving_anchor=_axis_point("x", 0.0),
                slide_axis="x",
            ),
        ])
        errors = _validate_mating_plan(plan, _brief())
        self.assertEqual(errors, [])

    def test_anchor_axis_mismatch_still_rejected(self):
        plan = _plan([
            MateSpec(
                mate_id="boom_hinge", mate_type=MateType.REVOLUTE,
                fixed_part_id="base", moving_part_id="boom",
                fixed_anchor=_axis_point("x", 12.5),
                moving_anchor=_axis_point("z", 0.0),
            ),
            MateSpec(
                mate_id="carrier_slide", mate_type=MateType.LINEAR,
                fixed_part_id="boom", moving_part_id="carrier",
                fixed_anchor=_axis_point("x", 0.0),
                moving_anchor=_axis_point("x", 0.0),
                slide_axis="x",
            ),
        ])
        errors = _validate_mating_plan(plan, _brief())
        self.assertTrue(any("anchor axes mismatch" in e for e in errors), errors)


class TestRevolutePrecompUsesAnchorAxisLetter(unittest.TestCase):
    """Codegen precompensation keys off the anchor's axis LETTER, not the
    postprocess heuristic: a large-offset axis_point hinge about X must
    pick up the X precomp (Rotation(0, 90, 180)) -- the old
    _get_anchor_axis call returned None (|offset|>1) and fell back to the
    identity 'z' precomp, leaving build123d's implicit reframe uncancelled
    and the part lying sideways."""

    def test_x_axis_large_offset_revolute_gets_x_precomp(self):
        mate = MateSpec(
            mate_id="boom_hinge", mate_type=MateType.REVOLUTE,
            fixed_part_id="base", moving_part_id="boom",
            fixed_anchor=_axis_point("x", 45.0),
            moving_anchor=_axis_point("x", 0.0),
        )
        src = generate_assembly_script(_brief(), [mate], Path("."))
        self.assertIn("Rotation(0.0, 90.0, 180.0)", src)

    def test_y_cylinder_selector_pair_gets_y_precomp(self):
        mate = MateSpec(
            mate_id="boom_hinge", mate_type=MateType.REVOLUTE,
            fixed_part_id="base", moving_part_id="boom",
            fixed_anchor=_cylinder_selector("y"),
            moving_anchor=_cylinder_selector("y"),
        )
        src = generate_assembly_script(_brief(), [mate], Path("."))
        self.assertIn("Rotation(-90.0, 0.0, -90.0)", src)


if __name__ == "__main__":
    unittest.main()
