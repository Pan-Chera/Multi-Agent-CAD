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


# Volume below which a mesh is treated as zero-volume for interference
# probing (BUG-016). ``_pair_collides`` uses ``min(|Va|, |Vb|)`` as a
# penetration proxy; a zero-volume mesh (flat plate, single-triangle STL,
# sheet metal without thickness) zeroes the proxy and false-passes. The
# epsilon catches near-degenerate shells (floating-point thickness).
_ZERO_VOLUME_EPS_MM3 = 1e-9


# ---------------------------------------------------------------------------
# Geometry loading
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Anchor maths (mirror of the runtime derivation in the generated script)
# ---------------------------------------------------------------------------


def _anchor_world_point(anchor: Anchor, bb_min, bb_max):
    cx = (bb_min[0] + bb_max[0]) / 2.0
    cy = (bb_min[1] + bb_max[1]) / 2.0
    cz = (bb_min[2] + bb_max[2]) / 2.0
    if anchor.kind == AnchorKind.SELECTOR:
        # SELECTOR anchors carry a semantic query, not bbox algebra. Callers
        # that can resolve the real face do so BEFORE calling this; this
        # fallback (bbox centre) only guards resolver-unavailable paths so a
        # SELECTOR anchor can never crash QA with KeyError: None (N2a).
        return (cx, cy, cz)
    if anchor.kind == AnchorKind.FACE:
        # Face centre = bbox centre + outward normal * half-extent
        # (normals from geometry_utils, single source of truth).
        n = _FACE_NORMALS[anchor.face]
        return (
            cx + n[0] * (bb_max[0] - bb_min[0]) / 2.0,
            cy + n[1] * (bb_max[1] - bb_min[1]) / 2.0,
            cz + n[2] * (bb_max[2] - bb_min[2]) / 2.0,
        )
    if anchor.kind == AnchorKind.SPHERE:
        # Sphere anchor: explicit sphere_center_mm in part-local coords.
        # QA transforms the part-local center through the placed Location
        # in the caller; here we return the part-local point so the caller
        # can apply the transform (mirrors _emit_anchor SPHERE branch).
        sc = anchor.sphere_center_mm or [0.0, 0.0, 0.0]
        return (float(sc[0]), float(sc[1]), float(sc[2]))
    if anchor.axis is None:
        # AXIS_POINT with axis=None (shouldn't happen, but guard) — fall
        # back to bbox centre so QA never crashes with KeyError: None.
        return (cx, cy, cz)
    if anchor.point_mm is not None:
        return tuple(float(v) for v in anchor.point_mm)
    off = anchor.offset_mm or 0.0
    return {
        "x": (cx + off, cy, cz),
        "y": (cx, cy + off, cz),
        "z": (cx, cy, cz + off),
    }[anchor.axis]


def _point_bbox_gap(point, bb_min, bb_max) -> float:
    """Distance from ``point`` to an axis-aligned bbox: 0 when the point is
    inside (or on) the box, otherwise the Euclidean distance to the nearest
    point on the box surface."""
    d2 = 0.0
    for k in range(3):
        if point[k] < bb_min[k]:
            d2 += (bb_min[k] - point[k]) ** 2
        elif point[k] > bb_max[k]:
            d2 += (point[k] - bb_max[k]) ** 2
    return math.sqrt(d2)


# ---------------------------------------------------------------------------
# Check implementations
# ---------------------------------------------------------------------------


def _resolve_qa_anchor(anchor, step_path: str | None):
    """Resolve a SELECTOR anchor to real topology (part-local).

    Returns the resolver dict (point/axis/selector/...) or None when
    unresolvable (caller falls back to bbox algebra). The point and axis
    are in the part's LOCAL coordinates -- authoritative for an unmoved
    part; a moved part's caller must shift via ``_local_to_world_point``.
    """
    if anchor.kind != AnchorKind.SELECTOR or not step_path:
        return None
    try:
        from mac_assembly.selector_resolver import resolve_selector_anchor

        return resolve_selector_anchor(
            step_path,
            anchor.selector_query.model_dump(mode="json", exclude_none=True),
        )
    except Exception:  # noqa: BLE001
        return None


def _resolve_qa_point(anchor, step_path: str | None):
    """Resolve a SELECTOR anchor to a real topology point (part-local).

    Returns (point, True) on success; (None, False) when unresolvable
    (caller falls back to bbox algebra). NOTE: the point is in the part's
    LOCAL coordinates -- authoritative for the (unmoved) fixed side; for a
    moved part the caller must not treat it as a world position.

    BUG-002 (P0 revision): callers that need to distinguish "selector
    miss" (definitive -- geometry absent) from "probe error"
    (infrastructure fault) from "non-SELECTOR anchor" (use bbox algebra)
    must call :func:`_resolve_qa_point_3state` instead. The legacy
    two-tuple collapses those three states into (None, False), which
    caused silent bbox-fallback when a SELECTOR cylinder query matched
    no face on the part (e.g. a Box part with a cylinder SELECTOR --
    the fixed datum became the Box bbox centre, then the moving-side
    target verification passed, silently false-PASSing the mate).
    """
    resolved = _resolve_qa_anchor(anchor, step_path)
    if resolved:
        return tuple(resolved["point"]), True
    return None, False


def _resolve_qa_point_3state(anchor, step_path: str | None):
    """Three-state resolution of a SELECTOR anchor.

    Returns ``(status, point)`` where ``status`` is one of:

      * ``"found"``      -- ``point`` is the resolved part-local point.
      * ``"not_found"``  -- SELECTOR probe found NO matching face
        (geometry absent). Callers MUST fail with the selector-miss
        signature; do NOT fall back to bbox algebra.
      * ``"error"``      -- STEP unreadable / cadpy unavailable /
        SelectorIndex build failed, OR anchor is SELECTOR but
        ``step_path`` is missing/empty, OR selector_query is None.
        UNVERIFIABLE.
      * ``"no_selector"``-- anchor is not SELECTOR; caller uses
        FACE/AXIS_POINT/SPHERE bbox algebra (existing behavior).

    Replaces the legacy two-tuple ``_resolve_qa_point`` for call sites
    that must avoid the silent bbox-fallback (BUG-002 P0 revision).

    P1-1 revision: SELECTOR + missing step_path / missing selector_query
    is now ``"error"`` (UNVERIFIABLE), not ``"no_selector"``. Treating a
    SELECTOR with no STEP as "no_selector" let _check_kinematics fall
    through to bbox algebra and silently sweep about a guessed axis.
    """
    status, result = _resolve_qa_anchor_3state(anchor, step_path)
    if status == "found" and result:
        return "found", tuple(result["point"])
    return status, None


def _resolve_qa_anchor_3state(anchor, step_path: str | None):
    """Three-state resolution returning the FULL resolver result dict.

    Shared by ``_check_mates`` (via _resolve_qa_point_3state) and
    ``_check_kinematics`` (directly), so both paths fail closed
    identically on SELECTOR miss / probe error (P1-1 requirement 4:
    avoid one path fail-closed + another path fallback).

    Returns ``(status, result)`` where ``status`` is one of:

      * ``"found"``      -- ``result`` is the resolver dict
        (point/axis/selector/...).
      * ``"not_found"``  -- SELECTOR probe found NO matching face
        (geometry absent). Callers MUST fail with the selector-miss
        signature.
      * ``"error"``      -- STEP unreadable / cadpy unavailable /
        SelectorIndex build failed, OR SELECTOR + missing step_path,
        OR SELECTOR + missing selector_query. UNVERIFIABLE.
      * ``"no_selector"``-- anchor is not SELECTOR; caller uses
        FACE/AXIS_POINT/SPHERE bbox algebra.

    Result dict (status ``"found"``)::

        {"point": (x, y, z),
         "axis": (dx, dy, dz),
         "selector": "f5",
         "surface": "plane"|"cylinder",
         "radius": float | None,
         "coordinate": float | None,
         "candidate_count": int,
         "role": str | None (cylinder only)}
    """
    if anchor.kind != AnchorKind.SELECTOR:
        return "no_selector", None
    sq = anchor.selector_query
    if sq is None:
        return "error", None
    if not step_path:
        return "error", None
    try:
        from mac_assembly.selector_resolver import probe_selector_anchor

        status, result = probe_selector_anchor(
            step_path,
            sq.model_dump(mode="json", exclude_none=True),
        )
    except Exception:  # noqa: BLE001
        return "error", None
    if status == "found" and result:
        return "found", result
    return status, None


def _probe_selector_anchor(step_path: str, query: dict) -> tuple[str, dict | None]:
    """Three-state probe wrapper for the rigid-no-target uniqueness
    check (BUG-043). Delegates to selector_resolver.probe_selector_anchor
    so QA can read the candidate_count without modifying the public
    resolver API.
    """
    try:
        from mac_assembly.selector_resolver import probe_selector_anchor

        return probe_selector_anchor(step_path, query)
    except Exception:  # noqa: BLE001
        return "error", None


def _validate_selector_anchor(
    anchor,
    step_path: str | None,
    part_id: str,
    anchor_label: str,
    tolerance_mm: float,
) -> tuple[bool, str, dict | None]:
    """Fail-closed validation shared by every mate type.

    A selector must resolve, identify exactly one semantic candidate, and
    satisfy every explicit target coordinate in the part-local frame.  This
    runs before mate-specific early returns so motion mates cannot bypass
    moving-side validation.
    """
    if anchor.kind != AnchorKind.SELECTOR:
        return True, "", None

    sq = anchor.selector_query
    sq_dict = (
        sq.model_dump(mode="json", exclude_none=True)
        if sq is not None else {}
    )
    status, result = _resolve_qa_anchor_3state(anchor, step_path)
    if status == "not_found":
        return False, (
            f"SELECTOR anchor matched no face on {part_id}: {sq_dict!r}"
        ), None
    if status == "error":
        return False, (
            f"UNVERIFIABLE: {anchor_label} SELECTOR probe errored "
            f"(infrastructure fault) on {part_id}"
        ), None
    if status != "found" or result is None:
        return False, (
            f"UNVERIFIABLE: {anchor_label} SELECTOR returned "
            f"unexpected status {status!r} on {part_id}"
        ), None

    targets = []
    if sq is not None:
        for name, idx in (("x", 0), ("y", 1), ("z", 2)):
            value = getattr(sq, f"target_{name}_mm", None)
            if value is not None:
                targets.append((name, idx, float(value)))

    candidate_count = int(result.get("candidate_count") or 0)
    if candidate_count == 0:
        if targets:
            return False, (
                f"UNVERIFIABLE: {anchor_label} SELECTOR target matches no "
                f"candidate on {part_id}: {sq_dict!r}"
            ), result
        return False, (
            f"UNVERIFIABLE: {anchor_label} SELECTOR resolved without a "
            f"semantic candidate on {part_id}: {sq_dict!r}"
        ), result
    if candidate_count != 1:
        return False, (
            f"UNVERIFIABLE: {anchor_label} SELECTOR has "
            f"{candidate_count} candidate faces on {part_id}; target does "
            f"not uniquely disambiguate: {sq_dict!r}"
        ), result

    if targets:
        point = tuple(float(v) for v in result["point"])
        tol = max(float(tolerance_mm), 0.5)
        deltas = [
            (name, abs(point[idx] - target), target)
            for name, idx, target in targets
        ]
        worst_name, worst_delta, worst_target = max(
            deltas, key=lambda item: item[1]
        )
        if worst_delta > tol:
            return False, (
                f"UNVERIFIABLE: {anchor_label} SELECTOR target mismatch on "
                f"{part_id}: resolved {point}, target_{worst_name}_mm="
                f"{worst_target:.3f}, delta={worst_delta:.3f}mm "
                f"(tol {tol:.3f}mm)"
            ), result

    return True, "", result


# Bounded LRU: the mtime+size key grows by one entry per remodel of a part,
# so an unbounded dict leaks across a long session (B17).
_orig_center_cache: "OrderedDict[str, tuple]" = OrderedDict()
_orig_bbox_cache: "OrderedDict[str, tuple]" = OrderedDict()
_ORIG_CENTER_CACHE_MAX = 64


