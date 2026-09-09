#!/usr/bin/env python3
"""human_viewer.py —— 只读展示讨论进展（pi-agents-helper 阶段 1）。

human 通道的展示进程（docs/pi-helper-design.md §5.2）：纯 bare 只读，
无写路径。增量输出 `--since <ref>` 之后的新消息 + 状态变化（mode 切换）。

用法:
  python3 human_viewer.py <base>                  # 当前状态 + 全部消息
  python3 human_viewer.py <base> --since <ref>    # 增量（ref 之后）
  python3 human_viewer.py <base> --follow         # 循环展示直到讨论结束

输出契约（稳定文本，供壳/主 pi 消费）：
  【状态】<mode>
  [<path>] <from> (<type>): <summary 若有>
  <正文>
  ---

--follow 模式：游标持久化 <base>/.viewer-cursor（记录的 ref），重启不丢；
讨论 done（mode == concluded）→ 打印 result.md 路径后退出。

复用边界：frontmatter 解析/正文提取/消息文件判定/git log 输出解析全部
来自 meeting_fs（单一实现）；本模块只做 bare 只读组装与展示格式。
"""

import argparse
import json
import os
import sys
import time

from meeting_fs import (run_git, git_show, git_head, is_message_file,
                        parse_log_nameonly, extract_body, parse_frontmatter)
from meeting_engine import aggregate_mode, POLL_INTERVAL


def _protocol(bare):
    """HEAD:protocol.json → dict（读不到 → {}）。"""
    r = run_git(bare, "show", "HEAD:protocol.json", check=False)
    if r.returncode != 0:
        return {}
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {}


def participants_from_bare(bare):
    """参与者列表（单一事实源 = bare HEAD 的 protocol.json）。"""
    return _protocol(bare).get("participants") or None


def result_path(base, bare):
    """result.md 实际位置（work-<resultWriter>/result.md，与 --wait 一致）。"""
    rw = _protocol(bare).get("resultWriter", "")
    return os.path.join(base, f"work-{rw}", "result.md") if rw else ""


def new_messages(bare, since):
    """since 之后的新消息文件（commit 拓扑序，旧→新）。

    返回: list[(commit, path)]——只含消息文件（作者/NNNN.md，含 human/）。
    since: git ref（""/None = 全部）
    """
    if since:
        r = run_git(bare, "log", f"{since}..HEAD", "--name-only",
                    "--format=%H", "--reverse", check=False)
    else:
        r = run_git(bare, "log", "HEAD", "--name-only",
                    "--format=%H", "--reverse", check=False)
    result = []
    for commit, fls in parse_log_nameonly(r.stdout):
        for f in fls:
            if is_message_file(f):
                result.append((commit, f))
    return result


def format_message(path, content):
    """消息 → 展示行。返回 str | None（frontmatter 不可用）。"""
    fm = parse_frontmatter(content)
    if not fm:
        return None
    body = extract_body(content) or ""
    header = f"[{path}] {fm.get('from', '?')} ({fm.get('type', '?')})"
    if fm.get("summary"):
        header += f": {fm['summary']}"
    if body:
        return f"{header}\n{body}\n---"
    return f"{header}\n---"


def incremental(bare, agents, since):
    """单次增量读取。

    返回: (mode, lines: list[str], head, done)
    - mode: 当前聚合 mode（meeting/all-freezing/round-robin/concluded）
    - lines: 新消息展示行（旧→新）
    - head: 当前 HEAD
    - done: 讨论是否已收尾（mode == concluded）
    """
    mode = aggregate_mode(bare, agents)
    lines = []
    for commit, path in new_messages(bare, since):
        content = git_show(bare, commit, path)
        if content is None:
            continue
        s = format_message(path, content)
        if s:
            lines.append(s)
    head = git_head(bare)
    return mode, lines, head, mode == "concluded"


def _cursor_path(base):
    return os.path.join(base, ".viewer-cursor")


def _read_cursor(base):
    try:
        with open(_cursor_path(base)) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _write_cursor(base, ref):
    with open(_cursor_path(base), "w") as f:
        f.write(ref + "\n")


def follow(base, bare, agents, poll_interval=POLL_INTERVAL):
    """--follow：循环展示（tail -f 式）直到讨论结束。"""
    since = _read_cursor(base)
    last_mode = None
    while True:
        mode, lines, head, done = incremental(bare, agents, since)
        if mode != last_mode:
            print(f"【状态】{mode}", flush=True)
            last_mode = mode
        for s in lines:
            print(s, flush=True)
        if head != since:
            _write_cursor(base, head)
            since = head
        if done:
            print(f"【讨论已结束】result.md: {result_path(base, bare)}",
                  flush=True)
            return
        time.sleep(poll_interval)


def main():
    parser = argparse.ArgumentParser(description="human 讨论展示（只读）")
    parser.add_argument("base", help="讨论目录（含 repo.git）")
    parser.add_argument("--since", default=None, help="增量起点 ref（git ref）")
    parser.add_argument("--follow", action="store_true", help="循环展示直到结束")
    args = parser.parse_args()

    base = os.path.abspath(os.path.expanduser(args.base))
    bare = os.path.join(base, "repo.git")
    if not os.path.isdir(bare):
        print(f"错误: 讨论不存在: {base}", file=sys.stderr)
        return 1

    agents = participants_from_bare(bare)
    if not agents:
        print(f"错误: 无法读取 protocol.json（讨论未初始化?）: {base}",
              file=sys.stderr)
        return 1

    sys.stdout.reconfigure(line_buffering=True)
    if args.follow:
        follow(base, bare, agents)
    else:
        mode, lines, _, done = incremental(bare, agents, args.since)
        print(f"【状态】{mode}", flush=True)
        for s in lines:
            print(s, flush=True)
        if done:
            print(f"【讨论已结束】result.md: {result_path(base, bare)}",
                  flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # 管道消费者提前关闭（如 `| head` 截断）：静默退出，不打印 traceback
        # ——viewer 是工具，被主 pi/wrapper 管道消费时输出截断是正常场景。
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        sys.exit(0)
