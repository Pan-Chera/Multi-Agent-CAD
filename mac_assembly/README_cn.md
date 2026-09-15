# MAC Assembly — 从自然语言生成多零件 CAD 装配体（技术预览）

> [English](README.md) · [项目首页](../README_cn.md)

`mac_assembly` 将 MAC 的[单零件工作流](../multi_agent_cad/README_cn.md)扩展到机构与可动装配体。对外输入仍然是普通的工程自然语言：用户描述零件、接口、尺寸和运动，工作流在内部创建结构化 CAD 与装配关系。

流程导出独立零件几何、装配后的 STEP/STL/GLB、URDF 交接文件、清单及 QA 文件。目标是生成可检查、可供仿真后续处理的产物；这并不意味着任意无约束提示词都能得到可制造的设计。

本模块仍是实验功能。展示内容是经筛选的产物，而不是成功率统计；项目也不以此宣称优于 CAD Agent Skill。所有几何与关节在用于制造或仿真前都应人工检查。

## 展示

### 小型功能装配体

| 铰链式双爪夹持器 | 导向直线推杆 | 旋转叉形钥匙工具 | 可复用五指手 |
|---|---|---|---|
| ![铰链式双爪夹持器](../assets/assemblies/simple/hinged-twin-claw-gripper.gif) | ![导向直线推杆](../assets/assemblies/simple/guided-linear-plunger.gif) | ![旋转叉形钥匙工具](../assets/assemblies/simple/rotary-fork-key-tool.gif) | ![可复用五指手](../assets/assemblies/simple/reusable-five-digit-hand.gif) |

这些例子使用 [`assembly_prompts/natural_language_benchmarks/`](assembly_prompts/natural_language_benchmarks/) 中详细但不依赖内部格式的自然语言需求生成。

### 复杂视觉装配体

| 三轴龙门测量单元 | 伸缩式电影机器人吊臂 |
|---|---|
| ![三轴龙门测量单元](../assets/assemblies/complex/three-axis-gantry-metrology-cell.gif) | ![伸缩式电影机器人吊臂](../assets/assemblies/complex/telescopic-cinema-robot-crane.gif) |

| 重型移动机械臂 | 高级视觉检测机械臂 |
|---|---|
| ![重型移动机械臂](../assets/assemblies/complex/heavy-duty-mobile-manipulator.gif) | ![高级视觉检测机械臂](../assets/assemblies/complex/advanced-vision-inspection-robot-arm.gif) |

以上展示用于说明生成的几何和装配结构，**不构成定量成功率声明**；可复现的装配体评估发布之前，不应由展示图片推断成功率。

## 工作流

```text
自然语言需求
      │
      ▼
拆件规划 ──► 装配关系规划 ──► 零件生成
   ▲             ▲                │
   │             │                ▼
重新拆件      重新规划配合       装配代码生成
   │             │                │
   └────── 反馈路由 ◄── 判定 ◄── 装配 QA
                    │        │
                 重建零件    └─ 修复装配脚本
```

| 阶段 | 职责 | 主要产物 |
|---|---|---|
| Decomposer | 判断需要哪些物理零件与接口 | `AssemblyBrief` |
| Mating Architect | 将接口转为锚点、偏移、限位和配合关系 | `MatingPlan` |
| PartBuilder | 生成不同零件，或复用已有几何 | 每零件 STEP/STL/Python |
| Assembler | 将装配关系确定性地转为 build123d 装配代码 | `temp_assembly_*.py` |
| AssemblyQA | 检查数量、对齐、干涉、运动扫掠和外形范围 | `AssemblyQAReport` |
| Judge | 依据 QA 证据和渲染视图决定后续操作 | 结构化决策 |
| FeedbackRouter | 将错误送到能够修复它的阶段 | 有预算上限的路由 |

前两个规划阶段将“有哪些东西”和“它们如何定位”分开。零件生成在各自独立工作目录中复用 MAC 单零件工作流。Assembler 本身是确定性的；仅在装配脚本需要修复时调用 LLM。

