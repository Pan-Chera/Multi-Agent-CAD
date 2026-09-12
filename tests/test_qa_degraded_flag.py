"""Degraded delivery gate, QA side: run_assembly_qa must stamp
has_degraded_parts / degraded_part_ids / generation_warnings from the
PartResults, and must NOT turn them into errors (all_passed stays True on
clean geometry -- the flag routes to the Judge, not into a repair loop).

Real deterministic pipeline: codegen -> assembly script subprocess ->
run_assembly_qa on the produced artifacts. No external LLM calls.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from build123d import Box, export_step

from mac_assembly.assembly_codegen import run_assembly_script, write_assembly_script
from mac_assembly.assembly_qa import run_assembly_qa
from mac_assembly.schemas_assembly import (
    AssemblyBrief,
    AssemblyErrorType,
    PartResult,
    PartSpec,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


class TestQADegradedFlag(unittest.TestCase):
    def test_degraded_part_result_flags_report_without_failing_qa(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            part_dir = work / "parts" / "block"
            part_dir.mkdir(parents=True)
            step = part_dir / "temp_output_features.step"
            export_step(Box(6, 6, 6), str(step))

            brief = AssemblyBrief(
                assembly_name="deg",
                parts=[PartSpec(part_id="block", part_name="block",
                                description="d")],
                user_request_raw="r",
                expected_part_count=1,
            )
            script = write_assembly_script(
                brief, [], work, _REPO_ROOT, 0,
                part_step_overrides={"block": str(step.relative_to(work))},
            )
            ok, tail = run_assembly_script(
                script, work, timeout=300, python_bin=sys.executable
            )
            self.assertTrue(ok, f"assembly script failed: {tail}")

            part_results = {
                "block": PartResult(
                    part_id="block", ok=True, step_path=str(step),
                    degraded=True,
                    warnings=["v3 base body failed; kept previous STEP"],
                ),
            }
            report = run_assembly_qa(brief, [], part_results, work)

            # Flagged for the Judge ...
            self.assertTrue(report.has_degraded_parts)
            self.assertEqual(report.degraded_part_ids, ["block"])
            self.assertTrue(any(
                w.startswith("block: v3 base body failed")
                for w in report.generation_warnings
            ))
            # ... but NOT an error: clean single-part geometry still passes.
            self.assertTrue(report.all_passed)
            self.assertEqual(report.error_type, AssemblyErrorType.NONE)
            self.assertEqual(report.error_details, [])

    def test_clean_part_results_leave_flag_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            part_dir = work / "parts" / "block"
            part_dir.mkdir(parents=True)
            step = part_dir / "temp_output_features.step"
            export_step(Box(6, 6, 6), str(step))
            brief = AssemblyBrief(
                assembly_name="deg2",
                parts=[PartSpec(part_id="block", part_name="block",
                                description="d")],
                user_request_raw="r",
                expected_part_count=1,
            )
            script = write_assembly_script(
                brief, [], work, _REPO_ROOT, 0,
                part_step_overrides={"block": str(step.relative_to(work))},
            )
            ok, tail = run_assembly_script(
                script, work, timeout=300, python_bin=sys.executable
            )
            self.assertTrue(ok, f"assembly script failed: {tail}")
            part_results = {
                "block": PartResult(part_id="block", ok=True,
                                    step_path=str(step)),
            }
            report = run_assembly_qa(brief, [], part_results, work)
            self.assertFalse(report.has_degraded_parts)
            self.assertEqual(report.degraded_part_ids, [])
            self.assertTrue(report.all_passed)


if __name__ == "__main__":
    unittest.main()
