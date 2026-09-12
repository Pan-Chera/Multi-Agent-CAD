"""P0-3 / P1-7: v3 remodel no-op guard + degraded PartResult semantics.

All MAC/LLM entry points (run_part, run_part_remodel) are mocked -- no
external LLM calls. Real build123d geometry is used for the base STEP so
the feature chain + export paths run for real.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from build123d import Align, Box, Pos, export_step

from mac_assembly import part_generator as pg
from mac_assembly.schemas_assembly import (
    BaseBodySpec,
    Feature,
    FeatureAttachment,
    PartResult,
    PartSpec,
    base_body_fingerprint,
    part_spec_fingerprint,
)


def _plate_step(path: Path) -> Path:
    """Real 60x40x12 plate STEP (X=-30..30, Y=-20..20, Z=0..12)."""
    shape = Pos(0, 0, 6) * Box(60, 40, 12, align=(Align.CENTER,) * 3)
    export_step(shape, str(path))
    return path


def _v3_spec(desc="plate base", attach=(0.0, 20.0, 0.0), ear_length=20.0):
    return PartSpec(
        part_id="palm", part_name="palm", description="audit",
        base_body=BaseBodySpec(
            description=desc,
            key_dimensions={"w": 60, "d": 40, "t": 12},
            local_bounds={"xmin": -30, "xmax": 30, "ymin": -20, "ymax": 20,
                          "zmin": 0, "zmax": 12},
        ),
        features=[Feature(
            name="clevis_fork",
            params={"ear_length": ear_length, "ear_width": 16,
                    "tongue_thickness": 3, "bore_radius": 2.5,
                    "bar_thickness": 12, "clearance_side": 0.1,
                    "pin_axis": "z"},
            attachment=FeatureAttachment(
                attach_point_mm=list(attach), direction="+y"),
        )],
    )


class TestV3NoOpRemodelGuard(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.parts_root = Path(self._tmp.name)
        self.part_dir = self.parts_root / "palm"
        self.part_dir.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def _write_cache(self, spec, outcome, base_step_exists=True):
        if base_step_exists:
            _plate_step(self.part_dir / "temp_output_base.step")
        (self.part_dir / "temp_v3_spec.json").write_text(json.dumps({
            "base_body_description": spec.base_body.description,
            "features": [f.model_dump(mode="json") for f in spec.features],
            "spec_fingerprint": part_spec_fingerprint(spec),
            "base_body_fingerprint": base_body_fingerprint(spec),
            "outcome": outcome,
        }), encoding="utf-8")

    def test_unchanged_failed_spec_short_circuits_to_recompose_marker(self):
        spec = _v3_spec()
        self._write_cache(spec, outcome="failed")
        with mock.patch.object(pg, "run_part") as m_run, \
             mock.patch.object(pg, "run_part_remodel") as m_remodel:
            result = pg.run_part_with_features(spec, self.parts_root,
                                               feedback="QA: fork embedded")
        m_run.assert_not_called()
        m_remodel.assert_not_called()
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("v3 spec unchanged"))
        self.assertIn("RECOMPOSE", result.error)

    def test_unchanged_successful_spec_does_not_short_circuit(self):
        spec = _v3_spec()
        self._write_cache(spec, outcome="ok")
        with mock.patch.object(pg, "run_part") as m_run:
            result = pg.run_part_with_features(spec, self.parts_root,
                                               feedback="QA: something")
        # outcome=ok -> fast path reuses the cached base (no MAC Coder run)
        m_run.assert_not_called()
        self.assertTrue(result.ok)

    def test_feature_change_reruns_feature_chain_without_mac_coder(self):
        old_spec = _v3_spec(ear_length=20.0)
        self._write_cache(old_spec, outcome="failed")
        new_spec = _v3_spec(ear_length=24.0)  # base identical, feature changed
        with mock.patch.object(pg, "run_part") as m_run:
            result = pg.run_part_with_features(new_spec, self.parts_root,
                                               feedback="QA: fork too short")
        m_run.assert_not_called()  # base reused, features re-applied
        self.assertTrue(result.ok, result.error)
        self.assertTrue(Path(result.step_path).is_file())
        # The new spec's fingerprint was recorded in the cache.
        cached = json.loads(
            (self.part_dir / "temp_v3_spec.json").read_text(encoding="utf-8"))
        self.assertEqual(cached["spec_fingerprint"],
                         part_spec_fingerprint(new_spec))
        self.assertEqual(cached["outcome"], "ok")

    def test_base_description_change_forces_base_rebuild(self):
        old_spec = _v3_spec(desc="old plate")
        self._write_cache(old_spec, outcome="ok")
        new_spec = _v3_spec(desc="NEW plate")  # base changed
        failed_no_step = PartResult(part_id="palm", ok=False, attempts=1,
                                    error="mock failure", step_path="")
        with mock.patch.object(pg, "run_part", return_value=failed_no_step) as m_run:
            result = pg.run_part_with_features(new_spec, self.parts_root,
                                               feedback="QA: dims wrong")
        self.assertEqual(m_run.call_count, 1)  # MAC Coder re-run for the base
        self.assertFalse(result.ok)
        self.assertIn("base body generation failed", result.error)


class TestDegradedPartResult(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.parts_root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_failed_base_with_step_is_degraded_not_clean_success(self):
        spec = _v3_spec()
        part_dir = self.parts_root / "palm"
        part_dir.mkdir()
        base_step = _plate_step(part_dir / "leftover_base.step")
        failed = PartResult(part_id="palm", ok=False, attempts=1,
                            error="mesh not watertight",
                            step_path=str(base_step))
        with mock.patch.object(pg, "run_part", return_value=failed):
            result = pg.run_part_with_features(spec, self.parts_root)
        self.assertTrue(result.ok)          # assembly may proceed for review
        self.assertTrue(result.degraded)    # ...but it is NOT a clean success
        self.assertIsNone(result.error)     # no more ok=True + error=warning
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("mesh not watertight", result.warnings[0])
        self.assertTrue(Path(result.step_path).is_file())  # artifact kept

    def test_clean_success_is_not_degraded(self):
        spec = _v3_spec()
        part_dir = self.parts_root / "palm"
        part_dir.mkdir()
        base_step = _plate_step(part_dir / "temp_output_base.step")
        ok = PartResult(part_id="palm", ok=True, attempts=1,
                        step_path=str(base_step))
        with mock.patch.object(pg, "run_part", return_value=ok):
            result = pg.run_part_with_features(spec, self.parts_root)
        self.assertTrue(result.ok)
        self.assertFalse(result.degraded)
        self.assertEqual(result.warnings, [])
        self.assertIsNone(result.error)

    def test_old_partresult_json_without_new_fields_still_parses(self):
        legacy = PartResult.model_validate({
            "part_id": "x", "step_path": "/a.step", "ok": True,
        })
        self.assertEqual(legacy.spec_fingerprint, "")
        self.assertFalse(legacy.degraded)
        self.assertEqual(legacy.warnings, [])


if __name__ == "__main__":
    unittest.main()
