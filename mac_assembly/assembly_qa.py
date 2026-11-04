"""AssemblyQA: closed-loop detection for the assembled compound.

Deterministic checks decide pass/fail (philosophy reused from the CAD
Skills ``inspection-and-validation.md``):

1. **Part count** -- solids in the exported STEP == len(brief.parts).
2. **Envelope** -- assembly bbox within tolerance of the brief.
3. **Mate alignment** -- each MateSpec's two named datums are re-derived
   from the *placed* geometry and their world-space delta is compared
   against the expected offset (the in-process analogue of
   ``scripts/inspect align``).
4. **Interference** -- pairwise mesh overlap between placed parts
   (containment sampling + surface gap, via trimesh).

Implementation modules:
* _qa_anchors: selector resolution, cached original bounds and transforms
* _qa_mates: mate alignment and physical interface checks
* _qa_collision: mesh loading, surface gaps and interference
* _qa_kinematics: joint sweeps and moving-subtree collision checks
* _qa_dimensions: envelopes, assembled ranges and reconciliation

This module owns the caches, configuration binding and ReconcileResult type.
Helpers resolve those bindings here at call time to retain caller/test overrides.

Visual review stays diagnostic (rendered views go to the Assembly
Judge), exactly like ``snapshot-review.md``: "visual review is
diagnostic, not authoritative".
"""

from __future__ import annotations

import json
import math
import time
from collections import OrderedDict
from pathlib import Path
from typing import NamedTuple

from mac_assembly import config_assembly as cfg
from mac_assembly.assembly_codegen import _get_anchor_axis
from mac_assembly.geometry_utils import (
    AXIS_DIRS as _AXIS_DIRS,
    FACE_NORMALS as _FACE_NORMALS,
    closest_point_robust,
    mesh_containment_available,
    shift_pt,
)
from mac_assembly.schemas_assembly import (
    Anchor,
    AnchorKind,
    AssemblyBrief,
    AssemblyErrorType,
    AssemblyQAReport,
    InterferenceCheck,
    KinematicCheck,
    MateCheck,
    MateType,
)
from multi_agent_cad.render_views import _render_isometric_views


# A zero-volume shell cannot provide a reliable penetration-volume proxy.
_ZERO_VOLUME_EPS_MM3 = 1e-9

# Bounded original-geometry caches. Keys include mtime and size so in-place
# remodels invalidate old bounds. Keep one shared cache for all QA helpers.
_orig_center_cache: "OrderedDict[str, tuple]" = OrderedDict()
_orig_bbox_cache: "OrderedDict[str, tuple]" = OrderedDict()
_ORIG_CENTER_CACHE_MAX = 64


class ReconcileResult(NamedTuple):
    """Reconcile errors + structured routing attribution (P1-8).

    attribution:
      - 'part_geometry': a part-level defect (e.g. bore/shaft radii
        incompatible); `part_ids` names the parts the router should send
        to part_builder.
      - 'ambiguous': cannot be attributed from the measured data alone
        (e.g. stacking height vs envelope mismatch -- could be part dims
        OR mate offsets); the Judge decides.
    """

    errors: list[str]
    attribution: str = "ambiguous"
    part_ids: list[str] = []


