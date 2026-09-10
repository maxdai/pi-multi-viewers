> **存档说明**：本文件是 2026-09-10 一次真实多视角自审（性能 / 简单 / 铁律
> 三视角）的 result.md 原文，主题为"code review 本项目的代码合理性"。
> 该轮抓到 13 项确认问题（守卫 fail-open、pgrep 自匹配、protocol.json 读取
> 散落、concluded 判定误报、engine 直做 I/O 等），修复见后续 commit。
> 报告中的 P1–P4/A1–A4/B1–B5/C1–C5 等编号为**当次讨论内部编号**，不可跨
> 文档核验；提及的路径（如 /tmp 现场）已清理。

# 代码评审结论（多视角协同分析）

> **分析**：pi-multi-viewers 代码合理性评审（性能 / 简单 / 铁律 三视角，meeting 协议）
> **日期**：2026-09-10
> **分析对象**：`/root/pi-multi-viewers` 全项目（核心实现 ~4,000 行 Python + wrapper + 352 测试）
> **产出**：评审意见清单（审阅型任务，未修改任何代码）
> **效力声明**：以下为**视角共识**，不是用户裁决——修复实施前需用户确认。

---

## 一、审查方式

三视角独立通读核心实现（`meeting_core` / `meeting_fs` / `meeting_engine` /
`meeting_loop` / `start_discussion` / `human_viewer` / `human_sayer` / `scripts/mv.sh` /
`fake_agent`），并各自运行**实时探针**验证可疑点（非纯静态推理）：

- 铁律视角：6 个探针（status 误判、pgrep 自匹配、锁态上溯、result_writer 双读/IndexError、
  `.gitignore` 覆盖、锁免疫）；
- 性能视角：热路径复刻（39/198/498 条 fixture）、fork 构建 cProfile 分解、ceiling 六场景探针、
  活现场内存测量；
- 简单视角：调用点穷尽清点（grep 枚举）、现场锁态观测、空 participants 回落核对。

三视角对关键事实**独立复现并互相核对**（如守卫 fail-open 由三方各自观测到同一现象；
`agents=[]` 安全回落由简单提出、性能实测消费端确认）。

---

## 二、确认的问题（按批组织，含修复约束）

### 批 A —— 守卫层（4 项）

#### A1【中】`_lock_git` 守卫 fail-open，且落点与 LLM 实际 cwd 错位（F7 = 性能#4 = 简单S4）

三方独立观测（lock 生效期间实测）：

```
work-<agent>/.git 改名 .git.locked（锁生效）
从 workdir:  git rev-parse --git-dir → /root/pi-multi-viewers/.git  ← 静默上溯到主仓库
LLM 的 bash: pwd → /root/pi-multi-viewers（fork 模式 spawn cwd）→ 主仓库可直接操作（含 GitHub push 远端）
git pull --rebase --autostash（锁态）: 1054.6ms rc=0（真实网络 fetch）  ← 对照解锁后 11ms
```

- 设计前提是 `cwd = workdir`（helper 模式）；fork 模式把 spawn cwd 改为主项目
  （`meeting_loop.py:323`），**守卫挡的是 LLM 不去的门**；
- `_lock_git` docstring 声称"任何 git 操作失败（not a git repository）"——**被实证推翻**；
- **缓解事实（已核查）**：主项目 `.gitignore:4` 有 `discuss-*/`，`git check-ignore` 确认讨论
  目录整体被忽略 → 杂散 `git add -A` 无法把消息提交进主仓库，**"流程绕过"风险未打开**；
  真实剩余风险是杂散**写命令**作用于**用户主仓库**（比守卫原本要保护的讨论流程更贵）。

**修复（三方一致）**：
1. spawn 注入 `GIT_CEILING_DIRECTORIES=<讨论目录>`——必须
   `env={**os.environ, "GIT_CEILING_DIRECTORIES": base_dir}`（`Popen(env=)` 是整体替换，
   漏合并会丢 PATH）；单点注入（`_run_wake_proc` 的 Popen），不做 loop 侧对称注入；
2. 注释**范围化**："使**从讨论 workdir 发起**的 git 操作 fail-closed；主项目仓库不在守卫范围，
   其约束归指令层"（不留全称断言）；
