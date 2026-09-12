"""BUG-025: when the Judge LLM call failed on all attempts, node_assembly_judge
used to fabricate a default `REPAIR_ASSEMBLY` decision. This sent every QA
failure type to the assembler (script repair), which cannot fix part
geometry or mate design. The fix: return judge_decision=None and let
node_feedback_router route via its existing decision=None path (which
already picks the right stage from qa.error_type).

No external LLM calls; the Judge LLM call is monkeypatched to raise.
"""
from __future__ import annotations

import unittest
from unittest import mock

from mac_assembly.nodes_assembly import node_assembly_judge
from mac_assembly.schemas_assembly import (
    AssemblyBrief,
    AssemblyErrorType,
    AssemblyQAReport,
    PartSpec,
)


def _state(qa_error_type: AssemblyErrorType):
    brief = AssemblyBrief(
        assembly_name="t",
        parts=[PartSpec(part_id="a", part_name="a", description="a")],
        interfaces=[],
        user_request_raw="r",
    )
    qa = AssemblyQAReport(
        all_passed=False,
        error_type=qa_error_type,
        error_details=[f"simulated {qa_error_type.value} failure"],
    )
    return {
        "work_dir": "/tmp/bug-025-test",
        "assembly_brief": brief,
        "qa_report": qa,
        "iteration_count": 5,  # past MIN_RETRY so judge runs
        "node_history": [],
        "execution_log": [],
    }


class TestJudgeFailureNoFabrication(unittest.TestCase):
    def _run_with_failed_judge(self, qa_error_type):
        state = _state(qa_error_type)
        # Force every Judge LLM attempt to raise (parse + retry paths).
        with mock.patch(
            "mac_assembly.nodes_assembly.call_llm_json",
            side_effect=RuntimeError("simulated judge LLM crash"),
        ), mock.patch(
            "mac_assembly.nodes_assembly._judge_gate",
            side_effect=RuntimeError("gate should not be reached"),
        ), mock.patch(
            "mac_assembly.nodes_assembly.render_assembly_views",
            return_value=[],
        ), mock.patch(
            "mac_assembly.nodes_assembly._load_user_images",
            return_value=[],
        ):
            out = node_assembly_judge(state)
        return out

    def test_judge_failure_returns_none_decision(self):
        out = self._run_with_failed_judge(AssemblyErrorType.INTERFERENCE)
        self.assertIsNone(
            out["judge_decision"],
            "Judge failure must return decision=None, not fabricate "
            "REPAIR_ASSEMBLY (BUG-025)",
        )

    def test_judge_failure_logs_failure_info(self):
        out = self._run_with_failed_judge(AssemblyErrorType.INTERFERENCE)
        log_text = " ".join(out.get("execution_log", []))
        # The log must mention this was a judge failure (not a real
        # decision) so an operator can diagnose it.
        self.assertTrue(
            "judge" in log_text.lower() and (
                "fail" in log_text.lower() or "error" in log_text.lower()
            ),
            f"execution_log must record judge failure, got: {log_text}",
        )

    def test_judge_failure_does_not_fabricate_repair_assembly(self):
        out = self._run_with_failed_judge(AssemblyErrorType.PART_MISSING)
        decision = out["judge_decision"]
        # The bug fabricated REPAIR_ASSEMBLY for every failure type.
        # PART_MISSING should NOT route to assembler via a fabricated
        # decision -- the router's decision=None path routes it to
        # part_builder.
        self.assertIsNone(
            decision,
            "PART_MISSING + Judge failure must NOT fabricate a decision "
            "that would route to assembler (script repair cannot make "
            "a missing part appear)",
        )


if __name__ == "__main__":
    unittest.main()
