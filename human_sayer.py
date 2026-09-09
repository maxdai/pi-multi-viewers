#!/usr/bin/env python3
"""human_sayer.py —— human 插话命令（pi-agents-helper 阶段 2）。

human 通道的写入进程（docs/pi-helper-design.md §5.3）：写一条 human
消息到 bare（经 work-human 提交通道），agents 下次轮询即可看到并可响应。

用法:
  python3 human_sayer.py <base> <文本>             # 文本参数（可多行）
  echo "多行文本" | python3 human_sayer.py <base>  # 无文本参数时读 stdin

frontmatter 由本命令**确定性补全**（人只提供正文，设计 §2.2）：
  from: human / type: message（强制——人不写流程信号）/
  mode: 写入时当前聚合 mode / seen_at: 写入时 HEAD / to: all /
  summary: 正文首行截取（可选）

并发容错（设计 §5.3）：
  - flock work-human/.human.lock：两个 sayer 串行（主 pi + 手动插话）
  - 写前 pull + git_push 容错重试：与 agents 并发写（push 非快进 →
    pull --rebase → 重推）
"""

import argparse
import fcntl
import os
import sys

from meeting_fs import (git_head, git_pull, git_commit, git_push,
                        next_msg_id, write_message, commit_message)
from meeting_engine import aggregate_mode, participants

SUMMARY_MAX = 60          # summary 截取长度（frontmatter 值单行化后展示用）
HUMAN = "human"           # 固定名（保留字，start_discussion 校验）


def _summary(body):
    """正文首行截取为 summary（frontmatter 值单行化前的摘要）。"""
    first = next((l.strip() for l in body.splitlines() if l.strip()), "")
    return first[:SUMMARY_MAX] if first else ""


def say(workdir, body):
    """写一条 human 消息（flock + pull + frontmatter + commit + push）。

    workdir: work-human 路径
    body: 正文（多行）
    返回: (path, summary)——消息文件路径与摘要
    """
    bare = os.path.join(os.path.dirname(workdir), "repo.git")
    lock_path = os.path.join(workdir, ".human.lock")
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        # 写前同步（对齐生产 commit_new_files：pull 后再写，防序号/HEAD 落后）
        git_pull(workdir)
        head = git_head(bare)
        agents = participants(workdir)
        mode = aggregate_mode(bare, agents) if agents else "meeting"
        mid = next_msg_id(workdir, HUMAN)
        path = f"{HUMAN}/{mid}.md"
        fm = {
            "from": HUMAN,
            "type": "message",        # 强制——人不写流程信号
            "mode": mode,             # 写入时当前聚合 mode（不参与判定，仅信息）
            "seen_at": head,          # 人看到了当前全部状态
            "to": "all",
        }
        summ = _summary(body)
        if summ:
            fm["summary"] = summ
        write_message(workdir, path, fm, body)
        git_commit(workdir, [path], commit_message(HUMAN, mid))
        git_push(workdir)             # 容错重试（与 agents 并发写）
        fcntl.flock(lf, fcntl.LOCK_UN)
    return path, summ


def interactive(workdir):
    """交互模式：逐行累积，空行提交（粘贴多行/打字统一语义）。

    终端交互插话用法（实测 2026-08-30 用户反馈：shell 敲命令不直观）：
      python3 human_sayer.py <base> -i
    然后直接输入插话内容：
      > 第一行
      > 第二行（继续累积）
      > （空行 Enter）→ 提交整块，已发送 human/NNNN.md
    粘贴多行文本：粘贴的换行逐行进入累积，最后空行提交；
    空行且无累积 → 忽略；Ctrl-D 退出。
    """
    print("输入插话内容（逐行累积，空行 Enter 提交；Ctrl-D 退出）",
          flush=True)
    lines = []
    while True:
        try:
            line = input("> ")
        except EOFError:
            break
        if line.strip():
            lines.append(line)
        elif lines:
            body = "\n".join(lines)
            lines = []
            path, summ = say(workdir, body)
            print(f"已发送 {path}" + (f"（摘要: {summ}）" if summ else ""),
                  flush=True)


def main():
    parser = argparse.ArgumentParser(description="human 插话（写一条消息）")
    parser.add_argument("base", help="讨论目录（含 repo.git 与 work-human）")
    parser.add_argument("text", nargs="?", default=None,
                        help="插话文本（可多行；不传则读 stdin）")
    parser.add_argument("-i", "--interactive", action="store_true",
                        help="交互模式：逐行输入，空行提交（终端手动插话）")
    args = parser.parse_args()

    base = os.path.abspath(os.path.expanduser(args.base))
    workdir = os.path.join(base, "work-human")
    bare = os.path.join(base, "repo.git")
    if not os.path.isdir(bare):
        print(f"错误: 讨论不存在: {base}", file=sys.stderr)
        return 1
    if not os.path.isdir(workdir):
        print(f"错误: work-human 不存在: {workdir}（讨论创建于 human 功能之前?）",
              file=sys.stderr)
        return 1

    if args.interactive:
        interactive(workdir)
        return 0

    body = args.text if args.text is not None else sys.stdin.read()
    body = body.strip()
    if not body:
        print("错误: 插话内容为空（传 <文本> 参数或经 stdin 输入）",
              file=sys.stderr)
        return 1

    path, summ = say(workdir, body)
    print(f"已发送 {path}" + (f"（摘要: {summ}）" if summ else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
