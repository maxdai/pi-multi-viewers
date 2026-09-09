#!/usr/bin/env python3
"""meeting_engine.py —— Meeting 模式唯一状态机引擎（v2 重构）。

设计原则（用户确认 2026-08-09：合理是第一原则）：
- 状态机只此一份——fake_agent 与 meeting_loop 通过注入 responder 复用
- 协议信号（freezing/af/pass/concluded）由引擎确定性写，不经过 responder
- responder 只负责"响应一轮"（LLM 唤醒或随机决策），返回是否产出
- 判定函数（all_last_types/aggregate_mode/rr_next_speaker 等）集中于此，不重复实现
- 写 af 判定统一在循环顶部（消掉 mode 分派中的重复）
- max_rr 兜底收尾由 resultWriter 写（与 RR 经验一致，非 starter）

responder 接口：
    responder(workdir, agent, head, meta, is_first, rr_turn, retry,
              finalizing=False, finalize_reason=None) -> bool
    - rr_turn=True  → RR 轮次（pass，无异议——单向流）
    - rr_turn=False → meeting（message / freezing）
    - retry=True    → 上一轮无产出，强制表态
    - finalizing=True → 收尾：写 result.md（收尾指令，双面约束）
    - finalize_reason → 收尾原因（consensus/quota/stall）
    - 返回值被忽略（审核#12 死契约）：产出判定靠 _produced（消息文件数）
      ——responder 不需要返回 bool，返回 True/False 皆可
"""

import json
import os
import random
import re
import time

from meeting_fs import (
    git_head, git_pull, git_commit, git_push,
    list_my_messages, new_messages_with_meta, next_msg_id,
    read_point, read_message, write_message, commit_message, setup_commit,
    run_git, parse_frontmatter, serialize_message,
    git_show, is_message_file, parse_log_nameonly,
)
from meeting_core import (
    should_write_af, can_start_rr, validate_and_fix, is_all_last_in,
    aggregate_mode as core_aggregate_mode,
    has_new_messages_for_me,
)
POLL_INTERVAL = 2.0
JITTER = 0.3
MAX_RETRY = 3


def log(agent, msg):
    print(f"[{time.strftime('%H:%M:%S')}] {agent}: {msg}", flush=True)


# ---------------------------------------------------------------
# 判定辅助（集中于此，fake/loop 不重复实现）
# ---------------------------------------------------------------

def participants(workdir):
    """参与者列表（order）。"""
    proto = json.load(open(os.path.join(workdir, "protocol.json")))
    return list(proto.get("participants", []))


def result_writer(workdir):
    """resultWriter（收尾写者）。"""
    proto = json.load(open(os.path.join(workdir, "protocol.json")))
    return proto.get("resultWriter", participants(workdir)[-1])


def _each_agent_messages(bare, agents):
    """bare 树中每个 agent 的完整消息列表（frontmatter dict，按序号升序）。

    所有 bare 读取的单一入口（审核 D：不搞两套读取——_each_agent_last
    由本函数派生）。供 core 判定使用（设计 10.2 状态机只此一份：
    引擎只做 bare 组装，判定归 core）。
    批量读（review5 M3）：git cat-file --batch 一次进程读全部，
    不用每消息一次 git_show（O(n) subprocess）。
    """
    r = run_git(bare, "ls-tree", "-r", "--name-only", "HEAD", check=False)
    files = [l.strip() for l in r.stdout.strip().splitlines() if l.strip()]
    msg_files = [f for f in files if is_message_file(f)]
    if not msg_files:
        return {a: [] for a in agents}
    # 批量读内容（git cat-file --batch：一次进程读全部消息文件）
    contents = _cat_batch(bare, msg_files)
    by_agent = {}
    for f in msg_files:
        ag, name = f.split("/")
        by_agent.setdefault(ag, []).append((name, f))
    result = {}
    for a in agents:
        entries = sorted(by_agent.get(a, []))
        msgs = []
        for name, path in entries:
            c = contents.get(path)
            if c is None:
                continue
            fm = parse_frontmatter(c)
            if fm:
                msgs.append(fm)
        result[a] = msgs
    return result


