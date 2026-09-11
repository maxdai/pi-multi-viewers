> **存档说明**：2026-09-11 真实多视角自审（性能 / 简单 / 铁律）的 result.md
> 原文，主题为"review 本项目的代码，查看代码的合理性"。该轮抓到 8 项问题
> （R1–R8：校验多实现、wrapper 越界检查、`--start` 静默删 spec、session 查找
> 两套规则等）+ 6 项归并建议。修复见后续 commit；报告中的编号为**当次讨论
> 内部编号**，不可跨文档核验；同时该轮本身也是一次时间流实测（框架空闲仅
> 3 秒 / 44 分钟讨论）。

# 多视角分析结论：review 本项目的代码，查看代码的合理性

## 0. 元信息

| 项 | 值 |
|---|---|
| 分析对象 | `/root/pi-multi-viewers`（约 4700 行 Python + wrapper），工作区 HEAD `663f013` |
| 参与者 | 性能 / 简单 / 铁律（3 视角，meeting 10/7 → RR 全员 pass） |
| 模型 | `opencode-go/deepseek-flash`（thinking max），fork 模式 `budget`（默认，携带主会话上下文） |
| 消息规模 | 性能 13 / 简单 10 / 铁律 11 条实质消息（共 34，另 2 条 loop 协议信号） |
| 任务边界 | 审阅——只提意见不改代码；只读命令 + 秒级探针（未跑全量测试套件） |

## 1. 总体判断

代码总体健康：分层清晰（core 纯判定 / fs I/O / engine 状态机 / loop 壳），单一事实源
在近期修复后基本落实（protocol 单读原语、`check_agent_name` 单实现、`run_git` 读路径单点）。
本轮发现集中在三类：**同一语义多份实现/规则分叉**、**制品文本（注释/文案）与机制保证不符**、
**边界越界（产品层替第三方扩展做体检、观测层绑定状态机常量）**。无高危缺陷
（无数据损坏、无流程死锁、无安全类问题）；共 8 项确认问题 + 6 项归并 + 3 项性能记录项。

## 2. 确认问题与处置

### 2.1【中】R1 + R8：viewers/agents 校验与列举策略多实现，且 `--agents` 在 prepare 路径规则缺位

**R1（策略双实现 + 已漂移）**

- 同一套规则（目录缺失 / 空视角 / 名字合法 / ≥2）在两条路径各写一遍：
  `_snapshot_viewers`（prepare，`start_discussion.py:533-571`）与 `_resolve_spec` 的
  viewers 回退分支（`:622-657`）。
- 已漂移的证据（grep/读码核对）：非法名文案 `:558`（带 `（viewers/{n}.md）`）vs
  `:468`（不带）；≥2 检查 `:560` vs `:653`（后者多列参与者）。`validate_participants`
  docstring 自称「唯一文案」（`:464`），被 `:558` 自身证伪。
- 隐藏文件规则分叉：`.md` 列举全仓 3 处——`_discover_viewers:493`
  （`and not f.startswith(".")`）、`_resolve_spec` 的 `.order` extra `:627` 与无 `.order`
  回退 `:631`（两处只判 `.md`）。后果：`spec/agents/.draft.md` → 参与者名 `.draft`
  （`check_agent_name` 不拦点号），静默变成参与者。

**R8（实测：规则缺位，不只是文案不一致）**

- `_parse_agents` 只调 `_check_reserved`（**只查 human**），完整规则集在 `--spec-gen`
  路径**没有**应用。两条探针实测（产物已清）：
  - `--agents "a/b"` → `gen_spec_skeleton:297` **FileNotFoundError traceback**，且
    spec 目录**半成品落盘**（models/background/question/README 已写、无 agents/）
    ——既非"明确报错"，也违反"不合规零产物"；
  - `--agents "a b,c"` → **骨架生成成功**（非法名 `a b.md` 与 `.order` 落盘），
    直到 start 的 `main:1147` 才被拒绝——用户在中间还审阅了这个 spec。

