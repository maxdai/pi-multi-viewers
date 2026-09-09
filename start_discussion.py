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
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TPL_DIR = os.path.join(HERE, "templates")
PI_AGENT_DIR = os.environ.get("PI_CODING_AGENT_DIR",
                             os.path.expanduser("~/.pi/agent"))
GIT_USER = "meeting-bot"
GIT_EMAIL = "meeting-bot@local"


def run(cmd, cwd=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"cmd {cmd} 失败: {r.stderr.strip()}")
    return r


def _spec_read(spec_dir, rel):
    """读 spec 文件内容，永远跳过第一行（说明行，B 方案）。

    设计 16.4：第一行是骨架生成时的用途说明，不注入；正文从第二行起。
    文件不存在 → 返回 None（逐文件独立回退）。
    """
    fp = os.path.join(spec_dir, rel)
    if not os.path.isfile(fp):
        return None
    with open(fp) as f:
        lines = f.read().splitlines()
    return "\n".join(lines[1:]).strip("\n")


def _default_model():
    """本机默认模型（如 opencode-go/deepseek-v4-flash）。

    从 pi settings.json 读取 defaultProvider/defaultModel 拼接为
    provider/model。若 defaultModel 已含 '/' 则直接使用。
    无默认模型配置 → 返回 None（pi-agent.json 不写 model，回退 pi 默认）。
    获取失败（pi 不可用/无 settings）→ 返回 None。
    """
    try:
        with open(os.path.join(PI_AGENT_DIR, "settings.json")) as f:
            cfg = json.load(f)
        provider = cfg.get("defaultProvider") or ""
        model = cfg.get("defaultModel") or ""
        if not model:
            return None
        if "/" in model:
            return model
        if provider:
            return f"{provider}/{model}"
        return model
    except (OSError, ValueError):
        return None




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


def _strip_empty_sections(question):
    """去掉 question.md 中未填的可选节（打磨项 2026-09-01 讨论结论）。

    模板生成的可选节（## 初始立场 / ## 待回答的问题）如果用户没编辑，
    节内只有占位符行（"- X: 立场" / "- 问题"）——注入前整节去掉，
    避免占位符混入讨论环境。判据精确：占位符是确定字符串，用户真实
    内容不会写成 "- X: 立场"（立场值就是"立场"二字）。
    """
    placeholder = re.compile(r"^-\s+(\w+:)?\s*立场$|^-\s+问题$")
    out = []
    pending_title = None  # 当前节的标题（占位符节连标题一起删）
    cur = []  # 当前节内容行
    cur_is_placeholder = True

    def flush():
        if not cur_is_placeholder:
            if pending_title is not None:
                out.append(pending_title)
            out.extend(cur)

    for line in question.splitlines():
        if line.startswith("## "):
            flush()
            pending_title = line
            cur = []
            cur_is_placeholder = True
        elif line.strip() == "":
            cur.append(line)
        else:
            cur.append(line)
            if not placeholder.match(line):
                cur_is_placeholder = False
    flush()
    return "\n".join(out)