def _cat_batch(bare, paths):
    """git cat-file --batch 批量读文件内容（review5 M3）。

    用 subprocess 管道：输入 rev:path 列表，输出为 "<sha> <type> <size>\n<content>\n"
    块序列。HEAD 消息文件用 HEAD:path 语法（--batch 支持 rev:path）。
    返回 {path: content}。

    **必须二进制模式读**（用户 9343 现场修复）：cat-file 的 size 是**字节数**，
    text 模式 read(size) 读**字符数**——中文 UTF-8 3 字节/字符 → 错位 →
    后续 header 全乱 → readline 阻塞等数据 → 挂起死锁（真实 LLM 讨论中文，
    FakeAgent 测试 ASCII 单字节所以本地测试没抓到——R1 根因复发）。
    异常安全：try/finally 保证 stdin.close() + wait()（异常不泄漏进程，
    否则 cat-file 常驻等 stdin EOF——top 3 个常驻进程即死锁现场）。
    """
    if not paths:
        return {}
    import subprocess as sp
    proc = sp.Popen(
        ["git", "-C", bare, "cat-file", "--batch"],
        stdin=sp.PIPE, stdout=sp.PIPE, stderr=sp.DEVNULL)
    result = {}
    try:
        for p in paths:
            proc.stdin.write(f"HEAD:{p}\n".encode())
            proc.stdin.flush()
            header = proc.stdout.readline()   # 二进制：按字节读行
            if not header:
                break
            header = header.decode("utf-8", "replace").strip()
            parts = header.split()
            # header 格式："<sha> <type> <size>" 或 "<rev> missing"
            if len(parts) != 3 or parts[1] == "missing":
                continue
            try:
                size = int(parts[2])
            except ValueError:
                continue
            content = proc.stdout.read(size)   # 二进制：按字节读 content
            proc.stdout.readline()             # 消费块尾换行
            result[p] = content.decode("utf-8", "replace")
    finally:
        proc.stdin.close()
        proc.wait()
    return result


def _each_agent_last(bare, agents):
    """bare 树中每个 agent 最后一条消息的 {type, mode, next}。

    由 _each_agent_messages（唯一 bare 读取）派生：取每 agent 列表末尾。
    这是状态判定的数据源（设计 11.8）。
    """
    messages = _each_agent_messages(bare, agents)
    result = {}
    for a in agents:
        msgs = messages.get(a, [])
        if not msgs:
            result[a] = None
            continue
        fm = msgs[-1]
        result[a] = {
            "type": fm.get("type"),
            "mode": fm.get("mode"),
            "next": fm.get("next"),
        }
    return result


def aggregate_mode(bare, agents):
    """全局模式（审核#6：判定下沉 core，引擎只传数据）。

    仅 stall 分支用（pull 后需要新鲜数据重检，L-M2）；循环内模式判定
    用循环顶组装的 messages 派生。
    """
    return core_aggregate_mode(_each_agent_last(bare, agents))


def rr_next_speaker(bare, agents):
    """RR 阶段轮到谁 = 最近的参与者消息的 next。

    RR 串行不变量（设计 11.8）：RR 阶段同一时刻只有一个 agent 写 +
    push（next 链唯一确定下一位）→ 最后发言者 = 最近的一条参与者消息
    （helper 设计 3.4：human 插话不算发言者，不参与轮转）。
    故"最近的参与者消息"的 next 就是下一位发言人。

    human 插话（HEAD = human 消息，无 next）会打断轮转链——逐 commit
    往前找最后一条参与者消息（helper 设计 3.4；否则 next 缺失 →
    所有 agent 的 nxt != agent → RR 死锁，只能等 stall 兑底）。

    **等价性约束**（用户 2026-08-30 严格核对）：非 human 场景必须与
    原实现完全等价（git log -1 + 文件列表最后一个消息文件 msg_files[-1]
    ——原语义），仅 human 场景走逐 commit 回退路径。
    """
    # ① HEAD 是参与者消息 → 原实现语义不变（git log -1，取最后一个消息文件）
    r = run_git(bare, "log", "-1", "--name-only", "--format=%H", check=False)
    lines = r.stdout.strip().splitlines()
    files = [l.strip() for l in lines[1:] if l.strip()]
    msg_files = [f for f in files if is_message_file(f)]
    if msg_files and msg_files[-1].split("/")[0] in agents:
        c = git_show(bare, "HEAD", msg_files[-1])
        if c is None:
            return None
        fm = parse_frontmatter(c)
        if not fm:
            return None
        return fm.get("next")

    # ② HEAD 非参与者消息（human 插话）→ 逐 commit 往前找最近的参与者
    # 消息（--skip=1 跳过 HEAD；每组文件仍取最后一个消息文件，对齐①语义）
    r = run_git(bare, "log", "--name-only", "--format=%H", "--skip=1",
                check=False)
    for commit, fls in parse_log_nameonly(r.stdout):
        msg_files = [f for f in fls if is_message_file(f)]
        if not msg_files:
            continue
        last = msg_files[-1]
        if last.split("/")[0] not in agents:
            continue        # 非参与者（human 插话）→ 继续往前找
        c = git_show(bare, commit, last)
        if c is None:
            return None
        fm = parse_frontmatter(c)
        if not fm:
            return None
        return fm.get("next")
    return None


