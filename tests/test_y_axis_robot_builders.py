"""Regression checks for deterministic Y-axis robot-assembly builders."""

from mac_assembly.builders import build_part


def test_y_axis_robot_builders_are_connected_and_have_expected_bounds():
    expected = {
        "y_axis_truss_clevis_link": ((-18, -22, -18), (198, 22, 18)),
        "y_axis_wrist_carrier": ((-18, -27, -23), (78, 27, 23)),
        "y_axis_tilt_yoke": ((-50, -62, -70), (50, 62, 0)),
        "symmetric_sensor_pod": ((0, -37, -33), (74, 37, 33)),
        "y_axis_motor_pod": ((-18, -6, -18), (18, 9, 18)),
        "y_axis_bearing_cap": ((-14, -4, -14), (14, 2, 14)),
    }
    for name, (want_min, want_max) in expected.items():
        shape = build_part(name, {})
        assert len(shape.solids()) == 1, f"{name} must be one connected solid"
        bbox = shape.bounding_box()
        for actual, wanted in zip(tuple(bbox.min), want_min):
            assert abs(actual - wanted) < 1e-6
        for actual, wanted in zip(tuple(bbox.max), want_max):
            assert abs(actual - wanted) < 1e-6


def test_y_axis_hinge_pin_fits_fork_bore():
    link = build_part(
        "y_axis_truss_clevis_link",
        {"fork_bore_radius": 6.0, "pin_radius": 5.5},
    )
    assert len(link.solids()) == 1
    assert 6.0 - 5.5 == 0.5


def test_y_axis_retained_pin_heads_form_one_solid_and_span_fork():
    link = build_part(
        "y_axis_truss_clevis_link",
        {
            "fork_ear_thickness": 10.0,
            "fork_ear_center_y": 22.0,
            "tongue_thickness": 30.0,
            "pin_radius": 7.4,
            "pin_length": 56.0,
            "pin_cap_radius": 10.0,
            "pin_cap_thickness": 3.0,
        },
    )
    assert len(link.solids()) == 1
    bbox = link.bounding_box()
    # Fork outer faces are at Y=+-27; retained heads sit immediately outside.
    assert bbox.min.Y <= -30.0
    assert bbox.max.Y >= 30.0




def test_y_axis_tilt_yoke_has_two_coaxial_bores_and_one_solid():
    shape = build_part("y_axis_tilt_yoke", {})
    assert len(shape.solids()) == 1
    # Two R8 through-bores remove approximately 2*pi*r^2*depth volume.
    unbored = 100 * 124 * 10 + 2 * (100 * 18 * 62) - 2 * (100 * 18 * 2)
    expected = unbored - 2 * 3.141592653589793 * 8**2 * 18
    assert abs(shape.volume - expected) < 1.0


def test_symmetric_sensor_pod_optional_y_pivots_extend_both_sides():
    shape = build_part(
        "symmetric_sensor_pod",
        {
            "body_length": 92,
            "body_width": 82,
            "body_height": 68,
            "lens_radius": 18,
            "boss_radius": 27,
            "boss_depth": 8,
            "pivot_radius": 7.7,
            "pivot_length": 10,
        },
    )
    bbox = shape.bounding_box()
    assert round(bbox.min.Y, 6) == -51.0
    assert round(bbox.max.Y, 6) == 51.0
    assert len(shape.solids()) == 1
    assert shape.is_valid
