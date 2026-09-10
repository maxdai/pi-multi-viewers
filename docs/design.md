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
     （移除窗口内 compaction 条目——见 §一 与不变量 I5）；日志与验收用
     （丢弃数为 0 而规模远超预算 = 异常信号）。产物须闭合：
     源保留区条目数 = 产物非 preface 条目数 + `forkSourceDropped`。
2. **消费侧规模（验收/成本基准）**：**唤醒 1 的第一次请求** `input +
   cacheRead`（含系统提示与工具定义）。引用必须带唤醒序号，否则数字不可比。
3. **校准比（est → 真实）**：唤醒 1 ≈ **1.66×**（e2e10 三点独立样本：
   1.656 / 1.657 / 1.659，±0.2%）；唤醒中后期至 ~2.7×——**不得固化为
   2.0×**（不同测点值不同）。
4. **预算只约束"基线"**：`budget` 管的是 fork 源；**运行规模随该 agent 会话
   累积增长**（实测 w1 132k → w5 192–207k；满程外推 290–350k，标注为外推）。
   容量估算不得用 "est × agent 数 × 轮数"。
5. **预计值不得用作验收口径**：日志中的派生数字只是可读性便利。

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

### 被否决方案（含重估触发条件）

| 方案 | 否因 | 重估触发 | 所在位置 |
|---|---|---|---|
| **省一次重读**（首唤时把 tail id 从 `build_fork_source` 传给 `append_handoff_turns`） | ①收益仅 0.011s（budget 产物；full 产物 134ms + 37MB 峰值）；②方案自败（保留重读兜底则被指瑕疵的推导代码一行未减）；③新增静默失败面（tail id 可能过期 → 接错节点）；④把 session 格式知识泄漏到 loop 层。同层替代已评估未采纳：`append_handoff_turns` 只保留尾行（不跨层传状态、不新增失效面）——量级 0.4s vs ~130s/唤醒（0.3%） | 源 ≥100MB 或 N≫3（内存 ≈2.5×文件大小×N） | `meeting_fs.append_handoff_turns` / `meeting_loop` 首唤路径 |
| **共享 budget 基座**（三 agent 共用缓存） | 引入持久状态 + 失效规则 + 跨进程原子写/清理义务，与"单一事实源/确定性归 loop/无静默"冲突；收益 ≈181ms×3（本机、15.3MB 源实测；旧记录写 ≈0.28s×3——口径不可考，两者差 55%，按修订记录并列不静默替换），相对 ~130s/唤醒可忽略 | N≫3 且会话至 100MB 量级 | `meeting_loop` 首唤路径 |
| **裁剪改流式 / 环形缓冲** | 唯一收益是内存峰值 +37MB；会扩大"先折叠再裁"不变量的证明面 | 源规模使峰值内存成为实际瓶颈时 | `meeting_fs._budget_entries` |
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
