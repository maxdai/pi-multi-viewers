"""spec 生成层——question/骨架/agents 快照（从 start_discussion 拆出，S2）。

职责：`mv.sh --prepare` 与 CLI `--spec-gen` 的全部产物生成——
question.md、models.md 骨架、viewers 校验与快照、agent 定义文件。
**单一职责**：只生成"分析开始前"的静态产物；环境创建（clone/bare）
与运行期状态属于 start_discussion 主文件。

依赖方向：只准 import meeting_core / meeting_fs（不准 import 主文件、
不准互相 import——见 start_discussion 顶部分层注释）。
"""

import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TPL_DIR = os.path.join(HERE, "templates")
PI_AGENT_DIR = os.environ.get("PI_CODING_AGENT_DIR",
                             os.path.expanduser("~/.pi/agent"))
MAX_AGENT_NAME_LEN = 32

import meeting_fs
from meeting_fs import run_git, DEFAULT_STALL_TIMEOUT


def _join_model_ref(provider, model_id):
    """按 pi 契约把 (provider, model_id) 拼成完整 model ref。

    契约（pi 源码 resolveSpawnContext）：PI_PROVIDER=provider、
    PI_MODEL=model id；session 的 model_change 同样分 provider/modelId
    两字段；settings 的 defaultModel/defaultProvider 同构。**model id 本身
    可含 '/'**（聚合类 provider 的命名空间 id，如 commandcode-goat 的
    "deepseek/deepseek-v4-flash"）——因此拼接是**无条件**的字段拼接，
    不得按"是否含斜杠"猜形状（形状启发式会把 provider 丢掉，解析到
    同名的另一个 provider，静默失真；2026-09-10 实测 fix）。

    provider 缺失时只能原样返回（无法拼接）——这是调用方应保证的前置。
    """
    provider = (provider or "").strip()
    model_id = (model_id or "").strip()
    if not model_id:
        return ""
    if not provider:
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
    # session 文件兜底（查找规则单点：current_session_file）
    try:
        sf = current_session_file()
        if sf:
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


def pi_sessions_dir(cwd):
    """主 pi session 目录（编码约定单点，T7 收归 e2e7 评审）。

    pi 的 session 目录编码 = "--" + 去首尾斜杠 + 内斜杠换 "-" + "--"
    （/root/x → --root-x--；/tmp → --tmp--）。此前该约定在
    _detect_pi_model_thinking 与 resolve_fork_source 两处字面量重复，
    AGENTS.md 明言"编码错一根横线 = 静默解析不到"——风险点不应复制。
    """
    enc = "--" + cwd.strip("/").replace("/", "-") + "--"
    return os.path.join(PI_AGENT_DIR, "sessions", enc)


def current_session_file():
    """当前主 pi 的 session 文件路径（解析不到 → 空串）。

    **查找规则的唯一实现**（此前两处各写一遍：resolve_fork_source 按
    PI_SESSION_ID 匹配文件名、_detect_pi_model_thinking 兜底取目录内
    **字典序最后**——同一概念两套规则，兜底可能选到别的 session）。
    规则：PI_SESSION_FILE（pi 直接给的路径，最精确）→ PI_SESSION_ID
    匹配文件名 → 目录内字典序最后（都无法确认时只能如此，调用方自决
    是否接受）。
    """
    sf = os.environ.get("PI_SESSION_FILE") or ""
    if sf and os.path.isfile(sf):
        return sf
    sdir = pi_sessions_dir(os.getcwd())
    try:
        cands = sorted(f for f in os.listdir(sdir) if f.endswith(".jsonl"))
    except OSError:
        cands = []
    if not cands:
        return ""
    sid = os.environ.get("PI_SESSION_ID") or ""
    if sid:
        hits = [f for f in cands if sid in f]
        if hits:
            return os.path.join(sdir, hits[-1])
    return os.path.join(sdir, cands[-1])


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
                      "session 内经 wrapper 启动。若确实在 session 内，"
                      "检查是否有扩展接管了 bash 工具（如 AFT 的 "
                      "`~/.config/cortexkit/aft.jsonc` 未设 \"bash\": false）"
                      "——接管后 pi 的环境变量不会传入 bash")
    # 委托 current_session_file（查找规则单点）；这里只管错误语义
    path = current_session_file()
    if not path or sid not in os.path.basename(path):
        sdir = pi_sessions_dir(os.getcwd())
        enc = os.path.basename(sdir)
        return None, (f"错误: 未找到主 session 文件（{enc}/*_{sid}.jsonl）"
                      "——fork-only 模式必须挂载主 session")
    return path, None


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


