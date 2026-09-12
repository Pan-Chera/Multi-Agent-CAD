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

import ast
import os
import re
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
from mac_assembly.part_generator import (
    has_explicit_accepted_cache,
    is_non_retryable_error,
    set_api_key_snapshot,
)
from mac_assembly.selector_resolver import probe_selector_anchor

# ---------------------------------------------------------------------------
# Feedback router (node -- mutates state, then a conditional edge routes)
# ---------------------------------------------------------------------------

# "SELECTOR anchor matched no face on <part_id>: <query dict repr>" --
# emitted verbatim by the generated assembly script (assembly_codegen
# _emit_anchor), so the part id and the failing semantic query can be
# recovered deterministically from the traceback tail.
_SELECTOR_MISS_RE = re.compile(
    r"SELECTOR anchor matched no face on ([A-Za-z0-9_\-]+): (\{.*\})"
)

# Round-feature vocabulary for the spec-promise text check.
_CYL_KEYWORD_RE = re.compile(
    r"\b(?:bore|bores|hole|holes|cylinder|cylindrical|pin|pins|shaft|"
    r"axle|journal|hub|dowel|clevis|eye|knuckle|pivot|bearing|rod)\b",
    re.IGNORECASE,
)

# key_dimensions keys that denote a cylindrical RADIUS/DIAMETER. A key
# qualifies only if it carries a radius/diameter token AND is not a
# coordinate/other-dimension key. This is the fix for the cross-context
# number-gluing false positive: arbitrary keys (width, height, thickness,
# axis_ranges, *_midpoint_local) are NEVER cylindrical evidence, so a
# width=30 can no longer be read as a promised Ø30 bore.
_NON_CYL_DIM_KEY_RE = re.compile(
    r"(?:midpoint|center|centre|position|origin|offset|range|coord|"
    r"location|angle|length|width|height|thick|depth|extent|count|qty|"
    r"number|mass|weight|volume|area|spacing|pitch|distance|_x\b|_y\b|_z\b)",
    re.IGNORECASE,
)


def _cyl_dim_kind(key: str) -> str | None:
    """Return the unit semantics encoded by a key_dimensions key.

    Keeping radius and diameter distinct is essential: a ``bore_radius=30``
    must not be accepted as evidence for a queried R15 cylinder merely
    because 30 also happens to equal its diameter.
    """
    if _NON_CYL_DIM_KEY_RE.search(key):
        return None
    lowered = key.lower()
    tokens = set(re.split(r"[^a-z0-9]+|_+", lowered))
    if "radius" in lowered or "r" in tokens:
        return "radius"
    if "diam" in lowered or "d" in tokens:
        return "diameter"
    return None


def _is_cyl_dim_key(key: str) -> bool:
    """Backward-compatible predicate used by focused tests."""
    return _cyl_dim_kind(key) is not None


def _iter_key_dim_scalars(obj, key: str = ""):
    """Yield ``(key, float)`` for every scalar under ``key_dimensions``,
    propagating the nearest dict key so a list of radii keeps its key."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _iter_key_dim_scalars(v, str(k))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _iter_key_dim_scalars(v, key)
    elif isinstance(obj, bool):
        return
    elif isinstance(obj, (int, float)):
        yield key, float(obj)


def _matches_cyl_dimension(kind: str, num: float, radius: float) -> bool:
    """Match a typed dimension against the selector's radius."""
    expected = radius if kind == "radius" else 2.0 * radius
    return abs(num - expected) <= 0.05


def _radius_diameter_mentions(sentence: str) -> list[tuple[str, float]]:
    """Explicit radius/diameter numeric mentions in ``sentence`` ONLY.

    A bare "N mm" (thickness / width / depth / hole COUNT) is deliberately
    NOT collected -- treating it as a radius was the glue that produced the
    false positives ("Plate 15 mm thick ... holes R3" read as a promised
    R15). Only these forms count:
      * ``R15`` / ``Ø30`` / ``⌀30``      (prefix denotes a round dimension)
      * ``diameter 30`` / ``dia 30`` / ``radius 15`` / ``radius of 15``
      * ``30 mm diameter`` / ``15 mm radius``
    """
    nums: list[tuple[str, float]] = []
    for m in re.finditer(r"\bR\s*(\d+(?:\.\d+)?)", sentence,
                         re.IGNORECASE):
        nums.append(("radius", float(m.group(1))))
    for m in re.finditer(r"(?:Ø|⌀)\s*(\d+(?:\.\d+)?)", sentence):
        nums.append(("diameter", float(m.group(1))))
    for m in re.finditer(
        r"\b(diameter|dia|radius)\b\s*(?:of)?\s*(\d+(?:\.\d+)?)",
        sentence, re.IGNORECASE,
    ):
        kind = "radius" if m.group(1).lower() == "radius" else "diameter"
        nums.append((kind, float(m.group(2))))
    for m in re.finditer(
        r"(\d+(?:\.\d+)?)\s*(?:mm)?\s*\b(diameter|dia|radius)\b",
        sentence, re.IGNORECASE,
    ):
        kind = "radius" if m.group(2).lower() == "radius" else "diameter"
        nums.append((kind, float(m.group(1))))
    return nums


