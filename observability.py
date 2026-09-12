"""观测层——状态判定 / --report / --wait（从 start_discussion 拆出，S2）。

职责：**运行期只读观测**——check_status 状态机、--report 观测面聚合、
--wait 阻塞观察、loop 进程存活检测。不写任何产物（报告不落盘——
观测面契约：冷路径一次性，不持久化）。

依赖方向：只准 import meeting_core / meeting_fs / meeting_engine /
human_viewer（observability 是"读"侧，human_viewer.incremental 是它
的进展数据源）；不准 import 主文件。
"""

import datetime
import glob
import json
import os
import re
import sys
import time

import human_viewer
import meeting_core
import meeting_engine
import meeting_fs


def _loop_pids(base):
    """本讨论存活的 loop PID 列表。

    **判据 = argv 精确相等，不是命令行文本正则**（2026-09-10 评审 A2）：
    `pgrep -f <正则>` 会匹配到**任何**命令行里含该文本的进程——从 shell
    包装调用时（`bash -c "...pgrep -f 'meeting_loop.py.*<base>'..."`）会命中
    调用者自身，误判"有 loop 存活"。项目已固化该教训（docs/test-methodology.md
    方法 2：方括号技巧或精确 PID），此处用 /proc 的 argv 逐项比较根治：
    只看 argv 里是否有**恰好等于** `os.path.join(base, "meeting_loop.py")`
    的元素——与启动方（Popen cmd 的第一个参数）同一构造。
    附带：/proc 扫描 ≈1.1ms vs pgrep ≈5.9ms（不构成选型理由，理由是判据精度）。

    读不到 /proc（非 Linux/权限）→ 返回空列表（fail-open：与"无 loop"同义，
    只影响状态显示，不影响流程——loop 自身不依赖此函数）。
    """
    target = os.path.join(base, "meeting_loop.py")
    pids = []
    for entry in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(entry, "rb") as f:
                argv = f.read().decode("utf-8", "replace").split("\0")
        except OSError:
            continue
        if target in argv:
            pids.append(entry.split("/")[2])
    return pids


def _loops_alive(base):
    """讨论的 loop 进程是否存活（argv 精确匹配，见 _loop_pids）。"""
    return bool(_loop_pids(base))


def find_current_dir(cwd=None, sid=None):
    """发现"本 session 当前的分析目录"——**单一实现**（wrapper 与 extension
    共用；此前只有 extension 里一份 TS 实现，wrapper 侧完全没有）。

    为什么需要：消费命令（--status/--cleanup/--view/--say…）此前都要求调用方
    传绝对路径，而唯一知道路径的是 `--start` 的输出——主 pi 得把它记在
    LLM 上下文里再原样复用（改错/截断/相对路径都出过）。发现逻辑下沉后，
    命令可以不带目录，**路径不需要经过任何 LLM 记忆**。

    **只做精确匹配**（`mv-<sid>-*` 中时间戳最新者）：

    为什么**没有**"取项目下最新 `mv-*`"的兜底（e2e16 评审 2:0:1 裁定删除）：
    1. **破坏性操作不应由工具猜目标**——`--cleanup`/`--say` 作用于发现结果，
       兜底意味着"猜一个目录然后删它/写它"；最坏失败应是响亮报错而非静默
       操作错对象；
    2. 兜底需要机器可判的"这是降级"信号，而 rc 0 + stderr 中文文案不是可靠
       信号（extension 曾用 `err.includes("警告")` 还原——文案一改就静默失效）；
    3. 兜底的"最新"按整名排序（含 sid 段）→ 跨 sid 时**系统性取旧**（与
       "取最新"的语义相反）；
    4. **不存在"必须无目录且必然无 sid"的设计内场景**：pi 内两条通道都有 sid
       （bash 注入 `PI_SESSION_ID` / extension `ctx.sessionManager`），终端主
       路径本就用 `--start` 输出的显式目录。无 sid 时应当报错请调用方显式传目录。

    判别 = 目录含 `repo.git`（同 engine："bare 是讨论存在的唯一标志"——光看
    名字会命中残留/无关目录）；`mv-spec-*` 不在匹配前缀内（那是尚未被
    `--start` 消费的 spec 目录）。

    返回绝对路径 | None。
    """
    cwd = cwd or os.getcwd()
    sid = sid if sid is not None else os.environ.get("PI_SESSION_ID", "")
    if not sid:
        return None
    try:
        names = sorted(os.listdir(cwd))
    except OSError:
        return None
    # 同 sid 的目录名尾缀 `YYYYMMDD-HHMMSS` 定长可比 → 排序即时间序
    cands = [n for n in names
             if n.startswith(f"mv-{sid}-")
             and os.path.isdir(os.path.join(cwd, n, "repo.git"))]
    return os.path.join(cwd, cands[-1]) if cands else None


