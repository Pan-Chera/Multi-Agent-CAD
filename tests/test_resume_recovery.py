"""Interrupted part generation resumes from matching source before regeneration.

All tests are local and mock the Aider subprocess path; no model is called.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mac_assembly.schemas_assembly import PartResult, PartSpec


class TestResumeRecoveryCandidate(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mac_resume_"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.part = self.root / "parts" / "p1"
        (self.part / "pipeline_cache").mkdir(parents=True)
        self.request = "make the current part"
        (self.part / "temp_design_0.py").write_text(
            "# TODO_AIDER\n", encoding="utf-8"
        )
        (self.part / "pipeline_cache" / "cad_brief.json").write_text(
            json.dumps({"user_request_raw": self.request}), encoding="utf-8"
        )

    def test_matching_unfinished_script_is_offered_once(self):
        from mac_assembly.part_generator import _resume_recovery_candidate

        first = _resume_recovery_candidate(self.part, self.request)
        second = _resume_recovery_candidate(self.part, self.request)
        self.assertEqual(first, self.part / "temp_design_0.py")
        self.assertIsNone(second)

    def test_changed_request_rejects_stale_script(self):
        from mac_assembly.part_generator import _resume_recovery_candidate

        self.assertIsNone(
            _resume_recovery_candidate(self.part, "different current request")
        )
        self.assertFalse((self.part / "resume_recovery_attempts.json").exists())

    def test_existing_pre_qa_step_keeps_repaired_source_recoverable(self):
        from mac_assembly.part_generator import _resume_recovery_candidate

        (self.part / "temp_output_0.step").write_text("valid", encoding="utf-8")
        self.assertEqual(
            _resume_recovery_candidate(self.part, self.request),
            self.part / "temp_design_0.py",
        )

    def test_a_new_script_content_gets_one_new_recovery(self):
        from mac_assembly.part_generator import _resume_recovery_candidate

        self.assertIsNotNone(_resume_recovery_candidate(self.part, self.request))
        (self.part / "temp_design_0.py").write_text(
            "# a distinct interrupted generation\n", encoding="utf-8"
        )
        self.assertIsNotNone(_resume_recovery_candidate(self.part, self.request))
        self.assertIsNone(_resume_recovery_candidate(self.part, self.request))


class TestRunPartResumeRouting(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mac_resume_route_"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.parts = self.root / "parts"
        self.part = self.parts / "p1"
        (self.part / "pipeline_cache").mkdir(parents=True)
        self.spec = PartSpec(part_id="p1", part_name="P1", description="desc")
        (self.part / "temp_design_0.py").write_text("# unfinished\n", encoding="utf-8")
        (self.part / "pipeline_cache" / "cad_brief.json").write_text(
            json.dumps({"user_request_raw": "desc"}), encoding="utf-8"
        )

    def test_successful_recovery_avoids_full_subprocess(self):
        from mac_assembly import part_generator as pg

        recovered = PartResult(
            part_id="p1", ok=True,
            step_path=str(self.part / "temp_output_aider.step"),
            stl_path=str(self.part / "temp_output_aider.stl"),
        )
        with mock.patch.object(pg, "run_part_remodel", return_value=recovered) as remodel, \
                mock.patch.object(
                    pg, "_stream_subprocess",
                    side_effect=AssertionError("full generation must not run"),
                ):
            result = pg.run_part(self.spec, self.parts)
        self.assertIs(result, recovered)
        self.assertEqual(remodel.call_args.kwargs["code_path"], self.part / "temp_design_0.py")

    def test_retryable_recovery_failure_falls_back_to_full_generation(self):
        from mac_assembly import part_generator as pg

        with mock.patch.object(pg, "run_part_remodel", return_value=None), \
                mock.patch.object(
                    pg, "_stream_subprocess", return_value=(1, "ordinary failure\n", False)
                ) as full:
            result = pg.run_part(self.spec, self.parts)
        self.assertFalse(result.ok)
        full.assert_called_once()

    def test_changed_request_forces_inner_plan_refresh(self):
        from mac_assembly import part_generator as pg

        (self.part / "pipeline_cache" / "cad_brief.json").write_text(
            json.dumps({"user_request_raw": "stale description"}), encoding="utf-8"
        )
        with mock.patch.object(pg, "_resume_recovery_candidate", return_value=None), \
                mock.patch.object(
                    pg, "_stream_subprocess", return_value=(1, "ordinary failure\n", False)
                ) as full:
            pg.run_part(self.spec, self.parts)
        self.assertEqual(full.call_args.kwargs["env"]["MAC_FORCE_REFRESH"], "1")

    def test_matching_request_preserves_inner_plan_cache(self):
        from mac_assembly import part_generator as pg

        with mock.patch.object(pg, "_resume_recovery_candidate", return_value=None), \
                mock.patch.object(
                    pg, "_stream_subprocess", return_value=(1, "ordinary failure\n", False)
                ) as full:
            pg.run_part(self.spec, self.parts)
        self.assertEqual(full.call_args.kwargs["env"]["MAC_FORCE_REFRESH"], "")

    def test_explicit_matching_cache_skips_all_generation(self):
        from mac_assembly import part_generator as pg
        import hashlib

        for name in ("accepted.step", "accepted.stl", "accepted.py"):
            (self.part / name).write_text("artifact", encoding="utf-8")
        (self.part / "accepted_part_cache.json").write_text(json.dumps({
            "request_sha256": hashlib.sha256(b"desc").hexdigest(),
            "step": "accepted.step",
            "stl": "accepted.stl",
            "python": "accepted.py",
        }), encoding="utf-8")
        with mock.patch.object(
            pg, "_stream_subprocess",
            side_effect=AssertionError("accepted cache must skip generation"),
        ):
            result = pg.run_part(self.spec, self.parts)
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 0)

    def test_explicit_stale_cache_does_not_bypass_generation(self):
        from mac_assembly import part_generator as pg

        (self.part / "accepted_part_cache.json").write_text(json.dumps({
            "request_sha256": "stale",
            "step": "missing.step",
            "stl": "missing.stl",
            "python": "missing.py",
        }), encoding="utf-8")
        with mock.patch.object(pg, "_resume_recovery_candidate", return_value=None), \
                mock.patch.object(
                    pg, "_stream_subprocess", return_value=(1, "ordinary failure\n", False)
                ) as full:
            result = pg.run_part(self.spec, self.parts)
        self.assertFalse(result.ok)
        full.assert_called_once()

    def test_public_cache_entry_supports_non_v2_callers(self):
        from mac_assembly import part_generator as pg
        import hashlib

        for name in ("accepted.step", "accepted.stl", "accepted.py"):
            (self.part / name).write_text("artifact", encoding="utf-8")
        (self.part / "accepted_part_cache.json").write_text(json.dumps({
            "request_sha256": hashlib.sha256(b"desc").hexdigest(),
            "step": "accepted.step",
            "stl": "accepted.stl",
            "python": "accepted.py",
        }), encoding="utf-8")
        result = pg.load_explicit_accepted_cache(self.spec, self.parts)
        self.assertIsNotNone(result)
        self.assertTrue(result.ok)


if __name__ == "__main__":
    unittest.main()
