"""P0-1 / P1-6: world-axis transforms in AssemblyQA.

Covers the local->world DIRECTION helper (rotation only, degenerate-safe),
the chained-joint kinematic sweep (a moved fixed part's local axis must be
rotated into world before sweeping -- revolute SELECTOR axis AND
linear/cylindrical slide axis), and the moved-part FACE anchor datum
(local face centre + normal through the placed Location, never the rotated
world bbox face).

No external LLM calls: the selector resolver is monkeypatched and meshes
are built with trimesh primitives.
"""
from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import trimesh
from build123d import Align, Box, Rot, export_step

from mac_assembly import assembly_qa as qa
from mac_assembly.schemas_assembly import (
    Anchor,
    AnchorKind,
    FaceQuery,
    MateSpec,
    MateType,
)


def _box_mesh(center, size=2.0):
    return trimesh.creation.box(
        extents=(size, size, size),
        transform=trimesh.transformations.translation_matrix(center),
    )


def _placed(meshes_by_label):
    placed = []
    for label, mesh_list in meshes_by_label.items():
        bb = np.vstack([m.bounds for m in mesh_list])
        placed.append((label, tuple(bb.min(axis=0)), tuple(bb.max(axis=0))))
    return placed


def _revolute(mate_id, fixed, moving, anchor):
    return MateSpec(
        mate_id=mate_id,
        mate_type=MateType.REVOLUTE,
        fixed_part_id=fixed,
        moving_part_id=moving,
        fixed_anchor=anchor,
        moving_anchor=anchor,
    )


class TestLocalToWorldDirection(unittest.TestCase):
    def test_rotation_only_no_translation(self):
        # Rotate about X by +90 deg: (0,0,1) -> (0,-1,0); translation must
        # NOT leak into a direction.
        loc = ((100.0, 50.0, -30.0), (90.0, 0.0, 0.0))
        d = qa._local_to_world_direction((0.0, 0.0, 1.0), loc)
        self.assertIsNotNone(d)
        for got, want in zip(d, (0.0, -1.0, 0.0)):
            self.assertAlmostEqual(got, want, places=9)

    def test_identity_location_keeps_direction(self):
        d = qa._local_to_world_direction((1.0, 0.0, 0.0), ((3, 4, 5), (0, 0, 0)))
        for got, want in zip(d, (1.0, 0.0, 0.0)):
            self.assertAlmostEqual(got, want, places=9)

    def test_yaw_90_rotates_x_to_y(self):
        d = qa._local_to_world_direction((1.0, 0.0, 0.0), ((0, 0, 0), (0, 0, 90)))
        for got, want in zip(d, (0.0, 1.0, 0.0)):
            self.assertAlmostEqual(got, want, places=9)

    def test_no_location_data_returns_local_approximation(self):
        # Legacy manifest without `location`: documented pure-translation
        # approximation -- the local direction is returned unchanged.
        d = qa._local_to_world_direction((0.0, 1.0, 0.0), None)
        self.assertEqual(d, (0.0, 1.0, 0.0))

    def test_result_is_normalized(self):
        d = qa._local_to_world_direction((0.0, 0.0, 7.5), ((1, 2, 3), (30, 40, 0)))
        self.assertAlmostEqual(math.sqrt(sum(v * v for v in d)), 1.0, places=9)


