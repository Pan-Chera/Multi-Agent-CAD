"""Assembly pipeline nodes (LangGraph).

Stages and their reuse profile:

* ``node_decomposer``     -- new prompt, reuses MAC's LLM client/JSON
                             retry + image_preprocess (multimodal input).
* ``node_part_builder``   -- thin orchestration around the UNCHANGED
                             single-part MAC pipeline (part_generator).
* ``node_assembler``      -- deterministic codegen (zero tokens) with an
                             LLM repair fallback (repair-loop.md pattern).
* ``node_assembly_qa``    -- deterministic closed-loop detection
                             (assembly_qa) + rendered views for the judge.
* ``node_assembly_judge`` -- MAC QA-Judge pattern: 5-layer anti-
                             hallucination defence, evidence gate, and
                             multimodal rendered views.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mac_assembly import config_assembly as cfg
from mac_assembly.assembly_codegen import (
    postprocess_axial_offsets,
    run_assembly_script,
    write_assembly_script,
)
from mac_assembly.assembly_qa import (
    reconcile_dimensions,
    render_assembly_views,
    run_assembly_qa,
)
from mac_assembly.llm_utils import (
    build_multimodal_content,
    call_llm_json,
    encode_png_data_url,
)
from mac_assembly.part_generator import (
    run_part,
    run_part_builder,
    run_part_builder_remodel,
    run_part_remodel,
    run_part_reuse,
    run_part_with_features,
)
from mac_assembly.schemas_assembly import (
    AssemblyBrief,
    AssemblyErrorType,
    AssemblyGraphState,
    AssemblyJudgeAction,
    AssemblyJudgeDecision,
    AssemblyQAReport,
    MatingPlan,
    PartResult,
)
from multi_agent_cad.image_preprocess import (
    _encode_jpeg_data_url,
    _load_user_images,
)
from multi_agent_cad.nodes import (
    _extract_code_from_llm_response,
    _is_multimodal_unsupported_error,
    _llm_client,
)

_PROMPTS = Path(__file__).resolve().parent / "prompts"


def _prompt(name: str) -> str:
    return (_PROMPTS / name).read_text(encoding="utf-8")


def _image_fingerprint(images_dir: Path) -> str:
    """Stable hash of image files (name + mtime_ns + size) so changing
    images invalidates the Decomposer cache even when the text prompt is
    unchanged. Empty string when the directory has no images."""
    import hashlib

    if not images_dir.is_dir():
        return ""
    h = hashlib.sha256()
    for img in sorted(images_dir.iterdir()):
        if img.is_file():
            st = img.stat()
            h.update(f"{img.name}:{st.st_mtime_ns}:{st.st_size}\n".encode())
    return h.hexdigest()[:16]


def _log(state, msg: str) -> list[str]:
    return list(state.get("execution_log", [])) + [msg]


# ---------------------------------------------------------------------------
# Agent 1: Decomposer (the WHAT -- parts + functional interfaces)
# ---------------------------------------------------------------------------


def node_decomposer(state: AssemblyGraphState) -> dict:
    """Assembly-level Spec Planner: request -> AssemblyBrief JSON.

    Structure-only output (parts + prose interfaces + envelope). Mate
    design is delegated to the Mating Architect.
    """
    user_request = state["user_request"]
    work_dir = Path(state["work_dir"])
    recompose_feedback = state.get("repair_context", "")

    images_dir = Path.cwd() / "user_input_images"
    image_fp = _image_fingerprint(images_dir)

    cache = work_dir / "assembly_cache" / "assembly_brief.json"
    if cache.is_file() and not recompose_feedback:
        try:
            raw_data = json.loads(cache.read_text(encoding="utf-8"))
            cached_fp = raw_data.pop("__image_fingerprint", "") if isinstance(raw_data, dict) else ""
            # Cache invalidation: changing images (add/remove/edit) MUST
            # invalidate the brief even when the text prompt is unchanged --
            # the Decomposer is multimodal and the previous brief may have
            # read geometry from a now-replaced image. Also invalidate when
            # the user first adds images to a previously text-only brief.
            if cached_fp == image_fp:
                brief = AssemblyBrief.model_validate(raw_data)
                return {
                    "assembly_brief": brief,
                    "execution_log": _log(state, "decomposer: cache hit"),
                    "node_history": state.get("node_history", []) + ["decomposer"],
                }
            print(f"[assembly] decomposer: cache miss (images changed)")
        except Exception:  # noqa: BLE001 - stale cache -> regenerate
            cache.unlink(missing_ok=True)

    user_prompt = f"## User Request\n\n{user_request}"
    if recompose_feedback:
        user_prompt += (
            "\n\n## Feedback on the previous decomposition (fix these "
            f"problems):\n{recompose_feedback}"
        )

    content: str | list
    images = _load_user_images(images_dir)
    if images and cfg.DECOMPOSER_MULTIMODAL != "never":
        content = build_multimodal_content(
            user_prompt,
            [_encode_jpeg_data_url(b) for b in images],
        )
    else:
        content = user_prompt

    try:
        raw = call_llm_json(
            _prompt("decomposer.md"),
            content,
            model=cfg.DECOMPOSER_MODEL,
            temperature=cfg.DECOMPOSER_TEMPERATURE,
            max_tokens=cfg.DECOMPOSER_MAX_TOKENS,
            extra_kwargs=cfg.DECOMPOSER_KWARGS,
        )
        # Determinism hygiene: sort parts/interfaces by id so downstream
        # caching + routing are stable across runs.
        raw.get("parts", []).sort(key=lambda p: p.get("part_id", ""))
        raw.get("interfaces", []).sort(key=lambda i: i.get("interface_id", ""))
        brief = AssemblyBrief.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        import traceback
        print(f"[assembly] decomposer FAILED: {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        return {
            "assembly_brief": None,
            "execution_log": _log(state, f"decomposer FAILED: {exc}"),
            "node_history": state.get("node_history", []) + ["decomposer"],
        }

    cache.parent.mkdir(parents=True, exist_ok=True)
    # Embed the image fingerprint in the cache file so a subsequent run
    # with changed images skips the stale cache and regenerates.
    cache_data = brief.model_dump(mode="json")
    cache_data["__image_fingerprint"] = image_fp
    cache.write_text(json.dumps(cache_data, indent=2), encoding="utf-8")

    return {
        "assembly_brief": brief,
        "decomposer_runs": state.get("decomposer_runs", 0) + 1,
        "repair_context": "",
        "execution_log": _log(
            state,
            f"decomposer: {len(brief.parts)} parts, "
            f"{len(brief.interfaces)} interfaces",
        ),
        "node_history": state.get("node_history", []) + ["decomposer"],
    }


# ---------------------------------------------------------------------------
# Agent 1b: Mating Architect (the HOW -- structured MateSpecs)
# ---------------------------------------------------------------------------


def _validate_mating_plan(plan: MatingPlan, brief: AssemblyBrief) -> list[str]:
    """Deterministic cross-checks between plan and brief.

    The assembly-layer analogue of MAC's ``_normalize_architect_plan``:
    mechanical validation at the stage boundary catches LLM drift before
    it reaches the codegen. Checks: reference existence, single fixed
    root, acyclicity (B6), interface coverage, anchor semantics that the
    deterministic translator can actually express (face_to_face +Z
    stacking requires top/bottom faces).
    """
    from mac_assembly.assembly_codegen import _get_anchor_axis, _toposort_mates
    from mac_assembly.schemas_assembly import AnchorKind, MateType

    errors: list[str] = []
    part_ids = {p.part_id for p in brief.parts}

    if plan.assembly_name != brief.assembly_name:
        errors.append(
            f"assembly_name mismatch: plan {plan.assembly_name!r} vs brief "
            f"{brief.assembly_name!r}"
        )
    for m in plan.mates:
        for role in ("fixed_part_id", "moving_part_id"):
            if getattr(m, role) not in part_ids:
                errors.append(f"mate {m.mate_id!r} references unknown part")
        if m.fixed_part_id == m.moving_part_id:
            errors.append(f"mate {m.mate_id!r} mates a part to itself")
        # SPHERE anchors are only valid for ball mates. A SPHERE anchor on
        # a non-ball mate either crashes codegen (revolute/coaxial need an
        # axis direction; _emit_anchor returns None for SPHERE -> Axis(pt,
        # None) runtime error) or is semantically wrong (face_to_face /
        # rigid with sphere_center as datum). The "requires principal axis"
        # check below catches revolute/coaxial/linear/cylindrical with a
        # different message; this catches rigid + face_to_face too, and
        # gives a clearer error for the others.
        if m.mate_type != MateType.BALL:
            for role, anchor in (("fixed", m.fixed_anchor), ("moving", m.moving_anchor)):
                if anchor.kind == AnchorKind.SPHERE:
                    errors.append(
                        f"mate {m.mate_id!r}: SPHERE anchor on {role} side "
                        f"is only valid for ball mates; {m.mate_type.value} "
                        f"needs a face / axis_point / selector anchor (or "
                        f"change the mate type to ball if the joint is a "
                        f"2-DOF spherical pair)"
                    )
        if m.mate_type in (MateType.LINEAR, MateType.CYLINDRICAL):
            if not m.slide_axis:
                errors.append(
                    f"mate {m.mate_id!r}: {m.mate_type.value} requires 'slide_axis' "
                    "(the slide/rotation direction x/y/z)"
                )
        if m.mate_type == MateType.FACE_TO_FACE:
            if m.fixed_anchor.kind == AnchorKind.FACE and m.fixed_anchor.face != "top":
                errors.append(
                    f"mate {m.mate_id!r}: face_to_face offset applies along "
                    f"world +Z -- fixed anchor must be the 'top' face, got "
                    f"{m.fixed_anchor.face!r}"
                )
            if m.moving_anchor.kind == AnchorKind.FACE and m.moving_anchor.face != "bottom":
                errors.append(
                    f"mate {m.mate_id!r}: face_to_face seats the moving part "
                    f"above (+Z) -- moving anchor must be the 'bottom' face, "
                    f"got {m.moving_anchor.face!r}"
                )
        # Revolute/coaxial/linear/cylindrical mates need both anchors to
        # define a principal axis (cylinder selector, small-offset
        # axis_point, or face normal) and the axes must agree. Without
        # this check, AssemblyHelper.revolute_frame()/coaxial()/linear_frame()
        # raises at codegen time -- catch the LLM drift at the stage
        # boundary instead.
        if m.mate_type in (
            MateType.REVOLUTE, MateType.COAXIAL,
            MateType.LINEAR, MateType.CYLINDRICAL,
        ):
            fa_axis = _get_anchor_axis(m.fixed_anchor)
            ma_axis = _get_anchor_axis(m.moving_anchor)
            if fa_axis is None or ma_axis is None:
                errors.append(
                    f"mate {m.mate_id!r}: {m.mate_type.value} requires both "
                    f"anchors to define a principal axis (cylinder selector, "
                    f"small-offset axis_point, or face); got fixed="
                    f"{fa_axis!r}, moving={ma_axis!r}"
                )
            elif fa_axis != ma_axis:
                errors.append(
                    f"mate {m.mate_id!r}: {m.mate_type.value} anchor axes "
                    f"mismatch (fixed={fa_axis!r}, moving={ma_axis!r}) -- "
                    f"both anchors must share the same principal axis"
                )
            elif (
                m.mate_type in (MateType.LINEAR, MateType.CYLINDRICAL)
                and m.slide_axis and fa_axis != m.slide_axis
            ):
                errors.append(
                    f"mate {m.mate_id!r}: {m.mate_type.value} slide_axis "
                    f"{m.slide_axis!r} != anchor axis {fa_axis!r}"
                )
        # Ball mates: both anchors must be SPHERE kind (sphere center
        # + radius). The ball's sphere_radius should be <= the socket
        # cavity's sphere_radius for the ball to fit; flag geometry
        # violations where the ball is larger than the cavity.
        if m.mate_type == MateType.BALL:
            if m.fixed_anchor.kind != AnchorKind.SPHERE:
                errors.append(
                    f"mate {m.mate_id!r}: ball requires fixed anchor "
                    f"kind=sphere, got kind={m.fixed_anchor.kind.value}"
                )
            if m.moving_anchor.kind != AnchorKind.SPHERE:
                errors.append(
                    f"mate {m.mate_id!r}: ball requires moving anchor "
                    f"kind=sphere, got kind={m.moving_anchor.kind.value}"
                )
            if (m.fixed_anchor.kind == AnchorKind.SPHERE
                    and m.moving_anchor.kind == AnchorKind.SPHERE
                    and m.fixed_anchor.sphere_radius_mm is not None
                    and m.moving_anchor.sphere_radius_mm is not None
                    and m.moving_anchor.sphere_radius_mm
                        > m.fixed_anchor.sphere_radius_mm):
                errors.append(
                    f"mate {m.mate_id!r}: ball radius "
                    f"{m.moving_anchor.sphere_radius_mm}mm > socket cavity "
                    f"radius {m.fixed_anchor.sphere_radius_mm}mm -- ball "
                    f"cannot fit inside the socket (use a smaller ball "
                    f"radius or a larger socket cavity)"
                )

    # v3 SELECTOR disambiguation: when a mate references a v3 part
    # (base_body set) with 2+ cylinder-producing features (clevis_fork /
    # clevis_tongue / through_bore / knuckle_ear), the SELECTOR anchor on
    # that part MUST specify the 2 target fields that lie in the plane
    # PERPENDICULAR to the cylinder's pin axis -- otherwise the resolver
    # picks the first matching cylinder for every mate, stacking all
    # moving parts at one feature root. See plan §4d.
    #
    # The 2 required target fields depend on the cylinder axis (pin axis)
    # reported in the SELECTOR query:
    #   axis="x" (pin=X) → bores along X, disambiguate in YZ plane →
    #                      need target_y_mm + target_z_mm
    #   axis="y" (pin=Y) → bores along Y, disambiguate in XZ plane →
    #                      need target_x_mm + target_z_mm
    #   axis="z" (pin=Z) → bores along Z, disambiguate in XY plane →
    #                      need target_x_mm + target_y_mm (legacy default)
    # The previous version hardcoded target_x + target_y, which is wrong
    # for pin=X (where target_x is along the bore axis — irrelevant for
    # disambiguation) and for pin=Y (where target_y is along the bore).
    _PIN_AXIS_REQUIRED_TARGETS: dict[str, tuple[str, str]] = {
        "x": ("target_y_mm", "target_z_mm"),
        "y": ("target_x_mm", "target_z_mm"),
        "z": ("target_x_mm", "target_y_mm"),
    }
    v3_parts = {p.part_id: p for p in brief.parts if p.base_body is not None}
    _CYL_FEATURES = ("clevis_fork", "clevis_tongue", "through_bore", "knuckle_ear")
    for m in plan.mates:
        for role, anchor, other_pid in (
            ("fixed", m.fixed_anchor, m.fixed_part_id),
            ("moving", m.moving_anchor, m.moving_part_id),
        ):
            if other_pid not in v3_parts:
                continue
            if anchor.kind != AnchorKind.SELECTOR:
                continue
            v3_spec = v3_parts[other_pid]
            cyl_features = [f for f in v3_spec.features if f.name in _CYL_FEATURES]
            if len(cyl_features) < 2:
                continue  # single cyl feature -- no disambiguation needed
            q = anchor.selector_query
            if q is None:
                continue  # schema validator already caught this
            cyl_axis = str(getattr(q, "axis", "z") or "z").lower()
            required_targets = _PIN_AXIS_REQUIRED_TARGETS.get(
                cyl_axis, ("target_x_mm", "target_y_mm")
            )
            missing = [
                f for f in required_targets if getattr(q, f) is None
            ]
            if missing:
                errors.append(
                    f"mate {m.mate_id!r}: SELECTOR anchor on v3 part "
                    f"{other_pid!r} ({role}) lacks {', '.join(missing)}, "
                    f"but the part has {len(cyl_features)} cylinder-producing "
                    f"features with axis={cyl_axis!r}. For pin axis "
                    f"{cyl_axis.upper()}, disambiguation is in the "
                    f"{'YZ' if cyl_axis=='x' else 'XZ' if cyl_axis=='y' else 'XY'} "
                    f"plane -- set the 2 perpendicular target fields to "
                    f"the corresponding feature's attach_point_mm coords. "
                    f"Without these, the resolver picks the first matching "
                    f"cylinder for every mate, stacking all moving parts "
                    f"at one feature root."
                )

    moved = [m.moving_part_id for m in plan.mates]
    unmoved = part_ids - set(moved)
    if plan.mates:
        if not unmoved:
            errors.append("every part is moved by some mate -- need a fixed root")
        elif len(unmoved) > 1:
            errors.append(
                f"multiple unmoved roots {sorted(unmoved)} -- the mate graph "
                "must be a single tree rooted at one fixed part"
            )
        else:
            # B6: acyclicity -- a mate that never becomes ready is on a cycle.
            ordered = _toposort_mates(plan.mates, part_ids)
            if len(ordered) < len(plan.mates):
                stuck = [m.mate_id for m in plan.mates if m not in ordered]
                errors.append(f"mate graph contains a cycle involving: {stuck}")

    covered = {(m.fixed_part_id, m.moving_part_id) for m in plan.mates}
    for itf in brief.interfaces:
        if (itf.part_a, itf.part_b) not in covered:
            errors.append(
                f"interface {itf.interface_id!r} ({itf.part_a} -> {itf.part_b}) "
                "has no covering mate"
            )
    return errors


def node_mating_architect(state: AssemblyGraphState) -> dict:
    """Brief (+ optional QA feedback) -> MatingPlan JSON."""
    import hashlib

    brief: AssemblyBrief | None = state.get("assembly_brief")
    if brief is None:
        return {
            "mating_plan": None,
            "execution_log": _log(state, "mating_architect: no brief"),
            "node_history": state.get("node_history", []) + ["mating_architect"],
        }

    work_dir = Path(state["work_dir"])
    remate_feedback = state.get("repair_context", "")

    # Brief fingerprint: invalidate the mating plan cache when the brief
    # changes (recompose, image-driven re-decomposition, or brief edits).
    # The previous cache only validated the plan against the new brief --
    # a plan that happened to structurally validate would survive even
    # when its anchor dims no longer matched the regenerated parts.
    brief_fp = hashlib.sha256(
        brief.model_dump_json().encode("utf-8")
    ).hexdigest()[:16]

    cache = work_dir / "assembly_cache" / "mating_plan.json"
    if cache.is_file() and not remate_feedback:
        try:
            raw_data = json.loads(cache.read_text(encoding="utf-8"))
            cached_fp = raw_data.pop("__brief_fingerprint", "") if isinstance(raw_data, dict) else ""
            if cached_fp == brief_fp:
                plan = MatingPlan.model_validate(raw_data)
                errs = _validate_mating_plan(plan, brief)
                if not errs:
                    return {
                        "mating_plan": plan,
                        "execution_log": _log(state, "mating_architect: cache hit"),
                        "node_history": state.get("node_history", []) + ["mating_architect"],
                    }
            else:
                print(f"[assembly] mating_architect: cache miss (brief changed)")
        except Exception:  # noqa: BLE001
            cache.unlink(missing_ok=True)

    user_prompt = (
        "## AssemblyBrief\n\n```json\n"
        + brief.model_dump_json(indent=2)
        + "\n```"
    )
    if remate_feedback:
        user_prompt += (
            "\n\n## QA feedback on the previous mating plan (fix these; "
            "measured deltas are real geometry, trust them):\n"
            f"{remate_feedback}"
        )

    plan: MatingPlan | None = None
    validation_errors: list[str] = []
    for attempt in range(2):  # one structured-retry on validation failure
        try:
            raw = call_llm_json(
                _prompt("mating_architect.md"),
                user_prompt + ("\n\n## Previous plan errors\n" + "\n".join(validation_errors) if validation_errors else ""),
                model=cfg.MATING_MODEL,
                temperature=cfg.MATING_TEMPERATURE,
                max_tokens=cfg.MATING_MAX_TOKENS,
                extra_kwargs=cfg.MATING_KWARGS,
            )
            raw.get("mates", []).sort(key=lambda m: m.get("mate_id", ""))
            candidate = MatingPlan.model_validate(raw)
            validation_errors = _validate_mating_plan(candidate, brief)
            if not validation_errors:
                plan = candidate
                break
        except Exception as exc:  # noqa: BLE001
            validation_errors = [f"parse/validation error: {exc}"]
            print(f"[assembly] mating_architect attempt {attempt+1}: parse/validation error: {exc}")

    if plan is None:
        print(f"[assembly] mating_architect FAILED — validation errors:")
        for e in validation_errors[:8]:
            print(f"  - {e}")
        return {
            "mating_plan": None,
            "execution_log": _log(
                state,
                "mating_architect FAILED: " + " | ".join(validation_errors[:4]),
            ),
            "node_history": state.get("node_history", []) + ["mating_architect"],
        }

    # Deterministic axial_offset_mm override for the pivot-post + link-bar
    # and link-bar + link-bar revolute patterns. The LLM consistently
    # writes wrong axial_offset_mm values (correctly reasons -3 in notes
    # then writes -11 in JSON); this computes the correct value from the
    # brief's structured key_dimensions values along the mate's principal
    # axis (x/y/z -- generalized from Z-only), chain-tracking the
    # per-axis world_offset through the mate graph so multi-joint chains
    # (finger segments stacked on a palm post, or horizontal clevis
    # chains along X/Y) are placed correctly.
    plan = postprocess_axial_offsets(plan, brief)

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache_data = plan.model_dump(mode="json")
    cache_data["__brief_fingerprint"] = brief_fp
    cache.write_text(json.dumps(cache_data, indent=2), encoding="utf-8")

    return {
        "mating_plan": plan,
        "mating_runs": state.get("mating_runs", 0) + 1,
        "repair_context": "",
        "execution_log": _log(
            state,
            f"mating_architect: {len(plan.mates)} mates "
            f"(run {state.get('mating_runs', 0) + 1})",
        ),
        "node_history": state.get("node_history", []) + ["mating_architect"],
    }


# ---------------------------------------------------------------------------
# Agent 2/3: per-part single-part MAC runs
# ---------------------------------------------------------------------------


def node_part_builder(state: AssemblyGraphState) -> dict:
    """Run the unchanged single-part MAC pipeline for every missing/failed part."""
    brief: AssemblyBrief = state["assembly_brief"]
    work_dir = Path(state["work_dir"])
    parts_root = work_dir / "parts"

    existing: dict[str, PartResult] = dict(state.get("part_results") or {})
    remodel_ids = set(state.get("remodel_part_ids") or [])
    feedback = state.get("repair_context", "")

    # Reuse maps (computed once per node call).
    # template_of[instance_id] = template_id (each instance has exactly
    #   one template -- the AssemblyBrief validator rejected chains).
    # instances_of[template_id] = [instance_id, ...] (reverse map).
    template_of = {
        p.part_id: p.reuses_part_id
        for p in brief.parts if p.reuses_part_id is not None
    }
    instances_of: dict[str, list[str]] = {}
    for inst_id, tmpl_id in template_of.items():
        instances_of.setdefault(tmpl_id, []).append(inst_id)

    # Symmetrically expand remodel_ids: mentioning a template remodels
    # its instances too (so they pick up the new geometry); mentioning
    # an instance remodels its template (geometry is shared). Without
    # this, a remodeled template's old STEP copy in an instance's
    # directory would silently drift.
    expanded_remodel = set(remodel_ids)
    for rid in list(remodel_ids):
        if rid in template_of:
            expanded_remodel.add(template_of[rid])
        if rid in instances_of:
            expanded_remodel.update(instances_of[rid])
    remodel_ids = expanded_remodel

    # Stable two-bucket sort: templates first, then instances. Within
    # each bucket the Decomposer's order is preserved. This guarantees
    # the template's STEP exists before any instance tries to copy it.
    ordered_parts = sorted(
        brief.parts,
        key=lambda p: (
            1 if p.reuses_part_id is not None else 0,
            brief.parts.index(p),
        ),
    )

    results = dict(existing)
    for spec in ordered_parts:
        prev = existing.get(spec.part_id)
        if prev is not None and prev.ok and spec.part_id not in remodel_ids:
            continue
        part_feedback = ""
        if spec.part_id in remodel_ids and feedback:
            part_feedback = feedback

        # Reuse path: skip MAC pipeline, copy template's STEP/STL/py.
        # Prefer the result from THIS iteration (templates-first sort
        # means the template was just processed one or more iterations
        # ago in the same loop) so a freshly generated or remodeled
        # template's new STEP is what gets copied; fall back to the
        # pre-node state only if the template wasn't touched this round.
        # Without this lookup order, `existing.get` returns the STALE
        # pre-iteration result and the instance copies the template's
        # old (pre-remodel) STEP.
        if spec.reuses_part_id is not None:
            tmpl_res = (
                results.get(spec.reuses_part_id)
                or existing.get(spec.reuses_part_id)
            )
            result = run_part_reuse(spec, parts_root, template_result=tmpl_res)
            results[spec.part_id] = result
            status = "OK (reuse)" if result.ok else f"FAILED ({result.error})"
            print(f"[assembly] part {spec.part_id}: {status}")
            continue

        # v3 path: LLM base body + feature operators. Does NOT fall through
        # to full regen on failure -- doing so would re-introduce the LLM-
        # writes-kinematic-features failure mode v3 was designed to avoid.
        # run_part_with_features does its own Aider retry internally; if it
        # still fails, the result is stored with ok=False and the
        # FeedbackRouter / Judge routes (REMODEL_PARTS / RECOMPOSE / HALT).
        # The feature-only-remodel fast path (skip MAC Coder when
        # base_body.description is byte-identical to cached version) is
        # detected inside run_part_with_features via temp_v3_spec.json.
        if spec.base_body is not None:
            result = run_part_with_features(spec, parts_root, part_feedback)
            results[spec.part_id] = result
            if result.ok:
                status = "OK (v3 base+features)"
                if result.error:
                    # continue-on-failure: base body failed but STEP exists,
                    # assembly will use the failed geometry as-is.
                    status += f" [WARN: {result.error}]"
            else:
                status = f"FAILED ({result.error})"
            print(f"[assembly] part {spec.part_id}: {status}")
            continue  # success or failure, the result is stored -- no fall-through

        # Builder path: if the Decomposer specified a builder for this
        # part, call it directly (zero tokens, deterministic geometry).
        # Bypasses the LLM-driven single-part pipeline entirely. Builders
        # are for structures the LLM struggles with (horizontal-axis
        # cylinders, knuckle ears with horizontal bores).
        # Remodel: ask the LLM to adjust builder params from the QA
        # feedback, then re-call the builder (one small LLM call, no
        # Aider on the non-executable audit file). Falls through to full
        # regeneration if param adjustment fails.
        if spec.builder:
            if not part_feedback:
                result = run_part_builder(spec, parts_root)
                results[spec.part_id] = result
                status = "OK (builder)" if result.ok else f"FAILED ({result.error})"
                print(f"[assembly] part {spec.part_id}: {status}")
                continue
            patched = run_part_builder_remodel(spec, parts_root, part_feedback)
            if patched is not None:
                # run_part_builder_remodel returns None on any failure
                # (LLM call, param validation, build crash) -- a non-None
                # result is success.
                results[spec.part_id] = patched
                print(f"[assembly] part {spec.part_id}: OK (builder remodel)")
                continue
            # Builder remodel failed: store a failed result and let the
            # Judge / FeedbackRouter route (REMODEL_PARTS / RECOMPOSE /
            # HALT). Does NOT fall through to run_part (LLM full regen)
            # -- the regen geometry's topology may not match the builder-
            # derived SELECTOR anchors the Mating Architect specified,
            # and it re-introduces the LLM-writes-kinematic-features
            # failure mode builders avoid. Symmetric with the v3
            # base_body path (no fall-through, see L597-602).
            results[spec.part_id] = PartResult(
                part_id=spec.part_id,
                ok=False,
                attempts=1,
                error="builder remodel unavailable/failed (LLM param adjust or build)",
            )
            print(f"[assembly] part {spec.part_id}: FAILED (builder remodel)")
            continue

        # Remodel path: patch the existing design with Aider (preserves
        # verified features, fewer tokens) before falling back to a full
        # from-scratch regeneration. Skipped for builder parts (handled
        # above) because their audit py file is non-executable.
        if spec.part_id in remodel_ids and part_feedback and not spec.builder:
            patched = run_part_remodel(spec, parts_root, part_feedback)
            if patched is not None and patched.ok:
                results[spec.part_id] = patched
                print(f"[assembly] part {spec.part_id}: OK (aider remodel)")
                continue
            print(f"[assembly] part {spec.part_id}: aider remodel unavailable/"
                  f"failed -> full regeneration")

        result: PartResult | None = None
        for attempt in range(cfg.PART_MAX_ATTEMPTS):
            result = run_part(
                spec,
                parts_root,
                feedback=part_feedback if attempt == 0 else "",
                force_refresh=attempt > 0 or bool(part_feedback),
            )
            result.attempts = attempt + 1
            if result.ok:
                break
        results[spec.part_id] = result
        status = "OK" if result.ok else f"FAILED ({result.error})"
        print(f"[assembly] part {spec.part_id}: {status}")

    return {
        "part_results": results,
        "remodel_part_ids": [],
        "execution_log": _log(
            state,
            "part_builder: "
            + ", ".join(f"{k}={'ok' if v.ok else 'fail'}" for k, v in results.items()),
        ),
        "node_history": state.get("node_history", []) + ["part_builder"],
    }


# ---------------------------------------------------------------------------
# Agent 4a: Assembler (deterministic codegen + LLM repair fallback)
# ---------------------------------------------------------------------------


def _llm_repair_assembly(
    script_src: str,
    brief: AssemblyBrief,
    qa: AssemblyQAReport | None,
    exec_error: str = "",
) -> str | None:
    """LLM fallback that edits the generated assembly script.

    B4 fix: the script execution traceback (when present) is the primary
    evidence -- without it the repair agent was guessing blind.
    """
    sections = ["## AssemblyBrief\n\n```json\n" + brief.model_dump_json(indent=2) + "\n```"]
    if exec_error:
        sections.append(
            "## Script execution failure (the script produced no STEP -- "
            "fix this first)\n\n```\n" + exec_error[-2000:] + "\n```"
        )
    if qa is not None and qa.error_details:
        sections.append(
            "## QA failures\n\n"
            + "\n".join(f"- {d}" for d in qa.error_details)
        )
    user_prompt = (
        "## Current temp_assembly.py\n\n```python\n"
        + script_src
        + "\n```\n\n" + "\n\n".join(sections)
    )
    client = _llm_client()
    messages = [
        {"role": "system", "content": _prompt("assembly_repair.md")},
        {"role": "user", "content": user_prompt},
    ]
    try:
        resp = client.chat.completions.create(
            model=cfg.ASSEMBLY_REPAIR_MODEL,
            messages=messages,
            temperature=cfg.ASSEMBLY_REPAIR_TEMPERATURE,
            max_tokens=cfg.ASSEMBLY_REPAIR_MAX_TOKENS,
            timeout=180,
            **cfg.ASSEMBLY_REPAIR_KWARGS,
        )
        raw = resp.choices[0].message.content or ""
        fixed = _extract_code_from_llm_response(raw)
        if "AssemblyHelper" in fixed and "asm.build" in fixed:
            return fixed
        return None
    except Exception:  # noqa: BLE001 - repair is best-effort
        return None


def node_assembler(state: AssemblyGraphState) -> dict:
    brief: AssemblyBrief = state["assembly_brief"]
    plan: MatingPlan | None = state.get("mating_plan")
    if plan is None:
        report = AssemblyQAReport(
            error_details=["mating plan missing -- cannot assemble"],
            all_passed=False,
            error_type=AssemblyErrorType.FATAL,
        )
        return {
            "qa_report": report,
            "assembly_step_path": "",
            "qa_skipped_iter": True,  # no STEP produced, no QA detector run
            "execution_log": _log(state, "assembler: no mating plan"),
            "node_history": state.get("node_history", []) + ["assembler"],
        }
    work_dir = Path(state["work_dir"])
    iteration = state.get("iteration_count", 0)
    qa: AssemblyQAReport | None = state.get("qa_report")
    part_results: dict = state.get("part_results") or {}

    # Missing parts short-circuit into a synthetic part_missing QA report.
    missing = [
        p.part_id for p in brief.parts
        if not (part_results.get(p.part_id) and part_results[p.part_id].ok)
    ]
    if missing:
        report = AssemblyQAReport(
            part_count_expected=brief.expected_part_count,
            part_count_measured=len(brief.parts) - len(missing),
            part_count_passed=False,
            missing_parts=missing,
            error_details=[f"part generation failed: {missing}"],
            all_passed=False,
            error_type=AssemblyErrorType.PART_MISSING,
            needs_remodel_part_ids=missing,
        )
        return {
            "qa_report": report,
            "assembly_step_path": "",
            "qa_skipped_iter": True,  # no STEP produced, no QA detector run
            "execution_log": _log(state, f"assembler: missing parts {missing}"),
            "node_history": state.get("node_history", []) + ["assembler"],
        }

    # Zero-token dimension reconciliation: verify the mating plan against
    # MEASURED part bboxes before spending an assembly+QA cycle on it.
    reconcile_errors = reconcile_dimensions(brief, plan.mates, part_results)
    if reconcile_errors:
        report = AssemblyQAReport(
            error_details=reconcile_errors,
            all_passed=False,
            error_type=AssemblyErrorType.RECONCILE,
            needs_mate_fix_ids=[m.mate_id for m in plan.mates],
        )
        return {
            "qa_report": report,
            "assembly_step_path": "",
            "qa_skipped_iter": True,  # no STEP produced, no QA detector run
            "execution_log": _log(state, "assembler: " + reconcile_errors[0][:120]),
            "node_history": state.get("node_history", []) + ["assembler"],
        }

    # 1) Deterministic codegen (zero tokens) -- always regenerate the base.
    script_path = write_assembly_script(brief, plan.mates, work_dir, _REPO_ROOT, iteration)

    ok, tail = run_assembly_script(
        script_path, work_dir,
        timeout=cfg.ASSEMBLY_SCRIPT_TIMEOUT,
        python_bin=cfg.PYTHON_BIN or sys.executable,
    )

    # 2) LLM repair fallback on ANY execution failure (B4: previously
    #    first-round failures with qa=None skipped repair entirely).
    if not ok:
        repaired = _llm_repair_assembly(
            script_path.read_text(encoding="utf-8"), brief, qa, exec_error=tail
        )
        if repaired:
            script_path = work_dir / f"temp_assembly_{iteration}_repaired.py"
            script_path.write_text(repaired, encoding="utf-8")
            ok, tail = run_assembly_script(
                script_path, work_dir,
                timeout=cfg.ASSEMBLY_SCRIPT_TIMEOUT,
                python_bin=cfg.PYTHON_BIN or sys.executable,
            )

    return {
        "assembly_py_path": str(script_path),
        "assembly_step_path": str(work_dir / "assembly_output.step"),
        "assembly_stl_path": str(work_dir / "assembly_output.stl"),
        # Clear the qa_report on the normal path so the downstream QA node
        # can distinguish "assembler produced a (possibly failed) script"
        # (no existing report -> synthesize fresh FATAL) from "assembler
        # early-returned with RECONCILE/PART_MISSING this iteration"
        # (existing report -> preserve, the assembler already classified
        # the failure with specific routing hints).
        "qa_report": None,
        # Normal path: the QA detector will run on the produced STEP, so
        # iteration_count +1 is appropriate. Clear the skip flag in case
        # the previous iteration early-returned and set it.
        "qa_skipped_iter": False,
        "execution_log": _log(
            state,
            f"assembler iter={iteration}: "
            + ("built " + str(script_path.name) if ok else f"FAILED: {tail}"),
        ),
        "node_history": state.get("node_history", []) + ["assembler"],
    }


# ---------------------------------------------------------------------------
# Agent 4b: AssemblyQA
# ---------------------------------------------------------------------------


def node_assembly_qa(state: AssemblyGraphState) -> dict:
    brief: AssemblyBrief = state["assembly_brief"]
    plan: MatingPlan | None = state.get("mating_plan")
    work_dir = Path(state["work_dir"])

    if not Path(state.get("assembly_step_path") or "").is_file():
        # The assembler node may have already classified this failure via
        # an early-return path (RECONCILE for mate-dim mismatch,
        # PART_MISSING for missing parts, FATAL for missing mating plan).
        # That report carries specific routing hints (needs_mate_fix_ids /
        # needs_remodel_part_ids) that the Judge + Router use to dispatch
        # to remate vs. remodel vs. repair_assembly. Without this
        # preservation, the generic "STEP not produced" FATAL we'd
        # synthesize here misroutes every reconcile failure to
        # repair_assembly (the script isn't even the problem -- the mates
        # are), and the loop never recovers.
        #
        # The assembler clears qa_report=None on its normal path (script
        # was written, may or may not have produced a STEP), so a non-None
        # report here means the assembler early-returned this iteration.
        existing = state.get("qa_report")
        if (existing is not None
                and not existing.all_passed
                and existing.error_type in (
                    AssemblyErrorType.RECONCILE,
                    AssemblyErrorType.PART_MISSING,
                    AssemblyErrorType.FATAL,
                )):
            report = existing
        else:
            # Distinguish "a part failed -> no STEP" (route back to
            # part_builder) from "all parts OK but assembler produced no
            # STEP" (assembler script bug -> FATAL -> repair_assembly).
            part_results = state.get("part_results") or {}
            failed_parts = [
                pid for pid, r in part_results.items()
                if not getattr(r, "ok", False)
            ]
            if failed_parts:
                report = AssemblyQAReport(
                    error_details=[
                        "assembly STEP was not produced because part generation "
                        f"failed: {', '.join(failed_parts)}"
                    ],
                    all_passed=False,
                    error_type=AssemblyErrorType.PART_MISSING,
                    missing_parts=failed_parts,
                    needs_remodel_part_ids=failed_parts,
                    part_count_expected=brief.expected_part_count or len(brief.parts),
                    part_count_measured=sum(
                        1 for r in part_results.values()
                        if getattr(r, "ok", False)
                    ),
                    part_count_passed=False,
                )
            else:
                report = AssemblyQAReport(
                    error_details=["assembly STEP was not produced"],
                    all_passed=False,
                    error_type=AssemblyErrorType.FATAL,
                )
    else:
        report = run_assembly_qa(
            brief,
            plan.mates if plan else [],
            state.get("part_results") or {},
            work_dir,
        )

    status = "PASS" if report.all_passed else f"FAIL ({report.error_type.value})"
    print(f"[assembly] QA: {status}")
    for d in report.error_details[:8]:
        print(f"    - {d}")

    # Consume iteration_count +1 ONLY when the real QA detector ran on a
    # produced STEP. When the assembler early-returned (no mating plan /
    # missing parts / reconcile errors), it set qa_skipped_iter=True; the
    # QA node just propagates the existing report without mesh / envelope
    # / kinematic checks, so it should NOT burn the outer-loop budget. The
    # per-route sub-budgets (MATINGS_MAX_RUNS / DECOMPOSER_MAX_RUNS) still
    # bind for their respective routes; this only stops the outer
    # ASSEMBLY_MAX_ITERATIONS from being consumed by reconcile-failure
    # spam (D8: 4 consecutive reconcile failures previously halved the
    # outer budget without ever running a single QA detector).
    if state.get("qa_skipped_iter"):
        next_iter = state.get("iteration_count", 0)
        iter_note = " (qa_skipped_iter: no +1)"
    else:
        next_iter = state.get("iteration_count", 0) + 1
        iter_note = ""

    return {
        "qa_report": report,
        "iteration_count": next_iter,
        # Clear the skip flag after consuming it so the next iteration's
        # normal path (STEP produced) increments iteration_count cleanly.
        "qa_skipped_iter": False,
        "execution_log": _log(state, f"assembly_qa: {status}{iter_note}"),
        "node_history": state.get("node_history", []) + ["assembly_qa"],
    }


# ---------------------------------------------------------------------------
# Agent 5: Assembly Judge (MAC QA-Judge pattern)
# ---------------------------------------------------------------------------


def _judge_gate(decision: AssemblyJudgeDecision, qa: AssemblyQAReport) -> AssemblyJudgeDecision:
    """Code-level anti-hallucination gate (layer 3 of 5).

    * accept/halt with empty evidence -> repair_assembly
    * accept on part_missing/interference/fatal -> needs high confidence
    """
    if decision.action in (AssemblyJudgeAction.ACCEPT, AssemblyJudgeAction.HALT):
        if not [e for e in decision.evidence if e.strip()]:
            return decision.model_copy(update={
                "action": AssemblyJudgeAction.REPAIR_ASSEMBLY,
                "reason": "DOWNGRADED (empty evidence): " + decision.reason,
            })
        needs_high = qa.error_type in (
            AssemblyErrorType.PART_MISSING,
            AssemblyErrorType.INTERFERENCE,
            AssemblyErrorType.FATAL,
        )
        if decision.action == AssemblyJudgeAction.ACCEPT and needs_high and decision.confidence != "high":
            return decision.model_copy(update={
                "action": AssemblyJudgeAction.REPAIR_ASSEMBLY,
                "reason": "DOWNGRADED (accept on hard error needs high confidence): "
                          + decision.reason,
            })
    return decision


def node_assembly_judge(state: AssemblyGraphState) -> dict:
    qa: AssemblyQAReport | None = state.get("qa_report")
    # Skip Judge only when (a) disabled, (b) no QA report, (c) QA passed, or
    # (d) iteration_count below MIN_RETRY AND the failure is not a borderline
    # false-positive. The is_likely_false_positive override lets the Judge
    # run on the first iteration when the only failures are envelope overshoot
    # within 2x tolerance or interference volume under 2x tolerance -- so an
    # ACCEPT can terminate the loop instead of forcing a route back to
    # part_builder / assembler for what is really a too-strict envelope.
    if (
        not cfg.ASSEMBLY_JUDGE_ENABLED
        or qa is None
        or qa.all_passed
        or (
            state.get("iteration_count", 0) < cfg.ASSEMBLY_JUDGE_MIN_RETRY
            and not getattr(qa, "is_likely_false_positive", False)
        )
    ):
        return {
            "judge_decision": None,
            "execution_log": _log(state, "judge: skipped"),
            "node_history": state.get("node_history", []) + ["judge"],
        }

    brief: AssemblyBrief = state["assembly_brief"]
    work_dir = Path(state["work_dir"])

    user_prompt = (
        f"## User Request\n\n{brief.user_request_raw}\n\n"
        f"## AssemblyBrief (special_features = design intent)\n\n"
        + json.dumps(brief.special_features, indent=2)
        + "\n\n## QA Report\n\n```json\n"
        + qa.model_dump_json(indent=2)
        + "\n```\n\n"
        f"retry_count: {state.get('iteration_count', 0)}"
    )

    content: str | list = user_prompt
    views = render_assembly_views(work_dir)
    image_urls: list[str] = []
    # B7 fix: the judge prompt promises user_image[N] -- actually feed them
    # (same loader as the Decomposer, deterministic alphabetical order).
    user_images = _load_user_images(Path.cwd() / "user_input_images")
    image_urls += [_encode_jpeg_data_url(b) for b in user_images]
    image_urls += [encode_png_data_url(v) for v in views]
    if image_urls and cfg.ASSEMBLY_JUDGE_MULTIMODAL != "never":
        content = build_multimodal_content(user_prompt, image_urls)

    decision: AssemblyJudgeDecision | None = None
    multimodal_mode = cfg.ASSEMBLY_JUDGE_MULTIMODAL
    try:
        raw = call_llm_json(
            _prompt("assembly_judge.md"),
            content,
            model=cfg.ASSEMBLY_JUDGE_MODEL,
            temperature=cfg.ASSEMBLY_JUDGE_TEMPERATURE,
            max_tokens=cfg.ASSEMBLY_JUDGE_MAX_TOKENS,
            extra_kwargs=cfg.ASSEMBLY_JUDGE_KWARGS,
        )
        decision = _judge_gate(AssemblyJudgeDecision.model_validate(raw), qa)
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
        if (
            image_urls
            and isinstance(content, list)
            and multimodal_mode == "auto"
            and _is_multimodal_unsupported_error(err)
        ):
            # Auto-fallback: retry once without images (MAC Judge pattern).
            try:
                raw = call_llm_json(
                    _prompt("assembly_judge.md"),
                    user_prompt,
                    model=cfg.ASSEMBLY_JUDGE_MODEL,
                    temperature=cfg.ASSEMBLY_JUDGE_TEMPERATURE,
                    max_tokens=cfg.ASSEMBLY_JUDGE_MAX_TOKENS,
                    extra_kwargs=cfg.ASSEMBLY_JUDGE_KWARGS,
                )
                decision = _judge_gate(
                    AssemblyJudgeDecision.model_validate(raw), qa
                )
            except Exception:  # noqa: BLE001
                decision = None
        if decision is None:
            decision = AssemblyJudgeDecision(
                action=AssemblyJudgeAction.REPAIR_ASSEMBLY,
                confidence="medium",
                reason=f"judge call failed ({err[:200]}); safe default repair",
                evidence=[],
            )

    print(f"[assembly] JUDGE -> {decision.action.value} "
          f"(conf={decision.confidence}, evidence={len(decision.evidence)})")
    return {
        "judge_decision": decision,
        "execution_log": _log(
            state,
            f"judge: {decision.action.value} - {decision.reason[:120]}",
        ),
        "node_history": state.get("node_history", []) + ["judge"],
    }
