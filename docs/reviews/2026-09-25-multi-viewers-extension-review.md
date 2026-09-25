<!-- 存档：docs/reviews/2026-09-25-multi-viewers-extension-review.md
     来源：一次真实多视角分析的 result.md 原文（未删改，仅加本头与下方说明）。
     分析场次目录已随 cleanup 删除；文中消息编号（如 效率/0003）不可再核验，
     仅作溯源线索（与代码注释引用约定一致：行为以自描述为准）。 -->

# 存档说明

- **主题**：审阅把 `/multi-viewers` 改成 extension 这次的实现（extension 代码 /
  CLI 机器标记行契约 / 文档同步）
- **场次**：`mv-mv-main-20260925-094526`（3 视角：效率 / 简单 / 铁律；真实 pi 讨论，
  默认档 `mc-tools`、`forkMode=budget`、`maxMeeting=15`；约 49 分钟，三方共识收敛）
- **本场同时是两件事的首次验证**：
  - **extension 流程的首次真实使用**（prepare → 暂停点弹窗 → start → 预填观看命令；
    本场暴露 2 项静态审阅看不到的问题：① 主题连引号进入 topic/question.md
    ② 观看命令只有"预填"一个出口——被覆盖后用户找不到它）
  - **A 形态（spec 不再经 LLM 扩写）的首场**：本场未跑偏（守住了"只提意见不改代码"，
    每项带 `文件:行`）——A 的判定窗口按报告 §六.1 为 1+2~3 场
- **落地**（本场 6 类问题 + 2 项首用发现，一次收口）：
  P1 `stalled` 并流进 done 路径（不再让用户等一个永不到来的收尾）·
  P2 `stalled` 用独立文案（不复用 done 的 result 路径文案）·
  P3 删掉与暂停点自相矛盾的过期头注释 ·
  P4 删掉单机回退路径，改**加载期响亮失败** ·
  P5 两个扩展**合并为一个单元**（三命令 + `shared.ts`，消 ≈35 行已漂移的重复）·
  P6 取消提示补"在当前 pi session 内执行" + **harness 进仓库**（`tests/extension_harness.ts`，
  `run_tests.sh` 缺 bun 可见跳过 / `MV_REQUIRE_BUN=1` 严格）·
  首用两项：主题剥引号 · notify 里带一份观看命令
- **明确不做**（随决策 22 记录）：`--json` · resume 命令面 · env 回退配置 ·
  `runCli` 超时 · 启动路径继续优化（已在 1–2s 地板）

# /multi-viewers extension 改造审阅 · 三方共识结果

**主题**：审阅把 `/multi-viewers` 改成 extension 这次的实现（代码 / 标记行契约 / 文档），
只提意见不改代码。
**参与者**：效率 / 简单 / 铁律（3 视角；meeting 自由讨论 + round-robin 全体 pass）。
**方法**：只读核对，每项带 `文件:行`；语义类问题**先推演确认再定论**（铁律「发现偏差先
推演」）；**本场未修改任何文件、未运行真实分析**。
**场次**：`mv-mv-main-20260925-094526`（fork 模式，mc-tools 档）。

---

## 0. 结论摘要

1. **方向成立、净收益为正**：流程骨架零 LLM（代码执行 prepare → 门禁暂停点 → start →
   预填观看命令）；机读判据从"读中文文案"改为**退出码 + 机器标记行**；内容起草仍留普通
   对话。省下的是 3–4 个主 session LLM 回合与 4 类已观测失误的返工，**收益不在启动路径**
   ——启动机器成本已到 1–2s 地板（实测：env 创建 0.91s、fork 655ms/84MB/agent、
   单次 CLI 58–66ms）。
2. **共发现 6 类问题**：其中 **stalled 分支是唯一行为错误**（把"无 loop 存活的静止态"
   指引成"等待一个永不到来的收尾"）；其余为契约缺口（`[result]` 对 stalled）、过期注释、
   开发机回退补丁、跨扩展重复 + 稳定入口约定、取消提示可发现性 + 测试半边缺口。
3. **讨论收敛出 10 项冻结实现清单**（§三）与 **7 项「本轮明确不做」**（§四），并裁定
   5 处设计语义：stalled 并流、`[result]` 取 (ii)、回退删除改加载期响亮失败、
   mv.sh 记"第一方直连"例外、launch 失败窗口只记注释不改行为。