class TestMovedPartFaceAnchorDatum(unittest.TestCase):
    """P1-6: a rotated part's LOCAL top face must not be replaced by the
    rotated world bbox's top face."""

    def setUp(self):
        patcher = mock.patch.object(
            qa, "_orig_bbox", return_value=((0.0, 0.0, 0.0), (10.0, 10.0, 10.0))
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_moved_part_face_datum_transforms(self):
        anchor = Anchor(kind=AnchorKind.FACE, face="top")
        bboxes = {"partA": ((0.0, 0.0, 0.0), (10.0, 10.0, 10.0))}
        locations = {"partA": ((10.0, 0.0, 0.0), (90.0, 0.0, 0.0))}
        pt, direction, err = qa._anchor_world_datum(
            anchor, "partA", bboxes, {"partA": "/fake.step"}, locations,
            moved_set={"partA"},
        )
        self.assertIsNone(err)
        # Local top-face centre (5,5,10) through Rx90 -> (5,-10,5) + t.
        for got, want in zip(pt, (15.0, -10.0, 5.0)):
            self.assertAlmostEqual(got, want, places=6)
        # Local top normal (0,0,1) through Rx90 -> (0,-1,0).
        for got, want in zip(direction, (0.0, -1.0, 0.0)):
            self.assertAlmostEqual(got, want, places=9)

    def test_unmoved_part_keeps_legacy_bbox_algebra(self):
        anchor = Anchor(kind=AnchorKind.FACE, face="top")
        bboxes = {"partB": ((0.0, 0.0, 0.0), (10.0, 10.0, 10.0))}
        pt, direction, err = qa._anchor_world_datum(
            anchor, "partB", bboxes, {}, {}, moved_set=set(),
        )
        self.assertIsNone(err)
        self.assertIsNone(direction)
        for got, want in zip(pt, (5.0, 5.0, 10.0)):
            self.assertAlmostEqual(got, want, places=6)

    def test_moved_part_without_step_fails_loudly(self):
        anchor = Anchor(kind=AnchorKind.FACE, face="top")
        pt, direction, err = qa._anchor_world_datum(
            anchor, "partC", {"partC": ((0, 0, 0), (10, 10, 10))},
            {}, {"partC": ((0, 0, 0), (0, 0, 0))}, moved_set={"partC"},
        )
        self.assertIsNone(pt)
        self.assertIsNotNone(err)
        self.assertIn("cannot be", err)


class TestChainedKinematicSweep(unittest.TestCase):
    """P0-1: the fixed part of a child joint was rotated by the parent
    mate; the child's local joint axis must be swept in WORLD coords."""

    def _selector_anchor(self):
        return Anchor(
            kind=AnchorKind.SELECTOR,
            selector_query=FaceQuery(surface="cylinder", axis="x"),
        )

    def test_revolute_selector_axis_transformed_for_moved_fixed_part(self):
        # mid is rotated 90 deg about Z by mate1; its local bore axis +X
        # becomes world +Y. tip sits at (10,0,0); sweeping about the world
        # Y axis through the origin moves tip to (+-8.66, 0, -+5) and into
        # the blocker at (8.66, 0, 5) at -30 deg. Sweeping about the LOCAL
        # X axis (the old bug) leaves tip exactly on the axis -- no motion,
        # no collision, silent false PASS.
        meshes = {
            "root": [_box_mesh((0.0, 0.0, -30.0), 4.0)],
            "mid": [_box_mesh((0.0, 0.0, 0.0), 2.0)],
            "tip": [_box_mesh((10.0, 0.0, 0.0), 2.0)],
            "blocker": [_box_mesh((8.66, 0.0, 5.0), 2.5)],
        }
        locations = {
            "root": ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0)),
            "mid": ((0.0, 0.0, 0.0), (0.0, 0.0, 90.0)),
            "tip": ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0)),
            "blocker": ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0)),
        }
        mate1 = _revolute(
            "m1", "root", "mid",
            Anchor(kind=AnchorKind.FACE, face="top"),
        )
        mate2 = _revolute("m2", "mid", "tip", self._selector_anchor())
        fake_resolved = {
            "point": (0.0, 0.0, 0.0),
            "axis": (1.0, 0.0, 0.0),
            "selector": "f0",
            "surface": "cylinder",
        }
        # P1-1: _check_kinematics now uses _resolve_qa_anchor_3state
        # (shared with _check_mates) so SELECTOR miss / probe error fails
        # closed instead of falling back to bbox algebra. Mock the 3state
        # helper to provide the resolved axis for this world-axis test.
        with mock.patch.object(
            qa, "_resolve_qa_anchor_3state",
            return_value=("found", fake_resolved),
        ):
            checks = qa._check_kinematics(
                _placed(meshes), [mate1, mate2], meshes,
                step_paths={}, locations=locations,
            )
        by_id = {c.mate_id: c for c in checks}
        self.assertIn("m2", by_id)
        chk = by_id["m2"]
        self.assertEqual(chk.sweep_unit, "deg")
        self.assertFalse(
            chk.passed,
            f"world-axis sweep should hit the blocker; detail={chk.detail}",
        )
        self.assertIn("collision", chk.detail.lower())
        self.assertIn("tip", chk.detail)

    def test_linear_slide_axis_transformed_for_moved_fixed_part(self):
        # mid rotated 90 deg about Z: its local slide axis x becomes world
        # y. The slider at the origin translated +-20mm along world Y hits
        # the blocker at (0,9.5,0) at the +10mm sample (1.5mm overlap --
        # deep enough for the depth-tolerant collision test); along the
        # LOCAL (wrong) world X axis it never moves toward the blocker.
        # mid sits far away at (0,-40,0) so it cannot cause the collision
        # itself.
        meshes = {
            "mid": [_box_mesh((0.0, -40.0, 0.0), 2.0)],
            "slider": [_box_mesh((0.0, 0.0, 0.0), 2.0)],
            "blocker": [_box_mesh((0.0, 9.5, 0.0), 2.0)],
        }
        locations = {
            "mid": ((0.0, 0.0, 0.0), (0.0, 0.0, 90.0)),
            "slider": ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0)),
            "blocker": ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0)),
        }
        mate1 = _revolute(
            "m_root", "blocker", "mid",
            Anchor(kind=AnchorKind.FACE, face="top"),
        )
        mate2 = MateSpec(
            mate_id="m_slide",
            mate_type=MateType.LINEAR,
            fixed_part_id="mid",
            moving_part_id="slider",
            fixed_anchor=Anchor(kind=AnchorKind.AXIS_POINT, axis="x"),
            moving_anchor=Anchor(kind=AnchorKind.AXIS_POINT, axis="x"),
            slide_axis="x",
        )
        with mock.patch.object(qa, "_resolve_qa_anchor", return_value=None):
            checks = qa._check_kinematics(
                _placed(meshes), [mate1, mate2], meshes,
                step_paths={}, locations=locations,
            )
        by_id = {c.mate_id: c for c in checks}
        chk = by_id["m_slide"]
        self.assertEqual(chk.sweep_unit, "mm")
        self.assertFalse(
            chk.passed,
            f"world-Y slide should hit the blocker; detail={chk.detail}",
        )
        self.assertIn("collision", chk.detail.lower())