**处置（两条合并为同一处修法）**

- 策略级单点：`_discover_viewers` = 列举 + 非空（隐藏排除只此一处）；共享
  `_viewer_set_error(names, empty)` 承载"空正文文案 + ≥2"；名字规则继续走
  `check_agent_name`/`validate_participants`（已是单实现，只收**调用点**）。
- `_parse_agents`（调用点 `main:995`，早于 `--spec-gen` 分派 `:1008`）改调
  `validate_participants`，删 `_check_reserved`（`:472`，单调用点薄包装）。
- **prepare 路径前提**：`--spec-gen` 分支在 `main:1022` 即 return，早于中心闸门
  `:1147`——所以 `_snapshot_viewers` 内部的名字校验**必须保留**（它承载 prepare 路径的
  "不合规零产物"）；`_resolve_spec:647` 的内层调用在 start 路径冗余，可删。
- **验收**：四入口（CLI `--agents` 的 prepare/start、viewers 的 prepare/start、
  spec/agents 的 start）**同一违规 ⇒ 同一消息**，且**在任何文件落盘之前拦截**。
- 附带：`validate_participants` docstring 的「唯一文案」随本次修法成真或删除。
- 边界（不主张）：写盘 I/O 失败（磁盘满/权限）下的半成品**不加**事务/回滚机制
  ——复杂度不匹配，触发条件 = 真实出现。

### 2.2【中】R2：wrapper 越界检查第三方扩展（AFT）配置

- `scripts/mv.sh:51-126 check_aft_bash()`（76 行 ≈ wrapper 的 20%，含内嵌 Python 的
  字符串感知 JSONC 解析器），在 `--prepare`（`:204`）/`--start`（`:272`）前读
  `~/.config/cortexkit/aft.jsonc` 检查 `"bash": false`。
- 三个具体问题：
  1. **未装 AFT 的机器也告警**（配置不存在 → `_aft_warn`，`mv.sh:65`），且该行为被
     `tests/test_wrapper.py::test_missing_config_warns` 固化为规格——未装 = 未接管 =
     一切正常，是假阳性，修复指引指向不存在的文件；
  2. 告警称「插话扩展找不到分析目录」——`/multi-viewers-say` extension **尚不存在**
     （`package.json` 仅 `pi.prompts`，无 `extensions/`），该后果当前不可发生；
  3. README/AGENTS/design 均未记录这条环境要求。
- 边界判据：AFT 是另一个 pi 扩展，其配置属**运行环境**；本工具 wrapper 的职责是
  prepare/start/view/say/cleanup。且"models.md 退化"的真正弱项是自家的 R6
  ——**告警让用户去修别人的配置，而弱项在我们仓内**。
- **处置**：删除为主；备选（保留告警）= 仅在 AFT 配置存在时检查 + 后果文案改为真实后果
  + README 一行环境要求。

### 2.3【中】R3：`--start` 静默删除 spec 目录（可审计性）

- `scripts/mv.sh:301` `rm -rf "$spec_dir"`：无守卫（只要求 `question.md` 存在）、无提示、
  无文档；而 prompt 明确让用户"查看/编辑 spec"（`prompts/multi-viewers.md` 步骤 2）。
- 实测当前讨论目录：bare HEAD 只跟踪 `.gitignore`/`protocol.json`/`question.md`/消息文件
  （核对时点 13 条）；`AGENTS.md`、`.pi/`、`pi-agent.json` 被 `.gitignore` 排除（本地文件）。
  推论：`--start` 删 spec + `--cleanup` 删讨论目录 ⇒ **视角任务书（agents 快照）、注入
  背景（AGENTS.md 背景节）、models 配置在 cleanup 后不可恢复**——spec 是这些内容在用户侧
  的**唯一副本**。
