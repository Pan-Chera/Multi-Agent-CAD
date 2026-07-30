from __future__ import annotations

import importlib.util
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath

from cadpy.catalog import find_source_by_cad_ref
from cadpy.metadata import parse_generator_metadata


REPO_ROOT = Path.cwd().resolve()
CAD_ROOT = REPO_ROOT
STEP_SUFFIXES = (".step", ".stp")
INSTANCE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
IDENTITY_TRANSFORM = (
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
)


class AssemblySpecError(ValueError):
    pass


@dataclass(frozen=True)
class AssemblyInstanceSpec:
    instance_id: str
    source_path: Path
    path: str
    name: str
    transform: tuple[float, ...]
    use_source_colors: bool = True


@dataclass(frozen=True)
class AssemblyNodeSpec:
    instance_id: str
    name: str
    transform: tuple[float, ...]
    use_source_colors: bool = True
    source_path: Path | None = None
    path: str | None = None
    children: tuple["AssemblyNodeSpec", ...] = ()


@dataclass(frozen=True)
class AssemblySpec:
    assembly_path: Path
    instances: tuple[AssemblyInstanceSpec, ...]
    children: tuple[AssemblyNodeSpec, ...] = ()


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def find_step_path(cad_ref: str) -> Path | None:
    source = find_source_by_cad_ref(cad_ref)
    if source is not None and source.kind in {"part", "assembly"}:
        return source.step_path.resolve() if source.step_path is not None else None
    return None


def resolve_cad_source_path(cad_ref: str) -> tuple[str, Path] | None:
    source = find_source_by_cad_ref(cad_ref)
    if source is not None:
        if source.kind == "assembly":
            return "assembly", source.source_path
        if source.kind == "part":
            step_path = source.step_path
            return ("part", step_path) if step_path is not None else None
    return None


def multiply_transforms(left: tuple[float, ...], right: tuple[float, ...]) -> tuple[float, ...]:
    product: list[float] = []
    for row in range(4):
        for column in range(4):
            total = 0.0
            for offset in range(4):
                total += left[(row * 4) + offset] * right[(offset * 4) + column]
            product.append(total)
    return tuple(product)


def read_assembly_spec(assembly_path: Path) -> AssemblySpec:
    resolved_path = assembly_path.resolve()
    payload = _run_assembly_generator(resolved_path)
    return assembly_spec_from_payload(resolved_path, payload)


def assembly_spec_from_payload(assembly_path: Path, payload: object) -> AssemblySpec:
    resolved_path = assembly_path.resolve()

    if isinstance(payload, list):
        payload = {"children": payload}
    if not isinstance(payload, dict):
        raise AssemblySpecError(f"{_display_path(resolved_path)} gen_step() must return an assembly list or object")

    allowed_fields = {"instances", "children"}
    extra_fields = sorted(str(key) for key in payload if key not in allowed_fields)
    if extra_fields:
        joined = ", ".join(extra_fields)
        raise AssemblySpecError(
            f"{_display_path(resolved_path)} has unsupported assembly field(s): {joined}"
        )

    raw_instances = payload.get("instances")
    raw_children = payload.get("children")
    has_instances = "instances" in payload
    has_children = "children" in payload
    if has_instances == has_children:
        raise AssemblySpecError(
            f"{_display_path(resolved_path)} must define exactly one of non-empty instances or children"
        )

    if has_instances:
        if not isinstance(raw_instances, list) or not raw_instances:
            raise AssemblySpecError(f"{_display_path(resolved_path)} must define a non-empty instances array")
        instances = tuple(
            _parse_instance_spec(resolved_path, raw_instance, field_name=f"instances[{index}]")
            for index, raw_instance in enumerate(raw_instances, start=1)
        )
        _reject_duplicate_sibling_names(resolved_path, instances, field_name="instances")
        children = tuple(_node_from_instance(instance) for instance in instances)
    else:
        if not isinstance(raw_children, list) or not raw_children:
            raise AssemblySpecError(f"{_display_path(resolved_path)} must define a non-empty children array")
        children = tuple(
            _parse_node_spec(resolved_path, raw_child, field_name=f"children[{index}]")
            for index, raw_child in enumerate(raw_children, start=1)
        )
        _reject_duplicate_sibling_names(resolved_path, children, field_name="children")
        instances = tuple(_leaf_instances_from_nodes(children))

    return AssemblySpec(
        assembly_path=resolved_path,
        instances=instances,
        children=children,
    )


def assembly_spec_children(assembly_spec: AssemblySpec) -> tuple[AssemblyNodeSpec, ...]:
    if assembly_spec.children:
        return assembly_spec.children
    return tuple(_node_from_instance(instance) for instance in assembly_spec.instances)


def _node_from_instance(instance: AssemblyInstanceSpec) -> AssemblyNodeSpec:
    return AssemblyNodeSpec(
        instance_id=instance.instance_id,
        name=instance.name,
        transform=instance.transform,
        use_source_colors=instance.use_source_colors,
        source_path=instance.source_path,
        path=instance.path,
        children=(),
    )