class TestMovedPartLocationGate(unittest.TestCase):
    """P2 follow-up: a moved part without manifest location data is
    NEVER approximated. AABB equality is not rotation evidence -- a 180
    deg turn about any principal axis preserves the ordered dimensions
    of EVERY part (dimension-symmetric parts additionally hide 90 deg
    turns) -- so the gate has no evidence path at all."""

    def test_location_present_returns_it(self):
        loc = ((1.0, 2.0, 3.0), (0.0, 0.0, 0.0))
        got, err = qa._moved_part_location("p", {"p": loc})
        self.assertEqual(got, loc)
        self.assertIsNone(err)

    def test_no_location_is_unverifiable_even_with_matching_dims(self):
        # Matching ordered bbox dims used to be accepted as "unrotated"
        # evidence; they prove nothing, so the gate refuses anyway.
        got, err = qa._moved_part_location("p", {})
        self.assertIsNone(got)
        self.assertIsNotNone(err)
        self.assertIn("no location", err)
        self.assertIn("180 deg", err)

    def test_180deg_yaw_counterexample_real_geometry(self):
        # The counterexample that killed the dimension-evidence heuristic,
        # built from real geometry: an asymmetric Box(10,4,2) yawed 180
        # deg has a provably IDENTICAL AABB to its own original STEP --
        # yet its local top/right/front datums now point the opposite
        # way. A moved part without a manifest location must therefore
        # stay UNVERIFIABLE rather than pass on dimensions.
        with tempfile.TemporaryDirectory() as tmp:
            step = Path(tmp) / "asym.step"
            export_step(
                Box(10, 4, 2, align=(Align.CENTER,) * 3), str(step)
            )
            bb = (
                Rot(0, 0, 180)
                * Box(10, 4, 2, align=(Align.CENTER,) * 3)
            ).bounding_box()
            placed = (
                (bb.min.X, bb.min.Y, bb.min.Z),
                (bb.max.X, bb.max.Y, bb.max.Z),
            )
            orig = qa._orig_bbox(str(step))
            # Premise: the 180 deg rotation leaves the AABB identical.
            for k in range(3):
                self.assertAlmostEqual(
                    (placed[1][k] - placed[0][k])
                    - (orig[1][k] - orig[0][k]),
                    0.0,
                    places=6,
                )
        # Verdict: still no location -> still UNVERIFIABLE.
        got, err = qa._moved_part_location("p", {})
        self.assertIsNone(got)
        self.assertIsNotNone(err)
        self.assertIn("no location", err)


