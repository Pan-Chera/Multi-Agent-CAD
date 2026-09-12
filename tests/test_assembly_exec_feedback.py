"""Fix: a failed assembly-script run reported only a generic FATAL
("assembly STEP was not produced") -- the actual traceback was discarded,
so the Judge and feedback_router guessed repair_assembly, and the
deterministic codegen regenerated a byte-identical script every iteration
(telescopic-crane run 2026-09-09: three identical scripts, then budget
exhaustion). On that run the real failure was a SELECTOR anchor matching
no face: a mating-PLAN defect (the plan referenced an R15 cylinder that
the part's spec never had), which only remating can fix.

Chain fixed here:
  1. node_assembler stashes the exec tail in state.assembly_exec_error;
  2. node_assembly_qa folds it into the FATAL report's error_details;
  3. node_feedback_router attributes the "SELECTOR anchor matched no
     face" signature deterministically (overriding the judge): geometry
     present in the current STEP -> assembler (rerun), absent but
     spec-promised -> part_builder (remodel), absent and never promised
     -> mating_architect (remate, MATING_MAX_RUNS budget);
  4. _llm_repair_assembly is no longer silent (prints the failure reason)
     and honours LLM_API_TIMEOUT instead of a hardcoded 180s.

No external LLM calls: the repair path is mocked.
"""
from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mac_assembly import config_assembly as cfg
from mac_assembly import graph_assembly as graph
from mac_assembly import nodes_assembly as nodes
from mac_assembly.schemas_assembly import (
    AssemblyBrief,
    AssemblyErrorType,
    AssemblyJudgeAction,
    AssemblyJudgeDecision,
    AssemblyQAReport,
    PartResult,
    PartSpec,
)

_SELECTOR_MISS = (
    "script execution failure:\n"
    "RuntimeError: SELECTOR anchor matched no face on pedestal_housing: "
    "{'surface': 'cylinder', 'axis': 'z', 'select': 'closest_to', "
    "'value_mm': 15.0}"
)


def _brief():
    return AssemblyBrief(
        assembly_name="t",
        parts=[PartSpec(part_id="base", part_name="base", description="d")],
        user_request_raw="r",
        expected_part_count=1,
    )


def _fatal_qa(details):
    return AssemblyQAReport(
        all_passed=False,
        error_type=AssemblyErrorType.FATAL,
        error_details=details,
    )


