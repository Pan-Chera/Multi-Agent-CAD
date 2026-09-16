# 单零件工作流

> [项目首页](../README_cn.md) · [English](README.md) · [完整工作流设计](WORKFLOW.md)

本文档聚焦 MAC 单零件工作流特有的配置、产物、缓存、架构与 QA 行为。
安装和首次运行请直接参照[项目快速开始](../README_cn.md#快速开始)，避免在多个
README 中重复维护同一套命令。[装配体工作流](../mac_assembly/README_cn.md)
复用这一流水线，但具有独立的输入、缓存、验证与导出机制。

## Benchmark 模型展示

10 条共享 benchmark 提示词从常规机械加工零件逐步扩展到多特征、多实体几何。

| P1 方块 | P2 法兰 | P3 L 形支架 | P4 阶梯轴 | P5 开口箱体 |
|---|---|---|---|---|
| ![P1 方块](../assets/parts/benchmark01.gif) | ![P2 法兰](../assets/parts/benchmark02.gif) | ![P3 L 形支架](../assets/parts/benchmark03.gif) | ![P4 阶梯轴](../assets/parts/benchmark04.gif) | ![P5 开口箱体](../assets/parts/benchmark05.gif) |

| P6 航空叉耳 | P7 发动机气缸 | P8 离心叶轮 | P9 螺旋楼梯 | P10 行星齿轮组 |
|---|---|---|---|---|
| ![P6 航空叉耳](../assets/parts/benchmark06.gif) | ![P7 发动机气缸](../assets/parts/benchmark07.gif) | ![P8 离心叶轮](../assets/parts/benchmark08.gif) | ![P9 螺旋楼梯](../assets/parts/benchmark09.gif) | ![P10 行星齿轮组](../assets/parts/benchmark10.gif) |

## 原创模型展示

以下 10 条提示词由本项目设计，覆盖机械、装饰及一体打印可动结构。

| 蜂巢收纳座 | 陀螺仪摆件 | 灯塔 | 手机支架 | 笼中小球 |
|---|---|---|---|---|
| ![蜂巢收纳座](../assets/parts/show1.gif) | ![陀螺仪摆件](../assets/parts/show2.gif) | ![灯塔](../assets/parts/show3.gif) | ![手机支架](../assets/parts/show4.gif) | ![笼中小球](../assets/parts/show5.gif) |

| 可动陀螺仪 | 多环链 | 马尔他机构 | 等离子反应堆 | 刹车盘 |
|---|---|---|---|---|
| ![可动陀螺仪](../assets/parts/show6.gif) | ![多环链](../assets/parts/show7.gif) | ![马尔他机构](../assets/parts/show8.gif) | ![等离子反应堆](../assets/parts/show9.gif) | ![刹车盘](../assets/parts/show10.gif) |

## 单零件工作流基准数据

原始工作流和更新后的工作流分别在对应的 Qwen 3.7、Qwen 3.8 配置下进行评估：

| 工作流 | 模型 | 环内验证 | Token | 估算成本 | 特征通过率 |
|---|---|---|---:|---:|---:|
| CAD Skill 复现 | Qwen 3.7 | — | 103.95M | ¥125.69 | 138/141（97.9%） |
| CAD Skill 复现 | Qwen 3.8 | 视觉 | 87.46M | ¥199.18 | 132/141（93.6%） |
| MAC v1 | Qwen 3.7 | 几何 | 0.90M | ¥9.66 | 140/141（99.3%） |
| **MAC v2** | Qwen 3.8 | 视觉 + 几何 | **1.40M** | **¥32.83** | **140/141（99.3%）** |

在 Qwen 3.7 配置下，MAC v1 相比复现的 Skill 基线减少了 116 倍记录
Token，估算成本降低了 13 倍。在加入视觉验证的 Qwen 3.8 配置下，MAC v2
仍减少了 62.7 倍记录 Token，估算成本降低了 6.1 倍。

MAC v1 指原始单零件工作流及其已发布的基准运行；MAC v2 指加入视觉验证和
独立 Judge 后的新版单零件工作流。装配体生成作为单独的技术预览，不包含在
上述基准数据中。

在两组已有配置中，MAC 均保持了 140/141 的特征通过率，同时相比对应的 Skill
复现运行显著减少了记录 Token 和估算成本。上述数字并非通用成功率，方法和
限制见[基准测试细节与适用范围](#基准测试细节与适用范围)。

## 配置与模型提供商

在 [`config.py`](config.py) 中设置 `USER_REQUEST`、提供商端点 `DS_BASE_URL` 和各阶段模型参数。API key 应放在 `DASHSCOPE_API_KEY` 环境变量中，不要提交到源码。如需恢复默认配置：

```bash
python -m multi_agent_cad._config_defaults --reset
```

模型接口兼容 OpenAI API。`SPEC_PLANNER_*`、`ARCHITECT_*`、`CODER_*` 与 `REPAIR_*` 分别配置各阶段，`AIDER_*` 控制代码修复；每阶段均可独立指定模型、温度、token 上限和额外参数。默认端点为 DashScope。更换提供商时，应同时修改 `DS_BASE_URL`、模型 ID 以及 Aider 使用的 litellm 模型名。模型 ID 会随提供商和时间变化，使用前请核对。离开 Qwen 时，还要从 `*_KWARGS` 中移除 `enable_thinking` 等提供商专属参数。

下例只说明**配置形状**，不代表针对某个提供商的实测配置：

```python
DS_BASE_URL = "https://api.example.com/v1"
SPEC_PLANNER_MODEL = "provider-model-id"
ARCHITECT_MODEL = "provider-model-id"
CODER_MODEL = "provider-model-id"
REPAIR_MODEL = "provider-model-id"
SPEC_PLANNER_KWARGS = ARCHITECT_KWARGS = CODER_KWARGS = REPAIR_KWARGS = {}
AIDER_MODEL = "provider-prefix/provider-model-id"
```

`DASHSCOPE_API_KEY` 的变量名是历史遗留名称，它承载当前所配置提供商的密钥。

## 单零件运行细节

基础启动命令见项目快速开始。CLI 运行时会流式输出 LangGraph 事件；每轮 QA
后的 10 秒检查点允许选择自动迭代（`1`）、注入新需求（`2`）或停止并保留
当前产物（`3`），超时默认自动迭代。

若希望基于现有 `temp_design*.py` 按新需求修改：

```bash
python -m multi_agent_cad.graph_aider
```

| 文件 | 用途 |
|---|---|
| `temp_design_*.py` | 生成的 build123d 源码 |
| `temp_output_*.step` / `.stl` | 可编辑或可打印的几何 |
| `temp_measurements_*.json` | 特征测量数据 |
| `temp_missed_*.json` | 运行诊断及遗漏特征信息 |

迭代后缀不一定是 `0`；请检查当前运行生成的文件，不要假设文件名固定。

### Web UI 行为

Web UI 的启动命令见根目录快速开始。界面提供配置表单、浏览器 3D 预览和
文件下载，但目前会自动迭代，不暴露 CLI 的轮间检查点。Web UI 为任务建立
独立临时目录，而 CLI 默认将 `temp_*` 产物写入仓库根目录。安全和网络监听
说明统一维护在根 README 与 [`SECURITY.md`](../SECURITY.md) 中。

## 缓存与更换提示词

`pipeline_cache/cad_brief.json` 缓存 Spec Planner 的输出；`pipeline_cache/architect_plan.json` 缓存 Geometric Architect 的输出。重跑**同一**需求时，可以复用这两阶段并重新执行代码生成和修复。

**重要：**单零件缓存只检查文件是否存在，**不会**验证当前 `USER_REQUEST` 是否与缓存一致。生成**不同**模型前，只删除这两个缓存文件：

```bash
rm pipeline_cache/cad_brief.json pipeline_cache/architect_plan.json
```

也可在 [`graph.py`](graph.py) 的 `get_default_initial_state` 中设置 `force_refresh: True`。装配体工作流采用独立的缓存与指纹规则，不要将这里的行为直接套用到装配体。

参考图可放在仓库根目录的 `user_input_images/`。Spec Planner 可以据此提取几何意图，QA Judge 可以将参考图与渲染视图对比。Web UI 任务目录没有输入图片时，会使用根目录的图片。

## 流水线与验证

```text
自然语言需求 / 参考图
  → Spec Planner（CADBrief）
  → Geometric Architect（ArchitectPlan）
  → 确定性 Python 翻译；必要时由 Aider 补足
  → 执行 build123d，导出 STEP/STL
  → 特征测量、QA Judge、有预算上限的修复循环
```

各 Agent 传递紧凑的结构化产物，而不是反复发送完整对话。[`nodes.py`](nodes.py) 中的确定性翻译器无需额外代码生成模型调用，就能处理拉伸、旋转、孔、布尔操作、阵列、镜像、圆角、倒角和抽壳等常见操作。不支持的规划步骤可能留下 `TODO_AIDER` 标记，由模型辅助补齐。

QA 过程可以审查：规划结果、生成代码、测量值、遗漏特征诊断、渲染图和修复决策均可在磁盘查看。Judge 可以接受结果、在不可能满足的需求上停止，或请求修复；多模态配置可提供渲染视图，不可用时回退至纯文本。完整阶段设计、状态定义和保护机制见 [`WORKFLOW.md`](WORKFLOW.md)。

## 基准测试细节与适用范围

[项目首页](../README_cn.md#单零件基准测试)的总指标来自单零件工作流中**10 条共享提示词、141 个评估特征**的对比测试；它**不是**装配体成功率，也不保证未见提示词的表现。逐提示词的 token/成本明细和方法见 [`qwen3.7_token.md`](../docs/qwen3.7_token.md)、[英文评估](../docs/quantified_quality.md)与[中文评估](../docs/quantified_quality_cn.md)。基准提示词源自 [earthtojake/text-to-cad](https://github.com/earthtojake/text-to-cad)；展示用提示词为本项目原创。

除非附有可复现的计时方案，否则运行耗时方面的数字仅应视为非正式观察。对于生成的几何，尤其是“一体打印即可活动”的多实体零件，仍须按目标制造工艺进行检查与验证。
