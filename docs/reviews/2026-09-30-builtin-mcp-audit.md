<!-- 存档：docs/reviews/2026-09-30-builtin-mcp-audit.md
     来源：一次真实多视角分析的 result.md 原文（未删改，仅加本头与下方说明）。
     分析场次目录已随 cleanup 删除；文中消息编号不可再核验，仅作溯源线索
     （与代码注释引用约定一致：行为以自描述为准）。 -->

# 存档说明

- **主题**：自审 —— agents 的联网检索能力从第三方 `pi-mcp-adapter` 换成 pi 内置 MCP
  （`-e builtin:mcp`）+ `exposure: direct`：这个改动站不站得住？有没有没想到的副作用？
- **场次**：`mv-mv-main-20260930-135705`（3 视角：效率 / 简单 / 铁律；真实 pi 讨论；
  `forkMode=budget`、`extensionPolicy=mc-tools`（声明=生效，无降级）、配额 **meeting=20**
  （用户的默认值配置）、`maxRR=7`；墙钟 **32m43s**、38 条消息、35 次唤醒（15/10/10）、
  rc≠0 0 次 / retry 0 次、收尾中位 1.1s、存续 **共识**（RR 全体 pass）、并行度 2.34；
  对象 = 本仓 HEAD `af9ee53`）
- **判定**：**替换成立**（净简化 + 职责归还平台 + 第三方依赖清零）；同批落地 9 项
  零运行时成本修正；**配置层不动**（三方撤回各自的配置建议——主 pi 实测在
  用全部三个 server：12 个 session / 13,144 次 toolCall 里 51 次 mcp 调用）
- **本场最重的发现（新问题）**：**per-server 静默失效** —— 某个 MCP server 连不上 /
  凭据过期时，agents 当次静默失去那件工具，分析照常跑完且产物零痕迹。链条（我逐处
  复核过上游源码）：`Promise.allSettled` 吞掉失败（`extensions/mcp/index.ts`）→
  `reportProblems` 只 `ctx.ui.notify`（不 exit）→ 非交互模式用 `noOpUIContext`
  （`core/extensions/runner.ts` 的 `notify: () => {}`）→ 我方只在 rc≠0 时读 stderr
  ⇒ **`rc=0` ≠ 工具可用**（我此前探针报告里的那条推论说满了，撤回）
- **本场同时验证**：`-e builtin:mcp` **真生效**（另一场功能臂真搜到网页标题）；每唤醒
  成本 **≈ 0**（38 次唤醒「启动前」中位 −1.3s、三 agent 合计 −2s/−5s/+19s，对照 AFT
  445.9s 与 MC historian 447s 差三个数量级）；agents **自然**用 MCP = **0 次**
  （边界后逐条统计工具调用：bash 58/65/56、read 7/0/19、write 10/10/15、edit 4/1/0，
  无一条 `mcp__*`）
- **过程诚实记录**：三视角共撤回/更正 13 项（含两处**曾被判死代码的分支改判为留**、
  一处"唤醒成功 ⇒ MCP 已加载"的推论撤回、若干自己的配置建议撤回）
- **落地**：`839ef4f`（九项：删无据断言 / rc≠0 落 stderr / 报告「平台能力：未观测」/
  两条守卫注释改准 / `or reason` 真实理由 + 移除条件 / 三条不变式 / 前提 pi≥0.99 /
  exposure 成本口径 / 值域三档边界；495 python + 54 harness 全绿）；方法论 #27
  （检测器不得继承被检测对象的失效面 · 多余的文字一旦断言未知即错误）

# 多视角分析结果：内置 MCP 替换（`-e builtin:mcp`）与 `exposure: direct` 自审

- **场次**：`mv-mv-main-20260930-135705`（效率 / 简单 / 铁律 三视角，模型 `…deepseek-v4.1-flash`, xhigh）
- **对象**：本仓 HEAD `af9ee53`（`feat(ext-policy)!: mc-tools 第二份入口改用 pi 内置 MCP（删第三方 adapter 依赖）`）
- **判定**：**替换成立，可以发布**；但必须同批完成 9 项零运行时成本的修正/落字（§6），
  且有一项新发现（per-server 静默失效，§3）需要在文档中如实声明。

---

## 1. 结论摘要

