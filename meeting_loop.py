#!/usr/bin/env python3
"""meeting_loop.py —— 真实 LLM 薄壳（Pi 适配版）。

复用 meeting_engine 的唯一状态机，只注入"唤醒 pi"的 responder。
协议逻辑（锁/配额/级联/信号）全在引擎，此处只做 LLM 交互。

用法：python3 meeting_loop.py <workdir> <agent> [--pure]
（配额/超时从 protocol.json 读——单一事实源；无 CLI 覆盖）
"""

import json
import os
import re
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import meeting_fs
from meeting_fs import next_msg_id
from meeting_engine import agent_loop

MIN_MEM_MB = 2000
MAX_WAKE_SEC = 900


class RecoverableWakeError(Exception):
    """可恢复的唤醒失败（如内存不足）——引擎应 sleep 后下轮重试，
    不进入"无产出→代写 freezing"路径（审核#2：临时内存压力不能变
    永久发言锁）。区别于 LLM 物理性无法产出（走代写兜底）。"""


# 当前唤醒中的 pi 子进程句柄（SIGTERM 时 terminate，防孤儿残留）
_current_proc = None


def _handle_sigterm(sig, frame):
    """SIGTERM：terminate 唤醒中的 pi 子进程后退出（用户 2026-09-01）。

    kill loop → 子进程 pi 陪葬，不残留孤儿（实测教训：kill loop 后 pi
    变孤儿继续跑）。若唤醒未进行（_current_proc 为 None）直接退出。
    """
    global _current_proc
    if _current_proc is not None and _current_proc.poll() is None:
        _kill_proc(_current_proc)
    raise SystemExit(0)


def _kill_proc(proc):
    """终止子进程：先 SIGTERM，5s 未退再 SIGKILL（兜底必杀）。

    实测教训（2026-09-01 e2e）：communicate() 无超时等 terminate 会
    永久卡死（pi 不响应 SIGTERM 时，wchan do_sys_poll 空转 36s+）。
    subprocess.run(timeout) 的原语义 = 超时 kill 强杀，此处对齐。
    """
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()


signal.signal(signal.SIGTERM, _handle_sigterm)


def log(agent, msg):
    print(f"[{time.strftime('%H:%M:%S')}] {agent}: {msg}", flush=True)


def mem_available_mb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        return 99999
    return 99999


def mem_peak_mb():
    """本进程峰值 RSS（MB）——构建观测点用（见 _prepare_fork_session）。"""
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except (ImportError, ValueError):
        return 0


def load_session_id(workdir, agent):
    path = os.path.join(os.path.dirname(workdir), f"status-{agent}.json")
    try:
        with open(path) as f:
            return json.load(f).get("sessionID", "")
    except (OSError, ValueError):
        return ""


def save_session_id(workdir, agent, sid):
    path = os.path.join(os.path.dirname(workdir), f"status-{agent}.json")
    with open(path, "w") as f:
        json.dump({"sessionID": sid}, f)