def rr_active_count(messages, agents):
    """RR 阶段轮数：starter（order[0]）的 mode==round-robin 消息数。

    设计 3：max_rr_rounds = RR 轮数，数 starter 的 mode==round-robin
    消息（第一条 pass 本身 + 后续），不从讨论开始数（避免
    meeting 阶段消息混入）。
    messages: _each_agent_messages 的产物（L-M2：循环顶组装一次，派生计数）
    """
    starter = agents[0]
    return sum(1 for fm in messages.get(starter, [])
               if fm.get("mode") == "round-robin")


def _meeting_speak_count(messages, agent):
    """该 agent 已产出的 meeting 内容发言轮（从 bare 重算）。

    数 mode==meeting 且 type==message 的消息（LLM 内容发言）。
    代写 freezing/pass 不计入（设计 11.1）。
    从共享事实（bare）推导——loop 崩溃/重启后不丢配额（审核#1）。
    messages: _each_agent_messages 的产物（L-M2 派生）
    """
    return sum(1 for fm in messages.get(agent, [])
               if fm.get("mode") == "meeting" and fm.get("type") == "message")


def human_msg_count(bare):
    """bare 中 human 插话数（配额增量，helper 设计 §4）。

    human 消息 = human/ 目录下的消息文件（human 不在 participants）。
    从共享事实（bare 树）推导——无状态传递、崩溃安全。
    用途：meeting 配额上限 = max_meeting + human_msg_count（human 每
    发言一次所有 agent 配额 +1，抵消响应消耗——helper 设计 4.1）。
    """
    r = run_git(bare, "ls-tree", "-r", "--name-only", "HEAD", check=False)
    files = [l.strip() for l in r.stdout.strip().splitlines() if l.strip()]
    return sum(1 for f in files
               if f.startswith("human/") and is_message_file(f))


def _produced(workdir, agent, before):
    return len(list_my_messages(workdir, agent)) > before


# ---------------------------------------------------------------
# 协议信号（引擎确定性写，不经过 responder）
# ---------------------------------------------------------------

def write_af_if_no_rr(bare, workdir, agent, agents):
    """写 all-freezing，但 pull 后重检：bare 已有 RR（任一 agent 最后一条
    mode==round-robin）则放弃。

    设计（并发竞态修复）：should_write_af 检查在 pull 前做（基于旧视图），
    pull 后可能发现别人已启动 RR——此时补写 af 会让最新 commit 变回
    all-freezing → 全局 mode 判定回退 → 卡死（实测 5-agent）。
    决定必须基于最新共享事实：pull 后重检，过期决定不执行。

    判定用聚合（_each_agent_last，设计 11.8），不用 git grep 全文：
    grep 不区分 frontmatter/正文且是正则（'-' 为范围符），LLM 讨论
    正文引用该字符串会误匹配 → 死锁（code-review 实测）。
    注意：head 不传（P1）——pull 后内部重取（pull 可能带回新 commit，
    seen_at 必须是最新共享事实）。
    """
    # 写消息前先 pull（同步最新）
    git_pull(workdir)
    # pull 后重取 head（pull 可能带回新 commit，seen_at 必须是最新共享事实）
    head = git_head(workdir)
    # 重检：bare 是否有 RR 已启动（任一 agent 最后一条 mode==round-robin）
    lasts = _each_agent_last(bare, agents)
    if any(v is not None and v.get("mode") == "round-robin" for v in lasts.values()):
        log(agent, "bare 已有 RR 启动——放弃补写 af")
        return
    # 无 RR → 正常写 af
    write_protocol_signal(workdir, agent, "all-freezing", "all-freezing")


