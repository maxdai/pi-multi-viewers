<!-- 存档：docs/reviews/2026-09-25-extension-mechanisms-review.md
     来源：一次真实多视角分析的 result.md 原文（未删改，仅加本头与下方说明）。
     分析场次目录已随 cleanup 删除；文中消息编号不可再核验，仅作溯源线索
     （与代码注释引用约定一致：行为以自描述为准）。 -->

# 存档说明

- **主题**：复验 0.8.2 新上线的三处机制（① `--cleanup` 落盘报告 ② `mv.sh --set-viewer`
  ③ 观看命令交付通道）+ 这三处与扩展 harness 的测试覆盖、断言强度
- **场次**：`mv-mv-main-20260925-153113`（3 视角：效率 / 简单 / 铁律；真实 pi 讨论；
  `forkMode=budget`、扩展策略 `mc-tools`（声明=生效）、`maxMeeting=15`；共识收敛，
  墙钟 28m42s / 46 次唤醒 / provider error 38 次）
- **判定**：三处机制**可以收下**（效率账：① <0.3s/场换掉 20–40 分钟重跑取数；
  ② 一次命令调用省掉 LLM 的校验/重试回合；③ ~百 token/场换掉分钟级往返）
- **本场抓到（多数在最新那批代码里，均属防静默失效）**：
  - **①(c) 显示层失败会跳过清理**（最重要）：`cleanup_discussion` 的 print 在管道关闭时抛
    `BrokenPipeError` → 逃逸 → **rmtree 被跳过**、目录残留、rc≠0。三处逃逸点：
    banner print 在 try 外 / except 处理器自身再 print / 写盘失败处理器又 print。
    修法 = `_print_best_effort`（全部 stdout 走它，函数内无裸 `print(`）+ `rmtree` 进
    `finally` ⇒ **rmtree 必达**；唯一例外 = 产物留存真失败（result.md 权威位在待删目录内）
    修后由 `51a4535` 落地，本仓复现脚本前后对照（逃逸+残留 → 无逃逸+已删+报告仍落盘）
  - **② `--set-viewer` 半成功**：集合级校验在**写之后** → viewers/ 里预置坏文件时
    "rc≠0 但新文件已写入"。修法 **B′**（校验前移到读 stdin/写之前，单一组合点
    `spec_gen._viewer_set_errors`）；另 `exists` → `lexists`
  - **③ 通道模型修正**：从"三个出口（缺一不可）"收敛为**四通道角色模型**；并指出
    `custom_message` **随 fork 进入每场分析上下文**（~2 行/场，有界）——此前只说"主 session"
  - 文档描述簇（三处"不持久化"复述只同步了一处）、断言偏弱（"落盘≡打印"未锁）、
    prompt 里 README 三要点的第二处复述、fail-open 宽窄不对称（Exception vs OSError）
- **落地**：`51a4535`（475 python 测试 + 48 harness 断言全绿；净增 ≈11 行生产代码、
  零运行时行为变化）。报告另记录"明确不做"（fork 过滤 / `ctx.mode` 分支 / 符号链接测试矩阵）
  与已知限（widget 仅运行期常驻）

# 0.8.2 复验 · 三方共识结果

参与者：效率 / 简单 / 铁律（3 视角，真实 pi 讨论；`forkMode=budget`、`mc-tools`、`maxMeeting=15`）。
主题：复验 0.8.2 新上线的三处机制（① `--cleanup` 落盘报告 ② `mv.sh --set-viewer` ③ 观看命令交付通道）+ 测试覆盖与断言强度。**只提意见，不改代码、不跑测试**。

## 0. 结论摘要

1. **三处机制方向正确、净收益为正**（效率账：① 用 <0.3s/场换掉 20–40 分钟重跑取数；② 用一次命令调用省掉 LLM 的校验/重试回合；③ 用 ~百 token/场换掉分钟级往返）。
2. ① ② 的实现基本符合设计，但有**一处文档描述缺口簇、一处测试断言弱、一处职责边界含糊**；③ 在讨论**进行中**因真实观察新增了第四个交付通道（`setWidget`，HEAD `0b28256`），审查基准随之推进，结论按版本分段。
3. **本批必须落地**（三方一致，零运行时变化）：
   - ① **显示层失败可跳过清理**的修补（`_print_best_effort` + `rmtree` 必达；唯一例外=产物留存真失败）——本轮最重要行为修复；
   - ① 落盘内容 ≡ 打印内容 的等值断言 + 写失败 fail-open 分支用例 + 三处"不持久化"复述改引用 + 两处新能力文案；
   - ② **B′**：集合校验前移到写之前（单点 `_viewer_set_errors` + `if names:` 守卫），消掉"写成功但 rc≠0"的半成功；
   - ② prompt 删去 README 三要点复述与命名括注；
   - ③ `index.ts:19`/`:96` 的"三个出口"改四通道角色表述；决策 22 收敛为角色表 + 三注记（fork 复制事实、widget「（运行期）」、why-not-appendEntry）；
   - harness **单一日志**（sendMessage 入 `calls`、删 `sent`、失败路径精确 kinds）+ ① 三断言用例——**净删代码、覆盖变强**。
