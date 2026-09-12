"""Builder bases can use the same deterministic feature operators as v3."""

from pathlib import Path

from mac_assembly.part_generator import run_part_builder
from mac_assembly.schemas_assembly import PartSpec


def _spec() -> PartSpec:
    return PartSpec.model_validate({
        "part_id": "carrier",
        "part_name": "Carrier",
        "description": "Builder carrier with a standard Z-pin clevis fork",
        "builder": {
            "name": "telescope_carrier",
            "params": {"length": 260, "width": 40, "body_height": 20},
        },
        "features": [{
            "name": "clevis_fork",
            "params": {
                "ear_length": 30, "ear_width": 20, "bore_radius": 6,
                "tongue_thickness": 8, "bar_thickness": 16,
                "clearance_side": 0.2, "pin_axis": "z",
            },
            "attachment": {
                "attach_point_mm": [260, 0, 10],
                "direction": "+x", "surface_axis": "+x",
            },
        }],
    })


def test_schema_accepts_features_on_builder():
    assert _spec().features[0].name == "clevis_fork"


def test_builder_runner_applies_standard_features(tmp_path: Path):
    result = run_part_builder(_spec(), tmp_path)
    assert result.ok, result.error
    assert Path(result.step_path).is_file()
    audit = Path(result.py_path).read_text(encoding="utf-8")
    assert "clevis_fork" in audit
