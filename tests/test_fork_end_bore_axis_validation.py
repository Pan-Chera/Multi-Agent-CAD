"""BUG-037: fork_end used to silently fall through to the "z" branch for
any unrecognized bore_axis value (e.g. "x", "garbage"). An LLM typo would
produce a vertical bore without any signal. Reject upfront.

No external LLM calls.
"""
from __future__ import annotations

import unittest

from mac_assembly.builders import fork_end


class TestForkEndBoreAxisValidation(unittest.TestCase):
    def test_bore_axis_y_ok(self):
        fork_end(bar_length=20, bar_width=20, bar_thickness=5,
                 ear_length=10, ear_spacing=8, bore_radius=2, bore_axis="y")

    def test_bore_axis_z_ok(self):
        fork_end(bar_length=20, bar_width=20, bar_thickness=5,
                 ear_length=10, ear_spacing=8, bore_radius=2, bore_axis="z")

    def test_bore_axis_x_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            fork_end(bar_length=20, bar_width=20, bar_thickness=5,
                     ear_length=10, ear_spacing=8, bore_radius=2,
                     bore_axis="x")
        self.assertIn("bore_axis", str(ctx.exception))

    def test_bore_axis_garbage_rejected(self):
        with self.assertRaises(ValueError):
            fork_end(bar_length=20, bar_width=20, bar_thickness=5,
                     ear_length=10, ear_spacing=8, bore_radius=2,
                     bore_axis="garbage")


if __name__ == "__main__":
    unittest.main()
