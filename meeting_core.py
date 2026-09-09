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
    return all(t == "all-freezing" for t in all_last_types.values())


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
        return "meeting"   # 空 → 无任何 agent → 无冻结无 RR（review5 F3）
    types = {a: (v["type"] if v else None) for a, v in all_last.items()}
    modes = {a: (v["mode"] if v else None) for a, v in all_last.items()}
    if any(t == "concluded" for t in types.values()):
        return "concluded"
    if any(m == "round-robin" for m in modes.values()):
        return "round-robin"
    if all(t in ("freezing", "all-freezing") for t in types.values()):
        return "all-freezing"
    return "meeting"
