"""meeting 模式文件/git 操作层（I/O 封装）——meeting_core 的配套。

职责三类（设计原则 RR 教训）：
- 纯逻辑在 meeting_core（无 I/O），本模块只做文件/git 操作
- 每 commit 一条消息（约束 3.1）：commit 顺序 = 消息顺序
- 读取点从消息链 seen_at 推导（无本地游标状态）
- 所有 git 操作用 subprocess（与生产 local_loop 一致）
- **session fork 源生成/裁剪**（build_fork_source 与配套的 _curate/_fold/
  append_handoff_turns）：读主 session 文件、写 fork 源、注入切换叙事
  ——session 文件格式知识只留在本模块，调用方（meeting_loop）不内联解析
"""

import json
import os
import uuid
import re
import subprocess
import time
from datetime import datetime, timezone

# ---------------------------------------------------------------
# git 基础操作
# ---------------------------------------------------------------

def run_git(workdir, *args, check=True, timeout=30):
    """执行 git 命令。

    -c core.quotepath=false：所有输出（log/ls-tree/diff 的路径）原样
    UTF-8 不带引号转义——中文视角名（viewers 核心）下 quotepath 默认
    转义会让路径解析全部失效（is_message_file 不匹配带引号路径 → 消息
    读不到 → 无限首启/RR 死锁，2026-09-09 实测）。一处统一，全部命令
    生效（各调用点的 -z 双保险）。
    """
    r = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args], cwd=workdir,
        capture_output=True, text=True, timeout=timeout
    )
    if check and r.returncode != 0:
        raise RuntimeError(f"git {args} 失败: out={r.stdout.strip()[:200]!r} err={r.stderr.strip()[:200]!r}")
    return r


def read_protocol(bare):
    """读取共享协议（`HEAD:protocol.json`）——**协议读取的唯一实现**。

    为什么是单一来源、且必须走 bare：protocol.json 是**共享事实**
    （loop/engine/viewer/wrapper 都依据它），只有 setup 写过一次并
    commit+push 进 bare；work-<agent> 下的本地副本是工作副本，LLM 有
    bash 工具可以改动它——若判定读本地，LLM 就能影响流程判定。
    走 bare HEAD = 判定只认已提交事实（与"状态从 git 共享事实推导"一致）。

    任何失败（bare 不存在 / 无 HEAD / 文件缺失 / JSON 坏）→ 返回 {}：
    调用方各自决定失败语义（loop 门 `[fatal]`、engine 响亮抛错、
    viewer 显示"未初始化"）——原语本身不做政策。
    """
    r = run_git(bare, "show", "HEAD:protocol.json", check=False)
    if r.returncode != 0:
        return {}
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def git_head(workdir):
    """当前 HEAD。"""
    return run_git(workdir, "rev-parse", "HEAD").stdout.strip()


def git_pull(workdir):
    """pull（--rebase 自动处理分叉）。"""
    return run_git(workdir, "pull", "--rebase", "--autostash", check=False)


def git_commit(workdir, files, subject):
    """提交指定文件（每 commit 一条消息约束）。

    files: list[相对路径]
    subject: commit subject
    """
    run_git(workdir, "add", "--", *files)
    run_git(workdir, "commit", "-m", subject)


def git_push(workdir):
    """push，带并发容错：非快进失败 → pull --rebase → 重推。

    meeting 并发写场景：多个 agent 可能同时 push（如同时写 af），
    后到者 push 非快进失败——必须 pull 合并后重推，保证消息进 bare。
    check=False 静默吞失败会导致消息卡在本地 → 共享事实（bare）
    不完整 → 收敛死锁（复现现场：全员 af 卡死，can_start_rr 永不满足）。
    """
    for attempt in range(5):
        r = run_git(workdir, "push", check=False)
        if r.returncode == 0:
            return r
        # 非快进（并发别人先推）→ pull --rebase 合并 → 重推
        run_git(workdir, "pull", "--rebase", "--autostash", check=False)
        time.sleep(0.2 * (attempt + 1))
    # 重试耗尽：抛异常（不再静默/仅打印——消息滞留本地会让 bare 不完整，
    # 收敛死锁。审核 E。统一由 agent_loop 顶层异常边界处理。）
    raise RuntimeError(
        f"git_push 重试耗尽: {r.stdout.strip()} {r.stderr.strip()}")


def git_ls_files(workdir, agent_dir):
    """列出某 agent 目录下已提交的消息文件（含序号排序）。

    -z：git 默认转义非 ASCII 路径（quotepath，中文路径输出成
    "\\346\\200..." 带引号）——中文视角名（viewers 核心）会拿到
    带引号路径 → read_message 读不到 → list_my_messages 恒空 → is_first
    恒 True → 无限首启（2026-09-09 实测：freezing 卡死根因）。-z
    （NUL 分隔）不做引号转义，输出原始 UTF-8 路径。
    """
    r = run_git(workdir, "ls-files", "-z", agent_dir, check=False)
    out = r.stdout.rstrip("\0")
    return sorted(out.split("\0")) if out else []