def check_status(base):
    """讨论状态（单值；状态全集显式于此，T3/#7 修复 e2e7 评审）：

      not-exists   无 bare（目录不存在/未创建）
      done         result.md + concluded（权威收尾完成）
      running      有 loop 存活（讨论中 / 收尾中——收尾中细分见下）
      stalled      有 result.md 无 concluded 且 **loop 均不存活**
                   （收尾中断：rw 崩溃在 result.md 之后、concluded 之前）
      stopped      无 result.md 且 loop 不存活（未启动/中断）

    修复动因（e2e7 评审 T3）：原实现"有 result.md 无 concluded"恒返回
    running 且不看存活 → "收尾进行中"与"收尾间隙崩溃"不可区分，--wait
    无限轮询（无终止上界）。现 stalled 使 --wait 有界退出。
    移除恒 None 第二返回值（#7 装饰性契约）——信息由状态本身表达。
    """
    bare = meeting_fs.bare_of_base(base)
    if not os.path.isdir(bare):
        return "not-exists"
    # 读路径统一走 fs.run_git（quotepath 加固单点；run_cmd 只做一次性
    # 环境命令——init/clone/config/push）
    # 读路径统一走 fs.run_git（quotepath 加固单点；run_cmd 只做一次性
    # 环境命令——init/clone/config/push）
    agents = meeting_fs.read_protocol(bare).get("participants", [])
    # "收尾完成"判据**单源** = human_viewer.is_finished（concluded 且
    # HEAD:result.md 有效）——与 viewer 的 done 同一判据（§3.5-P5：此前
    # viewer 认 concluded、这里还额外认 result.md 存在，分叉会让 viewer
    # 在产物落盘前先报"已结束"并打印尚不存在的路径）。
    # 不用 git grep 全文：行文本匹配会被 result.md/消息正文里的
    # `type: concluded` 误触发（实测）；human 消息天然排除（aggregate_mode
    # 只按 participants 取末条）。
    if human_viewer.is_finished(bare, agents):
        return "done"
    # 未完成：有 result.md 但未收尾 → 看 loop 存活区分收尾中/收尾中断
    r = meeting_fs.run_git(bare, "log", "--all", "--format=%H", "--",
                           meeting_fs.RESULT_MD, check=False)
    if r.stdout.strip():
        return "running" if _loops_alive(base) else "stalled"
    return "running" if _loops_alive(base) else "stopped"


