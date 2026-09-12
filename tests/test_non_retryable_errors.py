"""P0-2: authentication / infrastructure failures must not enter any retry
loop -- neither the inner PART_MAX_ATTEMPTS regeneration loop nor the outer
PART_BUILDER_MAX_RUNS router budget.

No external LLM calls; run_part and the QA inputs are mocked.
"""
from __future__ import annotations

import unittest
from unittest import mock

from mac_assembly.part_generator import is_non_retryable_error
from mac_assembly.schemas_assembly import (
    AssemblyBrief,
    AssemblyErrorType,
    AssemblyQAReport,
    PartResult,
    PartSpec,
)


class TestClassifier(unittest.TestCase):
    def test_non_retryable_signatures(self):
        for err in (
            "RuntimeError: DASHSCOPE_API_KEY is not set.  Either export it "
            "as an environment variable or set DS_API_KEY in config.py.",
            "returncode=1; tail: Error code: 401 - Authentication Fails, "
            "Your API key is invalid",
            "AuthenticationError: Error code: 401",
            "PermissionDeniedError: Error code: 403",
            "Invalid API-key provided. [code: invalid_api_key]",
            "v3 base body generation failed after Aider retry. Base error: "
            "returncode=1; tail: 401 Unauthorized",
            "Error code: 404 - Model not found: qwen3.8-maxx",
            "dashscope: model not exist! please check your model name.",
            "litellm.NotFoundError: invalid model id",
            "openai.APIConnectionError: Invalid URL",
            "returncode=1; tail: ModuleNotFoundError: No module named 'rtree'",
        ):
            self.assertTrue(is_non_retryable_error(err), msg=err)

    def test_plain_importerror_stays_retryable(self):
        """A generated script's own bad import (ImportError, not
        ModuleNotFoundError) must keep its repair/retry chances -- only a
        missing project DEPENDENCY (ModuleNotFoundError) is infrastructure."""
        self.assertFalse(
            is_non_retryable_error(
                "part subprocess crashed: ImportError: cannot import name 'x'"
            )
        )
        self.assertFalse(
            is_non_retryable_error(
                "returncode=1; tail: ImportError: cannot import name "
                "'make_cylinder' from 'build123d'"
            )
        )

    def test_retryable_errors_stay_retryable(self):
        for err in (
            "single-part pipeline timed out after 3600s",
            "part subprocess crashed: ReadTimeout: The read operation "
            "timed out",
            "json.decoder.JSONDecodeError: Expecting value: line 1 column 1",
            "returncode=1; tail: StdFail_NotDone: boolean operation failed",
            "part subprocess crashed: [Errno 2] No such file or directory",
            "builder 'knuckle_hinge_ear' crashed: params mismatch",
            "pipeline reported success but STEP/STL artifacts missing",
            None,
            "",
            "unknown failure",
        ):
            self.assertFalse(is_non_retryable_error(err), msg=repr(err))


def _failed_result(part_id: str, error: str) -> PartResult:
    return PartResult(part_id=part_id, ok=False, error=error)