| # | 结论 | 状态 |
|---|---|---|
| 1 | 第二份入口从第三方 `pi-mcp-adapter` 换成 pi 内置 `-e builtin:mcp` = **净简化 + 职责归还平台** | 三视角一致支持 |
| 2 | `--no-extensions` 关的是"扩展发现**与内置扩展**" ⇒ **必须**显式 `-e builtin:mcp`（实现前提，已落字） | 已满足 |
| 3 | **新发现：per-server（连接级）失效在本形态下完全静默**——`rc=0 ≠ 工具可用` | 三视角一致（互补证据） |
| 4 | 观测能力位（MCP 探针）：**本轮不做**，登记为"条件触发的既定路径" + 报告写"能力未观测" | 三方一致（B+登记） |
| 5 | 两处曾被判"死代码"的分支改判为 **留**（`else: raise` / `or reason`） | 三方一致 |
| 6 | 一处**无据断言**必须删（`meeting_loop.py:356` 半句，5 个副本） | 三方一致 |
| 7 | A 层三项（有墙钟成本）**无干净注入点 ⇒ 只文档化**；配置层改动 = **用户拍板项** | 三方一致 |
| 8 | 本场推荐改动集**不需要任何真实 LLM 运行**验证（单测 + harness，分钟级、0 LLM） | 三方一致 |

---

## 2. 替换成立的理由（三视角证据）

- **铁律（职责边界/复杂度）**：删掉 `resolve_mcp_adapter_entry`（19 行）+ `MCP_ADAPTER_PACKAGE` + 测试侧 6 处 mock，
  用一个**常量** `meeting_fs.BUILTIN_MCP_ENTRY = "builtin:mcp"`（`meeting_fs.py:903`）替代——
  从"解析第三方包内部布局"降为"引用平台标识"，是**职责归还平台**；第三方依赖在本档清零
  （本档 = pi 内置扩展 + 用户自选的 MC 包）。
- **简单（净简化）**：降级状态空间从"2 入口 × 各 on/off + 部分/完全标签"收敛为"1 个可失败入口"；
  测试 mock 点 16 → 9；遗留符号全仓活代码 0 命中。
- **效率（成本）**：固定成本打平/略优——
  - **token 差值 +1,025/请求**（探针 n=2，两臂各两次独立运行同值：含 = 105,334 / 不含 = 104,309；
    配置 = 3 server / 5 工具全 `direct`）；
  - **延迟边际 +0.3–0.8s/唤**（被组内方差淹没，与旧 adapter 的 +0.2s/唤同量级）；
  - **3 个 server 并行连接**（上游 `extensions/mcp/index.ts:816` `Promise.all`）⇒ 成本 = max(单 server)，非 Σ。
- **功能验证（真跑一次，含内置 MCP 的臂）**：要求"用 MCP 搜一次"的臂真实取回网页标题
  （`The Agent Harness Where the Coding Agent Extends Itself`，pyshine.com）⇒ `builtin:mcp` **确实生效**。
- **操作前提（已核 pi --help:43 原文 + 上游源码）**：`--no-extensions` 亦关内置扩展
  （`core/extensions/index.ts` 的 `{ name: "mcp", builtin: true }`；`core/resource-loader.ts` 在
  `noExtensions` 时只保留 CLI 显式 `-e`），且 `-e <path>` 接受 `builtin:<name>`。
- **未知 builtin 名 = loud + fatal**（核实三处源码）：`core/resource-loader.ts:715-719`
  （`Unknown built-in extension`）→ `main.ts:793-797` 诊断 → `main.ts:906-916` `process.exit(1)`。
  ⇒ 因此 **pi < 0.99 不是静默降级，而是每次唤醒 rc=1 硬失败**（可见）。

---

## 3. 本场最重要的发现：**per-server 静默失效**（完整链条）

**结论：某个 MCP server 连不上 / OAuth 过期时，agents 当次唤醒静默失去联网工具，而分析照常跑完，
报告里 rc≠0=0、收尾≈0、"生效=mc-tools" 一切干净。**

链条（逐处上游源码 + 我方代码）：

