"""
Modify-Existing-File Workflow: load an existing CAD Python script and iterate on it.

Skips Spec Planner / Geometric Architect / Python Coder. Instead:
1. Discover an existing ``temp_design*.py`` (or use ``current_python_code_path``
   from state if explicitly set).
2. Execute it once to produce fresh STEP/STL.
3. Hand off to ``node_autonomous_skill_loop`` — exactly the same Phase 4 as the
   original workflow. Aider iteratively modifies the .py based on the user
   request (which is treated as MODIFICATION REQUIREMENTS for the existing
   model, not a from-scratch spec).

Pipeline topology::

    existing_file_loader ──→ autonomous_skill_loop ──→ END

Usage::

    python -m multi_agent_cad.graph_aider
"""

from __future__ import annotations

import sys
import subprocess
from pathlib import Path

# Ensure the project root is on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from langgraph.graph import END, StateGraph

# Importing token_tracker patches openai.resources.chat.completions.Completions.create
# and registers a litellm.success_callback, so Aider + direct LLM calls are both captured.
from multi_agent_cad.token_tracker import tracker as _token_tracker
from multi_agent_cad.schemas import GraphState
from multi_agent_cad.nodes import node_autonomous_skill_loop


# ---------------------------------------------------------------------------
# Node: Existing File Loader
# ---------------------------------------------------------------------------


