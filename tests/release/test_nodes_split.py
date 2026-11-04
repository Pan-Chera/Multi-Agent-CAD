"""Compatibility checks for extracted node helpers; no external model calls."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from openai import OpenAI

from multi_agent_cad import nodes
from multi_agent_cad.schemas import CADBrief


def test_planning_caches_follow_job_override_without_cross_job_reuse(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(nodes, "_CFG_SP_MULTIMODAL", "never")
    monkeypatch.setattr(nodes, "_llm_client", lambda: object())
    calls = []

    def reply(*args, **kwargs):
        calls.append(nodes._CACHE_DIR)
        brief = CADBrief(part_name=nodes._CACHE_DIR.parent.name,
                         part_category="flat-plate", user_request_raw="plate")
        return brief.model_dump_json(), "offline response"

    monkeypatch.setattr(nodes, "_call_llm_json_with_retry", reply)
    for job in ("first", "second", "first"):
        cache = tmp_path / job / "pipeline_cache"
        monkeypatch.setattr(nodes, "_CACHE_DIR", cache)
        result = nodes.node_spec_planner({"user_request": "plate"})
        assert result["cad_brief"].part_name == job
        assert json.loads((cache / "cad_brief.json").read_text())["part_name"] == job
    assert calls == [tmp_path / job / "pipeline_cache" for job in ("first", "second")]


def test_repair_helper_honors_live_facade_dependencies_and_token_context(monkeypatch, tmp_path):
    # Import the graph as a caller does: its token tracker wraps the facade's
    # node entry points. SDK calls below use an in-memory HTTP transport.
    from multi_agent_cad import graph
    from multi_agent_cad.token_tracker import _current_module, _detect_module, tracker

    monkeypatch.setattr(tracker, "_calls", [])
    monkeypatch.setattr(nodes, "_REPAIR_MODEL", "offline-repair-model")
    monkeypatch.setattr(nodes, "_REPAIR_KWARGS", {})
    monkeypatch.setattr(nodes, "_CFG_LLM_CODEGEN_API_TIMEOUT", 123)
    requests = []
    output = "# repaired by the offline fixture\n" + "value = 1\n" * 8

    def response(request):
        requests.append(request)
        # Context is also visible from an async callback, as needed by Aider.
        async def callback_context():
            return _detect_module()
        assert asyncio.run(callback_context()) == "node_python_coder"
        return httpx.Response(200, json={
            "id": "offline", "object": "chat.completion", "created": 0,
            "model": "offline-repair-model",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": output}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        })

    with OpenAI(api_key="offline-placeholder", base_url="https://offline.invalid/v1",
                http_client=httpx.Client(transport=httpx.MockTransport(response))) as client:
        monkeypatch.setattr(nodes, "_llm_client", lambda: client)
        source = tmp_path / "design.py"
        source.write_text("# original\n", encoding="utf-8")

        def deterministic(*args):
            ok, error = nodes._run_direct_repair_fallback(
                script_path_obj=source, error_details=["wrong size"], user_request="plate",
            )
            assert ok, error
            return {"offline_repaired": True}

        monkeypatch.setattr(nodes, "_node_python_coder_deterministic", deterministic)
        assert graph.node_python_coder({"architect_plan": object()}) == {"offline_repaired": True}

    assert source.read_text(encoding="utf-8") == output.strip()
    assert json.loads(requests[0].content)["model"] == "offline-repair-model"
    assert requests[0].extensions["timeout"]["read"] == 123
    summary = tracker.summary()
    assert summary["n_calls"] == 1
    assert summary["total_tokens"] == 18
    assert summary["per_module"]["node_python_coder"]["total"] == 18
    assert _current_module.get() == "unknown"


def test_token_context_is_restored_when_extracted_helper_raises(monkeypatch):
    from multi_agent_cad import graph
    from multi_agent_cad.token_tracker import _current_module

    def broken(*args):
        assert _current_module.get() == "node_python_coder"
        raise RuntimeError("offline execution failure")

    monkeypatch.setattr(nodes, "_node_python_coder_deterministic", broken)
    with pytest.raises(RuntimeError, match="offline execution failure"):
        graph.node_python_coder({"architect_plan": object()})
    assert _current_module.get() == "unknown"


def test_child_environment_override_reaches_extracted_execution(monkeypatch, tmp_path):
    """Changing the historical hook must affect the actual child execution."""
    monkeypatch.setattr(nodes, "_build_child_env", lambda iteration: {
        "MAC_SPLIT_MARKER": str(iteration), "PYTHONIOENCODING": "utf-8",
    })
    script = tmp_path / "design.py"
    script.write_text('''import os
from pathlib import Path
assert os.environ["MAC_SPLIT_MARKER"] == "9"
Path("part.step").write_text("step")
Path("part.stl").write_text("stl")
''', encoding="utf-8")
    ok, error = nodes._execute_cad_script(script, tmp_path / "part.step",
                                          tmp_path / "part.stl", iteration=9)
    assert ok, error


def test_extracted_step_qa_uses_replaced_topology_loader(monkeypatch):
    from multi_agent_cad.schemas import VerificationTarget

    calls = []
    monkeypatch.setattr(nodes, "_load_step_topology", lambda path: (
        calls.append(path) or (None, None, None, None, None, None, "offline topology failure")
    ))
    target = VerificationTarget(id="volume", description="volume", kind="volume", expected_value=1)
    report = nodes._run_engine_a_cadpy(
        step_path="offline.step", cad_brief={"verification_targets": [target]},
        engine_b_mesh_resolution={},
    )
    assert calls == ["offline.step"]
    assert "offline topology failure" in str(report.errors)