def _leaf_instances_from_nodes(nodes: tuple[AssemblyNodeSpec, ...]) -> tuple[AssemblyInstanceSpec, ...]:
    instances: list[AssemblyInstanceSpec] = []
    for node in nodes:
        if node.children:
            instances.extend(_leaf_instances_from_nodes(node.children))
            continue
        if node.source_path is None or node.path is None:
            raise AssemblySpecError(f"Assembly leaf node {node.instance_id!r} is missing a STEP path")
        instances.append(
            AssemblyInstanceSpec(
                instance_id=node.instance_id,
                source_path=node.source_path,
                path=node.path,
                name=node.name,
                transform=node.transform,
                use_source_colors=node.use_source_colors,
            )
        )
    return tuple(instances)


def _run_assembly_generator(assembly_path: Path) -> object:
    try:
        generator_metadata = parse_generator_metadata(assembly_path)
    except Exception as exc:
        raise AssemblySpecError(f"Failed to parse {_display_path(assembly_path)}") from exc
    if generator_metadata is None or generator_metadata.kind != "assembly":
        raise AssemblySpecError(
            f"{_display_path(assembly_path)} must define a gen_step() assembly return"
        )

    module_name = (
        "_cad_assembly_"
        + _display_path(assembly_path).replace("/", "_").replace("\\", "_").replace("-", "_").replace(".", "_")
    )
    module_spec = importlib.util.spec_from_file_location(module_name, assembly_path)
    if module_spec is None or module_spec.loader is None:
        raise AssemblySpecError(f"Failed to load assembly generator: {_display_path(assembly_path)}")

    module = importlib.util.module_from_spec(module_spec)
    original_sys_path = list(sys.path)
    search_paths = [
        str(REPO_ROOT),
        str(CAD_ROOT),
        str(assembly_path.parent),
    ]
    for parent in assembly_path.parents:
        if parent == REPO_ROOT.parent:
            break
        if (
            (parent / "STEP" / "__init__.py").is_file()
            or (parent / "robot_common" / "__init__.py").is_file()
        ):
            search_paths.append(str(parent))
    for candidate in reversed(search_paths):
        if candidate not in sys.path:
            sys.path.insert(0, candidate)

    try:
        sys.modules[module_name] = module
        module_spec.loader.exec_module(module)
    except Exception as exc:
        raise AssemblySpecError(f"{_display_path(assembly_path)} failed while loading") from exc
    finally:
        sys.path[:] = original_sys_path

    gen_step = getattr(module, "gen_step", None)
    if not callable(gen_step):
        raise AssemblySpecError(f"{_display_path(assembly_path)} does not define a callable gen_step()")
    try:
        envelope = gen_step()
    except Exception as exc:
        raise AssemblySpecError(f"{_display_path(assembly_path)} gen_step() failed") from exc
    if isinstance(envelope, list):
        return {"children": envelope}
    if not isinstance(envelope, dict) or ("instances" not in envelope and "children" not in envelope):
        raise AssemblySpecError(
            f"{_display_path(assembly_path)} gen_step() must return an assembly list or legacy envelope with instances or children"
        )
    return {key: envelope[key] for key in ("instances", "children") if key in envelope}


def _parse_instance_spec(assembly_path: Path, raw_instance: object, *, field_name: str) -> AssemblyInstanceSpec:
    if not isinstance(raw_instance, dict):
        raise AssemblySpecError(f"{_display_path(assembly_path)} {field_name} must be an object")
    allowed_instance_fields = {"path", "name", "transform", "use_source_colors"}
    extra_instance_fields = sorted(str(key) for key in raw_instance if key not in allowed_instance_fields)
    if extra_instance_fields:
        joined = ", ".join(extra_instance_fields)
        raise AssemblySpecError(
            f"{_display_path(assembly_path)} {field_name} has unsupported field(s): {joined}"
        )
    name = _normalize_instance_name(
        assembly_path,
        raw_instance.get("name"),
        field_name=f"{field_name}.name",
    )
    raw_path = _require_text(
        assembly_path,
        raw_instance.get("path"),
        field_name=f"{field_name}.path",
    )
    source_path, normalized_path = _resolve_instance_step_path(
        assembly_path,
        raw_path,
        field_name=f"{field_name}.path",
    )
    return AssemblyInstanceSpec(
        instance_id=name,
        source_path=source_path,
        path=normalized_path,
        name=name,
        transform=_normalize_transform(
            assembly_path,
            raw_instance.get("transform"),
            field_name=f"{field_name}.transform",
        ),
        use_source_colors=_normalize_bool(
            assembly_path,
            raw_instance.get("use_source_colors", True),
            field_name=f"{field_name}.use_source_colors",
        ),
    )


