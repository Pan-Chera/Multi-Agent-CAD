# Single-Part Workflow

> [Project home](../README.md) · [简体中文](README_cn.md) · [Full pipeline design](WORKFLOW.md)

This guide covers the configuration, artifacts, caching, architecture, and QA
behavior specific to MAC's single-part workflow. For installation and the
first-run commands, use the [project Quick Start](../README.md#quick-start).
The [assembly workflow](../mac_assembly/README.md) builds on this pipeline but
has separate inputs, caches, validation, and exports.

## Benchmark Gallery

The ten shared benchmark prompts progress from conventional machined parts to
more complex multi-feature and multi-body geometry.

| P1 Block | P2 Flange | P3 L-Bracket | P4 Stepped Shaft | P5 Enclosure |
|---|---|---|---|---|
| ![P1 rectangular block](../assets/parts/benchmark01.gif) | ![P2 circular flange](../assets/parts/benchmark02.gif) | ![P3 L-bracket](../assets/parts/benchmark03.gif) | ![P4 stepped shaft](../assets/parts/benchmark04.gif) | ![P5 open-top enclosure](../assets/parts/benchmark05.gif) |

| P6 Clevis | P7 Engine Cylinder | P8 Impeller | P9 Spiral Staircase | P10 Planetary Gears |
|---|---|---|---|---|
| ![P6 aerospace clevis](../assets/parts/benchmark06.gif) | ![P7 radial-engine cylinder](../assets/parts/benchmark07.gif) | ![P8 centrifugal impeller](../assets/parts/benchmark08.gif) | ![P9 spiral staircase](../assets/parts/benchmark09.gif) | ![P10 planetary gears](../assets/parts/benchmark10.gif) |

## Original Model Gallery

These ten prompts were designed for this project and show a broader range of
mechanical, decorative, and print-in-place geometry.

| Honeycomb Organizer | Gyroscope Ornament | Lighthouse | Smartphone Stand | Ball-in-Cage |
|---|---|---|---|---|
| ![Honeycomb organizer](../assets/parts/show1.gif) | ![Gyroscope ornament](../assets/parts/show2.gif) | ![Lighthouse](../assets/parts/show3.gif) | ![Smartphone stand](../assets/parts/show4.gif) | ![Ball-in-cage](../assets/parts/show5.gif) |

| Articulable Gyroscope | Multi-Link Chain | Geneva Mechanism | Plasma Reactor | Brake Disc |
|---|---|---|---|---|
| ![Articulable gyroscope](../assets/parts/show6.gif) | ![Multi-link chain](../assets/parts/show7.gif) | ![Geneva mechanism](../assets/parts/show8.gif) | ![Plasma reactor](../assets/parts/show9.gif) | ![Brake disc](../assets/parts/show10.gif) |

## Single-Part Workflow Benchmark

The original and updated workflows were evaluated under their corresponding
Qwen 3.7 and Qwen 3.8 configurations:

| Workflow | Model | In-loop verification | Tokens | Estimated cost | Feature pass rate |
|---|---|---|---:|---:|---:|
| CAD Skill reproduction | Qwen 3.7 | — | 103.95M | ¥125.69 | 138/141 (97.9%) |
| CAD Skill reproduction | Qwen 3.8 | Visual | 87.46M | ¥199.18 | 132/141 (93.6%) |
| MAC v1 | Qwen 3.7 | Geometric | 0.90M | ¥9.66 | 140/141 (99.3%) |
| **MAC v2** | Qwen 3.8 | Visual + geometric | **1.40M** | **¥32.83** | **140/141 (99.3%)** |

Under Qwen 3.7, MAC v1 used 116× fewer recorded tokens and had a 13×
lower estimated cost than the reproduced Skill baseline. Under Qwen 3.8 with
visual verification, MAC v2 used 62.7× fewer recorded tokens and had a 6.1×
lower estimated cost.

MAC v1 is the original single-part workflow and its published benchmark run.
MAC v2 is the updated single-part workflow with visual verification and the
independent Judge. Assembly generation is a separate technology preview and is
not included in these figures.

Across both documented configurations, MAC retained 140/141 feature coverage
while using substantially fewer recorded tokens and lower estimated cost than
the corresponding reproduced Skill runs. These figures are not a general
success rate. See [Benchmark details and scope](#benchmark-details-and-scope)
for the methodology and limitations.

## Configuration and model providers

Edit [`config.py`](config.py) to set `USER_REQUEST`, the provider endpoint (`DS_BASE_URL`), and per-stage model settings. Keep API keys in the `DASHSCOPE_API_KEY` environment variable rather than committing them to source. To reset a customized configuration:

```bash
python -m multi_agent_cad._config_defaults --reset
```

The model interface is OpenAI-compatible. `SPEC_PLANNER_*`, `ARCHITECT_*`, `CODER_*`, and `REPAIR_*` configure the corresponding stages; `AIDER_*` controls code repair. Each can have its own model, temperature, token limit, and keyword arguments. The default endpoint is DashScope. Other OpenAI-compatible endpoints can be used by changing `DS_BASE_URL`, model IDs, and Aider's litellm model name. Model IDs vary by provider and time: verify them with your provider. When switching away from Qwen, remove provider-specific options such as `enable_thinking` from `*_KWARGS`.

For example, the **configuration shape** for an OpenAI-compatible provider is:

```python
DS_BASE_URL = "https://api.example.com/v1"
SPEC_PLANNER_MODEL = "provider-model-id"
ARCHITECT_MODEL = "provider-model-id"
CODER_MODEL = "provider-model-id"
REPAIR_MODEL = "provider-model-id"
SPEC_PLANNER_KWARGS = ARCHITECT_KWARGS = CODER_KWARGS = REPAIR_KWARGS = {}
AIDER_MODEL = "provider-prefix/provider-model-id"
```

This is illustrative, not a tested claim for a specific provider. The environment variable name `DASHSCOPE_API_KEY` is historical; it carries the configured provider's key.

## Single-Part Operation

The basic launch command is documented in the project Quick Start. During a
CLI run, the terminal streams LangGraph events and each QA pass opens a
10-second checkpoint: auto-iterate (`1`), inject a new requirement (`2`), or
stop and retain the current artifacts (`3`). Timeout selects auto-iterate.

To modify an existing `temp_design*.py` with a new request:

```bash
python -m multi_agent_cad.graph_aider
```

| Artifact | Purpose |
|---|---|
| `temp_design_*.py` | Generated build123d source |
| `temp_output_*.step` / `.stl` | Geometry for editing or printing |
| `temp_measurements_*.json` | Measured feature data |
| `temp_missed_*.json` | Runtime and missed-feature diagnostics |

The iteration suffix is not necessarily `0`; inspect the latest run rather than assuming a fixed filename.

### Web UI behavior

The Web UI launch commands are in the root Quick Start. It provides a
configuration form, browser 3D preview, and downloads, but currently
auto-iterates instead of exposing the CLI checkpoint. UI jobs use isolated
temporary directories; CLI runs write `temp_*` artifacts to the repository
root. Security and network-binding notes are also maintained in the root
README and [`SECURITY.md`](../SECURITY.md).

## Cache and prompt changes

`pipeline_cache/cad_brief.json` stores the Spec Planner output; `pipeline_cache/architect_plan.json` stores the Geometric Architect output. Re-running the **same** request can reuse these stages and restart code generation/repair.

**Important:** the single-part cache checks file presence, not whether the current `USER_REQUEST` matches its contents. Before generating a **different** model, remove only these two cache files:

```bash
rm pipeline_cache/cad_brief.json pipeline_cache/architect_plan.json
```

Alternatively, set `force_refresh: True` in [`graph.py`](graph.py)'s `get_default_initial_state`. The assembly workflow has separate caches and fingerprint rules; do not assume this single-part behavior applies there.

Reference images can be placed in repository-root `user_input_images/`. Spec Planner can use them to infer geometric intent, and the QA Judge can compare them with rendered views. For Web UI jobs, the root image folder is used when the job directory has no input images.

## Pipeline and validation

```text
natural-language request / images
  → Spec Planner (CADBrief)
  → Geometric Architect (ArchitectPlan)
  → deterministic Python translation, with Aider fallback where needed
  → execute build123d and export STEP/STL
  → measurements + QA Judge + bounded repair loop
```

Agents exchange compact structured artifacts instead of replaying an entire conversation. The deterministic translator in [`nodes.py`](nodes.py) handles common operations such as extrusion, revolution, holes, booleans, patterns, mirrors, fillets, chamfers, and shells without an extra code-generation model call. Unsupported plan steps may produce `TODO_AIDER` markers for model-assisted completion.

QA is inspectable: plans, generated code, measurements, missed-feature diagnostics, renderings, and repair decisions remain available on disk. The Judge can accept a result, halt on an impossible request, or request repair; multimodal configuration can provide rendered views, with text-only fallback when unavailable. The full stage design, state definitions, and safeguards are documented in [`WORKFLOW.md`](WORKFLOW.md).

## Benchmark details and scope

The headline comparison on the [project homepage](../README.md#single-part-benchmark) covers **ten shared prompts and 141 evaluated features** in the single-part workflow. It does **not** measure assembly success or establish a general guarantee for unseen prompts. Full prompt-level token/cost data and methodology are in [`qwen3.7_token.md`](../docs/qwen3.7_token.md), the [English evaluation](../docs/quantified_quality.md), and the [Chinese evaluation](../docs/quantified_quality_cn.md). The benchmark prompts originate from [earthtojake/text-to-cad](https://github.com/earthtojake/text-to-cad); the showcase prompts are original to this repository.

Wall-clock speed figures should be treated as informal observations unless accompanied by a reproducible timing protocol. Generated geometry, especially multi-body print-in-place parts, should still be inspected and validated for the intended manufacturing process.
