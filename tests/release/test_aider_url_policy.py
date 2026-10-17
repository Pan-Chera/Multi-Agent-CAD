"""Aider may edit CAD scripts, but prompt URLs must not trigger web tooling."""
from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from multi_agent_cad import nodes


@pytest.mark.parametrize("stage", ["hybrid", "generation", "repair"])
@pytest.mark.parametrize("reference", ["", " Reference: https://example.com/bracket.txt"])
def test_aider_edits_without_opening_prompt_urls(monkeypatch, tmp_path, stage, reference):
    # If installed, exercise the library's real URL preprocessing too. The
    # factory contract remains tested in CI without the optional dependency.
    try:
        from aider.coders import Coder as RealCoder
    except ImportError:
        RealCoder = None

    script = tmp_path / "temp_design_0.py"
    original = "import build123d as bd\ndef gen_step():\n    return {'shape': bd.Box(10, 20, 30)}\n"
    script.write_text(original)
    request = "Create a 10x20x30 mm bracket." + reference
    created, prompts = [], []
    web = Mock(side_effect=AssertionError("URL scraping / installation must not run"))
    fallback = Mock(side_effect=AssertionError("No real model calls in this test"))
    monkeypatch.setattr(nodes, "_llm_client", fallback)
    # Production code sets these as part of Aider's provider setup.
    monkeypatch.setenv("DASHSCOPE_API_KEY", "issue11-dummy-key")
    for name in (nodes._API_BASE_ENV_VAR, nodes._API_KEY_ENV_VAR):
        if name:
            monkeypatch.setenv(name, "issue11-parent-value")

    class IO:
        def __init__(self, **kwargs):
            self.yes = kwargs["yes"]
            self.dry_run = kwargs["dry_run"]

        def confirm_ask(self, *args, **kwargs):
            return self.yes

    def create(**kwargs):
        created.append(kwargs)

        def run(prompt):
            prompts.append(prompt)
            if RealCoder is not None:
                coder = object.__new__(RealCoder)
                coder.detect_urls = kwargs.get("detect_urls", True)
                coder.rejected_urls = set()
                coder.io = kwargs["io"]
                coder.commands = SimpleNamespace(is_command=lambda _: False, cmd_web=web)
                coder.check_for_file_mentions = Mock()
                assert coder.preproc_user_input(prompt) == prompt
            script.write_text(original + "\n# edited by Aider\n")

        return SimpleNamespace(run=run, summarize_end=Mock())

    modules = {name: ModuleType(name) for name in
               ("aider", "aider.coders", "aider.models", "aider.io")}
    modules["aider.coders"].Coder = SimpleNamespace(create=create)
    modules["aider.models"].Model = lambda _: SimpleNamespace(extra_params={})
    modules["aider.io"].InputOutput = IO
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    if stage == "hybrid":
        code = original + "# TODO_AIDER: fillet\n# Feature data: {}\n"
        assert nodes._fill_unsupported_with_aider(script, code, request)
    elif stage == "generation":
        assert nodes.generate_initial_solution(script_path=str(script), user_request=request)
    else:
        ok, error = nodes._run_repair_on_script(
            script_path=str(script), user_request=request,
            error_details=["Width must remain 10 mm." + reference],
        )
        assert ok, error

    assert len(created) == 1
    settings = created[0]
    assert settings.get("detect_urls") is False
    assert settings["io"].yes is True
    assert settings["io"].dry_run is False
    assert settings["fnames"] == [str(script)]
    assert nodes._BUILD123D_REF in settings["read_only_fnames"]
    assert settings["auto_commits"] is False
    assert settings["add_gitignore_files"] is True
    assert request in prompts[0]
    assert "edited by Aider" in script.read_text()
    web.assert_not_called()
    fallback.assert_not_called()
