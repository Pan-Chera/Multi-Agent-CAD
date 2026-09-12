import pytest

from mac_assembly.assembly_codegen import _mate_block
from mac_assembly.schemas_assembly import Anchor, AnchorKind, MateSpec, MateType


def _rigid(**updates):
    data = dict(
        mate_id="wheel_mount",
        mate_type=MateType.RIGID,
        fixed_part_id="floor",
        moving_part_id="wheel",
        fixed_anchor=Anchor(kind=AnchorKind.FACE, face="bottom"),
        moving_anchor=Anchor(kind=AnchorKind.FACE, face="top"),
    )
    data.update(updates)
    return MateSpec(**data)


def test_rigid_translation_is_emitted_in_fixed_local_frame():
    src = _mate_block(0, _rigid(translation_mm=[0, -60, -30]))
    assert "_shift_xyz(" in src
    assert "_parts['floor']" in src
    assert "(0.0, -60.0, -30.0)" in src


def test_translation_rejects_wrong_length():
    with pytest.raises(ValueError, match="exactly"):
        _rigid(translation_mm=[1, 2])


def test_translation_rejects_non_rigid_mate():
    with pytest.raises(ValueError, match="only applies to rigid"):
        _rigid(mate_type=MateType.FACE_TO_FACE, translation_mm=[0, 1, 0])


def test_explicit_axis_point_decouples_point_from_axis():
    anchor = Anchor(
        kind=AnchorKind.AXIS_POINT, axis="x", point_mm=[0, 0, 38]
    )
    mate = _rigid(fixed_anchor=anchor)
    src = _mate_block(0, mate)
    assert "_local_point(_parts['floor'], (0.0, 0.0, 38.0))" in src


def test_explicit_axis_point_requires_three_coordinates():
    with pytest.raises(ValueError, match="exactly"):
        Anchor(kind=AnchorKind.AXIS_POINT, axis="x", point_mm=[0, 1])
