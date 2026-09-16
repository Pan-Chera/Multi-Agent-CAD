"""Assembly handoff: snapshot packet + GLB + URDF export + viewer manifest.

Mirrors the CAD Skills convention of handing finished geometry to a viewer:
after the assembly is built we (a) export a GLB the browser
``<model-viewer>`` / CAD Viewer can open directly, (b) export a URDF +
per-link meshes for grasp simulators (PyBullet / MuJoCo / MoveIt), (c)
keep the rendered snapshot views, and (d) write a ``handoff_manifest.json``
listing every reviewable artifact.

The GLB is produced from the assembly mesh with trimesh (no cadpy scene
machinery needed); per-part STEP sources stay on disk for provenance.
"""

from __future__ import annotations

import json
from pathlib import Path


def export_assembly_glb(assembly_dir: Path) -> Path | None:
    """Export ``assembly_output.stl`` -> ``assembly_output.glb`` (trimesh).

    Best-effort: returns None on failure (trimesh unavailable, STL
    unreadable, GLB export crash). BUG-046: the failure reason is
    returned via the GLB-missing detail surfacing in build_handoff's
    manifest (``assembly_glb=None`` is no longer silent -- the operator
    sees a missing GLB line and the handoff_error.log captures the
    traceback when build_handoff wraps the call).
    """
    stl = assembly_dir / "assembly_output.stl"
    glb = assembly_dir / "assembly_output.glb"
    if not stl.is_file():
        return None
    try:
        import trimesh

        mesh = trimesh.load(str(stl), force="mesh")
        mesh.export(str(glb), file_type="glb")
        return glb if glb.is_file() else None
    except Exception as exc:  # noqa: BLE001 - GLB is best-effort
        # BUG-046: do NOT silently swallow. Surface the failure reason
        # to the handoff_error.log so an operator can diagnose why GLB
        # is missing. STEP/URDF artifacts are independent and remain
        # valid -- this only affects the viewer-ready GLB.
        try:
            log_path = assembly_dir / "handoff_error.log"
            with log_path.open("a", encoding="utf-8") as f:
                import traceback

                f.write(
                    f"\nGLB export failed: {type(exc).__name__}: {exc}\n"
                )
                f.write(traceback.format_exc())
        except Exception:  # noqa: BLE001 - log write must not mask the original
            pass
        return None


def collect_snapshot_views(assembly_dir: Path) -> list[Path]:
    """Return the rendered isometric snapshot PNGs (diagnostic review)."""
    view_dir = assembly_dir / "assembly_judge_views"
    if not view_dir.is_dir():
        return []
    return sorted(view_dir.glob("view_*.png"))


