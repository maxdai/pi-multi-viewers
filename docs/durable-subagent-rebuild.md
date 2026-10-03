# 用 durable subagent 重构 pmv 的提要

> **性质：可能性记录，不是开发计划。** 本文整理 2026-10-03 的可行性核实结果，供**将来
> durable 的接口相对固定之后**再决定是否重构。
> **触发条件**：① durable 自述"API 随版本变"（现为 1.0.0）转为稳定；② 上游 CLI 是否把主路径
> 切到它（见 `docs/design.md` §六 的三条触发）；③ 出现"多进程形态"真正吃痛的点（见 §4）。
> **所有上游引用都带 `文件:行`**，便于将来复核；上游文档本身不复制（会漂移）。
> **摸底口径**：基于 **pi 1.0.0**（2026-10-03）；durable 自身源码 `packages/durable/src` = **60 文件 /
> 17,662 行**（口径：`find src -name '*.ts'` 计数 + 全部 `.ts` 行数求和；不含 chord / pi-ai 等依赖）。

## 0. 一句话结论

**基本功能全部可以重建**（含 fork + 两轮 handoff 注入、每会话配置、确定性 loop、human 插话），
且四处比现在更自然（entry 级 fork、commit-then-visible、steering、任务图）；**真正的代价是
"判定纯净性"的守护从进程边界退为代码纪律**，加上"自组装面"归我们（MCP 最实）。**收益是
进程与格式手术都消失**（轻量），**代价是多三个上游包**（durable / pi-ai / chord，其中 durable 实验性）。

## 1. 动机与一条更正

- **动机（用户 2026-10-03）**：更轻量级、依赖更少；pi 原生有了 subagent，不再需要"另起 pi"。
- **更正**：我们现在的**多进程形态是"不得已"**（当时 pi 不支持 subagent），**不是优点**。
  "视角间故障隔离 / OS 级控制"是那个不得已的**副产物**，不应列入优点清单。
- 因此本文不把"进程"当资产；只把**一条不变式**当资产：**判定 = 已提交状态的纯函数**
  （`meeting_core` 自述"纯函数，无 I/O"）。它是设计意图，与进程无关，**跨运行时有效**。

## 2. 映射表：基本功能 → durable 里的表达

| 我们的功能 | durable 表达 | 证据 |
|---|---|---|
| N 视角各自独立会话 | `createConversation()` × N；逐会话 `configure({ model, thinkingLevel, extensions, tools, instructions, cwd })` | `durable/README.md` "Per-Conversation Agent" |
| fork 主会话上下文 | 读主 session **jsonl**（路径契约不变）→ 同样的窗口/折叠逻辑 → `tx.appendEntry()` 逐条写入新会话 | `src/session/transaction.ts:343` `appendEntry()` |
| 条目种类映射 | `pi.user` / `pi.assistant` / `pi.tool-result` / `pi.system` / `pi.compaction` / `pi.reset` | `src/entries.ts:15-40`（每种都注明 `model` 载荷类型） |
| 两轮 handoff 注入 | 同上，在创建提交里**一次写完**（`fork(entryId, { init })` 或 `createConversation({ init })`） | `src/harness/types.ts:340` `ConversationInit = (tx, id) => …` |
| 视角任务书注入 | `pi.system` 条目（`model` 是 `[SystemMessage]`）或 `configure({ instructions })` | `src/entries.ts` `SystemEntry` |
| cwd = 主项目 | 逐会话 `cwd` + `env` 钩子（可"一个会话一个容器"） | `README.md` "Environment" |
| 视角间消息 / 协议状态 | **docs**（`defineDoc`：scope `session`/`conversation`/`task`，`history` `latest`/`rewindable`，`fork` `initial`/`current`/`asOf`；`tx.doc()` 与条目**同一次提交**）**或保留 git** | `src/documents.ts:39-57` |
| 确定性 loop（配额/冻结/RR/收敛） | task 的 **`phases` 穷尽映射** + `checkpoint`（phase-typed）+ `version`/`migrate` | `src/types.ts` `TaskDefinition` |
| "等其它视角提交" | `{ status: "waiting", checkpoint, on: [taskId…], policy: "allSettled" }` —— **`on` 可命名任意任务**（非自己拥有的须 `allSettled`） | `durable/docs/spec.md` §5.5 "Waiting" |
| 轮询（`poll_interval`） | **消失**：`waiting` 不占调用，任务终结即恢复 | 同上 |
| 崩溃恢复 | open 时 `running` → `pending`、`waiting` 保持、重新评估 `completing`/`failFast` | `spec.md:610-612` |
| human 插话 | `submit({ whenBusy: "steer" })` + `watch()` / `viewState()`；**submission 会 resume 调度** | `README.md` "Busy Conversations"；`src/harness/harness.ts:199` |
| 时间（stall 超时等） | `now?: () => number` **宿主注入**；**条目里没有时间戳** | `harness.ts:179`；`src/types.ts` `EntryRecord` |
| 收尾 result.md | 写文件（工具）或 doc | — |
| 自带工具集 | `CodingTools` = read/write/edit/bash（"nothing installs it automatically"） | `src/tools/index.ts:19` |

## 3. 会失去 / 会变弱（已核到最窄口径）