3. **数字与来源进 commit**（workdir 侧 pull 1054.6ms→1.0ms，锁态、本机），不进注释；
4. 先红后绿测试：锁态下 `git rev-parse --git-dir`（cwd=workdir）须非零退出。

**裁决记录**：不把守卫扩展到主项目仓库（rename 式守卫对用户资产不可能 fail-closed，要设防需
换机制类——沙箱/钩子；收益不支撑扩面）。条件重估：出现"主仓库被讨论 agent 杂散写"的
可观测事件时，换机制类再议。

ceiling 修法验证（性能 6 项探针 + 铁律 3 项 + 真实布局复核）：正常态不受影响 / 锁态
fail-closed / 多路径正常 / 不存在路径容错 / 项目根及其子目录不受影响 / pi 自身的 footer
git 调用（JS 自实现上溯 + 在 repoDir 上只读 `symbolic-ref`）不受影响。

#### A2【中】`pgrep -f` 自匹配 —— 与项目已固化约定冲突（F4）

- `start_discussion._loops_alive:853-857` 用 `pgrep -f "meeting_loop.py.*<base>..."`；
- 探针实证：无 loop 时也能匹配到调用者自身命令行；
- **加重定性**：`docs/test-methodology.md` §2 已明文记录该坑与对策（方括号技巧/精确 PID），
  `scripts/check-residue.sh:98` 有现成实现——**违反项目自己已固化的约定**（设计符合度问题，
  无需等故障发生即应当修）。

**修复**：判据从"命令行文本正则"降为 **argv 字段相等**（简单给出 `/proc/<pid>/cmdline` 扫描
形态，无子进程无正则，顺带删目录边界正则与 `re.escape`）；性能实测 /proc 1.1ms vs
pgrep 5.9ms（附带改善，但不作为修改理由——理由是约定冲突）。

**实现约束**：比较必须与启动方同一构造 `os.path.join(base, "meeting_loop.py")`，且不得改变
launcher 构造式，否则"loop 存在但检测不到"会静默误报。测试面事实：现有测试无一断言"存活
loop 的 status"，换形态后测试面零改动（新增测试时假进程须用同一构造）。

#### A3【低】`check-residue.sh` 注释与实现不符（F9，同类第 4 实例）

注释称"过滤自身 shell PID（`$$` 及其父链）"，实现是 `grep -v "grep\|bash -c"` 文本过滤。
低优先（开发脚本非产品路径）。简单视角偏好改注释（零行为变化）；若改成 `$$` 过滤则注释
必须同批重写——不留"注释说机制 A、实现是机制 B"的状态。

#### A4【中】指令层冲突：项目 AGENTS.md 的 Git 准则 vs 工作协议（F8）

fork 模式的上下文含主项目文件自动发现（cwd/AGENTS.md 祖先发现），故 agent 的 system prompt 同时含：

| 来源 | 指令 |
|---|---|
| 主项目 `AGENTS.md`（自动发现） | "每次改动先更新本地 git（add+commit）"、"阶段性完成即推送" |
| `work-<agent>/AGENTS.md`（注入） | "不要执行任何 git 操作（commit/push/pull 由本地循环负责）" |

**切换叙事切不掉它**（尾部注入针对历史叙事，而主项目 AGENTS.md 是每次唤醒重新注入的常驻
规则）；在"改代码"类讨论里最尖锐（本场的"不执行 git 写操作"是 question.md 特有，通用形态
不带这句）。

**修复（三方一致）**：协议侧（`templates/AGENTS.md.tpl`）一句话优先级声明——项目规则中与
讨论流程冲突的 git 准则在本讨论中不适用。**反对**：(a) 移除主项目 AGENTS.md 注入
（产品上下文核心价值）；(b) 任何"指令合并/过滤/优先级引擎"（为一句文本冲突发明状态 =
过度设计）。分层依据：**机制性文本归脚本注入（协议），项目资产原样保留**。

---

### 批 B —— 读取路径净删（5 项，一次重构）

> 批 B 涉及 4 个文件（fs / engine / start_discussion / human_viewer），diff 规模最大，
> **建议单独立项评审**。

#### B1【中】`protocol.json` 读取散落 6 处 / 3 种读法 / 3 种失败语义（S1）

