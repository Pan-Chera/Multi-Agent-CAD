# MAC — 多 Agent 文本到 CAD 生成框架

MAC（Multi-Agent CAD）是一个解耦的多 agent 文本到 CAD 生成框架：把"需求解析、
几何设计、代码生成、错误修复"拆给独立的 agent，agent 之间只传结构化 JSON
（而非反复重读完整对话历史），在 10 个 prompt 基准上把总成本从 ¥125.69 降到
**¥9.67（13×）**、总 token 从 103.9M 降到 **0.90M（116×）**，特征通过率
**99.3%**（140/141），同时保持全流程白盒可审计。

本仓库包含两条工作流，后者在前者之上扩展：

| 目录 | 工作流 | 一句话 |
|---|---|---|
| [multi_agent_cad/](multi_agent_cad/) | **单零件工作流**（MAC 核心） | 一段文本 → 单个可打印 3D 零件（STEP/STL/GLB） |
| [mac_assembly/](mac_assembly/) | **装配体工作流** | 一段文本 → 多零件装配体（STEP/STL/GLB + URDF），每个零件独立走单零件管线 |

```
                ┌─────────────────────────────────────────────┐
                │  mac_assembly — 装配体工作流（7 节点）        │
                │  decomposer → mating_architect → part_builder │
                │        → assembler → assembly_qa → judge     │
                │                  │  四条反馈回路               │
                │                  ▼  子进程零改动复用           │
                │  ┌─────────────────────────────────────┐     │
                │  │  multi_agent_cad — 单零件工作流      │     │
                │  │  Spec Planner → Geometric Architect │     │
                │  │  → Python Coder → Skill Loop + Judge│     │
                │  └─────────────────────────────────────┘     │
                └─────────────────────────────────────────────┘
```

## 单零件工作流（multi_agent_cad/）

4 个解耦的 agent 阶段，每阶段可独立选模（混合路由）：

1. **Spec Planner** — 需求 → `CADBrief`（结构化 JSON，可读参考图）
2. **Geometric Architect** — brief → `ArchitectPlan`（草图、特征步骤、选择器）
3. **Python Coder** — plan → build123d 代码：确定性翻译器
   [`_plan_to_code`](multi_agent_cad/nodes.py) 优先承接常见 CAD 操作
   （extrude/revolve/hole/boolean/pattern/mirror/fillet/chamfer/shell），
   **零 token**；仅不支持的步骤留给 Aider 兜底
4. **Autonomous Skill Loop** — 执行 → 双引擎 QA（包围盒/单体/水密）→
   Aider 修复 → QA Judge（可 ACCEPT/HALT/REPAIR，多模态渲染视图）

中间产物（brief/plan/源码/测量/诊断）全部落盘可审计；迭代 checkpoint
允许中途介入。Web UI（表单配置 + 3D 预览）与 CLI 共用同一管线。

详细文档：[multi_agent_cad/README_cn.md](multi_agent_cad/README_cn.md)（中文）/
[multi_agent_cad/README.md](multi_agent_cad/README.md)（English）/
[multi_agent_cad/WORKFLOW.md](multi_agent_cad/WORKFLOW.md)（流水线图与各阶段设计）

## 装配体工作流（mac_assembly/）

在单零件管线之上加一层装配语义，7 节点 LangGraph + 四条反馈回路：

- **Decomposer** — 装配请求 → `AssemblyBrief`：拆零件 + 接口语义
  （what），输出 PartSpec 列表
- **Mating Architect** — 接口 → `MatingPlan`：结构化 MateSpec
  （anchor/offset/limits，how），两级间有确定性校验层
- **PartBuilder** — 每个零件**零改动复用**单零件管线（子进程 + per-part
  cwd/缓存隔离）；另有三条省 token 路径：参数化 builder 库（22 个，
  零 token）、v3 base+features（LLM 底座 + 确定性运动学特征算子）、
  part reuse（相同零件只生成一次模板，实例复制）
- **Assembler** — 确定性翻译器 `assembly_codegen`：MateSpec →
  AssemblyHelper 源码，零 token；LLM 仅在脚本失败时修复
- **AssemblyQA** — 零件计数 / mate 对齐 / 双向干涉（深度容差，接触不算
  碰撞）/ revolute ±30° 扫角 + linear 平移扫掠运动学 / 包络对账
- **Judge + FeedbackRouter** — accept/halt/remate/remodel/recompose/repair
  错误归因路由，各自带预算上限；SELECTOR 锚点失配走确定性归因（三态探测）

交付 GLB + URDF（供 PyBullet/MuJoCo/MoveIt）+ snapshot 包 + manifest。
装配请求 prompt 库见
[mac_assembly/assembly_prompts/](mac_assembly/assembly_prompts/)
（含 complex_models 展示级 prompt 与复杂度预算规则）。

详细文档：[mac_assembly/README.md](mac_assembly/README.md)
（架构、反馈路由决策表、builder/feature/reuse 三条省 token 路径、
anchor 与 mate 语义、已知边界）

## 快速开始

```bash
git clone https://github.com/Pan-Chera/Multi-Agent-CAD
cd Multi-Agent-CAD
conda env create -f environment.yml
conda activate multi_agent_cad
pip install --no-deps "aider-chat==0.82.3"   # numpy 2.x 冲突的 workaround
export DASHSCOPE_API_KEY="sk-..."            # 或写入 multi_agent_cad/config.py 的 DS_API_KEY
```

（纯 pip 安装、Windows、换用其它 LLM provider 见
[multi_agent_cad/README_cn.md §2](multi_agent_cad/README_cn.md#2-快速上手)。）

单零件（编辑 [multi_agent_cad/config.py](multi_agent_cad/config.py) 的
`USER_REQUEST` 后）：

```bash
python -m multi_agent_cad.graph        # 产物在 pipeline_cache/ + temp_*.py/.step
python -m multi_agent_cad.web          # 或 Web UI（需 pip install -e ".[web]"）
```

装配体：

```bash
MAC_ASSEMBLY_REQUEST="$(cat mac_assembly/assembly_prompts/Rack_Pinion_Parallel_Gripper_Request.md)" \
python -m mac_assembly                 # 产物在 assembly_jobs/job_<时间戳>/
```

## 更多

- 实物打印画廊与基准模型（P1–P10 / S1–S10）：[multi_agent_cad/README_cn.md §1](multi_agent_cad/README_cn.md#1-实物打印画廊)，图片在 [assets/](assets/)
- 定量基准方法论与逐 prompt token/成本明细：[docs/quantified_quality_cn.md](docs/quantified_quality_cn.md)
- 学术引用：[multi_agent_cad/README.md §5 Citation](multi_agent_cad/README.md#5-citation)
- 测试：仓库根 `tests/`（装配层 + 管线同步校验）

## License

MIT — 见 [LICENSE](LICENSE)。vendored 的
[packages/cadpy](packages/cadpy)（STEP/GLB 运行时 + AssemblyHelper）
衍生自 [earthtojake/text-to-cad](https://github.com/earthtojake/text-to-cad)。
