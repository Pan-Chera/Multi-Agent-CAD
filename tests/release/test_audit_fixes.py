"""Regression tests for the project audit; all model responses are offline."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from multi_agent_cad import config, nodes
from multi_agent_cad.schemas import CADBrief, EngineReport, ErrorType, JudgeAction, JudgeDecision, QAReport
from multi_agent_cad.web import server


def _brief(request="plate"):
    return CADBrief(part_name="plate", part_category="flat-plate", user_request_raw=request)


def _report(passed=False):
    return QAReport(
        cad_brief_id="plate", engine_a=EngineReport(engine_name="cadpy_analysis"),
        engine_b=EngineReport(engine_name="check_mesh"), all_passed=passed,
        error_type=ErrorType.NONE if passed else ErrorType.DIMENSION,
        failed_count=0 if passed else 1, error_details=[] if passed else ["wrong width"],
    )


def test_planner_cache_is_invalidated_by_request_images_and_settings(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(nodes, "_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(nodes, "_CFG_SP_MULTIMODAL", "never")
    monkeypatch.setattr(nodes, "_llm_client", lambda: object())
    calls = []

    def reply(*args, **kwargs):
        calls.append(kwargs)
        return _brief().model_dump_json(), ""

    monkeypatch.setattr(nodes, "_call_llm_json_with_retry", reply)
    first = nodes.node_spec_planner({"user_request": "plate"})
    nodes.node_spec_planner({"user_request": "plate"})
    assert len(calls) == 1
    second = nodes.node_spec_planner({"user_request": "different plate"})
    assert len(calls) == 2
    assert first["cad_brief"].user_request_raw != second["cad_brief"].user_request_raw
    monkeypatch.setattr(config, "SPEC_PLANNER_MODEL", "different-model")
    nodes.node_spec_planner({"user_request": "different plate"})
    assert len(calls) == 3
    monkeypatch.setattr(nodes, "_CFG_SP_MULTIMODAL", "auto")
    images = tmp_path / nodes._CFG_USER_IMAGES_DIR
    images.mkdir()
    (images / "reference.png").write_bytes(b"version one")
    before = nodes._planning_fingerprint("spec_planner", "plate")
    (images / "reference.png").write_bytes(b"version two")
    assert nodes._planning_fingerprint("spec_planner", "plate") != before


def test_architect_does_not_use_another_briefs_cache(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(nodes, "_CACHE_DIR", tmp_path)
    (tmp_path / "architect_plan.json").write_text(json.dumps({"__input_fingerprint": "wrong"}))
    monkeypatch.setattr(nodes, "_llm_client", lambda: (_ for _ in ()).throw(RuntimeError("new plan requested")))
    with pytest.raises(RuntimeError, match="new plan requested"):
        nodes.node_geometric_architect({"cad_brief": _brief(), "user_request": "new"})


@pytest.mark.parametrize("script", ["pass\n", "from pathlib import Path\nPath('output.step').touch()\nPath('output.stl').touch()\n"])
def test_execution_rejects_stale_or_empty_outputs(tmp_path, script):
    source = tmp_path / "source.py"
    source.write_text(script)
    step, stl = tmp_path / "output.step", tmp_path / "output.stl"
    step.write_text("old step")
    stl.write_text("old stl")
    measurements = tmp_path / "temp_measurements_0.json"
    measurements.write_text('{"stale": true}')
    ok, error = nodes._execute_cad_script(source, step, stl)
    assert not ok and "did not produce" in error
    assert not measurements.exists()


def test_existing_loader_executes_gen_step_with_filtered_environment(monkeypatch, tmp_path):
    from multi_agent_cad.graph_aider import node_existing_file_loader

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUDIT_FAKE_API_KEY", "dummy-only")
    source = tmp_path / "temp_design_0.py"
    source.write_text('''import os, json
from pathlib import Path
def gen_step():
    Path("probe.json").write_text(json.dumps({"secret": "AUDIT_FAKE_API_KEY" in os.environ, "iteration": os.environ.get("ITERATION")}))
    Path("temp_output_aider_baseline_0.step").write_text("new step")
    Path("temp_output_aider_baseline_0.stl").write_text("new stl")
''')
    result = node_existing_file_loader({"current_python_code_path": str(source)})
    assert Path(result["current_step_path"]).read_text() == "new step"
    assert json.loads((tmp_path / "probe.json").read_text()) == {"secret": False, "iteration": "0"}


def test_assembly_execution_filters_secrets(monkeypatch, tmp_path):
    from mac_assembly.assembly_codegen import run_assembly_script

    monkeypatch.setenv("AUDIT_FAKE_API_KEY", "dummy-only")
    script = tmp_path / "assembly.py"
    script.write_text('''import os
from pathlib import Path
assert "AUDIT_FAKE_API_KEY" not in os.environ
Path("assembly_output.step").write_text("new assembly")
''')
    assert run_assembly_script(script, tmp_path, timeout=10)[0]


@pytest.mark.parametrize("as_dict", [False, True])
def test_usage_counts_cached_and_reasoning_subsets_once(as_dict):
    from multi_agent_cad.token_tracker import _Tracker

    usage = dict(prompt_tokens=100, completion_tokens=10, total_tokens=110,
                 prompt_tokens_details={"cached_tokens": 80},
                 completion_tokens_details={"reasoning_tokens": 9})
    if not as_dict:
        usage = SimpleNamespace(**usage)
    tracker = _Tracker()
    tracker._record(model="model", usage=usage, module="probe")
    assert tracker.summary()["total_tokens"] == 110
    assert tracker.summary()["total_cache_read"] == 80
    assert tracker.summary()["total_reasoning"] == 9


@pytest.fixture
def loop(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    script = tmp_path / "temp_design_0.py"
    script.write_text("pass\n")
    step, stl = tmp_path / "old.step", tmp_path / "old.stl"
    step.write_text("old")
    stl.write_text("old")
    monkeypatch.setattr(nodes, "_CFG_MAX_RETRIES", 1)
    monkeypatch.setattr(nodes, "_run_engine_a_cadpy", lambda **kw: EngineReport(engine_name="cadpy_analysis"))
    monkeypatch.setattr(nodes, "_run_engine_b_check_mesh", lambda **kw: EngineReport(engine_name="check_mesh"))
    monkeypatch.setattr(nodes, "node_judge_qa", lambda **kw: None)
    monkeypatch.setattr(nodes, "_prompt_iteration_choice", lambda **kw: 1)
    monkeypatch.setattr(nodes, "_optimize_print_orientation", lambda path: (path, None))
    return dict(workflow_id="original", current_python_code_path=str(script),
                current_step_path=str(step), current_stl_path=str(stl), user_request="plate")


def test_final_repair_is_verified_before_reporting_failure(monkeypatch, loop):
    reports = iter([_report(False), _report(True)])
    monkeypatch.setattr(nodes, "_merge_engine_reports", lambda **kw: next(reports))
    repairs = []
    monkeypatch.setattr(nodes, "_run_repair_on_script", lambda **kw: (repairs.append(kw) is None, ""))
    monkeypatch.setattr(nodes, "_execute_cad_script", lambda *a, **kw: (True, ""))
    result = nodes.node_autonomous_skill_loop(loop)
    assert result["error_type"] == ErrorType.NONE
    assert result["qa_report"].all_passed
    assert len(repairs) == 1
    assert result["current_python_code_path"] == loop["current_python_code_path"]


def test_judge_decision_is_refreshed_after_each_geometry_change(monkeypatch, loop):
    monkeypatch.setattr(nodes, "_CFG_MAX_RETRIES", 2)
    monkeypatch.setattr(nodes, "_merge_engine_reports", lambda **kw: _report(False))
    monkeypatch.setattr(nodes, "_run_repair_on_script", lambda **kw: (True, ""))
    monkeypatch.setattr(nodes, "_execute_cad_script", lambda *a, **kw: (True, ""))
    seen = []

    def judge(**kw):
        seen.append(kw["retry"])
        return JudgeDecision(action=JudgeAction.REPAIR, reason="repair", evidence=["wrong width"], confidence="high")

    monkeypatch.setattr(nodes, "node_judge_qa", judge)
    assert nodes.node_autonomous_skill_loop(loop)["error_type"] == ErrorType.FATAL
    assert seen == [0, 1, 2]


def test_missing_initial_artifacts_preserve_repair_failure(monkeypatch, loop):
    loop.update(workflow_id="aider", current_step_path=None, current_stl_path=None)
    monkeypatch.setattr(nodes, "_run_repair_on_script", lambda **kw: (False, "repair unavailable"))
    result = nodes.node_autonomous_skill_loop(loop)
    assert result["error_type"] == ErrorType.FATAL
    assert result["current_step_path"] is None
    assert "repair unavailable" in "\n".join(result["execution_log"])


def test_urdf_keeps_original_local_mesh_and_revision(tmp_path):
    import trimesh
    from mac_assembly.urdf_export import _find_part_stl

    part = tmp_path / "parts" / "beam"
    part.mkdir(parents=True)
    original = part / "temp_output_0.stl"
    trimesh.creation.box(extents=[10, 20, 100]).export(original)
    rotated, _ = nodes._optimize_print_orientation(original)
    assert rotated != original
    assert _find_part_stl(tmp_path, "beam") == original
    (part / "temp_output_99_rotated.stl").write_bytes(rotated.read_bytes())
    assert _find_part_stl(tmp_path, "beam") == original
    (tmp_path / "assembly_manifest.json").write_text(json.dumps([
        {"label": "beam", "source_step": str(part / "temp_output_0.step")},
    ]))
    (part / "temp_output_1.stl").write_text("newer but not assembled")
    assert _find_part_stl(tmp_path, "beam") == original
    original.unlink()
    assert _find_part_stl(tmp_path, "beam") is None


def test_revolute_sweep_uses_joint_limits():
    from mac_assembly.assembly_qa import _check_kinematics
    from mac_assembly.schemas_assembly import MateType

    mate = SimpleNamespace(mate_id="hinge", mate_type=MateType.REVOLUTE,
                           fixed_part_id="base", moving_part_id="arm",
                           limit_lower=0, limit_upper=90)
    result = _check_kinematics([], [mate], {})[0]
    assert result.samples == [0, 22.5, 45, 67.5, 90]


@pytest.mark.parametrize("workflow", ["original", "aider"])
def test_web_runner_delivers_final_state_and_correct_workflow(monkeypatch, tmp_path, capsys, workflow):
    from multi_agent_cad import graph, graph_aider, web_runner
    import trimesh

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MAC_CONFIG_FILE", "already-loaded")
    monkeypatch.setattr(nodes, "_CACHE_DIR", nodes._CACHE_DIR)
    source = tmp_path / "source.py"
    source.write_text("pass\n")
    step, stl = tmp_path / "temp_output_0.step", tmp_path / "temp_output_0.stl"
    step.write_text("accepted")
    trimesh.creation.box().export(stl)
    (tmp_path / "temp_output_99.step").write_text("newer unaccepted")
    (tmp_path / "temp_measurements_2.json").write_text('{"current":true}')
    selected = []

    class Graph:
        def stream(self, state, options):
            if workflow == "aider":
                assert state["current_python_code_path"] == str(source)
            yield {"qa": dict(error_type=ErrorType.NONE, current_step_path=str(step),
                              current_stl_path=str(stl), current_python_code_path=str(source),
                              measurement_iteration=2, qa_report=_report(True))}

    monkeypatch.setattr(graph, "build_graph", lambda: (selected.append("original") or Graph()))
    monkeypatch.setattr(graph_aider, "build_graph_aider", lambda: (selected.append("aider") or Graph()))
    monkeypatch.setattr(sys, "argv", ["runner", "--out", str(tmp_path), "--config", "unused", "--workflow", workflow, "--source", str(source)])
    assert web_runner.main() == 0
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert selected == [workflow]
    assert result["step"] == str(step)
    assert result["measurements"] == str(tmp_path / "temp_measurements_2.json")
    assert Path(result["glb"]).is_file()
    assert nodes._CACHE_DIR == tmp_path / "pipeline_cache"


def test_exit_codes_follow_business_failure_and_missing_artifacts(tmp_path):
    from multi_agent_cad.graph import pipeline_exit_code
    from mac_assembly.graph_assembly import assembly_exit_code

    step, stl = tmp_path / "part.step", tmp_path / "part.stl"
    step.write_text("step")
    stl.write_text("stl")
    state = dict(error_type=ErrorType.NONE, current_step_path=str(step), current_stl_path=str(stl))
    assert pipeline_exit_code(state) == 0
    assert pipeline_exit_code({**state, "error_type": ErrorType.FATAL}) == 1
    assert assembly_exit_code({"assembly_step_path": str(step), "qa_report": _report(True)}) == 0
    assert assembly_exit_code({"assembly_step_path": str(step), "qa_report": _report(False)}) == 1
    step.unlink()
    assert pipeline_exit_code(state) == 1
    assert assembly_exit_code({"qa_report": _report(True)}) == 1


def test_sse_replays_to_multiple_clients_and_respects_cursor(monkeypatch):
    job = {"events": [{"log": "first"}, {"done": True}], "result": {"done": True}}
    monkeypatch.setitem(server._JOBS, "replay", job)
    with TestClient(server.app, base_url="http://127.0.0.1:8000") as client:
        first = client.get("/api/jobs/replay/events").text
        second = client.get("/api/jobs/replay/events").text
        resumed = client.get("/api/jobs/replay/events", headers={"Last-Event-ID": "1"}).text
    assert first == second
    assert '"first"' in first and '"first"' not in resumed
    assert "id: 2" in resumed and '"done": true' in resumed


def test_sse_broadcasts_progress_to_simultaneous_subscribers():
    async def probe():
        job = {"events": [], "result": None}
        server._JOBS["broadcast"] = job
        req = Request({"type": "http", "headers": []})
        streams = [(await server.job_events("broadcast", req)).body_iterator for _ in range(2)]
        reads = [asyncio.create_task(anext(stream)) for stream in streams]
        await asyncio.sleep(0)
        server._publish(job, {"log": "both browsers"})
        results = await asyncio.wait_for(asyncio.gather(*reads), 2)
        assert results[0] == results[1]
        for stream in streams:
            await stream.aclose()
        assert not job["subscribers"]
        server._JOBS.pop("broadcast")
    asyncio.run(probe())


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group integration")
def test_cancel_stops_descendants_that_outlive_runner(tmp_path):
    async def probe():
        marker = tmp_path / "orphan-finished"
        child = f"import time;from pathlib import Path;time.sleep(0.7);Path({str(marker)!r}).touch()"
        parent = f"import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',{child!r}]);print('ready',flush=True);time.sleep(30)"
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-B", "-c", parent, start_new_session=True,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.stdout.readline(), 5)
            await server._terminate_job({"proc": proc, "process_group": True})
            await asyncio.sleep(1)
            assert proc.returncode is not None and not marker.exists()
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
    asyncio.run(probe())


def test_web_aider_copies_selected_source_and_provider_updates_judge(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_JOBS", {})
    source_dir = tmp_path / "old"
    source_dir.mkdir()
    source = source_dir / "source.py"
    source.write_text("# selected source\n")
    monkeypatch.setitem(server._JOBS, "source-job", {
        "tempdir": source_dir, "result": {"done": True, "py": str(source)},
    })
    spawns = []

    async def lines():
        yield b'{"done":true}\n'

    async def spawn(*args, **kwargs):
        spawns.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=lines(), wait=AsyncMock(return_value=0))

    def job_dir(prefix):
        path = tmp_path / prefix
        path.mkdir()
        return str(path)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(server.tempfile, "mkdtemp", job_dir)
    monkeypatch.setenv("MAC_WEB_HOST", "127.0.0.1")
    with TestClient(server.app, base_url="http://127.0.0.1:8000") as client:
        body = dict(prompt="add a hole", api_key="fake-key", workflow="aider", source_job_id="source-job",
                    config={"DS_BASE_URL": "https://api.openai.com/v1", "CODER_MODEL": "chosen-model"})
        response = client.post("/api/run", headers={"X-MAC-CSRF": "1"}, json=body)
        assert response.status_code == 200, response.text
        args, kwargs = spawns[0]
        copied = Path(args[args.index("--source") + 1])
        assert copied != source and copied.read_text() == source.read_text()
        saved = json.loads(Path(kwargs["env"]["MAC_CONFIG_FILE"]).read_text())
        assert saved["JUDGE_MODEL"] == "chosen-model"
        assert saved["JUDGE_KWARGS"] == saved["REPAIR_KWARGS"] == {}
        body.pop("source_job_id")
        assert client.post("/api/run", headers={"X-MAC-CSRF": "1"}, json=body).status_code == 400


def test_completed_download_does_not_fall_back_to_unaccepted_files(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_JOBS", {"complete": {
        "tempdir": tmp_path, "result": {"done": True, "step": None, "measurements": None},
    }})
    (tmp_path / "temp_output_99.step").write_text("unaccepted")
    (tmp_path / "temp_measurements_0.json").write_text("{}")
    with TestClient(server.app, base_url="http://127.0.0.1:8000") as client:
        assert client.get("/api/jobs/complete/files/model.step").status_code == 404
        assert client.get("/api/jobs/complete/files/measurements.json").status_code == 404


def test_part_harvest_uses_accepted_manifest_instead_of_newer_files(tmp_path):
    from mac_assembly.part_generator import _final_artifact_paths

    paths = [tmp_path / name for name in ("temp_output_0.step", "temp_output_0.stl", "temp_design_0.py")]
    for path in paths:
        path.write_text("accepted")
    (tmp_path / "temp_output_99.step").write_text("newer unaccepted")
    (tmp_path / "part_final_artifacts.json").write_text(json.dumps(dict(zip(
        ("current_step_path", "current_stl_path", "current_python_code_path"), map(str, paths),
    ))))
    assert _final_artifact_paths(tmp_path) == tuple(paths)
    paths[0].unlink()
    assert _final_artifact_paths(tmp_path)[0] is None


def test_single_quote_main_guard_runs_once(tmp_path):
    source = tmp_path / "source.py"
    source.write_text('''from pathlib import Path
def gen_step():
    count = Path("count")
    count.write_text(str(int(count.read_text()) + 1) if count.exists() else "1")
    Path("part.step").write_text("step")
    Path("part.stl").write_text("stl")
if __name__ == '__main__':
    gen_step()
''')
    assert nodes._execute_cad_script(source, tmp_path / "part.step", tmp_path / "part.stl")[0]
    assert (tmp_path / "count").read_text() == "1"


@pytest.mark.parametrize("module", ["multi_agent_cad.graph", "multi_agent_cad.graph_aider"])
def test_cli_returns_nonzero_for_graph_reported_fatal(monkeypatch, module, capsys):
    import runpy
    from importlib import import_module
    from langgraph.graph import StateGraph

    class FailedGraph:
        def stream(self, *args):
            yield {"planner": {"error_type": ErrorType.FATAL}}

    monkeypatch.setattr(StateGraph, "compile", lambda *args, **kwargs: FailedGraph())
    with pytest.raises(SystemExit) as exited:
        runpy.run_path(import_module(module).__file__, run_name="__main__")
    assert exited.value.code == 1
    assert "FATAL" in capsys.readouterr().out


def test_copied_source_exports_and_hashes_in_the_new_job(tmp_path):
    old_job = tmp_path / "old"
    old_job.mkdir()
    new_job = tmp_path / "new job's directory"
    new_job.mkdir()
    source = new_job / "source.py"
    source.write_text(f'''from pathlib import Path
def export_step(shape, filename):
    Path(filename).write_text("step")
def export_stl(shape, filename):
    Path(filename).write_text("stl")
def gen_step():
    export_step(None, r"{old_job.as_posix()}/original.step")
    export_stl(None, r"{old_job.as_posix()}/original.stl")
    with open(r"{old_job.as_posix()}/original.step", "rb") as f:
        assert f.read() == b"step"
''')
    step, stl = new_job / "new.step", new_job / "new.stl"
    assert nodes._execute_cad_script(source, step, stl)[0]
    assert step.read_text() == "step" and stl.read_text() == "stl"
    assert not list(old_job.iterdir())