调用点：`meeting_engine.py:57`（participants）、`:63`（result_writer）、
`meeting_loop.py:520`、`start_discussion.py:1051`/`:1077`、`human_viewer.py:36`。
失败语义各不相同（engine 放任上抛 / loop `[fatal]` / start 报错 / viewer `{}`）。

**同根第二缺陷（探针实证）**：`result_writer()` 的
`proto.get("resultWriter", participants(workdir)[-1])` —— 默认值**无条件先求值**：
每次调用读两遍 protocol.json（实测 2 次），且 `participants` 为空时即使 `resultWriter`
已配置也抛 **IndexError**（`.get` 根本没机会返回）。

**修复（三方一致）**：
- fs 出**单形态原语** `read_protocol(bare) -> dict`（`HEAD:protocol.json`；任何失败返回 `{}`）
  ——**无 `ref` 参数、无双形态**（单形态 = 消除选择；"每调用点选源并写理由"的契约随之消解）；
- 6+1 处全走它；`human_viewer._protocol` 删除；
- **失败语义契约**：原语统一 `{}` + **各调用点一行显式政策**（loop：`if not proto: [fatal]
  exit 1`；engine `participants()`：`if not agents: raise`（响亮失败）；`check_status`：
  **无需政策分支**——core 空 dict 守卫使 `aggregate_mode({})` → `"meeting"`，自然落
  running/stalled）；
- **数据源定案 = 单源 bare HEAD**（engine 撤掉本地文件读路径——不是"新增第二源"而是"撤掉
  文件读"，engine 始终只有一条数据源，只是换成共享事实那条）。附带收益：消除"LLM 改本地
  protocol.json → engine 看局部视图"的风险（影响面归零）；
- **实现面**：`participants`/`result_writer` **保留 `workdir` 签名**，函数体内自推
  `bare = dirname(workdir)/repo.git` → **零调用点签名改动**（调用点：engine 3 处 +
  `human_sayer.py:55` 跨模块 1 处）；
- 测试面核实：四个 fixture（`test_dir_forms` / `test_main_paths` / `test_meeting_engine` /
  `test_meeting_loop`）的 setup 全部 commit+push protocol.json 到 bare，**测试面不受影响**。

#### B2【中】`check_status` 的 concluded 判定：文本 grep 误判 + 双定义（F1 = S2）

- 实现（`start_discussion.py:883-889`）：`git grep -l "^type: concluded$" HEAD -- :(exclude)human/*`
  ——**行文本 grep、扫 HEAD 全树**；
- 注释声称"读消息文件 frontmatter 的 type（不用 git grep 全文——正文出现会误匹配）"——**被实证推翻**；
- **精确触发条件（探针修正）**：需 `type: concluded` **整行独占**才触发（散文提及不触发；
  代码块引用 frontmatter 示例、协议片段粘贴是现实来源）。探针确认 **`result.md` 自身被命中
  即返回 `done`**；检索面是 HEAD 全树（唯一排除 `human/*`）；
- **影响链**：`check_status` 误报 done → `--wait` 的终止分支（stalled/stopped）被跳过 →
  落到 `incremental.done`（真实推导 = False）→ **10s 轮询无上界**（T3/e2e7 修复要消灭的
  失败类换形态回归）；
- **"任何消息含 concluded"语义无消费方（推演确认）**：唯一写者是 `write_protocol_signal`
  （`next_msg_id` → 恒为该 agent 最新消息文件），写入后 `agent_loop` 下一轮即退出（重启亦然）
  → "任一 agent 末条 == concluded"与"信号存在"在可达状态下等价，grep 的宽语义只有假阳性。

**修复（三方一致）**：删 grep 实现，**复用既有 `aggregate_mode`**（`check_status` 内两步组装：
fs 原语取 participants + `aggregate_mode(bare, agents) == "concluded"`）。

- **无第三份读取器、不新增引擎谓词**（`is_concluded(bare)` 被否：engine 已有单一定义，
  且新谓词会引入 engine 的第二条数据源路径）；不 import `human_viewer`（状态判定不挂展示模块）；
- done **保留两条件**（result.md 存在 **且** concluded），注释依据（简单给出）：
  **stalled 分支存在的全部意义就是"报告已写、流程信号未写"**——只看 concluded 则 stalled
  与 stopped 合并（丢状态），只看 result.md 则 stalled 直接消失（review5 A5 原始缺陷）；
  两个条件各对应一个已有分支 = check_status 的**状态图需要**；