1. **进程级故障的爆炸半径** —— 注意口径：durable 的失败隔离是**任务级**的（一次生成失败结算为
   `{status:"failed"}`，其它会话继续），**不是"一个视角失败全灭"**。真正不隔离的是**进程级**
   故障：原生崩溃 / OOM / **事件循环被同步代码阻塞**。
2. **CPU / 阻塞型真并行** —— 可缓解（`env` 钩子"每会话一个容器"），但那是额外机制。
3. **外部可读性** —— 条目无时间戳、存储不是给人读的形态；两条缓解：① 选 **JSONL 存储后端**
   （append-only 文件）；② **git 降级为单向导出视图**（每次协议提交后由宿主写一个 commit，
   判定永不读 git ⇒ 单一事实源仍是 docs，但 `git log` 审计面保住）。

## 4. 困难点（修正后，按"卡人程度"排）

| 序 | 困难 | 性质 |
|---|---|---|
| ① | **自组装面**：MCP（durable **零支持**）、模型/凭据/settings、AGENTS.md 等上下文发现 | 工作量；MCP 最实 |
| ② | **导入器**：jsonl → 条目种类映射 + 工具调用↔结果配对 | 有界机械（比现在简单：不用维护 parentId 链） |
| ③ | **git vs docs 的岔路** | 设计选择（见 §3.3 的缓解） |
| ④ | **观测/报告**：条目无时间戳 ⇒ 现有报告字段要自记 | 工作量 |
| ⑤ | **进程级故障隔离** | 结构性损失，不可消除 |
| ⑥ | **上游实验性**（API 可能变；`PI_EXPERIMENTAL` 门禁；`experimental/` 子系统仍在动） | 外部风险 |

**已排除（曾以为是困难，核实后不成立）**：主会话**不必**进 durable（用 `createConversation` +
`init` 播种即可，只需一个导入器）；**不必**另起 pi（自带 `CodingTools`，一个 TS 宿主 + durable +
pi-ai 即可）；human 的 sayer/viewer 都不是障碍（宿主就是我们的程序，viewer 可内嵌）。

## 5. 待核清单（本文未落地的项）

1. **MCP 自建的形状**：连 server → 发现工具 → `registry.install(defineExtension({ tools }))`
   在 durable 的注册/扩展 API 里能否**动态安装**（理论可行：`registry.install` + `defineTool`）；
   另注意 durable **没有 codemode**，MCP 工具会直接声明给模型（上下文成本）。
2. **`pi server` / client 的可编程驱动面**：若要从**外部进程**驱动会话（而非把编排放进宿主）。
3. **human 通道的客户端形态**：`pi client` / pi-web 对 durable 会话的支持程度。
4. **`on` 跨会话等待的实测**：spec 允许（`allSettled`），但未在真实任务图上跑过。
5. **导入器保真度**：工具调用↔结果配对、compaction 锚点（`head` 语义）的等价性。

## 6. 如果将来做：建议的阶段与顺序

0. **零成本准备（现在就能做，且已在做）**：继续守住"判定 = 已提交状态的纯函数"与"状态只从
   共享事实推导"——这两条正是 durable replay 要求的同一性质，是**唯一跨运行时有效**的资产。
1. 纯逻辑先行：`meeting_core`（判定）+ engine 的状态机判定（~1000 行，逻辑直搬）。
2. 宿主 + 存储 + 会话：`Harness.open` + 逐会话 `configure` + 任务 phase 化（含 `waiting`）。
3. 导入器：jsonl → 条目（含 handoff 注入）。
4. 观测/报告：自记时间条目 + `pi.usage` / `taskGraph` / `inspect`。
5. 扩展命令 / CLI / human 通道。
6. 测试体系（纯函数单测可直搬；subprocess 级测试需重写）。

**会消失的**：`meeting_fs`（1488 行，jsonl/git 操作）与 `meeting_loop`（710 行，进程管道）的
大部分。**验收面**：现有报告的字段（每次唤醒构成表 / 终止原因 / 配额 / 扩展策略）可直接作为
行为对照。

## 7. 上游依据索引（复核用）

```bash
# 库文档与设计
packages/durable/README.md                    # Per-Conversation Agent / Busy / Reset and Handoff / Your Own State …
packages/durable/docs/spec.md                 # §5.5 Structured concurrency（waiting / on / policy）
# 关键实现
packages/durable/src/entries.ts               # 内置条目种类（pi.user / pi.assistant / …）
packages/durable/src/documents.ts             # defineDoc（scope / history / fork）
packages/durable/src/tools/index.ts           # CodingTools（read/write/edit/bash）
packages/durable/src/harness/types.ts         # TaskDefinition / ConversationInit / TaskInspection
packages/durable/src/harness/harness.ts       # now 注入（:179）、submission resume 调度（:199）
# CLI 侧的 durable 路径（门禁与装配）
packages/coding-agent/src/core/experimental.ts        # PI_EXPERIMENTAL 门禁
packages/coding-agent/src/experimental/durable/harness-setup.ts  # createCodingRegistry（只装工具集 + prompt）
packages/coding-agent/src/experimental/session-catalog.ts        # 会话目录 + session.sqlite
```