class TestMateCheckPlacementGate(unittest.TestCase):
    """_check_mates / _check_kinematics refuse to guess datums for moved
    parts that carry no manifest location -- no approximation is accepted."""

    def _f2f(self):
        return MateSpec(
            mate_id="m1",
            mate_type=MateType.FACE_TO_FACE,
            fixed_part_id="root",
            moving_part_id="lid",
            fixed_anchor=Anchor(kind=AnchorKind.FACE, face="top"),
            moving_anchor=Anchor(kind=AnchorKind.FACE, face="bottom"),
            offset_mm=0.0,
            tolerance_mm=0.5,
        )

    _PLACED = [
        ("root", (0.0, 0.0, 0.0), (10.0, 10.0, 10.0)),
        ("lid", (0.0, 0.0, 10.0), (10.0, 10.0, 12.0)),
    ]

    def test_moved_part_without_location_fails(self):
        checks = qa._check_mates([self._f2f()], self._PLACED, None, {}, {})
        chk = checks[0]
        self.assertFalse(chk.passed)
        self.assertIn("UNVERIFIABLE", chk.detail)
        self.assertIn("no location", chk.detail)

    def test_legacy_pure_translation_is_still_unverifiable(self):
        # A genuinely unrotated legacy pose (the lid really was only
        # translated: dims (10,10,2) would match the STEP) is refused
        # all the same: AABB equality cannot PROVE the absence of
        # rotation, so moved parts get no approximation at all.
        checks = qa._check_mates(
            [self._f2f()], self._PLACED, None, {"lid": "/lid.step"}, {}
        )
        chk = checks[0]
        self.assertFalse(chk.passed)
        self.assertIn("UNVERIFIABLE", chk.detail)
        self.assertIn("no location", chk.detail)

    def test_degenerate_principal_axis_transform_fails_explicitly(self):
        # Chained revolute: mid was moved by m1; m2's FACE-anchored pivot
        # axis must be transformed through mid's location. A degenerate
        # transform used to silently keep the UNROTATED axis; it must now
        # fail the check.
        placed = [
            ("root", (0.0, 0.0, 0.0), (10.0, 10.0, 10.0)),
            ("mid", (0.0, 0.0, 10.0), (10.0, 10.0, 20.0)),
            ("tip", (0.0, 0.0, 20.0), (10.0, 10.0, 22.0)),
        ]
        mate1 = _revolute(
            "m1", "root", "mid", Anchor(kind=AnchorKind.FACE, face="top")
        )
        mate2 = _revolute(
            "m2", "mid", "tip", Anchor(kind=AnchorKind.FACE, face="top")
        )
        locations = {
            "root": ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0)),
            "mid": ((0.0, 0.0, 10.0), (0.0, 0.0, 0.0)),
            "tip": ((0.0, 0.0, 20.0), (0.0, 0.0, 0.0)),
        }
        with mock.patch.object(
            qa, "_orig_bbox",
            side_effect=lambda p: ((0.0, 0.0, 0.0), (10.0, 10.0, 10.0)),
        ), mock.patch.object(qa, "_local_to_world_direction", return_value=None):
            checks = qa._check_mates(
                [mate1, mate2], placed, None, {"mid": "/mid.step"}, locations
            )
        by_id = {c.mate_id: c for c in checks}
        self.assertFalse(by_id["m2"].passed)
        self.assertIn("UNVERIFIABLE", by_id["m2"].detail)
        self.assertIn("degenerate", by_id["m2"].detail)

    def test_degenerate_selector_axis_transform_fails_explicitly(self):
        placed = [
            ("root", (0.0, 0.0, 0.0), (10.0, 10.0, 10.0)),
            ("mid", (0.0, 0.0, 10.0), (10.0, 10.0, 20.0)),
            ("tip", (0.0, 0.0, 20.0), (10.0, 10.0, 22.0)),
        ]
        mate1 = _revolute(
            "m1", "root", "mid", Anchor(kind=AnchorKind.FACE, face="top")
        )
        mate2 = _revolute(
            "m2", "mid", "tip",
            Anchor(
                kind=AnchorKind.SELECTOR,
                selector_query=FaceQuery(surface="cylinder", axis="x"),
            ),
        )
        locations = {
            "root": ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0)),
            "mid": ((0.0, 0.0, 10.0), (0.0, 0.0, 0.0)),
            "tip": ((0.0, 0.0, 20.0), (0.0, 0.0, 0.0)),
        }
        resolved = {
            "point": (0.0, 0.0, 5.0), "axis": (1.0, 0.0, 0.0),
            "selector": "f0", "surface": "cylinder",
            "candidate_count": 1,
        }
        with mock.patch.object(
            qa, "_resolve_qa_point_3state",
            return_value=("found", (0.0, 0.0, 15.0)),
        ), mock.patch.object(
            qa, "_resolve_qa_anchor_3state",
            return_value=("found", resolved),
        ), mock.patch.object(qa, "_resolve_qa_anchor", return_value=resolved), \
             mock.patch.object(
                 qa, "_local_to_world_direction", return_value=None
             ):
            checks = qa._check_mates(
                [mate1, mate2], placed, None, {"mid": "/mid.step"}, locations
            )
        by_id = {c.mate_id: c for c in checks}
        self.assertFalse(by_id["m2"].passed)
        self.assertIn("UNVERIFIABLE", by_id["m2"].detail)
        self.assertIn("selector axis", by_id["m2"].detail)


