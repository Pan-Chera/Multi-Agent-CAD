"""Regression: planar SELECTOR target coordinates define the datum."""

import tempfile
from pathlib import Path

from build123d import Align, Box, export_step

from mac_assembly.selector_resolver import resolve_selector_anchor


def test_planar_selector_uses_in_plane_target_coordinates():
    with tempfile.TemporaryDirectory() as td:
        step = Path(td) / "plate.step"
        shape = Box(20, 12, 10, align=(Align.CENTER, Align.CENTER, Align.MIN))
        export_step(shape, str(step))
        result = resolve_selector_anchor(
            str(step),
            {
                "surface": "plane",
                "axis": "z",
                "normal_sign": 1,
                "select": "closest_to",
                "value_mm": 10.0,
                "target_x_mm": 3.0,
                "target_y_mm": -2.0,
            },
        )
        assert result is not None
        assert result["point"] == (3.0, -2.0, 10.0)
