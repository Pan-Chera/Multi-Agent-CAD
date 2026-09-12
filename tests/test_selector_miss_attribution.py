"""P1-2: reconcile selector-miss attribution must go through the
router's _attribute_selector_miss (spec-promise check), not be
hard-wired to part_geometry.

The previous reconcile_dimensions set ``attribution="part_geometry"``
on SELECTOR not_found, which routed every selector miss to part_builder.
But a selector miss may be:
  * a part build defect (spec promised the cylinder but the STEP is
    missing it) -> part_builder (remodel).
  * a plan hallucination (spec never promised the cylinder; the
    mating_architect invented it) -> mating_architect (remate).
  * a probe error / rerun fault -> assembler rerun-only.

Fix:
  1. reconcile emits the canonical selector-miss format
     ``SELECTOR anchor matched no face on <part_id>: <query_dict_repr>``
     so the router's ``_SELECTOR_MISS_RE`` can parse part_id + query.
  2. reconcile does NOT pre-set ``attribution="part_geometry"`` for
     selector miss -- the router decides via _attribute_selector_miss.
  3. _check_mates and _check_kinematics use the same canonical format.

Tests:
  A. plan queries R5, PartSpec promises R5 (text), STEP has no R5 ->
     routes to part_builder (remodel).
  B. plan queries R5, PartSpec never promises any cylinder ->
     routes to mating_architect (remate).
  C. probe error (STEP unreadable) -> routes to assembler rerun-only.
  D. selector-miss detail is parseable by _SELECTOR_MISS_RE.

No external LLM calls; STEP files built with build123d, router is
pure state logic.
"""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from build123d import Align, Box, Cylinder, Pos, export_step

from mac_assembly import config_assembly as cfg
from mac_assembly import graph_assembly as graph
from mac_assembly.assembly_qa import reconcile_dimensions
from mac_assembly.schemas_assembly import (
    Anchor,
    AnchorKind,
    AssemblyBrief,
    AssemblyErrorType,
    AssemblyQAReport,
    FaceQuery,
    FunctionalInterface,
    InterfaceType,
    MateSpec,
    MateType,
    MatingPlan,
    PartResult,
    PartSpec,
)


def _box_step_no_cylinder(path: Path):
    """A simple Box with NO cylindrical faces."""
    plate = Pos(0, 0, 5) * Box(20, 20, 10, align=(Align.CENTER,) * 3)
    export_step(plate, str(path))


def _shaft_step(path: Path, radius: float = 5.0, height: float = 8.0):
    """Solid shaft along Z (a real cylinder)."""
    shape = Cylinder(radius=radius, height=height, align=(Align.CENTER,) * 3)
    shape = Pos(0, 0, height / 2.0) * shape
    export_step(shape, str(path))


def _brief_with_promise(fixed_id, moving_id, fixed_desc, moving_desc):
    """Brief where the fixed PartSpec text may or may not promise a
    cylinder (R5 bore)."""
    return AssemblyBrief(
        assembly_name="t",
        parts=[
            PartSpec(part_id=fixed_id, part_name=fixed_id, description=fixed_desc),
            PartSpec(part_id=moving_id, part_name=moving_id, description=moving_desc),
        ],
        interfaces=[
            FunctionalInterface(
                interface_id="itf", part_a=fixed_id, part_b=moving_id,
                interface_type=InterfaceType.HINGE, description="",
            ),
        ],
        user_request_raw="r",
    )


def _plan_with_r5_selector(fixed_id, moving_id):
    """A plan that queries an R5 cylinder on the fixed part (which the
    STEP may or may not actually contain)."""
    return MatingPlan(
        assembly_name="t",
        mates=[
            MateSpec(
                mate_id="m1",
                mate_type=MateType.REVOLUTE,
                fixed_part_id=fixed_id,
                moving_part_id=moving_id,
                fixed_anchor=Anchor(
                    kind=AnchorKind.SELECTOR,
                    selector_query=FaceQuery(
                        surface="cylinder", axis="z",
                        select="closest_to", value_mm=5.0,
                    ),
                ),
                moving_anchor=Anchor(
                    kind=AnchorKind.AXIS_POINT, axis="z", offset_mm=0.0,
                ),
            ),
        ],
    )


def _result(part_id, step_path):
    return PartResult(
        part_id=part_id, step_path=str(step_path), stl_path="", py_path="",
        part_dir=str(step_path.parent), ok=True, attempts=1, token_usage={},
    )


