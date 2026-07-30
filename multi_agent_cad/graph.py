"""
LangGraph orchestration for the Dual-Engine Multi-Agent CAD System.

Wires the four agent nodes into a stateful pipeline with conditional
routing so the system can self-correct dimension errors and topology
failures without human intervention.

Pipeline topology (Autonomous Skill Loop architecture)::

    planner ──→ architect ──→ coder ──→ autonomous_skill_loop ──→ END
       │             │            │
       └─────────────┴────────────┘
         (self-correction loops,
          max 3 retries each)

The **autonomous_skill_loop** node replaces the old qa → repair → qa
LangGraph loop.  All bug-fixing happens **inside** this node's internal
Aider + Dual-QA while loop.  LangGraph itself no longer handles error
routing after the coder — the super-node either returns PASS (→ END)
or exhausts its retry budget (→ END with FATAL).

Usage::

    python -m multi_agent_cad.graph
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

# Ensure the project root is on sys.path so the script can be run directly
# via ``python multi_agent_cad/graph.py`` (not just ``python -m``).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from langgraph.graph import END, StateGraph

# Importing token_tracker patches openai.resources.chat.completions.Completions.create
# and aider.models.Model so every API call (direct + Aider) is captured.
from multi_agent_cad.token_tracker import tracker as _token_tracker

from multi_agent_cad.schemas import ErrorType, GraphState
from multi_agent_cad.config import USER_REQUEST as _USER_REQUEST
from multi_agent_cad.nodes import (
    node_spec_planner,
    node_geometric_architect,
    node_python_coder,
    node_autonomous_skill_loop,
)

# ---------------------------------------------------------------------------
# Routing functions
# ---------------------------------------------------------------------------


# Per-node retry cap — self-correction loops stop after this many attempts
_MAX_SELF_RETRIES = 3


def route_after_planner(state: GraphState) -> str:
    """After Spec Planner: self-correct on error, proceed on success.

    * Planner has a valid cad_brief → forward to architect.
    * Planner failed (no cad_brief, error present) AND retries remain → retry planner.
    * Planner failed AND retries exhausted → FATAL → END.
    * FATAL → END.
    """
    error = _normalize_error(state.get("error_type"))

    if error == ErrorType.FATAL:
        return END

    # Count how many times planner has run
    history: list = state.get("node_history", [])
    planner_runs = sum(1 for n in history if n == "planner")

    if state.get("cad_brief") is not None:
        return "architect"  # success — proceed

    # No cad_brief — planner failed.  Retry if budget remains.
    if planner_runs < _MAX_SELF_RETRIES:
        return "planner"  # self-correct

    return END  # exhausted


def route_after_architect(state: GraphState) -> str:
    """After Geometric Architect: self-correct on error, proceed on success.

    * Architect has a valid architect_plan → forward to coder.
    * Architect failed (no plan, error present) AND retries remain → retry architect.
    * FATAL → END.
    """
    error = _normalize_error(state.get("error_type"))

    if error == ErrorType.FATAL:
        return END

    history: list = state.get("node_history", [])
    architect_runs = sum(1 for n in history if n == "architect")

    if state.get("architect_plan") is not None:
        return "coder"  # success — proceed

    # No architect_plan — architect failed.  Retry if budget remains.
    if architect_runs < _MAX_SELF_RETRIES:
        return "architect"  # self-correct

    return END  # exhausted


def route_after_coder(state: GraphState) -> Literal["autonomous_skill_loop", "coder", "__end__"]:
    """Decide where to go after the Python Coder node.

    * Files generated successfully → forward to autonomous_skill_loop.
    * Coder crashed (DIMENSION) AND retries remain → loop back to Coder.
    * Coder crashed AND retries exhausted → END (halt).
    * FATAL → halt immediately.
    """
    error = _normalize_error(state.get("error_type"))

    if error == ErrorType.FATAL:
        return END

    step_path = state.get("current_step_path")
    stl_path = state.get("current_stl_path")

    if step_path and stl_path:
        return "autonomous_skill_loop"

    # No output files — coder failed.  Check retry budget.
    history: list = state.get("node_history", [])
    coder_runs = sum(1 for n in history if n == "coder")
    if coder_runs < _MAX_SELF_RETRIES:
        return "coder"  # self-correct

    # Retries exhausted — fall through to autonomous_skill_loop so Aider
    # can attempt to fix the code that the deterministic coder couldn't
    # generate correctly.  The script_path from the deterministic coder's
    # attempt is still on disk; Aider can edit it in place.
    return "autonomous_skill_loop"


def _normalize_error(raw) -> ErrorType | None:
    """Coerce error_type from state (str, Enum, or None) into an ErrorType."""
    if raw is None:
        return None
    if isinstance(raw, ErrorType):
        return raw
    try:
        return ErrorType(str(raw))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


def build_graph() -> StateGraph:
    """Construct and compile the LangGraph state machine.

    Returns
    -------
    StateGraph
        A compiled LangGraph workflow ready for ``.stream()`` or ``.invoke()``.
    """
    workflow = StateGraph(GraphState)

    # -- Register nodes -----------------------------------------------------
    workflow.add_node("planner", node_spec_planner)
    workflow.add_node("architect", node_geometric_architect)
    workflow.add_node("coder", node_python_coder)
    workflow.add_node("autonomous_skill_loop", node_autonomous_skill_loop)

    # -- Entry point --------------------------------------------------------
    workflow.set_entry_point("planner")

    # -- Conditional edges (every node checks for FATAL before proceeding) --
    workflow.add_conditional_edges(
        "planner",
        route_after_planner,
        {"architect": "architect", "planner": "planner", END: END},
    )

    workflow.add_conditional_edges(
        "architect",
        route_after_architect,
        {"coder": "coder", "architect": "architect", END: END},
    )

    # -- Conditional: coder → autonomous_skill_loop (or self-loop on failure)
    workflow.add_conditional_edges(
        "coder",
        route_after_coder,
        {
            "autonomous_skill_loop": "autonomous_skill_loop",
            "coder": "coder",
            END: END,
        },
    )

    # -- Unconditional: autonomous_skill_loop ALWAYS goes to END ------------
    # The super-node handles all QA and repair internally.  When it returns,
    # the part is either ready (error_type=NONE) or exhausted (error_type=FATAL).
    workflow.add_edge("autonomous_skill_loop", END)

    return workflow.compile()


# ---------------------------------------------------------------------------
# Console output formatting
# ---------------------------------------------------------------------------

# ANSI colour codes (safe fallback when stdout is not a TTY)
_C_RESET = "\033[0m"
_C_BOLD = "\033[1m"
_C_DIM = "\033[2m"
_C_GREEN = "\033[32m"
_C_YELLOW = "\033[33m"
_C_RED = "\033[31m"
_C_CYAN = "\033[36m"
_C_MAGENTA = "\033[35m"
_C_BLUE = "\033[34m"

_NODE_COLORS = {
    "planner": _C_CYAN,
    "architect": _C_MAGENTA,
    "coder": _C_BLUE,
    "autonomous_skill_loop": _C_GREEN,
}

_NODE_LABELS = {
    "planner": "Spec Planner",
    "architect": "Geometric Architect",
    "coder": "Python Coder",
    "autonomous_skill_loop": "Autonomous Skill Loop (Aider + Dual-QA)",
}


def _use_colour() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _maybe_colour(text: str, code: str) -> str:
    if _use_colour():
        return f"{code}{text}{_C_RESET}"
    return text


def _print_header(title: str) -> None:
    print()
    print(_maybe_colour(f"╔{'═' * 58}╗", _C_BOLD))
    print(_maybe_colour(f"║ {title:<56} ║", _C_BOLD))
    print(_maybe_colour(f"╚{'═' * 58}╝", _C_BOLD))
    print()


def _print_node_start(node_name: str, iteration: int) -> None:
    colour = _NODE_COLORS.get(node_name, "")
    label = _NODE_LABELS.get(node_name, node_name)
    print(_maybe_colour(f"┌─ {label} ─────────────────────────────────────┐", colour))
    print(_maybe_colour(f"│  iteration: {iteration:<41} │", colour))


def _print_node_end(node_name: str, log_lines: list[str] | None) -> None:
    colour = _NODE_COLORS.get(node_name, "")
    if log_lines:
        for line in log_lines[:6]:
            clean = line[:54]
            print(_maybe_colour(f"│  {clean:<52} │", _C_DIM))
        if len(log_lines) > 6:
            print(_maybe_colour(f"│  ... ({len(log_lines)} log lines total){' ' * 25} │", _C_DIM))
    print(_maybe_colour(f"└{'─' * 54}┘", colour))
    print()


def _print_final_report(state: dict) -> None:
    """Print a human-readable summary after the pipeline finishes."""
    qa = state.get("qa_report")
    error = _normalize_error(state.get("error_type"))
    iterations = state.get("iteration_count", 0)
    step_path = state.get("current_step_path", "N/A")
    stl_path = state.get("current_stl_path", "N/A")

    print()
    _print_header("PIPELINE COMPLETE")

    if error == ErrorType.NONE:
        print(_maybe_colour("  STATUS  : PASS", _C_GREEN))
    elif error == ErrorType.FATAL:
        print(_maybe_colour("  STATUS  : FATAL", _C_RED))
    else:
        print(_maybe_colour(f"  STATUS  : HALTED ({error.value if error else 'unknown'})", _C_YELLOW))

    print(f"  Iterations    : {iterations}")
    print(f"  STEP file     : {step_path}")
    print(f"  STL file      : {stl_path}")

    if qa is not None:
        qa_obj = qa
        if hasattr(qa_obj, "passed_count"):
            print(f"  QA passed     : {qa_obj.passed_count}")
            print(f"  QA failed     : {qa_obj.failed_count}")
        if hasattr(qa_obj, "strength_score") and qa_obj.strength_score is not None:
            print(f"  Strength      : {qa_obj.strength_score}/100")
        if hasattr(qa_obj, "error_details") and qa_obj.error_details:
            print()
            print(_maybe_colour("  Issues:", _C_YELLOW))
            for detail in qa_obj.error_details[:5]:
                print(f"    - {detail[:70]}")

    print()
    print(_maybe_colour(f"  Full execution log: {len(state.get('execution_log', []))} entries", _C_DIM))

    # Print the last few log entries
    execution_log = state.get("execution_log", [])
    if execution_log:
        print()
        print(_maybe_colour("  Final log entries:", _C_DIM))
        for entry in execution_log[-8:]:
            print(_maybe_colour(f"    {entry[:100]}", _C_DIM))

    print()
    print("─" * 60)
    print()


# ---------------------------------------------------------------------------
# Shared configuration (used by both graph.py and graph_aider.py)
# ---------------------------------------------------------------------------


def get_default_initial_state(workflow_id: str = "original") -> GraphState:
    """Return the default initial state for the CAD pipeline.

    This function is shared between graph.py (original workflow) and
    graph_aider.py (Aider-first workflow) to ensure both use the same
    user request.

    Parameters
    ----------
    workflow_id : str
        Identifier for the workflow type. "original" for the standard pipeline,
        "aider" for the Aider-first pipeline. Used to separate output files.

    Returns
    -------
    GraphState
        Initial state dictionary with user_request and default parameters.
    """
    return {
        "user_request": _USER_REQUEST,
        "iteration_count": 0,
        "max_iterations": 5,
        "force_refresh": False,   # Set to True to force regeneration
        "workflow_id": workflow_id,  # "original" or "aider"
        "node_history": [],
        "execution_log": [],
    }

# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # -- Force UTF-8 on Windows terminals (prevents UnicodeEncodeError) -----
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    # ------------------------------------------------------------------
    # Build the graph
    # ------------------------------------------------------------------
    print()
    _print_header("DUAL-ENGINE MULTI-AGENT CAD SYSTEM")
    print("  Compiling LangGraph workflow ...")
    app = build_graph()
    print("  Ready.")
    print()

    # ------------------------------------------------------------------
    # Initial state
    # ------------------------------------------------------------------
    initial_state: GraphState = get_default_initial_state(workflow_id="original")

    print("  Initial Request:")
    print(f"    {initial_state['user_request']}")
    print()

    # ------------------------------------------------------------------
    # Reset token tracker for this workflow run
    # ------------------------------------------------------------------
    _token_tracker.reset()

    # ------------------------------------------------------------------
    # Run the pipeline
    # ------------------------------------------------------------------
    last_node = None
    accumulated_state: dict = dict(initial_state)
    exit_code = 0
    try:
        for event in app.stream(initial_state, {"recursion_limit": 50}):
            # LangGraph emits events keyed by node name
            for node_name, node_output in event.items():
                # Merge node output into accumulated state for final report
                accumulated_state.update(node_output)

                if node_name != last_node:
                    iteration = node_output.get("iteration_count", 0)
                    _print_node_start(node_name, iteration)
                    last_node = node_name

                # Print execution log lines if present
                log_lines = node_output.get("execution_log")
                if log_lines:
                    for line in log_lines:
                        clean = str(line)[:54]
                        print(_maybe_colour(f"│  {clean:<52} │", _C_DIM))

                _print_node_end(node_name, log_lines)
    except KeyboardInterrupt:
        print("\n\n  Interrupted by user.\n")
        exit_code = 130
    except Exception as exc:
        print(f"\n  Pipeline error: {exc}\n")
        import traceback
        traceback.print_exc()
        exit_code = 1

    # ------------------------------------------------------------------
    # Final report (always runs — on interrupt, shows partial state)
    # ------------------------------------------------------------------
    # Use the state accumulated from stream events (avoids re-running the
    # entire pipeline with app.invoke()).
    _print_final_report(accumulated_state)

    # ------------------------------------------------------------------
    # Token usage summary (per-module breakdown for this workflow run).
    # Runs even on interrupt/error so you always see what was consumed.
    # ------------------------------------------------------------------
    _token_tracker.print_summary()

    if exit_code:
        sys.exit(exit_code)