def git_show(workdir, commit, path):
    """读取某 commit 中某文件的内容。"""
    r = run_git(workdir, "show", f"{commit}:{path}", check=False)
    if r.returncode != 0:
        return None
    return r.stdout


# ---------------------------------------------------------------
# frontmatter 解析
# ---------------------------------------------------------------

def _frontmatter_end(content):
    """frontmatter 块闭合行号（开 `---` + 闭 `---` 完整才返回；否则 None）。

    块边界确认**只此一份**（parse_frontmatter / extract_body 共用）——
    先边界后解析（review5 A1 根治，用户方法：先确认块边界与完整性再读取）。
    """
    if not content.startswith("---"):
        return None
    lines = content.splitlines()
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return i
    return None


def parse_frontmatter(content):
    """先确认 frontmatter 块完整（开 `---` + 闭 `---`），再解析字段
    （review5 A1 根治，用户方法：先边界后解析）。

    返回 None = 无 frontmatter 或块不完整（不可用）——调用方据此跳过
    （读路径）或删除重写（写路径 commit_new_files）。
    块完整 → dict（字段可信，不会把正文当字段吞掉）。

    旧实现"只查开头、遍历到文件尾"：缺闭合 `---` 时返回部分解析结果
    （1-2 字段）→ 调用方 if not fm 判不出"完整 vs 残缺"→ 误当成功
    → serialize None → 原样 commit → 确定性修复丢失（A1 根因）。
    """
    end = _frontmatter_end(content)
    if end is None:
        return None          # 无 frontmatter 或块不完整 → 不可用
    lines = content.splitlines()
    fm = {}
    for line in lines[1:end]:
        m = re.match(r"^([a-zA-Z_]+):\s*(.*)$", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if val.startswith('"') and val.endswith('"') and len(val) >= 2:
                val = val[1:-1]   # 对齐 serialize_message 的剥引号（审核#17）
            fm[key] = val
    return fm


def extract_body(content):
    """frontmatter 块之后的正文（块不完整 → None）。

    与 parse_frontmatter 共用 _frontmatter_end（块边界只此一份）。
    human_viewer 展示用（读 bare 内容，非工作区文件）。
    """
    end = _frontmatter_end(content)
    if end is None:
        return None
    lines = content.splitlines()
    return "\n".join(lines[end + 1:]).strip()


def remove_message(workdir, path):
    """删除工作区文件（原语；调用方决定政策——engine 删除无效消息文件）。

    fs 只做"删一个文件"的机械动作；为什么删、删了之后等谁重写，
    都是 engine 的判定职责（L16 边界：IO 归 fs）。
    """
    os.remove(os.path.join(workdir, path))


def write_text(workdir, path, text):
    """写文本文件（原语；原子性不做——调用方随后 commit 才是权威化点）。"""
    with open(os.path.join(workdir, path), "w") as f:
        f.write(text)


def file_size(workdir, path):
    """文件字节数；不存在/不可读 → -1（调用方按"无效"处理）。

    合并"存在性 + 大小"两次系统调用为一个判定接口：调用方（result.md
    有效性校验）原本是 exists() + getsize() 两步，两步之间文件可能变化。
    """
    try:
        return os.path.getsize(os.path.join(workdir, path))
    except OSError:
        return -1


def cat_batch(bare, paths):
    """`git cat-file --batch` 批量读（一次进程读多个 rev:path）——批量读取原语。

    **必须二进制模式读**（用户 9343 现场修复）：cat-file 的 size 是**字节数**，
    text 模式 read(size) 读**字符数**——中文 UTF-8 3 字节/字符 → 错位 →
    后续 header 全乱 → readline 阻塞等数据 → 挂起死锁（真实 LLM 讨论中文，
    FakeAgent 测试 ASCII 单字节所以本地测试没抓到——R1 根因复发）。
    异常安全：try/finally 保证 stdin.close() + wait()（异常不泄漏进程，
    否则 cat-file 常驻等 stdin EOF——top 3 个常驻进程即死锁现场）。

    返回 {path: content}。为什么保留批量形态：调用方每轮循环顶都要读全部
    消息（O(n) 次 git_show = 16.8× 回归，实测 733ms vs 43.6ms@498 条）。
    """
    if not paths:
        return {}
    proc = subprocess.Popen(
        ["git", "-C", bare, "cat-file", "--batch"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
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


def read_message(workdir, path):
    """读消息文件（工作区），返回 (frontmatter, body)。"""
    full = os.path.join(workdir, path)
    if not os.path.exists(full):
        return None, None
    with open(full) as f:
        content = f.read()
    return parse_frontmatter(content), content


def _fm_to_lines(frontmatter):
    """frontmatter → 行列表（review5 F4：write_message 与 serialize_message
    共用清洗规则——值单行化 + 剥引号，避免两处实现漂移）。"""
    lines = ["---"]
    for k, v in frontmatter.items():
        s = str(v).replace("\n", " ").replace("\r", " ").strip()
        if s.startswith('"') and s.endswith('"') and len(s) >= 2:
            s = s[1:-1]
        lines.append(f"{k}: {s}")
    lines.append("---")
    return lines


def write_message(workdir, path, frontmatter, body):
    """写消息文件（frontmatter + body）。"""
    full = os.path.join(workdir, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write("\n".join(_fm_to_lines(frontmatter)) + "\n\n" + body + "\n")


def serialize_message(frontmatter, original_content):
    """替换原文件 frontmatter 部分（保留 body），返回新内容。

    original_content: 原文件全文
    返回: 新全文（frontmatter 按给定 dict 重写，body 保留）

    边界语义与 parse_frontmatter **不同**（刻意分离，用户 2026-08-30）：
    原实现用 `lines[0].strip() != "---"`（允许前导空格），_frontmatter_end
    是 `startswith`（严格）——统一会改变 serialize_message 的行为（核心
    写路径，commit_new_files 用），保持各自原语义。
    """
    lines = original_content.splitlines()
    # 找到第一个 --- 和第二个 ---
    if not lines or lines[0].strip() != "---":
        return None
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return None
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    return "\n".join(_fm_to_lines(frontmatter)) + "\n\n" + body + "\n"


# ---------------------------------------------------------------
# 消息目录操作
# ---------------------------------------------------------------

def list_my_messages(workdir, agent_dir):
    """列出某 agent 的全部消息（含 frontmatter），按序号升序。"""
    paths = git_ls_files(workdir, agent_dir)
    msgs = []
    for p in paths:
        fm, _ = read_message(workdir, p)
        if fm:
            msgs.append(fm)
    return msgs


def next_msg_id(workdir, agent_dir):
    """下一个消息序号（已有消息最大序号 + 1，4 位补零）。

    用 max+1 不用 len+1：LLM 跳号（写 0005 但只有 0001-0003）时
    len+1=4 会与已写序号错位，且写流程信号时可能覆盖 LLM 的跳号
    消息（审核 G4）。max+1 根治。
    """
    paths = git_ls_files(workdir, agent_dir)
    nums = [int(p.split("/")[-1].split(".")[0]) for p in paths
            if p.split("/")[-1][:4].isdigit()]
    n = (max(nums) if nums else 0) + 1
    return f"{n:04d}"


def commit_message(agent, msg_id):
    """commit subject 格式。"""
    return f"discuss: {agent}/{msg_id}"


# ---------------------------------------------------------------
# 读取点（从消息链推导，无本地状态）
# ---------------------------------------------------------------

def setup_commit(workdir):
    """讨论起点 = 仓库根 commit（setup 提交，question.md 诞生点）。

    用途：无历史 agent（从未 responder 成功）写协议信号的 seen_at 兜底——
    固定锚点，从起点读一条不漏。head 兜底会随并发 push 漂移，把从未
    读过的消息虚假标记已读（实测 2026-09-03 crash_recovery flaky 根因）。
    """
    r = run_git(workdir, "rev-list", "--max-parents=0", "HEAD")
    return r.stdout.strip()


def read_point(workdir, agent_dir):
    """读取点 = 我最后一条参与消息的 seen_at（跳 freezing）。

    返回: str（"" = 从根读起）
    """
    from meeting_core import read_point_seen_at
    msgs = list_my_messages(workdir, agent_dir)
    return read_point_seen_at(msgs)


def list_new_messages(workdir, since_ref):
    """since 之后的新消息文件（路径列表）。

    since_ref: git ref（"" = 全部）
    返回: list[str]
    """
    if not since_ref:
        # 无读取点：列出全部消息文件
        r = run_git(workdir, "ls-tree", "-r", "-z", "--name-only", "HEAD", check=False)
        files = r.stdout.rstrip("\0").split("\0") if r.stdout.strip() else []
    else:
        r = run_git(workdir, "diff", "-z", "--name-only",
                    f"{since_ref}..HEAD", check=False)
        files = r.stdout.rstrip("\0").split("\0") if r.stdout.strip() else []
    # 只保留消息文件（作者目录/NNNN.md）
    return [f for f in files if is_message_file(f)]   # P7：统一（L8 漏 fs 这处）


def new_messages_with_meta(workdir, since_ref, me=None):
    """新消息（带元数据）：path / from / to / stale。

    阶段 4：seen_at 陈旧检测（设计文档 3.3）——对每条消息标注
    "该消息的 seen_at 之后是否有更新"（git diff --name-only seen_at..HEAD 非空 = 陈旧）。

    me: 本 agent 名——提供时过滤自己的消息（审核#15：build_wake_prompt
    不列自己刚 commit 的消息，避免 prompt 噪音 + token 浪费；触发判定
    本身已过滤 from==me，不受影响；list_new_messages 保持全量语义）。
    since_ref: 读取点（git ref）
    返回: list[dict]：{path, from, to, seen_at, stale}
    """
    paths = list_new_messages(workdir, since_ref)
    result = []
    for p in paths:
        fm, _ = read_message(workdir, p)
        if not fm:
            continue
        if me is not None and fm.get("from") == me:
            continue
        seen = fm.get("seen_at", "")
        stale = False
        if seen:
            # 陈旧 = 该消息的 seen_at 之后有**其他**消息更新（commit 拓扑序）。
            # 注意：作者写消息时取 seen_at，自身 commit 紧随其后——自身 commit
            # 不算"后续更新"。判定：seen_at..HEAD 的 diff 中，除本消息外还有
            # 其他消息文件（别人的新消息或更新）→ stale。
            r = run_git(workdir, "diff", "-z", "--name-only",
                        f"{seen}..HEAD", check=False)
            changed = (r.stdout.rstrip("\0").split("\0")
                       if r.stdout.strip() else [])
            others = [f for f in changed
                      if is_message_file(f) and f != p]
            stale = bool(others)

        result.append({
            "path": p,
            "from": fm.get("from", p.split("/")[0]),
            "to": fm.get("to", "all"),
            "seen_at": seen,
            "stale": stale,
        })
    return result


def is_message_file(path):
    """判断路径是否为消息文件（作者/NNNN.md）。

    作者目录不限 ASCII：viewers 中文视角名是产品核心（可读性/性能/…）。
    约束对齐 agent 名校验（禁 / 与空白；目录名不匹配即非消息文件——
    human/、protocol.json 等天然排除）。
    """
    return bool(re.match(r"^[^/\s]+/\d{4}\.md$", path))


def parse_log_nameonly(output):
    """解析 `git log --name-only --format=%H` 输出 → [(commit, [files])]。

    按 commit 拓扑序（输出顺序）；每个 commit 的文件列表含其变更文件。
    引擎（rr_next_speaker 回退路径）与 human_viewer（new_messages）共用
    ——git 输出解析只此一份，避免两处实现漂移。
    """
    commits = []
    cur = None
    files = []
    for line in output.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if len(line) == 40 and all(c in "0123456789abcdef" for c in line):
            if cur is not None:
                commits.append((cur, files))
            cur, files = line, []
        else:
            files.append(line)
    if cur is not None:
        commits.append((cur, files))
    return commits

# ---------------------------------------------------------------
# fork 源生成与裁剪（session 文件层）
# ---------------------------------------------------------------
#
# budget 模式参数（fork 源裁剪）
#
# fork 源模式（**值集合与默认值的单一事实源**）：
#   budget     —— 预算 + 折叠（默认；长会话唯一可行）
#   compaction —— 按 compaction 边界（中小会话，内容原样）
#   full       —— 全量（小会话/验证）
# 两种语义角色（勿混）：FORK_MODES 是**处理哪个模式**（分派仍用字面量）；
# DEFAULT_FORK_MODE 是**缺省填谁**（仅默认值位置，全仓引此常量）。
# 历史值 active/curated（rename 前）按非法值处理（解析入口即报错）。
FORK_MODES = ("budget", "compaction", "full")
DEFAULT_FORK_MODE = "budget"
#
# 版本守卫：值域校验在 build_fork_source 入口（open 之前）做——非法值
# 与历史值（改名前的 active/curated）一律报错，绝不静默落到"混合分支"
# （曾实测：非法值走"边界后全量、不折叠、无预算"→ 930k tokens 超窗，
# 且被 engine 的异常边界吞成廉价重试）。
#
# 预算取值（口径见设计文档「规模口径」节）：
# - 现行重估公式（**事后归纳，非原始设计目标**）：基线 ≤ 约 20%×
#   （模型窗口 − 输出预留）；当前实例 80k est ≈ 132k 真实 ≈ 664k 的 20%。
# - 校准比（est → 真实）：唤醒 1 ≈ 1.66×；唤醒中后期至 ~2.7×（不固化为 2.0×）。
# - 该预算只约束 **fork 源（基线）**；运行规模随该 agent 会话累积增长
#   （实测 w1 132k → w5 192–207k）。
# - pi 默认 compaction（keepRecentTokens=20000 / reserveTokens=16384，
#   见 pi DEFAULT_COMPACTION_SETTINGS）——讨论 agent 需要更宽的近期窗口。
BUDGET_KEEP_TOKENS = 80000
_TOOL_RESULT_KEEP = 8          # 最近 N 条工具输出保留（其余省略）
_TOOL_RESULT_MAX_CHARS = 4000  # 保留范围内单条工具输出上限
_TOOL_CALL_MAX_CHARS = 1200    # 工具调用参数上限


def read_fork_stats(path):
    """读 fork 源 header 的统计字段。

    只读首行（O(1)，与产物大小无关）。日志所需的 mode/估算/丢弃数取自
    **同一来源**（产物自描述 header）
    ——header 格式知识只留本模块，调用方不内联解析（条数由构建返回值提供）。
    读失败返回 {}（调用方降级为只打模式/条数，不阻塞首唤）。
    """
    try:
        with open(path, encoding="utf-8") as f:
            hdr = json.loads(f.readline())
    except (OSError, ValueError):
        return {}
    if not isinstance(hdr, dict) or hdr.get("type") != "session":
        return {}
    return {
        "mode": hdr.get("forkSourceMode") or "",
        "est": hdr.get("forkSourceTokensEst"),
        "dropped": hdr.get("forkSourceDropped"),
    }


def _est_tokens(text):
    """粗估 token 数（≈3 字符/token）——仅用于预算裁剪。

    无文本条目（如 compaction）按 1 token 计——量级 <0.01%，不构成
    容量风险；est 只是集合近似指纹，不是数值承诺（见 I4）。
    """
    return max(1, len(text) // 3)


def _entry_text(entry):
    """条目的文本贡献（预算估算用；不追求精确，只求可比较）。"""
    m = entry.get("message")
    if not isinstance(m, dict):
        return ""
    c = m.get("content")
    if isinstance(c, str):
        return c
    if not isinstance(c, list):
        return ""
    parts = []
    for x in c:
        if not isinstance(x, dict):
            continue
        t = x.get("type")
        if t == "text":
            parts.append(x.get("text") or "")
        elif t == "thinking":
            parts.append(x.get("thinking") or "")
        elif t == "toolCall":
            parts.append(json.dumps(x.get("arguments", {}), ensure_ascii=False))
    return "\n".join(parts)


def _shrink_value(v, limit):
    """递归截断参数里的长字符串（保留结构/键名——工具调用 JSON 必须
    仍是合法对象：部分 provider 对回放的历史 toolCall 参数有形状要求，
    换成一个 {"_truncated": …} 对象会改变参数形状）。"""
    if isinstance(v, str):
        return v if len(v) <= limit else v[:limit] + f"…[截断，原 {len(v)} 字符]"
    if isinstance(v, dict):
        return {k: _shrink_value(x, limit) for k, x in v.items()}
    if isinstance(v, list):
        return [_shrink_value(x, limit) for x in v]
    return v


def _fold_entry(entry, full_result):
    """折叠单条目（budget）：返回新条目（无改动则原样返回）。

    - thinking 块整体丢弃——推理痕迹不承担信息职责，但占原始文本约 40%
    - toolCall 参数超限截断（保留名字与开头，够辨认“做了什么”）
    - 工具输出：仅最近若干条保留全文（且单条限长），更早的换为省略标记
      （长度信息保留，便于判断“曾发生过什么”）
    """
    m = entry.get("message")
    if not isinstance(m, dict):
        return entry
    content = m.get("content")
    if not isinstance(content, list):
        return entry
    role = m.get("role")
    out, changed = [], False
    for x in content:
        if not isinstance(x, dict):
            out.append(x)
            continue
        t = x.get("type")
        if t == "thinking":
            changed = True
            continue
        if t == "toolCall":
            args = x.get("arguments", {})
            shrunk = _shrink_value(args, _TOOL_CALL_MAX_CHARS)
            if shrunk != args:
                n = dict(x)
                n["arguments"] = shrunk
                out.append(n)
                changed = True
            else:
                out.append(x)
            continue
        if t == "text" and role == "toolResult":
            txt = x.get("text") or ""
            if not full_result:
                n = dict(x)
                n["text"] = f"[工具输出已省略 {len(txt)} 字符]"
                out.append(n)
                changed = True
                continue
            if len(txt) > _TOOL_RESULT_MAX_CHARS:
                n = dict(x)
                n["text"] = (txt[:_TOOL_RESULT_MAX_CHARS] +
                             f"\n…[工具输出截断，共 {len(txt)} 字符]")
                out.append(n)
                changed = True
                continue
        out.append(x)
    if not changed:
        return entry
    if not out:
        out = [{"type": "text", "text": "[内容已省略]"}]
    n = dict(entry)
    nm = dict(m)
    nm["content"] = out
    n["message"] = nm
    return n


def _normalize_entries(entries):
    """移除窗口内的 compaction 条目，并把指向它们的 parentId 上溯桥接。

    为什么必须移除（pi replay 语义，2026-09-10 实测）：replay 以**路径上
    最后一个 compaction** 的 `firstKeptEntryId` 为起点——窗口内若有
    compaction 且它不是窗口首条，则锚之前的条目会被**静默丢弃**
    （实测产物 [preface, old, k1, k2, C1, n0..n2] → 可见仅 [C1, n0..n2]，
    我们的 preface 也被丢掉）。移除后窗口内无锚，replay 从链首开始 =
    全集合可见（不变量 I4：声明集合 == replay 可见集合）。

    桥接规则：
      - 被移除 compaction 的子条目 → parentId 上溯到第一个非 compaction
        祖先（连续 compaction 链也正确）
      - 上溯出产物（祖先被裁剪）→ 链首显式 `parentId=None`（与 preface 同型）

    **为什么连退化情形也要移除**（e2e11 实测 + I4 测试推翻初版守卫）：
    初版守卫“窗口仅含 compaction → 保留该条”会破坏 I4——保留的 comp
    仍带 `firstKeptEntryId`，而其锚点早已被预算裁掉 → replay 仍会丢弃
    锚点之前的全部条目（含我们的 preface）。因此一致地移除全部 comp；
    空 body 由调用方以 preface（“已省略…”说明）兼顾，产物不会为空。

    信息损失有界：最后一条 compaction 的摘要已在 preface（"此前压缩
    摘要"行）；更早的是被滚动摘要覆盖的旧摘要。

    返回 (new_entries, removed_n)。
    """
    comps = [e for e in entries if e.get("type") == "compaction"]
    if not comps:
        return entries, 0
    removed = {e.get("id"): e for e in comps if e.get("id")}
    out = []
    for e in entries:
        if e.get("type") == "compaction":
            continue
        pid = e.get("parentId")
        while pid in removed:          # 上溯桥接（可能连续多个）
            pid = removed[pid].get("parentId")
        out.append(dict(e, parentId=pid))
    # 上溯出产物（祖先被预算裁掉）→ 链首显式 None
    ids = {e.get("id") for e in out}
    out = [e if e.get("parentId") in ids else dict(e, parentId=None)
           for e in out]
    return out, len(comps)


def _budget_entries(entries, keep_tokens, summary=""):
    """budget 裁剪流水线的后半段（前置在 build_fork_source：边界选取）。

    流水线：**fold → trim → align → normalize**；preface 与 est 由调用方
    在规范化后施加（顺序不可换——est 必须反映最终产物）。

    返回 **(preface 文本, kept 条目, 预算丢弃数, 规范化移除数)**——两个
    计数分开回报，因为 header 的 `forkSourceDropped` 是两者之和（不变量
    I5 记账闭合），而 preface 文本只描述预算省略部分。
    """
    # ① fold：折叠（最近 _TOOL_RESULT_KEEP 条工具输出保留全文）
    seen, folded = 0, []
    for e in reversed(entries):
        full = True
        if (e.get("message") or {}).get("role") == "toolResult":
            seen += 1
            full = seen <= _TOOL_RESULT_KEEP
        folded.append(_fold_entry(e, full))
    folded.reverse()
    # ② trim：从尾部累计**折叠后**规模（顺序很重要：按原始规模裁会把
    #    预算浪费在随后就被折叠掉的内容上——实测 80k 预算只落到 17.5k）
    cut, acc, i = len(folded), 0, len(folded) - 1
    while i >= 0:
        t = _est_tokens(_entry_text(folded[i]))
        if cut < len(folded) and acc + t > keep_tokens:
            break
        acc += t
        cut = i
        i -= 1
    # ③ align：provider 硬约束（tool 消息必须有其 tool_calls 前置）
    #  - 保留区以 toolResult 开头 → 向前扩展，把它的 toolCall 一起纳入
    #  - 扩展到顶仍是孤儿（源本身不完整）→ 丢弃这些无主条目
    # （DeepSeek/OpenAI 直接报 "Messages with role 'tool' must be a
    #  response to a preceding message with 'tool_calls'"，2026-09-10 实测）
    # 两段式：先向前扩展（cut 只减），再清理头部孤儿（cut 只增，必然终止）
    while cut > 0 and (folded[cut].get("message") or {}).get("role") == "toolResult":
        cut -= 1
    while folded[cut:] and \
            (folded[cut].get("message") or {}).get("role") == "toolResult":
        cut += 1
    # 计数在**对齐之后**按最终切点重算（曾按对齐前的切点绑定进 dropped，
    # 回扩条目会同时留在 kept 与 dropped → 双重计数，实测场景 B/C）
    kept = folded[cut:]
    dropped = folded[:cut]
    # ④ normalize：移除窗口内 compaction 并桥接（见 _normalize_entries）
    kept, removed_n = _normalize_entries(kept)
    dropped_tokens = sum(_est_tokens(_entry_text(e)) for e in dropped)
    preface = ""
    # 规范化可能把 body 清空（窗口只容下了 compaction）——此时必须给出
    # preface 作为内容（否则产物只剩 header；且它正是那段历史的交代）
    if dropped or summary or not kept:
        lines = [
            "[上下文说明] 本次分析的上下文来自主 pi 会话，"
            "过早的历史已按预算压缩。",
        ]
        if dropped:
            lines.append(
                f"已省略更早的 {len(dropped)} 条消息（约 {dropped_tokens // 1000}k tokens）。")
        if summary:
            lines.append(f"此前压缩摘要：{summary.strip()}")
        preface = "\n".join(lines)
    return preface, kept, len(dropped), removed_n


def build_fork_source(src_session, out_path, new_id, new_cwd,
                      mode=DEFAULT_FORK_MODE, keep_tokens=None):
    """生成 fork 源 session 文件——供 wake_llm 首唤 `--session` 直接打开
    （不用 `pi --fork`：那是一份全量拷贝，且我们需在尾部注入切换叙事）。

    三种裁剪策略（header 的 forkSourceMode 标记可核查）：
      budget：**预算 + 折叠**（默认）——恢复 pi 自身的压缩不变量（摘要 +
        最近窗口）；**构建期**不依赖任何扩展（运行期上下文仍受环境扩展
        的渲染期裁剪影响）。为什么需要：主 pi 实际发送的上下文
        比文件条目小得多（压缩层不在条目里）——实测本会话原始条目
        919k/934k tokens，加 384k completion 预留 > 1M 窗口。
        compaction/full 都是条目级裁剪、无总量上限，对长会话不够。
      compaction：从最后一条 compaction 的 `firstKeptEntryId` 起的条目
        （**含该 compaction 条目本身，位于其自然位置**）——主 session 的
        **条目级**压缩态，内容原样保留（thinking/工具输出不折叠）；
        锚点 ID 不在源中 → 明确报错
      full：全部条目（含无 compaction 的引导 session）
    budget 与 compaction 在**无 compaction 时**均全量（引导 session 本就
    干净无先例）；budget 仍会跑折叠与统计。

    产物不变量（I1–I5；由构造保证 + 测试断言，**不做生产守卫**——判定
    依据是复杂度匹配：失败路径设计与归因的成本高于"用检查代替构造纪律"）：
      I1 产物内 id 唯一
      I2 链连续：从末条上溯可覆盖**全部**条目；除链首外 parentId 均指向
         产物内条目（链首显式；compaction 模式下链首的父在窗口外属边界
         语义——replay 走到此处即停）
      I3 replay 所用锚点（路径上最后一个 compaction 的 firstKeptEntryId）
         指向产物内条目
      I4 **声明集合 == pi replay 可见集合**（budget/compaction 两个**窗口
         构造**模式；budget 据此移除窗口内 compaction——否则 replay 会丢弃
         锚点之前的条目，含 preface，实测 2026-09-10）。
         full 模式**不适用**：它是源的忠实拷贝，replay 可见集合与**源会话
         自身**语义一致（源里有什么 compaction 就有什么可见性边界），
         我们不做窗口构造，也不改写历史。
      I5 记账闭合：源保留区条目数 = 产物非 preface 条目数 +
         `forkSourceDropped`（= 预算丢弃数 + 规范化移除数）

    产物 header 自描述（单一来源）：forkSourceMode /
    forkSourceTokensEst（估算基准，**不预测请求规模**）/ forkSourceDropped。
    keep_tokens：budget 的预算（默认 BUDGET_KEEP_TOKENS）。
    返回 (entries_written, error)。
    """
    if mode not in FORK_MODES:
        # 配置错误就地暴露（不落盘、不生成半成品）——见常量块"版本守卫"
        return 0, f"未知 forkMode: {mode!r}（合法值: {'/'.join(FORK_MODES)}）"
    try:
        with open(src_session, encoding="utf-8") as f:
            entries = [json.loads(l) for l in f if l.strip()]
    except (OSError, ValueError) as e:
        return 0, f"源 session 读取失败: {e}"
    header = next((e for e in entries if e.get("type") == "session"), None)
    if header is None:
        return 0, "源 session 无 header"
    comps = [(i, e) for i, e in enumerate(entries)
             if e.get("type") == "compaction"]
    summary = ""
    new_ts = header.get("timestamp")
    idx_by_id = {e.get("id"): i for i, e in enumerate(entries) if e.get("id")}
    if mode == "full" or (mode == "compaction" and not comps):
        # full：全部条目。compaction 遇无 compaction 的源（引导 session）
        # 也全量——本就无边界可依（既有语义，标记 full）
        body = entries[1:]
        if mode == "compaction":
            mode = "full"
    elif mode == "compaction":
        # compaction：条目级压缩态，内容原样保留（thinking/工具输出不折叠）。
        # 产物 = entries[kept_idx:]——它**已含**最后一条 compaction 条目
        # （pi 的 appendCompaction 使锚点位置小于 comp 位置），“补一条
        # comp” 恒为重复且引入 last-wins 顺序依赖（e2e11 实测）
        comp = comps[-1][1]
        summary = comp.get("summary") or ""
        new_ts = comp.get("timestamp") or new_ts
        kept_idx = idx_by_id.get(comp.get("firstKeptEntryId"))
        if kept_idx is None:
            return 0, (f"firstKeptEntryId {comp.get('firstKeptEntryId')} "
                       f"不在源 session 中")
        body = entries[kept_idx:]
    elif mode == "budget":
        # budget：压缩态边界 → 折叠 → 预算裁剪 → 边界对齐 → 规范化
        if comps:
            comp = comps[-1][1]
            summary = comp.get("summary") or ""
            new_ts = comp.get("timestamp") or new_ts
            kept_idx = idx_by_id.get(comp.get("firstKeptEntryId"))
            if kept_idx is None:
                # 边界 ID 失效：从最后 compaction 条目之后取（容错）
                kept_idx = entries.index(comp) + 1
            body = entries[kept_idx:]
        else:
            body = entries[1:]
        preface, body, dropped_n, removed_n = _budget_entries(
            body, keep_tokens or BUDGET_KEEP_TOKENS, summary)
        if preface:
            pid = uuid.uuid4().hex[:8]
            body = [{
                "type": "message", "id": pid, "parentId": None,
                "timestamp": datetime.now(timezone.utc).isoformat().replace(
                    "+00:00", "Z"),
                "message": {"role": "user",
                            "content": [{"type": "text", "text": preface}]},
            }] + body
            # 保留区首条接回 preface（一条链；规范化已保证它不再指向
            # 窗口外）
            if len(body) > 1:
                body[1] = dict(body[1], parentId=pid)
        # 双口径合计（不变量 I5 记账闭合）：header 计“预算丢弃 + 规范化
        # 移除”，而 preface 文本只描述预算部分
        dropped_total = dropped_n + removed_n
    else:                                   # pragma: no cover
        # 值域守卫之后仍可达的只剩“新值已入 FORK_MODES 但分派未跟上”
        return 0, f"forkMode {mode!r} 尚未实现分派"
    new_header = {
        "type": "session",
        "version": header.get("version", 3),
        "id": new_id,
        "timestamp": new_ts,
        "cwd": new_cwd,
        "parentSession": src_session,
        "forkSourceMode": mode,
    }
    if mode == "budget":
        # 产物侧指纹（口径见设计文档「规模口径」）：对**最终产物**统一
        # 计算（单一测点）；不预测请求规模
        new_header["forkSourceTokensEst"] = sum(
            _est_tokens(_entry_text(e)) for e in body)
        new_header["forkSourceDropped"] = dropped_total
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(new_header, ensure_ascii=False) + "\n")
        for e in body:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return len(body) + 1, None

# 测试引导 session 登记日志（防误删，用户 2026-09-09）：每个测试/脚本
# 引导 session 创建时登记一行——清理时回查日志确认"是我建的测试产物"
# 才删，未登记 = 非本流程创建（真实会话），不得删。路径与
# scripts/check-residue.sh 的对照逻辑共享。
SESSION_REGISTRY = os.path.join(
    os.path.expanduser("~/.pi"), "pi-multi-viewers-test-sessions.log")


def _registry_log(session_id, out_path, cwd):
    """登记一条测试引导 session 记录（追加）。"""
    rec = {
        "created": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "path": os.path.abspath(out_path),
        "cwd": cwd,
    }
    try:
        with open(SESSION_REGISTRY, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 登记失败不阻塞引导创建（清理时该条会显示未登记 → 不删，安全侧）


def build_bootstrap(out_path, cwd, session_id=None):
    """生成空白引导 session 文件（零 LLM，替代 pi --print "就绪"）。

    用途（2026-09-09）：脚本/测试场景无主 session 时，给 meeting_loop 一
    个合法 fork 源。空 header + 零 message——agent 首唤 --session 打开后
    第一条 user 消息就是 wake prompt（任务描述，无"就绪"噪音 turn）。
    冒烟实测：仅 header 的文件可被 pi --session 正常打开续写。

    每次创建写登记日志（SESSION_REGISTRY）——清理回查归属（防误删）。

    返回 (out_path, error)。
    """
    sid = session_id or str(uuid.uuid4())
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    header = {"type": "session", "version": 3, "id": sid,
              "timestamp": ts, "cwd": cwd}
    try:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(header, ensure_ascii=False) + "\n")
    except OSError as e:
        return None, f"引导 session 写入失败: {e}"
    _registry_log(sid, out_path, cwd)
    return out_path, None

def preserve_result_md(base):
    """保存 result.md：从 bare git 历史复制到父级目录（T2 合并，e2e7 评审）。

    两个触发点共享本实现（此前 start_discussion/meeting_loop 各一份，
    日志格式/import 方式/边界处理三处漂移）：
      - resultWriter loop 退出（收尾完成时保存）
      - cleanup（清理前兜底保存）
    命名 <base目录名>-result.md（与讨论目录同级）。
    result.md 权威位置 = bare git 历史；无 result.md → 跳过（不报错）。
    返回保存路径或 None。
    """
    bare = os.path.join(base, "repo.git")
    if not os.path.isdir(bare):
        return None
    r = run_git(bare, "show", "HEAD:result.md", check=False)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    base_name = os.path.basename(base.rstrip("/")) or "discussion"
    dest = os.path.join(os.path.dirname(base.rstrip("/")) or ".",
                        f"{base_name}-result.md")
    with open(dest, "w") as f:
        f.write(r.stdout)
    return dest

def append_handoff_turns(session_file, turns):
    """session 尾部追加对话回合（切换叙事，2026-09-09 设计）。

    turns: [(role, text), ...]——按序追加，parentId 接到现有链尾，
    最简字段（role/content，无 provider/usage——pi 加载只关心角色与
    内容，冒烟实测通过）。
    用途：fork 源尾部注入"停止旧任务 → 新任务说明 → 确认"对话，
    显式切断历史叙事惯性（agent 读到的最后叙事是任务切换共识，
    无法再把自己当成旧叙事的延续）。
    返回追加条数。
    """
    with open(session_file, encoding="utf-8") as f:
        lines = [json.loads(l) for l in f if l.strip()]
    parent = None
    for e in reversed(lines):
        if e.get("id"):
            parent = e["id"]
            break
    n = 0
    with open(session_file, "a", encoding="utf-8") as f:
        for role, text in turns:
            eid = uuid.uuid4().hex[:8]
            e = {"type": "message", "id": eid, "parentId": parent,
                 "timestamp": datetime.now(timezone.utc).isoformat().replace(
                     "+00:00", "Z"),
                 "message": {"role": role,
                             "content": [{"type": "text", "text": text}]}}
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
            parent = eid
            n += 1
    return n