def _orig_bbox_center(step_path: str) -> tuple | None:
    """Cached original (pre-placement) bbox centre of a part STEP.

    Cache key includes mtime+size (reuses selector_resolver._cache_key) so
    an in-place Aider remodel never shifts the N1 datum against stale
    original geometry.
    """
    bb = _orig_bbox(step_path)
    if bb is None:
        return None
    (bmin, bmax) = bb
    return tuple((bmin[k] + bmax[k]) / 2.0 for k in range(3))


def _orig_bbox(step_path: str) -> tuple[tuple, tuple] | None:
    """Cached ORIGINAL (pre-placement) bbox of a part STEP as
    ((xmin, ymin, zmin), (xmax, ymax, zmax)).

    Needed to re-derive a FACE / AXIS_POINT anchor's part-LOCAL datum
    (face centre / axis-offset point) on the un-rotated geometry, which is
    then transformed into world coords via the placed Location -- using the
    PLACED world bbox instead would silently read the wrong face off any
    part that a parent mate has rotated (P1-6).
    """
    from mac_assembly.selector_resolver import _cache_key

    key = _cache_key(step_path)
    if key in _orig_bbox_cache:
        _orig_bbox_cache.move_to_end(key)
        return _orig_bbox_cache[key]
    try:
        from build123d import import_step

        bb = import_step(step_path).bounding_box()
        v = (
            (bb.min.X, bb.min.Y, bb.min.Z),
            (bb.max.X, bb.max.Y, bb.max.Z),
        )
    except Exception:  # noqa: BLE001
        v = None
    _orig_bbox_cache[key] = v
    _orig_bbox_cache.move_to_end(key)
    while len(_orig_bbox_cache) > _ORIG_CENTER_CACHE_MAX:
        _orig_bbox_cache.popitem(last=False)
    return v


def _local_to_world_point(point, location_data, step_path: str | None = None,
                          placed_bbox=None) -> tuple:
    """Transform a part-LOCAL selector point into world coords.

    Preferred path: ``location_data`` (translation + Euler XYZ rotation in
    degrees, from the manifest) -- applies the full 4x4 transform so
    rotated poses (chains of revolute joints where the fixed part of
    mate N is itself rotated by mate N-1) are handled exactly. The previous
    bbox-centre-translation approximation silently used the wrong axis
    direction for chained revolute joints (the local axis was used as
    world, ignoring the parent's rotation).

    Fallback: when ``location_data`` is None (old manifest without the
    ``location`` field), approximate the transform as a pure translation
    by the (placed - original) bbox-centre delta. Exact for pure-translation
    assemblies; close enough for the approximate pivot/proximity checks
    when no parent rotation is involved. This fallback only serves parts
    NOT subject to the ``_moved_part_location`` gate (roots/unmoved
    parts, whose legacy poses never carried rotation): a MOVED part
    without manifest location is failed UNVERIFIABLE by that gate before
    it can reach this code.
    """
    if location_data is not None:
        translation, rotation_euler = location_data
        from build123d import Location, Vector

        has_rot = any(abs(a) > 1e-9 for a in rotation_euler)
        if has_rot:
            loc = Location(
                Vector(*translation),
                Vector(*rotation_euler),
            )
        else:
            loc = Location(Vector(*translation))
        world = loc * Location(tuple(point))
        return (world.position.X, world.position.Y, world.position.Z)
    # Fallback: bbox-center shift (pure translation approximation).
    if step_path and placed_bbox:
        orig = _orig_bbox_center(step_path)
        if orig is not None:
            shift = tuple(
                (placed_bbox[0][k] + placed_bbox[1][k]) / 2.0 - orig[k]
                for k in range(3)
            )
            return tuple(point[k] + shift[k] for k in range(3))
    return tuple(point)


def _local_to_world_direction(local_direction, location_data) -> tuple | None:
    """Transform a part-LOCAL direction into WORLD coords (rotation only,
    no translation).

    Reuses the "transform the endpoint, transform the origin, subtract"
    logic of ``_check_mates``' selector-axis path: the shared Location
    multiplication cancels the translation, leaving the rotated direction.

    When ``location_data`` is None (legacy manifest without the
    ``location`` field) there is NO rotation information; the unchanged
    local direction is returned -- exact for pure-translation assemblies,
    and the same approximation ``_local_to_world_point`` makes for
    points. Only reachable for ungated (root/unmoved) parts: callers
    transforming a MOVED part's direction must first pass
    ``_moved_part_location``, which fails UNVERIFIABLE when the manifest
    location is missing, so a possibly-rotated legacy pose never
    silently sweeps about an unrotated axis.

    Returns None when the transform produces a degenerate (near-zero)
    vector -- callers must fail their check explicitly instead of silently
    sweeping about a garbage axis.
    """
    if location_data is None:
        return tuple(local_direction)
    translation, rotation_euler = location_data
    from build123d import Location, Vector

    if any(abs(a) > 1e-9 for a in rotation_euler):
        loc = Location(Vector(*translation), Vector(*rotation_euler))
    else:
        loc = Location(Vector(*translation))
    origin = loc * Location((0.0, 0.0, 0.0))
    end = loc * Location(tuple(local_direction))
    d = (
        end.position.X - origin.position.X,
        end.position.Y - origin.position.Y,
        end.position.Z - origin.position.Z,
    )
    mag = math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2])
    if mag < 1e-9:
        return None
    return (d[0] / mag, d[1] / mag, d[2] / mag)


def _moved_part_location(part_id, locations):
    """Placement data for a MOVED part: manifest location or bust.

    A moved part (placed by a parent mate) MUST carry a manifest
    ``location``. Legacy manifests without the ``location`` field get NO
    approximation: AABB comparison was deliberately rejected as
    "no-rotation" evidence because it proves nothing -- a 180 deg turn
    about ANY principal axis leaves the ordered AABB dimensions of EVERY
    part unchanged (and a dimension-symmetric part additionally hides 90
    deg turns), while the local face/axis datums and SELECTOR axis
    directions silently point elsewhere after such a turn. Restoring a
    pose must use real transform data (the current codegen manifest
    records every part's Location), never AABB inference.

    Returns ``(location_data | None, error | None)``:
    - ``(loc, None)`` -- manifest location present; transform exactly.
    - ``(None, err)`` -- moved part without a location; callers must
      fail their check UNVERIFIABLE instead of guessing.
    """
    loc = locations.get(part_id)
    if loc is not None:
        return loc, None
    return None, (
        f"{part_id} was moved (placed by a parent mate) but the assembly "
        f"manifest carries no location for it: the world pose cannot be "
        f"re-derived. AABB dimensions are NOT accepted as no-rotation "
        f"evidence -- a 180 deg turn about any principal axis leaves the "
        f"ordered AABB dimensions of every part unchanged -- so the "
        f"pure-translation approximation is refused outright for moved "
        f"parts. Re-run the current assembly codegen (its manifest "
        f"records every part's location) instead of guessing the pose."
    )


def _anchor_local_datum(anchor: Anchor, orig_bbox) -> tuple[tuple, tuple]:
    """Part-LOCAL (point, direction) of a FACE / AXIS_POINT anchor,
    computed from the part's ORIGINAL (unplaced) bbox -- the same algebra
    ``_anchor_world_point`` applies to the placed bbox, which is only
    correct when the part was never rotated (P1-6).
    """
    bmin, bmax = orig_bbox
    cx = (bmin[0] + bmax[0]) / 2.0
    cy = (bmin[1] + bmax[1]) / 2.0
    cz = (bmin[2] + bmax[2]) / 2.0
    if anchor.kind == AnchorKind.FACE:
        n = _FACE_NORMALS[anchor.face]
        return (
            (
                cx + n[0] * (bmax[0] - bmin[0]) / 2.0,
                cy + n[1] * (bmax[1] - bmin[1]) / 2.0,
                cz + n[2] * (bmax[2] - bmin[2]) / 2.0,
            ),
            tuple(n),
        )
    axis = anchor.axis or "z"
    if anchor.point_mm is not None:
        return tuple(float(v) for v in anchor.point_mm), _AXIS_DIRS[axis]
    off = anchor.offset_mm or 0.0
    pt = {
        "x": (cx + off, cy, cz),
        "y": (cx, cy + off, cz),
        "z": (cx, cy, cz + off),
    }[axis]
    return pt, _AXIS_DIRS[axis]


def _anchor_world_datum(
    anchor: Anchor,
    part_id: str,
    bboxes,
    step_paths,
    locations,
    moved_set,
) -> tuple[tuple | None, tuple | None, str | None]:
    """Resolve a FACE / AXIS_POINT anchor to (world_point, world_dir).

    For a part that was NEVER moved (not in moved_set) the placed bbox
    equals the original bbox and the local direction IS the world
    direction -- the legacy bbox algebra is exact, kept for compatibility.

    For a MOVED part the datum is computed on the part's ORIGINAL bbox
    (local frame) and then pushed through the placed Location (point via
    ``_local_to_world_point``, direction via ``_local_to_world_direction``
    -- rotation only). Using the PLACED world bbox instead would read the
    wrong face off a rotated part (a part rolled 90 deg has a different
    physical face at the world "top").

    Returns (None, None, error_detail) when the transform is impossible
    (no STEP to measure the original bbox from, or a degenerate world
    direction). Callers must fail their check with the error detail --
    silently falling back to the rotated-part bbox algebra would measure
    the wrong datum.
    """
    if part_id not in moved_set:
        return (
            _anchor_world_point(anchor, *bboxes[part_id]),
            None,  # direction unused by unmoved callers' legacy paths
            None,
        )
    # Placement gate FIRST: a moved part needs the manifest location,
    # full stop (see _moved_part_location -- AABB comparison is not
    # accepted as no-rotation evidence). Without it, the transforms
    # below would silently guess the world datum.
    loc, loc_err = _moved_part_location(part_id, locations)
    if loc_err:
        return None, None, loc_err
    step_path = step_paths.get(part_id)
    if not step_path:
        return None, None, (
            f"{part_id} anchor {anchor.kind.value} datum cannot be "
            f"re-derived: part is moved (placed by a parent mate) but no "
            f"part STEP is available to measure the original bbox"
        )
    orig = _orig_bbox(step_path)
    if orig is None:
        return None, None, (
            f"{part_id} anchor {anchor.kind.value} datum cannot be "
            f"re-derived: original bbox of {step_path} failed to load"
        )
    local_pt, local_dir = _anchor_local_datum(anchor, orig)
    world_pt = _local_to_world_point(
        local_pt, loc, step_path, bboxes[part_id]
    )
    world_dir = _local_to_world_direction(local_dir, loc)
    if world_dir is None:
        return None, None, (
            f"{part_id} anchor {anchor.kind.value} world direction "
            f"degenerate after transform through the placed location"
        )
    return world_pt, world_dir, None


