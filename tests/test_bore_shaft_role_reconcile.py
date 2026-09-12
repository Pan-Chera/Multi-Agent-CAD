"""BUG-002: R6 shaft/bore radius compatibility check used `abs(fixed_r -
moving_r)` which is directionless -- an interference fit (shaft larger than
bore by 0-2mm) passed as a valid clearance fit. The fix adds topology-aware
role detection (inner bore vs outer shaft) via the cadpy face normal vs the
radial direction at the face center.

No external LLM calls; STEP files are built with build123d, topology is read
via cadpy's SelectorIndex (already a project dependency).
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
    InterfaceType,
    FunctionalInterface,
    MateSpec,
    MateType,
    MatingPlan,
    PartResult,
    PartSpec,
)


def _shaft_step(path: Path, radius: float, height: float = 10.0):
    """A solid cylinder along Z: its curved face is an OUTER shaft."""
    shape = Cylinder(radius=radius, height=height, align=(Align.CENTER,) * 3)
    shape = Pos(0, 0, height / 2.0) * shape
    export_step(shape, str(path))


def _bore_step(path: Path, radius: float, plate: float = 20.0, thick: float = 6.0):
    """A plate with a through-bore along Z: the curved face inside the hole
    is an INNER bore."""
    plate = Box(plate, plate, thick, align=(Align.CENTER,) * 3)
    plate = Pos(0, 0, thick / 2.0) * plate
    hole = Cylinder(radius=radius, height=thick + 2, align=(Align.CENTER,) * 3)
    hole = Pos(0, 0, thick / 2.0) * hole
    export_step(plate - hole, str(path))


def _brief_and_plan(fixed_id: str, moving_id: str, axis: str = "z") -> tuple:
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
                    selector_query=FaceQuery(surface="cylinder", axis=axis),
                ),
                moving_anchor=Anchor(
                    kind=AnchorKind.SELECTOR,
                    selector_query=FaceQuery(surface="cylinder", axis=axis),
                ),
            ),
        ],
    )
    return brief, plan


def _result(step_path: Path) -> PartResult:
    return PartResult(
        part_id="x", step_path=str(step_path), stl_path="", py_path="",
        part_dir=str(step_path.parent), ok=True, attempts=1, token_usage={},
    )


class TestCylinderRoleDetection(unittest.TestCase):
    """Topology-aware role classification: outer shaft vs inner bore."""

    def test_outer_shaft_role(self):
        from mac_assembly.selector_resolver import cylinder_radii_with_role_along
        with tempfile.TemporaryDirectory() as tmp:
            step = Path(tmp) / "shaft.step"
            _shaft_step(step, radius=5.0)
            roles = cylinder_radii_with_role_along(str(step), "z")
            self.assertTrue(roles, "expected at least one cylinder face")
            for r, role in roles:
                self.assertEqual(role, "outer",
                                  f"shaft cylinder must be 'outer', got {role} for R={r}")

    def test_inner_bore_role(self):
        from mac_assembly.selector_resolver import cylinder_radii_with_role_along
        with tempfile.TemporaryDirectory() as tmp:
            step = Path(tmp) / "bore.step"
            _bore_step(step, radius=5.0)
            roles = cylinder_radii_with_role_along(str(step), "z")
            self.assertTrue(roles, "expected at least one cylinder face")
            for r, role in roles:
                self.assertEqual(role, "inner",
                                  f"bore cylinder must be 'inner', got {role} for R={r}")


class TestReconcileRadiusDirectionalCompatibility(unittest.TestCase):
    """R6 must distinguish clearance fit from interference fit by role + direction."""

    def _run(self, fixed_step: Path, moving_step: Path) -> "ReconcileResult":
        from mac_assembly.assembly_qa import ReconcileResult  # noqa: F401
        brief, plan = _brief_and_plan("fixed", "moving")
        part_results = {
            "fixed": _result(fixed_step),
            "moving": _result(moving_step),
        }
        # Override part ids in the results so reconcile finds them
        part_results["fixed"] = part_results["fixed"].model_copy(
            update={"part_id": "fixed"}
        )
        part_results["moving"] = part_results["moving"].model_copy(
            update={"part_id": "moving"}
        )
        return reconcile_dimensions(brief, plan.mates, part_results)

    def test_bore_larger_than_shaft_passes(self):
        """Clearance fit: bore R=5.2 + shaft R=5 -> 0.2mm clearance."""
        with tempfile.TemporaryDirectory() as tmp:
            fixed = Path(tmp) / "bore.step"; _bore_step(fixed, radius=5.2)
            moving = Path(tmp) / "shaft.step"; _shaft_step(moving, radius=5.0)
            r = self._run(fixed, moving)
        self.assertEqual(r.errors, [],
                         f"clearance fit must pass, got errors: {r.errors}")

    def test_shaft_larger_than_bore_fails(self):
        """Interference fit: bore R=5 + shaft R=5.2 -> shaft bigger -> FAIL."""
        with tempfile.TemporaryDirectory() as tmp:
            fixed = Path(tmp) / "bore.step"; _bore_step(fixed, radius=5.0)
            moving = Path(tmp) / "shaft.step"; _shaft_step(moving, radius=5.2)
            r = self._run(fixed, moving)
        # Must be a definite (blocking) incompatibility, not a UNVERIFIABLE
        # warning. The error string carries "radii incompatible".
        self.assertTrue(any("radii incompatible" in e for e in r.errors),
                        f"interference fit must be flagged incompatible: {r.errors}")

    def test_exact_fit_fails(self):
        """Bore R=5 + shaft R=5 -> 0 clearance, no rotation room -> FAIL."""
        with tempfile.TemporaryDirectory() as tmp:
            fixed = Path(tmp) / "bore.step"; _bore_step(fixed, radius=5.0)
            moving = Path(tmp) / "shaft.step"; _shaft_step(moving, radius=5.0)
            r = self._run(fixed, moving)
        self.assertTrue(any("radii incompatible" in e for e in r.errors),
                        f"exact fit must be flagged incompatible: {r.errors}")

    def test_two_outer_shafts_fails(self):
        """Two shafts (outer+outer) cannot form a revolute joint."""
        with tempfile.TemporaryDirectory() as tmp:
            fixed = Path(tmp) / "shaft1.step"; _shaft_step(fixed, radius=5.0)
            moving = Path(tmp) / "shaft2.step"; _shaft_step(moving, radius=4.8)
            r = self._run(fixed, moving)
        self.assertTrue(any("radii incompatible" in e for e in r.errors),
                        f"outer+outer must be flagged incompatible: {r.errors}")


if __name__ == "__main__":
    unittest.main()
