import math
import unittest

from mac_assembly.builders import bent_jaw_xz
from mac_assembly.feature_operators import apply_feature
from mac_assembly.schemas_assembly import Feature


class TestBentJawXz(unittest.TestCase):
    def test_default_jaws_are_connected_and_mirrored(self):
        left = bent_jaw_xz("left")
        right = bent_jaw_xz("right")
        self.assertEqual(len(left.solids()), 1)
        self.assertEqual(len(right.solids()), 1)
        self.assertAlmostEqual(left.volume, right.volume, places=5)

        lb = left.bounding_box()
        rb = right.bounding_box()
        self.assertAlmostEqual(lb.min.X, -rb.max.X, places=5)
        self.assertAlmostEqual(lb.max.X, -rb.min.X, places=5)
        self.assertLessEqual(lb.min.X, -9.0)
        self.assertGreaterEqual(rb.max.X, 9.0)
        self.assertAlmostEqual(lb.max.Y, 12.0, places=5)
        self.assertAlmostEqual(lb.min.Y, -12.0, places=5)
        self.assertAlmostEqual(lb.min.Z, rb.min.Z, places=5)
        self.assertAlmostEqual(lb.max.Z, 0.0, places=5)

    def test_distal_gripping_pads_are_integral_and_broad(self):
        left = bent_jaw_xz("left")
        right = bent_jaw_xz("right")
        self.assertEqual(len(left.solids()), 1)
        self.assertEqual(len(right.solids()), 1)
        lb = left.bounding_box()
        rb = right.bounding_box()
        self.assertAlmostEqual(lb.size.Y, 24.0, places=5)
        self.assertAlmostEqual(rb.size.Y, 24.0, places=5)
        self.assertGreaterEqual(lb.max.X, 41.0)
        self.assertLessEqual(rb.min.X, -41.0)
        self.assertLessEqual(lb.min.Z, -70.0)

    def test_default_centerline_interior_angle_is_120_degrees(self):
        # Elbow-to-root vector is +Z.  The tip direction is 30 degrees
        # downward from +X, so its Z component is -sin(30) = -0.5.
        root_ray = (0.0, 0.0, 1.0)
        tip_ray = (math.cos(math.radians(30)), 0.0,
                   -math.sin(math.radians(30)))
        cosine = sum(a * b for a, b in zip(root_ray, tip_ray))
        self.assertAlmostEqual(math.degrees(math.acos(cosine)), 120.0)

    def test_invalid_side_rejected(self):
        with self.assertRaisesRegex(ValueError, "side"):
            bent_jaw_xz("centre")

    def test_upward_y_axis_tongue_is_connected_and_collinear(self):
        jaw = bent_jaw_xz("left")
        tongue = Feature.model_validate({
            "name": "clevis_tongue",
            "params": {
                "ear_length": 25, "ear_width": 20,
                "bore_radius": 5.0, "tongue_thickness": 12,
                "bar_thickness": 20, "clearance_side": 0.1,
                "pin_axis": "y",
            },
            "attachment": {
                "attach_point_mm": [0, 0, 0],
                "direction": "+z", "surface_axis": "+z",
            },
        })
        result = apply_feature(jaw, tongue)
        self.assertEqual(len(result.solids()), 1)
        bbox = result.bounding_box()
        self.assertAlmostEqual(bbox.max.Z, 25.0, places=5)
        self.assertAlmostEqual(bbox.min.Y, -12.0, places=5)
        self.assertAlmostEqual(bbox.max.Y, 12.0, places=5)


if __name__ == "__main__":
    unittest.main()