def _check_mates(mates, placed, meshes_by_label=None, step_paths=None,
                 locations=None) -> list[MateCheck]:
    step_paths = step_paths or {}
    locations = locations or {}
    bboxes = {label: (bmin, bmax) for label, bmin, bmax in placed}
    moved_set = {m.moving_part_id for m in mates}
    checks: list[MateCheck] = []
    for mate in mates:
        # LINEAR/CYLINDRICAL are parametrised by position_mm, not offset_mm;
        # reporting the wrong "expected" makes the QA report incomparable.
        expected_mm = (
            mate.position_mm if mate.mate_type in (MateType.LINEAR, MateType.CYLINDRICAL)
            else mate.offset_mm
        )
        chk = MateCheck(
            mate_id=mate.mate_id,
            mate_type=mate.mate_type,
            expected_mm=expected_mm,
            tolerance_mm=mate.tolerance_mm,
        )
        if mate.fixed_part_id not in bboxes or mate.moving_part_id not in bboxes:
            chk.passed = False
            chk.detail = "part bbox missing (part not placed?)"
            checks.append(chk)
            continue
        # Verifiable-placement gate: every MOVED part this mate references
        # must carry manifest location data, full stop (see
        # _moved_part_location -- a missing location has NO acceptable
        # approximation, because AABB equality cannot certify "unrotated").
        # Without it, every local->world transform below (SELECTOR point,
        # SPHERE centre, FACE/AXIS_POINT datum, axis direction) would be
        # a silent guess on a possibly rotated pose.
        _unverifiable = None
        for _pid in (mate.fixed_part_id, mate.moving_part_id):
            if _pid in moved_set:
                _loc, _loc_err = _moved_part_location(_pid, locations)
                if _loc_err:
                    _unverifiable = _loc_err
                    break
        if _unverifiable:
            chk.passed = False
            chk.delta_mm = mate.tolerance_mm * 2
            chk.detail = f"UNVERIFIABLE: {_unverifiable}"
            checks.append(chk)
            continue
        # Fixed anchor: SELECTOR resolves on the real topology. For an
        # intermediate fixed part that was itself moved, the resolved point
        # is part-LOCAL -- shift it into world coords first (N1).
        fp_status, fp_sel = _resolve_qa_point_3state(
            mate.fixed_anchor, step_paths.get(mate.fixed_part_id)
        )
        if fp_status == "found":
            fp = fp_sel
            if (
                mate.fixed_part_id in moved_set
                and step_paths.get(mate.fixed_part_id)
            ):
                fp = _local_to_world_point(
                    fp_sel,
                    locations.get(mate.fixed_part_id),
                    step_paths[mate.fixed_part_id],
                    bboxes[mate.fixed_part_id],
                )
        elif fp_status == "not_found":
            # SELECTOR probe found NO matching face on the fixed part.
            # The previous code fell through to the FACE/AXIS_POINT
            # bbox fallback, treating the SELECTOR as if it named a
            # face -- the fixed datum became the part's bbox centre,
            # then the moving-side target verification passed,
            # silently false-PASSing the mate (BUG-002 P0 revision:
            # "SELECTOR 解析失败后的 bbox 中心不能当成有效基准").
            #
            # P1-2: canonical format so the router's
            # _SELECTOR_MISS_RE can parse part_id + query and
            # attribute via _attribute_selector_miss (spec-promised
            # -> remodel, plan hallucinated -> remate).
            _fp_sq = mate.fixed_anchor.selector_query
            _fp_sq_dict = (
                _fp_sq.model_dump(mode="json", exclude_none=True)
                if _fp_sq is not None else {}
            )
            chk.passed = False
            chk.delta_mm = mate.tolerance_mm * 2
            chk.detail = (
                f"SELECTOR anchor matched no face on "
                f"{mate.fixed_part_id}: {_fp_sq_dict!r}"
            )
            checks.append(chk)
            continue
        elif fp_status == "error":
            # Infrastructure fault -- UNVERIFIABLE, not a silent PASS.
            chk.passed = False
            chk.delta_mm = mate.tolerance_mm * 2
            chk.detail = (
                f"UNVERIFIABLE: fixed SELECTOR probe errored "
                f"(infrastructure fault) on {mate.fixed_part_id}"
            )
            checks.append(chk)
            continue
        elif mate.fixed_anchor.kind == AnchorKind.SPHERE:
            # SPHERE fixed anchor (ball mate's socket side): sphere_center_mm
            # is part-LOCAL. For a moved fixed part (chained ball mate),
            # transform through the placed location; for a root fixed part
            # at origin, the transform is identity and this still returns
            # the part-local value.
            sc = mate.fixed_anchor.sphere_center_mm or [0.0, 0.0, 0.0]
            fp = _local_to_world_point(
                (float(sc[0]), float(sc[1]), float(sc[2])),
                locations.get(mate.fixed_part_id),
                step_paths.get(mate.fixed_part_id),
                bboxes[mate.fixed_part_id],
            )
        else:
            # FACE / AXIS_POINT fixed anchor. For an unmoved fixed part the
            # placed-bbox algebra is exact. For a MOVED fixed part (placed
            # by a parent mate) the datum must be derived on the ORIGINAL
            # bbox and pushed through the placed Location -- a rotated
            # part's world "top" bbox face is a different physical face
            # than its local top (P1-6).
            fp, _fp_dir, fp_err = _anchor_world_datum(
                mate.fixed_anchor, mate.fixed_part_id, bboxes,
                step_paths, locations, moved_set,
            )
            if fp is None:
                chk.passed = False
                chk.delta_mm = mate.tolerance_mm * 2
                chk.detail = f"UNVERIFIABLE: {fp_err}"
                checks.append(chk)
                continue

        # Validate both SELECTOR anchors before any mate-type branch can
        # return early.  Previously LINEAR/CYLINDRICAL and
        # REVOLUTE/COAXIAL only checked the fixed datum and could PASS even
        # when the moving selector named geometry that did not exist.
        selector_failed = False
        for anchor_label, part_id, anchor in (
            ("fixed", mate.fixed_part_id, mate.fixed_anchor),
            ("moving", mate.moving_part_id, mate.moving_anchor),
        ):
            ok, detail, _result = _validate_selector_anchor(
                anchor,
                step_paths.get(part_id),
                part_id,
                anchor_label,
                mate.tolerance_mm,
            )
            if not ok:
                chk.passed = False
                chk.delta_mm = mate.tolerance_mm * 2
                chk.detail = detail
                checks.append(chk)
                selector_failed = True
                break
        if selector_failed:
            continue

        if mate.mate_type in (MateType.LINEAR, MateType.CYLINDRICAL):
            # Slide joints have no single point datum inside the moving part,
            # but the slide datum (fixed anchor) must still land near the
            # placed moving part. This basic proximity check works even when
            # the kinematic sweep is disabled (R5 -- was an unconditional pass).
            mbmin, mbmax = bboxes[mate.moving_part_id]
            gap = _point_bbox_gap(fp, mbmin, mbmax)
            diag = math.sqrt(sum(
                (mbmax[k] - mbmin[k]) ** 2 for k in range(3)
            ))
            tol = max(diag, 1.0)  # datum may sit up to ~one part-diagonal away
            chk.measured_mm = gap
            chk.delta_mm = gap
            chk.passed = gap <= tol
            chk.detail = (
                f"{mate.mate_type.value} datum-proximity sanity check: "
                f"slide datum {gap:.1f}mm from moving part (limit "
                f"{tol:.1f}mm). This does NOT verify the static "
                f"position_mm={mate.position_mm} (joint-enforced at "
                f"placement, not independently re-derived); motion range "
                f"validated by sweep"
            )
            checks.append(chk)
            continue

        if mate.mate_type in (MateType.REVOLUTE, MateType.COAXIAL):
            # Motion joints rotate the moving part by their static pose, so
            # the moving bbox centre no longer marks the datum. Robust check
            # instead: the joint pivot (fixed-side anchor SHIFTED by
            # axial_offset_mm along the rotation axis -- the actual location
            # the moving part's bore aligns to) must land inside (or near)
            # the placed moving part's bbox. Without the axial_offset shift
            # the check uses the unshifted cylinder midpoint, which for a
            # stacked joint (post midpoint Z=14, axial_offset=-3 -> actual
            # pivot Z=11; arm bbox Z=8..14 -> unshifted Z=14 fails on
            # boundary, shifted Z=11 passes inside) gives false FAILs.
            mbmin, mbmax = bboxes[mate.moving_part_id]
            tol = max(mate.tolerance_mm, 1.0)
            # Resolve the fixed anchor's axis direction (for SELECTOR, the
            # resolver returns it in part-local coords; transform to world
            # like the point -- rotation only, via the shared helper).
            axis_dir = None
            resolved_full = _resolve_qa_anchor(
                mate.fixed_anchor, step_paths.get(mate.fixed_part_id)
            )
            if resolved_full:
                ax_local = tuple(resolved_full["axis"])
                if sum(abs(v) for v in ax_local) > 1e-9:
                    if mate.fixed_part_id in moved_set:
                        axis_dir = _local_to_world_direction(
                            ax_local, locations.get(mate.fixed_part_id)
                        )
                        if axis_dir is None:
                            # Degenerate transform: fail explicitly instead
                            # of silently sweeping about a fallback axis.
                            chk.passed = False
                            chk.delta_mm = tol * 2
                            chk.detail = (
                                "UNVERIFIABLE: selector axis direction "
                                "degenerate after transform through "
                                f"{mate.fixed_part_id}'s placed location"
                            )
                            checks.append(chk)
                            continue
                    else:
                        axis_dir = ax_local
            if not axis_dir or sum(abs(v) for v in axis_dir) < 1e-9:
                # Mirror codegen's axis derivation (_get_anchor_axis maps a
                # FACE anchor to its normal axis, e.g. left/right -> x).
                # anchor.axis first so AXIS_POINT keeps its explicit axis;
                # raw `anchor.axis or "z"` would sweep every FACE-anchored
                # revolute about Z regardless of the face normal.
                axis_name = (
                    mate.fixed_anchor.axis
                    or _get_anchor_axis(mate.fixed_anchor)
                    or "z"
                )
                axis_dir = _AXIS_DIRS.get(axis_name, _AXIS_DIRS["z"])
                if mate.fixed_part_id in moved_set:
                    # A moved fixed part's LOCAL principal axis is not a
                    # world axis (parent mate rotated it): transform it too
                    # instead of sweeping about an unrotated direction.
                    # A degenerate transform fails the check explicitly --
                    # silently keeping the unrotated axis would sweep about
                    # a direction the placed part no longer has.
                    transformed = _local_to_world_direction(
                        axis_dir, locations.get(mate.fixed_part_id)
                    )
                    if transformed is None:
                        chk.passed = False
                        chk.delta_mm = tol * 2
                        chk.detail = (
                            "UNVERIFIABLE: fixed-part axis "
                            f"{axis_name!r} degenerate after transform "
                            f"through {mate.fixed_part_id}'s placed location"
                        )
                        checks.append(chk)
                        continue
                    axis_dir = transformed
            # Apply axial_offset_mm to the fixed anchor point along the
            # axis direction. Same math as codegen's runtime _shift_pt --
            # both call mac_assembly.geometry_utils.shift_pt so they cannot
            # diverge.
            off = mate.axial_offset_mm or 0.0
            fp_shifted = shift_pt(fp, axis_dir, off)
            inside = all(
                mbmin[k] - tol <= fp_shifted[k] <= mbmax[k] + tol
                for k in range(3)
            )
            chk.measured_mm = 0.0
            chk.delta_mm = 0.0 if inside else tol * 2
            chk.passed = inside
            chk.detail = (
                f"pivot ({fp_shifted[0]:.1f},{fp_shifted[1]:.1f},"
                f"{fp_shifted[2]:.1f}) [fixed {fp[0]:.1f},{fp[1]:.1f},"
                f"{fp[2]:.1f} + axial_offset {off:.1f}] "
                f"{'inside' if inside else 'OUTSIDE'} moving bbox"
            )
            checks.append(chk)
            continue

        # Moving anchor: SELECTOR resolves in the part's LOCAL coords, which
        # are stale after placement -- only usable for geometry-shape checks,
        # not world-position deltas.
        mp_status, mp_sel = _resolve_qa_point_3state(
            mate.moving_anchor, step_paths.get(mate.moving_part_id)
        )
        mp_local = (mp_status == "found")
        if mp_status == "found":
            mp = mp_sel
        elif mp_status == "not_found":
            # SELECTOR probe found NO matching face on the moving part.
            # Same fail-closed rule as the fixed side (BUG-002 P0
            # revision): do NOT fall through to bbox algebra.
            #
            # P1-2: canonical format so the router's
            # _SELECTOR_MISS_RE can parse part_id + query.
            _mp_sq = mate.moving_anchor.selector_query
            _mp_sq_dict = (
                _mp_sq.model_dump(mode="json", exclude_none=True)
                if _mp_sq is not None else {}
            )
            chk.passed = False
            chk.delta_mm = mate.tolerance_mm * 2
            chk.detail = (
                f"SELECTOR anchor matched no face on "
                f"{mate.moving_part_id}: {_mp_sq_dict!r}"
            )
            checks.append(chk)
            continue
        elif mp_status == "error":
            chk.passed = False
            chk.delta_mm = mate.tolerance_mm * 2
            chk.detail = (
                f"UNVERIFIABLE: moving SELECTOR probe errored "
                f"(infrastructure fault) on {mate.moving_part_id}"
            )
            checks.append(chk)
            continue
        elif mate.moving_anchor.kind == AnchorKind.SPHERE:
            # SPHERE moving anchor (ball mate's ball side): sphere_center_mm
            # is part-LOCAL; transform through the placed location or QA
            # reads it as a world point and reports a false delta. The
            # connect() call did translate the part — we must mirror that
            # transform here, otherwise every ball mate fails with a
            # delta equal to the intended translation.
            sc = mate.moving_anchor.sphere_center_mm or [0.0, 0.0, 0.0]
            mp = _local_to_world_point(
                (float(sc[0]), float(sc[1]), float(sc[2])),
                locations.get(mate.moving_part_id),
                step_paths.get(mate.moving_part_id),
                bboxes[mate.moving_part_id],
            )
        else:
            # FACE / AXIS_POINT moving anchor. The moving part is always in
            # moved_set (it was just placed by this mate), so derive the
            # datum on the ORIGINAL bbox and transform through the placed
            # Location -- the placed-bbox face of a rotated part is a
            # different physical face than the anchor names (P1-6).
            mp, _mp_dir, mp_err = _anchor_world_datum(
                mate.moving_anchor, mate.moving_part_id, bboxes,
                step_paths, locations, moved_set,
            )
            if mp is None:
                chk.passed = False
                chk.delta_mm = mate.tolerance_mm * 2
                chk.detail = f"UNVERIFIABLE: {mp_err}"
                checks.append(chk)
                continue
        # Rigid translation_mm is expressed in the fixed part's LOCAL
        # frame. Mirror codegen by rotating that basis through the fixed
        # part's placed transform before comparing world-space datums.
        if mate.mate_type == MateType.RIGID and any(
            abs(float(v)) > 1e-12 for v in mate.translation_mm
        ):
            basis = []
            for axis in (
                (1.0, 0.0, 0.0),
                (0.0, 1.0, 0.0),
                (0.0, 0.0, 1.0),
            ):
                world_axis = axis
                if mate.fixed_part_id in moved_set:
                    world_axis = _local_to_world_direction(
                        axis, locations.get(mate.fixed_part_id)
                    )
                if world_axis is None:
                    # BUG-042: the previous code fell back to the
                    # UNROTATED local axis here -- silently mis-measuring
                    # the world translation on a rotated part and
                    # potentially false-passing. Fail closed instead:
                    # mark the mate UNVERIFIABLE so the router can remate.
                    chk.passed = False
                    chk.delta_mm = mate.tolerance_mm * 2
                    chk.detail = (
                        f"UNVERIFIABLE: rigid translation_mm basis axis "
                        f"{axis} could not be transformed into a world "
                        f"direction (degenerate transform through "
                        f"{mate.fixed_part_id}'s placed location) -- "
                        f"refusing to fall back to the unrotated local "
                        f"axis and risk a false PASS"
                    )
                    checks.append(chk)
                    # Skip the rest of this mate's checks.
                    basis = None
                    break
                basis.append(world_axis)
            if basis is None:
                continue
            tv = tuple(float(v) for v in mate.translation_mm)
            fp = tuple(
                fp[k] + sum(tv[j] * basis[j][k] for j in range(3))
                for k in range(3)
            )
        dx, dy, dz = mp[0] - fp[0], mp[1] - fp[1], mp[2] - fp[2]

        if mate.mate_type == MateType.FACE_TO_FACE:
            # Lateral offset is only re-derivable from world anchors; a
            # SELECTOR moving anchor is local (post-placement stale).
            lateral = 0.0 if mp_local else math.hypot(dx, dy)
            lat_src = "n/a (selector anchor)" if mp_local else f"{lateral:.3f}mm"
            # Prefer the REAL topology gap (min surface distance) over the
            # bbox-derived dz: it stays correct when a part's bbox extreme is
            # not the mating face (knob on a lid, skirt below a cap).
            real_gap = None
            if meshes_by_label is not None:
                real_gap = _min_gap_between(
                    meshes_by_label.get(mate.fixed_part_id, []),
                    meshes_by_label.get(mate.moving_part_id, []),
                )
            if real_gap is None and mp_local:
                # Unverifiable combination (D8): the only remaining gap
                # source is abs(dz), but dz mixes a part-LOCAL selector
                # point (mp, stale after placement -- see above) with a
                # WORLD fixed point (fp), a physically meaningless number
                # that would pass/fail at random. Refuse to fabricate a
                # measurement; fail loudly with an explicit reason. The
                # synthetic delta follows the pivot check's tol*2
                # convention for "failed without a real measurement".
                chk.measured_mm = 0.0
                chk.delta_mm = mate.tolerance_mm * 2
                chk.passed = False
                chk.detail = (
                    "UNVERIFIABLE: face_to_face gap needs the topology "
                    "measurement, but no meshes are available for these "
                    "parts, and the SELECTOR moving anchor resolves to a "
                    "part-local point that cannot be compared against the "
                    "world fixed anchor (the bbox dz fallback would be a "
                    "meaningless local-vs-world difference)"
                )
                checks.append(chk)
                continue
            gap_meas = real_gap if real_gap is not None else abs(dz)
            gap_src = "topology" if real_gap is not None else "bbox"
            axial_delta = gap_meas - mate.offset_mm
            chk.measured_mm = gap_meas
            chk.delta_mm = max(abs(axial_delta), lateral)
            chk.passed = chk.delta_mm <= mate.tolerance_mm
            chk.detail = (
                f"gap={gap_meas:.3f}mm [{gap_src}] (expected {mate.offset_mm:.3f}), "
                f"lateral={lat_src}"
            )
        else:
            if mp_local:
                # Rigid datum with a SELECTOR moving anchor. The joint
                # (connect aligns the resolved real datum) enforces world
                # coincidence, so the local point cannot be re-derived as
                # a world position. But we CAN verify the resolver picked
                # the intended datum: if the user stated target_x/y/z_mm in
                # the selector_query (disambiguating multi-bore parts --
                # e.g. a link_bar's +X vs -X bore, or a clevis twin), the
                # resolved part-local point must match the stated target
                # within tolerance. Without this, a wrong-bore pick
                # (target=+26, resolver returns -26) goes undetected --
                # the joint still aligns SOMETHING, just not the intended
                # datum, and the moving part lands in the wrong place.
                #
                # BUG-002 (P0 revision): probe BOTH fixed and moving
                # SELECTOR anchors with their OWN selector_query. The
                # previous code re-used the MOVING anchor's query for
                # BOTH probes -- so when the moving query had a target
                # (entering the per-axis target check branch below),
                # the fixed-side uniqueness was never checked, silently
                # passing a multi-bore fixed part. Now: each side is
                # probed independently, regardless of the other side's
                # target state.
                fixed_step = step_paths.get(mate.fixed_part_id)
                moving_step = step_paths.get(mate.moving_part_id)

                def _probe_anchor_uniqueness(
                    anchor, step_path: str | None,
                    anchor_label: str, part_id: str,
                ) -> tuple[bool, str]:
                    """Probe a SELECTOR anchor with its OWN query.

                    Returns (passed, detail).

                    P0-1 requirement 6: even with a target, candidate_count
                    must be 1 for the mate to pass. A target_z_mm alone
                    on two parallel Z-axis bores cannot disambiguate
                    (both clusters' along extents contain z) -- the count
                    stays >= 2 -> fail.

                    P1-2 requirement 7: selector-miss detail uses the
                    canonical ``SELECTOR anchor matched no face on
                    <part_id>: <query_dict_repr>`` format so the router's
                    ``_SELECTOR_MISS_RE`` can parse part_id + query.
                    """
                    if anchor.kind != AnchorKind.SELECTOR:
                        return True, (
                            f"{anchor_label} non-SELECTOR anchor "
                            f"(no uniqueness probe)"
                        )
                    sq = anchor.selector_query
                    if sq is None:
                        return False, (
                            f"UNVERIFIABLE: {anchor_label} SELECTOR "
                            f"has no selector_query on {part_id}"
                        )
                    if not step_path:
                        return False, (
                            f"UNVERIFIABLE: {anchor_label} SELECTOR "
                            f"anchor has no step_path on {part_id}"
                        )
                    sq_dict = sq.model_dump(
                        mode="json", exclude_none=True
                    )
                    try:
                        status, result = _probe_selector_anchor(
                            step_path, sq_dict
                        )
                    except Exception:  # noqa: BLE001
                        return False, (
                            f"UNVERIFIABLE: {anchor_label} SELECTOR "
                            f"probe errored (infrastructure fault) "
                            f"on {part_id}"
                        )
                    if status == "error":
                        return False, (
                            f"UNVERIFIABLE: {anchor_label} SELECTOR "
                            f"probe errored (infrastructure fault) "
                            f"on {part_id}"
                        )
                    if status == "not_found" or result is None:
                        # Selector-miss: canonical format so the router's
                        # _SELECTOR_MISS_RE can parse part_id + query and
                        # attribute via _attribute_selector_miss (spec-
                        # promised -> remodel, plan hallucinated -> remate).
                        return False, (
                            f"SELECTOR anchor matched no face on "
                            f"{part_id}: {sq_dict!r}"
                        )
                    if status != "found":
                        return False, (
                            f"UNVERIFIABLE: {anchor_label} SELECTOR "
                            f"probe returned {status!r} on {part_id}"
                        )
                    cand = int(result.get("candidate_count") or 0)
                    if cand == 0:
                        # Defensive: status=found but count=0.
                        return False, (
                            f"SELECTOR anchor matched no face on "
                            f"{part_id}: {sq_dict!r}"
                        )
                    has_target = (
                        sq.target_x_mm is not None
                        or sq.target_y_mm is not None
                        or sq.target_z_mm is not None
                    )
                    if cand == 1:
                        if has_target:
                            return True, (
                                f"{anchor_label} selector-anchored "
                                f"(target-disambiguated, unique match) "
                                f"on {part_id}"
                            )
                        return True, (
                            f"{anchor_label} selector-anchored rigid "
                            f"datum (unique candidate, no target to "
                            f"verify) on {part_id}"
                        )
                    # cand > 1: target did not actually disambiguate
                    # (e.g. target_z_mm on two parallel Z-axis bores)
                    # OR no target at all. Either way, ambiguous.
                    if has_target:
                        return False, (
                            f"UNVERIFIABLE: {anchor_label} SELECTOR "
                            f"has {cand} candidate faces on {part_id} "
                            f"despite target -- target does not "
                            f"disambiguate (lateral vs along-axis "
                            f"mismatch); router should remate with a "
                            f"lateral target"
                        )
                    return False, (
                        f"UNVERIFIABLE: {anchor_label} SELECTOR has "
                        f"{cand} candidate faces on {part_id} but no "
                        f"target_x/y/z_mm to disambiguate -- "
                        f"router should remate with a target"
                    )

                # Probe BOTH fixed and moving anchors with their OWN
                # queries -- independent of whether either has a target.
                fix_pass, fix_detail = _probe_anchor_uniqueness(
                    mate.fixed_anchor, fixed_step,
                    "fixed", mate.fixed_part_id,
                )
                if not fix_pass:
                    chk.measured_mm = mate.tolerance_mm * 2
                    chk.delta_mm = mate.tolerance_mm * 2
                    chk.passed = False
                    chk.detail = fix_detail
                    checks.append(chk)
                    continue
                mov_pass, mov_detail = _probe_anchor_uniqueness(
                    mate.moving_anchor, moving_step,
                    "moving", mate.moving_part_id,
                )
                if not mov_pass:
                    chk.measured_mm = mate.tolerance_mm * 2
                    chk.delta_mm = mate.tolerance_mm * 2
                    chk.passed = False
                    chk.detail = mov_detail
                    checks.append(chk)
                    continue

                # Per-axis target check on the moving anchor (if it
                # specified target_x/y/z_mm). The resolved point is the
                # chosen cylinder's bbox midpoint = bore centre (modulo
                # numerical precision); tol = max(tolerance_mm, 0.5) is
                # generous. A wrong-bore pick is typically off by tens
                # of mm, so this is a strong signal.
                sq = mate.moving_anchor.selector_query
                targets: list[tuple[str, int, float]] = []
                if sq is not None:
                    if sq.target_x_mm is not None:
                        targets.append(("x", 0, float(sq.target_x_mm)))
                    if sq.target_y_mm is not None:
                        targets.append(("y", 1, float(sq.target_y_mm)))
                    if sq.target_z_mm is not None:
                        targets.append(("z", 2, float(sq.target_z_mm)))
                if not targets:
                    # Both unique, no target to verify -> joint-enforced
                    # PASS.
                    chk.measured_mm = 0.0
                    chk.delta_mm = 0.0
                    chk.passed = True
                    chk.detail = (
                        "selector-anchored rigid datum (joint-enforced; "
                        "unique candidates on both fixed and moving, "
                        "no target to verify)"
                    )
                    checks.append(chk)
                    continue
                tol = max(mate.tolerance_mm, 0.5)
                worst_axis, worst_delta = "", 0.0
                for axis_name, idx, target_val in targets:
                    delta = abs(mp_sel[idx] - target_val)
                    if delta > worst_delta:
                        worst_axis, worst_delta = axis_name, delta
                chk.measured_mm = worst_delta
                chk.delta_mm = worst_delta
                chk.passed = worst_delta <= tol
                target_str = ", ".join(f"{n}={v}" for n, _, v in targets)
                chk.detail = (
                    f"selector-anchored rigid datum target check: "
                    f"resolved ({mp_sel[0]:.2f},{mp_sel[1]:.2f},"
                    f"{mp_sel[2]:.2f}) vs target [{target_str}]; "
                    f"worst axis {worst_axis} delta={worst_delta:.3f}mm "
                    f"(tol {tol:.3f}mm)"
                )
                checks.append(chk)
                continue
            dist = math.sqrt(dx * dx + dy * dy + dz * dz)
            chk.measured_mm = dist
            chk.delta_mm = dist
            chk.passed = dist <= mate.tolerance_mm
            chk.detail = (
                f"datum coincidence dist={dist:.3f}mm "
                f"(delta=({dx:.2f},{dy:.2f},{dz:.2f}))"
            )
        checks.append(chk)
    return checks