def wait_for_completion(base):
    """`--wait`：阻塞展示进展直到收尾或终态。返回退出码（0 完成 / 1 终止）。

    从 main 内联抽出（main 里最大单块；项目方法论把 main/CLI 分发
    列为独立测试盲区）。**纯结构变换**：调用序列与 sleep 序列逐字
    不变——helper 只做机械动作（状态判定 → 打印 → 增量展示 → sleep），
    不吸收"何时进入分支"的阶段判断。

    终止语义（四种终态，各有明确文案）：stalled / not-exists /
    stopped（按 loop-*.log 分叉成因）/ done（含固定位 result.md 提示）。
    """
    # T1 收归（e2e7 评审）：进展展示复用 human_viewer.incremental
    # （原内联 65 行自行 git log 全量 + 手工解析 frontmatter——与
    # viewer 两套输出格式、非增量、概念丢失）。incremental 走
    # since..HEAD 增量 + 统一 format_message。
    # （函数内重复 import human_viewer 已删——顶层已有；e2e16 F4 清理项）
    sys.stdout.reconfigure(line_buffering=True)
    print(f"[wait] 等待讨论完成: {base}")
    bare = meeting_fs.bare_of_base(base)
    agents = human_viewer.participants_from_bare(bare) or []
    since = ""   # 首次全量（--wait 一次性观察，无游标持久需求）
    first = True
    while True:
        state = check_status(base)
        if state == "stalled":
            print("[wait] 收尾中断（result.md 已提交、concluded 缺失、"
                  "无 loop 存活）——停止等待；可读 result.md 或 --cleanup")
            return 1
        if state == "not-exists":
            print(f"[wait] 讨论不存在: {base}")
            return 1
        if state == "stopped":
            # 终态：无 result.md 且无 loop 存活。
            #
            # **不用 loop-*.log 存在性分叉成因**（§3.4-P4）：那是拿日志当
            # 判定输入（唯一实例，与"日志零判定输入"不变量冲突），且两个
            # 分支给出的动作此前都不可执行（`--start` 对已存在目录报"请先
            # --cleanup"；裸 `--start <base>` 又因缺 question.md 失败）。
            # 合并为一条动作完整的提示——真正可执行的是 `--skip-setup
            # --start`（环境不完整则先 --cleanup 重建）。
            print(f"[wait] 未在运行且未收尾（{base}：无 loop 存活、无 "
                  "result.md）——查 loop-*.log / status-*.json 判断原因；"
                  "重跑：--skip-setup --start（protocol 缺失则先 --cleanup "
                  "后重建）")
            return 1
        _mode, lines, head, done, _progress = human_viewer.incremental(
            bare, agents, since,
            meeting_fs.read_protocol(bare).get("maxMeetingRounds"))
        if done:
            for line in lines:
                print(line)
                print()
            print("[wait] 讨论完成 ✅")
            # 固定位（与 prompt 收尾指引一致）：resultWriter 的 loop
            # 退出时保存、cleanup 兜底再存一次——调用方无需推 rw 是谁
            print(f"[wait] result.md: {base}-result.md")
            return 0
        for line in lines:
            print(f"[wait] {time.strftime('%H:%M:%S')} 新进展:")
            print(line)
            print()
        since = head or since
        if first:
            first = False
        # 观察刷新节奏（消费端常量——与 loop 的空闲重试节奏有意独立，
        # 见 human_viewer.OBSERVER_POLL_INTERVAL 注释）。原硬编码 10s
        # 会让结束观察延迟最长 10s（e2e13 时间流分析：唯一 >10s 的
        # 非必要等待点）。
        time.sleep(human_viewer.OBSERVER_POLL_INTERVAL)


