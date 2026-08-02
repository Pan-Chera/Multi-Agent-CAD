"""
multi_agent_cad — Dual-Engine Multi-Agent CAD System powered by LangGraph.

This package implements a four-node LangGraph agent pipeline:

  1. Spec Planner              — user intent → CADBrief
  2. Geometric Architect       — CADBrief → ArchitectPlan (build123d modeling steps)
  3. Python Coder              — ArchitectPlan → build123d Python code → .step + .stl
  4. Autonomous Skill Loop     — internal Aider + Dual-QA closed loop (replaces
                                 the old qa → repair → qa LangGraph cycle)

The Autonomous Skill Loop runs an internal while-loop that:
  - Runs Engine A (cadpy.analysis) on the .step topology
  - Runs Engine B (check_mesh.py) on the .stl triangulation
  - Merges the QA reports; returns PASS if all checks pass
  - On failure, feeds error details to Aider (deepseek-v4-pro)
    which edits the Python source; then re-executes and re-tests
  - Max 5 internal retries before escalating to FATAL

Quick start::

    from multi_agent_cad.graph import build_graph

    app = build_graph()
    result = app.invoke({"user_request": "Design an L-bracket ..."})
"""

# --- Web UI config override (opt-in) ---------------------------------------
# When MAC_CONFIG_FILE points to a JSON file, apply its keys to the config
# module BEFORE nodes/graph are imported (they bind config values at import
# time via `from multi_agent_cad.config import X`). This lets the web UI
# (and any future CLI runner) inject per-job config without rewriting
# config.py. No-op when the env var is unset.
import os as _os
import json as _json

_mac_cfg_file = _os.environ.get("MAC_CONFIG_FILE")
if _mac_cfg_file:
    import multi_agent_cad.config as _mac_cfg
    with open(_mac_cfg_file, "r", encoding="utf-8") as _f:
        _mac_overrides = _json.load(_f)
    for _k, _v in _mac_overrides.items():
        setattr(_mac_cfg, _k, _v)

from multi_agent_cad.schemas import (
    # Enums
    ErrorType,
    ModelingStepType,
    VerificationKind,
    # Core models
    CADBrief,
    ArchitectPlan,
    QAReport,
    VerificationTarget,
    VerificationResult,
    EngineReport,
    # Modeling plan
    ModelingStep,
    Sketch,
    SketchEntity,
    Point2D,
    Point3D,
    # LangGraph state
    GraphState,
)

from multi_agent_cad.nodes import (
    node_spec_planner,
    node_geometric_architect,
    node_python_coder,
    node_autonomous_skill_loop,
)

__all__ = [
    # Enums
    "ErrorType",
    "ModelingStepType",
    "VerificationKind",
    # Core models
    "CADBrief",
    "ArchitectPlan",
    "QAReport",
    "VerificationTarget",
    "VerificationResult",
    "EngineReport",
    # Modeling plan
    "ModelingStep",
    "Sketch",
    "SketchEntity",
    "Point2D",
    "Point3D",
    # LangGraph state
    "GraphState",
    # Agent nodes
    "node_spec_planner",
    "node_geometric_architect",
    "node_python_coder",
    "node_autonomous_skill_loop",
]

__version__ = "1.0.0"
