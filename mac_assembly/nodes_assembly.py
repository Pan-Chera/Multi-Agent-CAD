"""Assembly pipeline nodes (LangGraph).

Stages and their reuse profile:

* ``node_decomposer``     -- new prompt, reuses MAC's LLM client/JSON
                             retry + image_preprocess (multimodal input).
* ``node_part_builder``   -- thin orchestration around the UNCHANGED
                             single-part MAC pipeline (part_generator).
* ``node_assembler``      -- deterministic codegen (zero tokens) with an
                             LLM repair fallback (repair-loop.md pattern).
* ``node_assembly_qa``    -- deterministic closed-loop detection
                             (assembly_qa) + rendered views for the judge.
* ``node_assembly_judge`` -- MAC QA-Judge pattern: 5-layer anti-
                             hallucination defence, evidence gate, and
                             multimodal rendered views.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mac_assembly import config_assembly as cfg
from mac_assembly.assembly_codegen import (
    _anchor_axis_letter,
    _toposort_mates,
    postprocess_axial_offsets,
    run_assembly_script,
    write_assembly_script,
)
from mac_assembly.assembly_qa import (
    reconcile_dimensions,
    render_assembly_views,
    run_assembly_qa,
)
from mac_assembly.llm_utils import (
    build_multimodal_content,
    call_llm_json,
    encode_png_data_url,
)
from mac_assembly.part_generator import (
    _load_cumulative_tokens,
    has_explicit_accepted_cache,
    is_non_retryable_error,
    load_explicit_accepted_cache,
    run_part,
    run_part_builder,
    run_part_builder_remodel,
    run_part_remodel,
    run_part_reuse,
    run_part_with_features,
)
from mac_assembly.schemas_assembly import (
    AnchorKind,
    AssemblyBrief,
    AssemblyErrorType,
    AssemblyGraphState,
    AssemblyJudgeAction,
    AssemblyJudgeDecision,
    AssemblyQAReport,
    Feature,
    MateType,
    MatingPlan,
    PartResult,
    PartSpec,
    part_spec_fingerprint,
)


def _axis_ranges(value) -> dict[str, tuple[float, float]]:
    """Normalize simple {x/y/z: [min,max]} ranges; ignore rich variants."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, tuple[float, float]] = {}
    for axis in ("x", "y", "z"):
        pair = value.get(axis)
        if (
            isinstance(pair, (list, tuple)) and len(pair) == 2
            and all(isinstance(v, (int, float)) for v in pair)
        ):
            out[axis] = (float(pair[0]), float(pair[1]))
    return out


def _requested_physical_part_count(request: str) -> int | None:
    """Read an explicit English physical-part count, not a geometry count."""
    import re

    words = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
        "fifteen": 15, "sixteen": 16, "seventeen": 17,
        "eighteen": 18, "nineteen": 19, "twenty": 20,
    }
    match = re.search(
        r"\b(?:exactly\s+)?(\d+|" + "|".join(words) + r")\s+physical\s+parts\b",
        request.lower(),
    )
    if not match:
        return None
    token = match.group(1)
    return int(token) if token.isdigit() else words[token]


def _normalize_standard_finger_bars(brief: AssemblyBrief) -> None:
    """Map plain rounded phalanx bodies to the equivalent exact builder."""
    for part in brief.parts:
        base = part.base_body
        if base is None or part.builder is not None:
            continue
        description = base.description.lower().strip()
        if description.startswith("rounded rectangular plate"):
            dims = base.key_dimensions
            ranges = _axis_ranges(dims.get("axis_ranges")) if isinstance(dims, dict) else {}
            try:
                width = float(dims["width"])
                depth = float(dims["depth"])
                thickness = float(dims["thickness"])
            except (KeyError, TypeError, ValueError):
                continue
            if (
                min(width, depth, thickness) > 0
                and set(ranges) == {"x", "y", "z"}
                and abs(ranges["x"][0] + width / 2) < 0.2
                and abs(ranges["x"][1] - width / 2) < 0.2
                and abs(ranges["y"][1] - ranges["y"][0] - depth) < 0.2
                and abs(ranges["z"][0]) < 0.2
                and abs(ranges["z"][1] - thickness) < 0.2
            ):
                part.builder = {
                    "name": "rounded_palm_plate_xy",
                    "params": {
                        "width": width, "depth": depth, "thickness": thickness,
                        "center_y": sum(ranges["y"]) / 2,
                        "corner_radius": 6.0,
                    },
                }
                part.base_body = None
            continue
        is_bar = (
            description.startswith("rounded rectangular bar")
            or description.startswith("create a rounded rectangular bar")
        )
        is_plain_block = description.startswith(
            "create a rounded rectangular block"
        )
        if not (is_bar or is_plain_block):
            continue
        dims = base.key_dimensions
        if not isinstance(dims, dict):
            continue
        try:
            width = float(dims["width"])
            length = float(dims["length"])
            thickness = float(dims["thickness"])
        except (KeyError, TypeError, ValueError):
            continue
        if min(width, length, thickness) <= 0:
            continue
        # Only normalize a bar whose prose and dimensions agree on a simple
        # proximal-origin Y extent. More elaborate phalanges remain in v3.
        import re
        ranges = _axis_ranges(dims.get("axis_ranges"))
        if "y" in ranges:
            lo, hi = ranges["y"]
        else:
            match = re.search(r"\by\s*=\s*(-?\d+(?:\.\d+)?)\s*\.\.\s*(-?\d+(?:\.\d+)?)", description)
            if match is None:
                continue
            lo, hi = map(float, match.groups())
        if abs(lo) < 0.2 and abs(hi - length) < 0.2:
            direction = "+y"
        elif abs(hi) < 0.2 and abs(lo + length) < 0.2:
            direction = "-y"
        else:
            continue
        part.builder = {
            "name": "rounded_finger_bar_y",
            "params": {
                "length": length, "width": width, "thickness": thickness,
                "direction": direction, "corner_radius": 2.0,
            },
        }
        part.base_body = None


def _normalize_transverse_clevis_centres(brief: AssemblyBrief) -> None:
    """Correct only the unambiguous legacy half-thickness Z convention.

    The operator centres a horizontal pin's bore on ``attach_z``.  When
    the bar thickness exactly fills the base body's Z extent, a proposed
    attachment at the body's bottom (rather than its centre) is the old
    pin=z convention, not a feasible centred transverse hinge.  Intentionally
    thinner or offset ears are left untouched and checked normally.
    """
    for part in brief.parts:
        import re
        # Cached briefs may carry a note from an earlier normalization
        # pass.  Remove only our own machine-added note before recomputing
        # from the current builder geometry, so stale Z=8 guidance cannot
        # compete with the corrected Z=0 datum in the mating prompt.
        part.description = re.sub(
            r" Transverse-pin feature .*?superseded\.",
            "", part.description,
        )
        dims = part.key_dimensions if isinstance(part.key_dimensions, dict) else {}
        ranges = _axis_ranges(dims.get("axis_ranges"))
        if part.builder:
            try:
                from mac_assembly.builders import build_part
                bb = build_part(
                    str(part.builder.get("name", "")),
                    dict(part.builder.get("params") or {}),
                ).bounding_box()
                real = {
                    "x": (float(bb.min.X), float(bb.max.X)),
                    "y": (float(bb.min.Y), float(bb.max.Y)),
                    "z": (float(bb.min.Z), float(bb.max.Z)),
                }
            except Exception:
                real = {}
            # A builder's local frame is authoritative.  A shifted but
            # same-sized declared range is a coordinate-label mistake,
            # not a different shape.  Correct the metadata before the
            # mating planner sees it; dimension changes remain errors.
            if ranges and real and all(
                abs((ranges[a][1] - ranges[a][0]) -
                    (real[a][1] - real[a][0])) < 0.2
                for a in ranges
            ):
                changed = any(
                    max(abs(ranges[a][i] - real[a][i]) for i in (0, 1)) > 0.2
                    for a in ranges
                )
                if changed:
                    dims["axis_ranges"] = {
                        a: [real[a][0], real[a][1]] for a in ranges
                    }
                    ranges = {a: real[a] for a in ranges}
                    part.description += (
                        " Authoritative builder local axis ranges: "
                        + ", ".join(
                            f"{a}={real[a][0]:g}..{real[a][1]:g}"
                            for a in ranges
                        )
                        + "; earlier conflicting local range prose is superseded."
                    )
        if not ranges and part.base_body is not None:
            base_dims = part.base_body.key_dimensions
            if isinstance(base_dims, dict):
                ranges = _axis_ranges(base_dims.get("axis_ranges"))
        if "z" not in ranges:
            continue
        lo, hi = ranges["z"]
        thickness, centre = hi - lo, (lo + hi) / 2
        for feature in part.features or []:
            if feature.name not in ("clevis_fork", "clevis_tongue"):
                continue
            if str(feature.params.get("pin_axis", "z")).lower() not in ("x", "y"):
                continue
            if feature.attachment.direction not in ("+x", "-x", "+y", "-y"):
                continue
            bar = feature.params.get("bar_thickness")
            if not isinstance(bar, (int, float)) or float(bar) > thickness + 0.2:
                continue
            point = feature.attachment.attach_point_mm
            # Two observed legacy interpretations: body bottom, or the
            # lower edge of a thinner clevis band. Both denote a feature
            # meant to sit on the body mid-plane, not an offset hinge.
            if min(
                abs(float(point[2]) - lo),
                abs(float(point[2]) - hi),
                abs(float(point[2]) - (centre - float(bar) / 2)),
                abs(float(point[2]) - (centre + float(bar) / 2)),
            ) > 0.2:
                continue
            feature.attachment.attach_point_mm = [
                float(point[0]), float(point[1]), float(centre)
            ]
            part.description += (
                f" Transverse-pin feature {feature.name} at local "
                f"({float(point[0]):g}, {float(point[1]):g}) uses "
                f"authoritative bore-centre Z={centre:g}; any earlier "
                "bottom-of-body attach-Z calculation is superseded."
            )


def _normalize_inward_clevis_directions(brief: AssemblyBrief) -> None:
    """Make end-face clevis features protrude outside their builder body.

    A tongue placed at the minimum Y face but pointing +Y is swallowed by
    the base union, silently losing its bore.  The end face uniquely
    determines the outward direction, so this is a safe correction; features
    away from an end face are left for ordinary geometry validation.
    """
    for part in brief.parts:
        if not part.builder:
            continue
        try:
            from mac_assembly.builders import build_part
            bb = build_part(
                str(part.builder.get("name", "")),
                dict(part.builder.get("params") or {}),
            ).bounding_box()
        except Exception:
            continue
        ends = {
            "x": (float(bb.min.X), float(bb.max.X)),
            "y": (float(bb.min.Y), float(bb.max.Y)),
            "z": (float(bb.min.Z), float(bb.max.Z)),
        }
        for feature in part.features or []:
            if feature.name not in ("clevis_fork", "clevis_tongue"):
                continue
            direction = feature.attachment.direction
            axis = direction[-1]
            coord = float(feature.attachment.attach_point_mm["xyz".index(axis)])
            lo, hi = ends[axis]
            wrong = (
                direction.startswith("+") and abs(coord - lo) < 0.3
                or direction.startswith("-") and abs(coord - hi) < 0.3
            )
            if not wrong:
                continue
            corrected = ("-" if direction[0] == "+" else "+") + axis
            feature.attachment.direction = corrected
            if feature.attachment.surface_axis == direction:
                feature.attachment.surface_axis = corrected
            part.description += (
                f" Authoritative {feature.name} at local {axis}={coord:g} "
                f"protrudes {corrected} OUTWARD from the end face; any "
                "earlier inward direction is superseded."
            )


def _complete_standard_finger_hinges(brief: AssemblyBrief) -> None:
    """Complete a missing distal fork on a standard straight finger bar.

    Only apply when an explicit hinge interface connects two +Y standard
    bars, the child has a proximal tongue, and the parent has no distal
    fork.  The existing tongue specifies the entire clevis size and pin
    axis; the parent's builder specifies its distal face.  Other part
    families and ambiguous interfaces remain untouched.
    """
    by_id = {p.part_id: p for p in brief.parts}
    for interface in brief.interfaces or []:
        if interface.interface_type.value != "hinge":
            continue
        parent = by_id.get(interface.part_a)
        child = by_id.get(interface.part_b)
        if parent is None or child is None:
            continue
        parent_src = by_id.get(parent.reuses_part_id, parent) if parent.reuses_part_id else parent
        child_src = by_id.get(child.reuses_part_id, child) if child.reuses_part_id else child
        if (
            (parent_src.builder or {}).get("name") != "rounded_finger_bar_y"
            or (child_src.builder or {}).get("name") != "rounded_finger_bar_y"
            or (parent_src.builder or {}).get("params", {}).get("direction") != "+y"
            or (child_src.builder or {}).get("params", {}).get("direction") != "+y"
        ):
            continue
        length = float(parent_src.builder["params"]["length"])
        if any(
            f.name == "clevis_fork" and f.attachment.direction == "+y"
            and abs(float(f.attachment.attach_point_mm[1]) - length) < 0.3
            for f in parent_src.features or []
        ):
            continue
        tongues = [
            f for f in child_src.features or []
            if f.name == "clevis_tongue" and f.attachment.direction == "-y"
            and abs(float(f.attachment.attach_point_mm[1])) < 0.3
        ]
        if len(tongues) != 1:
            continue
        datum = tongues[0]
        z = float(datum.attachment.attach_point_mm[2])
        fork = Feature.model_validate({
            "name": "clevis_fork",
            "params": dict(datum.params),
            "attachment": {
                "attach_point_mm": [0.0, length, z],
                "direction": "+y", "surface_axis": "+y",
            },
        })
        parent_src.features.append(fork)
        parent_src.description += (
            f" Authoritative distal (+Y) clevis fork at local Y={length:g}, "
            f"Z={z:g} receives {child.part_id}'s proximal tongue; "
            "the hinge requires both physical halves."
        )