def gen_spec_skeleton(spec_dir, participants):
    """生成 spec 骨架（--spec-gen）：question.md + background.md + models.md + agents/X.md。

    每个文件第一行 = 用途说明（不注入，设计 16.4）；文件预先 touch 好
    方便手工修改（用户设计 2026-08-11）。
    """
    os.makedirs(os.path.join(spec_dir, "agents"), exist_ok=True)
    # README.md：从模板复制（内容不变——模板化，用户 7909）
    shutil.copyfile(os.path.join(TPL_DIR, "spec-readme.md.tpl"),
                    os.path.join(spec_dir, "README.md"))
    # question.md：第一行说明 + 基本结构模板（用户 7713：提供基本结构）
    q = [
        "# question.md——讨论起点（话题/立场/待答问题，自由 markdown）。本行是说明行，不会注入。",
        "",
        "# 讨论主题：请填写",
        "",
        "## 初始立场（可选，每参与者一行）",
    ]
    q += [f"- {p}: 立场" for p in participants]
    q += ["", "## 待回答的问题（可选）", "- 问题", ""]
    with open(os.path.join(spec_dir, "question.md"), "w") as f:
        f.write("\n".join(q))
    # background.md（第一行说明 + 空正文——用户 7707：文件可以是空的）
    with open(os.path.join(spec_dir, "background.md"), "w") as f:
        f.write("# background.md——共享背景（注入每个 work 的 AGENTS.md 背景节）。"
                "本行是说明行，不会注入。\n\n")
    # models.md（用户 8024/9204/9271：预列各 agent，每行 agent名: model，
    # variant 默认 max 隐式——只有非 max 才写 `, variant`，日常更简洁）
    with open(os.path.join(spec_dir, "models.md"), "w") as f:
        lines = ["# models.md——模型配置（可选）。每行：agent名: model[， variant]。"
                 "model 默认 default，variant 默认 max（只有不用 max 才写 variant）。"
                 "本行是说明行，不会注入。"]
        lines += [f"{p}: default" for p in participants]
        f.write("\n".join(lines) + "\n")
    # agents/X.md（第一行说明 + 空正文，按 participants 逐个 touch）
    for p in participants:
        with open(os.path.join(spec_dir, "agents", f"{p}.md"), "w") as f:
            f.write(f"# {p}.md——agent {p} 的分工/补充（追加到 agent {p} 定义正文）。"
                    f"本行是说明行，不会注入。\n\n")
    # agents/.order：固化 --agents 顺序（审核#6——sorted() 推断破坏顺序语义，
    # starter/默认 resultWriter/RR 轮转链依赖 participants 顺序）
    with open(os.path.join(spec_dir, "agents", ".order"), "w") as f:
        f.write("\n".join(participants) + "\n")


def gen_agens_md(args, agent, participants, spec_background=None,
                 main_pi_cwd=None, prepare_file=None):
    """meeting 协议 AGENTS.md（共享协议 + background；身份/立场在 agent
    定义/question.md）。

    spec_background: spec 提供时优先（设计 16.6），否则 args.background，否则占位。
    main_pi_cwd: 主 pi 工作目录（程序化注入，用户 2026-09-02）——直接进
      AGENTS.md（注入 system prompt 的载体），不经 background.md 转接：
      background.md 是人工编辑的讨论内容（用户审核 spec 时看），cwd 是
      环境事实（程序化写入），分开保持各自干净。None（手动场景）→ 节隐藏。
    prepare_file: 背景提炼文件绝对路径（discuss_prepare_<sid>.md，用户
      2026-09-02 两 prompt 拆分）——存在时模板 {PREPARE_FILE_SECTION}
      位置填充引用节（位置在模板显式可见，文本单份定义在 python 避免
      双份耦合）；None → 空串（无节）。
    """
    others = [p for p in participants if p != agent]
    sample = others[0] if others else "x"
    background = (spec_background if spec_background is not None
                  else (args.background or "（无）"))
    prepare_section = (
        "\n## 讨论背景文件\n\n"
        f"本次讨论的背景提炼文件位于 `{prepare_file}`"
        "（含讨论主题与背景信息），需要了解完整背景时可读取。\n"
        if prepare_file else ""
    )
    with open(os.path.join(TPL_DIR, "AGENTS.md.tpl")) as f:
        tpl = f.read()
    out = tpl.format(
        AGENT_NAME=agent,
        N=str(len(participants)),
        PARTICIPANTS_DISPLAY="、".join(participants),
        SAMPLE_OTHER=sample,
        BACKGROUND=background,
        MAIN_PI_CWD=main_pi_cwd or "（未提供）",
        PREPARE_FILE_SECTION=prepare_section,
    )
    if not main_pi_cwd:
        # 手动场景不注入 cwd 节（隐藏，不写占位）
        out = out.replace("\n## 主 pi 工作目录\n\n讨论环境的主 pi 在 `（未提供）` 目录运行。与该目录相关的信息\n（源码、文档、配置）可在其中查找：如有需要可查看相关文件以获取\n比本背景更详细的信息。\n", "")
    return out


