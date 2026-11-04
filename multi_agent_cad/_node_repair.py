"""Aider generation/repair, repair prompts and direct-API fallback.

Internal implementation module; callers continue to use ``multi_agent_cad.nodes``.
Shared settings and replaceable helpers are resolved through that facade at call
time, preserving per-job overrides and existing monkeypatch hooks. No mutable
configuration or cache state is copied into this module.
"""
from __future__ import annotations

import json
import os
import re
import textwrap
from pathlib import Path

from multi_agent_cad import nodes as _nodes


def _join_aider_summarizer(coder) -> None:
    """Join aider's background chat-summarizer thread after coder.run().

    When the conversation outgrows the model's chat-history budget, aider
    spawns a NON-daemon thread that compresses the history via an LLM call.
    MAC's pipeline finishes its last coder.run() without building another
    prompt, so aider's own summarize_end() (which joins the thread) never
    runs. At interpreter exit Python runs the concurrent.futures atexit
    handler -- shutting litellm's global executor down -- BEFORE joining
    non-daemon threads, so an in-flight summary call dies with "cannot
    schedule new futures after shutdown": a full-conversation API call is
    billed but discarded, and the noise it prints lands at the tail of
    subprocess stdout where _tail_error looks for the REAL failure reason
    (on the 2026-09-08 gantry run it masked the actual
    "AUTONOMOUS LOOP -- MAX RETRIES EXHAUSTED" fatal).
    """
    try:
        coder.summarize_end()
    except Exception:
        pass  # summary bookkeeping must never fail the pipeline