def _validate_decomposition_brief(brief: AssemblyBrief) -> list[str]:
    """Cheap deterministic consistency checks before any part is generated."""
    errors: list[str] = []
    requested_count = _requested_physical_part_count(brief.user_request_raw)
    if requested_count is not None and len(brief.parts) != requested_count:
        errors.append(
            f"request explicitly requires {requested_count} physical parts, "
            f"but brief has {len(brief.parts)}. Reuse a real installed part "
            "as each geometry source; do not add template-only physical parts"
        )
    by_id = {part.part_id: part for part in brief.parts}
    for part in brief.parts:
        # An upright, downward-hanging bent jaw is chiral. Rotating one
        # identical solid to oppose it can turn its vertical direction over.
        if part.reuses_part_id:
            source = by_id.get(part.reuses_part_id)
            source_builder = source.builder if source is not None else None
            if isinstance(source_builder, dict) and source_builder.get("name") == "bent_jaw_xz":
                errors.append(
                    f"part {part.part_id}: cannot reuse chiral bent_jaw_xz "
                    f"geometry from {part.reuses_part_id}; use an independent "
                    "builder part with the opposite side"
                )
        dims = part.key_dimensions if isinstance(part.key_dimensions, dict) else {}
        ranges = _axis_ranges(dims.get("axis_ranges"))
        for axis, (lo, hi) in ranges.items():
            if hi <= lo:
                errors.append(f"part {part.part_id}: axis_ranges.{axis} must have max > min")
        for axis in ("x", "y", "z"):
            declared = dims.get(f"overall_{axis}")
            if axis in ranges and isinstance(declared, (int, float)):
                actual = ranges[axis][1] - ranges[axis][0]
                if abs(actual - float(declared)) > max(0.2, abs(float(declared)) * 0.01):
                    errors.append(
                        f"part {part.part_id}: overall_{axis}={declared} conflicts "
                        f"with local axis_ranges.{axis}={list(ranges[axis])} "
                        f"(extent {actual})"
                    )
        assembled = _axis_ranges(dims.get("assembled_axis_ranges"))
        if assembled and set(assembled) != {"x", "y", "z"}:
            errors.append(
                f"part {part.part_id}: assembled_axis_ranges must provide x, y, z"
            )

        # A transverse-pin clevis with the same thickness as its body is
        # centred on that body's mid-Z plane.  For pin=x/y the feature
        # operator interprets attach_z as the BORE-CENTRE Z, unlike the
        # legacy pin=z bottom-of-body convention.  Catch the common
        # "attach_z = centre - half_thickness" hallucination before any
        # part tokens are spent.  Do not constrain deliberately offset or
        # thinner clevis features.
        body_ranges = ranges
        if not body_ranges and part.base_body is not None:
            base_dims = part.base_body.key_dimensions
            if isinstance(base_dims, dict):
                body_ranges = _axis_ranges(base_dims.get("axis_ranges"))
        if "z" in body_ranges:
            z_lo, z_hi = body_ranges["z"]
            body_thickness = z_hi - z_lo
            body_mid_z = (z_lo + z_hi) / 2
            for feature in part.features or []:
                if feature.name not in ("clevis_fork", "clevis_tongue"):
                    continue
                pin = str(feature.params.get("pin_axis", "z")).lower()
                direction = feature.attachment.direction
                bar_thickness = feature.params.get("bar_thickness")
                if (
                    pin not in ("x", "y")
                    or direction not in ("+x", "-x", "+y", "-y")
                    or not isinstance(bar_thickness, (int, float))
                    or abs(float(bar_thickness) - body_thickness) > 0.2
                ):
                    continue
                attach_z = float(feature.attachment.attach_point_mm[2])
                if abs(attach_z - body_mid_z) > 0.3:
                    errors.append(
                        f"part {part.part_id}: {feature.name} pin_axis={pin} "
                        f"has attach_z={attach_z:g} but body mid-Z={body_mid_z:g}; "
                        "for horizontal transverse pins attach_z is the bore "
                        "centre, not the bottom of the bar. Correct the "
                        "feature attachment or explicitly use a thinner/"
                        "offset clevis if that is intentional"
                    )

        # A clevis added at an end face must point OUT of that face.
        # Pointing +Y from the Y-min face puts the tip bore inside the
        # existing bar; the boolean union fills the hole and the part can
        # still export as "OK". Check the feature's real body end, not just
        # whether the additive solid fused.
        if part.builder:
            try:
                from mac_assembly.builders import build_part
                _bb = build_part(
                    str(part.builder.get("name", "")),
                    dict(part.builder.get("params") or {}),
                ).bounding_box()
                end_ranges = {
                    "x": (float(_bb.min.X), float(_bb.max.X)),
                    "y": (float(_bb.min.Y), float(_bb.max.Y)),
                    "z": (float(_bb.min.Z), float(_bb.max.Z)),
                }
            except Exception:
                end_ranges = {}
            for feature in part.features or []:
                if feature.name not in ("clevis_fork", "clevis_tongue"):
                    continue
                direction = feature.attachment.direction
                axis = direction[-1]
                if axis not in end_ranges:
                    continue
                coord = float(feature.attachment.attach_point_mm["xyz".index(axis)])
                lo_end, hi_end = end_ranges[axis]
                if (
                    direction.startswith("+") and abs(coord - lo_end) < 0.3
                    or direction.startswith("-") and abs(coord - hi_end) < 0.3
                ):
                    errors.append(
                        f"part {part.part_id}: {feature.name} at local "
                        f"{axis}={coord:g} points {direction} INTO the base "
                        "body; reverse direction so the bore protrudes "
                        "outside the end face, otherwise the base union "
                        "fills the hole"
                    )

        if isinstance(part.builder, dict) and part.builder.get("name") == "bent_jaw_xz":
            side = (part.builder.get("params") or {}).get("side")
            name = f"{part.part_id} {part.part_name}".lower()
            if "left" in name and side != "left":
                errors.append(f"part {part.part_id}: left bent jaw needs builder side='left'")
            if "right" in name and side != "right":
                errors.append(f"part {part.part_id}: right bent jaw needs builder side='right'")

        # Builders have authoritative local coordinates. Validate plain
        # builder outputs now so prose cannot redefine their frame.
        if part.builder and not part.features and ranges:
            try:
                from mac_assembly.builders import BUILDERS
                name = str(part.builder.get("name", ""))
                params = dict(part.builder.get("params") or {})
                shape = BUILDERS[name](**params)
                bb = shape.bounding_box()
                measured = {
                    "x": (float(bb.min.X), float(bb.max.X)),
                    "y": (float(bb.min.Y), float(bb.max.Y)),
                    "z": (float(bb.min.Z), float(bb.max.Z)),
                }
                for axis in ranges:
                    if max(abs(ranges[axis][i] - measured[axis][i]) for i in (0, 1)) > 0.2:
                        errors.append(
                            f"part {part.part_id}: builder {name!r} actually "
                            f"produces local {axis}={list(measured[axis])}, not "
                            f"axis_ranges.{axis}={list(ranges[axis])}; put the "
                            "desired world range in assembled_axis_ranges"
                        )
            except Exception as exc:  # validation must expose bad builder input
                errors.append(f"part {part.part_id}: builder preflight failed: {exc}")
        elif part.builder and part.features:
            # Builder+feature composition is deterministic. Validate the
            # actual connection before spending a PartBuilder remodel round
            # on an impossible fork/tongue (or a floating attachment).
            try:
                from mac_assembly.builders import build_part
                from mac_assembly.feature_operators import apply_feature
                builder_name = str(part.builder.get("name", ""))
                shape = build_part(builder_name, dict(part.builder.get("params") or {}))
                for feature in part.features:
                    shape = apply_feature(shape, feature)
            except Exception as exc:
                errors.append(
                    f"part {part.part_id}: builder+feature preflight failed: "
                    f"{type(exc).__name__}: {exc}"
                )
    # If one side of an articulated interface explicitly uses a fork,
    # the opposing plain finger-bar body needs a tongue.  A revolute mate
    # between the fork and a solid rectangular end has no mechanical
    # bearing even if the kinematic graph can declare a joint.
    for interface in brief.interfaces or []:
        if str(interface.interface_type.value) != "hinge":
            continue
        a = by_id.get(interface.part_a)
        b = by_id.get(interface.part_b)
        if a is None or b is None:
            continue
        a_src = by_id.get(a.reuses_part_id, a) if a.reuses_part_id else a
        b_src = by_id.get(b.reuses_part_id, b) if b.reuses_part_id else b
        a_fork = any(f.name == "clevis_fork" for f in a_src.features or [])
        b_fork = any(f.name == "clevis_fork" for f in b_src.features or [])
        a_tongue = any(f.name == "clevis_tongue" for f in a_src.features or [])
        b_tongue = any(f.name == "clevis_tongue" for f in b_src.features or [])
        # For the standard +Y finger-bar chain, the parent must expose a
        # fork at its DISTAL (+Y) end and the child a tongue at its
        # PROXIMAL (-Y) end.  Merely finding a fork/tongue somewhere on
        # both parts is insufficient: a middle link's distal fork belongs
        # to the next hinge, not the preceding one.
        a_builder = (a_src.builder or {}).get("name")
        b_builder = (b_src.builder or {}).get("name")
        if b_builder == "rounded_finger_bar_y":
            child_root_tongue = any(
                f.name == "clevis_tongue"
                and f.attachment.direction == "-y"
                and abs(float(f.attachment.attach_point_mm[1])) < 0.3
                for f in b_src.features or []
            )
            if not child_root_tongue:
                errors.append(
                    f"hinge {interface.interface_id}: child {b.part_id} "
                    "needs a proximal (-Y) clevis_tongue with a real bore"
                )
            if a_builder == "rounded_finger_bar_y":
                a_length = float((a_src.builder or {}).get("params", {}).get("length", 0))
                parent_tip_fork = any(
                    f.name == "clevis_fork"
                    and f.attachment.direction == "+y"
                    and abs(float(f.attachment.attach_point_mm[1]) - a_length) < 0.3
                    for f in a_src.features or []
                )
                if not parent_tip_fork:
                    errors.append(
                        f"hinge {interface.interface_id}: parent {a.part_id} "
                        "needs a distal (+Y) clevis_fork with real bores; "
                        "a fork on the child does not serve this joint"
                    )
            elif not a_fork:
                errors.append(
                    f"hinge {interface.interface_id}: parent {a.part_id} "
                    "lacks a fork to receive the child's root tongue"
                )
            continue
        # Other geometries are allowed either orientation; a middle link
        # may have both feature types for distinct neighbouring joints.
        if (a_fork and b_tongue) or (b_fork and a_tongue):
            continue
        if a_fork and (b_src.builder or {}).get("name") == "rounded_finger_bar_y":
            errors.append(
                f"hinge {interface.interface_id}: {a.part_id} has a "
                f"clevis_fork but {b.part_id} lacks a mating clevis_tongue"
            )
        elif b_fork and (a_src.builder or {}).get("name") == "rounded_finger_bar_y":
            errors.append(
                f"hinge {interface.interface_id}: {b.part_id} has a "
                f"clevis_fork but {a.part_id} lacks a mating clevis_tongue"
            )
    return errors
from multi_agent_cad.config import LLM_API_TIMEOUT as _LLM_API_TIMEOUT
from multi_agent_cad.image_preprocess import (
    _SUPPORTED_EXTENSIONS,
    _encode_jpeg_data_url,
    _load_user_images,
)
from multi_agent_cad.nodes import (
    _extract_code_from_llm_response,
    _is_multimodal_unsupported_error,
    _llm_client,
)

_PROMPTS = Path(__file__).resolve().parent / "prompts"


def _prompt(name: str) -> str:
    return (_PROMPTS / name).read_text(encoding="utf-8")


def _image_fingerprint(images_dir: Path) -> str:
    """Stable hash of image files (name + mtime_ns + size) so changing
    images invalidates the Decomposer cache even when the text prompt is
    unchanged. Empty string when the directory has no images."""
    import hashlib

    if not images_dir.is_dir():
        return ""
    h = hashlib.sha256()
    for img in sorted(images_dir.iterdir()):
        if img.is_file():
            st = img.stat()
            h.update(f"{img.name}:{st.st_mtime_ns}:{st.st_size}\n".encode())
    return h.hexdigest()[:16]


