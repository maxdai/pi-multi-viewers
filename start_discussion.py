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
import meeting_fs
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


def run_cmd(cmd, cwd=None, check=True):
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


def _join_model_ref(provider, model_id):
    """按 pi 契约把 (provider, model_id) 拼成完整 model ref。

    契约（pi 源码 resolveSpawnContext）：PI_PROVIDER=provider、
    PI_MODEL=model id；session 的 model_change 同样分 provider/modelId
    两字段。**model id 本身可含 '/'**（聚合类 provider 的命名空间 id，
    如 commandcode-goat 的 "deepseek/deepseek-v4-flash"）——因此不得按
    "是否含斜杠"猜测（形状启发式会把 provider 丢掉，解析到同名的另一个
    provider，静默失真；2026-09-10 实测 fix）。

    幂等：id 已带该 provider 前缀时原样返回（兼容 PI_MODEL 已是完整
    ref 的形态）；provider 缺失时只能原样返回。
    """
    provider = (provider or "").strip()
    model_id = (model_id or "").strip()
    if not model_id:
        return ""
    if not provider or model_id.lower().startswith(provider.lower() + "/"):
        return model_id
    return f"{provider}/{model_id}"


def _default_model():
    """本机默认模型（如 opencode-go/deepseek-v4-flash）。

    从 pi settings.json 读取 defaultProvider/defaultModel，按契约拼接
    （_join_model_ref——defaultModel 同样可能是含 '/' 的 id）。
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
        return _join_model_ref(provider, model) or None
    except (OSError, ValueError):
        return None


def _detect_pi_model_thinking():
    """探测主 pi 当前 model/thinking（spec models.md 预填，对齐 wrapper 旧语义）。

    顺序：PI_MODEL/PI_PROVIDER/PI_REASONING_LEVEL 环境变量（wrapper 由主 pi
    bash 注入）→ session 文件最后 model_change/thinking_level_change 事件
    （PI_SESSION_FILE 或 cwd 编码目录最新 jsonl）→ settings 默认（_default_model）。
    返回 (model, thinking)——缺失项为空串。
    """
    model = os.environ.get("PI_MODEL") or ""
    provider = os.environ.get("PI_PROVIDER") or ""
    thinking = os.environ.get("PI_REASONING_LEVEL") or ""
    # 契约拼接（不按形状猜——id 可含 '/'，见 _join_model_ref）
    model = _join_model_ref(provider, model)
    if model and thinking:
        return model, thinking
    # session 文件兜底
    try:
        sf = os.environ.get("PI_SESSION_FILE") or ""
        if not (sf and os.path.isfile(sf)):
            sd = pi_sessions_dir(os.getcwd())
            cands = sorted(
                f for f in os.listdir(sd) if f.endswith(".jsonl")
            ) if os.path.isdir(sd) else []
            sf = os.path.join(sd, cands[-1]) if cands else ""
        if sf and os.path.isfile(sf):
            sm = st = sp = ""
            with open(sf, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    t = ev.get("type")
                    if t == "model_change":
                        sm = ev.get("modelId") or sm
                        sp = ev.get("provider") or sp
                    elif t == "thinking_level_change":
                        st = ev.get("thinkingLevel") or st
            if not model and sm:
                # model_change 是 provider + modelId 两字段——同样按契约拼接
                model = _join_model_ref(sp, sm)
            if not thinking and st:
                thinking = st
    except OSError:
        pass
    if not model:
        model = _default_model() or ""
    return model, thinking


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


def gen_spec_skeleton(spec_dir, participants, topic=None, background=None,
                      viewers_dir=None):
    """生成 spec 骨架（--spec-gen，唯一实现）：question.md + background.md +
    models.md + agents/。

    agents/ 三态：viewers_dir 给定 → _snapshot_viewers（校验+快照，失败
    返回 (None, err)、零产物）；显式 participants → 占位骨架；两者皆无 →
    无 agents/（start 报错提示）。
    topic/background: wrapper --prepare 传入（直接填进骨架）；CLI 直用
      时缺省 = 占位文案。
    models.md 预填主 pi 当前 model/thinking（_detect_pi_model_thinking，
    对齐旧 wrapper read_pi_model_thinking 语义——用户少改一个文件）。
    每个文件第一行 = 用途说明（不注入，设计 16.4）。
    """
    if viewers_dir:
        participants, err = _snapshot_viewers(spec_dir, viewers_dir)
        if err:
            return None, err
    # spec 目录本身无条件创建（2026-09-09 回归修复：agents 创建并入条件
    # 分支后，viewers 骨架曾连 spec_dir 都不建 → README copy 崩）
    os.makedirs(spec_dir, exist_ok=True)
    # agents/ 占位骨架仅显式 --agents 时生成（viewers 快照路径已建好并
    # 含内容——不能被占位覆盖；两者皆无 = viewers 发现留给启动时点）
    if participants and not viewers_dir:
        os.makedirs(os.path.join(spec_dir, "agents"), exist_ok=True)
    # README.md：从模板复制（内容不变——模板化，用户 7909）
    shutil.copyfile(os.path.join(TPL_DIR, "spec-readme.md.tpl"),
                    os.path.join(spec_dir, "README.md"))
    # question.md：第一行说明 + 基本结构模板（用户 7713：提供基本结构）
    q = [
        "# question.md——分析起点（话题/立场/待答问题，自由 markdown）。本行是说明行，不会注入。",
        "",
        f"# 分析主题：{topic or "请填写"}",
        "",
        "## 初始立场（可选，每参与者一行）",
    ]
    q += [f"- {p}: 立场" for p in participants]
    q += ["", "## 待回答的问题（可选）", "- 问题", ""]
    with open(os.path.join(spec_dir, "question.md"), "w") as f:
        f.write("\n".join(q))
    # background.md（第一行说明 + 正文；用户 7707：文件可以是空的）
    with open(os.path.join(spec_dir, "background.md"), "w") as f:
        f.write("# background.md——显式边界与约定（注入每个 work 的 AGENTS.md 背景节）。"
                "本行是说明行，不会注入。\n\n")
        if background:
            f.write(background + "\n")
    # models.md（用户 8024/9204/9271：预列各 agent，每行 agent名: model，
    # variant 默认 max 隐式——只有非 max 才写 `, variant`，日常更简洁；
    # model/thinking 预填主 pi 当前值，用户少改一个文件）
    pm, pt = _detect_pi_model_thinking()
    with open(os.path.join(spec_dir, "models.md"), "w") as f:
        lines = ["# models.md——模型配置（可选）。每行：agent名: model[, variant]。"
                 "model 默认 default，variant 默认 max（只有不用 max 才写 variant）。"
                 "本行是说明行，不会注入。"]
        for p in participants:
            if pm and pt:
                lines.append(f"{p}: {pm}, {pt}")
            elif pm:
                lines.append(f"{p}: {pm}")
            elif pt:
                lines.append(f"{p}: default, {pt}")
            else:
                lines.append(f"{p}: default")
        f.write("\n".join(lines) + "\n")
    # agents/X.md 占位 + .order（仅显式 --agents 时；快照路径的 .order
    # 由 _snapshot_viewers 写入，此处不得重写）
    if participants and not viewers_dir:
        for p in participants:
            with open(os.path.join(spec_dir, "agents", f"{p}.md"), "w") as f:
                f.write(f"# {p}.md——agent {p} 的分工/补充（追加到 agent {p} 定义正文）。"
                        f"本行是说明行，不会注入。\n\n")
        # .order：固化 --agents 顺序（审核#6——sorted() 推断破坏顺序语义，
        # starter/默认 resultWriter/RR 轮转链依赖 participants 顺序）
        with open(os.path.join(spec_dir, "agents", ".order"), "w") as f:
            f.write("\n".join(participants) + "\n")
    return participants, None


def gen_agents_md(args, agent, participants, spec_background=None,
                 main_pi_cwd=None):
    """meeting 协议 AGENTS.md（共享协议 + background；身份/立场在 agent
    定义/question.md）。

    spec_background: spec 提供时优先（设计 16.6），否则 args.background，否则占位。
    main_pi_cwd: 主 pi 工作目录（程序化注入，用户 2026-09-02）——直接进
      AGENTS.md（注入 system prompt 的载体），不经 background.md 转接：
      background.md 是人工编辑的讨论内容（用户审核 spec 时看），cwd 是
      环境事实（程序化写入），分开保持各自干净。None（手动场景）→ 节隐藏。
    """
    others = [p for p in participants if p != agent]
    sample = others[0] if others else "x"
    background = (spec_background if spec_background is not None
                  else (args.background or "（无）"))
    with open(os.path.join(TPL_DIR, "AGENTS.md.tpl")) as f:
        tpl = f.read()
    # cwd 节：占位符填充（T5 修复，e2e7 评审）——原实现先 format 出
    # "（未提供）"再整段字符串 replace 删除：模板文案/换行一改，隐藏
    # 静默失效（模板与 python 双份文本耦合）。占位符方案：节文本单份
    # 定义在此，模板位置显式可见；None → 空串（节消失）
    if main_pi_cwd:
        cwd_section = (
            f"\n## 主 pi 工作目录\n\n分析环境的主 pi 在 `{main_pi_cwd}` "
            f"目录运行。与该目录相关的信息\n（源码、文档、配置）可在其中"
            f"查找：如有需要可查看相关文件以获取\n比本背景更详细的信息。\n")
    else:
        cwd_section = ""
    out = tpl.format(
        AGENT_NAME=agent,
        N=str(len(participants)),
        PARTICIPANTS_DISPLAY="、".join(participants),
        SAMPLE_OTHER=sample,
        BACKGROUND=background,
        MAIN_PI_CWD_SECTION=cwd_section,
    )
    return out


def gen_agent_def(agent, participants, models=None, stances=None, extra=None):
    """agent prompt 文件（Pi 适配：纯 markdown，无 opencode frontmatter）。

    Pi 没有 opencode agent 定义机制；每个 agent 的身份/分工通过
    --append-system-prompt 注入。模型与 thinking 写进 pi-agent.json，
    由 meeting_loop 启动时以 --model/--thinking 传入。
    分层（2026-08-09）：身份/特有内容在此；共享协议/背景在 AGENTS.md；
    话题/立场/问题在 question.md。
    extra: spec/agents/X.md 内容（跳过首行）追加到正文尾部。
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
                 result_writer=None, stall_timeout=600,
                 fork_source=None, fork_cwd=None, fork_mode="active"):
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
    if fork_source:
        # fork 模式（多视角）：首唤挂载主 session + cwd=主项目
        # forkMode: active（默认，压缩态）/ curated（预算裁剪 + 折叠，
        # 恢复 pi 压缩不变量——长会话 fork 的可行模式）/ full（全量，
        # 用户 2026-09-10 参数化：验证/保留上下文两种策略均一等公民）
        proto["forkSource"] = fork_source
        proto["forkCwd"] = fork_cwd or os.getcwd()
        proto["forkMode"] = fork_mode
    return proto


