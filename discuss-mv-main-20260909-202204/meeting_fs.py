"""meeting 模式文件/git 操作层（I/O 封装）——meeting_core 的配套。

设计原则（RR 教训）：
- 纯逻辑在 meeting_core（无 I/O），本模块只做文件/git 操作
- 每 commit 一条消息（约束 3.1）：commit 顺序 = 消息顺序
- 读取点从消息链 seen_at 推导（无本地游标状态）
- 所有 git 操作用 subprocess（与生产 local_loop 一致）
"""

import os
import re
import subprocess
import time

# ---------------------------------------------------------------
# git 基础操作
# ---------------------------------------------------------------

def run_git(workdir, *args, check=True, timeout=30):
    """执行 git 命令。"""
    r = subprocess.run(
        ["git", *args], cwd=workdir, capture_output=True, text=True, timeout=timeout
    )
    if check and r.returncode != 0:
        raise RuntimeError(f"git {args} 失败: out={r.stdout.strip()[:200]!r} err={r.stderr.strip()[:200]!r}")
    return r


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
    """列出某 agent 目录下已提交的消息文件（含序号排序）。"""
    r = run_git(workdir, "ls-files", agent_dir, check=False)
    return sorted(r.stdout.strip().splitlines()) if r.stdout.strip() else []


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
        r = run_git(workdir, "ls-tree", "-r", "--name-only", "HEAD", check=False)
        files = r.stdout.strip().splitlines() if r.stdout.strip() else []
    else:
        r = run_git(workdir, "diff", "--name-only", f"{since_ref}..HEAD", check=False)
        files = r.stdout.strip().splitlines() if r.stdout.strip() else []
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
            r = run_git(workdir, "diff", "--name-only", f"{seen}..HEAD", check=False)
            changed = r.stdout.strip().splitlines() if r.stdout.strip() else []
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
    """判断路径是否为消息文件（作者/NNNN.md）。"""
    return bool(re.match(r"^[a-z]+/\d{4}\.md$", path))


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

