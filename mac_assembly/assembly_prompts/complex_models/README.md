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
Only prompts with a completed local assembly output are included here.

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
