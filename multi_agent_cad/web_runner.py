"""Headless runner for the web UI (and any future CLI).

Reads config via the ``MAC_CONFIG_FILE`` env var (applied to
``multi_agent_cad.config`` by the package ``__init__`` before nodes/graph
bind their constants). Writes NDJSON progress lines to stdout, then a final
``{"done": ...}`` record with absolute paths to all artifacts.

Usage (invoked by ``multi_agent_cad.web.server``, not normally by humans)::

    MAC_CONFIG_FILE=/tmp/job/config.json \\
    DASHSCOPE_API_KEY=sk-... \\
    python -m multi_agent_cad.web_runner --config /tmp/job/config.json \\
        --out /tmp/job --workflow original

The runner chdir's into ``--out`` so all ``temp_*`` artifacts land there
(isolation = the B8 problem solved for the web path, zero nodes.py changes).
``stdin`` is /dev/null in the parent, so the interactive checkpoint auto-
selects "1" (non-TTY) and never blocks.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _emit(obj: dict) -> None:
    print(json.dumps(obj), flush=True)


def _find(pattern: str) -> Path | None:
    matches = sorted(
        Path.cwd().rglob(pattern),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def _locate_glb(step_path: Path | None) -> Path | None:
    """Find a renderable GLB. Three strategies in priority order."""
    # 1) cadpy writes a GLB as part of run_script_generator (deterministic coder).
    glbs = sorted(
        Path.cwd().rglob("*.glb"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if glbs:
        return glbs[0]
    # 2) Fallback: convert STL -> GLB via trimesh (LLM-fallback coder path).
    try:
        import trimesh  # type: ignore
        stl = _find("temp_output_*.stl")
        if stl:
            mesh = trimesh.load(str(stl))
            glb_path = (step_path.with_suffix(".glb")
                        if step_path else Path("model.glb"))
            mesh.export(str(glb_path), file_type="glb")
            if glb_path.is_file():
                return glb_path
    except Exception as e:
        _emit({"warn": f"trimesh GLB fallback failed: {e}"})
    return None


def main() -> int:
    parser = argparse.ArgumentParser(prog="mac-web-runner")
    parser.add_argument("--config", required=True,
                        help="Path to config JSON (also pointed at by MAC_CONFIG_FILE)")
    parser.add_argument("--out", required=True,
                        help="Output directory; the runner chdir's here")
    parser.add_argument("--workflow", default="original",
                        choices=["original", "aider"])
    args = parser.parse_args()

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    os.chdir(out)

    # If the server didn't set MAC_CONFIG_FILE, set it from --config so the
    # runner can also be invoked directly (e.g. for testing without the server).
    if not os.environ.get("MAC_CONFIG_FILE"):
        os.environ["MAC_CONFIG_FILE"] = str(args.config)

    # Importing multi_agent_cad triggers __init__.py, which applies the
    # MAC_CONFIG_FILE overrides to multi_agent_cad.config BEFORE nodes/graph
    # bind their module-level constants.
    from multi_agent_cad.graph import build_graph, get_default_initial_state
    from multi_agent_cad.token_tracker import tracker as _tracker

    _tracker.reset()
    state = get_default_initial_state(workflow_id=args.workflow)
    # Never reuse a stale planner/architect cache for a fresh prompt.
    state["force_refresh"] = True

    app = build_graph()
    final: dict = dict(state)
    try:
        for event in app.stream(state, {"recursion_limit": 50}):
            for node, node_out in event.items():
                final.update(node_out)
                log_lines = node_out.get("execution_log") or [""]
                _emit({
                    "stage": node,
                    "log": str(log_lines[-1]) if log_lines else "",
                    "iter": node_out.get("iteration_count", 0),
                })
    except Exception as e:
        import traceback
        _emit({"error": f"pipeline crashed: {e}",
               "traceback": traceback.format_exc()[-2000:]})
        return 1

    step = _find("temp_output_*.step")
    stl = _find("temp_output_*.stl")
    py = _find("temp_design_*.py")
    measurements = _find("temp_measurements_*.json")
    missed = _find("temp_missed_*.json")
    glb = _locate_glb(step)

    err = final.get("error_type")
    err_val = err.value if hasattr(err, "value") else (err or "none")
    summary = _tracker.summary()

    _emit({
        "done": True,
        "error_type": str(err_val),
        "step": str(step) if step else None,
        "stl": str(stl) if stl else None,
        "glb": str(glb) if glb else None,
        "py": str(py) if py else None,
        "measurements": str(measurements) if measurements else None,
        "missed": str(missed) if missed else None,
        "tokens": summary["total_tokens"],
        "api_calls": summary["n_calls"],
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