def build_report(base):
    """只读报告（`--report`）——观测面的**唯一机器消费出口**。

    契约（design.md 观测面契约节）：
    - **冷路径一次性**：不常驻、不被轮询；调用方（人/主 pi）按需触发。
    - **不持久化**：视图不占"数字的家"——数字的家是 bare（判定域）、
      loop log 的登记字段、pi session 的文档化字段；报告只是它们的一次投影。
    - **fail-open**：任何一段读不出（缺目录/缺文件/格式变）→ 该段显示 n/a，
      不报错、不改判定、不阻塞。
    - **跨度分标**：进程跨度（elapsed_ms）≠ per-response 跨度（session
      时间戳差）≠ 墙钟跨度（commit 时间差）——各自标名，不混算。
    - **不得升级为验收 gate**：有效期判断留给人 + result.md（本轮 §4 明确
      不做运行期评分）。

    返回输出行列表（调用方 print）。
    """
    out = []
    out.append(f"[报告] {base}")
    bare = meeting_fs.bare_of_base(base)
    if not os.path.isdir(bare):
        out.append("  分析目录不存在（已 cleanup？）——n/a")
        return out
    agents = meeting_fs.read_protocol(bare).get("participants", [])
    if not agents:
        out.append("  协议不可读（participants 空）——n/a")
        return out
    proto = meeting_fs.read_protocol(bare)

    # ---- 一次读取（消息文件 = 权威口径），供流程/配额/冻结/RR 四段共用 ----
    msgs = meeting_engine.each_agent_messages(bare, agents)
    per_agent = {a: len(msgs.get(a, [])) for a in agents}
    # human 消息不在 participants 里（视而不见原则）——单独数 human/ 目录的
    # 消息文件。**不用 commit subject 统计**：那是自由文本（`discuss: X/NNNN`），
    # 格式一改/手写就静默归零（2026-09-11 实测：构造环境 subject 不同 → "提交 0"
    # 而实际有 3 条消息）；消息文件是判定域的事实，格式由本仓控制。
    r_h = meeting_fs.run_git(bare, "ls-tree", "-r", "-z", "--name-only",
                             "HEAD", check=False)
    human_n = sum(1 for f in r_h.stdout.rstrip("\0").split("\0")
                  if f and f.startswith("human/")
                  and meeting_fs.is_message_file(f))

    # ---- 流程时间线（时间戳来自 commit——消息文件不带墙钟） ----
    r = meeting_fs.run_git(bare, "log", "--reverse", "--format=%ct%x09%s",
                           "HEAD", check=False)
    rows = []
    for line in r.stdout.splitlines():
        if "\t" not in line:
            continue
        ts, subj = line.split("\t", 1)
        rows.append((int(ts), subj))
    if rows:
        span = rows[-1][0] - rows[0][0]
        detail = " / ".join(f"{a} {n}" for a, n in per_agent.items())
        if human_n:      # human 单列明细（它不是参与者），但计入合计
            detail += f" / human {human_n}"
        out.append(f"流程：{len(agents)} agents | 消息 "
                   f"{sum(per_agent.values()) + human_n}"
                   f"（含流程信号；{detail}）| 墙钟跨度 {_dur(span)}"
                   f"（首末 commit 差）")
    # 最长无进展间隔（相邻 commit 间隔的最大值）
    gaps = [(rows[i + 1][0] - rows[i][0], rows[i][0], rows[i + 1][0])
            for i in range(len(rows) - 1)]
    if gaps:
        g, t1, t2 = max(gaps)
        out.append(f"节奏：最长无进展 interval {_dur(g)}"
                   f"（{_hhmm(t1)} → {_hhmm(t2)}，commit 间隔）")

    # ---- 配额与 human 插话（bare 派生，无状态） ----
    # **配额消耗从 frontmatter 统计**（mode==meeting 且 type==message），
    # 不是"该 agent 的消息总数"——上限约束的是 meeting 发言轮次，而一个
    # agent 的消息里还有 freezing/all-freezing/pass/concluded 等流程信号。
    # 两者混算会出现"meeting 6/2"这种超限假象（口径错误，2026-09-11 实测）。
    # msgs 由上方流程段一次读取提供（同一读取派生四段）。
    lasts = {a: (msgs[a][-1] if msgs[a] else None) for a in agents}
    types = {a: (lasts[a].get("type") if lasts[a] else None) for a in agents}
    quota_meeting = proto.get("maxMeetingRounds", 10)
    quota_rr = proto.get("maxRRRounds", 7)
    out.append("配额：meeting " + "、".join(
        f"{a} {meeting_core.meeting_speak_count(msgs, a)}/{quota_meeting}"
        for a in agents)
        + f"（消耗/上限，口径 = mode:meeting 且 type:message）"
        f"| RR 上限 {quota_rr}/agent | human 插话 {human_n} 条"
        "（不占配额；各 agent 上限 +human 条数）")
    frozen = meeting_core.frozen_agents(agents, types)
    not_frozen = [a for a in agents if a not in frozen]
    out.append(f"冻结：{len(frozen)}/{len(agents)} 已冻结"
               + (f"（{'、'.join(frozen)}）" if frozen else "")
               + (f"；未冻结 {'、'.join(not_frozen)}" if not_frozen else ""))
    # aggregate_mode 期望 {agent: {type, mode}}（core 判定入口形态）——
    # 用 `.get` 规范化：消息缺字段（老产物/手工 fixture）时按 None 处理，
    # 不得 KeyError（报告契约：读不出 → 降级，不崩）
    mode_now = meeting_core.aggregate_mode(
        {a: ({"type": fm.get("type"), "mode": fm.get("mode")} if fm else None)
         for a, fm in lasts.items()})
    if mode_now == meeting_core.M_ROUND_ROBIN:
        out.append(f"RR：轮到 "
                   f"{meeting_engine.rr_next_speaker(bare, agents) or '（未定）'}")
    else:
        out.append(f"阶段：{mode_now}")
    # 标题与口径：一次读取派生的三样观测面（配额进度 / 冻结集合 / RR 位置）

    # ---- 进程事实（登记字段；日志的唯一机器消费点） ----
    proc = _report_wake_fields(base)
    out.append("进程（loop log 登记字段）：")
    if not proc:
        out.append("  n/a（无完成行——尚未唤醒或日志缺失）")
    for a in agents:
        d = proc.get(a)
        if not d:
            out.append(f"  {a}: n/a")
            continue
        out.append(f"  {a}: 唤醒 {d['wakes']} 次 | 进程跨度 总 "
                   f"{_dur(d['total_ms'] // 1000)} / 最大 "
                   f"{_dur(d['max_ms'] // 1000)} | rc≠0 {d['fails']} 次")

    # ---- LLM 运行事实（session 文档化字段；流式预过滤，不整文件解析） ----
    out.append("LLM（session 文档化字段）：")
    any_usage = False
    for a in agents:
        m = _report_session_metrics(base, a)
        if not m:
            out.append(f"  {a}: n/a")
            continue
        any_usage = True
        u = m["usage"]
        # usage 合计：reasoning ⊂ output（**不可相加**）——缺席时省略该括注
        # （缺席 ≠ 0），出现时即使为 0 也写出
        rea = ("" if u["reasoning"] is None
               else f"（reasoning {_num(u['reasoning'])}）")
        out.append(f"  {a}: 响应 {m['responses']} 次 | input {_num(u['input'])}"
                   f" | cacheRead {_num(u['cacheRead'])}"
                   f" | output {_num(u['output'])}{rea}")
        # 按 stopReason 原值分组：**键集固定，不随数据增长**——新同类数字
        # 只多一个键。error 那笔账（计数 + 时长）就在这里，这是 provider
        # 失败从"完全不可见"变为"一行可读"的落点。
        # 键 = 原值（含 None）：解释性命名会因"长消息也是 toolUse"立刻过期。
        parts = []
        for k in sorted(m["stop"], key=lambda x: (x is None, x or "")):
            d = m["stop"][k]
            parts.append(f"{k if k is not None else '（无）'} "
                         f"{d['n']}（{_dur(d['sec'])}）")
        out.append(f"      stopReason：{' | '.join(parts)}")
    if not any_usage:
        out.append("  n/a（session 缺失，或无本轮数据——边界条目自 2026-09-11 "
                   "起写入，此前的老分析不适用）")
    # ---- 档位对照：声明值（spec/pi-agent.json）vs 生效值（session） ----
    out.append(_report_levels_line(base, agents))
    out.append("（口径：进程跨度=pi 进程生命周期；输出=prompt 分段合计；"
               "墙钟=commit 时间差——三者不可互替；"
               "stopReason 括注 = **响应跨度合计**（不含工具执行/唤醒间隔）；"
               "消息数含流程信号（freezing/pass/concluded）与 human，"
               "配额只计 meeting 发言）")
    return out


