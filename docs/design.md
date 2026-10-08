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

> 本机制建立在 pi 的 **session jsonl 文件**上——那是一处**外部契约**，
> 依赖清单与迁移触发条件见 **§六**。

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
**同理**（2026-10-03 修）：剔除旧会话的 `thinking_level_change` 也必须
**桥接其子条目的 parentId**——pi 的上下文构建从 leaf 沿 parentId 上溯、
遇缺失父节点**静默停止**，只过滤不修链 ⇒ 断点之前的全部条目对模型不可见
（实测可达 65/13454 条）。**删除类操作必配桥接**是本项目的一条硬纪律。

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
| `pi-sessions/fork-src-*.jsonl` | pi | 文档化 session schema（`usage`/`stopReason`/`timestamp`/`thinkingLevel`） | fork 构建 + `--report` | 否（报告用） | O(MB) 全量 → **禁轮询** |
| `result.md`（固定位） | resultWriter loop | 结论文档 | 人 | 是（收尾判据） | — |
| `--report`（视图） | observability | 文本行（`--cleanup` 另落盘 `<base>-report.txt`） | 人（**三个出口**，见下） | **否**（不得升级为验收 gate） | 冷路径一次性 —— **O(session 大小)**：每 agent 读整个 fork-src jsonl（实测 3 × 789KB ≈ 2.4MB/次、50–150ms/次，×3 出口 <0.3s/次分析），**不得进入任何轮询路径**（e2e16 评审量化） |

**报告的字段集**（e2e17 评审后定稿，后续增补不计数——字段行以本表为准）——
**谓词分组 + 对照 + 事实行**，
全部**只读已有家**（session 的文档化字段 + loop log 登记字段），不新增度量、
不在 loop log 增记（同一事实两处 = 双写）：

| 组 | 形状 | 落点 |
|---|---|---|
| `usage` 合计 | `{input, cacheRead, output, reasoning}` | 每 agent 一行；`reasoning ⊂ output`（**不可相加**），缺席省略括注 |
| `requests_by_stopReason` | `{键=stopReason 原值: 计数}` | 与下一项同一行（`stopReason：toolUse 78（1234s）｜…`） |
| `seconds_by_stopReason` | `{键=stopReason 原值: Δ 合计}` | 同上；**口径 = 相邻条目 Δ 合计**（每次唤醒首条响应**也计入**；跨唤醒空闲不进入 Δ——唤醒 prompt 本身是一条 user 条目，空闲落在"上一唤醒末条 → 本次 user 条目"之间）。⚠️ 曾短暂采用"首响不计时"，理由（跨唤醒空闲）经 e2e20 评审批证伪后**已撤回**（见决策 19 的 e2e20 修正段） |
| `effective_levels` | `[thinkingLevel…]`（session 侧，去重保序） | `档位：声明 X ｜ 生效 Y ｜ ✓一致 / ⚠不一致` |
| （对照）`declared` | `pi-agent.json.thinking` | 同上——声明值与生效值**并列**（此前从没人对照过：探测失败会静默取档，spec 表面正常） |
| `扩展策略` 行 | `声明 / 生效 / strict / 降级原因` | 声明 = `protocol.extensionPolicy`；生效 = loop log 的**登记行**（首唤打一次：`扩展策略: 声明=X 生效=Y strict=0\|1[ 降级原因=…]`）→ 报告给"声明 vs 生效 + ⚠ 生效≠声明"（与"档位：声明/生效"同型；e2e24 评审 E2）|
| `终止` 行 | 分类 + 原料计数 | `终止：共识（RR 全体 pass）｜freezing N / all-freezing N / pass N / stall 接管行 N`；分类判据只有事实（bare 的 type 计数 + loop log 的"超时兜底/声明接管"字样）——**不做评分** |
| `唤醒构成` 表 | 每次唤醒一行 + 每 agent 合计 | 四端点（spawn / 首事件 / 末事件 / exit）+ 往返数 + `Δ助手` + `Δ工具` + retry + 本唤醒内的 commit；合计给 `跨度 = 启动前 + 事件内 + 收尾` 与平均/往返/retry（e2e23 分析产出，见下） |
| `跨度` 段 | 两个直标量各一行（`Σ进程跨度` / `墙钟跨度`）+ 派生量（`并行度`） | 两量**各自命名、不可互替**；**0 是合法值**（判缺失一律 `is None`——与「缺席≠0」是同一纪律的两面）；派生量的**算式永远打印**（n/a 时也打）——2026-09-27 复盘审计产出（外部引用曾把两个 span 混算：Σ1538s÷751s=2.05 vs ÷636s=2.42）；全部派生自「进程」段的登记字段与 bare 的 commit 时间，不新增度量 |

**`唤醒构成` 表的精度契约**（e2e23 多视角分析定稿）：**可推导** = 分段 Δ、
按 role 拆分、计数、retry、终止原因、区间重叠、离群 top-N；**不承诺** =
单次调用内的 TTFT/生成拆分（session 只有完成时间戳，为它插桩的收益不支撑成本）。
**纪律**：账用对账恒等式（`Σ(启动+事件内+收尾) = Σ进程跨度`——不等超 2s 时报告
打印"时间源未对齐"事实行），**归因用成本模型**，两者不得混用；跨度阈值只作
报告事实行，**不进代码分支**。

两个设计要点：
- **键 = `stopReason` 原值（含 `None`）**：名字即事实、不会过期——
  `requests_tool/final` 这类解释性命名会因"长消息也是 `toolUse`"立刻过期。
  键集合不随数据增长（新增同类数字只多一个键）；保留 `None` 键，否则
  "计数相同、时长差两个数量级"的情形会静默丢失。
- **provider 失败那笔账就在这里**：`error` 键同时给计数与时长——此前它
  完全不可见（e2e13 实测 11% 墙钟零记录；e2e17 实测区间 11%–19%）。

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
fail-open（报告失败不阻断清理，且**打印**失败原因不静默）。报告随 `--cleanup` **落盘**一份到 `<base>-report.txt`（与 `-result.md` 同级）——原先只在终端出现一次，目录删掉后无法复查（复盘时长口径时踩到，用户 2026-09-25 定）。

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
日志不 JSON 化（主消费者是人）、报告不自动落固定位（视图不占“家”；**例外**：cleanup 删目录前打印并落盘一份快照 `<base>-report.txt`，见观测面契约）。

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
   （session 隔离）；**无兜底**——未匹配即报错 rc 1（行为事实源 = 决策 15：
   破坏性命令不猜目录）。观看仍用 `!!` 流式（命令 API 无原生流式
   通道，bash 流式是平台原生能力）。
10. **question.md 的主题行措辞 = `# 分析主题：`（唯一）**：生成端
    （`gen_question` / `gen_spec_skeleton`）、模板（`spec-readme`）、prompt、
    消费端（`setup_environment` 提取 `protocol.topic`）四处同一措辞；
    消费端**只认它**，旧措辞 spec → fail-fast（明确报错，不静默退化）。
    曾出现双轨（生成产 `# 讨论主题：`、消费端写兼容循环兜两种）——那
    正是"补丁掩盖设计缺陷"的形态（不改生产、只兜消费端）。
10b. **无产出重试上限 3 + stall 兜底 600s**（机制事实，2026-09-14 补记）：
    `meeting_engine.MAX_RETRY = 3` —— agent 唤醒后未产出消息时最多重试 3 次，每次重试
    相当于**一次完整唤醒**（真场 48–87s/唤）→ 单轮最坏 **+2.5–4.5 分钟**；仍无产出则
    loop 代写（不耗该 agent 配额）。`meeting_fs.DEFAULT_STALL_TIMEOUT = 600` —— 全体
    无新 commit 达 600s 即进入"接管"分支（死锁的墙钟下限 = 10 分钟/次）。