| 环节 | 位置 | 行为 |
|---|---|---|
| 单 server 连接结果 | `extensions/mcp/index.ts:816` `Promise.all` + **`:818` `Promise.allSettled(getClient)`** | 失败被 **吞掉**，不抛 |
| 连接失败/需认证 | `:408-419` `reportProblems` → `ctx.ui.notify(…, "warning")` | **不写 `runtime.diagnostics`** ⇒ 不 exit（对照 `main.ts:906-916`） |
| 10s 帽触发 | `:833-847`（`startupWaitMs` 默认 `:79`=10000） | 只 `ui.notify(..., "info")` |
| 整体加载失败 | `:820-826` / `:884` | 也只 `ui.notify(..., "error")` |
| **通知通道本身** | `core/extensions/runner.ts:323-327` `noOpUIContext.notify = () => {}`；`:565` 未传 ui 即用 noOp | **我们的模式（`--mode json` + `--print`）既非 rpc（`rpc-mode.ts:320`）也非 interactive（`interactive-mode.ts:2532`）⇒ notify 是 no-op** |
| 我方消费 | `meeting_loop.py:515-521` | stderr **只在 `rc≠0` 时**被读；`rc=0` 时丢弃（`_log_wake_done` 只记 session/elapsed/rc，`:527-537`） |
| 我方产物 | `wake-logs/*.txt`（`meeting_loop.py:491-498`） | 只有命令行全文 = 证"**请求**了 `-e builtin:mcp`"，不证"加载成功" |
| 登记行 | `meeting_loop.py:367-376` | 由策略代码产出 = 证"**策略跑过**"，不证任何 server 连接 |
| session 文件 | 本场三个 agent 会话逐条枚举 | 条目类型无工具集条目；`session` 头只有 fork 元数据；`toolNames`/`availableTools` 递归 **0 命中** ⇒ **生效工具集不上盘** |

**⇒ 合起来的完整链条：失败 → 不 exit → 只 notify → notify 是 no-op → 产物里没有任何痕迹。**
（三视角独立得到同一结论：铁律/0003 §3、简单/0005 §1、效率/0006 §1，证据互补。）

**附带修正**：`rc=0 ⇒ MCP 已加载` 的推论**不成立**——它只证明扩展加载器接受了
`builtin:mcp` 这个名字，**不证明任何 server 连上、任何工具可用**（简单/0005 §1 自撤回该推论）。

---

## 4. 观测裁决：本轮**不做**探针，改为"登记 + 声明"

**报告侧（零成本）**：显式写 **"平台能力：未观测（本档不采）"**，把不可见说出来。

**登记（非行动项，写入 `docs/design.md` 决策 20 的"盲区/既定路径"段）**——出现真正消费者时按此实现：

```
触发条件：用户报"agents 没搜到" / 上游给出工具注册的可读信号（例：pi mcp list 之外的可读出口）
做法：pi mcp list --json   一次/场：--start 采集 → 落盘分析目录 → --report 读文件
      （探针在组合层/start 层，不进 observability——保持"零插桩事后推导"）
代价：常态 +2.8s（实测 2.72–2.80s，n=3）；最坏 +T（我们侧超时，T ≤ 10s）→ 记"未验证"
取值三态：ok / 异常（state≠connected，点名 server）/ 未验证 —— 禁写"不可用"
护栏：
  1 不进 observability（报告必须是纯事后推导，同目录两次报告一致）
  2 超时 + fail-open（超时/命令缺失 → "未验证"，绝不阻塞或失败 run）
  3 三态写死，禁写"不可用"（我们只知道"此刻不可达"，不知道"当时不可用"）
  4 文档写明只覆盖持续性失效（凭据过期/端点下线），不覆盖"启动正常、中途断"的瞬断
  5 可注入/测试可短路（否则默认单测会真的连三个远端 server：见 §6 计数）
  6 采样必须以 cwd = fork_cwd（agents 的 spawn cwd）启动、继承 PI_CODING_AGENT_DIR
边界依据：main.ts:585 process.cwd() → :610 runMcpCommand({cwd}) → cli.ts:190/196-197 读
      <cwd>/.pi/mcp.json + trust；而 meeting_loop.py:393 spawn_cwd = fork_cwd or workdir
      ⇒ 在仓库根采样，若被分析项目有 .pi/mcp.json，采样集 ≠ agents 所见（把近似当事实）
两条已被证伪的错路（别再捡）：
  ① 成功路径 stderr —— notify 是 no-op，什么都没写
  ② session 文件 —— 工具集不上盘
```

**不做探针的理由（三方一致）**：无现实消费者（不会据此重试/中止/换档）+ 收益频率无数据
（`mcp.log` 不存在，无失败率口径）+ 会把整批从"**0 次 LLM 验证**"变成"需真跑 e2e 验证（20–40 min）"。

