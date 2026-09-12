#!/usr/bin/env python3
"""mv_cli.py —— 多视角分析命令行（wrapper 的实际实现）。

**为什么这层是 Python**（原来是 364 行 bash，2026-09-12 收敛）：
它曾住着参数解析（--prepare/--view/--say 三处）、目录名构造、spec 删除策略、
路径规范化与约 60 行用户文案。这层产出的真实 bug **全部出自 bash 陷阱**——
`shift` 吃掉显式目录（F5 回归）、`local` 重复声明清空变量、命令替换里的
`exit` 不进父 shell（双重错误消息）、参数静默丢弃（P0）；而 bash 在本项目
工具链里**零行覆盖**（Python 覆盖率看不到它），缺口只能靠真实 e2e 或评审
暴露。收敛后：逻辑进入覆盖率与单测，解析用统一出口，不再有
shift/subshell/local 类陷阱。

`scripts/mv.sh` 保留为稳定入口 shim（prompt/README 引用该路径，不能变）。

**子进程边界**：环境创建/启动（start_discussion.py）、观看（human_viewer.py）、
插话（human_sayer.py）仍以子进程调用——它们是各自的 CLI 入口（argparse +
main），行为与 bash 版逐字一致；本模块只做「解析 → 决议 → 调用 → 展示」。

**退出码约定**（沿用 bash 版）：无参数/参数不足 → usage + rc 2；
--help → usage + rc 0；其余错误 → `错误: ...` 到 stderr + rc 1；
子命令的业务退出码原样透传。
"""

import fnmatch
import os
import shutil
import subprocess
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
PYTHON = os.environ.get("PYTHON") or "python3"
START_DISCUSSION = os.path.join(HERE, "start_discussion.py")
OBSERVABILITY = os.path.join(HERE, "observability.py")
HUMAN_VIEWER = os.path.join(HERE, "human_viewer.py")
HUMAN_SAYER = os.path.join(HERE, "human_sayer.py")

# 展示名：shim 传入调用路径（沿用 bash 的 `$0` 语义——usage 与启动块里显示
# 用户实际敲的那条命令）；直接跑 mv_cli.py 时退回默认名。
PROG = os.environ.get("MV_INVOKED_AS") or "scripts/mv.sh"

USAGE = f"""用法:
  {PROG} --prepare "<问题>" [--background "<背景>"] [--agents "a,b,c"|4]
  {PROG} --start <spec目录> [--fork-mode compaction|budget|full]
  {PROG} --status  [dir]
  {PROG} --report  [dir]
  {PROG} --wait    [dir]
  {PROG} --cleanup [dir]
  {PROG} --view    [dir] [--since <ref>]
  {PROG} --say     [dir] "<文本>"

消费命令的 <dir> 可省略（自动发现本 session 当前分析——按 cwd 下
mv-<PI_SESSION_ID>-* 最新；无匹配则报错要求显式传目录）

默认参数:
  agents=a,b,c  max-meeting=10  max-rr=7   # 配额默认值的权威在 python argparse（本层不传）

--agents: 逗号分隔名称列表（如 "x,y"）或纯数字（如 4 → 生成 a..d）；
          human 是保留名，不能作为参与者

human 通道:
  --view: 增量查看分析进展（--since 之后的新消息+状态；末尾输出 HEAD=<hash>，主 pi 记录作下轮 --since）
  --say:  插话（写一条 human 消息，agents 可见并可回应）
"""


def fail(msg):
    """错误出口（沿用 bash 约定：`错误: ` 前缀 + stderr + rc 1）。"""
    print(f"错误: {msg}", file=sys.stderr)
    raise SystemExit(1)


# ---------------------------------------------------------------
# 目录参数：解析（显式优先 / 省略则自动发现）与校验
# ---------------------------------------------------------------

def normalize_dir(path):
    """目录参数规范化：裸名（无路径符）会被 start_discussion 当成相对路径
    找错目录（实测 2026-09-03）——所有消费命令入口统一转绝对路径。"""
    return os.path.realpath(path)


def resolve_dir(arg):
    """显式参数优先；**省略则自动发现**（observability `--find-dir` 单一实现，
    extension 同入口）。失败原因由那个 python 进程给出（错误文案留一处，
    e2e16 F7）：stderr 直通、退出码原样透传。

    为什么允许省略：唯一知道路径的是 `--start` 的输出，此前消费命令都要求
    把它传给每个命令，等于让调用方（主 pi）把长绝对路径记在 LLM 上下文里
    再复用——改错/截断/相对路径都出过。
    """
    if arg:
        return normalize_dir(arg)
    r = subprocess.run([PYTHON, OBSERVABILITY, "--find-dir"],
                       stdout=subprocess.PIPE, text=True)
    if r.returncode != 0:
        raise SystemExit(r.returncode)
    return normalize_dir(r.stdout.strip())


def require_dir(d):
    if not d:
        fail("缺少目录参数")
    if not os.path.isdir(d):
        fail(f"目录不存在: {d}")
    return d


