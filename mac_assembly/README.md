# MAC Assembly — Natural Language to Multi-Part CAD (Technology Preview)

> [简体中文](README_cn.md) · [Project home](../README.md)

`mac_assembly` extends MAC's [single-part pipeline](../multi_agent_cad/README.md)
to mechanisms and articulated assemblies. A public request remains ordinary
engineering prose: users describe parts, interfaces, dimensions, and motion;
the workflow creates its structured CAD and mating representations internally.

The pipeline exports separate-part geometry, an assembled STEP/STL/GLB, a URDF
handoff, manifests, and QA artifacts. It is designed for inspectable generation
and simulation handoff—not as a guarantee that every unconstrained prompt is
manufacturing-ready.

This module is experimental. Gallery entries are curated outputs rather than a
measured success rate, and no superiority claim over CAD-agent skills is made.
Always inspect generated geometry and joints before downstream use.

## Gallery

### Compact functional assemblies

| Twin-Claw Gripper | Guided Plunger | Rotary Fork-Key | Five-Digit Hand |
|---|---|---|---|
| ![Twin-claw gripper](../assets/assemblies/simple/hinged-twin-claw-gripper.gif) | ![Guided linear plunger](../assets/assemblies/simple/guided-linear-plunger.gif) | ![Rotary fork-key tool](../assets/assemblies/simple/rotary-fork-key-tool.gif) | ![Reusable five-digit hand](../assets/assemblies/simple/reusable-five-digit-hand.gif) |

These four examples are driven by detailed but schema-free natural-language
requests in [`assembly_prompts/natural_language_benchmarks/`](assembly_prompts/natural_language_benchmarks/).

### Complex visual assemblies

| Three-Axis Gantry Metrology Cell | Telescopic Cinema Robot Crane |
|---|---|
| ![Three-axis gantry](../assets/assemblies/complex/three-axis-gantry-metrology-cell.gif) | ![Cinema robot crane](../assets/assemblies/complex/telescopic-cinema-robot-crane.gif) |

| Heavy-Duty Mobile Manipulator | Advanced Vision Inspection Arm |
|---|---|
| ![Mobile manipulator](../assets/assemblies/complex/heavy-duty-mobile-manipulator.gif) | ![Vision inspection arm](../assets/assemblies/complex/advanced-vision-inspection-robot-arm.gif) |

The gallery demonstrates generated geometry and assembly structure. It is not
a quantitative success-rate claim; benchmark assembly metrics should only be
added after a reproducible evaluation is published.

### Generation usage of the complex gallery

| Assembly | Planning & mating | Part generation & repair | Judge & other workflow | Total tokens | Estimated cost |
|---|---:|---:|---:|---:|---:|
| Three-Axis Gantry Metrology Cell | about 178,000 | 461,811 | 260,157 | **about 900,000** | **about ¥15** |
| Telescopic Cinema Robot Crane | 535,407 | 663,427 | about 451,000 | **about 1,650,000** | **about ¥23** |
| Heavy-Duty Mobile Manipulator | about 520,000 | 2,678,005 | 2,602,142 | **about 5,800,000** | **about ¥89** |
| Advanced Vision Inspection Robot Arm | 85,016 | 160,367 | about 55,000 | **about 300,000** | **about ¥5** |
| **Total** | **about 1,318,000** | **3,963,610** | **about 3,368,000** | **about 8,650,000** | **about ¥132** |

## Pipeline

```text
natural-language request
        │
        ▼
Decomposer ──► Mating Architect ──► PartBuilder
    ▲                  ▲                  │
    │                  │                  ▼
 recompose          remate            Assembler
    │                  │                  │
    └──── FeedbackRouter ◄── Judge ◄── AssemblyQA
                       │          │
                  remodel part    └─ repair assembly script
```

| Stage | Responsibility | Main artifact |
|---|---|---|
| Decomposer | Decide what physical parts and interfaces exist | `AssemblyBrief` |
| Mating Architect | Turn interfaces into anchors, offsets, limits, and mates | `MatingPlan` |
| PartBuilder | Generate each distinct component or reuse existing geometry | per-part STEP/STL/Python |
| Assembler | Deterministically translate mates into build123d assembly code | `temp_assembly_*.py` |
| AssemblyQA | Check count, alignment, interference, motion sweeps, and envelope | `AssemblyQAReport` |
| Judge | Ground the next action in QA evidence and rendered views | structured decision |
| FeedbackRouter | Send failures to the layer that can actually repair them | bounded route |