**平台状态查询能力（已核实，供将来实现）**：`pi mcp list [--json]` 是文档化子命令
（`extensions/mcp/cli.ts:37/70`），JSON 形如 `{servers:[…], errors:[…]}`（`:463-470`），
每项带 `state` / `tools` / `error`；退出码语义 `:461`（有 config error 或任一 enabled server
`state !== "connected"` ⇒ 非零）。**属"文档化结构化接口"，可作机器判据**（见 §9 三档边界）。

---

## 5. 两处"死代码"改判为**留**（本场主要的自我纠错成果）

### 5.1 `else: raise`（`meeting_loop.py:361-366`）——留代码，只改注释

- **原判**（简单/0001 §2）：与 `:326` 的守卫重复 ⇒ 不可达 ⇒ 删。
- **改判**：两处拦的是**两种不同失效模式**——`:326` 拦**元组之外**的值（用户传错），
  `:361` 拦**元组之内、但无分支**的值（开发者加值忘加分支；此时 `:326` **不响**）。
- **删掉的代价**：新策略值会静默落进 `all` 档 = pi 默认发现 = 载入 AFT/MC 全档
  ⇒ **分钟×N 级**、且无信号（成本不对称：保留 0 成本 vs 删除后最坏分钟×N）。
- **要改的**：`meeting_loop.py:362-363` 现注释"当前守卫下不可达"与同句"值域增长时这里响"
  自相矛盾——改为：
  > 当 `EXTENSION_POLICIES` 增长而分派未同步加分支时在此响（元组**内**无分支 ≠ 元组**外**的值：
  > 后者由 `:326` 的守卫拦）；当前元组下不可达，**不是死代码**。

### 5.2 `or reason`（`observability._report_extension_line`）——留分支，改测试论证 + 写移除条件

- **原判**（简单/0001 §3）：现行 writer 下 `reason ⇒ d≠e`，故 `or reason` 永不改变结果 ⇒ 死判定；单测还"冻住"了它。
- **改判**：**reader/writer 版本结构性解耦**——
  - 快照**只含 4 个模块**（`start_discussion.py:429-430`：meeting_loop/fs/core/engine），
    **不含 observability**；而 `--report` 走**主仓**代码（`mv_cli.py:36,287-289` → `start_discussion.py:582`）；
  - ⇒ loop 日志由"分析启动那一刻的快照"写，报告由"今天的代码"读；
  - `af9ee53^:361-362` 真的产出过 `声明=mc-tools 生效=mc-tools 降级原因=部分：…`（**d==e 且 reason 非空**）。
- **要求**：① 测试 docstring 改**真实理由**（跨版本回落，源行 `git show af9ee53^:meeting_loop.py:361-362`）；
  ② 补**移除条件**："当不再存在 af9ee53 之前启动、且仍需 `--report` 的分析目录时可删"。

---

## 6. 必须修：一处**无据断言**（5 个副本同批改）

`meeting_loop.py:356` 的 `downgrade_reason = f"{err}；内置 MCP 工具不受影响"` —— 在 §3 已证
"工具是否可用不可观测"的前提下，这是**断言了未知**（"多余的文字一旦断言了未知，就从啰嗦升级为错误"）。

**落点取"删"而非"改"**（理由：改写会在 5 处**再复述一遍**"入口"这个已在
`meeting_loop.py:343`、`meeting_fs.py:870/878` 单点声明过的事实；删则净减文字且假保证从仓库消失）：

| # | 位置 | 动作 |
|---|---|---|
| 1 | `meeting_loop.py:356` | `downgrade_reason = err`（删半句） |
| 2 | `tests/test_main_paths.py:686` | fixture 文本同步删 |
| 3 | `tests/test_meeting_loop.py:862` | 断言同步删 |
| 4 | `tests/test_meeting_loop.py:798-804`（docstring） | "降级不牵连内置 MCP"改述为入口层或删 |
| 5 | `docs/design.md:578-579`（半句） | 删，保留"原因字段=入口层失败原因" |

---

## 7. A 层：有墙钟成本的三（+1）项 —— **只文档化**，无干净注入点

