"""Two rebuild-loop defects observed on the gantry metrology run
(2026-09-08): a remodel round REPLACED a usable degraded part with a hard
failure, and a broken base STEP stayed cached so the feature-only fast
path replayed the same export failure until the remodel budget burned out.

Fix 1 (regression guard): node_part_builder never lets a FAILED rebuild
evict a usable (ok, possibly degraded) same-spec PartResult.
Fix 2 (poisoned-cache invalidation): a failed final export (or an
unloadable base STEP) drops temp_output_base.step + temp_v3_spec.json so
the next round does a FULL base regeneration instead of replaying; exports
go through dot-prefixed staging files so a failed write never truncates
the previous round's final STEP.

No external LLM calls: run_part / run_part_remodel /
run_part_with_features are mocked; build123d geometry is real.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from build123d import Align, Box, Pos, export_step

from mac_assembly import nodes_assembly as nodes
from mac_assembly import part_generator as pg
from mac_assembly.schemas_assembly import (
    AssemblyBrief,
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


def _v3_spec(part_id="palm"):
    return PartSpec(
        part_id=part_id, part_name=part_id, description="audit",
        base_body=BaseBodySpec(
            description="plate base",
            key_dimensions={"w": 60, "d": 40, "t": 12},
            local_bounds={"xmin": -30, "xmax": 30, "ymin": -20, "ymax": 20,
                          "zmin": 0, "zmax": 12},
        ),
        features=[Feature(
            name="clevis_fork",
            params={"ear_length": 20.0, "ear_width": 16,
                    "tongue_thickness": 3, "bore_radius": 2.5,
                    "bar_thickness": 12, "clearance_side": 0.1,
                    "pin_axis": "z"},
            attachment=FeatureAttachment(
                attach_point_mm=[0.0, 20.0, 0.0], direction="+y"),
        )],
    )


# ---------------------------------------------------------------------------
# Fix 1: node-level regression guard
# ---------------------------------------------------------------------------

class TestNodeRegressionGuard(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.spec = _v3_spec()

    def _state(self, prev: PartResult | None, remodel=True):
        part_results = {self.spec.part_id: prev} if prev else {}
        return {
            "assembly_brief": AssemblyBrief(
                assembly_name="t", parts=[self.spec],
                user_request_raw="r", expected_part_count=1,
            ),
            "work_dir": str(self.work_dir),
            "part_results": part_results,
            "remodel_part_ids": [self.spec.part_id] if remodel else [],
            "repair_context": "QA feedback",
            "node_history": [],
            "execution_log": [],
        }

    def _run(self, state, rebuilt: PartResult):
        with mock.patch.object(nodes, "run_part_with_features",
                               return_value=rebuilt) as m:
            out = nodes.node_part_builder(state)
        m.assert_called_once()
        return out

    def test_failed_rebuild_keeps_previous_degraded_ok_result(self):
        prev = PartResult(
            part_id=self.spec.part_id, ok=True, degraded=True,
            step_path="/old/features.step",
            warnings=["base body generation failed"],
            spec_fingerprint=part_spec_fingerprint(self.spec),
        )
        rebuilt = PartResult(
            part_id=self.spec.part_id, ok=False,
            error="v3 final export failed: Failed to write STEP file",
        )
        out = self._run(self._state(prev), rebuilt)
        kept = out["part_results"][self.spec.part_id]
        self.assertTrue(kept.ok)
        self.assertTrue(kept.degraded)
        self.assertEqual(kept.step_path, "/old/features.step")

    def test_failed_rebuild_keeps_previous_clean_ok_result(self):
        prev = PartResult(
            part_id=self.spec.part_id, ok=True,
            step_path="/old/features.step",
            spec_fingerprint=part_spec_fingerprint(self.spec),
        )
        rebuilt = PartResult(part_id=self.spec.part_id, ok=False, error="boom")
        out = self._run(self._state(prev), rebuilt)
        kept = out["part_results"][self.spec.part_id]
        self.assertTrue(kept.ok)
        self.assertEqual(kept.step_path, "/old/features.step")

    def test_successful_rebuild_still_replaces_degraded_prev(self):
        prev = PartResult(
            part_id=self.spec.part_id, ok=True, degraded=True,
            step_path="/old/features.step",
            spec_fingerprint=part_spec_fingerprint(self.spec),
        )
        rebuilt = PartResult(
            part_id=self.spec.part_id, ok=True,
            step_path="/new/features.step",
        )
        out = self._run(self._state(prev), rebuilt)
        new = out["part_results"][self.spec.part_id]
        self.assertTrue(new.ok)
        self.assertFalse(new.degraded)
        self.assertEqual(new.step_path, "/new/features.step")

    def test_failed_prev_is_replaced_by_new_failure(self):
        prev = PartResult(
            part_id=self.spec.part_id, ok=False, error="old error",
            spec_fingerprint=part_spec_fingerprint(self.spec),
        )
        rebuilt = PartResult(
            part_id=self.spec.part_id, ok=False, error="new error",
        )
        out = self._run(self._state(prev), rebuilt)
        got = out["part_results"][self.spec.part_id]
        self.assertFalse(got.ok)
        self.assertEqual(got.error, "new error")

    def test_stale_fingerprint_prev_is_not_protected(self):
        # Older-spec result: keeping it could mismatch the current mates,
        # so a failed rebuild honestly replaces it (prune normally drops
        # these already -- this pins the guard's own condition).
        prev = PartResult(
            part_id=self.spec.part_id, ok=True,
            step_path="/old/features.step", spec_fingerprint="STALE",
        )
        rebuilt = PartResult(part_id=self.spec.part_id, ok=False, error="boom")
        out = self._run(self._state(prev), rebuilt)
        got = out["part_results"][self.spec.part_id]
        self.assertFalse(got.ok)
        self.assertEqual(got.error, "boom")


# ---------------------------------------------------------------------------
# Fix 2: poisoned base cache invalidation + staging export
# ---------------------------------------------------------------------------

class TestExportFailureInvalidatesCache(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.parts_root = Path(self._tmp.name)
        self.part_dir = self.parts_root / "palm"
        self.part_dir.mkdir()
        self.spec = _v3_spec()

    def _write_cache(self, outcome="ok"):
        _plate_step(self.part_dir / "temp_output_base.step")
        (self.part_dir / "temp_v3_spec.json").write_text(json.dumps({
            "base_body_description": self.spec.base_body.description,
            "features": [f.model_dump(mode="json") for f in self.spec.features],
            "spec_fingerprint": part_spec_fingerprint(self.spec),
            "base_body_fingerprint": base_body_fingerprint(self.spec),
            "outcome": outcome,
        }), encoding="utf-8")

    def _failing_export(self, *args, **kwargs):
        raise RuntimeError("Failed to write STEP file")

    def test_export_failure_drops_base_and_spec_cache(self):
        self._write_cache(outcome="ok")
        prev_final = self.part_dir / "temp_output_features.step"
        prev_final.write_bytes(b"PREVIOUS ROUND GEOMETRY")
        with mock.patch("build123d.export_step", side_effect=self._failing_export):
            result = pg.run_part_with_features(
                self.spec, self.parts_root, feedback="QA: fix")
        self.assertFalse(result.ok)
        self.assertIn("v3 final export failed", result.error)
        # Poisoned base + spec cache dropped -> next round full regen.
        self.assertFalse((self.part_dir / "temp_output_base.step").exists())
        self.assertFalse((self.part_dir / "temp_v3_spec.json").exists())
        # Staging: the previous round's final STEP survived intact.
        self.assertEqual(prev_final.read_bytes(), b"PREVIOUS ROUND GEOMETRY")
        self.assertEqual(list(self.part_dir.glob(".staging_features*")), [])

    def test_next_round_after_invalidation_regenerates_base(self):
        self._write_cache(outcome="ok")
        with mock.patch("build123d.export_step", side_effect=self._failing_export):
            pg.run_part_with_features(self.spec, self.parts_root,
                                      feedback="QA: fix")
        # Second round: the fast path must NOT fire -- run_part (MAC Coder)
        # is called for a full base regeneration.
        failed_base = PartResult(part_id="palm", ok=False, attempts=1,
                                 error="no step", step_path="")
        with mock.patch.object(pg, "run_part",
                               return_value=failed_base) as m_run, \
             mock.patch.object(pg, "run_part_remodel", return_value=None):
            result = pg.run_part_with_features(self.spec, self.parts_root,
                                               feedback="QA: fix")
        m_run.assert_called_once()
        self.assertFalse(result.ok)

    def test_corrupt_cached_base_invalidates_cache(self):
        self._write_cache(outcome="ok")
        (self.part_dir / "temp_output_base.step").write_bytes(b"NOT A STEP")
        with mock.patch("build123d.import_step",
                        side_effect=RuntimeError("corrupt step")):
            result = pg.run_part_with_features(self.spec, self.parts_root,
                                               feedback="QA: fix")
        self.assertFalse(result.ok)
        self.assertIn("v3 cached base STEP load failed", result.error)
        self.assertFalse((self.part_dir / "temp_output_base.step").exists())
        self.assertFalse((self.part_dir / "temp_v3_spec.json").exists())

    def test_salvaged_garbage_base_import_failure_invalidates_cache(self):
        garbage = self.part_dir / "leftover.step"
        garbage.write_bytes(b"GARBAGE")
        ok_but_garbage = PartResult(part_id="palm", ok=True, attempts=1,
                                    step_path=str(garbage))
        with mock.patch.object(pg, "run_part", return_value=ok_but_garbage), \
             mock.patch("build123d.import_step",
                        side_effect=RuntimeError("bad geometry")):
            result = pg.run_part_with_features(self.spec, self.parts_root,
                                               feedback="")
        self.assertFalse(result.ok)
        self.assertIn("v3 base STEP load failed after generation", result.error)
        self.assertFalse((self.part_dir / "temp_output_base.step").exists())
        self.assertFalse((self.part_dir / "temp_v3_spec.json").exists())

    def test_successful_export_promotes_staging_without_leftovers(self):
        self._write_cache(outcome="ok")
        result = pg.run_part_with_features(self.spec, self.parts_root,
                                           feedback="QA: fix")
        self.assertTrue(result.ok, result.error)
        final = self.part_dir / "temp_output_features.step"
        self.assertTrue(final.is_file())
        self.assertGreater(final.stat().st_size, 100)
        self.assertEqual(list(self.part_dir.glob(".staging_features*")), [])


if __name__ == "__main__":
    unittest.main()
