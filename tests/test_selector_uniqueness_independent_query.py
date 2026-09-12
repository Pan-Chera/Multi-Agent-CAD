"""BUG-002 (P0 revision): _check_mates' SELECTOR uniqueness probe used
to use the MOVING anchor's selector_query for BOTH the fixed and moving
probes. So a fixed part with two R5 bores + no target + a moving part
with target_x=20 (which enters the per-axis target verification branch,
skipping the uniqueness probe entirely) silently false-passed the
multi-bore fixed ambiguity.

Also: when a fixed SELECTOR cylinder query probed a part with NO
matching cylinder face (e.g. a Box), the resolver returned None and
the code fell back to the FACE/AXIS_POINT bbox centre -- the "datum"
became the part's bbox centre, then the moving-side target verification
passed, false-PASSing an unsatisfiable mate.

Also: two co-axial same-radius blind holes on the same axis (one
drilled from each end of a block) used to dedup to candidate_count=1
(the dedup only checked origin-lateral position + radius, ignoring the
axial gap). The resolved datum landed at Z=midpoint -- in solid
material between the two holes.

Fixes:
  1. _probe_anchor_uniqueness takes the anchor (not the step_path +
     label), and uses each anchor's OWN selector_query.
  2. The probe runs for BOTH sides unconditionally, regardless of
     whether the moving side has a target.
  3. _resolve_qa_point_3state returns "no_selector" / "found" /
     "not_found" / "error" so the call site can fail closed instead of
     falling back to bbox algebra.
  4. _semantic_candidate_count uses gap-based clustering: gap <= radius
     = split-face of one bore; gap > radius = separate bores.

No external LLM calls; STEP files built with build123d.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from build123d import Align, Box, Cylinder, Pos, export_step

from mac_assembly.selector_resolver import probe_selector_anchor


def _two_r5_holes_step(path: Path):
    """40x40x6 plate with TWO R5 through-bores at X=-12 and X=+12."""
    plate = Pos(0, 0, 3) * Box(40, 40, 6, align=(Align.CENTER,) * 3)
    hole_a = Pos(-12, 0, 3) * Cylinder(
        radius=5.0, height=8, align=(Align.CENTER,) * 3
    )
    hole_b = Pos(12, 0, 3) * Cylinder(
        radius=5.0, height=8, align=(Align.CENTER,) * 3
    )
    export_step(plate - hole_a - hole_b, str(path))


def _box_no_cylinder_step(path: Path):
    """A simple Box with NO cylindrical faces. A cylinder SELECTOR
    query must return 'not_found' against this part."""
    plate = Pos(0, 0, 5) * Box(20, 20, 10, align=(Align.CENTER,) * 3)
    export_step(plate, str(path))


def _two_blind_holes_same_axis_step(path: Path):
    """A 20x20x20 block with TWO R3 blind holes on the same Z-axis
    (lateral (0,0)), one at the bottom (Z=0..5) and one at the top
    (Z=15..20). The two holes are NOT connected -- there is solid
    material between them. They must NOT dedup to candidate_count=1
    (the merged-bbox midpoint Z=10 lands in solid material)."""
    block = Pos(0, 0, 10) * Box(20, 20, 20, align=(Align.CENTER,) * 3)
    bottom_hole = Pos(0, 0, 2.5) * Cylinder(
        radius=3.0, height=5, align=(Align.CENTER,) * 3
    )
    top_hole = Pos(0, 0, 17.5) * Cylinder(
        radius=3.0, height=5, align=(Align.CENTER,) * 3
    )
    export_step(block - bottom_hole - top_hole, str(path))


class TestSelectorUniquenessIndependentQuery(unittest.TestCase):
    """probe_selector_anchor candidate_count must reflect the actual
    logical cylinder count after gap-based clustering."""

    def test_two_blind_holes_same_axis_count_is_2(self):
        """Two co-axial same-radius blind holes with a gap > radius must
        report candidate_count=2 (separate bores), not 1 (split-face
        of one bore). The resolved datum must NOT land at the
        midpoint between the two holes (which is in solid material)."""
        with tempfile.TemporaryDirectory() as tmp:
            step = Path(tmp) / "block.step"
            _two_blind_holes_same_axis_step(step)
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
            f"two separate blind holes (gap > radius) must report "
            f"count>=2 (separate bores), got "
            f"{result.get('candidate_count')}"
        )
        # Resolved point must NOT land at Z=10 (solid material between
        # the two holes). It should be inside one of the holes (Z in
        # [0, 5] or Z in [15, 20]).
        z = float(result["point"][2])
        self.assertFalse(
            6.0 <= z <= 14.0,
            f"resolved datum Z={z} lands in solid material between the "
            f"two blind holes (must be inside one hole)"
        )


if __name__ == "__main__":
    unittest.main()
