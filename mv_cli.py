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

import spec_gen  # --viewers 复用其单一判据（列举/名字/集合校验）

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
  {PROG} --start <spec目录> [--extension-policy none|mc-tools|all]
  {PROG} --start <spec目录> [--max-meeting N] [--max-rr N] [--stall-timeout S]
                             # 配额：建环境时固化进 protocol.json（默认 15 / 7 / 600）
  {PROG} --status  [dir]
  {PROG} --report  [dir]
  {PROG} --wait    [dir]
  {PROG} --cleanup [dir]
  {PROG} --view    [dir] [--since <ref>]
  {PROG} --say     [dir] "<文本>"
  {PROG} --viewers                           # 列出并校验当前项目的 viewers/（只读；建视角时用）
  {PROG} --set-viewer <名字>                 # 新建一个视角文件（正文从 stdin 读；只新建不覆盖）

消费命令的 <dir> 可省略（自动发现本 session 当前分析——按 cwd 下
mv-<PI_SESSION_ID>-* 最新；无匹配则报错要求显式传目录）

默认参数:
  agents=a,b,c  max-meeting=15  max-rr=7   # 默认值的权威在 python argparse；
                                           # 配额可在 --start 时传（建环境时固化，运行中不可改）

--agents: 逗号分隔名称列表（如 "x,y"）或纯数字（如 4 → 生成 a..d）；
          human 是保留名，不能作为参与者

human 通道:
  --view: 增量查看分析进展（--since 之后的新消息+状态；末尾输出 HEAD=<hash>，主 pi 记录作下轮 --since）
  --say:  插话（写一条 human 消息，agents 可见并可回应）
"""


def fail(msg):
    """错误出口（沿用 bash 约定：`错误: ` 前缀 + stderr + rc 1）。"""
    sys.stdout.flush()  # 已打印的正常输出先落地（stderr 无缓冲，否则会插到前面）
    print(f"错误: {msg}", file=sys.stderr)
    raise SystemExit(1)


def fail_verbatim(msg):
    """错误文本**自带 `错误: ` 前缀**（来自 spec_gen 的单一判据）→ 原样输出。

    与 fail() 的差别只是前缀归属：判据的实现方（spec_gen）负责文案与前缀
    （`start_discussion` 同样 `print(err)` 原样输出）；再包一层会变成
    "错误: 错误: …"（实测踩过）。
    """
    sys.stdout.flush()
    print(msg, file=sys.stderr)
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

def cmd_viewers(args):
    """列出并校验**当前项目**的 `viewers/`（只读）。

    为什么需要它：视角是**长期资产**，创建/检查它的时刻通常**还没有任何分析
    目录**（消费命令的目录自动发现对此不适用——它找的是 mv-<sid>-*）。判据全部
    复用 `spec_gen` 的单一实现（列举 `list_agent_md` / 名字 `check_agent_name` /
    集合 `viewer_set_error`），这一层不另写一套规则。
    """
    if args:
        fail(f"未知参数: {' '.join(args)}（--viewers 不接受参数——只检查项目 cwd 的 viewers/）")
    vdir = os.path.join(os.getcwd(), "viewers")
    if not os.path.isdir(vdir):
        fail(f"未找到 {vdir}——视角文件放在项目 cwd 的 viewers/<视角名>.md"
             f"（文件名即视角名；可跑 /multi-viewers-setup 交互式建立）")
    return _validate_and_print_viewers(vdir)


def _validate_and_print_viewers(vdir):
    """列出 + 校验 + 打印（`--viewers` 与 `--set-viewer` 的**同一实现**）。

    判据全部复用 `spec_gen` 的单一实现（列举 `list_agent_md` / 名字
    `check_agent_name` / 集合 `viewer_set_error`）——这一层不另写规则。
    数量不足（<2）是**提示**不是错误：建 1 个是合法中间状态（只有启动一次
    分析才要求 ≥2，那条判据由 `viewer_set_error` 独占）。
    """
    names, _briefs, empty = spec_gen._discover_viewers(vdir)
    if not names:
        fail(f"{vdir} 下没有 *.md——文件名即视角名（如 viewers/效率.md）")
    empty_names = {n for n, _why in empty}
    print(f"[viewers] {vdir}", flush=True)  # 与 stderr 的错误行保序（管道下也如此）
    for n in names:
        notes = []
        name_err = spec_gen.check_agent_name(n)
        if name_err:
            notes.append(f"文件名非法：{name_err}")
        if n in empty_names:
            notes.append("空：没有视角内容")
        suffix = f"（{'；'.join(notes)}）" if notes else ""
        print(f"  {n}.md{suffix}")
    err = spec_gen.viewer_entry_errors(names, empty)
    if err:
        fail_verbatim(err)
    gap = spec_gen.viewers_count_gap(names)
    if gap:
        print(f"  校验：{gap}（建 1 个是合法的中间状态）")
        return 0
    print(f"  校验：通过（{len(names)} 个视角，名字与内容均合法）")
    return 0


def cmd_set_viewer(args):
    """新建一个视角文件：`--set-viewer <名字>`，正文**从 stdin 读**。

    为什么是命令而不是"让 LLM 直接写文件"：文件名即视角名，于是**命名规则 /
    不覆盖已有 / 空正文拒绝 / 写完校验并回显**这四条本来只能写在 prompt 里当
    纪律（LLM 会漏），现在由机制保证（判据复用 `spec_gen` 单一实现）。LLM/人
    只负责**内容**——那是它该做的部分。不提供 `--force`：本命令语义 = 只新建，
    改已有视角请直接编辑文件。
    """
    if len(args) != 1:
        fail("用法: --set-viewer <名字>（正文从 stdin 读；如 "
             "`mv.sh --set-viewer 效率 <<'EOF' … EOF`）")
    name = args[0]
    err = spec_gen.check_agent_name(name)
    if err:
        fail(f"非法视角名（{err}）：{name}")
    vdir = os.path.join(os.getcwd(), "viewers")
    target = os.path.join(vdir, f"{name}.md")
    # lexists（不是 exists）：悬空符号链接在 exists 下为假 → 会被"覆盖"写入，
    # 违背"绝不覆盖"的承诺（悬空链接是这条承诺目前的唯一破口）。
    if os.path.lexists(target):
        fail(f"视角已存在，不覆盖: {target}（改名，或直接编辑该文件）")
    # **写前**做集合级校验（评审 ② B′）：否则 viewers/ 里已有坏文件时，本命令
    # 会"先写成功、再以 rc≠0 退出"——副作用已发生却报失败（半成功）。
    # 用 if names 守卫：names 为 None 表示目录还不存在/还没有视角，那是合法起点
    # （不守卫会让 validate_participants(None) 抛 TypeError——首次建视角即命中）。
    names, _briefs, empty = spec_gen._discover_viewers(vdir)
    if names:
        err = spec_gen.viewer_entry_errors(names, empty)
        if err:
            fail_verbatim(f"{err}\n（修正 viewers/ 后再建新视角——本次未写入任何文件）")
    body = sys.stdin.read().strip()
    if not body:
        fail(f"视角内容为空（{name}）——正文从 stdin 传入；空视角没有 lenses，"
             f"分析会退化成同名随机视角")
    os.makedirs(vdir, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write(body + "\n")
    print(f"[set-viewer] 已写入 {target}\n")
    print(body + "\n")
    return _validate_and_print_viewers(vdir)


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
    # 机器可读标记（供 pi extension 解析；人类文字照常保留）：扩展靠它拿
    # spec 路径，不解析人类文案（文案会变，标记不变）。
    print(f"[prepare] spec={spec_dir}")
    print(f"""已生成分析 spec:
  {spec_dir}