def _report_levels_line(base, agents):
    """档位对照行：**声明值 vs 生效值**（e2e17 评审 §1）。

    为什么需要：声明值（`pi-agent.json.thinking`）从来没人跟生效值对照过——
    探测失败 → 静默取 DEFAULT_THINKING，spec 文件表面完全正常。这一行是那个
    缺口的最小可见性形态：**零新增字段**（两个值都已有家，只是从没被并列读出）、
    **零新增分支**（集合比较）。

    生效值来源 = session 的 `thinking_level_change` 条目（pi 在会话缺该条目时
    写入，位于边界之后）——**本仓只读，不复刻 pi 的解析链**（那是 pi 的配置）。
    fail-open：任一侧读不到 → 显式 n/a（不猜、不写 0）。
    """
    declared, effective = {}, {}
    for a in agents:
        try:
            with open(os.path.join(base, f"work-{a}", "pi-agent.json")) as f:
                v = json.load(f).get("thinking") or ""
            if v:
                declared[a] = v
        except (OSError, ValueError):
            continue
        levels = _report_session_levels(base, a)
        effective[a] = levels
    if not declared and not effective:
        return "档位：n/a（无 pi-agent.json 且无 session 档位条目）"
    d_set = set(declared.values())
    e_set = {x for v in effective.values() for x in v}
    d_txt = "、".join(sorted(d_set)) if d_set else "n/a"
    e_txt = "、".join(sorted(e_set)) if e_set else "n/a"
    if d_set and e_set:
        same = (
            f"✓ 一致" if d_set == e_set
            # 不一致是两个方向的异常：声明了没生效（写错/被覆盖），或生效值
            # 不在声明里（外部改档/会话遗留）——两种都要人看到
            else f"⚠ 不一致（声明 {d_txt} / 生效 {e_txt}）")
    else:
        same = "（一侧 n/a，无法对照）"
    detail = "、".join(f"{a} {v or 'n/a'}"
                       for a, v in ((a, declared.get(a, "")) for a in agents))
    return f"档位：声明 {d_txt}（{detail}）| 生效 {e_txt} | {same}"