- **处置**：
  - 必须级（零成本、独立成立）：删除前打印一行 + **只删本工具生成形态**（`mv-spec-*`）；
  - 可选级（需裁决）：要审计链则选"**不删**"（零机制优于新增"归档"机制），README 说明
    `mv-spec-*` 由用户处置；**不要**在 `--cleanup` 新增"猜 spec 路径"机制（新耦合）。

### 2.4【低】R4：`main` 233 行 / `agent_loop` 231 行（补丁沉积观察）

- `start_discussion.main`（`:956-1188`）= argparse 声明 + 5 个命令分支 + setup/start 全混；
  `meeting_engine.agent_loop`（`:526-757`）分支 ①–⑤.5，注释含约 15 处历史修复溯源。
  项目自己的方法论把 `main()`/CLI 分发列为独立测试盲区（拼接点教训）。
- **处置**：`main` 支持按命令机械拆分，**优先抽 `--wait` 内联循环**（≈`:1046-1101`，
  main 里最大单块）；`agent_loop` 只做小步归并（见 2.8 ④），不做整体重排。
- **归并护栏（一条可验收不变量）**：**纯结构变换——调用序列与 sleep 序列逐字不变；
  主流程阶段骨架的可见性不降。** 具体化：helper 只做机械动作、不吸收"何时进入分支"的
  阶段判断；不增加 bare 读取次数；轮内快照的派生关系保持；两侧日志可区分。

### 2.5【低】R5：`check-residue.sh` 注释"同一判据"失真

- `scripts/check-residue.sh:104` 称"与 `start_discussion._loop_pids` 同一判据"；实际
  bash 匹配 `*/meeting_loop.py`（**任意**讨论的 loop，残留检查语义），python `_loop_pids`
  匹配 `== <base>/meeting_loop.py`（**指定**讨论，存活语义）——同一**技术**（argv 逐项精确
  匹配），**判据不同**。属"声明 ≠ 保证"类。**处置**：改注释为"同一技术、判据不同"。

### 2.6【低】R6："当前 session 文件"两套查找规则（+ 记录项）

- `resolve_fork_source`（`:507-530`）按 `PI_SESSION_ID` 匹配文件名；
  `_detect_pi_model_thinking` 兜底（`:124-130`）取 sessions 目录内**字典序最后**的
  `.jsonl`——同一概念两套选择规则，可能选到别的 session（低危：只影响 models.md 预填）。
- **处置**：共用 `current_session_file()` 原语（sid 优先，兜底同一规则）。
- **记录项（尾部反向扫描，不催本轮）**：兜底现为整文件逐行解析（~0.1s，仅 prepare 且
  env 缺失时触发）。若改尾部扫描，验收条件：
  1. **停止条件与穷尽条件分离**——两类事件都找到才停；到达文件头是**合法常路**（事件可能
     根本不存在，如只有 `model_change`），"找不到"正常返回，不得抛错/重试/死循环；
  2. 语义与现值**逐位一致**：最后一次出现胜出；
  3. **不得设"有界前缀"**——找不到就继续向头部扫（有界截断 = 静默降级到 `_default_model()`）；
  4. 实现细节：反向块读要丢弃/拼接半行，且块须**扩张直至覆盖整行**（JSONL 单行可能超长）；
  5. 等价测试：以**现全扫实现为测试期 oracle**（先例：`tests/test_meeting_fs.py` 的
     `_replay_visible`——"测试期 oracle + 禁生产复刻"），fixture 覆盖事件在头/尾、仅单类
     事件、两类都无、**事件跨块边界**；等价测试只钉**正确性**（返回值逐位相等），
     **性能属性用测量、不用断言**。

### 2.7【低】R7：切换叙事拼接 agent 定义文件正文

- `meeting_loop._prepare_fork_session`（`:280-293`）读 `.pi/agent/<agent>.md`
  （`gen_agent_def` 产物，含生成头「# Pi 多视角参与者 X / 你是 X… / 参与者：… / 你使用
  模型…」）整段拼进 user turn；`_read_perspective_brief`（`:452-474`）**>500 字截断**在
  句中。身份/任务措辞因此在三处各自生成（`gen_agent_def` / 切换叙事 / `AGENTS.md.tpl`）。