def _dir_has_images(directory: Path) -> bool:
    """True when the directory holds at least one loadable image file --
    the same extension set ``_load_user_images`` scans for."""
    try:
        return any(
            f.is_file() and f.suffix.lower() in _SUPPORTED_EXTENSIONS
            for f in directory.iterdir()
        )
    except OSError:
        return False


def _input_images_dir(work_dir: Path) -> Path:
    """Resolve the user-input images directory WITHOUT depending on the
    process CWD (a resume launched from another directory previously
    fingerprinted/loaded a different -- empty -- directory, silently
    invalidating the Decomposer cache and starving the Judge's
    user_image[N] evidence).

    Resolution order: a ``user_input_images`` INSIDE the job directory
    that actually CONTAINS images (stable across resumes; an empty one --
    e.g. left behind by a failed bootstrap -- no longer shadows the CWD
    source), then the legacy repo-root CWD location (backward compatible:
    same-directory runs are unchanged).

    Persistence: when images are found at the CWD location, they are
    COPIED into the job directory on first use (idempotent, copy2 keeps
    mtime_ns so ``_image_fingerprint``'s name+mtime_ns+size hash is stable
    across the copy and the Decomposer cache does not invalidate). Every
    later caller -- including a resume launched from a different CWD --
    then resolves the SAME images from the self-contained job directory,
    so the CWD fallback is a one-time bootstrap, not a permanent
    dependency. If the copy fails (read-only job dir etc.) the CWD path
    is still returned (old behaviour, with a warning).
    """
    wd_images = Path(work_dir) / "user_input_images"
    if wd_images.is_dir() and _dir_has_images(wd_images):
        return wd_images
    cwd_images = Path.cwd() / "user_input_images"
    if not cwd_images.is_dir():
        return cwd_images
    if not _dir_has_images(cwd_images):
        return cwd_images
    try:
        import shutil

        files = sorted(f for f in cwd_images.iterdir() if f.is_file())
        wd_images.mkdir(parents=True, exist_ok=True)
        for f in files:
            dest = wd_images / f.name
            if not dest.exists():
                shutil.copy2(f, dest)
        return wd_images
    except OSError as exc:
        print(f"[assembly] WARNING: cannot persist user input images into "
              f"{wd_images} ({exc}); falling back to the CWD directory")
    return cwd_images


def _prune_part_results_for_brief(
    brief: AssemblyBrief, part_results: dict[str, PartResult] | None
) -> dict[str, PartResult]:
    """Drop stale PartResults after a (re)composition (P0-2).

    - entries for part_ids that no longer exist in the brief are removed;
    - entries whose ``spec_fingerprint`` no longer matches the current
      PartSpec are removed, so ``prev.ok`` can never resurrect stale
      geometry after a recompose changed the spec under the same part_id
      (description / builder params / base_body / features / attachment);
    - a template whose entry was dropped also drops its reuse instances
      (they would copy a stale template STEP -- geometry is shared);
    - legacy results without a fingerprint are treated as unvalidated and
      dropped (rebuild), matching the "no fingerprint -> needs validation"
      compatibility rule.

    Returns a NEW dict; the input is never mutated.
    """
    results = dict(part_results or {})
    if not results:
        return results
    fps = _effective_part_fingerprints(brief)
    results = {pid: r for pid, r in results.items() if pid in fps}
    dropped: set[str] = set()
    for pid, r in results.items():
        if getattr(r, "spec_fingerprint", "") != fps[pid]:
            dropped.add(pid)
    # Propagate through the reuse relation: an instance's geometry IS its
    # template's geometry.
    for p in brief.parts:
        if p.reuses_part_id in dropped:
            dropped.add(p.part_id)
    for pid in dropped:
        results.pop(pid, None)
    return results


def _effective_part_fingerprints(brief: AssemblyBrief) -> dict[str, str]:
    """Include template geometry identity in reuse-instance cache keys."""
    import hashlib

    base = {p.part_id: part_spec_fingerprint(p) for p in brief.parts}
    out = dict(base)
    for p in brief.parts:
        if p.reuses_part_id is not None:
            payload = f"{base[p.part_id]}:{base[p.reuses_part_id]}"
            out[p.part_id] = hashlib.sha256(payload.encode()).hexdigest()[:16]
    return out


def _log(state, msg: str) -> list[str]:
    return list(state.get("execution_log", [])) + [msg]


# ---------------------------------------------------------------------------
# Agent 1: Decomposer (the WHAT -- parts + functional interfaces)
# ---------------------------------------------------------------------------


def node_decomposer(state: AssemblyGraphState) -> dict:
    """Assembly-level Spec Planner: request -> AssemblyBrief JSON.

    Structure-only output (parts + prose interfaces + envelope). Mate
    design is delegated to the Mating Architect.
    """
    user_request = state["user_request"]
    work_dir = Path(state["work_dir"])
    recompose_feedback = state.get("repair_context", "")

    images_dir = _input_images_dir(work_dir)
    image_fp = _image_fingerprint(images_dir)
    request_fp = hashlib.sha256(user_request.encode("utf-8")).hexdigest()
    decomposer_prompt = _prompt("decomposer.md")
    prompt_fp = hashlib.sha256(decomposer_prompt.encode("utf-8")).hexdigest()

    cache = work_dir / "assembly_cache" / "assembly_brief.json"
    if cache.is_file() and not recompose_feedback:
        try:
            raw_data = json.loads(cache.read_text(encoding="utf-8"))
            cached_fp = raw_data.pop("__image_fingerprint", "") if isinstance(raw_data, dict) else ""
            cached_request_fp = raw_data.pop("__request_fingerprint", "") if isinstance(raw_data, dict) else ""
            cached_prompt_fp = raw_data.pop("__decomposer_prompt_fingerprint", "") if isinstance(raw_data, dict) else ""
            # Cache invalidation: changing images (add/remove/edit) MUST
            # invalidate the brief even when the text prompt is unchanged --
            # the Decomposer is multimodal and the previous brief may have
            # read geometry from a now-replaced image. Also invalidate when
            # the user first adds images to a previously text-only brief.
            if (cached_fp == image_fp and cached_request_fp == request_fp
                    and cached_prompt_fp == prompt_fp):
                brief = AssemblyBrief.model_validate(raw_data)
                _normalize_standard_finger_bars(brief)
                _normalize_transverse_clevis_centres(brief)
                _normalize_inward_clevis_directions(brief)
                _complete_standard_finger_hinges(brief)
                cached_errors = _validate_decomposition_brief(brief)
                if not cached_errors:
                    return {
                        "assembly_brief": brief,
                        "execution_log": _log(state, "decomposer: cache hit"),
                        "node_history": state.get("node_history", []) + ["decomposer"],
                    }
                print("[assembly] decomposer: cache miss (brief consistency failed)")
            if cached_fp != image_fp:
                reason = "images changed"
            elif cached_request_fp != request_fp:
                reason = "request changed"
            else:
                reason = "decomposer prompt changed"
            print(f"[assembly] decomposer: cache miss ({reason})")
        except Exception:  # noqa: BLE001 - stale cache -> regenerate
            cache.unlink(missing_ok=True)

    user_prompt = f"## User Request\n\n{user_request}"
    if recompose_feedback:
        user_prompt += (
            "\n\n## Feedback on the previous decomposition (fix these "
            f"problems):\n{recompose_feedback}"
        )

    images = _load_user_images(images_dir)

    try:
        brief = None
        validation_errors: list[str] = []
        for attempt in range(3):
            attempt_text = user_prompt
            if validation_errors:
                attempt_text += (
                    "\n\n## Deterministic consistency errors in your previous "
                    "brief\nFix all of these without changing the user's intent:\n"
                    + "\n".join(f"- {e}" for e in validation_errors)
                    + "\nIf a named builder does not directly implement the "
                    "described geometry, do not force its dimensions to fit; "
                    "choose the appropriate generation path instead."
                )
            if images and cfg.DECOMPOSER_MULTIMODAL != "never":
                attempt_content = build_multimodal_content(
                    attempt_text, [_encode_jpeg_data_url(b) for b in images]
                )
            else:
                attempt_content = attempt_text
            raw = call_llm_json(
                decomposer_prompt,
                attempt_content,
                model=cfg.DECOMPOSER_MODEL,
                temperature=cfg.DECOMPOSER_TEMPERATURE,
                max_tokens=cfg.DECOMPOSER_MAX_TOKENS,
                extra_kwargs=cfg.DECOMPOSER_KWARGS,
            )
            raw.get("parts", []).sort(key=lambda p: p.get("part_id", ""))
            raw.get("interfaces", []).sort(key=lambda i: i.get("interface_id", ""))
            try:
                candidate = AssemblyBrief.model_validate(raw)
            except ValueError as exc:
                # Schema-level cross-field checks (notably physical part
                # count) must reach the same bounded feedback loop as the
                # post-schema geometry checks.
                validation_errors = [
                    f"AssemblyBrief schema validation failed: {exc}"
                ]
                candidate = None
            if candidate is not None:
                _normalize_standard_finger_bars(candidate)
                _normalize_transverse_clevis_centres(candidate)
                _normalize_inward_clevis_directions(candidate)
                _complete_standard_finger_hinges(candidate)
                validation_errors = _validate_decomposition_brief(candidate)
            if not validation_errors:
                brief = candidate
                break
            print(
                f"[assembly] decomposer attempt {attempt + 1}: "
                f"{len(validation_errors)} consistency error(s)"
            )
            for error in validation_errors[:8]:
                print(f"  - {error}")
        if brief is None:
            raise ValueError(
                "decomposition failed deterministic consistency checks: "
                + " | ".join(validation_errors[:8])
            )
    except Exception as exc:  # noqa: BLE001
        import traceback
        print(f"[assembly] decomposer FAILED: {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        return {
            "assembly_brief": None,
            # BUG-007: a failed LLM call still consumes the
            # DECOMPOSER_MAX_RUNS budget. Previously the counter was
            # only incremented on the success path, so failed calls
            # looped unbounded via the FATAL -> repair -> decomposer
            # route, burning the outer ASSEMBLY_MAX_ITERATIONS budget
            # instead of the per-route DECOMPOSER_MAX_RUNS.
            "decomposer_llm_calls": state.get("decomposer_llm_calls", 0) + 1,
            "execution_log": _log(state, f"decomposer FAILED: {exc}"),
            "node_history": state.get("node_history", []) + ["decomposer"],
        }

    cache.parent.mkdir(parents=True, exist_ok=True)
    # Embed the image fingerprint in the cache file so a subsequent run
    # with changed images skips the stale cache and regenerates.
    cache_data = brief.model_dump(mode="json")
    cache_data["__image_fingerprint"] = image_fp
    cache_data["__request_fingerprint"] = request_fp
    cache_data["__decomposer_prompt_fingerprint"] = prompt_fp
    cache.write_text(json.dumps(cache_data, indent=2), encoding="utf-8")

    # A (re)composed brief invalidates stale part results NOW (not in
    # part_builder) so route_after_mating sees changed parts as not-ready
    # and routes through part_builder instead of assembling old geometry.
    pruned_results = _prune_part_results_for_brief(
        brief, state.get("part_results")
    )
    dropped = sorted(set((state.get("part_results") or {})) - set(pruned_results))

    return {
        "assembly_brief": brief,
        "part_results": pruned_results,
        "decomposer_llm_calls": state.get("decomposer_llm_calls", 0) + 1,
        "repair_context": "",
        "execution_log": _log(
            state,
            f"decomposer: {len(brief.parts)} parts, "
            f"{len(brief.interfaces)} interfaces"
            + (f"; invalidated stale results: {dropped}" if dropped else ""),
        ),
        "node_history": state.get("node_history", []) + ["decomposer"],
    }


# ---------------------------------------------------------------------------
# Agent 1b: Mating Architect (the HOW -- structured MateSpecs)
# ---------------------------------------------------------------------------


def _anchor_axis_for_validation(anchor) -> str | None:
    """Principal axis an anchor defines, for plan validation.

    Differs from codegen's ``_get_anchor_axis`` for AXIS_POINT anchors:
    there, ``None`` for |offset_mm| > 1 means "postprocess_axial_offsets
    must leave the LLM's axial_offset_mm alone", not "this anchor defines
    no axis" -- reusing it as a validation gate rejected the mating
    prompt's own documented pattern (joint-end datums at half the part
    extent; the prompt's canonical revolute example uses offsets 12.5 and
    -30.0, which killed the telescopic-crane run on 2026-09-09).
    """
    return _anchor_axis_letter(anchor)