def _load_placed_solids(assembly_dir: Path):
    """Return (placed, locations, error) from the exported assembly STEP.

    Solids are mapped back to part labels via the manifest written by the
    assembly script (nearest bbox centre), so QA never trusts STEP label
    metadata.

    ``locations`` is ``{label: (translation_tuple, rotation_euler_xyz_deg)}``
    from the manifest -- the part's full world transform after joints are
    applied. QA uses this to map part-local selector points through the
    FULL placed transform (not just bbox-centre translation) so rotated
    poses (chains of revolute joints) are handled exactly.
    """
    from build123d import import_step

    step_path = assembly_dir / "assembly_output.step"
    manifest_path = assembly_dir / "assembly_manifest.json"
    if not step_path.is_file():
        return None, None, "assembly_output.step missing"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else []

    locations: dict[str, tuple[tuple, tuple]] = {}
    for entry in manifest:
        loc = entry.get("location")
        if loc and "translation" in loc and "rotation_euler_xyz_deg" in loc:
            locations[entry["label"]] = (
                tuple(loc["translation"]),
                tuple(loc["rotation_euler_xyz_deg"]),
            )

    _t0 = time.perf_counter()
    compound = import_step(str(step_path))
    solids = list(compound.solids())
    print(f"[assembly] QA timing: assembly STEP import={time.perf_counter() - _t0:.1f}s")
    if not solids:
        return None, None, "assembly STEP contains no solids"

    placed = []
    used: set[int] = set()
    for entry in manifest:
        label = entry["label"]
        centre = (
            (entry["bbox_min"][0] + entry["bbox_max"][0]) / 2.0,
            (entry["bbox_min"][1] + entry["bbox_max"][1]) / 2.0,
            (entry["bbox_min"][2] + entry["bbox_max"][2]) / 2.0,
        )
        best_idx, best_d, best_size_err = None, float("inf"), float("inf")
        exp_size = (
            entry["bbox_max"][0] - entry["bbox_min"][0],
            entry["bbox_max"][1] - entry["bbox_min"][1],
            entry["bbox_max"][2] - entry["bbox_min"][2],
        )
        for idx, solid in enumerate(solids):
            if idx in used:
                continue
            bb = solid.bounding_box()
            c = bb.center()
            d = (c.X - centre[0]) ** 2 + (c.Y - centre[1]) ** 2 + (c.Z - centre[2]) ** 2
            # Tie-break on bbox SIZE similarity. Concentric pairs
            # (ball-in-socket, bushing-on-standoff) share a bbox centre, so
            # the centroid distance ties and a pure `d < best_d` assigns
            # labels by compound enumeration order -- ball and socket swap
            # labels deterministically and every downstream mate/kinematics
            # check then measures the wrong part. Sizes differ by design,
            # so the size error disambiguates.
            size_err = (
                abs((bb.max.X - bb.min.X) - exp_size[0])
                + abs((bb.max.Y - bb.min.Y) - exp_size[1])
                + abs((bb.max.Z - bb.min.Z) - exp_size[2])
            )
            if d < best_d - 1e-6 or (
                d <= best_d + 1e-6 and size_err < best_size_err - 1e-9
            ):
                best_idx, best_d, best_size_err = idx, d, size_err
        if best_idx is None:
            continue
        used.add(best_idx)
        bb = solids[best_idx].bounding_box()
        placed.append((label, (bb.min.X, bb.min.Y, bb.min.Z), (bb.max.X, bb.max.Y, bb.max.Z)))
    # Solids without a manifest match get an anonymous label.
    for idx, solid in enumerate(solids):
        if idx not in used:
            bb = solid.bounding_box()
            placed.append((f"unmatched_{idx}", (bb.min.X, bb.min.Y, bb.min.Z),
                           (bb.max.X, bb.max.Y, bb.max.Z)))
    return placed, locations, None