def _dur(sec):
    """人类可读时长（口径由调用方在同一行标注——进程跨度/墙钟/间隔）。"""
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


def _hhmm(ts):
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _report_wake_fields(base):
    """解析 loop-*.log 的登记字段（`elapsed_ms` / `rc`）。

    **日志的唯一机器消费点**（观测面契约：日志零判定输入，报告只读已登记
    字段——不解析自由文本、不做启发式猜测）。fail-open：读不到 → 跳过。
    """
    out = {}
    for f in sorted(glob.glob(os.path.join(base, "loop-*.log"))):
        agent = os.path.basename(f)[len("loop-"):-len(".log")]
        d = {"wakes": 0, "total_ms": 0, "max_ms": 0, "fails": 0}
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    m = re.search(r"elapsed_ms=(\d+) rc=(-?\d+)", line)
                    if not m:
                        continue
                    d["wakes"] += 1
                    ms, rc = int(m.group(1)), int(m.group(2))
                    d["total_ms"] += ms
                    d["max_ms"] = max(d["max_ms"], ms)
                    if rc != 0:
                        d["fails"] += 1
        except OSError:
            continue
        if d["wakes"]:
            out[agent] = d
    return out


def _report_session_metrics(base, agent):
    """该 agent **本轮**（边界之后）的 session 运行事实——**单一适配器**。

    字段来源 = pi 的**文档化** session schema（`docs/session-format.md`：
    `usage` / `stopReason` / `timestamp`）——**只读已有家，不新增记录**
    （同一事实两处 = 双写；loop log 的不变量是零判定输入）。

    返回：{responses, usage: {input, cacheRead, output, reasoning},
           stop: {stopReason 原值: {"n": 次数, "sec": 响应跨度合计}}}
    `usage["reasoning"]` = None 表示**从未出现**（缺席 ≠ 0）。

    响应跨度口径：`Δt = ts(本条) − ts(紧邻前一条事件)`——单次遍历顺序读取，
    不需要随机访问。error 类单列（usage 全零）——不并入也不丢弃：
    否则 provider 抖动会被算成"生成变慢"（e2e14 评审）。
    fail-open：文件缺失/字段变 → 返回 {}。
    """
    fp = _agent_session_file(base, agent)
    if not fp:
        return {}
    # **只统计边界之后的条目**（本轮运行事实）——fork 携带的历史条目里也
    # 有大量 assistant+usage，全文件统计会把主 pi 的历史算成本次分析的
    # 消耗（2026-09-11 实测：717 条 fork 历史被算成"本轮 367 次响应 /
    # input 1.2M"）。边界由 append_handoff_turns 写入（显式登记，非推断）。
    r = {"responses": 0,
         "usage": {"input": 0, "cacheRead": 0, "output": 0,
                   "reasoning": None},
         "stop": {}}
    prev_ts = None
    for ev in meeting_fs.iter_after_boundary(fp):
        m = ev.get("message") or {}
        if m.get("role") != "assistant":
            prev_ts = ev.get("timestamp") or prev_ts
            continue
        r["responses"] += 1
        reason = m.get("stopReason")
        dur = _delta_seconds(prev_ts, ev.get("timestamp"))
        d = r["stop"].setdefault(reason, {"n": 0, "sec": 0})
        d["n"] += 1
        if dur is not None:
            d["sec"] += dur
        usage = m.get("usage") or {}
        for k, key in (("input", "input"), ("cacheRead", "cacheRead"),
                       ("output", "output")):
            v = usage.get(k)
            if isinstance(v, int):
                r["usage"][key] += v
        rv = usage.get("reasoning")
        if isinstance(rv, int):
            r["usage"]["reasoning"] = (r["usage"]["reasoning"] or 0) + rv
        prev_ts = ev.get("timestamp") or prev_ts
    if not r["responses"]:
        return {}
    return r


