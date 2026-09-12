"""P1-9: snap-to-surface attachment semantics + disjoint-feature failure.

Also covers the no-rtree proximity fallback (closest_point_robust): a
missing optional dependency must cost time, never correctness -- it used
to raise inside _snap_to_surface and surface as a "geometry failure".

Real build123d geometry, no external LLM calls.
"""
from __future__ import annotations

import unittest
from unittest import mock

from build123d import Align, Box, Pos

from mac_assembly.feature_operators import _snap_to_surface, apply_feature
from mac_assembly.schemas_assembly import Feature, FeatureAttachment


def _plate():
    """60x40x12 plate: X=-30..30, Y=-20..20, Z=0..12."""
    return Pos(0, 0, 6) * Box(60, 40, 12, align=(Align.CENTER,) * 3)


def _feature(name, params, attach, direction, surface_axis=None):
    att = {"attach_point_mm": list(attach), "direction": direction}
    if surface_axis is not None:
        att["surface_axis"] = surface_axis
    return Feature(name=name, params=params, attachment=FeatureAttachment(**att))


FORK_PARAMS = {"ear_length": 20, "ear_width": 16, "tongue_thickness": 3,
               "bore_radius": 2.5, "bar_thickness": 12, "clearance_side": 0.1,
               "pin_axis": "z"}


class TestSnapToSurfaceAxis(unittest.TestCase):
    def test_top_face_attach_with_explicit_surface_axis_snaps(self):
        # Attach 1mm above the top face (Z=10 on a Z=0..10 test box),
        # feature protruding +y: the surface normal is +z, NOT -direction.
        base = Pos(0, 0, 5) * Box(10, 10, 10, align=(Align.CENTER,) * 3)
        pt, warning = _snap_to_surface(base, [0.0, 0.0, 11.0], "+y",
                                       surface_axis="+z")
        self.assertIsNone(warning)
        self.assertAlmostEqual(pt[2], 10.0, places=3)

    def test_legacy_default_rejects_perpendicular_normal(self):
        base = Pos(0, 0, 5) * Box(10, 10, 10, align=(Align.CENTER,) * 3)
        pt, warning = _snap_to_surface(base, [0.0, 0.0, 11.0], "+y")
        self.assertIsNotNone(warning)
        self.assertIn("tilted", warning)
        # reverted to the original attach point
        self.assertAlmostEqual(pt[2], 11.0, places=6)

    def test_within_tolerance_no_snap(self):
        base = Pos(0, 0, 5) * Box(10, 10, 10, align=(Align.CENTER,) * 3)
        pt, warning = _snap_to_surface(base, [0.0, 0.0, 10.2], "+z")
        self.assertIsNone(warning)
        self.assertAlmostEqual(pt[2], 10.2, places=6)

    def test_aligned_direction_still_snaps_as_before(self):
        base = Pos(0, 0, 5) * Box(10, 10, 10, align=(Align.CENTER,) * 3)
        pt, warning = _snap_to_surface(base, [0.0, 0.0, 12.0], "+z")
        self.assertIsNone(warning)
        self.assertAlmostEqual(pt[2], 10.0, places=3)


class TestAdditiveConnectionEnforcement(unittest.TestCase):
    def test_side_face_fork_fuses(self):
        feat = _feature("clevis_fork", FORK_PARAMS, (0.0, 20.0, 0.0), "+y")
        out = apply_feature(_plate(), feat)
        self.assertEqual(len(out.solids()), 1)

    def test_top_face_knuckle_ear_fuses(self):
        # Ear cylinder is CENTRED on the attach point: on the top face with
        # a lateral +y direction, half the ear embeds -> connected.
        feat = _feature("knuckle_ear",
                        {"ear_radius": 4.0, "ear_length": 8.0,
                         "bore_radius": 1.5},
                        (0.0, 0.0, 12.0), "+y")
        out = apply_feature(_plate(), feat)
        self.assertEqual(len(out.solids()), 1)

    def test_disjoint_additive_feature_fails_loudly(self):
        # Attach far off the +X side with direction +y: the snap reverts
        # (surface normal +x is perpendicular to ±y), the fork lands 170mm
        # away from the plate -> fully disjoint -> must fail loudly.
        feat = _feature("clevis_fork", FORK_PARAMS, (200.0, 0.0, 6.0), "+y")
        with self.assertRaises(RuntimeError) as ctx:
            apply_feature(_plate(), feat)
        self.assertIn("disjoint", str(ctx.exception))

    def test_partially_fused_feature_fails_loudly(self):
        # Attach on the +Y side face but at mid-height (Z=6) with a 12mm
        # fork: the lower ear fuses, the UPPER ear floats above the plate
        # (Z=13.6..18 vs plate Z<=12). A half-attached kinematic feature
        # is just as broken as a fully disjoint one -> must fail.
        feat = _feature("clevis_fork", FORK_PARAMS, (0.0, 20.0, 6.0), "+y")
        with self.assertRaises(RuntimeError) as ctx:
            apply_feature(_plate(), feat)
        self.assertIn("did not fuse", str(ctx.exception))

    def test_disjoint_allowed_when_explicitly_opted_in(self):
        params = {**FORK_PARAMS, "allow_disjoint": True}
        feat = _feature("clevis_fork", params, (200.0, 0.0, 6.0), "+y")
        out = apply_feature(_plate(), feat)  # no raise
        # A clevis fork is TWO Z-separated ear solids; unfused with the
        # base they stay separate -> base(1) + ears(2) = 3 solids.
        self.assertEqual(len(out.solids()), 3)

    def test_subtractive_through_bore_unaffected(self):
        plate = _plate()
        feat = _feature("through_bore", {"radius": 3.0}, (0.0, 0.0, 6.0), "+z")
        out = apply_feature(plate, feat)
        self.assertEqual(len(out.solids()), 1)
        self.assertLess(out.volume, plate.volume)

    def test_subtractive_disjoint_still_refuses(self):
        feat = _feature("through_bore", {"radius": 3.0, "height": 4.0},
                        (0.0, 200.0, 6.0), "+z")
        with self.assertRaises(RuntimeError):
            apply_feature(_plate(), feat)