def run_assembly_qa(
    brief: AssemblyBrief,
    mates: list,
    part_results: dict,
    assembly_dir: Path,
    authoritative_step_paths: dict[str, str] | None = None,
) -> AssemblyQAReport:
    placed, locations, load_err = _load_placed_solids(assembly_dir)

    # Populate envelope_expected_mm from the brief so _envelope_is_borderline
    # can compare measured vs expected. Without this the field defaults to {}
    # and _all_errors_borderline always returns False (since `not {}` is True),
    # disabling the borderline false-positive override in the Judge -- which
    # forces iteration-0 borderline failures to route back instead of early
    # ACCEPTing. When the brief declares no envelope, dict({}) == {} preserves
    # the "no ground truth -> can't judge borderline" conservative behavior.
    report = AssemblyQAReport(
        part_count_expected=brief.expected_part_count or len(brief.parts),
        envelope_expected_mm=dict(brief.overall_envelope_mm),
    )

    if load_err is not None:
        report.error_type = AssemblyErrorType.FATAL
        report.all_passed = False
        report.error_details = [f"assembly load failed: {load_err}"]
        _finalize(report, mates)
        return report

    labels = [l for l, _bmin, _bmax in placed if not l.startswith("unmatched_")]
    unmatched_count = len(placed) - len(labels)
    report.part_count_measured = len(placed)  # diagnostic: actual solid count
    # Pass if every expected part has a labelled solid. Extra shells
    # (unmatched_N) are tessellation artifacts of a single part exporting
    # as multiple connected components -- they are flagged downstream by
    # interference/envelope checks, not as a part_count failure (was: any
    # extra shell -> part_count_passed=False -> FATAL -> wrong route).
    report.part_count_passed = len(labels) == report.part_count_expected
    report.missing_parts = [
        p.part_id for p in brief.parts if p.part_id not in labels
    ]

    # Load placed meshes once; shared by mate-gap, interference and the
    # kinematic sweep (R2: previously reloaded inside _check_kinematics).
    try:
        comp_labels = _load_components(assembly_dir, placed)
    except Exception:  # noqa: BLE001 - no STL / trimesh unavailable
        comp_labels = []
    if not comp_labels:
        try:
            import trimesh  # noqa: F401
            _trimesh_ok = True
        except ImportError:
            _trimesh_ok = False
        if _trimesh_ok:
            # trimesh IS available yet produced zero labelled components:
            # the assembly STL artifact is missing/corrupt/unsplittable --
            # a toolchain-level failure of the assembly PRODUCT, not part
            # interference. Classifying this INTERFERENCE misroutes it to
            # the Mating Architect, which burns MATING_MAX_RUNS "fixing" a
            # non-existent mate problem. FATAL routes to the assembler
            # (the script owns STL export), matching the load_err path.
            report.error_type = AssemblyErrorType.FATAL
            report.all_passed = False
            report.error_details = [
                "assembly mesh load failed: placed_stl/ and "
                "assembly_output.stl yielded no components -- broken "
                "assembly artifact (STL export), not a mate problem"
            ]
            _finalize(report, mates)
            return report
    meshes_by_label: dict[str, list] = {}
    for _label, _mesh in comp_labels:
        if _label != "unknown":
            meshes_by_label.setdefault(_label, []).append(_mesh)
    # Part STEP paths (SELECTOR anchors resolve against real part topology).
    step_paths = {
        pid: r.step_path
        for pid, r in (part_results or {}).items()
        if getattr(r, "ok", False) and getattr(r, "step_path", "")
    }
    # Inspect exactly the STEP files consumed by the assembly script.  A
    # graph cache may retain an older usable PartResult even after the
    # assembler pins a newer deterministic-builder artifact.
    if authoritative_step_paths:
        step_paths.update({
            str(pid): str(path)
            for pid, path in authoritative_step_paths.items()
            if path
        })
    # Part-generation warnings (e.g. v3 degraded base bodies) travel into the
    # QA report so the Judge sees them next to the deterministic failures
    # instead of them being silently swallowed by an ok=True PartResult.
    report.generation_warnings = [
        f"{pid}: {w}"
        for pid, r in sorted((part_results or {}).items())
        for w in (getattr(r, "warnings", None) or [])
    ]
    # Degraded parts (ok=True but generation only partially succeeded, e.g.
    # a v3 base body that failed whose STEP was kept) must reach the Judge
    # even when every geometric check passes: an all_passed report would
    # otherwise deliver a known-imperfect artifact as a silent success.
    # This is a routing FLAG, not an error -- error_details/all_passed stay
    # untouched so no deterministic repair loop fires on it alone.
    report.degraded_part_ids = sorted(
        pid for pid, r in (part_results or {}).items()
        if getattr(r, "degraded", False)
    )
    report.has_degraded_parts = bool(report.degraded_part_ids)
    # Environment probe: without trimesh ray-containment (rtree) the
    # interference / kinematic collision pairs are SKIPPED instead of
    # measured -- their PASS results are false negatives. The pipeline's
    # _preflight_dependencies refuses to START in such an environment;
    # this stamp covers DIRECT run_assembly_qa calls (re-QA scripts) and
    # keeps the gap visible in the report the Judge reads, never silent.
    if not mesh_containment_available():
        report.generation_warnings.append(
            "QA ENVIRONMENT: trimesh ray-containment unavailable (the "
            "'rtree' dependency is missing; the pipeline preflight "
            "refuses to start without it) -- interference and kinematic "
            "collision checks SKIP every pair, so their PASS results are "
            "UNRELIABLE (false negatives possible). Fix: pip install rtree"
        )

    _t0 = time.perf_counter()
    report.mate_checks = _check_mates(
        mates, placed, meshes_by_label, step_paths, locations,
    )
    # Datum coincidence alone does not prove a physically connected
    # assembly: a virtual joint axis can sit in empty space while the two
    # solids remain centimetres apart. Reject such false PASSes using the
    # real per-part meshes. Small modelling clearances are allowed; an
    # explicit face_to_face gap is allowed up to its requested offset.
    mate_by_id = {m.mate_id: m for m in mates}
    for check in report.mate_checks:
        if not check.passed:
            continue
        mate = mate_by_id.get(check.mate_id)
        if mate is None:
            continue
        surface_gap = _min_gap_between(
            meshes_by_label.get(mate.fixed_part_id, []),
            meshes_by_label.get(mate.moving_part_id, []),
            n_samples=800,
        )
        if surface_gap is None:
            continue
        allowed = 2.5
        if mate.mate_type == MateType.FACE_TO_FACE:
            allowed = max(allowed, abs(float(mate.offset_mm)) + mate.tolerance_mm)
        if surface_gap > allowed:
            check.passed = False
            check.measured_mm = surface_gap
            check.delta_mm = surface_gap - allowed
            check.detail = (
                f"DISCONNECTED mate: real surface gap={surface_gap:.3f}mm "
                f"> allowed {allowed:.3f}mm despite coincident abstract "
                "datums; move the solids into physical contact or add a "
                "real connecting part"
            )
    report.mates_passed = all(c.passed for c in report.mate_checks)
    _t_mates = time.perf_counter() - _t0

    report.envelope_measured_mm, env_errors = _check_envelope(brief, placed)
    report.envelope_passed = not env_errors

    # Optional target world ranges are emitted internally by the Decomposer
    # when a natural-language request distinguishes a part's own local frame
    # from its assembled pose. Validate them before asking an LLM to infer
    # placement from collisions.
    assembled_range_errors = _check_assembled_axis_ranges(brief, placed)
    if assembled_range_errors:
        report.mates_passed = False

    _t0 = time.perf_counter()
    report.interference_checks = _check_interference(comp_labels, mates)
    report.interference_passed = all(c.passed for c in report.interference_checks)
    _t_interference = time.perf_counter() - _t0

    _t0 = time.perf_counter()
    report.kinematic_checks = _check_kinematics(
        placed, mates, meshes_by_label, step_paths, locations,
    )
    report.kinematics_passed = all(c.passed for c in report.kinematic_checks)
    _t_kinematics = time.perf_counter() - _t0
    # Lightweight stage timing (stdout only; no thresholds, no behavior
    # change) so QA hot spots are visible without a profiler.
    print(
        f"[assembly] QA timing: mates={_t_mates:.1f}s "
        f"interference={_t_interference:.1f}s "
        f"kinematics={_t_kinematics:.1f}s"
    )

    details: list[str] = []
    if not report.part_count_passed:
        details.append(
            f"part count: expected {report.part_count_expected}, "
            f"measured {report.part_count_measured}; missing={report.missing_parts}"
        )
    details += [f"mate {c.mate_id} FAIL: {c.detail}" for c in report.mate_checks if not c.passed]
    details += assembled_range_errors
    details += env_errors
    details += [
        f"interference {c.part_a}/{c.part_b} FAIL: {c.detail}"
        for c in report.interference_checks if not c.passed
    ]
    details += [
        f"kinematic {c.mate_id} FAIL: {c.detail}"
        for c in report.kinematic_checks if not c.passed
    ]
    if details and (
        not report.interference_passed or not report.kinematics_passed
        or not report.mates_passed
    ):
        # The Mating Architect sees the brief but not the generated meshes.
        # A bare "collision" gives it no way to tell whether a sleeve is
        # 8 mm or 28 mm too high. Feed the actual placed world bounds back
        # without changing PASS/FAIL or asking it to guess from an image.
        for label, bmin, bmax in placed:
            if label.startswith("unmatched_"):
                continue
            details.append(
                f"placement diagnostic {label}: world bbox "
                f"X=[{bmin[0]:.2f},{bmax[0]:.2f}] "
                f"Y=[{bmin[1]:.2f},{bmax[1]:.2f}] "
                f"Z=[{bmin[2]:.2f},{bmax[2]:.2f}] mm"
            )
    report.error_details = details
    report.all_passed = not details

    _finalize(report, mates)
    return report


