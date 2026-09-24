"""Small, stable release checks that do not call an external model.

The exhaustive maintainer regression suite intentionally remains local.  This
file protects only public packaging, security defaults, prompts, schemas, and
the final status contract.
"""

from __future__ import annotations

import ast
import contextlib
import io
import re
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _assignment_value(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {path}")


def test_public_packages_import() -> None:
    import cadpy  # noqa: F401
    import mac_assembly  # noqa: F401
    import multi_agent_cad  # noqa: F401


def test_distribution_includes_both_workflows() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    includes = data["tool"]["setuptools"]["packages"]["find"]["include"]
    assert "multi_agent_cad*" in includes
    assert "mac_assembly*" in includes
    assert "cadpy*" in includes


def test_tracked_config_contains_no_api_key() -> None:
    assert _assignment_value(ROOT / "multi_agent_cad/config.py", "DS_API_KEY") == ""


def test_web_ui_is_local_and_server_copy_is_opt_in() -> None:
    source = (ROOT / "multi_agent_cad/web/server.py").read_text(encoding="utf-8")
    assert '_DEFAULT_HOST = "127.0.0.1"' in source
    assert 'MAC_WEB_ALLOW_DEST_PATH' in source
    assert re.search(r"if dest_path and .*ALLOW_DEST_PATH", source)


def test_security_policy_does_not_claim_subprocess_sandbox() -> None:
    text = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "not a security sandbox" in text
    assert "same operating-system permissions" in text


def test_natural_language_benchmark_prompts_are_plain_prose() -> None:
    prompt_dir = ROOT / "mac_assembly/assembly_prompts/natural_language_benchmarks"
    prompts = sorted(prompt_dir.glob("*.md"))
    assert len(prompts) >= 4
    for path in prompts:
        text = path.read_text(encoding="utf-8")
        assert len(text.split()) >= 40, path.name
        assert "```" not in text, path.name
        assert not re.search(r"\b(?:json|python)\s*[:=]", text, re.I), path.name
        assert not re.search(r"\b(?:AssemblyHelper|mirror_part|add_mate)\b", text), path.name


def test_assembly_schema_defaults_support_text_only_providers() -> None:
    from mac_assembly.schemas_assembly import AssemblyQAReport

    report = AssemblyQAReport()
    assert report.semantic_verification == "unverified"
    assert report.semantic_modification_suggestions == []


def test_semantic_failure_can_never_be_reported_as_pass() -> None:
    from mac_assembly.graph_assembly import _final_report
    from mac_assembly.schemas_assembly import (
        AssemblyJudgeAction,
        AssemblyJudgeDecision,
        AssemblyQAReport,
    )

    qa = AssemblyQAReport(
        all_passed=True,
        semantic_verification="failed",
        semantic_issues=["moving jaw is mirrored incorrectly"],
    )
    decision = AssemblyJudgeDecision(
        action=AssemblyJudgeAction.REPAIR_ASSEMBLY,
        confidence="high",
        reason="Visual semantics do not match the request.",
    )
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        _final_report({"qa_report": qa, "judge_decision": decision})
    output = stream.getvalue()
    assert "INCOMPLETE (visual semantic verification failed)" in output
    assert "STATUS  : PASS" not in output


def test_readme_local_markdown_links_exist() -> None:
    for readme in (ROOT / "README.md", ROOT / "README_cn.md"):
        text = readme.read_text(encoding="utf-8")
        for target in re.findall(r"!?(?:\[[^]]*\])\(([^)]+)\)", text):
            if "://" in target or target.startswith("#"):
                continue
            clean = target.split("#", 1)[0]
            assert (ROOT / clean).exists(), f"broken link in {readme.name}: {target}"


def test_assembly_prompt_builders_registered() -> None:
    """Every builder.name referenced by a tracked assembly prompt must be
    a registered key in BUILDERS, so the PartBuilder dispatch can resolve it.
    Helper functions in builders.py are intentionally not registered -- this
    test only enforces the contract from the prompt side.
    """
    from mac_assembly.builders import BUILDERS

    prompt_root = ROOT / "mac_assembly" / "assembly_prompts"
    referenced: dict[str, list[str]] = {}
    for path in sorted(prompt_root.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"^\s*builder\.name\s*=\s*([A-Za-z_][A-Za-z0-9_]*)", text, re.MULTILINE):
            name = match.group(1)
            referenced.setdefault(name, []).append(str(path.relative_to(ROOT)))
    assert referenced, "expected at least one builder.name reference in assembly prompts"
    missing = [name for name in referenced if name not in BUILDERS]
    assert not missing, (
        f"assembly prompts reference builders missing from BUILDERS: "
        f"{ {name: referenced[name] for name in missing} }"
    )


def test_bent_jaw_xz_smoke() -> None:
    """build_part('bent_jaw_xz', ...) must dispatch to the builder and
    return a Compound -- guards the BUG-001 regression where the builder
    was defined but absent from BUILDERS.
    """
    pytest.importorskip("build123d")
    from mac_assembly.builders import build_part

    result = build_part("bent_jaw_xz", {"side": "left"})
    assert result is not None
    assert hasattr(result, "solids")
    solids = list(result.solids())
    assert len(solids) == 1


def _extract_json_cases() -> list[tuple[str, str, str]]:
    return [
        (
            "single_fence",
            '```json\n{"a": 1}\n```',
            '{"a": 1}',
        ),
        (
            "example_then_actual",
            'Sure, here is an example:\n```json\n{"example": 1}\n```\n'
            'And the actual answer:\n```json\n{"actual": 2}\n```',
            '{"actual": 2}',
        ),
        (
            "prose_then_single_fence",
            'Prose text before.\n```json\n{"x": 7}\n```',
            '{"x": 7}',
        ),
        (
            "no_fence_with_inline_object",
            'text {"no": "fence"} trailing',
            '{"no": "fence"}',
        ),
        (
            "two_fences_last_with_surrounding_prose",
            '```json\n{"first": 1}\n```\nprose between\n'
            '```json\nleading prose {"last": 2} trailing prose\n```',
            '{"last": 2}',
        ),
    ]


@pytest.mark.parametrize("name, raw, expected", _extract_json_cases())
def test_extract_json_last_fenced_block(name: str, raw: str, expected: str) -> None:
    """BUG-003: `_extract_json_from_llm` must return the LAST fenced JSON
    block (not the first), matching `_extract_code_from_llm_response`'s
    last-block-wins contract.  The no-fence fallback and the outermost
    ``{ ... }`` span extraction inside a fence must be preserved.
    """
    import json

    from multi_agent_cad.nodes import _extract_json_from_llm

    result = _extract_json_from_llm(raw)
    assert result == expected, f"[{name}] expected {expected!r}, got {result!r}"
    # The extracted string must be valid JSON for all five cases.
    json.loads(result)


def test_build123d_ref_not_in_editable_fnames() -> None:
    """BUG-007: every `Coder.create(...)` call in `multi_agent_cad/nodes.py`
    must pass `_BUILD123D_REF` via `read_only_fnames=[...]` (read-only
    context) -- never via `fnames=[...]` (editable files).  Otherwise the
    LLM can edit the build123d reference doc, and the corruption persists
    across runs.  `script_path` must remain in `fnames`; `auto_commits` and
    `add_gitignore_files` must not be silently dropped.
    """
    source = (ROOT / "multi_agent_cad" / "nodes.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    coder_calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "Coder"
            and node.func.attr == "create"
        ):
            coder_calls.append(node)
    assert coder_calls, "expected at least one Coder.create call in nodes.py"

    problems: list[str] = []
    for call in coder_calls:
        fnames_kws = [kw for kw in call.keywords if kw.arg == "fnames"]
        readonly_kws = [kw for kw in call.keywords if kw.arg == "read_only_fnames"]
        auto_kws = [kw for kw in call.keywords if kw.arg == "auto_commits"]
        gitignore_kws = [kw for kw in call.keywords if kw.arg == "add_gitignore_files"]

        if not fnames_kws:
            problems.append("Coder.create call is missing fnames=[...]")
            continue
        fnames_list = fnames_kws[0].value
        if not isinstance(fnames_list, ast.List):
            problems.append(f"Coder.create fnames is not a list literal: {ast.dump(fnames_list)}")
            continue
        fnames_elems = [
            elt.id if isinstance(elt, ast.Name) else
            (ast.literal_eval(elt) if isinstance(elt, (ast.Str, ast.Constant)) else "<expr>")
            for elt in fnames_list.elts
        ]
        if "_BUILD123D_REF" in fnames_elems:
            problems.append(
                f"BUG-007 regression: _BUILD123D_REF in fnames={fnames_elems}"
            )
        if not any(
            isinstance(elt, ast.Name) and elt.id == "script_path"
            or (isinstance(elt, ast.Call) and isinstance(elt.func, ast.Name) and elt.func.id == "str"
                and any(isinstance(a, ast.Name) and a.id == "script_path" for a in elt.args))
            for elt in fnames_list.elts
        ):
            problems.append(
                f"Coder.create fnames={fnames_elems} is missing script_path"
            )

        if not readonly_kws:
            problems.append("Coder.create call is missing read_only_fnames=[...]")
            continue
        readonly_list = readonly_kws[0].value
        if not isinstance(readonly_list, ast.List):
            problems.append(
                f"Coder.create read_only_fnames is not a list literal: {ast.dump(readonly_list)}"
            )
            continue
        readonly_elems = [
            elt.id if isinstance(elt, ast.Name) else
            (ast.literal_eval(elt) if isinstance(elt, (ast.Str, ast.Constant)) else "<expr>")
            for elt in readonly_list.elts
        ]
        if "_BUILD123D_REF" not in readonly_elems:
            problems.append(
                f"BUG-007 regression: _BUILD123D_REF not in read_only_fnames={readonly_elems}"
            )

        if not auto_kws or not isinstance(auto_kws[0].value, ast.Constant) or auto_kws[0].value.value is not False:
            problems.append("Coder.create must keep auto_commits=False")
        if not gitignore_kws or not isinstance(gitignore_kws[0].value, ast.Constant) or gitignore_kws[0].value.value is not True:
            problems.append("Coder.create must keep add_gitignore_files=True")

    assert not problems, "BUG-007 / Aider-contract regression: " + "; ".join(problems)
