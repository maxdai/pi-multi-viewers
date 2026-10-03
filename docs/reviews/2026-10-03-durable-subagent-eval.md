<!-- 存档：docs/reviews/2026-10-03-durable-subagent-eval.md
     来源：一次真实多视角分析的 result.md 原文（未删改，仅加本头与下方说明）。
     分析场次目录已随 cleanup 删除；文中消息编号不可再核验，仅作溯源线索
     （与代码注释引用约定一致：行为以自描述为准）。 -->

# 存档说明

- **主题**：评估 pi 1.0.0 的 **durable subagent**（`@earendil-works/pi-durable`）能否用在本项目——
  它的 session 形态、与我们的 fork + 多进程编排相比如何、有没有真正用得上（或该等待）的部分。
- **场次**：`mv-mv-main-20261003-144433`（视角：效率 / 简单 / 铁律；档位 `high` ×3；扩展策略
  `mc-tools`；配额 `max-meeting 20`；**终止 = 共识**，RR 全体 pass，无保留分歧）
  ｜对象 = 本仓 HEAD `7c79dbd` + pi 1.0.0 源码（`/root/research/pi`）
- **判定**：**现在不采用 durable**（不引依赖、不换会话后端、不改并发模型、不把视角改成 durable
  子对话、不为未采用的运行时预留接口）；**简单性动机 0 条**。四条"越界型"否决：判定纯净性 /
  单一事实源 / 持久化职责重复 / 并发模型冲突；红线 = **运行时归属不转移**。
  重估触发分两类两条：A 依赖破裂（见 `docs/design.md` §六 三条）／B 痛苦（G2 失败损失、
  G3 延迟 SLA）。
- **落地**：**结论未落地为代码改动**（本场结论即"不改"）。本场另案两条已落地：
  (丙) 异常路径未提交产出**可见化** + (3) 超时注释与实现对齐 → commit `0bf7fa0`
  （`meeting_engine._uncommitted_slots` + 异常边界日志；500 python + 54 harness 全绿，
  先红后绿已验证）。
- **备注**：本场最有价值的**副产品**是它核到的"**两层持久化**"（会话层逐条落盘 + 协议层
  每唤醒一个提交点）——它纠正了此前"一次唤醒被中断 = 白付全部模型时间"的量级判断，
  并把失败损失收窄为"会话层未落盘的那一步 + 协议层那条消息"。另：本场也是
  2026-10-03 fork 链桥接修复（`7c79dbd`）的**首次真场验证**（三个 fork 源 pi 可达 == 产物总数、
  悬空 parentId = 0）。

# result.md —— 多视角分析结论

**主题**：评估 pi 1.0.0 的 durable subagent（`@earendil-works/pi-durable`）能否用在本项目——
它的 session 形态是什么样、与我们的 fork + 多进程编排相比如何、有没有我们真正用得上（或该等待）的部分？

**场次**：`mv-mv-main-20261003-144433`｜视角：效率 / 简单 / 铁律｜对象 = 本仓 HEAD `7c79dbd` + pi 1.0.0 源码（`/root/research/pi`）
**纪律**：只读调研（未启动任何真实 pi 运行）、不改代码；引用给 `文件:行`；未核实项已显式标注。

---

## 0. 共识结论（三方一致）

> **现在不采用 durable**：不引依赖、不换会话后端、不改并发模型、不把视角改成 durable 子对话、
> **不为未采用的运行时预留入口或接口**。
> **简单性动机 0 条；可吸收清单清零。**
> 重估触发分**两类两条**（A 依赖破裂 / B 痛苦），详见 §5。

分歧在自由讨论阶段已全部收敛（见 §7 收敛轨迹）；收尾轮转三方全体 `pass`，**无保留分歧**。

---

## 1. 形态对比（问题 1）

