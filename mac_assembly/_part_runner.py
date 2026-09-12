"""Subprocess runner: one single-part MAC pipeline, cwd-isolated.

Executed as ``python -m mac_assembly._part_runner [--mode {full,aider}]`` with::

    cwd    = <part_dir>                 (all temp_* files land here)
    env    MAC_PART_REQUEST   = single-part prompt
          MAC_PART_DIR        = absolute part dir (cache isolation)
          MAC_FORCE_REFRESH   = "1" to ignore the per-part plan cache (full mode only)

Modes:
    full   (default) -- regenerate a part from scratch via ``multi_agent_cad.graph``
                        (Spec Planner -> Architect -> Coder -> Skill Loop).
                        Reads MAC_FORCE_REFRESH from env so callers control caching.
    aider            -- patch an EXISTING part via ``multi_agent_cad.graph_aider``
                        (Aider-first workflow modifies the part's ``temp_design*.py``
                        with assembly-level feedback). Requires a pre-existing
                        ``temp_design*.py`` in the part dir; otherwise exits 3 so
                        the caller falls back to full regeneration. force_refresh
                        is hardcoded True -- the cache holds the pre-remodel design
                        hash, which is exactly what we want to bypass.

Patches before building the graph:

* ``multi_agent_cad.config.USER_REQUEST`` -- the part description.
* ``multi_agent_cad.nodes._CACHE_DIR``    -- per-part pipeline_cache so
  parallel/sequential parts never share Spec Planner / Architect caches.

The 10s interactive checkpoint auto-selects "1" (auto-iterate) because
stdin is not a TTY under subprocess -- exactly the CI behaviour MAC
already implements.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _parse_mode(argv: list[str]) -> str:
    parser = argparse.ArgumentParser(
        prog="mac_assembly._part_runner",
        description="Single-part MAC subprocess (full regenerate or aider remodel).",
    )
    parser.add_argument(
        "--mode", choices=("full", "aider"), default="full",
        help="full: regenerate from scratch (default). aider: patch existing design.",
    )
    args, _ = parser.parse_known_args(argv)
    return args.mode


def main(argv: list[str] | None = None) -> int:
    mode = _parse_mode(sys.argv[1:] if argv is None else argv)
    request = os.environ.get("MAC_PART_REQUEST", "")
    part_dir = Path(os.environ.get("MAC_PART_DIR", os.getcwd())).resolve()

    if not request:
        print(f"[part_runner:{mode}] MAC_PART_REQUEST not set", file=sys.stderr)
        return 2

    # Aider-first needs an existing design to patch.
    if mode == "aider" and not list(part_dir.glob("temp_design*.py")):
        print("[part_runner:aider] no existing temp_design*.py -- cannot patch")
        return 3

    # Patch config BEFORE importing multi_agent_cad.graph / graph_aider (they
    # capture USER_REQUEST at import time).
    import multi_agent_cad.config as mac_config

    mac_config.USER_REQUEST = request

    import multi_agent_cad.nodes as mac_nodes

    part_dir.mkdir(parents=True, exist_ok=True)
    mac_nodes._CACHE_DIR = part_dir / "pipeline_cache"  # per-part cache isolation

    if mode == "aider":
        from multi_agent_cad.graph_aider import build_graph_aider
        app = build_graph_aider()
        force_refresh = True  # bypass the pre-remodel cache hash
        workflow_id = "aider"
    else:
        from multi_agent_cad.graph import build_graph
        app = build_graph()
        force_refresh = os.environ.get("MAC_FORCE_REFRESH", "") == "1"
        workflow_id = "original"

    initial_state = {
        "user_request": request,
        "iteration_count": 0,
        "max_iterations": 5,
        "force_refresh": force_refresh,
        "workflow_id": workflow_id,
        "node_history": [],
        "execution_log": [],
    }
    explicit_code_path = os.environ.get("MAC_PART_CODE_PATH", "").strip()
    if explicit_code_path:
        initial_state["current_python_code_path"] = explicit_code_path

    final: dict = dict(initial_state)
    crashed = False
    try:
        for event in app.stream(initial_state, {"recursion_limit": 60}):
            for _node, node_output in event.items():
                if isinstance(node_output, dict):
                    final.update(node_output)
    except Exception as exc:  # noqa: BLE001 - report and fail this attempt
        print(f"[part_runner:{mode}] pipeline error: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        crashed = True
    finally:
        # Persist this subprocess's token usage so the parent can aggregate
        # end-to-end cost (the project's core metric). In finally so a
        # crashed pipeline's spend is still accounted (B11).
        try:
            import json

            from multi_agent_cad.token_tracker import tracker

            summary = tracker.summary()
            summary.pop("calls", None)  # detail not needed for aggregation
            (part_dir / "token_summary.json").write_text(
                json.dumps(summary, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001 - accounting must never fail the run
            print(f"[part_runner:{mode}] token summary failed: {exc}")

    if crashed:
        return 1

    error = final.get("error_type")
    error_str = getattr(error, "value", str(error)) if error else "none"
    ok = error_str == "none"

    print(f"[part_runner:{mode}] PART_DONE error_type={error_str}")
    return 0 if ok else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