class TestFeedbackRouter(unittest.TestCase):
    def _state(self, results: dict) -> dict:
        qa = AssemblyQAReport(
            error_details=[
                "assembly STEP was not produced because part generation "
                f"failed: {', '.join(results)}"
            ],
            all_passed=False,
            error_type=AssemblyErrorType.PART_MISSING,
            missing_parts=list(results),
            needs_remodel_part_ids=list(results),
        )
        return {
            "qa_report": qa,
            "judge_decision": None,
            "iteration_count": 0,
            "max_iterations": 8,
            "part_results": results,
            "execution_log": [],
            "node_history": [],
            "part_builder_remodel_runs": 0,
            "mating_architect_runs": 1,
            "decomposer_llm_calls": 1,
        }

    def test_missing_api_key_routes_to_end_without_budget(self):
        """The 2026-09-10 failure mode: every part subprocess died with
        'DASHSCOPE_API_KEY is not set' and the router burned all 4
        PART_BUILDER_MAX_RUNS rounds on it. It must route to END."""
        from mac_assembly.graph_assembly import node_feedback_router

        results = {
            "chassis_body": _failed_result(
                "chassis_body",
                "returncode=1; tail: RuntimeError: DASHSCOPE_API_KEY is "
                "not set.  Either export it as an environment variable or "
                "set DS_API_KEY in multi_agent_cad/config.py.",
            )
        }
        update = node_feedback_router(self._state(results))
        self.assertEqual(update["__next__"], "end")
        # no part_builder round charged
        self.assertNotIn("part_builder_remodel_runs", update)
        note = update["execution_log"][-1]
        self.assertIn("configuration/infrastructure", note)

    def test_retryable_failure_still_routes_to_part_builder(self):
        from mac_assembly.graph_assembly import node_feedback_router

        results = {
            "chassis_body": _failed_result(
                "chassis_body",
                "single-part pipeline timed out after 3600s",
            )
        }
        update = node_feedback_router(self._state(results))
        self.assertEqual(update["__next__"], "part_builder")
        self.assertEqual(update["part_builder_remodel_runs"], 1)

    def test_passing_qa_not_hijacked_by_stale_failed_result(self):
        """A kept-best previous failure must not END a passing delivery."""
        from mac_assembly.graph_assembly import node_feedback_router

        results = {
            "chassis_body": _failed_result(
                "chassis_body", "ModuleNotFoundError: No module named 'x'"
            ),
        }
        state = self._state(results)
        qa = state["qa_report"]
        state["qa_report"] = qa.model_copy(update={
            "all_passed": True,
            "error_type": AssemblyErrorType.NONE,
            "missing_parts": [],
            "needs_remodel_part_ids": [],
            "error_details": [],
        })
        update = node_feedback_router(state)
        self.assertEqual(update["__next__"], "end")
        self.assertIn("QA passed", update["execution_log"][-1])


class TestInnerAttemptLoop(unittest.TestCase):
    def _run_node(self, error: str, max_attempts: int) -> tuple[dict, list]:
        from mac_assembly import nodes_assembly as na

        brief = AssemblyBrief(
            assembly_name="t",
            parts=[PartSpec(part_id="p1", part_name="P1", description="d")],
            user_request_raw="r",
        )
        state = {
            "assembly_brief": brief,
            "work_dir": "/tmp/mac_nr_test_unused",
            "part_results": {},
            "remodel_part_ids": [],
            "repair_context": "",
            "execution_log": [],
            "node_history": [],
        }
        calls: list[str] = []

        def fake_run_part(spec, parts_root, **kwargs):
            calls.append(spec.part_id)
            return PartResult(part_id=spec.part_id, ok=False, error=error)

        with mock.patch.object(na, "run_part", fake_run_part), \
                mock.patch(
                    "mac_assembly.config_assembly.PART_MAX_ATTEMPTS",
                    max_attempts,
                ):
            out = na.node_part_builder(state)
        return out, calls

    def test_inner_loop_stops_after_non_retryable_failure(self):
        """PART_MAX_ATTEMPTS=2: a missing-key failure must NOT launch the
        second full regeneration attempt."""
        out, calls = self._run_node(
            "returncode=1; tail: RuntimeError: DASHSCOPE_API_KEY is not set.",
            max_attempts=2,
        )
        self.assertEqual(calls, ["p1"])  # one attempt only
        self.assertFalse(out["part_results"]["p1"].ok)

    def test_inner_loop_retries_normal_failures(self):
        out, calls = self._run_node(
            "single-part pipeline timed out after 3600s", max_attempts=2
        )
        self.assertEqual(calls, ["p1", "p1"])  # both attempts consumed
        self.assertFalse(out["part_results"]["p1"].ok)


if __name__ == "__main__":
    unittest.main()
