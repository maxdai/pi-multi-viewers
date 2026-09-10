# pi-multi-viewers 开发指南

> 本文件指导**本项目本身的开发**。与 `templates/AGENTS.md.tpl`（运行时参与
> 分析的 LLM 行为指令）是两回事。

## 项目目的

pi-agents-helper（多方讨论达成共识）的平行第四代：**多视角协同分析**。
把主 pi session **fork** 成 N 个视角 agent（人物/开发准则等稳定视角），
各带视角任务书，在 meeting 协议下交锋、修正、收敛。与 pi-agents-helper
共享 meeting 协议核心（core/fs/engine），独立演化、互不依赖。

## 架构（fork-only，唯一模式）

```
meeting_core.py      纯逻辑判定——无 I/O（判定只看参与者，human 视而不见）
meeting_fs.py        git/文件层
meeting_engine.py    【唯一状态机】+ 协议信号 + responder 注入
meeting_loop.py      Pi 薄壳：首唤 --fork+--name → 存 sid → --session-id 续接
fake_agent.py        测试薄壳：responder = 随机决策
start_discussion.py  环境生成/启动/清理（viewers 发现 / spec 解析 / fork 源）
human_viewer.py      【human 通道】只读展示（增量/--follow/游标）
human_sayer.py       【human 通道】插话命令（单次/stdin/交互 -i）
scripts/mv.sh        wrapper（prepare/start/status/wait/cleanup/view/say）
prompts/multi-viewers.md  /multi-viewers 入口（视角设计三原则 + 审核闸门）
package.json         npm 包 pi-multi-viewers（pi.prompts 注册；发版待办）
templates/           AGENTS.md.tpl / agent.md.tpl / gitignore.tpl / spec-readme.md.tpl
viewers/             示范稳定视角（性能/可读性——对立视角，措辞经实验验证）
docs/examples/first-experiment/  首次实验存档（机制验证 + 模板原型 + 真实消息）
tests/               测试（unittest discover tests）
```

**fork 三件套**（初始化层与 pi-agents-helper 的全部差异所在）：

1. **session fork**：首唤 `pi --fork <主session> --session-id <预生成UUID>
   --name <分析名>-<视角名>`——agent 携带主 session 全量上下文；后续唤醒
   `--session-id <sid>` 续接（sid 存 `status-<agent>.json`）。`--name` 是
   显示名 label（session_info entry），不劫持 id（id 归机制=UUID，名字归人）
2. **cwd = 主项目**（forkCwd）：agent 直接读项目文件；work_dir 仅消息交换
   区，prompt 中所有路径**绝对化**（msg_path/meta/result.md）
3. **协议注入**：work-X/AGENTS.md（讨论协议）不在主项目祖先链上，pi 不会
   自动发现——wake_llm 无条件 `--append-system-prompt` 注入（视角 prompt_file
   同理）。e2e 曾暴露缺口：无注入时靠模型能力偶尔跑通，非设计保证

**上下文三层**：fork 对话历史（自动）+ 主项目文件（自动，含 cwd/AGENTS.md
祖先发现）+ spec background.md（人工可选边界约定——prepare 蒸馏机制已
移除，fork 使其冗余；background 只写显式边界，不复述对话）。

**关键约定**：pi sessions 目录编码 = `--` + 去首尾斜杠内斜杠换 `-` + `--`
（`/tmp` → `--tmp--`；wrapper 解析 fork 源依赖它，编码错一根横线 = 静默
解析不到——已加显式报错）。**fork-only fail-fast**：缺 fork 源 = 明确报错
（loop/start/wrapper 三层），无静默退化（无上下文的视角分析违背产品本质）。

**viewers/ 分支约定**：spec 的 `agents/` 目录存在 = 显式模式（优先）；
不存在 → 项目 cwd 的 `viewers/*.md` 发现（文件名即 agent 名：中文合法，
禁路径分隔符/空白/human/≤32；文件名排序定 starter/RR/resultWriter）；
两者皆无 → 明确报错。**meeting 至少 2 个 LLM agents**（0/1 个视角拒绝启动）。

**核心不变式**：状态机只在 `meeting_engine.agent_loop` 一份；fake_agent 与
meeting_loop 通过注入 responder 复用。human 插话不改变状态机——只在
判定函数的**输入过滤**与**配额增量**两处扩展（沿用 pi-agents-helper）。


## 开发铁律（每次修改代码后必核验三条，用户要求 2026-08-09）

1. **职责边界**：各部件是否只做自己该做的任务，而没有在做别的部件
   本应负责的任务（LLM 内容 / 引擎流程 / fs I/O / core 纯逻辑 / human writer 写消息）
2. **复杂度匹配**：各部分代码复杂度是否符合设计本应的复杂度，
   不应让大量补丁堆出不必要的复杂度（设计简洁，实现复杂=方法有问题）
3. **设计符合度**：代码实现是否真的符合设计——逐项对照设计文档，
   发现偏差先推演确认，不急着测试（测试验证设计，不替设计纠错）

## 设计原则

沿用 pi-agents-meeting-discuss / pi-agents-helper 的全部原则（确定性归
loop、状态从 git 共享事实推导、单一事实源 = protocol.json、无静默铁律、
测试对象 = 生产对象、新概念禁令、信息层/流程层分离、human 保留名），
并新增（fork 模式专属）：

1. **fork-only**：唯一模式。缺 fork 源 = 明确报错——不提供无上下文退化
   （违背产品本质）；脚本场景先 `pi --print` 造引导 session。
