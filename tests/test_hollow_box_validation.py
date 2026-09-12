"""BUG-026: hollow_box had no parameter validation. A user could pass
outer_h < floor_t (negative inner height) or wall_t >= outer_w/2 (no
inner cavity) and produce degenerate geometry. Add explicit validation.

No external LLM calls; pure builder validation.
"""
from __future__ import annotations

import unittest

from mac_assembly.builders import hollow_box


class TestHollowBoxValidation(unittest.TestCase):
    def test_valid_params_succeed(self):
        # 30x20x10 box with 2mm walls and 2mm floor.
        result = hollow_box(outer_w=30, outer_d=20, outer_h=10, wall_t=2.0,
                            floor_t=2.0)
        self.assertIsNotNone(result)

    def test_negative_outer_dim_rejected(self):
        with self.assertRaises(Exception):
            hollow_box(outer_w=-1, outer_d=20, outer_h=10, wall_t=2.0)

    def test_zero_outer_dim_rejected(self):
        with self.assertRaises(Exception):
            hollow_box(outer_w=0, outer_d=20, outer_h=10, wall_t=2.0)

    def test_negative_wall_t_rejected(self):
        with self.assertRaises(Exception):
            hollow_box(outer_w=30, outer_d=20, outer_h=10, wall_t=-1.0)

    def test_wall_too_thick_rejected(self):
        # 2*wall_t >= outer_w means no inner cavity.
        with self.assertRaises(Exception):
            hollow_box(outer_w=10, outer_d=20, outer_h=10, wall_t=6.0)

    def test_floor_t_too_thick_rejected(self):
        # floor_t >= outer_h means no interior height.
        with self.assertRaises(Exception):
            hollow_box(outer_w=30, outer_d=20, outer_h=5, wall_t=2.0,
                       floor_t=10.0)


if __name__ == "__main__":
    unittest.main()