4. **一句话**：三处机制可以收下；本批做的是"让清理必定发生、让校验先于副作用、让文档按实际写"——全部是防静默失效类的修复，生产代码净增 ≈11 行（显示层 ~6 + B′ ~5），其余为断言与文档。

---

## 1. 审查基准与版本口径（一个动态事实，先钉死）

| 项 | 事实 |
|---|---|
| 0.8.2 | tag `v0.8.2` = `81c8820`（内容：`e06bceb` 报告落盘 + `--set-viewer` + 归档脚本；`81c8820` 发版） |
| ③ 的历史 | `sendMessage` 第三出口 ∈ **0.8.1**（`e92eacc`，见 `v0.8.0..v0.8.1`）；④ `setWidget` ∈ **`0b28256`**（讨论进行中落地，未发版） |
| 本场 HEAD | `0b28256`（"观看命令加常驻面板出口"）——③ 按**四通道**审，① ② 与 v0.8.2 一致 |
| 引用口径 | 复验结论**按版本分段**：①② 对 v0.8.2；③ 对 0.8.1（sendMessage）/ `0b28256`（widget）——避免被读成"0.8.2 已复验"而重复检查 |
| 数字校准 | harness **48** 条断言（0b28256 后）；口径见 `docs/design.md:106`（`build_report` 实测 50–150ms/次、×3 出口 <0.3s/场，单点数字非承诺） |
| 行号校准 | setup prompt：命名括注 `:19`、三要点复述 `:33-34`、复核义务 `:55-56`；README 三要点 `:129-133` |

讨论中审查对象发生变化（`0b28256` 在 15:34 落地），**结论绑定 revision**——执行时以当时 HEAD 为准。

## 2. ① `--cleanup` 落盘报告（`meeting_fs.report_path` + fail-open）

### 2.1 复验判定

- **实现正确**：`cleanup_discussion` 只调一次 `build_report`，同一份 `lines` 既打印又落盘（无二次遍历）；落盘在 `shutil.rmtree` 之前；路径声明 `meeting_fs.report_path` 与 `result_path` 同家（各一行，未过度参数化）。`<base>-report.txt` 已在 `.gitignore:8`（`check-ignore` 实测命中）。
- **职责边界**：落盘点选在 cleanup（"cleanup 是唯一清理点"，design.md:202）正确；观测数字从"只在终端出现一次"变为"可复查"。

### 2.2 发现（按类别）

**(a) 文档描述簇（两个方向）**：
- 旧话未删：`observability.py:5`（模块头）与 `:217`（`build_report` docstring）仍写"**不持久化**……视图不占'数字的家'"，`docs/design.md:204` 仍写"报告不自动落固定位（视图不占'家'）"——同一提交只更新了 design.md:167-171（观测面契约），留下三处相反复述。
- 新话未加：`start_discussion.py:436`（cleanup docstring）与扩展收尾弹窗 `index.ts:180-181`（"清理时还会打印一次分析报告"）都未提报告已**落盘**——用户事后只找 result，不知道有 report.txt，① 的"可复查"收益在**发现性**上打折。
- 改法（按仓库纪律"删复述引权威 > 最短准确陈述"）：`build_report` 处改为"函数自身不写文件；唯一落盘点在 `cleanup_discussion`"；design.md:204 补"（cleanup 删除前例外）"；弹窗/docstring 加一句"报告落盘到 `<分析目录>-report.txt`"。

**(b) fail-open 宽窄不对称**：生成段 `except Exception`（:454-459）与写盘段 `except OSError`（:465-470）——统一为两段同捕 `Exception`（对称、免推理；`join` 抛非 `OSError` 会阻断 rmtree 的自述矛盾一并消失）。