10c. **分析目录内的代码副本 = 运行快照（非事实源）**（机制事实，2026-09-14 补记）：
    `start_discussion.setup_environment` 把 4 个模块拷进分析目录，`meeting_loop`
    **从副本运行**（`start_discussion.py:416-419` / `:710`）。因此"改主仓代码对
    正在跑的分析不生效"是**设计如此**；排查时可用副本与主仓同名文件 `diff` 溯源，
    cleanup 后副本随目录消失、不可再追。

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
15. **消费命令的目录可省略（自动发现，仅精确 sid 匹配）**：`--view/--say/
    --status/--report/--wait/--cleanup` 不带目录 → 按 cwd + `PI_SESSION_ID`
    发现当前分析（`mv-<sid>-*` 最新；判据 = 含 `repo.git`，同 engine
    "bare 是分析存在的唯一标志"；`mv-spec-*` 不在前缀内）。
    **无降级兜底**（e2e16 评审 2:0:1 裁定）：曾有"无 sid 匹配 → 取项目下
    最新 `mv-*`"的兜底，三宗罪——①破坏性操作（`--cleanup`/`--say`）会作用
    于**猜测目录**；②降级只能靠 stderr 中文文案识别（extension 曾用
    `includes("警告")` 还原布尔，文案一改静默失效，与"判据用退出码"自相
    矛盾）；③"最新"按整名排序，跨 sid 时**系统性取旧**。核查确认**不存在
    "必须无目录且必然无 sid"的设计内场景**（pi 两条通道都有 sid，终端主路径
    本就显式带目录）→ 未匹配 = rc 1 报错请显式传目录。
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
17. **CLI 层 = Python（bash 只留 shim）**：`scripts/mv.sh` 是 5 行 shim
    （找到仓库根 → `exec python3 mv_cli.py "$@"`），解析/决议/调用/展示全在
    `mv_cli.py`。**路径 `scripts/mv.sh` 不变**（prompt/README/用户习惯引用
    它）。收敛依据不是"更整洁"，而是这层产出的真实 bug **全部出自 bash
    陷阱**（`shift` 吃掉显式目录 = F5 回归、`local` 重复声明清空变量、命令
    替换里的 `exit` 不进父 shell 致双重错误消息、参数静默丢弃 = P0），且
    bash 在本项目工具链里**零行覆盖**（Python 覆盖率看不到它）——缺口只能靠
    真实 e2e 或评审暴露。收敛后逻辑进入覆盖率与进程内单测
    （tests/test_mv_cli.py），解析统一出口。子进程边界不变：start_discussion
    / human_viewer / human_sayer 仍以子进程调用（各自是 CLI 入口）。
    顺带三处"无静默"加固（bash 版是静默的）：status/report/wait/cleanup 的
    多余参数响亮失败、`--say` 多于 2 个参数响亮失败（未加引号文本会被当成
    目录）、`--view` 的 viewer 失败透传 rc 且**不打印 HEAD 游标**（给失败的
    一轮发游标会让下一次 `--since` 静默跳过消息）。
18. **配置自明性三件（e2e17 评审落地）**：
    1. **spec 永远写显式 variant**——`models.md` 两个槽都显式（model 写
       `default` = 继承本机；variant 缺省 = `DEFAULT_THINKING`）；探测失败
       时打一行 stderr 提示（**可见**，不阻断——终端直用本就没有 `PI_*`），
       而不是让 spec 表面正常、生效值静默取档。
    2. **`variant` 槽没有 `default` 别名**——`default` 只在 model 槽有意义
       （= 继承）。相邻两行同词反义会让读者必错，且别名不增加表达力
       （空已是缺省）；写了 `default` 就按原值透传（可见失败）。
       档位默认值单一声明点 = `meeting_fs.DEFAULT_THINKING`。
    3. **fork 源不携带旧会话的 `thinking_level_change`**——本场档位由 CLI
       显式传入（恒非空），旧条目既不是本场生效值、又会让 pi **跳过**写
       自己的档位条目（`if (!hasThinkingEntry) append`）→ session 里没有
       "本场生效档位"这个事实。剔除后 pi 在边界之后补写，报告才能并列
       「声明值 vs 生效值」。`model_change` **不剔除**（无 `--model` 的路径
       靠它回填主 pi 模型——活配置，不是陈旧副本）。
