"""Real OCP volume measurement and assembly CLI completion contracts."""
from __future__ import annotations

import math
from unittest.mock import Mock

import pytest

from multi_agent_cad import nodes
from multi_agent_cad.schemas import CADBrief, VerificationTarget


@pytest.mark.parametrize("with_hole", [False, True])
def test_step_qa_measures_exact_volume_without_instrumentation(tmp_path, monkeypatch, capsys, with_hole):
    from build123d import Box, Cylinder, export_step

    monkeypatch.chdir(tmp_path)
    shape = Box(10, 20, 30)
    expected = 6000.0
    if with_hole:
        shape = shape - Cylinder(2, 30)
        expected -= math.pi * 2**2 * 30
    step = tmp_path / "part.step"
    export_step(shape, step)
    topology = nodes._load_step_topology(str(step))
    assert topology[-1] is None
    assert topology[4] == pytest.approx(expected, abs=1e-5)
    assert "Volume calculation skipped" not in capsys.readouterr().out

    def report(nominal):
        brief = CADBrief(
            part_name="volume-audit", part_category="flat-plate", user_request_raw="box",
            verification_targets=[VerificationTarget(
                id="solid-volume", kind="volume", description="exact solid volume",
                nominal=nominal, tolerance_upper=0.1, tolerance_lower=0.1,
            )],
        )
        return nodes._run_engine_a_cadpy(
            step_path=str(step), cad_brief=brief, engine_b_mesh_resolution={},
            feature_measurements={},
        )

    passed = report(expected)
    assert not passed.errors
    assert passed.raw_json["volume_mm3"] == pytest.approx(expected, abs=1e-5)
    assert passed.results[0].passed
    assert passed.results[0].measured_value == pytest.approx(expected, abs=0.01)
    # Restored measurement must reject wrong geometry as well as accept it.
    failed = report(expected + 100)
    assert not failed.results[0].passed


@pytest.mark.parametrize("delivered", [False, True])
def test_assembly_main_requires_deliverable_after_successful_preflight(monkeypatch, tmp_path, delivered):
    from mac_assembly import graph_assembly as graph, part_generator
    from mac_assembly.schemas_assembly import AssemblyQAReport

    monkeypatch.setenv("DASHSCOPE_API_KEY", "offline-preflight-key")
    monkeypatch.setattr(part_generator, "_API_KEY_SNAPSHOT", None)
    monkeypatch.setattr(graph, "_preflight_dependencies", lambda: None)
    monkeypatch.setattr(graph, "get_initial_state", lambda: {
        "user_request": "offline assembly", "work_dir": str(tmp_path),
        "execution_log": [], "node_history": [],
    })
    event = {"qa_report": AssemblyQAReport(all_passed=True)}
    if delivered:
        step = tmp_path / "assembly.step"
        step.write_text("offline STEP fixture", encoding="utf-8")
        event["assembly_step_path"] = str(step)
    app = Mock()
    app.stream.return_value = iter([{"offline": event}])
    monkeypatch.setattr(graph, "build_assembly_graph", lambda: app)
    monkeypatch.setattr(graph, "_final_report", lambda state: None)
    monkeypatch.setattr(graph, "_print_end_to_end_tokens", lambda state: None)
    monkeypatch.setattr(graph, "_token_tracker", Mock())
    handoff = Mock(return_value={})
    monkeypatch.setattr("mac_assembly.handoff.build_handoff", handoff)
    monkeypatch.setattr("mac_assembly.handoff.print_handoff", Mock())

    assert graph.main() == (0 if delivered else 1)
    assert app.stream.call_count == 1
    assert handoff.call_count == int(delivered)
