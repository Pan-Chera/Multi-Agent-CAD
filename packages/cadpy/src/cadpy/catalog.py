from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

from .metadata import GeneratorMetadata, normalize_mesh_numeric, parse_generator_metadata


REPO_ROOT = Path.cwd().resolve()
CAD_ROOT = REPO_ROOT
STEP_SUFFIXES = (".step", ".stp")
EXPLORER_ARTIFACT_FILENAMES = {
    ".glb": "model.glb",
    ".topology.json": "topology.json",
    ".topology.bin": "topology.bin",
}
IGNORED_DISCOVERY_DIR_NAMES = {
    "__pycache__",
    ".cache",
    ".eggs",
    ".env",
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "build",
    "dist",
    "env",
    "node_modules",
    "site-packages",
    "venv",
}
GENERATOR_NAME_MARKERS = (b"gen_step",)


class CadSourceError(ValueError):
    pass


@dataclass(frozen=True)
class StepImportOptions:
    stl: str | None = None
    three_mf: str | None = None
    glb: str | None = None
    mesh_tolerance: float | None = None
    mesh_angular_tolerance: float | None = None

    @property
    def has_metadata(self) -> bool:
        return any(
            (
                self.stl is not None,
                self.three_mf is not None,
                self.glb is not None,
                self.mesh_tolerance is not None,
                self.mesh_angular_tolerance is not None,
            )
        )


@dataclass(frozen=True)
class CadSource:
    source_ref: str
    cad_ref: str
    kind: str
    source_path: Path
    source: str
    origin_path: Path
    script_path: Path | None = None
    generator_metadata: GeneratorMetadata | None = None
    step_path: Path | None = None
    stl_path: Path | None = None
    three_mf_path: Path | None = None
    native_glb_path: Path | None = None
    dxf_path: Path | None = None
    urdf_path: Path | None = None
    sdf_path: Path | None = None
    mesh_tolerance: float | None = None
    mesh_angular_tolerance: float | None = None
    color: tuple[float, float, float, float] | None = None

    @property
    def glb_path(self) -> Path | None:
        return explorer_artifact_path_for_step_path(self.step_path, ".glb") if self.step_path is not None else None

    @property
    def generated_paths(self) -> tuple[Path, ...]:
        paths: list[Path] = []
        if self.source == "generated":
            if self.step_path is not None:
                paths.append(self.step_path)
            if self.dxf_path is not None:
                paths.append(self.dxf_path)
            if self.urdf_path is not None:
                paths.append(self.urdf_path)
            if self.sdf_path is not None:
                paths.append(self.sdf_path)
        if self.stl_path is not None:
            paths.append(self.stl_path)
        if self.three_mf_path is not None:
            paths.append(self.three_mf_path)
        if self.native_glb_path is not None:
            paths.append(self.native_glb_path)
        if self.glb_path is not None:
            paths.append(self.glb_path)
        return tuple(path.resolve() for path in paths)


def iter_cad_sources(root: Path | None = None) -> tuple[CadSource, ...]:
    root = CAD_ROOT if root is None else root
    resolved_root = root.resolve()
    python_sources = _iter_python_sources(resolved_root)
    generated_step_paths = {
        source.step_path.resolve()
        for source in python_sources
        if source.step_path is not None
    }
    sources = [
        *python_sources,
        *_iter_step_sources(resolved_root, excluded_step_paths=generated_step_paths),
    ]
    by_cad_ref: dict[str, CadSource] = {}
    by_source_ref: dict[str, CadSource] = {}
    by_step_path: dict[Path, CadSource] = {}
    by_generated_path: dict[Path, CadSource] = {}
    for source in sources:
        existing = by_cad_ref.get(source.cad_ref)
        if existing is not None:
            raise CadSourceError(
                "Duplicate CAD STEP ref "
                f"{source.cad_ref!r}: {_source_label(existing)} and {_source_label(source)}"
            )
        by_cad_ref[source.cad_ref] = source
        existing_source = by_source_ref.get(source.source_ref)
        if existing_source is not None:
            raise CadSourceError(
                "Duplicate CAD source ref "
                f"{source.source_ref!r}: {_source_label(existing_source)} and {_source_label(source)}"
            )
        by_source_ref[source.source_ref] = source
        if source.step_path is not None:
            existing_step = by_step_path.get(source.step_path.resolve())
            if existing_step is not None:
                raise CadSourceError(
                    "Duplicate CAD STEP source "
                    f"{_relative_to_repo(source.step_path)}: {_source_label(existing_step)} and {_source_label(source)}"
            )
            by_step_path[source.step_path.resolve()] = source
        for generated_path in source.generated_paths:
            resolved_generated_path = generated_path.resolve()
            existing_generated = by_generated_path.get(resolved_generated_path)
            if existing_generated is not None and existing_generated.source_ref != source.source_ref:
                raise CadSourceError(
                    "Duplicate CAD generated output "
                    f"{_relative_to_repo(generated_path)}: "
                    f"{_source_label(existing_generated)} and {_source_label(source)}"
                )
            by_generated_path[resolved_generated_path] = source
    return tuple(sorted(by_cad_ref.values(), key=lambda source: source.source_ref))


