#!/usr/bin/env python3
"""meeting_loop.py —— 真实 LLM 薄壳（Pi 适配版）。

复用 meeting_engine 的唯一状态机，只注入"唤醒 pi"的 responder。
协议逻辑（锁/配额/级联/信号）全在引擎，此处只做 LLM 交互。

用法：python3 meeting_loop.py <workdir> <agent> [--extension-policy V]
（配额/超时从 protocol.json 读——单一事实源；无 CLI 覆盖）
"""

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import meeting_fs
from meeting_fs import next_msg_id, log
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
    操作"）+ 主项目 .gitignore（mv-*/ 使分析内容不会被杂散
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
    # summary 模式（决策 24）：源换成 setup 时生成的 base（pi 已 compact 的副本，
    # 远端=定向摘要、近端=原始窗口）。base 缺失 ⇒ **可见地**回落 budget：
    # 正常情况下 setup 生成失败时已把 protocol 改回 budget（单一事实源诚实），
    # 这里只兜"直调/竞态"——绝不静默换成"无摘要的 compaction"。
    src_session, mode = fork_source, fork_mode
    if fork_mode == "summary":
        base = os.path.join(os.path.dirname(workdir),
                            meeting_fs.COMPACT_BASE_NAME)
        if os.path.exists(base):
            src_session = base
        else:
            log(agent, "[warn] summary 模式的 base 不存在 → 本次按 budget 运行")
            mode = "budget"
    t0 = time.perf_counter()
    n, err = meeting_fs.build_fork_source(
        src_session, fork_src, sid, fork_cwd or workdir, mode=mode)
    if err:
        log(agent, f"[fatal] fork 源生成失败（mode={mode}）: {err}")
        raise RuntimeError(err)
    build_ms = (time.perf_counter() - t0) * 1000
    # 统计取自产物自描述 header（单一来源；读失败降级为只打条数）
    stats = meeting_fs.read_fork_stats(fork_src)
    perf = f"{build_ms:.0f}ms/{mem_peak_mb():.0f}MB"
    if stats.get("est") is not None:
        est = stats["est"]
        est_txt = f"est≈{est // 1000}k" if est >= 1000 else f"est≈{est}"
        # dropped 只有 budget 模式有（summary 的丢弃记账在 base 生成那一步，
        # 见 context-base.json）——没有就不印那半句，绝不打印 "丢弃 None 条"
        _d = stats.get("dropped")
        drop_txt = f"丢弃 {_d} 条，" if _d is not None else ""
        log(agent, f"fork 源（{stats.get('mode') or fork_mode}，{n} 条，"
                   f"{drop_txt}{est_txt}"
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
                    session_dir, first_wake, extension_policy, prompt,
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
    # ---- agent 进程的扩展策略（design.md 决策 20；三档见 meeting_fs）----
    # 三档共用**同一段零扩展前缀**；mc-tools 只决定"能否再追加 -e <MC 入口>"。
    # 为什么这样写：此前 mc-tools 的降级分支自己拼前缀后**提前 return**，把后面的
    # model/thinking/system-prompt/--print 与 spawn cwd 全截断——那条"降级"路径在
    # 无 MC 的机器上产出残缺命令（e2e24 评审 S1；根因 = 前缀抄三遍 + 短路返回）。
    # 现在**只有一个出口**（本函数末尾），降级只是"少追加一个 -e"。
    #
    # 三档语义：mc-tools（默认）给 agents `ctx_search`（MC 的只读检索工具；
    # entry = 工具注册 + 两个生命周期钩子 → 不带 historian/压缩）；none 零扩展（零依赖）；
    # all 走 pi 默认发现（A/B 与显式 opt-in，须有净收益账）。
    no_ext = ["--no-extensions", "--no-skills", "--no-prompt-templates",
              "--no-themes"]
    effective_policy = extension_policy
    downgrade_reason = ""
    if extension_policy not in meeting_fs.EXTENSION_POLICIES:
        # 值域守卫在 __main__ 已拦（配置错误不进 engine）；直调也要响
        raise RuntimeError(f"未知 extensionPolicy: {extension_policy!r}"
                           f"（合法值: {'/'.join(meeting_fs.EXTENSION_POLICIES)}）")
    if extension_policy == "none":
        cmd += no_ext
    elif extension_policy == "mc-tools":
          # mc-tools = 零扩展 + 显式加载**三份能力**（零第三方依赖）：
          #   ① MC 的 subagent-entry（工具注册 + 生命周期钩子，无 historian）→ ctx_search
          #   ② pi **内置** MCP 扩展 → web_search / web_reader / zread 等
          #   ③ pi **内置** codemode 扩展 → 让 ② 的工具真正可达（见下方“为什么 ③ 必需”）
          # 为什么必须显式 -e：`--no-extensions` 关的是"扩展发现**与内置扩展**"
          # （pi --help 原文）——内置 MCP 与 codemode 都在关停范围；`-e <path>` 同时
          # 接受 `builtin:<name>`（pi --help 原文）。
          # 为什么 ③ 必需：server 用 pi 默认 `exposure: codemode` 时，其工具不声明给
          # 模型、只能从 codemode 脚本调用；少了 ③，MCP 扩展的 ensureDiscoveryActive
          # 找不到 codemode 工具，只发一条 ui.notify 警告（而我们的非交互模式里 notify
          # 是 no-op）⇒ 工具注册了却调不到、且静默（2026-09-30 读源码 + 用户裁决走原生）。
          # 降级语义：唯一"可失败"的入口是 MC（解析第三方包的内部文件）——失败则
          # **本档核心能力（ctx_search）不到位** → 生效=none，原因里点名；②③ 是常量
          # 入口、恒在，故降级不涉及它们（原因字段只写入口层事实）。
        cmd += no_ext
        cmd += ["-e", meeting_fs.BUILTIN_MCP_ENTRY]
        cmd += ["-e", meeting_fs.BUILTIN_CODEMODE_ENTRY]
        entry, err = meeting_fs.resolve_mc_tools_entry()
        if entry:
            cmd += ["-e", entry]
        else:
            if meeting_fs.mc_tools_strict():
                # 严格模式（测试/探针保真）：缺入口即响，不降级——否则测试可能在
                # "没装 MC"的环境里通过，而 ctx_search 从未生效
                log(agent, f"[fatal] mc-tools 档入口解析失败（严格模式）：{err}")
                raise RuntimeError(f"mc-tools 档不可用: {err}")
            # 允许而非要求：缺 MC → 少一份 -e，但**可见**
            # 原因只写**入口层**事实（err 来自解析）。不写“MCP 工具不受影响”：
            # 工具是否可用我们观测不到（非交互模式下 server 状态不进任何产物，
            # 见 docs/design.md 决策 20「平台能力的可见性」）。
            downgrade_reason = err
            effective_policy = "none"
    elif extension_policy == "all":
        # pi 默认发现：**不加**任何 --no-*、也不加 -e（A/B 与显式 opt-in）
        pass
    else:
        # 失败模式不同，与上面 :326 的守卫不可互相替代：
        #   · :326 拦**元组之外**的值（用户传错）
        #   · 这里拦**元组之内、但无分支**的值（加值忘加分支；此时 :326 不响）
        # 删掉的代价不对称：新策略值会静默落进 `all` 档（= pi 默认发现 =
        # 载入 AFT/MC 全档，分钟×N 级且无信号）；保留 0 成本。当前三档都有
        # 分支 ⇒ 本分支不可达，但**不是死代码**。
        raise RuntimeError(
            f"扩展策略分派未穷尽: {extension_policy!r}"
            f"（合法值: {'/'.join(meeting_fs.EXTENSION_POLICIES)}）")
    if first_wake:
        # 登记行（观测面的稳定字段；报告据此给"声明 vs 生效"）。只在首唤打：
        # 策略在一次运行内不变，变了也是配置错误（重跑即可）。
        # 降级时把原因写进**登记行的同一行**（S2：第二行是复述，已删——报告只解析
        # 本行，`observability._report_extension_line` 的 regex 匹配到行尾）。
        # 不再有"部分/完全"标签：本档只有 MC 一个入口需要解析（内置 MCP 是常量），
        # 失败即"核心能力缺失"，原因本身会点名（2026-09-30）。
        reason = f" 降级原因={downgrade_reason}" if downgrade_reason else ""
        log(agent, f"扩展策略: 声明={extension_policy} 生效={effective_policy}"
                   f" strict={int(meeting_fs.mc_tools_strict())}{reason}")
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


# ---- 定向摘要（决策 24）的常量 ----
# base session：pi 在自己 compact 过的副本上产出的"摘要 + 近端窗口"——
# 各 agent 的首唤都从它切出 fork 源（一次生成、三视角共享）。
# 摘要生成的硬超时：一次模型调用（输入是会话前缀，可能很大）。超时 ⇒ 回落
# budget（不阻断分析）——宁可退到旧行为，不可让 setup 挂住。
COMPACT_TIMEOUT_SEC = 900


def build_summary_instructions(topic):
    """定向摘要的 `customInstructions`（单一措辞来源）。

    为什么这么写（决策 24）：pi 把 `customInstructions` 追加成
    "Additional focus: …"（`core/compaction/compaction.ts:719-720`）——它
    **只影响摘要内容**，不影响"哪些消息进摘要"（那是按位置的前缀切）。所以这段
    文字的全部任务是：① 说清"该保留什么"（与主题相关的事实）；② **抑制框架
    偏差**——摘要会被模型**先于**近端原始窗口读到，它若替读者做取舍排序，等于
    给每个视角预设了一个框架（本项目最在意的那类偏差）。
    """
    return (
        f"这次摘要将作为一次关于「{topic}」的多视角分析任务的背景。"
        "只保留与该主题相关的：决策及其理由、被否决的方案与重估条件、"
        "实测数字与其口径（测点/样本/可比性）、涉及的文件与函数与命令、"
        "以及仍未解决的问题。与该主题无关的过程（其它任务、无关讨论、"
        "例行工具流水）一律一句带过或不写。"
        "**只陈述事实与结论，不要替读者做取舍排序、不要给建议、不要预测"
        "读者需要什么。**保留精确的路径、函数名、数字与错误信息。"
    )


def generate_compact_base(main_session, out_path, model, thinking, topic):
    """在**副本**上跑一次 pi 自己的 compact（带定向指令）→ base session（决策 24）。

    为什么走 `pi --mode rpc` 而不是自己写摘要 prompt：摘要器 = pi 自己那份
    （同一套结构化检查点 prompt + `customInstructions`），**不漂移**；RPC 通道
    能对**指定副本**工作（2026-10-08 实测：`--session <副本>` 打开的确实是副本、
    只读命令不写盘）。副本 ⇒ **主 session 永不被改动**。

    为什么**零扩展**（不带任何 `-e`）：摘要是一次纯模型调用、不需要工具；不加载
    扩展也就不可能把 MC 的 historian 之类带进来（它只在我们显式 `-e` 时才有）。

    失败/超时 ⇒ `(None, error)`，调用方**回落 budget**（不阻断分析）。

    返回 `(stats, error)`；成功时另写同目录 `context-base.json`（报告读它）。
    """
    instr = build_summary_instructions(topic)
    try:
        shutil.copyfile(main_session, out_path)
    except OSError as e:
        return None, f"复制主 session 失败: {e}"
    cmd = ["pi", "--mode", "rpc", "--session", out_path,
           "--no-extensions", "--no-skills", "--no-prompt-templates",
           "--no-themes"]
    if model:
        cmd += ["--model", model]
    if thinking:
        cmd += ["--thinking", thinking]
    payload = json.dumps({"id": "compact", "type": "compact",
                          "customInstructions": instr}) + "\n"
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=_spawn_env(out_path))
        out, errout = proc.communicate(payload, timeout=COMPACT_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return None, f"摘要生成超时（>{COMPACT_TIMEOUT_SEC}s）"
    except OSError as e:
        return None, f"摘要进程启动失败: {e}"
    ok, detail = False, ""
    for line in (out or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("command") == "compact":
            ok = bool(ev.get("success"))
            if not ok:
                detail = json.dumps(ev.get("error"), ensure_ascii=False)[:200]
    if not ok:
        tail = (errout or "").strip().replace("\n", " ")[:160]
        return None, (f"compact 未成功（rc={proc.returncode}"
                      f"{'，' + detail if detail else ''}"
                      f"{'，stderr: ' + tail if tail else ''}）")
    stats, ferr = meeting_fs.finalize_compaction_base(out_path)
    if ferr:
        return None, ferr
    stats.update({"model": model, "thinking": thinking, "topic": topic,
                  "instructions": instr, "rc": proc.returncode})
    try:
        with open(os.path.join(os.path.dirname(out_path),
                              meeting_fs.COMPACT_BASE_STATS), "w",
                  encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
    except OSError as e:
        # 统计写不动不影响 base 本身（报告会显示 n/a）
        log("_", f"[warn] context-base.json 写入失败: {e}")
    return stats, ""


def _spawn_env(workdir):
    """agent 进程的环境（Popen 的 env 是**整体替换** → 必须合并 os.environ，
    否则丢 PATH）。

    只注入 `GIT_CEILING_DIRECTORIES=<讨论目录>`：**git 上溯防护**（实现 A1）。
    为什么需要：`_lock_git` 把 work-<agent>/.git 改名后，git 的默认行为是
    **向上继续找仓库**——fork 模式下 cwd = 主项目，实测锁态下
    `git rev-parse --git-dir` 从 workdir 发起会命中主项目 .git（rc=0），守卫
    形同虚设（见 `_lock_git` docstring 的范围说明）。本进程的 argv 就是 agent
    会话里 bash 工具所继承的环境来源。
    （此前还注入 XDG_CONFIG_HOME 做作用域配置——随"屏蔽 AFT"整块退役。）
    """
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": os.path.dirname(workdir)}
    return env


def _run_wake_proc(cmd, spawn_cwd, workdir, agent):
    """spawn + 分片等待（#3 拆分）：返回 CompletedProcess。

    三路径语义（2026-09-01 定，不得改变）：
      ① pi 正常结束 → 返回 CompletedProcess
      ② 讨论目录被清理（cleanup）→ _kill_proc + SystemExit(0)（干净退出）
      ③ 总超时 → _kill_proc + 抛 TimeoutExpired（上层 = 引擎异常边界：
         **只 log + 回轮询、不自动重试**，见 meeting_engine.py 的 except Exception；
         本次唤醒产出不提交——若已写出消息文件，下一唤醒写**同一槽位**覆盖它，
         引擎会记一行「⚠ 未提交产出将被同槽覆盖」）
    """
    global _current_proc
    base = os.path.dirname(workdir)
    # stdout 全量缓冲（A，e2e7 评审）：唯一消费者是调用方的 parse_session
    # ——只取 session 头的兜底路径（sid 已预生成，续接不依赖 parse
    # 成功）。性能实测 ≈150-200 KB/唤醒、峰值亚 MB（不构成风险）；
    # 若改为流式读取，必须让"谁读 session 头"同样显式可见（可读性保留票）。
    #
    # 环境构造统一在 _spawn_env（GIT_CEILING_DIRECTORIES 的 git 上溯防护
    # 理由见该函数；brief：本进程的 argv 就是 agent 会话里 bash 工具所继承的
    # 环境来源，而 _lock_git 只锁 work-<agent>/.git、git 默认会向上找仓库）。
    proc = subprocess.Popen(cmd, cwd=spawn_cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            env=_spawn_env(workdir))
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


def wake_llm(workdir, agent, prompt,
             extension_policy=meeting_fs.DEFAULT_EXTENSION_POLICY,
             fork_source=None, fork_cwd=None,
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
                                     extension_policy, prompt, fork_mode, topic)

    log_dir = os.path.join(base, "wake-logs")
    os.makedirs(log_dir, exist_ok=True)
    os.makedirs(session_dir, exist_ok=True)
    # wake-log = 命令行全文（**单一来源**）：prompt 通过 argv（--append-system-prompt
    # / positional）进入 cmd，故此处不再另写 PROMPT 段——此前两处逐字重复占
    # 文件 40–43%（§3.5-P8）。shlex.quote 逐元素引用 → CMD 是**可真行级 grep**
    # 的单行（含空格/换行的 prompt 值不会把记录撕成多行）。
    with open(os.path.join(log_dir, f"{agent}-{int(time.time())}.txt"), "w") as f:
        f.write("CMD: " + " ".join(shlex.quote(a) for a in cmd) + "\n")
    t0 = time.monotonic()
    log(agent, f"唤醒 pi (session={sid})")
    _lock_git(workdir)
    try:
        r = _run_wake_proc(cmd, spawn_cwd, workdir, agent)
    finally:
        restore_git_lock(workdir)

    # 进程事实登记（§3.2）：elapsed_ms 与 rc **无家**（session 首个 entry
    # 之前是黑箱、rc 只有 Popen 知道）→ 在数据已在手处就地捕获，零解析。
    # rc 总是写（零成本、权威）；elapsed_ms 用 monotonic 差值，跨度 = **pi
    # 进程生命周期**（spawn → exit，与 wake prompt 跨度/墙钟都不同口径）。
    _log_wake_done(agent, sid, r, int((time.monotonic() - t0) * 1000))
    new_sid = parse_session(r.stdout) or sid
    if new_sid:
        save_session_id(workdir, agent, new_sid)
    if r.returncode != 0:
        # 通用诊断（**只落盘、不解析、不作分支**）：rc≠0 时 stderr 此前只被读来
        # 判断“是不是 session 失效”，其余丢弃——诊断信息本可留在 loop 日志。
        # 截断 200 字符（先例 meeting_fs 的 [:200]）并**显式标注截断口径**。
        # 注意：这**不**解决“MCP server 静默失效”——那种情形 rc=0、stderr 为空
        # （上游 notify 在非交互模式是 no-op），见 design.md 决策 20。
        err_txt = (r.stderr or "").strip().replace("\n", " ⏎ ")
        if err_txt:
            log(agent, f"pi stderr（截断 200 字符）: {err_txt[:200]}")
        # 常见可重试失败：session 文件损坏/不存在。pi 对 --session-id
        # 通常自动创建；保留 stderr 日志便于诊断。明确 "No session
        # found" 则清空 status 后下轮新建。
        if "No session found" in (r.stderr or "") or "Session not found" in (r.stderr or ""):
            log(agent, "唤醒失败（session 无效）——清空重试")
            sp = os.path.join(base, f"status-{agent}.json")
            if os.path.exists(sp):
                os.remove(sp)
    return new_sid, r.returncode


def _log_wake_done(agent, sid, r, elapsed_ms):
    """唤醒完成行（登记字段 + ISO8601 时间戳）。

    §3.2 契约：`elapsed_ms` = pi 进程生命周期跨度；`rc` = 进程返回值（权威、
    总是写）。超时/被 kill 路径走异常分支（本函数不执行）——**缺席 ≠ 0**：
    没有值就不写字段，读侧按 n/a 处理。
    时间戳升级为 ISO8601（含日期）：秒级 `HH:MM:SS` 无法跨天 join，也无法
    与 session/commit 时间对齐（§3.5-P10）。
    """
    log(agent, f"pi 完成（session={sid} elapsed_ms={elapsed_ms} "
               f"rc={r.returncode}）")


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


def make_responder(extension_policy, fork_source=None, fork_cwd=None,
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
            result_path = os.path.join(workdir, meeting_fs.RESULT_MD)
            prompt = (f"讨论已收敛（{reason_txt}）。"
                      f"请写 result.md 到 {result_path}，总结讨论结论。")
            if retry:
                prompt = (f"你上一次被唤醒但未生成有效的 result.md。"
                          f"请现在写 result.md（非空，总结讨论结论）到 {result_path}。")
            if mem_available_mb() < MIN_MEM_MB:
                log(agent, "内存不足——抛可恢复异常（不代写 freezing，下轮重试）")
                raise RecoverableWakeError("内存不足")
            wake_llm(workdir, agent, prompt, extension_policy,
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
        wake_llm(workdir, agent, prompt, extension_policy,
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
              "[--max-meeting N] [--max-rr N] [--stall-timeout S]"
              " [--extension-policy none|mc-tools|all]")
        sys.exit(1)
    workdir, agent = sys.argv[1], sys.argv[2]
    recover_git_lock(workdir, agent)
    # 扩展策略：CLI 先填，协议字段（若有）覆盖——与 forkMode 同款
    extension_policy = meeting_fs.DEFAULT_EXTENSION_POLICY
    if "--extension-policy" in sys.argv:
        i = sys.argv.index("--extension-policy")
        if i + 1 < len(sys.argv):
            extension_policy = sys.argv[i + 1]
    mm, mr, st = 10, 7, 600
    # 协议从 **bare HEAD** 读（单一来源 = 共享事实；本地副本 LLM 可改）——
    # 与 engine participants()/check_status 同一原语
    bare = meeting_fs.bare_of_workdir(workdir)
    proto = meeting_fs.read_protocol(bare)
    if not proto:
        print(f"[fatal] protocol.json 读取失败（bare HEAD 无有效内容）: {bare}",
              flush=True)
        sys.exit(1)
    if proto.get("extensionPolicy"):
        extension_policy = proto["extensionPolicy"]
    if extension_policy not in meeting_fs.EXTENSION_POLICIES:
        # 值域守卫（与 forkMode 同款四层守卫之一：loop 门）——非法值不进 engine
        # 重试路径，直接 fatal 退出（配置错误就该在启动时响）
        print(f"[fatal] 非法 extensionPolicy: {extension_policy!r}"
              f"（合法值: {'/'.join(meeting_fs.EXTENSION_POLICIES)}）", flush=True)
        sys.exit(1)
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
                   make_responder(extension_policy,
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
