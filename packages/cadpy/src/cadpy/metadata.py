from __future__ import annotations

import ast
import math
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path.cwd().resolve()
CAD_ROOT = REPO_ROOT
DEFAULT_MESH_TOLERANCE = 0.02
DEFAULT_MESH_ANGULAR_TOLERANCE = 0.6


@dataclass(frozen=True)
class MeshSettings:
    tolerance: float
    angular_tolerance: float


@dataclass(frozen=True)
class GeneratorMetadata:
    script_path: Path
    kind: str
    display_name: str | None
    generator_names: tuple[str, ...]
    has_gen_step: bool
    has_gen_dxf: bool
    has_gen_urdf: bool
    has_gen_sdf: bool
    stl: str | None
    three_mf: str | None
    mesh_tolerance: float | None
    mesh_angular_tolerance: float | None


STEP_ENVELOPE_FIELDS = {
    "shape",
    "instances",
    "children",
    "assembly_mates",
    "stl",
    "3mf",
    "mesh_tolerance",
    "mesh_angular_tolerance",
}
DXF_ENVELOPE_FIELDS = {"document"}
URDF_ENVELOPE_FIELDS = {"xml"}
SDF_ENVELOPE_FIELDS = {"xml"}


DEFAULT_MESH_SETTINGS = MeshSettings(
    tolerance=DEFAULT_MESH_TOLERANCE,
    angular_tolerance=DEFAULT_MESH_ANGULAR_TOLERANCE,
)


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def normalize_mesh_numeric(value: object, *, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{field_name} must be finite")
    if normalized <= 0.0:
        raise ValueError(f"{field_name} must be greater than 0")
    return normalized


def resolve_mesh_settings(
    *,
    cad_ref: str,
    generator_metadata: GeneratorMetadata | None,
    mesh_tolerance: float | None = None,
    mesh_angular_tolerance: float | None = None,
) -> MeshSettings:
    tolerance = DEFAULT_MESH_SETTINGS.tolerance
    angular_tolerance = DEFAULT_MESH_SETTINGS.angular_tolerance
    if mesh_tolerance is not None:
        tolerance = mesh_tolerance
    if mesh_angular_tolerance is not None:
        angular_tolerance = mesh_angular_tolerance
    return MeshSettings(
        tolerance=tolerance,
        angular_tolerance=angular_tolerance,
    )


def parse_generator_metadata(script_path: Path) -> GeneratorMetadata | None:
    try:
        tree = ast.parse(script_path.read_text(), filename=str(script_path))
    except (FileNotFoundError, SyntaxError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Failed to parse {_display_path(script_path)}") from exc

    display_name: str | None = None
    kind: str | None = None
    has_gen_step = False
    has_gen_dxf = False
    has_gen_urdf = False
    has_gen_sdf = False
    generator_names: list[str] = []
    for node in tree.body:
        target: ast.expr | None = None
        value: ast.AST | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            value = node.value
        if isinstance(target, ast.Name) and value is not None:
            if target.id == "DISPLAY_NAME" and isinstance(value, ast.Constant) and isinstance(value.value, str):
                display_name = value.value.strip()

        if not isinstance(node, ast.FunctionDef) or node.name not in {"gen_step", "gen_dxf", "gen_urdf", "gen_sdf"}:
            continue
        generator_names.append(node.name)

        if node.args.args or node.args.posonlyargs or node.args.kwonlyargs:
            raise ValueError(
                f"{_display_path(script_path)} {node.name}() must not require arguments"
            )
        if node.args.vararg or node.args.kwarg:
            raise ValueError(
                f"{_display_path(script_path)} {node.name}() must not accept variadic arguments"
            )

        if node.decorator_list:
            raise ValueError(
                f"{_display_path(script_path)} {node.name}() must not use CAD generator decorators; "
                "return the generated content directly instead"
            )

        if node.name == "gen_step":
            kind = _parse_step_return_metadata(
                script_path=script_path,
                function=node,
            )
            has_gen_step = True
        elif node.name == "gen_dxf":
            _parse_dxf_envelope_metadata(
                script_path=script_path,
                function=node,
            )
            has_gen_dxf = True
        elif node.name == "gen_urdf":
            _parse_urdf_envelope_metadata(
                script_path=script_path,
                function=node,
            )
            has_gen_urdf = True
        else:
            _parse_sdf_envelope_metadata(
                script_path=script_path,
                function=node,
            )
            has_gen_sdf = True

    if not has_gen_step and not has_gen_dxf and not has_gen_urdf and not has_gen_sdf:
        return None
    if not has_gen_step and (has_gen_urdf or has_gen_sdf):
        raise ValueError(
            f"{_display_path(script_path)} gen_urdf() and gen_sdf() require gen_step()"
        )

    return GeneratorMetadata(
        script_path=script_path.resolve(),
        kind=kind,
        display_name=display_name,
        generator_names=tuple(generator_names),
        has_gen_step=has_gen_step,
        has_gen_dxf=has_gen_dxf,
        has_gen_urdf=has_gen_urdf,
        has_gen_sdf=has_gen_sdf,
        stl=None,
        three_mf=None,
        mesh_tolerance=None,
        mesh_angular_tolerance=None,
    )


def _parse_step_return_metadata(
    *,
    script_path: Path,
    function: ast.FunctionDef,
) -> str:
    return_node = _single_return_value(script_path=script_path, function=function)
    local_assignments = _function_local_assignments(function)
    if not isinstance(return_node, ast.Dict):
        return _parse_bare_step_return(
            script_path=script_path,
            function=function,
            return_node=return_node,
            local_assignments=local_assignments,
        )

    envelope = _parse_literal_return_envelope(script_path=script_path, function=function)
    _reject_unsupported_fields(
        script_path=script_path,
        function_name=function.name,
        envelope=envelope,
        allowed_fields=STEP_ENVELOPE_FIELDS,
    )
    has_shape = "shape" in envelope
    has_instances = "instances" in envelope
    has_children = "children" in envelope
    has_assembly = has_instances or has_children
    if has_instances and has_children:
        raise ValueError(
            f"{_display_path(script_path)} gen_step() envelope must define only one of "
            "'instances' or 'children'"
        )
    if has_shape == has_assembly:
        raise ValueError(
            f"{_display_path(script_path)} gen_step() envelope must define exactly one of "
            "'shape', 'instances', or 'children'"
        )
    if has_shape:
        return (
            "assembly"
            if _is_compound_assembly_expression(
                envelope["shape"],
                local_assignments=local_assignments,
            )
            else "part"
        )
    return "assembly"


def _parse_bare_step_return(
    *,
    script_path: Path,
    function: ast.FunctionDef,
    return_node: ast.expr,
    local_assignments: dict[str, ast.expr] | None = None,
) -> str:
    if isinstance(return_node, ast.List):
        return "assembly"
    if isinstance(return_node, ast.Name) and return_node.id in {"instances", "children"}:
        return "assembly"
    if _is_compound_assembly_expression(
        return_node,
        local_assignments=local_assignments or {},
    ):
        return "assembly"
    if isinstance(return_node, ast.Constant) and return_node.value is None:
        raise ValueError(
            f"{_display_path(script_path)} {function.name}() must return a shape, assembly list, "
            "or legacy envelope dict"
        )
    return "part"


def _function_local_assignments(function: ast.FunctionDef) -> dict[str, ast.expr]:
    assignments: dict[str, ast.expr] = {}
    for statement in function.body:
        if isinstance(statement, ast.Return):
            break
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            value = statement.value
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.value, ast.expr):
            target = statement.target
            value = statement.value
        if isinstance(target, ast.Name) and value is not None:
            assignments[target.id] = value
    return assignments


def _is_compound_assembly_expression(
    expression: ast.expr,
    *,
    local_assignments: dict[str, ast.expr],
    seen_names: set[str] | None = None,
) -> bool:
    if isinstance(expression, ast.Name):
        seen_names = set(seen_names or set())
        if expression.id in seen_names:
            return False
        target = local_assignments.get(expression.id)
        if target is None:
            return False
        seen_names.add(expression.id)
        return _is_compound_assembly_expression(
            target,
            local_assignments=local_assignments,
            seen_names=seen_names,
        )
    if not isinstance(expression, ast.Call) or _call_tail_name(expression.func) != "Compound":
        return False
    if expression.args and _is_multi_item_sequence_expression(
        expression.args[0],
        local_assignments=local_assignments,
    ):
        return True
    for keyword in expression.keywords:
        if keyword.arg == "children" and _is_nonempty_expression(keyword.value):
            return True
        if keyword.arg == "obj" and _is_multi_item_sequence_expression(
            keyword.value,
            local_assignments=local_assignments,
        ):
            return True
    return False


def _call_tail_name(function: ast.expr) -> str | None:
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        return function.attr
    return None


def _is_nonempty_expression(expression: ast.expr) -> bool:
    if isinstance(expression, ast.Constant) and expression.value is None:
        return False
    if isinstance(expression, (ast.List, ast.Tuple, ast.Set)):
        return bool(expression.elts)
    return True


def _is_multi_item_sequence_expression(
    expression: ast.expr,
    *,
    local_assignments: dict[str, ast.expr],
    seen_names: set[str] | None = None,
) -> bool:
    if isinstance(expression, ast.Name):
        seen_names = set(seen_names or set())
        if expression.id in seen_names:
            return False
        target = local_assignments.get(expression.id)
        if target is None:
            return False
        seen_names.add(expression.id)
        return _is_multi_item_sequence_expression(
            target,
            local_assignments=local_assignments,
            seen_names=seen_names,
        )
    if isinstance(expression, (ast.List, ast.Tuple, ast.Set)):
        return len(expression.elts) > 1
    return False


def _parse_dxf_envelope_metadata(
    *,
    script_path: Path,
    function: ast.FunctionDef,
) -> str | None:
    return_node = _single_return_value(script_path=script_path, function=function)
    if not isinstance(return_node, ast.Dict):
        return None
    envelope = _parse_literal_return_envelope(script_path=script_path, function=function)
    _reject_unsupported_fields(
        script_path=script_path,
        function_name=function.name,
        envelope=envelope,
        allowed_fields=DXF_ENVELOPE_FIELDS,
    )
    if "document" not in envelope:
        raise ValueError(f"{_display_path(script_path)} gen_dxf() envelope must define 'document'")
    return None


def _parse_urdf_envelope_metadata(
    *,
    script_path: Path,
    function: ast.FunctionDef,
) -> str | None:
    return_node = _single_return_value(script_path=script_path, function=function)
    if not isinstance(return_node, ast.Dict):
        return None
    envelope = _parse_literal_return_envelope(script_path=script_path, function=function)
    _reject_unsupported_fields(
        script_path=script_path,
        function_name=function.name,
        envelope=envelope,
        allowed_fields=URDF_ENVELOPE_FIELDS,
    )
    if "xml" not in envelope:
        raise ValueError(f"{_display_path(script_path)} gen_urdf() envelope must define 'xml'")
    return None


def _parse_sdf_envelope_metadata(
    *,
    script_path: Path,
    function: ast.FunctionDef,
) -> str | None:
    return_node = _single_return_value(script_path=script_path, function=function)
    if not isinstance(return_node, ast.Dict):
        return None
    envelope = _parse_literal_return_envelope(script_path=script_path, function=function)
    _reject_unsupported_fields(
        script_path=script_path,
        function_name=function.name,
        envelope=envelope,
        allowed_fields=SDF_ENVELOPE_FIELDS,
    )
    if "xml" not in envelope:
        raise ValueError(f"{_display_path(script_path)} gen_sdf() envelope must define 'xml'")
    return None


def _parse_literal_return_envelope(
    *,
    script_path: Path,
    function: ast.FunctionDef,
) -> dict[str, ast.expr]:
    value = _single_return_value(script_path=script_path, function=function)
    if not isinstance(value, ast.Dict):
        raise ValueError(
            f"{_display_path(script_path)} {function.name}() must return a generator envelope dict"
        )
    envelope: dict[str, ast.expr] = {}
    for key_node, value_node in zip(value.keys, value.values, strict=True):
        if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
            raise ValueError(
                f"{_display_path(script_path)} {function.name}() envelope keys must be string literals"
            )
        key = key_node.value
        if key in envelope:
            raise ValueError(
                f"{_display_path(script_path)} {function.name}() envelope duplicate field: {key}"
            )
        envelope[key] = value_node
    return envelope


def _single_return_value(
    *,
    script_path: Path,
    function: ast.FunctionDef,
) -> ast.expr:
    returns = [statement for statement in function.body if isinstance(statement, ast.Return)]
    if len(returns) != 1 or returns[0].value is None:
        raise ValueError(
            f"{_display_path(script_path)} {function.name}() must return one value"
        )
    return returns[0].value


def _reject_unsupported_fields(
    *,
    script_path: Path,
    function_name: str,
    envelope: dict[str, ast.expr],
    allowed_fields: set[str],
) -> None:
    extra_fields = sorted(key for key in envelope if key not in allowed_fields)
    if extra_fields:
        joined = ", ".join(extra_fields)
        raise ValueError(
            f"{_display_path(script_path)} {function_name}() envelope has unsupported field(s): {joined}"
        )


def _literal_field(
    *,
    script_path: Path,
    function_name: str,
    envelope: dict[str, ast.expr],
    field_name: str,
) -> object | None:
    if field_name not in envelope:
        return None
    try:
        return ast.literal_eval(envelope[field_name])
    except (ValueError, SyntaxError) as exc:
        raise ValueError(
            f"{_display_path(script_path)} {function_name}() envelope {field_name} must be a literal"
        ) from exc


def _parse_path_field(
    *,
    script_path: Path,
    function_name: str,
    envelope: dict[str, ast.expr],
    field_name: str,
) -> str | None:
    value = _literal_field(
        script_path=script_path,
        function_name=function_name,
        envelope=envelope,
        field_name=field_name,
    )
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{_display_path(script_path)} {function_name}() envelope {field_name} "
            "must be a non-empty string"
        )
    if "\\" in value:
        raise ValueError(
            f"{_display_path(script_path)} {function_name}() envelope {field_name} "
            "must use POSIX '/' separators"
        )
    return value.strip()