**(c) 显示层失败可跳过清理（本场最重要的行为发现，3/3 决定本批修）**：
- 现状三处逃逸点：banner print 在 `try` 之外（:452）；`try` 内 handler 自身 `print`（:459）→ 再次 flush 时重抛；写盘段把 BrokenPipe 当 `OSError` 捕获后**也再 print**（:469-470）。此外 `_preserve_result_md` 的 print（:432）在报告段之前，也在保护之外。
- 结果**随缓冲而定**：整份输出留在缓冲区（管道 ≈8KB）→ 异常只在解释器退出时出现、清理已完成；报告超过缓冲或未缓冲 → 循环中途炸、**rmtree 被跳过**（目录残留 + rc≠0）。
- **修补形态（冻结）**：
  - 新增 `_print_best_effort`；契约 = **`cleanup_discussion` 的全部 stdout 输出都走它**（含早退 `:446` 与 `_preserve_result_md` 的 `:432`）——按"出口"定义、可 `grep` 校验（函数内无裸 `print(`）；
  - 不变量：**`rmtree` 必达，唯一例外 = 产物留存真失败（I/O）**；显示层失败永不算留存失败；
  - `_preserve_result_md(base)` 调用**留在 `try` 之外**——它真失败（`meeting_fs.py:1255` 的 `open(dest,"w")` 不吞）时冒泡、**不删目录**（result.md 权威位置 `<base>/repo.git` 在待删目录内，删了就永久丢失）；
  - `try/finally`（`rmtree` + 末行提示）只从**报告段**起；rmtree 自身失败仍冒泡；清理成功 **rc 0**。

**(d) 测试缺口**：
- 落盘内容 ≡ 打印内容**未断言**（现只 `assertIn("配额：meeting", report)`，tests/test_main_paths.py:319-323；落盘只写前 3 行也能过）——提交信息所称"内容与打印一致"未被锁；
- **写失败 fail-open 分支零覆盖**（`[cleanup] 报告保存失败（不影响清理）`，tests/ 无命中）。

### 2.3 冻结清单（①）

- [ ] `start_discussion.py`：`_print_best_effort` 助手 + 全部 stdout 走它 + `cleanup_discussion` 重排（`try/finally` 只包报告段、`rmtree` 在 `finally`；`_preserve_result_md` 在 try 外；统一捕 `Exception`；清理成功 rc 0）
- [ ] `observability.py:5`/`:217`、`design.md:204`：改为引用落盘点，不复述"不持久化"
- [ ] `start_discussion.py:436` docstring + `extensions/multi-viewers/index.ts:180-181` 弹窗：补"报告落盘 `<分析目录>-report.txt`"
- [ ] 测试：`assertEqual(落盘, 打印)`（print 已 mock，可直接重建）+ 写失败分支用例 + **三断言新用例**（目录已删 + rc 0 + `<base>-report.txt` 存在）

## 3. ② `mv.sh --set-viewer`

### 3.1 复验判定

- **四条保证确由机制保证**（逐条核对，判据复用 `spec_gen` 单一实现）：命名合法（`check_agent_name`，写前）；不覆盖（`os.path.exists` 写前拒绝 + 测试断言原文件未动）；空正文拒绝（且不建目录）；写完校验并回显（`_validate_and_print_viewers`）。
- **prompt 义务已下移**：第 3 步明确"四条由命令保证，不需要你另外检查"；内容经 stdin 传命令（不直接写文件）。LLM 只负责内容——职责分工正确。
- 复杂度：`--viewers` 与 `--set-viewer` 共用同一校验实现（净简化，无第二套规则）；守卫扁平、无 `--force`。

### 3.2 发现与裁定

- **半成功（职责边界含糊）**：`cmd_set_viewer` 先写文件、后调集合级校验；若 viewers/ 里**预先**有坏文件（空文件/非法名），命令 rc≠0 **但新文件已写入**。可复现：`viewers/` 放一个空 `甲.md` → `--set-viewer 乙` → `乙.md` 已写、rc=1。
  **裁定（3/3）：采纳 B′**（最小形态）：
  - 抽 `_viewer_set_errors(names, empty) = validate_participants(names) or (viewer_set_error(names, empty) if empty else None)`——**唯一组合点**（`if empty` 保护：否则建第 2 个视角会被 ≥2 判据误判为错误）；
  - `_validate_and_print_viewers` 改调它（判据组合不再有第二份）；
  - 前置检查放 `exists` 检查后、**读 stdin 前**（错误路径不消费输入），**只留 `if names:` 守卫**（去掉冗余 `os.path.isdir`——`_discover_viewers` 对目录缺失/空/无 md 一律返回 `(None,None,[])`；不守卫则 `validate_participants(None)` 抛 TypeError，**首次建视角即命中**）；
  - 组合用例：预置坏视角 → 断言 **rc≠0 且新文件不存在**；「已写入 `<path>`」仅出现在成功路径。
  - 回退 A（零改动）：保留半成功，但契约行必须写**双义**（"rc≠0 = 未写入，或已写入但目录整体不合规——以「已写入」行区分"），且用一条用例钉住；A/B′ 的断言不可共用。