4. **净复杂度账为负（变简）**：合并消 ≈35 行逐字重复（且副本已实际漂移）；删回退消一条
   单机死分支；stalled 并流 ≈0 净增；`[result]` 取 (ii) 零契约增量。
5. **执行建议**：**合并先行** → 其余编辑落在同一扩展单元 → 文档 + harness 一次收口 →
   **一次 reload、一次真跑复验**（不按项分次真跑）。

---

## 一、总体判定

| 维度 | 判定 | 依据 |
|---|---|---|
| 职责边界 | 成立 | 扩展只做"流程骨架 + UI"（spawn CLI、解析标记、弹窗、预填）；引擎/IO/判定留在 CLI/loop；LLM 内容起草未进 handler。唯一越界点是扩展**重述**观测层状态语义并抄错（stalled，P1） |
| 复杂度匹配 | 成立但有一类债 | 无新概念被引入（标记行沿用既有 `[status]`/`[result]` 约定）；债在跨扩展复制与开发机回退（P4/P5）；合并后净减 |
| 设计符合度 | 大体符合 | 与设计决策 22 一致（零 LLM 骨架、两段式、标记契约）；偏差集中在过期注释（P3）、`[result]` 缺口（P2）与文档措辞 |

参考基线：`extensions/multi-viewers/index.ts`（~230 行，主入口，2 天改 2 次）、
`extensions/multi-viewers-say/index.ts`、`mv_cli.py`、`observability.py`、
`start_discussion.py`、`docs/design.md` 决策 22、`AGENTS.md`、`README.md`。

---

## 二、确认问题清单

### P1（行为错误，唯一）stalled 被并入 running → 指引用户"继续等"

- 状态单源：`stalled = 有 result.md 无 concluded 且 **loop 均不存活**`（`observability.py:109`）；
  `--wait` 对它的既有出路：「**停止等待**；可读 result.md 或 --cleanup」（`observability.py:165-168`）。
- 扩展现状：`state === "running" || state === "stalled"` → 「分析仍在进行——完成后再说」
  （`extensions/multi-viewers/index.ts:191-196`）→ 用户会等一个**永远不会到来**的收尾。
- 安全前提已核：`cleanup_discussion` 先保存 result.md → 打印报告 → 再删目录，且**无状态
  守卫**（`start_discussion.py:435-459`），所以 stalled 下走 cleanup 完全可用。

### P2（契约缺口）`[result]` 标记只在 done 打印 → 并流后必然踩空

- `--status` 仅 `st == "done"` 时打印 `[result]`（`start_discussion.py:624-629`），且有
  negative 测试锁 running 不打印（`tests/test_main_paths.py:674-680`）。
- 扩展 fallback `result ?? "(未找到 result 路径)"` 与 confirm 正文的 `?? "?"`
  （`index.ts:210,216`）在 stalled 下必然命中。
- 更深一层：stalled 下 `<base>-result.md` **尚未生成**——保存在收尾（`meeting_loop.py:668-670`，
  rw 正常退出后）或清理（`start_discussion.py:448-449`）触发，而 stalled 的定义正是
  rw 崩溃在保存之前（`meeting_fs.py:1224-1245` 为保存实现）。
- 裁定：**取 (ii)**——扩展侧 stalled 用独立文案（"result.md 已提交；清理会先打印报告并
  保存结果"），CLI 判据、决策 22 标记清单、negative 测试**零变更**。反对 (i)（CLI 对
  done|stalled 都打印）：会把"已存在"与"将存在"两种语义压进同一标记（状态双义）。

### P3（文档/注释偏差）文件头注释与门禁行为自相矛盾

- `index.ts:24` 仍写「要改 spec 就先取消，编辑后自行 --start」，而 handler 已是
  confirm **暂停点**（`:137-144`，用户 2026-09-24 拍板：弹窗保持打开、可在其它窗口编辑、
  改完点确认继续）。修法：**删掉那句**，头部只留最短准确陈述（复述必漂移的反例）。

### P4（补丁）回退 `/root/pi-multi-viewers` 是单机死分支