- `result.md` 条件**不进 engine**（只有 check_status 用，搬进去 = engine 携带 CLI 专属分支）。

成本（性能实测）：收敛后 `--wait` 每轮 +6.3ms（含 participants from bare）= **0.063% 单核**；
**成本不构成选型依据**。双读登记口径："**每轮两次 `aggregate_mode` 全量读**（check_status
一次纯新增 + incremental 一次兼作显示）"——进修复 commit（附来源），不散落设计文档。

#### B3【中】engine 直做 fs 的 I/O，且同函数内读写路径不一致（F2）

- 清单（`meeting_engine.py`）：`:57`/`:63` 裸 `json.load(open(protocol.json))`；
  `:102-145` `_cat_batch` 内 `Popen git cat-file`（git I/O 落在 engine）；`:421`/`:429`
  `os.remove`；`:444` `open(path, "w")`；`:496-499` `os.path.exists/getsize`；
  `:529` `open(result_path, "w")`（兜底写 result.md）；
- **自身注释即依据**：`commit_new_files:416` 写着"IO 归 fs（L16）"并走 `read_message`
  ——同一函数里**读走 fs、写/删走裸 os**（`finalize_discussion` 同理）。

**修复（三方一致，三条边界）**：
1. fs 补**原语** `cat_batch(bare, paths)` / `remove_message` / `write_text`（只搬原语，
   **不做"快照对象"跨函数传**——跨函数状态比多一次调用复杂，且诱导缓存）；
2. **批量读形态原样保留**（`_each_agent_messages` 每轮循环顶都跑；性能红线：逐条
   `git_show` = **16.8× 回归** 733ms vs 43.6ms@498 条）+ **性能理由随函数迁移**
   （docstring 那句"不用每消息一次 git_show（O(n) subprocess）"不得丢失）；
3. engine 保留组装职责（吃 fs 原语返回的原始内容，自己派生判定），fs 只出原语。

#### B4【低】两个 git 执行入口，quotepath 加固不一致（F3）

`meeting_fs.run_git:25` 带 `-c core.quotepath=false`（注释自述"一处统一，全部命令生效"）；
`start_discussion.run_cmd:37` 是裸 git（被 `check_status` 的 log/grep、`--wait` 的 show 等使用）。
当前调用恰好不解析路径输出（未触发已知 bug），但加固只覆盖一半，是同类问题的潜在根因。

**修复**：**只留一个**——读路径（log/grep/show）改走 `fs.run_git`；`run_cmd` 退回"一次性环境
命令"（init/clone/config/pgrep 改 /proc 后只剩这些）。与 F2 同批。

#### B5【低】`_unlock_git` 与 `recover_git_lock` 逐字重复（S3）

`meeting_loop.py:207-216` 与 `:219-225` 条件/目录名/rename 逐字相同，仅差一行 log。

**修复**：合并为 `restore_git_lock(workdir) -> bool`（**纯函数 + 返回值**，log 由唯一需要它
的调用点决定 → 保住"只在确实恢复时打"；finally 忽略返回值，启动 `if restore: log(...)`）。
保留在 `meeting_loop.py`（配对可见性，L4 原理由仍有效，不搬 fs）。

---

### 批 C —— 注释与文档（4 项）

#### C1【低】`agent_loop` 注释"其余判定全部派生"过强（F5）

注释（`meeting_engine.py:583-584`）称"循环顶一次全量 bare 读取…其余判定全部派生"；
实际 `human_msg_count` / `_stall_elapsed` 每轮各自 `ls-tree`，`rr_next_speaker` 在 RR 分支另读。

**修复**：注释改成"消息列表类判定派生；计数/时序类单独读（各自无状态）"——零成本事实对齐。
**不做收编**（三方一致：为 ~3ms/轮 跨函数传快照并约束调用顺序不划算）。

#### C2 "声明 vs 保证"类目（四实例，随批扫查）

同类病症：**注释/文档的断言强度或范围超过机制保证**。

