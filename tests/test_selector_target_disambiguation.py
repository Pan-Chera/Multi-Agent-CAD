"""P0-1: target presence does NOT equal disambiguation complete.

The previous `_semantic_candidate_count` returned 1 whenever ANY target
field was non-None, treating ``target_z_mm`` alone as sufficient to
disambiguate two parallel Z-axis bores at different XY. The target_z
constraint is satisfied by BOTH bores (both have the same Z extent), so
the count must stay >= 2 (ambiguous).

Also: ``_check_mates`` used to short-circuit ``has_target -> True`` for
any non-None target field. Now it requires ``candidate_count == 1``
even with a target, so a target_z_mm on two parallel bores fails.

Test geometry: 40x40x10 plate with two R5 Z-axis through-bores at
X=-10 and X=+10.
  * target_z_mm=5 alone -> count>=2, mate FAILS (ambiguous).
  * target_x_mm=-10 -> count=1, resolves to the left bore.

No external LLM calls; STEP files built with build123d.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from build123d import Align, Box, Cylinder, Pos, export_step

from mac_assembly.assembly_qa import _check_mates
from mac_assembly.selector_resolver import probe_selector_anchor
from mac_assembly.schemas_assembly import (
    Anchor,
    AnchorKind,
    FaceQuery,
    MateSpec,
    MateType,
)


def _two_parallel_z_holes_step(path: Path):
    """40x40x10 plate with TWO R5 through-bores along Z at X=-10/+10."""
    plate = Pos(0, 0, 5) * Box(40, 40, 10, align=(Align.CENTER,) * 3)
    hole_l = Pos(-10, 0, 5) * Cylinder(
        radius=5.0, height=12, align=(Align.CENTER,) * 3
    )
    hole_r = Pos(10, 0, 5) * Cylinder(
        radius=5.0, height=12, align=(Align.CENTER,) * 3
    )
    export_step(plate - hole_l - hole_r, str(path))


def _single_r5_shaft_step(path: Path):
    """Solid R5 shaft along Z (the moving part)."""
    shape = Cylinder(radius=5.0, height=12, align=(Align.CENTER,) * 3)
    shape = Pos(0, 0, 6) * shape
    export_step(shape, str(path))


def _bbox_of(step_path: str):
    """Aggregate all face bboxes via the selector resolver's index."""
    from mac_assembly.selector_resolver import _selector_index
    index, _ = _selector_index(step_path)
    mins = [float("inf")] * 3
    maxs = [float("-inf")] * 3
    for face in index.faces:
        bb = face.get("bbox")
        if not bb:
            continue
        mn = bb.get("min") or []
        mx = bb.get("max") or []
        for i in range(min(len(mn), 3)):
            mins[i] = min(mins[i], float(mn[i]))
        for i in range(min(len(mx), 3)):
            maxs[i] = max(maxs[i], float(mx[i]))
    return (tuple(mins), tuple(maxs))


def _placed(step_paths: dict[str, str]):
    placed = []
    for label, step in step_paths.items():
        bb = _bbox_of(step)
        placed.append((label, tuple(bb[0]), tuple(bb[1])))
    return placed


def _rigid_mate(fixed_id, moving_id, fixed_query, moving_query):
    return MateSpec(
        mate_id="m1",
        mate_type=MateType.RIGID,
        fixed_part_id=fixed_id,
        moving_part_id=moving_id,
        fixed_anchor=Anchor(
            kind=AnchorKind.SELECTOR, selector_query=fixed_query,
        ),
        moving_anchor=Anchor(
            kind=AnchorKind.SELECTOR, selector_query=moving_query,
        ),
    )