def _build_autonomous_repair_prompt(
    *,
    user_request: str,
    error_details: list[str],
    feature_measurements: dict | None = None,
    special_features: list[str] | None = None,
) -> str:
    """Construct the strict autonomous repair prompt for the Aider closed-loop.

    This is the enhanced version used by ``node_autonomous_skill_loop``.
    It includes four iron-clad rules that the model MUST follow, plus the
    original user request to prevent goal drift across multiple iterations.

    Parameters
    ----------
    feature_measurements : dict | None
        White-box instrumentation measurements from temp_measurements_{iteration}.json.
        These are precise measurements of individual features BEFORE boolean merge.
    special_features : list[str] | None
        Non-trivial geometric constraints extracted from CADBrief by the Spec
        Planner (symmetry, feature placement, avoidance rules, etc.).  Shown
        right after user_request so Aider verifies each item explicitly.
    """
    req_section = ""
    if user_request:
        req_section = textwrap.dedent(f"""\
        ## Original Design Requirements (Original Design Requirements — GROUND TRUTH)

        The following is the user's original request. In all iteration repairs, you must strictly satisfy these requirements,
        and must not deviate from or simplify the original design intent due to error repair.

        {user_request}

        """)

    # Special features section — non-trivial geometric constraints extracted
    # by Spec Planner.  Shown right after user_request so Aider sees them as
    # a verification checklist, not lost in the long user_request text.
    special_features_section = ""
    if special_features:
        features_lines = []
        for i, feat in enumerate(special_features, 1):
            features_lines.append(f"  [{i}] {feat}")
        special_features_section = (
            "## 🔍 Special Design Constraints (Special Features — MUST verify each item)\n\n"
            "The following constraints are extracted separately from the user's requirements by the Spec Planner, and are the non-routine geometric constraints of this part.\n"
            "**The QA system only detects overall bounding box, connectivity, and watertightness, not these constraints.**\n"
            "**You are responsible for checking each constraint one by one to see if it is satisfied in the current code.** Any violation must be fixed.\n\n"
            + "\n".join(features_lines)
            + "\n\n**Self-check**: After repairing, check each constraint one by one to see if it is satisfied. For example:\n"
            "- Symmetry constraint → Check each sketch's control_points, Y coordinate range -h..h\n"
            "- Rib position constraint → Check the rib's Pos coordinate, whether Y is ±LUG_OUTER_FACE_Y\n"
            "- Avoidance rule → Check whether cutout vertices are at least 5mm away from mounting holes\n\n"
        )

    # Add feature measurements section if available
    measurements_section = ""
    if feature_measurements:
        measurements_section = "## 📏 Feature Measurements (Feature Measurements — White-box Instrumentation)\n\n"
        measurements_section += "The following is precise bounding box measurement data for each independent feature **before** boolean merge.\n"
        measurements_section += "**The QA system only detects overall bounding box, connectivity, and watertightness, not the dimensions of individual features.**\n"
        measurements_section += "**You are responsible for actively comparing this data with the dimensions in the user request, discovering and fixing feature-level deviations.**\n\n"
        measurements_section += "```json\n"
        measurements_section += json.dumps(feature_measurements, indent=2, ensure_ascii=False)
        measurements_section += "\n```\n\n"
        measurements_section += "**Usage**:\n"
        measurements_section += "- Compare each feature dimension in the user request (hole diameter, plate thickness, boss height, etc.) with the corresponding entry in the white-box data\n"
        measurements_section += "- Compare `size_x/y/z` with the nominal value in the user request, deviations exceeding 0.5mm need to be fixed\n"
        measurements_section += "- Check `min_x/y/z` and `max_x/y/z` to confirm the feature position is correct\n"
        measurements_section += "- This data is collected from independent features before merging, and is more precise than reverse inference from the merged STEP\n\n"

    errors_section = "No specific QA errors reported."
    if error_details:
        errors_section = "The following QA detection found errors that need to be fixed:\n\n"
        for i, err in enumerate(error_details, 1):
            errors_section += f"  [{i}] {err}\n"

    return textwrap.dedent(f"""\
    {req_section}\
    {special_features_section}\
    {measurements_section}\
    ## Task Instructions

    You are repairing a build123d CAD script. Your primary responsibility is to implement the user's original design intent,
    while making **minimal, precise modifications** to the code.

    **Important**: Each iteration only fixes **1-2 most critical errors**, do not attempt to fix all errors at once.

    ### Judge has already decided REPAIR — execute, don't re-evaluate

    The QA Judge agent has already evaluated the QA report and selected
    `REPAIR` (not ACCEPT, not HALT). This means:
    - The QA failures are **real** — not false positives (Judge would have ACCEPTed those)
    - The request is **not mathematically self-contradictory** — Judge would have HALTed those
    - Your job is to **execute the repair**, not re-decide whether to repair

    The Judge's `reason` and `evidence` are prepended to `error_details` as
    `JUDGE context (proceeding to repair): <reason> | Evidence: <evidence...>`.
    If the reason starts with `DEFENSIVE CORRECTION:`, this is a physical-soundness
    fix (e.g. add overlap, add fillet, increase thickness) that preserves user
    intent — apply it as a defensive override. Otherwise, treat the reason as
    Judge's prioritized guidance for which errors to fix first.

    ### Information Priority (from high to low)

    1. **Judge's reason + evidence** (in error_details, prefixed with
       `JUDGE context`) — Judge has done the high-level triage; trust its priority
    2. **QA Report** (overall bounding box, connectivity, watertightness) — core
       reference for topology and overall dimensions
    3. **White-box Feature Measurement Data** (📏 Feature Measurements section) —
       core reference for feature-level dimensions
    4. **User Quantitative Data** (specific dimension values in user_request) —
       satisfy as much as possible without violating the first three

    **Note**: User qualitative intent vs QA report conflicts (e.g. user wants
    multi-body assembly but QA flags FATAL connectivity) are **Judge's
    responsibility** — Judge decides ACCEPT (multi-body is intentional) or
    REPAIR (real disconnect). You won't see REPAIR for intentional multi-body
    designs. If you somehow suspect a QA/Judge misjudgment, still apply the
    fix — Judge's decision is authoritative; disputes go through the user
    checkpoint (Phase 1.9) or by rerunning with `JUDGE_ENABLED=False`.

    **Processing Principles**:
    - Feature-level dimensions (hole diameter, plate thickness, boss height,
      fillet radius, etc.) must be verified by comparing white-box measurement
      data with the user request. If a feature's `size_x/y/z` in the white-box
      data deviates from the nominal in the user request by > 0.5mm, actively
      fix it.
    - Coordinate verification should use `min/max_x/y/z` (actual bounding box)
      from white-box data, not hand calculations or coordinate comments in the
      code. Comments may be outdated from previous fixes without being updated.

    **Examples**:
    - QA report shows overall Z deviation 3mm → Check each feature's size_z in
      white-box data to locate the cause
    - White-box data shows hole diameter feature size_x=6mm but user requests
      14mm → Fix the hole cutting tool
    - User labels hole diameter 4mm but wall thickness at that position is only
      3mm → Adjust hole diameter to a reasonable value (structure takes priority
      over quantitative data)

    ## QA Error Report

    {errors_section}

    ## ⚠️ Absolute Iron Rules — Violating any one means repair failure

    ### 🔴 Iron Rule 1: Feature Preservation (Feature Preservation)
    It is strictly prohibited to escape topology error checking by deleting existing holes, chamfers, or cutting features!
    You **can only** fix it through the following methods:
      - Adjust coordinate position (Pos / Location / translate offset)
      - Rotate structure or feature (Rot / rotate) — encouraged if rotation can better satisfy design requirements or fix problems
      - Increase or decrease extrusion length (amount=)
      - Add new boolean merge structure (body_a + body_b / BOOLEAN_UNION)
      - Adjust hole diameter or position
      - Increase overlap between adjacent solids (push solids into each other via Pos)

    If parts are separated (disconnected bodies), fix by adjusting coordinate positions,
    increasing extrusion overlap (≥0.1mm), or adding a new boolean merge (body = body_a + body_b).
    **Absolutely prohibited** to delete features like holes, slots, reinforcement ribs to make errors "disappear".

    **Encourage rotation**: If rotating the entire structure or a feature can better satisfy design requirements (e.g., better alignment of mounting holes), you should actively use rotation operations.

    ### 🔴 Iron Rule 2: Coordinate System & Transformation Order (Coordinate System & Transformation Order)
    build123d uses a right-handed coordinate system:
      - X axis: left-right direction (positive direction to the right)
      - Y axis: front-back direction (positive direction forward)
      - Z axis: up-down direction (positive direction upward)

    **Transformation order is crucial — must "extrude first, then position"**:
    In the algebra API, `Pos` and `Rot` behave differently for sketch and solid.
    **Positioning sketch first and then extruding will cause coordinate offset**, because `extrude` will ignore
    the existing transformation of the sketch and extrude starting from Z=0.

      - ❌ **Error (common trap)**: Pos/Rot sketch first, then extrude
        ```python
        sk_placed = Pos(0, 8, 10) * Rot(X=90) * sk  # ← transform sketch
        solid = extrude(sk_placed, amount=18)         # ← extrude ignores transform!
        # Result: solid position is wrong, offset exactly equals amount
        ```
      - ✅ **Correct**: Extrude in XY plane first (along Z), then rotate and position the solid
        ```python
        solid_temp = extrude(sk, amount=18)            # ← extrude in XY plane, along Z
        solid = Pos(0, 8, 10) * Rot(X=90) * solid_temp # ← transform solid
        ```
      - ✅ **Also correct**: Do boolean operations on solid first, then apply unified transformation
        ```python
        solid = extrude(Rectangle(36, 42), amount=18)  # X=36, Y=42, Z=0→18
        solid = Rot(X=90) * solid                      # Y→Z, Z→-Y
        solid = Pos(0, 8, 10) * solid                  # Move to position
        ```

    **Mapping of coordinates under Rot(X=90)**:
      - Original X → New X (unchanged)
      - Original Y → New Z
      - Original Z → New -Y
    So the Y direction height of sketch (XY plane) → becomes the global Z direction height.
    The Z direction extrusion amount of extrude → becomes the global -Y direction (needs Pos correction).

    **Coordinate comment synchronization**: When you modify any `Pos()` or `Rot()` parameter, you must simultaneously
    update the adjacent coordinate comments (e.g., `# Y=8..26, Z=9..52`). Outdated comments are worse than no comments
    — they will cause subsequent fixes to be based on wrong hand-calculated expectations. If you cannot determine the new coordinate
    range, delete the comment rather than leaving wrong values.

    **Extrusion direction must be perpendicular to the sketch plane**: XY plane sketch → extrude along Z.
    If other directions are needed, extrude along Z first then Rot the solid.
      - ❌ `extrude(sk_in_XY, dir=(1,0,0))` → "gp_Dir::CrossCross() zero norm"
      - ✅ `solid = Pos(...) * Rot(Y=90) * extrude(sk, amount=6)`

    **Debugging tip**: QA report feature coordinates do not match code Pos → almost always the "position sketch first
    then extrude" problem. Fix: extrude first, then apply Rot/Pos to the solid.

    **Feature Measurement Data**:
    Check `temp_measurements_{{iteration}}.json` during repair (if it exists),
    which records the precise bounding box of each feature before boolean merge:
    `min_x, min_y, min_z, max_x, max_y, max_z`.
    If a feature's min/max does not match the Pos value in the code,
    it indicates the transformation order of this feature has a problem.

    ### 🔴 Iron Rule 3: 100% Algebra API (Algebra API)
    Only use top-level functions like `extrude()`, `Box()`, `body + body`. See `build123d_reference.md` for details.

    ### 🔴 Iron Rule 4: No Context Managers (NO Context Managers)
    `BuildPart`/`BuildSketch`/`Locations` **absolutely prohibited**.
    **Only exception**: `BuildLine` can create wireframes containing arcs, used with `make_face(wire.wire())`.
    ```python
    with BuildLine() as lug_wire:
        Line((-18, 0), (18, 0))
        ThreePointArc((18, 34), (0, 52), (-18, 34))
        Line((-18, 34), (-18, 0))
    sk_lug_profile = make_face(lug_wire.wire())
    ```

    ### 🔴 Iron Rule 5: Preserve Original Intent (Preserve Original Intent)
    In multiple iteration repairs, do not forget the user's initial design goal. All features described in the "Original Design Requirements" above
    (holes, slots, reinforcement ribs, chamfers, mounting surfaces, etc.) must be preserved.
    If a feature is missing in the current code, you must **add** it rather than ignore it.
    Focus on the positional relationships between features.

    ### 🔴 Iron Rule 6: Through-cut / Overshoot Boolean Cut Tools (Overshoot Boolean Cut Tools)
    Must use `_safe_cut(body, tool, label)`. Z-axis through-holes must use `Align.MIN`
    (see the Holes section of `build123d_reference.md` for details).
    The cutting tool must exceed the target surface by ≥2mm at both ends, otherwise OpenCascade coplanar failure.
      - `BLIND_HOLE` → check if Align.MIN is missing
      - `MISSED_CUT` → check Pos coordinates and Rot direction

    ### 🔴 Iron Rule 7: Fillets and Chamfers Last (Fillets and Chamfers Last) — see exception below

    Fillets and chamfers are **the most fragile operations**. Every boolean operation will
    invalidate all edge selectors, so chamfers **usually must** be placed after all boolean operations (union, cut).

    **Correct operation order**:
    ```text
    1. Base solid
    2. Additive features (lugs, ribs, bosses)
    3. Subtractive features (holes, cutouts, slots)
    4. Shell (if needed)
    5. Fillets and Chamfers (last!)
    ```

    **⚠️ Exception: When the fillet area does not overlap with subsequent boolean areas, you can fillet first then union**

    If a complete Circle edge is split into multiple arcs after boolean (e.g., backplate outer circle
    split into 12 segments by 12 blade unions), filleting multiple arcs will fail at degenerate junctions
    (ChFi3d cannot handle edge endpoints where only 2 faces intersect).

    In this case, if the fillet area (e.g., outer edge Circle) and subsequent boolean area (e.g., internal features
    union) **do not overlap spatially**, you can **fillet the complete Circle first, then union the internal features**:

    ```python
    # ✅ Fillet backplate outer circle first (complete Circle, fillet succeeds)
    solid = extrude(Circle(45), amount=6)
    outer_edges = [e for e in solid.edges().filter_by(Plane.XY) if e.length > 200]
    solid = fillet(outer_edges, radius=1.5)  # Complete Circle, fillet succeeds

    # Then union internal features (does not touch the fillet outer circle area)
    hub = Pos(0, 0, 5) * extrude(Circle(13), amount=23)
    solid = solid + hub  # hub is in the center, does not break outer circle fillet
    ```

    Judgment method: the fillet edge's bounding box does not intersect with the subsequent boolean bounding box.

    **Not applicable**: fillet area overlaps with boolean area (e.g., lug-to-base fillet and
    lug union are in the same area), must follow the "fillet last" rule.

    **Chamfer edge selection**: Use `solid.edges().filter_by(Axis.Z)` or coordinate filtering to precisely select edges,
    wrap each `try/except` to prevent failure crashes. See `build123d_reference.md` Fillet section for details.

    ### 🔴 Iron Rule 8: `_measure_feature` Records Variable State at Call Time
    `_measure_feature(var, name, type)` **immediately captures** `var`'s bounding box at **call time**.
    It **must be placed after feature creation and before any operation that modifies the variable**.

    If you use loops/boolean operations to accumulate multiple copies, **measure the prototype first, then enter the loop**:
    ```python
    # ✅ Correct — measure a single prototype
    blade = extrude(sk_blade, amount=3.0)
    _measure_feature(blade, 'step-05-blade', 'extrude')   # ← single blade 3mm

    result = blade
    for _i in range(1, 12):
        result = result + Rot(Z=_i * 30) * blade          # loop does not change blade

    # ❌ Error — measure the accumulated body
    for _i in range(12):
        result = (result or new) + new
    _measure_feature(result, 'step-05-blade', 'extrude')  # ← 12 blades as a whole!
    ```

    **The same applies to any operation that modifies the variable**: boolean merge (`a + b`), loop accumulation,
    variable reassignment. There is only one rule — **`_measure_feature` immediately follows feature creation,
    before the variable is modified**.

    ### 🔴 Iron Rule 9: Rib Sketch Plane Must Be Orthogonal to Connection Face

    A rib connects two faces (e.g., base plate + lug outer side). The rib's **sketch plane** and **connection face plane**
    must be orthogonal (cannot be the same), and the rib's **extrusion direction** = connection face normal direction.

    Mnemonic: connection face in XZ → rib sketch in YZ; connection face in YZ → rib sketch in XZ; connection face in XY → rib sketch in XZ or YZ.

    **Self-check**: If the rib sketch plane == connection face plane, immediately switch to an orthogonal plane. **Do not use Pos/Rot
    rotation to remedy wrong plane selection** — directly switch planes, remap local coordinates according to the new plane.

    ## Repair Strategy — Classified by Error Type

    Select the corresponding repair strategy based on the error type in the QA report:

    ### 🔧 TOPOLOGY / Connectivity (highest priority)
    Symptoms: parts disconnected, multiple shells, abnormally small volume
    Fix:
      - Increase overlap between adjacent solids (≥0.2mm, recommended 0.3-0.5mm)
      - Push solids into each other via Pos to make them share volume
      - Add `body = body_a + body_b` boolean merge
      - Check whether any solid's Pos coordinates cause gaps between them

    ### 🔧 MISSED_CUT / CUT POSITION ERROR (high priority)
    Symptoms: cutting tool does not intersect with target body, holes/slots are not cut out
    Fix:
      - Check whether the cutting tool's `Pos()` coordinates are within the target body range
      - **Increase Cylinder/extrude height**, ensure both ends exceed the target surface by ≥1mm
      - Check whether the `Rot()` direction is consistent with the cutting direction
      - Use `_safe_cut`'s log output to locate which specific cut failed

    ### 🔧 DIMENSION (Dimensional Deviation)
    Symptoms: dimension in some direction is too large or too small
    Fix:
      - Use 70% damping: `new_value = old_value + 0.7 * (target - measured)`
      - Do not jump to the target value all at once, avoid overshoot oscillation
      - If white-box measurement data is correct but QA report is wrong → check the Pos/Rot transformation order in the code
      - If the deviation exactly equals some extrude amount → typical "position first then extrude" bug

    ### 🔧 HOLE MISSING (Hole Missing)
    Symptoms: holes should exist but 0 holes detected
    Fix:
      - Confirm the cutting Cylinder's Pos coordinates are correct (inside the target body)
      - Confirm the Cylinder height is sufficient to penetrate the target body (exceeds by 1mm on both ends)
      - Confirm the Rot direction is correct (along the penetration direction of the target body)
      - If it is a Y-direction hole: `Pos(0, 0, z) * Rot(X=90) * Cylinder(radius, height)`
      - If it is a Z-direction hole: `Pos(x, y, center_z) * Cylinder(radius, height)`
        (center_z = penetration range center, e.g., base plate Z=0..10 → center_z=5)

    ### 🔧 FILLET / CHAMFER FAILURE
    Symptoms: chamfer operation exception caught by except, QA reports fillet size mismatch
    Fix:
      - Ensure chamfers are **after all boolean operations**
      - Reduce chamfer radius (if it exceeds local geometry)
      - Precisely filter edge selectors (filter by Z coordinate, direction, position)
      - Split large groups of chamfers into smaller groups, each with independent try-except

    ### 🔧 WALL THICKNESS
    Symptoms: wall thickness insufficient
    Fix: increase dimension parameters in the relevant direction

    **Boolean merge overlap guidance**:
    - Minimum overlap: 0.2mm (avoid micro features)
    - Recommended overlap: 0.3-0.5mm (ensure reliable merge)
    - Maximum overlap: no more than 10% of feature size (avoid changing external dimensions)

    **Chamfer edge selection guidance**:
    ```python
    # Select edges of specific direction
    vertical_edges = solid.edges().filter_by(Axis.Z)
    horizontal_edges = solid.edges().filter_by(Axis.X)

    # Select edges at specific position
    base_edges = [e for e in solid.edges() if abs(e.center_point().Z) < 1]
    lug_edges = [e for e in solid.edges() if abs(e.center_point().Z - 10) < 2]

    # Safe chamfer
    try:
        solid = fillet(selected_edges, radius=2.0)
    except Exception:
        pass  # If chamfer fails, skip instead of crashing
    ```

    **Rotation example**:
    ```python
    # Rotate a single feature
    hole_tool = Rot(Z=45) * Cylinder(radius=5, height=20)

    # Rotate the entire structure
    body = Rot(X=90) * body  # Rotate 90 degrees around X axis
    ```

    ## Task

    Fix the code in this file so that it passes all the above QA checks.
    **Only make necessary modifications** — do not rewrite the entire file.
    **Only fix 1-2 errors at a time**, prioritize fixing the most severe errors.
    Focus on the specific errors listed in the QA report.
    """)


