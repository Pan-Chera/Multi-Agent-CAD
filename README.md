# MAC — Multi-Agent CAD for Parts and Assemblies

> Generate editable CAD parts and multi-part assemblies from natural language.

> [简体中文](README_cn.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
[![Powered by build123d](https://img.shields.io/badge/Powered%20by-build123d-orange.svg)](https://github.com/gumyr/build123d)
![Outputs](https://img.shields.io/badge/Outputs-STEP%20%7C%20STL%20%7C%20GLB-brightgreen.svg)
![Assembly export](https://img.shields.io/badge/Assemblies-URDF-purple.svg)

MAC turns an ordinary design brief into editable engineering geometry. It can
generate a single printable part or decompose a mechanism into separately
generated components, assemble them, validate the result, and export the
artifacts needed for downstream inspection and simulation workflows.

MAC was originally released as a single-part Text-to-CAD workflow and has
since received nearly 1,000 GitHub stars. This release preserves that workflow
while adding visual verification, an independent Judge, and experimental
multi-part assembly generation.

![MAC Web UI walkthrough](assets/parts/ui_demo.gif)

## Assembly Gallery (Technology Preview)

Building on the original single-part workflow, MAC v2 introduces experimental
natural-language assembly generation. The examples below are curated outputs,
not a measured prompt-success rate, and the assembly workflow is not presented
as outperforming CAD-agent skills. Validate generated geometry and joints before
manufacturing or simulation use.

### Complex assemblies

| Three-Axis Gantry Metrology Cell | Telescopic Cinema Robot Crane |
|---|---|
| A bridge-style inspection platform with three orthogonal motion stages. | A multi-stage crane with an articulated support and telescopic payload arm. |
| ![Three-axis gantry metrology cell](assets/assemblies/complex/three-axis-gantry-metrology-cell.gif) | ![Telescopic cinema robot crane](assets/assemblies/complex/telescopic-cinema-robot-crane.gif) |

| Heavy-Duty Mobile Manipulator | Advanced Vision Inspection Robot Arm |
|---|---|
| A mobile base carrying a visually detailed articulated manipulator. | A multi-joint inspection arm with a framed vision payload. |
| ![Heavy-duty mobile manipulator](assets/assemblies/complex/heavy-duty-mobile-manipulator.gif) | ![Advanced vision inspection robot arm](assets/assemblies/complex/advanced-vision-inspection-robot-arm.gif) |

### Complex-gallery generation usage

| Assembly | Planning & mating | Part generation & repair | Judge & other workflow | Total tokens | Estimated cost |
|---|---:|---:|---:|---:|---:|
| Three-Axis Gantry Metrology Cell | about 178,000 | 461,811 | 260,157 | **about 900,000** | **about ¥15** |
| Telescopic Cinema Robot Crane | 535,407 | 663,427 | about 451,000 | **about 1,650,000** | **about ¥23** |
| Heavy-Duty Mobile Manipulator | about 520,000 | 2,678,005 | 2,602,142 | **about 5,800,000** | **about ¥89** |
| Advanced Vision Inspection Robot Arm | 85,016 | 160,367 | about 55,000 | **about 300,000** | **about ¥5** |
| **Total** | **about 1,318,000** | **3,963,610** | **about 3,368,000** | **about 8,650,000** | **about ¥132** |

### Compact functional assemblies

| Hinged Twin-Claw Gripper | Guided Linear Plunger | Rotary Fork-Key Tool | Reusable Five-Digit Hand |
|---|---|---|---|
| ![Hinged twin-claw gripper](assets/assemblies/simple/hinged-twin-claw-gripper.gif) | ![Guided linear plunger](assets/assemblies/simple/guided-linear-plunger.gif) | ![Rotary fork-key tool](assets/assemblies/simple/rotary-fork-key-tool.gif) | ![Reusable five-digit hand](assets/assemblies/simple/reusable-five-digit-hand.gif) |

These curated compact examples exercise natural-language decomposition, joint
interfaces, repeated-part reuse, and mirrored geometry. The complex gallery
shows how the same workflow scales to larger visual assemblies. See the
[assembly workflow documentation](mac_assembly/README.md) for implementation
details and current limitations.

## URDF Simulation

The assembly workflow can export generated mechanisms as URDF files for
downstream robotics and physics simulation. The demonstrations below use an
AI-generated hand assembly to execute object rotation and pick-and-place tasks
in simulation.

| Object Rotation | Pick and Place |
|---|---|
| ![AI-generated hand rotating an object in simulation](assets/assemblies/simulation/ai_hand_twist.gif) | ![AI-generated hand performing pick and place in simulation](assets/assemblies/simulation/ai_hand_pick_place.gif) |

## Part Gallery

MAC also generates standalone mechanical and creative parts. The examples
below are editable CAD results, not image-only generations.

| Honeycomb Organizer | Gyroscope Ornament | Lighthouse | Smartphone Stand | Ball-in-Cage |
|---|---|---|---|---|
| ![Honeycomb organizer](assets/parts/show1.gif) | ![Gyroscope ornament](assets/parts/show2.gif) | ![Lighthouse](assets/parts/show3.gif) | ![Smartphone stand](assets/parts/show4.gif) | ![Ball-in-cage](assets/parts/show5.gif) |

| Articulable Gyroscope | Multi-Link Chain | Geneva Mechanism | Plasma Reactor | Brake Disc |
|---|---|---|---|---|
| ![Articulable gyroscope](assets/parts/show6.gif) | ![Multi-link chain](assets/parts/show7.gif) | ![Geneva mechanism](assets/parts/show8.gif) | ![Plasma reactor](assets/parts/show9.gif) | ![Brake disc](assets/parts/show10.gif) |

The [single-part workflow documentation](multi_agent_cad/README.md) covers
configuration, execution, caching, and QA. Benchmark prompts and per-model
data are linked from its benchmark-details section.

## Real-World Results

![3D-printed MAC models](assets/parts/overview.jpg)

The printed collection above includes benchmark parts and original showcase
models generated by the single-part workflow. MAC can also produce
print-in-place mechanisms containing separate bodies and functional
clearances:

<p align="center">
  <img src="assets/parts/articulable.gif" width="520" alt="Printed articulable MAC models">
</p>

## Single-Part Benchmark

The following numbers apply specifically to the documented ten-prompt,
141-feature **single-part-workflow benchmark**. They are not assembly success-rate or
assembly-cost claims.

| Workflow | Model | In-loop verification | Tokens | Estimated cost | Feature pass rate |
|---|---|---|---:|---:|---:|
| CAD Skill reproduction | Qwen 3.7 | — | 103.95M | ¥125.69 | 138/141 (97.9%) |
| CAD Skill reproduction | Qwen 3.8 | Visual | 87.46M | ¥199.18 | 132/141 (93.6%) |
| MAC v1 | Qwen 3.7 | Geometric | 0.90M | ¥9.66 | 140/141 (99.3%) |
| **MAC v2** | Qwen 3.8 | Visual + geometric | **1.40M** | **¥32.83** | **140/141 (99.3%)** |

Under the Qwen 3.7 setup, MAC v1 used 116× fewer recorded tokens and
had a 13× lower estimated cost than the reproduced Skill baseline. Under
Qwen 3.8 with visual verification, MAC v2 used 62.7× fewer recorded tokens
and had a 6.1× lower estimated cost.

MAC v1 refers to the original single-part workflow and its published benchmark
run. MAC v2 refers to the updated single-part workflow with visual verification
and the independent Judge. Assembly generation is presented separately as a
technology preview and is not included in these benchmark figures.

Across the two documented model configurations, MAC retained a 140/141 feature
pass rate while using substantially fewer recorded tokens and lower estimated
cost than the corresponding reproduced Skill runs.

Methodology and raw breakdowns:
[single-part README](multi_agent_cad/README.md),
[English evaluation](docs/quantified_quality.md), and
[Chinese evaluation](docs/quantified_quality_cn.md).

## Why MAC?

- **Natural-language input** — describe geometry, dimensions, interfaces, and
  motion without writing CAD code or an internal schema.
- **Parts and assemblies** — use one project for standalone printable parts
  and articulated, multi-component mechanisms.
- **Editable engineering outputs** — export STEP, STL, and GLB rather than a
  render-only result; assemblies also provide a URDF handoff.
- **Auditable generation** — inspect structured briefs, geometry plans,
  generated Python, measurements, QA reports, and repair history.
- **Automatic execution and repair** — generated CAD is executed and checked,
  with bounded feedback loops for recoverable failures.
- **Reuse instead of regeneration** — repeated and mirrored components can be
  derived from one source geometry.
- **Model-flexible stages** — configure different OpenAI-compatible models for
  planning, geometry, coding, and repair.
- **Visual workflow** — the single-part pipeline includes a browser UI with 3D
  preview and downloadable results.
- **Built on a proven workflow** — MAC v2 extends the original nearly
  1,000-star single-part pipeline with visual verification and assembly
  generation while retaining the existing part-generation workflow.

## Quick Start

### Install

```bash
git clone https://github.com/Pan-Chera/Multi-Agent-CAD.git
cd Multi-Agent-CAD
conda env create -f environment.yml
conda activate multi_agent_cad
pip install --no-deps "aider-chat==0.82.3"
export DASHSCOPE_API_KEY="your-key"
```

The final `pip install` is needed because `aider-chat` pins NumPy 1.x, which
conflicts with build123d's NumPy 2.x requirement. It is therefore omitted from
`environment.yml`; `--no-deps` avoids replacing the working NumPy version.

The default configuration targets an OpenAI-compatible DashScope endpoint.
Other providers are covered in the
[single-part configuration guide](multi_agent_cad/README.md#configuration-and-model-providers).

> **pip users (no conda)**: `aider-chat` pins `numpy==1.26.4`, but `build123d>=0.8` requires `numpy>=2,<3` — these conflict in pure pip. Use this workaround (verified on macOS arm64 + Python 3.11):
>
> ```bash
> python3.11 -m venv .venv
> source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\activate
> pip install --upgrade pip
> # Install aider first (pulls numpy 1.26.4 + transitive deps), then force-upgrade numpy.
> # Verified: aider 0.82.3 imports cleanly on numpy 2.x — the pin is over-cautious upstream.
> pip install "aider-chat==0.82.3"
> pip install --no-deps --force-reinstall "numpy>=2,<2.3"
> pip install "build123d>=0.8" "langgraph>=0.2,<0.3" "langgraph-checkpoint>=2.0,<3.0" \
>             "pydantic>=2.5" "openai>=1.20.0" "anthropic>=0.30" \
>             "trimesh>=4.0" "rtree>=1.1" "scipy>=1.10" "scikit-learn>=1.3" \
>             "fastapi>=0.110" "uvicorn[standard]>=0.27" "ipython>=8.15" "pytest>=7.4"
> # --no-deps skips re-checking the numpy pin in pyproject.toml; fastapi+uvicorn
> # are already installed by the previous step, so the [web] extras resolve.
> pip install --no-deps -e .
> ```
>
> The last step registers the `mac-config-reset` console script and lets you run `python -m multi_agent_cad.graph` from any directory. See [requirements.txt](requirements.txt) / [pyproject.toml](pyproject.toml) for the canonical dependency list.

> **Windows**: the same `conda env create` + `pip install --no-deps aider-chat==0.82.3` flow works — `trimesh` and `rtree` come from conda-forge prebuilt; `OCP` is pulled in transitively by `build123d` (via its PyPI dep `cadquery-ocp-novtk`). Don't use the pure-pip workaround below on Windows — native wheels for `trimesh`/`rtree` can be unreliable. Set the API key in PowerShell as `$env:DASHSCOPE_API_KEY = "sk-..."` (or `set DASHSCOPE_API_KEY=sk-...` in cmd.exe). For the Web UI under conda, `pip install -e ".[web]"` inside the activated env works — `uvloop` auto-skips on Windows. Windows isn't in CI, but the code avoids Unix-only APIs and uses UTF-8 throughout; issues welcome.

### Generate one part

Set `USER_REQUEST` in [`multi_agent_cad/config.py`](multi_agent_cad/config.py),
then run:

```bash
python -m multi_agent_cad.graph
```

For the single-part browser UI:

```bash
pip install -e ".[web]"
python -m multi_agent_cad.web
```

The UI listens on `127.0.0.1` by default. It executes generated Python with
your user account's permissions, so do not expose it directly to an untrusted
network. Copying results to an arbitrary local directory is disabled unless
`MAC_WEB_ALLOW_DEST_PATH=1` is explicitly set.

### Generate an assembly

Assembly input is also ordinary natural language. Run an included request:

```bash
MAC_ASSEMBLY_REQUEST="$(cat mac_assembly/assembly_prompts/natural_language_benchmarks/01_hinged_twin_claw_gripper.md)" \
python -m mac_assembly
```

Or provide a brief directly:

```bash
MAC_ASSEMBLY_REQUEST="Create a two-part hinged clamp with a fixed base and one rotating jaw." \
python -m mac_assembly
```

Assembly jobs are written under `assembly_jobs/job_<timestamp>/`, including
separate-part geometry, assembled STEP/STL/GLB, URDF, manifests, and QA
artifacts. See the [assembly guide](mac_assembly/README.md) before relying on
an output for manufacturing or simulation.

## Documentation

- [Single-part workflow: setup, architecture, benchmarks, and Web UI](multi_agent_cad/README.md)
- [Assembly workflow: decomposition, mating, reuse, QA, and exports](mac_assembly/README.md)
- [单零件中文文档](multi_agent_cad/README_cn.md)

## Citation

If you find this project useful for your research, please consider citing:

```bibtex
@misc{mac2026,
  author       = {Guanxing Qu and Xueyan Zou},
  title        = {MAC (Multi-Agent CAD): A Decoupled Multi-Agent Framework for Text-to-CAD Generation},
  year         = {2026},
  publisher    = {GitHub},
  journal      = {GitHub repository},
  howpublished = {\url{https://github.com/Pan-Chera/Multi-Agent-CAD}}
}
```

The quantitative single-part evaluation uses
[earthtojake/text-to-cad](https://github.com/earthtojake/text-to-cad) (CAD
Skills) as the comparison baseline. If you cite the benchmark comparison,
please also cite that project:

```bibtex
@misc{texttocad2026,
  author       = {earthtojake},
  title        = {CAD Skills: A skills library for CAD, robotics, and hardware design agents},
  year         = {2026},
  publisher    = {GitHub},
  journal      = {GitHub repository},
  howpublished = {\url{https://github.com/earthtojake/text-to-cad}}
}
```

## License

MIT — see [LICENSE](LICENSE).

The vendored [`packages/cadpy`](packages/cadpy) STEP/GLB runtime is derived
from [earthtojake/text-to-cad](https://github.com/earthtojake/text-to-cad)
(CAD Skills) and is redistributed under its original MIT license — see
[`packages/cadpy/LICENSE`](packages/cadpy/LICENSE).

## Acknowledgements

- [Tsinghua University, IEI Lab](https://maureenzou.github.io/lab.html) — the
  lab where this project was developed; provided the research environment and
  advisor guidance.
- [earthtojake/text-to-cad](https://github.com/earthtojake/text-to-cad) (CAD
  Skills) — source of the comparison baseline and the ten shared single-part
  benchmark prompts. The vendored [`packages/cadpy`](packages/cadpy) runtime
  also derives from this project and retains its original MIT copyright.
- [build123d](https://github.com/gumyr/build123d) — algebraic B-rep CAD kernel.
- [LangGraph](https://langchain-ai.github.io/langgraph/) — stateful agent
  orchestration.
- [Aider](https://aider.chat/) — LLM-driven code repair.
- [Qwen Model Studio](https://www.alibabacloud.com/help/en/model-studio/) —
  OpenAI-compatible models used in the documented experiments.