def _finalize(report: AssemblyQAReport, mates: list) -> None:
    """Compute error type + routing hints (repair-loop.md classification)."""
    if report.all_passed:
        report.error_type = AssemblyErrorType.NONE
        return
    if report.part_count_passed is False and report.missing_parts:
        report.error_type = AssemblyErrorType.PART_MISSING
        report.needs_remodel_part_ids = list(report.missing_parts)
        return
    if not report.kinematics_passed:
        report.error_type = AssemblyErrorType.KINEMATIC
    elif not report.interference_passed:
        report.error_type = AssemblyErrorType.INTERFERENCE
    elif not report.mates_passed:
        report.error_type = AssemblyErrorType.MATE_MISALIGNMENT
    elif not report.envelope_passed:
        report.error_type = AssemblyErrorType.ENVELOPE
    else:
        # NOT dead code (R4): the two early-return paths above
        # (assembly load failure / empty placed-mesh artifact) pre-set
        # error_type=FATAL and call _finalize with every check flag still
        # at its default True -- this branch is what keeps them FATAL
        # (needs_mate_fix_ids + the borderline flag below are the reason
        # they call _finalize at all). On the main path it is the
        # defensive fallback for a failure with no check-level source.
        report.error_type = AssemblyErrorType.FATAL
    report.needs_mate_fix_ids = [
        c.mate_id for c in report.mate_checks if not c.passed
    ] + [c.mate_id for c in report.kinematic_checks if not c.passed]

    # Borderline-failure flag: True only when EVERY failing check is
    # borderline (envelope overshoot within 2x tolerance OR interference
    # volume under 2x tolerance). Mate misalignment and kinematic failures
    # are never borderline -- their presence forces False. The Judge node
    # reads this to override its iteration_count < MIN_RETRY skip so an
    # ACCEPT can terminate the loop instead of forcing a route back.
    # Mixed real + borderline failures stay False so the real errors
    # still drive a route-back.
    report.is_likely_false_positive = _all_errors_borderline(report)