class TestQAFoldsExecTail(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.state = {
            "assembly_brief": _brief(),
            "mating_plan": None,
            "work_dir": str(self.work),
            "assembly_step_path": str(self.work / "assembly_output.step"),
            "part_results": {
                "base": PartResult(part_id="base", ok=True, step_path="s"),
            },
            "qa_report": None,          # assembler's normal path cleared it
            "assembly_exec_error": _SELECTOR_MISS,
            "iteration_count": 0,
            "execution_log": [],
            "node_history": [],
        }

    def test_fatal_report_carries_selector_traceback(self):
        out = nodes.node_assembly_qa(self.state)
        report = out["qa_report"]
        self.assertEqual(report.error_type, AssemblyErrorType.FATAL)
        self.assertTrue(
            any("SELECTOR anchor matched no face" in d for d in report.error_details),
            report.error_details,
        )

    def test_fatal_report_generic_without_exec_error(self):
        self.state["assembly_exec_error"] = ""
        out = nodes.node_assembly_qa(self.state)
        report = out["qa_report"]
        self.assertEqual(
            report.error_details,
            ["assembly STEP was not produced"],
        )


class TestSelectorMissRoutesToRemate(unittest.TestCase):
    def _state(self, mating_calls):
        qa = _fatal_qa(
            ["assembly STEP was not produced", _SELECTOR_MISS]
        )
        decision = AssemblyJudgeDecision(
            action=AssemblyJudgeAction.REPAIR_ASSEMBLY,
            confidence="high",
            reason="fix the script",
            evidence=["e"],
        )
        return {
            "assembly_brief": _brief(),
            "qa_report": qa,
            "judge_decision": decision,
            "iteration_count": 1,
            "max_iterations": cfg.ASSEMBLY_MAX_ITERATIONS,
            "mating_architect_runs": mating_calls,
            "part_builder_remodel_runs": 0,
            "execution_log": [],
            "node_history": [],
        }

    def test_selector_miss_beats_judge_repair_assembly(self):
        state = self._state(mating_calls=1)
        with tempfile.NamedTemporaryFile(suffix=".step") as f:
            state["part_results"] = {
                "pedestal_housing": PartResult(
                    part_id="pedestal_housing", ok=True, step_path=f.name,
                )
            }
            with mock.patch.object(graph, "probe_selector_anchor",
                                   return_value=("not_found", None)):
                out = graph.node_feedback_router(state)
        self.assertEqual(out["__next__"], "mating_architect")
        # The remate feedback must name the failing part + query.
        self.assertIn("SELECTOR anchor matched no face", out["repair_context"])

    def test_selector_miss_budget_exhausted_ends(self):
        state = self._state(mating_calls=cfg.MATING_MAX_RUNS)
        with tempfile.NamedTemporaryFile(suffix=".step") as f:
            state["part_results"] = {
                "pedestal_housing": PartResult(
                    part_id="pedestal_housing", ok=True, step_path=f.name,
                )
            }
            with mock.patch.object(graph, "probe_selector_anchor",
                                   return_value=("not_found", None)):
                out = graph.node_feedback_router(state)
        self.assertEqual(out["__next__"], "end")


class TestSelectorMissAttribution(unittest.TestCase):
    """P1-1: a SELECTOR miss is NOT unconditionally a mating-plan defect.
    The router must attribute it deterministically via the three-state
    probe (found / not_found / error) + the spec-promise check:
      FOUND        -> assembler RERUN-ONLY (repair_context MUST be ""),
      PROBE_ERROR  -> assembler RERUN-ONLY (infrastructure, not absence),
      NOT_FOUND + spec promised the cylinder -> part_builder (remodel),
      NOT_FOUND + spec never promised it     -> mating_architect (remate).
    """

    QUERY = "{'surface': 'cylinder', 'axis': 'z', 'select': 'closest_to', 'value_mm': 15.0}"

    def _state(self, spec_desc="d", key_dims=None, step_path="", part_in_brief=True):
        parts = [PartSpec(part_id="base", part_name="base", description="d")]
        if part_in_brief:
            parts.append(PartSpec(
                part_id="pedestal_housing", part_name="pedestal",
                description=spec_desc, key_dimensions=key_dims or {},
            ))
        brief = AssemblyBrief(
            assembly_name="t", parts=parts,
            user_request_raw="r", expected_part_count=len(parts),
        )
        qa = _fatal_qa([
            "assembly STEP was not produced",
            "script execution failure:\nRuntimeError: SELECTOR anchor "
            f"matched no face on pedestal_housing: {self.QUERY}",
        ])
        decision = AssemblyJudgeDecision(
            action=AssemblyJudgeAction.REPAIR_ASSEMBLY,
            confidence="high", reason="fix the script", evidence=["e"],
        )
        return {
            "assembly_brief": brief,
            "qa_report": qa,
            "judge_decision": decision,
            "iteration_count": 1,
            "max_iterations": cfg.ASSEMBLY_MAX_ITERATIONS,
            "mating_architect_runs": 1,
            "part_builder_remodel_runs": 0,
            "part_results": {
                "pedestal_housing": PartResult(
                    part_id="pedestal_housing", ok=True, step_path=step_path,
                ),
            },
            "execution_log": [],
            "node_history": [],
        }

    def test_geometry_present_in_current_step_routes_to_assembler(self):
        with tempfile.NamedTemporaryFile(suffix=".step") as f:
            state = self._state(step_path=f.name)
            hit = {"selector": "f5", "surface": "cylinder", "point": (0, 0, 0),
                   "axis": (0, 0, 1), "radius": 15.0, "coordinate": None}
            with mock.patch.object(graph, "probe_selector_anchor",
                                   return_value=("found", hit)):
                out = graph.node_feedback_router(state)
        self.assertEqual(out["__next__"], "assembler")
        self.assertIn("rerun-only", out["execution_log"][-1])
        # P2: rerun-only means an EMPTY repair_context -- a non-empty one
        # would force node_assembler branch 3 to LLM-repair (and possibly
        # replace) a script whose deterministic rerun already succeeds.
        self.assertEqual(out["repair_context"], "")

    def test_probe_error_routes_to_assembler_rerun_only(self):
        # cadpy/STEP fault: NOT proof the geometry is absent -> must not
        # remodel the part or remate; rerun the assembler instead.
        with tempfile.NamedTemporaryFile(suffix=".step") as f:
            state = self._state(
                spec_desc="Cabinet with a central Z-axis guide bore R15.",
                step_path=f.name,
            )
            with mock.patch.object(graph, "probe_selector_anchor",
                                   return_value=("error", None)):
                out = graph.node_feedback_router(state)
        self.assertEqual(out["__next__"], "assembler")
        self.assertIn("probe_error", out["execution_log"][-1])
        self.assertEqual(out["repair_context"], "")

    def test_probe_raising_exception_counts_as_probe_error(self):
        with tempfile.NamedTemporaryFile(suffix=".step") as f:
            state = self._state(step_path=f.name)
            with mock.patch.object(graph, "probe_selector_anchor",
                                   side_effect=RuntimeError("cadpy exploded")):
                out = graph.node_feedback_router(state)
        self.assertEqual(out["__next__"], "assembler")
        self.assertEqual(out["repair_context"], "")

    def test_promised_but_missing_routes_to_part_builder(self):
        # A successful probe that finds no matching face is required before
        # the spec can attribute the failure to part geometry.
        with tempfile.NamedTemporaryFile(suffix=".step") as f:
            state = self._state(
                spec_desc="Cabinet with a central Z-axis guide bore R15.",
                key_dims={"bore_radius": 15.0}, step_path=f.name,
            )
            with mock.patch.object(graph, "probe_selector_anchor",
                                   return_value=("not_found", None)):
                out = graph.node_feedback_router(state)
        self.assertEqual(out["__next__"], "part_builder")
        self.assertEqual(out["remodel_part_ids"], ["pedestal_housing"])
        self.assertEqual(out["part_builder_remodel_runs"], 1)

    def test_not_promised_routes_to_mating(self):
        # Purely prismatic cabinet spec (the real telescopic-crane case):
        # the plan hallucinated the R15 cylinder -> remate.
        with tempfile.NamedTemporaryFile(suffix=".step") as f:
            state = self._state(
                spec_desc="Flat-sided cabinet, planar faces only.",
                step_path=f.name,
            )
            with mock.patch.object(graph, "probe_selector_anchor",
                                   return_value=("not_found", None)):
                out = graph.node_feedback_router(state)
        self.assertEqual(out["__next__"], "mating_architect")
        self.assertIn("never promised", out["repair_context"])

    def test_probe_absent_overrides_false_spec_promise(self):
        # Spec promises R15, but re-resolving the CURRENT STEP still misses
        # (probe runs because the file exists) -> the geometry truly is not
        # there; spec promise then routes to remodel, not remate.
        with tempfile.NamedTemporaryFile(suffix=".step") as f:
            state = self._state(
                spec_desc="central Z-axis guide bore R15",
                step_path=f.name,
            )
            with mock.patch.object(graph, "probe_selector_anchor",
                                   return_value=("not_found", None)):
                out = graph.node_feedback_router(state)
        self.assertEqual(out["__next__"], "part_builder")

    def test_remodel_route_respects_part_builder_budget(self):
        with tempfile.NamedTemporaryFile(suffix=".step") as f:
            state = self._state(
                spec_desc="central Z-axis guide bore R15", step_path=f.name,
            )
            state["part_builder_remodel_runs"] = cfg.PART_BUILDER_MAX_RUNS
            with mock.patch.object(graph, "probe_selector_anchor",
                                   return_value=("not_found", None)):
                out = graph.node_feedback_router(state)
        self.assertEqual(out["__next__"], "end")

    def test_missing_step_is_probe_error_not_geometry_evidence(self):
        state = self._state(
            spec_desc="central Z-axis guide bore R15",
            step_path="/nonexistent/pedestal.step",
        )
        out = graph.node_feedback_router(state)
        self.assertEqual(out["__next__"], "assembler")
        self.assertEqual(out["repair_context"], "")
        self.assertIn("probe_error", out["execution_log"][-1])

    def test_part_outside_brief_degrades_to_remate(self):
        # The missing-face part is not even in the brief (stale QA detail):
        # no spec to check -> conservative remate, never a crash.
        with tempfile.NamedTemporaryFile(suffix=".step") as f:
            state = self._state(part_in_brief=False, step_path=f.name)
            with mock.patch.object(graph, "probe_selector_anchor",
                                   return_value=("not_found", None)):
                out = graph.node_feedback_router(state)
        self.assertEqual(out["__next__"], "mating_architect")


class TestSpecPromisesGeometry(unittest.TestCase):
    def _spec(self, desc, key_dims=None):
        return PartSpec(part_id="p", part_name="p", description=desc,
                        key_dimensions=key_dims or {})

    _Q = {"surface": "cylinder", "axis": "z", "value_mm": 15.0}

    def test_radius_mention_with_keyword(self):
        self.assertTrue(graph._spec_promises_geometry(
            self._spec("plate with a central bore R15 for the pivot"), self._Q))

    def test_diameter_mention_with_keyword(self):
        self.assertTrue(graph._spec_promises_geometry(
            self._spec("hub with Ø30 through-hole"), self._Q))

    def test_key_dimensions_number_counts(self):
        self.assertTrue(graph._spec_promises_geometry(
            self._spec("pivot housing", {"bore_radius": 15.0}), self._Q))

    def test_number_without_cylinder_keyword_is_not_a_promise(self):
        self.assertFalse(graph._spec_promises_geometry(
            self._spec("cabinet 120x120x200, wall thickness 15"), self._Q))

    def test_keyword_without_matching_number_is_not_a_promise(self):
        self.assertFalse(graph._spec_promises_geometry(
            self._spec("bore R20 on top"), self._Q))

    def test_plane_queries_never_promise(self):
        q = {"surface": "plane", "axis": "z", "value_mm": 15.0}
        self.assertFalse(graph._spec_promises_geometry(
            self._spec("bore R15"), q))

    def test_no_value_requires_named_axis(self):
        q = {"surface": "cylinder", "axis": "z"}
        self.assertTrue(graph._spec_promises_geometry(
            self._spec("central Z-axis bore through the body"), q))
        self.assertFalse(graph._spec_promises_geometry(
            self._spec("a bore somewhere"), q))

    # --- P1 regression: cross-context number gluing (reviewer repro) -------
    # The old check accepted ANY round keyword + ANY R/2R number anywhere in
    # the spec, so an unrelated thickness/width got read as a promised bore.
    # Each of these returned True before and must now return False.

    def test_thickness_number_not_glued_to_hole_keyword(self):
        # "15 mm thick" is a thickness, not a radius; the only real radius
        # mention is R3. Must NOT be read as promising R15.
        self.assertFalse(graph._spec_promises_geometry(
            self._spec("Plate 15 mm thick with four mounting holes R3"),
            self._Q))

    def test_width_key_not_glued_to_bore_keyword(self):
        # width=30 is an arbitrary key (a diameter-shaped number), bore is
        # R5 -- neither is a promised R15/Ø30 cylinder.
        self.assertFalse(graph._spec_promises_geometry(
            self._spec("Housing with bore R5", {"width": 30.0}), self._Q))

    def test_width_number_not_glued_to_pin_hole_keyword(self):
        # "30 mm wide" is a width; the only radius is R4.
        self.assertFalse(graph._spec_promises_geometry(
            self._spec("Clevis 30 mm wide with pin hole R4"), self._Q))

    def test_coordinate_key_is_not_cylindrical_evidence(self):
        # 15.0 here is an axis midpoint coordinate, not a bore radius.
        self.assertFalse(graph._spec_promises_geometry(
            self._spec("d", {"bore_axis_midpoint_local": [15.0, 0.0, 9.0]}),
            self._Q))

    # --- positive controls: the strict check must NOT over-reject ---------

    def test_real_radius_mention_survives_unrelated_width_key(self):
        self.assertTrue(graph._spec_promises_geometry(
            self._spec("Housing with bore R15", {"width": 30.0}), self._Q))

    def test_semantic_diameter_key_counts(self):
        self.assertTrue(graph._spec_promises_geometry(
            self._spec("d", {"hole_diameter": 30.0}), self._Q))

    def test_diameter_phrase_after_number_counts(self):
        self.assertTrue(graph._spec_promises_geometry(
            self._spec("a 30 mm diameter central bore"), self._Q))

    def test_radius_mention_in_other_sentence_not_glued(self):
        # keyword sentence has no matching number; the R15 lives in a
        # different sentence with no round keyword -> not feature-local.
        self.assertFalse(graph._spec_promises_geometry(
            self._spec("Four mounting holes. Overall width R15 reference."),
            self._Q))

    def test_radius_is_not_reinterpreted_as_diameter(self):
        self.assertFalse(graph._spec_promises_geometry(
            self._spec("central bore R30", {"bore_radius": 30.0}), self._Q))

    def test_diameter_is_not_reinterpreted_as_radius(self):
        self.assertFalse(graph._spec_promises_geometry(
            self._spec("central hole Ø15", {"hole_diameter": 15.0}), self._Q))

    def test_explicit_wrong_axis_is_not_a_promise(self):
        self.assertFalse(graph._spec_promises_geometry(
            self._spec("R15 bore along local X axis", {"bore_radius": 15.0}),
            self._Q))


class TestAttributeSelectorMissParsing(unittest.TestCase):
    def test_unparseable_detail_degrades_to_remate(self):
        kind, part, _ = graph._attribute_selector_miss(
            {}, "SELECTOR anchor matched no face, unknown format")
        self.assertEqual(kind, "remate")
        self.assertIsNone(part)

    def test_bad_query_dict_degrades_to_remate(self):
        kind, part, _ = graph._attribute_selector_miss(
            {}, "SELECTOR anchor matched no face on base: {not python}")
        self.assertEqual(kind, "remate")
        self.assertEqual(part, "base")


class TestLLMRepairObservability(unittest.TestCase):
    def _run(self, response):
        client = mock.MagicMock(name="client")
        if isinstance(response, Exception):
            client.chat.completions.create.side_effect = response
        else:
            client.chat.completions.create.return_value = mock.MagicMock(
                choices=[mock.MagicMock(message=mock.MagicMock(content=response))]
            )
        buf = io.StringIO()
        with mock.patch.object(nodes, "_llm_client", return_value=client), \
                contextlib.redirect_stdout(buf):
            out = nodes._llm_repair_assembly(
                "# script\n", _brief(), qa=None, exec_error="boom",
            )
        return out, buf.getvalue()

    def test_api_failure_prints_reason(self):
        out, printed = self._run(RuntimeError("Request timed out"))
        self.assertIsNone(out)
        self.assertIn("LLM script repair failed: Request timed out", printed)

    def test_marker_gate_rejection_prints_reason(self):
        out, printed = self._run("```python\nprint('no helpers here')\n```")
        self.assertIsNone(out)
        self.assertIn("LLM script repair rejected", printed)

    def test_timeout_uses_configured_value(self):
        client = mock.MagicMock(name="client")
        client.chat.completions.create.return_value = mock.MagicMock(
            choices=[mock.MagicMock(message=mock.MagicMock(content=""))]
        )
        with mock.patch.object(nodes, "_llm_client", return_value=client), \
                contextlib.redirect_stdout(io.StringIO()):
            nodes._llm_repair_assembly("# script\n", _brief(), qa=None)
        _, kwargs = client.chat.completions.create.call_args
        self.assertEqual(kwargs["timeout"], nodes._LLM_API_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