def _axes_mentioned(sentence: str) -> set[str]:
    """Explicit cylinder-axis declarations in a feature sentence."""
    axes: set[str] = set()
    patterns = (
        r"\b(?:local\s+)?([xyz])\s*[- ]?\s*axis\b",
        r"\baxis\s*(?:is|=|along)?\s*(?:the\s+)?(?:local\s+)?([xyz])\b",
        r"\balong\s+(?:the\s+)?(?:local\s+)?([xyz])(?:\s*[- ]?\s*axis)?\b",
    )
    for pattern in patterns:
        axes.update(m.group(1).lower() for m in re.finditer(
            pattern, sentence, re.IGNORECASE
        ))
    return axes


def _round_feature_axes(text: str, v: float) -> set[str]:
    """Axes explicitly attached to a matching round feature sentence."""
    axes: set[str] = set()
    for sentence in re.split(r"[.;\n]+", text):
        if not _CYL_KEYWORD_RE.search(sentence):
            continue
        mentions = _radius_diameter_mentions(sentence)
        if mentions and not any(
            _matches_cyl_dimension(kind, num, v) for kind, num in mentions
        ):
            continue
        axes.update(_axes_mentioned(sentence))
    return axes


def _text_promise_cylinder(text: str, v: float, axis: str) -> bool:
    """A round-feature keyword AND a matching radius/diameter mention in
    the SAME sentence (the reviewer's proximity requirement: the feature
    word and the dimension must belong to one feature, not be glued from
    opposite ends of the description)."""
    for sentence in re.split(r"[.;\n]+", text):
        if not _CYL_KEYWORD_RE.search(sentence):
            continue
        mentions = _radius_diameter_mentions(sentence)
        if any(_matches_cyl_dimension(kind, num, v)
               for kind, num in mentions):
            declared_axes = _axes_mentioned(sentence)
            if declared_axes and axis not in declared_axes:
                continue
            return True
    return False


def _key_dims_promise_cylinder(key_dims, v: float) -> bool:
    for key, num in _iter_key_dim_scalars(key_dims or {}):
        kind = _cyl_dim_kind(key)
        if kind is not None and _matches_cyl_dimension(kind, num, v):
            return True
    return False


def _spec_promises_geometry(spec, query: dict) -> bool:
    """Did the PartSpec ever promise the geometry this SELECTOR queried?

    Deterministic heuristic, deliberately conservative: a false negative
    costs one remate (the pre-attribution behaviour), a false positive
    costs a needless rebuild of a CORRECT part, so ambiguity resolves to
    'not promised'. Evidence must be feature-local -- a round-feature
    keyword next to an explicit radius/diameter in one sentence, or a
    semantic ``*_radius`` / ``*_diameter`` key -- never a bare number
    glued to an unrelated keyword.

    Only cylinder queries are checkable: ``value_mm`` is a radius, and
    specs mention either the radius ("R15", "bore_radius": 15.0) or the
    diameter ("Ø30"). Plane queries carry a coordinate, not a feature
    dimension, so no reliable promise signal exists -> False.
    """
    if str(query.get("surface", "")).lower() != "cylinder":
        return False
    text = str(spec.description or "")
    value = query.get("value_mm")
    if value is None:
        # No radius to match ("largest cylinder along <axis>"): require the
        # spec to name that axis explicitly AND talk about a round feature.
        axis = str(query.get("axis", "")).lower()
        return bool(axis) and bool(_CYL_KEYWORD_RE.search(text)) and bool(
            re.search(rf"\b{re.escape(axis)}\s*-?\s*(?:axis|axial)\b",
                      text, re.IGNORECASE)
        )
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    axis = str(query.get("axis", "")).lower()
    # An explicit feature-axis declaration is authoritative. A matching
    # radius on the wrong axis describes a different cylinder and therefore
    # cannot justify rebuilding the part for this selector query.
    declared_axes = _round_feature_axes(text, v)
    if axis and declared_axes and axis not in declared_axes:
        return False
    # (A) structured evidence: a typed cylindrical key matching R or Ø.
    if _key_dims_promise_cylinder(getattr(spec, "key_dimensions", None), v):
        return True
    # (B) textual evidence: same-sentence keyword + explicit R/Ø mention.
    return _text_promise_cylinder(text, v, axis)


def _generation_config_promises_geometry(spec, query: dict) -> bool:
    """Does the executable part-generation config request this geometry?

    ``PartSpec.description`` and ``key_dimensions`` describe intent, but a
    deterministic builder only receives ``builder.params`` plus ``features``.
    Treating prose-only intent as a builder defect creates a no-op remodel
    loop: the builder faithfully regenerates the same geometry forever.

    Free-form parts remain prose-driven.  For ``base_body`` parts, the base
    body's own prompt and deterministic features are executable inputs.
    """
    if str(query.get("surface", "")).lower() != "cylinder":
        return False

    # A free-form part is generated directly from its description.
    builder = getattr(spec, "builder", None)
    base_body = getattr(spec, "base_body", None)
    features = list(getattr(spec, "features", None) or [])
    if builder is None and base_body is None:
        return _spec_promises_geometry(spec, query)

    executable_texts: list[str] = []
    executable_dims: list[dict] = []
    if builder is not None:
        executable_dims.append(dict(getattr(builder, "params", {}) or {}))
    if base_body is not None:
        executable_texts.append(str(getattr(base_body, "description", "") or ""))
    for feature in features:
        executable_texts.append(str(getattr(feature, "name", "") or ""))
        params = dict(getattr(feature, "params", {}) or {})
        executable_dims.append(params)
        # Preserve axis discrimination for standard kinematic features.
        pin_axis = params.get("pin_axis")
        if pin_axis:
            executable_texts[-1] += f" {pin_axis}-axis"

    value = query.get("value_mm")
    if value is not None:
        try:
            radius = float(value)
        except (TypeError, ValueError):
            return False
        if not any(_key_dims_promise_cylinder(dims, radius)
                   for dims in executable_dims):
            return False

    axis = str(query.get("axis", "") or "").lower()
    combined = ". ".join(executable_texts)
    declared_axes = _round_feature_axes(combined, float(value)) \
        if combined and value is not None else set()
    if axis and declared_axes and axis not in declared_axes:
        return False
    return bool(executable_dims or combined)