- **prompt 残留（3/3 同意删）**：
  - `prompts/multi-viewers-setup.md:33-34`：三要点是 README:129-133 的**第二处完整复述**（该 prompt `:30` 已声明 README 为唯一事实源）→ 删复述、留指针（删后零额外读取）；
  - `:19` 命名括注是 `check_agent_name` 的部分抄写（漏 ≤32/human）→ 删；
  - `:55-56` "编辑后跑 `--viewers` 复核"是 LLM 义务（有 prepare/start 硬 gate 兜底）→ 在决策 22 记为**有 backstop 的有意残留**，不新增编辑命令。
- `os.path.exists` → `os.path.lexists`（一字收紧"绝不覆盖"，悬空符号链接是目前唯一破口）；**不**铺符号链接测试矩阵。

## 4. ③ 观看命令交付通道（0.8.1 的 sendMessage + `0b28256` 的 widget）

### 4.1 事实核验

- 投递已实证：`sendMessage` 写入会话成功；**本场三个 fork 源各含恰好 1 条** `customType=multi-viewers` 的 `custom_message`（逐文件解析：481/550/555 条目中各 1）——**说明它进入每场分析各视角的上下文**（fork 源在**首唤**构建，`meeting_loop._prepare_fork_session`），不是"只在主 session"。
- 显示端：pi-web 把 `custom_message` 渲染为**折叠块**（需点击/重载）→ 用户实测"看不见"；`notify` 弹窗关闭即消失；`setWidget` 面板为**一眼可见、不需点击**（pi-web 注释 "Persistent widget panel … Not a popup"）。

### 4.2 角色模型（替代"缺一不可/三个出口"的说法）

| 通道 | 作用域 | 上下文成本 | 必需性/角色 |
|---|---|---|---|
| `setEditorText` 预填 | 仅 TUI | 0 | TUI 便利（能直接跑） |
| `notify` | 全模式 | 0 | 即时反馈（会消失） |
| `pi.sendMessage` | 全模式 | ~100–200 token/回合（主 session）+ 每场 fork 3×~100–200（一次性、有界） | **持久留痕**（折叠；跨重启仍在） |
| `ctx.ui.setWidget` | 全模式（pi-web 已验证） | 0（纯 UI） | **运行期常驻可见** |

### 4.3 冻结清单（③）

- [ ] `extensions/multi-viewers/index.ts:19` 与 **`:96`**（第二处，枚举漏 widget）→ 改四通道角色表述（与决策 22 一致）；
- [ ] 决策 22 的 UI 通道段**收敛为 4 行角色表 + 三注记**：① fork 复制事实（`design.md:589` 现只写"参与 LLM 上下文的一行"，须补"随 fork 进每场分析"）；② widget「**（运行期）**」（fire-and-forget UI 状态、非会话条目，跨重启不恢复）；③ why-not-appendEntry（零上下文留痕档存在但渲染 TUI-only，pi-web 不可见）；
- [ ] 文档可如实写"显示层失败也不阻断清理"（①(c) 修完后）；
- **不做**：fork 过滤（`build_fork_source` 不按 customType 过滤；新增黑名单=构造层获得扩展类型知识，跨层耦合换 ~2 行噪音，不配）；`ctx.mode` 分支（把客户端差异搬进扩展）；"瘦身" sendMessage（收益 <50 token/回合，不配 churn）。

### 4.4 display-only 口径（写准，防未来翻案）

`sendMessage` **无** excludeFromContext 选项（options 仅 `{triggerTurn, deliverAs}`；`excludeFromContext` 属 bashExecution）；**零上下文留痕档存在**——`pi.appendEntry` + `registerEntryRenderer`（"do NOT participate in LLM context"），但渲染器是 TUI 组件、pi-web 无渲染路径 ⇒ **可见 ∩ 零上下文 = 空集**，跨模式成本不可归零、接受现值。widget 的零上下文已覆盖"面板可见"需求。

## 5. 测试与断言（冻结清单）