def _all_errors_borderline(report: AssemblyQAReport) -> bool:
    """True when every failing check is borderline. Mate misalignment and
    kinematic failures have no borderline band -- any such failure is real
    and forces False. The prior error_type-based dispatch only checked the
    highest-priority failing category (set above), missing real failures in
    lower-priority categories that the priority chain skipped -- e.g.
    borderline interference + real envelope failure set error_type=INTERFERENCE
    then flagged is_likely_false_positive=True without checking envelope.
    """
    if any(not c.passed for c in report.mate_checks):
        return False
    if any(not c.passed for c in report.kinematic_checks):
        return False
    if not _envelope_is_borderline(report):
        return False
    if not _interference_is_borderline(report):
        return False
    return True


def _envelope_is_borderline(report: AssemblyQAReport) -> bool:
    """All envelope errors overshoot by less than 1 extra tolerance band
    (i.e. measured - expected falls in (tol, 2*tol]). Tighter than 2*tol
    is a real violation; the borderline band catches tessellation / mesh
    inflation drift that just barely crosses the threshold.
    """
    expected = report.envelope_expected_mm
    measured = report.envelope_measured_mm
    if not expected or not measured:
        return False
    for axis, exp in expected.items():
        if exp <= 0:
            continue
        meas = measured.get(axis, 0.0)
        tol = exp * cfg.ENVELOPE_TOLERANCE_PCT
        err = abs(meas - exp)
        if err <= tol:
            continue  # this axis passed -- not the failure axis
        if err > 2 * tol:
            return False  # real violation, not borderline
    return True