class TestProximityNoRtreeFallback(unittest.TestCase):
    """The accelerated trimesh.proximity.closest_point raises at call time
    when the optional rtree package is missing; closest_point_robust must
    fall back to the identical naive query."""

    def test_closest_point_robust_falls_back_to_naive(self):
        import trimesh

        from mac_assembly.geometry_utils import closest_point_robust

        mesh = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
        pts = [[0.0, 0.0, 8.0]]  # 3mm above the top face
        with mock.patch(
            "trimesh.proximity.closest_point",
            side_effect=ImportError("rtree must be installed"),
        ):
            closest, dist, tri = closest_point_robust(mesh, pts)
        ref_closest, ref_dist, _ref_tri = trimesh.proximity.closest_point_naive(
            mesh, pts
        )
        self.assertAlmostEqual(float(dist[0]), float(ref_dist[0]), places=6)
        self.assertAlmostEqual(float(dist[0]), 3.0, places=6)
        for got, want in zip(closest[0], ref_closest[0]):
            self.assertAlmostEqual(float(got), float(want), places=6)

    def test_closest_point_robust_uses_fast_path_when_available(self):
        import trimesh

        from mac_assembly.geometry_utils import closest_point_robust

        mesh = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
        _c, dist, _t = closest_point_robust(mesh, [[0.0, 0.0, 6.0]])
        self.assertAlmostEqual(float(dist[0]), 1.0, places=6)

    def test_snap_to_surface_survives_missing_rtree(self):
        # End-to-end through _snap_to_surface: with the accelerated query
        # raising, the snap must still land exactly on the top face.
        base = Pos(0, 0, 5) * Box(10, 10, 10, align=(Align.CENTER,) * 3)
        with mock.patch(
            "trimesh.proximity.closest_point",
            side_effect=ImportError("rtree must be installed"),
        ):
            pt, warning = _snap_to_surface(base, [0.0, 0.0, 12.0], "+z")
        self.assertIsNone(warning)
        self.assertAlmostEqual(pt[2], 10.0, places=3)


class TestMeshContainmentProbe(unittest.TestCase):
    """The functional rtree probe must reflect the real containment
    capability (a silent False negative here would hide skipped
    interference/kinematic pairs from the QA environment warning)."""

    def test_probe_matches_real_containment(self):
        import trimesh

        from mac_assembly import geometry_utils as gu

        gu._MESH_CONTAINMENT_PROBE = None  # force a fresh probe
        try:
            got = gu.mesh_containment_available()
            box = trimesh.creation.box(extents=(2.0, 2.0, 2.0))
            try:
                expected = bool(box.contains([[0.0, 0.0, 0.0]])[0])
            except Exception:  # noqa: BLE001 - rtree missing
                expected = False
            self.assertEqual(got, expected)
            # Cached on the second call.
            self.assertEqual(gu.mesh_containment_available(), expected)
        finally:
            gu._MESH_CONTAINMENT_PROBE = None

    def test_probe_false_when_containment_raises(self):
        from mac_assembly import geometry_utils as gu

        gu._MESH_CONTAINMENT_PROBE = None
        try:
            with mock.patch(
                "trimesh.creation.box",
                side_effect=ModuleNotFoundError("No module named 'rtree'"),
            ):
                self.assertFalse(gu.mesh_containment_available())
        finally:
            gu._MESH_CONTAINMENT_PROBE = None


if __name__ == "__main__":
    unittest.main()