- **harness 单一日志（第一优先简化）**：mock 的 `sendMessage` 也 push 进 `calls`（kind="sendMessage"）；删 `sent` 数组与两段特判；成功后精确 `eq(kinds(calls), [...])` 一次覆盖四通道；取消/失败路径精确 `eq(kinds(calls), ["notify:error"])`（现失败路径只 `includes("notify:error") && !includes("setEditorText")`，四通道下只挡一个）；content 用**语义包含**（`includes(dir)`/`includes(watch)`），不锁排版；D2 负断言改**正向**（"含 TUI 限定词"）。
- ①：等值断言 + 写失败分支 + 显示层三断言用例（目录已删 + rc 0 + report.txt 存在）。
- ②：组合用例（rc≠0 且文件不存在）；两条 None 路径已由现有首建用例覆盖（`test_creates_and_validates` / `test_creates_viewers_dir_when_missing`），无需新装置。
- 数字：harness 现 **48** 条；本批预计 +4~6 条断言、删 `sent` 相关代码——**净减代码**。

## 6. 文档同步清单

1. **报告出口描述**：`design.md:139` 第 2 项（`--cleanup`）补"打印 + **落盘快照**"，**计数保持三**（比加第四出口少一个概念；`design.md:106`、`meeting_fs.py:57` 不动）。
2. **术语拆分**：观看命令 = 「**四个交付通道**」；报告保留「出口」（全仓"三个出口"5 处属两个机制，改 `:19`/`:96` 时勿误伤报告三处）。
3. 决策 22：角色表 + 三注记（见 4.3）；观测面契约处（:167-171）已更新，保持。
4. `AGENTS.md`：如无引用冲突无需改（版本号唯一事实源 = package.json；本批若发版按"修复+文档"升第三位）。
5. 版本引用按 §1 分段。

## 7. 明确不做 / 已知限 / 回退口径

- **不做**：fork 过滤、`ctx.mode` 分支、符号链接测试矩阵、② 的 C 方案（改为"创建成功即 rc0、集合问题仅提示"——属产品行为变更）、为 prompt 的"复核义务"新增编辑命令。
- **回退口径**：② 若取 A，契约行写双义（§3.2）；①(c) 若本批不修（不推荐，已 3/3 支持修），则须双落点——`cleanup_discussion` 代码注释（主防线）+ 结论一行，且**不得**声称"fail-open 覆盖显示层"。
- **已知限**（记录在案）：widget 仅运行期常驻（pi 重启/reload 后不恢复，持久记录靠折叠的 custom_message）；手动 `mv.sh --cleanup` 或外部删除后，面板会短暂指向已删目录（下次运行同 key 覆盖）；`sendMessage` 内容参与每场 fork 上下文（~2 行/场，预算窗口内，有界）。

## 8. 收敛过程（消息索引）

- **开题（效率/0001、简单/0001、铁律/0001）**：效率给运行开销账（三处净收益为正）+ 两问（report.txt 是否 ignore、display-only 档）；简单给简洁性复验（① 哨兵可省/两文案未同步、② 最干净、③ 头注释三出口矛盾）+ harness 失败路径断言弱于取消路径；铁律按三铁律逐条核（① 三处"不持久化"只同步一处、断言强度、fail-open 宽窄；② 校验在写之后 + prompt 复述；③"缺一不可"强于设计 + fork 下游效应 + 版本归属修正）。
- **交错与澄清**：挂账两项由简单/铁律独立结清（`.gitignore:8` 命中；零上下文档存在但 TUI-only）；效率两次撤回（"viewer 看不到"被 fork 源证据证伪；"零成本前置"被组合复制论证撤回）；简单撤回哨兵建议（BrokenPipe 反例成立）；铁律更正自身（fork 在首唤构建，本场即已进入；0009 的"受保护块从留存开始"按字面会丢产物，由简单/0007 修正、铁律/0015 确认）。
- **关键分歧裁定**：① 显示层项——效率先降级 → 铁律/0007 代码验证（三逃逸点、缓冲而定）→ 效率/0008 收回、支持本批修 → 简单/0006 站修 → **3/3 修**；② 半成功——效率 A→B′、简单 A→B′、铁律 B′，**最终三方一致 B′**（A 回退写双义）；③ 通道——从"三个出口（缺一不可）"收敛为四通道角色模型（保留、不加过滤）。
- **RR 表态**：效率/0015、简单/0011、铁律/0015 均 `pass`，无异议——共识闭合。

## 9. 一句话结论

**三处机制可以收下；本批修复的全部是"防静默失效"类问题——清理必达（唯一例外=产物留存真失败）、校验先于副作用、文档按实际写；生产代码净增 ≈11 行、断言净增 ≈4~6 条（并删 `sent` 相关代码），运行时零变化。**