def _validate_mating_plan(plan: MatingPlan, brief: AssemblyBrief) -> list[str]:
    """Deterministic cross-checks between plan and brief.

    The assembly-layer analogue of MAC's ``_normalize_architect_plan``:
    mechanical validation at the stage boundary catches LLM drift before
    it reaches the codegen. Checks: reference existence, single fixed
    root, acyclicity (B6), interface coverage, anchor semantics that the
    deterministic translator can actually express (face_to_face +Z
    stacking requires top/bottom faces).
    """
    errors: list[str] = []
    part_ids = {p.part_id for p in brief.parts}
    part_by_id = {p.part_id: p for p in brief.parts}

    if plan.assembly_name != brief.assembly_name:
        errors.append(
            f"assembly_name mismatch: plan {plan.assembly_name!r} vs brief "
            f"{brief.assembly_name!r}"
        )
    for m in plan.mates:
        for role in ("fixed_part_id", "moving_part_id"):
            if getattr(m, role) not in part_ids:
                errors.append(f"mate {m.mate_id!r} references unknown part")
        if m.fixed_part_id == m.moving_part_id:
            errors.append(f"mate {m.mate_id!r} mates a part to itself")
        # An axis_point is permitted, but it must locate the ACTUAL
        # protruding clevis bore, not the part origin.  An origin anchor
        # makes the parts overlap by the ear extension (8mm in the hand
        # benchmark) even though both declared axes are technically X.
        if m.mate_type == MateType.REVOLUTE and m.moving_anchor.kind == AnchorKind.AXIS_POINT:
            spec = part_by_id.get(m.moving_part_id)
            if spec is not None and spec.reuses_part_id:
                spec = part_by_id.get(spec.reuses_part_id, spec)
            if spec is not None:
                datums = []
                for feature in spec.features or []:
                    if feature.name not in ("clevis_fork", "clevis_tongue"):
                        continue
                    params = feature.params
                    if str(params.get("pin_axis", "z")) != str(m.moving_anchor.axis):
                        continue
                    try:
                        distance = float(params["ear_length"]) - float(params["ear_width"]) / 2
                        point = list(map(float, feature.attachment.attach_point_mm))
                        direction = feature.attachment.direction
                        k = "xyz".index(direction[-1])
                        point[k] += distance * (1 if direction[0] == "+" else -1)
                        if str(params.get("pin_axis", "z")) == "z":
                            point[2] += float(params["bar_thickness"]) / 2
                        datums.append(point)
                    except (KeyError, TypeError, ValueError):
                        pass
                anchor_point = m.moving_anchor.point_mm
                if datums and anchor_point is not None:
                    error = min(
                        sum((float(anchor_point[i]) - d[i]) ** 2 for i in range(3)) ** 0.5
                        for d in datums
                    )
                    if error > 0.5:
                        errors.append(
                            f"mate {m.mate_id!r}: moving axis_point "
                            f"{list(anchor_point)} is {error:.1f}mm from "
                            f"every real clevis bore on {m.moving_part_id}; "
                            f"use the feature bore datum {datums} or a "
                            "SELECTOR cylinder anchor, not part origin"
                        )
        # SPHERE anchors are only valid for ball mates. A SPHERE anchor on
        # a non-ball mate either crashes codegen (revolute/coaxial need an
        # axis direction; _emit_anchor returns None for SPHERE -> Axis(pt,
        # None) runtime error) or is semantically wrong (face_to_face /
        # rigid with sphere_center as datum). The "requires principal axis"
        # check below catches revolute/coaxial/linear/cylindrical with a
        # different message; this catches rigid + face_to_face too, and
        # gives a clearer error for the others.
        if m.mate_type != MateType.BALL:
            for role, anchor in (("fixed", m.fixed_anchor), ("moving", m.moving_anchor)):
                if anchor.kind == AnchorKind.SPHERE:
                    errors.append(
                        f"mate {m.mate_id!r}: SPHERE anchor on {role} side "
                        f"is only valid for ball mates; {m.mate_type.value} "
                        f"needs a face / axis_point / selector anchor (or "
                        f"change the mate type to ball if the joint is a "
                        f"2-DOF spherical pair)"
                    )
        if m.mate_type in (MateType.LINEAR, MateType.CYLINDRICAL):
            if not m.slide_axis:
                errors.append(
                    f"mate {m.mate_id!r}: {m.mate_type.value} requires 'slide_axis' "
                    "(the slide/rotation direction x/y/z)"
                )
        if m.mate_type == MateType.LINEAR and abs(
            math.remainder(float(m.angle_deg), 360.0)
        ) > 1e-6:
            errors.append(
                f"mate {m.mate_id!r}: linear mates cannot carry a static "
                f"angle_deg (got {m.angle_deg}) -- build123d LinearJoint "
                f"silently forces angle=0 for a RigidJoint counterpart; use "
                f"cylindrical if the rotation about the slide axis matters"
            )
        if m.mate_type == MateType.FACE_TO_FACE:
            if m.fixed_anchor.kind == AnchorKind.FACE and m.fixed_anchor.face != "top":
                errors.append(
                    f"mate {m.mate_id!r}: face_to_face offset applies along "
                    f"world +Z -- fixed anchor must be the 'top' face, got "
                    f"{m.fixed_anchor.face!r}"
                )
            if m.moving_anchor.kind == AnchorKind.FACE and m.moving_anchor.face != "bottom":
                errors.append(
                    f"mate {m.mate_id!r}: face_to_face seats the moving part "
                    f"above (+Z) -- moving anchor must be the 'bottom' face, "
                    f"got {m.moving_anchor.face!r}"
                )
        # Revolute/coaxial/linear/cylindrical mates need both anchors to
        # define a principal axis (cylinder selector, axis_point, or face
        # normal) and the axes must agree. Without
        # this check, AssemblyHelper.revolute_frame()/coaxial()/linear_frame()
        # raises at codegen time -- catch the LLM drift at the stage
        # boundary instead.
        if m.mate_type in (
            MateType.REVOLUTE, MateType.COAXIAL,
            MateType.LINEAR, MateType.CYLINDRICAL,
        ):
            fa_axis = _anchor_axis_for_validation(m.fixed_anchor)
            ma_axis = _anchor_axis_for_validation(m.moving_anchor)
            if fa_axis is None or ma_axis is None:
                errors.append(
                    f"mate {m.mate_id!r}: {m.mate_type.value} requires both "
                    f"anchors to define a principal axis (cylinder selector, "
                    f"axis_point, or face); got fixed="
                    f"{fa_axis!r}, moving={ma_axis!r}"
                )
            elif fa_axis != ma_axis:
                errors.append(
                    f"mate {m.mate_id!r}: {m.mate_type.value} anchor axes "
                    f"mismatch (fixed={fa_axis!r}, moving={ma_axis!r}) -- "
                    f"both anchors must share the same principal axis"
                )
            elif (
                m.mate_type in (MateType.LINEAR, MateType.CYLINDRICAL)
                and m.slide_axis and fa_axis != m.slide_axis
            ):
                errors.append(
                    f"mate {m.mate_id!r}: {m.mate_type.value} slide_axis "
                    f"{m.slide_axis!r} != anchor axis {fa_axis!r}"
                )
        # Ball mates: both anchors must be SPHERE kind (sphere center
        # + radius). The ball's sphere_radius should be <= the socket
        # cavity's sphere_radius for the ball to fit; flag geometry
        # violations where the ball is larger than the cavity.
        if m.mate_type == MateType.BALL:
            if m.fixed_anchor.kind != AnchorKind.SPHERE:
                errors.append(
                    f"mate {m.mate_id!r}: ball requires fixed anchor "
                    f"kind=sphere, got kind={m.fixed_anchor.kind.value}"
                )
            if m.moving_anchor.kind != AnchorKind.SPHERE:
                errors.append(
                    f"mate {m.mate_id!r}: ball requires moving anchor "
                    f"kind=sphere, got kind={m.moving_anchor.kind.value}"
                )
            if (m.fixed_anchor.kind == AnchorKind.SPHERE
                    and m.moving_anchor.kind == AnchorKind.SPHERE
                    and m.fixed_anchor.sphere_radius_mm is not None
                    and m.moving_anchor.sphere_radius_mm is not None
                    and m.moving_anchor.sphere_radius_mm
                        > m.fixed_anchor.sphere_radius_mm):
                errors.append(
                    f"mate {m.mate_id!r}: ball radius "
                    f"{m.moving_anchor.sphere_radius_mm}mm > socket cavity "
                    f"radius {m.fixed_anchor.sphere_radius_mm}mm -- ball "
                    f"cannot fit inside the socket (use a smaller ball "
                    f"radius or a larger socket cavity)"
                )
            # Sphere anchors must be the actual deterministic feature
            # centres, not a nearby side-face coordinate invented from the
            # prose.  ball_cavity uses attach_point directly; ball_stem uses
            # attach + (-direction)*(stem_length+sphere_radius).
            for role, pid, anchor, feature_name in (
                ("fixed", m.fixed_part_id, m.fixed_anchor, "ball_cavity"),
                ("moving", m.moving_part_id, m.moving_anchor, "ball_stem"),
            ):
                spec = part_by_id.get(pid)
                if spec is not None and spec.reuses_part_id:
                    spec = part_by_id.get(spec.reuses_part_id, spec)
                features = [f for f in (spec.features if spec else []) if f.name == feature_name]
                if len(features) != 1 or anchor.sphere_center_mm is None:
                    continue
                feature = features[0]
                expected = list(map(float, feature.attachment.attach_point_mm))
                if feature_name == "ball_stem":
                    direction = feature.attachment.direction
                    k = "xyz".index(direction[-1])
                    distance = float(feature.params["stem_length"]) + float(feature.params["sphere_radius"])
                    expected[k] += distance * (-1 if direction[0] == "+" else 1)
                delta = sum(
                    (float(anchor.sphere_center_mm[i]) - expected[i]) ** 2
                    for i in range(3)
                ) ** 0.5
                if delta > 0.3:
                    errors.append(
                        f"mate {m.mate_id!r}: {role} sphere center "
                        f"{list(anchor.sphere_center_mm)} is {delta:.1f}mm "
                        f"from actual {feature_name} center {expected} on {pid}"
                    )
            moving_spec = part_by_id.get(m.moving_part_id)
            desc = (moving_spec.description if moving_spec else "").lower()
            if (
                "local +y" in desc and "world +x" in desc
                and m.ball_axis_2 == "z"
                and abs(float(m.ball_pitch_deg)) < 1e-6
            ):
                yaw = math.remainder(float(m.ball_yaw_deg), 360.0)
                if abs(yaw + 90.0) > 1.0:
                    errors.append(
                        f"mate {m.mate_id!r}: brief requires local +Y to "
                        f"map to world +X, which needs -90deg about +Z; "
                        f"ball_yaw_deg={yaw:g} maps it toward world -X"
                    )

    # v3 SELECTOR disambiguation: when a mate references a v3 part
    # (base_body set) with 2+ cylinder-producing features (clevis_fork /
    # clevis_tongue / through_bore / knuckle_ear), the SELECTOR anchor on
    # that part MUST specify the 2 target fields that lie in the plane
    # PERPENDICULAR to the cylinder's pin axis -- otherwise the resolver
    # picks the first matching cylinder for every mate, stacking all
    # moving parts at one feature root. See plan §4d.
    #
    # The 2 required target fields depend on the cylinder axis (pin axis)
    # reported in the SELECTOR query:
    #   axis="x" (pin=X) → bores along X, disambiguate in YZ plane →
    #                      need target_y_mm + target_z_mm
    #   axis="y" (pin=Y) → bores along Y, disambiguate in XZ plane →
    #                      need target_x_mm + target_z_mm
    #   axis="z" (pin=Z) → bores along Z, disambiguate in XY plane →
    #                      need target_x_mm + target_y_mm (legacy default)
    # The previous version hardcoded target_x + target_y, which is wrong
    # for pin=X (where target_x is along the bore axis — irrelevant for
    # disambiguation) and for pin=Y (where target_y is along the bore).
    _PIN_AXIS_REQUIRED_TARGETS: dict[str, tuple[str, str]] = {
        "x": ("target_y_mm", "target_z_mm"),
        "y": ("target_x_mm", "target_z_mm"),
        "z": ("target_x_mm", "target_y_mm"),
    }
    v3_parts = {p.part_id: p for p in brief.parts if p.base_body is not None}
    _CYL_FEATURES = ("clevis_fork", "clevis_tongue", "through_bore", "knuckle_ear")
    for m in plan.mates:
        for role, anchor, other_pid in (
            ("fixed", m.fixed_anchor, m.fixed_part_id),
            ("moving", m.moving_anchor, m.moving_part_id),
        ):
            if other_pid not in v3_parts:
                continue
            if anchor.kind != AnchorKind.SELECTOR:
                continue
            v3_spec = v3_parts[other_pid]
            cyl_features = [f for f in v3_spec.features if f.name in _CYL_FEATURES]
            if len(cyl_features) < 2:
                continue  # single cyl feature -- no disambiguation needed
            q = anchor.selector_query
            if q is None:
                continue  # schema validator already caught this
            cyl_axis = str(getattr(q, "axis", "z") or "z").lower()
            required_targets = _PIN_AXIS_REQUIRED_TARGETS.get(
                cyl_axis, ("target_x_mm", "target_y_mm")
            )
            missing = [
                f for f in required_targets if getattr(q, f) is None
            ]
            if missing:
                errors.append(
                    f"mate {m.mate_id!r}: SELECTOR anchor on v3 part "
                    f"{other_pid!r} ({role}) lacks {', '.join(missing)}, "
                    f"but the part has {len(cyl_features)} cylinder-producing "
                    f"features with axis={cyl_axis!r}. For pin axis "
                    f"{cyl_axis.upper()}, disambiguation is in the "
                    f"{'YZ' if cyl_axis=='x' else 'XZ' if cyl_axis=='y' else 'XY'} "
                    f"plane -- set the 2 perpendicular target fields to "
                    f"the corresponding feature's attach_point_mm coords. "
                    f"Without these, the resolver picks the first matching "
                    f"cylinder for every mate, stacking all moving parts "
                    f"at one feature root."
                )

    # Template-only parts (referenced by reuses_part_id but never directly
    # mated) participate in geometry generation but NOT in the assembly
    # tree. Exclude them from the single-root check so the validator
    # doesn't flag them as "unmoved roots".
    mated_ids = {
        m.fixed_part_id for m in plan.mates
    } | {m.moving_part_id for m in plan.mates}
    template_only_ids = {
        p.part_id for p in brief.parts
        if any(q.reuses_part_id == p.part_id for q in brief.parts)
        and p.part_id not in mated_ids
    }
    graph_part_ids = part_ids - template_only_ids

    moved = [m.moving_part_id for m in plan.mates]
    unmoved = graph_part_ids - set(moved)
    n_graph = len(graph_part_ids)
    # Edge-count necessary condition (BUG-013): a tree on N nodes needs N-1
    # edges. The Pydantic schema can't enforce this because it doesn't see
    # the brief (and ``len(brief.parts)`` over-counts template-only parts).
    # Catches the silent-under-expansion case: 3 non-template parts + 0
    # mates used to pass validation and the assembler emitted only the
    # root part while QA reported PASS.
    if n_graph > 1 and len(plan.mates) < n_graph - 1:
        errors.append(
            f"mate graph is under-connected: {len(plan.mates)} mate(s) for "
            f"{n_graph} non-template part(s) -- a spanning tree needs at "
            f"least {n_graph - 1} mate(s)"
        )
    # Connectivity / single-root / acyclicity checks. The ``plan.mates``
    # guard used to skip these when mates was empty, which let multi-part
    # briefs with zero mates through. Use ``n_graph > 1`` instead: a
    # single-part (or empty) graph is trivially valid; a multi-part graph
    # needs exactly one fixed root (``unmoved`` size 1), and a toposort
    # that consumes every mate (else there's a cycle in a sub-component).
    # The unmoved set IS the connectivity check -- a disconnected graph
    # has >= 2 unmoved roots (forest) or 0 (cycle through all parts);
    # toposort catches the mixed case (one tree + one cyclic component).
    if n_graph > 1:
        if not unmoved:
            errors.append("every part is moved by some mate -- need a fixed root")
        elif len(unmoved) > 1:
            errors.append(
                f"multiple unmoved roots {sorted(unmoved)} -- the mate graph "
                "must be a single tree rooted at one fixed part"
            )
        else:
            # B6: acyclicity -- a mate that never becomes ready is on a cycle.
            ordered = _toposort_mates(plan.mates, graph_part_ids)
            if len(ordered) < len(plan.mates):
                ordered_ids = {m.mate_id for m in ordered}
                stuck = [m.mate_id for m in plan.mates if m.mate_id not in ordered_ids]
                errors.append(f"mate graph contains a cycle involving: {stuck}")

    # Coverage is checked on the UNORDERED part pair: the brief's
    # part_a/part_b direction is advisory (the Decomposer picks it before
    # the root is known), and the Architect may legitimately flip
    # fixed/moving on a mate to satisfy the single-root-tree constraint
    # above -- flipping does not change the assembled geometry (a mate
    # fixes the RELATIVE pose; direction only picks the placement order).
    # Directionality is owned by the tree checks above; this check only
    # verifies each interface is realized by SOME mate between the same
    # two parts (matching the Architect prompt's "between the same two
    # parts" contract).
    covered = {frozenset((m.fixed_part_id, m.moving_part_id)) for m in plan.mates}
    for itf in brief.interfaces:
        if frozenset((itf.part_a, itf.part_b)) not in covered:
            errors.append(
                f"interface {itf.interface_id!r} ({itf.part_a} <-> {itf.part_b}) "
                "has no covering mate between its two parts (in either "
                "direction)"
            )
    return errors