def source_from_path(
    path: Path,
    *,
    step_kind: str = "part",
    step_options: StepImportOptions | None = None,
) -> CadSource | None:
    resolved = path.resolve()
    if resolved.suffix.lower() == ".py":
        return _read_python_source(resolved, allow_dxf_only=True)
    if resolved.suffix.lower() in STEP_SUFFIXES:
        return _read_step_source(resolved, kind=step_kind, options=step_options)
    return None


def source_by_cad_ref(root: Path | None = None) -> dict[str, CadSource]:
    return {source.cad_ref: source for source in iter_cad_sources(root)}


def find_source_by_cad_ref(cad_ref: str, root: Path | None = None) -> CadSource | None:
    normalized = normalize_cad_ref(cad_ref)
    return source_by_cad_ref(root).get(normalized or "")


def find_source_by_source_ref(source_ref: str, root: Path | None = None) -> CadSource | None:
    normalized = normalize_source_ref(source_ref)
    if not normalized:
        return None
    for source in iter_cad_sources(root):
        if source.source_ref == normalized:
            return source
    return None


def find_source_by_path(path: Path, root: Path | None = None) -> CadSource | None:
    resolved_path = path.resolve()
    for source in iter_cad_sources(root):
        paths = [
            source.source_path,
            source.step_path,
            source.script_path,
            source.dxf_path,
            source.urdf_path,
            source.sdf_path,
            *source.generated_paths,
        ]
        if any(candidate is not None and candidate.resolve() == resolved_path for candidate in paths):
            return source
    return None


def source_ref_from_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(CAD_ROOT.resolve())
    except ValueError:
        return resolved.as_posix()
    return relative.as_posix()


def cad_ref_from_step_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(CAD_ROOT.resolve())
    except ValueError:
        relative = PurePosixPath(resolved.as_posix())
    name = relative.name
    suffix = relative.suffix.lower()
    if suffix in STEP_SUFFIXES:
        return relative.with_suffix("").as_posix()
    raise CadSourceError(f"{_relative_to_repo(path)} is not a CAD STEP source")


def normalize_source_ref(raw_ref: str) -> str | None:
    normalized = str(raw_ref or "").replace("\\", "/").strip().strip("/")
    if not normalized:
        return None
    parts = normalized.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        return None
    return "/".join(parts)


def normalize_cad_ref(raw_ref: str) -> str | None:
    normalized = normalize_source_ref(raw_ref)
    if not normalized:
        return None
    suffix = PurePosixPath(normalized).suffix.lower()
    if suffix in {".py", *STEP_SUFFIXES}:
        normalized = str(PurePosixPath(normalized).with_suffix(""))
    return normalized


def artifact_path_for_step_path(step_path: Path, suffix: str) -> Path:
    return step_path.resolve().with_suffix(suffix)


def hidden_artifact_path_for_step_path(step_path: Path, suffix: str) -> Path:
    base = step_path.resolve()
    return base.with_name(f".{base.stem}{suffix}").resolve()


def explorer_directory_for_step_path(step_path: Path) -> Path:
    base = step_path.resolve()
    return (base.parent / f".{base.name}").resolve()


def hidden_glb_path_for_step_path(step_path: Path) -> Path:
    base = step_path.resolve()
    return (base.parent / f".{base.name}.glb").resolve()


def legacy_explorer_artifact_path_for_step_path(step_path: Path, suffix: str) -> Path:
    base = step_path.resolve()
    artifact_name = EXPLORER_ARTIFACT_FILENAMES.get(suffix)
    if artifact_name is None:
        raise ValueError(f"Unsupported STEP explorer artifact suffix: {suffix}")
    return (explorer_directory_for_step_path(base) / artifact_name).resolve()


def explorer_artifact_path_for_step_path(step_path: Path, suffix: str) -> Path:
    if suffix == ".glb":
        return hidden_glb_path_for_step_path(step_path)
    return legacy_explorer_artifact_path_for_step_path(step_path, suffix)


def _iter_python_sources(root: Path) -> tuple[CadSource, ...]:
    sources: list[CadSource] = []
    for script_path in _iter_paths(root, "*.py"):
        if not _looks_like_generator_script(script_path):
            continue
        source = _read_python_source(script_path)
        if source is not None:
            sources.append(source)
    return tuple(sources)