def write_protocol_signal(workdir, agent, type_, mode, next_=None):
    """写流程控制消息（freezing/af/pass/concluded）并 commit+push。

    pass：RR 轮次的 loop 代写表态（responder 无法产出时），带 next 轮转链。
    写前同步（review5 F1/L1）：pull 后自取 head——调用方 head 可能是
    pull 前的旧值（F1 双路径不一致）；统一"写前同步"（pull + 重取 head）。

    seen_at 沿用上一个（用户 10030 定案）：loop 自己生成的新消息不能更新
    seen_at（seen_at = LLM 处理过的最新消息位置）；loop 消息的 seen_at
    = 当前读取点（LLM 最后处理点），不推进。
    """
    # 写前同步：pull 带回并发新 commit（F1）
    git_pull(workdir)
    # seen_at = 当前读取点（LLM 最后处理点，loop 消息不推进）——
    # 无历史（agent 从未 responder 成功）→ 兜底到讨论起点（仓库根 commit
    # = setup 提交，大家读到 question.md 的时刻）。不用 head：head 随并发
    # push 漂移，会把 agent 从未读过的消息虚假标记已读（实测 2026-09-03
    # crash_recovery flaky 根因）；起点固定，从起点读一条不漏。
    seen_at = read_point(workdir, agent) or setup_commit(workdir)
    head = git_head(workdir)  # validate_and_fix 用（协议信号正文不含 head 语义）
    msg_id = next_msg_id(workdir, agent)
    fm = {
        "from": agent,
        "type": type_,
        "mode": mode,
        "seen_at": seen_at,
        "summary": f"[loop] {type_}",
    }
    if type_ == "pass":
        # pass：RR 轮次表态（含 starter 启动 RR 的第一条），带 next 轮转链
        fm["mode"] = "round-robin"
        fm["next"] = next_
    # 协议信号路径：放行引擎专用 type（all-freezing/concluded），M1；
    # seen_at 保留（loop 消息沿用上一个，不强制为 head，用户 10030）
    fixed, _ = validate_and_fix(fm, agent, mode, head,
                                allow_protocol_types=True,
                                loop_message=True)
    path = f"{agent}/{msg_id}.md"
    write_message(workdir, path, fixed, f"loop protocol signal: {type_}")
    git_commit(workdir, [path], commit_message(agent, msg_id))
    git_push(workdir)
    log(agent, f"[loop] 写流程控制 {type_} ({path})")


def respond_with_fallback(workdir, agent, responder, head, meta, is_first,
                          rr_turn, before, agents):
    """一次响应轮：responder 写内容文件 → 引擎补全提交 → 查产出。

    职责分离：responder（LLM）只写内容文件；补全字段/commit/push
    由本函数（引擎）调用 commit_new_files 接管。无产出则重试，
    重试仍失败 → loop 代写流程控制（无静默铁律的完整性）。
    """
    mode = "round-robin" if rr_turn else "meeting"
    # ① responder 写内容文件
    responder(workdir, agent, head, meta, is_first, rr_turn, False)
    # ② 引擎补全提交（responder 只写，流程归引擎）
    commit_new_files(workdir, agent, head, mode)
    if _produced(workdir, agent, before):
        return True
    for _ in range(MAX_RETRY):
        log(agent, f"无产出（无静默铁律）——重试")
        responder(workdir, agent, head, meta, is_first, rr_turn, True)
        commit_new_files(workdir, agent, head, mode)
        if _produced(workdir, agent, before):
            return True
    # ③ responder 始终无法产出 → loop 代写（保证流程完整）
    if rr_turn:
        order = agents
        nxt_a = order[(order.index(agent) + 1) % len(order)]
        log(agent, f"responder 无法产出——loop 代写 pass（无静默铁律最后执行）")
        write_protocol_signal(workdir, agent, "pass", "round-robin",
                              nxt_a)
    else:
        log(agent, f"responder 无法产出——loop 代写 freezing")
        write_protocol_signal(workdir, agent, "freezing", "meeting")
    # 代写 = 无产出（设计 11.1：只数 responder 成功产出的轮，
    # 代写不耗配额）——返回 False（审核#1：原先 return True 让 C' 修复失效）
    return False


# ---------------------------------------------------------------
# 唯一状态机
# ---------------------------------------------------------------

