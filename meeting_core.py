"""meeting 模式核心逻辑（纯函数，无 I/O）——阶段 1 确定性单测对象。

设计原则（RR 教训）：
- 流程控制归确定性代码，不依赖 LLM
- 判定函数只吃数据结构（dict/list），I/O 在 meeting_fs.py（本模块不碰文件）
- 单调性：冻结/af 判定只看"最后一条消息"，陈旧视图只会延迟不会误判

消息 frontmatter 关键字段（meeting 模式）：
    from: 作者
    type: message | freezing | all-freezing | pass | concluded
    mode: meeting | all-freezing | round-robin | concluded
    to:   目标（meeting 专用：单个 agent 或 all；缺省 = all）
    seen_at: 消息生成时的 git HEAD
"""

MEETING_TYPES = {"message", "freezing", "all-freezing", "pass", "concluded"}
"""meeting 模式合法 type 集合（校验器白名单）"""

# ---------------------------------------------------------------
# 状态机词汇（**字面量的家**）——消息 type / 全局 mode 的合法值。
# 谁消费：core（全部判定）、engine（信号写点 + 分支）、loop（fatal 文案）、
# viewer（展示）、fake_agent（responder 决策池）。
# 为什么建家：这些字符串是**状态机的词表**，任何一处拼写漂移 = 判定静默
# 失效。曾是 46 处字面量散在 6 个文件（e2e15 自审 S3）。
# 注意：**不要**为了用常量而替换注释/日志文案里的词（那是给人读的）——
# 只替换"参与判定的值"。
# ---------------------------------------------------------------
T_MESSAGE = "message"
T_FREEZING = "freezing"
T_ALL_FREEZING = "all-freezing"
T_PASS = "pass"
T_CONCLUDED = "concluded"
M_MEETING = "meeting"
M_ROUND_ROBIN = "round-robin"
M_ALL_FREEZING = T_ALL_FREEZING          # mode 值与 type 值同形（af 复用）
M_CONCLUDED = T_CONCLUDED

# ---------------------------------------------------------------
# 1. 冻结判定（阶段 1.1）
# ---------------------------------------------------------------

def last_message_type(messages):
    """给定某 agent 的全部消息（按序号排序），返回最后一条的 type。

    messages: list[dict]（每项含 type 字段，按时间/序号升序）
    返回: str | None（无消息 → None）
    """
    if not messages:
        return None
    return messages[-1].get("type")


def is_all_last(by_agent, predicate):
    """通用判定：所有参与者最后一条消息都满足 predicate。

    by_agent: {agent: list[dict]}（每个 agent 的完整消息列表，按序号升序）
    predicate: callable(type_str) -> bool
    返回: bool
    """
    for agent, msgs in by_agent.items():
        t = last_message_type(msgs)
        if t is None:          # 有人从未发言 → 判定不成立
            return False
        if not predicate(t):
            return False
    return True


def is_all_last_in(by_agent, types):
    """所有参与者最后一条消息 ∈ types（宽松确认：freezing 或 af 均可）。"""
    allowed = set(types)
    return is_all_last(by_agent, lambda t: t in allowed)


# ---------------------------------------------------------------
# 2. 读取点（阶段 1.2）
# ---------------------------------------------------------------

def read_point_seen_at(messages):
    """读取点 = 最后一条消息的 seen_at（用户 10030 定案）。

    messages: list[dict]（按序号升序）
    返回: str | ""（无消息 → "" 从根读起）

    语义：seen_at = loop 交给 pi 处理的那批新消息、且 pi
    run 正确写入新消息后由 loop 填入的值。loop 自己写的新消息（协议信号）
    沿用上一个 seen_at、不推进——所以无论最后一条是 LLM 消息还是 loop
    消息，它的 seen_at 都是 LLM 最后处理点，直接取即可（用户 10024：
    不需要反序找 + 类型判断，直接通过 seen_at）。
    """
    if not messages:
        return ""
    return messages[-1].get("seen_at", "")


# ---------------------------------------------------------------
# 3. 校验器（阶段 1.3）
# ---------------------------------------------------------------

