from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from cadpy.assembly_spec import (
    AssemblySpec,
    AssemblySpecError,
    AssemblyNodeSpec,
    IDENTITY_TRANSFORM,
    assembly_spec_children,
    multiply_transforms,
    read_assembly_spec,
)
from cadpy.catalog import cad_ref_from_step_path, find_source_by_path, source_from_path
from cadpy.render import existing_part_glb_path, part_glb_path, relative_to_repo


class AssemblyResolutionError(ValueError):
    pass


@dataclass(frozen=True)
class CatalogEntry:
    cad_ref: str
    source_ref: str
    kind: str
    source_path: Path
    step_path: Path | None = None
    assembly_spec: AssemblySpec | None = None
    stl_path: Path | None = None
    glb_path: Path | None = None


@dataclass(frozen=True)
class ResolvedPartInstance:
    instance_path: tuple[str, ...]
    cad_ref: str
    name: str | None
    color: str | None
    use_source_colors: bool
    step_path: Path
    stl_path: Path | None
    glb_path: Path
    transform: tuple[float, ...]


EntryResolver = Callable[[Path], CatalogEntry | None]


def filesystem_entry(source_path: Path) -> CatalogEntry | None:
    resolved = source_path.resolve()
    if resolved.suffix.lower() in {".step", ".stp"}:
        source = find_source_by_path(resolved)
        if source is not None and source.kind == "assembly" and source.script_path is not None:
            try:
                assembly_spec = read_assembly_spec(source.source_path)
            except AssemblySpecError as exc:
                raise AssemblyResolutionError(str(exc)) from exc
            return CatalogEntry(
                cad_ref=source.cad_ref,
                source_ref=source.source_ref,
                kind="assembly",
                source_path=source.source_path,
                step_path=source.step_path,
                assembly_spec=assembly_spec,
            )
        if source is not None and source.step_path is not None:
            return CatalogEntry(
                cad_ref=source.cad_ref,
                source_ref=source.source_ref,
                kind="part",
                source_path=source.source_path,
                step_path=source.step_path,
                stl_path=source.stl_path,
                glb_path=existing_part_glb_path(source.step_path) or part_glb_path(source.step_path),
            )
        if not resolved.is_file():
            return None
        cad_ref = cad_ref_from_step_path(resolved)
        return CatalogEntry(
            cad_ref=cad_ref,
            source_ref=relative_to_repo(resolved),
            kind="part",
            source_path=resolved,
            step_path=resolved,
            glb_path=existing_part_glb_path(resolved) or part_glb_path(resolved),
        )
    source = source_from_path(resolved) if resolved.exists() else None
    if source is None:
        source = find_source_by_path(source_path)
    if source is None:
        return None
    if source.kind == "assembly" and source.script_path is not None:
        try:
            assembly_spec = read_assembly_spec(source.source_path)
        except AssemblySpecError as exc:
            raise AssemblyResolutionError(str(exc)) from exc
        return CatalogEntry(
            cad_ref=source.cad_ref,
            source_ref=source.source_ref,
            kind="assembly",
            source_path=source.source_path,
            assembly_spec=assembly_spec,
        )
    if source.step_path is None:
        return None
    return CatalogEntry(
        cad_ref=source.cad_ref,
        source_ref=source.source_ref,
        kind="part",
        source_path=source.source_path,
        step_path=source.step_path,
        stl_path=source.stl_path,
        glb_path=existing_part_glb_path(source.step_path) or part_glb_path(source.step_path),
    )