def commit_new_files(workdir, agent, head, mode):
    """loop 接管流程：校验/补全 agent 写的内容文件 + commit + push。

    LLM（responder）只写内容文件（最小 frontmatter，可能漏字段），
    loop 在此确定性补全协议字段并提交：
    - from/seen_at/mode：validate_and_fix 修复（mode 无条件覆盖为当前阶段）
    - next：RR 阶段（round-robin）的 pass/message 补轮转链（LLM 不知道顺序）

    mode: 当前阶段（meeting/round-robin）。
    返回值被忽略（调用点用 _produced 判定产出，review4 L1）——
    new_files 非空即 True，即使全部被校验拦截删除。
    """
    agent_dir = os.path.join(workdir, agent)
    if not os.path.isdir(agent_dir):
        return False
    files = sorted(os.listdir(agent_dir))
    new_files = [f for f in files
                 if re.match(r"^\d{4}\.md$", f)
                 and not _is_committed(workdir, f"{agent}/{f}")]
    if not new_files:
        return False
    for f in new_files:
        path = os.path.join(agent_dir, f)
        fm, content = read_message(workdir, f"{agent}/{f}")   # IO 归 fs（L16）
        if content is None:
            continue
        if not fm:
            log(agent, f"[fix] {f} 无 frontmatter 或块不完整（parse 返回 None，A1），删除等待重写")
            os.remove(path)
            continue
        fixed, errors = validate_and_fix(fm, agent, mode, head)
        if errors:
            # 语义字段（type）非法——拦截，不提交（坏消息不进 bare，
            # 设计 11.4）+ 删除滞留文件（审核#14：否则孤儿未跟踪文件每轮
            # 重扫白耗重试；删除后序号复用，responder 重写自然接续）
            log(agent, f"[fix] {f} 校验失败: {errors}——删除，等待重写")
            os.remove(path)
            continue
        # RR 阶段（round-robin）的消息必须带 next（轮转链）——
        # next 是轮转顺序（协议状态），LLM 不知道，loop 确定性补写。
        # pass/message 都补（设计 11.6）：单向流下 LLM 正常只写 pass，
        # 但违规写 message 时若不补 next → rr_next_speaker 返回 None
        # → 轮转链断（防御性兜底）
        # review5 A6：next 无条件覆盖为顺序下一位（与 mode 无条件覆盖对称）。
        # 只补缺省会让 LLM 写错的 next 保留 → 轮转顺序混乱。单向流下
        # RR 只有 pass（无异议），next 是轮转顺序（协议状态），LLM 不知道。
        if mode == "round-robin" and fixed.get("type") in ("pass", "message"):
            order = participants(workdir)
            fixed["next"] = order[(order.index(agent) + 1) % len(order)]
        new_content = serialize_message(fixed, content)
        if new_content and new_content != content:
            with open(path, "w") as fh:
                fh.write(new_content)
        git_commit(workdir, [f"{agent}/{f}"],
                   commit_message(agent, f.split(".")[0]))
    git_push(workdir)
    log(agent, f"提交并推送 {len(new_files)} 条新消息")
    return True


def _is_committed(workdir, path):
    """文件是否已提交（tracked）。

    用 git ls-files（tracked = 已提交过），不用 git status --porcelain：
    status 会把"已提交但被 LLM 覆盖修改"的文件显示为 modified → 被当新
    消息重新提交 → 消息不可变（设计 3.1）被破坏（审核 G3）。
    tracked 文件即使 modified 也拒绝提交。
    """
    r = run_git(workdir, "ls-files", "--", path, check=False)
    return path in r.stdout.split()


def _stall_elapsed(bare, agents, last_head, last_head_time, head):
    """无进展累计（秒）——review5 M1 重写，去 %ct 墙钟依赖。

    旧实现用 bare HEAD 的 %ct（commit 时间）与 time.time() 比较——四失真源：
    ① 跨机时钟快 → %ct 未来值 → stall 永不触发（死锁无兜底）；② 跨机时钟
    慢 → 原生 commit %ct 偏旧 → 活跃期误触发；③ 同机 pull --rebase 重放
    更新 %ct → 系统性推迟；④ setup commit 起点 → 创建与启动间隔 >600s
    即空转收尾。

    新语义：HEAD 变化是共享事实（无时钟参与），本地墙钟只测"相邻两轮
    看到的 HEAD 未变的等待累计"。仅当 bare 存在消息文件（讨论真正开始，
    setup commit 不算）后累计。返回 (累计秒, 新的 last_head, 新的起始时间)。
    """
    r = run_git(bare, "ls-tree", "-r", "--name-only", "HEAD", check=False)
    has_msg = any(is_message_file(f) for f in r.stdout.splitlines())
    if not has_msg:
        return 0.0, head, time.time()   # 讨论未开始（setup 阶段）→ 不累计
    if head == last_head:
        return time.time() - last_head_time, last_head, last_head_time
    return 0.0, head, time.time()


def _result_md_valid(result_path):
    """result.md 有效性校验：存在 + 非空 + 有实质内容（>50 字符）。

    LLM 可能写空文件/仅 frontmatter——存在性检查退化为空提交（审核 A2）。
    """
    try:
        if not os.path.exists(result_path):
            return False
        size = os.path.getsize(result_path)
        return size > 50
    except OSError:
        return False