- **处置**：切换叙事只写「身份与视角任务书见 system prompt 注入；主题：X」，不拼文件正文
  （消第三处身份措辞与 500 字截断分支），与 viewers 分层方向一致。

### 2.8 简单视角的六项归并/删除（性能评估全部中性，逐项处置）

1. **轮转表达式 ×4**（`meeting_engine.py:356/:420/:635/:675`）+
   `order = agents` 别名 ×2（`:355/:537`）→ 收成 `_next_in_order(order, agent)`
   （纯表达式、无 I/O；放 engine 私有或入 core 便于单测）。
2. **`bare` 推导 ×13**（engine `:63/:74/:549/:555`、loop `:383/:549`、fs `:938`、
   viewer `:150`、sayer `:48/:117`、start `:908/:1055/:1110`）→ fs 单点。
   **形态约束**：不得"接受 workdir 或 base"（那是按值形状猜入参，违反 `_join_model_ref`
   已立的禁止类）→ 两个具名入口，或调用方规范化后统一传入。判定类 API 统一收 bare 时
   **必须删掉** engine 的"函数体内自推 bare"兼容壳（否则留下第三种约定）；
   `human_viewer.participants_from_bare` 的重复随之消解。
3. **两个纯转发壳**：`meeting_engine._cat_batch`（→ `meeting_fs.cat_batch`）、
   `meeting_loop._unlock_git`（→ `restore_git_lock`）——删壳直调（批读形态/锁语义不变），
   测试引用同步（`tests/test_meeting_engine.py` 导入 `_cat_batch`、
   `tests/test_meeting_loop.py:630` 调 `_unlock_git`）。删壳前确认二进制读的理由
   （cat-file size 是字节数 vs `read(size)` 是字符数 → 中文错位死锁）完整保存在
   `meeting_fs.cat_batch` docstring。
4. **`agent_loop` 两组结构重复** → 小步归并：
   - RR 表态块 ×2（starter `:623-638` / 轮次 `:664-682`）：helper 边界止于
     "`read_point` → `new_messages_with_meta` → 分支（respond | 写 pass）"；
     **"不是我的回合 → sleep" 留在调用点、不进助手**；两侧日志走显式参数区分。
   - "rw 收尾 / 其余等待" ×2（consensus `:647-653` / quota `:656-663`）：原样移入
     `_finalize_or_wait(...)`，**每条路径恰一次 sleep 或一次 finalize**，`continue`
     语义与顺序不变。
5. **`result.md` 实际路径 ×2**（`human_viewer.result_path:42-45`、
   `start_discussion.py:1090`）→ fs 单点或共享实现（同一次 `read_protocol` 等量替换，
   不改变调用次数）。
6. **测试环境骨架 ×6+ 文件**（`test_meeting_engine.make_env`、`test_viewer.make_discussion`、
   `test_flow_composition`/`test_main_paths`/`test_dir_forms` 的 setUp、`test_human._env`、
   `test_stage4`/`test_meeting_concurrency` 同型）→ `tests/helpers.py` 的
   `make_discussion(participants, rw, **extra)`；各文件保留特化构造（测试独立性不牺牲），
   helper 不得在用例间共享可变状态。

## 3. 性能视角：记录项（不修 + 触发条件）与等待点核验

### 3.1 构建期（一次性；已量化；不修）

| 项 | 实测 | 触发条件 / 观测点 |
|---|---|---|
| `json.loads` 占构建耗时 **49%**（15.3MB 源 → 181ms/agent） | 本机实测 | 构建 >1s（观测点=首唤日志"构建 Nms/峰值 RSS"） |
| `entries`/`folded` 双列表驻留 → RSS 峰值 **55MB ≈3.6×** 源 | 本机实测 | 峰值 RSS >~500MB 或源 >~100MB |
| 三 agent 各构建一次（≈543ms） | 本机实测 | N≫3 且会话至 100MB 量级 |
| 三处冗余 pass ≈11ms（构建的 6%） | 本机实测 | 构建 >1s |

