# pi-multi-viewers 设计文档

多视角协同分析：把主 pi session **fork** 成 N 个视角 agent，各带一份视角
任务书，在 meeting 协议下交锋、收敛，产出共识结果。

与 [pi-agents-helper](https://github.com/maxdai/pi-agents-helper) 平行演化，
共享 meeting 协议核心（`meeting_core` / `meeting_fs` / `meeting_engine`），
差异集中在**初始化层**（fork 源生成 + 视角注入 + cwd）。协议行为定义
（信息层/流程层分离、配额语义、状态机推演）见上游设计文档
`../pi-agents-helper/docs/pi-helper-design.md`，对本项目同样有效。

---

## 一、fork 源：三种模式

首唤不用 `pi --fork`（全量拷贝、且无法在尾部注入切换叙事），而是由本地
循环**生成 fork 源文件**（`meeting_fs.build_fork_source`），再用
`pi --session <fork 源> --name <分析名>-<视角名>` 打开。

> **下列产物数字的锚**（口径要求见 §二）：产物侧，2026-09-10，本仓库
> 主 session（≈6.8k 条 / 15MB）。**条数/MB 随主会话增长漂移**（每次跑值
> 不同），故本节数字只作量级示意；消费侧数字（tokens）一律带唤醒序号。

| 模式 | 做法 | 产物量级（锚见上） | 适用 |
|---|---|---|---|
| `budget`（默认） | 从 compaction 边界起 + 折叠（丢 thinking、旧工具输出换省略标记、长参数截断）+ 按预算从尾部保留，更早的丢弃并写「上下文说明」preface | ≈0.8k 条 / ≈0.9 MB；`est` 以**预算为上界**（preface 计入、边界回扩可
略上浮——精确式见 §二），丢弃数记在 `forkSourceDropped` | 长会话**唯一可行** |
| `compaction` | 从最后 compaction 的 `firstKeptEntryId` 起，内容原样（不折叠） | ≈2.9k 条 / ≈5.7 MB | 中小会话（零信息损失） |
| `full` | 全部条目（源会话的**忠实拷贝**，含其 compaction 条目与可见性
边界——我们不做窗口构造、不改写历史） | ≈6.8k 条 / ≈15 MB | 小会话 / 验证 |

**replay 可见性（实测，2026-09-10）**：pi 的 replay 取"路径上最后一个
compaction 的 `firstKeptEntryId` 起 + 其后的条目"——窗口内含 compaction
时，锚点之前的条目（含我们的 preface）会被静默丢弃。因此 budget 模式
**移除窗口内全部 compaction 并桥接 parentId**（不变量 I4）；compaction
模式的锚点由构造保证在产物内；full 模式是忠实拷贝，可见性同源会话。

无 compaction 的源（如引导 session）：`compaction` 全量兜底（标记 `full`），
`budget` 仍跑折叠与统计（干净源下几乎无操作）。

### 为什么需要 budget（容量事实，2026-09-10 实测）

- fork 携带的是 session **原始条目**；主 pi 实际发送的上下文由压缩层在
  **渲染时**生成（不在条目里）——同一 session：原始条目文本 ≈930k tokens
  （消息预算侧，2026-09-10），而主 pi 每次请求 ≈185k–237k tokens
  （唤醒序号不适用；随主会话增长）。
- 模型窗口 1M，每次请求含 384k completion 预留 → 消息预算 ≈664k tokens。
- 实测：`full` 首请求（唤醒 1）1,303,280 tokens、`compaction` 934,630
  tokens → 均被 provider 400 拒绝；`pi --fork` 原生命令同样超窗（731,331）。
- `budget` 首唤（唤醒 1 首请求）≈132k tokens → 讨论完整收敛
  （3 视角 / 15–19 分钟；两次 e2e 实测）。

---

## 二、规模口径（单一事实源；README、AGENTS.md、本文件 §一 三处均引本节）

1. **产物侧指纹**（header 自描述，`meeting_fs.read_fork_stats` 单点解析）：
   - `forkSourceTokensEst`：同一基准构建的确定性结果（三 agent 恒同值）
     ——用于校验"产物是否同源 / 裁剪是否符合预期"，**不预测请求规模**；
     字符数 / 3 估算，字段名带 `Est` 即为提醒。**以预算为上界**（非"钉住
     值"）：`est ≤ max(预算, est(末条)) + Σ est(回扩条目) + est(preface)`；
     工程余量 ~53 万 tokens（消息预算 664k 量级），无需为此加保护。
   - `forkSourceDropped`：**双口径合计** = 预算裁剪丢弃数 + 结构规范化移除数
     （移除窗口内 compaction 条目——见 §一 与不变量 I5）；**仅 `budget`
     模式写该字段**（compaction/full 不做预算裁剪，header 无此键——读者
     勿以为三模式皆有）。日志与验收用（丢弃数为 0 而规模远超预算 = 异常
     信号）。产物须闭合：源保留区条目数 = 产物非 preface 条目数 +
     `forkSourceDropped`。
2. **消费侧规模（验收/成本基准）**：**唤醒 1 的第一次请求** `input +
   cacheRead`（含系统提示与工具定义）。引用必须带唤醒序号，否则数字不可比。
3. **校准比（est → 真实）**：唤醒 1 ≈ **1.66×**（e2e10 三点独立样本：
   1.656 / 1.657 / 1.659，±0.2%）；唤醒中后期至 ~2.7×——**不得固化为
   2.0×**（不同测点值不同）。
4. **预算只约束"基线"**：`budget` 管的是 fork 源；**运行规模随该 agent 会话
   累积增长**（实测 w1 132k → w5 192–207k；满程外推 290–350k，标注为外推）。
   容量估算不得用 "est × agent 数 × 轮数"。
5. **预计值不得用作验收口径**：日志中的派生数字只是可读性便利。

### 观测面契约（可观测性）

> 原则（e2e14 自审裁定）：**日志是纯信息层**——零判定输入；观测面的
> 格式/消费者/成本必须显式成文（此前 `loop-*` / `wake-logs` 在
> `design.md` / `README.md` grep **零命中**，"有没有时间戳"要读源码才能
> 回答——这本身就是症状）。

### 三域 owner（事实源边界）

| 域 | 事实 | 性质 | 取数 |
|---|---|---|---|
| bare（git） | 消息 / frontmatter / 协议 / 推进节奏 | **判定域** | 现场派生（无状态） |
| loop log | 进程事实（唤醒 / 完成 / 超时 / rc） | **无家** | **就地捕获**（零解析） |
| pi session | LLM 运行事实（usage / stopReason / 时间戳） | 有家（文档化 schema） | **冷路径读**（单一适配器） |

### 观测面登记

| 面 | owner | 格式 | 消费者 | 参与判定 | 成本档 |
|---|---|---|---|---|---|
| `loop-<agent>.log` | loop + engine（stdout 重定向） | `[YYYY-MM-DDTHH:MM:SS.mmm] <agent>: <msg>` | 人（grep/肉眼）+ `--report`（**仅登记字段**） | **否** | O(1) 捕获 |
| `wake-logs/<agent>-<epoch>.txt` | loop | `CMD: <shlex.quote 单行>` | 人（排错第一手段） | 否 | O(prompt) |
| `status-<agent>.json` | loop | `{"sessionID": ...}` | 流程（崩溃恢复） | 是（恢复用） | O(1) |
| `pi-sessions/fork-src-*.jsonl` | pi | 文档化 session schema | fork 构建 + `--report` | 否（报告用） | O(MB) 全量 → **禁轮询** |
| `result.md`（固定位） | resultWriter loop | 结论文档 | 人 | 是（收尾判据） | — |
| `--report`（视图） | observability | 文本行 | 人（**三个出口**，见下） | **否**（不得升级为验收 gate） | 冷路径一次性 |

**报告的三个出口**（同一 `build_report`，同一份内容）：
1. **`--follow` 结束**——viewer 在 done 分支自动附报告（`human_viewer._print_report`）。
   这是**用户通道自带**：用户执行 `!!` 命令就在结束时直接看到，**零 LLM 参与**
   （此前只靠 prompt 要求主 pi"记得转述"——那是 LLM 依赖，会漏；机制化后
   用户必然看到）。
2. **`--cleanup`**——删目录前最后一次可读（结果与 1 重复出现是刻意的：
   不同时点各看一次，且清理后现场已不存在）。
3. **`--report`**——独立入口（中途查看 / 脚本消费）。
`--view --since`（主 pi 增量轮询通道）**不附报告**——它面向 LLM，
输出进 context，报告对模型无用且占 token。

### 本轮边界（`mv.analysis-start`）

fork 源尾部在切换叙事之后追加一条 `custom_message` 边界条目（`meeting_fs.
BOUNDARY_TYPE`）——**显式登记"历史（fork 携带）/ 本轮"的分界**。

为什么必须显式：`--report` 的 LLM 段要统计**本次分析**的 usage，而 session
文件里同时含 fork 携带的历史条目（也有 assistant + usage）。按条数/时间戳
推断都会漂移（切换叙事改措辞、时钟精度）。2026-09-11 实测：不带边界时报告
把 717 条 fork 历史算成"本轮 367 次响应 / input 1.2M / cacheRead 136.6M"。

**viewer 进度行（三样观测面）**：`--follow` 每轮在【状态】之外打印
【进度】行——`meeting <消耗>/<上限> · … ｜ freezing <已冻结>/<总数>（名单）
｜ rr → <下一位>`。**状态名一律用协议术语**（`meeting`/`freezing`/`rr`；
译成中文会引入第二套命名，"冻结"到底指 freezing 还是 all-freezing 说不清）。
判定与 `--report` 共用 core/engine 单一实现（`meeting_speak_count` /
`frozen_agents` / `rr_next_speaker`），且共用调用方已读的 `msgs`——
**零新增 bare 读取**（实测改动前后同为 6 次 run 调用 + 1 次 cat-file 批读；
`rr` 位置仅在 RR 阶段按需调权威实现）。进度行**只在变化时打印**（避免刷屏）。

**报告的打印位置**：`--report`（手动，任意时刻）+ `--cleanup` 前（自动，
删目录前最后一次可读——目录删后 `--report` 不可用）。cleanup 层对报告
fail-open（报告失败不阻断清理，且**打印**失败原因不静默）。

消费规则：`meeting_fs.iter_after_boundary` 只产出边界之后的条目；**未找到
边界（老产物/手工 session）→ 返回空、按 n/a 处理，不得退回全文扫描**
（那正是修掉的口径错误）。pi 对 `custom_message` 条目的容忍已冒烟验证。

### 登记字段（无家就地捕获）

唤醒完成行追加两个字段——**只有这里**产出，`--report` 是唯一读者：

- `elapsed_ms=<int>`：**跨度 = pi 进程生命周期**（spawn → exit，monotonic 差值）；
- `rc=<int>`：进程返回值，**总是写**（超时/被 kill 路径不写 = 缺席）。

### 不变量与纪律

1. **日志零判定输入（绝对）**：全仓无一处解析日志内容参与流程判定
   （唯一例外曾为 `--wait` 的 `glob(loop-*.log)` 存在性分叉，已删除）。
2. **缺席 ≠ 0**：观测拿不到的显示 `n/a`，绝不允许把"没测到"写成 0。
3. **一个数字一个口径**：进程跨度（`elapsed_ms`）≠ per-response 跨度
   （session 时间戳差）≠ 墙钟跨度（commit 时间差），必须标名、不得混算。
4. **取数判据"家有无"**：① 无家 → 登记（给它造家）；② 有家且读它不需
   越界假设（文档化字段）→ 复用；③ 有家但只能靠未文档化的私有细节读出
   → 按无家处理。配套：非判定性 / 可降级性（删产物行为不变）/ 成本档。
5. **成本档准入**：O(1) 捕获鼓励；O(小) 冷路径允许；O(MB) 全量解析
   **禁轮询/常驻**，仅冷路径按需。

### 明确不做（及理由，e2e14 §4）

不做第二持久化面（信息不减就不新增数据面）、不做心跳文件/常驻监控进程
（可从 `/proc` + HEAD 派生；第二事实源 → 双写/残留/一致性成本）、不在
轮询路径消费 MB 面、不做运行期 LLM 评分（有效性判断留给人 + result.md）、
不做目录内 retention（cleanup 是唯一清理点）、message frontmatter 不加
时间戳（第二事实源 + 该字段由 LLM 写，不可信；权威时间 = commit 时间）、
日志不 JSON 化（主消费者是人）、报告不自动落固定位（视图不占"家"）。

## 数字的归宿（一个数字只留一个"家"）

| 类型 | 例 | 去处 |
|---|---|---|
| **结构性质**（不随数据漂移） | "批量读一次进程 / 逐条读 O(n) 子进程" | **docstring**（随函数迁移，不得丢失） |
| **修复依据的实测值** | 733ms/43.6ms、16.8×、1054.6ms→1.0ms | **commit message**（带口径 + 来源） |
| **长期可复用的口径数字** | 构建 181ms/55MB、pi 会话 RSS ~1GB、校准比 1.66× | **本节**（design.md 口径） |

判据：**会在下一次评审中被引用吗？** 会 → 本节；只解释本次为何这样改 → commit。
commit 是溯源记录、本节是长期引用点——不并存两份权威值（长期引用点漂移时
就地更新带新口径）。
**术语注意**：上表第一类**不称"不变量"**——本仓"不变量"已专指 fork 源产物
结构的 I1–I5（docstring / 设计文档 / 测试名三处对齐），一词两义会造成歧义。

### 数字的四个类别（每个数字必须能回答"它是什么"）

| 类别 | 例 | 要求 |
|---|---|---|
| 配置派生 | 消息预算 664k = 1M − 384k | 附推导式与重估触发 |
| 实测 | 132.2–132.4k、1.66× | 附口径与测点 |
| 外推 | 满程 290–350k | 附依据与触发 |
| 事后拟合关系 | "基线 ≤ 20%×（窗口−预留）" | 标注为事后归纳（见下） |

### 预算取值的依据来源（honest attribution）

- **初版取值来源**：按"对齐 MC 在主会话保留的未丢弃量（88k）"设定——该
  判断后证为**刻度混淆**（88k 是真实 token，est 应对应 ≈53k）。
- **现行重估公式**：`基线 ≤ 约 20% ×（模型窗口 − 输出预留）`——当前实例
  80k est ≈ 132k 真实 ≈ 664k 的 20%。**该公式是事后归纳，不是原始设计
  目标**，用于将来窗口/预留变化时的重估，不用于追溯解释当初取值。
- 参考点：pi 自身默认 compaction 为 `keepRecentTokens=20000` /
  `reserveTokens=16384`（`DEFAULT_COMPACTION_SETTINGS`）——讨论 agent 需要
  更宽的近期窗口，故取更高的量级。

---

## 三、决策记录

### 已定（关键项）

1. **fork-only**：唯一模式；缺 fork 源 = 三层 fail-fast，无静默退化。
2. **fork 源由本地生成**（不用 `pi --fork`）：可在尾部注入切换叙事，
   且形态可控（模式/统计自描述）。
3. **切换叙事**：fork 源尾部注入 2 对对话（"停止旧任务" → assistant 询问
   → 新任务说明 → assistant 确认）——用最后一段对话切断历史叙事惯性。
   主题取自 `protocol.json.topic`（**不**二次解析 question.md）。
4. **budget 为默认模式**（长会话唯一可行；compaction/full 留作中小会话与验证）。
5. **模型引用按契约拼接**（provider + id 两字段，无条件拼接）——不按值的
   形状猜（形状启发式会把 provider 丢掉，静默解析到同名模型）。
6. **viewers/ 稳定视角资产**：`--prepare` 快照进 `spec/agents/`（可按场改），
   文件名即 agent 名（中文合法），排序定 starter/RR/resultWriter，≥2 视角。
   **视角文件只写视角内容**——身份（"你是 X"）、参与者名单、消息格式、
   独立纪律都由脚本从 agent 名生成（agent 名 = 文件名，单一来源；手写
   身份必然与文件名漂移）；**空视角任务书拒绝启动**（无 lenses 的 agent
   会让多视角退化成同名随机视角）。
7. **协议单一来源 = `bare HEAD:protocol.json`**（`meeting_fs.read_protocol`
   唯一实现）：engine/loop/viewer/status 全部经它读取，**不读 workdir 本地
   副本**——本地副本是 LLM 可写的工作副本，判定读本地等于把流程判定暴露给
   被审查者。附带修掉 `result_writer` 的默认值求值缺陷（原实现
   `proto.get("resultWriter", participants(workdir)[-1])` 的第二参数无条件
   求值：每次读两遍协议，且 participants 为空时抛 IndexError——即使
   resultWriter 已配置）。
8. **状态判定复用状态机定义**：`check_status` 的 concluded 判定调
   `meeting_engine.aggregate_mode`（core 单一判定），**不用 `git grep` 全文
   匹配**——行文本匹配会被 result.md / 消息正文里的 `type: concluded`
   误触发（实测误报 done → `--wait` 落无上界轮询）。
9. **插话走 extension（零 LLM）**：`/multi-viewers-say <文本>` =
   `registerCommand` handler 直接 spawn `human_sayer.py`（一次调用一次返回），
   结果经 `ctx.ui.notify` 反馈——**不经过 LLM**（插话本质是本地命令执行；
   经 LLM 会引入不确定性与额外延迟）。目录发现零状态文件：
   `ctx.cwd` + `sessionManager.getSessionId()` → `mv-<sid>-*` 最新
   （session 隔离）；无 sid 目录时兜底项目下最新 `mv-*`（排除
   `mv-spec-*`）并**警告降级**
   （宁可提示也不静默插错分析）。观看仍用 `!!` 流式（命令 API 无原生流式
   通道，bash 流式是平台原生能力）。
10. **question.md 的主题行措辞 = `# 分析主题：`（唯一）**：生成端
    （`gen_question` / `gen_spec_skeleton`）、模板（`spec-readme`）、prompt、
    消费端（`setup_environment` 提取 `protocol.topic`）四处同一措辞；
    消费端**只认它**，旧措辞 spec → fail-fast（明确报错，不静默退化）。
    曾出现双轨（生成产 `# 讨论主题：`、消费端写兼容循环兜两种）——那
    正是"补丁掩盖设计缺陷"的形态（不改生产、只兜消费端）。
11. **stall 接管 = 心跳式软仲裁（非互斥）**：非 rw 在无进展超时时接管收尾，
    先 pull 重检共享事实（concluded / result.md）→ 写接管声明 commit
    （**只为推进 HEAD**，使对方 `_stall_elapsed` 归零而退出该分支）→
    finalize。毫秒级同轮窗口存在（双方都可能 finalize），但**不是死锁**：
    靠 push 容错 + 下轮 concluded 退出兜底，产物始终唯一。声明只降低并发
    概率，**不构成互斥保证**（注释勿写"天然唯一"）。实测（2026-09-11，
    381 测试中的 `TestStallTakeover`）：a 进入接管 0.11s 内完成声明→收尾→
    concluded，b 晚 1.4s 只见 concluded 即退出——正常时序下软仲裁生效。
12. **`result.md` 的文件名与有效性阈值 = `meeting_fs.RESULT_MD` /
    `RESULT_MD_MIN_BYTES`**：产品级核心产物，此前 8 处字面量分散在
    engine/loop/viewer/start_discussion/fake_agent（`repo.git` 早已收归
    fs 层，产物名却没有家）。文件叫什么、多大算有效——同一概念的两个
    数字住在一起。
13. **分层 = core ← fs ← engine ← loop，`start_discussion` 是组合层**
    （S2 拆分后）：三项职责按"谁消费"拆到两个同层模块——
    `spec_gen.py`（分析前的静态产物生成 + 为其服务的 pi 环境探测）、
    `observability.py`（运行期只读观测）；主文件保留 CLI 分发、环境创建、
    启动、清理与编排。**判据是职责而非行数**：spec 生成与观测各自 350–520
    行、概念内聚，再拆会制造碎片（import 网变复杂而收益递减）。
    两个子模块只依赖底层（core/fs/engine），互不依赖、不反向依赖主文件；
    主文件 re-export 子模块公开符号，保证 `from start_discussion import X`
    的既有调用方（tests/wrapper）零改动。
    **拆分暴露的纪律**：`mock.patch` 必须打在**符号定义处**（拆前
    `start_discussion.check_status` 与定义处同址，拆后 mock re-export
    不生效——本轮 4 处测试因此假绿/失败，已改到 `spec_gen` /
    `observability`）。
14. **报告附在 `--follow` 输出末尾（机制化，不依赖 LLM）**：`--follow` 是
    用户直接执行的通道（`!!`），done 时自动打印报告——用户零操作看到运行
    事实。**为什么不能只靠 prompt**：让主 pi"记得跑 `--report` 并转述"是
    流程依赖 LLM（会漏、不可验收），正是本项目一贯要消除的形态；报告既然
    是给用户的，就该长在用户直接看的通道上。`--view --since` 不附（主 pi
    通道，进 context 且对模型无用）。
15. **消费命令的目录可省略（自动发现）**：`--view/--say/--status/--report/
    --wait/--cleanup` 不带目录 → 按 cwd + `PI_SESSION_ID` 发现当前分析
    （`mv-<sid>-*` 最新；兜底最新 `mv-*` 并警告；判据 = 含 `repo.git`，
    同 engine"bare 是分析存在的唯一标志"；排除 `mv-spec-*`）。
    **动机**：唯一知道路径的是 `--start` 的输出，此前每个消费命令都要求
    传它 → 主 pi 必须把长绝对路径记在 LLM 上下文里复用（改错/截断/相对
    路径都出过）。发现逻辑下沉后**路径不经过 LLM**。
    实现单点 = `observability.find_current_dir`（wrapper 的 `resolve_dir`
    与 extension 同走 `observability.py --find-dir`——此前只有 extension
    里一份 TS 实现，wrapper 侧完全没有，两份口径会漂移）。
    `--say` 两形态**按参数个数区分**（1 个 = 文本+自动发现；2 个 = 目录+
    文本）——不是按值猜语义。
    配套：`--status` 在 done 时打印 `[result] <路径>`（目录可省略后调用方
    无法自己拼 `<目录>-result.md`，路径必须由机制给出）。
16. **prompt 里的失败判据用退出码，不用报错文本匹配**：原 prompt 要求
    "若报错含'未找到 viewers/ 目录'…" —— 匹配 stderr 文案，wrapper 文案
    一改就静默失配（LLM 会以为没报错而继续）。改为"命令非零退出 → 停下来
    读报错原文问用户"：判据降为退出码（wrapper 的 `fail()` 保证 `exit 1`），
    原因解释交还给输出原文。
17. **git 守卫范围 = 从讨论 workdir 发起的操作**（`GIT_CEILING_DIRECTORIES`
   注入于 spawn）；主项目仓库不在守卫范围（agent 的 cwd 就是主项目，其
   约束归指令层 + 主项目 `.gitignore`）。要拦主仓库需换机制类（沙箱/钩子），
   经评估收益不支撑扩面。

### 被否决方案（含重估触发条件）

| 方案 | 否因 | 重估触发 | 所在位置 |
|---|---|---|---|
| **省一次重读**（首唤时把 tail id 从 `build_fork_source` 传给 `append_handoff_turns`） | ①收益仅 0.011s（budget 产物；full 产物 134ms + 37MB 峰值）；②方案自败（保留重读兜底则被指瑕疵的推导代码一行未减）；③新增静默失败面（tail id 可能过期 → 接错节点）；④把 session 格式知识泄漏到 loop 层。同层替代已评估未采纳：`append_handoff_turns` 只保留尾行（不跨层传状态、不新增失效面）——量级 0.4s vs ~130s/唤醒（0.3%） | 源 ≥100MB 或 N≫3（内存 ≈2.5×文件大小×N） | `meeting_fs.append_handoff_turns` / `meeting_loop` 首唤路径 |
| **共享 budget 基座**（三 agent 共用缓存） | 引入持久状态 + 失效规则 + 跨进程原子写/清理义务，与"单一事实源/确定性归 loop/无静默"冲突；收益 ≈181ms×3（本机、15.3MB 源实测；旧记录写 ≈0.28s×3——口径不可考，两者差 55%，按修订记录并列不静默替换），相对 ~130s/唤醒可忽略 | N≫3 且会话至 100MB 量级 | `meeting_loop` 首唤路径 |
| **裁剪改流式 / 环形缓冲** | 收益 = 内存峰值 +37MB **与解析时间**（实测 `json.loads` 占构建耗时 **49%**、约 90% 解析条目最终被预算弃用）——两者都随源规模线性增长；会扩大"先折叠再裁"不变量的证明面 | 源规模使峰值内存或解析耗时成为实际瓶颈时（观测点：首唤日志的构建耗时/峰值 RSS） | `meeting_fs._budget_entries` |
| **两阶段裁剪**（先廉价估算定窗，再只折叠保留区） | 收益 ≈7ms，为可忽略收益引入复杂度 | 折叠成本成为可测瓶颈（当前 0.01s/2788 条） | `meeting_fs._budget_entries` |
| **预算提前配置化**（进 protocol/spec） | 灵敏度低：每 10k est ≈ 2.5% 消息预算；80k→53k 仅省 6.6%，代价是保留窗口缩短；配置面成本（每个读者须知其存在/语义/边界） | 出现明确的"按讨论调预算"需求 | `meeting_fs` 常量块 |
| **改写锚点**（把被裁掉/被移除的 compaction 的 `firstKeptEntryId` 批量改写为窗口内条目） | 语义上伪造历史字段（锚点是 pi 写的记录，不是我们的）；且中间锚仍会悬空——**已撤回**（其测量 0.07ms/0.27ms、est 恒等**不并入成稿**） | 出现必须让所有历史锚都可解析的消费者时 | `meeting_fs` compaction/budget 边界 |
| **保留 `_join_model_ref` 幂等特判** | 与"不得按形状猜"自相矛盾；对"`<provider>/` 开头"的命名空间 id 会少拼 provider（同类误判仍在） | 出现按契约必须传完整 ref 的来源时 | `start_discussion._join_model_ref` |

### 结构拆分的触发条件（当前不拆）

`meeting_fs` 的 fork 源区块现为一个章节，实际包含 11 个函数：
`read_fork_stats` / `_est_tokens` / `_entry_text` / `_shrink_value` /
`_fold_entry` / `_budget_entries` / `build_fork_source` /
`append_handoff_turns`，以及同章的引导/回流三函数 `_registry_log` /
`build_bootstrap` / `preserve_result_md`。
（列举集合须同批对照——文档"列举集合"与实际集合失配是 e2e10 评审发现的
一类问题；完整制度见 docs/test-methodology.md。）
满足任一条件时再拆为独立模块（或 `meeting_fork.py`）：

- 引入**摘要生成**（对丢弃区做 LLM 摘要，而非只带 compaction summary）
- 引入 **MC 增强**（读 Magic Context 的 compartments 提升旧史摘要质量）
- **预算可配置**（模式参数化到 spec/protocol）

---

## 四、记录项（不修；每项必带**现在就能用的观测点**）

| 项 | 实测/性质（口径） | 触发条件（观测点） |
|---|---|---|
| 三处冗余 pass（`_entry_text` 双算 / dropped 全量重算 / `_shrink_value` 先拷贝再比较） | 合计 ~11ms = 构建的 **6%**（本机、15.3MB 源） | 首唤日志已含**构建耗时与峰值 RSS**（`fork 源（… 构建 181ms/55MB）`）→ 构建 >1s 时复测占比（占比是插桩型判据，非监控） |
| O(源) 内存（全量 `entries` + `folded` 驻留） | 15.3MB 源 → RSS 峰值 **55MB**（≈3.6×）；3 loop 并发 ≈165MB | 同一日志的 RSS 字段 >~500MB，或源 >~100MB |
| **pi 会话常驻内存（fork 场景主导项）** | 实测 **~1GB/个**（935 / 1157 / 954MB，`ps -o rss`；本机 16GB、当时会话长度含扩展）——比 loop 侧高一个量级，容量规划须以它为准 | N≥8 或内存紧张时复核（`ps -o rss` 逐 pi 进程） |
| 每 agent 各自构建一次 fork 源（不共享） | ≈181ms×3；三 loop 是独立进程，共享需跨进程协调/失效/原子写义务（与「共享 budget 基座」否决同因） | N≫3 且会话至 100MB 量级 |
| `_est_tokens` 对空文本返 1 | 量级 <0.01%；由 I4（集合身份）消解——est 是集合近似指纹而非数值承诺 | 若将来把 est 用作容量硬判据 |

**口径纪律**（方法 14 扩展）：改数字先判“漂移 vs 复测”；**无锚旧值按
修订记录处理**（并列旧值与其口径不可考，不静默替换）。首个实例：
「共享 budget 基座」行的收益由 ≈0.28s×3 修订为 181ms×3。

---

## 五、已知边界与未覆盖

- `compaction` / `full` 两模式在长会话下的实跑行为未验证（分析中仅用
  `budget`）——`full` 已知超窗，`compaction` 在中小会话可用。
- MC 增强路线未实现：fork 源**构建**不依赖任何扩展（纯 pi 语义 + 预算 +
  折叠）；**运行期**上下文受环境扩展（如已安装 MC）的渲染期裁剪影响
  （实测：三份 fork 会话各有 67–70 条被 MC 丢弃的旧内容）；未装扩展时的
  轨迹取决于 pi 核心 compaction（本环境未观测）。
- 预算取值 80k vs 53k 的**产出质量对比未做**：质量无单点判据、N=3 欠功率。
  若重启该实验，须**先登记判据**（可机械核查：覆盖 question.md 评审项数、
  给出 `file:line` 次数、是否达配额）与比较单位（per-agent / per-wake）。
- 图片块与非字符串叶子在预算估算中的计入未覆盖（当前余量充足）。
