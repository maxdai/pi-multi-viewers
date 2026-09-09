"""meeting_core 阶段 1 单测——覆盖 1.1-1.4 全部函数 + 边界 + 单调性。

运行：python3 -m unittest discover -s tests -v
"""

import unittest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meeting_core import (
    last_message_type,
    is_all_last, is_all_last_in,
    read_point_seen_at,
    validate_and_fix, MEETING_TYPES,
    has_new_messages_for_me, should_write_af, can_start_rr,
    aggregate_mode,
)

HEAD = "abc123"
HEAD2 = "def456"


def msg(type_, seen_at=HEAD, mode="meeting", from_="x", to=None):
    """构造一条消息 dict。"""
    d = {"type": type_, "seen_at": seen_at, "mode": mode, "from": from_}
    if to is not None:
        d["to"] = to
    return d


# ---------------------------------------------------------------
# 1.1 冻结判定
# ---------------------------------------------------------------

class TestFreeze(unittest.TestCase):

    def test_last_message_type_basic(self):
        self.assertEqual(last_message_type([]), None)
        self.assertEqual(last_message_type([msg("message")]), "message")
        self.assertEqual(last_message_type([msg("message"), msg("freezing")]), "freezing")

    def test_is_all_last_freezing_true(self):
        by_agent = {
            "a": [msg("message"), msg("freezing")],
            "b": [msg("message"), msg("freezing")],
            "c": [msg("message"), msg("freezing")],
        }
        self.assertTrue(is_all_last_in(by_agent, {"freezing"}))

    def test_is_all_last_freezing_false_any_message(self):
        # c 最后一条是 message（发言打破冻结）→ False
        by_agent = {
            "a": [msg("freezing")],
            "b": [msg("freezing")],
            "c": [msg("message")],
        }
        self.assertFalse(is_all_last_in(by_agent, {"freezing"}))

    def test_is_all_last_freezing_false_silent(self):
        # c 从未发言（容忍缺席）→ 判定不成立（不能视为冻结）
        by_agent = {
            "a": [msg("freezing")],
            "b": [msg("freezing")],
            "c": [],
        }
        self.assertFalse(is_all_last_in(by_agent, {"freezing"}))

    def test_is_all_last_af(self):
        by_agent = {
            "a": [msg("all-freezing")],
            "b": [msg("all-freezing")],
            "c": [msg("all-freezing")],
        }
        self.assertTrue(is_all_last_in(by_agent, {"all-freezing"}))
        # 有人还停在 freezing → False（af 是确认屏障）
        by_agent["c"] = [msg("freezing")]
        self.assertFalse(is_all_last_in(by_agent, {"all-freezing"}))

    def test_is_all_last_pass(self):
        by_agent = {
            "a": [msg("pass")],
            "b": [msg("pass")],
            "c": [msg("pass")],
        }
        self.assertTrue(is_all_last_in(by_agent, {"pass"}))
        by_agent["b"] = [msg("message")]  # 发言打破 → False
        self.assertFalse(is_all_last_in(by_agent, {"pass"}))

    def test_is_all_last_in_loose(self):
        # 宽松：freezing 或 af 都算（非 starter 确认收尾）
        by_agent = {
            "a": [msg("freezing")],
            "b": [msg("all-freezing")],
            "c": [msg("freezing")],
        }
        self.assertTrue(is_all_last_in(by_agent, {"freezing", "all-freezing"}))

    def test_monotonicity(self):
        """单调性：只会看到更多 freezing/af，不会变少。

        核心：如果判定在完整视图下成立，那么在任意陈旧视图下也成立
        （stale 视图 = 缺失一些消息，但已有的消息不可变）。
        """
        full = {
            "a": [msg("message"), msg("freezing"), msg("all-freezing")],
            "b": [msg("message"), msg("freezing"), msg("all-freezing")],
            "c": [msg("message"), msg("freezing"), msg("all-freezing")],
        }
        # 完整视图：全 af → True
        self.assertTrue(is_all_last_in(full, {"all-freezing"}))
        # 陈旧视图（a 还没看到 b/c 的 af，但自己已 af）：
        #   在 a 的视图里 b、c 最后是 freezing → 判定为 False
        #   → a 不会误启动 RR（等下一 pull）→ 单调性：只会延迟，不会误判
        a_view = {
            "a": [msg("message"), msg("freezing"), msg("all-freezing")],
            "b": [msg("message"), msg("freezing")],
            "c": [msg("message"), msg("freezing")],
        }
        self.assertFalse(is_all_last_in(a_view, {"all-freezing"}))
        # 且 af 判定只会在看到更多消息后成立，不会在看到更少时成立：
        #   如果完整视图成立，任何删掉部分消息的视图都不成立（或更晚成立）
        self.assertTrue(is_all_last_in(full, {"all-freezing"}))   # 全量成立
        self.assertFalse(is_all_last_in(a_view, {"all-freezing"}))  # 删消息 → 不成立

    def test_monotonicity_property(self):
        """单调性属性测试：判定随消息补全单调不回退。

        正确不变量（消息不可变 ⟹ 判定不回退）：
        - prefix（截断视图）全 af ⟹ full 全 af（af 是最终类型，补全后仍是 af）
        - prefix 全 freezing ⟹ full 至少 freezing 或 af
        反向不成立是正常的：full 全 af 时 prefix 可能停在 message（陈旧视图
        看不到别人的 af——只会延迟启动，不会误判启动；因为消息不可变，
        任何看到的 af 都是真实 af，不存在"看到假 af"）。
        """
        import random
        random.seed(42)
        for _ in range(200):
            by_agent = {}
            for ag in "abc":
                n = random.randint(1, 4)
                seq = []
                for i in range(n):
                    if i == 0:
                        t = "message"
                    elif i == 1:
                        t = "freezing"
                    else:
                        t = "all-freezing"
                    seq.append(msg(t, seen_at=f"h{i}", from_=ag))
                by_agent[ag] = seq

            full_af = is_all_last_in(by_agent, {"all-freezing"})
            full_fz = is_all_last_in(by_agent, {"freezing"})

            for _ in range(5):
                prefix_view = {}
                for ag, seq in by_agent.items():
                    k = random.randint(0, len(seq))
                    prefix_view[ag] = seq[:k]
                # 不变量 1：prefix 全 af ⟹ full 全 af（判定不回退）
                if is_all_last_in(prefix_view, {"all-freezing"}):
                    self.assertTrue(full_af,
                        f"prefix 全 af 但 full 不是: {by_agent}")
                # 不变量 2：prefix 全 freezing ⟹ full 中每个 agent 都在冻结态或更后
                # （冻结演进允许异步：有人已 af 有人还在 freezing——中间态合法；
                #   真正的单调性是“没人回退”，不是“全体同步”）
                if is_all_last_in(prefix_view, {"freezing"}):
                    self.assertTrue(
                        is_all_last_in(by_agent, {"freezing", "all-freezing"}),
                        f"prefix 全 freezing 但 full 中有人回退到 message: {by_agent}",
                    )

    def test_is_all_last_predicate_custom(self):
        by_agent = {"a": [msg("message")], "b": [msg("message")]}
        self.assertTrue(is_all_last(by_agent, lambda t: t == "message"))
        self.assertFalse(is_all_last(by_agent, lambda t: t == "freezing"))