def _attribute_selector_miss(state: AssemblyGraphState, detail: str):
    """Deterministic root-cause attribution for a SELECTOR anchor miss.

    Returns ``(kind, part_id, reason)`` with kind in {"repair",
    "probe_error", "remodel", "recompose", "remate"}:

    1. Probe the failing query against the part's CURRENT STEP with the
       same resolver the script used (three-state):
         * FOUND  -> "repair": the geometry exists; the failed run used a
           stale STEP or hit a transient resolver fault. Rerun the
           assembler (remating cannot help, the plan was not wrong).
         * ERROR  -> "probe_error": the STEP would not load / cadpy fault /
           index build failed. This is INFRASTRUCTURE, NOT proof the
           geometry is absent, so it must NOT drive a rebuild or a remate
           -- route to a rerun instead.
         * NOT_FOUND -> the geometry is genuinely absent; fall through.
    2. Absent + executable generation config promised this cylinder ->
       "remodel": the part build failed to realize its own spec.
    3. Absent + prose promised it but a deterministic builder/base-body
       config omitted it -> "recompose": decomposition failed to encode the
       requested geometry (a rebuild would be byte-identical).
    4. Absent + the spec never promised it -> "remate": the plan
       hallucinated an anchor on unspecified geometry (telescopic-crane
       2026-09-09: R15 cylinder queried on a prismatic pedestal_housing).

    Unparseable details degrade to "remate" (the pre-attribution
    behaviour), so the guard never makes routing worse than before.
    """
    m = _SELECTOR_MISS_RE.search(detail)
    if m is None:
        return "remate", None, "unparsed selector-miss detail"
    part_id = m.group(1)
    try:
        query = ast.literal_eval(m.group(2))
    except (ValueError, SyntaxError):
        query = None
    if not isinstance(query, dict):
        return "remate", part_id, "unparseable selector query"

    # (1) three-state geometry probe against the CURRENT part STEP
    pr = (state.get("part_results") or {}).get(part_id)
    step_path = str(getattr(pr, "step_path", "") or "") if pr is not None else ""
    if not step_path:
        return "probe_error", part_id, (
            f"cannot probe {part_id}: current PartResult has no STEP path -- "
            f"missing state is not proof that selector geometry is absent"
        )
    if not Path(step_path).is_file():
        return "probe_error", part_id, (
            f"cannot probe {part_id}: current STEP does not exist at "
            f"{step_path} -- a stale path is not proof that selector "
            f"geometry is absent"
        )
    if step_path:
        try:
            status, hit = probe_selector_anchor(step_path, query)
        except Exception as exc:  # noqa: BLE001 - unexpected probe fault
            status, hit = "error", None
            print(f"[assembly] selector-miss probe raised: "
                  f"{type(exc).__name__}: {exc}")
        if status == "found":
            return "repair", part_id, (
                f"queried geometry IS present in the current STEP of "
                f"{part_id} (resolves to selector "
                f"{(hit or {}).get('selector')!r}) -- the failed run used a "
                f"stale STEP or hit a resolver fault; rerun the script, "
                f"remating cannot help"
            )
        if status == "error":
            return "probe_error", part_id, (
                f"could not probe {part_id}'s STEP at {step_path} "
                f"(unreadable STEP / cadpy unavailable / index build "
                f"failed) -- this is an infrastructure fault, NOT proof "
                f"the geometry is absent; rerun the assembler, do not "
                f"rebuild the part or rewrite the plan on a probe error"
            )
        # status == "not_found": geometry genuinely absent -> fall through.

    # (2)/(3) geometry absent: plan defect or part defect?
    brief = state.get("assembly_brief")
    spec = None
    if brief is not None:
        spec = next((p for p in brief.parts if p.part_id == part_id), None)
    if spec is not None and _generation_config_promises_geometry(spec, query):
        return "remodel", part_id, (
            f"executable generation config of {part_id} promises the queried cylinder "
            f"(axis={query.get('axis')}, r={query.get('value_mm')}) but the "
            f"built STEP has no such face -- part build defect"
        )
    if spec is not None and _spec_promises_geometry(spec, query):
        return "recompose", part_id, (
            f"prose/key dimensions of {part_id} promise the queried cylinder "
            f"(axis={query.get('axis')}, r={query.get('value_mm')}) but its "
            f"builder/base_body/features do not encode it -- decomposition "
            f"defect; rebuilding the unchanged config cannot help"
        )
    return "remate", part_id, (
        f"mating plan references geometry {part_id}'s spec never promised "
        f"(query={query})"
    )


