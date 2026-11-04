"""Assembly-QA split compatibility using real geometry and live facade hooks."""
from __future__ import annotations

import json
import os
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
import trimesh

from mac_assembly import assembly_qa as qa
from mac_assembly.schemas_assembly import (
    Anchor, AnchorKind, AssemblyBrief, AssemblyErrorType, MateSpec, MateType, PartSpec,
)


def _brief(envelope=None):
    return AssemblyBrief(
        assembly_name="qa-split", user_request_raw="two blocks", expected_part_count=2,
        overall_envelope_mm=envelope or {},
        parts=[PartSpec(part_id=p, part_name=p, description="block") for p in ("base", "arm")],
    )


def _assembly(directory, offset):
    from build123d import Box, Compound, Pos, export_step, export_stl

    directory.mkdir(parents=True, exist_ok=True)
    solids = [Box(4, 4, 4), Pos(0, 0, offset) * Box(4, 4, 4)]
    compound = Compound(children=solids)
    export_step(compound, directory / "assembly_output.step")
    export_stl(compound, directory / "assembly_output.stl")
    placed = directory / "placed_stl"
    placed.mkdir()
    manifest, results = [], {}
    for label, solid, z in zip(("base", "arm"), solids, (0, offset)):
        bounds = solid.bounding_box()
        manifest.append({
            "label": label,
            "bbox_min": [bounds.min.X, bounds.min.Y, bounds.min.Z],
            "bbox_max": [bounds.max.X, bounds.max.Y, bounds.max.Z],
            "location": {"translation": [0, 0, z], "rotation_euler_xyz_deg": [0, 0, 0]},
        })
        export_stl(solid, placed / f"{label}.stl")
        step = directory / f"{label}.step"
        export_step(Box(4, 4, 4), step)
        results[label] = SimpleNamespace(ok=True, step_path=str(step), warnings=[], degraded=False)
    (directory / "assembly_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    mate = MateSpec(
        mate_id="stack", mate_type=MateType.FACE_TO_FACE,
        fixed_part_id="base", moving_part_id="arm",
        fixed_anchor=Anchor(kind=AnchorKind.FACE, face="top"),
        moving_anchor=Anchor(kind=AnchorKind.FACE, face="bottom"),
    )
    return _brief({"x": 4, "y": 4, "z": offset + 4}), [mate], results


@pytest.mark.parametrize("offset,passed", [(4, True), (1, False)])
def test_real_assembly_qa_accepts_contact_and_rejects_penetration(tmp_path, monkeypatch, offset, passed):
    monkeypatch.setattr(qa, "_orig_bbox_cache", OrderedDict())
    brief, mates, results = _assembly(tmp_path, offset)
    rng_state = np.random.get_state()
    try:
        np.random.seed(1729)
        report = qa.run_assembly_qa(brief, mates, results, tmp_path)
    finally:
        np.random.set_state(rng_state)
    assert report.part_count_passed
    assert report.all_passed is passed, report.error_details
    assert report.interference_passed is passed
    assert report.error_type == (AssemblyErrorType.NONE if passed else AssemblyErrorType.INTERFERENCE)
    assert isinstance(qa.reconcile_dimensions(brief, [], results), qa.ReconcileResult)


def test_original_bbox_cache_invalidation_and_lru_stay_shared(tmp_path, monkeypatch):
    from build123d import Box, export_step

    cache = OrderedDict()
    monkeypatch.setattr(qa, "_orig_bbox_cache", cache)
    monkeypatch.setattr(qa, "_ORIG_CENTER_CACHE_MAX", 1)
    path = tmp_path / "part.step"
    export_step(Box(2, 4, 6), path)
    first = qa._orig_bbox(str(path))
    assert first[1][0] - first[0][0] == pytest.approx(2)
    assert len(cache) == 1
    assert qa._orig_bbox(str(path)) is first
    stamp = path.stat().st_mtime_ns
    export_step(Box(8, 4, 6), path)
    os.utime(path, ns=(stamp + 2_000_000_000, stamp + 2_000_000_000))
    second = qa._orig_bbox(str(path))
    assert second[1][0] - second[0][0] == pytest.approx(8)
    assert len(cache) == 1
    assert next(iter(cache.values())) is second