def _state(qa, decision=None, iteration=0, **extra):
    state = {
        "assembly_brief": _brief_with_promise("fixed", "moving", "", ""),
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


class TestSelectorMissAttributionFormat(unittest.TestCase):
    """reconcile selector-miss detail must use the canonical format so
    the router's _SELECTOR_MISS_RE can parse part_id + query."""

    def test_reconcile_emits_canonical_selector_miss_format(self):
        """reconcile_dimensions with a fixed SELECTOR that finds no
        cylinder must emit an error matching
        'SELECTOR anchor matched no face on <part_id>: <query_dict>'."""
        with tempfile.TemporaryDirectory() as tmp:
            fixed_step = Path(tmp) / "box.step"
            _box_step_no_cylinder(fixed_step)
            moving_step = Path(tmp) / "shaft.step"
            _shaft_step(moving_step)
            brief = _brief_with_promise(
                "fixed", "moving",
                # PartSpec does NOT promise a cylinder (no R/Ø text).
                "A plain box",
                "A shaft",
            )
            plan = _plan_with_r5_selector("fixed", "moving")
            part_results = {
                "fixed": _result("fixed", fixed_step),
                "moving": _result("moving", moving_step),
            }
            r = reconcile_dimensions(brief, plan.mates, part_results)
        # Find the selector-miss error.
        sel_miss = next(
            (e for e in r.errors if "SELECTOR anchor matched no face" in e),
            None,
        )
        self.assertIsNotNone(
            sel_miss,
            f"reconcile must emit a selector-miss error, got: {r.errors}"
        )
        # The canonical format must be parseable by _SELECTOR_MISS_RE.
        m = graph._SELECTOR_MISS_RE.search(sel_miss)
        self.assertIsNotNone(
            m,
            f"selector-miss detail must match _SELECTOR_MISS_RE: {sel_miss}"
        )
        # Captured part_id must be the actual fixed part_id.
        self.assertEqual(
            m.group(1), "fixed",
            f"parsed part_id must be 'fixed', got {m.group(1)}"
        )
        # Captured query dict must be parseable as a Python dict.
        import ast
        query = ast.literal_eval(m.group(2))
        self.assertIsInstance(query, dict)
        self.assertEqual(query.get("surface"), "cylinder")
        self.assertEqual(query.get("axis"), "z")

    def test_reconcile_does_not_preset_part_geometry_attribution(self):
        """reconcile_dimensions must NOT pre-set
        attribution='part_geometry' for selector miss. Let the router
        decide based on the PartSpec text."""
        with tempfile.TemporaryDirectory() as tmp:
            fixed_step = Path(tmp) / "box.step"
            _box_step_no_cylinder(fixed_step)
            moving_step = Path(tmp) / "shaft.step"
            _shaft_step(moving_step)
            brief = _brief_with_promise(
                "fixed", "moving", "A plain box", "A shaft",
            )
            plan = _plan_with_r5_selector("fixed", "moving")
            part_results = {
                "fixed": _result("fixed", fixed_step),
                "moving": _result("moving", moving_step),
            }
            r = reconcile_dimensions(brief, plan.mates, part_results)
        # The attribution must NOT be hard-wired to part_geometry for a
        # selector miss (the spec text here never promised a cylinder).
        self.assertNotEqual(
            r.attribution, "part_geometry",
            f"reconcile must NOT pre-set part_geometry for selector miss; "
            f"got attribution={r.attribution!r}"
        )


class TestSelectorMissRouting(unittest.TestCase):
    """The router's _attribute_selector_miss decides remodel vs remate
    based on the PartSpec text promise, NOT reconcile's pre-set
    attribution."""

    def test_builder_prose_only_promise_routes_to_recompose(self):
        """A deterministic builder cannot realize geometry mentioned only
        in prose/key_dimensions; rebuilding unchanged params is a no-op."""
        with tempfile.TemporaryDirectory() as tmp:
            fixed_step = Path(tmp) / "box.step"
            _box_step_no_cylinder(fixed_step)
            brief = _brief_with_promise(
                "fixed", "moving",
                "A mounting plate with two Y-axis R5 hinge bores",
                "A shaft",
            )
            brief.parts[0].builder = {
                "name": "mounting_plate",
                "params": {
                    "width": 140, "depth": 100, "thickness": 22,
                    "hole_radius": 3.2, "hole_dx": 55, "hole_dy": 35,
                },
            }
            state = _state(
                AssemblyQAReport(
                    all_passed=False,
                    error_type=AssemblyErrorType.RECONCILE,
                    error_details=[
                        "SELECTOR anchor matched no face on fixed: "
                        "{'surface': 'cylinder', 'axis': 'y', "
                        "'select': 'closest_to', 'value_mm': 5.0}"
                    ],
                ),
                assembly_brief=brief,
                part_results={"fixed": _result("fixed", fixed_step)},
            )
            with mock.patch.object(
                graph, "probe_selector_anchor", return_value=("not_found", None)
            ):
                out = graph.node_feedback_router(state)
        self.assertEqual(out["__next__"], "decomposer")
        self.assertIn("structured part spec", out["execution_log"][-1])

    def _qa_with_selector_miss(self, brief, part_results, mate):
        """Build a QA report that carries the selector-miss detail from
        reconcile, mimicking what node_assembler does."""
        r = reconcile_dimensions(brief, mate, part_results)
        sel_miss_errors = [
            e for e in r.errors if "SELECTOR anchor matched no face" in e
        ]
        if not sel_miss_errors:
            raise AssertionError(
                f"test setup error: no selector-miss in reconcile errors: "
                f"{r.errors}"
            )
        return AssemblyQAReport(
            all_passed=False,
            error_type=AssemblyErrorType.RECONCILE,
            error_details=sel_miss_errors,
            error_attribution=r.attribution,
            attribution_part_ids=r.part_ids,
        )

    def test_spec_promised_r5_routes_to_part_builder(self):
        """PartSpec text promises R5 (e.g. 'a plate with an R5 bore')
        but the STEP has no cylinder -> part_builder (remodel)."""
        with tempfile.TemporaryDirectory() as tmp:
            fixed_step = Path(tmp) / "box.step"
            _box_step_no_cylinder(fixed_step)
            moving_step = Path(tmp) / "shaft.step"
            _shaft_step(moving_step)
            brief = _brief_with_promise(
                "fixed", "moving",
                # Spec text DOES promise R5 (radius=5).
                "A plate with an R5 bore along the z-axis",
                "A shaft",
            )
            plan = _plan_with_r5_selector("fixed", "moving")
            part_results = {
                "fixed": _result("fixed", fixed_step),
                "moving": _result("moving", moving_step),
            }
            qa = self._qa_with_selector_miss(brief, part_results, plan.mates)
            state = _state(qa)
            state["assembly_brief"] = brief
            state["part_results"] = part_results
            out = graph.node_feedback_router(state)
        self.assertEqual(
            out["__next__"], "part_builder",
            f"spec-promised R5 selector miss must route to part_builder "
            f"(remodel), got: {out.get('__next__')}"
        )
        self.assertIn("fixed", out.get("remodel_part_ids", []))

    def test_spec_never_promised_cylinder_routes_to_mating_architect(self):
        """PartSpec text never promises any cylinder -> the plan
        hallucinated an R5 anchor on unspecified geometry -> route to
        mating_architect (remate)."""
        with tempfile.TemporaryDirectory() as tmp:
            fixed_step = Path(tmp) / "box.step"
            _box_step_no_cylinder(fixed_step)
            moving_step = Path(tmp) / "shaft.step"
            _shaft_step(moving_step)
            brief = _brief_with_promise(
                "fixed", "moving",
                # Spec text does NOT promise any cylinder.
                "A plain rectangular plate, 20x20x10",
                "A shaft",
            )
            plan = _plan_with_r5_selector("fixed", "moving")
            part_results = {
                "fixed": _result("fixed", fixed_step),
                "moving": _result("moving", moving_step),
            }
            qa = self._qa_with_selector_miss(brief, part_results, plan.mates)
            state = _state(qa)
            state["assembly_brief"] = brief
            state["part_results"] = part_results
            out = graph.node_feedback_router(state)
        self.assertEqual(
            out["__next__"], "mating_architect",
            f"plan-hallucinated R5 selector miss must route to "
            f"mating_architect (remate), got: {out.get('__next__')}"
        )

    def test_probe_error_routes_to_assembler_rerun(self):
        """When _attribute_selector_miss probes the current STEP and the
        probe itself errors (e.g. STEP unreadable), the router routes
        to assembler rerun-only (not remodel/remate)."""
        with tempfile.TemporaryDirectory() as tmp:
            fixed_step = Path(tmp) / "box.step"
            _box_step_no_cylinder(fixed_step)
            moving_step = Path(tmp) / "shaft.step"
            _shaft_step(moving_step)
            brief = _brief_with_promise(
                "fixed", "moving", "A plain box", "A shaft",
            )
            plan = _plan_with_r5_selector("fixed", "moving")
            part_results = {
                "fixed": _result("fixed", fixed_step),
                "moving": _result("moving", moving_step),
            }
            qa = self._qa_with_selector_miss(brief, part_results, plan.mates)
            state = _state(qa)
            state["assembly_brief"] = brief
            state["part_results"] = part_results
            # Mock probe_selector_anchor to return "error" -- simulates
            # a probe infrastructure fault during the router's
            # _attribute_selector_miss probe.
            with mock.patch(
                "mac_assembly.graph_assembly.probe_selector_anchor",
                return_value=("error", None),
            ):
                out = graph.node_feedback_router(state)
        self.assertEqual(
            out["__next__"], "assembler",
            f"probe error must route to assembler rerun-only (not "
            f"remodel/remate), got: {out.get('__next__')}"
        )
        # Rerun-only: repair_context must be empty.
        self.assertEqual(
            out.get("repair_context", ""), "",
            f"probe error rerun must have empty repair_context, got: "
            f"{out.get('repair_context')!r}"
        )


if __name__ == "__main__":
    unittest.main()
