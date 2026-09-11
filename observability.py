"""观测层——状态判定 / --report / --wait（从 start_discussion 拆出，S2）。

职责：**运行期只读观测**——check_status 状态机、--report 观测面聚合、
--wait 阻塞观察、loop 进程存活检测。不写任何产物（报告不落盘——
观测面契约：冷路径一次性，不持久化）。

依赖方向：只准 import meeting_core / meeting_fs / meeting_engine /
human_viewer（observability 是"读"侧，human_viewer.incremental 是它
的进展数据源）；不准 import 主文件。
"""

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
    import human_viewer
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
        u = _report_session_usage(base, a)
        if not u:
            out.append(f"  {a}: n/a")
            continue
        any_usage = True
        out.append(f"  {a}: input {u['input']} | cacheRead "
                   f"{u['cache_read']} | output {u['output']} | 响应 "
                   f"{u['responses']} 次 | error {u['errors']} 次")
    if not any_usage:
        out.append("  n/a（session 缺失，或无本轮数据——边界条目自 2026-09-11 "
                   "起写入，此前的老分析不适用）")
    out.append("（口径：进程跨度=pi 进程生命周期；输出=prompt 分段合计；"
               "墙钟=commit 时间差——三者不可互替）")
    return out


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


def _report_session_usage(base, agent):
    """从该 agent 的 session 文件取 usage（**单一适配器** + 流式预过滤）。

    字段来源 = pi 的**文档化** session schema（`docs/session-format.md`：
    `usage` / `stopReason`）。行级预过滤（`"usage" in line` 才 json.loads）
    ——避免对 MB 级文件整解析（实测 json.loads 3MB ≈27ms，预过滤可省大部分）。
    fail-open：文件缺失/字段变 → 返回 {}。
    """
    try:
        with open(os.path.join(base, f"status-{agent}.json")) as f:
            sid = json.load(f).get("sessionID") or ""
    except (OSError, ValueError):
        sid = ""
    if not sid:
        return {}
    fp = os.path.join(base, "pi-sessions", f"fork-src-{sid}.jsonl")
    if not os.path.isfile(fp):
        return {}
    # **只统计边界之后的条目**（本轮运行事实）——fork 携带的历史条目里也
    # 有大量 assistant+usage，全文件统计会把主 pi 的历史算成本次分析的
    # 消耗（2026-09-11 实测：717 条 fork 历史被算成"本轮 367 次响应 /
    # input 1.2M"）。边界由 append_handoff_turns 写入（显式登记，非推断）。
    u = {"input": 0, "cache_read": 0, "output": 0, "responses": 0, "errors": 0}
    for ev in meeting_fs.iter_after_boundary(fp):
        m = ev.get("message") or {}
        if m.get("role") != "assistant":
            continue
        u["responses"] += 1
        if m.get("stopReason") == "error":
            u["errors"] += 1
        usage = m.get("usage") or {}
        for k, key in (("input", "input"), ("cacheRead", "cache_read"),
                       ("output", "output")):
            v = usage.get(k)
            if isinstance(v, int):
                u[key] += v
    if not u["responses"]:
        return {}
    # 数字格式化（人读）：千分位缩写
    for k in ("input", "cache_read", "output"):
        u[k] = _num(u[k])
    return u


def _num(n):
    """人可读数字（k/M 缩写；原值精度对人读报告无意义）。"""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)