def _sample_surface(mesh, n: int = 300):
    import numpy as np
    import trimesh

    try:
        pts, _face_idx = trimesh.sample.sample_surface(mesh, n)
        return np.asarray(pts)
    except Exception:  # noqa: BLE001 - fall back to vertices
        return np.asarray(mesh.vertices)


def _load_components(assembly_dir: Path, placed):
    """Load per-part placed meshes, labelled.

    Preferred source: the ``placed_stl/<label>.stl`` files exported by the
    assembly script (exact-contact assemblies merge into one shell in the
    compound STL, making any split unreliable -- including trimesh's
    networkx fallback on the resulting non-watertight mesh). Fallback:
    split the compound STL and match components to labels
    containment-first.
    """
    import trimesh

    per_part_dir = assembly_dir / "placed_stl"
    components = []
    if per_part_dir.is_dir():
        for stl in sorted(per_part_dir.glob("*.stl")):
            try:
                mesh = trimesh.load(str(stl), force="mesh")
            except Exception:  # noqa: BLE001
                continue
            if len(getattr(mesh, "faces", [])):
                components.append((stl.stem, mesh))
        if components:
            return components

    whole = trimesh.load(str(assembly_dir / "assembly_output.stl"), force="mesh")
    shells = whole.split(only_watertight=False)
    bboxes = [(l, bmin, bmax) for l, bmin, bmax in placed if not l.startswith("unmatched_")]

    def contains(bmin, bmax, cb, tol=0.5):
        return all(
            cb[0][k] >= bmin[k] - tol and cb[1][k] <= bmax[k] + tol for k in range(3)
        )

    def centre_dist(bmin, bmax, c):
        p = ((bmin[0] + bmax[0]) / 2.0, (bmin[1] + bmax[1]) / 2.0, (bmin[2] + bmax[2]) / 2.0)
        return sum((p[k] - c[k]) ** 2 for k in range(3))

    comp_labels = []
    for comp in shells:
        cb = comp.bounds
        centre = (cb[0] + cb[1]) / 2.0
        candidates = [l for l, bmin, bmax in bboxes if contains(bmin, bmax, cb)]
        pool = candidates or [l for l, _bmin, _bmax in bboxes]
        best = min(pool, key=lambda l: centre_dist(
            *next((bmin, bmax) for ll, bmin, bmax in bboxes if ll == l), centre
        )) if pool else "unknown"
        comp_labels.append((best, comp))
    return comp_labels


