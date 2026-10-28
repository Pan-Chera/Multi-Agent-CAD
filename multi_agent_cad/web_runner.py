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


def _locate_glb(step_path: Path | None, stl_path: Path | None = None) -> Path | None:
    """Build the preview from the exact mesh selected by the final state."""
    if stl_path is None or not stl_path.is_file():
        return None
    try:
        import trimesh
        glb_path = stl_path.with_suffix(".glb")
        trimesh.load(str(stl_path), force="mesh").export(str(glb_path), file_type="glb")
        return glb_path if glb_path.is_file() else None
    except Exception as exc:
        _emit({"warn": f"final GLB conversion failed: {exc}"})
        return None


def _artifact(value) -> Path | None:
    p = Path(value) if value else None
    return p.resolve() if p and p.is_file() and p.stat().st_size > 0 else None


def main() -> int:
    parser = argparse.ArgumentParser(prog="mac-web-runner")
    parser.add_argument("--config", required=True,
                        help="Path to config JSON (also pointed at by MAC_CONFIG_FILE)")
    parser.add_argument("--out", required=True,
                        help="Output directory; the runner chdir's here")
    parser.add_argument("--workflow", default="original",
                        choices=["original", "aider"])
    parser.add_argument("--source", type=Path, help="Existing Python source for the aider workflow")
    args = parser.parse_args()

    source = args.source.resolve() if args.source else None
    if args.workflow == "aider" and (source is None or not source.is_file()):
        _emit({"error": "aider workflow requires an existing --source Python file"})
        return 2
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

    from multi_agent_cad import nodes
    nodes._CACHE_DIR = out / "pipeline_cache"
    _tracker.reset()
    state = get_default_initial_state(workflow_id=args.workflow)
    # Never reuse a stale planner/architect cache for a fresh prompt.
    state["force_refresh"] = True

    if args.workflow == "aider":
        from multi_agent_cad.graph_aider import build_graph_aider
        state["current_python_code_path"] = str(source)
        app = build_graph_aider()
    else:
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

    step = _artifact(final.get("current_step_path"))
    stl = _artifact(final.get("current_stl_path"))
    print_stl = _artifact(final.get("current_print_stl_path"))
    py = _artifact(final.get("current_python_code_path"))
    measured_at = final.get("measurement_iteration", 0)
    measurements = _artifact(out / f"temp_measurements_{measured_at}.json")
    missed = _artifact(out / f"temp_missed_{measured_at}.json")
    glb = _locate_glb(step, stl)
    qa = final.get("qa_report")
    semantic = qa.get("semantic_verification", "unverified") if isinstance(qa, dict) else getattr(qa, "semantic_verification", "unverified")
    if semantic == "unverified":
        _emit({"warn": "Visual semantics were not verified; inspect the generated model."})

    err = final.get("error_type")
    err_val = err.value if hasattr(err, "value") else (err or "none")
    if err_val == "none" and (step is None or stl is None or semantic == "failed"):
        err_val = "fatal"
    summary = _tracker.summary()

    _emit({
        "done": True,
        "error_type": str(err_val),
        "step": str(step) if step else None,
        "stl": str(stl) if stl else None,
        "print_stl": str(print_stl) if print_stl else None,
        "semantic_verification": semantic,
        "glb": str(glb) if glb else None,
        "py": str(py) if py else None,
        "measurements": str(measurements) if measurements else None,
        "missed": str(missed) if missed else None,
        "tokens": summary["total_tokens"],
        "api_calls": summary["n_calls"],
    })
    return 0 if err_val == "none" else 1


if __name__ == "__main__":
    raise SystemExit(main())