| 实例 | 声明 | 实际 |
|---|---|---|
| F1 `check_status` 注释 | "不用 git grep 全文——正文出现会误匹配" | 就是 git grep 全文，且断言不准 |
| A1 `_lock_git` 注释 | "任何 git 操作失败" | 静默换仓库成功（上溯） |
| C1 `agent_loop` 注释 | "其余判定全部派生" | 3 处每轮各自重读 |
| A3 `check-residue.sh` 注释 | "过滤自身 shell PID（$$ 及其父链）" | 文本模式过滤 |

这正是 `docs/test-methodology.md` 方法 11 的领域——但此前主要落在**制品文本**（文档/日志），
**代码内注释未被同一标准扫过**。建议随批做一次全称断言扫查（搜"任何/所有/全部/唯一/始终/
总是/永不"），逐条对照机制回答"凭什么成立"——**不允许留下回答不了的全称断言**。形态：
不新增工具/文档，结果直接以注释改动进修复 commit。

（自我更正记录：铁律视角在本讨论中自己也犯过一次"表述过宽"——F1 首轮说"正文引用很自然"
经探针后收窄为"需整行独占"；另一次是把己方推演写成他人陈述，经核对原文后更正。
这类错误的自然发生率就是这么高。）

#### C3 数字三分法与"一个数字一个家"

| 类型 | 例 | 去处 |
|---|---|---|
| **结构性质**（不随数据漂移） | "批量读一次进程 / 逐条读 O(n) 子进程" | **docstring**（即"理由随迁"那句） |
| **修复依据的实测值** | 733ms/43.6ms、16.8×、1054.6ms→1.0ms、5.9ms→1.1ms | **commit message**（带口径 + 来源） |
| **长期可复用的口径数字** | 构建 181ms/55MB、pi 会话 RSS、校准比 1.66× | **`docs/design.md` 口径节** |

判据：**会在下一次评审中被引用吗？** 会 → design.md；只解释本次为何这样改 → commit。
边界：**一个数字只留一个"家"**（commit 是溯源记录、design.md 是长期引用点，不并存两份
权威值；长期引用点漂移时就地更新带新口径）。

**术语修正**：上述第一类**不称"不变量"**——该项目 `不变量` 已专指 **I1–I5**（fork 源产物
结构约束，docstring / design.md / 测试名三处对齐），一词两义会造成歧义。改称**结构性质**。

#### C4 文档修正（3 处）

1. **F6**：`docs/design.md` §二 `forkSourceDropped` 条未写明**仅 budget 模式写该字段**
   （代码 `meeting_fs.py:772-777`：两字段都在 `if mode == "budget":` 分支内）——读者会以为
   三模式都有。一行修复（建议措辞见 性能/0009 §4）；
2. **措辞修正**：design.md §三 被否决方案行"唯一收益是内存峰值"被实测推翻（json.loads 占
   构建 **49%** 耗时、**90%** 解析条目被弃用）——改"收益 = 内存峰值 + 解析时间"（否决结论
   不变，触发条件不变）；
3. **规模口径补行**：design.md §四 只记 loop 侧内存（165MB 量级），漏掉主导项——
   **pi 会话常驻内存 ~1GB/个**（实测 935/1157/954MB，`ps -o rss`，本机 16GB、当前会话长度
   含扩展；触发：N≥8 或内存紧张时复核）。**与现有 loop 侧行并列**（差一个量级，读者需两行）。

#### C5 合规项（修复完成判据的一部分）

`AGENTS.md` Git 准则第 5 条："对协议行为、产品形态的任何修改，必须同步 README 与相关模板。"
本批至少两处触发：F8（协议模板加声明 → 同步 `templates/AGENTS.md.tpl`）、A1/C1/C4 的
行为描述修改（同步 README 机制表 / AGENTS.md 对应节）。修复单里"同步项"与"先红后绿测试"
**并列**为完成判据，不是事后跟进。

---

## 三、确认无问题（量化支撑）

1. **轮询热路径**（`agent_loop` 空闲路径）：26.5ms/轮/agent、8 个 git 子进程；扩展性线性
   （39→8.2ms / 198→20.1ms / 498→52.6ms ≈ 0.11ms/条 + 3ms 固定）；3 agent 并发 ≈ 4% 单核。
   触发轮有界（最坏 33.8ms）。当前规模无瓶颈。