def _deep_penetration_frac(outer, pts, depth_tol: float) -> float:
    """Fraction of ``pts`` that lie inside ``outer`` by more than depth_tol.

    ``outer.contains`` (ray parity) excludes boundary points, so parts
    merely touching face-to-face produce 0 -- contact is not interference.
    Points strictly inside are then measured for depth via closest_point;
    only penetration deeper than ``depth_tol`` counts (shallow STL-tessellation
    slivers are ignored).
    """
    import numpy as np

    inside = np.asarray(outer.contains(pts))
    if not inside.any():
        return 0.0
    try:
        # Robust helper: naive fallback when rtree is missing (otherwise
        # every inside point counts as "deep" -- false interference).
        _cp, dist, _fid = closest_point_robust(outer, pts[inside])
        deep = int((np.asarray(dist) > depth_tol).sum())
    except Exception:  # noqa: BLE001 - depth unavailable; count all inside
        deep = int(inside.sum())
    return deep / float(len(pts))


def _min_gap_between(meshes_a, meshes_b, n_samples: int = 400) -> float | None:
    """Minimum surface-to-surface distance between two part meshes (real
    topology, not bbox). Returns the clearance when separated, ~0 when
    touching, and 0 (clamped) when overlapping. None if it cannot be
    computed. This is the authoritative "functional gap" for face_to_face
    mates and solves bbox != mating-face cases (a knob raises the bbox top
    but the real gap is measured between the actual surfaces)."""
    import numpy as np
    import trimesh

    try:
        pts_a = np.vstack([_sample_surface(m, n_samples) for m in meshes_a])
        pts_b = np.vstack([_sample_surface(m, n_samples) for m in meshes_b])
        best = float("inf")
        for outer, pts in ((meshes_b, pts_a), (meshes_a, pts_b)):
            # distance from each sampled point to the nearest point on outer
            merged = trimesh.util.concatenate(*outer) if len(outer) > 1 else outer[0]
            # Robust helper: naive fallback when rtree is missing (otherwise
            # the real-topology gap silently degrades to None -> bbox dz).
            _cp, dist, _fid = closest_point_robust(merged, pts)
            if len(dist):
                best = min(best, float(np.asarray(dist).min()))
        return None if best == float("inf") else max(0.0, best)
    except Exception:  # noqa: BLE001
        return None


def _pair_collides(mesh_a, mesh_b) -> tuple[bool | None, str, float]:
    """Bidirectional penetration test with a DEPTH tolerance.

    Contact/touching parts (face_to_face mates, a block resting on a rail)
    are NOT interference: ``contains`` excludes boundary points and only
    penetration deeper than ``INTERFERENCE_DEPTH_TOL_MM`` counts.

    Three-state result (BUG-004):
      * ``True``  -- collision detected (penetration exceeds tolerance)
      * ``False`` -- verified no collision (sampling + contains + depth all
        ran cleanly and produced a below-threshold result)
      * ``None``  -- UNVERIFIABLE: an internal trimesh/numpy failure
        prevented the probe from running. Callers MUST treat None as
        ``passed=False`` with an UNVERIFIABLE detail; silently promoting
        None to "no collision" was a false PASS.
    """
    depth_tol = cfg.INTERFERENCE_DEPTH_TOL_MM
    # Zero-volume / non-finite mesh guard (BUG-016): the volume-based
    # penetration proxy ``vol = frac * min(|Va|, |Vb|)`` collapses to 0
    # when either mesh has no enclosed volume (flat plates, sheet metal,
    # single-triangle STLs). ``vol <= tol`` is then trivially true and the
    # pair false-passes even when a zero-volume plate clearly crosses a
    # closed box. NaN/inf volume (from a malformed mesh where trimesh's
    # ``center_mass = integrated[1:4] / volume`` divides by zero, or from a
    # property that raises on a degenerate shell) bypasses the ``<= eps``
    # comparison entirely (``nan <= x`` is False) and the proxy propagates
    # NaN, producing a nonsense collision verdict. Return UNVERIFIABLE --
    # the volume proxy is degenerate for this geometry, and surface
    # sampling alone is not an authoritative collision test. The try wraps
    # the property access: ``getattr`` only defaults when the attribute is
    # ABSENT, not when the property RAISES, so a raising ``volume``
    # property would escape the function and crash the QA pipeline.
    try:
        vol_a = float(abs(getattr(mesh_a, "volume", 0.0)))
        vol_b = float(abs(getattr(mesh_b, "volume", 0.0)))
    except Exception:  # noqa: BLE001 - volume property raised (malformed mesh)
        return (
            None,
            "UNVERIFIABLE (mesh.volume property raised) -- cannot "
            "establish a finite volume for the collision probe",
            0.0,
        )
    bad_a = (not math.isfinite(vol_a)) or vol_a <= _ZERO_VOLUME_EPS_MM3
    bad_b = (not math.isfinite(vol_b)) or vol_b <= _ZERO_VOLUME_EPS_MM3
    if bad_a or bad_b:
        if bad_a and bad_b:
            zero_side = "both meshes"
        elif bad_a:
            zero_side = "mesh_a"
        else:
            zero_side = "mesh_b"
        return (
            None,
            f"UNVERIFIABLE (zero-volume {zero_side}: vol_a={vol_a:.3e}, "
            f"vol_b={vol_b:.3e}) -- volume-based penetration proxy is "
            f"degenerate; surface sampling alone is not authoritative",
            0.0,
        )
    try:
        pts_a = _sample_surface(mesh_a)
        pts_b = _sample_surface(mesh_b)
        frac = max(
            _deep_penetration_frac(mesh_b, pts_a, depth_tol),
            _deep_penetration_frac(mesh_a, pts_b, depth_tol),
        )
    except Exception as exc:  # noqa: BLE001 - probe failed; do NOT false-pass
        return None, f"UNVERIFIABLE (sampling/contains/depth): {type(exc).__name__}: {exc}", 0.0
    vol = frac * float(min(abs(mesh_a.volume), abs(mesh_b.volume)))
    passed = (
        frac <= cfg.INTERFERENCE_VERTEX_FRACTION
        and vol <= cfg.INTERFERENCE_VOLUME_TOL_MM3
    )
    return (not passed), (
        f"penetration >{depth_tol}mm: {frac * 100:.1f}% (vol proxy {vol:.1f}mm3)"
    ), vol


