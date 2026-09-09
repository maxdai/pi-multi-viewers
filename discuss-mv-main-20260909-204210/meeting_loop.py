#!/usr/bin/env python3
"""meeting_loop.py —— 真实 LLM 薄壳（Pi 适配版）。

复用 meeting_engine 的唯一状态机，只注入"唤醒 pi"的 responder。
协议逻辑（锁/配额/级联/信号）全在引擎，此处只做 LLM 交互。

用法：python3 meeting_loop.py <workdir> <agent> [--max-meeting N] [--max-rr N] [--stall-timeout S] [--pure]
"""

import json
import os
import re
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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


def session_id(workdir, agent):
    """返回本 agent 的 pi session id（first run 创建，之后复用）。

    session 文件存放在 <base>/pi-sessions，随讨论目录一起清理。
    id 使用 base 目录名 + agent 名，保证同一讨论内各异、且可读。
    """
    base = os.path.dirname(workdir)
    base_name = os.path.basename(base.rstrip("/")) or "discussion"
    ident = re.sub(r"[^A-Za-z0-9._-]+", "-", f"discuss-{base_name}-{agent}")
    return ident


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
    任何 git 操作失败（"not a git repository"），loop 完成后改回。
    """
    git_dir = os.path.join(workdir, ".git")
    locked = git_dir + ".locked"
    if os.path.isdir(git_dir) and not os.path.exists(locked):
        os.rename(git_dir, locked)


def _unlock_git(workdir):
    """LLM 对话完成后恢复本地 git：.git.locked 改回 .git。

    finally 中调用——任何异常/超时路径都恢复（崩溃残留由
    recover_git_lock 在下次启动时处理）。
    """
    git_dir = os.path.join(workdir, ".git")
    locked = git_dir + ".locked"
    if os.path.isdir(locked) and not os.path.exists(git_dir):
        os.rename(locked, git_dir)


def recover_git_lock(workdir, agent):
    """启动时恢复崩溃残留的 git 锁：.git.locked → .git。"""
    git_dir = os.path.join(workdir, ".git")
    locked = git_dir + ".locked"
    if os.path.isdir(locked) and not os.path.exists(git_dir):
        os.rename(locked, git_dir)
        log(agent, "检测到 .git 残留锁（上次中断）——已恢复")


def wake_llm(workdir, agent, prompt, pure=False, fork_source=None, fork_cwd=None):
    """唤醒 pi（fork-only：首唤 --fork，后续 --session-id 续接）。返回 sessionID。

    每次唤醒记录完整命令行 + prompt 到 wake-logs/（排错第一手段）。

    fork 模式（唯一模式）：首次唤醒（无已存 sid）以 --fork 挂载主
    session 全量上下文 + --name 可读显示名（id 由 pi 生成 UUID——id 归
    机制、名字归人）；agent 进程 cwd = fork_cwd（主项目，可直接读项目
    文件）。后续唤醒 sid 已存 → --session-id 续接。
    实测 2026-09-09（docs/examples/first-experiment + e2e）：fork 上下文
    携带、--name 落盘（session_info label）、续接模式全部通过。

    协议注入：workdir/AGENTS.md（讨论协议）不在主项目 cwd 的祖先链上，
    pi 不会自动发现——无条件 --append-system-prompt 注入（文件存在才加）。
    e2e 曾暴露缺口：无注入时靠模型能力偶尔能跑通，非设计保证。
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
        # 预生成 UUID 并显式传 --session-id：pi --fork 接受指定 id——
        # 即使输出解析失败，本进程也有确定 sid（续接不依赖 parse 成功）
        import uuid
        sid = str(uuid.uuid4())
        base_name = os.path.basename(base.rstrip("/")) or "discussion"
        display_name = f"{base_name}-{agent}"
        cmd = ["pi", "--mode", "json", "--fork", fork_source,
               "--session-id", sid, "--name", display_name,
               "--session-dir", session_dir]
        spawn_cwd = fork_cwd or workdir
    else:
        cmd = ["pi", "--mode", "json", "--session-id", sid,
               "--session-dir", session_dir]
        spawn_cwd = fork_cwd or workdir
    if pure:
        # Pi 的 pure 近似：关闭外部扩展/技能/prompt-template/主题加载，
        # 保留内置工具（read/bash/edit/write）与项目内 AGENTS.md。
        cmd += ["--no-extensions", "--no-skills", "--no-prompt-templates", "--no-themes"]
    model = cfg.get("model") or ""
    if model:
        cmd += ["--model", model]
    thinking = cfg.get("thinking") or ""
    if thinking:
        cmd += ["--thinking", thinking]
    prompt_file = cfg.get("prompt_file") or ""
    if prompt_file and os.path.isfile(os.path.join(workdir, prompt_file)):
        cmd += ["--append-system-prompt", os.path.join(workdir, prompt_file)]
    # 协议 AGENTS.md 注入（fork-only 缺口修复，见 docstring）：
    protocol_md = os.path.join(workdir, "AGENTS.md")
    if os.path.isfile(protocol_md):
        cmd += ["--append-system-prompt", protocol_md]
    # 非交互模式 + JSON 事件流；自动信任项目本地文件（AGENTS.md 等）
    cmd += ["--approve", "--print", prompt]

    log_dir = os.path.join(base, "wake-logs")
    os.makedirs(log_dir, exist_ok=True)
    os.makedirs(session_dir, exist_ok=True)
    with open(os.path.join(log_dir, f"{agent}-{int(time.time())}.txt"), "w") as f:
        f.write("CMD: " + " ".join(cmd) + "\n\nPROMPT:\n" + prompt + "\n")
    log(agent, f"唤醒 pi (session={sid})")
    _lock_git(workdir)
    global _current_proc
    try:
        proc = subprocess.Popen(cmd, cwd=spawn_cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        _current_proc = proc
        try:
            # 分片等待：每片检查讨论目录是否被清理（cleanup 删目录）——
            # 唤醒阻塞不再屏蔽自退出（完善流程：cleanup 是唯一清理操作，
            # 无需手动 kill；用户 2026-09-01 原则：不固定为错误操作+补丁）。
            out = err = None
            normal = False
            deadline = time.time() + MAX_WAKE_SEC
            while time.time() < deadline:
                try:
                    out, err = proc.communicate(timeout=15)
                    normal = True
                    break  # 正常结束
                except subprocess.TimeoutExpired:
                    if not os.path.isdir(os.path.join(base, "repo.git")):
                        log(agent, "讨论目录已清理——终止唤醒中的 pi")
                        _kill_proc(proc)
                        raise SystemExit(0)  # 干净退出（SystemExit 不被 except 捕获）
            if not normal:
                # 总超时：保持原语义（run(timeout) 超时 = kill 强杀 + 抛
                # TimeoutExpired → 上层可恢复重试）
                _kill_proc(proc)
                raise subprocess.TimeoutExpired(cmd, MAX_WAKE_SEC)
        finally:
            _current_proc = None
        r = subprocess.CompletedProcess(cmd, proc.returncode, out or "", err or "")
    finally:
        _unlock_git(workdir)

    new_sid = parse_session(r.stdout) or sid
    if new_sid:
        save_session_id(workdir, agent, new_sid)
    if r.returncode != 0:
        # 常见可重试失败：session 文件损坏/不存在？pi 对 --session-id 通常
        # 自动创建；保留 stderr 日志便于诊断。若明确 "No session found" 则
        # 清空 status 后下轮新建。
        if "No session found" in (r.stderr or "") or "Session not found" in (r.stderr or ""):
            log(agent, "唤醒失败（session 无效）——清空重试")
            sp = os.path.join(base, f"status-{agent}.json")
            if os.path.exists(sp):
                os.remove(sp)
    return new_sid, r.returncode


def _read_perspective_brief(workdir, agent):
    """读视角任务书正文（wake prompt 身份重申用）。

    来源：work-<agent>/.pi/agent/<agent>.md（prompt_file 注入源的同一份）。
    缺失/超长（>500 字，任务书是全量正文，wake 只需首段锚定）→ 截取前
    500 字；文件不存在 → None（不影响唤醒，仅少一段重申）。
    """
    fp = os.path.join(workdir, ".pi/agent", f"{agent}.md")
    try:
        with open(fp, encoding="utf-8") as f:
            brief = f.read().strip()
    except OSError:
        return None
    if len(brief) > 500:
        brief = brief[:500] + "…（见 system prompt 完整任务书）"
    return brief or None


def make_responder(pure, fork_source=None, fork_cwd=None):
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
        from meeting_fs import next_msg_id
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
                 fork_source=fork_source, fork_cwd=fork_cwd)
        return True
    return responder