def test_configuration_rebinding_reaches_dimension_helpers(monkeypatch):
    monkeypatch.setattr(qa, "cfg", SimpleNamespace(ENVELOPE_TOLERANCE_PCT=0.01))
    placed = [("base", (0, 0, 0), (10.5, 10, 10))]
    _, errors = qa._check_envelope(_brief({"x": 10}), placed)
    assert errors
    monkeypatch.setattr(qa, "cfg", SimpleNamespace(ENVELOPE_TOLERANCE_PCT=0.1))
    assert not qa._check_envelope(_brief({"x": 10}), placed)[1]


def test_reconcile_keeps_return_type_and_replaceable_bbox_loader(monkeypatch):
    loader = Mock(return_value=None)
    monkeypatch.setattr(qa, "_orig_bbox", loader)
    result = qa.reconcile_dimensions(_brief(), [], {
        "base": SimpleNamespace(ok=True, step_path="offline.step"),
    })
    loader.assert_called_once_with("offline.step")
    assert isinstance(result, qa.ReconcileResult)
    assert result.attribution == "part_geometry"
    assert result.part_ids == ["base"]


def test_kinematic_sweep_uses_replaced_collision_hook(monkeypatch):
    monkeypatch.setattr(qa.cfg, "KINEMATIC_ENABLED", True)
    pair = Mock(return_value=(False, "", 0.0))
    monkeypatch.setattr(qa, "_pair_collides", pair)
    mesh = trimesh.creation.box(extents=(2, 2, 2))
    meshes = {"base": [mesh], "arm": [mesh.copy()]}
    placed = [(label, tuple(mesh.bounds[0]), tuple(mesh.bounds[1])) for label in meshes]
    anchor = Anchor(kind=AnchorKind.AXIS_POINT, axis="z", point_mm=[0, 0, 0])
    mate = MateSpec(
        mate_id="slider", mate_type=MateType.LINEAR, fixed_part_id="base", moving_part_id="arm",
        fixed_anchor=anchor, moving_anchor=anchor, slide_axis="z", limit_lower=-12, limit_upper=0,
    )
    check = qa._check_kinematics(placed, [mate], meshes)[0]
    assert pair.call_count > 0
    assert check.passed
    assert min(check.samples) == -12
    assert max(check.samples) == 0


def test_selector_miss_remains_distinct_from_probe_error(monkeypatch):
    from mac_assembly import selector_resolver
    from mac_assembly.schemas_assembly import FaceQuery

    anchor = Anchor(kind=AnchorKind.SELECTOR, selector_query=FaceQuery(surface="plane"))
    for status in ("not_found", "error"):
        monkeypatch.setattr(selector_resolver, "probe_selector_anchor", lambda *args, status=status: (status, None))
        assert qa._resolve_qa_point_3state(anchor, "offline.step") == (status, None)


@pytest.mark.parametrize("entry", [
    "mac_assembly.assembly_qa", "mac_assembly._qa_anchors", "mac_assembly._qa_mates",
    "mac_assembly._qa_collision", "mac_assembly._qa_kinematics", "mac_assembly._qa_dimensions",
    "mac_assembly.nodes_assembly", "mac_assembly.graph_assembly",
])
def test_fresh_process_import_order_keeps_one_facade(entry, tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("MAC_CONFIG_FILE", None)
    code = """import importlib, sys, typing
importlib.import_module(sys.argv[1])
from mac_assembly import assembly_qa as qa
from mac_assembly import _qa_anchors, _qa_mates, _qa_collision, _qa_kinematics, _qa_dimensions
for module in (_qa_anchors, _qa_mates, _qa_collision, _qa_kinematics, _qa_dimensions):
    assert module._qa is qa
    assert '_orig_bbox_cache' not in vars(module)
assert _qa_dimensions.ReconcileResult is qa.ReconcileResult
assert typing.get_type_hints(qa.reconcile_dimensions)['return'] is qa.ReconcileResult
from mac_assembly import nodes_assembly
assert nodes_assembly.run_assembly_qa is qa.run_assembly_qa
"""
    result = subprocess.run([sys.executable, "-B", "-c", code, entry], cwd=tmp_path,
                            env=env, capture_output=True, text=True, encoding="utf-8", timeout=45)
    assert result.returncode == 0, result.stderr