def node_mating_architect(state: AssemblyGraphState) -> dict:
    """Brief (+ optional QA feedback) -> MatingPlan JSON."""
    import hashlib

    brief: AssemblyBrief | None = state.get("assembly_brief")
    if brief is None:
        return {
            "mating_plan": None,
            "execution_log": _log(state, "mating_architect: no brief"),
            "node_history": state.get("node_history", []) + ["mating_architect"],
        }

    work_dir = Path(state["work_dir"])
    remate_feedback = state.get("repair_context", "")

    # Brief fingerprint: invalidate the mating plan cache when the brief
    # changes (recompose, image-driven re-decomposition, or brief edits).
    # The previous cache only validated the plan against the new brief --
    # a plan that happened to structurally validate would survive even
    # when its anchor dims no longer matched the regenerated parts.
    brief_fp = hashlib.sha256(
        brief.model_dump_json().encode("utf-8")
    ).hexdigest()[:16]
    mating_prompt = _prompt("mating_architect.md")
    prompt_fp = hashlib.sha256(mating_prompt.encode("utf-8")).hexdigest()[:16]

    cache = work_dir / "assembly_cache" / "mating_plan.json"
    if cache.is_file() and not remate_feedback:
        try:
            raw_data = json.loads(cache.read_text(encoding="utf-8"))
            cached_fp = raw_data.pop("__brief_fingerprint", "") if isinstance(raw_data, dict) else ""
            cached_prompt_fp = raw_data.pop("__mating_prompt_fingerprint", "") if isinstance(raw_data, dict) else ""
            if cached_fp == brief_fp and cached_prompt_fp == prompt_fp:
                plan = MatingPlan.model_validate(raw_data)
                errs = _validate_mating_plan(plan, brief)
                if not errs:
                    # Apply deterministic normalizers to cached plans too.
                    # Previously only newly generated plans were processed,
                    # so a known double-offset bug survived every resume.
                    plan = postprocess_axial_offsets(plan, brief)
                    return {
                        "mating_plan": plan,
                        "execution_log": _log(state, "mating_architect: cache hit"),
                        "node_history": state.get("node_history", []) + ["mating_architect"],
                    }
            else:
                print(f"[assembly] mating_architect: cache miss (brief changed)")
        except Exception:  # noqa: BLE001
            cache.unlink(missing_ok=True)

    user_prompt = (
        "## AssemblyBrief\n\n```json\n"
        + brief.model_dump_json(indent=2)
        + "\n```"
    )
    if remate_feedback:
        user_prompt += (
            "\n\n## QA feedback on the previous mating plan (fix these; "
            "measured deltas are real geometry, trust them):\n"
            f"{remate_feedback}"
        )

    plan: MatingPlan | None = None
    validation_errors: list[str] = []
    for attempt in range(2):  # one structured-retry on validation failure
        try:
            raw = call_llm_json(
                mating_prompt,
                user_prompt + ("\n\n## Previous plan errors\n" + "\n".join(validation_errors) if validation_errors else ""),
                model=cfg.MATING_MODEL,
                temperature=cfg.MATING_TEMPERATURE,
                max_tokens=cfg.MATING_MAX_TOKENS,
                extra_kwargs=cfg.MATING_KWARGS,
            )
            raw.get("mates", []).sort(key=lambda m: m.get("mate_id", ""))
            candidate = MatingPlan.model_validate(raw)
            validation_errors = _validate_mating_plan(candidate, brief)
            if not validation_errors:
                plan = candidate
                break
            print(
                f"[assembly] mating_architect attempt {attempt+1}: "
                f"{len(validation_errors)} validation error(s):"
            )
            for e in validation_errors[:8]:
                print(f"  - {e}")
        except Exception as exc:  # noqa: BLE001
            validation_errors = [f"parse/validation error: {exc}"]
            print(f"[assembly] mating_architect attempt {attempt+1}: parse/validation error: {exc}")

    if plan is None:
        print(f"[assembly] mating_architect FAILED — validation errors:")
        for e in validation_errors[:8]:
            print(f"  - {e}")
        return {
            "mating_plan": None,
            # Feed the errors back so route_after_mating can loop into
            # another architect run (remate machinery) while the
            # MATING_MAX_RUNS budget lasts -- a first-pass failure used to
            # be terminal, killing runs in minutes (telescopic crane,
            # 2026-09-09).
            "mating_architect_runs": state.get("mating_architect_runs", 0) + 1,
            "repair_context": (
                "The previous mating plan was rejected by deterministic "
                "validation. Fix ALL of these:\n"
                + "\n".join(f"- {e}" for e in validation_errors[:8])
            ),
            "execution_log": _log(
                state,
                "mating_architect FAILED: " + " | ".join(validation_errors[:4]),
            ),
            "node_history": state.get("node_history", []) + ["mating_architect"],
        }

    # Deterministic axial_offset_mm override for the pivot-post + link-bar
    # and link-bar + link-bar revolute patterns. The LLM consistently
    # writes wrong axial_offset_mm values (correctly reasons -3 in notes
    # then writes -11 in JSON); this computes the correct value from the
    # brief's structured key_dimensions values along the mate's principal
    # axis (x/y/z -- generalized from Z-only), chain-tracking the
    # per-axis world_offset through the mate graph so multi-joint chains
    # (finger segments stacked on a palm post, or horizontal clevis
    # chains along X/Y) are placed correctly.
    plan = postprocess_axial_offsets(plan, brief)

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache_data = plan.model_dump(mode="json")
    cache_data["__brief_fingerprint"] = brief_fp
    cache_data["__mating_prompt_fingerprint"] = prompt_fp
    cache.write_text(json.dumps(cache_data, indent=2), encoding="utf-8")

    return {
        "mating_plan": plan,
        "mating_architect_runs": state.get("mating_architect_runs", 0) + 1,
        "repair_context": "",
        "execution_log": _log(
            state,
            f"mating_architect: {len(plan.mates)} mates "
            f"(run {state.get('mating_architect_runs', 0) + 1})",
        ),
        "node_history": state.get("node_history", []) + ["mating_architect"],
    }


# ---------------------------------------------------------------------------
# Agent 2/3: per-part single-part MAC runs
# ---------------------------------------------------------------------------