# ---------------------------------------------------------------
# 1.2 读取点
# ---------------------------------------------------------------

class TestReadPoint(unittest.TestCase):

    def test_read_point_seen_at(self):
        """读取点 = 最后一条消息的 seen_at（用户 10030 定案）。

        不再跳 freezing / 反序找参与消息——loop 消息的 seen_at 沿用
        上一个（不推进），所以最后一条的 seen_at 恒 = LLM 最后处理点，
        直接取即可。
        """
        # 最后是 LLM 消息 → 其 seen_at = 处理点
        msgs = [msg("message", seen_at=HEAD), msg("freezing", seen_at=HEAD2)]
        self.assertEqual(read_point_seen_at(msgs), HEAD2)  # 直接取最后一条
        # 无消息 → ""（从根读起）
        self.assertEqual(read_point_seen_at([]), "")

    def test_read_point_loop_msg_carries_over(self):
        """loop 消息（af/pass/concluded）seen_at 沿用上一个：
        最后是 loop 消息时读取点 = 之前的 LLM 处理点。"""
        # LLM message（seen_at=HEAD）→ loop af（沿用 HEAD，非新 head）
        msgs = [msg("message", seen_at=HEAD),
                msg("all-freezing", seen_at=HEAD)]  # loop 消息沿用
        self.assertEqual(read_point_seen_at(msgs), HEAD)

    def test_read_point_after_participate(self):
        """冻结后恢复参与：新 message 的 seen_at = 恢复时刻（LLM 消息推进）。"""
        msgs = [
            msg("message", seen_at=HEAD),
            msg("freezing", seen_at=HEAD2),
            msg("message", seen_at="fff999"),
        ]
        self.assertEqual(read_point_seen_at(msgs), "fff999")


