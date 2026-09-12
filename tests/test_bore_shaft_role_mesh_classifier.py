"""BUG-002 (revised): the previous bbox-based cylinder role classifier
systematically misclassified:
  - a 40x40 plate's external R5.2 boss as 'inner' (radius 5.2 < plate
    half-extent 20)
  - a thin-wall tube's inner R5.0 wall as 'outer' (radius 5.0 ≈ outer
    extent 5.3)
  - any off-origin cylinder (lateral offset != 0)

The fix replaces bbox comparison with mesh point-in-solid: for each
cylinder face, sample r-epsilon and r+epsilon points along several radial
directions and test mesh.contains(). r-eps inside + r+eps outside = outer
shaft; r-eps outside + r+eps inside = inner bore; inconsistent = unknown.

No external LLM calls; STEP files are built with build123d, mesh is loaded
via trimesh (a project pre-flight dependency).
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from build123d import (
    Align,
    Box,
    Cylinder,
    Pos,
    export_step,
)

from mac_assembly.selector_resolver import cylinder_radii_with_role_along


def _solid_shaft_step(path: Path, radius: float = 5.0, height: float = 10.0):
    """Solid cylinder along Z -- curved face is OUTER shaft."""
    shape = Cylinder(radius=radius, height=height, align=(Align.CENTER,) * 3)
    shape = Pos(0, 0, height / 2.0) * shape
    export_step(shape, str(path))


def _plate_with_hole_step(
    path: Path, plate_w: float = 40.0, plate_h: float = 10.0,
    bore_radius: float = 5.0, bore_offset=(0.0, 0.0),
):
    """Plate with a through-bore -- curved face inside the hole is INNER."""
    plate = Box(plate_w, plate_w, plate_h, align=(Align.CENTER,) * 3)
    plate = Pos(0, 0, plate_h / 2.0) * plate
    hole = Cylinder(
        radius=bore_radius, height=plate_h + 2,
        align=(Align.CENTER,) * 3,
    )
    hole = Pos(bore_offset[0], bore_offset[1], plate_h / 2.0) * hole
    export_step(plate - hole, str(path))


def _boss_on_plate_step(
    path: Path, plate_w: float = 40.0, plate_h: float = 10.0,
    boss_radius: float = 5.2, boss_height: float = 8.0,
    boss_offset=(0.0, 0.0),
):
    """External cylindrical boss on top of a plate -- curved face is OUTER
    shaft even though the plate's lateral extent (20) is much larger than
    the boss radius (5.2). The bbox classifier misclassified this as
    'inner'."""
    plate = Box(plate_w, plate_w, plate_h, align=(Align.CENTER,) * 3)
    plate = Pos(0, 0, plate_h / 2.0) * plate
    boss = Cylinder(
        radius=boss_radius, height=boss_height,
        align=(Align.CENTER,) * 3,
    )
    boss = Pos(boss_offset[0], boss_offset[1],
               plate_h + boss_height / 2.0) * boss
    export_step(plate + boss, str(path))


def _thin_walled_tube_step(
    path: Path, outer_radius: float = 5.3, inner_radius: float = 5.0,
    height: float = 10.0,
):
    """Thin-wall tube along Z: outer cylinder face = outer shaft; inner
    cylinder face (the bore) = inner. Wall = 0.3mm. The bbox classifier
    misclassified the inner R5.0 as 'outer' (5.0 ≈ max lateral 5.3)."""
    outer = Cylinder(
        radius=outer_radius, height=height, align=(Align.CENTER,) * 3
    )
    outer = Pos(0, 0, height / 2.0) * outer
    inner = Cylinder(
        radius=inner_radius, height=height + 2,
        align=(Align.CENTER,) * 3,
    )
    inner = Pos(0, 0, height / 2.0) * inner
    export_step(outer - inner, str(path))


def _offset_bore_step(path: Path, plate_w=40.0, plate_h=10.0, bore_r=5.0,
                     bore_offset=(15.0, 0.0)):
    """Off-origin through-bore -- still INNER (lateral offset doesn't
    change which side of the face the material is on)."""
    _plate_with_hole_step(
        path, plate_w=plate_w, plate_h=plate_h,
        bore_radius=bore_r, bore_offset=bore_offset,
    )


class TestCylinderRoleMeshClassifier(unittest.TestCase):
    """Mesh point-in-solid classification: covers the counterexamples the
    bbox classifier got wrong (BUG-002 revision)."""

    def test_solid_shaft_is_outer(self):
        with tempfile.TemporaryDirectory() as tmp:
            step = Path(tmp) / "shaft.step"
            _solid_shaft_step(step, radius=5.0)
            roles = cylinder_radii_with_role_along(str(step), "z")
        self.assertTrue(roles, "expected at least one cylinder")
        for r, role in roles:
            self.assertEqual(
                role, "outer",
                f"solid shaft R={r} must be 'outer', got {role!r}"
            )

    def test_centered_plate_hole_is_inner(self):
        with tempfile.TemporaryDirectory() as tmp:
            step = Path(tmp) / "plate.step"
            _plate_with_hole_step(step, bore_radius=5.0, bore_offset=(0, 0))
            roles = cylinder_radii_with_role_along(str(step), "z")
        self.assertTrue(roles)
        for r, role in roles:
            self.assertEqual(
                role, "inner",
                f"centered plate hole R={r} must be 'inner', got {role!r}"
            )

    def test_boss_on_plate_is_outer(self):
        """Counterexample: a R5.2 boss on a 40x40 plate is OUTER (material
        is INSIDE the cylinder footprint). The bbox classifier said
        'inner' because 5.2 < 20 (plate half-extent)."""
        with tempfile.TemporaryDirectory() as tmp:
            step = Path(tmp) / "boss.step"
            _boss_on_plate_step(
                step, plate_w=40.0, plate_h=10.0,
                boss_radius=5.2, boss_height=8.0,
                boss_offset=(0.0, 0.0),
            )
            roles = cylinder_radii_with_role_along(str(step), "z")
        self.assertTrue(roles, "expected at least one cylinder (boss)")
        for r, role in roles:
            self.assertEqual(
                role, "outer",
                f"boss on plate R={r} must be 'outer' (material inside), "
                f"got {role!r}"
            )

    def test_offcenter_boss_on_plate_is_outer(self):
        """Counterexample: an off-center boss (e.g. at +10,+10 on a 40x40
        plate) is still OUTER. Bbox classifier said 'inner' because the
        global bbox is centered on origin."""
        with tempfile.TemporaryDirectory() as tmp:
            step = Path(tmp) / "offset_boss.step"
            _boss_on_plate_step(
                step, plate_w=40.0, plate_h=10.0,
                boss_radius=5.2, boss_height=8.0,
                boss_offset=(10.0, 10.0),
            )
            roles = cylinder_radii_with_role_along(str(step), "z")
        self.assertTrue(roles)
        for r, role in roles:
            self.assertEqual(
                role, "outer",
                f"off-center boss R={r} must be 'outer', got {role!r}"
            )

    def test_thin_walled_tube_outer_and_inner(self):
        """Counterexample: thin-wall tube (outer R5.3, inner R5.0) must
        classify outer face as 'outer' and inner face as 'inner'. Bbox
        classifier said BOTH were 'outer' (5.0 ≈ 5.3 ≈ max lateral)."""
        with tempfile.TemporaryDirectory() as tmp:
            step = Path(tmp) / "tube.step"
            _thin_walled_tube_step(
                step, outer_radius=5.3, inner_radius=5.0, height=10.0
            )
            roles = cylinder_radii_with_role_along(str(step), "z")
        # Should have two cylinder faces: outer + inner.
        self.assertEqual(
            len(roles), 2,
            f"thin-wall tube should have 2 cylinder faces, got {roles}"
        )
        # Sort by radius descending: largest first = outer wall.
        roles_sorted = sorted(roles, key=lambda x: -x[0])
        self.assertEqual(
            roles_sorted[0][1], "outer",
            f"outer wall R={roles_sorted[0][0]} must be 'outer', "
            f"got {roles_sorted[0][1]!r}"
        )
        self.assertEqual(
            roles_sorted[1][1], "inner",
            f"inner wall R={roles_sorted[1][0]} must be 'inner', "
            f"got {roles_sorted[1][1]!r}"
        )

    def test_offset_bore_is_inner(self):
        """Off-origin through-bore is still INNER (material is outside
        the cylinder radius)."""
        with tempfile.TemporaryDirectory() as tmp:
            step = Path(tmp) / "offset_bore.step"
            _offset_bore_step(
                step, plate_w=40.0, plate_h=10.0, bore_r=5.0,
                bore_offset=(15.0, 0.0),
            )
            roles = cylinder_radii_with_role_along(str(step), "z")
        self.assertTrue(roles)
        for r, role in roles:
            self.assertEqual(
                role, "inner",
                f"off-center bore R={r} must be 'inner', got {role!r}"
            )


class TestReconcileBlocksInterferenceFit(unittest.TestCase):
    """Reconcile must produce a BLOCKING incompatibility (not a warning)
    when topology roles are known and an interference fit is detected."""

    def test_shaft_larger_than_bore_is_blocking_incompatibility(self):
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

        def _res(step_path):
            return PartResult(
                part_id="x", step_path=str(step_path), stl_path="",
                py_path="", part_dir=str(step_path.parent),
                ok=True, attempts=1, token_usage={},
            )

        with tempfile.TemporaryDirectory() as tmp:
            bore_step = Path(tmp) / "bore.step"
            shaft_step = Path(tmp) / "shaft.step"
            _plate_with_hole_step(bore_step, bore_radius=5.0)
            _solid_shaft_step(shaft_step, radius=5.2)  # shaft > bore: interference
            brief = AssemblyBrief(
                assembly_name="t",
                parts=[
                    PartSpec(part_id="bore_part", part_name="bore_part",
                             description=""),
                    PartSpec(part_id="shaft_part", part_name="shaft_part",
                             description=""),
                ],
                interfaces=[
                    FunctionalInterface(
                        interface_id="itf", part_a="bore_part",
                        part_b="shaft_part",
                        interface_type=InterfaceType.HINGE,
                        description="",
                    ),
                ],
                user_request_raw="r",
            )
            plan = MatingPlan(
                assembly_name="t",
                mates=[
                    MateSpec(
                        mate_id="m1", mate_type=MateType.REVOLUTE,
                        fixed_part_id="bore_part",
                        moving_part_id="shaft_part",
                        fixed_anchor=Anchor(
                            kind=AnchorKind.SELECTOR,
                            selector_query=FaceQuery(
                                surface="cylinder", axis="z"),
                        ),
                        moving_anchor=Anchor(
                            kind=AnchorKind.SELECTOR,
                            selector_query=FaceQuery(
                                surface="cylinder", axis="z"),
                        ),
                    ),
                ],
            )
            part_results = {
                "bore_part": _res(bore_step),
                "shaft_part": _res(shaft_step),
            }
            r = reconcile_dimensions(brief, plan.mates, part_results)
        # Must be a DEFINITE incompatibility (blocking), not a UNVERIFIABLE
        # warning. The error string carries "radii incompatible".
        self.assertTrue(
            any("radii incompatible" in e for e in r.errors),
            f"interference fit (shaft > bore) must produce blocking "
            f"incompatibility, got: {r.errors}"
        )


if __name__ == "__main__":
    unittest.main()