def _read_python_source(script_path: Path, *, allow_dxf_only: bool = False) -> CadSource | None:
    resolved_script_path = script_path.resolve()
    metadata = parse_generator_metadata(resolved_script_path)
    if metadata is None:
        return None
    if not metadata.has_gen_step:
        # Standalone DXF drafting source: valid as an explicit gen_dxf target,
        # but it has no STEP entry, so directory catalogs skip it.
        if not allow_dxf_only:
            return None
        return CadSource(
            source_ref=source_ref_from_path(resolved_script_path),
            cad_ref="",
            kind="dxf",
            source_path=resolved_script_path,
            source="generated",
            origin_path=resolved_script_path,
            script_path=resolved_script_path,
            generator_metadata=metadata,
            step_path=None,
            stl_path=None,
            three_mf_path=None,
            native_glb_path=None,
            dxf_path=resolved_script_path.with_suffix(".dxf"),
            urdf_path=None,
            sdf_path=None,
            mesh_tolerance=None,
            mesh_angular_tolerance=None,
        )
    if metadata.kind not in {"part", "assembly"}:
        raise CadSourceError(
            f"{_relative_to_repo(resolved_script_path)} must define a part or assembly gen_step() entry"
        )
    step_path = resolved_script_path.with_suffix(".step")
    dxf_path = resolved_script_path.with_suffix(".dxf") if metadata.has_gen_dxf else None
    urdf_path = resolved_script_path.with_suffix(".urdf") if metadata.has_gen_urdf else None
    sdf_path = resolved_script_path.with_suffix(".sdf") if metadata.has_gen_sdf else None
    return CadSource(
        source_ref=source_ref_from_path(resolved_script_path),
        cad_ref=cad_ref_from_step_path(step_path),
        kind=metadata.kind,
        source_path=resolved_script_path,
        source="generated",
        origin_path=resolved_script_path,
        script_path=resolved_script_path,
        generator_metadata=metadata,
        step_path=step_path,
        stl_path=None,
        three_mf_path=None,
        native_glb_path=None,
        dxf_path=dxf_path,
        urdf_path=urdf_path,
        sdf_path=sdf_path,
        mesh_tolerance=None,
        mesh_angular_tolerance=None,
    )


def _iter_step_sources(root: Path, *, excluded_step_paths: set[Path]) -> tuple[CadSource, ...]:
    sources: list[CadSource] = []
    for pattern in ("*.step", "*.stp"):
        for step_path in _iter_paths(root, pattern):
            if step_path.resolve() in excluded_step_paths:
                continue
            sources.append(_read_step_source(step_path, kind="part"))
    return tuple(sorted(sources, key=lambda source: source.source_ref))


def _read_step_source(
    step_path: Path,
    *,
    kind: str,
    options: StepImportOptions | None = None,
) -> CadSource:
    resolved_step_path = step_path.resolve()
    options = options or StepImportOptions()
    if kind not in {"part", "assembly"}:
        raise CadSourceError(f"{_relative_to_repo(resolved_step_path)} kind must be 'part' or 'assembly'")
    if resolved_step_path.suffix.lower() not in STEP_SUFFIXES:
        raise CadSourceError(f"{_relative_to_repo(resolved_step_path)} source must end in .step or .stp")
    if not resolved_step_path.is_file():
        raise CadSourceError(
            f"{_relative_to_repo(resolved_step_path)} source does not exist"
        )
    stl_path = (
        _resolve_configured_artifact_path(
            options.stl,
            base_path=resolved_step_path,
            default_path=None,
            expected_suffixes=(".stl",),
            field_name="stl",
        )
        if options.stl is not None
        else None
    )
    three_mf_path = (
        _resolve_configured_artifact_path(
            options.three_mf,
            base_path=resolved_step_path,
            default_path=None,
            expected_suffixes=(".3mf",),
            field_name="3mf",
        )
        if options.three_mf is not None
        else None
    )
    native_glb_path = (
        _resolve_configured_artifact_path(
            options.glb,
            base_path=resolved_step_path,
            default_path=None,
            expected_suffixes=(".glb",),
            field_name="glb",
        )
        if options.glb is not None
        else None
    )

    cad_ref = cad_ref_from_step_path(resolved_step_path)

    return CadSource(
        source_ref=source_ref_from_path(resolved_step_path),
        cad_ref=cad_ref,
        kind=str(kind),
        source_path=resolved_step_path,
        source="imported",
        origin_path=resolved_step_path,
        step_path=resolved_step_path,
        stl_path=stl_path,
        three_mf_path=three_mf_path,
        native_glb_path=native_glb_path,
        mesh_tolerance=normalize_step_numeric(
            options.mesh_tolerance,
            base_path=resolved_step_path,
            field_name="mesh_tolerance",
        ),
        mesh_angular_tolerance=normalize_step_numeric(
            options.mesh_angular_tolerance,
            base_path=resolved_step_path,
            field_name="mesh_angular_tolerance",
        ),
    )