- `FALLBACK_ROOT = "/root/pi-multi-viewers"`（`index.ts:57`）；say 扩展为逐文件三目回退
  （`multi-viewers-say/index.ts:53-59`）——两种写法本身即漂移。
- 触发条件（找不到包根 = 包被拆散/复制）恰是"开发机以外"的场景 → 在非开发机 100% 指向
  不存在的路径，把"包根解析失败"变成更晚、更难诊断的失败。
- 裁定：**删除，加载期 throw + 行动性错误信息**（"找不到 pi-multi-viewers 包根——请用
  pi install / npm 安装"）；**不加 env 覆盖**（新配置面=新概念，无真实场景）。

### P5（重复 + 约定）跨扩展复制 ≈35 行（已漂移）+ 稳定入口未闭合

- `findPackageRoot` 两处**逐字相同**（`index.ts:39` 与 `multi-viewers-say/index.ts:35`），
  连同导入样板重复 ≈35 行；副本已实际漂移（P4 两种回退写法即证据）。
- 已核实合并无平台障碍：上游扩展**支持多文件**（`docs/extensions.md:237-243`）；加载器
  **一层扫描、子目录只认 `index.ts`/manifest**（`loader.ts:665-698,704-708`）；一个扩展
  注册多命令可行（`commands` 为 Map，`registerCommand` 多次调用各自成键，`loader.ts:287-294`）。
- **约束（防静默复杂度）**：助手模块必须放**入口子目录内**；`extensions/` 平级**禁放**
  `.ts` 助手（规则 1 会把它当独立扩展加载）。
- 稳定入口：`scripts/mv.sh` 是文档化"稳定入口"，但扩展为包内第一方消费者、直连
  `mv_cli.py`（say 已有直连先例）。**性能上零差异**（实测 shim 61ms vs 直连 58–66ms）
  ——裁定按**契约一致性**记一行"第一方直连"例外，不以性能论据选边。

### P6（可发现性 + 测试）取消提示的 sid 条件 + 契约测试只锁生产者半边

- `mv_cli.py:348-353` 目录名 `mv{-(sid)}-<stamp>`；`--find-dir` 按 `mv-<PI_SESSION_ID>-*`
  匹配且**无兜底**（`observability.py:819-834`）。pi 的 bash 工具默认注入 `PI_SESSION_ID`
  （上游 `bash.ts:234` 默认 true → `183-185`），所以 pi 内 `!`-执行没问题；**外部终端**
  执行则目录无 sid，之后 `/multi-viewers-say`、`/multi-viewers-finish` 都定位不到。
  修法：取消提示加一句「（请在当前 pi session 内执行）」；**反对**新增 resume 命令面。
- `tests/test_mv_cli.py::TestMachineMarkers` 锁住 CLI 侧三条标记（好），但**扩展消费端
  零仓库内测试**（上次 16 断言的装置是临时且已删）。这是唯一值得追加的投入。

---

## 三、冻结实现清单（三方共识，实现者对照用）

1. **stalled 并流**：`running` → 等待；`done|stalled` → notify → confirm → cleanup
   （文案按状态参数化；**两处 fallback 不得复用于 stalled**，`index.ts:210,216`）；
   `stopped` → 建议手动清理；守卫保留（CLI 侧无防，扩展是唯一防线）。
2. **头部注释删句**（`index.ts:24`）。
3. **取消提示加句**：「（请在当前 pi session 内执行）」。
4. **删首条 notify**（`index.ts:130`；弹窗与取消分支已各给一次路径）。
5. **删回退** → 加载期响亮失败（无 env、无 `??` 残留），错误信息给行动。
6. **合并**：一个扩展单元、三命令；助手模块放入口子目录内；AGENTS 记"平级禁放 `.ts` 助手"。
7. **harness**：单套覆盖合并后单元；`tests/` + `run_tests.sh` 检测 bun、无则**可见跳过**、
   `MV_REQUIRE_BUN=1` 严格开关；断言按**新语义**（stalled → 不含两种 fallback 文本、
   confirm 前不 cleanup、confirm 后 cleanup 一次且输出含保存路径；running/stopped 不清理
   的防线性保留）。
