"""BUG-002 (P0 revision): reconcile_dimensions used to compare ALL
cylinder pairs along the joint axis on each part. A part with the
joint's R5 bore PLUS an unrelated R6 bore would pair the R6 (treating
it as a bore) with an R5.5 shaft -- 0.5mm "clearance" -- and false-pass
the interference fit on the actual R5 joint feature.

Fix: when an anchor has a cylinder SELECTOR query, use the
SELECTOR-resolved specific cylinder (with its classified role) for that
side, instead of falling back to all cylinders along the axis.

No external LLM calls; STEP files built with build123d.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from build123d import Align, Box, Cylinder, Pos, export_step

from mac_assembly.assembly_qa import reconcile_dimensions
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
    PartResult,
    PartSpec,
)


def _plate_with_r5_bore_and_r6_bore_step(path: Path):
    """40x40x6 plate with a R5 through-bore at origin (the joint feature)
    AND an unrelated R6 through-bore at (+15, 0) -- a counter-example
    part that exposes the any-pair false-pass."""
    plate = Pos(0, 0, 3) * Box(40, 40, 6, align=(Align.CENTER,) * 3)
    r5_hole = Pos(0, 0, 3) * Cylinder(
        radius=5.0, height=8, align=(Align.CENTER,) * 3
    )
    r6_hole = Pos(15, 0, 3) * Cylinder(
        radius=6.0, height=8, align=(Align.CENTER,) * 3
    )
    export_step(plate - r5_hole - r6_hole, str(path))


def _shaft_step(path: Path, radius: float, height: float = 10.0):
    """A solid cylinder along Z: outer shaft."""
    shape = Cylinder(radius=radius, height=height, align=(Align.CENTER,) * 3)
    shape = Pos(0, 0, height / 2.0) * shape
    export_step(shape, str(path))


def _brief_and_plan(
    fixed_id: str,
    moving_id: str,
    fixed_query: FaceQuery,
    moving_query: FaceQuery,
    axis: str = "z",
) -> tuple:
    brief = AssemblyBrief(
        assembly_name="t",
        parts=[
            PartSpec(part_id=fixed_id, part_name=fixed_id, description=""),
            PartSpec(part_id=moving_id, part_name=moving_id, description=""),
        ],
        interfaces=[
            FunctionalInterface(
                interface_id="itf", part_a=fixed_id, part_b=moving_id,
                interface_type=InterfaceType.HINGE, description="",
            ),
        ],
        user_request_raw="r",
    )
    plan = MatingPlan(
        assembly_name="t",
        mates=[
            MateSpec(
                mate_id="m1",
                mate_type=MateType.REVOLUTE,
                fixed_part_id=fixed_id,
                moving_part_id=moving_id,
                fixed_anchor=Anchor(
                    kind=AnchorKind.SELECTOR,
                    selector_query=fixed_query,
                ),
                moving_anchor=Anchor(
                    kind=AnchorKind.SELECTOR,
                    selector_query=moving_query,
                ),
            ),
        ],
    )
    return brief, plan


def _result(part_id: str, step_path: Path) -> PartResult:
    return PartResult(
        part_id=part_id, step_path=str(step_path), stl_path="", py_path="",
        part_dir=str(step_path.parent), ok=True, attempts=1, token_usage={},
    )


class TestReconcileSelectorResolvedCylinder(unittest.TestCase):
    """reconcile must compare the SELECTOR-resolved cylinder, not any
    pair on the part."""

    def _run(self, fixed_step: Path, moving_step: Path,
             fixed_query: FaceQuery, moving_query: FaceQuery):
        brief, plan = _brief_and_plan(
            "fixed", "moving", fixed_query, moving_query
        )
        part_results = {
            "fixed": _result("fixed", fixed_step),
            "moving": _result("moving", moving_step),
        }
        return reconcile_dimensions(brief, plan.mates, part_results)

    def test_r5_bore_with_unrelated_r6_plus_r55_shaft_interference(self):
        """Counterexample: fixed part has a R5 bore (the joint feature)
        + an unrelated R6 bore. Moving part is a R5.5 shaft. The R5
        joint feature is an interference fit (5.5 > 5). The previous
        any-pair code paired the R6 (as bore) with R5.5 (as shaft) ->
        0.5mm "clearance" -> false PASS. Now: the SELECTOR query
        closest_to R5 resolves to the R5 bore (the joint feature), and
        the R5.5 shaft vs R5 bore is a blocking interference."""
        with tempfile.TemporaryDirectory() as tmp:
            fixed = Path(tmp) / "plate.step"
            _plate_with_r5_bore_and_r6_bore_step(fixed)
            moving = Path(tmp) / "shaft.step"
            _shaft_step(moving, radius=5.5)
            r = self._run(
                fixed, moving,
                FaceQuery(surface="cylinder", axis="z",
                          select="closest_to", value_mm=5.0),
                FaceQuery(surface="cylinder", axis="z"),
            )
        self.assertTrue(
            any("radii incompatible" in e for e in r.errors),
            f"R5 bore + R5.5 shaft = interference (must FAIL), got: "
            f"{r.errors}"
        )

    def test_r6_bore_with_r55_shaft_clearance_passes(self):
        """Same geometry, but the SELECTOR closest_to R6 resolves to the
        R6 bore (the unrelated one) -- which IS a 0.5mm clearance fit
        with the R5.5 shaft. This MUST pass to confirm the fix isn't
        over-rejecting legitimate fits."""
        with tempfile.TemporaryDirectory() as tmp:
            fixed = Path(tmp) / "plate.step"
            _plate_with_r5_bore_and_r6_bore_step(fixed)
            moving = Path(tmp) / "shaft.step"
            _shaft_step(moving, radius=5.5)
            r = self._run(
                fixed, moving,
                FaceQuery(surface="cylinder", axis="z",
                          select="closest_to", value_mm=6.0),
                FaceQuery(surface="cylinder", axis="z"),
            )
        self.assertEqual(
            r.errors, [],
            f"R6 bore + R5.5 shaft = 0.5mm clearance (must PASS), got: "
            f"{r.errors}"
        )

    def test_r5_bore_with_r49_shaft_clearance_passes(self):
        """R5 bore + R4.9 shaft = 0.1mm clearance. Must PASS."""
        with tempfile.TemporaryDirectory() as tmp:
            fixed = Path(tmp) / "plate.step"
            _plate_with_r5_bore_and_r6_bore_step(fixed)
            moving = Path(tmp) / "shaft.step"
            _shaft_step(moving, radius=4.9)
            r = self._run(
                fixed, moving,
                FaceQuery(surface="cylinder", axis="z",
                          select="closest_to", value_mm=5.0),
                FaceQuery(surface="cylinder", axis="z"),
            )
        self.assertEqual(
            r.errors, [],
            f"R5 bore + R4.9 shaft = 0.1mm clearance (must PASS), got: "
            f"{r.errors}"
        )


if __name__ == "__main__":
    unittest.main()