# ---------------------------------------------------------------
# 1.3 校验器
# ---------------------------------------------------------------

class TestValidator(unittest.TestCase):

    def test_valid_message(self):
        fm = {"type": "message", "from": "a", "seen_at": HEAD, "mode": "meeting"}
        fixed, errors = validate_and_fix(dict(fm), "a", "meeting", HEAD)
        self.assertEqual(errors, [])
        self.assertEqual(fixed["type"], "message")

    def test_valid_freezing(self):
        fm = {"type": "freezing", "from": "a", "seen_at": HEAD, "mode": "meeting"}
        fixed, errors = validate_and_fix(dict(fm), "a", "meeting", HEAD)
        self.assertEqual(errors, [])

    def test_deterministic_fix_from_wrong(self):
        # from 错误 → 修复为 agent 名（不报错，确定性修复）
        fm = {"type": "message", "from": "zzz", "seen_at": HEAD, "mode": "meeting"}
        fixed, errors = validate_and_fix(dict(fm), "a", "meeting", HEAD)
        self.assertEqual(fixed["from"], "a")
        self.assertEqual(errors, [])

    def test_deterministic_fix_seen_at_wrong(self):
        # seen_at 缺失/错误 → 修复为当前 HEAD
        fm = {"type": "message", "from": "a", "mode": "meeting"}
        fixed, errors = validate_and_fix(dict(fm), "a", "meeting", HEAD)
        self.assertEqual(fixed["seen_at"], HEAD)
        fm2 = {"type": "message", "from": "a", "seen_at": "old", "mode": "meeting"}
        fixed2, _ = validate_and_fix(dict(fm2), "a", "meeting", HEAD)
        self.assertEqual(fixed2["seen_at"], HEAD)

    def test_deterministic_fix_mode_missing(self):
        fm = {"type": "message", "from": "a", "seen_at": HEAD}
        fixed, _ = validate_and_fix(dict(fm), "a", "meeting", HEAD)
        self.assertEqual(fixed["mode"], "meeting")

    def test_mode_overwritten_to_current_stage(self):
        """mode 无条件覆盖为当前阶段（设计 11.5）：LLM 照抄模板写错
        mode（如 RR 阶段写 meeting）→ 被覆盖为 round-robin，
        防止阶段回退。"""
        # RR 阶段：LLM 写 pass 但 mode 错写 meeting → 覆盖为 round-robin
        fm = {"type": "pass", "from": "a", "seen_at": HEAD, "mode": "meeting"}
        fixed, errors = validate_and_fix(dict(fm), "a", "round-robin", HEAD)
        self.assertEqual(fixed["mode"], "round-robin")
        # meeting 阶段：LLM 写 message 但 mode 错写 round-robin → 覆盖为 meeting
        fm2 = {"type": "message", "from": "a", "seen_at": HEAD, "mode": "round-robin"}
        fixed2, _ = validate_and_fix(dict(fm2), "a", "meeting", HEAD)
        self.assertEqual(fixed2["mode"], "meeting")

    def test_to_directed_forced_all(self):
        """#7（review6）：to 定向是预留能力（设计 14 决策 B）——生产
        validate_and_fix 无条件强制 to=all，to=b 在真实流程不可达。
        契约测试：合法 to=b 的消息被强制为 all（不是报错）。"""
        fm = {"type": "message", "from": "a", "seen_at": HEAD,
              "mode": "meeting", "to": "b"}
        fixed, errors = validate_and_fix(dict(fm), "a", "meeting", HEAD)
        self.assertEqual(fixed["to"], "all", "to 定向应被强制为 all")
        # 缺省 to 也补为 all
        fm2 = {"type": "message", "from": "a", "seen_at": HEAD,
               "mode": "meeting"}
        fixed2, _ = validate_and_fix(dict(fm2), "a", "meeting", HEAD)
        self.assertEqual(fixed2.get("to", "all"), "all", "缺省 to 应为 all")

    def test_invalid_type_reported(self):
        # type 非法 → 报告错误（调用方决定重写唤醒）
        fm = {"type": "attack", "from": "a", "seen_at": HEAD, "mode": "meeting"}
        _, errors = validate_and_fix(dict(fm), "a", "meeting", HEAD)
        self.assertEqual(len(errors), 1)
        self.assertIn("type", errors[0])

    def test_llm_types_valid(self):
        # M1：LLM 路径（默认）只放行 message/freezing/pass
        for t in ("message", "freezing", "pass"):
            fm = {"type": t, "from": "a", "seen_at": HEAD, "mode": "meeting"}
            _, errors = validate_and_fix(dict(fm), "a", "meeting", HEAD)
            self.assertEqual(errors, [], f"type {t} 应合法")

    def test_protocol_types_blocked_for_llm(self):
        # M1：引擎专用 type（all-freezing/concluded）LLM 路径必须拦截
        for t in ("all-freezing", "concluded"):
            fm = {"type": t, "from": "a", "seen_at": HEAD, "mode": "meeting"}
            _, errors = validate_and_fix(dict(fm), "a", "meeting", HEAD)
            self.assertEqual(len(errors), 1, f"type {t} 应被 LLM 路径拦截")
            self.assertIn("type", errors[0])

    def test_protocol_types_allowed_for_engine(self):
        # M1：协议信号路径（allow_protocol_types=True）放行全部 MEETING_TYPES
        for t in MEETING_TYPES:
            fm = {"type": t, "from": "a", "seen_at": HEAD, "mode": "meeting"}
            _, errors = validate_and_fix(dict(fm), "a", "meeting", HEAD,
                                         allow_protocol_types=True)
            self.assertEqual(errors, [], f"type {t} 应合法（引擎路径）")

    def test_to_invalid_fallback_all(self):
        fm = {"type": "message", "from": "a", "seen_at": HEAD, "mode": "meeting", "to": 42}
        fixed, _ = validate_and_fix(dict(fm), "a", "meeting", HEAD)
        self.assertEqual(fixed["to"], "all")

    def test_loop_message_keeps_seen_at(self):
        """loop 消息：seen_at 沿用（不强制为 head）——用户 10030 定案。"""
        fm = {"type": "freezing", "from": "a", "seen_at": "old-ref",
              "mode": "meeting"}
        fixed, errors = validate_and_fix(dict(fm), "a", "meeting", HEAD,
                                         loop_message=True)
        self.assertEqual(errors, [])
        self.assertEqual(fixed["seen_at"], "old-ref", "loop 消息 seen_at 应沿用")

    def test_loop_message_missing_seen_at_fallback_head(self):
        """loop 消息 seen_at 缺失（首条 loop 消息无读取点）→ head 兜底。"""
        fm = {"type": "freezing", "from": "a", "mode": "meeting"}
        fixed, _ = validate_and_fix(dict(fm), "a", "meeting", HEAD,
                                    loop_message=True)
        self.assertEqual(fixed["seen_at"], HEAD)

    def test_empty_frontmatter_all_fixed(self):
        """空 frontmatter：from/seen_at/mode 全补，type 缺失 → errors。"""
        fixed, errors = validate_and_fix({}, "a", "meeting", HEAD)
        self.assertEqual(fixed["from"], "a")
        self.assertEqual(fixed["seen_at"], HEAD)
        self.assertEqual(fixed["mode"], "meeting")
        self.assertEqual(fixed["to"], "all")
        self.assertEqual(len(errors), 1, "type 缺失应报错")
        self.assertIn("type", errors[0])

    def test_type_missing_reported(self):
        """type 字段缺失（None）→ 不在白名单 → errors。"""
        fm = {"from": "a", "seen_at": HEAD, "mode": "meeting"}
        _, errors = validate_and_fix(dict(fm), "a", "meeting", HEAD)
        self.assertEqual(len(errors), 1)
        self.assertIn("type", errors[0])