## 自然语言输入约定

用户提示词无需写 JSON、Python、builder 名称、选择器、schema 字段或内部 API。可以使用详细而专业的自然语言描述：

- 机构用途及大致外形范围；
- 物理零件与重复实例；
- 关键尺寸与间隙；
- 哪些部件固定、哪些部件运动；
- 铰链、滑块、圆柱、刚性连接或球铰关系；
- 期望的初始姿态与运动范围；
- 外观或功能验收要求。

Decomposer 将上述描述转换为内部 `PartSpec` 与接口对象。[自然语言基准提示词测试](../tests/test_natural_language_benchmark_prompts.py)保护这一对外输入边界。

## 零件生成路径

PartBuilder 为每个零件选择合适的生成方式：

1. **完整的单零件 MAC 工作流**：需求解析、几何规划、确定性代码翻译、执行与修复，适合自由形态几何。
2. **参数化 builder**：对常见机械几何确定性建模，不消耗生成 token。
3. **基础实体加特征**：LLM 生成主体，再确定性添加孔、叉耳、铰接凸耳、套筒、轴颈等配合特征。
4. **同形复用**：只生成一个源 STEP，再为独立安装实例复制几何。
5. **镜像复用**：在 XY、XZ 或 YZ 平面镜像源 STEP，导出真正独立、手性相反的部件，例如由左夹爪得到右夹爪。

当前 [`BUILDERS`](builders.py) 注册表含 **28** 个参数化 builder；若数量变化，以源码为准。它们针对水平孔、叉耳、连杆、板件、衬套、U 形座、桁架臂、传感器外壳等易出错结构。Builder 是内部实现选项，**不是**用户提示词必须使用的词汇。

### 复用语义

- `reuses_part_id` 指向安装实例的源几何。
- 每个实例保留独立 ID、位姿、配合关系和输出目录。
- 先生成源零件，再生成依赖实例；重建源零件会刷新依赖实例。
- `reuse_mirror_plane` 生成真实反射且手性不同的 STEP，而不是把两个同向零件假装成镜像。
- `spec_fingerprint` 在源零件规格改变时使旧复用结果失效。

## 配合关系与锚点

支持的配合语义包括 `rigid`、`face_to_face`、`coaxial`、`revolute`、`linear`、`cylindrical` 和 `ball`。

简单零件可以使用包围盒表面或主轴基准；复杂零件使用基于真实 STEP 拓扑解析的语义选择器，例如“靠近指定位置、半径符合要求的圆柱面”。解析后的数值选择结果记录在 `assembly_selector_audit.json`。

对于生成的叉耳特征，`direction` 表示特征延伸方向，`pin_axis` 表示铰链孔轴线，两者不能混淆。平面附着选择器还用 `surface_axis` 记录所选面的法向。显式区分这些轴，能避免将正确的特征装到错误的运动平面。

生成的 manifest 记录每个零件完整的位姿变换。QA 利用该变换把局部基准和轴线映射到世界坐标，这对链式连接及旋转关节尤为重要。如果缺失变换，会报告“无法验证”，而不会仅凭世界坐标轴对齐的包围盒猜测姿态。

## 验证与反馈

AssemblyQA 尽可能进行确定性检查，包括：

- 实际零件数量与标识；
- 配合轴与基准对齐；
- 最小间隙与考虑深度的干涉；
- 旋转及直线运动扫掠；
- 装配外形范围与尺寸协调；
- 所需输出文件是否存在。

Judge 读取结构化证据及可选渲染视图。几何 QA 与视觉语义验证分别记录：只有 Judge 实际看过当前模型的渲染图时才会标记为 `verified` 或 `failed`；API 对应模型不支持图像时会安全降级为 `unverified`，不会阻塞几何上有效的产物。发现可见语义错误时，Judge 会输出局部、可执行的修改建议。路由器将问题分配给有预算上限的四类反馈回路：