8. **文档同步**（按最终结构取一，勿并存）：AGENTS 结构清单/「当前形态」、**mv.sh 例外一行**、
   design 决策 22（并流 + (ii) + 下述"不做"附注）、`design.md:561` 双空格；
   `TestMachineMarkers` 与 `test_main_paths` 不动（(ii) 零 CLI 变更）。
9. **`cmd_start` why 注释一行**（`mv_cli.py:363-368` 旁）：删除与创建绑定 = 一次性消费；
   launch 失败则 spec 已消费、须 cleanup；重估触发 = 观测到 launch 失败或引入 resume/retry。
10. **一次 reload + 一次真跑复验**（批次执行，不按项分次）。

执行顺序：**合并先行**（后续编辑落在同一单元）→ 文档 + harness 一次收口。

---

## 四、「本轮明确不做」（随决策 22 记录，防翻案）

`--json`（新输出模式）· resume 新命令面 · env 回退配置 · `runCli` 超时（现结论"可改可
不改"；若将来加，只对只读调用且走共享一处）· 启动路径继续优化（已在 1–2s 地板）·
删除点顺序重排（改注释记录，不改行为）· 给 stalled 加第三种收尾动作或把"读 result"
搬进扩展（读走 `--report`/直接开文件，cleanup 自打印报告）。

---

## 五、讨论中的语义裁定与记录校正

| # | 事项 | 收敛过程 |
|---|---|---|
| 1 | **stalled 处置** | 简单初判"四分支保留"（未核状态语义）→ 铁律补核：stalled = 无存活 loop、`--wait` 出路是停止等待 → 双方同形**并流**（`done|stalled` 共用路径，零复制、分支数不变） |
| 2 | **`[result]` 契约** | 铁律 0003 曾偏 (i)，0005 改主张 (ii)（语义纯度论）；简单 0004 明确选 (ii)；效率倾向 (ii) → **三边 (ii)** |
| 3 | **回退路径** | 简单最初主张"统一两种写法"，0003 撤回改**删除**（"统一=两条死路归一，删除=零条死路"）；效率、铁律同 → 三边闭合 |
| 4 | **记录校正** | 效率原写"start 失败不删 spec"；铁律按 `mv_cli.py:359-373` 校正为「**创建失败不删 spec；launch 失败时 spec 已删**」→ 效率撤回并接受（错误不变量会被用来设计恢复路径，必须更正记录） |
| 5 | **mv.sh 取舍** | 效率实测排除性能论据（61ms vs 58–66ms）→ 按契约一致性记例外 |

---

## 六、遗留与观察项（非本轮改动）

1. **A 方案判定未完成**：`question.md` 现在只有主题原文（零 LLM 扩写）。判定窗口 = 本场
   及随后 2–3 场：首轮是否跑偏、是否守"只提意见不改代码"、轮次数 vs 基线。触发条件 =
   **≥1 次明显跑偏 → 启用"起草 prompt"**；实现必须是**独立对话/命令**（在 `/multi-viewers`
   之前完成），**不得**进 extension handler（决策 22 已否决的 A 变体）。
2. **唤醒结束日志**：归 loop（唤醒时序的拥有者），独立于本次收口——它是每唤延迟
   （48–222s，墙钟大头）的唯一度量入口，属另一议题。
3. **launch 失败**：发生率无记录；出现一次即触发"删除点/恢复路径"重估（见清单 9）。
4. **效率的流程观察**：同一份证据被三个视角各读一遍（本场为取证共约 10+ 次本地调用）
   ——取证值得，但机制上值得考虑复用以降感知成本（非实现问题，留档）。
5. **数据口径提醒**：效率的启动/唤醒数字为 n=1 实场观测（首唤 147–222s vs 稳态基线
   ~48s/唤醒），不构成承诺。

---

## 七、验证与局限

- 本场结论全部来自**只读核对 + 源码推演**：所有 `文件:行` 为 2026-09-25 工作区现场；
  上游加载器结论引自 `/root/research/pi`（v0.87 线）。
- **未做**：修改任何文件、运行真实 LLM、跑 harness（harness 尚不存在）。
- 落地正确性由实现批的 harness（新语义断言）+ **一次真跑复验**保证；若实现中出现
  新分支/新命令面/新输出模式，按本轮既定判据（能否删一个分支、复杂度增量应 ≤0）再议。
