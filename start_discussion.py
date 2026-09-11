#!/usr/bin/env python3
"""start_discussion.py —— Meeting 模式讨论环境生成 + 启动（独立于 agents-rr-discuss）。

用法：
  python3 start_discussion.py --dir mymeet --topic "主题" --agents a,b \
      [--stances '{"a": "立场1", "b": "立场2"}'] [--start] [--pure] \
      [--models '{"a": "provider/model"}'] [--max-meeting 10] [--max-rr 7]

复杂内容用 spec 规格目录（设计 16，与 CLI 内容参数互斥）：
  1. 生成骨架:  python3 start_discussion.py --spec-gen myspec --agents a,b,c
  2. 编辑内容:  vim myspec/question.md myspec/background.md myspec/models.md myspec/agents/*.md
  3. 创建讨论:  python3 start_discussion.py --dir mymeet --spec myspec/ --result-writer c

生命周期：
  创建（--dir）→ 启动（--start，可选）→ 观察（--status/--wait）→ 清理（--cleanup）

本文件 = **组合层**（CLI 分发 + 环境创建/启动/清理 + 编排）。同层拆出的模块：
  spec_gen.py     spec 生成（question/骨架/viewers 快照/agent 定义 + pi 环境探测）
  observability.py 观测（check_status / --report / --wait / loop 存活）

依赖方向：主文件 → {spec_gen, observability, meeting_fs, meeting_engine,
human_viewer}；两个子模块**只**依赖底层（core/fs/engine），不互相依赖、
不反向依赖主文件。为向后兼容（tests/wrapper 的 `from start_discussion
import X`），主文件 re-export 了子模块的公开符号（见文件尾部）。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

import meeting_fs
import observability
import spec_gen

HERE = os.path.dirname(os.path.abspath(__file__))
TPL_DIR = os.path.join(HERE, "templates")

GIT_USER = "meeting-bot"
GIT_EMAIL = "meeting-bot@local"


def run_cmd(cmd, cwd=None, check=True):
    """执行一次性环境命令（git init/clone/config/push 等）。

    **读路径不走这里**：读取仓库内容的 git 命令一律用 `meeting_fs.run_git`
    （带 core.quotepath=false 加固——中文视角名下 quotepath 转义会让路径
    解析失效；两个入口并存时加固只覆盖一半，是同类问题的潜在根因）。
    """
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"cmd {cmd} 失败: {r.stderr.strip()}")
    return r










def _spec_models(spec_dir, participants):
    """解析 models.md（容错，用户 8024/9204 定）：返回 {agent: (model, variant)}。

    每行格式：`agent名: model, variant`（model 与 variant 逗号分隔）。
    - model 缺省/'default' → None（创建时填本机默认模型）
    - variant 缺省/'default'/'max' → 'max'（默认档，专业用户才改）
    容错：空行/无 ':'/agent 不在 participants → 跳过；单字段行只有 model。
    规则：第一行说明跳过（_spec_read）。
    """
    if spec_dir is None:
        return {}
    content = _spec_read(spec_dir, "models.md")
    if content is None:
        return {}
    out = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        agent, _, rest = line.partition(":")
        agent = agent.strip()
        if agent not in participants:
            continue
        # 逗号分隔：model, variant（variant 可缺省）
        parts = [p.strip() for p in rest.split(",")]
        model = parts[0] if parts and parts[0] else ""
        variant = parts[1] if len(parts) > 1 else ""
        m = None if (not model or model == "default") else model
        v = "max" if (not variant or variant == "default") else variant
        out[agent] = (m, v)
    return out








def gen_agent_def(agent, participants, models=None, stances=None, extra=None):
    """agent prompt 文件（Pi 适配：纯 markdown，无 opencode frontmatter）。

    Pi 没有 opencode agent 定义机制；每个 agent 的身份/分工通过
    --append-system-prompt 注入。模型与 thinking 写进 pi-agent.json，
    由 meeting_loop 启动时以 --model/--thinking 传入。
    分层：**身份由本函数生成**（agent 名 = 文件名，单一来源）；视角内容
    （lenses/边界/交锋义务）来自 spec/agents/X.md 或 viewers/X.md。
    共享协议/背景在 AGENTS.md；话题/立场/问题在 question.md。
    extra: 视角任务书正文——追加在"你的视角任务书"标题之后。
    （thinking/variant 由 pi-agent.json 承载，不在本函数定义中）
    """
    model_body = ""
    if models and agent in models:
        model_body = f"你使用模型 {models[agent]} 参与讨论。\n"
    stance_ref = ("你的立场和观点见 question.md 与讨论中的发言。\n"
                  if stances and agent in stances else "")
    with open(os.path.join(TPL_DIR, "agent.md.tpl")) as f:
        tpl = f.read()
    result = (tpl
              .replace("{AGENT_NAME}", agent)
              .replace("{N}", str(len(participants)))
              .replace("{PARTICIPANTS_DISPLAY}", "、".join(participants))
              .replace("{MODEL_BODY}", model_body)
              .replace("{STANCE_REF}", stance_ref))
    if extra:
        # 视角正文接在"## 你的视角任务书"标题下（本函数只管拼接，
        # 身份与任务书的分界由模板定义）
        result = result.rstrip() + "\n\n" + extra.strip() + "\n"
    return result






def _resolve_path(p):
    """路径解析（review4 L10 抽取）：含路径符（/~.）→ 绝对路径；否则 cwd 下拼接。"""
    if any(ch in p for ch in "/~."):
        return os.path.abspath(os.path.expanduser(p))
    return os.path.join(os.getcwd(), p)





















def _resolve_spec(spec, agents, topic, background, stances, questions, models,
                  viewers_dir=None):
    """--spec 模式解析（审核#5 抽成可测函数）：互斥校验 + spec 目录 +
    participants 推断 + question.md 必填。

    participants 来源优先级：spec/agents/（显式自定义，含 .order）>
    viewers_dir 发现（项目稳定视角，*.md 文件名即 agent 名）> 错误。
    返回 (spec_dir, participants, viewer_briefs, error)——error 非 None 时
    前两者为 None；viewer_briefs = {agent: 视角正文}（viewers 模式非空）。
    """
    if not spec:
        return None, None, None, None
    # 明确互斥（用户 7782）：内容/参与者参数二选一，不留"忽略/优先"中间态
    # （审核#18→review4 M2）：--agents 默认 None——显式传（任何值，含 a,b）
    # 一律报互斥，消除默认值字符串比较的漏报/误报
    conflicting = []
    if agents is not None:
        conflicting.append("--agents")
    if topic:
        conflicting.append("--topic")
    if background:
        conflicting.append("--background")
    if stances:
        conflicting.append("--stances")
    if questions:
        conflicting.append("--questions")
    if models:
        conflicting.append("--models")
    if conflicting:
        return None, None, None, (
            f"错误: --spec 与 {', '.join(conflicting)} 互斥——"
            f"内容要么全在 spec，要么全在命令行")
    # spec 目录解析（相对 → cwd 下，L10 抽取）
    spec_dir = _resolve_path(spec)
    if not os.path.isdir(spec_dir):
        return None, None, None, f"错误: spec 目录不存在 {spec_dir}"
    agents_dir = os.path.join(spec_dir, "agents")
    viewer_briefs = {}
    if os.path.isdir(agents_dir):
        # participants 从 spec/agents/ 推断（无 --agents，避免冲突）
        # 顺序：agents/.order 固化 --agents 顺序（审核#6），未列出的 .md 按
        # 字母序追加（用户增删 agent 自然处理）；无 .order 回退 sorted
        order_file = os.path.join(agents_dir, ".order")
        if os.path.isfile(order_file):
            with open(order_file) as f:
                order = [l.strip() for l in f.read().splitlines() if l.strip()]
            listed = [p for p in order
                      if os.path.isfile(os.path.join(agents_dir, f"{p}.md"))]
            listed_set = set(listed)
            extra = sorted(n for n in list_agent_md(agents_dir)
                           if n not in listed_set)
            participants = listed + extra
        else:
            participants = list_agent_md(agents_dir)
        if not participants:
            return None, None, None, "错误: spec/agents/ 下没有 agent 定义文件"
    else:
        # viewers 发现（多视角产品约定）：cwd/viewers/*.md，文件名即 agent 名
        participants, viewer_briefs, empty = _discover_viewers(viewers_dir)
        if participants is None:
            return None, None, None, (
                "错误: spec 缺少 agents/ 且未找到 viewers/ 目录"
                "（项目 cwd 下建 viewers/<视角名>.md，或 --spec-gen --agents 生成）")
        # 集合级校验（空正文 / ≥2）——名字规则由 main 的中心闸门统一把
        # （同一实现；此处只管 viewers 特有的集合级规则）
        err = viewer_set_error(participants, empty)
        if err:
            return None, None, None, err
    # spec 必须有 question.md（讨论起点不可缺）
    if not os.path.isfile(os.path.join(spec_dir, "question.md")):
        return None, None, None, "错误: spec 缺少 question.md（讨论起点，先 --spec-gen 生成）"
    # 空正文校验（审核#19）：删到只剩说明行 → 无分析主题（CLI 路径有
    # --topic 必填对等约束）
    if not (_spec_read(spec_dir, "question.md") or "").strip():
        return None, None, None, "错误: spec 的 question.md 正文为空（讨论起点不可缺）"
    return spec_dir, participants, viewer_briefs, None


def _clone_work(base, p):
    """clone work-<p> + 配置 git 身份 + 建本地目录（T6 后唯一 clone 入口）。

    git 身份在此统一配置（调用方不再重复 config——e2e7 评审 T6）。
    """
    workdir = os.path.join(base, f"work-{p}")
    run_cmd(["git", "clone", meeting_fs.bare_of_base(base), workdir])
    run_cmd(["git", "config", "user.name", GIT_USER], cwd=workdir)
    run_cmd(["git", "config", "user.email", GIT_EMAIL], cwd=workdir)
    for sub in [".pi/agent", p]:   # L9：只建自己的目录（读走 bare，写有 makedirs 兜底）
        os.makedirs(os.path.join(workdir, sub), exist_ok=True)
    return workdir


def setup_environment(args, participants, base, spec_dir=None,
                      viewer_briefs=None):
    """生成讨论环境（bare + clones + 配置 + setup commit + 重建）。

    spec_dir: 讨论规格目录（设计 16）——内容优先：question.md →
    question.md、background.md → AGENTS.md 背景节、agents/X.md → agent 定义
    正文。逐文件独立回退（缺哪个走 CLI/占位）。
    viewer_briefs: viewers 发现的视角任务书 {agent: 正文}（2026-09-09）——
    优先级低于 spec/agents/（显式自定义胜出），作为 agent 定义正文。
    """
    # spec 内容预读（跳过首行说明）
    spec_question = _spec_read(spec_dir, "question.md") if spec_dir else None
    if spec_question is not None:
        # 未填的可选节（占位符）去掉，避免注入混入模板内容（打磨项 2026-09-01）
        spec_question = _strip_empty_sections(spec_question)
        # topic 固化（e2e7 评审 W——此前 spec 主路径 protocol.json.topic
        # 恒空串：gen_protocol(args.topic=None) 碰巧工作因 AGENTS.md 不
        # 消费 topic，但 protocol 是单一事实源，空串是撒谎）。提取
        # question.md 的 "# 分析主题：" 行；无可辨识主题行 → fail-fast
        # （与 _snapshot_viewers 的"不合规零产物"同哲学）
    spec_topic = None
    if spec_question is not None:
        for line in spec_question.splitlines():
            # **只认一种措辞**（"# 分析主题："）——生成端与消费端同一约定；
            # 旧 spec 用别的措辞 → 落到下方 fail-fast（明确报错，不静默）
            if line.startswith("# 分析主题："):
                spec_topic = line.replace("# 分析主题：", "").strip()
                break
    if spec_dir and not spec_topic:
        raise ValueError(
            f"错误: spec 的 question.md 缺少 '# 分析主题：' 行（无法固化 "
            f"protocol.topic）——补主题行后重试")
    spec_background = _spec_read(spec_dir, "background.md") if spec_dir else None
    spec_agents = {}
    if spec_dir:
        for p in participants:
            c = _spec_read(spec_dir, f"agents/{p}.md")
            if c is not None:
                spec_agents[p] = c
    # viewers briefs：spec/agents/ 优先（显式自定义胜出），否则 viewers 正文
    agent_extra = {p: (spec_agents.get(p) or (viewer_briefs or {}).get(p))
                   for p in participants}
    agent_extra = {k: v for k, v in agent_extra.items() if v}
    # models：spec 模式从 models.md 读（自包含，{agent: (model, variant)}），
    # CLI --models 已互斥（{agent: model} 旧格式——variant 用默认 max）
    if spec_dir:
        models = _spec_models(spec_dir, participants)
    else:
        models = {p: (m, "max") for p, m in (args.models or {}).items()}
    # default 模型 → 创建时实时获取 Pi 默认模型填入：
    # pi-agent.json 带 model 后，meeting_loop 才会传 --model；
    # 骨架期 models.md 仍写 default（--spec-gen 不获取），创建时（--spec）
    # 才解析。运行期固化不变（环境自包含）。
    if spec_dir:
        dm = _default_model()
        if dm:
            for p in participants:
                if p not in models or models[p][0] is None:
                    models[p] = (dm, models.get(p, (None, "max"))[1])
    # stance_ref（agent 定义"立场见 question.md"提示）：spec 模式一律保留
    # （设计 16.6：无法程序判断 question.md 有无立场节 → 一律提示；
    # 互斥下 CLI stances 必为 None，传占位 dict 触发生成）
    stances_arg = (args.stances if not spec_dir
                   else {p: "" for p in participants})

    os.makedirs(base, exist_ok=True)
    run_cmd(["git", "init", "--bare", meeting_fs.bare_of_base(base)])

    # T6 重构（e2e7 评审）：原流程 = 全部 clone → 写共享 → commit → 再
    # rmtree+clone 重建 others + 回写本地文件（2N-1 次 clone，~40% 冗余；
    # "保存→删→克隆→回写"是为绕开未跟踪文件冲突的补丁）。新流程：
    # 先 clone work-a 提交共享配置，再 clone others（一次拿到 setup
    # commit）——clone 恰 N 次，无重建、无回写、无重复 git config。
    wa = os.path.join(base, f"work-{participants[0]}")
    if os.path.exists(wa):
        shutil.rmtree(wa)
    _clone_work(base, participants[0])   # clone + git 身份 + 建目录

    # 共享配置（work-a 提交，setup commit 进 bare）
    with open(os.path.join(wa, "protocol.json"), "w") as f:
        json.dump(gen_protocol(spec_topic or args.topic, participants, args.max_meeting,
                               args.max_rr, args.pure, args.result_writer,
                               args.stall_timeout,
                               fork_source=getattr(args, "fork_source", None),
                               fork_cwd=os.getcwd(),
                               fork_mode=getattr(args, "fork_mode", meeting_fs.DEFAULT_FORK_MODE)),
                  f, indent=2, ensure_ascii=False)
    with open(os.path.join(wa, "question.md"), "w") as f:
        if spec_question is not None:
            # spec 提供 → 整文件（跳过首行）作为 question.md（设计 16.6）
            f.write(spec_question + "\n")
        else:
            f.write(gen_question(args.topic, args.stances, args.background,
                                 args.questions))
    with open(os.path.join(TPL_DIR, "gitignore.tpl")) as gtf:
        gitignore = gtf.read()
    with open(os.path.join(wa, ".gitignore"), "w") as f:
        f.write(gitignore)
    run_cmd(["git", "add", "-A"], cwd=wa)
    run_cmd(["git", "-c", f"user.name={GIT_USER}", "-c", f"user.email={GIT_EMAIL}",
         "commit", "-m", "discuss: setup"], cwd=wa)
    # push 当前分支（不用硬编码 master——用户可能配置了
    # init.defaultBranch=main，硬编码会导致 bare 双分支、clone 检出空
    # 分支 → 环境损坏。审核 C2。）
    branch = run_cmd(["git", "branch", "--show-current"], cwd=wa,
                 check=False).stdout.strip()
    run_cmd(["git", "push", meeting_fs.bare_of_base(base),
         branch or "master"], cwd=wa)

    # others clone（直接拿到 setup commit；work-a 已在上方创建）
    for p in participants[1:]:
        workdir = os.path.join(base, f"work-{p}")
        if os.path.exists(workdir):
            shutil.rmtree(workdir)
        _clone_work(base, p)   # clone + git 身份 + 建目录

    # 本地配置（每个 work 各自；.gitignore 已随 setup commit 分发）
    for p in participants:
        workdir = os.path.join(base, f"work-{p}")
        with open(os.path.join(workdir, "AGENTS.md"), "w") as f:
            f.write(gen_agents_md(args, p, participants, spec_background,
                                 main_pi_cwd=os.getcwd()))
        mv = models.get(p, (None, "max"))
        with open(os.path.join(workdir, ".pi/agent", f"{p}.md"), "w") as f:
            f.write(gen_agent_def(p, participants, {p: mv[0]} if mv[0] else None,
                                  stances_arg, agent_extra.get(p)))
        with open(os.path.join(workdir, "pi-agent.json"), "w") as f:
            json.dump({
                "model": mv[0] or "",
                "thinking": mv[1] if mv[1] else "max",
                "prompt_file": f".pi/agent/{p}.md",
            }, f, indent=2, ensure_ascii=False)

    # work-human：human 插话的提交通道（helper 设计 §5.4）——
    # 固定存在、不占参与者名额、无 agent 定义/pi-agent.json/AGENTS.md
    # （human 无 LLM 身份），不启动 loop 进程。
    # **必须在重建循环之外独立创建**（循环内会每迭代 clone 一次 →
    # 第二个参与者起 already exists，实测暴露）；rmtree 守卫幂等。
    wh = os.path.join(base, "work-human")
    if os.path.exists(wh):
        shutil.rmtree(wh)
    _clone_work(base, "human")

    # 复制 meeting_loop.py + 依赖模块（脚本同目录，自包含）
    for mod in ["meeting_loop.py", "meeting_fs.py", "meeting_core.py",
                "meeting_engine.py"]:
        shutil.copy(os.path.join(HERE, mod), os.path.join(base, mod))
    rw = args.result_writer or participants[-1]
    print(f"[setup] 环境就绪: {base}（{len(participants)} agents: {', '.join(participants)}）")
    print(f"[setup] resultWriter={rw}, maxMeeting={args.max_meeting}, maxRR={args.max_rr}, "
          f"立场={'有' if (args.stances or spec_dir) else '无'}, pure={args.pure}")


def _preserve_result_md(base):
    """清理前保存 result.md（薄包装 → meeting_fs.preserve_result_md，
    T2 合并：与 loop 退出路径共享同一实现）。"""
    dest = meeting_fs.preserve_result_md(base)
    if dest:
        print(f"[cleanup] 已保存 result.md → {dest}")


def cleanup_discussion(base):
    """清理一次讨论：保存 result.md（若存在）→ 删目录。

    result.md 是讨论唯一产物（审核报告等）——清理前先从 bare git 历史
    复制到父级目录（<base名>-result.md），避免清理丢产物（用户建议）。
    Pi 的 session 文件存放在 <base>/pi-sessions，随目录一起删除，无需
    额外清理全局 DB。
    不负责终止 loop 进程（职责边界，用户 2026-08-31 定）——loop 每轮
    检测到 repo.git 消失即自行退出（meeting_engine.agent_loop）。
    """
    if not os.path.isdir(base):
        print(f"[cleanup] 目录不存在: {base}")
        return
    _preserve_result_md(base)
    # 报告（**删目录前最后一次可读**——目录删后 --report 不可用）。
    # 报告是附加信息、清理是主职责：报告生成失败**不阻断**清理
    # （fail-open 只在这一层兜底——build_report 内部各段已各自 fail-open）。
    print("[cleanup] —— 本次分析报告（删除目录前最后一次可读）——")
    try:
        for line in build_report(base):
            print(line)
    except Exception as e:                       # noqa: BLE001（兜底不吞：打印）
        print(f"[cleanup] 报告生成失败（不影响清理）: {e!r}")
    shutil.rmtree(base)
    print(f"[cleanup] 已删除目录 {base}（含 pi-sessions）")








def _parse_agents(agents_arg):
    """--agents 解析（唯一实现，2026-09-09 从 wrapper 收归）：
    None → []；纯数字 n → a..（第 n 个字母）；逗号分隔 → 名称列表
    （去空白、滤空段）。数字下限 2（meeting 至少两个 LLM agents）。
    返回 (participants, error)。
    """
    if agents_arg is None:
        return [], None
    if agents_arg.isdigit():
        n = int(agents_arg)
        if n < 2:
            return None, "错误: agents 数量至少为 2（meeting 至少两个 LLM agents）"
        if n > 26:
            return None, "错误: agents 数量最多 26（a..z）"
        return [chr(97 + i) for i in range(n)], None
    participants = [a.strip() for a in agents_arg.split(",") if a.strip()]
    # 完整名字规则（非空/无空白与路径分隔符/非 human/≤32）——单实现。
    # 此前只查 human，非法名（如 "a/b"）会一路走到 spec-gen 才炸：
    # FileNotFoundError traceback + spec 半成品落盘（实测）
    err = validate_participants(participants)
    if err:
        return None, err
    return participants, None
















# ---- 从 spec_gen re-export（S2 拆分；向后兼容：tests/wrapper/extension
# 里的 `from start_discussion import X` 与 CLI 分发继续工作）----
from spec_gen import (  # noqa: F401
    check_agent_name, validate_participants, gen_question,
    _strip_empty_sections, gen_spec_skeleton, _spec_read,
    _snapshot_viewers, _discover_viewers, list_agent_md,
    viewer_set_error, gen_agents_md, gen_protocol,
    pi_sessions_dir, current_session_file, _join_model_ref,
    _default_model, _detect_pi_model_thinking, resolve_fork_source,
    PI_AGENT_DIR, MAX_AGENT_NAME_LEN,
)
from observability import (  # noqa: F401
    _loop_pids, _loops_alive, check_status, build_report,
    wait_for_completion,
)

def main():
    parser = argparse.ArgumentParser(description="Meeting 模式讨论环境")
    parser.add_argument("--dir",
                        help="讨论运行目录（创建/启动/清理/状态/等待用；--spec-gen 不需要）")
    parser.add_argument("--agents", default=None,
                        help="参与者（逗号分隔，默认 a,b；--spec 时不可传）")
    parser.add_argument("--topic", default=None, help="分析主题（--skip-setup 时不需要）")
    parser.add_argument("--stances", default=None, help='JSON: {"a": "立场"}')
    parser.add_argument("--background", default=None, help="背景说明")
    parser.add_argument("--questions", default=None, help="待回答问题（|分隔，对齐 RR）")
    parser.add_argument("--models", default=None, help='JSON: {"a": "provider/model"}')
    parser.add_argument("--result-writer", default=None, help="resultWriter（默认最后一位参与者）")
    parser.add_argument("--max-meeting", type=int, default=10, help="meeting 阶段发言配额（每 agent）")
    parser.add_argument("--max-rr", type=int, default=7, help="RR 阶段轮次配额（starter）")
    parser.add_argument("--stall-timeout", type=int,
                        default=meeting_fs.DEFAULT_STALL_TIMEOUT,
                        help="无进展超时兜底（秒，默认 600；防 provider API 慢）")
    parser.add_argument("--spec-gen", metavar="DIR", default=None,
                        help="生成 spec 骨架到 DIR（如 --spec-gen myspec/；不需 --dir）")
    parser.add_argument("--spec", default=None,
                        help="讨论规格目录（内容源：question/background/agents，优先于 CLI 内容参数）")
    parser.add_argument("--fork-mode", default=meeting_fs.DEFAULT_FORK_MODE,
                        choices=list(meeting_fs.FORK_MODES),
                        help="fork 裁剪策略：budget=预算+折叠（默认，长会话可行）；"
                             "compaction=按 compaction 边界（中小会话零损失）；"
                             "full=全量（小会话/验证）")
    parser.add_argument("--fork-source", default=None,
                        help="主 session 文件绝对路径（fork-only）：写入 "
                             "protocol.json，各 agent 首唤由本地生成 fork "
                             "源挂载主上下文；不传 = 从主 pi 环境自动解析"
                             "（PI_SESSION_ID；解析失败明确报错）")
    parser.add_argument("--pure", action="store_true", help="--pure 模式（禁外部插件）")
    parser.add_argument("--start", action="store_true", help="创建后启动讨论")
    parser.add_argument("--skip-setup", action="store_true",
                        help="跳过环境生成，只启动已有环境（需 --dir）")
    parser.add_argument("--cleanup", action="store_true", help="清理讨论（目录，含 pi-sessions）")
    parser.add_argument("--status", action="store_true", help="检查讨论状态")
    parser.add_argument("--report", action="store_true",
                        help="只读报告（观测面聚合：流程/配额/进程/LLM；"
                             "冷路径一次性，不持久化）")
    parser.add_argument("--wait", action="store_true", help="阻塞直到讨论完成")
    args = parser.parse_args()

    participants, agents_err = _parse_agents(args.agents)
    if agents_err:
        print(agents_err)
        sys.exit(1)
    try:
        args.stances = json.loads(args.stances) if args.stances else None
        args.models = json.loads(args.models) if args.models else None
    except ValueError as e:
        print(f"错误: JSON 参数解析失败: {e}（--stances/--models 需合法 JSON）")
        sys.exit(1)
    args.questions = args.questions.split("|") if args.questions else None

    # --spec-gen 直接带目录位置参数（--spec-gen myspec/，不需 --dir/--spec）
    if args.spec_gen:
        # agents/ 三态（2026-09-09）：显式 --agents = 占位骨架（覆盖路径）；
        # 缺省 = viewers 快照（校验前移——不合规零产物）；python 单一事实源，
        # bash --prepare 与 CLI 直用产出一致
        spec_dir = _resolve_path(args.spec_gen)   # L10
        viewers_dir = (os.path.join(os.getcwd(), "viewers")
                       if not participants else None)
        participants, err = gen_spec_skeleton(
            spec_dir, participants, topic=args.topic,
            background=args.background, viewers_dir=viewers_dir)
        if err:
            print(err)
            sys.exit(1)
        print(f"[spec-gen] 已生成骨架: {spec_dir}")
        return

    # 其他模式必须 --dir（讨论运行目录）
    if not args.dir:
        print("错误: 需要 --dir（讨论运行目录；--spec-gen 不需要）")
        sys.exit(1)
    # --dir 语义分场景（2026-09-03 修正）：
    # 创建模式（--dir 裸名 + 非消费标志）：快捷命名 → cwd/discussion-<name>
    # 消费模式（--cleanup/--status/--wait/--skip-setup，操作已存在目录）：
    #   裸名按字面解释（cwd 下同名目录），存在就用不存在报错——前缀快捷
    #   只属于创建；操作时套用会找错目录（实测 cleanup 裸名潜伏 bug）
    consuming = (args.cleanup or args.status or args.wait or args.skip_setup
                 or args.report)
    if any(ch in args.dir for ch in "/~."):
        base = os.path.abspath(os.path.expanduser(args.dir))
    elif consuming:
        base = os.path.join(os.getcwd(), args.dir)
    else:
        base = os.path.join(os.getcwd(), f"discussion-{args.dir}")

    if args.cleanup:
        cleanup_discussion(base)
        return
    if args.status:
        print(f"[status] {check_status(base)}")
        return
    if args.report:
        for line in build_report(base):
            print(line)
        return
    if args.wait:
        return wait_for_completion(base)
    # 创建（--start 总是 setup；--skip-setup = 跳过创建，只启动已有环境）
    if args.skip_setup:
        if not os.path.exists(base):
            print(f"错误: 环境不存在 {base}")
            sys.exit(1)
        print(f"[start] 跳过环境生成——只启动已有环境")
        # 参与者从已有环境的 protocol.json 读（单一事实源 = bare HEAD，
        # 不依赖 CLI；读不到 → 明确报错，不静默）
        participants = meeting_fs.read_protocol(
            meeting_fs.bare_of_base(base)).get("participants", [])
        if not participants:
            print("[error] 无法读取已有环境 protocol.json")
            sys.exit(1)
    else:
        # review5 A8：创建前检测 base 已存在——git init --bare 幂等不删
        # 旧对象，重复 --dir 会复用旧 bare（旧 concluded 污染新讨论）。
        # 提示先 --cleanup（或手动删目录）。
        if os.path.exists(base):
            print(f"错误: 讨论目录已存在 {base}（请先 --cleanup 或删除，"
                  f"避免旧 bare 污染）")
            sys.exit(1)
        # 创建分支（--spec 提供内容源时 spec 优先，设计 16.5）
        spec_dir = None
        viewer_briefs = {}
        if args.spec:
            # 互斥校验 + spec 目录解析 + participants 推断 + question.md 必填
            # （审核#5：抽成 _resolve_spec 可测函数）
            spec_dir, parts, viewer_briefs, err = _resolve_spec(
                args.spec, args.agents, args.topic, args.background,
                args.stances, args.questions, args.models,
                viewers_dir=os.path.join(os.getcwd(), "viewers"))
            if err:
                print(err)
                sys.exit(1)
            participants = parts
        # 非 spec：--agents 未传 → 默认 a,b（M2：argparse 默认 None）
        if not args.spec and not participants:
            participants = ["a", "b"]
        # agent 名校验 + 非空校验（审核#20：--agents "," 全空）
        # 2026-09-09 放宽：viewers 模式下文件名即 agent 名（中文人物名/
        # 视角名合法）——非法 = 空名/路径分隔符/空白/human 保留名/>32 字符
        if not participants:
            print("错误: 参与者为空（--agents 或 spec/agents/ 无有效 agent）")
            sys.exit(1)
        # 名字合法性（T4 收归：唯一实现 check_agent_name/validate_participants，
        # 含 human 保留名）
        err = validate_participants(participants)
        if err:
            print(err)
            sys.exit(1)
        # resultWriter 必须 ∈ participants（spec 推断或 CLI 的 participants）
        if args.result_writer and args.result_writer not in participants:
            print(f"错误: resultWriter {args.result_writer} 不在参与者 {participants} 中")
            sys.exit(1)
        # fork-only（2026-09-09 定）：创建必须携带主 session 文件——无
        # fork 上下文的多视角分析违背产品本质，明确报错而非静默退化。
        # --fork-source 未显式传 → 从主 pi 环境（PI_SESSION_ID）自动解析
        # （收归 python：与 models 探测同款路径约定）
        if not args.fork_source:
            args.fork_source, fork_err = resolve_fork_source()
            if fork_err:
                print(fork_err)
                sys.exit(1)
        # 无 spec 时创建必须给 --topic（否则是无效的 --start 单独用）
        if not args.spec and not args.topic:
            print("错误: 需要 --topic（或使用 --skip-setup 启动已有环境）")
            sys.exit(1)
        setup_environment(args, participants, base, spec_dir,
                          viewer_briefs=viewer_briefs)
    if args.start:
        # 启动每个 agent 的 meeting_loop（独立进程，git 触发）
        procs = []
        for p in participants:
            workdir = os.path.join(base, f"work-{p}")
            cmd = [sys.executable, os.path.join(base, "meeting_loop.py"),
                   workdir, p]
            if args.pure:
                cmd.append("--pure")
            # 配额（max-meeting/max-rr/stall-timeout）是环境属性：创建时
            # 固化在 protocol.json，启动继承（loop 读 protocol 优先）。
            # 不传 CLI —— 避免无条件覆盖 protocol.json 的固化值
            # （审核 C1：配额单一事实源；与 pure 处理一致）
            with open(os.path.join(base, f"loop-{p}.log"), "w") as f:
                procs.append(subprocess.Popen(cmd, stdout=f,
                                              stderr=subprocess.STDOUT,
                                              start_new_session=True))
        print(f"[start] 已拉起 {len(procs)} 个进程"
              f"（存活未校验；loop 状态见 status-*.json 与 {base}/loop-*.log）")


if __name__ == "__main__":
    # 传递 main 返回码（--wait 超时返回 1——此前被丢弃，wrapper 判据失效）
    sys.exit(main() or 0)