def flatten_entry(
    entry: CatalogEntry,
    *,
    resolve_entry: EntryResolver,
    parent_transform: tuple[float, ...] = IDENTITY_TRANSFORM,
    parent_color: str | None = None,
    parent_use_source_colors: bool = True,
    instance_path: tuple[str, ...] = (),
    stack: tuple[str, ...] = (),
) -> tuple[ResolvedPartInstance, ...]:
    if entry.kind == "part":
        if entry.step_path is None or entry.glb_path is None:
            raise AssemblyResolutionError(f"Part source {entry.source_ref} is missing STEP or GLB source paths")
        return (
            ResolvedPartInstance(
                instance_path=instance_path or (entry.cad_ref,),
                cad_ref=entry.cad_ref,
                name=None,
                color=parent_color,
                use_source_colors=parent_use_source_colors,
                step_path=entry.step_path,
                stl_path=entry.stl_path,
                glb_path=entry.glb_path,
                transform=parent_transform,
            ),
        )

    assembly_spec = entry.assembly_spec
    if assembly_spec is None:
        raise AssemblyResolutionError(f"Assembly source {entry.source_ref} is missing assembly spec data")
    if entry.source_ref in stack:
        cycle = " -> ".join((*stack, entry.source_ref))
        raise AssemblyResolutionError(f"Assembly cycle detected: {cycle}")

    resolved_parts: list[ResolvedPartInstance] = []
    next_stack = (*stack, entry.source_ref)
    for child in assembly_spec_children(assembly_spec):
        resolved_parts.extend(
            _flatten_node(
                child,
                parent_source_ref=entry.source_ref,
                resolve_entry=resolve_entry,
                parent_transform=parent_transform,
                parent_color=parent_color,
                parent_use_source_colors=parent_use_source_colors,
                instance_path=instance_path,
                stack=next_stack,
            )
        )
    return tuple(resolved_parts)


def _flatten_node(
    node: AssemblyNodeSpec,
    *,
    parent_source_ref: str,
    resolve_entry: EntryResolver,
    parent_transform: tuple[float, ...],
    parent_color: str | None,
    parent_use_source_colors: bool,
    instance_path: tuple[str, ...],
    stack: tuple[str, ...],
) -> tuple[ResolvedPartInstance, ...]:
    child_transform = multiply_transforms(parent_transform, node.transform)
    child_color = parent_color
    child_use_source_colors = parent_use_source_colors and node.use_source_colors
    child_instance_path = (*instance_path, node.instance_id) if instance_path else (node.instance_id,)

    if node.children:
        resolved_parts: list[ResolvedPartInstance] = []
        for child in node.children:
            resolved_parts.extend(
                _flatten_node(
                    child,
                    parent_source_ref=parent_source_ref,
                    resolve_entry=resolve_entry,
                    parent_transform=child_transform,
                    parent_color=child_color,
                    parent_use_source_colors=child_use_source_colors,
                    instance_path=child_instance_path,
                    stack=stack,
                )
            )
        return tuple(resolved_parts)

    if node.source_path is None or node.path is None:
        raise AssemblyResolutionError(f"Assembly source {parent_source_ref} contains node {node.name!r} without a STEP path")
    child_entry = resolve_entry(node.source_path)
    if child_entry is None:
        raise AssemblyResolutionError(
            f"Assembly source {parent_source_ref} references missing CAD source {node.path}"
        )
    if child_entry.kind == "part":
        if child_entry.step_path is None or child_entry.glb_path is None:
            raise AssemblyResolutionError(
                f"Part source {child_entry.source_ref} is missing STEP or GLB source paths"
            )
        return (
            ResolvedPartInstance(
                instance_path=child_instance_path,
                cad_ref=child_entry.cad_ref,
                name=node.name,
                color=child_color,
                use_source_colors=child_use_source_colors,
                step_path=child_entry.step_path,
                stl_path=child_entry.stl_path,
                glb_path=child_entry.glb_path,
                transform=child_transform,
            ),
        )
    return flatten_entry(
        child_entry,
        resolve_entry=resolve_entry,
        parent_transform=child_transform,
        parent_color=child_color,
        parent_use_source_colors=child_use_source_colors,
        instance_path=child_instance_path,
        stack=stack,
    )


def flatten_source_path(
    source_path: Path,
    *,
    resolve_entry: EntryResolver = filesystem_entry,
) -> tuple[ResolvedPartInstance, ...]:
    entry = resolve_entry(source_path)
    if entry is None:
        raise AssemblyResolutionError(f"CAD source not found: {source_path}")
    return flatten_entry(entry, resolve_entry=resolve_entry)
