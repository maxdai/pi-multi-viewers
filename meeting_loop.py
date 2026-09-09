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
                      msg_path=None):
    """唤醒 prompt（动态信息；协议规则在 AGENTS.md）。

    msg_path: loop 计算的下一个消息路径（如 'a/0003.md'）——指定 LLM
    应写的文件（用户 9618：文件名由 loop 决定而非 LLM 自己算，可靠性
    更高；仍不保证 LLM 会写，但消除"算错序号撞已提交"的假无产出主因）。
    LLM 不需要知道 git HEAD（用户 9773）：seen_at 是协议字段归 loop 填，
    传 HEAD 给 LLM 反而引入 git 概念——彻底脱离。
    """
    lines = []
    if retry:
        lines.append("你刚才被唤醒但没写消息。必须写一条消息文件。")
    if is_first:
        lines.append("（无新消息，你是讨论的第一位发言者）")
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


def wake_llm(workdir, agent, prompt, pure=False):
    """唤醒 pi（session 复用 + 失败回退）。返回 sessionID。

    每次唤醒记录完整命令行 + prompt 到 wake-logs/（排错第一手段）。
    """
    cfg = read_agent_config(workdir, agent)
    sid = load_session_id(workdir, agent)
    if not sid:
        sid = session_id(workdir, agent)
    base = os.path.dirname(workdir)
    session_dir = os.path.join(base, "pi-sessions")
    cmd = ["pi", "--mode", "json", "--session-id", sid,
           "--session-dir", session_dir]
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
        proc = subprocess.Popen(cmd, cwd=workdir, stdout=subprocess.PIPE,
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


def make_responder(pure):
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
            prompt = (f"讨论已收敛（{reason_txt}）。"
                      f"请写 result.md 到工作区根目录，总结讨论结论。")
            if retry:
                prompt = (f"你上一次被唤醒但未生成有效的 result.md。"
                          f"请现在写 result.md（非空，总结讨论结论）到工作区根目录。")
            if mem_available_mb() < MIN_MEM_MB:
                log(agent, "内存不足——抛可恢复异常（不代写 freezing，下轮重试）")
                raise RecoverableWakeError("内存不足")
            wake_llm(workdir, agent, prompt, pure)
            return True
        if rr_turn:
            state = "round-robin（轮到你：写 pass 确认共识，单向流无异议）"
        else:
            state = "meeting（有未读新消息，可发言或 freezing）"
        from meeting_fs import next_msg_id
        msg_path = f"{agent}/{next_msg_id(workdir, agent)}.md"
        prompt = build_wake_prompt(agent, meta, is_first, state, retry,
                                   msg_path=msg_path)
        if mem_available_mb() < MIN_MEM_MB:
            log(agent, "内存不足——抛可恢复异常（不代写 freezing，下轮重试）")
            raise RecoverableWakeError("内存不足")
        wake_llm(workdir, agent, prompt, pure)
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
    try:
        agent_loop(workdir, agent, make_responder(pure),
                   max_meeting=mm, max_rr=mr, stall_timeout=st)
    except KeyboardInterrupt:
        log(agent, "被中断")
        sys.exit(130)
    if agent == proto.get("resultWriter"):
        _preserve_result_md(workdir)