| 维度 | durable | 我们 |
|---|---|---|
| 形态 | **in-process TS 运行时库**：`Harness.open(storage, {models, registry, settings}, context)`（`packages/durable/README.md` Quick Start） | **进程外编排**：Python 调 `pi` 子进程 + jsonl + git |
| 规模 | `packages/durable/src` 60 文件 / ≈15.5k 行（不含 chord / pi-ai） | 核心 9 模块 6015 行（`meeting_fs.py` 1488 / `meeting_engine.py` 755 / `meeting_loop.py` 710 …） |
| 隔离性 | subagent = **同一 Harness 内的 child conversation**（进程内隔离；子代理"Starts as a copy of this conversation's agent"，README "Abort and Subagents"） | **每视角一个独立 pi 进程**（进程级隔离：失败域 / 并发 / 上下文窗口各自独立） |
| 上下文来源 | `fork(entryId)` = **同存储内 entry 粒度指针** | **跨存储产物**：从主 pi jsonl 读原始条目 → budget 裁剪 + 折叠 → 尾部注入切换叙事 → 落成 `pi --session <file>` 可打开的文件（`docs/design.md` §一） |
| 并发 | **单进程独占存储**："One process owns a storage at a time; there is no cross-process locking"（`packages/durable/README.md:527`） | **N 进程 + git 共享**（git 无独占锁；实测并行度 2.34） |
| 失败恢复 | per-step checkpoint + `replay:"safe"` + `requestId` 幂等 | **两层**：pi 会话层逐步落盘 + 稳定 sid 续接；协议层一唤醒一提交点（见 §3.3） |
| 成本 | 新增运行时（TypeBox 峰值 RSS ≈23MB unbundled / ≈4MB bundled，README "Storage"）+ 若用其压缩则**每次一次模型调用** | 编排层可省上界 ≈3.7% 墙钟（见 §4） |

**durable 能提供我们现在拿不到的能力吗？** 只在一处，且很小：**工作级检查点**（`replay:"safe"`）。
而它已被 pi 的会话层**大部分覆盖**（§3.3）；净增量只剩**工具副作用幂等**（`replay:"safe"`），
而我们的 agent 副作用主要是"读 + 末尾写一个消息文件"。

---

## 2. session 形态（问题 2）

- **durable 的会话**（CLI 侧）：每会话一个目录，含 `meta.json` + `session.sqlite`，**worker 独占**
  （`packages/coding-agent/src/experimental/session-catalog.ts`：`sessionStoragePath()` = `<dir>/<id>/session.sqlite`，注释 "Workers lock it and own the storage inside it"）。
- **门禁**：`process.env.PI_EXPERIMENTAL === "1"`（`packages/coding-agent/src/core/experimental.ts:2`），
  且只放行 `server` / `client` 两个子命令（`experimental/commands.ts:86`）。
- **CLI 主路径仍是 jsonl**：`docs/session-format.md` 仍写 "Sessions are stored as JSONL"、header version 3，
  `--session <path|id>` 仍接受任意文件；内置扩展清单未变（llama.cpp / codemode / tool-search / mcp）。
- **我们依赖的契约**：`jsonl + --session <任意路径文件>`，逐条见 `docs/design.md` §六（外部契约 / 迁移清单）。
- **结论**：`docs/design.md` §六 已写下裁定「**现在不改**——对着尚未被 CLI 使用的接口写代码是投机」。
  durable 的存储**技术上可独立 import**（`packages/durable/package.json` exports 有 `./storage/{memory,jsonl,sqlite}`），
  但**会话层不可独立**（`Harness.open` 一次拿到"存储 + 在其上跑 agent 的机器"）。⇒ **选项空间 = {什么都不买} ∪ {买整套运行时}，中间为空（存在但价值为零）**。
  且只借 storage 也不是"零收益的格式迁移"那么轻——它会与 git 形成**两个事实源**（违反"单一事实源 = protocol.json + git"）。

---

## 3. 需求对照（问题 3）：6 条硬需求只有 2 条落在 durable 范围内

| # | 硬需求 | durable 对应 | 判定 |
|---|---|---|---|
| ① | N 个视角**各自独立会话**（长思考中保持多视角关注） | child conversation（`tx.createConversation`） | **形态不同**：进程内 N 会话 vs N 个进程 ⇒进程级隔离（失败域/并发/窗口）消失 |
| ② | fork 主会话（budget 裁剪 + 切换叙事注入） | `fork(entryId)`（同存储、entry 粒度） | **无对应**：我们的 fork 是**跨存储产物文件**（§1） |
| ③ | cwd = 主项目 | `configure({cwd})` + `env` per conversation | 覆盖，但我们已有（fork 直接继承） |
| ④ | 消息经 **git** 交换 | 无（自有 inbox/docs/submissions） | **范围之外** |
| ⑤ | human 插话（文件 viewer/sayer） | `watch()`/`viewState()`（自有观察面） | **范围之外**（换过去等于重建通道） |
| ⑥ | **确定性 loop**（判定不看 LLM） | 由它的任务图/调度器决定 | **冲突**（见 §4 越界型） |

