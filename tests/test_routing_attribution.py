"""P1-8: ENVELOPE/RECONCILE routing attribution + Judge first-round gate.

No external LLM calls: call_llm_json is mocked; the router is pure state
logic.
"""
from __future__ import annotations

import tempfile
import unittest
import hashlib
import json
from pathlib import Path
from unittest import mock

from mac_assembly import config_assembly as cfg
from mac_assembly import graph_assembly as graph
from mac_assembly import nodes_assembly as nodes
from mac_assembly.assembly_qa import ReconcileResult
from mac_assembly.schemas_assembly import (
    AssemblyBrief,
    AssemblyErrorType,
    AssemblyJudgeAction,
    AssemblyJudgeDecision,
    AssemblyQAReport,
    FunctionalInterface,
    InterfaceType,
    PartSpec,
)


def _brief(ids=("base", "arm")):
    return AssemblyBrief(
        assembly_name="t",
        parts=[PartSpec(part_id=i, part_name=i, description="d") for i in ids],
        user_request_raw="r",
        expected_part_count=len(ids),
    )


def _state(qa, decision=None, iteration=0, **extra):
    state = {
        "assembly_brief": _brief(),
        "qa_report": qa,
        "judge_decision": decision,
        "iteration_count": iteration,
        "max_iterations": cfg.ASSEMBLY_MAX_ITERATIONS,
        "decomposer_llm_calls": 1,
        "mating_architect_runs": 1,
        "part_builder_remodel_runs": 0,
        "remodel_part_ids": [],
        "repair_context": "",
        "execution_log": [],
        "node_history": [],
    }
    state.update(extra)
    return state