The two planning stages deliberately separate **what exists** from **how it is
positioned**. Part generation reuses the single-part MAC pipeline in isolated
per-part working directories. The Assembler itself is deterministic; an LLM is
only used if its generated script requires repair.

## Natural-Language Input Contract

The user prompt must not require JSON, Python, builder names, selectors, schema
fields, or internal API knowledge. A useful request can still be detailed and
professional. It should describe:

- the mechanism's purpose and approximate envelope;
- physical components and repeated instances;
- important dimensions and clearances;
- which components are fixed or moving;
- hinge, slider, cylindrical, rigid, or ball-joint relationships;
- the desired neutral pose and motion range;
- visible or functional acceptance requirements.

The Decomposer translates that prose into internal `PartSpec` and interface
objects. Tests in [`tests/test_natural_language_benchmark_prompts.py`](../tests/test_natural_language_benchmark_prompts.py)
protect this public boundary.

## Part Generation Paths

PartBuilder chooses the smallest reliable path for each component:

1. **Full single-part MAC pipeline** — free-form geometry through Spec Planner,
   Geometric Architect, deterministic code translation, execution, and repair.
2. **Parameterized builder** — deterministic common mechanical geometry at zero
   generation tokens.
3. **Base body + features** — an LLM-generated body followed by deterministic
   bores, clevises, knuckles, sockets, stems, or other mating features.
4. **Identical reuse** — generate one source STEP and copy it into independent
   installed instances.
5. **Mirrored reuse** — reflect one source STEP across XY, XZ, or YZ and export
   an independent opposite-handed component, such as a right jaw derived from
   a left jaw.

The current [`BUILDERS`](builders.py) registry contains **28** parameterized
builders. Treat the source registry as authoritative if this number changes.
Builders target common failure-prone structures such as horizontal bores,
clevises, linkage bars, plates, bushings, yokes, trussed arm links, and sensor
housings. They are internal implementation choices, not vocabulary users must
put in their requests.

### Reuse semantics

- `reuses_part_id` points an installed instance to its source geometry.
- Every instance retains a unique ID, placement, mate, and output directory.
- Source components are generated before dependent instances.
- Rebuilding a source refreshes all dependent instances.
- `reuse_mirror_plane` creates a genuinely reflected/chiral STEP rather than
  pretending two same-handed copies form a mirror pair.
- A `spec_fingerprint` invalidates stale reuse when the source specification
  changes.

## Mates and Anchors

Supported mate semantics include:

- `rigid`
- `face_to_face`
- `coaxial`
- `revolute`
- `linear`
- `cylindrical`
- `ball`

Simple components can use bounding-box face or principal-axis datums. Complex
components use semantic selectors resolved against real STEP topology, such as
a cylindrical face of a specified radius near an expected location. Resolved
numeric selectors are recorded in `assembly_selector_audit.json`.

For generated clevis features, `direction` describes where the feature extends,
while `pin_axis` describes the hinge-bore axis; these are intentionally separate.
Planar attachment selectors likewise record `surface_axis` as the selected face
normal. Keeping these axes explicit prevents a valid feature from being rotated
into the wrong kinematic plane during assembly.

The generated manifest records each part's full placement transform. QA uses
that transform to convert local datums and axes into world coordinates, which
is essential for chained and rotated joints. Missing transforms are reported as
unverifiable rather than approximated from world-aligned bounding boxes.

## Validation and Feedback

AssemblyQA performs deterministic checks where possible:

- physical part count and labels;
- mate-axis and datum alignment;
- minimum gaps and depth-tolerant interference;
- revolute and translational motion sweeps;
- assembly envelope and dimensional reconciliation;
- presence of expected output artifacts.

The Judge receives structured evidence and optional rendered views. Geometric
QA and visual-semantic verification are reported separately: `verified` or
`failed` is used only when the Judge actually inspected rendered model views;
providers without image support fall back safely to `unverified` without
blocking an otherwise geometrically valid artifact. On a visible mismatch the
Judge returns localized modification suggestions. The router
then assigns a failure to one of four bounded loops:

| Failure owner | Route | Typical example |
|---|---|---|
| decomposition | recompose | wrong parts or missing interface |
| mating plan | remate | incorrect anchor, pose, or motion relationship |
| part geometry | remodel | required feature absent from generated STEP |
| assembly script | repair assembly | execution or deterministic code defect |