19. **[已被决策 20 取代，整套机制已删除] agent 进程的作用域配置（XDG_CONFIG_HOME）——关 AFT 语义搜索**：
    实测（2026-09-12）AFT 的**语义搜索**（本地 ONNX embedder
    all-MiniLM-L6-v2）让每个 pi 进程多活约 **57 秒**：带语义搜索 61.0s、
    关掉 3.3–4.4s、无扩展 2.2s（逐个扩展隔离 + 跨项目复现，非冷热/非竞争）；
    而 agent 的 pi 进程退出**在唤醒关键路径上**（loop 等进程结束才继续）→
    e2e17 那场 33 次唤醒 ≈ 墙钟 12 分钟 / 55 分钟（≈22%）。AFT 自己的日志
    显示它在 ~2s 内已 shutdown 完毕，"多活的 57 秒"像 ONNX 运行时线程/句柄
    残留（上游问题），我们不等它修。
    **做法**：不改用户配置（**主 pi 完全不受影响**），而是在建环境时生成
    `<base>/agent-config/`（`meeting_fs.build_agent_config`）——
    `cortexkit/aft.jsonc` = 用户配置键原样保留 + 语义搜索关闭（新旧键名
    都写）；`cortexkit/magic-context.jsonc` = 用户配置**逐字拷贝**（MC 行为
    不变）；再由 `meeting_loop._spawn_env` 把该目录作为 agent 进程的
    `XDG_CONFIG_HOME` 注入（**目录存在才注入**——老环境不改行为）。
    影响面实测：pi 自身不读 XDG_CONFIG_HOME（dist 零命中）、mcp-adapter
    不读、只有 MC 读（故拷贝）。副作用：agent 进程内
    `$XDG_CONFIG_HOME/git/config` 也随之改变（协议本就禁止 agent 跑 git，
    且有 GIT_CEILING_DIRECTORIES 兜底）。目录随讨论目录删除 → 零残留。
    已知边界：AFT 还读项目级 `<project>/.cortexkit/aft.jsonc`，用户项目若
    有该文件且显式开启语义搜索，可能覆盖本配置（本仓无该文件）。

    **「完全屏蔽 AFT」——已评估，暂不做（用户 2026-09-12 定：先保持现状，
    以后单独测试）**。事实与口径：
    - 现状只关**语义搜索**，AFT 仍加载（trigram 索引、LSP、工具集、每 agent
      一个 `aft` 索引服务）。剩余实测成本：无扩展 2.2s / 仅 MC 2.8s /
      AFT（语义关）3.3–4.5s → **~1–2s/唤醒 ≈ 2% 墙钟**（33 唤醒 ≈ 1 分钟
      / 55 分钟）。最初那 57s 已经拿回，**小 session 上几乎无剩余收益**。
      **限定（2026-09-14 注记）**："2%" 仅来自**小 session 单点探针**、**不可外推**——
      生产规模下 AFT 的收尾成本可达数分钟（承重证据 = 决策 20 的 e2e21 收尾占比
      66–78% vs e2e23 ≈0%；本节旧引的"445.9s 对照"已在 2026-09-27 审计中退出证据位）；
      "是否屏蔽 AFT"以决策 20（默认 mc-tools / none）为准，本句仅存档。
    - 三种屏蔽方式：`--no-extensions` + `-e <MC 扩展入口>`（AFT 不加载、
      MC 保留；入口可从 `~/.pi/agent/settings.json` 的 `packages` + 包的
      `pi.extensions` 解析，不硬编码）；`--pure`（全关，已实现）；现状。
    - 保留 AFT 的两个非速度理由：① AFT 默认 `hoist_builtin_tools` →
      agents 的 read/write/edit/bash 用的是 AFT 实现（**与主 pi 一致**），
      屏蔽后行为会变；② 每 agent 一个 `aft` 索引服务并发访问主项目索引。
    - **重估触发条件**：动机从“更快”变为“隔离/行为一致性”（如验证纯 pi
      工具下 agents 表现、或担心并发索引）；届时做成**协议层开关**
      （如 `agentsExtensions: default | no-aft | pure`）而非硬编码，保留
      两种形态可 A/B。

    **e2e19 自审（多视角审阅本次实现）修正三处**：
    1. **键名方向写反**：AFT 现行键是 `semantic_search`，`experimental_
       semantic_search` 是旧名（上游 `CONFIG_MIGRATIONS` 的 `oldKey`；读取端
       `semantic_search ?? experimental_semantic_search`）。此前恒写**旧名**
       → 靠迁移生效，上游一旦移除旧名即**静默回吐 57s**。现恒写现行名 +
       **删除**旧名（并存会触发上游 migration-conflict 警告，我们没理由制造）。
    2. **`_strip_jsonc` 会改写字符串值**（活着的 bug）：旧实现用逐字符状态机
       跟踪字符串、却把尾逗号正则作用于整段拼接文本 → 字符串里的 `", }"` /
       `", ]"` 被静默改写（结果仍是合法 JSON，`json.loads` 挡不住）。现按
       **字符串切分**（`re.split` 保留分隔串，正则只作用于偶数下标 = 串外文本），
       新增两条回归用例（先红后绿验证过：旧实现下红）。
    3. **AFT 关不掉/有副作用的条件**（评审核到源码，此前 docstring 把
       **确定覆盖**写成“可能覆盖”，并漏了用户侧副作用）：
       - 项目级 `<project>/.cortexkit/aft.json[c]` 对安全名单键（含
         `semantic_search`）**确定覆盖**作用域层，且上游**不打警告** → 目标
         项目自己开了语义搜索时我们关不掉（主场景暴露，本机免疫）；
       - 上游每个 pi 进程跑 `migrateAftConfigLocations()`，目标 =
         `configHome()/cortexkit/aft.jsonc`——被我们的 XDG 注入换成**临时
         副本**；legacy 源（`~/.pi/agent/aft/aft.json[c]`、`~/.opencode/aft/…`、
         项目级 .pi/.opencode 同形）会因语义不同被 **`unlinkSync` 删除**并留
         `.MOVED_READPLEASE`（指针指向将被 cleanup 删除的临时路径）；
       - 新增 `meeting_fs.af_resolution_notes(project_dir, home_dir=None)`：
         建环境时**命中才打印事实**（四条路径 × json/jsonc + `OPENCODE_CONFIG_DIR`
         条件项），不判定、不评级、不进 `--report`；影响面（谁读 XDG）写成
         **快照 + 重核动作**而非不变量。
    4. **（e2e20 评审批修正，撤回两处错误）**：
       - **边界判定改按字段**（`meeting_fs.iter_after_boundary`）：此前用子串
         `BOUNDARY_TYPE in line` → 主 session 历史里含该字面量的普通条目
         （引用它的 fixture 文本等）被当成边界 → **其后全部历史算进"本轮"**，
         报告 LLM 段虚高 ~4 倍（实测：误判起点早 490 行；assistant 310→66、
         Δ 40.7→9.8 分）。这也正是"Δ 合计 > 进程跨度"之谜的根因。
       - **Δ 口径"首响不计时"撤回**：该方案基于上述错误归因（以为超量 Δ 来自
         跨唤醒空闲）。实测证伪——每次唤醒都写 user 条目，空闲从不进入 Δ；
         被排除的是每次唤醒首条响应的真实耗时。现恢复"相邻条目 Δ 合计"。
       - **`_strip_jsonc` 改两趟法**：单趟"按字符串切分 + 奇偶下标"假设注释里
         无引号 → `/* say "hi" */` 这类输入解析失败（**功能回归**，旧状态机
         本是正确的）。现为"状态机只去注释 → 对无注释文本切分去尾逗号"。
       - 其余：写入侧改用具名推导（S2）· 清单对齐上游 `paths.js`（用户级
         OpenCode 根 = `configHome()/opencode` 非 `~/.opencode`；`.cortexkit`
         上游只读 `.jsonc`）· 删测试专用参数 `home_dir` 与 `proj` 别名 ·
         测试改强断言（不依赖进程环境变量）。
    5. **gate 判配置文件而非目录**：目录在、文件缺的半成品状态照注入会让 AFT
       静默回落默认（= 57s 回吐）。现判 `agent-config/cortexkit/aft.jsonc`
       （具名推导 `agent_config_aft_file`，写入侧与判定侧共用），未注入时打一行
       **事实**日志；`build_agent_config` 只返回 warnings（目录不再返回——
       生产端本来就不用），并删掉只有测试在用的 `source_config_home` 参数。