def _check_interference(comp_labels, mates=None) -> list[InterferenceCheck]:
    """Pairwise overlap between placed parts (depth-tolerant, trimesh).

    Also reports the real min-gap (clearance) for non-colliding pairs so
    the Judge/QA can reason about functional gaps, not just overlaps.
    """
    try:
        import numpy as np  # noqa: F401
        import trimesh  # noqa: F401
    except ImportError:
        return [
            InterferenceCheck(
                part_a="*", part_b="*", passed=True,
                detail="trimesh/numpy unavailable - interference check skipped",
            )
        ]

    if not comp_labels:
        return [InterferenceCheck(part_a="*", part_b="*", passed=False,
                                  detail="assembly STL split into no components")]

    # Directly mated pairs are NOT skipped wholesale: mate delta checks
    # anchor alignment, not volume penetration, so a wrong axial_offset_mm
    # could embed the moving part 50mm into the fixed part and pass mate
    # delta. The depth-tolerant `_pair_collides` already distinguishes
    # legitimate face-to-face contact / press-fit (boundary points are
    # excluded by `contains`, so touching = frac 0) from real penetration
    # beyond INTERFERENCE_DEPTH_TOL_MM. (BUG-003: this blanket skip used
    # to false-pass embedded mates.)
    checks: list[InterferenceCheck] = []
    n = len(comp_labels)
    for i in range(n):
        for j in range(i + 1, n):
            la, ma = comp_labels[i]
            lb, mb = comp_labels[j]
            if la == "unknown" or lb == "unknown" or la == lb:
                continue
            if len(ma.vertices) == 0 or len(mb.vertices) == 0:
                continue
            # AABB broad-phase: disjoint bounding boxes cannot contain
            # penetrating meshes, so the expensive surface sampling is
            # skipped. (Exact for the collision test -- AABB overlap is a
            # necessary condition for mesh penetration. The min-gap for
            # far-apart pairs still runs: it is the Judge's clearance
            # evidence and is NOT derivable from the AABB distance.)
            try:
                ba, bb = ma.bounds, mb.bounds
                aabb_disjoint = bool(
                    (ba[1] < bb[0]).any() or (bb[1] < ba[0]).any()
                )
            except Exception:  # noqa: BLE001 - bounds unavailable
                aabb_disjoint = False
            if aabb_disjoint:
                checks.append(InterferenceCheck(
                    part_a=la, part_b=lb, passed=True,
                    detail="no collision (bounding boxes disjoint)",
                    min_gap_mm=None, penetration_volume_mm3=0.0,
                ))
                continue
            collided, detail, vol = _pair_collides(ma, mb)
            # BUG-004: None means the probe could not run (trimesh/numpy
            # failure). Treat as failed -- never silently PASS an
            # unverifiable pair.
            if collided is None:
                checks.append(InterferenceCheck(
                    part_a=la, part_b=lb, passed=False, detail=detail,
                    min_gap_mm=None, penetration_volume_mm3=0.0,
                ))
                continue
            gap = None if collided else _min_gap_between([ma], [mb])
            checks.append(InterferenceCheck(
                part_a=la, part_b=lb, passed=not collided, detail=detail,
                min_gap_mm=gap, penetration_volume_mm3=vol,
            ))
    return checks


# ---------------------------------------------------------------------------
# Kinematic sweep (revolute joints)
# ---------------------------------------------------------------------------

# _AXIS_DIRS comes from geometry_utils.AXIS_DIRS (import alias above).


def _moving_subtree(mates, root_id: str) -> set[str]:
    """Parts rigidly carried by ``root_id`` (BFS through mate graph)."""
    subtree = {root_id}
    frontier = [root_id]
    while frontier:
        cur = frontier.pop()
        for m in mates:
            if m.fixed_part_id == cur and m.moving_part_id not in subtree:
                subtree.add(m.moving_part_id)
                frontier.append(m.moving_part_id)
    return subtree


def _check_kinematics(
    placed, mates, meshes_by_label, step_paths=None, locations=None
) -> list[KinematicCheck]:
    """Sweep every motion joint's moving subtree and test collisions
    against the static structure at each sample pose.

    * REVOLUTE     -- rotate the subtree about the joint axis over
      +/- ``KINEMATIC_SWEEP_DEG``.
    * LINEAR -- use explicit joint limits when available; otherwise sweep
      over +/- ``LINEAR_SWEEP_MM``. CYLINDRICAL uses the fallback sweep.

    A part may export as several connected shells (B5): every shell of
    the moving subtree moves together and every shell of the static
    structure is tested. The mesh is already AT the static pose, so each
    sample applies a relative transform on top of it.
    """
    if not cfg.KINEMATIC_ENABLED:
        return []
    step_paths = step_paths or {}
    locations = locations or {}
    moved_set = {m.moving_part_id for m in mates}
    motion = [
        m for m in mates
        if m.mate_type in (MateType.REVOLUTE, MateType.LINEAR, MateType.CYLINDRICAL)
    ]
    if not motion:
        return []

    import numpy as np
    import trimesh

    bboxes = {label: (bmin, bmax) for label, bmin, bmax in placed}

    checks: list[KinematicCheck] = []
    for mate in motion:
        rotational = mate.mate_type == MateType.REVOLUTE
        if rotational:
            samples = np.linspace(
                -cfg.KINEMATIC_SWEEP_DEG, cfg.KINEMATIC_SWEEP_DEG,
                cfg.KINEMATIC_SWEEP_SAMPLES,
            )
            unit = "deg"
        else:
            lower = mate.limit_lower if mate.mate_type == MateType.LINEAR else None
            upper = mate.limit_upper if mate.mate_type == MateType.LINEAR else None
            if lower is not None and upper is not None:
                samples = np.unique(np.concatenate((
                    np.linspace(lower, upper, cfg.LINEAR_SWEEP_SAMPLES),
                    np.array([0.0]),
                )))
            else:
                samples = np.linspace(
                    -cfg.LINEAR_SWEEP_MM, cfg.LINEAR_SWEEP_MM,
                    cfg.LINEAR_SWEEP_SAMPLES,
                )
            unit = "mm"

        chk = KinematicCheck(
            mate_id=mate.mate_id,
            # `sweep_deg` kept (legacy QA JSON compat) but its name is only
            # accurate for revolute; `samples` + `sweep_unit` are the
            # authoritative fields for readers (mm for linear/cylindrical).
            sweep_deg=[float(s) for s in samples],
            samples=[float(s) for s in samples],
            sweep_unit=unit,
        )
        if (mate.fixed_part_id not in bboxes or mate.moving_part_id not in bboxes
                or mate.fixed_part_id not in meshes_by_label
                or mate.moving_part_id not in meshes_by_label):
            chk.passed = False
            chk.detail = "joint parts missing from placed geometry"
            checks.append(chk)
            continue
        # Verifiable-placement gate for the FIXED part: the sweep axis /
        # pivot are derived in its local frame and transformed through its
        # placed Location. A moved fixed part without manifest location
        # data would be swept about a silently guessed world axis -- fail
        # explicitly instead (no approximation is accepted; see
        # _moved_part_location). The moving part's own location is not
        # transformed here (its meshes are swept relative to the static
        # pose), so it is not gated.
        if mate.fixed_part_id in moved_set:
            _loc, _loc_err = _moved_part_location(
                mate.fixed_part_id, locations
            )
            if _loc_err:
                chk.passed = False
                chk.detail = f"UNVERIFIABLE: {_loc_err}"
                checks.append(chk)
                continue

        if mate.mate_type in (MateType.LINEAR, MateType.CYLINDRICAL):
            # slide_axis is the joint axis in the FIXED part's LOCAL frame
            # (codegen emits it as an Axis on the fixed part). When the
            # fixed part was itself moved/rotated by a parent mate, the
            # local principal axis is NOT the world slide direction --
            # transform it through the placed Location (same rotation-only
            # math as the revolute SELECTOR axis) instead of sweeping along
            # an unrotated world axis.
            axis_dir = _AXIS_DIRS[mate.slide_axis or "x"]
            axis_pt = None  # translation sweep needs no pivot point (R4)
            if mate.fixed_part_id in moved_set:
                transformed = _local_to_world_direction(
                    axis_dir, locations.get(mate.fixed_part_id)
                )
                if transformed is None:
                    chk.passed = False
                    chk.detail = (
                        f"slide axis {mate.slide_axis!r} could not be "
                        f"transformed to a world direction (degenerate "
                        f"transform through {mate.fixed_part_id}'s placed "
                        f"location)"
                    )
                    checks.append(chk)
                    continue
                axis_dir = transformed
        else:
            # REVOLUTE / COAXIAL: a SELECTOR fixed anchor carries the axis in
            # selector_query (anchor.axis is None) -- resolve the REAL
            # topology axis point+direction (N2b: sweeping the nominal z
            # instead of the query axis gives wrong collision results), then
            # shift the local point into world coords if the fixed part was
            # itself moved (N1).
            #
            # P1-1: use the 3-state probe so a SELECTOR miss / probe error
            # fails closed (no silent bbox fallback). A Box part with a
            # cylinder SELECTOR query used to fall through to the
            # FACE/AXIS_POINT bbox branch and silently sweep about Z.
            ka_status, ka_result = _resolve_qa_anchor_3state(
                mate.fixed_anchor, step_paths.get(mate.fixed_part_id)
            )
            if ka_status == "found" and ka_result:
                axis_pt = tuple(ka_result["point"])
                ax = tuple(ka_result["axis"])
                axis_dir = ax if sum(abs(v) for v in ax) > 1e-9 else _AXIS_DIRS["z"]
                if mate.fixed_part_id in moved_set:
                    axis_pt = _local_to_world_point(
                        axis_pt,
                        locations.get(mate.fixed_part_id),
                        step_paths.get(mate.fixed_part_id),
                        bboxes[mate.fixed_part_id],
                    )
                    # N1 follow-up: the local axis DIRECTION must be rotated
                    # into world too -- previously only the POINT was
                    # transformed, so a chained joint (parent rotated the
                    # fixed part) swept about the wrong world axis.
                    transformed = _local_to_world_direction(
                        axis_dir, locations.get(mate.fixed_part_id)
                    )
                    if transformed is None:
                        chk.passed = False
                        chk.detail = (
                            f"joint axis direction could not be transformed "
                            f"to a world direction (degenerate transform "
                            f"through {mate.fixed_part_id}'s placed location)"
                        )
                        checks.append(chk)
                        continue
                    axis_dir = transformed
            elif ka_status == "not_found":
                # SELECTOR probe found NO matching face on the fixed part.
                # Fail closed with the selector-miss signature so the
                # router can attribute (spec-promised vs plan
                # hallucinated). Do NOT fall through to bbox algebra --
                # a Box part with a cylinder SELECTOR has no real axis
                # to sweep about.
                sq = mate.fixed_anchor.selector_query
                sq_dict = (
                    sq.model_dump(mode="json", exclude_none=True)
                    if sq is not None else {}
                )
                chk.passed = False
                chk.detail = (
                    f"SELECTOR anchor matched no face on "
                    f"{mate.fixed_part_id}: {sq_dict!r}"
                )
                checks.append(chk)
                continue
            elif ka_status == "error":
                # STEP unreadable / cadpy unavailable / SELECTOR + no
                # step_path / probe exception. UNVERIFIABLE.
                chk.passed = False
                chk.detail = (
                    f"UNVERIFIABLE: fixed SELECTOR probe errored "
                    f"(infrastructure fault) on {mate.fixed_part_id}"
                )
                checks.append(chk)
                continue
            else:
                # no_selector: non-SELECTOR anchor (FACE / AXIS_POINT).
                # Same fallback as the mate-delta check above: FACE anchors
                # carry axis=None, so derive the normal axis via
                # _get_anchor_axis instead of blindly sweeping about Z.
                axis_name = (
                    mate.fixed_anchor.axis
                    or _get_anchor_axis(mate.fixed_anchor)
                    or "z"
                )
                axis_dir = _AXIS_DIRS.get(axis_name, _AXIS_DIRS["z"])
                if mate.fixed_part_id in moved_set:
                    transformed = _local_to_world_direction(
                        axis_dir, locations.get(mate.fixed_part_id)
                    )
                    if transformed is None:
                        chk.passed = False
                        chk.detail = (
                            f"joint axis direction could not be transformed "
                            f"to a world direction (degenerate transform "
                            f"through {mate.fixed_part_id}'s placed location)"
                        )
                        checks.append(chk)
                        continue
                    axis_dir = transformed
                # FACE / AXIS_POINT pivot point: derive on the ORIGINAL bbox
                # and transform through the placed Location for moved parts
                # (P1-6) -- the placed world bbox reads the wrong face off a
                # rotated part.
                axis_pt, _pt_dir, pt_err = _anchor_world_datum(
                    mate.fixed_anchor, mate.fixed_part_id, bboxes,
                    step_paths, locations, moved_set,
                )
                if axis_pt is None:
                    chk.passed = False
                    chk.detail = f"UNVERIFIABLE: {pt_err}"
                    checks.append(chk)
                    continue
        subtree = _moving_subtree(mates, mate.moving_part_id)
        # The moving part itself is checked above, but a chained joint's
        # subtree (A->B->C: sweeping A's joint must carry C too) can contain
        # parts whose placed STL export failed (the codegen footer swallows
        # per-part export errors). meshes_by_label.get(sl, []) would then
        # silently sweep nothing for those parts -- a vacuous pass. Fail
        # the check instead so the router rebuilds the assembly script.
        missing_meshes = [sl for sl in subtree if sl not in meshes_by_label]
        if missing_meshes:
            chk.passed = False
            chk.detail = (
                f"subtree parts missing placed meshes: {missing_meshes} "
                f"(per-part STL export failed) -- refusing to pass a sweep "
                f"that would silently skip them"
            )
            checks.append(chk)
            continue
        statics = [l for l in meshes_by_label if l not in subtree]

        collisions: list[float] = []
        first_detail = ""
        # BUG-003: the joint's own moving/fixed interface must be swept
        # too. The previous blanket skip ("direct mate pair, never a
        # collision") meant a slider ramming its end-stop or a rotating
        # arm sweeping into its own pivot post went undetected. Now we
        # sweep the direct pair, but use baseline comparison.
        #
        # Baseline tracking is per (moving_mesh, static_mesh) PAIR, not
        # per (moving_part, fixed_part) -- one mesh pair's baseline
        # contact must NOT exempt a NEW collision on another mesh pair
        # of the same direct part interface (e.g. a tessellated part
        # exporting as multiple connected components).
        #
        # For each mesh pair we track TWO things:
        #   * baseline_hit (bool): was this pair colliding at sample 0?
        #   * baseline_vol (float): if so, what was the penetration volume?
        #   * baseline_probe_failed (set): the probe at sample 0 crashed.
        #
        # Failure rule (BUG-003 revision):
        #   * If baseline probe failed: UNVERIFIABLE for that mesh pair.
        #   * If baseline_hit is False: ANY hit at ANY sweep sample
        #     (including sample 0 in the sweep, which should be identical
        #     to the baseline probe -- a contradiction indicates either
        #     a probe bug or a real new collision) is a NEW collision
        #     and must FAIL. The volume tolerance does NOT apply here --
        #     a 1.0 mm³ collision in a pair that had no contact at
        #     baseline is a real failure, not "within tolerance".
        #   * If baseline_hit is True: use deepening tolerance. Allow
        #     penetration up to baseline_vol + INTERFERENCE_VOLUME_TOL_MM3;
        #     deeper penetration fails.
        baseline_hit: dict[tuple[int, int], bool] = {}
        baseline_vol: dict[tuple[int, int], float] = {}
        baseline_probe_failed: set[tuple[int, int]] = set()
        # Pre-sample at s=0 to record the designed-contact baseline for
        # the direct pair. Sample 0 is included in the sweep, so we'll
        # also re-test it (idempotent for non-direct pairs).
        try:
            T0 = trimesh.transformations.translation_matrix(
                (0.0, 0.0, 0.0)
            )
        except Exception:  # noqa: BLE001
            T0 = None
        if T0 is not None:
            for moving_mesh in meshes_by_label.get(mate.moving_part_id, []):
                moved0 = moving_mesh.copy()
                # Identity transform -- sample 0 is the initial pose.
                for static_mesh in meshes_by_label.get(
                    mate.fixed_part_id, []
                ):
                    pair_key = (id(moving_mesh), id(static_mesh))
                    try:
                        _hit0, _det0, vol0 = _pair_collides(
                            moved0, static_mesh
                        )
                        if _hit0 is None:
                            # Probe failure at sample 0: cannot establish
                            # baseline. Treat all subsequent collisions as
                            # UNVERIFIABLE rather than baseline-exempt.
                            baseline_probe_failed.add(pair_key)
                        elif _hit0:
                            baseline_hit[pair_key] = True
                            baseline_vol[pair_key] = max(
                                baseline_vol.get(pair_key, 0.0),
                                float(vol0),
                            )
                        else:
                            baseline_hit.setdefault(pair_key, False)
                    except Exception:  # noqa: BLE001
                        baseline_probe_failed.add(pair_key)

        for s in samples:
            if rotational:
                T = trimesh.transformations.rotation_matrix(
                    math.radians(float(s)), axis_dir, axis_pt
                )
            else:
                shift = (
                    axis_dir[0] * float(s),
                    axis_dir[1] * float(s),
                    axis_dir[2] * float(s),
                )
                T = trimesh.transformations.translation_matrix(shift)
            for sl in subtree:
                for moving_mesh in meshes_by_label.get(sl, []):
                    moved = moving_mesh.copy()
                    moved.apply_transform(T)
                    for st in statics:
                        # BUG-003: the joint's own moving/fixed interface
                        # (sl==moving, st==fixed) is NO LONGER skipped.
                        # Designed contact at sample=0 is allowed via the
                        # per-mesh-pair baseline; new or deepened
                        # penetration fails.
                        is_direct_interface = (
                            sl == mate.moving_part_id
                            and st == mate.fixed_part_id
                        )
                        for static_mesh in meshes_by_label[st]:
                            hit, detail, vol = _pair_collides(moved, static_mesh)
                            if hit is None:
                                # BUG-004: probe could not run -- treat as
                                # UNVERIFIABLE failure, not a silent PASS.
                                collisions.append(float(s))
                                first_detail = first_detail or (
                                    f"UNVERIFIABLE at {float(s):+.0f} {unit}: "
                                    f"{sl} vs {st} ({detail})"
                                )
                                break
                            if hit:
                                if is_direct_interface:
                                    pair_key = (
                                        id(moving_mesh), id(static_mesh)
                                    )
                                    if pair_key in baseline_probe_failed:
                                        # Baseline probe failed at sample=0
                                        # for THIS mesh pair -- UNVERIFIABLE.
                                        collisions.append(float(s))
                                        first_detail = first_detail or (
                                            f"UNVERIFIABLE at {float(s):+.0f} "
                                            f"{unit}: {sl} vs {st} "
                                            f"(baseline probe failed for "
                                            f"this mesh pair; {detail})"
                                        )
                                        break
                                    if not baseline_hit.get(pair_key, False):
                                        # No baseline contact for this
                                        # mesh pair at sample=0. ANY hit
                                        # during the sweep is a NEW
                                        # collision -- fail, regardless
                                        # of volume. The volume tolerance
                                        # only applies to deepening of
                                        # existing baseline contact.
                                        collisions.append(float(s))
                                        first_detail = first_detail or (
                                            f"new collision at "
                                            f"{float(s):+.0f} {unit}: "
                                            f"{sl} vs {st} "
                                            f"(vol {vol:.1f}; baseline "
                                            f"had no contact for this "
                                            f"mesh pair; {detail})"
                                        )
                                        break
                                    # baseline_hit is True for this pair:
                                    # allow penetration up to baseline_vol
                                    # + tolerance; deeper = deepened
                                    # collision (fail).
                                    base = baseline_vol.get(pair_key, 0.0)
                                    vol_tol = (
                                        base
                                        + cfg.INTERFERENCE_VOLUME_TOL_MM3
                                    )
                                    if float(vol) <= vol_tol:
                                        # Within deepening tolerance:
                                        # legitimate bearing/slider
                                        # contact, not a sweep collision.
                                        continue
                                    # Penetration deepened beyond
                                    # baseline + tolerance -- fail.
                                    collisions.append(float(s))
                                    first_detail = first_detail or (
                                        f"deepened collision at "
                                        f"{float(s):+.0f} {unit}: "
                                        f"{sl} vs {st} "
                                        f"(vol {vol:.1f} > baseline "
                                        f"{base:.1f} + tol; {detail})"
                                    )
                                    break
                                # Non-direct pair: any collision fails.
                                collisions.append(float(s))
                                first_detail = first_detail or (
                                    f"collision at {float(s):+.0f} {unit}: "
                                    f"{sl} vs {st} ({detail})"
                                )
                                break
                        if collisions and collisions[-1] == float(s):
                            break
                if collisions and collisions[-1] == float(s):
                    break
        chk.collision_deg = collisions
        chk.passed = not collisions
        chk.detail = first_detail or (
            f"swept {samples[0]:+.0f}..{samples[-1]:+.0f} {unit}, "
            f"no collisions vs statics {statics}"
        )
        checks.append(chk)
    return checks


