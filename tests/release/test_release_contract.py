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