第一优化点是**流式解析**而非折叠算法（49% 在 `json.loads`），但会扩大"先折叠再裁"的
证明面——未到触发条件不动；"共享 budget 基座"已评估否决（跨进程失效/原子写义务 > 收益）。
**反对线**：① 把循环顶批量读拆回逐条 `git_show` = **16.8× 回归**（实测）；② 为省
37MB/进程把构建改流式（证明面扩大、未到触发条件）。

### 3.2 热路径（多轮）

每轮循环顶一次全量 bare 批量读（消息类判定由此派生）+ 常数次无状态读
（`human_msg_count` / `_stall_elapsed` / RR `rr_next_speaker`）≈ **26.5ms/轮**
（3 agent 并发 ≈4% 单核）。**不为省 ~3ms/轮** 引入跨函数快照（会把状态耦合进判定签名）；
**不引入每轮缓存**（协议读取缓存成进程状态 = 把"protocol 不再变化"变成未成文的隐式不变量，
且与"判定基于最新共享事实"的修复方向相悖）。

### 3.3 等待点核验

- `human_viewer --follow`：`POLL_INTERVAL = 2.0s` → 观看滞后 ≤2s，**无问题**；
- `start_discussion.py:1100`：`--wait` 硬编码 `time.sleep(10)` → 结束观察延迟最长 10s，
  是唯一命中">10s 非必要等待"的点 → **统一为 ≤2s**（见 §4）。
- 未覆盖：`finalize_discussion`（rw 收尾的一次 LLM 调用）耗时占比未测——建议收尾时纳入
  基线，区分"LLM 生成"与"流程间隙"。

## 4. 唯一分歧及其关闭：`--wait` 常量归属

- **值**三方一致：观察延迟 10s → ≤2s（每次 `check_status` ≈3 个 git 子进程 + /proc 扫描
  ≈1.1ms，2s 频率 → 观察者 CPU <2% 单核；5× 频率换掉 8s 观察延迟，划算）。
- **符号归属**（曾分歧，已关闭）：铁律主张**消费端常量**——`POLL_INTERVAL` 是状态机
  空闲重试节奏（与 API/CPU 成本相关），观察刷新节奏是另一个概念（与 UX 延迟相关）；
  用 import 绑定会让"调 loop 节奏"静默改变观察契约（动作-远距离耦合）——
  **共享值 ≠ 共享概念**。性能 `0005` 撤回绑定、接受消费端常量；简单 `0004`/`0005` 接受。
- **结论**：定义 `human_viewer.OBSERVER_POLL_INTERVAL = 2.0`，viewer 与 `--wait` 共用；
  注释写明「与 loop 节奏当前相等但**有意独立**」+ 下界 **≥1s**（防将来有人调到 0.1s
  变成 ~30% 单核的无谓开销）。

## 5. 归并护栏的计数断言（新增测试规格）

- **前提**：只能在"注入 responder、进程内跑 `agent_loop`"的测试里做（先例：
  `tests/test_human.py:360-372` 的线程形态）；走 `spawn_agents` 的多进程形态父进程
  **计不到**。
- **双入口**：`meeting_fs.run_git`（`subprocess.run`，`meeting_fs.py:34`）与 `cat_batch`
  （`subprocess.Popen`，`:230`）——**只计 `run_git` 会漏掉热路径的批量读本身**。
- **替身形态（两案）**：`mock.patch.object(meeting_fs.subprocess, "run"/"Popen", …)`
  打的是 **subprocess 模块对象本身**（进程全局，隔离来自 test window）；若要 fs 级隔离，
  需替换 `meeting_fs.subprocess` 模块属性（此时才面对透明代理问题：须透传
  `DEVNULL`/`PIPE` 等常量）。**两者皆可，但声明的隔离性要写对**。