def generate_initial_solution(
    *,
    script_path: str,
    user_request: str,
    special_features: list[str] | None = None,
) -> bool:
    """Generate initial CAD solution from scratch using Aider or direct API.

    This is different from repair — it generates a complete implementation
    based on the user request, not fixing existing errors.

    Returns
    -------
    bool
        True if the code was successfully generated.  False on failure.
    """
    script_path_obj = Path(script_path)

    # Special features section — non-trivial geometric constraints from CADBrief
    special_features_section = ""
    if special_features:
        features_lines = "\n".join(f"  [{i}] {feat}" for i, feat in enumerate(special_features, 1))
        special_features_section = f"""
## 🔍 Special Features (MUST satisfy each constraint)

These constraints were extracted from the user request by Spec Planner.
QA only checks overall dimension / single_body / water_tightness — it does
NOT verify these.  Your implementation MUST satisfy each item:

{features_lines}

After implementing, self-check: does each special feature hold?
"""
    else:
        special_features_section = ""

    # Build generation prompt
    generation_prompt = f"""Please implement the gen_step() function to create the CAD model described in the user request.

USER REQUEST:
{user_request}
{special_features_section}
Requirements:
1. Implement the COMPLETE solution in the gen_step() function
2. Use build123d API (Box, Cylinder, Sphere, fillet, chamfer, extrude, etc.)
3. Return the completed Part from gen_step()
4. Make sure the geometry matches the user request EXACTLY
5. Use proper dimensions, positions, and boolean operations

Please replace the 'pass' statement in gen_step() with the full implementation.
"""

    # Try Aider first
    aider_available = False
    try:
        from aider.coders import Coder
        from aider.io import InputOutput
        from aider.models import Model
        _ = Model(_nodes._AIDER_MODEL_NAME)
        aider_available = True
    except Exception as exc:
        print(f"[GENERATE] Aider not available: {exc}")

    if aider_available:
        api_key = os.environ.get("DASHSCOPE_API_KEY", "") or _nodes._HARDCODED_API_KEY
        if api_key:
            if _nodes._API_BASE_ENV_VAR:
                os.environ[_nodes._API_BASE_ENV_VAR] = _nodes._DS_BASE_URL
            if _nodes._API_KEY_ENV_VAR:
                os.environ[_nodes._API_KEY_ENV_VAR] = api_key

            print(f"\n[GENERATE] Launching Aider to generate initial solution...")
            print(f"[GENERATE] Script: {script_path_obj.name}")
            print(f"[GENERATE] User request: {user_request[:100]}...")

            try:
                original_code = script_path_obj.read_text(encoding="utf-8")

                model = Model(_nodes._AIDER_MODEL_NAME)
                model.extra_params = {
            "max_tokens": _nodes._AIDER_MAX_TOKENS,  # avoid truncation
            # litellm-level request timeout (s): full-script generation
            # runs 30k-65k output tokens; 120s truncated these mid-file.
            "timeout": _nodes._CFG_LLM_CODEGEN_API_TIMEOUT,
            **_nodes._REPAIR_KWARGS,
        }
                io = InputOutput(
                    yes=True,
                    pretty=False,
                    dry_run=False,
                )
                coder = Coder.create(
                    main_model=model,
                    io=io,
                    detect_urls=False,
                    fnames=[script_path],
                    read_only_fnames=[_nodes._BUILD123D_REF],
                    auto_commits=False,
                    add_gitignore_files=True,
                )
                coder.run(generation_prompt)
                _nodes._join_aider_summarizer(coder)

                # Verify the file was modified
                try:
                    generated_code = script_path_obj.read_text(encoding="utf-8")
                except OSError:
                    print("[GENERATE] Cannot read file after Aider run.")
                    return False

                if generated_code.strip() == original_code.strip():
                    print("[GENERATE] WARNING: Aider made NO changes.")
                    print("[GENERATE] Falling back to direct API generation...")
                else:
                    delta = len(generated_code) - len(original_code)
                    print(f"[GENERATE] Aider completed: code generated (delta: {delta:+d} chars)")
                    return True

            except Exception as exc:
                print(f"[GENERATE] Aider exception: {exc}")
                print("[GENERATE] Falling back to direct API generation...")

    # Fallback: Direct API generation with retry
    print(f"\n[GENERATE] Using direct DashScope API to generate solution...")
    try:
        original_code = script_path_obj.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[GENERATE] Cannot read script: {exc}")
        return False

    client = _nodes._llm_client()

    system_prompt = """You are an expert build123d CAD engineer. Your job is to implement CAD models based on user requests.

You will receive:
1. The USER REQUEST — what to build
2. The CURRENT PYTHON CODE — a template with a gen_step() function that needs implementation

You must output the COMPLETE Python script with the gen_step() function fully implemented.
Use build123d API: Box, Cylinder, Sphere, fillet, chamfer, extrude, boolean operations (+, -, &), etc.
Return a Part from gen_step().

Output ONLY the complete Python code, no explanations."""

    user_prompt = f"""USER REQUEST:
{user_request}

CURRENT CODE:
```python
{original_code}
```

Please implement the gen_step() function to create the requested CAD model.
Output the COMPLETE Python script with the implementation."""

    # Retry logic for transient connection errors
    max_retries = 3
    for attempt in range(max_retries):
        try:
            print(f"[GENERATE] Attempt {attempt + 1}/{max_retries}...")
            response = client.chat.completions.create(
                model=_nodes._REPAIR_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=_nodes._REPAIR_TEMP,
                max_tokens=_nodes._REPAIR_MAX_TOKENS,
                **_nodes._REPAIR_KWARGS,
                timeout=_nodes._CFG_LLM_CODEGEN_API_TIMEOUT,  # configurable via config.py
            )

            generated_code = response.choices[0].message.content.strip()

            # Extract code from markdown if present
            if "```python" in generated_code:
                match = re.search(r"```python\s*(.*?)\s*```", generated_code, re.DOTALL)
                if match:
                    generated_code = match.group(1)

            # Write the generated code
            script_path_obj.write_text(generated_code, encoding="utf-8")

            delta = len(generated_code) - len(original_code)
            print(f"[GENERATE] Direct API completed: code generated (delta: {delta:+d} chars)")
            return True

        except Exception as exc:
            error_msg = str(exc).lower()
            # Check if it's a transient connection error
            if any(keyword in error_msg for keyword in ['connection', 'timeout', 'incomplete', 'peer closed']):
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                    print(f"[GENERATE] Connection error: {exc}")
                    print(f"[GENERATE] Retrying in {wait_time} seconds...")
                    import time
                    time.sleep(wait_time)
                    continue
            # Non-transient error or final attempt
            print(f"[GENERATE] Direct API exception: {exc}")
            return False