def _resolve_path(p):
    """路径解析（review4 L10 抽取）：含路径符（/~.）→ 绝对路径；否则 cwd 下拼接。"""
    if any(ch in p for ch in "/~."):
        return os.path.abspath(os.path.expanduser(p))
    return os.path.join(os.getcwd(), p)

MAX_AGENT_NAME_LEN = 32


def pi_sessions_dir(cwd):
    """主 pi session 目录（编码约定单点，T7 收归 e2e7 评审）。

    pi 的 session 目录编码 = "--" + 去首尾斜杠 + 内斜杠换 "-" + "--"
    （/root/x → --root-x--；/tmp → --tmp--）。此前该约定在
    _detect_pi_model_thinking 与 resolve_fork_source 两处字面量重复，
    AGENTS.md 明言"编码错一根横线 = 静默解析不到"——风险点不应复制。
    """
    enc = "--" + cwd.strip("/").replace("/", "-") + "--"
    return os.path.join(PI_AGENT_DIR, "sessions", enc)


def check_agent_name(name):
    """单个 agent/视角名合法性（T4 收归，e2e7 评审）：唯一实现。

    规则（viewers 文件名即 agent 名 → 同一套规则两处来源）：
    非空 / 无路径分隔符与空白 / ≤32 字符 / 非 human 保留名。
    返回错误信息或 None。
    """
    if not name:
        return "空名"
    if re.search(r"[/\\\s]", name):
        return "含路径分隔符或空白"
    if len(name) > MAX_AGENT_NAME_LEN:
        return f"超过 {MAX_AGENT_NAME_LEN} 字符"
    if name == "human":
        return "'human' 是保留名（human 插话通道），不可作为参与者"
    return None