20. **agent 进程扩展策略三档（默认 mc-tools）**（2026-09-13 定零扩展、
    2026-09-14 用户定为默认 mc-tools 并改“允许而非要求”；取代决策 19）：

    **证据（两类插件都在关键路径上，都是分钟级；承重证据 = A 类真场 + 上游账本）**：
    - **AFT**：大 session 上进程退出前多活数分钟（结构性原因：`meeting_loop` 等进程
      退出才继续 ⇒ 收尾段全额进用户等待）。**承重**：e2e21（插件在场）收尾段占进程跨度
      **66–78%**（进程总跨度 3676/3681/3709s，收尾 2769/2881/2437s）vs e2e23（零扩展）
      收尾 ≈0%。
      ⚠ **一条已撤回的引用**：本节曾引"全扩展收尾 **445.9s** vs 只留 MC **0.5s**"当
      受控对照——那批属 2026-09-13 装置有缺陷的探针（`docs/test-methodology.md` #26 已记），
      且 445.9/0.5 只是运行账本两条**总时长**（458s / 7s）的收尾段拆解，原始分段查无、
      n=1、提示词形态未记录 ⇒ **自愿退出证据位**，不作决策依据（2026-09-27 复盘审计）。
    - **MC**：它的 historian 对"带着大段未处理历史"的 session（agents 都是这种：
      fork 自大历史）**每次必失败并立刻重试**。**承重**：e2e21 同一场 historian
      **62 次 / 60 失败**（每 agent 每 ~2.5 分钟一次、每次 ~150s，失败即重排）vs
      e2e23 historian **0 次**；主 pi 侧的 32000 上限根因有 MC 自己的 `context.db`
      记录为证（#916：`maxTokens` 抬到 131072 后首跑成功、输出 36954）。
      ⚠ **另一条已撤回的引用**："给 MC 447.2s vs 不给 MC 10.3s（43 倍）"同属那批
      装置有缺陷的探针、**运行账本无对应臂**、且与同批账号里"仅 MC 收尾 0.5s"方向相反
      ⇒ 同样**退出证据位**（"归因混淆"属假说，不追查：追查成本 > 价值，决策不依赖它）。
      **根因修正（2026-09-14，仍有效）**：不止"模型 id 过期"——MC 源码
      `maxOutputTokens: historian?.maxTokens ?? 32000`，而 historian 模型是
      reasoning:true（推理流吃光上限 → "all reasoning, no text"）。主 pi 把
      `historian.maxTokens` 抬到 131072 后首跑即成功。故原结论限定为"**在该默认上限
      （32000）下不工作**"；agents 会话（fork 大历史）在抬高上限后**未复测**
      （只对 `all` 档有意义——默认档结构上不跑 historian）。
    - **真场验证（零扩展，e2e23）**：32 次唤醒 / **每次唤醒进程跨度 48.1s** /
      **收尾 ≈0%**（对照插件在场时 66–78%）/ historian **0 次** / 并行度 2.42/3.0 /
      档位对照 ✓。
      *口径注记*：该场墙钟曾记 **12m31s**，**源不可复核**；按同批可核数字反算
      （Σ进程跨度 1538s ÷ 并行度 2.42）≈ **10m36s**。引用请带这层限定。
      同机制的其他场次（各自 n=1，**均为"场次墙钟 / 每次唤醒跨度"**）：
      e2e19 17m19s·71.6s、e2e20 20m34s·81.6s、e2e21 **1h13m·330s**。

    **做法（三档；值域/默认值的家 = `meeting_fs.EXTENSION_POLICIES` /
    `DEFAULT_EXTENSION_POLICY`，协议字段 `extensionPolicy`，CLI
    `--extension-policy`）**：

    | 档 | 唤醒命令 | 语义 |
    |---|---|---|
    | `mc-tools`（默认） | 四个 `--no-*` + `-e builtin:mcp` + `-e builtin:codemode` + `-e <MC subagent-entry.js>`（MC 入口缺 → 可见降级：少它那份 `-e`、生效=none；**两份内置常量恒在**）
    | `none` | 四个 `--no-*` | 零扩展：最快、**零依赖** |
    | `all` | 不加任何 `--no-*` | pi 默认发现（A/B 与显式 opt-in）|

    **`mc-tools` 档 = 默认档**（2026-09-14 加并定为默认，用户裁定"提供 ctx_search
    是必要的 background 补充”）：**fork 本身是主背景通道**（agent 继承主会话的开发
    上下文），覆盖面受 ① fork 源内容 ② `budget` 裁剪比例限制；`ctx_search` 是它的
    **兜底**（按需检索记忆/文档/历史）。MC 的
    **两份入口的结构句与检测覆盖面（2026-09-27 复盘审计订正；2026-09-30 第二份改为内置）**：
    - **MC 的 `subagent-entry.js`**（MC 自己给"搜索类子代理"用的入口）= **工具注册 +
      两个生命周期钩子**（`session_start` 开 DB / `session_shutdown` 关 DB），**没有**
      historian / 压缩 / 打标类执行钩子 → agents 得到按需检索能力（memories / docs /
      历史）。*（此前写作"只注册工具、不装任何 hook"，与上游 0.43.2 源码不符，已订正。）*
      **检测覆盖面三面**：唤醒命令的**启动段**、报告**收尾列**、MC 自己的 `context.db` 账本。
    - **pi 内置 MCP 扩展**（`builtin:mcp`）= 在 **`session_start`** 建立连接（上游
      `extensions/mcp/index.ts` 的 `pi.on("session_start", …)`），连接后按服务器逐个
      `registerTool` ⇒ 成本落在**启动段**（"首个 prompt 最多等 10 秒"是上游文档写明的
      上界；先前第三方 adapter 的实测边际是 **+0.2s/唤**，内置实现**待测**——见 §假与口径）。
      **兜底 = 换档**（要零扩展就显式 `--extension-policy none`）。
    - **重估触发**：上游 MC / pi **升版**，或报告**启动段/尾列出现分钟级离群** → 重核
      （MC：`session_start`/`session_shutdown` 内是否新增工作；MCP：连接时长与工具注册数）。
    - **探针双向句**：那 2 臂探针（16–23s vs 4s）验的是**能力与卡死/收尾**，
      **不测钩子成本**。

    **真场计数**：historian **0**（e2e24 0/3、e2e25 0、e2e23 零扩展 0）；`ctx_search`
    可用 ✓；**成本未测得显著差异**（受控探针 n 小、组内方差 > 组间差 ✗；生产基线：
    首次真场 strict=1、n=19 → 唤醒启动段中位 **0.68s**、收尾中位 0.04s ✓）。
    *口径项*：fork 源随唤醒增长（构建时 606 条 / 1.40MB → 读数时 729–800 条 /
    1.44–1.56MB）——**是文件在涨，不是时间在涨**，不构成成本项。
    **入口解析 fail-fast**（`resolve_mc_tools_entry`：pi 的 packages → MC 包 →
    它声明的扩展入口 → 同目录 `subagent-entry.js`）；缺 MC 时**可见降级**（严格模式
    `MV_MC_TOOLS_STRICT=1` 才报错退出）。
    **实现纪律（e2e24 评审 S1 的教训）**：三档共用**同一段零扩展前缀**，
    `_build_wake_cmd` **只有一个出口**——降级只是"少追加一个 `-e`"。此前降级
    分支自拼前缀后提前 `return`，把 model/thinking/system-prompt/`--print` 与
    spawn cwd 全截断（"无 MC 的机器"上产出残缺命令）。**登记行**：首唤打
    `扩展策略: 声明=X 生效=Y strict=0|1 [降级原因=…]`——报告据此给"声明 vs 生效"
    （E2；与"档位"同型），否则降级只在 loop log 里、产品面看不见。
    
    **使用指引（2026-09-25）**：协议模板里有一节「需要项目历史时」告诉 agents何时用 `ctx_search`（并写明"项目文件优先、记忆可能过期"）——**它只在工具真会到位时出现**（策略为 mc-tools 且入口可解析），降级/零扩展时整节消失（不留空指引）。起因：e2e25 自然使用观察里三 agent 自发调用 **0 次**；加装指引后需在下一次"任务书不提工具"的场次里复测计数（utility 无客观判据，只做计数 + 抽看）。
    **数字口径规则（2026-09-27 定，审计产出）**：时间/成本数字必须带 **span · 测点 ·
    规模 · n**（例："收尾段，同一份 1.9MB session，n=1"）；**增长量带时点**（"构建时
    606 条 → 读数时 729–800 条"）；**无源或不可复核的数字一律标注**（"无源"/
    "不可复核"），不得直接引用。

    **回退判据（单行规则，2026-09-27 定）**：自 **2026-09-25**（装置变更：加协议
    指引 + 第二份入口）起，连续两场「**需求场景可证在 fork 窗口之外**」且 utility≈0
    → 提议回退 `none`（**需用户拍板**）。两入口**分列计数**（`ctx_search` / MCP 工具）；
    这是**能力取舍判据、不是性能开关**；数据来源 = 各场 `result.md` 的元信息
    （不设计数器/监控）。*前置限定不可省*——否则会把"需求不在窗口内"当成负证据
    （2026-09-27 那场即此情形：主题所需事实全在窗口/仓库内）。
    **两份入口（2026-09-25 用户裁决 B；2026-09-30 第二份改为 pi 内置 MCP）**：
    `mc-tools` 除 MC 的只读检索工具外，再显式 `-e builtin:mcp` 得到 MCP 工具
    （web_search / web_reader / zread 等）。**为什么改成内置**（用户 2026-09-30：
    "pi 原生支持 mcp，不需要另装插件了"）：pi 0.99+ 自带 MCP 扩展，配置写在
    `~/.pi/agent/mcp.json`（或项目 `.pi/mcp.json`）、工具名 `mcp__<server>__<tool>`；
    换成它 = **去掉一个第三方依赖**（`pi-mcp-adapter` 的入口解析/版本漂移全没了，
    `resolve_mcp_adapter_entry` 已删）。
    **语义清单（唯一权威段，别处引用不复述）**：
    ① MC 入口**允许而非要求**：解析失败不阻断分析（缺 MC 的机器照样跑）——它现在是
       **唯一"可失败"的入口**；内置 MCP 是常量入口、恒在（无第三方、无解析）。
    ② `MV_MC_TOOLS_STRICT=1`（测试/探针保真）→ MC 入口缺失即报错退出
       （否则测试可能在"没装 MC"的环境里通过，而 `ctx_search` 从未生效）；
       两份**内置常量**入口（`builtin:mcp` / `builtin:codemode`）不参与该判定
       （名字由 pi 注册、无需解析、不会失败）。
    ③ 生效值语义：**以本档核心能力为准** —— MC 入口解析成功 → `生效=mc-tools`；失败 →
       `生效=none`（本档核心 `ctx_search` 缺失）；原因字段 = **入口层失败原因**（不写
       工具可用性——那件事本档观测不到，见下方「平台能力的可见性」）。
       *（旧措辞「部分降级仍生效=mc-tools」出自「两份入口都会被解析」的时代，已随第二份
       改为常量入口而失效。）*
    ④ 为什么必须显式 `-e`：`--no-extensions` 关的是"扩展发现**与内置扩展**"
       （pi `--help` 原文）——内置 MCP **与 codemode** 都在关停范围，不显式加载 =
       agents 完全没有 MCP 工具（或被注册但调不到，见 ⑤）；`-e <path>` 接受
       `builtin:<name>`（同一份 help）。上游证据：
       `core/extensions/index.ts` 里 `{ name: "mcp", builtin: true }`，
       `core/resource-loader.ts` 在 `noExtensions` 时只保留 CLI 显式 `-e` 的扩展。
    ⑤ **exposure 回到 pi 默认（`codemode`）**，agents 也走**原生调用方式**（2026-09-30 用户裁决；
    此前一天曾短暂改成 `direct`，理由 = "更贴合 pi 的原生设计"）：
       - `codemode` = 工具**不声明**给模型，列在 **codemode 工具的描述**里，模型**写脚本**调用
         （`tools` / `ALL_TOOLS` / `text()` / `return` …）；`codemode` 工具在 pi 里是
         **注册为 inactive** 的，由 **MCP 扩展自动激活**（不是 pi 自己激活——见下一条）。好处：大工具面不进模型声明、脚本内可**并行调多个工具**、大结果先筛后回
         （直调 >20KB 会被掐中间）。
       - **代价**：脚本比直调多一步；且**必须显式加载 codemode 扩展**（见 ④）——`codemode` 是
         独立的内置扩展，`--no-extensions` 会关掉它；少了它，MCP 扩展只发一条
         `ui.notify("…they cannot be called.")`，而我们的非交互模式里 notify 是 **no-op**
         ⇒ 工具注册了却调不到、且**静默**（2026-09-30 读 `extensions/mcp/index.ts:336-359`
         + `core/extensions/runner.ts` 的 `noOpUIContext`）。**这是本档必须同时 `-e builtin:codemode`
         的全部理由。**
       - **隐含依赖（2026-10-08 核，pi 1.1.0）**：`codemode` 是**注册为 inactive** 的工具
         （codemode 扩展 docstring 原文：“registered inactive. Activate it with `--tools`,
         the `defaultTools` setting, or `setActiveTools()`; **the MCP extension activates it
         when MCP tools are only reachable from scripts**”），激活点在
         `extensions/mcp/index.ts:516-522`（`needsCodemode && hasCodemode && autoEnableCodemode`
         ⇒ `pi.setActiveTools([...active, CODEMODE_TOOL_NAME])`）。⇒ **codemode 的可用性依赖
         `-e builtin:mcp` 在场**：若将来移除 MCP 入口，codemode 会**静默失效**（工具在但
         inactive，notify 是 no-op）——那时须改用 **`--tools +codemode`**（pi ≥ 1.1.0 的
         “调整默认选择”形式）或 `defaultTools` 设置显式激活。
       - 该设置住在 `~/.pi/agent/mcp.json`（**用户级、也影响主 pi**）；项目级 `.pi/mcp.json`
         只按 **server 同名覆盖**且需 cwd 被 trust。**没有 env 覆盖**（2026-09-30 读 `config.ts`）。
       - **已实测（2026-09-30 探针，n=2，生产形态源与命令）**：agents 走 codemode 路径**真调通**
         MCP —— 工具调用 = `codemode`，脚本里 `await tools.mcp__web_search_prime__web_search_prime({…})`，
         两次都取回真实搜索结果，墙钟 **25–26s**（同款任务 direct 时代 16–31s，同量级）。
         *口径*：只验"能调通"（不验多工具并行 / 大结果过滤）；token **未做配对测量**——
         direct 那笔 +1,025 是同源配对所得，两轮之间 fork 源已变大，**数字不可跨轮比**。
    - 命令形状（三档都只是 `-e` 的有无）：
      `none` = 四个 `--no-*`；`mc-tools` = 四个 `--no-*` + 两份**内置常量**入口 + MC 入口；
      `all` = 不加任何 `--no-*`、也不加 `-e`（pi 默认发现）。
    不新增档位（保持简单）。`none` 零依赖（无 MC 的机器/CI 显式选它）。
    **实测（2026-09-30 真场，38 次唤醒）**：内置 MCP 的**每唤醒成本 ≈ 0**——逐唤醒「启
    动前」中位 **−1.3s**（测量偏置）、三 agent 合计 −2s / −5s / +19s、收尾中位 1.1s；
    对照 AFT 的 445.9s 与 MC historian 的 447s，差三个数量级。**唯一离群**：一次 19s 启
    动（未归因，不排除 provider 抖动）。
    **平台能力的可见性（2026-09-30 自审产出；含 4 条不变式）**：
    - **前提**：本档依赖 **pi ≥ 0.99**（`builtin:mcp` 与 `pi mcp` 子命令自该版本）。更早的 pi
      **不是静默降级而是硬失败**：未知 builtin 名 → `Unknown built-in extension`
      （`core/resource-loader.ts`）→ 诊断 → `process.exit(1)`（`main.ts`）⇒ 每次唤醒 rc=1。
    - **不变式 ①**：`meeting_loop` 里**两处守卫拦不同失效模式**，不可互相替代 —— 值域检查拦
      **元组之外**的值；`else: raise` 拦**元组之内、但无分支**的值。别再当死代码删（删掉后新
      策略值会静默落进 `all` 档 = 分钟×N 级且无信号）。
    - **不变式 ②**：分析目录里的**运行快照只含 4 个模块**（meeting_loop / fs / core / engine），
      **`observability` 不在快照里** ⇒ `--report` 永远用**主仓今天**的代码读**当时** writer 写的
      loop 日志（reader/writer 跨版本解耦）。判定条件必须容得下"旧 writer 产出 d==e 且
      reason 非空"（`or reason` 的存在理由）。
    - **不变式 ③**：**非交互模式下 MCP server 状态不进任何产物** —— 单 server 失败被
      `Promise.allSettled` 吞掉，只走 `ctx.ui.notify`，而 `--mode json --print` 用的是
      `noOpUIContext`（`core/extensions/runner.ts` 的 `notify: () => {}`）；session 也不落工具集。
      ⇒ **`rc=0` ≠ 工具可用**；报告写「**平台能力：未观测**」（缺席≠0 的同一纪律）。
    - **登记（既定路径，非行动项）**：真需要机器判据时用 **`pi mcp list --json`**（上游**文档化
      结构接口**；字段 `state`/`tools`/`error`，任一 enabled server 非 `connected` ⇒ 非零退出）。
      触发条件 = 用户报"agents 没搜到" 或 上游给出其它可读信号。做法：`--start` 时采一次 → 落盘
      分析目录 → `--report` 读文件（**探针在组合层/start 层，不进 observability**——报告必须
      保持"零插桩事后推导"）。代价：常态 **+2.7–2.8s**（n=3；复测 4.6s），最坏 +T（我们侧
      超时，T ≤ 10s）。取值三态 **ok / 异常（点名 server）/ 未验证**，**禁写"不可用"**（我们只知
      "此刻不可达"）。护栏：超时 + fail-open、可注入（默认单测不得真连远端）、采样须以
      **cwd = fork_cwd** 启动（项目级 `.pi/mcp.json` + trust 会改变 agents 所见，别把近似当事实）、
      文档写明只覆盖**持续性**失效（不覆盖"启动正常、中途断"）。**两条已证伪的错路**：成功路径
      stderr（notify 是 no-op）与 session 文件（工具集不上盘）——别再捡。
    - **exposure 的成本与口径**：`direct` 时代实测 **+1,025 tok/请求**（差值口径；n=2；配置 =
      3 server / 5 工具全 direct；人口 = agent 侧探针会话）——该配置**已于 2026-09-30 回退**为
      pi 默认 `codemode`（见语义清单 ⑤），所以这笔数字**只作历史对照**，不代表现行成本；
      `codemode` 的现行成本（工具描述 + 脚本往返）**未测**。数字随 server 数 / exposure 变化而
      作废；写**差值**不写绝对值。
    - **三档边界**（可复用的判据）：① 上游**文档化结构接口**（`pi mcp list --json` 字段、
      session 条目）→ **可作机器判据**，字段缺失/形状变 ⇒ 记"未知"；② 上游**人类 prose**
      （stderr / notify 文案）→ **只落盘留痕、不做分支**；③ 上游**内部布局**（第三方包
      `dist/*.js`）→ **不碰**（本次已删净）。
    **依赖边界**：见上方清单 ①（MC 允许而非要求；内置 MCP 是 pi 自带）——本段不再复述。
    第三方依赖为零：本档只用 pi 内置扩展 + 用户自己选的 MC 包。
    **死代码纪律**：`--extensions` 别名与 `extensions: true` 历史字段（只存在约 1 天）
    **已删净**（无移除条件的兼容层不留）；扩展策略只有一个入口：`--extension-policy`
    + 协议字段 `extensionPolicy`。

    **主 pi 不受影响**（只改我们 spawn 的 agent 命令行）。

    **`none` 档的代价**（该档专属，不是默认档的代价）：agents 用 pi 内置
    read/write/edit/bash/grep/glob，且**没有** `ctx_*` 检索与 MCP 工具；零扩展真场里
    三视角自述"内置工具胜任本任务、未因缺工具放弃或简化检查（符号级导航多 2–3
    步/文件）"——**自述 ≠ 测量**，但两次真场（e2e20、本场）均抓到真问题（F1/S1 等），
    无质量下降证据。默认档 `mc-tools` 则带两份只读工具入口（见上方清单）。

    **随此退役的机制**（删净、不留死代码）：`KEEP_EXTENSIONS`、
    `resolve_extension_entries`、`--pure`（语义反转为默认）、以及决策 19 那整套
    "作用域配置"（`build_agent_config`/`_strip_jsonc`/`af_resolution_notes` 等，
    已先一步退役）。

    **门槛条款（本场分析产出，写入设计纪律）**：任何"省时"提案先自证
    `净收益 = 节省 − 机制自身成本 × 频率 − 失效代价（含检测延迟）`；凡需改
    `meeting_engine`/`meeting_loop` 或新增常驻机制者，先过**审计三问**
    （① 职责归属怎么变 ② 净收益数据来源 ③ 新增失败面与检测延迟），不得直接实施。
    将来若要为 agents 加回任何扩展，须**显式 opt-in + 净收益账**（本次实证：
    AFT/MC 两次都是"加了才知道贵"）。
    **加回 AFT 的定价程序（2026-09-27 定，一句话，不另开节）**：任何加回 AFT 的
    提案须在**当前条件**下做 **1 次受控唤醒**（硬超时、用户当次同意）+ **收尾段
    实测** + 净收益账，**不得引用 2026-09-13 那批单点数字**（已退出证据位，见决策
    20 证据段的撤回记录）；反证到手前按预期量级 **≈ 1.4–2.1h/场加在关键链** 予以否决。

