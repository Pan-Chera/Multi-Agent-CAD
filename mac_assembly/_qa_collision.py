"""Placed mesh loading, surface gaps and interference checks.

Internal helpers; callers continue to use ``mac_assembly.assembly_qa``.
Resolve shared configuration, caches and replaceable helpers through that
module at call time so existing overrides and monkeypatch hooks stay live.
"""
from __future__ import annotations

import math
from pathlib import Path
from mac_assembly.schemas_assembly import InterferenceCheck


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
        _cp, dist, _fid = _qa.closest_point_robust(outer, pts[inside])
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
        pts_a = np.vstack([_qa._sample_surface(m, n_samples) for m in meshes_a])
        pts_b = np.vstack([_qa._sample_surface(m, n_samples) for m in meshes_b])
        best = float("inf")
        for outer, pts in ((meshes_b, pts_a), (meshes_a, pts_b)):
            # distance from each sampled point to the nearest point on outer
            merged = trimesh.util.concatenate(*outer) if len(outer) > 1 else outer[0]
            # Robust helper: naive fallback when rtree is missing (otherwise
            # the real-topology gap silently degrades to None -> bbox dz).
            _cp, dist, _fid = _qa.closest_point_robust(merged, pts)
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
    depth_tol = _qa.cfg.INTERFERENCE_DEPTH_TOL_MM
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
    bad_a = (not math.isfinite(vol_a)) or vol_a <= _qa._ZERO_VOLUME_EPS_MM3
    bad_b = (not math.isfinite(vol_b)) or vol_b <= _qa._ZERO_VOLUME_EPS_MM3
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
        pts_a = _qa._sample_surface(mesh_a)
        pts_b = _qa._sample_surface(mesh_b)
        frac = max(
            _qa._deep_penetration_frac(mesh_b, pts_a, depth_tol),
            _qa._deep_penetration_frac(mesh_a, pts_b, depth_tol),
        )
    except Exception as exc:  # noqa: BLE001 - probe failed; do NOT false-pass
        return None, f"UNVERIFIABLE (sampling/contains/depth): {type(exc).__name__}: {exc}", 0.0
    vol = frac * float(min(abs(mesh_a.volume), abs(mesh_b.volume)))
    passed = (
        frac <= _qa.cfg.INTERFERENCE_VERTEX_FRACTION
        and vol <= _qa.cfg.INTERFERENCE_VOLUME_TOL_MM3
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
            collided, detail, vol = _qa._pair_collides(ma, mb)
            # BUG-004: None means the probe could not run (trimesh/numpy
            # failure). Treat as failed -- never silently PASS an
            # unverifiable pair.
            if collided is None:
                checks.append(InterferenceCheck(
                    part_a=la, part_b=lb, passed=False, detail=detail,
                    min_gap_mm=None, penetration_volume_mm3=0.0,
                ))
                continue
            gap = None if collided else _qa._min_gap_between([ma], [mb])
            checks.append(InterferenceCheck(
                part_a=la, part_b=lb, passed=not collided, detail=detail,
                min_gap_mm=gap, penetration_volume_mm3=vol,
            ))
    return checks


# Bind the facade after definitions so importing either module first works.
# Helpers are not called during import; shared state remains owned by the facade.
from mac_assembly import assembly_qa as _qa
