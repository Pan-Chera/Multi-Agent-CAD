# Contributing to MAC (Multi-Agent CAD)

Thanks for your interest in improving MAC! This doc covers setup, conventions, and how to submit changes.

## 📦 Local development setup

### Prerequisites

- Python 3.11
- conda (miniconda or anaconda)
- A DashScope (Alibaba Cloud Bailian) API key for `qwen3.7-max` — or any OpenAI-compatible endpoint. See [multi_agent_cad/config.py](multi_agent_cad/config.py) for how to point at other providers.

### Install

```bash
git clone <your-fork-url>
cd Multi-Agent-CAD

# Create the conda env (Python 3.11 + all native deps via conda-forge)
conda env create -f environment.yml
conda activate multi_agent_cad
```

If you prefer pip, see [requirements.txt](requirements.txt) — but install `numpy`, `scipy`, `trimesh`, `rtree` from conda-forge first; their C extensions are unreliable as pure pip wheels.

### Configure API key

**Do not put your API key in [multi_agent_cad/config.py](multi_agent_cad/config.py) and commit it.** Export it as an environment variable instead:

```bash
# bash / zsh
export DASHSCOPE_API_KEY="sk-..."   # Despite the name, works for any OpenAI-compatible key
# PowerShell
$env:DASHSCOPE_API_KEY = "sk-..."
```

To persist across sessions:
- bash/zsh: add the `export` line to `~/.zshrc` (or `~/.bashrc`)
- PowerShell (one-time, new shells pick it up): `setx DASHSCOPE_API_KEY "sk-..."`, or set it via System Properties → Environment Variables

If you ever need to reset [multi_agent_cad/config.py](multi_agent_cad/config.py) to defaults (e.g. after a bad edit):

```bash
python -m multi_agent_cad._config_defaults --reset
```

### Run a smoke test

```bash
python -m multi_agent_cad.graph
```

This runs the default `USER_REQUEST` (a clevis bracket — see [multi_agent_cad/config.py](multi_agent_cad/config.py)). If the pipeline finishes and produces `temp_output_0.step` + `temp_output_0.stl`, your setup is good.

## 🧱 Project layout

```
multi_agent_cad/
├── __init__.py
├── config.py                  # User-editable config (DON'T COMMIT KEYS)
├── _config_defaults.py        # Template + --reset utility for config.py
├── graph.py                   # "original" workflow entry point
├── graph_aider.py             # "modify-existing-file" workflow entry point
├── nodes.py                   # All 4 LangGraph node implementations
├── schemas.py                 # Pydantic data models (CADBrief, ArchitectPlan, etc.)
├── token_tracker.py          # Token / API call accounting
├── build123d_reference.md    # API reference injected into Aider context
├── prompts/                   # Static LLM prompts for Spec Planner / Architect / Coder / Repair
└── WORKFLOW.md                # Internal architecture doc
legacy_refs/
└── check_mesh.py              # Engine B: STL mesh analysis (Union-Find connectivity)
packages/
└── cadpy/                     # Engine A: STEP topology analysis
pipeline_cache/                # Cached cad_brief.json + architect_plan.json (gitignored)
docs/                          # Project docs: quantified_quality.md, quantified_quality_cn.md, qwen3.7_token.md
```

## 🔄 Workflow conventions

### Code style

- Python 3.11+. No need for `from __future__ import annotations` — type hints work natively.
- 4-space indentation, line length around 100.
- Module-level functions prefixed with `_` are private (don't import from outside the module).
- All public node functions return a dict matching `GraphState` keys (see [multi_agent_cad/schemas.py](multi_agent_cad/schemas.py)).

### Commit messages

Use a concise imperative summary:

```
Add defensive correction for floating geometry in P9 spiral staircase
```

Or, if non-trivial:

```
Refactor _safe_fillet to use lambda edge-selector

Previously the edge list was computed once and cached, causing stale
edges after a boolean cut. Forcing re-evaluation each call fixes the
override bug seen on P8 item 13.
```

No formal commit message convention enforced — just make it descriptive.

### Before you push

1. Run a smoke test: `python -m multi_agent_cad.graph` on the default prompt. It should finish without crashing.
2. Verify you haven't accidentally committed secrets:
   ```bash
   # bash / zsh
   git diff --cached | grep -iE "(api[_-]?key|sk-sp|sk-proj|sk-svcacct)"
   # PowerShell (no grep — use Select-String)
   git diff --cached | Select-String -Pattern 'api[_-]?key|sk-sp|sk-proj|sk-svcacct' -AllMatches
   ```
   If that returns anything, you're about to commit a key — remove it first.
3. Verify [multi_agent_cad/config.py](multi_agent_cad/config.py) has `DS_API_KEY = ""` (not a real key).

## 🧪 Tests

There's no formal test suite yet. For now:

- Smoke test: run the default workflow end-to-end.
- For targeted changes, edit `USER_REQUEST` in [multi_agent_cad/config.py](multi_agent_cad/config.py) to a simple part (e.g. `Create a 50x50x6 mm base plate with a 20 mm central hole.`) and verify the output STEP matches.
- For QA / repair changes, force a failing prompt (e.g. remove a fillet instruction) and confirm Aider's repair loop kicks in.

If you're adding a new feature, please include a prompt that exercises it.

## 📝 Submitting changes

1. Fork the repo, create a branch: `git checkout -b my-feature`.
2. Make your changes. Keep commits focused — one logical change per commit.
3. Push to your fork: `git push origin my-feature`.
4. Open a PR against `main`. In the PR description, include:
   - What changed and why
   - Which prompt(s) you tested against
   - Token / cost numbers before vs after (if relevant — see [qwen3.7_token.md](docs/qwen3.7_token.md) for the benchmark format)

## 🐛 Reporting bugs

Open an issue with:

- The prompt that failed
- The relevant `temp_*.py` (the generated code — safe to share, no secrets)
- The `temp_missed_*.json` runtime diagnostics, if any
- The QA report output (from the terminal)
- The model + provider you used (e.g. `qwen3.7-max` via DashScope)

## 🤝 Code of conduct

Be kind. Be specific. Be wrong in public — that's how we learn.
