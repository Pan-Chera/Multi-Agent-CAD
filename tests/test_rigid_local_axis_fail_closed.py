"""BUG-042: when a moved fixed part had a non-zero rigid translation_mm
and _local_to_world_direction returned None (degenerate transform), the
code used to fall back to the UNROTATED local axis. That silently
mis-measured the world translation on a rotated part -> false PASS.

Fix: fail closed. If the fixed part is moved and direction transform
returns None, the MateCheck must be passed=False with UNVERIFIABLE
detail, NOT a silent pass with the wrong axis.

No external LLM calls; STEP files built with build123d.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import trimesh
from build123d import Align, Box, Pos, export_step

from mac_assembly import assembly_qa as qa
from mac_assembly.schemas_assembly import (
    Anchor,
    AnchorKind,
    MateSpec,
    MateType,
)


def _part_step(path: Path, size=4.0):
    shape = Box(size, size, size, align=(Align.CENTER,) * 3)
    shape = Pos(0, 0, size / 2.0) * shape
    export_step(shape, str(path))


class TestRigidLocalAxisFailClosed(unittest.TestCase):
    def test_moved_fixed_part_with_failed_direction_transform_fails(self):
        """Fixed part 'a' was moved (in moved_set via mate1), rigid
        translation_mm is non-zero, and _local_to_world_direction returns
        None. The MateCheck must fail closed with UNVERIFIABLE, NOT
        silently pass using the unrotated local axis."""
        with tempfile.TemporaryDirectory() as tmp:
            step_a = Path(tmp) / "a.step"
            _part_step(step_a)
            step_b = Path(tmp) / "b.step"
            _part_step(step_b)
            a = trimesh.creation.box(
                extents=(4, 4, 4),
                transform=trimesh.transformations.translation_matrix(
                    (0, 0, 2)
                ),
            )
            b = trimesh.creation.box(
                extents=(4, 4, 4),
                transform=trimesh.transformations.translation_matrix(
                    (0, 0, 2)
                ),
            )
            placed = [
                ("a", (-2, -2, 0), (2, 2, 4)),
                ("b", (-2, -2, 0), (2, 2, 4)),
            ]
            # mate1 moves 'a' (so 'a' is in moved_set). mate2 is the
            # RIGID mate whose fixed_part_id='a' (moved) and translation_mm
            # is non-zero -- triggers the bug path.
            mate1 = MateSpec(
                mate_id="m0", mate_type=MateType.FACE_TO_FACE,
                fixed_part_id="root", moving_part_id="a",
                fixed_anchor=Anchor(kind=AnchorKind.FACE, face="top"),
                moving_anchor=Anchor(kind=AnchorKind.FACE, face="bottom"),
                offset_mm=0.0,
            )
            mate2 = MateSpec(
                mate_id="m1", mate_type=MateType.RIGID,
                fixed_part_id="a", moving_part_id="b",
                fixed_anchor=Anchor(kind=AnchorKind.FACE, face="top"),
                moving_anchor=Anchor(kind=AnchorKind.FACE, face="bottom"),
                offset_mm=0.0,
                translation_mm=[10.0, 0.0, 0.0],
            )
            # Mock _local_to_world_direction so the FIRST TWO calls
            # (made by _anchor_world_datum for the moved fixed 'a' and
            # the moved moving 'b') succeed, but the subsequent calls
            # (the rigid translation_mm basis loop at line 792-813)
            # return None. This triggers the BUG-042 fail-open path --
            # the code at line 806 used to fall back to the unrotated
            # local axis, silently mis-measuring the world translation
            # on a rotated part.
            call_count = {"n": 0}
            def smart_local_to_world_direction(direction, location_data):
                call_count["n"] += 1
                if call_count["n"] <= 2:
                    # First two calls: _anchor_world_datum direction
                    # transforms. Return valid so functions succeed and
                    # execution reaches the rigid basis loop.
                    return tuple(float(v) for v in direction)
                # Subsequent calls: rigid translation basis loop.
                # Return None to simulate a degenerate transform on
                # the moved fixed part.
                return None
            with mock.patch.object(qa, "_local_to_world_direction",
                                   side_effect=smart_local_to_world_direction):
                checks = qa._check_mates(
                    [mate1, mate2], placed,
                    meshes_by_label={"a": [a], "b": [b]},
                    step_paths={"a": str(step_a), "b": str(step_b)},
                    locations={
                        "a": ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0)),
                        "b": ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0)),
                    },
                )
        rigid_check = next(
            (c for c in checks if c.mate_id == "m1"), None
        )
        self.assertIsNotNone(rigid_check, f"no m1 check, got {checks}")
        self.assertFalse(
            rigid_check.passed,
            f"moved fixed part with failed direction transform must FAIL "
            f"closed (UNVERIFIABLE), not pass with the wrong axis. "
            f"detail: {rigid_check.detail}"
        )
        # Detail must specifically mention the direction-transform failure
        # (not some other UNVERIFIABLE path).
        self.assertIn("UNVERIFIABLE", rigid_check.detail.upper())
        # Verify it's about the direction transform, not a different
        # UNVERIFIABLE branch.
        self.assertTrue(
            "direction" in rigid_check.detail.lower()
            or "transform" in rigid_check.detail.lower(),
            f"detail should mention direction/transform failure: "
            f"{rigid_check.detail}"
        )


if __name__ == "__main__":
    unittest.main()
