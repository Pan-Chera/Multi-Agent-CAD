"""Regression tests for selector false-PASS boundary cases."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from build123d import Align, Box, Cylinder, Pos, export_step

from mac_assembly.assembly_qa import _check_mates
from mac_assembly.schemas_assembly import (
    Anchor,
    AnchorKind,
    FaceQuery,
    MateSpec,
    MateType,
)
from mac_assembly.selector_resolver import _selector_index, probe_selector_anchor


def _bbox_of(path: str):
    index, _ = _selector_index(path)
    mins = [float("inf")] * 3
    maxs = [float("-inf")] * 3
    for face in index.faces:
        bbox = face.get("bbox") or {}
        for i, value in enumerate((bbox.get("min") or [])[:3]):
            mins[i] = min(mins[i], float(value))
        for i, value in enumerate((bbox.get("max") or [])[:3]):
            maxs[i] = max(maxs[i], float(value))
    return tuple(mins), tuple(maxs)


def _placed(step_paths):
    return [(part_id, *_bbox_of(path)) for part_id, path in step_paths.items()]


class TestSelectorFailClosedRegressions(unittest.TestCase):
    def test_asymmetric_blind_holes_do_not_merge_across_solid_wall(self):
        with tempfile.TemporaryDirectory() as tmp:
            step = Path(tmp) / "asymmetric_blind.step"
            block = Pos(0, 0, 15) * Box(
                20, 20, 30, align=(Align.CENTER,) * 3
            )
            lower = Pos(0, 0, 9) * Cylinder(
                3, 18, align=(Align.CENTER,) * 3
            )
            upper = Pos(0, 0, 25) * Cylinder(
                3, 10, align=(Align.CENTER,) * 3
            )
            export_step(block - lower - upper, str(step))
            status, result = probe_selector_anchor(str(step), {
                "surface": "cylinder",
                "axis": "z",
                # Both holes have the same radius but unequal areas.  Radius
                # selection keeps both in the candidate pool so this test
                # isolates material-gap clustering rather than the valid
                # "largest face" disambiguator.
                "select": "closest_to",
                "value_mm": 3.0,
            })

        self.assertEqual(status, "found")
        self.assertIsNotNone(result)
        self.assertGreaterEqual(int(result["candidate_count"]), 2)
        self.assertFalse(18.0 <= float(result["point"][2]) <= 20.0)

    def test_wrong_fixed_selector_target_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixed = Path(tmp) / "fixed.step"
            moving = Path(tmp) / "moving.step"
            shape = Pos(0, 0, 6) * Cylinder(
                5, 12, align=(Align.CENTER,) * 3
            )
            export_step(shape, str(fixed))
            export_step(shape, str(moving))
            paths = {"fixed": str(fixed), "moving": str(moving)}
            mate = MateSpec(
                mate_id="bad_fixed_target",
                mate_type=MateType.RIGID,
                fixed_part_id="fixed",
                moving_part_id="moving",
                fixed_anchor=Anchor(
                    kind=AnchorKind.SELECTOR,
                    selector_query=FaceQuery(
                        surface="cylinder", axis="z", target_x_mm=100.0
                    ),
                ),
                moving_anchor=Anchor(
                    kind=AnchorKind.SELECTOR,
                    selector_query=FaceQuery(
                        surface="cylinder", axis="z", target_x_mm=0.0
                    ),
                ),
            )
            check = _check_mates(
                [mate], _placed(paths), None, paths,
                locations={"moving": ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))},
            )[0]

        self.assertFalse(check.passed)
        self.assertIn("target matches no candidate", check.detail)

    def test_revolute_missing_moving_selector_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixed = Path(tmp) / "fixed.step"
            moving = Path(tmp) / "moving_box.step"
            export_step(
                Pos(0, 0, 6) * Cylinder(5, 12, align=(Align.CENTER,) * 3),
                str(fixed),
            )
            export_step(
                Pos(0, 0, 6) * Box(20, 20, 12, align=(Align.CENTER,) * 3),
                str(moving),
            )
            paths = {"fixed": str(fixed), "moving": str(moving)}
            query = FaceQuery(surface="cylinder", axis="z")
            mate = MateSpec(
                mate_id="missing_moving_selector",
                mate_type=MateType.REVOLUTE,
                fixed_part_id="fixed",
                moving_part_id="moving",
                fixed_anchor=Anchor(
                    kind=AnchorKind.SELECTOR, selector_query=query
                ),
                moving_anchor=Anchor(
                    kind=AnchorKind.SELECTOR, selector_query=query
                ),
            )
            check = _check_mates(
                [mate], _placed(paths), None, paths,
                locations={"moving": ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))},
            )[0]

        self.assertFalse(check.passed)
        self.assertIn(
            "SELECTOR anchor matched no face on moving", check.detail
        )


if __name__ == "__main__":
    unittest.main()
