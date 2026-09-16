"""Release guards for CAD-to-URDF frames and deliverable status."""

import json
import math
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import numpy as np
import trimesh

from mac_assembly import urdf_export as u


def _loc(xyz, rpy=(0, 0, 0)):
    return {"translation": list(xyz), "rotation_euler_xyz_deg": list(rpy)}


def _endpoint(part, xyz, rpy=(0, 0, 0)):
    return {"part": part, "position": list(xyz), "orientation": list(rpy)}


class TestUrdfReleaseGuards(unittest.TestCase):
    def test_intrinsic_cad_xyz_is_converted_to_urdf_rpy(self):
        cad = (20, 30, 40)
        urdf = u._cad_xyz_to_urdf_rpy(cad)
        rx, ry, rz = (math.radians(v) for v in cad)
        cx, sx = math.cos(rx), math.sin(rx)
        cy, sy = math.cos(ry), math.sin(ry)
        cz, sz = math.cos(rz), math.sin(rz)
        Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
        Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
        Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
        np.testing.assert_allclose(
            u._euler_xyz_deg_to_matrix(urdf), Rx @ Ry @ Rz, atol=1e-12
        )

    def test_three_link_chain_zero_pose_and_coaxial_motion(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / "assembly_manifest.json").write_text(json.dumps([
                {"label": "base", "location": _loc((10, 20, 0))},
                {"label": "arm", "location": _loc((10, 20, 30), (0, 0, 90))},
                {"label": "tool", "location": _loc((10, 50, 30), (20, 30, 40))},
            ]))
            (job / "assembly_mates.json").write_text(json.dumps([
                {"label": "base_to_arm", "relation": "revolute",
                 "fixed_endpoint": _endpoint("base", (10, 20, 30), (0, 0, 90)),
                 "moving_endpoint": _endpoint("arm", (10, 20, 30))},
                {"label": "arm_to_tool", "relation": "coaxial",
                 "fixed_endpoint": _endpoint("arm", (10, 50, 30), (20, 30, 40)),
                 "moving_endpoint": _endpoint("tool", (10, 50, 30))},
            ]))
            for label in ("base", "arm", "tool"):
                part_dir = job / "parts" / label
                part_dir.mkdir(parents=True)
                trimesh.creation.box(extents=(10, 10, 10)).export(
                    part_dir / "temp_output_0.stl"
                )
            with patch.object(u, "_validate", return_value=("ok", "test")):
                output = u.export_urdf(job)
            self.assertIsNotNone(output)
            robot = ET.parse(output).getroot()
            types = {j.get("name"): j.get("type") for j in robot.findall("joint")}
            self.assertEqual(types["arm_to_tool"], "continuous")
            self.assertEqual(
                robot.find("./joint[@name='arm_to_tool']/axis").get("xyz"),
                "0 0 1",
            )
            world_locs = u._load_world_locations(job)
            self.assertIsNone(u._zero_pose_error(robot, world_locs))
            # A 1-mm corruption of the second joint is rejected by the guard.
            origin = robot.find("./joint[@name='arm_to_tool']/origin")
            xyz = [float(v) for v in origin.get("xyz").split()]
            xyz[0] += 0.001
            origin.set("xyz", " ".join(str(v) for v in xyz))
            self.assertIn("tool", u._zero_pose_error(robot, world_locs))

    def test_validator_rejection_is_not_a_deliverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / "assembly_manifest.json").write_text(json.dumps([
                {"label": "base", "location": _loc((0, 0, 0))},
                {"label": "tip", "location": _loc((0, 0, 10))},
            ]))
            (job / "assembly_mates.json").write_text(json.dumps([
                {"label": "hinge", "relation": "revolute",
                 "fixed_endpoint": _endpoint("base", (0, 0, 10)),
                 "moving_endpoint": _endpoint("tip", (0, 0, 10))},
            ]))
            for label in ("base", "tip"):
                part_dir = job / "parts" / label
                part_dir.mkdir(parents=True)
                trimesh.creation.box(extents=(10, 10, 10)).export(
                    part_dir / "temp_output_0.stl"
                )
            urdf_dir = job / "urdf"
            urdf_dir.mkdir()
            prior = urdf_dir / f"{job.name}.urdf"
            prior.write_text("previous successful export", encoding="utf-8")
            with patch.object(u, "_validate", return_value=("failed", "bad XML")):
                self.assertIsNone(u.export_urdf(job))
            self.assertEqual(prior.read_text(encoding="utf-8"),
                             "previous successful export")
            self.assertFalse((urdf_dir / f".{job.name}.candidate.urdf").exists())

    def test_fallback_validator_can_import_dataclasses(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "source.py").write_text(
                "from __future__ import annotations\n"
                "from dataclasses import dataclass\n"
                "@dataclass\nclass Parsed:\n    name: str\n"
                "def read_urdf_source(path):\n    return Parsed(path.name)\n",
                encoding="utf-8",
            )
            urdf = directory / "robot.urdf"
            urdf.write_text("<robot name='robot'/>", encoding="utf-8")
            with patch.dict(os.environ, {"MAC_URDF_SKILL_PATH": str(directory)}):
                self.assertEqual(u._validate(urdf), ("ok", "urdf-skill source.py"))


if __name__ == "__main__":
    unittest.main()