def _run_repair_on_script(
    *,
    script_path: str,
    error_details: list[str],
    user_request: str,
    feature_measurements: dict | None = None,
    special_features: list[str] | None = None,
) -> tuple[bool, str | None]:
    """Repair a CAD Python script using Aider (primary) or direct API (fallback).

    **Primary path — Aider (headless)**:
      Uses ``aider.coders.Coder`` with ``openai/qwen3.8-max`` to directly
      edit the script file in place.  Aider's edit formats (search/replace,
      unified diff) are more reliable for targeted code fixes than asking an
      LLM to regenerate the entire file.

    **Fallback path — Direct DashScope API**:
      If Aider is not installed, falls back to sending the current code + QA
      errors to Qwen (DashScope) and extracting the fixed code from the response.

    Returns
    -------
    tuple[bool, str | None]
        (True, None) on success. (False, reason) on failure, where ``reason``
        is a short human-readable string identifying the actual cause (e.g.
        "Aider exception: Request timed out", "No DashScope API key",
        "Aider made no changes (timeout, model rejected, or context limit)").
        The caller surfaces this in the iteration log so the user is not
        misdirected into checking credentials when the real cause was a
        timeout or context-limit issue.
    """
    script_path_obj = Path(script_path)

    # ── Path A: Aider (primary) ──────────────────────────────────────────
    aider_available = False
    try:
        from aider.coders import Coder  # noqa: F811
        from aider.io import InputOutput
        from aider.models import Model
        # Verify Aider actually initializes (model registry, etc.)
        _ = Model(_nodes._AIDER_MODEL_NAME)
        aider_available = True
    except Exception as exc:
        print(f"[REPAIR] Aider not available: {exc}")
        print("[REPAIR] Falling back to direct DashScope API repair.")

    if aider_available:
        api_key = os.environ.get("DASHSCOPE_API_KEY", "") or _nodes._HARDCODED_API_KEY
        if not api_key:
            print("[REPAIR] No DashScope API key — cannot run Aider.")
        else:
            if _nodes._API_BASE_ENV_VAR:
                os.environ[_nodes._API_BASE_ENV_VAR] = _nodes._DS_BASE_URL
            if _nodes._API_KEY_ENV_VAR:
                os.environ[_nodes._API_KEY_ENV_VAR] = api_key

            repair_prompt = _nodes._build_autonomous_repair_prompt(
                user_request=user_request,
                error_details=error_details,
                feature_measurements=feature_measurements,
                special_features=special_features,
            )

            print(f"\n[AIDER REPAIR] Launching Aider on: {script_path_obj.name}")
            print(f"[AIDER REPAIR] Errors to fix: {len(error_details)}")
            print(f"[AIDER REPAIR] Prompt length: {len(repair_prompt)} chars")

            try:
                # Read original code BEFORE Aider runs
                try:
                    original_code = script_path_obj.read_text(encoding="utf-8")
                except OSError:
                    original_code = ""

                model = Model(_nodes._AIDER_MODEL_NAME)
                model.extra_params = {
            "max_tokens": _nodes._AIDER_MAX_TOKENS,  # avoid truncation
            # litellm-level request timeout (s): full-script generation
            # runs 30k-65k output tokens; 120s truncated these mid-file.
            "timeout": _nodes._CFG_LLM_CODEGEN_API_TIMEOUT,
            **_nodes._REPAIR_KWARGS,
        }
                io = InputOutput(
                    yes=True,       # auto-confirm all prompts
                    pretty=False,   # no colored / interactive output
                    dry_run=False,  # actually edit files
                )
                coder = Coder.create(
                    main_model=model,
                    io=io,
                    detect_urls=False,
                    fnames=[script_path],
                    read_only_fnames=[_nodes._BUILD123D_REF],
                    auto_commits=False,
                    add_gitignore_files=True,
                )
                coder.run(repair_prompt)
                _nodes._join_aider_summarizer(coder)

                # Verify the file still has valid content
                try:
                    fixed_code = script_path_obj.read_text(encoding="utf-8")
                except OSError as exc:
                    print("[AIDER REPAIR] Cannot read file after Aider run.")
                    return False, f"Cannot read file after Aider run: {exc}"

                if len(fixed_code.strip()) < 50:
                    print("[AIDER REPAIR] WARNING: file is nearly empty after Aider edit.")
                    return False, "File nearly empty after Aider edit"

                # Detect whether Aider actually changed the code
                if fixed_code.strip() == original_code.strip():
                    print("[AIDER REPAIR] WARNING: Aider made NO changes to the code.")
                    print("[AIDER REPAIR] This is usually a timeout, context-limit, or model rejection - NOT an API key issue.")
                    print("[AIDER REPAIR] Falling back to direct API repair ...")
                    _aider_no_change_reason = (
                        "Aider made no changes (timeout, context limit, or model rejected)"
                    )
                    # Fall through to Path B
                else:
                    delta = len(fixed_code) - len(original_code)
                    print(f"[AIDER REPAIR] Aider completed: code changed (delta: {delta:+d} chars)")
                    return True, None

            except Exception as exc:
                print(f"[AIDER REPAIR] Aider exception: {exc}")
                import traceback as _tb
                _tb.print_exc()
                print("[AIDER REPAIR] Falling back to direct API repair ...")
                _aider_no_change_reason = f"Aider exception: {exc}"
            else:
                # Try/except completed without raising and without returning;
                # only reachable when Aider made no changes (the fall-through
                # case above). ``_aider_no_change_reason`` is set in that path.
                pass
            try:
                _aider_no_change_reason
            except NameError:
                _aider_no_change_reason = None

    # ── Path B: Direct DashScope API (fallback) ───────────────────────────
    ok, fallback_reason = _nodes._run_direct_repair_fallback(
        script_path_obj=script_path_obj,
        error_details=error_details,
        user_request=user_request,
        special_features=special_features,
    )
    if ok:
        return True, None
    # If fallback failed, surface the most informative reason: prefer the
    # fallback's own error (e.g. "Request timed out"), but if the fallback
    # didn't say and Aider had a reason, use that.
    return False, fallback_reason or _aider_no_change_reason or "fallback repair failed"


