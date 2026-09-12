"""P0-2: gap <= radius heuristic cannot distinguish split-face from
two separate blind holes.

The previous code used ``gap <= max(radius, 0.5)`` as the threshold for
merging co-axial same-radius segments. Counterexample: a 20mm block
with two R3 blind holes drilled from each end (each ~9mm deep, leaving
a 2mm solid wall in the middle). The gap is 2mm, which is <= radius=3,
so the heuristic merged them -- candidate_count=1 and the resolved
datum landed at Z=midpoint = 10, which is INSIDE the solid wall.

Fix: use material connectivity (mesh.contains() at the midpoint of the
combined along-axis extent) to test if the gap is air (split-face --
merge) or solid (separate blind holes -- do NOT merge). When the test
is unavailable or fails, treat as ambiguous (do NOT merge, count as
separate).

Test geometry: 20x20x20 block, R3 Z-axis blind holes at Z=0..9 and
Z=11..20, with a 2mm solid wall at Z=9..11.

No external LLM calls; STEP files built with build123d.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from build123d import Align, Box, Cylinder, Pos, export_step

from mac_assembly.selector_resolver import probe_selector_anchor


def _two_blind_holes_with_solid_wall_step(path: Path):
    """20x20x20 block with R3 Z-axis blind holes at Z=0..9 (bottom)
    and Z=11..20 (top). The 2mm wall at Z=9..11 is solid material -- the
    two holes are SEPARATE, not split-face of one logical cylinder."""
    block = Pos(0, 0, 10) * Box(20, 20, 20, align=(Align.CENTER,) * 3)
    bottom_hole = Pos(0, 0, 4.5) * Cylinder(
        radius=3.0, height=9, align=(Align.CENTER,) * 3
    )
    top_hole = Pos(0, 0, 15.5) * Cylinder(
        radius=3.0, height=9, align=(Align.CENTER,) * 3
    )
    export_step(block - bottom_hole - top_hole, str(path))


def _clevis_slot_split_bore_step(path: Path):
    """A single through-bore split into two OCCT face segments by a
    lateral slot (clevis-fork geometry). The two segments are ONE
    logical cylinder (the gap between them is air), so count must be 1.
    """
    plate = Pos(0, 0, 5) * Box(40, 40, 10, align=(Align.CENTER,) * 3)
    # Slot in Y direction (1mm thick in Y) cutting through the plate,
    # intersecting the bore's lateral surface.
    slot = Pos(0, 0, 5) * Box(40, 1.0, 10, align=(Align.CENTER,) * 3)
    hole = Pos(0, 0, 5) * Cylinder(
        radius=5.0, height=12, align=(Align.CENTER,) * 3
    )
    export_step(plate - slot - hole, str(path))


class TestBlindHolesMaterialConnectivity(unittest.TestCase):
    """Material connectivity distinguishes split-face (merge) from
    separate blind holes (do NOT merge)."""

    def test_two_blind_holes_with_solid_wall_count_is_2(self):
        """R3 blind holes at Z=0..9 and Z=11..20 with a 2mm solid wall
        between them. The gap is 2mm, which is <= radius=3 (old
        heuristic merged them). Material connectivity correctly reports
        the gap as solid -> count=2."""
        with tempfile.TemporaryDirectory() as tmp:
            step = Path(tmp) / "block.step"
            _two_blind_holes_with_solid_wall_step(step)
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
            f"two blind holes with a solid wall (gap=2 <= radius=3 old "
            f"heuristic merged them) must report count>=2 (separate), "
            f"got {result.get('candidate_count')}"
        )

    def test_resolved_point_not_in_solid_wall(self):
        """The resolved datum must NOT land at Z=10 (inside the 2mm
        solid wall). It should be inside one of the blind holes (Z in
        [0, 9] or Z in [11, 20])."""
        with tempfile.TemporaryDirectory() as tmp:
            step = Path(tmp) / "block.step"
            _two_blind_holes_with_solid_wall_step(step)
            status, result = probe_selector_anchor(str(step), {
                "surface": "cylinder",
                "axis": "z",
                "select": "largest",
            })
        self.assertEqual(status, "found")
        self.assertIsNotNone(result)
        z = float(result["point"][2])
        self.assertFalse(
            9.0 <= z <= 11.0,
            f"resolved datum Z={z} lands inside the 2mm solid wall "
            f"(Z=9..11) between the two blind holes -- must be inside "
            f"one of the holes"
        )

    def test_target_z_left_picks_bottom_hole(self):
        """target_z_mm=4.5 (inside the bottom hole's Z=0..9 extent)
        must disambiguate to the bottom hole -> count=1."""
        with tempfile.TemporaryDirectory() as tmp:
            step = Path(tmp) / "block.step"
            _two_blind_holes_with_solid_wall_step(step)
            status, result = probe_selector_anchor(str(step), {
                "surface": "cylinder",
                "axis": "z",
                "select": "largest",
                "target_z_mm": 4.5,
            })
        self.assertEqual(status, "found")
        self.assertIsNotNone(result)
        self.assertEqual(
            int(result.get("candidate_count") or 0),
            1,
            f"target_z=4.5 must pick the bottom hole (count=1), got "
            f"{result.get('candidate_count')}"
        )

    def test_target_z_right_picks_top_hole(self):
        """target_z_mm=15.5 (inside the top hole's Z=11..20 extent)
        must disambiguate to the top hole -> count=1."""
        with tempfile.TemporaryDirectory() as tmp:
            step = Path(tmp) / "block.step"
            _two_blind_holes_with_solid_wall_step(step)
            status, result = probe_selector_anchor(str(step), {
                "surface": "cylinder",
                "axis": "z",
                "select": "largest",
                "target_z_mm": 15.5,
            })
        self.assertEqual(status, "found")
        self.assertIsNotNone(result)
        self.assertEqual(
            int(result.get("candidate_count") or 0),
            1,
            f"target_z=15.5 must pick the top hole (count=1), got "
            f"{result.get('candidate_count')}"
        )

    def test_clevis_slot_split_bore_count_is_1(self):
        """A single through-bore split by a lateral slot (clevis-fork
        geometry). The two OCCT face segments share the same axis + R,
        and the gap between them is AIR (the slot) -> merge -> count=1.
        This is the existing clevis-fork positive case; it must still
        pass after the material-connectivity refactor."""
        with tempfile.TemporaryDirectory() as tmp:
            step = Path(tmp) / "clevis.step"
            _clevis_slot_split_bore_step(step)
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
            f"clevis slot split bore (one logical cylinder) must report "
            f"count=1, got {result.get('candidate_count')}"
        )


if __name__ == "__main__":
    unittest.main()