| 项 | 数字/依据 | 口径 |
|---|---|---|
| ① 连接帽 `startupWaitMs` 10s × N | `index.ts:79`（默认 10000）+ `:833-847`（`before_agent_start` 等一次）+ `:177/796/835`（`waitedForStartup` 是 **per-session**，而我们**一唤一进程**）⇒ 每唤都等一次；某 server 慢/挂 → 最坏 **+350s/场（22min 场的 +27%）** | 上界、非均值 |
| ② `exposure=direct` 的 token | **差值 +1,025 tok/请求**（agent 侧实测，n=2，5 工具全 direct） | 配置一变（server 数或 exposure）**作废** |
| ③ 全局 `mcp.json` 共享税 | 主 pi 每次请求同量级 **+~1k（机制外推、未测）** | **外推，不是测量** |
| ④ `--approve` ⇒ 被分析项目的 `.pi/mcp.json` 进 agents 工具面 | ⇒ A 层成本**随被分析项目变化、无上界**；唯一杠杆 `--no-approve` 不可行（会丢项目 AGENTS.md） | 记为"无上界" |

**为什么只能文档化**：项目级 `mcp.json` 只在 `<cwd>/.pi/mcp.json` 且项目被 trust 时读
（`extensions/mcp/config.ts:100-101`），而 agents 的 cwd = **被分析项目**（写进去=污染用户仓库）；
无 env/CLI 覆盖；`startupWaitMs` 是**扩展注册选项**（`index.ts:76`），`config.ts` 无字段，无注入点。

---

## 8. 明确不做 + 一个用户拍板项

**不做**：报告新增 MCP 位/字段；启动期校验；import 期断言（要么**替换** `else`、要么不做，
手写"已处理集合"= 第二份会漂的抄本）；任何"每唤执行"的检查；`pi mcp list` 探针（**本轮**，见 §4 登记）。

**用户拍板项（不由分析代拍）**：是否做配置层改动（`enabled:false` 子集化 / per-server `exposure`）。
- 收益 ≈ **$0.003/场**（≈28.7k tok）；
- 代价落在**主 pi**：`mcp.json` 全局共享，`exposure` 不 gate 连接（`index.ts:805` 是 `servers.filter(isEnabled)`）
  ⇒ 混合 exposure 只省 token，`enabled:false` 才同时降 token 与失效面；
- **实测**（效率，方向性）：最近 12 个主 pi session / 13,144 次 toolCall，其中 `mcp` 网关 **51 次**，
  参数里 web-reader **12** / web-search-prime **9** / zread **5** ⇒ **三个 server 主 pi 都在用**
  （方法限制：子串匹配、含 describe 类调用，只作方向性证据）。
- ⇒ 三方均**撤回**原建议（效率撤回混合 exposure，简单撤回 `enabled:false`），默认**不动配置**；
  若用户明确"这两个我在主 pi 不用"，则按简单版 `enabled:false` 做（少一种机制）。

---

## 9. 纪律产出（可复用，建议进相应文档）

1. **"落盘可以，解析不行"——细化为三档**：
   | 档 | 对象 | 规则 |
   |---|---|---|
   | ① | 上游**文档化结构化接口**（`pi mcp list --json` 字段、session 条目） | **可作机器判据**；字段缺失/形状变 → **"未知"**，不写"不可用" |
   | ② | 上游**人类 prose**（stderr、notify 文案） | **只落盘/留痕**，不做分支 |
   | ③ | 上游**内部布局**（第三方包 `dist/*.js`） | **不碰**（本次已删） |
   （本仓已有合规先例：`observability.py:405-438` 解析 session 的 `thinking_level_change` 得"生效档位"。）
2. **检测器不得继承被检测对象的失效面**（效率/0010 §2；继承不可避免 ⇒ 关键是**上界 + fail-open**，
   不是"消除继承"）：`pi mcp list` 要连那三台 server，若某台挂住，探针自己也会挂住 ⇒ 必须我们侧套 `T ≤ 10s`。
3. **多余的文字一旦断言未知，就从"啰嗦"升级为"错误"**（简单/0005 §3，直接产出 §6 的修复）。
4. **无证据的改动建议，不比无证据的断言干净**（简单/0006 §1 自陈）。
5. **数字必须带口径**：本场统一为——写**差值**（+1,025）不写绝对值（105k 随 fork 源大小漂）；
   标明 n、配置版本、人口（哪些 session）、以及**机制外推 vs 实测**。
6. **三条不变式落字**（否则会被下一个人当死代码删掉）：① 两处守卫各拦一种失效模式；
   ② observability 不在快照、`--report` 永远用主仓版本；③ **非交互模式下 MCP server 状态不被报告 ⇒ rc=0 ≠ 工具可用**。