21. **git 守卫范围 = 从讨论 workdir 发起的操作**（`GIT_CEILING_DIRECTORIES`
   注入于 spawn）；主项目仓库不在守卫范围（agent 的 cwd 就是主项目，其
   约束归指令层 + 主项目 `.gitignore`）。要拦主仓库需换机制类（沙箱/钩子），
   经评估收益不支撑扩面。

22. **分析流程 = extension（零 LLM 骨架）**（2026-09-24 定）：`/multi-viewers` 与
    `/multi-viewers-finish` 由 prompt 改为 extension 命令——prepare → **门禁 = 暂停点**
    （`ui.confirm`：标题点明「暂停中，可在其它窗口修改 spec」，正文带 spec 路径 +
    文件清单；用户在**其它窗口**编辑该目录，改完点「确认」继续，取消则保留 spec）
    → start → **交付观看命令**（四个通道，见下方角色表，用户按 Enter 即执行）；收尾 status →
    确认 → cleanup。**为什么**：prompt 靠 LLM 逐步执行，每步都可能漏（实测：观看
    命令漏传 2 次、失败判据曾靠读中文报错文本、目录路径曾靠 LLM 记忆）；代码执行
    则天然不遗漏。**与 CLI 的契约 = 机器标记行**（`[prepare] spec=` / `[start] dir=` /
    `[start] watch=` / `[status]` / `[result]`），扩展不解析人类文案——文案可变，
    标记不可消失（`tests/test_mv_cli.py::TestMachineMarkers` 锁住）。**边界**：spec
    内容起草仍是真 LLM 工作（任务类型/产出形态），故为**两段式**——骨架零 LLM，
    起草/摘要走普通对话；门禁取消则保留 spec，用户编辑后自行 `mv.sh --start`。
    **收尾状态语义（2026-09-25 评审）**：`running` 等待；`done | stalled` 并流进
    "notify → confirm → cleanup"（照 running 处理会让用户等一个永不到来的收尾）；
    `stopped` 只提示手动清理。**状态定义以 `observability.check_status` 为单一
    事实源**（`stalled` = 有 result.md、无 concluded、loop 均不存活）——此处不复述其判据。
    `stalled` 用独立文案（其 result 由清理时保存，不复用 done 的"已留存"路径文案）；扩展是唯一守卫
    （CLI 的 cleanup 无状态防护）。**本轮明确不做**（防翻案）：`--json` 输出模式、
    resume 新命令面、env 回退配置、`runCli` 超时、启动路径继续优化（已在 1–2s 地板）、
    给 stalled 加第三种动作。**契约例外**：扩展作为包内第一方消费者**直连**
    `mv_cli.py`（实测 shim 61ms vs 直连 58–66ms，性能上零差异；按契约一致性记例外一行）。
    **建视角流程（`/multi-viewers-setup`，仍是 prompt）**：机械部分下移到
    `mv.sh --set-viewer <名字>`（正文从 stdin 读）——**命名规则 / 不覆盖已有 / 空正文拒绝 /
    写完校验并回显**四条由命令保证（此前是 prompt 里给 LLM 的纪律，会漏）；prompt 只负责
    **看项目给候选 + 内容撰写 + 与用户来回**（那才是 LLM 该做的）。
    **有意的 LLM 义务残留**：prompt 第 4 步要求「改完再跑 `--viewers` 复核」——它不新增
    命令面，且有硬 gate 兜底（prepare/start 的集合校验不过就拒绝启动），故保留。
    **UI 通道按 mode 分级（2026-09-25 实测）**：dialog（`select`/`confirm`/`input`/`editor`）
    全模式可用（RPC/web 走请求-响应子协议、阻塞等用户；不带 `timeout` 即不倒计时，
    暂停点成立）；`notify` 全模式可用（TUI = showStatus 行；pi-web 会关闭即消失）；
    **`setEditorText` 仅 TUI**——pi-web 忽略（`pi-web/static/app.js` 注释
    「set_editor_text … ignored」+ SDK `ui-context.ts` 里是空实现）。⇒ 交付观看命令
    不能只靠一个通道，**四个通道各司其职**：
    | 通道 | 作用域 | 上下文成本 | 角色 |
    |---|---|---|---|
    | `setEditorText` 预填 | 仅 TUI | 0 | TUI 便利（能直接回车跑） |
    | `notify` | 全模式 | 0 | 即时反馈（pi-web 关掉弹窗即消失） |
    | `pi.sendMessage`（custom_message） | 全模式 | ~百 token（主 session）+ 随 fork 进每场分析 | 持久留痕（pi-web 渲染为折叠块、需点击；起 0.5.19 实时出现） |
    | `ctx.ui.setWidget` | 全模式 | 0（纯 UI） | 运行期常驻可见（一眼看到、不需点击） |
    三条注记：① `sendMessage` 的 custom_message **会随 fork 进每场分析各视角的上下文**
    （fork 源在首唤由主 session 条目构建，不做类型过滤）——~2 行/场，有界；不为它加
    过滤（那会让构造层获得扩展类型知识，跨层耦合换几行噪音，不配）。② widget 是
    **运行期**状态（fire-and-forget UI，非会话条目；reload/重启后不恢复——持久记录靠
    custom_message）。③ 零上下文留痕档确实存在（`pi.appendEntry` + `registerEntryRenderer`，
    明确不进 LLM 上下文），但其渲染器是 TUI 组件、pi-web 无渲染路径 ⇒
    **可见 ∩ 零上下文 = 空集**，跨模式成本不可归零，接受现值。需要分级时用 `ctx.mode`。