- **分类**：按 git 子命令区分读/写（读类集合写成一个测试模块级常量），**只对读类断上界**
  ——in-process 形态下测试自身也在经 fs 写消息，不分类会把上界污染。
- **形态**：scenario 级（N 轮 FakeAgent 的总上界），**不在生产层加计数钩子**（测试关注点
  不进产品代码）；计数器用 `list.append` + 末尾 `len`。
- **上界须有推导**（实测值 + 余量写进测试 docstring）——无推导的常数是任意数，失配时会被
  随手放宽。
- **寿命**：断言对象是**结构**（每轮 spawn 上界），不是速度；"每轮一次全量读 + 常数次
  无状态读"是**既定设计决策**，故**结构性断言随设计走**（长于本次归并），
  **速度测量永不进单测**。

## 6. 确认无问题（有证据）

- `meeting_core.py` 纯判定：无 `import os/json/subprocess`（grep 验证）——core 不沾 I/O；
- engine 侧 I/O 已归 fs 原语（`file_size`/`write_text`/`remove_message`/`cat_batch`），
  无裸 `open(w)`/`os.remove`；
- protocol 读取单源：engine / loop / status / viewer 均经 `meeting_fs.read_protocol(bare)`，
  无第二实现；`check_status` 的 concluded 判定复用 `aggregate_mode`（非 `git grep` 全文）；
- git **读**路径统一 `fs.run_git`（quotepath 加固单点）；`run_cmd` 仅用于一次性环境命令；
- **A1 守卫现场证据**：本次讨论唤醒期间 `work-铁律/.git.locked` 存在，在 workdir 内执行
  git 得到 "not a git repository" 而非上溯主仓库——锁 + `GIT_CEILING_DIRECTORIES` 在真实
  运行中 fail-closed；
- wrapper 无 python 逻辑复刻（status/wait/prepare 全转发；`--agents` 解析、viewers 校验、
  fork 源解析均在 python）。

## 7. 未覆盖（诚实边界）

- `tests/` 362 个测试的断言强度未逐条审（建议：R1 修法后为**两条路径各**构造用例验证
  "同违规同消息"）；
- `docs/` 与实现的全量对照未做（只抽查 design.md 协议/规模口径相关节）；
- 性能：git fetch/push 延迟、human 通道 push 容错重试耗时、pi 冷启动固定开销、
  `finalize_discussion` 耗时未测；
- `scripts/mv.sh` 除 R2/R3 相关区域外的其余行未逐行审。

## 8. 过程与纪律说明

- **阶段**：meeting（多轮交锋，含两处实测探针）→ 全员 freezing（含 2 条 loop 协议信号）
  → RR 轮转全员 pass（性能 → 简单 → 铁律）→ 收尾。
- **证据纪律**：所有引用落到 `文件:行`；实测探针两处（R8：`--agents "a/b"` 崩溃与
  半成品落盘、`--agents "a b,c"` 骨架成功；R6：4 个真实 session 中 model/thinking 事件
  位置抽查——最后一次落在 94–100% 文件位置）。
- **"声明 ≠ 保证"类目**（两处）：`validate_participants` 的「唯一文案」、`check-residue.sh`
  的「同一判据」——均以证据推翻并给修法。
- **自我修正**：铁律视角原假设"model/thinking 事件集中在文件头，故尾部扫描无益"被实测
  数据推翻（事件贯穿全文、最后一次在 94–100%），当场撤回隐含反对。
- **术语校正**：`简单/0010` pass 中写的 `_available_agents` 实为 `_parse_agents`
  （全仓无前者）——实施时以 `_parse_agents` 为准；`validate_participants` 中心闸门
  位于 `start_discussion.py:1147`。
- **共识达成**：34 条消息全部收敛，无遗留异议（唯一曾分歧的 `--wait` 常量归属已按
  消费端常量关闭）。
