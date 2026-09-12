"""BUG-002 (P0 revision): _check_mates used to:

  * Use the MOVING anchor's selector_query for BOTH the fixed and
    moving uniqueness probes -- so a multi-bore fixed part with no
    target was never flagged when the moving side had a target.
  * Fall back to bbox algebra when a SELECTOR cylinder query found no
    face on the part (e.g. a Box with no cylinder) -- the "datum"
    became the bbox centre, and the moving-side target verification
    silently passed an unsatisfiable mate.

Fix: probe BOTH anchors with their OWN queries, unconditionally; fail
closed on not_found/error instead of falling back to bbox.

No external LLM calls; STEP files built with build123d.
"""
from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from build123d import Align, Box, Cylinder, Pos, export_step

from mac_assembly import assembly_qa as qa
from mac_assembly.schemas_assembly import (
    Anchor,
    AnchorKind,
    FaceQuery,
    MateSpec,
    MateType,
)


def _two_r5_holes_step(path: Path):
    """40x40x6 plate with TWO R5 through-bores at X=-12 and X=+12.
    No target on the fixed query -> multi-bore ambiguity."""
    plate = Pos(0, 0, 3) * Box(40, 40, 6, align=(Align.CENTER,) * 3)
    hole_a = Pos(-12, 0, 3) * Cylinder(
        radius=5.0, height=8, align=(Align.CENTER,) * 3
    )
    hole_b = Pos(12, 0, 3) * Cylinder(
        radius=5.0, height=8, align=(Align.CENTER,) * 3
    )
    export_step(plate - hole_a - hole_b, str(path))


def _box_no_cylinder_step(path: Path):
    """A simple Box with NO cylindrical faces."""
    plate = Pos(0, 0, 5) * Box(20, 20, 10, align=(Align.CENTER,) * 3)
    export_step(plate, str(path))


def _shaft_step(path: Path, radius: float = 5.0, height: float = 12.0):
    """Solid shaft along Z, placed at origin."""
    shape = Cylinder(radius=radius, height=height, align=(Align.CENTER,) * 3)
    shape = Pos(0, 0, height / 2.0) * shape
    export_step(shape, str(path))


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


def _bbox_of(step_path: str):
    """Compute the bbox of a STEP file by aggregating all face bboxes
    via the selector resolver's index."""
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
    """Build a 'placed' list from step paths (parts at the origin)."""
    placed = []
    for label, step in step_paths.items():
        bb = _bbox_of(step)
        placed.append((label, tuple(bb[0]), tuple(bb[1])))
    return placed


class TestMateCheckSelectorFailClosed(unittest.TestCase):
    """_check_mates must fail closed when SELECTOR probes fail, not
    silently fall back to bbox algebra or skip the fixed-side probe."""

    def test_fixed_multi_bore_no_target_moving_with_target_fails(self):
        """Counterexample 1: fixed has two R5 bores (no target on its
        query), moving has target_x=0. The previous code entered the
        per-axis target verification branch (because moving has a
        target) and never probed the fixed side for ambiguity. Now:
        the fixed side is probed independently and must report
        candidate_count=2 -> UNVERIFIABLE."""
        with tempfile.TemporaryDirectory() as tmp:
            fixed_step = Path(tmp) / "plate.step"
            _two_r5_holes_step(fixed_step)
            moving_step = Path(tmp) / "shaft.step"
            _shaft_step(moving_step, radius=5.0, height=8.0)
            step_paths = {"fixed": str(fixed_step), "moving": str(moving_step)}
            placed = _placed(step_paths)
            mate = _rigid_mate(
                "fixed", "moving",
                FaceQuery(surface="cylinder", axis="z"),  # no target
                FaceQuery(surface="cylinder", axis="z",
                          target_x_mm=0.0),  # moving has target
            )
            # Moving part is in moved_set -- give it a no-op location so
            # the verifiable-placement gate passes and we reach the
            # SELECTOR uniqueness probe.
            locations = {
                "moving": ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0)),
            }
            checks = qa._check_mates(
                [mate], placed, None, step_paths, locations=locations,
            )
        self.assertTrue(checks)
        self.assertFalse(
            checks[0].passed,
            f"fixed multi-bore + no target must FAIL, got: "
            f"{checks[0].detail}"
        )
        # Should mention the fixed-side ambiguity (multi-candidate).
        self.assertTrue(
            "UNVERIFIABLE" in checks[0].detail
            or "candidate" in checks[0].detail,
            f"detail should reference the ambiguity: {checks[0].detail}"
        )

    def test_fixed_box_no_cylinder_with_cylinder_query_fails(self):
        """Counterexample 2: fixed is a Box with NO cylindrical faces,
        but the fixed query requires cylinder. The previous code's
        _resolve_qa_point returned (None, False), fell through to the
        bbox fallback, and the mate silently passed. Now: the 3state
        resolver returns 'not_found' and the mate fails with the
        SELECTOR-miss signature."""
        with tempfile.TemporaryDirectory() as tmp:
            fixed_step = Path(tmp) / "box.step"
            _box_no_cylinder_step(fixed_step)
            moving_step = Path(tmp) / "shaft.step"
            _shaft_step(moving_step, radius=5.0, height=8.0)
            step_paths = {"fixed": str(fixed_step), "moving": str(moving_step)}
            placed = _placed(step_paths)
            mate = _rigid_mate(
                "fixed", "moving",
                FaceQuery(surface="cylinder", axis="z"),  # fixed requires cyl
                FaceQuery(surface="cylinder", axis="z",
                          target_x_mm=0.0),  # moving has cylinder
            )
            locations = {
                "moving": ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0)),
            }
            checks = qa._check_mates(
                [mate], placed, None, step_paths, locations=locations,
            )
        self.assertTrue(checks)
        self.assertFalse(
            checks[0].passed,
            f"fixed Box + cylinder SELECTOR must FAIL (not_found), "
            f"got: {checks[0].detail}"
        )
        self.assertIn(
            "SELECTOR anchor matched no face",
            checks[0].detail,
            f"detail should mention SELECTOR miss: {checks[0].detail}"
        )


if __name__ == "__main__":
    unittest.main()
