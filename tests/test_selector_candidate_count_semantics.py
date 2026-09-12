"""BUG-043 (revised): candidate_count used to count ALL topology
candidates before applying select/value_mm/target, so an R5 hole next to
an unrelated R8 cylinder reported candidate_count=2 (false ambiguous).

The fix: candidate_count = "logical candidates remaining after applying
the FULL query semantics" (select/value_mm filter, target disambiguation,
and split-cylinder deduplication).

Also: the previous code's "cand_count==0 -> passed=True" path was a
logic bug -- a selector that finds nothing must NOT silently pass; it
must fail with the selector-miss signature so the router's downstream
attribution can run. And fixed SELECTOR anchor (not just moving) must
also be uniqueness-checked.

No external LLM calls; STEP files built with build123d, probed via
cadpy SelectorIndex.
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

from mac_assembly.selector_resolver import probe_selector_anchor


def _r5_hole_plus_r8_cylinder_step(path: Path):
    """40x40 plate with a R5 through-bore at origin AND an unrelated
    solid R8 cylinder (a post) elsewhere. A query for the R5 bore
    (closest_to R5) must return candidate_count=1, not 2."""
    plate = Pos(0, 0, 5) * Box(40, 40, 10, align=(Align.CENTER,) * 3)
    r5_hole = Pos(0, 0, 5) * Cylinder(
        radius=5.0, height=12, align=(Align.CENTER,) * 3
    )
    r8_post = Pos(15, 0, 15) * Cylinder(
        radius=8.0, height=10, align=(Align.CENTER,) * 3
    )
    export_step(plate - r5_hole + r8_post, str(path))


def _two_r5_holes_step(path: Path):
    """40x40 plate with TWO R5 through-bores at different positions.
    A query without target_x_mm must return candidate_count=2 (truly
    ambiguous)."""
    plate = Pos(0, 0, 5) * Box(40, 40, 10, align=(Align.CENTER,) * 3)
    hole_a = Pos(-10, 0, 5) * Cylinder(
        radius=5.0, height=12, align=(Align.CENTER,) * 3
    )
    hole_b = Pos(10, 0, 5) * Cylinder(
        radius=5.0, height=12, align=(Align.CENTER,) * 3
    )
    export_step(plate - hole_a - hole_b, str(path))


def _split_bore_step(path: Path):
    """A single bore split into two face segments by a slot (e.g. a
    clevis fork bore). The two segments are ONE logical cylinder, so
    candidate_count must be 1, not 2."""
    # Build a clevis-like fork: a plate with two ear slabs and a
    # through-bore that crosses both ears (the slot between the ears
    # splits the OCCT cylinder face into 2 segments).
    plate = Pos(0, 0, 5) * Box(40, 40, 10, align=(Align.CENTER,) * 3)
    slot = Pos(0, 0, 5) * Box(40, 1.0, 10, align=(Align.CENTER,) * 3)
    hole = Pos(0, 0, 5) * Cylinder(
        radius=5.0, height=12, align=(Align.CENTER,) * 3
    )
    export_step(plate - slot - hole, str(path))


class TestCandidateCountSemantics(unittest.TestCase):
    """candidate_count must reflect post-filter logical candidate count,
    not pre-filter topology face count."""

    def test_r5_hole_with_unrelated_r8_post_count_is_1(self):
        """Query for closest_to R5 must return count=1 (only the R5
        cylinder matches the radius filter), not 2 (R5 + R8)."""
        with tempfile.TemporaryDirectory() as tmp:
            step = Path(tmp) / "step.step"
            _r5_hole_plus_r8_cylinder_step(step)
            status, result = probe_selector_anchor(str(step), {
                "surface": "cylinder",
                "axis": "z",
                "select": "closest_to",
                "value_mm": 5.0,
            })
        self.assertEqual(status, "found")
        self.assertIsNotNone(result)
        self.assertEqual(
            int(result.get("candidate_count") or 0),
            1,
            f"R5 + R8 with closest_to R5 must report count=1, got "
            f"{result.get('candidate_count')}"
        )

    def test_two_r5_holes_no_target_count_is_2(self):
        """Two same-radius bores at different positions, no target ->
        truly ambiguous -> count=2."""
        with tempfile.TemporaryDirectory() as tmp:
            step = Path(tmp) / "step.step"
            _two_r5_holes_step(step)
            status, result = probe_selector_anchor(str(step), {
                "surface": "cylinder",
                "axis": "z",
                "select": "largest",
            })
        self.assertEqual(status, "found")
        self.assertIsNotNone(result)
        self.assertGreaterEqual(
            int(result.get("candidate_count") or 0),
            2,
            f"two distinct R5 bores must report count>=2 (ambiguous), "
            f"got {result.get('candidate_count')}"
        )

    def test_split_bore_count_is_1(self):
        """A single logical bore split by a slot into 2 OCCT face segments
        must report count=1 (one logical cylinder)."""
        with tempfile.TemporaryDirectory() as tmp:
            step = Path(tmp) / "step.step"
            _split_bore_step(step)
            status, result = probe_selector_anchor(str(step), {
                "surface": "cylinder",
                "axis": "z",
                "select": "closest_to",
                "value_mm": 5.0,
            })
        self.assertEqual(status, "found")
        self.assertIsNotNone(result)
        self.assertEqual(
            int(result.get("candidate_count") or 0),
            1,
            f"split bore (one logical cylinder, 2 OCCT faces) must report "
            f"count=1, got {result.get('candidate_count')}"
        )


if __name__ == "__main__":
    unittest.main()
