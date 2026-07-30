"""
Data schemas for the Dual-Engine Multi-Agent CAD System.

This module defines the TypedDict-based LangGraph state and the Pydantic
BaseModel data structures that flow between the four agent nodes:

  1. Spec Planner      → CADBrief
  2. Geometric Architect → ArchitectPlan
  3. Python Coder      → current_python_code (str)
  4. Dual-Engine QA    → QAReport

Engine A (Deterministic) uses cadpy.analysis on .step geometry selectors.
Engine B (Heuristic)   uses check_mesh.py on the exported .stl triangulation.

Constraint: This file defines ONLY the data contracts.  No LangGraph node
implementations, API calls, or control-flow logic live here.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, TypedDict, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


# ============================================================================
#  Enums
# ============================================================================


class ErrorType(str, Enum):
    """Routing signal produced by the Dual-Engine QA node.

    Determines which upstream agent should receive the correction request
    in the next iteration of the LangGraph loop.
    """

    NONE = "none"
    """QA passed; the part is ready for final handoff."""

    DIMENSION = "dimension"
    """Minor parameter / numeric tweak needed (e.g. hole diameter, wall thickness).
    Routes back to the **Python Coder** node."""

    TOPOLOGY = "topology"
    """Structural / geometric approach is wrong (e.g. missing fillet, wrong
    boolean operation, incorrect sketch profile).  Routes back to the
    **Geometric Architect** node."""

    FATAL = "fatal"
    """Unrecoverable error (e.g. disconnected bodies detected by connectivity
    analysis, non-watertight mesh, physically impossible spec).  The loop
    should halt and report to the user."""


class ModelingStepType(str, Enum):
    """Well-known operation types that map to build123d API calls."""

    SKETCH_2D = "sketch_2d"
    """Define a 2D profile on a workplane (points, lines, arcs, splines)."""

    EXTRUDE = "extrude"
    """Linear extrusion of a sketch (additive or cut)."""

    REVOLVE = "revolve"
    """Revolve a sketch around an axis (additive or cut)."""

    FILLET = "fillet"
    """Round a selected edge or edge chain."""

    CHAMFER = "chamfer"
    """Bevel a selected edge or edge chain."""

    HOLE = "hole"
    """Drill / counterbore / countersink a cylindrical hole at a point."""

    SIMPLE_HOLE = "simple_hole"
    """Simple through hole (alias for hole)."""

    COUNTERBORE_HOLE = "counterbore_hole"
    """Counterbore hole (alias for hole with counterbore type)."""

    EXTRUDE_CUT = "extrude_cut"
    """Extrude a sketch as a subtractive cut."""

    CUT = "cut"
    """Generic subtractive operation (alias for boolean_cut)."""

    BOOLEAN_UNION = "boolean_union"
    """Fuse two or more bodies into one."""

    BOOLEAN_CUT = "boolean_cut"
    """Subtract one body (the tool) from another (the target)."""

    BOOLEAN_INTERSECT = "boolean_intersect"
    """Keep only the volume common to two bodies."""

    PATTERN_LINEAR = "pattern_linear"
    """Repeat a feature along a linear direction."""

    PATTERN_CIRCULAR = "pattern_circular"
    """Repeat a feature around an axis."""

    MIRROR = "mirror"
    """Mirror geometry across a plane."""

    SHELL = "shell"
    """Hollow out a solid to a specified wall thickness."""

    DRAFT = "draft"
    """Apply a draft angle to selected faces."""

    RIB = "rib"
    """Add a reinforcing rib / gusset between two faces."""

    REFERENCE = "reference"
    """Define a reference plane, axis, or point for downstream steps."""


class VerificationKind(str, Enum):
    """Kinds of measurable properties the QA engines can check."""

    OVERALL_DIMENSION = "overall_dimension"
    """Bounding-box size along X, Y, or Z."""

    HOLE_DIAMETER = "hole_diameter"
    """Diameter of a specific hole."""

    HOLE_POSITION = "hole_position"
    """Absolute or relative position of a hole center."""

    HOLE_DISTANCE = "hole_distance"
    """Center-to-center distance between two holes."""

    WALL_THICKNESS = "wall_thickness"
    """Local or equivalent wall thickness at a region."""

    EDGE_MARGIN = "edge_margin"
    """Distance from a hole center to the nearest external edge."""

    VOLUME = "volume"
    """Total part volume."""

    MASS = "mass"
    """Total part mass (requires material density)."""

    FILLET_RADIUS = "fillet_radius"
    """Radius of a specific fillet / round."""

    SURFACE_AREA = "surface_area"
    """Total or regional surface area."""

    ANGLE = "angle"
    """Angle between two faces, edges, or axes."""

    FLATNESS = "flatness"
    """Deviation of a face from a perfect plane."""

    PARALLELISM = "parallelism"
    """Angular deviation between two nominally parallel faces."""

    PERPENDICULARITY = "perpendicularity"
    """Angular deviation between two nominally perpendicular faces."""

    CONCENTRICITY = "concentricity"
    """Center offset between two nominally coaxial cylindrical features."""

    HOLE_DEPTH = "hole_depth"
    """Depth of a specific hole (blind-hole depth or counterbore depth)."""

    SLOT_DIMENSION = "slot_dimension"
    """Width, height, or depth of a slot / pocket feature."""

    COUNTERBORE_DIAMETER = "counterbore_diameter"
    """Diameter of a counterbore feature."""

    COUNTERBORE_DEPTH = "counterbore_depth"
    """Depth of a counterbore feature."""

    BOSS_DIAMETER = "boss_diameter"
    """Diameter of a boss or stepped feature."""

    BOSS_DEPTH = "boss_depth"
    """Depth/height of a boss or stepped feature."""

    THREADED_HOLE = "threaded_hole"
    """Presence and specification of a threaded hole (e.g. M4x0.7)."""

    WATER_TIGHTNESS = "water_tightness"
    """Whether the mesh is a closed, manifold solid."""

    SINGLE_BODY = "single_body"
    """Whether the mesh is a single connected component (no floating fragments)."""


# ============================================================================
#  Verification Target
# ============================================================================


class VerificationTarget(BaseModel):
    """A single measurable requirement the QA engines must check.

    Each target defines *what* to measure, *where* to measure it, and the
    acceptable range.  The QA engines produce a ``VerificationResult`` for
    every target in the CADBrief.
    """

    id: str = Field(
        ...,
        description="Unique identifier within the brief (e.g. 'hole-1-dia').",
        examples=["hole-1-dia", "overall-x", "wall-thickness-base"],
    )
    kind: VerificationKind = Field(
        ...,
        description="The measurable property to check.",
    )
    description: str = Field(
        ...,
        description="Human-readable description of what is being verified.",
        examples=["M4 clearance hole diameter on the base flange"],
    )

    # -- Target value (nominal) --
    nominal: float | None = Field(
        None,
        description="Nominal / design-intent value in mm (or mm² / mm³).",
    )

    # -- Tolerances --
    tolerance_upper: float = Field(
        0.1,
        description="Allowed positive deviation from nominal (mm).",
    )
    tolerance_lower: float = Field(
        0.1,
        description="Allowed negative deviation from nominal (mm).",
    )

    # -- Feature selectors (how to locate the measurement site) --
    face_selector_expression: str | None = Field(
        None,
        description=(
            "Selector expression that identifies the face(s) to measure on "
            "the .step topology.  Interpreted by cadpy.analysis / the "
            "selector index (e.g. 'Face:5', 'Cylinder:all')."
        ),
    )
    edge_selector_expression: str | None = Field(
        None,
        description="Selector expression for edge-based measurements (e.g. fillet radius).",
    )
    reference_feature_id: str | None = Field(
        None,
        description=(
            "ID of another VerificationTarget used as a datum / reference "
            "for relative measurements (e.g. hole-distance references two "
            "hole targets)."
        ),
    )
    measurement_axis: Literal["x", "y", "z"] | None = Field(
        None,
        description="Which axis the measurement should be taken along.",
    )

    # -- Metadata --
    critical: bool = Field(
        False,
        description="If True, a failure here triggers a FATAL error_type and halts the loop.",
    )
    notes: str | None = Field(
        None,
        description="Free-form engineering notes for the QA agents.",
    )


# ============================================================================
#  CAD Brief (Spec Planner output)
# ============================================================================


class CADBrief(BaseModel):
    """Formal CAD specification produced by the Spec Planner agent.

    This is the single-source-of-truth design document that all downstream
    agents consume.  It standardises the user's natural-language request into
    machine-readable constraints, units, and verification targets.

    The Spec Planner is responsible for:
      * Resolving ambiguities in the user's request
      * Converting colloquial measurements to mm
      * Setting a sensible origin and bounding-box envelope
      * Defining concrete verification targets the QA engines can measure
    """

    # -- Identity --
    part_name: str = Field(
        ...,
        description="Canonical part name (e.g. 'L-bracket-50x40x4-M4').",
        examples=["l-bracket-50x40x4-M4", "tube-clamp-12mm"],
    )
    part_category: str = Field(
        ...,
        description="High-level part category inferred from the request.",
        examples=["L-bracket", "U-bracket", "flat-plate", "tube-clamp", "custom"],
    )

    # -- Units & coordinate system --
    length_unit: Literal["mm", "cm", "m", "in"] = Field(
        "mm",
        description="All linear dimensions in the brief use this unit.",
    )
    origin_convention: str = Field(
        "centroid_on_base_plane",
        description=(
            "How the global origin should be placed.  Common conventions: "
            "'centroid_on_base_plane' (origin at XY centre of the bottom face, "
            "Z=0 on the bottom), 'corner_min' (origin at minimum X/Y/Z corner), "
            "'centroid_3d' (origin at the volumetric centroid)."
        ),
        examples=["centroid_on_base_plane", "corner_min", "centroid_3d"],
    )
    primary_workplane: Literal["XY", "XZ", "YZ"] = Field(
        "XY",
        description="The principal sketch plane for the base feature.",
    )

    # -- Envelope --
    max_extent_x_mm: float | None = Field(
        None,
        description="Maximum allowed bounding-box width (X).  None = unbounded.",
    )
    max_extent_y_mm: float | None = Field(
        None,
        description="Maximum allowed bounding-box depth (Y).  None = unbounded.",
    )
    max_extent_z_mm: float | None = Field(
        None,
        description="Maximum allowed bounding-box height (Z).  None = unbounded.",
    )
    target_volume_mm3: float | None = Field(
        None,
        description="Target part volume (informational, not a hard constraint).",
    )

    # -- Material --
    material: str | None = Field(
        None,
        description="Intended material (e.g. 'PLA', 'ABS', 'Al6061', 'steel-q235').",
    )
    material_density_g_cm3: float | None = Field(
        None,
        description="Material density for mass calculations.",
    )

    # -- Key parameters extracted from user request --
    key_parameters: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Named numeric parameters extracted from the user request. "
            "Values can be floats, ints, or nested lists for positions "
            "(e.g. {'base_thickness': 4.0, 'hole_positions': [[45.0, 30.0], ...]})."
        ),
    )

    # -- Verification targets --
    verification_targets: list[VerificationTarget] = Field(
        default_factory=list,
        description="Concrete, measurable checks the QA engines must perform.",
    )

    # -- Functional requirements (free-form) --
    functional_requirements: list[str] = Field(
        default_factory=list,
        description=(
            "Non-geometric requirements: load-bearing direction, environmental "
            "constraints, mounting method, expected lifetime, etc."
        ),
        examples=[
            "Must support 5 kg cantilever load in -Z direction",
            "Four M4 bolts mount the base to a flat aluminum extrusion",
            "Operating temperature -20°C to 80°C",
        ],
    )

    # -- Manufacturing intent --
    manufacturing_method: Literal["3d_print_fdm", "3d_print_sls", "cnc_3axis", "cnc_5axis", "injection_mold", "sheet_metal", "unspecified"] = Field(
        "unspecified",
        description="Primary manufacturing method.  Influences DFM checks in QA.",
    )

    # -- Special features (non-trivial geometric constraints) --
    special_features: list[str] = Field(
        default_factory=list,
        description=(
            "Non-trivial geometric constraints extracted from the user request "
            "that downstream agents (Architect, Aider) MUST verify.  Each item "
            "is an imperative sentence describing a constraint that is NOT "
            "captured by verification_targets (which only checks overall "
            "dimension / single_body / water_tightness).  Examples: symmetry "
            "requirements, feature placement rules, avoidance rules, special "
            "geometric shapes (semicircular top), feature direction constraints."
        ),
        examples=[
            "All features MUST be symmetric about the XZ plane (Y coordinates mirror about Y=0)",
            "Reinforcing ribs MUST attach to lug outer faces at Y=±LUG_OUTER_FACE_Y, not in X direction",
            "Lightening cutouts MUST avoid mounting holes — vertices at least 5mm away from (±45, ±20)",
            "Lug top MUST be semicircular arc (R=LUG_SEMICIRCLE_RADIUS), NOT triangular peak",
            "Clevis through-hole MUST penetrate along Y axis, not Z",
        ],
    )

    # -- Metadata --
    user_request_raw: str = Field(
        ...,
        description="The original user request text, preserved for traceability.",
    )
    spec_version: int = Field(
        1,
        description="Monotonic version of this brief (incremented on each Architect-route revision).",
    )


# ============================================================================
#  Architect Plan (Geometric Architect output)
# ============================================================================


class Point2D(BaseModel):
    """A 2D point in sketch space (mm).

    Accepts both ``{"x": 1.0, "y": 2.0}`` and ``[1.0, 2.0]`` (list form).
    """
    x: float
    y: float

    @model_validator(mode="before")
    @classmethod
    def _coerce_list(cls, v: object) -> object:
        if isinstance(v, (list, tuple)) and len(v) == 2:
            return {"x": float(v[0]), "y": float(v[1])}
        return v


class Point3D(BaseModel):
    """A 3D point in model space (mm).

    Accepts both ``{"x": 1, "y": 2, "z": 3}`` and ``[1, 2, 3]`` (list form).
    """
    x: float
    y: float
    z: float

    @model_validator(mode="before")
    @classmethod
    def _coerce_list(cls, v: object) -> object:
        if isinstance(v, (list, tuple)) and len(v) == 3:
            return {"x": float(v[0]), "y": float(v[1]), "z": float(v[2])}
        return v


class SketchEntity(BaseModel):
    """One primitive in a 2D sketch profile."""

    entity_type: Literal["line", "arc", "circle", "rectangle", "polygon", "slot", "spline", "text"] = Field(
        ...,
        description="Geometry primitive type.",
    )
    label: str | None = Field(
        None,
        description="Optional human-readable label (e.g. 'base-outline').",
    )

    # Line
    start: Point2D | None = Field(None, description="Line start point.")
    end: Point2D | None = Field(None, description="Line end point.")

    # Arc / Circle
    center: Point2D | None = Field(None, description="Arc or circle centre.")
    radius: float | None = Field(None, description="Arc or circle radius (mm).")
    start_angle_deg: float | None = Field(None, description="Arc start angle in degrees.")
    end_angle_deg: float | None = Field(None, description="Arc end angle in degrees.")

    # Rectangle / Slot
    width: float | None = Field(None, description="Rectangle / slot width (mm).")
    height: float | None = Field(None, description="Rectangle / slot height (mm).")
    corner_radius: float | None = Field(None, description="Corner fillet radius for rounded rectangles (mm).")

    # Polygon
    num_sides: int | None = Field(None, description="Number of sides for a regular polygon.")
    circumscribed_radius: float | None = Field(None, description="Circumscribed radius for a regular polygon (mm).")

    # Spline
    control_points: list[Point2D] | None = Field(None, description="Spline control points.")

    # Text
    text_string: str | None = Field(None, description="Text string for engraved / embossed text.")
    font_size: float | None = Field(None, description="Font size for text (mm).")


class Sketch(BaseModel):
    """A complete 2D sketch on a named workplane."""

    sketch_id: str = Field(
        ...,
        description="Unique identifier for this sketch within the plan.",
        examples=["base-profile", "side-flange", "mounting-tab"],
    )
    workplane: Literal["XY", "XZ", "YZ"] | str = Field(
        "XY",
        description=(
            "Build plane for the sketch.  Standard planes 'XY', 'XZ', 'YZ', "
            "or a reference to a previously defined face / offset plane "
            "(e.g. 'face:F3', 'offset:base-profile+5mm')."
        ),
    )
    workplane_offset_mm: float = Field(
        0.0,
        description="Offset distance from the nominal workplane (mm).",
    )
    entities: list[SketchEntity] = Field(
        default_factory=list,
        description="Ordered list of sketch primitives.",
    )
    constraints: list[str] = Field(
        default_factory=list,
        description=(
            "Geometric constraints applied to the sketch.  Free-form strings "
            "the Coder interprets: 'coincident(P1,P2)', 'horizontal(L1)', "
            "'vertical(L2)', 'parallel(L1,L2)', 'tangent(A1,L1)', etc."
        ),
    )
    fully_constrained: bool = Field(
        False,
        description="Whether the sketch is fully constrained (recommended for production).",
    )
    notes: str | None = Field(
        None,
        description="Additional guidance for the Coder (e.g. 'use SlotCenterLine').",
    )


class ModelingStep(BaseModel):
    """One step in the sequential build123d modeling plan.

    Each step describes a single build123d operation that the Python Coder
    translates into a method call on a ``Workplane``, ``Solid``, or ``Part``
    object.  Steps are ordered; each operates on the accumulated geometry
    from previous steps.
    """

    step_id: str = Field(
        ...,
        description="Unique identifier within the plan (e.g. 'step-01-extrude-base').",
        examples=["step-01-base-sketch", "step-02-extrude-base", "step-05-fillet-edges"],
    )
    step_type: ModelingStepType = Field(
        ...,
        description="The build123d operation to perform.",
    )
    label: str = Field(
        ...,
        description="Short human-readable label shown in logs / reports.",
        examples=["Extrude base plate 4mm", "Fillet all vertical edges R2"],
    )

    # -- Sketch reference (for extrude / revolve) --
    sketch_id: str | None = Field(
        None,
        description="Reference to a Sketch.sketch_id defined earlier in the plan.",
    )

    # -- Operation parameters --
    distance_mm: float | None = Field(
        None,
        description="Extrusion distance / cut depth (mm).",
    )
    direction: Literal["positive", "negative", "symmetric", "midplane", "both"] | None = Field(
        None,
        description="Extrusion direction relative to the sketch normal.",
    )
    taper_angle_deg: float | None = Field(
        None,
        description="Draft / taper angle for extrusion (degrees, 0 = vertical walls).",
    )

    # -- Revolve parameters --
    revolve_angle_deg: float | None = Field(
        None,
        description="Revolution angle (360 = full revolution).",
    )
    revolve_axis: Literal["x", "y", "z"] | None = Field(
        None,
        description="Axis of revolution.",
    )
    revolve_axis_point: Point3D | None = Field(
        None,
        description="Point the revolution axis passes through.",
    )

    # -- Fillet / Chamfer parameters --
    radius_mm: float | None = Field(
        None,
        description="Fillet radius (mm).",
    )
    chamfer_distance_mm: float | None = Field(
        None,
        description="Chamfer distance (mm, equal-leg).",
    )
    chamfer_distance1_mm: float | None = Field(
        None,
        description="Chamfer distance 1 (mm, unequal-leg).",
    )
    chamfer_distance2_mm: float | None = Field(
        None,
        description="Chamfer distance 2 (mm, unequal-leg).",
    )
    edge_selector: str | None = Field(
        None,
        description=(
            "Which edge(s) to fillet/chamfer.  Either a selector expression "
            "(e.g. 'Edge:1', 'Edges:all-vertical') or a description the Coder "
            "must translate into a selector (e.g. 'all edges of the base plate')."
        ),
    )

    # -- Hole parameters --
    hole_diameter_mm: float | None = Field(
        None,
        description="Finished hole diameter (mm).",
    )
    hole_depth_mm: float | None = Field(
        None,
        description="Hole depth (mm).  None or > thickness = through-all.",
    )
    hole_position: Point3D | None = Field(
        None,
        description=(
            "3D world-space centre point of the hole (mm). "
            "Accepts Point3D, Point2D (z=0 assumed), [x,y], or [x,y,z]."
        ),
    )
    hole_type: Literal["simple", "counterbore", "countersink", "tapped", "counterbored"] | None = Field(
        "simple",
        description="Type of hole.",
    )
    counterbore_diameter_mm: float | None = Field(
        None,
        description="Counterbore diameter (mm).",
    )
    counterbore_depth_mm: float | None = Field(
        None,
        description="Counterbore depth (mm).",
    )
    countersink_angle_deg: float | None = Field(
        None,
        description="Countersink included angle (degrees, typically 82 or 90).",
    )

    # -- Boolean parameters --
    target_step_id: str | None = Field(
        None,
        description="Reference to another step_id whose geometry is the target of a boolean operation.",
    )
    tool_step_id: str | None = Field(
        None,
        description="Reference to another step_id whose geometry is the tool in a boolean cut.",
    )

    # -- Pattern parameters --
    pattern_count: int | None = Field(
        None,
        description="Number of instances in a pattern (including the original).",
    )
    pattern_spacing_mm: float | None = Field(
        None,
        description="Spacing between pattern instances (mm, linear patterns).",
    )
    pattern_axis: Literal["x", "y", "z"] | None = Field(
        None,
        description="Axis for linear displacement or circular rotation.",
    )
    pattern_total_angle_deg: float | None = Field(
        None,
        description="Total angular span for a circular pattern (degrees, 360 = full circle).",
    )
    feature_step_ids: list[str] | None = Field(
        None,
        description="Step IDs of the features to be patterned.",
    )

    # -- Mirror parameters --
    mirror_plane: Literal["XY", "XZ", "YZ"] | None = Field(
        None,
        description="Mirror plane (standard or offset by a reference).",
    )
    mirror_plane_offset_mm: float | None = Field(
        None,
        description="Offset from the nominal mirror plane (mm).",
    )

    # -- Shell parameters --
    shell_thickness_mm: float | None = Field(
        None,
        description="Target wall thickness for a shell operation (mm).",
    )
    shell_open_faces: list[str] | None = Field(
        None,
        description="Face selector expressions for faces to remove in a shell operation.",
    )

    # -- Draft parameters --
    draft_angle_deg: float | None = Field(
        None,
        description="Draft angle in degrees.",
    )
    draft_neutral_plane: Literal["XY", "XZ", "YZ"] | str | None = Field(
        None,
        description="Neutral plane for the draft (faces are rotated about their intersection with this plane).",
    )
    draft_face_selectors: list[str] | None = Field(
        None,
        description="Face selector expressions for faces to draft.",
    )

    # -- Rib parameters --
    rib_thickness_mm: float | None = Field(
        None,
        description="Rib thickness (mm).",
    )
    rib_height_mm: float | None = Field(
        None,
        description="Rib height (mm).",
    )
    rib_face_selectors: list[str] | None = Field(
        None,
        description="Face selector expressions for faces to attach the rib to.",
    )

    # -- Reference geometry --
    reference_type: Literal["plane", "axis", "point"] | None = Field(
        None,
        description="Type of reference geometry to create (for REFERENCE steps).",
    )
    reference_definition: str | None = Field(
        None,
        description=(
            "How the reference is defined (e.g. 'offset:XY:10mm', "
            "'through:point1:point2', 'normal:face:F3:point:P1')."
        ),
    )

    # -- Metadata --
    depends_on: list[str] = Field(
        default_factory=list,
        description="Step IDs that must complete before this step can execute.",
    )
    notes: str | None = Field(
        None,
        description="Engineering guidance for the Coder (e.g. 'use Blind=through-all').",
    )

    @field_validator("hole_position", mode="before")
    @classmethod
    def _coerce_hole_position_3d(cls, v: object) -> object:
        """Accept Point2D, [x,y], or [x,y,z] → Point3D."""
        if v is None or isinstance(v, Point3D):
            return v
        if isinstance(v, Point2D):
            return Point3D(x=v.x, y=v.y, z=0.0)
        if isinstance(v, (list, tuple)):
            if len(v) == 2:
                return Point3D(x=float(v[0]), y=float(v[1]), z=0.0)
            if len(v) == 3:
                return Point3D(x=float(v[0]), y=float(v[1]), z=float(v[2]))
        return v


class ArchitectPlan(BaseModel):
    """Structured geometric modeling plan produced by the Geometric Architect.

    This is NOT a rigid CSG tree — it is a sequential, imperative recipe
    that the Python Coder translates directly into build123d API calls.

    Every step references named sketches and intermediate geometry IDs so
    the Coder can trace through the plan deterministically.
    """

    plan_id: str = Field(
        ...,
        description="Unique plan identifier, keyed to the CADBrief it was derived from.",
        examples=["plan-l-bracket-50x40x4-v1"],
    )
    cad_brief_id: str = Field(
        ...,
        description="Corresponding CADBrief.part_name this plan addresses.",
    )

    # -- Sketches --
    sketches: list[Sketch] = Field(
        default_factory=list,
        description="All 2D sketches defined in this plan (referenced by ModelingStep.sketch_id).",
    )

    # -- Modeling steps --
    steps: list[ModelingStep] = Field(
        default_factory=list,
        description="Ordered sequence of build123d operations.",
    )

    # -- Plan-level metadata --
    estimated_volume_mm3: float | None = Field(
        None,
        description="Rough volume estimate for sanity-checking before generation.",
    )
    key_dimensions: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Named dimensions the Coder should expose as variables at the top "
            "of the generated script. Values can be floats, ints, or lists "
            "(e.g. {'base_thickness': 4.0, 'hole_positions_x': [45.0, -45.0]})."
        ),
    )
    selector_map: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Maps VerificationTarget.id to a selector DSL expression for precise "
            "topology-based measurement. Keys are target IDs from the CADBrief "
            "(e.g. 'lug-extent-x', 'clevis-hole-dia'). Values are selector "
            "expressions like 'face:surface=cylinder,axis=z,sort=area:desc'. "
            "Engine A uses these to find and measure specific faces/edges on "
            "the STEP topology instead of fuzzy string matching."
        ),
    )
    tolerances_hint: str | None = Field(
        None,
        description="Guidance on build123d tolerance / precision settings.",
    )
    expected_step_count: int | None = Field(
        None,
        description="Expected number of modeling steps (for plan validation).",
    )

    # -- Traceability --
    plan_version: int = Field(
        1,
        description="Monotonic version (incremented on each revision from QA feedback).",
    )
    revision_history: list[Any] = Field(
        default_factory=list,
        description="Brief log of what changed in each revision (e.g. 'v2: increased fillet R1→R2 on edge chain E3').",
    )


# ============================================================================
#  QA Result (single check)
# ============================================================================


class VerificationResult(BaseModel):
    """The outcome of checking a single VerificationTarget."""

    target_id: str = Field(
        ...,
        description="References VerificationTarget.id.",
    )
    passed: bool = Field(
        ...,
        description="True if the measured value falls within the target's tolerance band.",
    )
    measured_value: float | None = Field(
        None,
        description="The actual measured value (mm, mm², or mm³ depending on VerificationKind).",
    )
    deviation: float | None = Field(
        None,
        description="Difference from nominal (measured - nominal).",
    )
    engine: Literal["cadpy_analysis", "check_mesh", "both"] = Field(
        ...,
        description="Which QA engine produced this result.",
    )

    # -- Noise / reliability flags (from check_mesh's mesh-resolution analysis) --
    is_mesh_noise: bool = Field(
        False,
        description=(
            "True if the measured deviation is within the mesh polygonisation "
            "noise floor (check_mesh's _deviation_is_noise).  When True, the "
            "failure is NOT a real design error and does not require code changes."
        ),
    )
    mesh_resolution_mm: float | None = Field(
        None,
        description="Estimated mesh edge length (resolution) at the measurement site.",
    )
    noise_explanation: str | None = Field(
        None,
        description="Human-readable explanation when the deviation is mesh noise.",
    )

    # -- Severity --
    severity: Literal["info", "warning", "error", "fatal"] = Field(
        "info",
        description="How severe the deviation is.",
    )

    # -- Raw data (for debugging / traceability) --
    raw_measurements: dict[str, object] = Field(
        default_factory=dict,
        description="Engine-specific raw measurement data for traceability.",
    )


# ============================================================================
#  QA Report (Dual-Engine QA node output)
# ============================================================================


class EngineReport(BaseModel):
    """The QA output from a single engine (cadpy.analysis or check_mesh)."""

    engine_name: Literal["cadpy_analysis", "check_mesh"] = Field(
        ...,
        description="Which engine produced this report.",
    )
    results: list[VerificationResult] = Field(
        default_factory=list,
        description="Per-target verification outcomes from this engine.",
    )
    summary: str = Field(
        "",
        description="One-paragraph plain-language summary of findings from this engine.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal warnings (e.g. 'mesh resolution is coarse, measurements may be ±0.35mm').",
    )
    errors: list[str] = Field(
        default_factory=list,
        description="Fatal or hard errors encountered during QA execution.",
    )
    raw_json: dict[str, object] | None = Field(
        None,
        description="Full raw output from the engine (for debugging).",
    )


class QAReport(BaseModel):
    """Merged Quality Assurance report from the Dual-Engine QA node.

    Produced by running:
      * Engine A (Deterministic): cadpy.analysis on the .step topology manifest
      * Engine B (Heuristic):   check_mesh.py on the exported .stl triangulation

    The two engine reports are merged here, and a consolidated routing decision
    (error_type) is derived so the LangGraph supervisor knows which upstream
    node to loop back to.
    """

    cad_brief_id: str = Field(
        ...,
        description="Which CADBrief this QA report evaluates.",
    )

    # -- Per-engine reports --
    engine_a: EngineReport = Field(
        ...,
        description="Deterministic QA results from cadpy.analysis on the .step topology.",
    )
    engine_b: EngineReport = Field(
        ...,
        description="Heuristic / physical QA results from check_mesh.py on the .stl mesh.",
    )

    # -- Consolidated results --
    all_passed: bool = Field(
        ...,
        description="True if every VerificationResult across both engines passed (ignoring mesh-noise-only failures).",
    )
    passed_count: int = Field(
        0,
        description="Number of verification targets that passed.",
    )
    failed_count: int = Field(
        0,
        description="Number of verification targets that failed (excluding mesh-noise-only failures).",
    )
    noise_only_fail_count: int = Field(
        0,
        description="Number of failures attributable solely to mesh polygonisation noise.",
    )

    # -- Routing decision --
    error_type: ErrorType = Field(
        ErrorType.NONE,
        description=(
            "Consolidated routing signal for the LangGraph supervisor: "
            "NONE → part is done; "
            "DIMENSION → send back to Python Coder for numeric tweaks; "
            "TOPOLOGY → send back to Geometric Architect for structural redesign; "
            "FATAL → halt and report to user."
        ),
    )
    error_details: list[str] = Field(
        default_factory=list,
        description="Specific error descriptions to guide the upstream agent's fix.",
    )

    # -- Improvement suggestions (from check_mesh's analysis) --
    improvement_suggestions: list[str] = Field(
        default_factory=list,
        description="Actionable improvement suggestions from the heuristic engine.",
    )

    # -- Structural warnings --
    connectivity_warning: str | None = Field(
        None,
        description=(
            "Non-null if check_mesh detected a connectivity / multi-body issue. "
            "A disconnected body is a FATAL error_type."
        ),
    )
    strength_score: int | None = Field(
        None,
        description="0-100 structural strength score from check_mesh's _assess_strength.",
    )
    manufacturability_warnings: list[str] = Field(
        default_factory=list,
        description="DFM / printability warnings from the heuristic engine.",
    )

    # -- Topology diff (from cadpy.analysis's selector_manifest_diff) --
    topology_changed: bool = Field(
        False,
        description="True if the STEP topology changed since the last iteration.",
    )
    topology_delta: dict[str, object] = Field(
        default_factory=dict,
        description="Topology diff details (face/edge/vertex count deltas, bbox change, etc.).",
    )

    # -- Metadata --
    iteration: int = Field(
        0,
        description="Which QA iteration this report corresponds to.",
    )


# ============================================================================
#  LangGraph State
# ============================================================================


class GraphState(TypedDict, total=False):
    """The shared state dictionary that flows through the LangGraph pipeline.

    Each agent node reads from and writes to this state.  LangGraph's
    reducer semantics (append for lists, merge for dicts) are configured
    in the graph definition, not here.

    Fields are total=False so nodes can return partial state updates.
    """

    # -- Input --
    user_request: str
    """Raw natural-language CAD request from the user (e.g. 'Design an L-bracket
    with a 50mm base, 40mm height, 4mm thickness, and M4 clearance holes')."""

    # -- Spec Planner output --
    cad_brief: CADBrief | None
    """Formal specification produced by the Spec Planner agent."""

    # -- Geometric Architect output --
    architect_plan: ArchitectPlan | None
    """Step-by-step build123d modeling plan produced by the Geometric Architect."""

    # -- Python Coder output --
    current_python_code: str | None
    """The latest build123d Python script generated by the Python Coder agent.
    This script must define a ``gen_step()`` callable that returns a build123d
    Shape, compatible with cadpy.generation's script-runner machinery."""

    current_python_code_path: str | None
    """Absolute filesystem path to the Python script file.
    Used by the autonomous skill loop to know which file to edit with Aider."""

    # -- Generated artifact paths --
    current_step_path: str | None
    """Absolute filesystem path to the most recently generated .step file."""

    current_stl_path: str | None
    """Absolute filesystem path to the most recently generated .stl file."""

    # -- Dual-Engine QA output --
    qa_report: QAReport | None
    """Merged QA report from the Dual-Engine QA node.  Its error_type field
    determines the next routing decision."""

    # -- Loop control --
    iteration_count: int
    """How many times the fix loop has executed (0-based, incremented before
    each QA pass).  Used for the LangGraph conditional edge."""

    error_type: ErrorType | None
    """Routing signal: DIMENSION → Coder, TOPOLOGY → Architect, NONE → END,
    FATAL → END (with error).  Set by the QA node; consumed by the conditional
    edge function."""

    max_iterations: int
    """Hard cap on the number of fix loops before giving up.  Typically 5-8."""

    retry_mode: str | None
    """When set to \"force_edit\", the Architect must output only changed
    numeric fields (JSON patch) rather than a full plan.  Set by QA when
    the dimensions hash matches the previous iteration."""

    stall_count: int
    """How many consecutive times the Architect returned unchanged
    key_dimensions.  Resets to 0 when dimensions actually change.
    If stall_count >= 2, the pipeline escalates to FATAL."""

    force_refresh: bool
    """If True, bypass all caches and regenerate from scratch."""

    workflow_id: str
    """Identifier for the active workflow: ``"original"`` (full pipeline) or
    ``"aider"`` (Aider-first pipeline).  Used by the autonomous skill loop to
    name output files and enable workflow-specific logic."""

    # -- Diagnostics / traceability --
    node_history: list[str]
    """Ordered list of node names visited (for debugging / logging)."""

    execution_log: list[str]
    """Human-readable log lines from each node (appended by each agent)."""

    # -- Topology diff (prevents infinite retry loops) --
    previous_manifest: dict | None
    """Topology fingerprint from the previous QA iteration.
    If the current STEP produces an identical manifest, the system detects
    a stalled retry loop and escalates to FATAL instead of retrying again."""

    previous_dimensions_hash: str | None
    """MD5 hash of the ArchitectPlan's key_dimensions from the previous
    iteration.  If the hash matches and QA still reports errors, the
    Architect did not actually change the plan — escalate to FATAL."""

    # -- Final output (set on successful completion) --
    # NOTE: final_step_path, final_stl_path, and final_report_markdown were
    # removed — no node ever writes them.  Use current_step_path / current_stl_path
    # from the autonomous skill loop's return value instead.