def _preserve_result_md(workdir):
    """收尾时把 result.md 从 bare 复制到父级（<base名>-result.md）。"""
    import subprocess as sp
    base = os.path.dirname(workdir)
    bare = os.path.join(base, "repo.git")
    if not os.path.isdir(bare):
        return
    r = sp.run(["git", "-C", bare, "show", "HEAD:result.md"],
               capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return
    base_name = os.path.basename(base.rstrip("/")) or "discussion"
    dest = os.path.join(os.path.dirname(base.rstrip("/")) or ".",
                        f"{base_name}-result.md")
    with open(dest, "w") as f:
        f.write(r.stdout)
    # 修复 2026-09-01（测试报告问题 1）：函数无 agent 上下文——
    # 原引用未定义变量 agent → NameError（写文件成功后 log 行崩溃，
    # 进程异常退出）。log 需要 agent 名参数，此处直接用模块 log 的
    # 全局格式打印（不带 agent 前缀）。
    print(f"[{time.strftime('%H:%M:%S')}] 已保存 result.md → {dest}", flush=True)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: python3 meeting_loop.py <workdir> <agent> "
              "[--max-meeting N] [--max-rr N] [--stall-timeout S] [--pure]")
        sys.exit(1)
    workdir, agent = sys.argv[1], sys.argv[2]
    recover_git_lock(workdir, agent)
    pure = "--pure" in sys.argv
    mm, mr, st = 10, 7, 600
    try:
        proto = json.load(open(os.path.join(workdir, "protocol.json")))
    except (OSError, ValueError) as e:
        print(f"[fatal] protocol.json 读取失败: {e}", flush=True)
        sys.exit(1)
    if proto.get("pure"):
        pure = True
    if proto.get("maxMeetingRounds"):
        mm = proto["maxMeetingRounds"]
    if proto.get("maxRRRounds"):
        mr = proto["maxRRRounds"]
    if proto.get("stallTimeoutSeconds"):
        st = proto["stallTimeoutSeconds"]
    for i, a in enumerate(sys.argv):
        if a in ("--max-meeting", "--max-rr", "--stall-timeout") \
                and i + 1 < len(sys.argv):
            if a == "--max-meeting":
                mm = int(sys.argv[i + 1])
            elif a == "--max-rr":
                mr = int(sys.argv[i + 1])
            else:
                st = int(sys.argv[i + 1])
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
                                  fork_cwd=proto.get("forkCwd") or ""),
                   max_meeting=mm, max_rr=mr, stall_timeout=st)
    except KeyboardInterrupt:
        log(agent, "被中断")
        sys.exit(130)
    if agent == proto.get("resultWriter"):
        _preserve_result_md(workdir)