**结论**：durable 只触及 ① 和 ③，而这两条**我们已经具备**；②④⑤⑥ 不在其范围或与其冲突。

### 3.3 两层持久性（本场核到，取代"整唤醒全额重付"的旧量级）

每次唤醒有**两个粒度不同的持久化层**：

> ① **会话层** = pi 的 `session.jsonl`，**逐步同步落盘**（`_appendEntry` → `_persist` → **`appendFileSync`**，
>    `packages/coding-agent/src/core/session-manager.ts:1191/1195/1189`；`appendMessage` `:1204-1213` 逐条一写），
>    失败后经**稳定 sid** 续接（`meeting_loop.py:493-496` 预生成 UUID、`:523` 失败保持）⇒ 承载"**对话工作**"；
> ② **协议层** = git 消息文件，**一次唤醒一个提交点**（`meeting_engine.py:326/332`；`next_msg_id` 源
>    `git_ls_files`，`meeting_fs.py:715`）⇒ 承载"**判定可见性**"。

**失败损失 = 会话层未落盘的那一步 + 协议层那条消息（≠ 整唤醒的模型时间）。**
失败形态两支：**异常路径**（timeout/被杀，`meeting_loop.py:463` 抛 `TimeoutExpired`）不进 `commit_new_files`
（由 `meeting_engine.py:747-753` 记 log + 回轮询、**不自动重唤**）；**正常返回但无产出**走 `MAX_RETRY`
（≤3，`meeting_engine.py:52`）。

> **已有能力**：durable 的 headline（进程死在 turn 中途、重开从最后提交点续）**大部分已由会话层提供**；
> 且我们**有直接的崩溃恢复测试**：`tests/test_meeting_concurrency.py:319 test_crash_recovery`（`crash_map` 30%，
> 口径 `:7`「崩溃恢复：…不丢不重」）——**该属性我们已测过**。

---

## 4. 否决项（按**理由类型**分三型；重估命运不同）

### 越界型（**不可让**，不随数据变化）
1. **判定纯净性**：采用它会把**库内状态**掺进判定输入，污染唯一保持纯的那一层——`meeting_core.py:1-5`
   自述「**纯函数，无 I/O**……判定函数只吃数据结构」。这不是收益/成本问题，是"要不要保住不变式"。
2. **单一事实源**：durable 的 `resume()` 以**它自己的存储**为权威（"以 pending 为准接着跑"），与我们
   "判定只看 git bare 事实"并存 = 两个事实源（违反"单一事实源 = protocol.json + git"）。
3. **持久化职责重复**：pi 会话层已在做工作级持久化（§3.3）；采用 durable = **两套持久化并存、无一方会被删掉**。
4. **并发模型冲突**：durable 存储**独占**（`README.md:527`）vs 我们 N 进程 + git 共享（git 无独占锁）；
   搬进单进程 = 用单点串行 + 单点故障换掉 OS 给的并行。

### 冗余型（已有覆盖；仅在"覆盖物失效"时重评）
1. `requestId` 幂等：我们的**消息级幂等是零成本副产品**（`next_msg_id` 源 `git_ls_files`，`meeting_fs.py:715`）
   ——未提交的孤儿占不到新序号，重写天然落回同一槽位。
2. "唤醒前记 msg_path 再查"（效率 §5 的原始设想）：`_produced`（`meeting_engine.py:233-234`）已覆盖"已写出"
   那一半；另一半（做了工作未写出）**本地记账也救不了**（没东西可查）⇒ 重复记账。

### 不匹配型（净增，需先证明净减）
1. 为将来预留 `--session-backend` 之类抽象层：提前支付 durable 的固定成本，收益现在为 0。
   判据：**它当下替代或删除了哪段代码？**答不上来即反对。
