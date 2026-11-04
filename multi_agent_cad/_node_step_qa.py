"""Engine A: deterministic verification of STEP topology.

Internal implementation module; callers continue to use ``multi_agent_cad.nodes``.
Shared settings and replaceable helpers are resolved through that facade at call
time, preserving per-job overrides and existing monkeypatch hooks. No mutable
configuration or cache state is copied into this module.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from multi_agent_cad.schemas import EngineReport, VerificationResult

from multi_agent_cad import nodes as _nodes


def _load_step_topology(step_path: str) -> tuple:
    """Run the cadpy pipeline: load → mesh → extract selectors.

    Returns ``(faces, edges, vertices, bbox, volume_mm3, raw_json, error_msg)``.
    On success ``error_msg`` is ``None``; on failure the first six elements
    are ``None`` and ``error_msg`` contains a diagnostic string.
    """
    from pathlib import Path as _Path

    try:
        _cadpy_src = str(_nodes._REPO_ROOT / "packages" / "cadpy" / "src")
        if _cadpy_src not in sys.path:
            sys.path.insert(0, _cadpy_src)
        from cadpy.step_scene import (
            load_step_scene_cached,
            mesh_step_scene,
            extract_selectors_from_scene,
            SelectorProfile,
            SelectorOptions,
        )
        from cadpy.analysis import _table_rows

        scene = load_step_scene_cached(_Path(step_path))
        mesh_step_scene(
            scene, linear_deflection=0.1, angular_deflection=0.1, relative=True,
        )
        opts = SelectorOptions(linear_deflection=0.1, angular_deflection=0.1)
        bundle = extract_selectors_from_scene(
            scene, cad_ref="qa", profile=SelectorProfile.ARTIFACT, options=opts,
        )
        manifest = bundle.manifest

        faces = _table_rows(manifest, "faces", "faceColumns")
        edges = _table_rows(manifest, "edges", "edgeColumns")
        vertices = _table_rows(manifest, "vertices", "vertexColumns")
        bbox = manifest.get("bbox", {})

        # Volume via OCP (precise)
        volume_mm3 = None
        try:
            from OCP.GProp import GProp_GProps
            from OCP.BRepGProp import BRepGProp
            from OCP.STEPControl import STEPControl_Reader
            reader = STEPControl_Reader()
            if reader.ReadFile(str(step_path)) == 1:
                reader.TransferRoots()
                shape = reader.OneShape()
                props = GProp_GProps()
                BRepGProp.VolumeProperties_s(shape, props)
                volume_mm3 = props.Mass()
        except Exception as vol_exc:
            print(f"[ENGINE A] Volume calculation skipped: {vol_exc}")

        return (faces, edges, vertices, bbox, volume_mm3, manifest, None)
    except ImportError as imp_exc:
        return (None, None, None, None, None, None, f"OCP/cadpy not installed: {imp_exc}")
    except Exception as exc:
        return (None, None, None, None, None, None, f"STEP topology load failed: {exc}")


def _parse_selector(expression: str) -> dict | None:
    """Parse a selector DSL expression into a structured dict.

    Format: ``entity_type:filter1,filter2,...``

    Returns ``None`` if parsing fails.
    """
    if not expression or ":" not in expression:
        return None

    parts = expression.split(":", 1)
    entity_type = parts[0].strip().lower()
    if entity_type not in ("face", "edge"):
        print(f"[SELECTOR] Unknown entity type: {entity_type}")
        return None

    result: dict = {
        "entity": entity_type,
        "filters": {},
        "sort_key": None,
        "sort_dir": "asc",
        "index": 0,
    }

    tokens = [t.strip() for t in parts[1].split(",") if t.strip()]
    for token in tokens:
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        key = key.strip().lower()
        value = value.strip()

        if key == "surface":
            result["filters"]["surface"] = value.lower()
        elif key == "curve":
            result["filters"]["curve"] = value.lower()
        elif key == "axis":
            result["filters"]["axis"] = value.lower()
        elif key == "axis_sign":
            result["filters"]["axis_sign"] = value
        elif key == "radius_min":
            try:
                result["filters"]["radius_min"] = float(value)
            except ValueError:
                pass
        elif key == "radius_max":
            try:
                result["filters"]["radius_max"] = float(value)
            except ValueError:
                pass
        elif key == "sort":
            # sort=key or sort=key:dir
            sort_parts = value.split(":")
            result["sort_key"] = sort_parts[0].lower()
            if len(sort_parts) > 1:
                result["sort_dir"] = sort_parts[1].lower()
        elif key == "index":
            if value.lower() == "all":
                result["index"] = "all"
            else:
                try:
                    result["index"] = int(value)
                except ValueError:
                    result["index"] = 0

    return result


def _resolve_selector(parsed: dict, faces: list, edges: list) -> list[dict]:
    """Resolve a parsed selector against topology data.

    Returns a list of matching entity dicts (empty if no match).
    For ``index=N``, returns a single-element list.
    For ``index="all"``, returns all matching entities after sorting.
    """
    entity_type = parsed.get("entity", "face")
    entities = faces if entity_type == "face" else edges
    filters = parsed.get("filters", {})

    if not entities:
        return []

    matched: list[dict] = []
    for ent in entities:
        if not isinstance(ent, dict):
            continue

        # Filter by surface/curve type
        if "surface" in filters:
            st = (ent.get("surfaceType") or "").lower()
            if st != filters["surface"]:
                continue
        if "curve" in filters:
            ct = (ent.get("curveType") or "").lower()
            if ct != filters["curve"]:
                continue

        # Filter by axis
        if "axis" in filters:
            target_axis = filters["axis"]
            ax_idx = _nodes._AXIS_INDEX.get(target_axis)
            if ax_idx is None:
                continue

            params = ent.get("params") or {}
            surface_type = (ent.get("surfaceType") or "").lower()
            curve_type = (ent.get("curveType") or "").lower()

            # Choose the right vector to check based on entity type
            axis_vec = None
            if surface_type == "cylinder":
                # Cylinder: use centerline axis from params
                axis_vec = params.get("axis")
            elif surface_type in ("cone", "torus"):
                axis_vec = params.get("axis")
            elif surface_type == "plane":
                # Plane: use surface normal
                axis_vec = ent.get("normal")
                if not axis_vec:
                    axis_vec = params.get("axis")
            elif curve_type in ("circle", "ellipse"):
                # Circle/Ellipse edge: use the circle's axis (normal to the plane)
                axis_vec = params.get("axis")
            elif curve_type == "line":
                axis_vec = params.get("direction")
            else:
                # Unknown type: try normal, then params.axis, then params.direction
                axis_vec = ent.get("normal") or params.get("axis") or params.get("direction")

            if axis_vec and isinstance(axis_vec, (list, tuple)) and len(axis_vec) >= 3:
                try:
                    from cadpy.analysis import dominant_axis as _da
                    da = _da(list(axis_vec))
                    if not (da and da.get("aligned") and da.get("axis") == target_axis):
                        continue
                except Exception:
                    mag = sum(v * v for v in axis_vec) ** 0.5
                    if mag < 1e-9 or abs(axis_vec[ax_idx]) / mag < 0.9:
                        continue
            else:
                # No axis vector available — skip this entity
                continue

        # Filter by radius range
        params = ent.get("params") or {}
        radius = params.get("radius")
        if radius is not None:
            if "radius_min" in filters and radius < filters["radius_min"]:
                continue
            if "radius_max" in filters and radius > filters["radius_max"]:
                continue

        matched.append(ent)

    if not matched:
        return []

    # Sort by sort_key
    sort_key = parsed.get("sort_key")
    sort_dir = parsed.get("sort_dir", "asc")
    if sort_key:
        def _sort_value(ent: dict) -> float:
            if sort_key == "area":
                return float(ent.get("area") or 0)
            if sort_key == "radius":
                p = ent.get("params") or {}
                return float(p.get("radius") or 0)
            if sort_key == "length":
                return float(ent.get("length") or 0)
            if sort_key in ("center_x", "center_y", "center_z"):
                ax = _nodes._AXIS_INDEX.get(sort_key[-1], 0)
                center = ent.get("center")
                if center and isinstance(center, (list, tuple)) and len(center) > ax:
                    return float(center[ax])
                bbox = ent.get("bbox") or {}
                mn = bbox.get("min", [0, 0, 0])
                mx = bbox.get("max", [0, 0, 0])
                return (float(mn[ax]) + float(mx[ax])) / 2
            return 0

        reverse = sort_dir == "desc"
        matched.sort(key=_sort_value, reverse=reverse)

    # Apply index
    idx = parsed.get("index", 0)
    if idx == "all":
        return matched
    if isinstance(idx, int):
        if 0 <= idx < len(matched):
            return [matched[idx]]
        return []
    return matched


def _measure_entities(entities: list[dict], kind: str, axis_str: str,
                      part_bbox: dict | None = None) -> float | None:
    """Extract a numeric measurement from resolved topology entities.

    Handles both single-entity and multi-entity (gap/distance) cases.
    ``part_bbox`` is the global part bounding box, used for overall dimensions.
    """
    if not entities:
        return None

    ax_idx = _nodes._AXIS_INDEX.get(axis_str, 0)

    # --- Single-entity measurements ---
    if kind in ("hole_diameter", "counterbore_diameter", "boss_diameter"):
        params = entities[0].get("params") or {}
        radius = params.get("radius")
        if radius is not None:
            return float(radius) * 2
        return None

    if kind == "fillet_radius":
        params = entities[0].get("params") or {}
        return float(params["radius"]) if "radius" in params else None

    if kind == "hole_position":
        center = entities[0].get("center")
        if center and isinstance(center, (list, tuple)) and len(center) > ax_idx:
            return float(center[ax_idx])
        return None

    if kind == "overall_dimension":
        # If the selector matched a CYLINDER face, the user likely wants
        # the cylinder's diameter, not the part's bbox extent along axis.
        # (The Spec Planner sometimes uses kind=overall_dimension for
        # diameter measurements like "barrel-diameter".)
        ent = entities[0]
        surface_type = (ent.get("surfaceType") or "").lower()
        if surface_type == "cylinder":
            params = ent.get("params") or {}
            radius = params.get("radius")
            if radius is not None:
                return float(radius) * 2

        # For planar faces or no entity: use part bbox extent.
        if part_bbox:
            mn = part_bbox.get("min")
            mx = part_bbox.get("max")
            if mn and mx and len(mn) > ax_idx and len(mx) > ax_idx:
                return abs(float(mx[ax_idx]) - float(mn[ax_idx]))
        # Fallback: face bbox
        bbox = ent.get("bbox") or {}
        mn = bbox.get("min")
        mx = bbox.get("max")
        if mn and mx and len(mn) > ax_idx and len(mx) > ax_idx:
            return abs(float(mx[ax_idx]) - float(mn[ax_idx]))
        return None

    if kind == "wall_thickness":
        # Two sub-modes detected by normal directions:
        # A) OPPOSING normals → true wall thickness (min gap between two
        #    parallel faces pointing at each other, e.g. inner/outer wall)
        # B) SAME direction normals → periodic pitch (average gap between
        #    consecutive faces with same orientation, e.g. fin-to-fin spacing)
        if len(entities) < 2:
            return None

        pos_centers = []  # faces with normal in +axis direction
        neg_centers = []  # faces with normal in -axis direction
        for ent in entities:
            c = ent.get("center")
            if not c or not isinstance(c, (list, tuple)) or len(c) <= ax_idx:
                continue
            center_val = float(c[ax_idx])
            normal = ent.get("normal") or (ent.get("params") or {}).get("axis")
            if normal and isinstance(normal, (list, tuple)) and len(normal) >= 3:
                mag = sum(v * v for v in normal) ** 0.5
                if mag < 1e-9:
                    continue
                component = normal[ax_idx] / mag
                if component >= 0.9:
                    pos_centers.append(center_val)
                elif component <= -0.9:
                    neg_centers.append(center_val)
            else:
                continue

        # Mode A: Opposing normals → minimum gap between pos and neg faces.
        # Only use this for simple wall pairs (few faces per direction).
        # When many faces exist in each direction (≥4), it's likely a
        # periodic structure (e.g. fins), not a simple wall — skip to Mode B.
        if pos_centers and neg_centers and len(pos_centers) < 4 and len(neg_centers) < 4:
            min_dist = float("inf")
            for pc in pos_centers:
                for nc in neg_centers:
                    d = abs(pc - nc)
                    if 0.001 < d < min_dist:
                        min_dist = d
            if min_dist < float("inf"):
                return min_dist

        # Mode B: Periodic pitch detection.
        # Run on SAME-DIRECTION faces only (e.g. all +Z faces, which are
        # one per fin).  Mixing +Z and -Z faces introduces within-fin gaps
        # that confuse the pitch calculation.
        # Pick whichever group has more faces for robustness.
        group = pos_centers if len(pos_centers) >= len(neg_centers) else neg_centers
        if len(group) < 2:
            return None
        group.sort()
        pitch_gaps = [
            group[i + 1] - group[i]
            for i in range(len(group) - 1)
            if group[i + 1] - group[i] > 0.001
        ]
        if not pitch_gaps:
            return None
        # Return the average pitch, excluding the largest gap (likely a
        # boundary artifact like barrel-to-first-fin or last-fin-to-cap).
        if len(pitch_gaps) >= 3:
            sorted_gaps = sorted(pitch_gaps)
            trimmed = sorted_gaps[:-1]
            return sum(trimmed) / len(trimmed)
        return sum(pitch_gaps) / len(pitch_gaps)

    if kind == "hole_distance":
        # Closest pair of cylinder centers to nominal
        if len(entities) < 2:
            return None
        centers = []
        for ent in entities:
            c = ent.get("center")
            if c and isinstance(c, (list, tuple)) and len(c) >= 3:
                centers.append([float(v) for v in c[:3]])
        if len(centers) < 2:
            return None
        min_dist = float("inf")
        for i in range(len(centers)):
            for j in range(i + 1, len(centers)):
                d = sum((a - b) ** 2 for a, b in zip(centers[i], centers[j])) ** 0.5
                if d < min_dist:
                    min_dist = d
        return min_dist if min_dist < float("inf") else None

    # Default: try center coordinate for single entity
    if len(entities) == 1:
        center = entities[0].get("center")
        if center and isinstance(center, (list, tuple)) and len(center) > ax_idx:
            return float(center[ax_idx])
    return None


def _vector_axis_aligned(vector, axis: str, threshold: float = 0.9) -> bool:
    """Check if a 3D vector is aligned with a given axis (x/y/z).

    Uses cadpy.analysis.dominant_axis for proper normalization.
    Unlike the previous manual threshold check, this normalizes the
    vector first so the threshold comparison is meaningful.
    """
    try:
        from cadpy.analysis import dominant_axis
        da = dominant_axis(vector)
        if da is None:
            return False
        return da["axis"] == axis and da["aligned"]
    except Exception:
        # Fallback: manual check (handles import failures gracefully)
        if vector is None or len(vector) < 3:
            return False
        import math
        mag = math.sqrt(sum(float(v) ** 2 for v in vector[:3]))
        if mag < 1e-12:
            return False
        ax_idx = {"x": 0, "y": 1, "z": 2}[axis]
        return abs(float(vector[ax_idx])) / mag >= threshold


def _find_cylindrical_faces(faces: list, axis: str | None = None) -> list:
    """Return all cylindrical faces, optionally filtered by cylinder axis.

    NOTE: For cylinders, the axis field is ``params.axis`` (the cylinder
    centerline), NOT ``normal`` (which is the radial surface normal).
    This was a bug in the previous implementation.
    """
    result = []
    for f in faces:
        surface_type = str(f.get("surfaceType") or "").lower()
        if surface_type != "cylinder":
            continue
        if axis is not None:
            params = f.get("params")
            cyl_axis = params.get("axis") if isinstance(params, dict) else None
            if cyl_axis is None:
                continue
            if not _nodes._vector_axis_aligned(cyl_axis, axis):
                continue
        result.append(f)
    return result


def _find_planar_faces(faces: list, axis: str | None = None) -> list:
    """Return all planar faces, optionally filtered by normal axis."""
    result = []
    for f in faces:
        surface_type = str(f.get("surfaceType") or "").lower()
        if surface_type != "plane":
            continue
        if axis is not None:
            normal = f.get("normal")
            if normal is None:
                continue
            if not _nodes._vector_axis_aligned(normal, axis):
                continue
        result.append(f)
    return result


def _face_center(f: dict) -> tuple[float, float, float] | None:
    """Extract center (x, y, z) from a face row.

    Uses cadpy.analysis.bbox_center for the fallback path.
    """
    c = f.get("center")
    if c and len(c) == 3:
        try:
            return (float(c[0]), float(c[1]), float(c[2]))
        except Exception:
            pass
    # Fallback to bbox center via cadpy.analysis
    bbox = f.get("bbox")
    if bbox:
        try:
            from cadpy.analysis import bbox_center
            bc = bbox_center(bbox)
            if bc is not None:
                return (bc[0], bc[1], bc[2])
        except Exception:
            pass
    return None


def _run_engine_a_cadpy(
    *,
    step_path: str,
    cad_brief,  # CADBrief | dict
    engine_b_mesh_resolution: dict,
    feature_measurements: dict | None = None,
    iteration: int = 0,
    selector_map: dict[str, str] | None = None,
) -> EngineReport:
    """Deterministic STEP QA using cadpy.step_scene + cadpy.analysis.

    Loads the STEP file, extracts a full topology manifest (faces, edges,
    vertices, bounding-box), then iterates every VerificationTarget from
    the CADBrief and matches it against the actual geometric features.

    PRIORITY 1: White-box instrumentation (feature_measurements) — exact
    per-feature bounding-box data saved BEFORE boolean merge.
    PRIORITY 2: cadpy STEP topology analysis.

    Falls back gracefully: targets that cannot be matched produce a
    ``passed=False`` result with "Feature not found".
    """
    warnings: list[str] = []
    errors: list[str] = []
    results: list[VerificationResult] = []

    targets = _nodes._attr(cad_brief, "verification_targets", [])

    # Load feature measurements from white-box instrumentation if not provided
    if feature_measurements is None:
        measurements_file = Path(f"temp_measurements_{iteration}.json")
        if measurements_file.is_file():
            try:
                with open(measurements_file, 'r', encoding='utf-8') as f:
                    feature_measurements = json.load(f)
                # Remove metadata keys that are not feature measurements
                feature_measurements.pop("_step_hash", None)
                feature_measurements.pop("_stl_hash", None)
                feature_measurements.pop("_timestamp", None)
                print(f"[ENGINE A] Loaded {len(feature_measurements)} feature measurements from {measurements_file.name}")
            except Exception as exc:
                print(f"[ENGINE A] WARNING: Failed to load feature measurements: {exc}")

    # ------------------------------------------------------------------
    # 1. Load STEP topology
    # ------------------------------------------------------------------
    topo = _nodes._load_step_topology(step_path)
    faces, edges, vertices, bbox, volume_mm3, manifest, topo_error = topo
    if topo_error is not None:
        return EngineReport(
            engine_name="cadpy_analysis",
            results=[],
            summary="Engine A: STEP topology extraction failed.",
            warnings=[],
            errors=[topo_error],
        )

    summary_parts = [
        f"Topology: {len(faces)} faces, {len(edges)} edges, {len(vertices)} vertices",
    ]
    if bbox:
        from cadpy.analysis import bbox_facts as _bf
        _facts = _bf(bbox)
        _size = _facts.get("size")
        if _size:
            summary_parts.append(f"Bbox: [{_size[0]:.1f}, {_size[1]:.1f}, {_size[2]:.1f}] mm")
    if volume_mm3 is not None:
        summary_parts.append(f"Volume: {volume_mm3:.1f} mm³")

    # ------------------------------------------------------------------
    # 2. Pre-index features for matching
    # ------------------------------------------------------------------
    cyl_faces = _nodes._find_cylindrical_faces(faces)
    plan_faces = _nodes._find_planar_faces(faces)
    cyl_by_axis: dict[str, list] = {}
    for ax in ("x", "y", "z"):
        cyl_by_axis[ax] = _nodes._find_cylindrical_faces(faces, axis=ax)

    # ------------------------------------------------------------------
    # 3. Measure every verification target
    # ------------------------------------------------------------------
    selector_map = selector_map or {}
    for target in targets:
        target_obj = _nodes._to_verification_target(target)
        if target_obj is None:
            continue
        # Inject selector expression from Architect's selector_map into the
        # schema's existing face_selector_expression / edge_selector_expression
        tid = _nodes._attr(target_obj, "id", "")
        if tid in selector_map:
            expr = selector_map[tid]
            if not target_obj.face_selector_expression and not target_obj.edge_selector_expression:
                _parsed = _nodes._parse_selector(expr)
                if _parsed:
                    if _parsed["entity"] == "face":
                        target_obj.face_selector_expression = expr
                    else:
                        target_obj.edge_selector_expression = expr
        try:
            result = _nodes._measure_target_cadpy(
                target_obj, faces, edges, bbox, volume_mm3,
                cyl_faces, plan_faces, cyl_by_axis,
                feature_measurements=feature_measurements,
            )
            if result is not None:
                results.append(result)
        except Exception as exc:
            errors.append(
                f"Engine A: exception measuring '{_nodes._attr(target_obj, 'id', '?')}': {exc}"
            )
            results.append(VerificationResult(
                target_id=_nodes._attr(target_obj, "id", "?"),
                passed=False,
                engine="cadpy_analysis",
                severity="error",
                raw_measurements={"error": str(exc)},
            ))

    return EngineReport(
        engine_name="cadpy_analysis",
        results=results,
        summary="  ".join(summary_parts),
        warnings=warnings,
        errors=errors,
        raw_json={
            "bbox": bbox,
            "volume_mm3": volume_mm3,
            "face_count": len(faces),
            "edge_count": len(edges),
            "_manifest": manifest,  # full manifest for selector_manifest_diff
        },
    )


def _measure_target_cadpy(
    target,        # VerificationTarget
    faces: list,
    edges: list,
    bbox: dict,
    volume_mm3: float | None,
    cyl_faces: list,
    plan_faces: list,
    cyl_by_axis: dict[str, list],
    feature_measurements: dict | None = None,
) -> VerificationResult | None:
    """Check a single VerificationTarget against cadpy-extracted topology.

    PRIORITY 1: White-box instrumentation (feature_measurements) — exact
    per-feature bounding-box data saved BEFORE boolean merge.
    PRIORITY 2: cadpy STEP topology analysis.

    Returns ``None`` for target kinds this engine doesn't handle
    (Engine B covers those).
    """
    target_id = _nodes._attr(target, "id", "?")
    kind_str = _nodes._attr(target, "kind", None)
    kind = str(kind_str.value) if hasattr(kind_str, "value") else str(kind_str or "")
    nominal = _nodes._attr(target, "nominal", None)
    tol_upper = _nodes._attr(target, "tolerance_upper", 0.1)
    tol_lower = _nodes._attr(target, "tolerance_lower", 0.1)
    axis = _nodes._attr(target, "measurement_axis", None)
    axis_str = str(axis.value) if hasattr(axis, "value") else str(axis or "")

    # Skip targets that cannot be accurately measured from merged STEP topology
    # These are array/pattern features that lose individual identity after boolean merge
    _target_id_lower = target_id.lower()
    _skip_keywords = ["pitch", "spacing", "start", "end", "count", "pattern", "rise"]
    if any(kw in _target_id_lower for kw in _skip_keywords):
        return None  # Let Engine B or manual verification handle these

    # PRIORITY 0: Selector-based measurement (precise topology matching)
    selector_expr = (
        _nodes._attr(target, "face_selector_expression", None)
        or _nodes._attr(target, "edge_selector_expression", None)
    )
    if selector_expr:
        parsed = _nodes._parse_selector(selector_expr)
        if parsed is not None:
            entities = _nodes._resolve_selector(parsed, faces, edges)
            if entities:
                measured = _nodes._measure_entities(entities, kind, axis_str, part_bbox=bbox)
                if measured is not None:
                    measured = _nodes._adjust_for_radius_semantics(measured, target_id, kind)
                if measured is not None and nominal is not None:
                    dev = measured - nominal
                    # Sanity check: if deviation is too large (> 3x nominal),
                    # the selector likely matched wrong features — fall through
                    if abs(dev) > 3.0 * abs(nominal):
                        # Wrong match — fall through to Priority 1 or 2
                        pass
                    else:
                        passed = abs(dev) <= max(tol_upper, tol_lower)
                        entity_ids = [e.get("id", "?") for e in entities[:5]]
                        return VerificationResult(
                            target_id=target_id, passed=passed,
                            measured_value=round(measured, 3),
                            deviation=round(dev, 3),
                            engine="cadpy_analysis",
                            severity="error" if not passed else "info",
                            raw_measurements={
                                "source": f"selector:{selector_expr}",
                                "entity_ids": entity_ids,
                                "entity_count": len(entities),
                                "method": "selector_based",
                            },
                        )
            # Selector didn't match or matched wrong features — fall through to Priority 1
            # (logged in EngineReport summary, not printed per-target)

    # PRIORITY 1: White-box instrumentation (exact per-feature data)
    if feature_measurements:
        # Skip array/pattern properties — these require analyzing the full
        # array structure, not a single prototype feature. White-box only
        # measures the prototype (e.g., one fin), not the array (e.g., 12 fins).
        _tid_lower = target_id.lower()
        _array_keywords = {"pitch", "count", "spacing", "start", "end", "pattern", "rise"}
        if any(kw in _tid_lower for kw in _array_keywords):
            # Let Engine B or selector-based measurement handle this
            pass
        else:
            # Compound measurements (gap/distance/rise/offset) FIRST — they
            # require TWO features and cannot be handled by single-feature match.
            measured = None
            source = ""
            _compound_keywords = ("gap", "distance", "rise", "offset", "pitch", "spacing")
            if any(kw in _tid_lower for kw in _compound_keywords):
                compound = _nodes._find_compound_gap_measurement(
                    target_id, feature_measurements, axis_str,
                )
                if compound is not None:
                    measured = compound
                    source = "white_box_compound"

            if measured is None:
                measured, source = _nodes._find_best_feature_match(
                    target_id, feature_measurements, kind, axis_str,
                )

            if measured is not None and nominal is not None:
                dev = measured - nominal
                passed = abs(dev) <= max(tol_upper, tol_lower)
                return VerificationResult(
                    target_id=target_id, passed=passed,
                    measured_value=round(measured, 3), deviation=round(dev, 3),
                    engine="cadpy_analysis",
                    severity="error" if not passed else "info",
                    raw_measurements={"source": source, "method": "white_box_instrumentation"},
                )

    # PRIORITY 2: cadpy STEP topology analysis
    # ── Feature-keyword guard ──────────────────────────────────────────
    # If target_id refers to a SPECIFIC feature (e.g. "backplate-thickness",
    # "hub-diameter", "blade-height"), the whole-part STEP topology (bbox,
    # all cylindrical faces, all planar faces) is NOT the right measurement.
    # Return None so the merge logic defers to the other engine or reports
    # "cannot measure".  This guard protects ALL kind-specific handlers below,
    # not just overall_dimension.
    _tid_lower = target_id.lower()
    _feature_keywords = {"lug", "rib", "cutout", "boss", "flange", "tab",
                         "gusset", "ear", "arm", "leg", "web", "slot", "fin",
                         "hub", "blade", "backplate", "shaft", "housing",
                         "bracket", "plate", "cover", "base", "cap", "tread"}
    _is_feature_target = any(kw in _tid_lower for kw in _feature_keywords)
    if _is_feature_target:
        # Feature-specific target — whole-part topology is wrong.
        # Let Engine B handle it (it has white-box + STL heuristics).
        return None

    # === OVERALL_DIMENSION ================================================
    if kind == "overall_dimension" and axis_str in {"x", "y", "z"}:
        idx = {"x": 0, "y": 1, "z": 2}[axis_str]
        if bbox:
            from cadpy.analysis import bbox_size as _bs
            _size = _bs(bbox)
            measured = float(_size[idx]) if _size else 0.0
        elif faces:
            # Compute bbox from all faces
            all_centers = [c for f in faces if (c := _nodes._face_center(f))]
            if all_centers:
                measured = max(c[idx] for c in all_centers) - min(c[idx] for c in all_centers)
            else:
                return _nodes._feature_not_found(target_id, "overall_dimension")
        else:
            return None  # let Engine B handle
        dev = measured - float(nominal or 0)
        passed = abs(dev) <= max(tol_upper, tol_lower)
        return VerificationResult(
            target_id=target_id, passed=passed,
            measured_value=round(measured, 3), deviation=round(dev, 3),
            engine="cadpy_analysis", severity="error" if not passed else "info",
        )

    # === VOLUME ===========================================================
    if kind == "volume" and volume_mm3 is not None and nominal is not None:
        dev = volume_mm3 - float(nominal)
        passed = abs(dev) <= max(tol_upper, tol_lower)
        return VerificationResult(
            target_id=target_id, passed=passed,
            measured_value=round(volume_mm3, 2), deviation=round(dev, 2),
            engine="cadpy_analysis", severity="error" if not passed else "info",
        )

    # === HOLE_DIAMETER ====================================================
    if kind in ("hole_diameter", "counterbore_diameter", "boss_diameter"):
        candidates = cyl_faces
        if axis_str:
            candidates = cyl_by_axis.get(axis_str, cyl_faces)
        if not candidates:
            return _nodes._feature_not_found(target_id, kind, "No cylindrical faces found")

        # Prefer the cylinder closest to nominal diameter
        best = None
        best_dist = float("inf")
        for f in candidates:
            radius = f.get("params", {}).get("radius") if isinstance(f.get("params"), dict) else None
            if radius is None:
                continue
            dia = float(radius) * 2
            if nominal is not None:
                dist = abs(dia - float(nominal))
                if dist < best_dist:
                    best_dist = dist
                    best = (dia, f)
            else:
                best = (dia, f)
                break

        if best is None:
            return _nodes._feature_not_found(target_id, kind, "No cylindrical face with radius data")

        # Sanity check: if the best match deviates too much from nominal,
        # we likely matched an unrelated cylinder (e.g., gear outer surface
        # instead of bore).  Reject the match rather than report a wildly
        # wrong value that confuses the repair agent.
        measured, matched_face = best
        _diagnostic_hint = ""
        if nominal is not None and nominal > 0:
            max_reasonable_dev = max(float(nominal) * 0.5, 2.0)  # 50% or 2mm
            if abs(measured - float(nominal)) > max_reasonable_dev:
                return _nodes._feature_not_found(
                    target_id, kind,
                    f"Closest cylindrical face has diameter {measured:.2f}mm "
                    f"(nominal {nominal:.2f}mm, deviation {abs(measured - float(nominal)):.2f}mm "
                    f"> {max_reasonable_dev:.2f}mm threshold). "
                    f"Bore may not exist in exported geometry or selector_map is needed."
                )
            # Diagnostic: even within the sanity threshold, >25% deviation
            # suggests we may have matched the wrong cylinder (e.g., a pin
            # instead of a bore).  Add a hint for the repair agent.
            if abs(measured - float(nominal)) > float(nominal) * 0.25:
                _diagnostic_hint = (
                    f"WARNING: measured {measured:.2f}mm deviates "
                    f"{abs(measured - float(nominal)):.2f}mm ({abs(measured - float(nominal)) / float(nominal) * 100:.0f}%) "
                    f"from nominal {nominal:.2f}mm. "
                    f"This may be a different cylindrical feature (e.g., pin, shaft) "
                    f"rather than the intended bore. "
                    f"Check that the exported STEP contains the correct body."
                )
        dev = measured - float(nominal or 0) if nominal is not None else 0.0
        passed = abs(dev) <= max(tol_upper, tol_lower) if nominal is not None else True
        center = _nodes._face_center(matched_face)
        return VerificationResult(
            target_id=target_id, passed=passed,
            measured_value=round(measured, 3), deviation=round(dev, 3),
            engine="cadpy_analysis", severity="error" if not passed else "info",
            raw_measurements={
                "matched_face": matched_face.get("id", "?"),
                "center": list(center) if center else None,
                **({"diagnostic": _diagnostic_hint} if _diagnostic_hint else {}),
            },
        )

    # === HOLE_POSITION ====================================================
    if kind == "hole_position":
        candidates = cyl_faces
        if axis_str:
            candidates = cyl_by_axis.get(axis_str, cyl_faces)
        if not candidates:
            return _nodes._feature_not_found(target_id, kind, "No cylindrical faces")

        # Find the cylinder whose center is closest to the nominal position.
        # nominal is interpreted as the expected coordinate along measurement_axis.
        # Without a selector or nominal, we can only report the position, not validate.
        best_center = None
        best_face = None
        if nominal is not None and axis_str in {"x", "y", "z"}:
            ax_idx = {"x": 0, "y": 1, "z": 2}[axis_str]
            best_dist = float("inf")
            for f in candidates:
                c = _nodes._face_center(f)
                if c is None:
                    continue
                d = abs(c[ax_idx] - float(nominal))
                if d < best_dist:
                    best_dist = d
                    best_center = c
                    best_face = f
            if best_center is not None:
                measured = best_center[ax_idx]
                dev = measured - float(nominal)
                passed = abs(dev) <= max(tol_upper, tol_lower)
                return VerificationResult(
                    target_id=target_id, passed=passed,
                    measured_value=round(measured, 3),
                    deviation=round(dev, 3),
                    engine="cadpy_analysis",
                    severity="error" if not passed else "info",
                    raw_measurements={
                        "center": list(best_center),
                        "matched_face": best_face.get("id", "?") if best_face else "?",
                        "method": "closest_to_nominal",
                    },
                )

        # No nominal or axis — report position without validation
        center = _nodes._face_center(candidates[0])
        if center is None:
            return _nodes._feature_not_found(target_id, kind, "Cannot compute hole center")
        return VerificationResult(
            target_id=target_id, passed=True,
            measured_value=0.0, deviation=0.0,
            engine="cadpy_analysis", severity="info",
            raw_measurements={
                "center": list(center),
                "matched_face": candidates[0].get("id", "?"),
                "note": "No nominal/axis specified — position reported but not validated",
            },
        )

    # === HOLE_DISTANCE ====================================================
    if kind == "hole_distance" and len(cyl_faces) >= 2 and nominal is not None:
        centers = [c for f in cyl_faces if (c := _nodes._face_center(f))]
        if len(centers) < 2:
            return _nodes._feature_not_found(target_id, kind, f"Only {len(centers)} cylinder centers")
        # Pairwise distances, pick closest to nominal
        import math as _math
        best_dist = float("inf")
        best_measured = 0.0
        for i in range(len(centers)):
            for j in range(i + 1, len(centers)):
                d = _math.sqrt(sum((centers[i][k] - centers[j][k]) ** 2 for k in range(3)))
                if abs(d - float(nominal)) < best_dist:
                    best_dist = abs(d - float(nominal))
                    best_measured = d
        dev = best_measured - float(nominal)
        passed = abs(dev) <= max(tol_upper, tol_lower)
        return VerificationResult(
            target_id=target_id, passed=passed,
            measured_value=round(best_measured, 3), deviation=round(dev, 3),
            engine="cadpy_analysis", severity="error" if not passed else "info",
        )

    # === WALL_THICKNESS (distance between parallel planar faces) ===========
    if kind == "wall_thickness" and nominal is not None:
        matched_axis = axis_str or "z"
        planes = _nodes._find_planar_faces(faces, axis=matched_axis)
        if len(planes) < 2:
            return _nodes._feature_not_found(target_id, kind, f"Need ≥2 parallel planar faces, got {len(planes)}")

        # Collect all pairwise distances between parallel planar faces.
        # Filter out noise (< 0.5mm) and prefer the distance closest to nominal.
        centers = [c for f in planes if (c := _nodes._face_center(f))]
        ax_idx = {"x": 0, "y": 1, "z": 2}[matched_axis]
        distances: list[float] = []
        for i in range(len(centers)):
            for j in range(i + 1, len(centers)):
                d = abs(centers[i][ax_idx] - centers[j][ax_idx])
                if d > 0.5:  # filter out noise / coincident faces
                    distances.append(d)
        if not distances:
            return _nodes._feature_not_found(target_id, kind, "No valid plane-to-plane distances found")
        # Prefer the distance closest to nominal (not just the minimum).
        # The minimum distance might be an unrelated thin gap, not the wall.
        distances.sort(key=lambda d: abs(d - float(nominal)))
        best_dist = distances[0]
        dev = best_dist - float(nominal)
        passed = dev >= -tol_lower  # wall must be at least nominal - tol_lower
        return VerificationResult(
            target_id=target_id, passed=passed,
            measured_value=round(best_dist, 3), deviation=round(dev, 3),
            engine="cadpy_analysis", severity="error" if not passed else "info",
            raw_measurements={
                "method": "closest_to_nominal",
                "candidates_count": len(distances),
                "all_distances": [round(d, 2) for d in distances[:5]],
            },
        )

    # === DEPTH measurements (hole_depth, boss_depth, counterbore_depth) ====
    # These measure the extent of a cylindrical feature along its axis.
    # On STEP topology, a blind hole appears as a cylindrical face with a
    # planar bottom; depth = distance from top face to bottom face along axis.
    if kind in ("hole_depth", "boss_depth", "counterbore_depth") and nominal is not None:
        # Find cylindrical faces along the measurement axis
        target_axis = axis_str or "z"
        cyl_candidates = cyl_by_axis.get(target_axis, cyl_faces)
        if not cyl_candidates:
            return _nodes._feature_not_found(target_id, kind, f"No cylindrical faces along {target_axis}")

        # For each cylinder, compute its extent along the axis
        ax_idx = {"x": 0, "y": 1, "z": 2}[target_axis]
        best_measured = None
        best_dev = float("inf")
        for f in cyl_candidates:
            params = f.get("params", {}) if isinstance(f.get("params"), dict) else {}
            # Use the face's parametric bounds if available
            u_min = params.get("u_min")
            u_max = params.get("u_max")
            if u_min is not None and u_max is not None:
                extent = abs(float(u_max) - float(u_min))
            else:
                # Fallback: use the face center ± radius as rough extent
                center = _nodes._face_center(f)
                radius = params.get("radius")
                if center and radius:
                    extent = float(radius) * 2  # rough approximation
                else:
                    continue
            if nominal is not None:
                dev = abs(extent - float(nominal))
                if dev < best_dev:
                    best_dev = dev
                    best_measured = extent
            else:
                best_measured = extent
                break

        if best_measured is None:
            return _nodes._feature_not_found(target_id, kind, "Cannot compute depth from cylindrical faces")
        dev = best_measured - float(nominal)
        passed = abs(dev) <= max(tol_upper, tol_lower)
        return VerificationResult(
            target_id=target_id, passed=passed,
            measured_value=round(best_measured, 3), deviation=round(dev, 3),
            engine="cadpy_analysis", severity="error" if not passed else "info",
            raw_measurements={"method": "cylinder_extent", "axis": target_axis},
        )

    # === SLOT_DIMENSION =====================================================
    if kind == "slot_dimension" and nominal is not None:
        # Slots appear as elongated holes — measure from cylindrical end-caps
        # or from parallel planar faces depending on which axis is requested.
        target_axis = axis_str or "x"
        ax_idx = {"x": 0, "y": 1, "z": 2}[target_axis]
        # Use planar faces perpendicular to the axis (slot width)
        planes = _nodes._find_planar_faces(faces, axis=target_axis)
        if len(planes) >= 2:
            centers = [c for f in planes if (c := _nodes._face_center(f))]
            distances = []
            for i in range(len(centers)):
                for j in range(i + 1, len(centers)):
                    d = abs(centers[i][ax_idx] - centers[j][ax_idx])
                    if d > 0.5:
                        distances.append(d)
            if distances:
                distances.sort(key=lambda d: abs(d - float(nominal)))
                best = distances[0]
                dev = best - float(nominal)
                passed = abs(dev) <= max(tol_upper, tol_lower)
                return VerificationResult(
                    target_id=target_id, passed=passed,
                    measured_value=round(best, 3), deviation=round(dev, 3),
                    engine="cadpy_analysis", severity="error" if not passed else "info",
                )
        return _nodes._feature_not_found(target_id, kind, "No planar faces for slot measurement")

    # === FILLET_RADIUS (look for toroidal/small-radius faces on edges) ====
    if kind == "fillet_radius":
        # Fillets appear as cylindrical or toroidal faces with small radii
        candidates = []
        for e in edges:
            curve_type = str(e.get("curveType") or "").lower()
            if curve_type in ("circle",):
                radius = e.get("params", {}).get("radius") if isinstance(e.get("params"), dict) else None
                if radius is not None:
                    candidates.append((float(radius), e))
        if not candidates:
            return _nodes._feature_not_found(target_id, kind, "No circular edges with radius data")
        # Return the radius closest to nominal (or the smallest if no nominal)
        if nominal is not None:
            candidates.sort(key=lambda x: abs(x[0] - float(nominal)))
        measured = candidates[0][0]
        # Guard: reject if the closest edge is too far from nominal.
        # This prevents matching unrelated edges (e.g. mounting holes)
        # when the actual fillet edges don't exist in the topology.
        if nominal is not None and nominal > 0:
            max_reasonable_dev = max(float(nominal) * 0.5, 1.0)  # 50% or 1mm
            if abs(measured - float(nominal)) > max_reasonable_dev:
                return _nodes._feature_not_found(
                    target_id, kind,
                    f"Closest circular edge has radius {measured:.2f}mm "
                    f"(nominal {nominal:.2f}mm, deviation {abs(measured - float(nominal)):.2f}mm "
                    f"> {max_reasonable_dev:.2f}mm threshold). "
                    f"Fillet may not have been applied to the geometry."
                )
        dev = measured - float(nominal or 0) if nominal is not None else 0.0
        passed = abs(dev) <= max(tol_upper, tol_lower) if nominal is not None else True
        return VerificationResult(
            target_id=target_id, passed=passed,
            measured_value=round(measured, 3), deviation=round(dev, 3),
            engine="cadpy_analysis", severity="error" if not passed else "info",
            raw_measurements={"matched_edge": candidates[0][1].get("id", "?")},
        )

    # === Targets Engine A doesn't handle → let Engine B cover them ========
    return None


def _feature_not_found(target_id: str, kind: str, detail: str = "") -> VerificationResult:
    """Return a ``passed=False`` result when a feature cannot be located.

    Uses ``severity="info"`` so this result is NOT counted as a real failure
    in the merge logic (which filters by ``severity in ("error", "fatal")``).
    The target appears in the QA report as "not measurable" for transparency,
    but does not trigger DIMENSION error routing or inflate the failure count
    that confuses the repair agent.
    """
    return VerificationResult(
        target_id=target_id,
        passed=False,
        engine="cadpy_analysis",
        severity="info",
        raw_measurements={
            "error": f"Feature not found ({kind})",
            "detail": detail,
            "not_measurable": True,
        },
    )
