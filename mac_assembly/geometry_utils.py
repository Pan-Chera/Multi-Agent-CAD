"""Shared geometry math used by both the generated assembly script
(``assembly_codegen``) and the QA engine (``assembly_qa``).

Single source of truth for the axial-offset shift math so the codegen
runtime and the QA mirror cannot diverge.
"""

from __future__ import annotations


def shift_pt(pt, direction, distance):
    """Shift a point along a direction by a signed distance.

    Used to apply ``axial_offset_mm`` to a revolute/coaxial fixed frame:
    shift the fixed anchor along the rotation axis so the moving part
    seats at the desired axial position (not the cylinder midpoint both
    anchors resolve to by default).

    Direction is normalized defensively -- callers pass a unit vector
    (face normal / axis / resolved SELECTOR axis), but we normalize again
    so a non-unit input cannot amplify the offset. A zero-length direction
    is treated as no-shift (returns ``pt`` unchanged) to avoid amplifying
    garbage into the part location.
    """
    mag = (direction[0] ** 2 + direction[1] ** 2 + direction[2] ** 2) ** 0.5
    if mag < 1e-9:
        return (pt[0], pt[1], pt[2])
    inv = 1.0 / mag
    return (pt[0] + distance * direction[0] * inv,
            pt[1] + distance * direction[1] * inv,
            pt[2] + distance * direction[2] * inv)