def validate_and_fix(frontmatter, agent, mode, head,
                     allow_protocol_types=False, loop_message=False):
    """校验并确定性修复一条消息的 frontmatter（meeting 版）。

    frontmatter: dict（agent 写的原始 frontmatter，可变）
    agent: 本 agent 名（from 应等于它）
    mode: 当前模式（meeting 等，确定性修复 mode 字段）
    head: 当前 git HEAD（seen_at 应等于它——仅 LLM 消息）
    allow_protocol_types: 是否放行引擎专用 type（all-freezing/concluded）。
        False（默认，LLM 路径 commit_new_files）：只放行 {message, freezing,
        pass}——协议信号全归 loop（设计 11.8），LLM 写引擎专用 type
        （如 concluded）会绕过收尾、result.md 未生成（review4 M1）。
        True（协议信号路径 write_protocol_signal）：完整 MEETING_TYPES。
    loop_message: 是否 loop 自己写的消息（协议信号）。True 时 seen_at
        不强制为 head——沿用上一个（用户 10030 定案：seen_at = LLM 处理
        过的最新位置，loop 消息不推进）。

    确定性修复（不依赖 LLM 判断，直接写）：
      - from: 缺失/错误 → agent 名
      - seen_at: 缺失 → head（LLM 消息）；loop 消息保留沿用值
      - mode: 缺失 → 当前模式
    语义校验（非法 → 报告，由调用方决定重写唤醒）：
      - type: 必须在白名单内（按来源区分，M1）
      - to: 非法目标 → "all"（容错）

    返回: (fixed_frontmatter, errors: list[str])
    """
    errors = []

    # --- 确定性修复 ---
    if not frontmatter.get("from") or frontmatter.get("from") != agent:
        frontmatter["from"] = agent
    if loop_message:
        # loop 消息：seen_at 沿用（write_protocol_signal 已传读取点）——
        # 只兜底缺失（首条 loop 消息无读取点 → head），不强制为 head
        if not frontmatter.get("seen_at"):
            frontmatter["seen_at"] = head
    elif not frontmatter.get("seen_at") or frontmatter.get("seen_at") != head:
        # LLM 消息：seen_at = 该轮 head（LLM 确实处理到那了）
        frontmatter["seen_at"] = head
    # mode 无条件覆盖为当前阶段（LLM 不该决定状态）——RR 阶段 LLM 照抄
    # 模板的 "mode: meeting" 会导致阶段回退（设计 11.5）
    frontmatter["mode"] = mode

    # --- 语义校验 ---
    t = frontmatter.get("type")
    allowed = (MEETING_TYPES if allow_protocol_types
               else {"message", "freezing", "pass"})
    if t not in allowed:
        errors.append(f"type '{t}' 非法，应为 {sorted(allowed)}")
    # to 无条件强制 all（审核#3）：设计决策"to 定向暂不启用，全 to all"——
    # 消除死参数（此前只容错非法，LLM 写其他值仍是无声死参数）
    frontmatter["to"] = "all"

    return frontmatter, errors


# ---------------------------------------------------------------
# 4. 触发条件（阶段 1.4）
# ---------------------------------------------------------------

def has_new_messages_for_me(new_messages, me):
    """meeting 触发条件：有新消息来自别人，且定向匹配我。

    new_messages: list[dict]（每条含 from/to 字段）
    me: 本 agent 名
    返回: bool
    定向匹配：to == me 或 to == all 或 to 缺省（缺省 = all）
    """
    for m in new_messages:
        f = m.get("from")
        if f == me:
            continue                      # 自己的消息不触发
        to = m.get("to", "all")
        if to == me or to == "all":
            return True
    return False


# ---------------------------------------------------------------
# 5. 冻结级联决策（阶段 3）——确定性判定，归 loop 不归 LLM
# ---------------------------------------------------------------


def next_in_order(order, agent):
    """轮转顺序的下一位（无 I/O 纯表达式）。

    RR（round-robin）阶段的 next 字段来源：**协议状态**（LLM 不知道，
    由 loop 确定性补写）。同一表达式此前在 engine 出现 4 处（starter 首轮、
    消息修复路径、RR 表态 ×2）——收成一处便于单测与语义统一。
    边界：agent 不在 order 中 → ValueError（那是调用方的编程错误，
    不静默返回首位——静默会打乱轮转链）。
    """
    return order[(order.index(agent) + 1) % len(order)]


