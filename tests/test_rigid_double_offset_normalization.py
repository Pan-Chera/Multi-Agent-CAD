from mac_assembly.assembly_codegen import postprocess_axial_offsets
from mac_assembly.schemas_assembly import AssemblyBrief, MatingPlan, PartSpec


def _brief():
    return AssemblyBrief(
        assembly_name="t",
        user_request_raw="r",
        expected_part_count=2,
        parts=[
            PartSpec(part_id="base", part_name="base", description="base"),
            PartSpec(part_id="item", part_name="item", description="item"),
        ],
    )


def test_axis_point_rigid_does_not_double_stack_height():
    plan = MatingPlan.model_validate({
        "assembly_name": "t",
        "mates": [{
            "mate_id": "m", "mate_type": "rigid",
            "fixed_part_id": "base", "moving_part_id": "item",
            "fixed_anchor": {"kind": "axis_point", "axis": "z", "offset_mm": 82.5},
            "moving_anchor": {"kind": "axis_point", "axis": "z", "offset_mm": -47.5},
            "axial_offset_mm": 117.5,
        }],
    })
    out = postprocess_axial_offsets(plan, _brief()).mates[0]
    assert out.axial_offset_mm == 0.0


def test_planar_rigid_keeps_lateral_translation_only():
    plan = MatingPlan.model_validate({
        "assembly_name": "t",
        "mates": [{
            "mate_id": "m", "mate_type": "rigid",
            "fixed_part_id": "base", "moving_part_id": "item",
            "fixed_anchor": {"kind": "selector", "selector_query": {
                "surface": "plane", "axis": "z", "normal_sign": 1,
                "select": "closest_to", "value_mm": 95.0,
            }},
            "moving_anchor": {"kind": "face", "face": "bottom"},
            "translation_mm": [-100.0, 0.0, 95.0],
            "axial_offset_mm": 12.0,
        }],
    })
    out = postprocess_axial_offsets(plan, _brief()).mates[0]
    assert out.translation_mm == [-100.0, 0.0, 0.0]
    assert out.axial_offset_mm == 0.0