def finalize_discussion(workdir, agent, responder, head, reason="consensus"):
    """收尾：唤醒 resultWriter 写 result.md → 校验 → concluded 同 commit。

    设计（继承主协议 7.4）：全员 pass 判定归引擎，result.md 内容归
    resultWriter 的 LLM（收尾指令）。双面约束：LLM 只在收尾指令时写。
    reason: consensus（全员 pass）/ quota（max_rr 兜底，标注未完全共识）
    """
    log(agent, f"收尾（{reason}）——唤醒写 result.md")
    # ① 唤醒 rw 写 result.md（收尾指令；双面约束：此时才允许写）
    responder(workdir, agent, head, [], False, False, False,
              finalizing=True, finalize_reason=reason)
    # ② 校验 result.md 有效（存在 + 非空）；无则重试（无静默铁律的扩展）
    result_path = os.path.join(workdir, "result.md")
    for _ in range(MAX_RETRY):
        if _result_md_valid(result_path):
            break
        log(agent, "result.md 未生成或无效——重试")
        responder(workdir, agent, head, [], False, False, True,
                  finalizing=True, finalize_reason=reason)
    # ③ 有效则 commit（产物非消息）；重试耗尽仍无效 → loop 兜底代写
    #    （保证收尾必有产物，不静默缺失——审核 A2）
    if _result_md_valid(result_path):
        _commit_result_md(workdir, agent, "discuss: result.md")
    else:
        log(agent, "result.md 重试后仍无效——loop 兜底代写")
        with open(result_path, "w") as fh:
            fh.write("# 讨论结论\n\n（resultWriter 未能生成有效 result.md，"
                     f"由本地循环兜底代写。收尾原因：{reason}）\n")
        _commit_result_md(workdir, agent, "discuss: result.md (loop fallback)")
    write_protocol_signal(workdir, agent, "concluded", "concluded")
    return True


def _commit_result_md(workdir, agent, subject):
    """幂等提交 result.md（审核#1）：已提交无改动 → 跳过。

    git commit 无改动退出码=1 → RuntimeError → 顶层 except → 重进
    finalize → 确定性死循环。分 commit 窗口的幂等保障。
    用 git status --porcelain（不用 git diff --quiet——diff 看不到
    未跟踪新文件，会误判"已提交"导致 result.md 永不 commit）。
    """
    r = run_git(workdir, "status", "--porcelain", "--", "result.md",
                check=False)
    if r.stdout.strip():
        git_commit(workdir, ["result.md"], subject)
    else:
        log(agent, "result.md 无改动——跳过 commit（幂等）")


