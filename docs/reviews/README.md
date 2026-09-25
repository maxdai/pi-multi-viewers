# 多视角自审存档（docs/reviews/）

本目录保存**本项目用多视角机制审阅自身实现**产出的报告——即"吃自己的狗粮"
（dogfooding）的实证记录。每份报告都是一次真实 pi 讨论的 `result.md` 原文，
未做删改（只加本说明与文件头）。

## 为什么存档

1. **机制有效性的证据**：这些报告不是模拟，是三个视角 agent 真实读代码、交叉引用、
   交锋收敛的产物（视角名随项目演化：早期 性能/可读性/铁律，2026-09-13 起 效率/简单/铁律）——报告里的问题清单确实抓到了
   真实缺陷（见下）。
2. **决策溯源**：修复批次（commit）常引用报告中的编号（如"P0/P1/P2"），
   保留原文才能核对当时的事实与推理。

归档动作的**机械部分**由 `scripts/archive-result.sh` 做（命名、正文逐字复制+字节校验、
存档头骨架、索引行、删仓库根松散副本）——判断部分（主题/问题/commit）仍由人写。
3. **方法论的样本**：报告本身演示了"观察句 vs 机制句"的证据强度要求
   （`docs/test-methodology.md` 方法 16）——其中也有几处机制句在讨论中被
   交叉复核推翻，是活教材。

## 索引