def node_part_builder(state: AssemblyGraphState) -> dict:
    """Run the unchanged single-part MAC pipeline for every missing/failed part."""
    brief: AssemblyBrief = state["assembly_brief"]
    work_dir = Path(state["work_dir"])
    parts_root = work_dir / "parts"

    # Defensive prune (idempotent): the decomposer already pruned after a
    # recompose, but part_builder can also be entered from the
    # REMODEL_PARTS / PART_MISSING routes with results from an older brief
    # shape in state.
    existing: dict[str, PartResult] = _prune_part_results_for_brief(
        brief, state.get("part_results")
    )
    remodel_ids = set(state.get("remodel_part_ids") or [])
    feedback = state.get("repair_context", "")

    # Reuse maps (computed once per node call).
    # template_of[instance_id] = template_id (each instance has exactly
    #   one template -- the AssemblyBrief validator rejected chains).
    # instances_of[template_id] = [instance_id, ...] (reverse map).
    template_of = {
        p.part_id: p.reuses_part_id
        for p in brief.parts if p.reuses_part_id is not None
    }
    instances_of: dict[str, list[str]] = {}
    for inst_id, tmpl_id in template_of.items():
        instances_of.setdefault(tmpl_id, []).append(inst_id)

    # Symmetrically expand remodel_ids: mentioning a template remodels
    # its instances too (so they pick up the new geometry); mentioning
    # an instance remodels its template (geometry is shared). Without
    # this, a remodeled template's old STEP copy in an instance's
    # directory would silently drift.
    expanded_remodel = set(remodel_ids)
    for rid in list(remodel_ids):
        if rid in template_of:
            expanded_remodel.add(template_of[rid])
        if rid in instances_of:
            expanded_remodel.update(instances_of[rid])
    remodel_ids = expanded_remodel

    # Stable two-bucket sort: templates first, then instances. Within
    # each bucket the Decomposer's order is preserved. This guarantees
    # the template's STEP exists before any instance tries to copy it.
    # (Index map instead of list.index: list.index is a linear scan with
    # pydantic __eq__ per element -- O(n^2) with surprising equality
    # semantics; the map is exact and O(n).)
    brief_order = {p.part_id: i for i, p in enumerate(brief.parts)}
    ordered_parts = sorted(
        brief.parts,
        key=lambda p: (
            1 if p.reuses_part_id is not None else 0,
            brief_order[p.part_id],
        ),
    )

    fps = _effective_part_fingerprints(brief)

    def _stamp(spec: PartSpec, result: PartResult) -> PartResult:
        """Attach the spec fingerprint so a later recompose can tell this
        result apart from one generated under a different spec."""
        fp = fps.get(spec.part_id, "")
        if getattr(result, "spec_fingerprint", "") == fp:
            return result
        return result.model_copy(update={"spec_fingerprint": fp})

    def _keep_best(spec: PartSpec, new_result: PartResult,
                   prev: PartResult | None) -> PartResult:
        """Regression guard: a FAILED rebuild never evicts a usable (ok,
        possibly degraded) result built from the SAME spec. Without this, a
        remodel round replaces a working degraded part with ok=False and
        the whole assembly dies at part_missing even though usable geometry
        existed a round earlier. Only same-spec results are protected --
        _prune_part_results_for_brief already drops fingerprint-mismatched
        entries, this is defense in depth."""
        if (
            prev is not None
            and prev.ok
            and not new_result.ok
            and getattr(prev, "spec_fingerprint", "") == fps[spec.part_id]
        ):
            print(f"[assembly] part {spec.part_id}: rebuild FAILED "
                  f"({new_result.error}); regression guard keeps the "
                  f"previous usable result "
                  f"(degraded={bool(getattr(prev, 'degraded', False))})")
            return prev
        return new_result

    results = dict(existing)
    for spec in ordered_parts:
        prev = existing.get(spec.part_id)
        # Reuse guard: a previous result may only be reused when it is a
        # CLEAN success (ok, not degraded) produced from the CURRENT spec
        # (fingerprint match). A degraded result (generation failed but a
        # STEP exists) must be rebuilt whenever part_builder is entered,
        # never silently skipped by prev.ok.
        if (
            prev is not None
            and prev.ok
            and not getattr(prev, "degraded", False)
            and spec.part_id not in remodel_ids
            and getattr(prev, "spec_fingerprint", "") == fps[spec.part_id]
        ):
            continue
        part_feedback = ""
        if spec.part_id in remodel_ids and feedback:
            part_feedback = feedback

        # Explicit user-approved disk caches apply uniformly before choosing
        # v2, v3, builder, reuse, or remodel generation.  Acceptance is an
        # operator decision and therefore supersedes stale QA/remodel feedback
        # carried in workflow state; invalidation is explicit (remove/update
        # accepted_part_cache.json or change the request fingerprint).
        accepted = load_explicit_accepted_cache(spec, parts_root)
        if accepted is not None:
            results[spec.part_id] = _stamp(spec, accepted)
            continue

        # Reuse path: skip MAC pipeline, copy template's STEP/STL/py.
        # `results` starts as a copy of the pruned pre-node state and every
        # processed template writes its fresh result into it, so a plain
        # lookup already prefers the THIS-iteration result (a remodeled
        # template's new STEP) and falls back to the pre-node result only
        # when the template wasn't touched this round.
        if spec.reuses_part_id is not None:
            tmpl_res = results.get(spec.reuses_part_id)
            result = run_part_reuse(spec, parts_root, template_result=tmpl_res)
            results[spec.part_id] = _stamp(spec, _keep_best(spec, result, prev))
            status = "OK (reuse)" if result.ok else f"FAILED ({result.error})"
            print(f"[assembly] part {spec.part_id}: {status}")
            continue

        # v3 path: LLM base body + feature operators. Does NOT fall through
        # to full regen on failure -- doing so would re-introduce the LLM-
        # writes-kinematic-features failure mode v3 was designed to avoid.
        # run_part_with_features does its own Aider retry internally; if it
        # still fails, the result is stored with ok=False and the
        # FeedbackRouter / Judge routes (REMODEL_PARTS / RECOMPOSE / HALT).
        # The feature-only-remodel fast path (skip MAC Coder when
        # base_body.description is byte-identical to cached version) is
        # detected inside run_part_with_features via temp_v3_spec.json.
        if spec.base_body is not None:
            result = run_part_with_features(spec, parts_root, part_feedback)
            results[spec.part_id] = _stamp(spec, _keep_best(spec, result, prev))
            if result.ok:
                if getattr(result, "degraded", False):
                    # continue-on-failure: base body generation failed but a
                    # STEP exists; the assembly proceeds with that geometry
                    # for inspection. NOT a clean success -- the result is
                    # flagged degraded and will be rebuilt on any later
                    # part_builder pass instead of being reused from cache.
                    status = "DEGRADED (v3 base+features)"
                    for w in getattr(result, "warnings", None) or []:
                        status += f" [WARN: {w}]"
                else:
                    status = "OK (v3 base+features)"
            else:
                status = f"FAILED ({result.error})"
            print(f"[assembly] part {spec.part_id}: {status}")
            continue  # success or failure, the result is stored -- no fall-through

        # Builder path: if the Decomposer specified a builder for this
        # part, call it directly (zero tokens, deterministic geometry).
        # Bypasses the LLM-driven single-part pipeline entirely. Builders
        # are for structures the LLM struggles with (horizontal-axis
        # cylinders, knuckle ears with horizontal bores).
        # Remodel: ask the LLM to adjust builder params from the QA
        # feedback, then re-call the builder (one small LLM call, no
        # Aider on the non-executable audit file). Falls through to full
        # regeneration if param adjustment fails.
        if spec.builder:
            if not part_feedback:
                result = run_part_builder(spec, parts_root)
                results[spec.part_id] = _stamp(spec, _keep_best(spec, result, prev))
                status = "OK (builder)" if result.ok else f"FAILED ({result.error})"
                print(f"[assembly] part {spec.part_id}: {status}")
                continue
            patched = run_part_builder_remodel(spec, parts_root, part_feedback)
            if patched.ok:
                results[spec.part_id] = _stamp(spec, patched)
                print(f"[assembly] part {spec.part_id}: OK (builder remodel)")
                continue
            # Builder remodel failed: store the failed result with its
            # specific error (LLM call failed / param validation failed /
            # build crashed) so the Judge / FeedbackRouter can route
            # accordingly (REMODEL_PARTS / RECOMPOSE / HALT). Does NOT
            # fall through to run_part (LLM full regen) -- the regen
            # geometry's topology may not match the builder-derived
            # SELECTOR anchors the Mating Architect specified, and it
            # re-introduces the LLM-writes-kinematic-features failure
            # mode builders avoid. Symmetric with the v3 base_body path
            # (no fall-through, see L597-602).
            results[spec.part_id] = _stamp(spec, _keep_best(spec, patched, prev))
            print(f"[assembly] part {spec.part_id}: FAILED (builder remodel: {patched.error})")
            continue

        # Remodel path: patch the existing design with Aider (preserves
        # verified features, fewer tokens) before falling back to a full
        # from-scratch regeneration. Skipped for builder parts (handled
        # above by the `if spec.builder:` block, which always continues)
        # because their audit py file is non-executable.
        if spec.part_id in remodel_ids and part_feedback:
            patched = run_part_remodel(spec, parts_root, part_feedback)
            if patched is not None and patched.ok:
                results[spec.part_id] = _stamp(spec, patched)
                print(f"[assembly] part {spec.part_id}: OK (aider remodel)")
                continue
            if (
                patched is not None
                and not patched.ok
                and is_non_retryable_error(patched.error)
            ):
                # P0-2: auth/endpoint/dependency failure -- full
                # regeneration cannot succeed either; store the classified
                # error and skip the fallback (the router routes to END).
                results[spec.part_id] = _stamp(
                    spec, _keep_best(spec, patched, prev))
                print(f"[assembly] part {spec.part_id}: FAILED "
                      f"(aider remodel, non-retryable: {patched.error})")
                continue
            print(f"[assembly] part {spec.part_id}: aider remodel unavailable/"
                  f"failed -> full regeneration")

        result: PartResult | None = None
        for attempt in range(cfg.PART_MAX_ATTEMPTS):
            # Retry feedback policy: attempt 0 carries the full QA feedback
            # (the corrective phrasing may itself have contributed to the
            # failure), later attempts keep a COMPACTED version -- the
            # leading hard-constraint lines -- instead of dropping all
            # feedback, so the regeneration still knows what was wrong.
            if attempt == 0 or not part_feedback:
                attempt_feedback = part_feedback
            else:
                attempt_feedback = (
                    "Previous correction attempt failed. Regenerate from "
                    "scratch, strictly honouring these constraints:\n"
                    + "\n".join(part_feedback.splitlines()[:6])
                )
            result = run_part(
                spec,
                parts_root,
                feedback=attempt_feedback,
                force_refresh=attempt > 0 or bool(part_feedback),
            )
            result.attempts = attempt + 1
            if result.ok:
                break
            if is_non_retryable_error(result.error):
                # P0-2: configuration/infrastructure failure (missing API
                # key, auth rejection, bad endpoint/model, missing
                # dependency) -- retrying the identical environment is a
                # deterministic no-op; stop without consuming the attempt
                # budget (2026-09-10: a blanked DS_API_KEY burned the
                # whole PART_BUILDER_MAX_RUNS on "key not set" failures).
                print(f"[assembly] part {spec.part_id}: non-retryable "
                      f"failure (configuration/infrastructure) -- not "
                      f"retrying: {result.error}")
                break
        results[spec.part_id] = _stamp(spec, _keep_best(spec, result, prev))
        status = "OK" if result.ok else f"FAILED ({result.error})"
        print(f"[assembly] part {spec.part_id}: {status}")

    # P1-3: refresh the cross-attempt token ledger. Read the LEDGER FILE for
    # each part -- a PartResult surviving _keep_best may be an OLDER success
    # whose token_usage predates newer attempts, and writing that back would
    # silently revert the cumulative state. The file on disk is authoritative.
    cumulative = dict(state.get("cumulative_part_token_usage") or {})
    for pid in results:
        ledger = _load_cumulative_tokens(parts_root / pid)
        u = ledger or (getattr(results[pid], "token_usage", {}) or {})
        if u:
            cumulative[pid] = u

    return {
        "part_results": results,
        "cumulative_part_token_usage": cumulative,
        "remodel_part_ids": [],
        "execution_log": _log(
            state,
            "part_builder: "
            + ", ".join(f"{k}={'ok' if v.ok else 'fail'}" for k, v in results.items()),
        ),
        "node_history": state.get("node_history", []) + ["part_builder"],
    }


# ---------------------------------------------------------------------------
# Agent 4a: Assembler (deterministic codegen + LLM repair fallback)
# ---------------------------------------------------------------------------


def _llm_repair_assembly(
    script_src: str,
    brief: AssemblyBrief,
    qa: AssemblyQAReport | None,
    exec_error: str = "",
    repair_context: str = "",
) -> str | None:
    """LLM fallback that edits the generated assembly script.

    B4 fix: the script execution traceback (when present) is the primary
    evidence -- without it the repair agent was guessing blind.
    repair_context carries the feedback_router's accumulated errors + judge
    rationale for REPAIR_ASSEMBLY routes where the script RUNS but the
    geometry/semantics are wrong (no traceback exists in that case).
    """
    sections = ["## AssemblyBrief\n\n```json\n" + brief.model_dump_json(indent=2) + "\n```"]
    if exec_error:
        sections.append(
            "## Script execution failure (the script produced no STEP -- "
            "fix this first)\n\n```\n" + exec_error[-2000:] + "\n```"
        )
    if repair_context:
        sections.append(
            "## Router feedback (why this script rebuild was requested -- "
            "the script runs but the assembled result failed QA/Judge)\n\n"
            + repair_context
        )
    if qa is not None and qa.error_details:
        sections.append(
            "## QA failures\n\n"
            + "\n".join(f"- {d}" for d in qa.error_details)
        )
    user_prompt = (
        "## Current temp_assembly.py\n\n```python\n"
        + script_src
        + "\n```\n\n" + "\n\n".join(sections)
    )
    client = _llm_client()
    messages = [
        {"role": "system", "content": _prompt("assembly_repair.md")},
        {"role": "user", "content": user_prompt},
    ]
    try:
        resp = client.chat.completions.create(
            model=cfg.ASSEMBLY_REPAIR_MODEL,
            messages=messages,
            temperature=cfg.ASSEMBLY_REPAIR_TEMPERATURE,
            max_tokens=cfg.ASSEMBLY_REPAIR_MAX_TOKENS,
            timeout=_LLM_API_TIMEOUT,
            **cfg.ASSEMBLY_REPAIR_KWARGS,
        )
        raw = resp.choices[0].message.content or ""
        fixed = _extract_code_from_llm_response(raw)
        if "AssemblyHelper" in fixed and "asm.build" in fixed:
            return fixed
        print(
            f"[assembly] LLM script repair rejected: extracted "
            f"{len(fixed)} chars without the AssemblyHelper/asm.build markers"
        )
        return None
    except Exception as exc:  # noqa: BLE001 - repair is best-effort
        # Best-effort, never fatal -- but never silent either: a bare
        # except hid a 180s timeout (thinking-mode model on a ~70KB
        # prompt) for a whole run while the outer loop burned its budget
        # on identical scripts (2026-09-09).
        print(f"[assembly] LLM script repair failed: {exc}")
        return None