def _interference_is_borderline(report: AssemblyQAReport) -> bool:
    """All interference failures have penetration volume under 2x the
    tolerance (i.e. <= 10 mm^3 by default). Volumes in this range are
    typically tessellation overlap of coplanar faces, not real collision.
    """
    for c in report.interference_checks:
        if c.passed:
            continue
        if c.penetration_volume_mm3 > 2 * cfg.INTERFERENCE_VOLUME_TOL_MM3:
            return False
    return True


def render_assembly_views(assembly_dir: Path) -> list[bytes]:
    """Diagnostic views for the Assembly Judge (reuse MAC render_views)."""
    stl = assembly_dir / "assembly_output.stl"
    if not stl.is_file():
        return []
    views = _render_isometric_views(
        stl, n_views=cfg.ASSEMBLY_JUDGE_VIEWS_COUNT, size=cfg.ASSEMBLY_JUDGE_VIEW_SIZE
    )
    if views and cfg.ASSEMBLY_JUDGE_SAVE_VIEWS:
        out_dir = assembly_dir / "assembly_judge_views"
        out_dir.mkdir(exist_ok=True)
        for i, png in enumerate(views):
            (out_dir / f"view_{i}.png").write_bytes(png)
    return views


# Load implementations after shared state and public types are initialized.
# Historical helper names remain available for callers and test replacements.
from mac_assembly._qa_anchors import (
    _anchor_world_point as _anchor_world_point,
    _point_bbox_gap as _point_bbox_gap,
    _resolve_qa_anchor as _resolve_qa_anchor,
    _resolve_qa_point as _resolve_qa_point,
    _resolve_qa_point_3state as _resolve_qa_point_3state,
    _resolve_qa_anchor_3state as _resolve_qa_anchor_3state,
    _probe_selector_anchor as _probe_selector_anchor,
    _validate_selector_anchor as _validate_selector_anchor,
    _orig_bbox_center as _orig_bbox_center,
    _orig_bbox as _orig_bbox,
    _local_to_world_point as _local_to_world_point,
    _local_to_world_direction as _local_to_world_direction,
    _moved_part_location as _moved_part_location,
    _anchor_local_datum as _anchor_local_datum,
    _anchor_world_datum as _anchor_world_datum,
)
from mac_assembly._qa_mates import (
    _check_mates as _check_mates,
)
from mac_assembly._qa_collision import (
    _sample_surface as _sample_surface,
    _load_components as _load_components,
    _deep_penetration_frac as _deep_penetration_frac,
    _min_gap_between as _min_gap_between,
    _pair_collides as _pair_collides,
    _check_interference as _check_interference,
)
from mac_assembly._qa_kinematics import (
    _moving_subtree as _moving_subtree,
    _check_kinematics as _check_kinematics,
)
from mac_assembly._qa_dimensions import (
    _check_envelope as _check_envelope,
    _check_assembled_axis_ranges as _check_assembled_axis_ranges,
    reconcile_dimensions as reconcile_dimensions,
)