def _parse_node_spec(assembly_path: Path, raw_node: object, *, field_name: str) -> AssemblyNodeSpec:
    if not isinstance(raw_node, dict):
        raise AssemblySpecError(f"{_display_path(assembly_path)} {field_name} must be an object")
    allowed_node_fields = {"path", "name", "transform", "use_source_colors", "children"}
    extra_node_fields = sorted(str(key) for key in raw_node if key not in allowed_node_fields)
    if extra_node_fields:
        joined = ", ".join(extra_node_fields)
        raise AssemblySpecError(
            f"{_display_path(assembly_path)} {field_name} has unsupported field(s): {joined}"
        )
    name = _normalize_instance_name(
        assembly_path,
        raw_node.get("name"),
        field_name=f"{field_name}.name",
    )
    transform = _normalize_transform(
        assembly_path,
        raw_node.get("transform"),
        field_name=f"{field_name}.transform",
    )
    use_source_colors = _normalize_bool(
        assembly_path,
        raw_node.get("use_source_colors", True),
        field_name=f"{field_name}.use_source_colors",
    )
    source_path: Path | None = None
    normalized_path: str | None = None
    if "path" in raw_node:
        raw_path = _require_text(
            assembly_path,
            raw_node.get("path"),
            field_name=f"{field_name}.path",
        )
        source_path, normalized_path = _resolve_instance_step_path(
            assembly_path,
            raw_path,
            field_name=f"{field_name}.path",
        )

    raw_children = raw_node.get("children")
    children: tuple[AssemblyNodeSpec, ...] = ()
    if "children" in raw_node:
        if not isinstance(raw_children, list) or not raw_children:
            raise AssemblySpecError(f"{_display_path(assembly_path)} {field_name}.children must be a non-empty array")
        children = tuple(
            _parse_node_spec(assembly_path, child, field_name=f"{field_name}.children[{index}]")
            for index, child in enumerate(raw_children, start=1)
        )
        _reject_duplicate_sibling_names(assembly_path, children, field_name=f"{field_name}.children")
    elif source_path is None:
        raise AssemblySpecError(f"{_display_path(assembly_path)} {field_name} must define path or children")

    return AssemblyNodeSpec(
        instance_id=name,
        name=name,
        transform=transform,
        use_source_colors=use_source_colors,
        source_path=source_path,
        path=normalized_path,
        children=children,
    )


def _normalize_instance_name(assembly_path: Path, raw_value: object, *, field_name: str) -> str:
    name = _require_text(assembly_path, raw_value, field_name=field_name)
    if not INSTANCE_NAME_PATTERN.fullmatch(name):
        raise AssemblySpecError(
            f"{_display_path(assembly_path)} {field_name} must contain only "
            "letters, numbers, '.', '_', or '-'"
        )
    return name


def _reject_duplicate_sibling_names(
    assembly_path: Path,
    nodes: tuple[AssemblyInstanceSpec, ...] | tuple[AssemblyNodeSpec, ...],
    *,
    field_name: str,
) -> None:
    seen: set[str] = set()
    for index, node in enumerate(nodes, start=1):
        name = str(node.instance_id)
        if name in seen:
            raise AssemblySpecError(
                f"{_display_path(assembly_path)} {field_name}[{index}].name duplicates {name!r}"
            )
        seen.add(name)


def _require_text(assembly_path: Path, raw_value: object, *, field_name: str) -> str:
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise AssemblySpecError(
            f"{_display_path(assembly_path)} {field_name} must be a non-empty string"
        )
    return raw_value.strip()


def _normalize_bool(assembly_path: Path, raw_value: object, *, field_name: str) -> bool:
    if isinstance(raw_value, bool):
        return raw_value
    raise AssemblySpecError(f"{_display_path(assembly_path)} {field_name} must be true or false")


def _normalize_transform(
    assembly_path: Path,
    raw_value: object,
    *,
    field_name: str,
) -> tuple[float, ...]:
    if not isinstance(raw_value, (list, tuple)) or len(raw_value) != 16:
        raise AssemblySpecError(
            f"{_display_path(assembly_path)} {field_name} must be a 16-number array"
        )
    values: list[float] = []
    for index, raw_number in enumerate(raw_value, start=1):
        if isinstance(raw_number, bool) or not isinstance(raw_number, (int, float)):
            raise AssemblySpecError(
                f"{_display_path(assembly_path)} {field_name}[{index}] must be a number"
            )
        value = float(raw_number)
        if not math.isfinite(value):
            raise AssemblySpecError(
                f"{_display_path(assembly_path)} {field_name}[{index}] must be finite"
            )
        values.append(value)
    return tuple(values)


def _resolve_instance_step_path(
    assembly_path: Path,
    raw_path: str,
    *,
    field_name: str,
) -> tuple[Path, str]:
    if "\\" in raw_path:
        raise AssemblySpecError(f"{_display_path(assembly_path)} {field_name} must use POSIX '/' separators")
    pure = PurePosixPath(raw_path)
    if pure.is_absolute() or any(part in {"", "."} for part in pure.parts):
        raise AssemblySpecError(
            f"{_display_path(assembly_path)} {field_name} must be a relative STEP path"
        )
    if pure.suffix.lower() not in STEP_SUFFIXES:
        raise AssemblySpecError(f"{_display_path(assembly_path)} {field_name} must end in .step or .stp")

    resolved = (assembly_path.parent / Path(*pure.parts)).resolve()
    if resolved.is_file():
        return resolved, pure.as_posix()

    raise AssemblySpecError(
        f"{_display_path(assembly_path)} {field_name} does not resolve to a STEP file: {raw_path!r}"
    )