class TestKinematicsPlacementGate(unittest.TestCase):
    def test_moved_fixed_part_without_location_fails_sweep(self):
        # Same chained setup as the world-axis test, but mid's manifest
        # location is missing: the sweep axis would be a guess ->
        # explicit UNVERIFIABLE failure (no approximation is accepted).
        meshes = {
            "root": [_box_mesh((0.0, 0.0, -30.0), 4.0)],
            "mid": [_box_mesh((0.0, 0.0, 0.0), 2.0)],
            "tip": [_box_mesh((10.0, 0.0, 0.0), 2.0)],
        }
        locations = {"root": ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))}
        mate1 = _revolute(
            "m1", "root", "mid", Anchor(kind=AnchorKind.FACE, face="top")
        )
        mate2 = _revolute(
            "m2", "mid", "tip",
            Anchor(
                kind=AnchorKind.SELECTOR,
                selector_query=FaceQuery(surface="cylinder", axis="x"),
            ),
        )
        checks = qa._check_kinematics(
            _placed(meshes), [mate1, mate2], meshes,
            step_paths={}, locations=locations,
        )
        by_id = {c.mate_id: c for c in checks}
        self.assertFalse(by_id["m2"].passed)
        self.assertIn("UNVERIFIABLE", by_id["m2"].detail)
        self.assertIn("no location", by_id["m2"].detail)


if __name__ == "__main__":
    unittest.main()