def gen_agent_def(agent, participants, models=None, stances=None, extra=None,
                  variant="max"):
    """agent prompt 文件（Pi 适配：纯 markdown，无 opencode frontmatter）。

    Pi 没有 opencode agent 定义机制；每个 agent 的身份/分工通过
    --append-system-prompt 注入。模型与 thinking 写进 pi-agent.json，
    由 meeting_loop 启动时以 --model/--thinking 传入。
    分层（2026-08-09）：身份/特有内容在此；共享协议/背景在 AGENTS.md；
    话题/立场/问题在 question.md。
    extra: spec/agents/X.md 内容（跳过首行）追加到正文尾部。
    variant: 本参数保留兼容（输出里不暴露，实际由 pi-agent.json 的
    thinking 字段承载）。
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
        result += "\n\n" + extra + "\n"
    return result




def gen_question(topic, stances, background, questions):
    """question.md（讨论起点：话题 + 可选立场 + 待回答问题）。

    分层（2026-08-09）：background 移到 AGENTS.md（共享，system prompt）；
    立场保持在此（非强制、可被说服，不进 system prompt）。
    """
    lines = [f"# 讨论主题：{topic}", ""]
    if stances:
        lines += ["## 初始立场", "每个参与者有自己的初始立场（可被论据说服）：", ""]
        for k, v in stances.items():
            lines.append(f"- {k}: {v}")
        lines += ["", "开场时请声明你的立场，然后参与讨论。"]
    if questions:
        lines += ["", "## 待回答的问题"] + [f"- {q}" for q in questions] + [""]
    return "\n".join(lines)


def gen_protocol(topic, participants, max_meeting, max_rr, pure=False,
                 result_writer=None, stall_timeout=600):
    """protocol.json（meeting 模式）。"""
    rw = result_writer or participants[-1]
    proto = {
        "mode": "meeting",
        "protocol_version": 2,
        "topic": topic or "",
        "participants": participants,
        "resultWriter": rw,
        "maxMeetingRounds": max_meeting,
        "maxRRRounds": max_rr,
        "stallTimeoutSeconds": stall_timeout,
        "commitPolicy": "one-message-per-commit",
    }
    if pure:
        proto["pure"] = True
    return proto



def _resolve_path(p):
    """路径解析（review4 L10 抽取）：含路径符（/~.）→ 绝对路径；否则 cwd 下拼接。"""
    if any(ch in p for ch in "/~."):
        return os.path.abspath(os.path.expanduser(p))
    return os.path.join(os.getcwd(), p)

def _resolve_spec(spec, agents, topic, background, stances, questions, models):
    """--spec 模式解析（审核#5 抽成可测函数）：互斥校验 + spec 目录 +
    participants 推断 + question.md 必填。

    返回 (spec_dir, participants, error)——error 非 None 时前两者为 None。
    """
    if not spec:
        return None, None, None
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
        return None, None, (
            f"错误: --spec 与 {', '.join(conflicting)} 互斥——"
            f"内容要么全在 spec，要么全在命令行")
    # spec 目录解析（相对 → cwd 下，L10 抽取）
    spec_dir = _resolve_path(spec)
    if not os.path.isdir(spec_dir):
        return None, None, f"错误: spec 目录不存在 {spec_dir}"
    agents_dir = os.path.join(spec_dir, "agents")
    if not os.path.isdir(agents_dir):
        return None, None, "错误: spec 目录缺少 agents/（先 --spec-gen 生成骨架）"
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
        extra = sorted(
            f[:-3] for f in os.listdir(agents_dir)
            if f.endswith(".md") and f[:-3] not in listed_set)
        participants = listed + extra
    else:
        participants = sorted(
            f[:-3] for f in os.listdir(agents_dir) if f.endswith(".md"))
    if not participants:
        return None, None, "错误: spec/agents/ 下没有 agent 定义文件"
    # spec 必须有 question.md（讨论起点不可缺）
    if not os.path.isfile(os.path.join(spec_dir, "question.md")):
        return None, None, "错误: spec 缺少 question.md（讨论起点，先 --spec-gen 生成）"
    # 空正文校验（审核#19）：删到只剩说明行 → 无讨论主题（CLI 路径有
    # --topic 必填对等约束）
    if not (_spec_read(spec_dir, "question.md") or "").strip():
        return None, None, "错误: spec 的 question.md 正文为空（讨论起点不可缺）"
    return spec_dir, participants, None


def _clone_work(base, p):
    """clone work-<p> + 配置 git 身份 + 建本地目录（首建/重建共用，P12）。

    重建段先保存 local_files 再调用本 helper（rmtree 后 clone 丢失
    git 身份 config，需重配）。
    """
    workdir = os.path.join(base, f"work-{p}")
    run(["git", "clone", os.path.join(base, "repo.git"), workdir])
    run(["git", "config", "user.name", GIT_USER], cwd=workdir)
    run(["git", "config", "user.email", GIT_EMAIL], cwd=workdir)
    for sub in [".pi/agent", p]:   # L9：只建自己的目录（读走 bare，写有 makedirs 兜底）
        os.makedirs(os.path.join(workdir, sub), exist_ok=True)
    return workdir


def setup_environment(args, participants, base, spec_dir=None):
    """生成讨论环境（bare + clones + 配置 + setup commit + 重建）。

    spec_dir: 讨论规格目录（设计 16）——内容优先：question.md →
    question.md、background.md → AGENTS.md 背景节、agents/X.md → agent 定义
    正文。逐文件独立回退（缺哪个走 CLI/占位）。
    """
    # spec 内容预读（跳过首行说明）
    spec_question = _spec_read(spec_dir, "question.md") if spec_dir else None
    if spec_question is not None:
        # 未填的可选节（占位符）去掉，避免注入混入模板内容（打磨项 2026-09-01）
        spec_question = _strip_empty_sections(spec_question)
    spec_background = _spec_read(spec_dir, "background.md") if spec_dir else None
    spec_agents = {}
    if spec_dir:
        for p in participants:
            c = _spec_read(spec_dir, f"agents/{p}.md")
            if c is not None:
                spec_agents[p] = c
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
    run(["git", "init", "--bare", os.path.join(base, "repo.git")])

    for p in participants:
        workdir = os.path.join(base, f"work-{p}")
        if os.path.exists(workdir):
            shutil.rmtree(workdir)
        _clone_work(base, p)   # P12：clone + git 身份 + 建目录

    # 共享配置（写 work-a，之后 setup commit 进 bare）
    wa = os.path.join(base, f"work-{participants[0]}")
    with open(os.path.join(wa, "protocol.json"), "w") as f:
        json.dump(gen_protocol(args.topic, participants, args.max_meeting,
                               args.max_rr, args.pure, args.result_writer,
                               args.stall_timeout),
                  f, indent=2, ensure_ascii=False)
    with open(os.path.join(wa, "question.md"), "w") as f:
        if spec_question is not None:
            # spec 提供 → 整文件（跳过首行）作为 question.md（设计 16.6）
            f.write(spec_question + "\n")
        else:
            f.write(gen_question(args.topic, args.stances, args.background,
                                 args.questions))

    # 本地配置（每个 work 各自）
    for p in participants:
        workdir = os.path.join(base, f"work-{p}")
        # git 身份（仓库级——后续 commit 需要，RR 时代踩过坑）
        run(["git", "config", "user.name", GIT_USER], cwd=workdir)
        run(["git", "config", "user.email", GIT_EMAIL], cwd=workdir)
        with open(os.path.join(workdir, "AGENTS.md"), "w") as f:
            f.write(gen_agens_md(args, p, participants, spec_background,
                                 main_pi_cwd=os.getcwd(),
                                 prepare_file=getattr(args, "prepare_file", None)))
        mv = models.get(p, (None, "max"))
        with open(os.path.join(workdir, ".pi/agent", f"{p}.md"), "w") as f:
            f.write(gen_agent_def(p, participants, {p: mv[0]} if mv[0] else None,
                                  stances_arg, spec_agents.get(p), variant=mv[1]))
        with open(os.path.join(workdir, "pi-agent.json"), "w") as f:
            json.dump({
                "model": mv[0] or "",
                "thinking": mv[1] if mv[1] else "max",
                "prompt_file": f".pi/agent/{p}.md",
            }, f, indent=2, ensure_ascii=False)
        with open(os.path.join(TPL_DIR, "gitignore.tpl")) as gtf:
            gitignore = gtf.read()
        with open(os.path.join(workdir, ".gitignore"), "w") as f:
            f.write(gitignore)

    # setup commit（work-a 提交共享配置）
    run(["git", "add", "-A"], cwd=wa)
    run(["git", "-c", f"user.name={GIT_USER}", "-c", f"user.email={GIT_EMAIL}",
         "commit", "-m", "discuss: setup"], cwd=wa)
    # push 当前分支（不用硬编码 master——用户可能配置了
    # init.defaultBranch=main，硬编码会导致 bare 双分支、clone 检出空
    # 分支 → 环境损坏。审核 C2。）
    branch = run(["git", "branch", "--show-current"], cwd=wa,
                 check=False).stdout.strip()
    run(["git", "push", os.path.join(base, "repo.git"),
         branch or "master"], cwd=wa)

    # 重建其他 work（避免本地未跟踪文件与 pull 冲突的踩坑）
    for p in participants[1:]:
        workdir = os.path.join(base, f"work-{p}")
        local_files = {}
        for rel in ["AGENTS.md", ".gitignore", f".pi/agent/{p}.md", "pi-agent.json"]:
            fp = os.path.join(workdir, rel)
            if os.path.exists(fp):
                with open(fp) as fh:
                    local_files[rel] = fh.read()
        shutil.rmtree(workdir)
        _clone_work(base, p)   # P12：clone + git 身份 + 建目录（重建段）
        for rel, content in local_files.items():
            with open(os.path.join(workdir, rel), "w") as f:
                f.write(content)

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
    """清理前保存 result.md：从 bare git 历史复制到父级目录。

    命名 <base目录名>-result.md（如 discussion-code-review-result.md）。
    result.md 权威位置 = bare git 历史（已提交），工作区可能未同步。
    无 result.md（未完成讨论）→ 跳过。
    """
    bare = os.path.join(base, "repo.git")
    if not os.path.isdir(bare):
        return
    r = run(["git", "show", "HEAD:result.md"], cwd=bare, check=False)
    if r.returncode != 0 or not r.stdout.strip():
        return
    base_name = os.path.basename(base.rstrip("/")) or "discussion"
    dest = os.path.join(os.path.dirname(base.rstrip("/")), f"{base_name}-result.md")
    with open(dest, "w") as f:
        f.write(r.stdout)
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
    shutil.rmtree(base)
    print(f"[cleanup] 已删除目录 {base}（含 pi-sessions）")




def check_status(base):
    """讨论状态：result.md 是否已在 git 历史（权威位置）。"""
    bare = os.path.join(base, "repo.git")
    if not os.path.isdir(bare):
        return "not-exists", None
    r = run(["git", "log", "--all", "--format=%H", "--", "result.md"],
            cwd=bare, check=False)
    if r.stdout.strip():
        # review5 A5：done 需 concluded 存在——rw 写 result.md 后、concluded
        # 前崩溃 → 只保存了报告但讨论未收尾，--wait 会误报完成。
        # 结构化检查（review5 A1 同族，用户 9161）：不用 git grep 全文
        # （正文出现 "type: concluded" 会误匹配）——从 HEAD 树读消息文件
        # frontmatter 的 type 字段（只读文件头，不全文 grep）。
        r2 = run(["git", "grep", "-l", "^type: concluded$", "HEAD", "--",
                  "*/*.md"], cwd=bare, check=False)
        if r2.stdout.strip():
            return "done", None
        # 有 result.md 但无 concluded → 收尾进行中（等 concluded 落盘）
        return "running", None
    # loop 进程是否存活（目录边界——审核#23：防止 discussion-1 匹配
    # discussion-1x）
    r = run(["pgrep", "-f",
             f"meeting_loop.py.*{re.escape(base)}( |$|/)"], check=False)
    if r.stdout.strip():
        return "running", None
    return "stopped", None


def main():
    parser = argparse.ArgumentParser(description="Meeting 模式讨论环境")
    parser.add_argument("--dir",
                        help="讨论运行目录（创建/启动/清理/状态/等待用；--spec-gen 不需要）")
    parser.add_argument("--agents", default=None,
                        help="参与者（逗号分隔，默认 a,b；--spec 时不可传）")
    parser.add_argument("--topic", default=None, help="讨论主题（--skip-setup 时不需要）")
    parser.add_argument("--stances", default=None, help='JSON: {"a": "立场"}')
    parser.add_argument("--background", default=None, help="背景说明")
    parser.add_argument("--questions", default=None, help="待回答问题（|分隔，对齐 RR）")
    parser.add_argument("--models", default=None, help='JSON: {"a": "provider/model"}')
    parser.add_argument("--result-writer", default=None, help="resultWriter（默认最后一位参与者）")
    parser.add_argument("--max-meeting", type=int, default=10, help="meeting 阶段发言配额（每 agent）")
    parser.add_argument("--max-rr", type=int, default=7, help="RR 阶段轮次配额（starter）")
    parser.add_argument("--stall-timeout", type=int, default=600,
                        help="无进展超时兜底（秒，默认 600；防 provider API 慢）")
    parser.add_argument("--spec-gen", metavar="DIR", default=None,
                        help="生成 spec 骨架到 DIR（如 --spec-gen myspec/；不需 --dir）")
    parser.add_argument("--spec", default=None,
                        help="讨论规格目录（内容源：question/background/agents，优先于 CLI 内容参数）")
    parser.add_argument("--prepare-file", default=None,
                        help="背景提炼文件绝对路径（discuss_prepare_<sid>.md，"
                             "wrapper 检测存在才传）；有则写入各 agent AGENTS.md 引用节")
    parser.add_argument("--pure", action="store_true", help="--pure 模式（禁外部插件）")
    parser.add_argument("--start", action="store_true", help="创建后启动讨论")
    parser.add_argument("--skip-setup", action="store_true",
                        help="跳过环境生成，只启动已有环境（需 --dir）")
    parser.add_argument("--cleanup", action="store_true", help="清理讨论（目录，含 pi-sessions）")
    parser.add_argument("--status", action="store_true", help="检查讨论状态")
    parser.add_argument("--wait", action="store_true", help="阻塞直到讨论完成")
    args = parser.parse_args()

    participants = ([a.strip() for a in args.agents.split(",") if a.strip()]
                    if args.agents else [])
    try:
        args.stances = json.loads(args.stances) if args.stances else None
        args.models = json.loads(args.models) if args.models else None
    except ValueError as e:
        print(f"错误: JSON 参数解析失败: {e}（--stances/--models 需合法 JSON）")
        return
    args.questions = args.questions.split("|") if args.questions else None

    # --spec-gen 直接带目录位置参数（--spec-gen myspec/，不需 --dir/--spec）
    if args.spec_gen:
        if not participants:
            print("错误: --spec-gen 需要 --agents（骨架按参与者预列 agents/ 与 .order，review5 F5）")
            return
        spec_dir = _resolve_path(args.spec_gen)   # L10
        gen_spec_skeleton(spec_dir, participants)
        print(f"[spec-gen] 已生成骨架: {spec_dir}")
        return

    # 其他模式必须 --dir（讨论运行目录）
    if not args.dir:
        print("错误: 需要 --dir（讨论运行目录；--spec-gen 不需要）")
        return
    # --dir 语义分场景（2026-09-03 修正）：
    # 创建模式（--dir 裸名 + 非消费标志）：快捷命名 → cwd/discussion-<name>
    # 消费模式（--cleanup/--status/--wait/--skip-setup，操作已存在目录）：
    #   裸名按字面解释（cwd 下同名目录），存在就用不存在报错——前缀快捷
    #   只属于创建；操作时套用会找错目录（实测 cleanup 裸名潜伏 bug）
    consuming = (args.cleanup or args.status or args.wait or args.skip_setup)
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
        state, _ = check_status(base)
        print(f"[status] {state}")
        return
    if args.wait:
        sys.stdout.reconfigure(line_buffering=True)
        print(f"[wait] 等待讨论完成: {base}")
        seen = set()
        while True:
            state, _ = check_status(base)
            if state == "done":
                print("[wait] 讨论完成 ✅")
                # resultWriter 从 git 读（单一事实源——protocol.json 在 work 里，
                # --wait 不依赖具体 work）
                rw = ""
                bare = os.path.join(base, "repo.git")
                r = run(["git", "show", "HEAD:protocol.json"], cwd=bare,
                        check=False)
                if r.returncode == 0:
                    try:
                        rw = json.loads(r.stdout).get("resultWriter", "")
                    except ValueError:
                        rw = ""
                rp = os.path.join(base, f"work-{rw}", "result.md") if rw else ""
                print(f"[wait] result.md: {rp}")
                return 0
            if state == "not-exists":
                print(f"[wait] 讨论不存在: {base}")
                return 1
            # 进展显示（git log 新 commit + summary——对齐 RR，meeting 版重写时丢了）
            bare = os.path.join(base, "repo.git")
            r = run(["git", "log", "--format=%H %s"], cwd=bare, check=False)
            for line in r.stdout.splitlines():
                if line in seen:
                    continue
                seen.add(line)
                if "discuss: setup" in line:
                    continue
                # subject 格式 "discuss: <作者>/<序号>" 或 "discuss: result.md"
                s = line.split(" ", 1)[1] if " " in line else line
                fname = s[len("discuss: "):].strip() if s.startswith("discuss: ") else s
                shown = False
                if "/" in fname and not fname.endswith(".md"):
                    h = line.split(" ", 1)[0]
                    r2 = run(["git", "-C", bare, "show", f"{h}:{fname}.md"],
                             check=False)
                    if r2.returncode == 0 and r2.stdout.startswith("---"):
                        # 读 type + summary（control message 状态由 type 决定，
                        # 不靠 summary——LLM 写的 freezing summary 不统一，
                        # 用户 9434：pass 就显示 [pass]）
                        mtype = ""
                        summ = ""
                        for fl in r2.stdout.splitlines():
                            if fl.startswith("type:"):
                                mtype = fl[len("type:"):].strip()
                            elif fl.startswith("summary:"):
                                summ = fl[len("summary:"):].strip()
                        if mtype and mtype != "message":
                            # control message：显示状态标记，不显示 summary
                            print(f"[wait] {time.strftime('%H:%M:%S')} 新进展: {s}"
                                  f"\n[wait]    └ [{mtype}]")
                            shown = True
                        elif summ:
                            print(f"[wait] {time.strftime('%H:%M:%S')} 新进展: {s}"
                                  f"\n[wait]    └ {summ}")
                            shown = True
                if not shown:
                    print(f"[wait] {time.strftime('%H:%M:%S')} 新进展: {s}")
            time.sleep(10)
        return 0

    # 创建（--start 总是 setup；--skip-setup = 跳过创建，只启动已有环境）
    if args.skip_setup:
        if not os.path.exists(base):
            print(f"错误: 环境不存在 {base}")
            return
        print(f"[start] 跳过环境生成——只启动已有环境")
        # 参与者从已有环境的 protocol.json 读（单一事实源，不依赖 CLI）
        try:
            r = run(["git", "show", "HEAD:protocol.json"],
                    cwd=os.path.join(base, "repo.git"), check=False)
            participants = json.loads(r.stdout).get("participants", [])
        except (ValueError, OSError):
            print("[error] 无法读取已有环境 protocol.json")
            return
    else:
        # review5 A8：创建前检测 base 已存在——git init --bare 幂等不删
        # 旧对象，重复 --dir 会复用旧 bare（旧 concluded 污染新讨论）。
        # 提示先 --cleanup（或手动删目录）。
        if os.path.exists(base):
            print(f"错误: 讨论目录已存在 {base}（请先 --cleanup 或删除，"
                  f"避免旧 bare 污染——review5 A8）")
            return
        # 创建分支（--spec 提供内容源时 spec 优先，设计 16.5）
        spec_dir = None
        if args.spec:
            # 互斥校验 + spec 目录解析 + participants 推断 + question.md 必填
            # （审核#5：抽成 _resolve_spec 可测函数）
            spec_dir, parts, err = _resolve_spec(
                args.spec, args.agents, args.topic, args.background,
                args.stances, args.questions, args.models)
            if err:
                print(err)
                return
            participants = parts
        # 非 spec：--agents 未传 → 默认 a,b（M2：argparse 默认 None）
        if not args.spec and not participants:
            participants = ["a", "b"]
        # agent 名校验（审核#7）+ 非空校验（审核#20：--agents "," 全空）
        if not participants:
            print("错误: 参与者为空（--agents 或 spec/agents/ 无有效 agent）")
            return
        bad = [p for p in participants if not re.match(r"^[a-z]+$", p)]
        if bad:
            print(f"错误: agent 名必须纯小写字母（[a-z]+）：{bad}")
            return
        # human 保留名（helper 设计 §2.1）：human 是插话通道，不是参与者——
        # 混入 participants 会被当成普通 agent 生成 loop 进程 + 进聚合判定
        if "human" in participants:
            print("错误: 'human' 是保留名（human 插话通道），不可作为参与者")
            return
        # resultWriter 必须 ∈ participants（spec 推断或 CLI 的 participants）
        if args.result_writer and args.result_writer not in participants:
            print(f"错误: resultWriter {args.result_writer} 不在参与者 {participants} 中")
            return
        # 无 spec 时创建必须给 --topic（否则是无效的 --start 单独用）
        if not args.spec and not args.topic:
            print("错误: 需要 --topic（或使用 --skip-setup 启动已有环境）")
            return
        setup_environment(args, participants, base, spec_dir)
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
        print(f"[start] 已启动 {len(procs)} 个 meeting loop 进程（log: {base}/loop-*.log）")


if __name__ == "__main__":
    main()
