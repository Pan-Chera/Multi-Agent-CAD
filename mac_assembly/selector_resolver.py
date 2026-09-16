"""Resolve a semantic SELECTOR anchor to real part topology via cadpy.

The Mating Architect cannot know numeric topology ids at plan time (parts
are not built yet), so a SELECTOR anchor carries a *semantic face query*
(surface type + normal/cylinder axis + selection criterion). After a part is
generated, this module builds the part's cadpy ``SelectorIndex`` and resolves
the query to an actual face, returning its real center / axis plus the cadpy
numeric selector (``f5``, ``o1.f5`` ...) for audit/traceability -- the
"semantic query + numeric audit" hybrid.

This is what closes the bbox != functional-datum loop on the ANCHOR side:
a skirted lid's mating face (the plate bottom, not the bbox bottom under the
skirt) or a knobbed lid's seat is located by real topology, so the mate can
actually be placed correctly, not just measured as wrong.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CADPY_SRC = _REPO_ROOT / "packages" / "cadpy" / "src"

from mac_assembly.geometry_utils import (  # noqa: E402
    AXIS_INDEX as _AXIS_INDEX,
    mesh_containment_available,
)

_lock = threading.Lock()
# Bounded LRU: the mtime+size key grows by one entry per remodel of a part
# (each holding a full SelectorIndex), so an unbounded dict leaks across a
# long session (B17).
_index_cache: "OrderedDict[str, Any]" = OrderedDict()
_INDEX_CACHE_MAX = 32


def _cache_key(step_path: str) -> str:
    """Cache key includes mtime+size so an in-place regenerated part STEP
    (e.g. after an Aider remodel) never resolves against stale topology."""
    import os

    try:
        st = os.stat(step_path)
        return f"{step_path}:{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        return str(step_path)


def _ensure_cadpy_path() -> None:
    import sys

    if str(_CADPY_SRC) not in sys.path:
        sys.path.insert(0, str(_CADPY_SRC))


def _selector_index(step_path: str):
    """Build (and cache) a cadpy SelectorIndex for a part STEP."""
    key = _cache_key(step_path)
    with _lock:
        if key in _index_cache:
            _index_cache.move_to_end(key)
            return _index_cache[key]
    _ensure_cadpy_path()
    from cadpy import lookup
    from cadpy.selector_types import SelectorProfile
    from cadpy.step_scene import extract_selectors_from_scene, load_step_scene

    scene = load_step_scene(Path(step_path))
    bundle = extract_selectors_from_scene(scene, profile=SelectorProfile.REFS)
    index = lookup.build_selector_index(bundle.manifest, buffers=bundle.buffers)
    with _lock:
        _index_cache[key] = (index, lookup)
        _index_cache.move_to_end(key)
        while len(_index_cache) > _INDEX_CACHE_MAX:
            _index_cache.popitem(last=False)
    return index, lookup


def _dominant_axis(vec) -> int:
    ax = [abs(float(v)) for v in vec]
    return ax.index(max(ax))


def probe_selector_anchor(step_path: str, query: dict) -> tuple[str, dict | None]:
    """Three-state resolution of a semantic face query against ``step_path``.

    Returns ``(status, result)`` where status is one of:

      * ``"found"``     -- a face matched; ``result`` is the datum dict below.
      * ``"not_found"`` -- the STEP and cadpy index are OK but NO face
        matches the query: the queried geometry is genuinely absent.
      * ``"error"``     -- the STEP could not be read / cadpy unavailable /
        SelectorIndex build failed. ``result`` is None. This is an
        INFRASTRUCTURE fault, NOT evidence the geometry is absent.

    Splitting ``not_found`` from ``error`` is what lets the feedback router
    attribute a SELECTOR miss correctly: only ``not_found`` may fall through
    to the spec-promise (remodel vs remate) decision, while ``error`` must
    route to a rerun/infrastructure path instead of rebuilding a good part
    or rewriting the mating plan on a cadpy hiccup.

    Result dict (status ``"found"``)::

        {"point": (x, y, z),        # face centre (plane) / point on axis (cyl)
         "axis": (dx, dy, dz),      # plane normal (signed) / cylinder axis
         "selector": "f5",          # cadpy numeric selector (audit)
         "surface": "plane"|"cylinder",
         "radius": float | None,    # cylinder only
         "coordinate": float}       # plane offset along axis / None for cyl
    """
    try:
        index, lookup = _selector_index(step_path)
    except Exception:  # noqa: BLE001 - cadpy unavailable / bad STEP
        return "error", None

    surface = str(query.get("surface", "plane")).lower()
    axis_name = str(query.get("axis", "z")).lower()
    axis_index = _AXIS_INDEX.get(axis_name, 2)
    select = str(query.get("select", "largest")).lower()
    value_mm = query.get("value_mm")
    normal_sign = query.get("normal_sign")

    # Load the part mesh once for both the cluster material-connectivity
    # test and the role classification (BUG-002 P0-1/P0-2 revision).
    mesh = None
    if surface == "cylinder":
        try:
            mesh = _part_mesh(step_path)
        except Exception:  # noqa: BLE001
            mesh = None

    chosen = None
    if surface == "plane":
        chosen = _pick_plane(index, axis_index, normal_sign, select, value_mm)
    elif surface == "cylinder":
        target = (
            query.get("target_x_mm"),
            query.get("target_y_mm"),
            query.get("target_z_mm"),
        )
        chosen = _pick_cylinder(
            index, axis_index, select, value_mm,
            target=target if any(t is not None for t in target) else None,
            mesh=mesh,
        )

    if chosen is None:
        return "not_found", None

    row, point, axis_vec, extra = chosen
    # On a large planar mounting face the target coordinates identify the
    # actual in-plane datum (for example X=-300 on a machine-base top), not
    # merely which face to choose.  Returning the face centre here used to
    # collapse all such children onto one point.
    if surface == "plane":
        point = list(point)
        targets = (
            query.get("target_x_mm"),
            query.get("target_y_mm"),
            query.get("target_z_mm"),
        )
        for i, target in enumerate(targets):
            if i != axis_index and target is not None:
                point[i] = float(target)
    selector_id = str(row.get("id") or "")
    try:
        selector = lookup.display_selector(selector_id, index) or selector_id
    except Exception:  # noqa: BLE001
        selector = selector_id
    result: dict = {
        "point": tuple(float(v) for v in point),
        "axis": tuple(float(v) for v in axis_vec),
        "selector": selector,
        "surface": surface,
        "radius": extra.get("radius"),
        "coordinate": extra.get("coordinate"),
        # BUG-043: candidate count for uniqueness disambiguation. On a
        # multi-bore part, a no-target SELECTOR used to silently pick the
        # first matching cylinder; QA had no signal to flag the
        # ambiguity. With this count, QA can distinguish unique (pass)
        # from multi-candidate (UNVERIFIABLE, route remate).
        #
        # P0-1 revision: the count now reflects the target's ACTUAL
        # disambiguation power. A target_z_mm alone cannot distinguish
        # two parallel Z-axis bores at different XY -- the count stays
        # >= 2. Only target components that select a unique cluster
        # (lateral components disambiguate parallel bores, along-axis
        # disambiguates axial-separated segments) reduce the count.
        "candidate_count": int(extra.get("candidate_count") or 0),
    }
    if surface == "cylinder":
        # Add the mesh-classified role (inner bore / outer shaft /
        # unknown) for the SPECIFIC cylinder this query resolved to.
        # reconcile_dimensions uses this to compare the actual joint
        # feature rather than any compatible cylinder pair on the part
        # (BUG-002 P0 revision): a part with R5 bore (joint feature) +
        # unrelated R6 bore must NOT pass an R5.5 shaft by pairing it
        # with the R6.
        try:
            result["role"] = _classify_cylinder_role_mesh(row, mesh)
        except Exception:  # noqa: BLE001
            result["role"] = "unknown"
    return "found", result


def resolve_selector_anchor(step_path: str, query: dict) -> dict | None:
    """Resolve a semantic face query to real topology on ``step_path``.

    Returns the datum dict on a match, None otherwise (miss OR probe
    error). This is the contract the generated assembly scripts and the
    QA engine consume; attribution code that must tell "geometry absent"
    from "STEP unreadable" calls :func:`probe_selector_anchor` directly.
    """
    _status, result = probe_selector_anchor(step_path, query)
    return result


def _pick_plane(index, axis_index, normal_sign, select, value_mm):
    candidates = []
    for row in index.faces:
        if str(row.get("surfaceType") or "").lower() != "plane":
            continue
        normal = row.get("normal")
        center = row.get("center")
        if not normal or not center:
            continue
        if _dominant_axis(normal) != axis_index:
            continue
        sign = 1 if float(normal[axis_index]) > 0 else -1
        if normal_sign is not None and sign != int(normal_sign):
            continue
        coord = float(center[axis_index])
        area = float(row.get("area") or 0.0)
        candidates.append((row, center, normal, coord, area))
    if not candidates:
        return None
    if select == "closest_to" and value_mm is not None:
        best = min(candidates, key=lambda c: abs(c[3] - float(value_mm)))
        # BUG-043: candidate_count must reflect post-filter survivors --
        # planes whose coordinate is within tolerance of the best match.
        # Other planes on the part (different heights) must NOT inflate
        # the count.
        best_coord = best[3]
        filtered = [c for c in candidates
                    if abs(c[3] - best_coord) <= 0.5]
        semantic_count = len(filtered)
    else:  # largest area
        best = max(candidates, key=lambda c: c[4])
        # Planes whose area is within 5% of the largest -- "equally
        # reasonable" alternative planes after the largest-area filter.
        best_area = best[4]
        if best_area > 0:
            filtered = [c for c in candidates
                        if c[4] >= best_area * 0.95]
        else:
            filtered = list(candidates)
        semantic_count = len(filtered)
    row, center, normal, coord, _area = best
    return row, center, normal, {
        "coordinate": coord,
        "candidate_count": semantic_count,
    }


def _pick_cylinder(index, axis_index, select, value_mm, target=None, mesh=None):
    """Pick the best-matching logical cylinder from the topology index.

    Builds logical cylinder clusters via :func:`_cylinder_clusters`
    (material-connectivity based), applies the select/value_mm filter,
    then picks the cluster whose axis line best matches the target (if
    given) or the first/best cluster otherwise.

    ``target`` (optional) is a (x, y, z) tuple (part-local) where some
    components may be None. The target's components disambiguate by:
      * lateral components (perpendicular to the cylinder axis) -- select
        a unique lateral position (e.g. which parallel bore).
      * along-axis component (parallel to the cylinder axis) -- select
        an axial position (e.g. which axial-separated blind hole).
    A target with only along-axis components CANNOT disambiguate parallel
    bores at different lateral positions (P0-1).

    Returns ``(row, point, axis, extra)`` where extra carries the radius
    and candidate_count. The candidate_count reflects the target's ACTUAL
    disambiguation power: the number of clusters matching ALL provided
    target components. A target_z_mm alone on two parallel Z-axis bores
    leaves count>=2 (ambiguous).

    The cluster helper and _semantic_candidate_count SHARE the cluster
    logic (P0-1 requirement 5: pick and count must use the same
    candidates).
    """
    candidates = _cylinder_candidates(index, axis_index)
    if not candidates:
        return None
    clusters = _cylinder_clusters(candidates, axis_index, mesh)
    if not clusters:
        return None
    # Apply select/value_mm filter on clusters (using representative
    # radius from each cluster's first segment).
    pool = _filter_clusters(clusters, select, value_mm)
    if not pool:
        return None
    # Pick the best cluster.
    best_cluster = _pick_best_cluster(pool, select, value_mm, target, axis_index)
    if best_cluster is None:
        return None
    # Use the representative segment for axis + row identity. The point
    # is overridden below with the merged-bbox midpoint (split-face) or
    # the target projection (merged OCCT face spanning multiple bores).
    rep = best_cluster['segments'][0]
    row, rep_point, axis, radius, _area = rep
    # Compute the resolved point.
    point = list(rep_point)
    # (1) For a split-face cluster (multiple segments merged via air
    # gap), override the along-axis coord with the merged-bbox midpoint.
    if len(best_cluster['segments']) > 1:
        point[axis_index] = (best_cluster['along_min'] + best_cluster['along_max']) / 2.0
    # (2) If a target is given and projects onto the cluster's axis line
    # within the merged bbox, use the projection (handles the merged
    # OCCT face case -- one face spanning multiple co-axial bores, target
    # picks one bore's attach point on the axis line).
    if target is not None:
        point = _project_target_onto_axis(
            point, axis, target, best_cluster, axis_index
        )
    point = tuple(point)
    semantic_count = _semantic_candidate_count(
        candidates, select, value_mm, target, axis_index, mesh
    )
    return row, point, axis, {
        "radius": radius,
        "candidate_count": semantic_count,
    }


def _cylinder_candidates(index, axis_index):
    """Collect all cylinder-face candidates along ``axis_index``."""
    candidates = []
    for row in index.faces:
        if str(row.get("surfaceType") or "").lower() != "cylinder":
            continue
        params = row.get("params") or {}
        origin = params.get("origin")
        axis = params.get("axis")
        radius = params.get("radius")
        if origin is None or axis is None:
            continue
        if _dominant_axis(axis) != axis_index:
            continue
        area = float(row.get("area") or 0.0)
        bbox = row.get("bbox") or {}
        try:
            mn = [float(v) for v in bbox.get("min", [0, 0, 0])]
            mx = [float(v) for v in bbox.get("max", [0, 0, 0])]
            point = [(mn[k] + mx[k]) / 2.0 for k in range(3)]
        except Exception:  # noqa: BLE001
            point = [float(v) for v in origin]
        axis_list = [float(v) for v in axis]
        dom = _dominant_axis(axis_list)
        if axis_list[dom] < 0:
            axis_list = [-v for v in axis_list]
        axis_norm = tuple(axis_list)
        candidates.append((row, point, axis_norm, float(radius or 0.0), area))
    return candidates


def _cylinder_clusters(candidates, axis_index, mesh):
    """Group co-axial same-radius segments into logical cylinders via
    material connectivity.

    Two segments belong to the same cluster when:
      * same axis line (parametric origin lateral within 0.5mm),
      * same radius (within 0.5mm),
      * AND the gap between them is AIR (not solid material), tested by
        probing mesh.contains() at the midpoint of the combined along-axis
        extent on the axis line.

    The material test distinguishes:
      * split face (one logical cylinder split by a slot/ear gap into
        multiple OCCT faces -- the gap is air, merge).
      * separate blind holes (two distinct cylinders on the same axis
        line with solid material between them -- do NOT merge).

    When the material test is unavailable (mesh None, rtree missing) or
    fails, treat as ambiguous (do NOT merge) -- conservative: may report
    false ambiguity but never false uniqueness.

    Returns a list of cluster dicts, each with:
      - 'segments': list of (row, point, axis_norm, radius, area)
      - 'lateral': (lat0, lat1) axis-line lateral from parametric origin
      - 'radius': representative radius (first segment's)
      - 'along_min', 'along_max': merged axial bbox extent
      - 'area': total lateral area (sum of segment areas)
    """
    lateral_tol = 0.5
    radius_tol = 0.5
    lateral_idx = [i for i in range(3) if i != axis_index]

    def _origin_lateral(c):
        params = (c[0].get("params") or {})
        origin = params.get("origin")
        if not origin or len(origin) < 3:
            return None
        return tuple(float(origin[i]) for i in lateral_idx)

    def _bbox_along(c):
        bb = c[0].get("bbox") or {}
        mn = bb.get("min") or [float("-inf")] * 3
        mx = bb.get("max") or [float("inf")] * 3
        return float(mn[axis_index]), float(mx[axis_index])

    def _gap_is_air(a_along, b_along, axis_origin, axis_direction):
        """True if the gap between two along-axis intervals on the same
        axis line is air (merge), False if solid (separate), None if
        undetermined."""
        if mesh is None or not mesh_containment_available():
            return None
        a_min, a_max = a_along
        b_min, b_max = b_along
        # Probe the midpoint of the actual gap.  The combined-extent
        # midpoint can lie inside the longer of two unequal blind holes,
        # falsely classifying the solid wall between them as air.
        if a_max < b_min:
            mid_along = (a_max + b_min) / 2.0
        elif b_max < a_min:
            mid_along = (b_max + a_min) / 2.0
        else:
            # Overlapping/touching co-axial same-radius segments have no
            # material gap to classify and represent one logical cylinder.
            return True
        if axis_origin is None or axis_direction is None:
            return None
        denom = float(axis_direction[axis_index])
        if abs(denom) < 1e-9:
            return None
        # Use the real parametric cylinder axis.  A candidate may only be
        # dominant-axis aligned, not exactly parallel to a world axis.
        t = (mid_along - float(axis_origin[axis_index])) / denom
        probe = [
            float(axis_origin[k]) + t * float(axis_direction[k])
            for k in range(3)
        ]
        try:
            import numpy as np
            contained = mesh.contains(np.array([probe]))
            return not bool(contained[0])
        except Exception:  # noqa: BLE001
            return None

    # Sort segments by along_min for deterministic clustering.
    sorted_cands = sorted(candidates, key=_bbox_along)
    clusters = []
    for c in sorted_cands:
        c_lat = _origin_lateral(c)
        c_rad = c[3]
        cmin, cmax = _bbox_along(c)
        clusters.append({
            'segments': [c],
            'lateral': c_lat,
            'radius': c_rad,
            'along_min': cmin,
            'along_max': cmax,
            'area': c[4],
        })
    # Transitive merge: iterate to fixpoint.
    improved = True
    while improved:
        improved = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                ci = clusters[i]
                cj = clusters[j]
                if ci['lateral'] is None or cj['lateral'] is None:
                    continue
                if abs(ci['radius'] - cj['radius']) > radius_tol:
                    continue
                if abs(ci['lateral'][0] - cj['lateral'][0]) > lateral_tol:
                    continue
                if abs(ci['lateral'][1] - cj['lateral'][1]) > lateral_tol:
                    continue
                params = ci['segments'][0][0].get("params") or {}
                gap_air = _gap_is_air(
                    (ci['along_min'], ci['along_max']),
                    (cj['along_min'], cj['along_max']),
                    params.get("origin"),
                    ci['segments'][0][2],
                )
                if gap_air is True:
                    ci['segments'].extend(cj['segments'])
                    ci['along_min'] = min(ci['along_min'], cj['along_min'])
                    ci['along_max'] = max(ci['along_max'], cj['along_max'])
                    ci['area'] += cj['area']
                    clusters.pop(j)
                    improved = True
                    break
            if improved:
                break
    return clusters


def _filter_clusters(clusters, select, value_mm):
    """Apply select/value_mm filter on clusters."""
    if not clusters:
        return []
    if select == "closest_to" and value_mm is not None:
        target_r = float(value_mm)
        best_r = min((cl['radius'] for cl in clusters),
                     key=lambda r: abs(r - target_r))
        pool = [cl for cl in clusters if abs(cl['radius'] - best_r) <= 0.5]
    else:
        best_a = max((cl['area'] for cl in clusters), default=0.0)
        if best_a > 0:
            pool = [cl for cl in clusters if cl['area'] >= best_a * 0.95]
        else:
            pool = list(clusters)
    return pool


def _pick_best_cluster(pool, select, value_mm, target, axis_index):
    """Pick the best cluster from the filtered pool."""
    if not pool:
        return None
    if target is None or not any(t is not None for t in target):
        # No target: pick the largest cluster (largest merged area).
        return max(pool, key=lambda cl: cl['area'])
    # Target given: pick the cluster whose axis line best matches the
    # target's lateral components + whose along extent contains the
    # target's along component.
    lateral_idx = [i for i in range(3) if i != axis_index]
    tx, ty, tz = target
    t_lateral_vals = []
    for k, idx in enumerate(lateral_idx):
        t = (tx, ty, tz)[idx]
        if t is not None:
            t_lateral_vals.append((k, float(t)))
    t_along = (tx, ty, tz)[axis_index]
    t_along_f = float(t_along) if t_along is not None else None

    def _cluster_dist(cl):
        """Distance² from target to cluster's axis line + along penalty."""
        d2 = 0.0
        if cl['lateral'] is None:
            # No origin lateral: use the bbox centre lateral as fallback.
            bb = cl['segments'][0][0].get("bbox") or {}
            mn = bb.get("min") or [0.0, 0.0, 0.0]
            mx = bb.get("max") or [0.0, 0.0, 0.0]
            lat0 = (float(mn[lateral_idx[0]]) + float(mx[lateral_idx[0]])) / 2.0
            lat1 = (float(mn[lateral_idx[1]]) + float(mx[lateral_idx[1]])) / 2.0
            cl_lat = (lat0, lat1)
        else:
            cl_lat = cl['lateral']
        for k, t_val in t_lateral_vals:
            d2 += (cl_lat[k] - t_val) ** 2
        if t_along_f is not None:
            cl_min = cl['along_min']
            cl_max = cl['along_max']
            if t_along_f < cl_min - 0.5:
                d2 += (cl_min - t_along_f) ** 2
            elif t_along_f > cl_max + 0.5:
                d2 += (t_along_f - cl_max) ** 2
        return d2

    return min(pool, key=_cluster_dist)


def _project_target_onto_axis(point, axis, target, cluster, axis_index):
    """Override the resolved point with the target's projection onto the
    chosen cluster's axis line, when the projection lies within the
    cluster's merged bbox. Handles the merged-OCCT-face case (one face
    spanning multiple co-axial bores -- target picks one bore's attach
    point on the axis line)."""
    if target is None:
        return point
    tx, ty, tz = target
    txv = float(tx) if tx is not None else 0.0
    tyv = float(ty) if ty is not None else 0.0
    tzv = float(tz) if tz is not None else 0.0
    ox, oy, oz = point[0], point[1], point[2]
    ax, ay, az = axis
    t = (txv - ox) * ax + (tyv - oy) * ay + (tzv - oz) * az
    proj = [ox + ax * t, oy + ay * t, oz + az * t]
    # Sanity check: projection must lie within the cluster's merged bbox
    # (+ small tolerance). If outside, the target is not on this
    # cluster's axis -- keep the merged-bbox midpoint resolved above.
    tol = 0.5
    bb = cluster['segments'][0][0].get("bbox") or {}
    mn = bb.get("min") or [float("-inf")] * 3
    mx = bb.get("max") or [float("inf")] * 3
    # Use the merged bbox for the along-axis coord; the per-segment bbox
    # for the lateral coords (the cluster's lateral extent is the union
    # of its segments' bboxes).
    for i in range(3):
        if i == axis_index:
            lo, hi = cluster['along_min'], cluster['along_max']
        else:
            lo = float(mn[i]) if i < len(mn) else float("-inf")
            hi = float(mx[i]) if i < len(mx) else float("inf")
        if not (lo - tol <= proj[i] <= hi + tol):
            return point
    return [float(v) for v in proj]


def _semantic_candidate_count(
    candidates, select, value_mm, target, axis_index, mesh,
) -> int:
    """Number of distinct logical candidates matching the query.

    A "logical candidate" is a unique cylinder cluster (after
    material-connectivity based merge of split-face segments). The count
    reflects the target's ACTUAL disambiguation power (P0-1):

      * No target: count = total clusters (post select/value_mm filter).
      * Target with lateral components: only clusters whose axis lateral
        matches the target's lateral components (within 0.5mm) count.
      * Target with along-axis component: only clusters whose along
        extent contains the target's along coord (within tol) count.
      * Target components not provided do NOT contribute to the filter.

    A target_z_mm alone on two parallel Z-axis bores at different XY
    leaves both clusters matching (count=2, ambiguous). A target_x_mm
    selects the unique cluster whose axis lateral matches (count=1).

    The cluster helper and _pick_cylinder SHARE this logic (P0-1
    requirement 5: pick and count must use the same candidates).
    """
    if not candidates:
        return 0
    clusters = _cylinder_clusters(candidates, axis_index, mesh)
    pool = _filter_clusters(clusters, select, value_mm)
    if not pool:
        return 1  # the best match itself
    if target is None or not any(t is not None for t in target):
        return len(pool)
    # Target given: count clusters matching ALL provided target components.
    lateral_idx = [i for i in range(3) if i != axis_index]
    tx, ty, tz = target
    t_lateral_vals = []
    for k, idx in enumerate(lateral_idx):
        t = (tx, ty, tz)[idx]
        if t is not None:
            t_lateral_vals.append((k, float(t)))
    t_along = (tx, ty, tz)[axis_index]
    t_along_f = float(t_along) if t_along is not None else None
    matching = 0
    for cl in pool:
        if cl['lateral'] is None:
            continue
        lateral_ok = True
        for k, t_val in t_lateral_vals:
            if abs(cl['lateral'][k] - t_val) > 0.5:
                lateral_ok = False
                break
        if not lateral_ok:
            continue
        if t_along_f is not None:
            tol = max(cl['radius'], 0.5)
            if not (cl['along_min'] - tol <= t_along_f <= cl['along_max'] + tol):
                continue
        matching += 1
    if matching == 0:
        # Target didn't match any cluster (mis-pointed). The resolver
        # still picks the closest, but the target did NOT disambiguate --
        # geometry exists but the target matches none of it.  Keep this
        # distinct from ambiguity (>1) and a missing surface (not_found).
        return 0
    return matching


def cylinder_radii_along(step_path: str, axis: str) -> list[float]:
    """All cylindrical-face radii on ``step_path`` whose axis is ``axis``.

    Used by the reconcile gate to check shaft/bore radius compatibility
    before an assembly cycle is spent (R6). Empty when cadpy is
    unavailable or the part has no cylinders along that axis.
    """
    return [r for r, _role in cylinder_radii_with_role_along(step_path, axis)]


def cylinder_radii_with_role_along(
    step_path: str, axis: str
) -> list[tuple[float, str]]:
    """Cylindrical-face radii along ``axis`` tagged with topology role.

    Each entry is ``(radius, role)`` where role is one of:

      * ``"outer"`` -- the cylinder face IS the part's outer boundary at
        that radius (a shaft / post / outer cylindrical wall). Material
        lies INSIDE the cylinder (r < radius is solid, r > radius is
        empty).
      * ``"inner"`` -- the cylinder face is the inner surface of a bore /
        hole (material lies OUTSIDE the cylinder: r < radius is empty,
        r > radius is solid).
      * ``"unknown"`` -- classification was inconclusive (mesh export
        failed, BRep point-in-solid test was inconsistent across sampled
        radial directions, or thin-wall geometry defeated the epsilon
        probe). Callers MUST treat unknown as UNVERIFIABLE -- never
        silently PASS.

    Role is determined by mesh point-in-solid (BUG-002): for each cylinder
    face, sample r-epsilon and r+epsilon points along N radial
    directions and test ``mesh.contains()``. r-eps inside + r+eps outside
    = outer; r-eps outside + r+eps inside = inner; inconsistent or
    exception = unknown. This replaces the previous bbox-comparison
    classifier that systematically misclassified external bosses on large
    plates (R5.2 boss on 40x40 plate said 'inner' because 5.2 < 20) and
    thin-wall tubes (inner R5.0 said 'outer' because 5.0 ≈ outer R5.3).
    """
    try:
        index, _lookup = _selector_index(step_path)
    except Exception:  # noqa: BLE001
        return []
    axis_index = _AXIS_INDEX.get(str(axis).lower(), 2)
    mesh = _part_mesh(step_path)
    out: list[tuple[float, str]] = []
    for row in index.faces:
        if str(row.get("surfaceType") or "").lower() != "cylinder":
            continue
        params = row.get("params") or {}
        cyl_axis = params.get("axis")
        radius = params.get("radius")
        if cyl_axis is None or radius is None:
            continue
        if _dominant_axis(cyl_axis) != axis_index:
            continue
        role = _classify_cylinder_role_mesh(row, mesh)
        out.append((float(radius), role))
    return out


# Cached trimesh meshes per part STEP. Bounded LRU so a long session
# (many remodels of one part) does not leak. Key is mtime+size so an
# in-place regeneration always re-exports the STL.
_part_mesh_cache: "OrderedDict[str, Any]" = OrderedDict()
_PART_MESH_CACHE_MAX = 16


def _part_mesh(step_path: str):
    """Load a part STEP as a trimesh mesh (cached). Returns None when
    the STEP cannot be loaded or STL export fails (callers must treat
    None as 'cannot classify' -> unknown)."""
    key = _cache_key(step_path)
    with _lock:
        if key in _part_mesh_cache:
            _part_mesh_cache.move_to_end(key)
            return _part_mesh_cache[key]
    import os
    import tempfile

    try:
        import trimesh
        from build123d import import_step, export_stl

        shape = import_step(step_path)
        with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            export_stl(shape, tmp_path, tolerance=0.05,
                       angular_tolerance=0.3)
            mesh = trimesh.load(tmp_path, force="mesh")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception:  # noqa: BLE001 - mesh unavailable -> unknown
        mesh = None
    with _lock:
        _part_mesh_cache[key] = mesh
        _part_mesh_cache.move_to_end(key)
        while len(_part_mesh_cache) > _PART_MESH_CACHE_MAX:
            _part_mesh_cache.popitem(last=False)
    return mesh


def _classify_cylinder_role_mesh(row: dict, mesh) -> str:
    """Classify a cylinder face via mesh point-in-solid (BUG-002).

    For each of N radial directions sampled around the cylinder axis,
    test r-epsilon and r+epsilon points against the part mesh's solid
    containment. r-eps inside + r+eps outside = outer (shaft); r-eps
    outside + r+eps inside = inner (bore); inconsistent or exception =
    unknown.
    """
    if mesh is None:
        return "unknown"
    import math

    try:
        import numpy as np
    except ImportError:
        return "unknown"

    try:
        params = row.get("params") or {}
        origin = params.get("origin")
        cyl_axis = params.get("axis")
        radius = params.get("radius")
        if origin is None or cyl_axis is None or radius is None:
            return "unknown"
        # Sample at the cylinder's axial midpoint (avoids endcaps where
        # topology might cap the cylinder). Use the face bbox midpoint
        # along the axis -- already computed by the resolver.
        bbox = row.get("bbox") or {}
        mn = bbox.get("min") or [0.0, 0.0, 0.0]
        mx = bbox.get("max") or [0.0, 0.0, 0.0]
        if len(mn) < 3 or len(mx) < 3:
            return "unknown"
        # Axis midpoint = the bbox midpoint projected on the axis line.
        # For a Z-axis cylinder with origin (x0, y0, z0) and axis (0,0,1),
        # the lateral coords are (x0, y0). The along-axis coord is the
        # bbox midpoint (mn[2] + mx[2]) / 2.
        axis_idx = _dominant_axis(cyl_axis)
        lateral_idx = [i for i in range(3) if i != axis_idx]
        # Lateral position of the cylinder axis (x0, y0 for Z-axis).
        ax_origin = [float(origin[i]) for i in range(3)]
        # Build a unit vector for the cylinder axis (normalized).
        ax_vec = [float(cyl_axis[i]) for i in range(3)]
        ax_mag = math.sqrt(sum(c * c for c in ax_vec))
        if ax_mag < 1e-9:
            return "unknown"
        ax_unit = [c / ax_mag for c in ax_vec]
        # Along-axis position: bbox midpoint along the dominant axis.
        along_pos = (float(mn[axis_idx]) + float(mx[axis_idx])) / 2.0
        # The cylinder axis line is: P(t) = origin + t * axis_unit.
        # Find the t that puts the line at along_pos along the dominant
        # axis. For axis=(0,0,1), origin.z=0, along_pos=5: t=5. The axis
        # midpoint is then origin + t * axis_unit = (0,0,5).
        if abs(ax_unit[axis_idx]) < 1e-9:
            return "unknown"
        t = (along_pos - ax_origin[axis_idx]) / ax_unit[axis_idx]
        axis_midpoint = [ax_origin[i] + t * ax_unit[i] for i in range(3)]
        # Build two perpendicular vectors in the lateral plane.
        # Pick any vector NOT parallel to ax_unit, cross to get perp1,
        # cross perp1 with ax_unit to get perp2.
        arbitrary = (1.0, 0.0, 0.0) if abs(ax_unit[0]) < 0.9 else (0.0, 1.0, 0.0)
        # perp1 = arbitrary × ax_unit
        p1 = (
            arbitrary[1] * ax_unit[2] - arbitrary[2] * ax_unit[1],
            arbitrary[2] * ax_unit[0] - arbitrary[0] * ax_unit[2],
            arbitrary[0] * ax_unit[1] - arbitrary[1] * ax_unit[0],
        )
        p1_mag = math.sqrt(sum(c * c for c in p1))
        if p1_mag < 1e-9:
            return "unknown"
        p1u = [c / p1_mag for c in p1]
        # perp2 = ax_unit × perp1
        p2 = (
            ax_unit[1] * p1u[2] - ax_unit[2] * p1u[1],
            ax_unit[2] * p1u[0] - ax_unit[0] * p1u[2],
            ax_unit[0] * p1u[1] - ax_unit[1] * p1u[0],
        )
        p2_mag = math.sqrt(sum(c * c for c in p2))
        if p2_mag < 1e-9:
            return "unknown"
        p2u = [c / p2_mag for c in p2]
        # Sample N radial directions (every 360/N degrees) around the
        # axis. For each, compute r-eps and r+eps points.
        N = 8
        r_eps = 0.1  # below typical wall thickness (>=0.5mm); above
                     # tessellation noise (~0.05mm).
        r_in_pts = []
        r_out_pts = []
        for k in range(N):
            theta = 2.0 * math.pi * k / N
            cos_t = math.cos(theta)
            sin_t = math.sin(theta)
            # Radial unit vector at angle theta (in the perp1/perp2 basis).
            radial = [
                cos_t * p1u[i] + sin_t * p2u[i] for i in range(3)
            ]
            # r-eps point: at radius - epsilon along radial.
            r_in_pts.append([
                axis_midpoint[i] + (float(radius) - r_eps) * radial[i]
                for i in range(3)
            ])
            # r+eps point: at radius + epsilon along radial.
            r_out_pts.append([
                axis_midpoint[i] + (float(radius) + r_eps) * radial[i]
                for i in range(3)
            ])
        # Test all 16 points in one batched mesh.contains() call.
        all_pts = np.array(r_in_pts + r_out_pts, dtype=float)
        contained = mesh.contains(all_pts)
        # First N entries are r-eps, next N are r+eps.
        r_in_inside = int(np.sum(contained[:N]))
        r_out_inside = int(np.sum(contained[N:]))
        # Classify: need majority agreement across radial directions.
        # r-eps inside + r+eps outside = outer (>=6/8 each direction).
        # r-eps outside + r+eps inside = inner.
        threshold = 6  # 75% majority
        if (r_in_inside >= threshold and
                (N - r_out_inside) >= threshold):
            return "outer"
        if ((N - r_in_inside) >= threshold and
                r_out_inside >= threshold):
            return "inner"
        # Mixed signal -- degenerate geometry, near-tangent sample, thin
        # wall that defeated epsilon probe, etc. Do NOT guess.
        return "unknown"
    except Exception:  # noqa: BLE001 - classification must never crash reconcile
        return "unknown"


def query_key(query: dict) -> str:
    """Stable string key for a face query (used in audit logs)."""
    return json.dumps(query, sort_keys=True)
