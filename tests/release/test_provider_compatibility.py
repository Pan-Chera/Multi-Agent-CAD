"""Provider compatibility without real credentials, network, or model calls."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from multi_agent_cad import config as cfg, nodes
from multi_agent_cad.schemas import CADBrief, VerificationTarget
from multi_agent_cad.web import server


def _target(**values):
    return dict(id="width", kind="overall_dimension", description="Width", nominal=10, **values)


@pytest.mark.parametrize("values,expected", [
    ({}, (0.1, 0.1)),
    ({"tolerance_upper": None, "tolerance_lower": None}, (0.1, 0.1)),
    ({"tolerance_upper": None, "tolerance_lower": 0}, (0.1, 0)),
    ({"tolerance_upper": 0, "tolerance_lower": None}, (0, 0.1)),
    ({"tolerance_upper": 0.25, "tolerance_lower": 0.5}, (0.25, 0.5)),
    ({"tolerance_upper": "0.25", "tolerance_lower": "0"}, (0.25, 0)),
])
def test_nested_tolerances(values, expected):
    brief = CADBrief.model_validate(dict(
        part_name="plate", part_category="flat-plate", user_request_raw="10 mm plate",
        verification_targets=[_target(**values)],
    ))
    target = brief.verification_targets[0]
    assert (target.tolerance_upper, target.tolerance_lower) == expected
    assert CADBrief.model_validate_json(brief.model_dump_json()) == brief


@pytest.mark.parametrize("value", ["invalid", [], {}])
def test_invalid_tolerances_still_rejected(value):
    with pytest.raises(ValidationError):
        VerificationTarget.model_validate(_target(tolerance_upper=value))


def test_planner_accepts_null_tolerances_without_retry(monkeypatch, tmp_path):
    response = json.dumps(dict(
        part_name="plate", part_category="flat-plate", user_request_raw="10 mm plate",
        verification_targets=[_target(tolerance_upper=None, tolerance_lower=None)],
    ))
    monkeypatch.setattr(nodes, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(nodes, "_CFG_SP_MULTIMODAL", "never")
    monkeypatch.setattr(nodes, "_llm_client", lambda: object())
    calls = []

    def reply(*args, **kwargs):
        calls.append(kwargs)
        return response, response

    monkeypatch.setattr(nodes, "_call_llm_json_with_retry", reply)
    result = nodes.node_spec_planner({"user_request": "10 mm plate", "force_refresh": True})
    assert len(calls) == 1
    assert result["cad_brief"].verification_targets[0].tolerance_upper == 0.1
    assert "SUCCESS" in result["execution_log"][0]
    assert (tmp_path / "cad_brief.json").is_file()


@pytest.fixture
def web_probe(monkeypatch, tmp_path):
    for key in ("MAC_WEB_HOST", "MAC_WEB_ALLOW_CUSTOM_ENDPOINT", "DASHSCOPE_API_KEY",
                "MAC_WEB_ALLOW_DEST_PATH", "MAC_WEB_DEST_ROOT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(cfg, "DS_API_KEY", "")
    server._JOBS.clear()
    spawns = []

    async def lines():
        yield b'{"done":true}\n'

    async def spawn(*args, **kwargs):
        spawns.append(kwargs)
        return SimpleNamespace(returncode=0, stdout=lines(), wait=AsyncMock(return_value=0))

    def job_dir(prefix):
        path = tmp_path / prefix
        path.mkdir()
        return str(path)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(server.tempfile, "mkdtemp", job_dir)
    with TestClient(server.app, base_url="http://127.0.0.1:8000") as client:
        yield client, spawns
    server._JOBS.clear()


def _post(client, **body):
    return client.post("/api/run", headers={"X-MAC-CSRF": "1"},
                       json={"prompt": "a plate", **body})


@pytest.mark.parametrize("form,request_key,env_key,file_key,expected", [
    ("form-key", "request-key", "env-key", "file-key", "form-key"),
    ("", "request-key", "env-key", "file-key", "request-key"),
    ("", "", "env-key", "file-key", "env-key"),
    ("", "", "", "file-key", "file-key"),
    (None, None, "env-key", "file-key", "env-key"),
    (" \t", " \n", " env-key ", "file-key", "env-key"),
])
def test_web_key_precedence_and_no_persistence(
    web_probe, monkeypatch, form, request_key, env_key, file_key, expected,
):
    client, spawns = web_probe
    monkeypatch.setenv("DASHSCOPE_API_KEY", env_key)
    monkeypatch.setattr(cfg, "DS_API_KEY", file_key)
    r = _post(client, api_key=form, config={"DS_API_KEY": request_key})
    assert r.status_code == 200, r.text
    assert spawns[0]["env"]["DASHSCOPE_API_KEY"] == expected
    job_config = Path(spawns[0]["env"]["MAC_CONFIG_FILE"]).read_text()
    assert "DS_API_KEY" not in json.loads(job_config)
    for key in ("form-key", "request-key", "env-key", "file-key"):
        assert key not in job_config
        assert key not in r.text
    schema = client.get("/api/config/schema")
    assert "DS_API_KEY" not in schema.json()["config"]
    assert "env-key" not in schema.text
    assert "file-key" not in schema.text


def test_no_key_still_rejected(web_probe):
    client, spawns = web_probe
    r = _post(client, api_key="  ")
    assert r.status_code == 400
    assert "api_key is required" in r.json()["detail"]
    assert not spawns


def test_openrouter_preset_runs_with_existing_safety_checks(web_probe, monkeypatch):
    client, spawns = web_probe
    preset = client.get("/api/config/schema").json()["providers"]["openrouter"]
    monkeypatch.setenv("DASHSCOPE_API_KEY", "env-key")
    r = _post(client, config={"DS_BASE_URL": preset["ds_base_url"],
                              "AIDER_MODEL": preset["aider_model_hint"]})
    assert r.status_code == 200, r.text
    saved = json.loads(Path(spawns[0]["env"]["MAC_CONFIG_FILE"]).read_text())
    assert saved["DS_BASE_URL"] == "https://openrouter.ai/api/v1"
    assert saved["AIDER_MODEL"] == "openai/" + preset["model_hint"]


@pytest.mark.parametrize("url", [
    "https://openrouter.ai.evil.example/api/v1",
    "https://evilopenrouter.ai/api/v1",
    "https://sub.openrouter.ai/api/v1",
    "https://openrouter.ai@evil.example/api/v1",
    "file://openrouter.ai/api/v1",
])
def test_openrouter_lookalikes_and_bad_scheme_still_rejected(web_probe, url):
    client, spawns = web_probe
    r = _post(client, api_key="form-key", config={"DS_BASE_URL": url})
    assert r.status_code == 400
    assert not spawns


@pytest.mark.parametrize("name", ["model.step", "model.stl", "source.py", "measurements.json"])
def test_existing_download_routes_serve_original_files(web_probe, tmp_path, name):
    client, _ = web_probe
    key = {"model.step": "step", "model.stl": "stl", "source.py": "py",
           "measurements.json": "measurements"}[name]
    artifact = tmp_path / name
    artifact.write_bytes(b"original-artifact")
    server._JOBS["download-probe"] = {"tempdir": tmp_path, "result": {key: str(artifact)}}
    r = client.get(f"/api/jobs/download-probe/files/{name}")
    assert r.status_code == 200
    assert r.content == b"original-artifact"


def test_provider_frontend_behavior():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for the frontend DOM probe")
    script = Path(__file__).with_name("web_provider_probe.cjs")
    result = subprocess.run(
        [node, str(script)], input=json.dumps({"config": server._config_schema(),
                                              "providers": server._PROVIDER_PRESETS}),
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