def validate_participants(participants):
    """整组名字校验（唯一文案）。返回错误或 None。"""
    for p in participants:
        err = check_agent_name(p)
        if err:
            return f"错误: 非法 agent 名（{err}）：{p}"
    return None


def _check_reserved(participants):
    """human 保留名校验（薄包装，调用点兼容）。"""
    for p in participants:
        if p == "human":
            return f"错误: {check_agent_name('human')}"
    return None


def _discover_viewers(viewers_dir):
    """发现 viewers 目录（多视角产品约定）：*.md 文件名即 agent 名。

    返回 (participants, briefs)——participants 按文件名排序（决定 starter/
    RR 轮转与默认 resultWriter）；briefs = {agent: 视角任务书正文}。
    目录不存在/无文件 → (None, None)（调用方决定报错或回退）。
    """
    if not os.path.isdir(viewers_dir):
        return None, None
    names = sorted(
        f[:-3] for f in os.listdir(viewers_dir)
        if f.endswith(".md") and not f.startswith("."))
    if not names:
        return None, None
    briefs = {}
    for n in names:
        with open(os.path.join(viewers_dir, f"{n}.md")) as f:
            briefs[n] = f.read().strip("\n")
    return names, briefs


def resolve_fork_source():
    """从主 pi 环境（PI_SESSION_ID）解析当前 session 文件绝对路径
    （fork-only 启动必需，2026-09-09 从 wrapper 收归——session 文件发现
    逻辑与 _detect_pi_model_thinking 同款路径约定）。

    sessions 目录编码 = "--" + 去首尾斜杠内斜杠换 "-" + "--"。
    返回 (fork_source 或 None, error)——PI_SESSION_ID 未注入/文件缺失
    都是明确错误（fork-only 无静默退化）。
    """
    sid = os.environ.get("PI_SESSION_ID") or ""
    if not sid:
        return None, ("错误: PI_SESSION_ID 未注入——多视角分析必须在主 pi "
                      "session 内经 wrapper 启动（见 README 环境要求）")
    sdir = pi_sessions_dir(os.getcwd())
    enc = os.path.basename(sdir)
    try:
        cands = sorted(f for f in os.listdir(sdir) if f.endswith(".jsonl"))
    except OSError:
        cands = []
    hits = [f for f in cands if sid in f]
    if not hits:
        return None, (f"错误: 未找到主 session 文件（{enc}/*_{sid}.jsonl）"
                      "——fork-only 模式必须挂载主 session")
    return os.path.join(sdir, hits[-1]), None