class TestRouterAttribution(unittest.TestCase):
    def test_part_geometry_attribution_routes_to_part_builder(self):
        qa = AssemblyQAReport(
            all_passed=False,
            error_type=AssemblyErrorType.RECONCILE,
            error_details=["reconcile: radii incompatible"],
            error_attribution="part_geometry",
            attribution_part_ids=["base", "arm"],
        )
        out = graph.node_feedback_router(_state(qa, decision=None))
        self.assertEqual(out["__next__"], "part_builder")
        self.assertEqual(out["remodel_part_ids"], ["base", "arm"])

    def test_part_geometry_ids_cross_checked_against_brief(self):
        qa = AssemblyQAReport(
            all_passed=False,
            error_type=AssemblyErrorType.RECONCILE,
            error_details=["x"],
            error_attribution="part_geometry",
            attribution_part_ids=["hallucinated"],
        )
        out = graph.node_feedback_router(_state(qa, decision=None))
        # No real id -> falls through to the mate-level default (remate).
        self.assertEqual(out["__next__"], "mating_architect")

    def test_ambiguous_attribution_keeps_remate_default(self):
        qa = AssemblyQAReport(
            all_passed=False,
            error_type=AssemblyErrorType.ENVELOPE,
            error_details=["envelope z: measured 70 vs expected 50"],
            error_attribution="ambiguous",
        )
        out = graph.node_feedback_router(_state(qa, decision=None))
        self.assertEqual(out["__next__"], "mating_architect")

    def test_mate_misalignment_still_remates(self):
        qa = AssemblyQAReport(
            all_passed=False,
            error_type=AssemblyErrorType.MATE_MISALIGNMENT,
            error_details=["mate m1 FAIL"],
        )
        out = graph.node_feedback_router(_state(qa, decision=None))
        self.assertEqual(out["__next__"], "mating_architect")

    def test_v3_unchanged_marker_routes_to_recompose(self):
        qa = AssemblyQAReport(
            all_passed=False,
            error_type=AssemblyErrorType.PART_MISSING,
            missing_parts=["palm"],
            needs_remodel_part_ids=["palm"],
            needs_recompose_ids=["palm"],
            error_details=["v3 spec unchanged; ..."],
        )
        out = graph.node_feedback_router(_state(qa, decision=None))
        self.assertEqual(out["__next__"], "decomposer")

    def test_v3_unchanged_respects_recompose_budget(self):
        qa = AssemblyQAReport(
            all_passed=False,
            error_type=AssemblyErrorType.PART_MISSING,
            missing_parts=["palm"],
            needs_remodel_part_ids=["palm"],
            needs_recompose_ids=["palm"],
            error_details=["v3 spec unchanged; ..."],
        )
        out = graph.node_feedback_router(
            _state(qa, decision=None,
                   decomposer_llm_calls=cfg.DECOMPOSER_MAX_RUNS)
        )
        self.assertEqual(out["__next__"], "end")

    def test_judge_decision_still_wins_over_attribution(self):
        qa = AssemblyQAReport(
            all_passed=False,
            error_type=AssemblyErrorType.RECONCILE,
            error_details=["x"],
            error_attribution="part_geometry",
            attribution_part_ids=["base"],
        )
        decision = AssemblyJudgeDecision(
            action=AssemblyJudgeAction.REMATE, confidence="high",
            reason="r", evidence=["e"],
        )
        out = graph.node_feedback_router(_state(qa, decision=decision))
        self.assertEqual(out["__next__"], "mating_architect")

    def test_reconcile_remodel_of_explicitly_accepted_part_remates(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            part_dir = work / "parts" / "base"
            part_dir.mkdir(parents=True)
            for name in ("accepted.step", "accepted.stl", "accepted.py"):
                (part_dir / name).write_text("artifact", encoding="utf-8")
            (part_dir / "accepted_part_cache.json").write_text(json.dumps({
                "request_sha256": hashlib.sha256(b"d").hexdigest(),
                "step": "accepted.step",
                "stl": "accepted.stl",
                "python": "accepted.py",
            }), encoding="utf-8")
            qa = AssemblyQAReport(
                all_passed=False,
                error_type=AssemblyErrorType.RECONCILE,
                error_details=["reconcile: radii incompatible"],
                error_attribution="part_geometry",
                attribution_part_ids=["base", "arm"],
            )
            decision = AssemblyJudgeDecision(
                action=AssemblyJudgeAction.REMODEL_PARTS,
                confidence="high", reason="resize interfaces",
                evidence=["measured mismatch"],
                remodel_part_ids=["base", "arm"],
            )
            out = graph.node_feedback_router(
                _state(qa, decision=decision, work_dir=str(work))
            )
        self.assertEqual(out["__next__"], "mating_architect")
        self.assertIn("immutable", out["repair_context"])
        self.assertEqual(out.get("part_builder_remodel_runs", 0), 0)


class TestReconcileResultShape(unittest.TestCase):
    def test_defaults(self):
        r = ReconcileResult([])
        self.assertEqual(r.errors, [])
        self.assertEqual(r.attribution, "ambiguous")
        self.assertEqual(r.part_ids, [])


class TestDegradedReconcilePolicy(unittest.TestCase):
    def test_radius_mismatch_signature_is_nonblocking_in_assembler_source(self):
        """Guard the policy at its integration point without running CAD."""
        import inspect
        source = inspect.getsource(nodes.node_assembler)
        self.assertIn("radius_warnings", source)
        self.assertIn("blocking_reconcile", source)
        self.assertIn("degraded continue", source)
        self.assertIn("radii incompatible", source)


class TestJudgeFirstRoundGate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        views = mock.patch.object(
            nodes, "render_assembly_views", return_value=[])
        images = mock.patch.object(nodes, "_load_user_images", return_value=[])
        llm = mock.patch.object(
            nodes, "call_llm_json",
            return_value={
                "action": "remate", "confidence": "high",
                "reason": "mock", "evidence": ["e"],
            },
        )
        views.start(); self.addCleanup(views.stop)
        images.start(); self.addCleanup(images.stop)
        self.call_llm = llm.start(); self.addCleanup(llm.stop)

    def _judge(self, qa, iteration=0):
        state = _state(qa, iteration=iteration)
        state["work_dir"] = str(self.work_dir)
        return nodes.node_assembly_judge(state)

    def test_envelope_runs_judge_at_iteration_zero(self):
        qa = AssemblyQAReport(
            all_passed=False, error_type=AssemblyErrorType.ENVELOPE,
            error_details=["envelope x off"],
        )
        out = self._judge(qa, iteration=0)
        self.assertIsNotNone(out["judge_decision"])
        self.call_llm.assert_called()

    def test_reconcile_runs_judge_at_iteration_zero(self):
        qa = AssemblyQAReport(
            all_passed=False, error_type=AssemblyErrorType.RECONCILE,
            error_details=["radii incompatible"],
        )
        out = self._judge(qa, iteration=0)
        self.assertIsNotNone(out["judge_decision"])

    def test_mate_misalignment_still_skipped_at_iteration_zero(self):
        qa = AssemblyQAReport(
            all_passed=False, error_type=AssemblyErrorType.MATE_MISALIGNMENT,
            error_details=["mate off"],
        )
        out = self._judge(qa, iteration=0)
        self.assertIsNone(out["judge_decision"])

    def test_mate_misalignment_runs_after_min_retry(self):
        qa = AssemblyQAReport(
            all_passed=False, error_type=AssemblyErrorType.MATE_MISALIGNMENT,
            error_details=["mate off"],
        )
        out = self._judge(qa, iteration=cfg.ASSEMBLY_JUDGE_MIN_RETRY)
        self.assertIsNotNone(out["judge_decision"])


class TestDegradedPassGate(unittest.TestCase):
    """Degraded delivery gate: an all_passed QA report with degraded parts
    must reach the Judge once, and a corrective Judge decision must beat
    the blanket "QA passed -> END" router rule."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        views = mock.patch.object(
            nodes, "render_assembly_views", return_value=[])
        images = mock.patch.object(nodes, "_load_user_images", return_value=[])
        llm = mock.patch.object(
            nodes, "call_llm_json",
            return_value={
                "action": "accept", "confidence": "high",
                "reason": "mock", "evidence": ["view[0]: degraded base ok"],
            },
        )
        views.start(); self.addCleanup(views.stop)
        images.start(); self.addCleanup(images.stop)
        self.call_llm = llm.start(); self.addCleanup(llm.stop)

    def _qa(self, **kw):
        base = dict(all_passed=True, error_type=AssemblyErrorType.NONE)
        base.update(kw)
        return AssemblyQAReport(**base)

    def _judge(self, qa, iteration=0):
        state = _state(qa, iteration=iteration)
        state["work_dir"] = str(self.work_dir)
        return nodes.node_assembly_judge(state)

    # --- schema defaults (backward compat) ---
    def test_report_defaults_are_clean(self):
        r = AssemblyQAReport()
        self.assertFalse(r.has_degraded_parts)
        self.assertEqual(r.degraded_part_ids, [])

    # --- judge gating ---
    def test_all_passed_clean_skips_judge(self):
        out = self._judge(self._qa())
        self.assertIsNone(out["judge_decision"])
        self.call_llm.assert_not_called()

    def test_all_passed_degraded_runs_judge_at_iteration_zero(self):
        qa = self._qa(has_degraded_parts=True, degraded_part_ids=["base"],
                      generation_warnings=["base: v3 base body failed"])
        out = self._judge(qa, iteration=0)
        self.assertIsNotNone(out["judge_decision"])
        self.call_llm.assert_called()

    # --- router ---
    def test_router_all_passed_no_decision_ends(self):
        qa = self._qa(has_degraded_parts=True, degraded_part_ids=["base"])
        out = graph.node_feedback_router(_state(qa, decision=None))
        self.assertEqual(out["__next__"], "end")

    def test_router_all_passed_accept_ends(self):
        qa = self._qa(has_degraded_parts=True, degraded_part_ids=["base"])
        decision = AssemblyJudgeDecision(
            action=AssemblyJudgeAction.ACCEPT, confidence="high",
            reason="showcase ok", evidence=["view[0]: fine"],
        )
        out = graph.node_feedback_router(_state(qa, decision=decision))
        self.assertEqual(out["__next__"], "end")

    def test_router_all_passed_remodel_routes_part_builder(self):
        qa = self._qa(has_degraded_parts=True, degraded_part_ids=["base"])
        decision = AssemblyJudgeDecision(
            action=AssemblyJudgeAction.REMODEL_PARTS, confidence="high",
            reason="degraded base wrong", evidence=["e"],
            remodel_part_ids=["base"],
        )
        out = graph.node_feedback_router(_state(qa, decision=decision))
        self.assertEqual(out["__next__"], "part_builder")
        self.assertEqual(out["remodel_part_ids"], ["base"])

    def test_router_remodel_ids_fall_back_to_degraded_ids(self):
        # Judge ordered a remodel but named no (valid) ids: the degraded
        # list is the actionable fallback on an all-passed report.
        qa = self._qa(has_degraded_parts=True, degraded_part_ids=["arm"])
        decision = AssemblyJudgeDecision(
            action=AssemblyJudgeAction.REMODEL_PARTS, confidence="high",
            reason="r", evidence=["e"], remodel_part_ids=[],
        )
        out = graph.node_feedback_router(_state(qa, decision=decision))
        self.assertEqual(out["__next__"], "part_builder")
        self.assertEqual(out["remodel_part_ids"], ["arm"])

    def test_router_all_passed_recompose_routes_decomposer(self):
        qa = self._qa(has_degraded_parts=True, degraded_part_ids=["base"])
        decision = AssemblyJudgeDecision(
            action=AssemblyJudgeAction.RECOMPOSE, confidence="high",
            reason="spec forced the degradation", evidence=["e"],
        )
        out = graph.node_feedback_router(_state(qa, decision=decision))
        self.assertEqual(out["__next__"], "decomposer")

    def test_router_all_passed_remate_still_ends(self):
        # Non-corrective decision on a geometrically passed report must not
        # spin the loop: nothing about mates is broken.
        qa = self._qa(has_degraded_parts=True, degraded_part_ids=["base"])
        decision = AssemblyJudgeDecision(
            action=AssemblyJudgeAction.REMATE, confidence="high",
            reason="r", evidence=["e"],
        )
        out = graph.node_feedback_router(_state(qa, decision=decision))
        self.assertEqual(out["__next__"], "end")

    def test_router_degraded_remodel_respects_part_budget(self):
        qa = self._qa(has_degraded_parts=True, degraded_part_ids=["base"])
        decision = AssemblyJudgeDecision(
            action=AssemblyJudgeAction.REMODEL_PARTS, confidence="high",
            reason="r", evidence=["e"], remodel_part_ids=["base"],
        )
        out = graph.node_feedback_router(
            _state(qa, decision=decision,
                   part_builder_remodel_runs=cfg.PART_BUILDER_MAX_RUNS)
        )
        self.assertEqual(out["__next__"], "end")

    def test_router_clean_pass_unaffected_by_corrective_decision(self):
        # No degraded parts: rule 0 ends the loop even if a (stale)
        # corrective decision lingers in state -- QA passed cleanly.
        qa = self._qa()
        decision = AssemblyJudgeDecision(
            action=AssemblyJudgeAction.REMODEL_PARTS, confidence="high",
            reason="r", evidence=["e"], remodel_part_ids=["base"],
        )
        out = graph.node_feedback_router(_state(qa, decision=decision))
        self.assertEqual(out["__next__"], "end")


class TestRouteAfterMatingFailureLoop(unittest.TestCase):
    """A first-pass mating_architect failure (plan=None) loops back into
    mating_architect with the errors as feedback while MATING_MAX_RUNS
    lasts, instead of ENDing the whole pipeline (telescopic-crane run,
    2026-09-09: two drifting plans killed the run before any part was
    built)."""

    def test_first_pass_failure_loops_back_under_budget(self):
        self.assertEqual(
            graph.route_after_mating({"mating_plan": None, "mating_architect_runs": 1}),
            "mating_architect",
        )

    def test_no_counter_first_failure_loops_back(self):
        self.assertEqual(
            graph.route_after_mating({"mating_plan": None}),
            "mating_architect",
        )

    def test_budget_exhausted_ends(self):
        self.assertEqual(
            graph.route_after_mating({
                "mating_plan": None,
                "mating_architect_runs": cfg.MATING_MAX_RUNS,
            }),
            graph.END,
        )

    def test_success_still_routes_to_part_builder(self):
        out = graph.route_after_mating({
            "mating_plan": object(),
            "part_results": {},
            "assembly_brief": _brief(),
        })
        self.assertEqual(out, "part_builder")


class TestConditionalEdgeMapsCoverRouterOutputs(unittest.TestCase):
    """LangGraph conditional edges route ONLY through the declared path
    map: an undeclared destination raises KeyError mid-run (the first-pass
    mating retry loop returned 'mating_architect' from route_after_mating
    before the self-loop was added to the edge map -- telescopic crane,
    2026-09-09). Assert every reachable router output is declared."""

    def test_mating_self_loop_registered_in_edge_map(self):
        app = graph.build_assembly_graph()
        ends = set()
        for branch in app.builder.branches["mating_architect"].values():
            ends.update(branch.ends)
        for state in (
            {"mating_plan": None, "mating_architect_runs": 1},   # retry loop
            {"mating_plan": None},                            # first failure
            {"mating_plan": None, "mating_architect_runs": cfg.MATING_MAX_RUNS},
            {"mating_plan": object(), "part_results": {}, "assembly_brief": _brief()},
        ):
            dest = graph.route_after_mating(state)
            self.assertIn(dest, ends, f"{dest!r} not declared in edge map")


class TestMatingArchitectFailureFeedback(unittest.TestCase):
    """The failure return carries the errors as repair_context + consumes
    the mating budget, feeding the loop above."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # One uncovered interface -> an empty mates list deterministically
        # fails _validate_mating_plan on both internal attempts.
        self.brief = AssemblyBrief(
            assembly_name="t",
            parts=[
                PartSpec(part_id="base", part_name="base", description="d"),
                PartSpec(part_id="lid", part_name="lid", description="d"),
            ],
            interfaces=[FunctionalInterface(
                interface_id="itf_1", part_a="base", part_b="lid",
                interface_type=InterfaceType.SEAT, description="seat",
            )],
            user_request_raw="r",
        )

    def test_failure_returns_feedback_and_counts_budget(self):
        state = {
            "assembly_brief": self.brief,
            "work_dir": self._tmp.name,
            "execution_log": [],
            "node_history": [],
        }
        with mock.patch.object(
            nodes, "call_llm_json",
            return_value={"assembly_name": "t", "mates": []},
        ) as m:
            out = nodes.node_mating_architect(state)
        self.assertIsNone(out["mating_plan"])
        self.assertEqual(m.call_count, 2)  # internal structured retry
        # P1-2 semantics: the counter tracks NODE RUNS (MATING_MAX_RUNS),
        # not raw LLM calls -- 2 calls in one run consume exactly 1.
        self.assertEqual(out["mating_architect_runs"], 1)
        self.assertIn("rejected by deterministic validation", out["repair_context"])
        self.assertIn("itf_1", out["repair_context"])


if __name__ == "__main__":
    unittest.main()