| 问题归属 | 路由 | 典型例子 |
|---|---|---|
| 拆件 | 重新拆件（recompose） | 零件错误或缺失接口 |
| 配合规划 | 重新规划配合（remate） | 锚点、姿态或运动关系错误 |
| 零件几何 | 重建零件（remodel） | STEP 缺失必需特征 |
| 装配脚本 | 修复装配脚本（repair assembly） | 执行错误或确定性代码缺陷 |

路由之前会在当前 STEP 上探测选择器命中情况，避免把规划凭空指定不存在的几何，与实际缺失几何混为一谈。认证错误、缺失依赖、重复且无进展的超时以及预算耗尽会以明确的未完成状态停止，而不是无限重试。

如果零件耗尽生成次数但仍留下可用 STEP，可以作为 `degraded` 结果继续参与装配，而不会被悄悄标成干净成功。其 ID 和告警会传入 QA 与 Judge，后者可选择接受、定向重建或停止交付。

## 快速开始

先按[项目首页](../README_cn.md#快速开始)从仓库根目录安装，再通过环境变量设置密钥：

```bash
export DASHSCOPE_API_KEY="your-key"
```

运行附带的自然语言需求：

```bash
MAC_ASSEMBLY_REQUEST="$(cat mac_assembly/assembly_prompts/natural_language_benchmarks/01_hinged_twin_claw_gripper.md)" \
python -m mac_assembly
```

或直接输入需求：

```bash
MAC_ASSEMBLY_REQUEST="Create a compact hinged inspection fixture with a fixed base and one rotating sensor bracket." \
python -m mac_assembly
```

在原有目录中续跑：

```bash
MAC_ASSEMBLY_WORK_DIR="assembly_jobs/job_YYYYMMDD_HHMMSS_00" \
MAC_ASSEMBLY_REQUEST="$(cat path/to/the/original_request.md)" \
python -m mac_assembly
```

装配 Agent、重试预算、容差和子进程超时配置位于 [`config_assembly.py`](config_assembly.py)；提供商及单零件模型配置位于 [`../multi_agent_cad/config.py`](../multi_agent_cad/config.py)。

## 输出目录

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

具体文件名会随迭代和交接阶段变化；manifest 与日志才是单次运行实际产物的权威记录。

## 缓存与恢复

- 拆件结果与配合方案分别缓存。
- 每个零件都有独立的规划缓存和 token 账本。
- 规格变化时，内容指纹会使旧零件结果失效。
- 重建失败不能驱逐同规格下较早的可用结果。
- 若需求与源几何指纹一致，未完成的生成脚本可续跑一次。
- 有毒或不可读取的中间 STEP 会被移除，不会被重复回放。
- 同规格连续两次超时，且没有新计划、脚本、STEP 或 STL 时，会暂停该零件；排查后可由操作者显式重试。

## 已知限制

- 零件生成目前是串行的。
- 装配体工作流尚无专用浏览器界面；可用外部查看器或 Blender 检查 GLB/STEP。
- 暂不集成商业标准件目录与下载。
- `face_to_face` 仅支持既定的定位约定；其他姿态应使用适当的轴类或刚性配合。
- 干涉与运动检查依赖声明的几何依赖包；缺少 containment 支持时，启动预检不会允许宣称“已验证通过”。
- URDF 属于文件级交接；动力学、执行器参数及特定任务的仿真验证仍需在下游完成。
- 复杂自由形态零件仍可能失败或需要进一步修改提示词。展示案例不代表任意提示词都能成功。

## 测试

在仓库根目录运行：

```bash
.venv_311/bin/python -m pytest tests/ -q
```

针对公开输入和文档的测试：

```bash
.venv_311/bin/python -m pytest \
  tests/test_natural_language_benchmark_prompts.py \
  tests/test_decomposer_prompt_sync.py -q
```

## 许可证与致谢

本项目使用 [MIT 许可证](../LICENSE)。装配体生成使用 build123d 和仓库内的 `cadpy` 装配运行时；后者源自 [earthtojake/text-to-cad](https://github.com/earthtojake/text-to-cad)，遵循其原始 MIT 许可证。