| 文件 | 主题 | 抓到的问题 | 修复 commit |
|---|---|---|---|
| `2026-09-10-e2e10-fork-source-modes-review.md` | fork 源模式描述与实现一致性（`docs/design.md` / README / AGENTS.md vs 代码） | 非法 `forkMode` 无 fail-fast（静默走"边界后全量"混合分支 → 930k tokens 超窗）；wrapper 静默丢弃 `--fork-mode`；文档数字混源无锚；三处"完整上下文"与有损默认矛盾 | `732fdff`（P0/P1/P2）、`fe1955d`（方法论 11–14） |
| `2026-09-10-e2e11-forkmode-guards-review.md` | forkMode 四层守卫与默认值单一源（含文档口径与方法论条目） | compaction 模式产物含**重复** compaction 条目；budget 窗口含 compaction 时 pi replay **静默丢弃前缀（含 preface）**；台账 `dropped` **双重计数**；`FORK_MODES` 与分派**非结构耦合** | `9412e32`（P1–P3 + I1–I5 不变量 + 台账） |
| `2026-09-11-e2e16-mechanization-review.md` | 机制化改动自审（消费命令目录可省略 + 报告机制化） | **`--view` 显式目录被静默丢弃（回归，测试只覆盖无目录形态）**；降级兜底可作用于破坏性操作（`--cleanup` 删猜测目录）+ extension 靠中文文案还原布尔 + 排序取最新在跨 sid 时系统性取旧；报告 O(session) 冷路径需约束成文 | 「机制化 1+2 修复」提交 |
| `2026-09-11-e2e14-observability-review.md` | 本项目可观测性自审（日志系统是否合理 + 如何监控讨论有效性） | 失败/重试不可见（provider error 零记录，实测占墙钟 11%）；耗时不可度量；token 无出口；**`--wait` 用 `glob(loop-*.log)` 兼任判定输入**；viewer 与 check_status 的 done 判据分叉；两份逐字相同的 `log()`；观测面契约在文档零命中 | 见「e2e14 修复」提交（第一批 §3.1–§3.5） |
| `2026-09-11-e2e13-code-review.md` | 全项目代码合理性评审（第二轮，含时间流实测） | `--agents` 非法名在 spec-gen 路径炸出 traceback + 半成品；viewers/agents 校验与列举多实现（隐藏文件静默入场）；wrapper 越界检查第三方扩展配置；`--start` 静默删 spec；session 文件两套查找规则；切换叙事拼 agent 定义正文 | `fe3aa09`（R1–R8） |
| `2026-09-10-e2e12-code-review.md` | 全项目代码合理性评审（性能 / 简单 / 铁律） | `_lock_git` 守卫 **fail-open**（锁态下 git 上溯到主项目仓库）；`pgrep -f` 自匹配；`protocol.json` 读取散落 6 处 / 3 种失败语义（含 `result_writer` 默认值求值缺陷）；`check_status` 用 `git grep` 全文误报 done；engine 直做 fs I/O；两处 git 入口加固不一致 | 见「e2e12 修复」提交（批 A/B/C） |
| `2026-09-12-e2e17-thinking-level-analysis.md` | thinking 档位降到 `high` 对速度与质量的影响（成本模型 `d = f×S`） | 质量**无任何判据**（全仓无落点）；真杠杆是**请求数**（每请求固定成本占 47%）与 provider 失败重试（11–19%）；报告缺按谓词分组字段 | `3c49d90` |
| `2026-09-13-e2e19-scoped-config-review.md` | 审阅「agent 进程作用域配置」（关 AFT 语义搜索） | 键名方向写反（恒写旧名 → 上游移除即静默回吐 57s）；`_strip_jsonc` **改写字符串值**；条件与副作用未文档化 | `c2be72d` |
| `2026-09-13-e2e20-pure-run-review.md` | 审阅 `c2be72d` 的 9 条修复（**该场 agents 零扩展**） | 边界**子串**误判把 fork 历史算进本轮（报告虚高 4–8 倍，也是"Δ 合计 > 墙钟"之谜根因）；单趟 strip 让注释含引号的输入解析失败（功能回归）；Δ 口径 B 归因被证伪 | `5e02a67` |
| `2026-09-13-e2e21-postfix-review.md` | 审阅 `5e02a67`（并验证报告口径修复） | AFT legacy 检测 4 组路径**多一层 `aft/`**（死检查 + 静默假阴性）；子串预筛是对设计的错误陈述；`n`/`sec` 分母不一致导致均值低估 | `5cc0b5e` 之后的批 |
| `2026-09-13-e2e23-time-breakdown-analysis.md` | 一次分析的**时间构成**（哪些必要、哪些可省） | AFT / MC historian 两笔分钟级开销都不在报告里；反对"不等退出就推进"与"新增常驻机制" | `98786d6` + `c46d996` / `9deefac` |
| `2026-09-14-e2e24-extension-policy-review.md` | 扩展策略三档（默认 mc-tools）的实现与证据链 + `ctx_search` 价值评估 | **S1：缺 MC 的降级路径提前 return → 命令被截断（缺 model/print/注入、cwd 错）**；S1a 测试判别力不足；E2 报告缺"声明 vs 生效"；E1 成本口径超出精度；T1–T3 文本矛盾；F5/F6/F7/S2/F9 解析链缺陷；**ctx_search 9 次调用全为问卷诱导、0 次决定性帮助、命中 1 条过期记忆** | `91a1171`（Batch 1+3）、`b9329fb`（Batch 2 删死代码）|
| `2026-09-14-e2e25-doc-drift-review.md` | 文档 vs 代码一致性（文档漂移）+ ctx_search 的**自然使用**观察 | **三处文档仍写"兜底 mv-* 并警告"而代码是无兜底、未匹配报错**（行为语义相反）；README 缺 `--extension-policy`；报告字段列表过期；4 条缺失项；**根因 = 对实现的复述**（治本：引事实源不复制）；自然使用观察：`ctx_search` **0 次**、historian 0 次 | `ba204ef` |
| `2026-09-25-multi-viewers-extension-review.md` | 把 `/multi-viewers` 改成 extension 这次的实现（extension 代码 / CLI 机器标记行契约 / 文档同步） | **P1：`stalled` 被当成 running → 让用户等一个永不到来的收尾**（唯一行为错误）；P2 `[result]` 只在 done 打印；P3 头注释与暂停点自相矛盾；P4 `/root/pi-multi-viewers` 单机死回退；P5 两扩展逐字重复 ≈35 行且已漂移（→ 合并为一单元三命令）；P6 取消提示缺 sid 提醒 + **扩展消费端零仓库内测试**；首用另暴露：主题带引号、观看命令只有预填一个出口 | 本批（合并 + P1–P6 + 首用两项 + harness 进仓库） |
| `2026-09-25-multi-viewers-postfix-review.md` | 复验 0.8.0 的 extension 合并与 P1–P6（含 harness 覆盖审查） | **P1–P6 逐条到位、合并净简化**；新抓 **漏 A：`run_tests.sh --reuse` 的错误成功信号**（harness 失败仍算绿 → 命中旧绿 + exit 0，修法 ②′ 清指纹 + rc==0 才写回）；D2 通知里的不实断言（pi-web 忽略 `setEditorText`）；B1/B2 契约前缀与不可执行出路；7 类现存分支零覆盖 + sid 注入与 percent-encoding 两装置缺口；D1/D3 文档漂移；S1–S3 简化 | `69a415a`（+ `e92eacc` 第三交付出口） |
| `2026-09-25-extension-mechanisms-review.md` | 复验 0.8.2 三处机制（报告落盘 / `--set-viewer` / 观看命令通道）+ 测试覆盖与断言强度 | **① 显示层失败会跳过清理**（BrokenPipe 逃逸 → rmtree 被跳过、目录残留；修法 `_print_best_effort` + rmtree 进 finally ⇒ 清理必达）；**② `--set-viewer` 半成功**（校验在写之后 → rc≠0 但文件已写入；改 B′ 校验前移）；③ 通道模型由「三出口」收敛为四通道角色表，并证实 custom_message 随 fork 进每场上下文；文档三处「不持久化」复述、断言偏弱、prompt 复述、fail-open 宽窄不对称 | `51a4535` |
| `2026-09-25-patch-audit-review.md` | 审阅 0.8.0 → 0.9.0 一周改动是否有补丁堆叠 / 复杂度失配 / 职责边界问题 | **判定：没有补丁堆叠**；真问题是**文档漂移 F1**（两份入口降级语义改了、6 处复述没跟）、**名实不符 F2/S3**（`_viewer_set_errors` 自称唯一组合点+数量≥2，皆不成立）、**契约只有注释 F4**（cleanup 裸 print 禁令 → 本仓首条 AST 结构断言）；顺带：自然使用复测 `ctx_search` 0 次（不可证伪那句指引）、MCP adapter 无收尾尾巴 | `6c024be` |

## 环境口径（读报告时的背景）

- 报告中的路径（如 `/tmp/mv-e2e9/disc4`、`discuss-mv-main-*`）是当时的**现场**，
  按要求已清理；数字（条数 / MB / tokens）随主会话增长漂移，引用时请对照
  `docs/design.md` §二的规模口径（产物侧数字随时间变化，消费侧数字必须带
  唤醒序号）。
- 报告里出现的讨论消息编号（`可读性/0005`、`铁律/0007` 等）指向当时的
  讨论消息文件，讨论目录已删——这些编号**不可再核验**，仅作溯源线索
  （与代码注释引用约定一致：行为以自描述为准）。