def build_handoff(
    work_dir: Path,
    assembly_brief=None,
    qa_report=None,
    part_results: dict | None = None,
) -> dict:
    """Assemble the handoff packet and write ``handoff_manifest.json``.

    Returns the manifest dict. All paths are relative to ``work_dir`` so the
    packet is portable.

    ``part_results``: the pipeline's authoritative PartResult map. When
    given, ``part_steps`` lists each part's FINAL STEP exactly once (the
    PartResult's step_path -- e.g. temp_output_features.step for v3 parts,
    not the intermediate temp_output_base.step). Without it, fall back to
    one newest ``temp_output_*.step`` per part directory (same resolution
    rule as the assembly codegen).
    """
    work_dir = Path(work_dir)
    glb = export_assembly_glb(work_dir)
    urdf = export_assembly_urdf(work_dir, assembly_brief)
    views = collect_snapshot_views(work_dir)

    parts: list[str] = []
    parts_root = work_dir / "parts"
    if part_results:
        for pid, r in sorted(part_results.items()):
            step = getattr(r, "step_path", "")
            if not step:
                continue
            step_path = Path(step)
            try:
                rel = str(step_path.resolve().relative_to(work_dir.resolve()))
            except ValueError:
                rel = str(step_path)
            if step_path.is_file():
                parts.append(rel)
    elif parts_root.is_dir():
        # Fallback (no part results in state): newest STEP per part dir --
        # one entry per part, not one per intermediate artifact.
        from mac_assembly.file_utils import newest_file

        for part_dir in sorted(p for p in parts_root.iterdir() if p.is_dir()):
            step = newest_file(part_dir, "temp_output_*.step")
            if step is not None:
                parts.append(str(step.relative_to(work_dir)))

    manifest = {
        "assembly_name": getattr(assembly_brief, "assembly_name", None),
        "assembly_step": "assembly_output.step"
        if (work_dir / "assembly_output.step").is_file() else None,
        "assembly_stl": "assembly_output.stl"
        if (work_dir / "assembly_output.stl").is_file() else None,
        "assembly_glb": str(glb.relative_to(work_dir)) if glb else None,
        "assembly_urdf": str(urdf.relative_to(work_dir)) if urdf else None,
        "assembly_script": _newest_name(work_dir, "temp_assembly_*.py"),
        "snapshot_views": [str(v.relative_to(work_dir)) for v in views],
        "part_steps": parts,
        "qa_passed": bool(getattr(qa_report, "all_passed", False)),
        # Degraded-delivery visibility: qa_passed can be True while a part
        # was only partially generated (degraded PartResult kept for its
        # usable artifact). Downstream consumers should read a non-empty
        # list as "showcase quality, not a clean success".
        "degraded_part_ids": list(getattr(qa_report, "degraded_part_ids", None) or []),
        # BUG-046: surface GLB / URDF export failures so a missing
        # viewer-ready artifact is not silent. STEP can still be valid
        # when GLB/URDF failed.
        "handoff_warnings": _collect_handoff_warnings(work_dir, glb, urdf),
        "viewer_hint": (
            "Open assembly_output.glb in any glTF viewer (or the CAD Viewer "
            "skill). Per-part STEP sources are under parts/<part_id>/. "
            "URDF (for grasp simulators) is under urdf/."
        ),
    }
    (work_dir / "handoff_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return manifest


def export_assembly_urdf(work_dir: Path, assembly_brief=None) -> Path | None:
    """Export ``<work_dir>/urdf/<assembly_name>.urdf`` + ``meshes/``.

    Best-effort: returns ``None`` if URDF export fails (e.g. no
    ``assembly_mates.json`` or no per-part STLs). Mirrors
    ``export_assembly_glb``. ``export_urdf`` already swallows its own
    exceptions (returns None), so no extra try/except here (R6).
    """
    from mac_assembly.urdf_export import export_urdf

    return export_urdf(work_dir, brief=assembly_brief)


def _newest_name(directory: Path, pattern: str) -> str | None:
    """Newest matching file's name -- delegates to file_utils.newest_file
    (single source of truth, R3)."""
    from mac_assembly.file_utils import newest_file

    p = newest_file(directory, pattern)
    return p.name if p else None


def _collect_handoff_warnings(
    work_dir: Path, glb: Path | None, urdf: Path | None
) -> list[str]:
    """Collect non-fatal handoff warnings (BUG-046). A missing GLB /
    URDF does NOT invalidate the STEP delivery, but the operator must
    see WHY the viewer-ready artifact is absent."""
    warnings: list[str] = []
    log_path = work_dir / "handoff_error.log"
    log_exists = log_path.is_file()
    stl = work_dir / "assembly_output.stl"
    if glb is None and stl.is_file():
        # STL existed but GLB was not produced -- export_assembly_glb
        # may have logged the traceback to handoff_error.log.
        if log_exists:
            warnings.append(
                "GLB export failed (see handoff_error.log); "
                "STEP/STL remain valid"
            )
        else:
            warnings.append(
                "GLB export failed; no detailed exporter error was "
                "recorded. STEP/STL remain valid"
            )
    if urdf is None and (work_dir / "parts").is_dir():
        # URDF was not produced -- urdf_export returned None. Only
        # mention handoff_error.log if it actually exists.
        if log_exists:
            warnings.append(
                "URDF export skipped or failed (see handoff_error.log)"
            )
        else:
            warnings.append(
                "URDF export skipped or failed; no detailed exporter "
                "error was recorded"
            )
    return warnings


def print_handoff(manifest: dict, work_dir: Path) -> None:
    print()
    print("  Handoff packet:")
    if manifest.get("assembly_glb"):
        print(f"    GLB (viewer) : {work_dir / manifest['assembly_glb']}")
    if manifest.get("assembly_urdf"):
        print(f"    URDF (sim)   : {work_dir / manifest['assembly_urdf']}")
    if manifest.get("assembly_step"):
        print(f"    STEP         : {work_dir / manifest['assembly_step']}")
    if manifest.get("snapshot_views"):
        print(f"    snapshots    : {len(manifest['snapshot_views'])} PNG(s) in "
              f"{work_dir / 'assembly_judge_views'}")
    print(f"    manifest     : {work_dir / 'handoff_manifest.json'}")
    # BUG-046: surface handoff warnings so an operator sees them on
    # stdout without having to open the manifest.
    for w in manifest.get("handoff_warnings", []) or []:
        print(f"    WARNING      : {w}")