2. **初始化三件套集中在 loop**：fork 源 + 视角注入 + 命名，其余层
   （协议/引擎/human 通道/结果回流）与 pi-agents-helper 零差异。
3. **稳定资产与每次分析分离**：视角 = viewers/ 项目资产（写好长期用），
   主题 = question.md（每次变）；不把视角内容散落进每次的 spec。
4. **单一事实源（骨架）**：spec 骨架生成只在 `gen_spec_skeleton` 一份
   （python）；wrapper `--prepare` 只做参数解析后转调，禁止 bash 复刻。

## 注释引用约定（#17，e2e7 评审）

代码注释中的历史编号引用（"设计 16.x"、"审核#N"、"review4/5 Lxx"、
"用户 NNNN"）出自**开发过程记录**（会话/上游文档），**在本仓库内不可
检索**。约定：这类引用只是溯源线索，**代码行为以自描述注释为准**——
读代码时不需要也无法追查编号原文；新增注释不再引入不可核验编号
（描述清楚"为什么"，编号可省略）。

## 设计文档

- `docs/examples/first-experiment/`：首次实验存档——机制验证结论、
  协议/视角模板原型（措辞经实验验证）、三条真实消息（可作 loop 测试 fixture）
- 上游设计文档：`../pi-agents-helper/docs/pi-helper-design.md`（共享协议
  核心的行为定义——信息层/流程层分离、配额语义、状态机推演对本项目
  同样有效）

## 单元测试重设计工程（沿用 pi-agents-helper 方法论 2026-09-01）

**背景**：只做"观察 agent loop 流程"的集成式测试不够——loop 跑通不代表
每个 API 正确；API 细节（边界、异常）未被逐一定义验证。

**流程（不可跳步）**：
1. **测试计划**：基于 agent loop + 流程设计，分解每个脚本的详细功能
2. **脚本 + API 列表**：每个 API 精确定义功能表现（正常/边界/不应出现的
   情况），API 组合应符合设计流程——**列表即测试基准，用户审阅后生效**
3. **逐 API 单元测试**：各种可能出现的情况 + 各种不应该出现的情况
4. **测试报告**：列出问题清单
5. **报告审阅（不动代码）**：每个问题的影响/修改方案/对逻辑流程的影响
6. **逐 API 修改 + 复测**（基于审阅结果）
7. **全流程回归**：完整测试 + 针对修改影响的补充测试

**进度单一事实源**：API 清单文档状态列（待测/已测/审阅/已修/复测）——
任何时候打开清单即知进度；会话中断/重启由 AGENTS.md + 清单恢复。

**拼接点盲区教训**（pi-agents-helper 实测教训，同样适用）：测试覆盖不能
只到函数级——main()/__main__/CLI 分发等**调用链拼接点**是独立盲区（mock
打不到 runpy 的 __main__ 新模块）。API 清单必须显式包含拼接点，测试用
真实 subprocess 构造环境跑生产调用链。

## 测试方法论（制度核心；方法细节见 docs/test-methodology.md）

**总原则（错误→制度化，用户 2026-09-09 定）**：出现错误后要记的不是
"这个 bug 怎么修"，而是**什么制度缺失让它漏过、让定位变慢**——补制度
保证同类错误结构性不再犯。复盘四问（详版见方法文档）：
1. 特性落地时有没有盘点它打破的既有假设（如"agent 名是 ASCII"）并补
   边界测试？
2. 定位方法对吗——纯机制层 bug（git/文件/编码/状态机）用确定性复现
   （单测/小实验），禁止真实 LLM e2e 碰运气（只做最终确认）？
3. 失败现场保留了吗（测试失败删 log = 测试白跑）？
4. 改装置/用新 API 先做秒级最小实验验证语义了吗（不跑长测试试错）？

**方法条目仓库**：全部具体方法（git 写前 pull / 进程检测 / 失败三分类 /
装置对齐 / 运行观察分离 / 参数形态矩阵二维 / 不留 session / 隔离 +
用例级 teardown / 失败保留现场）及历史实例，统一在
`docs/test-methodology.md`——新方法在那里追加，AGENTS.md 不逐条同步
（避免 100 个方法全堆进来）。

## Git 准则（用户约定，沿用）

1. **每次改动先更新本地 git**：对本项目代码/文档的每次修改，先 `git add` + `git commit` 记录。
2. **阶段性完成即推送**：完成一个阶段性修改后，必须同时 `git push origin main` 推送到 GitHub。
3. **本地与远程保持同步**：提交后确认工作区干净、远程与本地 HEAD 一致。
4. **提交信息**：使用清晰、描述性的 message，说明本次改动内容。
5. **行为/语义修改同步文档**：对协议行为、产品形态的任何修改，必须同步
   README 与相关模板。

## 安装/发版状态（2026-09-09）

- **当前形态**：prompt × 1（multi-viewers，未安装进 pi 环境）+ wrapper。
  extension（如 /multi-viewers-say）与 npm 发版 0.1.0 均为待办。
- **发版时参照** pi-agents-helper 的成熟路径：`pi install npm:pi-multi-viewers`
  用户级安装（package.json pi.prompts 声明）；开发机在 `~/.pi/agent/npm/`
  用 `npm install file:/root/pi-multi-viewers --legacy-peer-deps`（file: 依赖
  声明防 npm prune 清掉 symlink——上游两次实测教训）；prompt 路径用固定
  安装路径（只支持用户级）；改动 prompt 后 reload 生效。