def _dir_only(command, args):
    """status/report/wait/cleanup 的公共入口：只接受 `[dir]`。

    多余参数**响亮失败**而非静默忽略（无静默铁律 / P0：wrapper 不得静默
    丢弃参数）。
    """
    if len(args) > 1:
        fail(f"未知参数: {' '.join(args[1:])}（{command} 只接受 [dir]）")
    return require_dir(resolve_dir(args[0] if args else ""))


def _call(cmd):
    """子进程透传（stdout/stderr 直通，退出码原样返回）。"""
    return subprocess.run(cmd).returncode


# ---------------------------------------------------------------
# 消费命令
# ---------------------------------------------------------------

def cmd_status(args):
    d = _dir_only("--status", args)
    return _call([PYTHON, START_DISCUSSION, "--dir", d, "--status"])


def cmd_report(args):
    d = _dir_only("--report", args)
    return _call([PYTHON, START_DISCUSSION, "--dir", d, "--report"])


def cmd_wait(args):
    d = _dir_only("--wait", args)
    return _call([PYTHON, START_DISCUSSION, "--dir", d, "--wait"])


def cmd_cleanup(args):
    d = _dir_only("--cleanup", args)
    return _call([PYTHON, START_DISCUSSION, "--dir", d, "--cleanup"])


def cmd_view(args):
    """增量查看 + 输出 HEAD 游标。

    首参判形：非空且非 `--since` → 显式目录（与 status/report/wait/cleanup
    的 `resolve_dir(arg)` 同一判据；F5 回归教训：无条件 shift 会让显式目录
    被静默丢弃）。与 `--say` 的区别：`--say` 按**参数个数**区分（文本可含
    空格或以 `--` 开头，不能按值判形）；`--view` 的 `--since` 是本命令自己
    的选项名，可以按值判形。
    """
    since = None
    if args and args[0] != "--since":
        d = require_dir(resolve_dir(args[0]))
        args = args[1:]
    else:
        d = require_dir(resolve_dir(""))
    i = 0
    while i < len(args):
        if args[i] == "--since":
            if i + 1 >= len(args):
                fail("--since 需要一个值")
            since = args[i + 1]
            i += 2
        else:
            fail(f"未知参数: {args[i]}（--view 只接受 --since）")
    if not os.path.isdir(os.path.join(d, "repo.git")):
        fail(f"分析不存在: {d}（无 repo.git）")
    cmd = [PYTHON, HUMAN_VIEWER, d] + (["--since", since] if since else [])
    rc = _call(cmd)
    if rc:
        # viewer 失败（坏 ref / 仓库异常）→ **不打印游标**：失败时给出 HEAD
        # 会让下一次 --since 静默跳过消息（bash 版把 rc 吞成 0 且照打游标，
        # 属"静默失败"一类；此处改为响亮失败）
        return rc
    head = subprocess.run(["git", "-C", os.path.join(d, "repo.git"),
                           "rev-parse", "HEAD"],
                          stdout=subprocess.PIPE, text=True,
                          stderr=subprocess.DEVNULL).stdout.strip()
    print(f"HEAD={head}")
    return 0


def cmd_say(args):
    """插话。两种形态**按参数个数**区分（不按值猜语义）：

      `--say <dir> "<文本>"`   显式目录
      `--say "<文本>"`         目录自动发现

    多于 2 个参数**响亮失败**（bash 版静默忽略多余参数——未加引号的文本
    会被当成目录，行为不可预期；此处直接报错并提示加引号）。
    """
    if len(args) > 2:
        fail("--say 接受 1 个参数（文本）或 2 个（目录 文本）——"
             "文本含空格请加引号")
    if len(args) == 2:
        d = require_dir(resolve_dir(args[0]))
        text = args[1]
    else:
        d = require_dir(resolve_dir(""))
        text = args[0] if args else ""
    if not os.path.isdir(os.path.join(d, "work-human")):
        fail(f"分析缺少 work-human: {d}")
    if not text:
        fail("插话文本不能为空")
    return _call([PYTHON, HUMAN_SAYER, d, text])


# ---------------------------------------------------------------
# 创建类命令
# ---------------------------------------------------------------