def node_assembler(state: AssemblyGraphState) -> dict:
    brief: AssemblyBrief = state["assembly_brief"]
    plan: MatingPlan | None = state.get("mating_plan")
    if plan is None:
        report = AssemblyQAReport(
            error_details=["mating plan missing -- cannot assemble"],
            all_passed=False,
            error_type=AssemblyErrorType.FATAL,
        )
        return {
            "qa_report": report,
            "assembly_step_path": "",
            "qa_skipped_iter": True,  # no STEP produced, no QA detector run
            "execution_log": _log(state, "assembler: no mating plan"),
            "node_history": state.get("node_history", []) + ["assembler"],
        }
    work_dir = Path(state["work_dir"])
    iteration = state.get("iteration_count", 0)
    qa: AssemblyQAReport | None = state.get("qa_report")
    part_results: dict = state.get("part_results") or {}

    # Missing parts short-circuit into a synthetic part_missing QA report.
    missing = [
        p.part_id for p in brief.parts
        if not (part_results.get(p.part_id) and part_results[p.part_id].ok)
    ]
    if missing:
        # v3 no-op loop guard (P0-3): a part whose failure says the spec is
        # UNCHANGED since the last failed generation has no part-level
        # repair channel (same base + same features = same failure). Route
        # those to RECOMPOSE instead of burning PART_BUILDER_MAX_RUNS on
        # deterministic no-op rebuilds.
        recompose_ids = [
            pid for pid in missing
            if str(getattr(part_results.get(pid), "error", "") or "")
            .startswith("v3 spec unchanged")
        ]
        details = [f"part generation failed: {missing}"]
        for pid in missing:
            err = getattr(part_results.get(pid), "error", None)
            if err:
                details.append(f"{pid}: {err}")
        report = AssemblyQAReport(
            part_count_expected=brief.expected_part_count,
            part_count_measured=len(brief.parts) - len(missing),
            part_count_passed=False,
            missing_parts=missing,
            error_details=details,
            all_passed=False,
            error_type=AssemblyErrorType.PART_MISSING,
            needs_remodel_part_ids=missing,
            needs_recompose_ids=recompose_ids,
        )
        return {
            "qa_report": report,
            "assembly_step_path": "",
            "qa_skipped_iter": True,  # no STEP produced, no QA detector run
            "execution_log": _log(state, f"assembler: missing parts {missing}"),
            "node_history": state.get("node_history", []) + ["assembler"],
        }

    # Zero-token dimension reconciliation: verify the mating plan against
    # MEASURED part bboxes before spending an assembly+QA cycle on it.
    reconcile = reconcile_dimensions(brief, plan.mates, part_results)
    # Radius compatibility splits into two paths (BUG-002):
    #   * "radii incompatible" -- DEFINITE incompatibility (roles known +
    #     no bore/shaft pair fits). This is now BLOCKING: remate cannot
    #     change measured radii, so the parts themselves are wrong.
    #   * "radii UNVERIFIABLE" -- role detection was inconclusive or the
    #     arrangement is implicit-pin-without-explicit-marker. Stay on the
    #     warning path; downstream collision/kinematic QA is authoritative.
    radius_warnings = [
        e for e in reconcile.errors
        if e.startswith("reconcile: axis joint ") and "radii UNVERIFIABLE" in e
    ]
    blocking_reconcile = [e for e in reconcile.errors if e not in radius_warnings]
    for warning in radius_warnings:
        print(f"[assembly] reconcile WARNING (degraded continue): {warning}")
    if blocking_reconcile:
        report = AssemblyQAReport(
            error_details=blocking_reconcile,
            all_passed=False,
            error_type=AssemblyErrorType.RECONCILE,
            needs_mate_fix_ids=[m.mate_id for m in plan.mates],
            # Structured attribution (P1-8): a radius-incompatibility
            # failure is a PART-GEOMETRY defect (remate cannot change
            # measured radii); the router sends these part ids to
            # part_builder instead of the Mating Architect.
            error_attribution=reconcile.attribution,
            attribution_part_ids=reconcile.part_ids,
        )
        return {
            "qa_report": report,
            "assembly_step_path": "",
            "qa_skipped_iter": True,  # no STEP produced, no QA detector run
            "execution_log": _log(state, "assembler: " + blocking_reconcile[0][:120]),
            "node_history": state.get("node_history", []) + ["assembler"],
        }

    # Authoritative per-part STEP paths (relative to the job dir) from THIS
    # iteration's PartResults -- keeps a stale temp_output_*.step left in a
    # part directory by a failed regeneration out of the mtime glob.
    part_step_overrides: dict[str, str] = {}
    _work_dir_abs = work_dir.resolve()
    for pid, r in part_results.items():
        if getattr(r, "ok", False) and getattr(r, "step_path", ""):
            try:
                part_step_overrides[pid] = str(
                    Path(r.step_path).resolve().relative_to(_work_dir_abs)
                )
            except ValueError:
                pass  # STEP outside the job dir: let codegen glob instead

    # 1) Deterministic codegen (zero tokens) -- always regenerate the base.
    script_path = write_assembly_script(
        brief, plan.mates, work_dir, _REPO_ROOT, iteration,
        part_step_overrides=part_step_overrides,
    )

    ok, tail = run_assembly_script(
        script_path, work_dir,
        timeout=cfg.ASSEMBLY_SCRIPT_TIMEOUT,
        python_bin=cfg.PYTHON_BIN or sys.executable,
    )

    # repair_context is set by the feedback_router on the repair_assembly
    # route (judge REPAIR_ASSEMBLY / non-mate-level failures). Without
    # consuming it here, that route was a no-op loop: deterministic codegen
    # is a pure function of (brief, plan), so the regenerated script is
    # byte-identical to the one that just failed QA.
    repair_ctx = state.get("repair_context", "")

    # 2) LLM repair fallback on ANY execution failure (B4: previously
    #    first-round failures with qa=None skipped repair entirely).
    if not ok:
        repaired = _llm_repair_assembly(
            script_path.read_text(encoding="utf-8"), brief, qa,
            exec_error=tail, repair_context=repair_ctx,
        )
        if repaired:
            script_path = work_dir / f"temp_assembly_{iteration}_repaired.py"
            script_path.write_text(repaired, encoding="utf-8")
            ok, tail = run_assembly_script(
                script_path, work_dir,
                timeout=cfg.ASSEMBLY_SCRIPT_TIMEOUT,
                python_bin=cfg.PYTHON_BIN or sys.executable,
            )
    elif repair_ctx:
        # 3) Script RUNS but the router sent us back with failure context
        #    (geometry/semantics wrong, e.g. judge REPAIR_ASSEMBLY). Force
        #    the LLM pass even without a traceback -- otherwise this
        #    iteration reproduces the identical script and the identical
        #    QA failure until the outer budget is exhausted. If the
        #    repaired script fails to execute, keep the runnable
        #    deterministic one and let QA judge its geometry.
        repaired = _llm_repair_assembly(
            script_path.read_text(encoding="utf-8"), brief, qa,
            repair_context=repair_ctx,
        )
        if repaired:
            rep_path = work_dir / f"temp_assembly_{iteration}_repaired.py"
            rep_path.write_text(repaired, encoding="utf-8")
            ok2, tail2 = run_assembly_script(
                rep_path, work_dir,
                timeout=cfg.ASSEMBLY_SCRIPT_TIMEOUT,
                python_bin=cfg.PYTHON_BIN or sys.executable,
            )
            if ok2:
                script_path, tail = rep_path, tail2
            else:
                # The repaired script failed to execute. run_assembly_script
                # deletes the stale outputs BEFORE running, so the
                # deterministic script's good STEP/STL are gone at this
                # point -- re-run the deterministic script to restore them.
                # Without this, QA would see no STEP and synthesize FATAL
                # instead of judging the deterministic geometry (the stated
                # intent of keeping the runnable script), and every such
                # event would waste an outer-loop iteration + repair call.
                ok, tail = run_assembly_script(
                    script_path, work_dir,
                    timeout=cfg.ASSEMBLY_SCRIPT_TIMEOUT,
                    python_bin=cfg.PYTHON_BIN or sys.executable,
                )

    return {
        "assembly_py_path": str(script_path),
        # BUG-024: on script failure the STEP/STL files don't exist.
        # Writing their expected paths to state as if valid made
        # _final_report print "Assembly: <nonexistent path>" and the
        # handoff could pick up stale artifacts from a previous run.
        # Only emit the path when the file was actually produced.
        "assembly_step_path": (
            str(work_dir / "assembly_output.step")
            if ok and (work_dir / "assembly_output.step").is_file()
            else ""
        ),
        "assembly_stl_path": (
            str(work_dir / "assembly_output.stl")
            if ok and (work_dir / "assembly_output.stl").is_file()
            else ""
        ),
        # Exact files embedded into the generated script.  Downstream QA
        # must inspect these same artifacts rather than independently
        # resolving a possibly stale PartResult path.
        "assembly_part_step_paths": {
            pid: str((work_dir / rel).resolve())
            for pid, rel in part_step_overrides.items()
        } if ok else {},
        # Exec tail of the last failed run ("" on success). Without this,
        # the QA node's FATAL synthesis reports a generic "STEP was not
        # produced" and the Judge/Router never see WHY the script died
        # (telescopic-crane run 2026-09-09: the SELECTOR-miss traceback
        # was discarded three iterations in a row).
        "assembly_exec_error": "" if ok else tail[-1500:],
        # Consumed above (or irrelevant on this path); clear so a stale
        # context can never force LLM repair on a future plain rebuild.
        "repair_context": "",
        # Clear the qa_report on the normal path so the downstream QA node
        # can distinguish "assembler produced a (possibly failed) script"
        # (no existing report -> synthesize fresh FATAL) from "assembler
        # early-returned with RECONCILE/PART_MISSING this iteration"
        # (existing report -> preserve, the assembler already classified
        # the failure with specific routing hints).
        "qa_report": None,
        # Normal path: the QA detector will run on the produced STEP, so
        # iteration_count +1 is appropriate. Clear the skip flag in case
        # the previous iteration early-returned and set it.
        "qa_skipped_iter": False,
        "execution_log": _log(
            state,
            f"assembler iter={iteration}: "
            + ("built " + str(script_path.name) if ok else f"FAILED: {tail}"),
        ),
        "node_history": state.get("node_history", []) + ["assembler"],
    }


# ---------------------------------------------------------------------------
# Agent 4b: AssemblyQA
# ---------------------------------------------------------------------------


def node_assembly_qa(state: AssemblyGraphState) -> dict:
    brief: AssemblyBrief = state["assembly_brief"]
    plan: MatingPlan | None = state.get("mating_plan")
    work_dir = Path(state["work_dir"])

    if not Path(state.get("assembly_step_path") or "").is_file():
        # The assembler node may have already classified this failure via
        # an early-return path (RECONCILE for mate-dim mismatch,
        # PART_MISSING for missing parts, FATAL for missing mating plan).
        # That report carries specific routing hints (needs_mate_fix_ids /
        # needs_remodel_part_ids) that the Judge + Router use to dispatch
        # to remate vs. remodel vs. repair_assembly. Without this
        # preservation, the generic "STEP not produced" FATAL we'd
        # synthesize here misroutes every reconcile failure to
        # repair_assembly (the script isn't even the problem -- the mates
        # are), and the loop never recovers.
        #
        # The assembler clears qa_report=None on its normal path (script
        # was written, may or may not have produced a STEP), so a non-None
        # report here means the assembler early-returned this iteration.
        existing = state.get("qa_report")
        if (existing is not None
                and not existing.all_passed
                and existing.error_type in (
                    AssemblyErrorType.RECONCILE,
                    AssemblyErrorType.PART_MISSING,
                    AssemblyErrorType.FATAL,
                )):
            report = existing
        else:
            # Distinguish "a part failed -> no STEP" (route back to
            # part_builder) from "all parts OK but assembler produced no
            # STEP" (assembler script bug -> FATAL -> repair_assembly).
            part_results = state.get("part_results") or {}
            failed_parts = [
                pid for pid, r in part_results.items()
                if not getattr(r, "ok", False)
            ]
            if failed_parts:
                report = AssemblyQAReport(
                    error_details=[
                        "assembly STEP was not produced because part generation "
                        f"failed: {', '.join(failed_parts)}"
                    ],
                    all_passed=False,
                    error_type=AssemblyErrorType.PART_MISSING,
                    missing_parts=failed_parts,
                    needs_remodel_part_ids=failed_parts,
                    part_count_expected=brief.expected_part_count or len(brief.parts),
                    part_count_measured=sum(
                        1 for r in part_results.values()
                        if getattr(r, "ok", False)
                    ),
                    part_count_passed=False,
                )
            else:
                exec_tail = str(state.get("assembly_exec_error", "") or "").strip()
                details = ["assembly STEP was not produced"]
                if exec_tail:
                    # The assembler's real traceback (selector misses,
                    # import failures, codegen crashes) -- without it the
                    # Judge and feedback_router only ever see the generic
                    # line above and guess repair_assembly, which
                    # regenerates a byte-identical script.
                    details.append(
                        "script execution failure:\n" + exec_tail[-1200:]
                    )
                report = AssemblyQAReport(
                    error_details=details,
                    all_passed=False,
                    error_type=AssemblyErrorType.FATAL,
                )
    else:
        report = run_assembly_qa(
            brief,
            plan.mates if plan else [],
            state.get("part_results") or {},
            work_dir,
            authoritative_step_paths=(
                state.get("assembly_part_step_paths") or {}
            ),
        )

    status = "PASS" if report.all_passed else f"FAIL ({report.error_type.value})"
    print(f"[assembly] QA: {status}")
    for d in report.error_details[:8]:
        print(f"    - {d}")

    # Consume iteration_count +1 ONLY when the real QA detector ran on a
    # produced STEP. When the assembler early-returned (no mating plan /
    # missing parts / reconcile errors), it set qa_skipped_iter=True; the
    # QA node just propagates the existing report without mesh / envelope
    # / kinematic checks, so it should NOT burn the outer-loop budget. The
    # per-route sub-budgets (MATING_MAX_RUNS / DECOMPOSER_MAX_RUNS) still
    # bind for their respective routes; this only stops the outer
    # ASSEMBLY_MAX_ITERATIONS from being consumed by reconcile-failure
    # spam (D8: 4 consecutive reconcile failures previously halved the
    # outer budget without ever running a single QA detector).
    if state.get("qa_skipped_iter"):
        next_iter = state.get("iteration_count", 0)
        iter_note = " (qa_skipped_iter: no +1)"
    else:
        next_iter = state.get("iteration_count", 0) + 1
        iter_note = ""

    return {
        "qa_report": report,
        "iteration_count": next_iter,
        # Clear the skip flag after consuming it so the next iteration's
        # normal path (STEP produced) increments iteration_count cleanly.
        "qa_skipped_iter": False,
        "execution_log": _log(state, f"assembly_qa: {status}{iter_note}"),
        "node_history": state.get("node_history", []) + ["assembly_qa"],
    }


