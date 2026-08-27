"""Pydantic schemas for the multi-agent assembly pipeline.

Layered on top of MAC's single-part pipeline (CADBrief / ArchitectPlan).
Assembly philosophy and terminology are borrowed from the CAD Skills
``positioning.md`` reference:

* Positioning is authored in source (the generated ``temp_assembly.py``
  uses ``cadpy.assembly.AssemblyHelper`` + native build123d joints).
* Mates are semantic relationships (face_to_face / coaxial / rigid /
  revolute), not raw transforms.
* fixed-first directionality: ``MateSpec.fixed_part_id`` never moves.
* Every anchor is a *named datum* on a part (bbox face centre or an axis
  point) so the MateSpec -> AssemblyHelper translation is deterministic
  (the assembly-layer analogue of MAC's ``_plan_to_code``).
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class MateType(str, Enum):
    """Semantic mate type -- maps 1:1 onto AssemblyHelper methods."""

    RIGID = "rigid"                  # coincident anchors ( asm.connect )
    FACE_TO_FACE = "face_to_face"    # seated faces + gap      ( asm.face_to_face )
    COAXIAL = "coaxial"              # shared axis             ( asm.coaxial )
    REVOLUTE = "revolute"            # hinge w/ static pose    ( asm.revolute )
    LINEAR = "linear"                # slider along an axis    ( asm.connect linear )
    CYLINDRICAL = "cylindrical"      # rotation + translation  ( asm.connect cylindrical )
    BALL = "ball"                    # 2-DOF ball-and-socket   ( asm.ball )


class AnchorKind(str, Enum):
    """Deterministic named datum on a part (part-local coordinates)."""

    FACE = "face"          # centre of a bbox face (top/bottom/...)
    AXIS_POINT = "axis_point"  # bbox centre offset along an axis
    SELECTOR = "selector"  # real topology face resolved via cadpy (see FaceQuery)
    SPHERE = "sphere"      # sphere center + radius (ball joint anchor)


class FaceSelectCriterion(str, Enum):
    LARGEST = "largest"            # largest area among matching faces
    CLOSEST_TO = "closest_to"      # coordinate/radius nearest ``value_mm``


class FaceQuery(BaseModel):
    """Semantic face query for a SELECTOR anchor -- resolved against the
    part's real cadpy topology after the part is built (the anchor-side fix
    for bbox != functional datum, e.g. a skirted or knobbed lid)."""

    surface: Literal["plane", "cylinder"] = Field(..., description="Face type.")
    axis: Literal["x", "y", "z"] = Field(
        default="z",
        description="Plane normal axis, or cylinder axis.",
    )
    normal_sign: Literal[1, -1] | None = Field(
        None,
        description="plane only: +1 for a face whose normal points +axis "
                    "(e.g. top), -1 for the opposite (e.g. bottom mating face).",
    )
    select: FaceSelectCriterion = Field(default=FaceSelectCriterion.LARGEST)
    value_mm: float | None = Field(
        None,
        description="For closest_to: target plane coordinate along ``axis`` "
                    "(plane) or target radius (cylinder).",
    )
    # Position-based disambiguation for parts with multiple matching
    # cylinders (e.g. a link_bar with two through-holes of the same
    # radius -- both match `closest_to value_mm=R`, the resolver picks
    # one arbitrarily). Setting any of target_x/y/z_mm tells the resolver
    # to prefer the cylinder whose axis midpoint is nearest this target
    # position (in part-local coords). Use for the +X / -X end bores of
    # a link_bar: target_x_mm=+26 picks the +X bore, target_x_mm=-26
    # picks the -X bore.
    target_x_mm: float | None = Field(
        None,
        description="Optional: prefer the cylinder whose axis midpoint is "
                    "nearest this X (part-local). Disambiguates multi-bore parts.",
    )
    target_y_mm: float | None = Field(
        None,
        description="Optional: prefer the cylinder whose axis midpoint is "
                    "nearest this Y (part-local).",
    )
    target_z_mm: float | None = Field(
        None,
        description="Optional: prefer the cylinder whose axis midpoint is "
                    "nearest this Z (part-local).",
    )

    @model_validator(mode="after")
    def _check_closest_to_value(self) -> "FaceQuery":
        if self.select == FaceSelectCriterion.CLOSEST_TO and self.value_mm is None:
            raise ValueError(
                "select=closest_to requires value_mm (target coordinate or "
                "radius) -- otherwise the query silently degrades to largest"
            )
        return self


class AssemblyErrorType(str, Enum):
    NONE = "none"
    PART_MISSING = "part_missing"
    MATE_MISALIGNMENT = "mate_misalignment"
    INTERFERENCE = "interference"
    KINEMATIC = "kinematic"
    ENVELOPE = "envelope"
    RECONCILE = "reconcile"
    FATAL = "fatal"


class InterfaceType(str, Enum):
    """Functional interface category (Decomposer prose -> Architect mates)."""

    SEAT = "seat"      # face contact / stacking
    AXIS = "axis"      # coaxial / pin-in-bore
    HINGE = "hinge"    # rotational joint (1-DOF revolute)
    SLIDE = "slide"    # translational joint
    FIXED = "fixed"    # bolted / glued / same-substructure
    BALL = "ball"      # 2-DOF spherical joint (pitch + yaw, like a hip / thumb root)


# ---------------------------------------------------------------------------
# Decomposer output (assembly-level CADBrief)
# ---------------------------------------------------------------------------


class Anchor(BaseModel):
    """A named datum on one part.

    Kept fully structured (no free text) so ``assembly_codegen`` can
    translate anchors into build123d ``Location`` / ``Axis`` objects
    deterministically, with zero LLM involvement.
    """

    kind: AnchorKind = Field(..., description="Datum category.")
    face: Literal["top", "bottom", "left", "right", "front", "back"] | None = Field(
        None,
        description="For kind=face: which bbox face (top = +Z, bottom = -Z, "
                    "left = -X, right = +X, front = -Y, back = +Y).",
    )
    axis: Literal["x", "y", "z"] | None = Field(
        None,
        description="For kind=axis_point: offset direction from bbox centre.",
    )
    offset_mm: float = Field(
        default=0.0,
        description="For kind=axis_point: signed offset from bbox centre "
                    "along ``axis``. For kind=face: unused (0).",
    )
    selector_query: FaceQuery | None = Field(
        None,
        description="For kind=selector: semantic face query resolved against "
                    "the part's real topology (cadpy) at assembly time.",
    )
    sphere_center_mm: list[float] | None = Field(
        None,
        description="For kind=sphere: [x, y, z] sphere center in part-local "
                    "coords. The ball center on a ball part; the cavity "
                    "center on a socket part.",
    )
    sphere_radius_mm: float | None = Field(
        None,
        description="For kind=sphere: sphere radius in mm. Socket uses the "
                    "cavity radius; ball uses the ball's outer radius "
                    "(typically <= socket cavity radius for clearance).",
    )

    @model_validator(mode="after")
    def _check_kind_fields(self) -> "Anchor":
        if self.kind == AnchorKind.FACE and self.face is None:
            raise ValueError("kind=face requires 'face'")
        if self.kind == AnchorKind.AXIS_POINT and self.axis is None:
            raise ValueError("kind=axis_point requires 'axis'")
        if self.kind == AnchorKind.SELECTOR and self.selector_query is None:
            raise ValueError("kind=selector requires 'selector_query'")
        if self.kind == AnchorKind.SPHERE and (
            self.sphere_center_mm is None or self.sphere_radius_mm is None
        ):
            raise ValueError(
                "kind=sphere requires both 'sphere_center_mm' (list of 3 "
                "floats) and 'sphere_radius_mm' (float)"
            )
        return self


class FeatureAttachment(BaseModel):
    """Where + how a v3 feature operator attaches to the base body."""

    attach_point_mm: list[float] = Field(
        ...,
        description="[x, y, z] attach point on the base body (part-local "
                    "coords). The feature operator fuses (additive ops) "
                    "or subtracts (subtractive ops) at this point. The "
                    "Decomposer's planned attach_point assumes a nominal "
                    "base body geometry; apply_feature probes the actual "
                    "base surface and snaps if drift > 0.5mm (see "
                    "feature_operators._snap_to_surface).",
    )
    direction: Literal["+x", "-x", "+y", "-y", "+z", "-z"] = Field(
        default="+x",
        description="Direction the feature protrudes (for additive ops "
                    "like clevis_fork / clevis_tongue / knuckle_ear / "
                    "ball_stem) OR the axis it extends along (for "
                    "subtractive ops like through_bore / ball_cavity). "
                    "The attach-point snap + surface-normal check use "
                    "-direction as the expected surface normal.",
    )


class Feature(BaseModel):
    """A kinematic feature operator invocation on a v3 base body."""

    name: str = Field(
        ...,
        description="Operator key in FEATURE_OPERATORS registry "
                    "(mac_assembly.feature_operators). Initial set: "
                    "'clevis_fork', 'clevis_tongue', 'through_bore', "
                    "'ball_cavity', 'ball_stem', 'knuckle_ear'.",
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Operator-specific params (ear_length, bore_radius, "
                    "ear_width, tongue_thickness, clearance_side, "
                    "sphere_radius, stem_radius, etc.). Excludes "
                    "attach_point_mm + direction (those are on `attachment`).",
    )
    attachment: FeatureAttachment = Field(
        ...,
        description="Where + how the feature attaches to the base body.",
    )


class BaseBodySpec(BaseModel):
    """LLM-generated base body description for the MAC Coder agent."""

    description: str = Field(
        ...,
        description="Self-contained CAD prompt for the base body (overall "
                    "dims, key features, units=mm, origin convention). "
                    "The Decomposer includes attach-point hints so the "
                    "Coder agent leaves flat surfaces where features will "
                    "attach. This description is the cache key for the "
                    "feature-only-remodel fast path (run_part_with_features "
                    "skips MAC Coder when this description is byte-"
                    "identical to the previous run's cached description).",
    )
    key_dimensions: dict[str, Any] = Field(
        default_factory=dict,
        description="Critical dims the feature operators may need "
                    "(e.g. plate_w, plate_d, plate_t).",
    )
    local_bounds: dict[str, float] | None = Field(
        None,
        description="Structured axis-aligned bounding box of the base body "
                    "in part-local coords, as 6 floats: "
                    "{xmin, xmax, ymin, ymax, zmin, zmax}. Used by "
                    "run_part_with_features' _maybe_recenter_base_body to "
                    "auto-correct base body position when the LLM Coder "
                    "used Sketch+extrude (default-centered on origin) "
                    "instead of Pos(x,y,z)*Box(...) (positioned at the "
                    "spec's center). When None, recentering falls back to "
                    "regex-parsing the description for 'Local X=.., Y=.., "
                    "Z=..' (less reliable -- description format varies "
                    "across prompts). Decomposer should always set this "
                    "for rectangular-prism parts; organic shapes (e.g. "
                    "palm with fillets) can leave it None.",
    )


class PartSpec(BaseModel):
    """One physical part instance in the assembly.

    ``description`` is the *entire* prompt MAC's single-part pipeline
    sees: it must be self-contained (dimensions, features, units,
    origin convention) because Agent 2/3 never see other parts.
    Repeated parts (3 planet gears) are expanded into separate
    PartSpec entries by the Decomposer (planet_gear_1, ...).
    """

    part_id: str = Field(
        ...,
        description="Unique kebab-case ID, e.g. 'base_plate', 'planet_gear_1'.",
    )
    part_name: str = Field(..., description="Human-readable name.")
    description: str = Field(
        ...,
        description="Self-contained single-part CAD prompt: overall dims, "
                    "key features (holes/bosses/fillets), units=mm, origin "
                    "convention, and the mating interfaces this part exposes "
                    "(e.g. 'flat bottom face', 'central Z-axis bore R10').",
    )
    key_dimensions: dict[str, Any] = Field(
        default_factory=dict,
        description="Critical dims the Assembler may need, e.g. "
                    "{'height': 30.0, 'bore_radius': 10.0}. Values may be "
                    "floats OR lists of floats (for coordinate tuples like "
                    "'bore_axis_midpoint_local': [36.0, 0.0, 9.0] when the "
                    "Decomposer specifies explicit local coordinate ranges).",
    )
    builder: dict[str, Any] | None = Field(
        default=None,
        description='Optional: {"name": "knuckle_hinge_ear", "params": {...}}. '
                    "When specified, the PartBuilder calls the named builder "
                    "from mac_assembly.builders directly (bypassing the "
                    "LLM-driven Spec Planner -> Architect -> Coder -> Skill "
                    "Loop) to generate this part's geometry. Use for "
                    "structures the LLM struggles with (horizontal-axis "
                    "cylinders, knuckle ears with horizontal bores, etc.). "
                    "The description field is still required (for audit and "
                    "for the QA engine's mate anchor resolution against the "
                    "generated STEP topology).",
    )
    reuses_part_id: str | None = Field(
        default=None,
        description="If set, this PartSpec is an INSTANCE that reuses "
                    "another PartSpec's generated geometry. PartBuilder "
                    "skips the MAC pipeline and copies the template's "
                    "STEP/STL/py into this part's directory. Use for "
                    "identical repeats (4x M3 screws, 3x planet gears): "
                    "emit ONE source PartSpec with full description + "
                    "N-1 instance PartSpecs with reuses_part_id pointing "
                    "to the source. Mates still reference part_id (each "
                    "instance has its own mates + placement). Mutually "
                    "exclusive with `builder` and `base_body`.",
    )
    base_body: BaseBodySpec | None = Field(
        default=None,
        description="v3 path: LLM-generated base body spec. Mutually "
                    "exclusive with `builder` and `reuses_part_id`. When "
                    "set, PartBuilder runs the MAC Coder agent on "
                    "base_body.description to generate the base body "
                    "STEP, then applies each entry in `features` "
                    "(deterministic feature operators) via import_step "
                    "+ apply_feature. Token cost: 1x MAC Coder call + "
                    "0 per feature operator. Use for parts where the "
                    "base body shape needs LLM freedom BUT kinematic "
                    "features (forks, tongues, ball cavities, knuckle "
                    "ears) must be reliable.",
    )
    features: list[Feature] = Field(
        default_factory=list,
        description="v3 path: kinematic feature operators applied to "
                    "`base_body` in sequence. Each Feature = {name, "
                    "params, attachment}. Empty unless `base_body` is "
                    "set (validator rejects features without base_body). "
                    "Subtractive ops (through_bore, ball_cavity) should "
                    "precede additive ops (clevis_fork, etc.) so later "
                    "additive features aren't cut by earlier subtractive "
                    "ops -- the Decomposer is responsible for ordering.",
    )

    @field_validator("part_id")
    @classmethod
    def _part_id_slug(cls, v: str) -> str:
        import re

        slug = re.sub(r"[^a-z0-9]+", "_", v.strip().lower()).strip("_")
        if not slug:
            raise ValueError(f"part_id {v!r} normalizes to empty slug")
        return slug

    @model_validator(mode="after")
    def _check_generation_mode(self) -> "PartSpec":
        """At most one of `builder`, `reuses_part_id`, `base_body` may be
        set (3-way mutual exclusion). `features` is only valid when
        `base_body` is set (features attach to a base body).
        """
        modes_set = [
            ("builder", self.builder is not None),
            ("reuses_part_id", self.reuses_part_id is not None),
            ("base_body", self.base_body is not None),
        ]
        set_modes = [name for name, is_set in modes_set if is_set]
        if len(set_modes) > 1:
            raise ValueError(
                f"part {self.part_id!r}: {set_modes} are mutually "
                f"exclusive (a part uses ONE generation mode: v2 builder, "
                f"v3 base_body+features, or reuse)"
            )
        if self.features and self.base_body is None:
            raise ValueError(
                f"part {self.part_id!r}: `features` requires `base_body` "
                f"(cannot attach kinematic features without a base body "
                f"to attach to)"
            )
        return self


class MateSpec(BaseModel):
    """One semantic mate relationship (fixed part never moves)."""

    mate_id: str = Field(..., description="Unique ID, e.g. 'lid_on_base'.")
    mate_type: MateType = Field(..., description="Semantic mate type.")
    fixed_part_id: str = Field(..., description="PartSpec.part_id (stays fixed).")
    moving_part_id: str = Field(..., description="PartSpec.part_id (gets placed).")
    fixed_anchor: Anchor = Field(..., description="Named datum on the fixed part.")
    moving_anchor: Anchor = Field(..., description="Named datum on the moving part.")
    offset_mm: float = Field(
        default=0.0,
        description="face_to_face: gap between the seated faces (>=0 for "
                    "stacking). coaxial/rigid: unused.",
    )
    angle_deg: float = Field(
        default=0.0,
        description="revolute: static pose angle in degrees.",
    )
    axial_offset_mm: float = Field(
        default=0.0,
        description="revolute/coaxial/rigid: signed offset along the "
                    "anchor direction (rotation axis for revolute/coaxial, "
                    "or face-normal/axis-point-axis/SELECTOR-axis for rigid) "
                    "from the fixed anchor's resolved point to where the "
                    "moving anchor should land. REVOLUTE/COAXIAL: use to "
                    "seat the moving part on a surface when both anchors "
                    "resolve to cylinder midpoints (post midpoint Z=19.75, "
                    "arm hole midpoint local Z=4, arm bottom should rest on "
                    "plate top Z=10 -> axial_offset_mm=-5.75 puts the fixed "
                    "frame at Z=14, moving anchor at local Z=4 lands at "
                    "world Z=14, arm bottom local Z=0 lands at world Z=10). "
                    "RIGID: use to position a moving part at a specific "
                    "offset along the anchor direction -- e.g. a second ear "
                    "90mm to the right of the first, when both ears' hole "
                    "midpoints are the SELECTOR anchors (rigid aligns the "
                    "two midpoints exactly; axial_offset_mm=90 along the "
                    "hole axis direction slides the second ear to its "
                    "target X). Sign: positive shifts along the resolved "
                    "anchor direction.",
    )
    position_mm: float = Field(
        default=0.0,
        description="linear/cylindrical: static pose translation along the "
                    "mate axis in mm.",
    )
    slide_axis: Literal["x", "y", "z"] | None = Field(
        None,
        description="linear/cylindrical ONLY: the slide/rotation DIRECTION "
                    "(x/y/z). Independent of the anchor points, which locate "
                    "the datum. Required when mate_type is linear/cylindrical.",
    )
    ball_pitch_deg: float = Field(
        default=0.0,
        description="ball mate: static pose pitch (rotation about Y) in "
                    "degrees. The codegen emits this as the first element "
                    "of the build123d BallJoint `angles` 3-tuple.",
    )
    ball_yaw_deg: float = Field(
        default=0.0,
        description="ball mate: static pose yaw (rotation about Z) in "
                    "degrees. Emitted as the second element of the "
                    "BallJoint `angles` 3-tuple. The third element (roll) "
                    "is always 0 -- 2-DOF constraint per the user spec.",
    )
    tolerance_mm: float = Field(
        default=0.5,
        description="AssemblyQA tolerance for this mate.",
    )
    notes: str | None = Field(
        None,
        description="Geometry description (OK); operation instructions "
                    "(forbidden -- the deterministic translator ignores them).",
    )


class FunctionalInterface(BaseModel):
    """A functional (prose) relationship between two parts -- the WHAT.

    Written by the Decomposer; the Mating Architect translates each
    interface into one or more structured MateSpecs (the HOW).
    """

    interface_id: str = Field(..., description="e.g. 'lid_seats_on_base'.")
    part_a: str = Field(..., description="Fixed-side part_id.")
    part_b: str = Field(..., description="Moving-side part_id.")
    interface_type: InterfaceType = Field(..., description="Category.")
    description: str = Field(
        ...,
        description="Prose description incl. functional gap/clearance, "
                    "e.g. 'lid bottom face seats on the base top rim with "
                    "a 0.2 mm gasket gap; lid must stay removable'.",
    )


class AssemblyBrief(BaseModel):
    """Assembly-level CADBrief produced by the Decomposer agent (the WHAT).

    Structure-only: parts + functional interfaces + envelope + intent.
    Structured mates (anchors/offsets) are designed later by the Mating
    Architect -- mirroring MAC's Spec Planner / Geometric Architect split.
    """

    assembly_name: str = Field(..., description="e.g. 'planetary_gear_stage'.")
    parts: list[PartSpec] = Field(..., min_length=1)
    interfaces: list[FunctionalInterface] = Field(
        default_factory=list,
        description="Functional relationships the Mating Architect must "
                    "realize as MateSpecs.",
    )
    expected_part_count: int = Field(
        default=0,
        description="Total solids in the final assembly (== len(parts) "
                    "since instances are expanded). 0 = auto-compute.",
    )
    overall_envelope_mm: dict[str, float] = Field(
        default_factory=dict,
        description="Expected assembly bbox, e.g. {'x': 140, 'y': 140, 'z': 60}.",
    )
    special_features: list[str] = Field(
        default_factory=list,
        description="Non-trivial assembly intent for the Judge, e.g. "
                    "'gear mesh requires 0.4mm backlash', "
                    "'lid must be removable (not fused)'.",
    )
    user_request_raw: str = Field(...)

    @model_validator(mode="after")
    def _validate_refs(self) -> "AssemblyBrief":
        ids = [p.part_id for p in self.parts]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate part_id(s): {sorted(dupes)}")
        for itf in self.interfaces:
            for role in ("part_a", "part_b"):
                ref = getattr(itf, role)
                if ref not in ids:
                    raise ValueError(
                        f"interface {itf.interface_id!r} references unknown part {ref!r}"
                    )
        # If the LLM wrote "3 planet gears" in prose but emitted only 1
        # PartSpec, the only deterministic signal is `expected_part_count`:
        # the field defaults to 0 (auto-compute to len(parts)) which would
        # silently mask an under-expanded brief. When the LLM sets it
        # explicitly, we cross-check against the actual part list length.
        if 0 < self.expected_part_count != len(self.parts):
            raise ValueError(
                f"expected_part_count={self.expected_part_count} != "
                f"len(parts)={len(self.parts)} -- expand repeated parts "
                f"into separate PartSpec entries (the validator does NOT "
                f"auto-expand a single PartSpec that says '3x' in prose)"
            )
        if self.expected_part_count <= 0:
            self.expected_part_count = len(self.parts)
        # Reuse references: reuses_part_id must point to an existing
        # non-reuse PartSpec (no chains, no cycles, no self-reference).
        # Reuse chains (A reuses B reuses C) are rejected because they
        # make template-vs-instance reasoning ambiguous and break the
        # two-bucket sort in node_part_builder.
        template_ids = {p.part_id for p in self.parts if p.reuses_part_id is None}
        for p in self.parts:
            if p.reuses_part_id is not None:
                if p.reuses_part_id == p.part_id:
                    raise ValueError(
                        f"part {p.part_id!r} cannot reuse itself"
                    )
                if p.reuses_part_id not in template_ids:
                    raise ValueError(
                        f"part {p.part_id!r}.reuses_part_id="
                        f"{p.reuses_part_id!r} is not a template (a "
                        f"non-reuse PartSpec). Reuse chains (A reuses B "
                        f"reuses C) are not allowed -- point directly at "
                        f"a source part with its own description."
                    )
        return self


class MatingPlan(BaseModel):
    """Structured mate design produced by the Mating Architect (the HOW).

    The assembly-layer analogue of MAC's ArchitectPlan: consumes the
    AssemblyBrief's functional interfaces and emits deterministic-
    translatable MateSpecs (named datums, offsets, tolerances).
    """

    assembly_name: str = Field(..., description="Must match the brief.")
    mates: list[MateSpec] = Field(default_factory=list)
    mating_notes: list[str] = Field(
        default_factory=list,
        description="Assumptions the Architect made (audit trail).",
    )

    @model_validator(mode="after")
    def _validate_structure(self) -> "MatingPlan":
        # mate_id uniqueness -- downstream codegen and the QA engine key
        # off mate_id; a duplicate would silently overwrite one mate's
        # entry, losing the LLM's intent for one of them.
        mate_ids = [m.mate_id for m in self.mates]
        id_dupes = {mid for mid in mate_ids if mate_ids.count(mid) > 1}
        if id_dupes:
            raise ValueError(
                f"duplicate mate_id(s): {sorted(id_dupes)} -- each mate "
                f"needs a unique id (e.g. 'finger_1_on_palm', "
                f"'finger_2_on_palm', not 'finger_on_palm' twice)"
            )
        # A part can be the moving_part_id of at most one mate (the mate
        # graph must be a tree rooted at one fixed part). When the LLM
        # puts a part on the moving side of multiple mates, tell it which
        # role the part should likely play instead -- the bare
        # "ambiguous placement" message left the LLM guessing.
        moved = [m.moving_part_id for m in self.mates]
        dupes = {i for i in moved if moved.count(i) > 1}
        if dupes:
            raise ValueError(
                f"parts with multiple primary mates (ambiguous placement): "
                f"{sorted(dupes)}. Each part can be moving_part_id of at "
                f"most one mate; if a part here is meant to be the fixed "
                f"root (e.g. a palm or base plate), make it fixed_part_id "
                f"in those mates instead of moving_part_id."
            )
        return self


# ---------------------------------------------------------------------------
# Part generation results (Agent 2/3 -- single-part MAC runs)
# ---------------------------------------------------------------------------


class PartResult(BaseModel):
    """Outcome of running the single-part MAC pipeline for one PartSpec."""

    part_id: str
    step_path: str = ""
    stl_path: str = ""
    py_path: str = ""
    part_dir: str = ""
    ok: bool = False
    attempts: int = 0
    error: str | None = None
    token_usage: dict[str, int] = Field(
        default_factory=dict,
        description="Aggregated token usage of this part's subprocess "
                    "(total_tokens etc., from its token_summary.json).",
    )


# ---------------------------------------------------------------------------
# Assembly QA (closed-loop detection)
# ---------------------------------------------------------------------------


class MateCheck(BaseModel):
    mate_id: str
    mate_type: MateType
    expected_mm: float = 0.0
    measured_mm: float = 0.0
    delta_mm: float = 0.0
    tolerance_mm: float = 0.5
    passed: bool = True
    detail: str = ""


class InterferenceCheck(BaseModel):
    part_a: str
    part_b: str
    penetration_volume_mm3: float = 0.0
    min_gap_mm: float | None = Field(
        None,
        description="Real surface-to-surface clearance (None when colliding "
                    "or not measurable). Functional-gap evidence for the Judge.",
    )
    passed: bool = True
    detail: str = ""


class KinematicCheck(BaseModel):
    """Revolute-joint sweep result: rotate the moving subtree about the
    joint axis over +/- KINEMATIC_SWEEP_DEG and test collisions against
    the static structure at each sample angle."""

    mate_id: str
    sweep_deg: list[float] = Field(default_factory=list)
    collision_deg: list[float] = Field(default_factory=list)
    passed: bool = True
    detail: str = ""


class AssemblyQAReport(BaseModel):
    part_count_expected: int = 0
    part_count_measured: int = 0
    part_count_passed: bool = True
    missing_parts: list[str] = Field(default_factory=list)

    envelope_expected_mm: dict[str, float] = Field(default_factory=dict)
    envelope_measured_mm: dict[str, float] = Field(default_factory=dict)
    envelope_passed: bool = True

    mate_checks: list[MateCheck] = Field(default_factory=list)
    mates_passed: bool = True

    interference_checks: list[InterferenceCheck] = Field(default_factory=list)
    interference_passed: bool = True

    kinematic_checks: list[KinematicCheck] = Field(default_factory=list)
    kinematics_passed: bool = True

    error_details: list[str] = Field(default_factory=list)
    all_passed: bool = False
    error_type: AssemblyErrorType = AssemblyErrorType.NONE

    # Edge-case flag: True when the only failures are envelope overshoot
    # within 2x tolerance OR interference volume under 2x tolerance (i.e.
    # the failure is borderline -- a slightly-too-strict envelope or
    # tiny tessellation overlap, not a real geometric defect). When True
    # the Judge node runs even at iteration_count < MIN_RETRY so a human-
    # equivalent sanity check can ACCEPT and terminate instead of forcing
    # a route back to part_builder / assembler. Set by run_assembly_qa;
    # the assembler early-return paths (RECONCILE / PART_MISSING / FATAL)
    # leave this False because their failures are never borderline.
    is_likely_false_positive: bool = False

    # Routing hints for the feedback router.
    needs_remodel_part_ids: list[str] = Field(default_factory=list)
    needs_mate_fix_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Assembly Judge (anti-hallucination pattern reused from MAC's QA Judge)
# ---------------------------------------------------------------------------


class AssemblyJudgeAction(str, Enum):
    ACCEPT = "accept"                      # QA wrong / intent satisfied -> END
    REMATE = "remate"                      # mate design wrong -> Mating Architect
    REPAIR_ASSEMBLY = "repair_assembly"    # fix generated assembly source only
    REMODEL_PARTS = "remodel_parts"        # part geometry wrong
    RECOMPOSE = "recompose"                # decomposition wrong
    HALT = "halt"                          # request infeasible


class AssemblyJudgeDecision(BaseModel):
    action: AssemblyJudgeAction
    confidence: Literal["high", "medium"]
    reason: str = Field(..., description="One-paragraph rationale.")
    evidence: list[str] = Field(
        default_factory=list,
        description="Concrete measured facts, e.g. 'view[1]: lid sits flat on "
                    "base, gap 0.5mm matches spec', 'mate lid_on_base delta "
                    "0.12mm < tol 0.5mm'. Empty evidence on accept/halt is "
                    "auto-downgraded to repair by the code-level gate.",
    )
    remodel_part_ids: list[str] = Field(default_factory=list)
    defensive_correction: bool = Field(
        default=False,
        description="True when reason is a physics/fit correction (add "
                    "clearance, add overlap) that overrides literal intent.",
    )


# ---------------------------------------------------------------------------
# LangGraph state
# ---------------------------------------------------------------------------


class AssemblyGraphState(TypedDict, total=False):
    # Input
    user_request: str
    work_dir: str                      # assembly job directory

    # Stage outputs
    assembly_brief: AssemblyBrief | None
    mating_plan: MatingPlan | None
    part_results: dict[str, PartResult]   # part_id -> result

    assembly_py_path: str
    assembly_step_path: str
    assembly_stl_path: str

    qa_report: AssemblyQAReport | None
    judge_decision: AssemblyJudgeDecision | None

    # Loop control
    iteration_count: int
    max_iterations: int
    decomposer_runs: int
    mating_runs: int
    remodel_part_ids: list[str]
    repair_context: str                # accumulated QA errors for the assembler
    workflow_id: str
    node_history: list[str]
    execution_log: list[str]

    # Set True by the assembler's early-return paths (no mating plan /
    # missing parts / reconcile errors) -- they write a QA report without
    # running the assembly script or the full QA detector. The QA node
    # reads this to skip its own +1 on iteration_count, so budget is
    # only consumed when the real QA detector (mesh / envelope / mate
    # gap / kinematic sweep) actually runs.
    qa_skipped_iter: bool

    # Feedback router -> conditional edge routing target. MUST be in the
    # state schema (otherwise LangGraph silently drops unknown keys, and
    # `route_after_feedback` always sees None -> routes to END -- the loop
    # can never iterate on failure). Cleared by the conditional edge after
    # each read.
    __next__: str
