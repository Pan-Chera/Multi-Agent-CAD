# MAC — 从自然语言生成 CAD 零件与装配体

> 用自然语言生成可编辑的 CAD 零件和多零件装配体。[English](README.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
[![Powered by build123d](https://img.shields.io/badge/Powered%20by-build123d-orange.svg)](https://github.com/gumyr/build123d)
![Outputs](https://img.shields.io/badge/Outputs-STEP%20%7C%20STL%20%7C%20GLB-brightgreen.svg)
![Assembly export](https://img.shields.io/badge/Assemblies-URDF-purple.svg)

MAC 将普通设计需求转化为可编辑的工程几何。它既能生成单个可打印零件，也能将机构拆解成独立零件，完成装配、验证，并导出供检查及仿真工作流使用的文件。

MAC 最初以单零件 Text-to-CAD 工作流发布，目前已在 GitHub 获得接近
1000 Stars。本次版本保留原有单零件流程，并进一步加入视觉验证、独立
Judge，以及实验性的多零件装配体生成功能。

![MAC Web UI 操作演示](assets/parts/ui_demo.gif)

## 装配体展示（技术预览）

在原有单零件工作流基础上，MAC v2 新增了实验性的自然语言装配体生成功能。以下内容是经筛选的展示结果，不代表自然语言提示词成功率，也不用于宣称装配工作流优于 CAD Agent Skill。用于制造或仿真之前，请人工检查生成的几何和关节。

### 复杂装配体

| 三轴龙门测量单元 | 伸缩式电影机器人吊臂 |
|---|---|
| 三个正交运动级组成的桥式测量平台。 | 带铰接支撑与伸缩式载荷臂的多级吊臂。 |
| ![三轴龙门测量单元](assets/assemblies/complex/three-axis-gantry-metrology-cell.gif) | ![伸缩式电影机器人吊臂](assets/assemblies/complex/telescopic-cinema-robot-crane.gif) |

| 重型移动机械臂 | 高级视觉检测机械臂 |
|---|---|
| 移动底盘搭载细节丰富的铰接机械臂。 | 带框架式视觉载荷的多关节检测臂。 |
| ![重型移动机械臂](assets/assemblies/complex/heavy-duty-mobile-manipulator.gif) | ![高级视觉检测机械臂](assets/assemblies/complex/advanced-vision-inspection-robot-arm.gif) |

### 小型功能装配体

| 铰链式双爪夹持器 | 导向直线推杆 | 旋转叉形钥匙工具 | 可复用五指手 |
|---|---|---|---|
| ![铰链式双爪夹持器](assets/assemblies/simple/hinged-twin-claw-gripper.gif) | ![导向直线推杆](assets/assemblies/simple/guided-linear-plunger.gif) | ![旋转叉形钥匙工具](assets/assemblies/simple/rotary-fork-key-tool.gif) | ![可复用五指手](assets/assemblies/simple/reusable-five-digit-hand.gif) |

这些经筛选的小型例子展示了自然语言拆件、关节接口、重复零件复用与镜像几何；复杂例子展示了同一流程处理大型视觉装配体的能力。实现细节和当前限制见[装配体工作流文档](mac_assembly/README_cn.md)。

## 单零件展示

以下示例均为可编辑的 CAD 结果，而非仅有图片的生成结果。

| 蜂巢收纳座 | 陀螺仪摆件 | 灯塔 | 手机支架 | 笼中小球 |
|---|---|---|---|---|
| ![蜂巢收纳座](assets/parts/show1.gif) | ![陀螺仪摆件](assets/parts/show2.gif) | ![灯塔](assets/parts/show3.gif) | ![手机支架](assets/parts/show4.gif) | ![笼中小球](assets/parts/show5.gif) |

| 可动陀螺仪 | 多环链 | 马尔他机构 | 等离子反应堆 | 刹车盘 |
|---|---|---|---|---|
| ![可动陀螺仪](assets/parts/show6.gif) | ![多环链](assets/parts/show7.gif) | ![马尔他机构](assets/parts/show8.gif) | ![等离子反应堆](assets/parts/show9.gif) | ![刹车盘](assets/parts/show10.gif) |

[单零件工作流文档](multi_agent_cad/README_cn.md)介绍配置、运行、缓存与 QA，并链接到基准提示词和逐模型数据。

## 实物结果

![MAC 生成模型的 3D 打印实物](assets/parts/overview.jpg)

上图的打印实物包括单零件工作流生成的基准零件和原创展示模型。MAC 还可以生成包含独立实体及功能间隙的“一体打印即可活动”机构：

<p align="center">
  <img src="assets/parts/articulable.gif" width="520" alt="MAC 生成的可动打印模型">
</p>

## 单零件工作流基准测试

下列数字**仅适用于文档记录的 10 条提示词、141 个特征的单零件工作流基准测试**，不是装配体成功率或装配体成本数据。其中个别提示词可能生成多实体机构；“单零件工作流”指所使用的管线，而非保证输出只有一个实体。

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
独立 Judge 后的新版单零件工作流。装配体生成作为独立的技术预览展示，不包含
在上述基准数据中。

在两组已有模型配置中，MAC 均保持了 140/141 的特征通过率，同时相比对应的
Skill 复现运行显著减少了记录 Token 和估算成本。

方法与原始明细见[单零件 README](multi_agent_cad/README_cn.md)、[英文评估](docs/quantified_quality.md)和[中文评估](docs/quantified_quality_cn.md)。

## 为什么选择 MAC？

- **自然语言输入**：描述几何、尺寸、接口和运动关系，不必编写 CAD 代码或内部数据格式。
- **覆盖零件与装配体**：同一个项目支持独立可打印零件以及多部件可动机构。
- **可编辑的工程输出**：导出 STEP、STL 和 GLB，而不只是渲染图片；装配体还提供 URDF 交接文件。
- **可审查的生成过程**：可以检查结构化需求、几何计划、生成的 Python、测量结果、QA 报告与修复记录。
- **自动执行与修复**：执行并检查生成的 CAD，通过有上限的反馈回路处理可恢复错误。
- **复用代替重复生成**：重复零件和镜像零件可以从一份源几何得到。
- **灵活配置模型**：规划、几何、编码和修复阶段可分别配置 OpenAI 兼容模型。
- **可视化工作流**：单零件流程提供浏览器界面、3D 预览与下载。
- **延续已有工作流**：MAC v2 在接近 1000 Stars 的原单零件流程上增加视觉验证与装配体生成，同时保留已有的零件生成能力。

## 快速开始

### 安装

```bash
git clone https://github.com/Pan-Chera/Multi-Agent-CAD.git
cd Multi-Agent-CAD
conda env create -f environment.yml
conda activate multi_agent_cad
pip install --no-deps "aider-chat==0.82.3"
export DASHSCOPE_API_KEY="your-key"
```

最后一步 `pip install` 是必需的：`aider-chat` 固定依赖 NumPy 1.x，与
build123d 使用的 NumPy 2.x 冲突，因此没有放入 `environment.yml`；
`--no-deps` 可以避免替换当前可用的 NumPy 版本。

默认配置使用兼容 OpenAI API 的 DashScope 接口。其他提供商的配置见[单零件模型配置说明](multi_agent_cad/README_cn.md#配置与模型提供商)。

> **pip 用户（无 conda）**：`aider-chat` 锁定 `numpy==1.26.4`，与 `build123d>=0.8` 要求的 `numpy>=2,<3` 冲突，纯 pip 直接装失败。走以下 workaround（已在 macOS arm64 + Python 3.11 验证）：
>
> ```bash
> python3.11 -m venv .venv
> source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\activate
> pip install --upgrade pip
> # 先装 aider（会拉 numpy 1.26.4 + 一堆传递依赖），再强制覆盖 numpy 到 2.x。
> # 已验证 aider 0.82.3 在 numpy 2.x 上能正常 import——上游的 pin 是过度保守。
> pip install "aider-chat==0.82.3"
> pip install --no-deps --force-reinstall "numpy>=2,<2.3"
> pip install "build123d>=0.8" "langgraph>=0.2,<0.3" "langgraph-checkpoint>=2.0,<3.0" \
>             "pydantic>=2.5" "openai>=1.20.0" "anthropic>=0.30" \
>             "trimesh>=4.0" "rtree>=1.1" "scipy>=1.10" "scikit-learn>=1.3" \
>             "fastapi>=0.110" "uvicorn[standard]>=0.27" "ipython>=8.15" "pytest>=7.4"
> # --no-deps 跳过 pyproject.toml 的 numpy pin 重新检查；fastapi+uvicorn 已由上一步装好。
> pip install --no-deps -e .
> ```
>
> 最后一步同时注册 `mac-config-reset` 命令行脚本、并允许在任意目录（不只是仓库根）跑 `python -m multi_agent_cad.graph`。完整依赖清单见 [requirements.txt](requirements.txt) / [pyproject.toml](pyproject.toml)。

> **Windows**：在 Windows 上同样的 `conda env create` + `pip install --no-deps aider-chat==0.82.3` 流程可用——`trimesh` 和 `rtree` 来自 conda-forge 预编译包；`OCP` 由 `build123d` 的 PyPI 依赖 `cadquery-ocp-novtk` 传递性拉入。Windows 上不要走下面的纯 pip workaround——`trimesh`/`rtree` 的 native wheel 在 Windows 上不可靠。PowerShell 设 API key：`$env:DASHSCOPE_API_KEY = "sk-..."`（cmd.exe 用 `set DASHSCOPE_API_KEY=sk-...`）。conda 环境内跑 Web UI 用 `pip install -e ".[web]"`——`uvloop` 在 Windows 上自动跳过。Windows 不在 CI 里，但代码避开 Unix 专属 API、全程 UTF-8；遇到问题欢迎反馈。

### 生成一个零件

在 [`multi_agent_cad/config.py`](multi_agent_cad/config.py) 中设置 `USER_REQUEST`，然后运行：

```bash
python -m multi_agent_cad.graph
```

启动单零件浏览器界面：

```bash
pip install -e ".[web]"
python -m multi_agent_cad.web
```

界面默认仅监听 `127.0.0.1`。生成的 Python 会以当前用户权限执行，因此不要将
服务直接暴露到不可信网络。只有显式设置 `MAC_WEB_ALLOW_DEST_PATH=1` 后，界面
才允许把结果复制到任意本地目录。

### 生成装配体

装配体同样接受自然语言需求。可以运行附带的示例：

```bash
MAC_ASSEMBLY_REQUEST="$(cat mac_assembly/assembly_prompts/natural_language_benchmarks/01_hinged_twin_claw_gripper.md)" \
python -m mac_assembly
```

也可以直接传入需求：

```bash
MAC_ASSEMBLY_REQUEST="Create a two-part hinged clamp with a fixed base and one rotating jaw." \
python -m mac_assembly
```

装配任务写入 `assembly_jobs/job_<timestamp>/`，包含独立零件几何、装配后的 STEP/STL/GLB、URDF、清单及 QA 文件。在将产物用于制造或仿真之前，请阅读[装配体指南](mac_assembly/README_cn.md)。

## 文档

- [单零件工作流：安装、架构、基准测试与 Web UI](multi_agent_cad/README_cn.md)
- [装配体工作流：拆件、配合、复用、QA 与导出](mac_assembly/README_cn.md)
- [English README](README.md)

## 引用

如果本项目对你的研究有帮助，欢迎引用：

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

单零件定量评估使用
[earthtojake/text-to-cad](https://github.com/earthtojake/text-to-cad)（CAD
Skills）作为对比基线。如果引用该基准对比，也请引用原项目：

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

## 许可证

本项目使用 [MIT 许可证](LICENSE)。

仓库内的 [`packages/cadpy`](packages/cadpy) STEP/GLB 运行时源自
[earthtojake/text-to-cad](https://github.com/earthtojake/text-to-cad)（CAD
Skills），并依据其原始 MIT 许可证重新发布，详见
[`packages/cadpy/LICENSE`](packages/cadpy/LICENSE)。

## 致谢

- [清华大学 IEI Lab](https://maureenzou.github.io/lab.html)：提供项目开发所需的研究环境与指导。
- [earthtojake/text-to-cad](https://github.com/earthtojake/text-to-cad)（CAD
  Skills）：单零件定量评估的对比基线与 10 条共享基准提示词来源；仓库内的
  [`packages/cadpy`](packages/cadpy) 运行时同样源于该项目，并保留其原始
  MIT 版权声明。
- [build123d](https://github.com/gumyr/build123d)：代数式 B-rep CAD 建模内核。
- [LangGraph](https://langchain-ai.github.io/langgraph/)：有状态 Agent 编排框架。
- [Aider](https://aider.chat/)：基于 LLM 的代码修复工具。
- [Qwen Model Studio](https://www.alibabacloud.com/help/en/model-studio/)：文档实验使用的 OpenAI 兼容模型服务。