### 被否决方案（含重估触发条件）
| **围绕 pi 的 durable subagent 重构**（整项目换运行时） | 见提要：代价 = 判定纯净性的守护从**进程边界**退为**代码纪律** + 自组装面归我们（MCP 最实）+ 三个上游包（durable 实验性）；收益 = 进程与格式手术消失（轻量）。**现在不做**（上游 API 自述会变） | ① durable 接口相对固定；② 上游 CLI 把主路径切到它（见 §六 三条）；③ 多进程形态出现真正吃痛的点 | `docs/durable-subagent-rebuild.md`（可行性提要：映射表 / 损失口径 / 困难点 / 待核清单 / 阶段建议） |

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


23. **启动参数默认值走「配置文件 → spec → protocol.json」**（2026-09-27 用户定）：
    `/multi-viewers-config <键> <值>`（= `mv.sh --set-default`）改的是**默认值**，
    存在 pi agent 目录的 `multi-viewers.json`（用户级；键名与 CLI flag 同名）。
    **取值优先级（唯一实现在 `spec_gen.resolve_startup`）**：
    `--start` 显式 flag > `spec/startup.md` > 默认值配置 > 内置默认。
    为什么经 spec 而不是直接进 `protocol.json`：spec 是**用户审阅的产物**——
    把值写进 `spec/startup.md` 让"这次到底用多少"在暂停点可见可改（= 本轮的
    "特别指定"），运行期权威仍是 `protocol.json`（loop 每轮只读它，中途不可改）。
    **踩过的坑（用户点名要确认的那件事）**：`--start` 三个 flag 原先
    `default=DEFAULT_*`，而 `/multi-viewers` 这条路径不带 flag → argparse 默认值
    **无条件覆盖**任何偏好（永远 15/7/600）。修法 = 默认值改 `None`，让"没指定"
    与"指定成默认值"可区分；四层优先级由 `tests/test_startup_defaults.py` 锁
    （含"谁都没设 → 内置默认"这条反例断言）。生效值与来源在启动时逐键打印
    （`max-meeting=20（默认值配置）`），不存在静默覆盖。