def cmd_prepare(args):
    """生成 spec 骨架（`--spec-gen` 是唯一实现，本层只解析参数 + 展示）。"""
    if not args:
        print(USAGE, file=sys.stderr)
        return 2
    topic = args[0]
    background = None
    agents_list = None
    i = 1
    while i < len(args):
        a = args[i]
        if a == "--background":
            if i + 1 >= len(args):
                fail("--background 需要一个值")
            background = args[i + 1]
            i += 2
        elif a == "--agents":
            if i + 1 >= len(args):
                fail("--agents 需要一个值（逗号分隔名称列表或数字）")
            agents_list = args[i + 1]
            i += 2
        elif a in ("-h", "--help"):
            print(USAGE)
            return 0
        else:
            fail(f"未知参数: {a}（--prepare 只接受 <问题>、--background、--agents）")
    if not topic:
        fail("问题不能为空")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    spec_dir = os.path.join(os.getcwd(), f"mv-spec-{stamp}")
    # 目录不预创建——python 侧校验失败 = 零产物（预创建会让"不合规零产物"
    # 失效，实测 2026-09-09；成功时 gen_spec_skeleton 自建）。
    # agents 缺省 = viewers 快照模式：校验与快照都在 python 侧
    # （_snapshot_viewers；不合规根本不应该开始 spec-gen）。
    cmd = [PYTHON, START_DISCUSSION, "--spec-gen", spec_dir, "--topic", topic]
    if background:
        cmd += ["--background", background]
    if agents_list:
        cmd += ["--agents", agents_list]
    if _call(cmd):
        fail("spec 骨架生成失败（start_discussion --spec-gen，见上方错误）")
    print(f"""已生成分析 spec:
  {spec_dir}

请查看/编辑该目录，补充背景、各 agent 视角等。
编辑完成后，告诉我"继续"，我会自动启动分析。""")
    return 0


def cmd_start(spec_dir, extra):
    """创建分析环境 + 启动：两段调用 start_discussion（创建 → --start）。

    中间夹一步 spec 处理：**只删本工具生成的形态**（`mv-spec-*`），用户自建
    的 spec 目录不动（可能是有价值的视角快照）。删除前提示一行——spec 是
    视角任务书/背景/models 在用户侧的唯一副本，删了不可恢复。
    """
    require_dir(spec_dir)
    if not os.path.isfile(os.path.join(spec_dir, "question.md")):
        fail(f"spec 缺少 question.md: {spec_dir}")

    session_id = os.environ.get("PI_SESSION_ID", "")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    # 目录前缀 mv-：与 pi-agents-helper 的 discuss-<sid>-* **命名空间隔离**
    # ——两个系统的 extension 都按"同 sid 最新目录"发现目标，共用前缀会在
    # 同 session 并发两套时互相插错（且 human 消息格式兼容 → 静默写错）。
    dir_name = "mv" + (f"-{session_id}" if session_id else "") + f"-{stamp}"
    dir_path = os.path.join(os.getcwd(), dir_name)

    # 第 1 步：创建分析环境（不启动；fork 源由 python 侧 resolve_fork_source
    # 自动解析）。余参原样转发（--fork-mode 等）：值识别与默认值的唯一家在
    # python argparse，本层不解析、不丢弃。
    if _call([PYTHON, START_DISCUSSION, "--dir", dir_path,
              "--spec", spec_dir] + extra):
        fail("环境创建失败，请查看上方输出")

    if fnmatch.fnmatch(os.path.basename(os.path.normpath(spec_dir)),
                       "mv-spec-*"):
        print(f"[start] spec 已消费，删除（本工具生成形态）: {spec_dir}")
        shutil.rmtree(spec_dir, ignore_errors=True)
    else:
        print(f"[start] spec 已消费（保留未删——非 mv-spec-* 形态）: {spec_dir}")

    # 第 2 步：启动已有环境
    if _call([PYTHON, START_DISCUSSION, "--dir", dir_path,
              "--skip-setup", "--start"]):
        fail("启动失败，请查看上方输出")

    print(f"""多视角分析已启动
目录: {dir_path}

观看分析（复制执行，不进 LLM；Ctrl-C 中断后可插话再续看）:
!!python3 "{HUMAN_VIEWER}" {dir_path} --follow

查看进展: {PROG} --view {dir_path}
插话: {PROG} --say {dir_path} "<文本>"
查看状态: {PROG} --status {dir_path}
等待完成: {PROG} --wait {dir_path}
清理: {PROG} --cleanup {dir_path}

说明:
- human 通道：--view 增量查看（主 pi 记录末尾 HEAD 作下轮 --since）；
  --say 插话（agents 可见并可回应；已冻结 agent 不响应）
- 完成后 result.md 自动保存到固定位置：{dir_path}-result.md（与分析目录同级——resultWriter loop 退出时保存；cleanup 也会保存）
- 读取 result.md 摘要后请执行 --cleanup 清理分析目录""")
    return 0


# ---------------------------------------------------------------
# 分发
# ---------------------------------------------------------------

def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv:
        print(USAGE, file=sys.stderr)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "--prepare":
        return cmd_prepare(rest)
    if cmd == "--start":
        if not rest:
            fail("--start 需要 spec 目录参数")
        return cmd_start(rest[0], rest[1:])
    if cmd == "--status":
        return cmd_status(rest)
    if cmd == "--report":
        return cmd_report(rest)
    if cmd == "--wait":
        return cmd_wait(rest)
    if cmd == "--cleanup":
        return cmd_cleanup(rest)
    if cmd == "--view":
        return cmd_view(rest)
    if cmd == "--say":
        return cmd_say(rest)
    if cmd in ("-h", "--help"):
        print(USAGE)
        return 0
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