def _iter_paths(root: Path, pattern: str) -> tuple[Path, ...]:
    paths: list[Path] = []
    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            dirname
            for dirname in dirnames
            if dirname not in IGNORED_DISCOVERY_DIR_NAMES
        )
        for filename in sorted(filenames):
            if not fnmatch(filename, pattern):
                continue
            path = (Path(current_root) / filename).resolve()
            if path.is_file():
                paths.append(path)
    return tuple(paths)


def _looks_like_generator_script(script_path: Path) -> bool:
    try:
        source_bytes = script_path.read_bytes()
    except OSError:
        return False
    return any(marker in source_bytes for marker in GENERATOR_NAME_MARKERS)


def normalize_step_numeric(raw_value: object, *, base_path: Path, field_name: str) -> float | None:
    try:
        return normalize_mesh_numeric(raw_value, field_name=field_name)
    except ValueError as exc:
        raise CadSourceError(f"{_relative_to_repo(base_path)} {exc}") from exc


def normalize_step_color(
    raw_value: object,
    *,
    base_path: Path,
    field_name: str,
) -> tuple[float, float, float, float] | None:
    if raw_value is None:
        return None
    if isinstance(raw_value, str):
        value = raw_value.strip()
        if value.startswith("#"):
            value = value[1:]
        if len(value) not in {6, 8}:
            raise CadSourceError(f"{_relative_to_repo(base_path)} {field_name} must be #RRGGBB or #RRGGBBAA")
        try:
            components = [int(value[index : index + 2], 16) / 255.0 for index in range(0, len(value), 2)]
        except ValueError as exc:
            raise CadSourceError(f"{_relative_to_repo(base_path)} {field_name} must be valid hex") from exc
    elif (
        isinstance(raw_value, Sequence)
        and not isinstance(raw_value, (bytes, bytearray))
        and len(raw_value) in {3, 4}
    ):
        components = []
        for component in raw_value:
            try:
                number = float(component)
            except (TypeError, ValueError) as exc:
                raise CadSourceError(
                    f"{_relative_to_repo(base_path)} {field_name} components must be numeric"
                ) from exc
            if not 0.0 <= number <= 1.0:
                raise CadSourceError(
                    f"{_relative_to_repo(base_path)} {field_name} components must be between 0 and 1"
                )
            components.append(number)
    else:
        raise CadSourceError(f"{_relative_to_repo(base_path)} {field_name} must be an RGB/RGBA array or hex string")
    if len(components) == 3:
        components.append(1.0)
    return (float(components[0]), float(components[1]), float(components[2]), float(components[3]))


def _resolve_configured_artifact_path(
    raw_value: object,
    *,
    base_path: Path,
    default_path: Path | None,
    expected_suffixes: tuple[str, ...],
    field_name: str,
) -> Path:
    if raw_value is None:
        if default_path is None:
            raise CadSourceError(f"{_relative_to_repo(base_path)} {field_name} is required")
        resolved = default_path.resolve()
    else:
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise CadSourceError(f"{_relative_to_repo(base_path)} {field_name} must be a non-empty string")
        value = raw_value.strip()
        if "\\" in value:
            raise CadSourceError(f"{_relative_to_repo(base_path)} {field_name} must use POSIX '/' separators")
        pure = PurePosixPath(value)
        if pure.is_absolute() or any(part in {"", "."} for part in pure.parts):
            raise CadSourceError(f"{_relative_to_repo(base_path)} {field_name} must be relative")
        resolved = (base_path.parent.resolve() / Path(*pure.parts)).resolve()
    suffix = resolved.suffix.lower()
    if suffix not in expected_suffixes:
        joined = " or ".join(expected_suffixes)
        raise CadSourceError(f"{_relative_to_repo(base_path)} {field_name} must end in {joined}")
    return resolved


def _required_output(raw_value: str | None, *, script_path: Path, field_name: str) -> str:
    if raw_value is None:
        raise CadSourceError(f"{_relative_to_repo(script_path)} {field_name} is required")
    return raw_value


def _source_label(source: CadSource) -> str:
    if source.script_path is not None:
        return _relative_to_repo(source.script_path)
    return _relative_to_repo(source.source_path)


def _relative_to_repo(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()
