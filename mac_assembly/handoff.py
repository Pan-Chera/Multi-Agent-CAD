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
    """Export ``assembly_output.stl`` -> ``assembly_output.glb`` (trimesh)."""
    stl = assembly_dir / "assembly_output.stl"
    glb = assembly_dir / "assembly_output.glb"
    if not stl.is_file():
        return None
    try:
        import trimesh

        mesh = trimesh.load(str(stl), force="mesh")
        mesh.export(str(glb), file_type="glb")
        return glb if glb.is_file() else None
    except Exception:  # noqa: BLE001 - GLB is best-effort
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
) -> dict:
    """Assemble the handoff packet and write ``handoff_manifest.json``.

    Returns the manifest dict. All paths are relative to ``work_dir`` so the
    packet is portable.
    """
    work_dir = Path(work_dir)
    glb = export_assembly_glb(work_dir)
    urdf = export_assembly_urdf(work_dir, assembly_brief)
    views = collect_snapshot_views(work_dir)

    parts = []
    parts_root = work_dir / "parts"
    if parts_root.is_dir():
        for step in sorted(parts_root.glob("*/temp_output_*.step")):
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
    ``export_assembly_glb``.
    """
    try:
        from mac_assembly.urdf_export import export_urdf

        return export_urdf(work_dir, brief=assembly_brief)
    except Exception:  # noqa: BLE001 - URDF is best-effort
        return None


def _newest_name(directory: Path, pattern: str) -> str | None:
    cands = [p for p in directory.glob(pattern) if p.is_file()]
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_mtime).name


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
