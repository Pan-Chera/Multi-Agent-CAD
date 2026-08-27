"""Top-level assembly LangGraph + CLI.

Pipeline (closed loop)::

    decomposer ──> part_builder ──> assembler ──> assembly_qa ──> judge
         ^              │                              │            │
         │              │                              │            v
         │              │                              │     feedback_router
         │              │                              │      │   │   │   │
         │              └──── remodel_parts <──────────┘ <─────┘   │   │
         └──────── recompose <────────────────────────────────────┘   │
                    repair_assembly ──> assembler (iter+1) <──────────┘
                    END (accept / halt / budget exhausted)

The feedback router is a *node* (not a conditional edge) because it
mutates state: it converts the QA report + Judge decision into routing
hints (`remodel_part_ids`, `repair_context`) reused from MAC's
repair-prompt pattern and CAD Skills' repair-loop failure classes.

Usage::

    python -m mac_assembly                       # default request
    MAC_ASSEMBLY_REQUEST="..." python -m mac_assembly
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from langgraph.graph import END, StateGraph

from multi_agent_cad.token_tracker import tracker as _token_tracker

from mac_assembly import config_assembly as cfg
from mac_assembly.nodes_assembly import (
    node_assembler,
    node_assembly_judge,
    node_assembly_qa,
    node_decomposer,
    node_mating_architect,
    node_part_builder,
)
from mac_assembly.schemas_assembly import (
    AssemblyErrorType,
    AssemblyGraphState,
    AssemblyJudgeAction,
)

# ---------------------------------------------------------------------------
# Feedback router (node -- mutates state, then a conditional edge routes)
# ---------------------------------------------------------------------------


def node_feedback_router(state: AssemblyGraphState) -> dict:
    """Convert QA report + Judge decision into the next loop action.

    Failure classes mirror ``repair-loop.md``: part missing -> regenerate
    part; mates/interference -> repair assembly script; decomposition
    wrong -> recompose; infeasible -> halt.
    """
    qa = state.get("qa_report")
    decision = state.get("judge_decision")
    iteration = state.get("iteration_count", 0)

    if qa is None or qa.all_passed:
        return _route(state, "end", note="QA passed")
    if decision is not None and decision.action == AssemblyJudgeAction.ACCEPT:
        return _route(state, "end", note="judge ACCEPT: " + decision.reason[:160])
    if decision is not None and decision.action == AssemblyJudgeAction.HALT:
        return _route(state, "end", note="judge HALT: " + decision.reason[:160])

    if iteration >= state.get("max_iterations", cfg.ASSEMBLY_MAX_ITERATIONS):
        return _route(
            state, "end",
            note=f"budget exhausted after {iteration} assembly iterations",
        )

    errors = "\n".join(f"- {d}" for d in qa.error_details)
    judge_ctx = ""
    if decision is not None:
        judge_ctx = f"\nJudge decision ({decision.action.value}): {decision.reason}"
        if decision.evidence:
            judge_ctx += "\nEvidence: " + "; ".join(decision.evidence[:5])

    if decision is not None and decision.action == AssemblyJudgeAction.REMODEL_PARTS:
        ids = [i for i in decision.remodel_part_ids if i] or qa.needs_remodel_part_ids
        return _route(
            state, "part_builder",
            remodel_part_ids=ids,
            repair_context=errors + judge_ctx,
            note=f"judge remodel_parts: {ids}",
        )

    if decision is not None and decision.action == AssemblyJudgeAction.RECOMPOSE:
        if state.get("decomposer_runs", 1) < cfg.DECOMPOSER_MAX_RUNS:
            return _route(
                state, "decomposer",
                repair_context=errors + judge_ctx,
                note="judge recompose",
            )
        return _route(state, "end", note="recompose budget exhausted")

    if qa.error_type == AssemblyErrorType.PART_MISSING and qa.needs_remodel_part_ids:
        return _route(
            state, "part_builder",
            remodel_part_ids=qa.needs_remodel_part_ids,
            repair_context=errors,
            note=f"part_missing: {qa.needs_remodel_part_ids}",
        )

    # Mate design errors (misalignment / interference / kinematics /
    # envelope / reconciliation) go back to the Mating Architect with the
    # measured deltas.
    mate_level = qa.error_type in (
        AssemblyErrorType.MATE_MISALIGNMENT,
        AssemblyErrorType.INTERFERENCE,
        AssemblyErrorType.KINEMATIC,
        AssemblyErrorType.ENVELOPE,
        AssemblyErrorType.RECONCILE,
    )
    if (decision is not None and decision.action == AssemblyJudgeAction.REMATE) or (
        decision is None and mate_level
    ):
        if state.get("mating_runs", 1) < cfg.MATING_MAX_RUNS:
            return _route(
                state, "mating_architect",
                repair_context=errors + judge_ctx,
                note=f"remate ({qa.error_type.value})",
            )
        return _route(state, "end", note="remate budget exhausted")

    # Default: repair the generated assembly source (script-level failures).
    return _route(
        state, "assembler",
        repair_context=errors + judge_ctx,
        note=f"repair_assembly ({qa.error_type.value})",
    )


def _route(state: AssemblyGraphState, next_node: str, note: str = "", **extra) -> dict:
    update: dict = {"__next__": next_node}
    update.update(extra)
    update["execution_log"] = list(state.get("execution_log", [])) + [
        f"feedback_router -> {next_node}: {note}"
    ]
    update["node_history"] = list(state.get("node_history", [])) + ["feedback_router"]
    print(f"[assembly] ROUTE -> {next_node}  ({note})")
    return update


def route_after_decomposer(state: AssemblyGraphState) -> str:
    if state.get("assembly_brief") is None:
        return END
    return "mating_architect"


def route_after_mating(state: AssemblyGraphState) -> str:
    """After the Mating Architect: assemble directly on remate (parts are
    already built); build parts first on the initial pass."""
    if state.get("mating_plan") is None:
        return END
    part_results = state.get("part_results") or {}
    brief = state.get("assembly_brief")
    parts_ready = (
        brief is not None
        and all(
            part_results.get(p.part_id) and part_results[p.part_id].ok
            for p in brief.parts
        )
    )
    return "assembler" if parts_ready else "part_builder"


def route_after_feedback(state: AssemblyGraphState) -> str:
    return state.get("__next__", END)


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


def build_assembly_graph():
    workflow = StateGraph(AssemblyGraphState)

    workflow.add_node("decomposer", node_decomposer)
    workflow.add_node("mating_architect", node_mating_architect)
    workflow.add_node("part_builder", node_part_builder)
    workflow.add_node("assembler", node_assembler)
    workflow.add_node("assembly_qa", node_assembly_qa)
    workflow.add_node("judge", node_assembly_judge)
    workflow.add_node("feedback_router", node_feedback_router)

    workflow.set_entry_point("decomposer")
    workflow.add_conditional_edges(
        "decomposer", route_after_decomposer,
        {"mating_architect": "mating_architect", END: END},
    )
    workflow.add_conditional_edges(
        "mating_architect", route_after_mating,
        {"part_builder": "part_builder", "assembler": "assembler", END: END},
    )
    workflow.add_edge("part_builder", "assembler")
    workflow.add_edge("assembler", "assembly_qa")
    workflow.add_edge("assembly_qa", "judge")
    workflow.add_edge("judge", "feedback_router")
    workflow.add_conditional_edges(
        "feedback_router", route_after_feedback,
        {
            "assembler": "assembler",
            "part_builder": "part_builder",
            "mating_architect": "mating_architect",
            "decomposer": "decomposer",
            END: END,
        },
    )
    return workflow.compile()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def get_initial_state(request: str | None = None, work_dir: str | None = None) -> AssemblyGraphState:
    from datetime import datetime

    req = request or os.environ.get("MAC_ASSEMBLY_REQUEST") or cfg.DEFAULT_ASSEMBLY_REQUEST
    if work_dir is None:
        # Allow re-running on an existing job dir via env var (resumes in-place).
        env_work_dir = os.environ.get("MAC_ASSEMBLY_WORK_DIR")
        if env_work_dir:
            wd = Path(env_work_dir)
            if not wd.is_absolute():
                wd = Path.cwd() / wd
            wd.mkdir(parents=True, exist_ok=True)
            work_dir = str(wd)
    if work_dir is None:
        jobs_root = Path.cwd() / "assembly_jobs"
        jobs_root.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        for i in range(100):
            candidate = jobs_root / f"job_{stamp}_{i:02d}"
            if not candidate.exists():
                candidate.mkdir(parents=True)
                work_dir = str(candidate)
                break
        else:  # pragma: no cover
            work_dir = str(jobs_root / f"job_{stamp}_x")
    return {
        "user_request": req,
        "work_dir": work_dir,
        "assembly_brief": None,
        "mating_plan": None,
        "part_results": {},
        "iteration_count": 0,
        "max_iterations": cfg.ASSEMBLY_MAX_ITERATIONS,
        "decomposer_runs": 0,
        "mating_runs": 0,
        "remodel_part_ids": [],
        "repair_context": "",
        "workflow_id": "assembly",
        "node_history": [],
        "execution_log": [],
    }


def _print_end_to_end_tokens(state: dict) -> None:
    """Aggregate parent-process usage + every part subprocess's usage."""
    try:
        parent = _token_tracker.summary()
    except Exception:  # noqa: BLE001
        parent = {}
    part_totals = {"n_calls": 0, "total_tokens": 0, "total_input": 0, "total_output": 0}
    for pid, r in sorted((state.get("part_results") or {}).items()):
        u = getattr(r, "token_usage", {}) or {}
        part_totals["n_calls"] += int(u.get("n_calls", 0))
        for k in ("total_tokens", "total_input", "total_output"):
            part_totals[k] += int(u.get(k, 0))
    print()
    print("  End-to-end token usage (parent agents + all part pipelines):")
    print(f"    part subprocesses : {part_totals['n_calls']} calls, "
          f"{part_totals['total_tokens']:,} tokens "
          f"(in {part_totals['total_input']:,} / out {part_totals['total_output']:,})")
    print(f"    assembly agents   : {parent.get('n_calls', 0)} calls, "
          f"{parent.get('total_tokens', 0):,} tokens "
          f"(in {parent.get('total_input', 0):,} / out {parent.get('total_output', 0):,})")
    grand = part_totals["total_tokens"] + int(parent.get("total_tokens", 0))
    print(f"    TOTAL             : {grand:,} tokens")