# ---------------------------------------------------------------
# 1.4 触发条件
# ---------------------------------------------------------------

class TestTrigger(unittest.TestCase):

    def test_own_message_no_trigger(self):
        new = [msg("message", from_="a")]
        self.assertFalse(has_new_messages_for_me(new, "a"))

    def test_other_message_trigger(self):
        new = [msg("message", from_="b")]
        self.assertTrue(has_new_messages_for_me(new, "a"))

    def test_to_all_default(self):
        # to 缺省 = all → 触发
        new = [msg("message", from_="b")]
        self.assertTrue(has_new_messages_for_me(new, "a"))
        self.assertTrue(has_new_messages_for_me(new, "c"))

    def test_to_me_only(self):
        new = [msg("message", from_="b", to="a")]
        self.assertTrue(has_new_messages_for_me(new, "a"))
        self.assertFalse(has_new_messages_for_me(new, "c"))  # 定向给 a，c 不触发

    def test_to_all_explicit(self):
        new = [msg("message", from_="b", to="all")]
        self.assertTrue(has_new_messages_for_me(new, "a"))
        self.assertTrue(has_new_messages_for_me(new, "c"))

    def test_empty_no_trigger(self):
        self.assertFalse(has_new_messages_for_me([], "a"))


# ---------------------------------------------------------------
# 5. 冻结级联决策（阶段 3）
# ---------------------------------------------------------------