请查看/编辑该目录（question.md 任务书 / background.md 边界 / agents/*.md 视角快照 /
models.md 模型），确认后启动：
  {PROG} --start {spec_dir}""")
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

    # 删除与创建**绑定**在此步 = 一次性消费（spec 是视角任务书/背景/models 在
    # 用户侧的唯一副本）。已知边界：launch（第 2 步）失败时 spec 已删——重估触发
    # 条件 = 观测到一次 launch 失败，或将来引入 resume/retry 通道。
    if fnmatch.fnmatch(os.path.basename(os.path.normpath(spec_dir)),
                       "mv-spec-*"):
        print(f"spec 已消费，删除（本工具生成形态）: {spec_dir}")
        shutil.rmtree(spec_dir, ignore_errors=True)
    else:
        print(f"spec 已消费（保留未删——非 mv-spec-* 形态）: {spec_dir}")

    # 第 2 步：启动已有环境
    if _call([PYTHON, START_DISCUSSION, "--dir", dir_path,
              "--skip-setup", "--start"]):
        fail("启动失败，请查看上方输出")

    # 机器可读标记（供 pi extension 解析；人类文字照常保留）：watch= 供扩展
    # 把观看命令**原样**预填进输入框（不做改写/转述）。
    watch_cmd = f'!!python3 "{HUMAN_VIEWER}" {dir_path} --follow'
    print(f"[start] dir={dir_path}")
    print(f"[start] watch={watch_cmd}")
    print(f"""多视角分析已启动
目录: {dir_path}

观看分析（复制执行，不进 LLM；Ctrl-C 中断后可插话再续看）:
{watch_cmd}

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
    if cmd == "--viewers":
        return cmd_viewers(rest)
    if cmd == "--set-viewer":
        return cmd_set_viewer(rest)
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
