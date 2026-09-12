"""P1-10: handoff part_steps lists the AUTHORITATIVE final STEP per part.

No external LLM calls; trimesh/URDF export paths are exercised only on a
tmp dir with no assembly artifacts (they no-op).
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from mac_assembly.handoff import build_handoff
from mac_assembly.schemas_assembly import PartResult


class TestHandoffPartSteps(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        old = time.time() - 60
        for pid in ("palm", "finger"):
            d = self.work_dir / "parts" / pid
            d.mkdir(parents=True)
            base = d / "temp_output_base.step"
            base.write_text("intermediate")
            os.utime(base, (old, old))
            # final STEP written after the intermediate (mtime-newer, like
            # the real run_part_with_features export order)
            (d / "temp_output_features.step").write_text("final")

    def _results(self):
        return {
            "palm": PartResult(
                part_id="palm", ok=True,
                step_path=str(self.work_dir / "parts/palm/temp_output_features.step"),
            ),
            "finger": PartResult(
                part_id="finger", ok=True,
                step_path=str(self.work_dir / "parts/finger/temp_output_features.step"),
            ),
        }

    def test_part_results_list_only_final_steps(self):
        manifest = build_handoff(self.work_dir, part_results=self._results())
        self.assertEqual(
            sorted(manifest["part_steps"]),
            ["parts/finger/temp_output_features.step",
             "parts/palm/temp_output_features.step"],
        )

    def test_fallback_lists_one_newest_step_per_part_dir(self):
        manifest = build_handoff(self.work_dir)
        self.assertEqual(len(manifest["part_steps"]), 2)
        for p in manifest["part_steps"]:
            self.assertIn("temp_output_features.step", p)

    def test_missing_step_path_is_skipped(self):
        results = self._results()
        results["ghost"] = PartResult(part_id="ghost", ok=False, step_path="")
        manifest = build_handoff(self.work_dir, part_results=results)
        self.assertEqual(len(manifest["part_steps"]), 2)


if __name__ == "__main__":
    unittest.main()