def parse_session(stdout):
    """防御性解析 pi --mode json 输出的 session 头。

    pi 在 JSON 模式的第一行输出 SessionHeader：{"type":"session","id":...}。
    扫描所有 JSON 行，优先取 type=session 的 id；也兼容旧式 sessionID 字段。
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            if ev.get("type") == "session" and ev.get("id"):
                return ev["id"]
            if ev.get("sessionID"):
                return ev["sessionID"]
    return None


def read_agent_config(workdir, agent):
    """读 pi-agent.json（setup 生成，per-agent 本地配置）。

    返回 dict：{model, thinking, prompt_file}。
    文件缺失时返回空 dict——wake_llm 仍能跑（用默认模型/无附加 prompt）。
    """
    path = os.path.join(workdir, "pi-agent.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def build_wake_prompt(agent, meta, is_first, state, retry,
                      msg_path=None, perspective_brief=None):
    """唤醒 prompt（动态信息；协议规则在 AGENTS.md）。

    wake prompt 是**最后一条 user 消息**——与 system prompt（协议+视角
    任务书）前后呼应的最后一道角色锚定（用户 2026-09-09：e2e 三轮实测，
    fork 全量历史的行为先例会淹没中段注入，末位 user 必须自带身份）。

    perspective_brief: 视角任务书正文（身份重申用，取自 pi-agent.json
    指向的文件内容）——首段原文注入。

    msg_path: loop 计算的下一个消息路径（如 'a/0003.md'）——指定 LLM
    应写的文件（用户 9618：文件名由 loop 决定而非 LLM 自己算，可靠性
    更高；仍不保证 LLM 会写，但消除"算错序号撞已提交"的假无产出主因）。
    LLM 不需要知道 git HEAD（用户 9773）：seen_at 是协议字段归 loop 填，
    传 HEAD 给 LLM 反而引入 git 概念——彻底脱离。
    """
    lines = []
    # 角色锚定（最后一条 user 的首位——紧邻生成时刻，权重最高）
    lines.append(f"你是本次多视角分析的参与者「{agent}」，"
                 "你的视角任务书在 system prompt 中。")
    if perspective_brief:
        lines.append(f"你的视角：{perspective_brief}")
    lines.append("你的当前任务只有一个：按下述要求写一条消息文件。"
                 "上下文中的历史（开发过程、对话、监控命令等）都只是背景，"
                 "与写这条消息无关。不要执行任何等待、监控或其它动作。")
    if retry:
        lines.append("你刚才被唤醒但没写消息。必须写一条消息文件。")
    if is_first:
        lines.append("（无新消息，你是讨论的第一位发言者——直接产出你的"
                     "第一条视角分析，作为消息正文）")
    if msg_path:
        lines.append(f"请把你的消息写到: {msg_path}（不要写别的文件名）")
    lines.append(f"当前状态: {state}")
    lines.append("必须写完整 frontmatter：from（你）、type（message/freezing/pass）、"
                 "summary（message 类型必填，一句话概括你说了什么）")
    if meta:
        lines.append("需要读取的新消息：")
        for m in meta:
            lines.append(f"- {m['path']}")
    return "\n".join(lines)


def _lock_git(workdir):
    """唤醒 LLM 前锁定本地 git：.git 改名 .git.locked（原子）。

    LLM 有 bash 工具，理论上可执行 git commit/push 破坏 loop 的流程管理
    （绕过 commit_new_files 补全）。改名方案：LLM 对话期间 .git 不存在 →
    **从该 workdir 发起**的 git 操作失败（git 不再上溯到主项目——由
    `_run_wake_proc` 注入的 GIT_CEILING_DIRECTORIES 提供）；loop 完成后改回。

    **守卫范围（重要，勿读成全称）**：只约束"从讨论 workdir 发起的 git
    操作"。主项目仓库不在守卫范围——agent 的 cwd 就是主项目，它可以
    直接在其中执行 git；那部分约束归指令层（工作协议"不要执行 git
    操作"）+ 主项目 .gitignore（discuss-*/ 使讨论内容不会被杂散
    `git add -A` 提交进主仓库）。要真正拦住主仓库需换机制类（沙箱/钩子），
    经评估收益不支撑扩面（评审 A1 裁决记录）。

    归属说明：**刻意不搬到 meeting_fs**——_lock_git 与 finally 里的
    restore_git_lock（解锁）在调用点就近配对（"加锁必有解锁"可就地验证，
    异常路径一目了然）；搬去 fs 层会使该验证跨文件，收益为负（os.rename
    零成本、无 I/O 封装价值）。
    """
    git_dir = os.path.join(workdir, ".git")
    locked = git_dir + ".locked"
    if os.path.isdir(git_dir) and not os.path.exists(locked):
        os.rename(git_dir, locked)


def restore_git_lock(workdir):
    """把 .git.locked 改回 .git（锁恢复的唯一实现）。返回是否确实恢复。

    纯函数 + 返回值：**只在确实恢复时**需要打日志（启动路径打、finally
    路径不打——正常每轮都恢复不是事件，日志会变噪音）；调用方各自决定。
    两个调用点：`wake_llm` 的 finally（正常路径）与启动时的
    `recover_git_lock`（崩溃残留路径）。
    """
    git_dir = os.path.join(workdir, ".git")
    locked = git_dir + ".locked"
    if os.path.isdir(locked) and not os.path.exists(git_dir):
        os.rename(locked, git_dir)
        return True
    return False


def recover_git_lock(workdir, agent):
    """启动时恢复崩溃残留的 git 锁：.git.locked → .git（确实恢复才记日志）。"""
    if restore_git_lock(workdir):
        log(agent, "检测到 .git 残留锁（上次中断）——已恢复")


def _prepare_fork_session(workdir, agent, sid, fork_source, fork_cwd,
                          session_dir, fork_mode, topic):
    """首唤准备：生成 fork 源 + 注入切换叙事；返回 fork 源路径。

    与命令组装分开：这里是"首唤一次性准备"（文件生成/叙事注入/统计
    日志），后者是纯命令拼装——两事混在一起曾让单函数 125 行。

    构建观测点：耗时与峰值 RSS 随统计行一起打印（触发条件可核验的
    最低手段——不建指标体系、不进 status）。
    """
    fork_src = os.path.join(session_dir, f"fork-src-{sid}.jsonl")
    t0 = time.perf_counter()
    n, err = meeting_fs.build_fork_source(
        fork_source, fork_src, sid, fork_cwd or workdir, mode=fork_mode)
    if err:
        log(agent, f"[fatal] fork 源生成失败（mode={fork_mode}）: {err}")
        raise RuntimeError(err)
    build_ms = (time.perf_counter() - t0) * 1000
    # 统计取自产物自描述 header（单一来源；读失败降级为只打条数）
    stats = meeting_fs.read_fork_stats(fork_src)
    perf = f"{build_ms:.0f}ms/{mem_peak_mb():.0f}MB"
    if stats.get("est") is not None:
        est = stats["est"]
        est_txt = f"est≈{est // 1000}k" if est >= 1000 else f"est≈{est}"
        log(agent, f"fork 源（{stats.get('mode') or fork_mode}，{n} 条，"
                   f"丢弃 {stats.get('dropped')} 条，{est_txt}"
                   f"（字符/3 估算），构建 {perf}）: {os.path.basename(fork_src)}")
    else:
        log(agent, f"fork 源（{fork_mode}，{n} 条，构建 {perf}）: "
                   f"{os.path.basename(fork_src)}")
    # 切换叙事：源尾部注入"停止旧任务 → 新任务说明 → assistant 确认"
    # 对话——显式切断历史叙事惯性（agent 读到的最后叙事是任务切换
    # 共识，不再扮演主 pi）。任务说明 = 视角 brief + 主题（来自
    # protocol.json.topic，P1：单一事实源，不再二次解析 question.md）
    topic_txt = topic or "见 question.md"
    turns = [
        ("user", "从现在开始，我们停止之前的任务的执行，开始新任务。"),
        ("assistant", "好的，请说明新任务的具体信息。"),
        # 只交代"身份与任务书在哪"，**不拼视角文件正文**——身份/视角措辞
        # 的唯一来源是 system prompt 注入（gen_agent_def + --append-system-prompt）；
        # 此处再拼一遍会形成第三处措辞（与 gen_agent_def、AGENTS.md 各自生成），
        # 且此前拼的是整个 agent 定义文件（含生成头）并带 500 字截断分支
        ("user", f"新任务：你是多视角分析中的「{agent}」视角参与者。"
                 f"你的身份与视角任务书已在 system prompt 中注入——按它行事。"
                 f"分析主题：{topic_txt}。你的唯一任务是参与这次多视角"
                 f"分析——按视角产出分析/回应其他参与者，写消息文件的路径"
                 f"由本地循环在每次唤醒时告知。上下文中的历史（之前的开发、"
                 f"监控、测试等）都与新任务无关。"),
        ("assistant", f"好的，我已理解新任务：以「{agent}」视角参与分析"
                      f"（主题：{topic_txt}），完成每次唤醒指定的消息写入，"
                      f"不做任务以外的任何事。"),
    ]
    tn = meeting_fs.append_handoff_turns(fork_src, turns)
    log(agent, f"切换叙事已注入（{tn} 条消息/{len(turns) // 2} 对对话）")
    return fork_src


def _build_wake_cmd(workdir, agent, sid, cfg, fork_source, fork_cwd,
                    session_dir, first_wake, pure, prompt,
                    fork_mode=meeting_fs.DEFAULT_FORK_MODE, topic=""):
    """组装唤醒命令（#3 拆分，e2e7 评审）：返回 (cmd, spawn_cwd)。

    首唤：fork 源生成（_prepare_fork_session，fork_mode=compaction|budget|
    full）→ `--session` 直接打开；续接：`--session-id`。
    cwd = fork_cwd（主项目）优先。协议/视角注入在此追加。
    """
    base = os.path.dirname(workdir)
    if first_wake:
        base_name = os.path.basename(base.rstrip("/")) or "discussion"
        display_name = f"{base_name}-{agent}"
        fork_src = _prepare_fork_session(workdir, agent, sid, fork_source,
                                         fork_cwd, session_dir, fork_mode,
                                         topic)
        cmd = ["pi", "--mode", "json", "--session", fork_src,
               "--name", display_name, "--session-dir", session_dir]
    else:
        cmd = ["pi", "--mode", "json", "--session-id", sid,
               "--session-dir", session_dir]
    if pure:
        # Pi 的 pure 近似：关闭外部扩展/技能/prompt-template/主题加载，
        # 保留内置工具（read/bash/edit/write）与项目内 AGENTS.md。
        cmd += ["--no-extensions", "--no-skills", "--no-prompt-templates",
                "--no-themes"]
    model = cfg.get("model") or ""
    if model:
        cmd += ["--model", model]
    thinking = cfg.get("thinking") or ""
    if thinking:
        cmd += ["--thinking", thinking]
    prompt_file = cfg.get("prompt_file") or ""
    if prompt_file and os.path.isfile(os.path.join(workdir, prompt_file)):
        cmd += ["--append-system-prompt", os.path.join(workdir, prompt_file)]
    # 协议 AGENTS.md 注入（fork-only 缺口修复）：work 不在主项目 cwd
    # 祖先链上，pi 不会自动发现——无条件注入（文件存在才加）
    protocol_md = os.path.join(workdir, "AGENTS.md")
    if os.path.isfile(protocol_md):
        cmd += ["--append-system-prompt", protocol_md]
    # 非交互模式 + JSON 事件流；自动信任项目本地文件（AGENTS.md 等）
    cmd += ["--approve", "--print", prompt]
    return cmd, (fork_cwd or workdir)


def _run_wake_proc(cmd, spawn_cwd, workdir, agent):
    """spawn + 分片等待（#3 拆分）：返回 CompletedProcess。

    三路径语义（2026-09-01 定，不得改变）：
      ① pi 正常结束 → 返回 CompletedProcess
      ② 讨论目录被清理（cleanup）→ _kill_proc + SystemExit(0)（干净退出）
      ③ 总超时 → _kill_proc + 抛 TimeoutExpired（上层可恢复重试）
    """
    global _current_proc
    base = os.path.dirname(workdir)
    # stdout 全量缓冲（A，e2e7 评审）：唯一消费者是调用方的 parse_session
    # ——只取 session 头的兜底路径（sid 已预生成，续接不依赖 parse
    # 成功）。性能实测 ≈150-200 KB/唤醒、峰值亚 MB（不构成风险）；
    # 若改为流式读取，必须让"谁读 session 头"同样显式可见（可读性保留票）。
    #
    # GIT_CEILING_DIRECTORIES=<讨论目录>：**git 上溯防护**（实现 A1）。
    # 本进程的 argv 就是 agent 会话里 bash 工具所继承的环境来源——
    # 注入后，从 work-<agent> 发起的 git 不会上溯到主项目仓库。
    # 为什么需要：_lock_git 把 work-<agent>/.git 改名后，git 的默认行为是
    # **向上继续找仓库**——fork 模式下 cwd=主项目，实测锁态下
    # `git rev-parse --git-dir` 从 workdir 发起会命中主项目 .git（rc=0），
    # 守卫形同虚设（见 _lock_git docstring 的范围说明）。
    # 注：Popen 的 env 是**整体替换**，必须合并 os.environ（否则丢 PATH）。
    proc = subprocess.Popen(cmd, cwd=spawn_cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            env={**os.environ, "GIT_CEILING_DIRECTORIES": base})
    _current_proc = proc
    try:
        # 分片等待：每片检查讨论目录是否被清理（cleanup 删目录）——
        # 唤醒阻塞不再屏蔽自退出（cleanup 是唯一清理操作，不靠手动 kill）
        out = err = None
        normal = False
        deadline = time.time() + MAX_WAKE_SEC
        while time.time() < deadline:
            try:
                out, err = proc.communicate(timeout=15)
                normal = True
                break  # 正常结束
            except subprocess.TimeoutExpired:
                if not os.path.isdir(meeting_fs.bare_of_base(base)):
                    log(agent, "讨论目录已清理——终止唤醒中的 pi")
                    _kill_proc(proc)
                    raise SystemExit(0)  # 干净退出（不被 except 捕获）
        if not normal:
            # 总超时：保持原语义（超时 = kill 强杀 + 抛 TimeoutExpired
            # → 上层可恢复重试）
            _kill_proc(proc)
            raise subprocess.TimeoutExpired(cmd, MAX_WAKE_SEC)
    finally:
        _current_proc = None
    return subprocess.CompletedProcess(cmd, proc.returncode, out or "",
                                       err or "")


def wake_llm(workdir, agent, prompt, pure=False, fork_source=None, fork_cwd=None,
             fork_mode=meeting_fs.DEFAULT_FORK_MODE, topic=""):
    """唤醒 pi（fork-only：首唤由本地生成 fork 源 + `--session` 打开，
    后续 `--session-id` 续接）。返回 (sessionID, returncode)。

    每次唤醒记录完整命令行 + prompt 到 wake-logs/（排错第一手段）。
    命令组装与进程等待拆为 _build_wake_cmd / _run_wake_proc（#3 拆分，
    e2e7 评审——可读性：单函数曾 125 行/嵌套 5 层）。
    """
    if not fork_source:
        raise RuntimeError(
            "fork 源未配置（protocol.json 缺 forkSource）——多视角模式必须在"
            "主 pi session 内启动（无 session 时先在项目目录跑一次 pi --print 造引导 session）")
    cfg = read_agent_config(workdir, agent)
    sid = load_session_id(workdir, agent)
    base = os.path.dirname(workdir)
    session_dir = os.path.join(base, "pi-sessions")
    first_wake = not sid
    if first_wake:
        # 预生成 UUID 并显式传 --session-id：即使输出解析失败，本进程
        # 也有确定 sid（续接不依赖 parse 成功）
        import uuid
        sid = str(uuid.uuid4())
    cmd, spawn_cwd = _build_wake_cmd(workdir, agent, sid, cfg, fork_source,
                                     fork_cwd, session_dir, first_wake,
                                     pure, prompt, fork_mode, topic)

    log_dir = os.path.join(base, "wake-logs")
    os.makedirs(log_dir, exist_ok=True)
    os.makedirs(session_dir, exist_ok=True)
    with open(os.path.join(log_dir, f"{agent}-{int(time.time())}.txt"), "w") as f:
        f.write("CMD: " + " ".join(cmd) + "\n\nPROMPT:\n" + prompt + "\n")
    log(agent, f"唤醒 pi (session={sid})")
    _lock_git(workdir)
    try:
        r = _run_wake_proc(cmd, spawn_cwd, workdir, agent)
    finally:
        restore_git_lock(workdir)

    new_sid = parse_session(r.stdout) or sid
    if new_sid:
        save_session_id(workdir, agent, new_sid)
    if r.returncode != 0:
        # 常见可重试失败：session 文件损坏/不存在。pi 对 --session-id
        # 通常自动创建；保留 stderr 日志便于诊断。明确 "No session
        # found" 则清空 status 后下轮新建。
        if "No session found" in (r.stderr or "") or "Session not found" in (r.stderr or ""):
            log(agent, "唤醒失败（session 无效）——清空重试")
            sp = os.path.join(base, f"status-{agent}.json")
            if os.path.exists(sp):
                os.remove(sp)
    return new_sid, r.returncode


def _read_perspective_brief(workdir, agent):
    """读视角任务书正文（wake prompt 身份重申用）。

    来源：pi-agent.json 的 prompt_file 字段（与注入同一事实源，L3 修复
    ——原硬编码 .pi/agent/<agent>.md，与注入路径两处推导，prompt_file
    一改身份重申静默降级）；缺失字段回退默认路径（前向兼容）。
    缺失/超长（>500 字）→ 截断；文件不存在 → None（不影响唤醒）。
    """
    try:
        with open(os.path.join(workdir, "pi-agent.json")) as f:
            pf = json.load(f).get("prompt_file") or ""
    except (OSError, ValueError):
        pf = ""
    fp = (os.path.join(workdir, pf) if pf
          else os.path.join(workdir, ".pi/agent", f"{agent}.md"))
    try:
        with open(fp, encoding="utf-8") as f:
            brief = f.read().strip()
    except OSError:
        return None
    if len(brief) > 500:
        brief = brief[:500] + "…（见 system prompt 完整任务书）"
    return brief or None


def make_responder(pure, fork_source=None, fork_cwd=None,
                   fork_mode=meeting_fs.DEFAULT_FORK_MODE, topic=""):
    """构造真实 LLM responder：唤醒 pi，LLM 写内容文件。

    LLM 只提供内容（写消息文件），流程（补全字段/commit/push）
    由引擎的 commit_new_files 接管。"""
    def responder(workdir, agent, head, meta, is_first, rr_turn, retry,
                  finalizing=False, finalize_reason="consensus"):
        if finalizing:
            if finalize_reason == "consensus":
                reason_txt = "所有参与者已 pass，共识达成"
            elif finalize_reason == "quota":
                reason_txt = "达到轮次上限，未完全共识"
            else:
                reason_txt = "无进展超时（stall），未完全共识"
            # fork 模式（cwd=主项目）下“工作区根目录”有歧义——路径必须绝对
            result_path = os.path.join(workdir, "result.md")
            prompt = (f"讨论已收敛（{reason_txt}）。"
                      f"请写 result.md 到 {result_path}，总结讨论结论。")
            if retry:
                prompt = (f"你上一次被唤醒但未生成有效的 result.md。"
                          f"请现在写 result.md（非空，总结讨论结论）到 {result_path}。")
            if mem_available_mb() < MIN_MEM_MB:
                log(agent, "内存不足——抛可恢复异常（不代写 freezing，下轮重试）")
                raise RecoverableWakeError("内存不足")
            wake_llm(workdir, agent, prompt, pure,
                     fork_source=fork_source, fork_cwd=fork_cwd)
            return True
        if rr_turn:
            state = "round-robin（轮到你：写 pass 确认共识，单向流无异议）"
        else:
            state = "meeting（有未读新消息，可发言或 freezing）"
        # fork 模式（cwd=主项目）下 LLM 不在 workdir——msg_path/meta 必须
        # 绝对路径（legacy 模式下绝对路径同样有效，统一一条路径）
        msg_path = os.path.join(workdir, agent,
                                f"{next_msg_id(workdir, agent)}.md")
        meta_abs = [dict(m, path=os.path.join(workdir, m["path"]))
                    for m in meta]
        prompt = build_wake_prompt(agent, meta_abs, is_first, state, retry,
                                   msg_path=msg_path,
                                   perspective_brief=_read_perspective_brief(
                                       workdir, agent))
        if mem_available_mb() < MIN_MEM_MB:
            log(agent, "内存不足——抛可恢复异常（不代写 freezing，下轮重试）")
            raise RecoverableWakeError("内存不足")
        wake_llm(workdir, agent, prompt, pure,
                 fork_source=fork_source, fork_cwd=fork_cwd,
                 fork_mode=fork_mode, topic=topic)
        return True
    return responder


def _preserve_result_md(workdir):
    """收尾时保存 result.md（薄包装 → meeting_fs.preserve_result_md，
    T2 合并：与 cleanup 路径共享同一实现）。"""
    dest = meeting_fs.preserve_result_md(os.path.dirname(workdir))
    if dest:
        print(f"[{time.strftime('%H:%M:%S')}] 已保存 result.md → {dest}",
              flush=True)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: python3 meeting_loop.py <workdir> <agent> "
              "[--max-meeting N] [--max-rr N] [--stall-timeout S] [--pure]")
        sys.exit(1)
    workdir, agent = sys.argv[1], sys.argv[2]
    recover_git_lock(workdir, agent)
    pure = "--pure" in sys.argv
    mm, mr, st = 10, 7, 600
    # 协议从 **bare HEAD** 读（单一来源 = 共享事实；本地副本 LLM 可改）——
    # 与 engine participants()/check_status 同一原语
    bare = meeting_fs.bare_of_workdir(workdir)
    proto = meeting_fs.read_protocol(bare)
    if not proto:
        print(f"[fatal] protocol.json 读取失败（bare HEAD 无有效内容）: {bare}",
              flush=True)
        sys.exit(1)
    if proto.get("pure"):
        pure = True
    if proto.get("maxMeetingRounds"):
        mm = proto["maxMeetingRounds"]
    if proto.get("maxRRRounds"):
        mr = proto["maxRRRounds"]
    if proto.get("stallTimeoutSeconds"):
        st = proto["stallTimeoutSeconds"]
    # （CLI 配额覆盖通道已删——L5：协议是配额唯一事实源，生产无调用方；
    # docstring 用法行同步删除）
    fork_mode_cfg = proto.get("forkMode") or meeting_fs.DEFAULT_FORK_MODE
    if fork_mode_cfg not in meeting_fs.FORK_MODES:
        # 配置错误不是运行期故障（不进 engine 重试路径）——照 forkSource 先例
        print(f"[fatal] protocol.json 的 forkMode 非法: {fork_mode_cfg!r}"
              f"（合法值: {'/'.join(meeting_fs.FORK_MODES)}）——若来自旧版本"
              f"产物（rename 前的 active/curated），请清理分析目录后重跑",
              flush=True)
        sys.exit(1)
    fork_source = proto.get("forkSource") or ""
    if not fork_source:
        print("[fatal] protocol.json 缺 forkSource——多视角模式必须在主 pi "
              "session 内启动（wrapper 会自动解析；无 session 时先在项目目录"
              "跑一次 pi --print 造引导 session）", flush=True)
        sys.exit(1)
    try:
        agent_loop(workdir, agent,
                   make_responder(pure,
                                  fork_source=fork_source,
                                  fork_cwd=proto.get("forkCwd") or "",
                                  fork_mode=fork_mode_cfg,
                                  topic=proto.get("topic") or ""),
                   max_meeting=mm, max_rr=mr, stall_timeout=st)
    except KeyboardInterrupt:
        log(agent, "被中断")
        sys.exit(130)
    if agent == proto.get("resultWriter"):
        _preserve_result_md(workdir)
