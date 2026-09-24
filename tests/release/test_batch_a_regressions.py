"""Tracked runtime regression guards for Batch A fixes.

These tests exercise the runtime contracts behind the Batch A production
fixes -- they do not call any external model.

* BUG-002: ``_node_python_coder_deterministic`` routes a non-
  ``NotImplementedError`` from ``_plan_to_code`` to a failure-state dict
  (preserving ``coder`` in ``node_history``) and returns ``None`` for
  ``NotImplementedError`` to trigger the LLM fallback.  Before the fix,
  ``cwd`` was first assigned inside the ``try`` body, so the ``except``
  handler raised ``UnboundLocalError`` and the autonomous repair loop
  never saw the failure.

* BUG-007: Aider's three ``Coder.create(...)`` call sites in
  ``multi_agent_cad/nodes.py`` pass ``_BUILD123D_REF`` via
  ``read_only_fnames=[...]`` (read-only context), never via editable
  ``fnames=[...]``.  ``script_path`` remains editable; ``auto_commits=False``
  and ``add_gitignore_files=True`` are preserved.  Before the fix, the
  reference doc was editable and the LLM could corrupt it across runs.

The BUG-002 tests run in CI.  The BUG-007 tests self-skip when aider is
not installed (e.g. the release-contract CI job, which uses only the
core dependencies).  They run locally and in any environment that has
``aider-chat`` installed.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from multi_agent_cad import nodes as mac_nodes


# ---------------------------------------------------------------------------
# BUG-002 — runtime failure routing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "exc",
    [
        KeyError("missing field"),
        TypeError("bad type"),
        ValueError("bad value"),
        RuntimeError("runtime issue"),
        AttributeError("missing attribute"),
    ],
    ids=lambda exc: type(exc).__name__,
)
def test_coder_deterministic_failure_routing_returns_failure_state(exc):
    """A non-NotImplementedError from _plan_to_code must route to a
    failure-state dict -- not raise UnboundLocalError.  ``coder`` must
    appear in ``node_history`` and the failure description must reach
    ``qa_report.error_details`` and ``execution_log`` so the autonomous
    repair loop can consume it.
    """
    with mock.patch("multi_agent_cad.nodes._plan_to_code", side_effect=exc):
        result = mac_nodes._node_python_coder_deterministic(
            state={}, architect_plan=object(), iteration=0
        )

    assert isinstance(result, dict), (
        f"{type(exc).__name__} must produce a failure-state dict, not raise"
    )
    assert "coder" in result["node_history"], (
        "coder round must be recorded in node_history"
    )
    qa_report = result.get("qa_report")
    assert qa_report is not None, "failure state must carry a qa_report"
    error_details = getattr(qa_report, "error_details", []) or []
    assert any("Deterministic coder code generation failed" in d for d in error_details), (
        f"qa_report.error_details must carry the failure description; got: {error_details}"
    )
    log_lines = result.get("execution_log", []) or []
    assert any("Deterministic coder code generation failed" in line for line in log_lines), (
        f"execution_log must carry the failure description; got: {log_lines}"
    )


def test_coder_deterministic_not_implemented_returns_none_for_llm_fallback():
    """NotImplementedError must return None so the workflow falls back to the
    LLM coder -- this is the intentional escape hatch, not a crash.
    """
    with mock.patch(
        "multi_agent_cad.nodes._plan_to_code",
        side_effect=NotImplementedError("unsupported op"),
    ):
        result = mac_nodes._node_python_coder_deterministic(
            state={}, architect_plan=object(), iteration=0
        )
    assert result is None


# ---------------------------------------------------------------------------
# BUG-007 — Aider keeps _BUILD123D_REF read-only at all three call sites
# ---------------------------------------------------------------------------
try:
    import aider  # noqa: F401
    _HAS_AIDER = True
except ImportError:
    _HAS_AIDER = False


_ORIGINAL_CODE = (
    "import build123d as bd\n"
    "\n"
    "def gen_step():\n"
    "    body = bd.Box(60, 40, 12)\n"
    "    body = bd.Pos(0, 0, 6) * body\n"
    "    return {'shape': body}\n"
)

_CODE_WITH_PLACEHOLDER = (
    "import build123d as bd\n"
    "\n"
    "def gen_step():\n"
    "    body = bd.Box(60, 40, 12)\n"
    "    # TODO_AIDER: add fillet on top edges\n"
    "    # Feature data: {\"fillet_radius\": 3.0}\n"
    "    return {'shape': body}\n"
)


@pytest.fixture
def aider_script(tmp_path):
    """Write a fake script and set a dummy API key so the production code's
    preflight checks pass.  Aider's Coder/Model/InputOutput are mocked per
    test, so no network call is made.
    """
    script = tmp_path / "temp_design_0.py"
    script.write_text(_ORIGINAL_CODE, encoding="utf-8")
    with mock.patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key-123"}):
        yield script


@pytest.fixture
def patched_aider(aider_script):
    """Patch Aider's public Coder/Model/InputOutput classes.

    The production code imports them lazily inside each call site, so
    patching at the module level is sufficient.  Pattern mirrors
    ``tests/test_aider_summarizer_join.py``.  ``coder.run`` is wired to
    append a marker line to ``aider_script`` so each call site's post-run
    edit detection passes.
    """
    coder = mock.MagicMock(name="coder")

    def _fake_run(prompt):
        aider_script.write_text(
            aider_script.read_text(encoding="utf-8") + "\n# aider edit\n",
            encoding="utf-8",
        )

    coder.run.side_effect = _fake_run

    coder_cls = mock.patch("aider.coders.Coder")
    model_cls = mock.patch("aider.models.Model")
    io_cls = mock.patch("aider.io.InputOutput")
    c, m, i = coder_cls.start(), model_cls.start(), io_cls.start()
    c.create.return_value = coder
    coder.create_mock = c
    try:
        yield coder
    finally:
        for p in (coder_cls, model_cls, io_cls):
            p.stop()


def _assert_ref_read_only(coder, script):
    """The BUG-007 contract: _BUILD123D_REF is read-only, script_path is
    editable, and auto_commits/add_gitignore_files are preserved.
    """
    kwargs = coder.create_mock.create.call_args.kwargs
    assert "fnames" in kwargs, "Coder.create must be called with fnames=[...]"
    assert "read_only_fnames" in kwargs, (
        "Coder.create must be called with read_only_fnames=[...]"
    )

    fnames = list(kwargs["fnames"])
    readonly = list(kwargs["read_only_fnames"])

    assert mac_nodes._BUILD123D_REF not in fnames, (
        f"BUG-007: _BUILD123D_REF must not be in editable fnames={fnames}"
    )
    assert mac_nodes._BUILD123D_REF in readonly, (
        f"BUG-007: _BUILD123D_REF must be in read_only_fnames={readonly}"
    )
    script_str = str(script)
    assert any(str(elem) == script_str for elem in fnames), (
        f"script_path must be in editable fnames={fnames}"
    )
    assert kwargs.get("auto_commits") is False, "auto_commits=False must be preserved"
    assert kwargs.get("add_gitignore_files") is True, (
        "add_gitignore_files=True must be preserved"
    )


@pytest.mark.skipif(not _HAS_AIDER, reason="aider not installed in this environment")
def test_fill_unsupported_with_aider_passes_ref_read_only(aider_script, patched_aider):
    ok = mac_nodes._fill_unsupported_with_aider(
        aider_script, _CODE_WITH_PLACEHOLDER, "a test part"
    )
    assert ok
    patched_aider.create_mock.create.assert_called_once()
    _assert_ref_read_only(patched_aider, aider_script)


@pytest.mark.skipif(not _HAS_AIDER, reason="aider not installed in this environment")
def test_generate_initial_solution_passes_ref_read_only(aider_script, patched_aider):
    ok = mac_nodes.generate_initial_solution(
        script_path=str(aider_script), user_request="a test part"
    )
    assert ok
    patched_aider.create_mock.create.assert_called_once()
    _assert_ref_read_only(patched_aider, aider_script)


@pytest.mark.skipif(not _HAS_AIDER, reason="aider not installed in this environment")
def test_run_repair_on_script_passes_ref_read_only(aider_script, patched_aider):
    ok = mac_nodes._run_repair_on_script(
        script_path=str(aider_script),
        error_details=["MISSED_CUT: step-06-bore — tool had NO effect"],
        user_request="a test part",
    )
    assert ok
    patched_aider.create_mock.create.assert_called_once()
    _assert_ref_read_only(patched_aider, aider_script)
