# mac_assembly -- 多 Agent 装配流水线

在 MAC（多 Agent 单零件 CAD）之上扩展装配能力：**多 agent 拆分整个装配体任务，
每个零件独立走原有 MAC 流水线生成，再确定性组装成装配体 STEP，闭环 QA 反复迭代**。

设计原则：思想上和代码上最大化复用 [earthtojake/text-to-cad](https://github.com/earthtojake/text-to-cad)
（CAD Skills）与本项目（MAC）的现成件，不闭门造车。

## 架构

```
decomposer ──> mating_architect ──> part_builder ──> assembler ──> assembly_qa ──> judge
   (what:          (how: mates[]        (MAC 黑盒)     (确定性)                    │
   parts+接口语义    anchor/offset/tol)                                            v
     ^    ^                                                                        │
     │    └──── remate（mate 设计错，带 QA delta 反馈）<── feedback_router <────────┘
     └──────── recompose（拆分本身错）                     │  │  │
                       remodel_parts（零件几何错）→ part_builder；repair_assembly（脚本级缺陷）→ assembler
                       END（accept / halt / 预算耗尽）
```

Decomposer 与 Mating Architect 的拆分对标 MAC 单零件侧的 Spec Planner /
Geometric Architect 分工（"what" 与 "how" 分离，agent 之间只传结构化
JSON），两级之间有确定性校验层 `_validate_mating_plan`（引用存在性、
单主 mate、固定根唯一、接口全覆盖），错误归因因此可以路由到正确的
阶段：recompose / remate / remodel / repair 四条反馈回路各管一层。

| 阶段 | Agent | 复用来源 | 说明 |
|---|---|---|---|
| 1 | **Decomposer** | MAC `_llm_client` + `_call_llm_json_with_retry` + `image_preprocess` | 装配请求 -> `AssemblyBrief`（parts + 功能接口散文 + 包络 + 意图） |
| 1b | **Mating Architect** | positioning.md few-shot | 接口 -> `MatingPlan`（结构化 MateSpec：anchor/offset/tolerance） |
| 2/3 | **PartBuilder** | MAC 单零件流水线**零改动**（`_part_runner` 子进程 + per-part cwd/缓存隔离） | 每个零件独立跑 Spec Planner -> Architect -> 确定性 Coder -> Skill Loop |
| 4a | **Assembler** | CAD Skills `cadpy.assembly.AssemblyHelper` + positioning.md 哲学 | **确定性翻译器** `assembly_codegen`：MateSpec -> `asm.add/rigid_frame/face_to_face/revolute` 源码，零 token；LLM 仅在脚本失败时修复 |
| 4b | **AssemblyQA** | inspection-and-validation.md 哲学 + MAC render_views | 零件计数 / mate 对齐 delta / 双向干涉（射线法）/ **revolute ±30° 扫角运动学碰撞** / 包络 |
| 5 | **Assembly Judge** | MAC QA-Judge 反幻觉模式 | accept / remate / repair_assembly / remodel_parts / recompose / halt + evidence gate + 多模态 |
| 6 | **FeedbackRouter** | repair-loop.md 失败分类 | 四条回路 + 预算封顶（外层 8 轮默认 / remate 4 次 / recompose 2 次） |

## 反馈路由策略（FeedbackRouter 决策表）

`node_feedback_router` 是**节点**（非条件边）—— 它根据 QA report + Judge decision 改写
状态（`remodel_part_ids` / `repair_context` / `__next__`），再由条件边
`route_after_feedback` 按 `__next__` 路由。决策优先级
（[graph_assembly.py:60-143](mac_assembly/graph_assembly.py#L60-L143)）：

1. QA 全绿或 Judge `ACCEPT` / `HALT` → END
2. 外层 `ASSEMBLY_MAX_ITERATIONS` 耗尽 → END
3. Judge `REMODEL_PARTS` → part_builder（带 `remodel_part_ids`）
4. Judge `RECOMPOSE` → decomposer（受 `DECOMPOSER_MAX_RUNS` 限制）
5. QA `PART_MISSING` → part_builder（自动重试失败零件）
6. Judge `REMATE` 或 QA mate-level 错误 → mating_architect（受 `MATING_MAX_RUNS` 限制）
7. 默认（含 Judge `REPAIR_ASSEMBLY`、`FATAL`）→ assembler

| QA `error_type` | 含义 | 默认路由 |
|---|---|---|
| `PART_MISSING` | 零件生成失败 | part_builder（remodel） |
| `MATE_MISALIGNMENT` / `INTERFERENCE` / `KINEMATIC` / `ENVELOPE` / `RECONCILE` | mate 设计错 | mating_architect（remate） |
| `FATAL` | 脚本执行失败 / STEP 缺失 | assembler（repair_assembly） |
| `NONE` | 全绿 | END |

**预算绑定**：`MATING_MAX_RUNS=4` 和 `DECOMPOSER_MAX_RUNS=2` 在外层
`ASSEMBLY_MAX_ITERATIONS=8`（默认；`MAC_ASSEMBLY_MAX_ITER` 环境变量可覆盖）之前绑定
（per-route 优先）；外层 8 次是兜底，覆盖无 per-route 预算的 `repair_assembly` 路径，
并为复杂多零件装配（灵巧手等）留余地。设 `ASSEMBLY_MAX_ITERATIONS<4` 会让 remate
预算成死代码。

## 装配哲学（复用 skill 的 positioning.md）

- **定位写在源码里**：组装产物是 `temp_assembly_N.py`（AssemblyHelper + 原生
  build123d joints），不是对导出 STEP 的补丁；修复永远回到源码。
- **mate/joint 驱动，非裸变换**：`MateSpec` 是语义关系（face_to_face /
  coaxial / rigid / revolute），fixed-first 方向性贯穿始终。
- **每个 anchor 是命名 datum**（bbox 面心 / 轴向点），因此 MateSpec ->
  AssemblyHelper 的翻译是确定性的——这是 MAC `_plan_to_code` 在装配层的对应物。
- **确定性检查定 pass/fail，视觉只做诊断**：渲染视图喂 Judge 校准，
  量测数据冲突时信任量测（snapshot-review.md 原则）。

## Anchor 与 Mate 语义（数据契约）

**Anchor 双轨**（[schemas_assembly.py:135-172](mac_assembly/schemas_assembly.py#L135-L172)）：

| `kind` | 用法 | 何时用 |
|---|---|---|
| `face` | bbox 面心（top/bottom/left/right/front/back） | 简单价/盒件，bbox 面就是配合面 |
| `axis_point` | bbox 中心 + 沿 x/y/z 偏移 `offset_mm` | 轴上特定点（through-bore 中点） |
| `selector` | 语义面查询（`surface=plane/cylinder` + `axis` + `normal_sign` + `select=largest/closest_to` + 可选 `target_x/y/z_mm`），零件生成后由 cadpy `SelectorIndex` 解析到真实拓扑面 | 复杂件（裙边盖、多 bore 件、knuckle ear）—— bbox 面不是配合面 |
| `sphere` | 球心 (3 coords) + 半径 `sphere_radius_mm` | 球铰 anchor——球头 / 球窝的球心定位 |

SELECTOR 的 `target_x_mm` / `target_y_mm` / `target_z_mm` 用于多 bore 件（如 `link_bar`
两端同半径 bore）按位置消歧；解析结果写入 `assembly_selector_audit.json` 供审计。

**MateType**（[schemas_assembly.py:30-38](mac_assembly/schemas_assembly.py#L30-L38)）：
`rigid` / `face_to_face` / `coaxial` / `revolute` / `linear` / `cylindrical` / `ball`，1:1 映射到
`AssemblyHelper` 方法（`ball` 用 `asm.ball()`，2-DOF pitch+yaw，roll=0）。`fixed_part_id` 永远不动；`_toposort_mates` 保证 fixed 先 settle
再 anchor children（[assembly_codegen.py:246-268](mac_assembly/assembly_codegen.py#L246-L268)）。

**`axial_offset_mm`**（revolute/coaxial/rigid 用）：沿 anchor 方向（旋转轴 / 面法向 /
SELECTOR cylinder 轴）的带符号偏移，让 moving part 落到指定轴向位置而非 fixed anchor
解析点。典型用法：base post R6 + arm R6.2 bore，两 anchor 都解析到 cylinder 中点，无 offset
则 arm 浮在 post 中点；加 `axial_offset_mm` 让 arm 底面落到 plate 顶面。详见
`prompts/mating_architect.md` 的两个 worked example。

**确定性 override**（[assembly_codegen.py:433-548](mac_assembly/assembly_codegen.py#L433-L548)）：
`postprocess_axial_offsets` 在 LLM 写完 plan 后，对 **任意主轴（x/y/z）revolute** mate 从
Decomposer 散文里解析该轴范围（Iron Rule 6），确定性重算 `axial_offset_mm`，绕过 LLM 算错
JSON 数字的常见问题。两个 anchor 必须引用同一主轴（SELECTOR cylinder `axis=<x|y|z>`、
AXIS_POINT 小 offset、或 FACE 法向沿该轴）。per-axis 链式 tracking 让 mixed-axis 链
（shoulder yaw Z + elbow pitch X）在 static pose 是纯平移时正确工作；coplanar guard
跳过同层 clevis 链（overlap > 50% 小者 extent 时不 override，保留 LLM 的 `axial_offset_mm=0`）。

## 快速开始

```bash
export DASHSCOPE_API_KEY="sk-..."        # 或写入 multi_agent_cad/config.py 的 DS_API_KEY
python -m mac_assembly                    # 默认请求：两件式旋盖盒
MAC_ASSEMBLY_REQUEST="Create a planetary gear assembly ..." python -m mac_assembly
```

产物在 `assembly_jobs/job_<timestamp>/`：

```
assembly_cache/assembly_brief.json     # Decomposer 产物（what，可审计）
assembly_cache/mating_plan.json        # Mating Architect 产物（how，可单独重算）
parts/<part_id>/temp_output_*.step     # 各零件 STEP（各自跑完整 MAC 流水线）
parts/<part_id>/temp_design_*.py       # 各零件 build123d 源码
parts/<part_id>/part_log.txt           # 单零件流水线日志
temp_assembly_N.py                     # 确定性生成的装配源码（可修复）
assembly_output.step / .stl            # 最终装配体
assembly_manifest.json                 # 放置后各零件 bbox（QA 反查标签）
assembly_judge_views/                  # Judge 看的 4 张等轴测渲染
assembly_log.txt                       # 装配脚本执行日志
```

## 与 skill / MAC 的复用清单

| 复用件 | 来源 | 用法 |
|---|---|---|
| `cadpy.assembly.AssemblyHelper`（5 个装配模块） | CAD Skills（已 vendor 于 `packages/cadpy`） | 装配核心，零改动 import |
| positioning.md 模式 | CAD Skills | anchors/joints/mate 语义 + assembler/repair prompt few-shot |
| repair-loop.md 失败分类 | CAD Skills | FeedbackRouter 的路由策略 |
| inspection-and-validation.md | CAD Skills | QA 分层（确定性权威 / 视觉诊断） |
| 单零件 4-agent 流水线 | MAC（本项目） | PartBuilder 经 `_part_runner` 子进程完整复用 |
| `_llm_client` / `_call_llm_json_with_retry` / token_tracker | MAC | 所有 LLM 调用与计费 |
| `render_views._render_isometric_views` | MAC | Judge 的多模态视图 |
| QA-Judge 反幻觉 5 层防御模式 | MAC | Judge prompt + schema evidence + code gate |

## 新写代码（其余全部复用）

- `schemas_assembly.py` -- AssemblyBrief / MatingPlan / PartSpec / MateSpec /
  Anchor / FunctionalInterface / AssemblyQAReport / KinematicCheck /
  InterferenceCheck(min_gap) / AssemblyJudgeDecision（Pydantic）
- `assembly_codegen.py` -- 确定性组装翻译器（MateSpec -> AssemblyHelper 源码；
  拓扑排序执行；rigid/face_to_face/coaxial/revolute/linear/cylindrical 六类
  关节按 build123d 语义配对）
- `assembly_qa.py` -- 闭环检测（mate delta + **真实拓扑 min-gap** /
  **深度容差干涉**（接触不算碰撞）/ revolute 扫角 + linear/cylindrical 平移
  扫掠运动学 / 计数 / 包络 / 尺寸对账；组件-标签"包络包含优先"匹配）
- `part_generator.py` + `_part_runner.py` -- 单零件封装（cwd + 缓存隔离；
  `--mode full` 走 `multi_agent_cad.graph` 从零生成，`--mode aider` 走
  `graph_aider` 对既有 `temp_design*.py` 打 Aider 补丁）
- `handoff.py` -- GLB + URDF 导出 + snapshot 包 + handoff manifest
- `selector_resolver.py` -- SELECTOR anchor 解析器（语义面查询 -> cadpy
  SelectorIndex 真实面 + 数字选择子审计；mtime/size 缓存键防陈旧拓扑）
- `nodes_assembly.py` / `graph_assembly.py` -- 7 节点 LangGraph + 四条反馈
  路由（recompose / remate / remodel / repair）+ token 端到端聚合
- `prompts/` -- decomposer / mating_architect / assembly_repair / assembly_judge

## Builder 库（零 token 参数化几何）

[builders.py](mac_assembly/builders.py) 提供 16 个参数化 builder，覆盖 qwen3.8-max 容易
写错的几何（水平圆柱、knuckle ear、clevis fork 等）。Decomposer 在 `PartSpec.builder`
里指定 `{"name": <builder>, "params": {...}}`，PartBuilder 直接调用，绕过 LLM 单零件流水线。

| `name` | 用途 | 关键参数 |
|---|---|---|
| `knuckle_hinge_ear` | 立板 + 水平圆柱 ear（水平 through-bore） | `plate_w, plate_h, plate_t, ear_radius, ear_length, bore_radius, ear_z, ear_at_plus_x` |
| `shaft_with_arm` | rod（LOCAL Z）+ 垂直臂（让旋转可见） | `rod_radius, rod_length, arm_w, arm_h, arm_d, arm_offset_*` |
| `pivot_post` | plate + 顶面立柱（revolute pivot base） | `plate_w, plate_d, plate_t, post_radius, post_height, post_x, post_y` |
| `link_bar` | 两端 through-hole 的连杆 | `length, width, thickness, bore_radius, hole_offset` |
| `fork_end` | +X 端双 ear fork（clevis / rod end） | `bar_length, bar_width, bar_thickness, ear_length, ear_spacing, bore_radius, bore_axis` |
| `mounting_plate` | 4 角 + 可选中心 hole 的 plate | `width, depth, thickness, hole_radius, hole_dx, hole_dy, central_hole_radius` |
| `hollow_box` | 敞口盒（外壳 / 齿轮箱） | `outer_w, outer_d, outer_h, wall_t, floor_t` |
| `lid` | 平盖（可选 4 角 hole） | `width, depth, thickness, hole_radius, hole_dx, hole_dy` |
| `bracket_L` | L 形支架 | `plate_w, plate_h, plate_t, hole_radius, wall_holes, foot_holes` |
| `standoff` | 圆柱间隔件（可选 through-bore） | `radius, height, bore_radius` |
| `bushing` | 套筒（bore 沿 LOCAL Z，revolute 对齐用） | `outer_radius, inner_radius, length` |
| `gusset` | 直角三角形加强筋 | `side_a, side_b, thickness` |
| `clevis_link` | 两端 clevis tongue/fork 的连杆（平面运动链 link） | `bar_length, bar_width, bar_thickness, bore_radius, ear_length, ear_width, tongue_thickness, clearance_side, minus_x_end, plus_x_end` |
| `clevis_base_with_fork` | plate + 指定位置 clevis fork（运动链 base / 单 fork palm） | `plate_w, plate_d, plate_t, fork_x, fork_y, ear_length, ear_width, tongue_thickness, bore_radius, clearance_side, fork_direction` |
| `clevis_palm` | plate + 多个 clevis fork（多指 palm / 多链 base） | `plate_w, plate_d, plate_t, ear_length, ear_width, tongue_thickness, bore_radius, forks=[{fork_x, fork_y, fork_direction}, ...], clearance_side` |
| `ball_joint_socket` | 球铰部件（`role=socket` 球窝 / `role=ball` 球头，2-DOF 球面配合） | `role, sphere_radius, sphere_center_mm, socket_housing_w/d/h, socket_wall_t, socket_opening_radius, ball_stem_radius, ball_stem_length, ball_stem_direction` |

**设计约定**：带孔件（`knuckle_hinge_ear` / `link_bar` / `fork_end` / `bushing` / `clevis_*`）
的 bore 沿 LOCAL Z，让 revolute mate 把 moving part 的 LOCAL Z 对齐到旋转轴。
`shaft_with_arm` 的 rod 沿 LOCAL Z（不是 X），同因。bore 都 overshoot 1mm 避免 boolean
coincident-face kernel failure。

## Part Reuse（零 token 零件复用）

对于**完全相同**的重复零件（4x M3 螺丝、3x 行星齿轮、多指手的多个相同指节），
Decomposer 可以 emit 一份带完整描述的 source PartSpec + N-1 份带 `reuses_part_id`
指向 source 的 instance PartSpec。PartBuilder 只跑一次 MAC 流水线生成 template，
然后把 template 的 STEP/STL/py 复制到每个 instance 的 `parts/<instance_id>/` 目录。
每个 instance 仍然是装配体里独立的 labeled solid（独立 placement、独立 mate），
只是几何来自同一份 template。零 LLM token / instance，几何保证完全一致。

| 字段 | 用法 |
|---|---|
| `PartSpec.reuses_part_id` | 指向 template 的 `part_id`；instance 跳过 MAC 流水线，直接复制 template 的 STEP/STL/py |
| 互斥约束 | `builder` 和 `reuses_part_id` 不能同时设；一个零件要么调 builder 要么复用 template（validator 拒绝） |
| 链式禁止 | `reuses_part_id` 必须指向非复用 PartSpec（A reuses B reuses C 不允许；validator 拒绝） |
| 自反禁止 | `reuses_part_id` 不能等于自身 `part_id`（validator 拒绝） |
| instance description | 仍必须说明该 instance 在装配体中的安装位置/朝向（如「front-left 角，head up」），让 Mating Architect 推导正确的 mate |
| remodel 路由 | 任一 instance 进 `remodel_part_ids` → template 自动进（几何共享）；template remodeled 后所有 instance 自动重复制（防 STEP 文件陈旧漂移） |
| 排序保证 | `node_part_builder` 把 templates 排在 instances 之前处理（two-bucket 稳定排序），保证 template 已生成时 instance 才尝试复制 |

**Token 节省示例**：4 个相同螺丝 = 1× MAC 流水线 + 3× 文件复制（≈1× 而非 4× token 成本）。
灵巧手 15 关节场景（30 clevis fork + implicit pin），平均 2× 复用意味着从 60 次零件生成降到
~30 次——这是 mac_assembly 在多关节手上能否落地的关键优化。

## v3 Feature-based parts（LLM base + 可靠 feature）

v2 builder 把 base body + kinematic feature 耦合在一个 generator 里——
要么用 `clevis_palm` builder（有可靠 fork 但 plate 形状固定为矩形），
要么 LLM 自由生成（形状自由但 fork 不可靠）。v3 解耦：

- `base_body` (LLM Coder agent 生成自由形状的 base body，含 attach-point hint)
- `features` (确定性 feature operator 在指定 attach point 贴装 kinematic feature)

PartBuilder 跑一次 MAC 流水线生成 base body STEP，然后用 `import_step`
加载、依次应用每个 feature operator、导出最终 STEP。`run_part_with_features`
内部有 **feature-only remodel fast path**：若 remodel 时 `base_body.description`
与上次缓存的 `temp_v3_spec.json` 里的 description 字节级相同，跳过 MAC Coder
（0 LLM token），直接复用 `temp_output_base.step` 重跑 feature 链。

| 字段 | 用法 |
|---|---|
| `PartSpec.base_body` | `BaseBodySpec`：`description` (CAD prompt，含 attach-point hint) + `key_dimensions` |
| `PartSpec.features` | `list[Feature]`：每个 Feature = `name` (operator) + `params` + `attachment` (attach_point_mm + direction) |
| 互斥约束 | `builder` / `base_body` / `reuses_part_id` 三选一（validator 拒绝多选） |
| features gating | `features` 非空要求 `base_body` 非空（feature 必须贴在 base body 上） |
| 失败回退 | v3 失败 **不 fall-through 到 LLM Full Regen**——避免重蹈 LLM 写错水平圆柱的覆辙。返回 `ok=False` + 诊断，交 FeedbackRouter / Judge 决定 REMODEL_PARTS / RECOMPOSE / HALT |
| 0.2mm overshoot | 加法算子（clevis_fork / clevis_tongue / knuckle_ear / ball_stem）沿贴装法向反方向内嵌 0.2mm，避免 OpenCASCADE 共面布尔 union 数值崩溃 |
| snap-to-surface | attach_point 与 base 实际表面偏差 >0.5mm 时，沿 -direction snap 到最近表面；若法向偏 >15° 回退原坐标 + warning（保护 planar kinematics） |
| 多 solid fallback | base 是 Compound 多 solid 时直接 `base + feature` 会触发 `BRep_API` —— `apply_feature` 自动 fallback 到 per-solid fuse，最后 disjoint solid 兜底 |

**6 个 feature operator**：`clevis_fork` / `clevis_tongue` / `through_bore` /
`ball_cavity` / `ball_stem` / `knuckle_ear`。详见
[feature_operators.py](mac_assembly/feature_operators.py)。

**Token 节省**：v3 = 1× MAC Coder call（base body）+ 0 per feature operator。
LLM 全自由生成 = 1× MAC Coder call 写整个 part（含 fork / cavity 等难特征），
失败率高、重生成成本大。v3 把难特征从 LLM 剥离到确定性算子。

**SELECTOR 消歧**：v3 part 有多个相同 kinematic feature（如 5 个 clevis_fork
同 bore_radius）时，SELECTOR anchor 必须带 `target_x_mm` + `target_y_mm`
（指向对应 feature 的 `attach_point_mm`），否则 5 个 mate 全部解析到第一个
cylinder → 5 根手指堆叠到一个根部。`_validate_mating_plan` 强校验。

## Part 生成的 5 条路径

`node_part_builder` 根据零件类型和反馈状态选 5 条路径之一
（[part_generator.py](mac_assembly/part_generator.py)）：

| 路径 | 触发条件 | 函数 | LLM token |
|---|---|---|---|
| **v3 base+features** | `PartSpec.base_body` 指定 + `features` 非空 | `run_part_with_features` | 1× MAC Coder（base body）+ 0 per feature |
| **Builder 直生** | `PartSpec.builder` 指定 + 无 remodel 反馈 | `run_part_builder` | 0 |
| **Builder remodel** | `PartSpec.builder` 指定 + 有 QA 反馈 | `run_part_builder_remodel` | 1 次小 call（用 `MATING_MODEL` 调参） |
| **Aider remodel** | 无 builder + 有反馈 + 已有 `temp_design*.py` | `run_part_remodel` | Aider 补丁（保留已验证特征） |
| **完整重生成** | 无 builder + 反馈失败 / 首次生成 | `run_part` | 完整 MAC 单零件流水线 |

v3 path 失败 **不 fallback**——返回 `ok=False` + 诊断交 FeedbackRouter。其他两条 remodel 路径
失败都 fallback 到完整重生成。Aider remodel 走 MAC `graph_aider`，
保留已验证特征省 token；builder remodel 因为 `temp_design_builder.py` 是非执行 audit 文件
不能 Aider，所以走 LLM 调参 + 重 call builder。

## Clevis 平面运动链（tongue & groove 铰链）

灵巧手 / 多关节机构的常见模式：base → link1 → link2 → ... 平面 revolute 链。每个关节是
**tongue-fork 对**（一凸一凹，不能 tongue-tongue 或 fork-fork）。

- **Pin 轴 = Z（竖直）；fork slot 沿 Z 方向开**（厚度方向）。关键：Y 方向 slot 会让 tongue
  一旋转就撞 fork ear。fork 上 ear 占顶部 Z 带，下 ear 占底部 Z 带，中间空 Z slot 接收
  mating tongue（mid-Z slab）。所有特征以 Z = `bar_thickness/2` 为中心。
- **隐式 kinematic mate**：bore 共轴 + Z 方向 clearance 实现 revolute 约束，**不建模实体 pin**
  （实体 pin 会与 tongue/fork 干涉；URDF 导出时用 bore 共轴作为 revolute joint axis，
  无需 pin 实体——详见 [handoff.py](mac_assembly/handoff.py) + [urdf_export.py](mac_assembly/urdf_export.py)）。
- 链指派：
  - **base**：`clevis_base_with_fork`（plate + fork；`(fork_x, fork_y)` 是 bore 中心绝对
    坐标，必须在 `fork_direction` 上凸出 plate 边）
  - **中间 link**：`clevis_link(minus_x_end="tongue", plus_x_end="fork")`
  - **末端 link**：`clevis_link(minus_x_end="tongue", plus_x_end="plain")`
- 所有 link 共享 `bar_thickness`（同 Z 层），**并排**，不竖直堆叠。
- `bore_radius = pin_radius + 0.2mm`（pin 隐式）。
- 几何约束（builder 强制，[builders.py:594-644](mac_assembly/builders.py#L594-L644)）：
  `ear_width ≥ 2*(bore_radius + 1.0mm)`、`ear_length ≥ ear_width + clearance_side`、
  `tongue_thickness < bar_thickness - 2*clearance_side - 1.0mm`。

**mate 模式**：每个关节是 `revolute` mate，fixed/moving anchor 都是 SELECTOR cylinder
axis=z，分别指向 fork bore 和 tongue bore。`axial_offset_mm = 0`（同层不堆叠）。
`target_x_mm` 消歧 ±X 端 bore。**禁用 `face_to_face`**（那会堆叠，平面链应是单 Z 带）。

**多指 palm**：`clevis_base_with_fork` 一次只放一个 fork，适合单链 base；多指手 palm
用 `clevis_palm` builder（[builders.py:977](mac_assembly/builders.py#L977)），`forks` 参数
是 `[{fork_x, fork_y, fork_direction}, ...]` 列表，所有 fork 共享 `ear_length/ear_width/bore_radius`
（uniform fingers）。builder 强制两约束：(1) 每个 bore 必须在 `fork_direction` 上凸出 plate 边；
(2) 任意两个 fork 的 ear body 不得 AABB 重叠（lateral spacing ≥ `ear_width`），否则 boolean
union 会合并 Z slot 破坏 clevis 运动学。

## 装配脚本输出（codegen footer）

[assembly_codegen.py](mac_assembly/assembly_codegen.py) 生成的 `temp_assembly_N.py` 在
`asm.build()` 之后除了 STEP/STL 还写：

- **`assembly_manifest.json`**：每零件 placed bbox + 完整 `location`（translation +
  rotation_euler_xyz_deg）。QA 用 location 把 part-local SELECTOR 点经完整 4x4 变换投影
  到世界坐标——支持 chained revolute（mate N 的 fixed part 自身被 mate N-1 旋转过）。
- **`placed_stl/<label>.stl`**：每零件 placed mesh。精确接触装配在 compound STL 里会合并
  成一个 shell，trimesh split 不可靠；per-part STL 让 QA 干净地按零件取 mesh（kinematic
  sweep / 干涉 / min-gap 都用它）。
- **`assembly_selector_audit.json`**：每个 SELECTOR anchor 解析到的 cadpy 数字选择子
  （如 `f5` / `o1.f5`），语义+数字混合审计。
- **`assembly_mates.json`**：`AssemblyHelper.MateRelation` 列表（label / relation /
  fixed / moving / parameters）。

## 配置

`mac_assembly/config_assembly.py`：Decomposer / Repair / Judge 各自的模型、
温度、thinking 开关（混合路由）；闭环预算（外层迭代、per-part 重试、
recompose 上限）；QA 公差（包络 ±%、干涉体积/比例）；子进程超时。

## 已知边界（v1）

- 零件**串行**生成（并行化是机械改动：per-part 目录已隔离）。
- **市售件（step-parts 检索）是明确的 v1 非目标**（用户决定不做）：没有
  downloader、`PartSpec` 也没有 catalog 字段。需要时再整体接入。
- `face_to_face` 的 offset 沿世界 +Z（固定端 joint 局部 Z）。`_validate_mating_plan`
  硬性要求 fixed 面为 `top`、moving 面为 `bottom`（否则装配语义不成立）。
- **anchor 双轨**：`kind=face/axis_point` 按 bbox 定位（简单件够用）；复杂件
  用 **`kind=selector`** --语义面查询（面类型/法向/选取准则），零件生成后经
  cadpy SelectorIndex 解析到**真实拓扑面**，裙边/旋钮类零件不再错位；解析出
  的数字选择子写入 `assembly_selector_audit.json` 供审计（语义+数字混合）。
- 干涉判定为**带深度容差**的表面采样（`contains` 判内外 + `closest_point`
  测穿透深度）：面接触/贴合（signed gap≈0）**不算**干涉，只有穿透超过
  `INTERFERENCE_DEPTH_TOL_MM`（默认 0.3mm）才报。装 `rtree` 后精度最佳。
- linear/cylindrical mate：QA 沿 `slide_axis` 做平移扫掠（±`LINEAR_SWEEP_MM`），
  另有一个"滑动基准点应靠近被放置零件"的基本结构校验（运动学关闭时也不静默
  全绿）；cylindrical 的静态旋转 pose 经 `angle_deg % 360` 生效。
- **handoff 交付的是 GLB + URDF + snapshot 包 + manifest**（文件级交付；URDF 供抓取
  仿真器 PyBullet / MuJoCo / MoveIt 用）；没有实际启动 CAD Viewer 服务/返回
  可点链接（那是 cad-viewer skill 的 npm 运行时，未接入）。

### 未闭环项（后续可选，未实现）

- **CAD Viewer 实际启动**：调 cad-viewer skill 的 `npm run agent:start` 返回
  `?dir=&file=` 可点链接（当前仅文件级 handoff）。
- 尺寸对账的水平向堆叠检查（当前 face_to_face 语义本身只支持 +Z 堆叠；
  轴孔半径相容性检查已实现）。

## 验证状态（离线，无 LLM）

| 用例 | 结果 |
|---|---|
| 盒+盖 face_to_face | 全绿：拓扑 gap 0.200mm、包络 60x60x49.2、min-gap 0.20mm |
| 裙边盖（bbox 底面≠配合面） | **拓扑法测得 0.200mm**（bbox 会误算成 -5） |
| 干涉负用例（盖嵌入盒） | `INTERFERENCE` 检出（80% 深度穿透） |
| 滑块坐在导轨上（面接触） | 全绿：深度容差正确放行接触，linear 扫掠无碰撞 |
| linear / cylindrical mate | codegen 正确放置（滑轨 position、销钉同轴） |
| 3 件链 base→mid→top（mate id 反字母序） | **拓扑排序生效**：top 落在 20..26 |
| 运动学负用例（挡块在 -30° 扫掠路径） | 仅 -30° 检出碰撞 -> `KINEMATIC` |
| 运动学正用例（无挡块） | ±30° 全扫无碰撞 |
| 尺寸对账：链高 vs 包络 z | 一致 -> 空；人为改包络 -> 检出 `RECONCILE` |
| `_validate_mating_plan`：环 / 多根 / face_to_face 错误面 / 缺 slide_axis | 全部检出 |
| **SELECTOR 裙边盖闭环**（语义面查询 -> cadpy 真实面） | 配合面解析到真实 plate bottom（f5, z=35.2），bbox 法会错 5.2mm；放置后拓扑 gap=0.200、min-gap=0.2 全绿 |
| R6 轴孔半径对账 | R2 柱 vs R5.2 孔 -> 检出不兼容；R5 柱 vs R5.2 孔 -> 通过 |
| **N1 链深≥2 + 中间固定件 SELECTOR anchor** | 修复前 top 嵌入 mid 10mm；修复后 base(0,10) mid(10,20) top(20,26)，QA 全绿（接触 3 对全过） |
| 精确面接触装配的 per-part placed STL | 装配脚本按零件导出放置后网格，QA 优先读取，绕开接触合并/networkx 网格分裂不可靠问题 |
| **N2 SELECTOR anchor + revolute mate** | 修复前 QA 崩溃（KeyError: None）/扫错轴；修复后圆柱 SELECTOR 取面轴中点作基准、扫真实轴，碰撞结果与 bbox anchor 完全一致（[-30]） |
| handoff：GLB + URDF 导出 + snapshot + manifest | GLB/URDF/STEP/4 PNG/manifest 全部产出 |

> 功能扩展轮（对应审查报告"三、skill 已有资产复用"）：linear/cylindrical
> mate + 平移扫掠、真实拓扑 min-gap（face_to_face 用实测间隙，解决
> bbox≠功能基准）、深度容差干涉（接触不算碰撞）、remodel_parts 改走
> graph_aider 的 Aider 补丁（保留已验证特征、省 token）、handoff 包
> （GLB + URDF + snapshot + manifest）。

> 此前一轮代码审查驱动的 bug 修复（B1–B7）：mate 拓扑排序、revolute 关节
> 配对修正、负 angle 归一化、repair 喂执行栈、kinematics 多壳零件、mate 图
> 无环/单根校验、judge 输入与 prompt 对齐、face_to_face 面向硬校验、运动学
> 扫角去双重计数、实测尺寸对账层、token 端到端聚合。