def _delta_seconds(prev_iso, cur_iso):
    """两个 ISO 时间戳的秒差；任一缺失/不可解析 → None（缺席 ≠ 0）。"""
    if not prev_iso or not cur_iso:
        return None
    try:
        a = datetime.datetime.fromisoformat(prev_iso.replace("Z", "+00:00"))
        b = datetime.datetime.fromisoformat(cur_iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    d = (b - a).total_seconds()
    return d if d >= 0 else None


def _agent_session_file(base, agent):
    """该 agent 的 fork 源 session 文件路径（找不到 → ""）。"""
    try:
        with open(os.path.join(base, f"status-{agent}.json")) as f:
            sid = json.load(f).get("sessionID") or ""
    except (OSError, ValueError):
        sid = ""
    if not sid:
        return ""
    fp = os.path.join(base, "pi-sessions", f"fork-src-{sid}.jsonl")
    return fp if os.path.isfile(fp) else ""


def _report_session_levels(base, agent):
    """该 agent 本轮**生效的 thinking 档位**（去重、保序）——session 侧的家。

    pi 在会话缺 `thinking_level_change` 条目时写入它（每次打开会话至多一次），
    且位于我们的边界之后——所以边界过滤后读到的就是"本场生效值"。
    **本仓只读，不复刻 pi 的解析链**（`modelThinkingLevels` →
    `defaultThinkingLevel` 是 pi 的配置，复刻 = 两处实现/必漂移）。
    """
    fp = _agent_session_file(base, agent)
    if not fp:
        return []
    out = []
    for ev in meeting_fs.iter_after_boundary(fp):
        if ev.get("type") == "thinking_level_change":
            lv = ev.get("thinkingLevel")
            if lv and lv not in out:
                out.append(lv)
    return out


def _num(n):
    """人可读数字（k/M 缩写；原值精度对人读报告无意义）。"""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _main(argv=None):
    """观测层 CLI——目前只有一个子命令：`--find-dir`（供 wrapper 与
    extension 在"不带目录"时定位当前分析；逻辑单点，两个调用方不各写一份）。

    输出契约（二元，无中间态——降级语义已删除，e2e16 F1/F2/F7）：
      stdout = 绝对路径（找到时）；stderr = 原因（未找到）；
      退出码 0 = 找到，1 = 未找到（调用方应据此报错请用户显式传目录）。
    """
    import argparse
    ap = argparse.ArgumentParser(description="多视角分析：观测层工具")
    ap.add_argument("--find-dir", action="store_true",
                    help="定位当前分析目录（按 cwd + PI_SESSION_ID）")
    ap.add_argument("--cwd", default=None, help="项目目录（默认 $PWD）")
    ap.add_argument("--sid", default=None,
                    help="session id（默认 $PI_SESSION_ID）")
    args = ap.parse_args(argv)
    if not args.find_dir:
        ap.print_help()
        return 2
    d = find_current_dir(args.cwd, args.sid)
    if not d:
        print("错误: 未找到本 session 的分析目录（cwd 下无 "
              "mv-<PI_SESSION_ID>-* 环境）——请显式传目录参数",
              file=sys.stderr)
        return 1
    print(d)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