---

## 10. 落地方式与验证（**0 次真实 LLM 运行**）

- **合并成一次改动 + 一次全量验证**（不要分成多个周期）：
  ① `:362-363` 注释；② `or reason` 测试 docstring + 移除条件；③ 三条不变式落字；
  ④ 前提声明"pi ≥ 0.99"；⑤ §6 的 5 个副本删半句；⑥ token 数的口径（差值/人口/机制）；
  ⑦ `rc≠0` 时截断落 `stderr`（**只落不解析**、**归"通用诊断"**、**带截断口径**，
  先例 `meeting_fs.py:356` 的 `[:200]`）；⑧ `tests/test_meeting_loop.py:737` 的 `0–2` → `1–2`（stale）；
  ⑨ 报告写"平台能力：未观测"。
- **验证手段 = 现有测试套件**（492 py + 54 harness，分钟级、0 LLM）。
  唯一有行为的是第 ⑦ 条——用**单元测试造 rc≠0 的假进程输出**验证，**不需要 e2e**
  （本场无任何时序敏感的行为改动；按旧习惯"改完跑一场"是 20–40 分钟换不到本场所需证据）。
- **`rc≠0` 落 stderr 的归类要求**：必须在文档里归到"通用诊断"，**不得**列在"MCP 可见性"名下——
  §3 已证它 0 覆盖 per-server 静默；归类错了等于用一个 0 覆盖的机制去结一笔未结的账。

---

## 11. 过程诚实记录（撤回与更正）

| 谁 | 撤回/更正 | 原因 |
|---|---|---|
| 铁律 | 撤回"给 `builtin:mcp` 常量加能力位校验"（F2(a)）；撤回 F1 的"版本静默失效"分支；撤回自己提议的 LLM 探针 | 所有权分层（入口路径归我们→解析；名字归 pi→引用不校验；server 状态归 pi 运行时→查询）；源码链已证 loud+fatal；源码读比探针便宜且确定 |
| 简单 | 撤回 `0001` §2（`else` 不可达）、§3（`or reason` 死判定）、`0004` §5/`0005` §5（`enabled:false`）、`0004` §4（"唤醒成功 ⇒ MCP 已加载"）、`0004` §1（import 期断言） | 前提错误 + reader/writer 解耦 + 主 pi 实测 + no-op notify |
| 效率 | 撤回 `0003` §1 的覆盖面（`rc≠0` 落 stderr 抓不到 per-server）、撤回 `0001` §4 子集化建议、让出 (A) 探针 | 覆盖面经复核不成立；主 pi 在用三 server；无消费者 + 会把验证成本从分钟级推到 20–40 分钟 |
| 计数更正 | `setup_environment` 的直接执行点 = **7 处**（`tests/test_spec.py:297/566/589/599/635`、`tests/test_flow_composition.py:98`、`tests/test_startup_defaults.py:52`），另 `tests/test_main_paths.py:873` 一处是 **mock** ⇒ 记 **7+1** | 效率/0013 更正，铁律在轮转中在案确认 |

---

## 12. 三方立场清单（谁在什么视角上贡献了什么）

- **效率**：A 层定价（10s×N ≈ +350s/场最坏、+1,025 tok/差值、主 pi 共享税）、
  `pi mcp list --json` 实测价签（2.72–2.80s，n=3）、"每场一次 vs 每唤 = 35 倍差"、
  "检测器继承失效面"、`--approve ⇒ 无上界`、"B 层可 0 LLM 验证 ⇒ 不要 e2e"。
- **简单**：净简化计量（mock 16→9、状态空间收敛）、三处自我撤回与一次接受驳回、
  "三条不变式落字"（含本场核心产出：可读性缺失会被误读成复杂度过剩，进而诱导删除正确代码）、
  "`:356` 半句取删不取改"、护栏 5"可注入是前提"、探针归类 A 层（不入 B 层零成本批）。
- **铁律**：职责分层（入口路径/平台标识/运行时状态三分）、`else` 与 `or reason` 的实物反驳、
  per-server 静默链条的完整取证（`runner.ts:327` no-op）、`pi mcp list` 的 **cwd=fork_cwd 前提**、
  三档边界细化、`:356` 的编辑边界与 5 副本清单、登记文本的护栏合并。

**无未决分歧**。唯一留在用户手里的是 §8 的配置层改动（`enabled:false` / exposure 子集化）。