24. **定向摘要 fork 模式（近端窗口 L1 + 远端定向摘要 L2）**（2026-10-08 用户提出并定）：
    **动机（用户观察）**：过长的 session 历史会**分散 subagent 的注意力**，导致与本轮要求有偏差的
    thinking ⇒ 要的不是"历史越多越好"，而是**适量且与主题相关**的内容。
    **判据说明（重要）**：'偏差减少'**无法测量**（本项目至今无质量判据，e2e17 已记"缺席 ≠ 0"）
    ⇒ 本模式的验收只看**结构事实**（上下文构成 / 规模 / 摘要覆盖度）＋**用户 dogfooding 感知**；
    不做"先把预算调小、再观察偏差"这类**无判据实验**（用户 2026-10-08 否决该路径）。
    **形态（模型看到三层）**：① compactionSummary（远端**定向**摘要）
      ② 近端原始窗口（逐字、不折叠）③ 链尾 2 轮切换叙事（+ 边界条目）。
    磁盘上另存被摘要覆盖的原始历史（**保留**、模型看不见 ⇒ 非破坏性、可审计）。
    **依据（pi 语义，2026-10-08 读源码核实，全部带 `文件:行`）**：
    ① 上下文构建 `core/session-manager.ts:469-511`：取叶路径上**最后一条** compaction；其**之前**
       条目默认丢弃（除位于 `firstKeptEntryId` 之后者）；其**之后**条目**一律保留**（与锚点无关）；
       **锚点找不到 ⇒ 不报错**，静默变成"只留摘要 + compaction 之后的条目"（良性，本模式据此设计）。
    ② 摘要送达形态 `core/messages.ts:176-182`：`compactionSummary` → **user 角色** + 固定包装
       `COMPACTION_SUMMARY_PREFIX`（"The conversation history before this point was compacted into
       the following summary:"）与 `SUFFIX`（`</summary>`）。
    ③ pi 自己的切点 `compaction.ts:446`（`findCutPoint`）= 从最新往回累加到 `keepRecentTokens`
       （默认 **20000**，`compaction.ts:129`）；该值属**用户级** settings ⇒ **不可**由我们按场设置
       （会影响主 pi，同决策 20 的 exposure 教训）⇒ **L1 由我们自己的裁剪实现**。
    ④ 定向能力边界 `compaction.ts:719-720`（`customInstructions` 追加为 "Additional focus: …"）：
       **只能定向摘要内容，不能定向选取** —— 选取始终是"按位置的前缀切"。
       ⇒ 所以 L2 必须配 L1（否则近端仍是一大堆未过滤的原始消息）。
    **`systemMessage` 的两层区分（本轮澄清，防误解）**：compaction 条目里的 `systemMessage` 是
    **压缩那一刻的 prompt/工具状态快照**（`session-manager.ts:1270` 自动抓
    `getCurrentSystemMessage(buildSessionProjection())`），因为原来的 system 条目位于边界之前、
    会被一并丢弃 ⇒ 快照负责**补位**。它**不是** agents 的 AGENTS.md 来源：后者由**运行期重建**
    （`agent-session.ts:1470/1700` 的 `buildSystemPrompt`/`_rebuildSystemPrompt` + 我们两条
    `--append-system-prompt`（视角文件、work-X/AGENTS.md）+ **未传** `--no-context-files`）
    ⇒ **剥掉快照不影响 AGENTS.md/协议/视角任务书**；去掉的是主 pi 的旧 sections 与 `toolsAdded`。
    **实现路径（复用现有 compaction 模式，不做新的链式改写）**：
    ① **先造"有界输入"**（`meeting_fs.build_summary_input`，纯本地、零 LLM）：取主 session 的
       **可见集合**（= 主 pi 自己看到的那些条目）→ 只留参与上下文的条目（`message` 非 system、
       `custom_message`；`context_edit(replacement=null)` 的目标条目一并隐去）→ **从尾部按
       `_est_tokens` 累计到 `前部 K + 尾部 T`** → 桥接（窗口丢掉的更早条目也算删除，链首置 None）
       → 写成一份新 session（header 自描述：窗口大小 + 丢弃数）；
    ② setup（`--start`，每场**一次**、三视角共享，避免三进程竞争）：`pi --mode rpc --session <该文件>`
       + `--thinking off` + 本项目 flag → `{"type":"compact","customInstructions":"<定向指令>"}`
       ⇒ pi **就地**把它压成"摘要 + 近端窗口"（该文件即 base —— 分成"输入文件 + 产物文件"两个路径
       曾让产物根本不存在，测试当场抓到）；
    ③ 各 agent 首唤：在 **base** 上跑 `build_fork_source(mode="summary")` → 注入 handoff + 边界；
    ④ 摘要原文落盘（分析目录）+ header 标注 + 报告一行（摘要 est / 窗口 est / 摘要模型 / 限幅丢弃数）；
    ⑤ 任一步失败或超时 ⇒ **回退当前 budget 行为**（可见日志、不阻断分析）。
    **实测三条（2026-10-08 LLM 验证，均已复现或已修）**：
    ① **前部必须限幅**：主 session（37.8MB / 14,247 条）的可见集合是 356 条 / **1.34MB /
       est≈250k tokens**，pi 的 compact 要**通读**其中 310 条 / 1.2MB / est≈208k tokens
       —— 一次摘要调用 **>900s 没返回**（吃了 `COMPACT_TIMEOUT_SEC`）。项目文件读不完不是
       问题，"压缩历史"这件事本身变成超长调用才是问题 ⇒ 前部 K 进启动默认值
       （`summary-front`，默认 80000），窗口 = K + T = 100k est（实测取 107 条 / 0.43MB）。
    ② **摘要调用的档位继承会话**（pi `_getSummarizationRequestAuth` 返回
       `thinkingLevel: this.thinkingLevel`）——第一次验证传了 `high`，摘要器在做**高强度推理**，
       慢上加慢 ⇒ 摘要进程固定 `--thinking off`（机械任务不需要推理）。
    ③ **RPC 的 stdin 必须保持打开**：读端 stdin 收到 EOF 就开始退出并 **abort 正在进行的压缩**
       —— 用 `communicate()`（写完即关 stdin）时 0.8s 就返回
       `Turn prefix summarization failed: This operation was aborted`（看起来像模型/协议问题，
       其实是**我们提前关了它的输入**）。现在：保持 stdin 打开 + 后台线程收 stdout +
       拿到响应才关；输出结束（进程退出）与真超时用哨兵区分。
    另两条本轮发现（影响设计）：**主 session 的 31 条 compaction 全是 MC 写的占位摘要**
    （107–221 字符的标题列表，`fromHook=true`）⇒ **没有可复用的"前情摘要"**，前部必须真读一遍；
    **尾部 T 的作用域**：pi 的 `keepRecentTokens` 是用户级设置（改它会波及主 pi）⇒ 我们用一个
    **cwd 级项目设置**（`<分析目录>/summary-cwd/.pi/settings.json`，pi 会 `deepMergeSettings`
    把项目设置盖在全局之上）+ 摘要进程 `cwd=` 那个目录 ⇒ **只影响这一个进程**。
    对照组（小会话 718KB / `tokensBefore` 110k）：真 pi compact 成功，摘要 est 986 /
    窗口 est 25702 / 锚点前 42 条被覆盖 ⇒ 机制本身成立，问题只在前部规模。
    **布局约束（由 ① 推得）**：`[被摘要覆盖的历史] → [锚点..近端窗口] → [compaction 条目] →
    [handoff]`（= pi 自身的自然布局）；模型侧渲染顺序仍是"**摘要在前**"。
    **`preface` 必须移到 compaction 之后**（否则它位于 compaction 之前会被隐藏），措辞从
    "已按预算压缩"改为"已被摘要覆盖"。
    **代价（诚实）**：① 摘要会带入**摘要者的框架**（缓解：指令里写"只陈述事实、不要替读者取舍排序"、
       并保留近端原始窗口；摘要**不可复现** ⇒ 同一输入两次结果不同是已知代价，必须留档）；
    ② 近端细节损失（窗口越小越明显；缓解：项目文件可读 + `ctx_search`）；
    ③ 每场多一次 LLM 调用（setup 阶段，秒–分钟级；失败不阻断）。
    **默认**：先作为**新 fork 模式**（opt-in），默认仍是 `budget`；真跑验证后由用户决定是否翻转默认。
    **重估触发**：连续两场真跑后——用户感知无改善、或摘要明显带偏 ⇒ 回退默认 `budget`。
    前置事实（`pi --mode rpc` 可对副本 work、`compact` 条目形状、`buildContextEntries` 语义）
    已在 2026-10-08 零 LLM 核实，探针现场已清理。

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

