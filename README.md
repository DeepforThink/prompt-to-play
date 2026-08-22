# Prompt-to-Play

<div align="center">

**Agent 驱动的 Godot 游戏生成与闭环评测流水线**

从一段自然语言描述出发，自动生成、编译、试玩探针、截图评测、修复并选择最佳可玩 Godot 4 项目。

[![Tests](https://github.com/DeepforThink/prompt-to-play/actions/workflows/tests.yml/badge.svg)](https://github.com/DeepforThink/prompt-to-play/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![Godot](https://img.shields.io/badge/Godot-4.7%20.NET-478CBF?logo=godot-engine&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)

</div>

Prompt-to-Play 接收任意游戏 Prompt 和可选参考图，直接让模型编写真实的 Godot 场景、脚本、资源与着色器。系统不依赖 WorldSpec、固定实体表或场景模板，因此同一条流水线能够生成不同题材、视角、玩法和美术风格的游戏。

生成不是一次性“写完即交付”。项目包含三个职责隔离的 Agent，以及由宿主程序控制的构建、结构验证、交互探针、截图评测、反馈修复和最佳版本恢复闭环。最终产物是一个可继续编辑、关闭后仍可重新打开的标准 Godot 项目。

## Demo Gallery

> 点击封面播放仓库内的 MP4 演示。三款游戏均由同一个输入界面和同一套闭环流水线生成，未使用参考图。

| 冰峰拉力：雪线竞速 | 云界远征：风晶之门 | 轨道拾荒者：核心撤离 |
| --- | --- | --- |
| [![Alpine Valley Rally](media/demos/alpine-valley-rally.jpg)](media/demos/alpine-valley-rally.mp4) | [![Skybound](media/demos/skybound-wind-crystal-expedition.jpg)](media/demos/skybound-wind-crystal-expedition.mp4) | [![Orbital Salvage](media/demos/orbital-salvage-core-extraction.jpg)](media/demos/orbital-salvage-core-extraction.mp4) |
| **Alpine Valley Rally**<br>雪山拉力、车辆惯性与漂移、顺序检查点 | **Skybound: Wind Crystal Expedition**<br>浮空岛平台跳跃、水晶收集与传送门 | **Orbital Salvage: Core Extraction**<br>等距视角搜集、脉冲战斗、冲刺与撤离 |
| <code>WASD / 方向键</code> 驾驶，<code>R</code> 重开 | <code>WASD / 方向键</code> 移动，<code>Space</code> 跳跃 | <code>WASD / 方向键</code> 移动，<code>Space</code> 脉冲，<code>Shift</code> 冲刺 |

完整 Prompt：

- [冰峰拉力：雪线竞速](prompts/snow-rally-demo.txt)
- [云界远征：风晶之门](prompts/skybound-demo.txt)
- [轨道拾荒者：核心撤离](prompts/orbital-salvage-demo.txt)

## Architecture

~~~mermaid
flowchart LR
    U["Prompt + 可选参考图"] --> G["ProjectGeneratorAgent<br/>生成 generated/**"]
    G --> S["安全契约<br/>路径 / 类型 / 大小 / 能力检查"]
    S --> B["Godot + .NET 构建"]
    B --> H["可信 Harness<br/>结构检查 + 交互探针 + 截图"]
    H --> V["VisualEvaluationAgent<br/>视觉与玩法可读性评测"]
    V -->|"全部门槛通过"| C["候选版本"]
    V -->|"编译 / 结构 / 视觉问题"| R["CodeRepairAgent<br/>生成最小文件补丁"]
    R --> S
    C --> K["Accepted-first 最佳版本选择"]
    K --> P["可玩 Godot 项目<br/>+ 可审计运行证据"]
~~~

### 三个 Agent

| Agent | 输入 | 输出 | 职责 |
| --- | --- | --- | --- |
| <code>ProjectGeneratorAgent</code> | 原始 Prompt、参考图 | 完整 <code>generated/**</code> 文件包 | 从零实现 Prompt 要求的场景、玩法、相机、UI 与视觉表现 |
| <code>VisualEvaluationAgent</code> | 原始需求、参考图、实际截图 | 结构化评分与具体问题 | 独立评估需求符合度、构图、连贯性、细节、光照材质和玩法可读性 |
| <code>CodeRepairAgent</code> | 当前源码、编译/结构/截图证据、评测反馈 | <code>generated/**</code> 文件补丁 | 修复编译、场景、交互和视觉问题，并进入下一次验证 |

三个角色使用隔离的模型调用与严格 JSON 契约。视觉 Agent 只提出证据和评分；最终是否通过由宿主程序根据固定质量门槛决定，模型不能自行宣布完成。

### 自动闭环

每个候选修订都会经历：

1. 直接文件契约与静态安全检查；
2. Godot/.NET 编译；
3. 场景入口、玩家、目标、HUD、相机和可渲染内容检查；
4. 合成按键输入并验证可观察的状态变化；
5. 渲染一到两张绑定项目哈希的截图；
6. 视觉 Agent 评测；
7. 未通过时生成修复补丁并重新执行。

默认最多进行 4 轮修订，内部硬上限为 6。所有历史版本和证据保持不可变；系统优先选择通过全部门槛的版本，否则保留最佳可玩版本供诊断，不会把未达标结果伪装成成功。

### 信任边界

| 所有者 | 路径 | 责任 |
| --- | --- | --- |
| 仓库宿主 | <code>project.godot</code>、<code>.csproj</code>、<code>harness/**</code> | 稳定入口、构建、结构验证、交互探针和截图 |
| 模型 | <code>generated/**</code> | 当前 Prompt 对应的游戏内容 |
| 证据写入器 | <code>artifacts/runs/&lt;run-id&gt;/rev_&lt;n&gt;/**</code> | 日志、清单、截图、评分、Token/时间与版本选择 |
| 参考图发布器 | <code>references/**</code> | 按顺序复制并绑定哈希的用户参考图 |

模型只能修改 <code>generated/**</code>。宿主会拒绝路径穿越、绝对路径、链接/联接、不支持的扩展名、超限文件，以及尝试进程执行、网络访问、宿主文件系统、环境变量、反射、原生互操作或不安全代码的生成内容。

更完整的协议与证据说明见 [docs/PROMPT_TO_PLAY.md](docs/PROMPT_TO_PLAY.md)。

## Quick Start

### 环境要求

- Windows 10/11
- Python 3.11+
- .NET 8 SDK
- Godot 4.7.x .NET
- OpenAI Responses 兼容接口的 API Key

运行时不依赖 Codex CLI。CPU 可以完成生成、编译和结构检查；独立 GPU 会改善渲染和视觉评测速度，但不是启动条件。

### 1. 克隆项目

~~~powershell
git clone https://github.com/DeepforThink/prompt-to-play.git
cd prompt-to-play
~~~

### 2. 启动输入界面

推荐使用带掩码输入的 Windows 启动器：

~~~powershell
powershell -ExecutionPolicy Bypass -File scripts/start_prompt_to_play_api.ps1
~~~

默认配置为米醋 Responses 兼容端点和 <code>gpt-5.6-sol</code>。也可以使用：

~~~powershell
# OpenAI 官方端点
powershell -ExecutionPolicy Bypass -File scripts/start_prompt_to_play_api.ps1 -ApiProvider openai -Model <model-id>

# 其他 Responses 兼容端点
powershell -ExecutionPolicy Bypass -File scripts/start_prompt_to_play_api.ps1 -ApiProvider custom -BaseUrl https://example.com/v1 -Model <model-id>
~~~

启动器只把 API Key 放入新进程的环境变量，并在启动后清除自身副本；不会把密钥写入源码、Git、日志或命令行参数。

### 3. 生成游戏

在桌面界面中输入游戏描述，可选添加参考图片，然后点击 **生成并启动**。Seed 由规范化请求内部派生；时间、Token、评分和哈希是运行后记录的评价指标，不是用户必须填写的参数。

## Generated Projects

每次请求都会创建独立目录：

~~~text
../output/generated/<request-hash>/run-<id>/
├── project.godot
├── PromptToPlayDirect.csproj
├── generated/                 # 最终选中的模型源码
├── harness/                   # 可信验证宿主
└── artifacts/runs/<run-id>/   # 每轮构建、截图、评分和选择证据
~~~

关闭游戏不会删除项目。选中的项目可以直接用 Godot 再次打开、复制给队友或继续开发；只有生成或修复新游戏时才需要 API Key。

## Evaluation Evidence

| 评价指标 | 项目证据 |
| --- | --- |
| 场景相似度 | Prompt/参考图与哈希截图的视觉 Agent 对比 |
| 结构正确性 | 编译结果、入口/玩家/目标/HUD/相机检查与交互探针 |
| 自动化闭环 | 初始文件包、逐轮补丁、评测、不可变快照和最终选择 |
| 生成速度 | 规划、写入、构建、检查、截图、评测和修复的分阶段计时 |
| Token 消耗 | 每个 Agent 的输入/输出 Token 与模型调用轨迹 |
| 结果可复现性 | 规范化请求、内部 Seed、源码/截图哈希和重复运行对比 |

## Repository Layout

- <code>prompt_to_play/direct_generation.py</code>：直接文件 Schema、路径/代码安全验证与原子补丁。
- <code>prompt_to_play/direct_agents.py</code>：生成、视觉评测和代码修复三个 Agent。
- <code>prompt_to_play/direct_evaluation.py</code>：视觉反馈契约与宿主质量门槛。
- <code>prompt_to_play/direct_pipeline.py</code>：构建、验证、截图、修复、选择和启动编排。
- <code>prompt_to_play/direct_template/</code>：可信 Godot Harness 与模型内容边界。
- <code>prompt_to_play/launcher.py</code>：桌面 Prompt/参考图输入界面。
- <code>scripts/start_prompt_to_play_api.ps1</code>：掩码 API 启动器。
- <code>prompts/</code>：可复现实验 Prompt。
- <code>media/demos/</code>：压缩后的演示视频与封面。
- <code>tests/</code>：契约、安全、Agent、模板和流水线测试。

旧的 Schema/WorldSpec 实验模块仅作为历史实现参考，不被当前直接生成入口导入。

## Development

~~~powershell
python -m pytest -q
python -m compileall -q prompt_to_play scripts tests
~~~

提交前请确认：

- 没有 API Key、<code>.env</code>、个人路径或私有参考图；
- 没有 <code>output/</code>、<code>.godot/</code>、构建目录、缓存或运行证据；
- 演示媒体已经压缩，原始录屏保留在仓库外；
- README 中的演示链接和 Prompt 可以复现。

## Upstream and License

本项目基于 [Godogen](https://github.com/htdt/godogen) 的开放源码工作继续开发，并将核心运行路径扩展为面向任意 Prompt 的 Godot 直接生成、多 Agent 评测修复和证据化版本选择流水线。

项目遵循 [MIT License](LICENSE.md)。上游作者及许可证信息保留在仓库历史与许可证文件中。