2. "上游压缩替换我们 ~300 行手写裁剪"：**不成立**——见 §6「简单性动机 0 条」。

### 红线「运行时归属不转移」+ 两个守卫
> **线**：我们的 loop / `meeting_core` 拥有运行时；**不引入第二个运行时的入口**。
> **守卫一（运行期）**：判定输入不得含库内状态（`meeting_core.py:1-5`）。
> **守卫二（静态代码面）**：不得为未采用的运行时预留抽象/接口。
两个守卫**共享同一根因**（"谁拥有运行时"），三类越界型否决都挂在同一条线下。

---

## 5. 重估触发条件（**两类两条**；勿合并——合并会把"必须做"与"可以考虑"糊成一句）

### A. 依赖破裂触发（我们的代码会因此坏 ⇒ **必须适配**）
引用 `docs/design.md` §六 三条（不重抄）：
1. `docs/session-format.md` 改写，或 CLI 默认会话落 sqlite/repo 抽象（含 `--session-backend` 类开关）；
2. `--session <file>` 不再接受任意路径文件，或首唤报「打不开 fork 源 / 上下文为空」；
3. CHANGELOG 出现 "migrate sessions" / "sqlite default" 类条目。
- **A 类第一验证项**：换后**判定事实源是否仍是 git**（若 storage 成权威 ⇒ 需求⑥违约）。
- **判据**（过滤 A 类表）：「这条成立时，我们**哪一行代码会坏**？」——以此排除"上游包状态变化"
  （durable 毕业出 experimental **不改变我们的任何依赖** ⇒ 降格为"采用前置条件"，**不进 A 类**）。

### B. 痛苦触发（出现新痛点才**考虑采用**）
- **G2（失败损失）**：一次失败里「**会话层未落盘的步数 × 单步代价**」占比变得可观——
  **不是**"整唤醒的模型时间"。取数（0 LLM、现有产物）：① 失败类型分布（timeout/异常 vs rc≠0/无产出，
  决定有无 `MAX_RETRY` 重付）② 读失败唤醒的 `session.jsonl`，数"最后一条已落盘条目"到"应完成步"的差
  ③ 报告既有 **`retry` 列**。
  - 当前基线：本场 **rc≠0 0/35、retry 0、stall 0** ⇒ 三项**全无样本**。触发形态包含"单次工作 p95 很长"
    （当前 23–82s/唤醒，相差 1–2 个数量级）。
- **G3（延迟 SLA）**：要求"human 插话在 <1 个唤醒内被回应" ⇒ steering 成为功能需求（当前 human 消息 0 次）。

---

## 6. 定量结论（附口径）

| 项 | 数 | 口径 / 来源 |
|---|---|---|
| 模型延迟占比 | **96%**（Δ助手 Σ=4379s / Σ进程跨度 4560s） | `mv-mv-main-20260930-135705-report.txt` 逐唤醒求和（3 视角 / 35 唤 / 墙钟 32m43s / 并行度 2.34） |
| 编排层可省**上界** | **≈3.7% 墙钟**（收尾 Σ50s=2.5% + 启动 0.68s×35≈1.2%） | 同上；启动沿用探针实测中位 0.68s（报告「启动前」列存在时区偏置，不可用其和） |
| 我们的裁剪成本 | **29ms / 30ms / 28ms（n=3）**，1.4MB 主会话 | `build_fork_source(budget)`；每 agent 首唤一次 ⇒ ≈3 次/场 ≈0.005% 墙钟 |
| durable 的压缩成本 | **一次模型调用**（`SummaryRequest{model, thinkingLevel, maxTokens}` + `phase:"summarize"`） | `packages/durable/src/harness/compaction.ts:31-48` |
| 两层持久性的落盘粒度 | 会话层 = **每条条目**；协议层 = **每唤醒一次** | `session-manager.ts:1189`；`meeting_engine.py:326` |
| 失败基线 | **0/35**（rc≠0 0、retry 0、stall 0） | 报告各 agent 合计行 |

**简单性动机 = 0 条**（明确记录，防误记）：
- "上游压缩替换 300 行"**不成立**：两者**机制种类不同**——durable 的压缩是含模型调用的**摘要任务**，
  我们的是**确定性流水线**（`meeting_fs._budget_entries`）。把它换来 = **把一条故意确定性的路径重新
  变成 LLM 路径**（与本项目已撤掉 `prepare` 蒸馏同源，`AGENTS.md:63`），且关键路径上多一次等待 + token。