def _final_report(state: dict) -> None:
    print()
    print("=" * 70)
    print("  ASSEMBLY PIPELINE COMPLETE")
    print("=" * 70)
    qa = state.get("qa_report")
    decision = state.get("judge_decision")
    if qa is not None and (qa.all_passed or (decision is not None and str(decision.action) == "AssemblyJudgeAction.ACCEPT")):
        print("  STATUS  : PASS")
    elif decision is not None and "HALT" in str(decision.action):
        print("  STATUS  : HALTED (judge: infeasible request)")
    else:
        print("  STATUS  : INCOMPLETE (see QA report)")
    print(f"  Work dir   : {state.get('work_dir')}")
    print(f"  Assembly   : {state.get('assembly_step_path') or 'N/A'}")
    print(f"  Script     : {state.get('assembly_py_path') or 'N/A'}")
    results = state.get("part_results") or {}
    for pid, r in sorted(results.items()):
        print(f"  part {pid:<20} {'OK ' if r.ok else 'FAIL'} {r.step_path}")
    if qa is not None and qa.error_details:
        print("  QA issues:")
        for d in qa.error_details[:8]:
            print(f"    - {d}")
    print("=" * 70)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    print()
    print("=" * 70)
    print("  MULTI-AGENT CAD ASSEMBLY PIPELINE (mac_assembly)")
    print("=" * 70)

    initial = get_initial_state()
    print(f"  Request : {initial['user_request'][:100]}...")
    print(f"  Workdir : {initial['work_dir']}")
    print()

    _token_tracker.reset()
    app = build_assembly_graph()

    accumulated: dict = dict(initial)
    try:
        for event in app.stream(initial, {"recursion_limit": 120}):
            for node_name, node_output in event.items():
                if isinstance(node_output, dict):
                    node_output.pop("__next__", None)
                    accumulated.update(node_output)
    except KeyboardInterrupt:
        print("\n  Interrupted.\n")
    except Exception as exc:  # noqa: BLE001
        print(f"\n  Pipeline error: {exc}\n")
        import traceback

        traceback.print_exc()

    _final_report(accumulated)

    # Handoff packet: GLB + URDF + snapshot views + manifest (viewer-ready).
    # Best-effort: a handoff export bug should not crash the pipeline, but
    # silently swallowing the exception loses the traceback -- write the full
    # stack to <work_dir>/handoff_error.log and point the user at it so URDF/GLB
    # export regressions are actually debuggable.
    work_dir = Path(accumulated["work_dir"])
    try:
        from mac_assembly.handoff import build_handoff, print_handoff

        manifest = build_handoff(
            work_dir,
            assembly_brief=accumulated.get("assembly_brief"),
            qa_report=accumulated.get("qa_report"),
        )
        print_handoff(manifest, work_dir)
    except Exception as exc:  # noqa: BLE001 - handoff is best-effort
        import traceback

        tb = traceback.format_exc()
        try:
            (work_dir / "handoff_error.log").write_text(tb, encoding="utf-8")
            print(f"  handoff FAILED: {exc}  (see {work_dir}/handoff_error.log)")
        except Exception:  # noqa: BLE001 - log write must not mask the original error
            print(f"  handoff FAILED: {exc}\n{tb}")

    _print_end_to_end_tokens(accumulated)
    _token_tracker.print_summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())