def node_feedback_router(state: AssemblyGraphState) -> dict:
    """Convert QA report + Judge decision into the next loop action.

    Failure classes mirror ``repair-loop.md``: part missing -> regenerate
    part; mates/interference -> repair assembly script; decomposition
    wrong -> recompose; infeasible -> halt.

    Routing priority (first match wins) -- keep in sync with the README
    "反馈路由策略" table:

    0. qa None / all_passed, or judge ACCEPT / HALT -> END. Exception:
       all_passed WITH degraded parts runs the Judge anyway, and a
       corrective decision (REMODEL_PARTS / RECOMPOSE) falls through to
       rules 2/3 instead of ending -- a degraded artifact is never
       delivered as a silent success (ids fall back to
       qa.degraded_part_ids when the Judge names none).
    1. iteration_count >= ASSEMBLY_MAX_ITERATIONS -> END (global backstop)
    2. judge REMODEL_PARTS -> part_builder (judge's ids cross-checked
       against the brief; falls back to qa.needs_remodel_part_ids, then
       falls THROUGH to the rules below when neither yields a real id)
    3. judge RECOMPOSE -> decomposer (DECOMPOSER_MAX_RUNS)
    4. qa.needs_recompose_ids (parts whose failure carries the "v3 spec
       unchanged" marker: same base_body + features already failed, and
       the REMODEL route cannot modify the spec) -> decomposer
       (DECOMPOSER_MAX_RUNS). Prevents burning PART_BUILDER_MAX_RUNS on
       deterministic no-op rebuilds.
    5. qa PART_MISSING (+ needs_remodel_part_ids) -> part_builder
       (PART_BUILDER_MAX_RUNS). Beats a judge REMATE on purpose: a
       missing part is a BUILD failure, not a mate-design failure --
       remating cannot manufacture the part, and route_after_mating
       would bounce to part_builder anyway after burning a mating LLM
       call.
    6. judge REMATE, or (judge gave NO decision AND mate-level
       error_type: MATE_MISALIGNMENT / INTERFERENCE / KINEMATIC /
       ENVELOPE / RECONCILE) -> mating_architect (MATING_MAX_RUNS).
       Exception: no judge decision + error_attribution == 'part_geometry'
       (+ attribution_part_ids) -> part_builder with those ids (e.g. a
       reconcile radius mismatch is a part defect that remate cannot fix).
       ENVELOPE / RECONCILE always run the Judge from iteration 0 (see
       node_assembly_judge), so a missing decision here means the Judge
       is disabled.
       6b. "SELECTOR anchor matched no face" in qa.error_details ->
       deterministic attribution (_attribute_selector_miss), overriding
       the judge: probe the failing query against the part's CURRENT STEP
       (three-state probe_selector_anchor) and check the PartSpec text.
         * FOUND (geometry present) -> assembler RERUN-ONLY
           (repair_context=""; stale-STEP / transient resolver fault -- a
           rerun against the current STEP can succeed, remating cannot
           help; empty repair_context avoids forcing an LLM repair over a
           script that reruns fine);
         * PROBE_ERROR (STEP unreadable / cadpy fault) -> assembler
           RERUN-ONLY too: an infrastructure fault is NOT proof the
           geometry is absent, so it must not trigger a rebuild or remate;
         * NOT_FOUND + spec promised the cylinder (feature-local radius/
           diameter evidence) -> part_builder (PART_BUILDER_MAX_RUNS;
           build defect -- only a rebuild can manufacture the face);
         * NOT_FOUND + spec never promised it -> mating_architect
           (MATING_MAX_RUNS; the plan hallucinated the anchor, and the
           deterministic codegen would regenerate a byte-identical script
           under repair_assembly -- a provable no-op loop).
    7. default -> assembler (repair_assembly): judge REPAIR_ASSEMBLY,
       FATAL, and every unmatched combination.

    Authority rule: with a judge decision on the table, QA's error_type
    classification only routes via rule 4 (hard precondition); the
    mate-level half of rule 5 requires decision is None. In particular
    judge REPAIR_ASSEMBLY on a mate-level error (e.g. INTERFERENCE)
    goes to the assembler -- the judge wins over the QA class. Note the
    interaction with ``_judge_gate``: an ACCEPT on INTERFERENCE without
    high confidence is DOWNGRADED to REPAIR_ASSEMBLY, which lands here
    on the assembler route; script-level repair only converges for
    interference if the LLM edit actually moves parts, otherwise the
    outer budget (ASSEMBLY_MAX_ITERATIONS) burns out where remate would
    have been the durable fix.
    """
    qa = state.get("qa_report")
    decision = state.get("judge_decision")
    iteration = state.get("iteration_count", 0)

    if qa is None or qa.all_passed:
        # Exception (degraded delivery gate): geometry QA passed but the
        # report carries degraded parts, so the Judge ran anyway. A
        # corrective decision (REMODEL_PARTS on the degraded ids /
        # RECOMPOSE) wins over the blanket "QA passed -> END" -- otherwise
        # a known-imperfect artifact would be delivered as final success
        # and the Judge's verdict would be dead weight. ACCEPT / HALT /
        # no-decision / non-corrective actions still terminate as before.
        corrective = decision is not None and decision.action in (
            AssemblyJudgeAction.REMODEL_PARTS,
            AssemblyJudgeAction.RECOMPOSE,
        )
        if not (getattr(qa, "has_degraded_parts", False) and corrective):
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

    # P0-2: configuration / infrastructure failures (missing API key, auth
    # rejection, bad endpoint/model, missing dependency) are NOT retryable --
    # no amount of remodel / full regeneration / remate / recompose changes
    # the environment. Route straight to END instead of burning the
    # PART_BUILDER_MAX_RUNS budget on deterministic re-failures (2026-09-10:
    # a blanked DS_API_KEY made every part subprocess die with
    # "DASHSCOPE_API_KEY is not set" and the loop consumed all 4 remodel
    # rounds on it). Only checked on a failing QA (a passing delivery with a
    # kept-best previous result is unaffected).
    if qa is None or not qa.all_passed:
        non_retryable = [
            (pid, str(getattr(r, "error", "") or ""))
            for pid, r in sorted((state.get("part_results") or {}).items())
            if not getattr(r, "ok", True)
            and is_non_retryable_error(getattr(r, "error", None))
        ]
        if non_retryable:
            return _route(
                state, "end",
                note=(
                    "configuration/infrastructure failure (not retryable: "
                    "auth/endpoint/model/dependency): "
                    + "; ".join(
                        f"{pid}: {err[:120]}" for pid, err in non_retryable[:3]
                    )
                ),
            )

    errors = "\n".join(f"- {d}" for d in qa.error_details)
    judge_ctx = ""
    if decision is not None:
        judge_ctx = f"\nJudge decision ({decision.action.value}): {decision.reason}"
        if decision.evidence:
            judge_ctx += "\nEvidence: " + "; ".join(decision.evidence[:5])

    if decision is not None and decision.action == AssemblyJudgeAction.REMODEL_PARTS:
        # Cross-check the judge's part ids against the brief: a hallucinated
        # id would make part_builder skip every spec and burn a full loop
        # (QA then reproduces the same failure). Fall back to QA's own
        # remodel list; if neither source yields a real id, fall through to
        # the mate-level / script-repair routes below.
        brief = state.get("assembly_brief")
        brief_ids = {p.part_id for p in brief.parts} if brief else set()
        ids = [i for i in decision.remodel_part_ids if i in brief_ids]
        if not ids:
            ids = [i for i in qa.needs_remodel_part_ids if i in brief_ids]
        if not ids and qa.all_passed:
            # Degraded-delivery gate fall-through: QA passed, so there is
            # no needs_remodel list -- the actionable ids are the degraded
            # parts the Judge was convened about (it forgot to name them).
            ids = [
                i for i in getattr(qa, "degraded_part_ids", [])
                if i in brief_ids
            ]
        if ids:
            # An explicit accepted_part_cache.json is an operator decision
            # that freezes the current part geometry.  Sending a RECONCILE
            # failure back to part_builder with any such id is a deterministic
            # no-op: part_builder correctly reloads the accepted artifact and
            # the same radius mismatch returns until its budget is exhausted.
            # Let the Mating Architect redesign anchors/joint semantics around
            # the accepted geometry instead.
            if qa.error_type == AssemblyErrorType.RECONCILE:
                work_dir = Path(str(state.get("work_dir") or ""))
                spec_by_id = {p.part_id: p for p in (brief.parts if brief else [])}
                accepted_ids = [
                    pid for pid in ids
                    if pid in spec_by_id
                    and has_explicit_accepted_cache(
                        spec_by_id[pid], work_dir / "parts"
                    )
                ]
                if accepted_ids:
                    if state.get("mating_architect_runs", 1) < cfg.MATING_MAX_RUNS:
                        return _route(
                            state, "mating_architect",
                            repair_context=errors + judge_ctx + (
                                "\nOperator-approved immutable parts: "
                                + ", ".join(accepted_ids)
                                + ". Do not request part remodeling; redesign "
                                  "the mating plan around their measured geometry."
                            ),
                            note=(
                                "reconcile touches explicit accepted cache; "
                                f"remate immutable geometry {accepted_ids}"
                            ),
                        )
                    return _route(
                        state, "end",
                        note="remate budget exhausted for explicit accepted geometry",
                    )
            return _route_part_builder(
                state, ids,
                repair_context=errors + judge_ctx,
                note=f"judge remodel_parts: {ids}",
            )

    if decision is not None and decision.action == AssemblyJudgeAction.RECOMPOSE:
        if state.get("decomposer_llm_calls", 1) < cfg.DECOMPOSER_MAX_RUNS:
            return _route(
                state, "decomposer",
                repair_context=errors + judge_ctx,
                note="judge recompose",
            )
        return _route(state, "end", note="recompose budget exhausted")

    # v3 no-op loop guard (P0-3): parts whose generation failed with the
    # "v3 spec unchanged" marker have no part-level repair channel (same
    # base_body + same features reproduces the same failure). Route them to
    # the Decomposer INSTEAD of part_builder, which would burn
    # PART_BUILDER_MAX_RUNS on deterministic no-op rebuilds.
    if getattr(qa, "needs_recompose_ids", None):
        if state.get("decomposer_llm_calls", 1) < cfg.DECOMPOSER_MAX_RUNS:
            return _route(
                state, "decomposer",
                repair_context=errors + judge_ctx,
                note=(
                    "v3 spec unchanged since failed generation "
                    f"(parts {qa.needs_recompose_ids}); recompose required"
                ),
            )
        return _route(state, "end", note="recompose budget exhausted (v3 unchanged)")

    if qa.error_type == AssemblyErrorType.PART_MISSING and qa.needs_remodel_part_ids:
        # Beats judge REMATE deliberately (see docstring rule 4): a missing
        # part is a build failure. judge_ctx is still forwarded so the
        # rebuild sees the judge's reasoning, same as every other route.
        return _route_part_builder(
            state, qa.needs_remodel_part_ids,
            repair_context=errors + judge_ctx,
            note=f"part_missing: {qa.needs_remodel_part_ids}",
        )

    # SELECTOR-miss failures get DETERMINISTIC attribution (see
    # _attribute_selector_miss): the miss is not automatically a plan
    # defect. Probe the part's current STEP (three-state) and the PartSpec
    # text, then route:
    #   FOUND / PROBE_ERROR -> assembler RERUN-ONLY (repair_context="");
    #   NOT_FOUND + spec-promised -> part_builder (build defect);
    #   NOT_FOUND + never promised -> mating_architect (plan hallucinated).
    # This beats the Judge's decision deliberately (the Judge cannot know
    # the codegen is deterministic: with a plan defect, every
    # repair_assembly iteration regenerates a byte-identical script and
    # re-dies at the same raise -- telescopic-crane run 2026-09-09 burned
    # 3 iterations on identical scripts), same authority pattern as rule 5.
    sel_miss = next(
        (d for d in qa.error_details if "SELECTOR anchor matched no face" in d),
        None,
    )
    if sel_miss is not None:
        kind, miss_part, reason = _attribute_selector_miss(state, sel_miss)
        attribution_ctx = f"\nSelector-miss attribution: {reason}"
        if kind in ("repair", "probe_error"):
            # RERUN-ONLY: repair_context MUST stay empty. node_assembler
            # branch 3 forces _llm_repair_assembly whenever repair_context
            # is non-empty EVEN IF the deterministic rerun already
            # succeeded (P2) -- it would replace a now-correct script with
            # an LLM edit. With "" the assembler just regenerates + reruns
            # the deterministic script against the CURRENT STEP; if that
            # still fails, branch 2 (`if not ok`) runs the LLM repair with
            # the fresh traceback on its own. The attribution is preserved
            # in the execution_log note, not in repair_context.
            return _route(
                state, "assembler",
                repair_context="",
                note=f"selector miss rerun-only ({kind}): {reason[:120]}",
            )
        if kind == "remodel" and miss_part:
            return _route_part_builder(
                state, [miss_part],
                repair_context=errors + judge_ctx + attribution_ctx,
                note=f"selector miss: spec-promised geometry absent from "
                     f"built STEP ({miss_part})",
            )
        if kind == "recompose":
            if state.get("decomposer_llm_calls", 1) < cfg.DECOMPOSER_MAX_RUNS:
                return _route(
                    state, "decomposer",
                    repair_context=errors + judge_ctx + attribution_ctx,
                    note=f"selector miss: requested geometry omitted from "
                         f"structured part spec ({miss_part})",
                )
            return _route(
                state, "end",
                note="recompose budget exhausted (selector spec omission)",
            )
        if state.get("mating_architect_runs", 1) < cfg.MATING_MAX_RUNS:
            return _route(
                state, "mating_architect",
                repair_context=errors + judge_ctx + attribution_ctx,
                note=f"selector miss: plan references unspecified geometry "
                     f"({miss_part})",
            )
        return _route(state, "end", note="remate budget exhausted (selector miss)")

    # Mate design errors (misalignment / interference / kinematics /
    # envelope / reconciliation) go back to the Mating Architect with the
    # measured deltas. EXCEPT when the QA report carries a structured
    # part_geometry attribution (P1-8): e.g. a reconcile radius mismatch
    # is a part defect -- remate cannot change measured radii, so route
    # the named parts to part_builder. This branch only fires when the
    # Judge gave no decision (judge disabled); with a decision on the
    # table the Judge's action wins per the authority rule above.
    mate_level = qa.error_type in (
        AssemblyErrorType.MATE_MISALIGNMENT,
        AssemblyErrorType.INTERFERENCE,
        AssemblyErrorType.KINEMATIC,
        AssemblyErrorType.ENVELOPE,
        AssemblyErrorType.RECONCILE,
    )
    if (
        decision is None
        and mate_level
        and getattr(qa, "error_attribution", "ambiguous") == "part_geometry"
        and getattr(qa, "attribution_part_ids", None)
    ):
        attributed_ids = [i for i in qa.attribution_part_ids if i in {
            p.part_id for p in (state.get("assembly_brief").parts
                                if state.get("assembly_brief") else [])
        }]
        if attributed_ids:
            return _route_part_builder(
                state, attributed_ids,
                repair_context=errors + judge_ctx,
                note=(
                    f"part_geometry attribution ({qa.error_type.value}): "
                    f"{attributed_ids}"
                ),
            )
    if (decision is not None and decision.action == AssemblyJudgeAction.REMATE) or (
        decision is None and mate_level
    ):
        if state.get("mating_architect_runs", 1) < cfg.MATING_MAX_RUNS:
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