2. **职责边界正例**：`meeting_core` **零 import**（纯判定无 I/O）；`fake_agent`/`meeting_loop`
   通过注入 responder 复用唯一状态机；`human_sayer` 写路径全走 fs 原语；`human_viewer`
   无写路径；`mv.sh --status/--wait/--cleanup/--prepare` 全部委托 python（无 bash 复刻）；
   视角校验两入口同源（`_discover_viewers` 单一实现）；I1–I5 有测试且 I4 用 replay oracle
   （强断言形式）。
3. **F1 修复的成本不构成障碍**（三种实现同量级，`--status`/`--wait` 按需调用）。
4. **空 participants 安全回落**（`aggregate_mode({})` → `"meeting"`，消费端实测
   `incremental(bare, [])` → `done=False`——无需兜底分支）。
5. **单源 bare 读与守卫无交互**：bare 是 `base/repo.git`（独立目录），锁只动
   `base/work-X/.git` → 对锁免疫。

---

## 四、记录项（不修 / 维持 defer，附触发条件与观测点）

| 项 | 实测/性质（口径） | 触发条件（观测点） |
|---|---|---|
| fork 构建解析时间 O(源) | json.loads 109ms = **49%** 构建耗时；**90%** 解析条目被预算弃用；15MB 源 → 224ms 构建 / 57MB 峰值 RSS | 源 ≥100MB 或 RSS ≥500MB（`design.md` §四 既有触发；本轮修正其措辞见 C4） |
| `append_handoff_turns` 全量 parse | 12ms/次（与 §四 既有记录一致） | 维持不修 |
| 每轮 3×`ls-tree` / 2–3×`ls-files` 重复读 | ~3ms/轮（0.1% 单核） | 子进程数/轮成为可观测瓶颈时（当前 8 个）；不做收编（状态传递论证） |
| `--wait` 双读 | 每轮两次 `aggregate_mode` 全量读（incremental 一次兼作显示） | 登记进 F1 修复 commit；不修（两个替代法更差） |
| 三处冗余 pass（`_entry_text` 双算等） | 合计 ~11ms = 构建的 6% | 构建 >1s 时复测（首唤日志已含构建耗时与峰值 RSS） |

---

## 五、方法论沉淀（本讨论产出）

1. **引用前先核对原文**：铁律视角把己方推演写成"某某/000N 的修正"——引用完整性要求
   包含"引用前核对"这一半（被引用物变化 → 同批更新全部引用处）。
2. **超窗/守卫类修复的"三方独立探针"模式**：同一现象由三视角各自用不同方法观测
   （性能测成本、铁律测机制、简单测调用面），结论互证后才定案。
3. **修复完成判据 = 先红后绿测试 + 同步项**（不是"代码改完"）。
4. **共识 ≠ 用户裁决**：视角共识是**建议的修复清单**，实施取舍以用户确认为准。

---

## 六、未覆盖与开放项（诚实声明）

- **352 个测试未逐一审计**（抽样覆盖：fs 不变量、human 通道、main 路径、wrapper）；
  断言强度的全量评估不在本轮结论内。
- **修复后回归未验证**：批 A/B/C 全部未实施（代码未改）——各自视角的"修复后回归"声明
  延续待实施后验证。
- **pi 端到端行为**：ceiling 注入后 pi 的 footer 等表现需真实唤醒回归确认。
- **pi 进程启动/上下文加载延迟**（唤醒延迟的一部分，pi 侧）未测。
- **批 A/B/C 的实施顺序与工作量**未评估（属实施计划）。
- 修复依据的性能数字均为本机、fixture 或活现场口径，真实会话正文分布不同时量级可能上浮
  （离可感知阈值有两个数量级余量）。

---

## 七、给实施方的建议顺序

1. **批 A**（守卫）：独立小批，diff 小、收益明确（fail-closed + 1000× fail-fast）；
2. **批 B**（读取路径净删）：涉及 4 文件，**建议单独立项评审**后再动手；实施时用 grep
   重新枚举调用点（不沿用任一视角的列表），两个契约点（失败语义 / 数据源）为前置条件；
3. **批 C**（注释/文档）：随 A/B 实施同批完成（同步项 = 完成判据的一部分）。

---

*本结论由三视角协同分析产出；各方结论原文见 `work-性能/`、`work-简单/`、`work-铁律/`
各消息文件（讨论目录 repo.git 历史为权威单一事实源）。*
