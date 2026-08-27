"""Configuration for the multi-agent assembly pipeline.

Single-part stages reuse ``multi_agent_cad.config`` verbatim (per-stage
hybrid routing already lives there). This file only adds the
assembly-level stages (Decomposer / Assembly Repair / Assembly Judge)
and the closed-loop budgets.
"""

from __future__ import annotations

import os

# Reuse the single-part pipeline's provider settings (endpoint / API key
# resolution happens in multi_agent_cad.nodes._llm_client).
from multi_agent_cad.config import DS_BASE_URL  # noqa: F401  (re-export)

# ============================================================================
# Default assembly request (used when MAC_ASSEMBLY_REQUEST env is unset)
# ============================================================================

DEFAULT_ASSEMBLY_REQUEST = (
    "Create a two-piece screw-top box assembly as a single STEP file in "
    "millimeters. Part 1 'base_box': a 60 x 60 x 35 mm hollow open-top box "
    "with 3 mm walls and floor, outer bottom face flat at Z=0, centered on "
    "the XY origin. Part 2 'lid': a 60 x 60 x 6 mm flat lid with a small "
    "cylindrical pull knob (radius 6 mm, height 8 mm) centered on its top "
    "face. The lid sits flat on the base box top rim with a 0.2 mm gap. "
    "Both parts must remain separate solids in one STEP file."
)

# ============================================================================
# Stage: Decomposer (assembly-level Spec Planner)
# ============================================================================

DECOMPOSER_MODEL = os.environ.get("MAC_DECOMPOSER_MODEL", "qwen3.8-max")
DECOMPOSER_TEMPERATURE = 0.0
DECOMPOSER_MAX_TOKENS = 32768
DECOMPOSER_KWARGS: dict = {"extra_body": {"enable_thinking": False}}
DECOMPOSER_MULTIMODAL = "auto"   # "auto" | "always" | "never"

# ============================================================================
# Stage: Mating Architect (assembly-level Geometric Architect -- the HOW)
# ============================================================================

MATING_MODEL = os.environ.get("MAC_MATING_MODEL", "qwen3.8-max")
MATING_TEMPERATURE = 0.0
MATING_MAX_TOKENS = 32768
MATING_KWARGS: dict = {"extra_body": {"enable_thinking": False}}

# ============================================================================
# Stage: Assembly Repair (edits temp_assembly.py when mates fail QA)
# ============================================================================

ASSEMBLY_REPAIR_MODEL = os.environ.get("MAC_ASM_REPAIR_MODEL", "qwen3.8-max")
ASSEMBLY_REPAIR_TEMPERATURE = 0.0
ASSEMBLY_REPAIR_MAX_TOKENS = 16384
ASSEMBLY_REPAIR_KWARGS: dict = {"extra_body": {"enable_thinking": True}}

# ============================================================================
# Stage: Assembly Judge (mirrors MAC's QA Judge: temp 0, anti-hallucination)
# ============================================================================

ASSEMBLY_JUDGE_ENABLED = True
ASSEMBLY_JUDGE_MIN_RETRY = 1        # judge only from retry >= this
ASSEMBLY_JUDGE_MODEL = os.environ.get("MAC_ASM_JUDGE_MODEL", "qwen3.8-max")
ASSEMBLY_JUDGE_TEMPERATURE = 0.0
ASSEMBLY_JUDGE_MAX_TOKENS = 8192
ASSEMBLY_JUDGE_KWARGS: dict = {"extra_body": {"enable_thinking": True}}
ASSEMBLY_JUDGE_MULTIMODAL = "auto"  # "auto" | "always" | "never"
ASSEMBLY_JUDGE_VIEWS_COUNT = 4
ASSEMBLY_JUDGE_VIEW_SIZE = 512
ASSEMBLY_JUDGE_SAVE_VIEWS = True

# ============================================================================
# Closed-loop budgets
# ============================================================================

# Outer assembly loop: assembler -> QA -> (judge) -> route back.
# This is the GLOBAL safety net across all route types (remate / remodel /
# repair_assembly / recompose). The per-route sub-budgets (MATINGS_MAX_RUNS,
# DECOMPOSER_MAX_RUNS) bind FIRST for their respective routes; this cap only
# fires when a route has no sub-budget (e.g. repair_assembly) or as a final
# backstop. With MATING_MAX_RUNS=4 the remate route is bounded by its
# sub-budget at mating_runs=4 (iteration_count ~5); with DECOMPOSER_MAX_RUNS=2
# the recompose route binds even earlier. The outer cap therefore primarily
# constrains repair_assembly (no sub-budget) and guards against runaway loops.
# Default 8 gives repair_assembly room for complex multi-part assemblies
# (dexterous hand etc.); override via MAC_ASSEMBLY_MAX_ITER for quick local
# runs. Lowering below 4 would gate remates prematurely and make
# MATING_MAX_RUNS dead code.
ASSEMBLY_MAX_ITERATIONS = int(os.environ.get("MAC_ASSEMBLY_MAX_ITER", "8"))

# Per-part generation retries inside part_builder.
PART_MAX_ATTEMPTS = 2

# Decomposer re-planning cap (recompose route).
DECOMPOSER_MAX_RUNS = 2

# Mating Architect re-planning cap (remate route). Allows up to 3 remates
# (mating_runs reaches 4 -> 4 < 4 False -> remate budget exhausted) on top
# of the initial mating. Pair with ASSEMBLY_MAX_ITERATIONS >= 5 so this
# budget, not the outer cap, binds for the remate route.
MATING_MAX_RUNS = 4

# ============================================================================
# Kinematic sweep (revolute mates)
# ============================================================================

# Rotate the moving subtree about each revolute joint axis over
# +/- KINEMATIC_SWEEP_DEG at KINEMATIC_SWEEP_SAMPLES angles (incl. 0)
# and test collisions against the static structure at each angle.
KINEMATIC_ENABLED = True
KINEMATIC_SWEEP_DEG = 30.0
KINEMATIC_SWEEP_SAMPLES = 5

# Translate the moving subtree of each linear/cylindrical joint along the
# mate axis over +/- LINEAR_SWEEP_MM at LINEAR_SWEEP_SAMPLES positions.
LINEAR_SWEEP_MM = 20.0
LINEAR_SWEEP_SAMPLES = 5

# QA tolerances
ENVELOPE_TOLERANCE_PCT = 0.15       # +-15% per axis on overall envelope
INTERFERENCE_VOLUME_TOL_MM3 = 5.0   # pairwise solid overlap tolerance
INTERFERENCE_VERTEX_FRACTION = 0.02 # >2% of sampled points penetrating -> fail
# Penetration deeper than this counts as interference. Contact/touching
# parts (sd ~= 0) are NOT interference -- essential for face_to_face
# mates and parts resting on each other.
INTERFERENCE_DEPTH_TOL_MM = 0.3

# Subprocess timeouts (seconds)
PART_PIPELINE_TIMEOUT = 3600        # one full single-part MAC run
ASSEMBLY_SCRIPT_TIMEOUT = 300       # execute temp_assembly.py

# Python interpreter used for subprocesses (defaults to sys.executable).
PYTHON_BIN = os.environ.get("MAC_PYTHON", "")
