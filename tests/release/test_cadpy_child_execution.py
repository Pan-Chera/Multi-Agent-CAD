"""Exercise the real cadpy child, without model calls or real credentials."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from multi_agent_cad import nodes


_CODE = '''import json, os
from pathlib import Path
import build123d as bd
Path("env_probe.json").write_text(json.dumps({
    "pid": os.getpid(),
    "secrets_present": [key for key in (
        "DASHSCOPE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MAC_CONFIG_FILE"
    ) if key in os.environ],
    "iteration": os.environ.get("ITERATION"),
    "extra": os.environ.get("MAC_REVIEW_EXTRA"),
}))
# --- Generated solids ---
body = bd.Box(10, 20, 30)
iteration = os.environ["ITERATION"]
bd.export_stl(body, f"temp_output_{iteration}.stl")
Path(f"temp_measurements_{iteration}.json").write_text(json.dumps({"volume": body.volume}))
return {"shape": body}
'''


@pytest.mark.parametrize("hybrid", [False, True])
def test_cadpy_child_preserves_geometry_and_filters_env(monkeypatch, tmp_path, hybrid):
    """Both translated and Aider-filled modules execute outside the parent.

    The probe runs at import time, before gen_step(), so filtering only the
    function call (or only a later repair attempt) cannot satisfy this test.
    """
    import build123d as bd
    import trimesh

    monkeypatch.chdir(tmp_path)
    for key in ("DASHSCOPE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(key, "review-dummy-key")
    monkeypatch.setenv("MAC_CONFIG_FILE", str(tmp_path / "must-not-be-loaded.json"))
    monkeypatch.setenv("ITERATION", "parent-value")
    monkeypatch.setenv("MAC_CHILD_ENV_ALLOW", "MAC_REVIEW_EXTRA")
    monkeypatch.setenv("MAC_REVIEW_EXTRA", "allowed-value")
    monkeypatch.setattr(nodes, "_has_unsupported_placeholders", lambda code: hybrid)
    monkeypatch.setattr(nodes, "_plan_to_code", lambda *args: _CODE)

    def fill(script_path, *args, **kwargs):
        # Mimic an Aider edit: the actual edited file must be loaded by cadpy.
        script_path.write_text(script_path.read_text() + "\n# aider-filled\n")
        return True

    def unexpected_fill(*args, **kwargs):
        pytest.fail("plain deterministic generation must not call Aider")

    monkeypatch.setattr(nodes, "_fill_unsupported_with_aider", fill if hybrid else unexpected_fill)
    result = nodes._node_python_coder_deterministic({}, object(), 7)
    assert "SUCCESS" in result["execution_log"][-1], result
    probe = json.loads((tmp_path / "env_probe.json").read_text())
    assert probe["pid"] != os.getpid()
    assert probe["secrets_present"] == []
    assert probe["iteration"] == "7"
    assert probe["extra"] == "allowed-value"
    assert os.environ["DASHSCOPE_API_KEY"] == "review-dummy-key"
    assert os.environ["ITERATION"] == "parent-value"
    assert Path(result["current_python_code_path"]).is_file()
    assert Path(result["current_step_path"]).name == "temp_output_7.step"
    assert Path(result["current_stl_path"]).name == "temp_output_7.stl"
    assert bd.import_step(result["current_step_path"]).volume == pytest.approx(6000)
    mesh = trimesh.load(result["current_stl_path"], force="mesh")
    assert mesh.is_watertight
    assert mesh.volume == pytest.approx(6000)
    assert list(mesh.extents) == pytest.approx([10, 20, 30])
    assert json.loads((tmp_path / "temp_measurements_7.json").read_text())["volume"] == pytest.approx(6000)
    assert (tmp_path / "temp_token_snapshot.json").is_file()
    if hybrid:
        assert "aider-filled" in result["current_python_code"]


def test_cadpy_runtime_error_returns_repair_feedback(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(nodes, "_has_unsupported_placeholders", lambda code: False)
    monkeypatch.setattr(nodes, "_plan_to_code", lambda *args:
                        'import os\n# --- Generated solids ---\nraise ValueError("geometry-probe-failed")\n')
    # Stale artifacts must not turn a failing child into a successful result.
    (tmp_path / "temp_output_3.step").write_text("stale")
    result = nodes._node_python_coder_deterministic({}, object(), 3)
    assert result["error_type"] == nodes.ErrorType.DIMENSION
    assert result["node_history"] == ["coder"]
    assert "geometry-probe-failed" in "\n".join(result["qa_report"].error_details)
    assert not result.get("current_step_path")


def test_cadpy_timeout_returns_repair_feedback(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(nodes, "_has_unsupported_placeholders", lambda code: False)
    monkeypatch.setattr(nodes, "_plan_to_code", lambda *args:
                        'import time\n# --- Generated solids ---\ntime.sleep(30)\n')
    monkeypatch.setattr(nodes, "_CFG_CAD_SCRIPT_TIMEOUT", 0.5)
    result = nodes._node_python_coder_deterministic({}, object(), 2)
    assert result["error_type"] == nodes.ErrorType.DIMENSION
    assert "timed out" in "\n".join(result["qa_report"].error_details)


def test_cadpy_child_stops_when_parent_pipe_closes(tmp_path):
    """Closing the lifetime pipe (as on Web runner termination) stops CAD."""
    script = tmp_path / "design.py"
    script.write_text(
        'from pathlib import Path\nimport time\n'
        'Path("child-ready").write_text("ready")\n'
        'def gen_step():\n    time.sleep(30)\n'
    )
    with subprocess.Popen(
        [sys.executable, str(Path(nodes.__file__).with_name("_cadpy_runner.py")),
         "--script", str(script), "--step", str(tmp_path / "out.step"),
         "--stl", str(tmp_path / "out.stl"), "--iteration", "0", "--watch-parent"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        cwd=tmp_path, env=nodes._build_child_env(0),
    ) as proc:
        try:
            deadline = time.monotonic() + 20
            while not (tmp_path / "child-ready").exists():
                if proc.poll() is not None:
                    pytest.fail(proc.stderr.read().decode(errors="replace"))
                assert time.monotonic() < deadline, "cadpy child did not start"
                time.sleep(0.05)
            proc.stdin.close()
            assert proc.wait(timeout=5) != 0
            assert not (tmp_path / "out.step").exists()
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
