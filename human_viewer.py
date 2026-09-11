#!/usr/bin/env python3
"""human_viewer.py —— 只读展示分析进展。

human 通道的展示进程（docs/pi-helper-design.md §5.2）：纯 bare 只读，
无写路径。增量输出 `--since <ref>` 之后的新消息 + 状态变化（mode 切换）。

用法:
  python3 human_viewer.py <base>                  # 当前状态 + 全部消息
  python3 human_viewer.py <base> --since <ref>    # 增量（ref 之后）
  python3 human_viewer.py <base> --follow         # 循环展示直到分析结束

输出契约（稳定文本，供壳/主 pi 消费）：
  【状态】<mode>
  [<path>] <from> (<type>): <summary 若有>
  <正文>
  ---

--follow 模式：游标持久化 <base>/.viewer-cursor（记录的 ref），重启不丢；
分析 done（mode == concluded）→ 打印 result.md 路径后退出。

复用边界：frontmatter 解析/正文提取/消息文件判定/git log 输出解析全部
来自 meeting_fs（单一实现）；本模块只做 bare 只读组装与展示格式。
"""

import argparse
import json
import os
import sys
import time

import meeting_fs
from meeting_fs import (run_git, git_show, git_head, is_message_file,
                        parse_log_nameonly, extract_body, parse_frontmatter)
from meeting_engine import aggregate_mode


def participants_from_bare(bare):
    """参与者列表（单一事实源 = bare HEAD 的 protocol.json）。"""
    return meeting_fs.read_protocol(bare).get("participants") or None


def result_path(base):
    """result.md 的固定位（`<分析目录>-result.md`，与 --wait / prompt 一致）。

    resultWriter 的 loop 退出（concluded）时保存到该位置，cleanup 兜底再存
    一次；权威单一事实源是 bare 的 `HEAD:result.md`。调用方**无需**推
    resultWriter 是谁、也不必进 work 子目录——分析目录删除后该文件仍在。
    """
    return f"{base}-result.md"


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


def is_finished(bare, agents, mode=None):
    """分析是否**收尾完成**（`--wait`/viewer/`--status` 共用的唯一判据）。

    定义 = `concluded`（状态机聚合）**且** result.md 已进 bare 且有效。

    为什么两条件：`concluded` 是协议信号、result.md 是产物——只认 concluded
    会在产物落盘前先报"已结束"并打印尚不存在的路径（§3.5-P5 实测分叉）；
    只认 result.md 则无法区分"收尾进行中"与"收尾中断（stalled）"。
    为什么落 viewer：它是唯一增量实现的持有者，也是观察端判据的家。
    """
    if mode is None:
        mode = aggregate_mode(bare, agents)
    if mode != "concluded":
        return False
    content = git_show(bare, "HEAD", "result.md")
    return bool(content) and len(content) > 50


def incremental(bare, agents, since):
    """单次增量读取。

    返回: (mode, lines: list[str], head, done)
    - mode: 当前聚合 mode（meeting/all-freezing/round-robin/concluded）
    - lines: 新消息展示行（旧→新）
    - head: 当前 HEAD
    - done: 分析是否已收尾（mode == concluded）
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
    return mode, lines, head, is_finished(bare, agents, mode)


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


# 观察端刷新节奏（**有意独立于状态机的空闲重试节奏**）：
# meeting_engine.POLL_INTERVAL 是 loop 的空闲轮询（与 API/CPU 成本相关），
# 这里是"观察者多久看一眼"（与 UX 延迟相关）——共享值 ≠ 共享概念。
# 若直接绑定 engine 的常量，将来调 loop 节奏会**静默改变观察契约**
# （动作-远距离耦合）。两者当前同为 2.0s 只是巧合，改一个不影响另一个。
# 下界 ≥1s：每次刷新 ≈3 个 git 子进程 + /proc 扫描；调到 0.1s 会变成
# ~30% 单核的无谓开销。
OBSERVER_POLL_INTERVAL = 2.0


def follow(base, bare, agents, poll_interval=OBSERVER_POLL_INTERVAL):
    """--follow：循环展示（tail -f 式）直到分析结束。"""
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
            print(f"【分析已结束】result.md: {result_path(base)}",
                  flush=True)
            return
        time.sleep(poll_interval)


def main():
    parser = argparse.ArgumentParser(description="human 分析展示（只读）")
    parser.add_argument("base", help="分析目录（含 repo.git）")
    parser.add_argument("--since", default=None, help="增量起点 ref（git ref）")
    parser.add_argument("--follow", action="store_true", help="循环展示直到结束")
    args = parser.parse_args()

    base = os.path.abspath(os.path.expanduser(args.base))
    bare = meeting_fs.bare_of_base(base)
    if not os.path.isdir(bare):
        print(f"错误: 分析不存在: {base}", file=sys.stderr)
        return 1

    agents = participants_from_bare(bare)
    if not agents:
        print(f"错误: 无法读取 protocol.json（分析未初始化?）: {base}",
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
            print(f"【分析已结束】result.md: {result_path(base)}",
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