class TestSelectorTargetDisambiguation(unittest.TestCase):
    """candidate_count must reflect the target's ACTUAL disambiguation."""

    def test_target_z_alone_on_two_parallel_z_bores_count_is_2(self):
        """Two R5 Z-axis bores at X=-10/+10. ``target_z_mm=5`` constrains
        the along-axis position (both bores span Z=0..10) but NOT the
        lateral position. count must be >= 2 (ambiguous)."""
        with tempfile.TemporaryDirectory() as tmp:
            step = Path(tmp) / "plate.step"
            _two_parallel_z_holes_step(step)
            status, result = probe_selector_anchor(str(step), {
                "surface": "cylinder",
                "axis": "z",
                "select": "largest",
                "target_z_mm": 5.0,
            })
        self.assertEqual(status, "found")
        self.assertIsNotNone(result)
        self.assertGreaterEqual(
            int(result.get("candidate_count") or 0),
            2,
            f"target_z alone cannot disambiguate two parallel Z bores -- "
            f"count must be >= 2, got {result.get('candidate_count')}"
        )

    def test_target_x_picks_left_bore_count_is_1(self):
        """Same geometry, ``target_x_mm=-10``. The lateral target
        component selects the unique bore whose axis lateral matches.
        count must be 1."""
        with tempfile.TemporaryDirectory() as tmp:
            step = Path(tmp) / "plate.step"
            _two_parallel_z_holes_step(step)
            status, result = probe_selector_anchor(str(step), {
                "surface": "cylinder",
                "axis": "z",
                "select": "largest",
                "target_x_mm": -10.0,
            })
        self.assertEqual(status, "found")
        self.assertIsNotNone(result)
        self.assertEqual(
            int(result.get("candidate_count") or 0),
            1,
            f"target_x=-10 must disambiguate to the left bore (count=1), "
            f"got {result.get('candidate_count')}"
        )
        # Resolved point should be near x=-10.
        self.assertAlmostEqual(
            float(result["point"][0]), -10.0, places=1,
            msg=f"resolved point X should be near -10, got {result['point']}"
        )

    def test_mate_with_target_z_alone_on_two_parallel_bores_fails(self):
        """Counterexample C/D: moving has target_z only on a two-bore
        part. The mate must FAIL (target does not disambiguate)."""
        with tempfile.TemporaryDirectory() as tmp:
            fixed_step = Path(tmp) / "shaft.step"
            _single_r5_shaft_step(fixed_step)
            moving_step = Path(tmp) / "plate.step"
            _two_parallel_z_holes_step(moving_step)
            step_paths = {
                "fixed": str(fixed_step),
                "moving": str(moving_step),
            }
            placed = _placed(step_paths)
            mate = _rigid_mate(
                "fixed", "moving",
                FaceQuery(surface="cylinder", axis="z"),  # fixed unique
                FaceQuery(surface="cylinder", axis="z",
                          target_z_mm=5.0),  # moving target_z only
            )
            locations = {
                "moving": ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0)),
            }
            checks = _check_mates(
                [mate], placed, None, step_paths, locations=locations,
            )
        self.assertTrue(checks)
        self.assertFalse(
            checks[0].passed,
            f"target_z alone on two parallel bores must FAIL (ambiguous), "
            f"got: {checks[0].detail}"
        )
        # The detail must mention the ambiguity (count > 1 / target does
        # not disambiguate / UNVERIFIABLE).
        detail_upper = checks[0].detail.upper()
        self.assertTrue(
            "UNVERIFIABLE" in detail_upper
            or "CANDIDATE" in detail_upper
            or "DOES NOT DISAMBIGUATE" in detail_upper,
            f"detail should reference the ambiguity: {checks[0].detail}"
        )

    def test_mate_with_target_x_picks_bore_and_passes_or_fails_target_check(self):
        """With target_x_mm, the resolver picks the correct bore. The
        moving-side target verification runs (the resolved point's X
        vs target_x). The check is non-silent (not the no-target
        auto-pass)."""
        with tempfile.TemporaryDirectory() as tmp:
            fixed_step = Path(tmp) / "shaft.step"
            _single_r5_shaft_step(fixed_step)
            moving_step = Path(tmp) / "plate.step"
            _two_parallel_z_holes_step(moving_step)
            step_paths = {
                "fixed": str(fixed_step),
                "moving": str(moving_step),
            }
            placed = _placed(step_paths)
            mate = _rigid_mate(
                "fixed", "moving",
                FaceQuery(surface="cylinder", axis="z"),
                FaceQuery(surface="cylinder", axis="z",
                          target_x_mm=-10.0),  # picks the left bore
            )
            locations = {
                "moving": ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0)),
            }
            checks = _check_mates(
                [mate], placed, None, step_paths, locations=locations,
            )
        self.assertTrue(checks)
        # With target_x_mm, the count should be 1 (disambiguated), so the
        # target verification runs. The detail should mention "target"
        # (verification ran), not the no-target auto-pass.
        self.assertNotIn(
            "no target to verify", checks[0].detail.lower(),
            f"with target_x, the target verification must run (not the "
            f"no-target auto-pass), got: {checks[0].detail}"
        )


if __name__ == "__main__":
    unittest.main()