def _check_envelope(brief: AssemblyBrief, placed):
    errors: list[str] = []
    measured: dict[str, float] = {}
    if not brief.overall_envelope_mm or not placed:
        return measured, errors
    # Exclude unmatched shells (tessellation artifacts of a single part
    # exporting as multiple connected components) -- they sit inside the
    # part's real bbox and would otherwise inflate the assembly envelope.
    labeled = [p for p in placed if not p[0].startswith("unmatched_")]
    if not labeled:
        return measured, errors
    xs = [p[1][0] for p in labeled] + [p[2][0] for p in labeled]
    ys = [p[1][1] for p in labeled] + [p[2][1] for p in labeled]
    zs = [p[1][2] for p in labeled] + [p[2][2] for p in labeled]
    measured = {
        "x": max(xs) - min(xs),
        "y": max(ys) - min(ys),
        "z": max(zs) - min(zs),
    }
    for axis in ("x", "y", "z"):
        expected = brief.overall_envelope_mm.get(axis)
        if not expected:
            continue
        tol = expected * cfg.ENVELOPE_TOLERANCE_PCT
        if abs(measured[axis] - expected) > tol:
            errors.append(
                f"envelope {axis}: measured {measured[axis]:.1f}mm vs "
                f"expected {expected:.1f}mm (tol ±{tol:.1f}mm)"
            )
    return measured, errors


def _check_assembled_axis_ranges(brief: AssemblyBrief, placed) -> list[str]:
    """Check optional part world-space bbox targets emitted by Decomposer."""
    errors: list[str] = []
    placed_by_label = {label: (bmin, bmax) for label, bmin, bmax in placed}
    for part in brief.parts:
        dims = part.key_dimensions if isinstance(part.key_dimensions, dict) else {}
        target = dims.get("assembled_axis_ranges")
        if not isinstance(target, dict) or part.part_id not in placed_by_label:
            continue
        bmin, bmax = placed_by_label[part.part_id]
        for i, axis in enumerate(("x", "y", "z")):
            pair = target.get(axis)
            if not (
                isinstance(pair, (list, tuple)) and len(pair) == 2
                and all(isinstance(v, (int, float)) for v in pair)
            ):
                continue
            delta = (float(bmin[i]) - float(pair[0]), float(bmax[i]) - float(pair[1]))
            if max(abs(delta[0]), abs(delta[1])) > 0.5:
                errors.append(
                    f"assembled pose {part.part_id} {axis.upper()} mismatch: "
                    f"measured [{bmin[i]:.2f},{bmax[i]:.2f}] vs target "
                    f"[{float(pair[0]):.2f},{float(pair[1]):.2f}] mm; "
                    f"endpoint deltas [{delta[0]:+.2f},{delta[1]:+.2f}] mm"
                )
    return errors


# ---------------------------------------------------------------------------
# Dimension reconciliation (zero-token pre-assembly gate)
# ---------------------------------------------------------------------------


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