def _snapshot_viewers(spec_dir, viewers_dir):
    """viewers 校验（prepare 时点）+ 快照进 spec/agents/（单一事实源：
    所有视角都源自 viewers/，spec agents/ = 本场快照，可按场修改，
    分析结束 spec 即删、资产永续；用户 2026-09-09 设计）。

    校验（任何违规 → 返回错误，不产生任何 spec 文件——用户：不合规
    根本不应该开始 spec-gen）：目录存在 / ≥2 个合法 .md（排除隐藏）/
    无 human / 名字合法（空白/路径分隔符/≤32）。
    快照文件含说明行首行（spec 约定：_spec_read 跳过首行——裸拷贝会
    把正文首行当说明吃掉，实测缺口）。
    返回 (participants, error)。
    """
    names, _briefs = _discover_viewers(viewers_dir)
    if names is None:
        return None, ("错误: 未找到 viewers/ 目录——多视角分析的视角资产"
                      "必须先建好（项目 cwd 下 viewers/<视角名>.md，至少 2 个）")
    for n in names:
        err = check_agent_name(n)
        if err:
            return None, f"错误: 非法 agent 名（{err}）：{n}（viewers/{n}.md）"
    if len(names) < 2:
        return None, (f"错误: viewers/ 下仅发现 {len(names)} 个视角"
                      f"——多视角分析至少需要 2 个")
    agents_dir = os.path.join(spec_dir, "agents")
    os.makedirs(agents_dir, exist_ok=True)
    for n in names:
        with open(os.path.join(viewers_dir, f"{n}.md")) as f:
            brief = f.read()
        with open(os.path.join(agents_dir, f"{n}.md"), "w") as f:
            f.write(f"# {n}.md——快照自 viewers/{n}.md（本行说明不注入；"
                    f"按场修改这里，不影响 viewers/ 资产）\n\n")
            f.write(brief)
    with open(os.path.join(agents_dir, ".order"), "w") as f:
        f.write("\n".join(names) + "\n")
    return names, None


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
            extra = sorted(
                f[:-3] for f in os.listdir(agents_dir)
                if f.endswith(".md") and f[:-3] not in listed_set)
            participants = listed + extra
        else:
            participants = sorted(
                f[:-3] for f in os.listdir(agents_dir) if f.endswith(".md"))
        if not participants:
            return None, None, None, "错误: spec/agents/ 下没有 agent 定义文件"
    else:
        # viewers 发现（多视角产品约定）：cwd/viewers/*.md，文件名即 agent 名
        participants, viewer_briefs = _discover_viewers(viewers_dir)
        if participants is None:
            return None, None, None, (
                "错误: spec 缺少 agents/ 且未找到 viewers/ 目录"
                "（项目 cwd 下建 viewers/<视角名>.md，或 --spec-gen --agents 生成）")
        err_names = validate_participants(participants)
        if err_names:
            return None, None, None, err_names
        # meeting 至少两个 LLM agents（用户 2026-09-09）：1 个视角无对话可言
        if len(participants) < 2:
            return None, None, None, (
                f"错误: viewers/ 下仅发现 {len(participants)} 个视角"
                f"（{', '.join(participants)}）——多视角分析至少需要 2 个")
    # spec 必须有 question.md（讨论起点不可缺）
    if not os.path.isfile(os.path.join(spec_dir, "question.md")):
        return None, None, None, "错误: spec 缺少 question.md（讨论起点，先 --spec-gen 生成）"
    # 空正文校验（审核#19）：删到只剩说明行 → 无讨论主题（CLI 路径有
    # --topic 必填对等约束）
    if not (_spec_read(spec_dir, "question.md") or "").strip():
        return None, None, None, "错误: spec 的 question.md 正文为空（讨论起点不可缺）"
    return spec_dir, participants, viewer_briefs, None