# ---------------------------------------------------------------------------
# Agent 5: Assembly Judge (MAC QA-Judge pattern)
# ---------------------------------------------------------------------------


def _judge_gate(decision: AssemblyJudgeDecision, qa: AssemblyQAReport) -> AssemblyJudgeDecision:
    """Code-level anti-hallucination gate (layer 3 of 5).

    * accept/halt with empty evidence -> repair_assembly
    * accept on part_missing/interference/fatal -> needs high confidence
    """
    if decision.action in (AssemblyJudgeAction.ACCEPT, AssemblyJudgeAction.HALT):
        if not [e for e in decision.evidence if e.strip()]:
            return decision.model_copy(update={
                "action": AssemblyJudgeAction.REPAIR_ASSEMBLY,
                "reason": "DOWNGRADED (empty evidence): " + decision.reason,
            })
        needs_high = qa.error_type in (
            AssemblyErrorType.PART_MISSING,
            AssemblyErrorType.INTERFERENCE,
            AssemblyErrorType.FATAL,
        )
        if decision.action == AssemblyJudgeAction.ACCEPT and needs_high and decision.confidence != "high":
            return decision.model_copy(update={
                "action": AssemblyJudgeAction.REPAIR_ASSEMBLY,
                "reason": "DOWNGRADED (accept on hard error needs high confidence): "
                          + decision.reason,
            })
    return decision


def node_assembly_judge(state: AssemblyGraphState) -> dict:
    qa: AssemblyQAReport | None = state.get("qa_report")
    semantic_only = bool(
        qa is not None and qa.all_passed
        and not getattr(qa, "has_degraded_parts", False)
    )
    # Accepted geometry is immutable for this job. When its swept collision
    # has no independent part-geometry attribution, the only actionable
    # correction is a new mate pose. The multimodal Judge cannot remodel
    # these parts and costs a long model call before routing to the same
    # remate path; skip it without weakening the geometric QA failure.
    if (
        qa is not None
        and not qa.all_passed
        and qa.error_type == AssemblyErrorType.KINEMATIC
        and getattr(qa, "error_attribution", "ambiguous") != "part_geometry"
    ):
        brief = state.get("assembly_brief")
        work_dir = state.get("work_dir")
        if brief is not None and work_dir and all(
            has_explicit_accepted_cache(part, Path(work_dir) / "parts")
            for part in brief.parts
        ):
            return {
                "judge_decision": None,
                "execution_log": _log(state, "judge: skipped (immutable parts; remate)"),
                "node_history": state.get("node_history", []) + ["judge"],
            }
    # Skip Judge only when (a) disabled, (b) no QA report, (c) QA passed
    # with NO degraded parts, or (d) iteration_count below MIN_RETRY AND
    # the failure is neither a borderline false-positive NOR an
    # ENVELOPE/RECONCILE failure.
    # - QA-passed-but-degraded: a degraded PartResult (e.g. v3 base failed,
    #   fallback STEP kept) can pass every geometric check, yet it is NOT a
    #   clean success. Run the Judge at least once so the delivery is a
    #   deliberate accept-for-showcase (with the warnings cited) or a
    #   corrective remodel_parts/recompose -- never a silent pass.
    # - The is_likely_false_positive override lets the Judge run on the
    #   first iteration when the only failures are envelope overshoot within
    #   2x tolerance or interference volume under 2x tolerance -- so an
    #   ACCEPT can terminate the loop instead of forcing a route back for
    #   what is really a too-strict envelope.
    # - ENVELOPE / RECONCILE failures may be part-geometry defects (a part
    #   too big for the envelope, incompatible bore radii) that a blind
    #   first-round REMATE cannot fix -- it burns a Mating Architect call
    #   that the Judge's attribution (REMODEL_PARTS) would have avoided
    #   (P1-8). Let the Judge run on them from iteration 0.
    _qa_passed_degraded = bool(
        qa is not None and qa.all_passed
        and getattr(qa, "has_degraded_parts", False)
    )
    _needs_judge_early = (
        getattr(qa, "is_likely_false_positive", False)
        or _qa_passed_degraded
        or semantic_only
        or (qa is not None and qa.error_type in (
            AssemblyErrorType.ENVELOPE,
            AssemblyErrorType.RECONCILE,
        ))
    )
    if semantic_only and cfg.ASSEMBLY_JUDGE_MULTIMODAL == "never":
        return {
            "qa_report": qa.model_copy(update={
                "semantic_verification": "unverified",
            }),
            "judge_decision": None,
            "execution_log": _log(
                state, "judge: semantic visual check skipped (multimodal=never)"
            ),
            "node_history": state.get("node_history", []) + ["judge"],
        }

    if (
        not cfg.ASSEMBLY_JUDGE_ENABLED
        or qa is None
        or (qa.all_passed and not _qa_passed_degraded and not semantic_only)
        or (
            state.get("iteration_count", 0) < cfg.ASSEMBLY_JUDGE_MIN_RETRY
            and not _needs_judge_early
        )
    ):
        return {
            "judge_decision": None,
            "execution_log": _log(state, "judge: skipped"),
            "node_history": state.get("node_history", []) + ["judge"],
        }

    brief: AssemblyBrief = state["assembly_brief"]
    work_dir = Path(state["work_dir"])

    user_prompt = (
        f"## User Request\n\n{brief.user_request_raw}\n\n"
        f"## AssemblyBrief (special_features = design intent)\n\n"
        + json.dumps(brief.special_features, indent=2)
        + "\n\n## QA Report\n\n```json\n"
        + qa.model_dump_json(indent=2)
        + "\n```\n\n"
        f"retry_count: {state.get('iteration_count', 0)}\n\n"
        + (
            "The deterministic assembly checks passed. Perform an optional "
            "visual semantic check against the original natural-language "
            "request. Inspect orientation, handedness/symmetry, hinge and "
            "interface placement, feature shape, overlaps, gaps, and floating "
            "parts. A visible mismatch must include concrete localized "
            "modification_suggestions. If rendered current-model views are "
            "not available, mark semantics unverified and do not block delivery."
            if semantic_only else ""
        )
    )

    content: str | list = user_prompt
    views = render_assembly_views(work_dir)
    image_urls: list[str] = []
    # B7 fix: the judge prompt promises user_image[N] -- actually feed them
    # (same loader as the Decomposer, deterministic alphabetical order; same
    # CWD-independent directory resolution).
    user_images = _load_user_images(_input_images_dir(work_dir))
    image_urls += [_encode_jpeg_data_url(b) for b in user_images]
    image_urls += [encode_png_data_url(v) for v in views]
    if semantic_only and not views:
        return {
            "qa_report": qa.model_copy(update={
                "semantic_verification": "unverified",
            }),
            "judge_decision": None,
            "execution_log": _log(
                state, "judge: semantic visual check skipped (no rendered views)"
            ),
            "node_history": state.get("node_history", []) + ["judge"],
        }
    if image_urls and cfg.ASSEMBLY_JUDGE_MULTIMODAL != "never":
        content = build_multimodal_content(user_prompt, image_urls)

    decision: AssemblyJudgeDecision | None = None
    judge_failure_msg: str = ""
    multimodal_mode = cfg.ASSEMBLY_JUDGE_MULTIMODAL
    try:
        raw = call_llm_json(
            _prompt("assembly_judge.md"),
            content,
            model=cfg.ASSEMBLY_JUDGE_MODEL,
            temperature=cfg.ASSEMBLY_JUDGE_TEMPERATURE,
            max_tokens=cfg.ASSEMBLY_JUDGE_MAX_TOKENS,
            extra_kwargs=cfg.ASSEMBLY_JUDGE_KWARGS,
        )
        decision = _judge_gate(AssemblyJudgeDecision.model_validate(raw), qa)
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
        judge_failure_msg = f"{type(exc).__name__}: {err[:200]}"
        if (
            image_urls
            and isinstance(content, list)
            and multimodal_mode == "auto"
            and _is_multimodal_unsupported_error(err)
        ):
            if semantic_only:
                print(
                    "[assembly] semantic visual check skipped: configured "
                    "model does not support image input"
                )
                return {
                    "qa_report": qa.model_copy(update={
                        "semantic_verification": "unverified",
                    }),
                    "judge_decision": None,
                    "execution_log": _log(
                        state,
                        "judge: semantic visual check skipped "
                        "(model has no vision support)",
                    ),
                    "node_history": state.get("node_history", []) + ["judge"],
                }
            # Auto-fallback: retry once without images (MAC Judge pattern).
            try:
                raw = call_llm_json(
                    _prompt("assembly_judge.md"),
                    user_prompt,
                    model=cfg.ASSEMBLY_JUDGE_MODEL,
                    temperature=cfg.ASSEMBLY_JUDGE_TEMPERATURE,
                    max_tokens=cfg.ASSEMBLY_JUDGE_MAX_TOKENS,
                    extra_kwargs=cfg.ASSEMBLY_JUDGE_KWARGS,
                )
                decision = _judge_gate(
                    AssemblyJudgeDecision.model_validate(raw), qa
                )
            except Exception:  # noqa: BLE001
                decision = None
        # BUG-025: when every Judge attempt fails, return decision=None
        # and let node_feedback_router route via its existing
        # decision=None path (which already picks the right stage from
        # qa.error_type: PART_MISSING -> part_builder, MATE-level ->
        # mating_architect, FATAL -> assembler). The previous fallback
        # fabricated REPAIR_ASSEMBLY for every failure type, routing
        # part_missing and mate-level errors to the assembler, which
        # cannot fix them.
        if decision is None:
            print(
                f"[assembly] JUDGE FAILED ({judge_failure_msg}); "
                f"routing via decision=None path (qa.error_type="
                f"{qa.error_type.value if qa else 'none'})"
            )
            return {
                "qa_report": (
                    qa.model_copy(update={"semantic_verification": "unverified"})
                    if semantic_only else qa
                ),
                "judge_decision": None,
                "execution_log": _log(
                    state,
                    f"judge: call failed ({judge_failure_msg}); "
                    f"router will route by qa.error_type="
                    f"{qa.error_type.value if qa else 'none'}",
                ),
                "node_history": state.get("node_history", []) + ["judge"],
            }

    if semantic_only:
        has_view_evidence = any(
            "view[" in item.lower() for item in decision.evidence
        )
        if decision.semantic_verification == "failed" and has_view_evidence:
            suggestions = list(decision.modification_suggestions)
            if not suggestions and decision.reason.strip():
                suggestions = [decision.reason.strip()]
            updates: dict = {"modification_suggestions": suggestions}
            if decision.action in (
                AssemblyJudgeAction.ACCEPT,
                AssemblyJudgeAction.HALT,
            ):
                updates["action"] = (
                    AssemblyJudgeAction.REMODEL_PARTS
                    if decision.remodel_part_ids
                    else AssemblyJudgeAction.REPAIR_ASSEMBLY
                )
            decision = decision.model_copy(update=updates)
            qa = qa.model_copy(update={
                "semantic_verification": "failed",
                "semantic_issues": [decision.reason],
                "semantic_modification_suggestions": suggestions,
            })
        else:
            semantic = decision.semantic_verification
            if semantic in ("verified", "failed") and not has_view_evidence:
                semantic = "unverified"
            decision = decision.model_copy(update={
                "action": AssemblyJudgeAction.ACCEPT,
                "semantic_verification": semantic,
                "modification_suggestions": [],
            })
            # A response without current-view evidence remains non-blocking
            # and explicitly unverified, even if the model claimed otherwise.
            qa = qa.model_copy(update={
                "semantic_verification": semantic,
                "semantic_issues": [],
                "semantic_modification_suggestions": [],
            })

    print(f"[assembly] JUDGE -> {decision.action.value} "
          f"(conf={decision.confidence}, evidence={len(decision.evidence)})")
    return {
        "qa_report": qa,
        "judge_decision": decision,
        "execution_log": _log(
            state,
            f"judge: {decision.action.value} - {decision.reason[:120]}",
        ),
        "node_history": state.get("node_history", []) + ["judge"],
    }