def meeting_speak_count(messages, agent):
    """该 agent 的 meeting 内容发言轮（**配额消耗口径**，单一实现）。

    数 `mode == meeting` 且 `type == message` 的消息——LLM 的内容发言。
    代写 freezing / all-freezing / pass 等流程信号不计入（设计 11.1）。
    从共享事实（bare 派生的 messages）推导——loop 崩溃/重启不丢配额。

    messages: {agent: [frontmatter_dict, ...]}（engine.each_agent_messages 的产物）。
    **这是唯一实现**：此前 engine 私有一份 `_meeting_speak_count`，而
    `--report` 又按 commit subject 另数一遍（两套口径，实测报告出过
    "meeting 6/2" 的超限假象——6 是消息总数、2 是配额上限）。
    """
    return sum(1 for fm in messages.get(agent, [])
               if fm.get("mode") == M_MEETING and fm.get("type") == T_MESSAGE)


def frozen_agents(agents, all_last_types):
    """已冻结的 agent 列表（按 participants 顺序）——**冻结集合单一实现**。

    all_last_types: {agent: type|None}（aggregate_mode 的入参形态）。
    冻结 = type ∈ {freezing, all-freezing}（af 是冻结级联的推进态，
    与 engine 循环里"我已 af 则跳过"的宽松语义一致）。
    消费者：`--report`（现场算冻结进度）；engine 循环内的 others_frozen
    判定仍就地写（热路径、语义略有差别——那里是"除我之外全冻结"）。
    """
    return [a for a in agents
            if all_last_types.get(a) in ("freezing", "all-freezing")]


def should_write_af(all_last_types):
    """我是否应写 all-freezing：所有参与者最后一条都是 freezing（或 af）。

    all_last_types: {agent: type|None}（None = 从未发言）
    语义（主协议 7.2 + 异步演进修正）：
    - 严格版（全员都是 freezing）会卡死——a 先写 af 后，b 看到
      a=af, b/c=freezing → 永远不写 af（异步中间态，阶段 1 属性测试结论）。
    - 宽松版：全员冻结（freezing 或 af）即写 af——异步下自然收敛。
    """
    if not all_last_types:
        return False
    if any(t is None for t in all_last_types.values()):
        return False   # 有人从未发言 → 不能全员冻结
    return all(t in ("freezing", "all-freezing") for t in all_last_types.values())


def can_start_rr(all_last_types):
    """starter 是否可启动 RR：所有参与者最后一条都是 all-freezing。

    all_last_types: {agent: type|None}（None = 从未发言）
    语义（主协议 7.3）：单条件，"所有 agent 最后一条都是 af"。
    """
    if not all_last_types:
        return False
    if any(t is None for t in all_last_types.values()):
        return False
    return all(t == T_ALL_FREEZING for t in all_last_types.values())


def aggregate_mode(all_last):
    """全局模式 = 聚合所有 agent 最后一条消息（设计 11.8，审核#6 下沉 core）。

    all_last: {agent: {type, mode, ...}|None}（每 agent 最后一条消息）
    返回: "concluded" | "round-robin" | "all-freezing" | "meeting"
    1. 任一 type == concluded → concluded
    2. 任一 mode == round-robin → round-robin（pass 存在）
    3. 所有 type ∈ {freezing, af} → all-freezing
    4. 否则 → meeting
    """
    if not all_last:
        return M_MEETING   # 空 → 无任何 agent → 无冻结无 RR（review5 F3）
    types = {a: (v["type"] if v else None) for a, v in all_last.items()}
    modes = {a: (v["mode"] if v else None) for a, v in all_last.items()}
    if any(t == T_CONCLUDED for t in types.values()):
        return T_CONCLUDED
    if any(m == M_ROUND_ROBIN for m in modes.values()):
        return M_ROUND_ROBIN
    if all(t in (T_FREEZING, T_ALL_FREEZING) for t in types.values()):
        return T_ALL_FREEZING
    return "meeting"