def _run_direct_repair_fallback(
    *,
    script_path_obj: Path,
    error_details: list[str],
    user_request: str,
    special_features: list[str] | None = None,
) -> tuple[bool, str | None]:
    """Fallback: call DashScope API directly to regenerate the entire script.

    Returns (True, None) on success or (False, reason) on failure, where
    ``reason`` identifies the actual cause (e.g. "API call failed: Request
    timed out") so the caller can surface it in the iteration log instead
    of guessing at credentials.
    """

    try:
        current_code = script_path_obj.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[FALLBACK REPAIR] Cannot read script: {exc}")
        return False, f"Cannot read script: {exc}"

    if not current_code.strip():
        print("[FALLBACK REPAIR] Script is empty.")
        return False, "Script is empty"

    errors_text = "\n".join(f"  [{i+1}] {e}" for i, e in enumerate(error_details))

    # Special features section — non-trivial geometric constraints from CADBrief
    if special_features:
        features_lines = "\n".join(f"  [{i}] {feat}" for i, feat in enumerate(special_features, 1))
        special_features_section = (
            "## 🔍 Special Features (MUST verify each item)\n\n"
            "These constraints were extracted from the user request by Spec Planner.\n"
            "QA only checks overall dimension / single_body / water_tightness — it does\n"
            "NOT verify these.  Your fix MUST satisfy each item:\n\n"
            f"{features_lines}\n\n"
            "After fixing, self-check: does each special feature hold?\n\n"
        )
    else:
        special_features_section = ""

    user_prompt = textwrap.dedent(f"""\
    ## Original User Request (GROUND TRUTH — never deviate)

    {user_request}

    {special_features_section}\
    ## QA Error Report (fix EVERY error listed below)

    {errors_text}

    ## Current Python Code (the script that produced the errors above)

    ```python
    {current_code}
    ```

    ## Task

    Fix ALL errors listed in the QA report above.  Output the COMPLETE
    fixed Python script inside a ```python fenced code block.
    """)

    if len(user_prompt) > 900_000:
        max_code_len = 900_000 - len(user_prompt) + len(current_code) - 5000
        if max_code_len > 1000:
            truncated_code = current_code[:max_code_len] + "\n# ... (truncated)\n"
            user_prompt = user_prompt.replace(current_code, truncated_code)

    print(f"\n[FALLBACK REPAIR] Calling DashScope API directly ...")
    print(f"[FALLBACK REPAIR] Code: {len(current_code)} chars, Errors: {len(error_details)}")

    # Retry logic for transient connection errors
    max_retries = 3
    client = _nodes._llm_client()
    raw_response = ""

    for attempt in range(max_retries):
        try:
            print(f"[FALLBACK REPAIR] Attempt {attempt + 1}/{max_retries}...")
            response = client.chat.completions.create(
                model=_nodes._REPAIR_MODEL,
                temperature=_nodes._REPAIR_TEMP,
                messages=[
                    {"role": "system", "content": _nodes._SYSTEM_PROMPT_REPAIR},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=_nodes._REPAIR_MAX_TOKENS,
                timeout=_nodes._CFG_LLM_CODEGEN_API_TIMEOUT,  # configurable via config.py
                **_nodes._REPAIR_KWARGS,
            )
            raw_response = response.choices[0].message.content or ""
            break  # Success, exit retry loop

        except Exception as exc:
            error_msg = str(exc).lower()
            # Check if it's a transient connection error
            if any(keyword in error_msg for keyword in ['connection', 'timeout', 'incomplete', 'peer closed']):
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                    print(f"[FALLBACK REPAIR] Connection error: {exc}")
                    print(f"[FALLBACK REPAIR] Retrying in {wait_time} seconds...")
                    import time
                    time.sleep(wait_time)
                    continue
            # Non-transient error or final attempt
            print(f"[FALLBACK REPAIR] API call failed: {exc}")
            return False, f"API call failed: {exc}"

    if not raw_response.strip():
        print("[FALLBACK REPAIR] Empty response.")
        return False, "Empty LLM response"

    fixed_code = _nodes._extract_code_from_llm_response(raw_response)
    if not fixed_code or len(fixed_code.strip()) < 50:
        print(f"[FALLBACK REPAIR] Extracted code too short ({len(fixed_code)} chars).")
        return False, f"Extracted code too short ({len(fixed_code)} chars)"

    if fixed_code.strip() == current_code.strip():
        print("[FALLBACK REPAIR] LLM returned IDENTICAL code.")
        return True, None  # not a hard failure

    try:
        script_path_obj.write_text(fixed_code, encoding="utf-8")
        print(f"[FALLBACK REPAIR] Fixed code written: {script_path_obj.name} "
              f"({len(fixed_code)} chars, delta: {len(fixed_code) - len(current_code):+d})")
        return True, None
    except OSError as exc:
        print(f"[FALLBACK REPAIR] Cannot write: {exc}")
        return False, f"Cannot write fixed code: {exc}"