def _clone_work(base, p):
    """clone work-<p> + 配置 git 身份 + 建本地目录（T6 后唯一 clone 入口）。

    git 身份在此统一配置（调用方不再重复 config——e2e7 评审 T6）。
    """
    workdir = os.path.join(base, f"work-{p}")
    run_cmd(["git", "clone", os.path.join(base, "repo.git"), workdir])
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
            # 兼容两种措辞（make_spec fixture 用旧版"讨论主题"）
            for prefix in ("# 分析主题：", "# 讨论主题："):
                if line.startswith(prefix):
                    spec_topic = line.replace(prefix, "").strip()
                    break
            if spec_topic:
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
    run_cmd(["git", "init", "--bare", os.path.join(base, "repo.git")])

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
                               fork_mode=getattr(args, "fork_mode", "active")),
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
    run_cmd(["git", "push", os.path.join(base, "repo.git"),
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
    shutil.rmtree(base)
    print(f"[cleanup] 已删除目录 {base}（含 pi-sessions）")


def _loops_alive(base):
    """讨论的 loop 进程是否存活（目录边界匹配，防 discussion-1 匹配 -1x）。"""
    r = run_cmd(["pgrep", "-f",
             f"meeting_loop.py.*{re.escape(base)}( |$|/)"], check=False)
    return bool(r.stdout.strip())


def check_status(base):
    """讨论状态（单值；状态全集显式于此，T3/#7 修复 e2e7 评审）：

      not-exists   无 bare（目录不存在/未创建）
      done         result.md + concluded（权威收尾完成）
      running      有 loop 存活（讨论中 / 收尾中——收尾中细分见下）
      stalled      有 result.md 无 concluded 且 **loop 均不存活**
                   （收尾中断：rw 崩溃在 result.md 之后、concluded 之前）
      stopped      无 result.md 且 loop 不存活（未启动/中断）

    修复动因（e2e7 评审 T3）：原实现"有 result.md 无 concluded"恒返回
    running 且不看存活 → "收尾进行中"与"收尾间隙崩溃"不可区分，--wait
    无限轮询（无终止上界）。现 stalled 使 --wait 有界退出。
    移除恒 None 第二返回值（#7 装饰性契约）——信息由状态本身表达。
    """
    bare = os.path.join(base, "repo.git")
    if not os.path.isdir(bare):
        return "not-exists"
    r = run_cmd(["git", "log", "--all", "--format=%H", "--", "result.md"],
            cwd=bare, check=False)
    if r.stdout.strip():
        # done 需 concluded 存在（review5 A5）——rw 写 result.md 后、
        # concluded 前崩溃 → 只保存报告但未收尾，误报完成会丢流程语义。
        # 结构化检查：读 HEAD 树消息文件 frontmatter 的 type（不用
        # git grep 全文——正文出现 "type: concluded" 会误匹配）。
        # 排除 work-human（human 插话若带 type: concluded 会误判 done——
        # human 视而不见原则，e2e7 评审指出 grep */*.md 扫全树含 human/）
        r2 = run_cmd(["git", "grep", "-l", "^type: concluded$", "HEAD", "--",
                  ":(exclude)human/*"], cwd=bare, check=False)
        if r2.stdout.strip():
            return "done"
        # 有 result.md 无 concluded：看 loop 存活区分收尾中/收尾中断
        return "running" if _loops_alive(base) else "stalled"
    return "running" if _loops_alive(base) else "stopped"


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
    if "human" in participants:
        return None, _check_reserved(participants)
    return participants, None


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
    parser.add_argument("--fork-mode", default="active",
                        choices=["active", "full", "curated"],
                        help="fork 裁剪策略：active=压缩态（默认，条目级）；"
                             "full=全量；curated=预算裁剪+折叠（长会话可行）")
    parser.add_argument("--fork-source", default=None,
                        help="主 session 文件绝对路径（fork-only）：写入 "
                             "protocol.json，各 agent 首唤用活跃视图挂载主 "
                             "上下文；metadata 不传 = 从主 pi 环境自动解析"
                             "（PI_SESSION_ID；解析失败明确报错）")
    parser.add_argument("--pure", action="store_true", help="--pure 模式（禁外部插件）")
    parser.add_argument("--start", action="store_true", help="创建后启动讨论")
    parser.add_argument("--skip-setup", action="store_true",
                        help="跳过环境生成，只启动已有环境（需 --dir）")
    parser.add_argument("--cleanup", action="store_true", help="清理讨论（目录，含 pi-sessions）")
    parser.add_argument("--status", action="store_true", help="检查讨论状态")
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
        print(f"[status] {check_status(base)}")
        return
    if args.wait:
        # T1 收归（e2e7 评审）：进展展示复用 human_viewer.incremental
        # （原内联 65 行自行 git log 全量 + 手工解析 frontmatter——与
        # viewer 两套输出格式、非增量、概念丢失）。incremental 走
        # since..HEAD 增量 + 统一 format_message。
        import human_viewer
        sys.stdout.reconfigure(line_buffering=True)
        print(f"[wait] 等待讨论完成: {base}")
        bare = os.path.join(base, "repo.git")
        agents = human_viewer.participants_from_bare(bare) or []
        since = ""   # 首次全量（--wait 一次性观察，无游标持久需求）
        first = True
        while True:
            state = check_status(base)
            if state == "stalled":
                print("[wait] 收尾中断（result.md 已提交、concluded 缺失、"
                      "无 loop 存活）——停止等待；可读 result.md 或 --cleanup")
                return 1
            if state == "not-exists":
                print(f"[wait] 讨论不存在: {base}")
                return 1
            _mode, lines, head, done = human_viewer.incremental(
                bare, agents, since)
            if done:
                for line in lines:
                    print(line)
                    print()
                print("[wait] 讨论完成 ✅")
                rw = ""
                r = run_cmd(["git", "show", "HEAD:protocol.json"], cwd=bare,
                        check=False)
                if r.returncode == 0:
                    try:
                        rw = json.loads(r.stdout).get("resultWriter", "")
                    except ValueError:
                        rw = ""
                rp = os.path.join(base, f"work-{rw}", "result.md") if rw else ""
                print(f"[wait] result.md: {rp}")
                return 0
            for line in lines:
                print(f"[wait] {time.strftime('%H:%M:%S')} 新进展:")
                print(line)
                print()
            since = head or since
            if first:
                first = False
            time.sleep(10)
    # 创建（--start 总是 setup；--skip-setup = 跳过创建，只启动已有环境）
    if args.skip_setup:
        if not os.path.exists(base):
            print(f"错误: 环境不存在 {base}")
            sys.exit(1)
        print(f"[start] 跳过环境生成——只启动已有环境")
        # 参与者从已有环境的 protocol.json 读（单一事实源，不依赖 CLI）
        try:
            r = run_cmd(["git", "show", "HEAD:protocol.json"],
                    cwd=os.path.join(base, "repo.git"), check=False)
            participants = json.loads(r.stdout).get("participants", [])
        except (ValueError, OSError):
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
        print(f"[start] 已启动 {len(procs)} 个 meeting loop 进程（log: {base}/loop-*.log）")


if __name__ == "__main__":
    # 传递 main 返回码（--wait 超时返回 1——此前被丢弃，wrapper 判据失效）
    sys.exit(main() or 0)