def node_existing_file_loader(state: GraphState) -> dict:
    """Locate an existing CAD Python script, execute it, and hand off to the
    autonomous skill loop for iterative Aider modification.

    Discovery order:
      1. ``state["current_python_code_path"]`` if set and the file exists
         (lets the caller pin a specific file).
      2. The most recently modified ``temp_design*.py`` in ``cwd``
         (matches both ``temp_design_0.py`` from the original workflow and
         ``temp_design_aider_0.py`` from previous Aider runs).

    After locating the file, this node:
      - Reads its content (so downstream nodes see the current code).
      - Executes it once via ``subprocess.run`` to produce fresh STEP/STL.
      - Auto-discovers the produced ``.step`` / ``.stl`` (or reuses the most
        recent ones in cwd if the script doesn't produce new files).

    The user_request in state is treated as MODIFICATION REQUIREMENTS for the
    existing file (e.g., "add a fillet to the top edge"). The autonomous skill
    loop will feed this to Aider alongside the QA report.

    Parameters
    ----------
    state : GraphState
        Must contain ``user_request`` (the modification requirements). If
        ``current_python_code_path`` is set, that file is used; otherwise
        auto-discovery runs.

    Returns
    -------
    dict
        Partial state update with ``current_python_code``,
        ``current_python_code_path``, ``current_step_path``,
        ``current_stl_path``, ``workflow_id="aider"`` (so the autonomous
        loop uses Aider-style file naming for downstream outputs), and log
        lines.
    """
    user_request = state.get("user_request", "")
    iteration = state.get("iteration_count", 0)

    print(f"\n{'='*60}")
    print(f"  [MODIFY-EXISTING] Iteration {iteration}")
    print(f"{'='*60}")
    print(f"  Modification request: {user_request[:100]}...")

    # -- 1. Discover the existing .py file --------------------------------
    explicit_path = state.get("current_python_code_path")
    if explicit_path and Path(explicit_path).is_file():
        code_path = Path(explicit_path)
        print(f"  Using explicit file from state: {code_path.name}")
    else:
        # Auto-discover: most recently modified temp_design*.py in cwd
        candidates = sorted(
            Path.cwd().glob("temp_design*.py"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            print(f"\n  [ERROR] No existing Python file found.")
            print(f"  Run the original workflow first (python -m multi_agent_cad.graph)")
            print(f"  or set current_python_code_path in state to point to a .py file.")
            return {
                "error_type": "FATAL",
                "execution_log": [
                    f"node_existing_file_loader [iter {iteration}]: "
                    f"FATAL — no existing .py file to modify",
                ],
                "node_history": list(state.get("node_history", [])) + ["existing_file_loader"],
            }
        code_path = candidates[0]
        print(f"  Auto-discovered: {code_path.name} (most recent temp_design*.py)")

    # -- 2. Read content --------------------------------------------------
    try:
        python_code = code_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"  [ERROR] Cannot read {code_path}: {exc}")
        return {
            "error_type": "FATAL",
            "execution_log": [
                f"node_existing_file_loader [iter {iteration}]: "
                f"FATAL — cannot read {code_path}: {exc}",
            ],
            "node_history": list(state.get("node_history", [])) + ["existing_file_loader"],
        }

    # -- 3. Execute the script to produce fresh STEP/STL -------------------
    # The script typically writes to temp_output_*.step/.stl. After execution,
    # we auto-discover the most recent .step/.stl in cwd as the baseline
    # for the autonomous loop's QA.
    print(f"\n  Executing existing script to produce baseline STEP/STL ...")
    try:
        result = subprocess.run(
            [sys.executable, str(code_path)],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode == 0:
            print(f"  Initial execution: SUCCESS")
        else:
            print(f"  Initial execution: FAILED (autonomous loop will fix)")
            err = (result.stderr or "")[:200]
            print(f"  Error: {err}")
    except subprocess.TimeoutExpired:
        print(f"  Initial execution: TIMEOUT (autonomous loop will fix)")
    except Exception as e:
        print(f"  Initial execution: ERROR - {e}")
        print(f"  Autonomous loop will attempt to fix this")

    # -- 4. Auto-discover STEP/STL outputs --------------------------------
    # Prefer the most recent .step / .stl in cwd (the script just wrote them).
    step_candidates = sorted(
        Path.cwd().glob("temp_output*.step"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    stl_candidates = sorted(
        Path.cwd().glob("temp_output*.stl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    actual_step_path = str(step_candidates[0]) if step_candidates else None
    actual_stl_path = str(stl_candidates[0]) if stl_candidates else None

    if actual_step_path:
        print(f"  Baseline STEP: {Path(actual_step_path).name}")
    else:
        print(f"  Baseline STEP: (none — autonomous loop will start without QA)")
    if actual_stl_path:
        print(f"  Baseline STL : {Path(actual_stl_path).name}")
    else:
        print(f"  Baseline STL : (none — autonomous loop will start without QA)")

    return {
        "current_python_code": python_code,
        "current_python_code_path": str(code_path),  # autonomous loop edits this file in place
        "current_step_path": actual_step_path,
        "current_stl_path": actual_stl_path,
        "iteration_count": iteration,
        "workflow_id": "aider",  # Aider-style file naming for downstream outputs
        "execution_log": [
            f"node_existing_file_loader [iter {iteration}]: "
            f"loaded {code_path.name}, executed, STEP={'yes' if actual_step_path else 'no'} "
            f"STL={'yes' if actual_stl_path else 'no'}",
            f"  Modification request: {user_request[:80]}...",
        ],
        "node_history": list(state.get("node_history", [])) + ["existing_file_loader"],
    }


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


def build_graph_aider() -> StateGraph:
    """Construct and compile the Modify-Existing-File workflow.

    Returns
    -------
    StateGraph
        A compiled LangGraph workflow with 2 nodes:
        1. existing_file_loader - locates + executes an existing .py
        2. autonomous_skill_loop - Phase 4 (unchanged from original workflow):
           QA + Aider repair + iteration, with the user_request treated as
           modification requirements for the existing file.
    """
    workflow = StateGraph(GraphState)

    # -- Register nodes -----------------------------------------------------
    workflow.add_node("existing_file_loader", node_existing_file_loader)
    workflow.add_node("autonomous_skill_loop", node_autonomous_skill_loop)

    # -- Entry point --------------------------------------------------------
    workflow.set_entry_point("existing_file_loader")

    # -- Edges --------------------------------------------------------------
    # existing_file_loader → autonomous_skill_loop
    workflow.add_edge("existing_file_loader", "autonomous_skill_loop")

    # autonomous_skill_loop → END
    workflow.add_edge("autonomous_skill_loop", END)

    return workflow.compile()


# ---------------------------------------------------------------------------
# Console output formatting (reused from graph.py)
# ---------------------------------------------------------------------------

from multi_agent_cad.graph import (
    _print_header,
    _print_node_start,
    _print_node_end,
    _print_final_report,
)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # -- Force UTF-8 on Windows terminals -----------------------------------
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    # ------------------------------------------------------------------
    # Build the graph
    # ------------------------------------------------------------------
    print()
    _print_header("MODIFY-EXISTING-FILE CAD SYSTEM")
    print("  Compiling modify-existing workflow ...")
    app = build_graph_aider()
    print("  Ready.")
    print()

    # ------------------------------------------------------------------
    # Initial state (imported from graph.py so both workflows share the
    # same request). The user_request here is interpreted as MODIFICATION
    # REQUIREMENTS for an existing .py file in cwd.
    # ------------------------------------------------------------------
    from multi_agent_cad.graph import get_default_initial_state
    initial_state: GraphState = get_default_initial_state(workflow_id="aider")

    print("  Modification Request:")
    print(f"    {initial_state['user_request']}")
    print()

    # ------------------------------------------------------------------
    # Reset token tracker for this workflow run
    # ------------------------------------------------------------------
    _token_tracker.reset()

    # ------------------------------------------------------------------
    # Run the pipeline
    # ------------------------------------------------------------------
    exit_code = 0
    last_node = None
    accumulated_state: dict = dict(initial_state)
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
                        print(f"│  {clean:<52} │")

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
    _print_final_report(accumulated_state)

    # ------------------------------------------------------------------
    # Token usage summary (per-module breakdown for this workflow run).
    # Runs even on interrupt/error so you always see what was consumed.
    # ------------------------------------------------------------------
    _token_tracker.print_summary()

    if exit_code:
        sys.exit(exit_code)
