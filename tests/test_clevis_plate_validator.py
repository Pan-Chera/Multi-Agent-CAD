"""P1-5: clevis_palm / clevis_base_with_fork plate-edge validation.

Deterministic builder geometry only -- no external LLM calls.
"""
from __future__ import annotations

import unittest

from mac_assembly.builders import _validate_fork_on_plate, build_part

# Plate 100x60 (X=-50..50, Y=-30..30), ear_length=20, ear_width=16 ->
# R_tip=8, body_len=12; legal bore range along +x: (50, 61.8].
PLATE_W, PLATE_D = 100.0, 60.0
EAR_LENGTH, EAR_WIDTH = 20.0, 16.0

BASE_PARAMS = dict(
    plate_w=PLATE_W, plate_d=PLATE_D, plate_t=8.0,
    ear_length=EAR_LENGTH, ear_width=EAR_WIDTH,
    tongue_thickness=3.0, bore_radius=2.5, clearance_side=0.1,
)


def _fork(x, y, d):
    return {"fork_x": x, "fork_y": y, "fork_direction": d}


class TestValidatorDirections(unittest.TestCase):
    def test_valid_plus_x(self):
        _validate_fork_on_plate([_fork(60, 0, "+x")], PLATE_W, PLATE_D,
                                EAR_LENGTH, EAR_WIDTH, "t")

    def test_valid_minus_x(self):
        _validate_fork_on_plate([_fork(-60, 0, "-x")], PLATE_W, PLATE_D,
                                EAR_LENGTH, EAR_WIDTH, "t")

    def test_valid_plus_y(self):
        _validate_fork_on_plate([_fork(0, 40, "+y")], PLATE_W, PLATE_D,
                                EAR_LENGTH, EAR_WIDTH, "t")

    def test_valid_minus_y(self):
        _validate_fork_on_plate([_fork(0, -40, "-y")], PLATE_W, PLATE_D,
                                EAR_LENGTH, EAR_WIDTH, "t")


class TestValidatorFailures(unittest.TestCase):
    def test_bore_inside_plate_fails(self):
        with self.assertRaises(ValueError) as ctx:
            _validate_fork_on_plate([_fork(40, 0, "+x")], PLATE_W, PLATE_D,
                                    EAR_LENGTH, EAR_WIDTH, "clevis_palm")
        msg = str(ctx.exception)
        self.assertIn("INSIDE", msg)
        self.assertIn("fork 0", msg)
        self.assertIn("+x", msg)
        self.assertIn("50.0", msg)  # plate edge in the message

    def test_bore_too_far_out_fails(self):
        with self.assertRaises(ValueError) as ctx:
            _validate_fork_on_plate([_fork(90, 0, "+x")], PLATE_W, PLATE_D,
                                    EAR_LENGTH, EAR_WIDTH, "clevis_palm")
        msg = str(ctx.exception)
        self.assertIn("too far outside", msg)
        self.assertIn("disjoint", msg)

    def test_lateral_offset_off_plate_fails(self):
        with self.assertRaises(ValueError) as ctx:
            _validate_fork_on_plate([_fork(60, 45, "+x")], PLATE_W, PLATE_D,
                                    EAR_LENGTH, EAR_WIDTH, "clevis_palm")
        self.assertIn("lateral", str(ctx.exception))

    def test_negative_direction_bounds(self):
        # -x mirror of the "too far" case
        with self.assertRaises(ValueError):
            _validate_fork_on_plate([_fork(-90, 0, "-x")], PLATE_W, PLATE_D,
                                    EAR_LENGTH, EAR_WIDTH, "clevis_palm")
        # -x mirror of the "inside" case
        with self.assertRaises(ValueError):
            _validate_fork_on_plate([_fork(-40, 0, "-x")], PLATE_W, PLATE_D,
                                    EAR_LENGTH, EAR_WIDTH, "clevis_palm")

    def test_unknown_direction_fails(self):
        with self.assertRaises(ValueError):
            _validate_fork_on_plate([_fork(60, 0, "+z")], PLATE_W, PLATE_D,
                                    EAR_LENGTH, EAR_WIDTH, "clevis_palm")


class TestBuilderIntegration(unittest.TestCase):
    def test_clevis_base_with_fork_rejects_bore_inside_plate(self):
        with self.assertRaises(ValueError) as ctx:
            build_part("clevis_base_with_fork",
                       {**BASE_PARAMS, "fork_x": 40.0, "fork_y": 0.0,
                        "fork_direction": "+x"})
        self.assertIn("INSIDE", str(ctx.exception))

    def test_clevis_palm_rejects_too_far_bore(self):
        with self.assertRaises(ValueError) as ctx:
            build_part("clevis_palm",
                       {**BASE_PARAMS, "forks": [_fork(90, 0, "+x")]})
        self.assertIn("too far outside", str(ctx.exception))

    def test_clevis_palm_still_detects_fork_overlap(self):
        # Two +x forks 5mm apart laterally (ear_width=16 -> need >= 16mm)
        with self.assertRaises(ValueError) as ctx:
            build_part("clevis_palm",
                       {**BASE_PARAMS,
                        "forks": [_fork(60, -2.5, "+x"), _fork(60, 2.5, "+x")]})
        self.assertIn("overlap", str(ctx.exception))

    def test_clevis_palm_valid_multi_fork_builds(self):
        shape = build_part("clevis_palm", {
            **BASE_PARAMS,
            "forks": [_fork(-32, 40, "+y"), _fork(-16, 40, "+y"),
                      _fork(0, 40, "+y"), _fork(16, 40, "+y"),
                      _fork(32, 40, "+y")],
        })
        self.assertGreaterEqual(len(shape.solids()), 1)
        bb = shape.bounding_box()
        # v2 fork: bore at fork_y=40, tip circle R_tip=8 around it ->
        # forks protrude +Y past the plate edge (Y=30) up to Y=48
        self.assertAlmostEqual(bb.max.Y, 48.0, places=6)

    def test_clevis_base_with_fork_valid_builds(self):
        shape = build_part("clevis_base_with_fork",
                           {**BASE_PARAMS, "fork_x": 60.0, "fork_y": 0.0,
                            "fork_direction": "+x"})
        self.assertEqual(len(shape.solids()), 1)
        bb = shape.bounding_box()
        self.assertAlmostEqual(bb.max.X, 60.0 + 8.0, places=6)  # bore + R_tip


if __name__ == "__main__":
    unittest.main()