- **README 计时样本里的离群值 2494s**（2026-09-11，≈8× 中位）：成因未查（疑机器
  负载/挂起）。**观测点**：`tests/.cache/last.log` 的 `Ran N tests in Ns` 序列
  —— 若再次出现 >1000s 的单次值，比对同批 `--force` 的 CPU 时间以区分负载与挂起。

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

---

## 六、外部契约：我们对 pi session 格式的依赖（迁移清单）

fork 机制建立在 pi 的 **session jsonl 文件**上——这是**外部契约**，不是我们能单方面
稳定的内部设计。依赖逐条列出，供上游演进时**逐条验证**（快照：2026-09-22，
上游源码副本 `/root/research/pi`）。

| 依赖 | 内容 |
|---|---|
| CLI | `--session <path>`（须接受**任意路径文件**——我们的 fork 源在分析目录里）、`--session-id`、`--session-dir`、`--name`、`--model`/`--thinking`/`--append-system-prompt`/`--print`/`--approve` |
| 文件布局 | `~/.pi/agent/sessions/--<cwd 编码>--/<ts>_<sid>.jsonl`；编码 = 去首尾 `/`、内部 `/`→`-`（`spec_gen.pi_sessions_dir`；`PI_SESSION_FILE` 是更稳的入口） |
| 条目 schema | 每行一个 JSON：`type`/`id`/`parentId`/`timestamp`；消息体在 `message.{role,content}`；**未知类型一律原样透传**（`_fold_entry` 默认分支） |
| 语义（**只复刻这三处**） | ① replay 起点 = 路径上最后一个 `compaction` 的 `firstKeptEntryId`；② 可见集合 = 该锚点之后的条目（`_normalize_entries` 据此移除窗口内 compaction 并桥接 `parentId`，不变量 I4）；③ **上下文 = 从 leaf 沿 `parentId` 上溯**、遇缺失父节点**静默停止**（`buildContextEntries`/`buildSessionPath`）⇒ 删除条目必须桥接（`_bridge_parents`；2026-10-03 发现 tlc 剔除漏桥接时可达 65/13454 条） |
| 条目类型 | `compaction`（读/移除）、`thinking_level_change`（剔除继承值，否则 pi 不写本场生效值）、`session_info.name`、`custom_message`（我们的边界条目） |

**触碰面**（适配范围；口径 = 函数体行数，2026-09-22 摸底）：`meeting_fs` 456 行
（真正格式耦合 ≈300：`build_fork_source` + `_normalize_entries`）· `meeting_loop` 141 ·
`spec_gen` 66 · `observability` 33。

**触发条件**（任一出现即进入适配）：① `packages/coding-agent/docs/session-format.md`
改写，或 CLI 默认会话落到 sqlite/repo 抽象（含 `--session-backend` 类开关）；
② `--session <file>` 不再接受任意路径文件，或首唤报「打不开 fork 源 / 上下文为空」；
③ CHANGELOG 出现 "migrate sessions" / "sqlite default" 类条目。

**核对方式**（两条命令，读上游源码副本）
```bash
head -3 /root/research/pi/packages/coding-agent/docs/session-format.md   # 是否仍声明 stored as JSONL
grep -n '"--session"' /root/research/pi/packages/coding-agent/src/cli/args.ts
```

**上游现状与结论**：`pi-agent-core` 已有 `Session`/`SessionStorage`/`SessionRepo` 抽象 +
`jsonl`/`memory` 实现，SQLite 是独立包（`@earendil-works/pi-session-backend-sqlite-node`，
活跃开发）；**但 CLI 主路径仍是 jsonl**（`core/session-manager.ts`）、`session-format.md`
仍如此定义、Unreleased 无迁移条目。**现在不改**——对着尚未被 CLI 使用的接口写代码是投机。
（扩展侧另有稳定只读入口 `ctx.sessionManager`，进程外 loop 用不到，与"流程 extension 化"相关。）