- 且那段行**删不掉**：durable 的压缩作用于**它自己渲染的上下文**，我们的作用于**外进程条目集 → 产物文件**。

---

## 7. 收敛轨迹与保留分歧

**无保留分歧**（收尾三方全体 `pass`）。自由讨论中出现过并被**核码纠正**的差异，记录如下（供溯源）：

| 曾出现的主张 | 纠正 | 纠正依据 |
|---|---|---|
| "一次唤醒被中断 = 白付**全部**模型时间"（效率、铁律） | 收为"会话层未落盘的**那一步** + 协议层那条消息" | `session-manager.ts:1189` 逐步 `appendFileSync` + 稳定 sid 续接 |
| "重复付费窗口不存在"（简单） | 窗口存在，只是本地记账救不了 | `_produced` 粒度 = 消息文件数（`meeting_engine.py:233-234`） |
| "`commit_new_files` 无条件"（简单） | 异常路径到不了它 | `meeting_loop.py:463` 在 `:326` 之前抛 |
| "FakeHarness ≈ responder 注入同原理"（铁律） | 依据换成"**已有直接崩溃恢复测试**" | `fake_agent.py:4-5`（responder 只管内容决定）vs `test_meeting_concurrency.py:319` |
| "唯一简单的动机 = 少 300 行"（简单，后自行撤回） | 简单性动机 **0 条** | 机制种类不同（`compaction.ts:31-48` vs 确定性流水线） |

---

## 8. 本场顺带发现（**另案，与是否采用 durable 无关**；仅供 owner 决定）

1. **(丙) 边界：写了合法消息 + 异常路径 ⇒ 既不提交、也不回收、被下一唤醒同槽覆盖（静默丢弃）。**
   证据：`commit_new_files` 调用点只有 `meeting_engine.py:326/332`（无孤儿回收）；超时在 `:326` 之前抛出
   （`meeting_loop.py:463`）；`next_msg_id` 源 `git_ls_files`（`meeting_fs.py:715`）⇒ 下次写同一槽位。
   **代价上限**：内容已在会话层（§3.3），下次唤醒同 sid 可重建 ⇒ **≈ 一次短生成**；loop 侧**无任何日志**。
   **修法约束**（简单视角）：比例相称（**日志级**或异常路径一次提交尝试），**不得**借机构建"进度记忆 / 恢复机制"
   （那正是已否决的"库内状态进状态机"的本地版本）。
2. **事实表述须窄化**：「崩溃恢复：不丢不重（已测）」→「**已提交序号**不丢不重（`test_crash_recovery`，
   崩溃模拟在**产出前**，`fake_agent.py:104-105`）；**产出后未提交（(丙)）未覆盖**」。
   （不窄化会让 §3.3 的旧量级被重新引入，两处必须一起改才自洽。）
3. **注释与实现不符**（文档缺陷，最小对齐 = 改注释一行）：
   `meeting_loop.py:428-429` 称超时"上层可恢复重试"，而引擎 `meeting_engine.py:747-753` **只 log + 回轮询、不重试**。
4. **口径注**：`MAX_WAKE_SEC = 900`（**单次唤醒硬上限**，`meeting_loop.py:27`）≠ `DEFAULT_STALL_TIMEOUT = 600`
   （**引擎无进展阈值**，`meeting_fs.py:67`）——两个旋钮，引用须带口径。

---

## 9. 一句话结论（供产品决策入口）

> durable 是**另一层**（进程内持久化运行时）而非我们的替换品：采用它 = **转移运行时归属**，
> 在判定纯净性 / 单一事实源 / 持久化职责 / 并发模型四处越界（不可让），而它在墙钟（天花板 ≈3.7%）、
> 简单性（动机 0 条）、失败恢复（净增量仅工具副作用幂等、基线 0/35）三个维度都拿不到净收益。
> **现在不采用**；把"我们的代码会坏"（A 类，§六）与"出现新痛点"（B 类，G2/G3）两条触发登记下来，
> 并保留一条红线：**运行时归属不转移**。