Selector misses are probed against the current STEP before routing so a plan
hallucination is not confused with missing geometry. Authentication, missing
dependencies, repeated no-progress timeouts, and exhausted budgets stop with an
explicit incomplete status instead of retrying forever.

If a part exhausts generation attempts but still leaves a usable STEP, it may be
carried forward as `degraded` rather than silently presented as a clean success.
The degraded IDs and warnings remain visible to QA and the Judge, which can
accept the artifact, request targeted remodeling, or halt delivery.

## Quick Start

Install from the repository root following the [main README](../README.md#quick-start),
then export an API key through the environment:

```bash
export DASHSCOPE_API_KEY="your-key"
```

Run a bundled natural-language request:

```bash
MAC_ASSEMBLY_REQUEST="$(cat mac_assembly/assembly_prompts/natural_language_benchmarks/01_hinged_twin_claw_gripper.md)" \
python -m mac_assembly
```

Or pass a request directly:

```bash
MAC_ASSEMBLY_REQUEST="Create a compact hinged inspection fixture with a fixed base and one rotating sensor bracket." \
python -m mac_assembly
```

To resume a job in its existing directory:

```bash
MAC_ASSEMBLY_WORK_DIR="assembly_jobs/job_YYYYMMDD_HHMMSS_00" \
MAC_ASSEMBLY_REQUEST="$(cat path/to/the/original_request.md)" \
python -m mac_assembly
```

Configuration for assembly agents, retry budgets, tolerances, and subprocess
timeouts lives in [`config_assembly.py`](config_assembly.py). Provider and
single-part model settings live in [`../multi_agent_cad/config.py`](../multi_agent_cad/config.py).

## Output Layout

```text
assembly_jobs/job_<timestamp>/
├── assembly_cache/
│   ├── assembly_brief.json
│   └── mating_plan.json
├── parts/<part_id>/
│   ├── temp_design_*.py
│   ├── temp_output_*.step
│   ├── temp_output_*.stl
│   └── part_log.txt
├── placed_stl/<part_id>.stl
├── temp_assembly_*.py
├── assembly_output.step
├── assembly_output.stl
├── assembly_output.glb
├── assembly_output.urdf
├── assembly_manifest.json
├── assembly_mates.json
├── assembly_selector_audit.json
└── assembly_judge_views/
```

Exact names can vary by iteration and handoff stage. The manifest and logs are
the authoritative record of what a run actually produced.

## Caching and Recovery

- The decomposition and mating plan are cached separately.
- Each part owns an isolated planning cache and token ledger.
- Content fingerprints invalidate stale part results after specification
  changes.
- A failed rebuild cannot evict an earlier usable same-spec result.
- Incomplete generated scripts may be resumed once when request and source
  fingerprints match.
- A poisoned or unreadable intermediate STEP is removed rather than replayed.
- Two same-spec timeouts with no new plan, script, STEP, or STL pause the part;
  an operator may explicitly retry after investigating the cause.

## Known Limitations

- Part generation is currently serial.
- The assembly workflow has no dedicated browser UI; inspect its GLB/STEP with
  an external viewer or Blender.
- Catalog/download integration for commercial standard parts is out of scope.
- `face_to_face` is intentionally constrained to its supported placement
  convention; other orientations should use a suitable axis or rigid mate.
- Interference and motion checks require the declared geometry dependencies;
  startup preflight refuses to claim a verified pass when containment support
  is unavailable.
- URDF is a file-level handoff. Dynamics, actuator tuning, and task-specific
  simulation validation remain downstream responsibilities.
- Complex free-form parts can still fail or require prompt refinement. The
  gallery demonstrates outputs, not universal prompt success.

## Tests

From the repository root:

```bash
.venv_311/bin/python -m pytest tests/ -q
```

Focused public-input and documentation checks include:

```bash
.venv_311/bin/python -m pytest \
  tests/test_natural_language_benchmark_prompts.py \
  tests/test_decomposer_prompt_sync.py -q
```

## License and Acknowledgements

The project is released under the repository's [MIT License](../LICENSE).
Assembly generation uses build123d and the vendored `cadpy` assembly runtime;
the latter derives from [earthtojake/text-to-cad](https://github.com/earthtojake/text-to-cad)
under its original MIT license.