class TestFreezeCascade(unittest.TestCase):

    def test_should_write_af(self):
        # 全员 freezing → 写 af
        d = {"a": "freezing", "b": "freezing", "c": "freezing"}
        self.assertTrue(should_write_af(d))
        # 有人 message → 不写
        d2 = {"a": "freezing", "b": "message", "c": "freezing"}
        self.assertFalse(should_write_af(d2))
        # 有人 af 有人 freezing（异步中间态）→ 写 af（宽松：全员冻结即可）
        d3 = {"a": "all-freezing", "b": "freezing", "c": "freezing"}
        self.assertTrue(should_write_af(d3))
        # 有人从未发言（None）→ 不写（不能全员冻结）
        d4 = {"a": "freezing", "b": "freezing", "c": None}
        self.assertFalse(should_write_af(d4))
        # 空 → 不写
        self.assertFalse(should_write_af({}))

    def test_can_start_rr(self):
        # 全员 af → starter 可启动 RR
        d = {"a": "all-freezing", "b": "all-freezing", "c": "all-freezing"}
        self.assertTrue(can_start_rr(d))
        # 有人还在 freezing → 不能（异步演进中间态）
        d2 = {"a": "all-freezing", "b": "freezing", "c": "all-freezing"}
        self.assertFalse(can_start_rr(d2))
        # 有人 message（逃生口打破冻结）→ 不能
        d3 = {"a": "all-freezing", "b": "message", "c": "all-freezing"}
        self.assertFalse(can_start_rr(d3))
        # 有人从未发言 → 不能
        d4 = {"a": "all-freezing", "b": "all-freezing", "c": None}
        self.assertFalse(can_start_rr(d4))

    def test_aggregate_mode(self):
        # 无人冻结 → meeting
        self.assertEqual(aggregate_mode(
            {"a": {"type": "message", "mode": "meeting"},
             "b": {"type": "message", "mode": "meeting"}}), "meeting")
        # 有人冻结未全员 → meeting
        self.assertEqual(aggregate_mode(
            {"a": {"type": "freezing", "mode": "meeting"},
             "b": {"type": "message", "mode": "meeting"}}), "meeting")
        # 异步中间态：有人 af 有人 freezing → all-freezing
        self.assertEqual(aggregate_mode(
            {"a": {"type": "all-freezing", "mode": "all-freezing"},
             "b": {"type": "freezing", "mode": "meeting"}}), "all-freezing")
        # 全员 af → all-freezing
        self.assertEqual(aggregate_mode(
            {"a": {"type": "all-freezing", "mode": "all-freezing"},
             "b": {"type": "all-freezing", "mode": "all-freezing"}}), "all-freezing")
        # 从未参与（None）→ meeting
        self.assertEqual(aggregate_mode({"a": None, "b": None}), "meeting")
        # 空 dict → meeting（无任何 agent）
        self.assertEqual(aggregate_mode({}), "meeting")
        # 任一 mode==round-robin → round-robin（pass 存在）
        self.assertEqual(aggregate_mode(
            {"a": {"type": "pass", "mode": "round-robin"},
             "b": {"type": "pass", "mode": "round-robin"}}), "round-robin")
        # 任一 type==concluded → concluded
        self.assertEqual(aggregate_mode(
            {"a": {"type": "concluded", "mode": "concluded"},
             "b": {"type": "pass", "mode": "round-robin"}}), "concluded")


if __name__ == "__main__":
    unittest.main()
