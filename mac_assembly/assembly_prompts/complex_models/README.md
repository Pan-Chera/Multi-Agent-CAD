# Complex Assembly Showcase Prompts

These prompts are curated for visually impressive MAC Assembly gallery assets.
They deliberately favor large silhouettes, exposed joints, repeated modules,
truss structures, layered housings, and independently colored parts.

They are not the small reliability benchmark. A model can be useful as a
gallery asset after static CAD QA even if every joint has not yet been verified
in MuJoCo. Record the actual verification level next to every published model:

- `Generated`
- `CAD Verified`
- `Kinematics Verified`
- `URDF Exported`
- `MuJoCo Verified`

## Files and recommended generation order

1. `Advanced_Inspection_Robot_Request.md` — existing, highly constrained reference prompt.
2. `Automated_Gantry_Metrology_Cell_Request.md` — lowest risk; mostly rigid frame + linear axes.
3. `Telescopic_Cinema_Robot_Crane_Request.md` — medium risk; strong truss silhouette.
4. `Heavy_Duty_Mobile_Manipulator_Request.md` — medium/high risk; wheels + articulated arm.
5. `Orbital_Service_Satellite_Request.md` — high visual value; 7 instances / 5 geometries / 3 revolute joints (rewritten 2026-09-10, see complexity budget below).
6. `Quadruped_Inspection_Robot_Request.md` — highest risk; many mixed-axis joints.

## Complexity budget (2026-09-10 revision)

The original satellite prompt demanded 29 parts and died on the FIRST
decomposer call — the brief JSON for 29 PartSpecs exceeded the request
timeout three times (assembly_jobs/轨道服务卫星/run.log). Token data from
the completed runs says each LLM-generated part costs roughly 60-90k tokens
(gantry: 8 LLM parts -> 722k; crane: 12 -> 919k).

Rule for new prompts: keep the assembly at 4-6 LLM-generated geometries.
Fuse every rigid detail (panels, radiators, covers, pods) into its carrier
part as features; keep separate parts only where something genuinely moves.
A 29-part satellite is really a 4-part problem: bus (rigid everything),
solar wing (reuse x2), antenna (pan), service arm (reuse x2) — plus the
zero-token `bushing` docking ring. This sizes the whole run at roughly
350-450k tokens and keeps the decomposer output small enough to finish.

The single-part MAC pipeline runs with thinking disabled
(`multi_agent_cad/config.py` `*_KWARGS`). The rewritten parts above are
sized to pass without it (orthogonal box features, no horizontal-axis
bores). If a complex part still fails repeatedly, enable
`enable_thinking: True` for SPEC_PLANNER/ARCHITECT/CODER/AIDER before
rerunning that part.

`Quadruped_Inspection_Robot_Request.md` (27 parts) has the same
decomposer-timeout risk and needs the same 4-6 geometry rewrite before its
run — a quadruped cannot shrink below ~9 parts while keeping leg
articulation, so decide the trade-off first.

## Run one prompt after Qwen quota recovers

From the repository root:

```bash
MAC_ASSEMBLY_REQUEST="$(cat mac_assembly/assembly_prompts/complex_models/Automated_Gantry_Metrology_Cell_Request.md)" \
python -m mac_assembly
```

Run only one complex prompt at a time. Inspect the completed output before
starting the next one, and preserve successful per-part caches when repairing
an assembly. Do not restart every part when only one part or one mate failed.

## Publishing rule

Never imply that a gallery model passed simulation unless its exported URDF
was actually loaded and exercised. Publish exact prompts, model name, token
usage, elapsed time, repair count, and whether any prompt or source was
manually adjusted.

