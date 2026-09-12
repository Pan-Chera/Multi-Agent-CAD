"""BUG-043: a rigid SELECTOR mate with no target_x/y/z_mm used to
auto-pass with "joint-enforced; no target to verify". On a multi-bore part
this hides a wrong-bore pick -- the resolver chose one of N cylinders, QA
had no signal to flag ambiguity. Fix: the resolver exposes a candidate
count; QA passes only when the candidate is unique, otherwise marks
UNVERIFIABLE so the router can remate.

No external LLM calls; STEP files are built with build123d.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from build123d import Align, Box, Cylinder, Pos, export_step

from mac_assembly.assembly_qa import _check_mates
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
    PartResult,
    PartSpec,
)


def _single_bore_step(path: Path):
    """One plate with one bore -- unique cylinder candidate."""
    plate = Pos(0, 0, 5) * Box(40, 40, 10, align=(Align.CENTER,) * 3)
    hole = Pos(0, 0, 5) * Cylinder(
        radius=5, height=12, align=(Align.CENTER,) * 3
    )
    export_step(plate - hole, str(path))


def _double_bore_step(path: Path):
    """One plate with TWO same-radius bores -- ambiguous without a target."""
    plate = Pos(0, 0, 5) * Box(80, 40, 10, align=(Align.CENTER,) * 3)
    hole_a = Pos(-20, 0, 5) * Cylinder(
        radius=5, height=12, align=(Align.CENTER,) * 3
    )
    hole_b = Pos(20, 0, 5) * Cylinder(
        radius=5, height=12, align=(Align.CENTER,) * 3
    )
    export_step(plate - hole_a - hole_b, str(path))


def _rigid_mate(fixed_id, moving_id, with_target=False):
    query = FaceQuery(surface="cylinder", axis="z")
    if with_target:
        query = FaceQuery(
            surface="cylinder", axis="z",
            target_x_mm=20.0,  # picks the +X bore
        )
    return MateSpec(
        mate_id="m1",
        mate_type=MateType.RIGID,
        fixed_part_id=fixed_id,
        moving_part_id=moving_id,
        fixed_anchor=Anchor(
            kind=AnchorKind.SELECTOR, selector_query=query,
        ),
        moving_anchor=Anchor(
            kind=AnchorKind.SELECTOR, selector_query=query,
        ),
    )


def _brief_and_plan(fixed_id, moving_id, with_target=False):
    brief = AssemblyBrief(
        assembly_name="t",
        parts=[
            PartSpec(part_id=fixed_id, part_name=fixed_id, description=""),
            PartSpec(part_id=moving_id, part_name=moving_id, description=""),
        ],
        interfaces=[
            FunctionalInterface(
                interface_id="itf", part_a=fixed_id, part_b=moving_id,
                interface_type=InterfaceType.FIXED, description="",
            ),
        ],
        user_request_raw="r",
    )
    plan = MatingPlan(
        assembly_name="t",
        mates=[_rigid_mate(fixed_id, moving_id, with_target=with_target)],
    )
    return brief, plan


def _result(step_path):
    return PartResult(
        part_id="x", step_path=str(step_path), stl_path="", py_path="",
        part_dir=str(step_path.parent), ok=True, attempts=1, token_usage={},
    )


class TestRigidSelectorUniqueness(unittest.TestCase):
    def _run_qa(self, fixed_step, moving_step, with_target=False):
        brief, plan = _brief_and_plan("fixed", "moving", with_target=with_target)
        part_results = {
            "fixed": _result(fixed_step),
            "moving": _result(moving_step),
        }
        # Minimal placed-bbox list (label, bmin, bmax). For a rigid mate
        # the moving part is coincident with the fixed part.
        placed = [
            ("fixed", (-40.0, -40.0, 0.0), (40.0, 40.0, 10.0)),
            ("moving", (-40.0, -40.0, 0.0), (40.0, 40.0, 10.0)),
        ]
        step_paths = {
            "fixed": str(fixed_step),
            "moving": str(moving_step),
        }
        # Moving part carries an identity location so the verifiable-
        # placement gate passes (rigid = no rotation).
        locations = {
            "moving": {
                "translation": [0.0, 0.0, 0.0],
                "rotation_euler_xyz_deg": [0.0, 0.0, 0.0],
            },
        }
        return _check_mates(
            plan.mates, placed,
            meshes_by_label=None, step_paths=step_paths,
            locations=locations,
        )

    def test_single_bore_rigid_no_target_passes(self):
        """Unique candidate + no target -> joint-enforced PASS is correct."""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "f.step"; _single_bore_step(f)
            m = Path(tmp) / "m.step"; _single_bore_step(m)
            checks = self._run_qa(f, m, with_target=False)
        self.assertTrue(checks)
        self.assertTrue(
            checks[0].passed,
            f"unique-bore rigid mate must pass (joint-enforced), got: "
            f"{checks[0].detail}"
        )

    def test_double_bore_rigid_no_target_unverifiable(self):
        """Moving part has two same-radius bores + no target -> resolver
        picked one arbitrarily; QA must mark UNVERIFIABLE so router can
        remate. The ambiguity is on the MOVING anchor (per BUG-043
        scope)."""
        with tempfile.TemporaryDirectory() as tmp:
            # Single bore is FIXED, double bore is MOVING (multi-candidate).
            f = Path(tmp) / "f.step"; _single_bore_step(f)
            m = Path(tmp) / "m.step"; _double_bore_step(m)
            checks = self._run_qa(f, m, with_target=False)
        self.assertTrue(checks)
        self.assertFalse(
            checks[0].passed,
            f"ambiguous-bore rigid mate must NOT silently pass, got: "
            f"{checks[0].detail}",
        )
        self.assertIn("UNVERIFIABLE", checks[0].detail.upper())

    def test_double_bore_rigid_with_target_keeps_target_check(self):
        """With target_x_mm on the moving anchor, the existing resolved-
        point-vs-target check stays authoritative (BUG-043 only changes
        the no-target path; target disambiguation behavior is unchanged).
        The check runs and produces a non-silent verdict."""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "f.step"; _single_bore_step(f)
            m = Path(tmp) / "m.step"; _double_bore_step(m)
            checks = self._run_qa(f, m, with_target=True)
        self.assertTrue(checks)
        # Verdict is target-driven, not the no-target auto-pass. Either
        # the target matches (pass) or it doesn't (fail); both are
        # acceptable. What's NOT acceptable is the no-target "joint-
        # enforced; no target to verify" detail.
        self.assertNotIn(
            "no target to verify", checks[0].detail.lower(),
            f"with target, the target check must run, not auto-pass, "
            f"got: {checks[0].detail}",
        )


if __name__ == "__main__":
    unittest.main()