def agent_loop(workdir, agent, responder, max_meeting=10, max_rr=7,
               poll_interval=POLL_INTERVAL, stall_timeout=600):
    """主状态机（v2）。responder 注入：响应一轮并返回是否产出。

    stall_timeout: 无进展超时兜底（秒，默认 600=10 分钟）。任何 agent
    检测到 bare 无新 commit 超过此时间 → resultWriter 强制收尾
    （离线参与者死锁兜底，主协议 7.6）。
    """
    log(agent, f"meeting engine v2 启动 (meeting配额={max_meeting}, "
               f"rr配额={max_rr})")
    agents = participants(workdir)
    order = agents
    rw = result_writer(workdir)
    # meeting 发言轮数不存 RAM——每轮从 bare 重算（_meeting_speak_count）：
    # loop 崩溃/重启不丢配额（审核#1，状态从共享事实推导）
    # stall 无进展累计：本地墙钟 + HEAD 观测（review5 M1，无 %ct 依赖）
    last_head, last_head_time = None, time.time()

    while True:
        # 讨论被清理（cleanup 删除目录）→ 退出。loop 生命周期跟随其服务
        # 对象，不依赖 cleanup 杀进程（职责边界，用户 2026-08-31 定）。
        # repo.git 是讨论存在的唯一标志（workdir 被删后进程 cwd 仍可用，
        # 但 bare 消失 = 讨论不存在）。
        if not os.path.isdir(os.path.join(os.path.dirname(workdir), "repo.git")):
            log(agent, "讨论目录已移除（cleanup）——退出")
            return
        try:
            git_pull(workdir)
            head = git_head(workdir)
            bare = os.path.join(os.path.dirname(workdir), "repo.git")
            # 循环顶一次全量 bare 读取（L-M2：消除每轮 3-4 次重复 subprocess
            # 重读——同一轮内无写入、数据稳定），其余判定全部派生：
            messages = _each_agent_messages(bare, agents)
            lasts = {a: (messages[a][-1] if messages[a] else None)
                     for a in agents}
            all_last = {a: (lasts[a].get("type") if lasts[a] else None)
                        for a in agents}
            mode = core_aggregate_mode(lasts)

            # ===== ① concluded → 退出 =====
            if mode == "concluded":
                log(agent, "讨论已收尾，退出")
                return

            # ===== ①.5 无进展超时兜底（审核 H/K：离线参与者死锁）=====
            # bare 无新 commit 超过 stall_timeout → rw 强制收尾。覆盖：
            # 离线者 last=None（全员冻结永不成立）、RR turn-holder 掉线、
            # 全员等待无人响应。T 默认 600s（provider API 可能很慢）。
            # 仅 rw 执行收尾（单一写者），其他 agent 只等待。
            stall, last_head, last_head_time = _stall_elapsed(
                bare, agents, last_head, last_head_time, head)
            if stall > stall_timeout:
                log(agent, f"无进展超过 {stall_timeout}s——超时兜底")
                if agent == rw:
                    finalize_discussion(workdir, agent, responder, head,
                                        reason="stall")
                    continue
                # 非 rw：rw 可能离线（崩溃/未启动）——接管收尾（审核#2，
                # 主协议 7.6"任一 agent 可接管"，meeting 收紧成仅 rw 是缺口）。
                # 接管仲裁（review5 A3）：两个非 rw 同时接管 → 各自 finalize
                # → 双写 result.md → rebase 冲突真死锁。仲裁：
                # ① 先 pull 重检 bare 是否已收尾（result.md 已提交 → 让位）
                # ② 写接管声明消息（推进 HEAD → 其他 agent 的 stall 判定被
                #    重置 → 天然唯一接管者；push 冲突由现有容错重试仲裁，
                #    先到者赢）③ finalize。
                git_pull(workdir)
                if aggregate_mode(bare, agents) == "concluded":
                    log(agent, "收尾已完成（他人接管）——让位")
                    continue
                r = run_git(bare, "show", "HEAD:result.md", check=False)
                if r.returncode == 0:
                    log(agent, "result.md 已提交（他人收尾）——让位")
                    continue
                # 写接管声明（freezing——推进 HEAD + 不破坏流程）
                log(agent, "rw 未收尾——声明接管（推进 HEAD 天然唯一接管者）")
                write_protocol_signal(workdir, agent, "freezing",
                                      aggregate_mode(bare, agents) or "meeting")
                finalize_discussion(workdir, agent, responder, head,
                                    reason="stall")
                continue

            # ===== ② 全员冻结 → 写 af（统一判定，穿透 mode 分派）=====
            # 宽松条件：所有最后一条 ∈ {freezing, af}；我已 af 则跳过
            # pull 后重检 bare（写 af 专用函数）：RR 已启动则放弃（并发竞态修复）
            if should_write_af(all_last) and all_last.get(agent) != "all-freezing":
                write_af_if_no_rr(bare, workdir, agent, agents)
                continue

            # ===== ③ all-freezing 阶段：starter 启动 RR =====
            if mode == "all-freezing":
                # starter 严格条件：全员 af → 启动 RR（用户 10084/10089：
                # 唤醒目的 = 有新的文件可提交给 LLM 处理。starter 冻结期间
                # 别人可能发了 message（读取点后），必须唤醒读完再 pass；
                # 无新消息才确定性写 pass）
                if can_start_rr(all_last) and agent == order[0]:
                    rp = read_point(workdir, agent)
                    meta = new_messages_with_meta(workdir, rp, agent)
                    if has_new_messages_for_me(meta, agent):
                        # 有冻结期间的新消息 → 唤醒 LLM 读 + 写 pass
                        log(agent, "start RR——有冻结期间新消息，唤醒 LLM 读取")
                        before = len(list_my_messages(workdir, agent))
                        respond_with_fallback(
                            workdir, agent, responder, head, meta,
                            False, True, before, agents)
                    else:
                        # 无新消息 → 确定性写 pass（启动 RR，带 next 轮转链）
                        nxt_a = order[(order.index(agent) + 1) % len(order)]
                        write_protocol_signal(workdir, agent, "pass",
                                              "round-robin", nxt_a)
                    continue
                time.sleep(poll_interval)
                continue

            # ===== ④ round-robin 阶段（发言锁已解）=====
            if mode == "round-robin":
                # 全员 pass → resultWriter 收尾（写 result.md + concluded）
                # 判定收敛到 core（审核 D：判定只此一份——core 吃完整消息列表，
                # 引擎只做 bare 组装）
                if is_all_last_in(messages, {"pass"}):
                    if agent == rw:
                        finalize_discussion(workdir, agent, responder, head,
                                        reason="consensus")
                        continue
                    time.sleep(poll_interval)
                    continue
                # max_rr 兜底：RR 阶段活跃消息达上限 → resultWriter 强制收尾
                # （未完全共识，result.md 标注配额耗尽）
                if rr_active_count(messages, agents) >= max_rr:
                    if agent == rw:
                        log(agent, f"RR 配额耗尽（{max_rr}）——强制收尾")
                        finalize_discussion(workdir, agent, responder, head,
                                        reason="quota")
                        continue
                    time.sleep(poll_interval)
                    continue
                # next 驱动：轮到我才响应（惰性——只在 RR 分支才查 next）
                nxt = rr_next_speaker(bare, agents)
                if nxt != agent:
                    time.sleep(poll_interval)
                    continue
                # 唤醒目的 = 有新的文件可提交给 LLM 处理（用户 10084）：
                # 轮到我但读取点后无新消息 → 无需唤醒 LLM，确定性写 pass
                # （确认共识，带 next 轮转链）。有新消息 → 唤醒读完再 pass。
                rp = read_point(workdir, agent)
                meta = new_messages_with_meta(workdir, rp, agent)
                if not has_new_messages_for_me(meta, agent):
                    nxt_a = order[(order.index(agent) + 1) % len(order)]
                    write_protocol_signal(workdir, agent, "pass",
                                          "round-robin", nxt_a)
                    continue
                before = len(list_my_messages(workdir, agent))
                respond_with_fallback(workdir, agent, responder, head, meta,
                                      False, True, before, agents)
                continue

            # ===== ⑤ meeting 阶段 =====
            rp = read_point(workdir, agent)
            # is_first = 从未写过任何消息（含 freezing）——不能 read_point==""，
            # 因为 read_point 跳 freezing（冻结不推进已读，Q1 语义）——
            # 首启写 freezing 的 agent 会永远 read_point=="" → 无限首启（实测漏洞）
            is_first = (len(list_my_messages(workdir, agent)) == 0)
            meta = new_messages_with_meta(workdir, rp, agent)

            # ⑤.1 首启（全 starter 语义：question.md 出现 = 全员启动信号）
            if is_first:
                log(agent, "首启——参与讨论")
                before = len(list_my_messages(workdir, agent))
                respond_with_fallback(
                    workdir, agent, responder, head, meta,
                    True, False, before, agents)
                continue

            # ⑤.2 配额耗尽 → 确定性 freezing（review5 M2：提前为独立分支，
            # 在触发判断之前——全员配额尽 + 无新消息时，配额检查在触发之后
            # 永不执行（无人发言→无触发→永不收敛，只靠脆弱 stall）。
            # 配额 = 从 bare 重算（_meeting_speak_count：数 mode==meeting 且
            # type==message 的消息），不存 RAM（审核#1）。
            # 已冻结守卫：配额尽者写第一条 freezing 后不再重复写（否则每轮
            # 重写 freezing，消息膨胀 + push 冲突）——复用 others_frozen 分支
            # 的守卫模式。
            # human 配额增量（helper 设计 §4）：上限 = max_meeting +
            # human_msg_count（human 每发言一次所有 agent 配额 +1，
            # 抵消响应消耗——不加快配额耗尽）。
            quota = max_meeting + human_msg_count(bare)
            if (_meeting_speak_count(messages, agent) >= quota
                    and all_last.get(agent) != "freezing"):
                log(agent, f"meeting 配额耗尽（{quota} 轮，含 human 增量"
                           f"{quota - max_meeting}）——确定性 freezing")
                write_protocol_signal(workdir, agent, "freezing", "meeting")
                continue

            # ⑤.3 触发判断
            #   触发 = 有别人的新消息（正常响应）或 我成为唯一未冻结者（收尾表态）
            #   后者：其他人都冻结了，没人会回应我——发言无意义，确定性 freezing
            #   （设计缺口：meeting 触发原只定义"有新消息"，未覆盖"别人都冻结"）
            others_frozen = all(
                all_last.get(o) in ("freezing", "all-freezing")
                for o in agents if o != agent)
            triggered = has_new_messages_for_me(meta, agent)
            # 唯一未冻结者（无新消息）→ 确定性 freezing（不经 responder）
            if others_frozen and not triggered and all_last.get(agent) != "freezing":
                log(agent, "其他人都已冻结且无新消息——确定性 freezing")
                write_protocol_signal(workdir, agent, "freezing", "meeting")
                continue
            # 无新消息（非唯一未冻结者）→ 等待（L2：化简，先 freezing 后 sleep）
            if not triggered:
                time.sleep(poll_interval + random.uniform(-JITTER, JITTER))
                continue

            # ⑤.4 发言锁：我最后是 freezing → 锁（不发 message，等 RR）
            if all_last.get(agent) == "freezing":
                time.sleep(poll_interval)
                continue

            # ⑤.5 配额内：responder（message/freezing 二选一，无产出重试+代写）
            log(agent, f"触发——新消息 {len(meta)} 条")
            before = len(list_my_messages(workdir, agent))
            respond_with_fallback(
                workdir, agent, responder, head, meta,
                False, False, before, agents)
        except Exception as e:
            # 统一异常边界（审核 A3+G1/E）：responder（wake_llm timeout）
            # 或 git 异常（git_push 重试耗尽）穿透 while True → loop 崩溃
            # → agent 永久失联 → RR next 链阻塞。单次异常不终止讨论：
            # 记 log + 下一轮继续（设计 11.4 的异常路径扩展）。
            log(agent, f"异常（不终止讨论）: {type(e).__name__}: {e}")
            time.sleep(poll_interval)