def _route_part_builder(state: AssemblyGraphState, ids: list[str], note: str,
                        repair_context: str) -> dict:
    """Route to part_builder with a per-route budget.

    The part_builder route consumes NO outer-loop budget on the
    PART_MISSING path (the assembler early-returns with
    qa_skipped_iter=True, so iteration_count never advances and the outer
    ASSEMBLY_MAX_ITERATIONS backstop never fires). Without this cap, a
    persistently failing part (structurally infeasible builder params,
    a MAC pipeline that never succeeds) loops unbounded -- burning up to
    PART_MAX_ATTEMPTS full MAC pipeline runs per round -- until
    recursion_limit crashes the pipeline. Shared by the REMODEL_PARTS and
    PART_MISSING routes: both spend real tokens in part_builder.
    """
    runs = state.get("part_builder_remodel_runs", 0)
    if runs >= cfg.PART_BUILDER_MAX_RUNS:
        return _route(
            state, "end",
            note=f"part_builder budget exhausted after {runs} remodel "
                 f"rounds ({note})",
        )
    return _route(
        state, "part_builder",
        remodel_part_ids=ids,
        repair_context=repair_context,
        note=note,
        part_builder_remodel_runs=runs + 1,
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
    already built); build parts first on the initial pass.

    A failed architect run (both internal attempts rejected by validation)
    loops back into mating_architect with the errors as feedback while the
    MATING_MAX_RUNS budget lasts. A first-pass failure used to END the
    whole pipeline here -- two drifting plans killed the telescopic-crane
    run in minutes (2026-09-09) before any part was built.
    """
    if state.get("mating_plan") is None:
        if state.get("mating_architect_runs", 0) < cfg.MATING_MAX_RUNS:
            return "mating_architect"
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
    # The router writes the readable token "end" into __next__ (nicer logs /
    # execution_log entries), but LangGraph's termination sentinel is END
    # ("__end__"), and that is the only form the conditional-edge map below
    # accepts -- returning "end" raises KeyError('end') at EVERY router
    # termination (QA pass / judge ACCEPT / HALT / any budget exhaustion),
    # which main() then swallows as a generic "Pipeline error".
    nxt = state.get("__next__", END)
    return END if nxt == "end" else nxt


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
        {
            "mating_architect": "mating_architect",  # first-pass retry loop
            "part_builder": "part_builder",
            "assembler": "assembler",
            END: END,
        },
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
        "cumulative_part_token_usage": {},
        "iteration_count": 0,
        "max_iterations": cfg.ASSEMBLY_MAX_ITERATIONS,
        "decomposer_llm_calls": 0,
        "mating_architect_runs": 0,
        "part_builder_remodel_runs": 0,
        "remodel_part_ids": [],
        "repair_context": "",
        "workflow_id": "assembly",
        "node_history": [],
        "execution_log": [],
    }


def _print_end_to_end_tokens(state: dict) -> None:
    """Aggregate parent-process usage + every part subprocess's usage.

    P1-3: per-part figures come from ``cumulative_part_token_usage`` (the
    cross-attempt ledger) -- a part's LAST PartResult carries only its last
    attempt's summary, so a first-round 100k-token attempt that was later
    overwritten by a fast cheap failure used to drop out of the report
    entirely (2026-09-10: chassis_body's 101,276 tokens vanished this way).
    Falls back to the last PartResult's usage for parts with no ledger
    entry (legacy job dirs / builder-only paths).
    """
    try:
        parent = _token_tracker.summary()
    except Exception:  # noqa: BLE001
        parent = {}
    cumulative = state.get("cumulative_part_token_usage") or {}
    per_part: dict[str, dict] = {
        pid: (getattr(r, "token_usage", {}) or {})
        for pid, r in (state.get("part_results") or {}).items()
    }
    # Ledger wins where present, and covers parts dropped by a later
    # recompose (their spend must not vanish with their PartResult).
    per_part.update(cumulative)
    part_totals = {"n_calls": 0, "total_tokens": 0, "total_input": 0, "total_output": 0}
    for pid, u in sorted(per_part.items()):
        part_totals["n_calls"] += int(u.get("n_calls", 0))
        for k in ("total_tokens", "total_input", "total_output"):
            part_totals[k] += int(u.get(k, 0))
    print()
    print("  End-to-end token usage (parent agents + all part pipelines):")
    print(f"    part subprocesses : {part_totals['n_calls']} calls, "
          f"{part_totals['total_tokens']:,} tokens "
          f"(in {part_totals['total_input']:,} / out {part_totals['total_output']:,})")
    grand = part_totals["total_tokens"] + int(parent.get("total_tokens", 0))
    print(f"    TOTAL             : {grand:,} tokens  "
          f"(parent agents detail: see summary below)")



def _final_report(state: dict) -> None:
    print()
    print("=" * 70)
    print("  ASSEMBLY PIPELINE COMPLETE")
    print("=" * 70)
    qa = state.get("qa_report")
    decision = state.get("judge_decision")
    if qa is not None and (qa.all_passed or (decision is not None and decision.action == AssemblyJudgeAction.ACCEPT)):
        if getattr(qa, "has_degraded_parts", False):
            print(f"  STATUS  : PASS (degraded parts: "
                  f"{', '.join(qa.degraded_part_ids)} -- see "
                  f"generation_warnings in the QA report)")
        else:
            print("  STATUS  : PASS")
    elif decision is not None and decision.action == AssemblyJudgeAction.HALT:
        print("  STATUS  : HALTED (judge: infeasible request)")
    else:
        results_ = state.get("part_results") or {}
        config_failures = [
            (pid, str(getattr(r, "error", "") or ""))
            for pid, r in sorted(results_.items())
            if not getattr(r, "ok", True) and is_non_retryable_error(
                getattr(r, "error", None)
            )
        ]
        if config_failures:
            print("  STATUS  : INCOMPLETE (configuration/infrastructure "
                  "failure -- not retryable)")
            for pid, err in config_failures[:4]:
                print(f"    - {pid}: {err[:200]}")
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


def _preflight_dependencies() -> None:
    """Probe dependencies BEFORE the workflow runs and TERMINATE on a
    failed containment probe, so an environment gap is never misread as
    a geometry result.

    Core deps (build123d/langgraph/trimesh) fail loudly at import time.
    The ``rtree`` package (declared in requirements/pyproject) degrades
    two ways when missing:
    - proximity queries (snap-to-surface, min-gap, penetration depth)
      fall back to the slow naive path via closest_point_robust -- costs
      time, not correctness;
    - ray-parity containment (Mesh.contains) has NO fallback: the
      interference / kinematic collision checks would silently SKIP
      every pair and report false PASSes -- an all-green QA report with
      false negatives, delivered without the Judge ever running.

    That second failure mode is why a failed probe is FATAL here
    instead of a warning: the pipeline refuses to deliver a PASS it
    cannot verify. run_assembly_qa additionally stamps the QA report's
    generation_warnings so DIRECT calls (re-QA scripts) still surface
    the gap next to their results.
    """
    from mac_assembly.geometry_utils import mesh_containment_available

    if not mesh_containment_available():
        raise RuntimeError(
            "trimesh ray-containment probe FAILED (the 'rtree' "
            "dependency is missing in this environment): interference "
            "and kinematic collision checks would SKIP pairs and report "
            "FALSE PASSES -- the pipeline refuses to deliver a PASS it "
            "cannot verify. Fix: pip install rtree"
        )


def _resolve_api_key() -> str:
    """Startup snapshot of the DashScope API key (P0-1).

    Priority (unchanged from ``_llm_client``): the ``DASHSCOPE_API_KEY``
    environment variable, then ``multi_agent_cad.config.DS_API_KEY``. The
    resolved value is installed as the subprocess snapshot and, when it came
    from config.py, exported into this process's environment so the parent's
    own LLM calls are equally immune to a mid-run config edit. The value
    itself is never printed, logged, or written to any artifact.
    """
    key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not key:
        from multi_agent_cad import config as mac_config

        key = str(getattr(mac_config, "DS_API_KEY", "") or "").strip()
    return key


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    # P0-1: key preflight BEFORE any node, state, or work dir exists. The
    # Decomposer requires an LLM; with no key in EITHER source the run can
    # only burn tokens/time on deterministic failures -- exit FATAL instead.
    api_key = _resolve_api_key()
    if not api_key:
        print(
            "[assembly] FATAL: no DashScope API key found (neither the "
            "DASHSCOPE_API_KEY environment variable nor DS_API_KEY in "
            "multi_agent_cad/config.py). The Decomposer requires an LLM -- "
            "refusing to start: no workflow is built, no tokens are spent.",
            file=sys.stderr,
        )
        return 2
    set_api_key_snapshot(api_key)
    if not os.environ.get("DASHSCOPE_API_KEY"):
        # Key came from config.py: pin it in this process's environment too
        # so parent-agent LLM calls use the startup snapshot rather than
        # re-reading config.py at call time.
        os.environ["DASHSCOPE_API_KEY"] = api_key

    try:
        _preflight_dependencies()
    except RuntimeError as exc:
        print(f"[assembly] FATAL: {exc}", file=sys.stderr)
        return 2

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
    # Exit-code contract:
    #   0   normal completion
    #   1   runtime exception during app.stream
    #   130 KeyboardInterrupt (POSIX SIGINT convention)
    #   2   preflight failure (returned above, before this block)
    # A crashed run must NOT build a handoff packet or print a "successful
    # assembly" report -- CI/CD relies on the exit code AND on stdout not
    # lying about delivery.
    exit_code = 0
    try:
        for event in app.stream(initial, {"recursion_limit": 120}):
            for node_name, node_output in event.items():
                if isinstance(node_output, dict):
                    node_output.pop("__next__", None)
                    accumulated.update(node_output)
    except KeyboardInterrupt:
        exit_code = 130
        print("\n  Interrupted.\n")
    except Exception as exc:  # noqa: BLE001
        exit_code = 1
        print(f"\n  Pipeline error: {exc}\n")
        import traceback

        traceback.print_exc()

    # Diagnostics always run (token accounting + a brief crash banner) so an
    # operator can see what failed; the full _final_report + handoff are
    # reserved for the non-crash path.
    _print_end_to_end_tokens(accumulated)
    _token_tracker.print_summary()

    if exit_code != 0:
        # No valid assembly artifact on a crash -- do not call _final_report
        # (it would print "Assembly: <path>" pointing at a nonexistent file)
        # and do not attempt handoff (it would either fail trying to load a
        # missing STEP or, worse, succeed against a stale prior-run artifact
        # and present a crashed run as a clean delivery).
        print(f"  Run aborted (exit code {exit_code}). No handoff packet.")
        return exit_code

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
            part_results=accumulated.get("part_results"),
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

    return 0


if __name__ == "__main__":
    sys.exit(main())