def reconcile_dimensions(brief: AssemblyBrief, mates, part_results: dict) -> ReconcileResult:
    """Re-check the mating plan against MEASURED part geometry.

    The Mating Architect designs anchors from the dimensions the
    Decomposer *claimed*; after PartBuilder the actual bounding boxes
    are free to measure. This gate catches stacking-height drift before
    a doomed assembly+QA cycle is spent (the assembly-layer analogue of
    MAC's stage-boundary normalization: deterministic checks first,
    LLM retries only when they pass).

    Returns a ReconcileResult: the error strings (measured numbers
    embedded so the remate feedback carries ground truth) plus a
    structured attribution the router uses to route part_geometry
    failures to part_builder instead of always remating.
    """
    measured: dict[str, tuple[tuple, tuple]] = {}
    for spec in brief.parts:
        result = part_results.get(spec.part_id)
        if not result or not result.ok or not result.step_path:
            continue
        # _orig_bbox is cached by path+mtime+size, so a re-run reconcile
        # (every assembler iteration) does not re-import the STEP files.
        bb = _orig_bbox(result.step_path)
        if bb is None:
            return ReconcileResult(
                [f"reconcile: cannot load {spec.part_id} STEP "
                 f"({result.step_path})"],
                attribution="part_geometry",
                part_ids=[spec.part_id],
            )
        measured[spec.part_id] = bb

    if len(measured) != len(brief.parts):
        return ReconcileResult([])  # missing parts handled by the part_missing path
    if not mates:
        return ReconcileResult([])

    errors: list[str] = []
    attribution = "ambiguous"
    attribution_ids: list[str] = []

    # --- R6: shaft/bore radius compatibility for axis joints ----------------
    # For each revolute/coaxial/cylindrical mate, collect the real cylinder
    # radii along the joint axis on both parts (cadpy topology) and verify
    # the bore/shaft pairing is mechanically valid. Either orientation is
    # accepted:
    #   (a) fixed=bore + moving=shaft  -- bearing housing + rotating shaft.
    #   (b) fixed=shaft + moving=bore  -- base with pivot post + arm with bore
    #       (the most common finger-palm joint arrangement).
    #   (c) both sides equal radii      -- two bores joined by an implicit pin
    #       (not modeled as a part; kinematically valid even though the pin's
    #       own clearance can't be verified here).
    # Reject exact fit (clearance = 0 -> no rotation room). The previous
    # directional check (mv < f only) rejected (b) and (c), blocking valid
    # multi-joint chains where the unmoved base carries the pivot post. The
    # placed-geometry interference check (trimesh penetration) is the safety
    # net for the rare false-positive (R6 shaft in R5 bore would pass this
    # check as |6-5|=1, but the trimesh check catches the actual collision).
    try:
        from mac_assembly.selector_resolver import (
            cylinder_radii_with_role_along,
            probe_selector_anchor,
        )

        def _selector_cyl_role(anchor, step_path, side_label):
            """Resolve a SELECTOR cylinder anchor to its specific
            (status, role, radius). The previous reconcile_dimensions
            compared ALL cylinders along the joint axis on each part --
            a part with the joint's R5 bore plus an unrelated R6 bore
            would pair the R6 with an R5.5 shaft and false-pass the
            interference. Now: if the anchor has a cylinder SELECTOR
            query, resolve THAT specific cylinder and classify its role.

            Returns (status, role, radius):
              * status="found"     -- role/radius are the resolved
                cylinder's. Compare these specifically.
              * status="not_found" -- the query matched no face on this
                part. This is a SELECTOR miss (geometry absent); the
                caller must FAIL (blocking) -- do NOT fall back to the
                any-pair loop, which would silently PASS.
              * status="error"     -- infrastructure fault. UNVERIFIABLE.
              * status="no_selector" -- this anchor is not a cylinder
                SELECTOR. The caller falls back to the any-pair loop
                for this side (existing behavior).
            """
            if anchor.kind != AnchorKind.SELECTOR:
                return "no_selector", None, None
            sq = anchor.selector_query
            if sq is None or sq.surface != "cylinder":
                return "no_selector", None, None
            if not step_path:
                return "error", None, None
            query = sq.model_dump(mode="json", exclude_none=True)
            try:
                status, result = probe_selector_anchor(step_path, query)
            except Exception:  # noqa: BLE001
                return "error", None, None
            if status == "error":
                return "error", None, None
            if status == "not_found" or result is None:
                return "not_found", None, None
            role = str(result.get("role") or "unknown")
            radius = result.get("radius")
            if radius is None:
                return "error", None, None
            return "found", role, float(radius)

        for m in mates:
            if m.mate_type not in (MateType.REVOLUTE, MateType.COAXIAL,
                                   MateType.CYLINDRICAL):
                continue
            # SELECTOR cylinder's axis lives in selector_query.axis, not
            # anchor.axis (which is None for SELECTOR). Use _get_anchor_axis
            # (handles SELECTOR/AXIS_POINT/FACE) so the R6 radius check
            # queries the real joint axis; falls back to "z" only when
            # neither anchor defines a principal axis.
            axis = (
                m.slide_axis
                or _get_anchor_axis(m.fixed_anchor)
                or _get_anchor_axis(m.moving_anchor)
                or "z"
            )
            fixed_step = part_results[m.fixed_part_id].step_path
            moving_step = part_results[m.moving_part_id].step_path
            # Per-side SELECTOR resolution. If EITHER side has a cylinder
            # SELECTOR, we use the resolved specific cylinder for that
            # side and do NOT fall back to the any-pair loop for it.
            f_status, f_role, f_r = _selector_cyl_role(
                m.fixed_anchor, fixed_step, "fixed"
            )
            mv_status, mv_role, mv_r = _selector_cyl_role(
                m.moving_anchor, moving_step, "moving"
            )
            # SELECTOR-miss / probe-error handling: only fail if THAT
            # side actually had a cylinder SELECTOR. A non-SELECTOR
            # anchor (no_selector) falls through to the any-pair loop
            # below, preserving the existing behavior for AXIS_POINT /
            # FACE anchors on revolute joints.
            if f_status == "error" or mv_status == "error":
                errors.append(
                    f"reconcile: axis joint {m.mate_id!r} radii "
                    f"UNVERIFIABLE -- SELECTOR probe errored "
                    f"(infrastructure fault) for "
                    f"{('fixed' if f_status == 'error' else 'moving')} "
                    f"side; collision/kinematic QA will arbitrate"
                )
                continue
            if f_status == "not_found":
                # P1-2: do NOT pre-set attribution="part_geometry" here.
                # The selector miss may be a plan hallucination (spec
                # never promised this cylinder) OR a part build defect
                # (spec promised it but the STEP is missing it). The
                # feedback_router's _attribute_selector_miss decides
                # based on the PartSpec text. Emit the canonical
                # selector-miss format so the router's regex can parse
                # part_id + query.
                _f_sq = m.fixed_anchor.selector_query
                _f_sq_dict = (
                    _f_sq.model_dump(mode="json", exclude_none=True)
                    if _f_sq is not None else {}
                )
                errors.append(
                    f"SELECTOR anchor matched no face on "
                    f"{m.fixed_part_id}: {_f_sq_dict!r}"
                )
                continue
            if mv_status == "not_found":
                _mv_sq = m.moving_anchor.selector_query
                _mv_sq_dict = (
                    _mv_sq.model_dump(mode="json", exclude_none=True)
                    if _mv_sq is not None else {}
                )
                errors.append(
                    f"SELECTOR anchor matched no face on "
                    f"{m.moving_part_id}: {_mv_sq_dict!r}"
                )
                continue
            # Build the comparison pairs. If a side has a SELECTOR-resolved
            # cylinder, use ONLY that. Otherwise fall back to ALL
            # cylinders along the axis on that part (existing behavior).
            if f_status == "found":
                fixed_pairs = [(f_r, f_role)]
            else:
                fixed_pairs = cylinder_radii_with_role_along(
                    fixed_step, axis)
            if mv_status == "found":
                moving_pairs = [(mv_r, mv_role)]
            else:
                moving_pairs = cylinder_radii_with_role_along(
                    moving_step, axis)
            if not fixed_pairs or not moving_pairs:
                continue  # no cylinders along that axis on some side: skip
            # BUG-002: role-aware compatibility check. The previous
            # `abs(f - mv) <= 2.0` test was directionless -- an
            # interference fit (shaft larger than bore by 0-2mm) passed
            # as a valid clearance fit. Now we use topology role (inner
            # bore vs outer shaft) to determine direction.
            #
            # Rules per (fixed_r, fixed_role, moving_r, moving_role) pair:
            #   outer+inner (one shaft + one bore): clearance fit requires
            #     bore_r > shaft_r and 0 < bore_r - shaft_r <= 2.0mm.
            #     Exact fit (diff=0) is interference, no rotation room.
            #   inner+inner (both bores): implicit-pin case. Without an
            #     explicit implicit-pin marker on the mate, we cannot
            #     verify this is the intended semantics -- emit
            #     UNVERIFIABLE warning (degraded continue), do NOT
            #     silently PASS.
            #   outer+outer (both shafts): not a valid bearing arrangement
            #     for revolute/coaxial/cylindrical -- DEFINITE incompatible.
            #   any unknown role: UNVERIFIABLE warning (degraded).
            fits = False
            unverifiable = False
            for f_r2, f_role2 in fixed_pairs:
                for mv_r2, mv_role2 in moving_pairs:
                    if f_role2 == "unknown" or mv_role2 == "unknown":
                        unverifiable = True
                        continue
                    if f_role2 == "inner" and mv_role2 == "inner":
                        # Two bores: implicit-pin semantics not encoded on
                        # the mate -- UNVERIFIABLE, not a silent PASS.
                        unverifiable = True
                        continue
                    if f_role2 == "outer" and mv_role2 == "outer":
                        # Two shafts -- never a valid bearing pair.
                        continue
                    # One outer (shaft) + one inner (bore).
                    bore_r = f_r2 if f_role2 == "inner" else mv_r2
                    shaft_r = mv_r2 if mv_role2 == "outer" else f_r2
                    if 0 < (bore_r - shaft_r) <= 2.0:
                        fits = True
                        break
                if fits:
                    break
            if fits:
                continue
            fixed_summary = sorted(
                f"{round(r, 2)}/{role}" for r, role in fixed_pairs
            )
            moving_summary = sorted(
                f"{round(r, 2)}/{role}" for r, role in moving_pairs
            )
            if unverifiable:
                # Role-unknown or implicit-pin-without-explicit-marker:
                # degraded warning, NOT a blocking failure. Downstream
                # collision/kinematic QA is still authoritative.
                errors.append(
                    f"reconcile: axis joint {m.mate_id!r} radii UNVERIFIABLE -- "
                    f"{m.fixed_part_id} R={fixed_summary} "
                    f"vs {m.moving_part_id} R={moving_summary} "
                    f"along {axis}: role unknown or implicit-pin semantics; "
                    f"collision/kinematic QA will arbitrate"
                )
            else:
                errors.append(
                    f"reconcile: axis joint {m.mate_id!r} radii incompatible -- "
                    f"{m.fixed_part_id} R={fixed_summary} "
                    f"vs {m.moving_part_id} R={moving_summary} "
                    f"along {axis}: no compatible bore/shaft pair "
                    f"(need bore > shaft, 0..2mm clearance); check for "
                    f"interference or exact-fit (no rotation clearance)"
                )
                # Definite incompatibility is a PART-GEOMETRY defect
                # (the bores the parts actually carry don't fit), not a
                # mate-placement defect -- remating cannot change
                # measured radii. Only set attribution for the BLOCKING
                # path; UNVERIFIABLE stays on the warning path.
                attribution = "part_geometry"
                for pid in (m.fixed_part_id, m.moving_part_id):
                    if pid not in attribution_ids:
                        attribution_ids.append(pid)
    except Exception:  # noqa: BLE001 - radius check is best-effort
        pass

    # --- vertical stacking height vs envelope z ------------------------------
    env_z = brief.overall_envelope_mm.get("z")
    if not env_z:
        return ReconcileResult(errors, attribution, attribution_ids)
    # Only a purely vertical stacking tree has a predictable z height.
    if any(m.mate_type != MateType.FACE_TO_FACE for m in mates):
        return ReconcileResult(errors, attribution, attribution_ids)

    moved = {m.moving_part_id for m in mates}
    roots = [p.part_id for p in brief.parts if p.part_id not in moved]
    if len(roots) != 1:
        return ReconcileResult(errors, attribution, attribution_ids)

    children: dict[str, list] = {}
    for m in mates:
        children.setdefault(m.fixed_part_id, []).append(m)

    def stack_height(pid: str, seen: set[str]) -> float:
        if pid in seen:
            return 0.0
        seen.add(pid)
        bmin, bmax = measured[pid]
        own = bmax[2] - bmin[2]
        above = 0.0
        for m in children.get(pid, []):
            above = max(above, (m.offset_mm or 0.0) + stack_height(m.moving_part_id, seen))
        return own + above

    predicted_z = stack_height(roots[0], set())
    tol = env_z * cfg.ENVELOPE_TOLERANCE_PCT
    if abs(predicted_z - env_z) > tol:
        breakdown = " + ".join(
            f"{p.part_id}={measured[p.part_id][1][2] - measured[p.part_id][0][2]:.1f}"
            for p in brief.parts
        )
        gaps = sum(m.offset_mm or 0.0 for m in mates)
        errors.append(
            f"reconcile: measured stacking height {predicted_z:.1f}mm "
            f"({breakdown} + gaps {gaps:.1f}mm) vs envelope z {env_z:.1f}mm "
            f"(tol ±{tol:.1f}mm) -- adjust mates or envelope from MEASURED dims"
        )
    return ReconcileResult(errors, attribution, attribution_ids)


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


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