def gen_question(topic, stances, background, questions):
    """question.md（讨论起点：话题 + 可选立场 + 待回答问题）。

    分层（2026-08-09）：background 移到 AGENTS.md（共享，system prompt）；
    立场保持在此（非强制、可被说服，不进 system prompt）。
    """
    # 措辞与 spec 骨架（gen_spec_skeleton）、spec-readme 模板、prompt 统一为
    # "# 分析主题："——**单一措辞**，消费端只认它（此前生产路径产
    # "# 讨论主题：" 而消费端写兼容循环兜两种，根因却是生产自己在产旧措辞）
    lines = [f"# 分析主题：{topic}", ""]
    if stances:
        lines += ["## 初始立场", "每个参与者有自己的初始立场（可被论据说服）：", ""]
        for k, v in stances.items():
            lines.append(f"- {k}: {v}")
        lines += ["", "开场时请声明你的立场，然后参与讨论。"]
    if questions:
        lines += ["", "## 待回答的问题"] + [f"- {q}" for q in questions] + [""]
    return "\n".join(lines)


def gen_protocol(topic, participants, max_meeting, max_rr, pure=False,
                 result_writer=None,
                 stall_timeout=DEFAULT_STALL_TIMEOUT,
                 fork_source=None, fork_cwd=None,
                 fork_mode=meeting_fs.DEFAULT_FORK_MODE):
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
        # forkMode 取值域与默认值的定义在 meeting_fs（FORK_MODES /
        # DEFAULT_FORK_MODE，单一事实源）；此处只写入选定值
        proto["forkSource"] = fork_source
        proto["forkCwd"] = fork_cwd or os.getcwd()
        proto["forkMode"] = fork_mode
    return proto


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


def list_agent_md(d):
    """列出目录下的 agent 定义文件名（去 `.md`、**排除隐藏文件**、排序）。

    **列举规则的唯一实现**——viewers/ 与 spec/agents/ 两条来源共用。
    为什么排除隐藏文件：`.draft.md` 之类会被当成参与者（名为 `.draft`，
    点号不在名字规则的禁止集内）静默进入讨论。此前只有 viewers 分支
    排除、spec/agents 的两处列举没排除（实测缺口）。
    """
    return sorted(f[:-3] for f in os.listdir(d)
                  if f.endswith(".md") and not f.startswith("."))


def validate_participants(participants):
    """整组名字校验（**名字规则的唯一入口**）。返回错误或 None。

    覆盖：非空 / 无路径分隔符与空白 / ≤32 / 非 human 保留名。
    CLI（--agents）与 spec/viewers（文件名）三条来源路径都经此。
    """
    for p in participants:
        err = check_agent_name(p)
        if err:
            return f"错误: 非法 agent 名（{err}）：{p}"
    return None


def viewer_set_error(names, empty, where="viewers/"):
    """viewers 集合级校验（**唯一实现**）：空正文视角 + 至少 2 个。

    where: 报错时指路的目录前缀（"viewers/" 或 "spec/agents/"）。
    返回错误文本或 None。为什么单点：同一套规则曾在 prepare 快照路径与
    spec 解析路径各写一遍，文案与检查项已经漂移（实测：非法名文案带不带
    文件名后缀不一致、≥2 检查一处列参与者一处不列）。
    """
    if empty:
        # 空视角 = 没有 lenses 的 agent：行为由模型自由发挥，多视角退化成
        # "同名随机视角"——静默退化，与无静默铁律相悖（占位文件忘写是常见成因）
        detail = "、".join(f"{where}{n}.md（{why}）" for n, why in empty)
        return (f"错误: {detail}——视角任务书不能为空"
                f"（写清该视角用什么 lenses 看分析对象）")
    if len(names) < 2:
        return (f"错误: {where} 下仅发现 {len(names)} 个视角"
                f"（{', '.join(names)}）——多视角分析至少需要 2 个")
    return None


def _discover_viewers(viewers_dir):
    """发现 viewers 目录（多视角产品约定）：*.md 文件名即 agent 名。

    返回 (participants, briefs, errors)——participants 按文件名排序（决定
    starter/RR 轮转与默认 resultWriter）；briefs = {agent: 视角任务书正文}；
    errors = [(name, 原因)]（空/纯空白视角——这类视角无 lenses，会让多视角
    退化成同名随机视角，属静默退化，必须报错而非放行）。
    目录不存在/无文件 → (None, None, [])（调用方决定报错或回退）。
    """
    if not os.path.isdir(viewers_dir):
        return None, None, []
    names = list_agent_md(viewers_dir)
    if not names:
        return None, None, []
    briefs, errors = {}, []
    for n in names:
        with open(os.path.join(viewers_dir, f"{n}.md"), encoding="utf-8") as f:
            brief = f.read().strip("\n")
        if not brief.strip():
            errors.append((n, "空视角任务书（没有任何视角内容）"))
            continue
        briefs[n] = brief
    return names, briefs, errors


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
    names, _briefs, empty = _discover_viewers(viewers_dir)
    if names is None:
        return None, ("错误: 未找到 viewers/ 目录——多视角分析的视角资产"
                      "必须先建好（项目 cwd 下 viewers/<视角名>.md，至少 2 个）")
    err = validate_participants(names) or viewer_set_error(names, empty)
    if err:
        return None, err
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
