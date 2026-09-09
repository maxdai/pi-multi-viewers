"""流程组合测试——用真实 API 拼出完整讨论流程（单元测试重设计第 0 步）。

背景（用户 2026-09-01）：API 清单是测试基准，但清单本身需先验证——
把各 API 拼接在一起走完整流程，检查输入输出衔接是否符合设计流程。
不用 agent_loop 整体跑（那是集成测试形态），逐 API 显式调用拼装，
断链点 = 清单或实现的漏洞。

拼接链（api-list.md 组合验证节）：
setup → 判定数据 → 写入 → 配额/冻结 → af → RR → 收尾 → done → 清理
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import start_discussion as sd
from meeting_fs import (git_head, git_ls_files, git_show, list_my_messages,
                        new_messages_with_meta, next_msg_id, read_point,
                        write_message)
from meeting_core import (aggregate_mode as core_aggregate_mode,
                          can_start_rr, has_new_messages_for_me,
                          should_write_af, validate_and_fix)
from meeting_engine import (participants, result_writer, _each_agent_messages,
                            _each_agent_last, human_msg_count,
                            _meeting_speak_count, rr_next_speaker,
                            rr_active_count, write_protocol_signal,
                            commit_new_files, finalize_discussion)


class Args:
    """setup_environment 的 args 替身（CLI 参数结构）。"""

    def __init__(self, topic="组合测试主题", agents="a,b",
                 stances=None, background=None, questions=None,
                 models=None, max_meeting=10, max_rr=7, pure=False,
                 result_writer=None, stall_timeout=600):
        self.topic = topic
        self.agents = agents
        self.stances = stances
        self.background = background
        self.questions = questions
        self.models = models
        self.max_meeting = max_meeting
        self.max_rr = max_rr
        self.pure = pure
        self.result_writer = result_writer
        self.stall_timeout = stall_timeout


class FakeResponder:
    """模拟 LLM responder：写一条 message 内容文件（真实写路径的形状）。

    与 fake_agent 的区别：fake_agent 独立实现写文件；这里只做最小
    responder（写 frontmatter + 正文），补全/commit 全部走引擎 API——
    拼装链覆盖 commit_new_files 的真实路径。
    """

    def __init__(self, type_="message", body="回应"):
        self.type_ = type_
        self.body = body
        self.calls = 0
        self.result_md = None

    def __call__(self, workdir, agent, head, meta, is_first, rr_turn, retry,
                 finalizing=False, finalize_reason="consensus"):
        self.calls += 1
        if finalizing:
            self.result_md = f"# 结论\n\n（{finalize_reason}）组合测试生成的结论正文。" * 3
            with open(os.path.join(workdir, "result.md"), "w") as f:
                f.write(self.result_md)
            return True
        mid = next_msg_id(workdir, agent)
        path = f"{agent}/{mid}.md"
        fm = {
            "from": agent,
            "type": self.type_,
            "summary": f"[测试] {self.body}",
        }
        write_message(workdir, path, fm, self.body)
        return True


class TestFlowComposition(unittest.TestCase):
    """八条组合链的动态拼接验证（api-list.md 组合验证节）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="flow-compose-")
        self.base = os.path.join(self.tmp, "disc")
        self.args = Args()
        self.participants = ["a", "b"]
        sd.setup_environment(self.args, self.participants, self.base)
        self.bare = os.path.join(self.base, "repo.git")
        self.wa = os.path.join(self.base, "work-a")
        self.wb = os.path.join(self.base, "work-b")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 链 1：setup → 判定数据（S14 → E1/E2/E3/E5 → C9）----

    def test_chain1_setup_to_judgment(self):
        """setup 产物 → participants/result_writer 读取一致 → 状态判定。"""
        # E1/E2：从 protocol.json 读取
        ps = participants(self.wa)
        self.assertEqual(ps, ["a", "b"])
        rw = result_writer(self.wa)
        self.assertEqual(rw, "b")  # 缺省 = 最后参与者

        # E3/E5：bare 组装（setup commit 后无消息文件）
        messages = _each_agent_messages(self.bare, ps)
        self.assertEqual(messages, {"a": [], "b": []})
        lasts = _each_agent_last(self.bare, ps)
        self.assertEqual(lasts, {"a": None, "b": None})

        # C9：全 None → meeting
        mode = core_aggregate_mode(lasts)
        self.assertEqual(mode, "meeting")

    # ---- 链 2：写入（responder → E15 → bare 可见）----

    def test_chain2_write_path(self):
        """responder 写文件 → commit_new_files 补全提交 → bare 可读。"""
        head = git_head(self.wa)
        # responder 写内容文件（最小 frontmatter）
        resp = FakeResponder(body="链 2 测试")
        resp(self.wa, "a", head, [], False, False, False)
        # E15：引擎补全提交
        ok = commit_new_files(self.wa, "a", head, "meeting")
        self.assertTrue(ok)
        # bare 可见 + 字段补全
        messages = _each_agent_messages(self.bare, ["a", "b"])
        self.assertEqual(len(messages["a"]), 1)
        fm = messages["a"][0]
        self.assertEqual(fm["from"], "a")
        self.assertEqual(fm["mode"], "meeting")
        self.assertEqual(fm["to"], "all")
        self.assertEqual(fm["seen_at"], head)
        self.assertIn("type", fm)

    # ---- 链 3：判定数据流（bare → E3 → C9 模式）----

    def test_chain3_judgment_pipeline(self):
        """写 2 条 message → 判定仍 meeting；写 freezing+freezing → all-freezing。"""
        resp = FakeResponder()
        head = git_head(self.wa)
        resp(self.wa, "a", head, [], True, False, False)
        commit_new_files(self.wa, "a", head, "meeting")
        resp(self.wb, "b", head, [], False, False, False)
        commit_new_files(self.wb, "b", head, "meeting")

        messages = _each_agent_messages(self.bare, ["a", "b"])
        lasts = _each_agent_last(self.bare, ["a", "b"])
        self.assertEqual(core_aggregate_mode(lasts), "meeting")

        # 全员 freezing → all-freezing
        write_protocol_signal(self.wa, "a", "freezing", "meeting")
        write_protocol_signal(self.wb, "b", "freezing", "meeting")
        lasts2 = _each_agent_last(self.bare, ["a", "b"])
        self.assertEqual(core_aggregate_mode(lasts2), "all-freezing")

    # ---- 链 4：配额（E9 + E10 → 冻结判定）----

    def test_chain4_quota(self):
        """human_msg_count 增量 + speak_count 计算 + human 通道计数。"""
        # 无 human → 0
        self.assertEqual(human_msg_count(self.bare), 0)
        # a 写 1 条 message → speak_count = 1
        resp = FakeResponder()
        head = git_head(self.wa)
        resp(self.wa, "a", head, [], True, False, False)
        commit_new_files(self.wa, "a", head, "meeting")
        messages = _each_agent_messages(self.bare, ["a", "b"])
        self.assertEqual(_meeting_speak_count(messages, "a"), 1)
        # human 插话 → count = 1
        from human_sayer import say
        wh = os.path.join(self.base, "work-human")
        path, summ = say(wh, "human 插话测试")
        self.assertEqual(path, "human/0001.md")
        self.assertEqual(human_msg_count(self.bare), 1)
        # 配额上限 = max_meeting + human_count（E21 ⑤.2 的公式）
        self.assertLess(_meeting_speak_count(messages, "a"),
                        self.args.max_meeting + human_msg_count(self.bare))

    # ---- 链 5：RR 轮转（C8 → E13 pass → E7 next）----

    def test_chain5_rr_rotation(self):
        """全员 af → starter pass（带 next）→ rr_next_speaker 轮转。"""
        # 全员冻结
        write_protocol_signal(self.wa, "a", "freezing", "meeting")
        write_protocol_signal(self.wb, "b", "freezing", "meeting")
        # 全员 af（C7 宽松判定 → E12 写 af）
        lasts = _each_agent_last(self.bare, ["a", "b"])
        self.assertTrue(should_write_af({a: lasts[a]["type"] for a in lasts}))
        from meeting_engine import write_af_if_no_rr
        write_af_if_no_rr(self.bare, self.wa, "a", ["a", "b"])
        write_af_if_no_rr(self.bare, self.wb, "b", ["a", "b"])
        lasts = _each_agent_last(self.bare, ["a", "b"])
        self.assertTrue(can_start_rr({a: lasts[a]["type"] for a in lasts}))

        # starter pass 带 next → RR 模式
        write_protocol_signal(self.wa, "a", "pass", "round-robin", "b")
        lasts = _each_agent_last(self.bare, ["a", "b"])
        self.assertEqual(core_aggregate_mode(lasts), "round-robin")

        # E7：next 轮转
        self.assertEqual(rr_next_speaker(self.bare, ["a", "b"]), "b")
        # b pass 带 next → a
        write_protocol_signal(self.wb, "b", "pass", "round-robin", "a")
        self.assertEqual(rr_next_speaker(self.bare, ["a", "b"]), "a")
        # rr_active_count：starter 的 RR 消息数
        messages = _each_agent_messages(self.bare, ["a", "b"])
        self.assertEqual(rr_active_count(messages, ["a", "b"]), 1)

    # ---- 链 6：收尾（E19 → concluded → S17 done）----

    def test_chain6_finalize(self):
        """全员 pass → finalize（result.md + concluded）→ check_status done。"""
        # 构造全员 pass 的 RR 状态
        write_protocol_signal(self.wa, "a", "pass", "round-robin", "b")
        write_protocol_signal(self.wb, "b", "pass", "round-robin", "a")
        # a 再 pass（starter 首轮）——RR 需要全员 pass
        write_protocol_signal(self.wa, "a", "pass", "round-robin", "b")

        # 判定全员 pass → 收尾（rw=b）
        resp = FakeResponder()
        head = git_head(self.wa)
        from meeting_core import is_all_last_in
        messages = _each_agent_messages(self.bare, ["a", "b"])
        self.assertTrue(is_all_last_in(messages, {"pass"}))
        ok = finalize_discussion(self.wb, "b", resp, head, reason="consensus")
        self.assertTrue(ok)

        # concluded 落盘 + result.md 提交
        messages = _each_agent_messages(self.bare, ["a", "b"])
        self.assertEqual(messages["b"][-1]["type"], "concluded")
        r = sd.run(["git", "show", "HEAD:result.md"], cwd=self.bare,
                   check=False)
        self.assertEqual(r.returncode, 0)
        self.assertIn("组合测试", r.stdout)

        # S17：check_status = done
        from start_discussion import check_status
        status, _ = check_status(self.base)
        self.assertEqual(status, "done")

    # ---- 链 7：human 通道（H2 → E10 → V4/V5 展示）----

    def test_chain7_human_channel(self):
        """say 写 human 消息 → 计数 + viewer 增量可见。"""
        from human_sayer import say
        from human_viewer import new_messages, format_message, incremental
        wh = os.path.join(self.base, "work-human")
        path, summ = say(wh, "human 插话第一行\n第二行")
        self.assertEqual(path, "human/0001.md")
        self.assertEqual(summ, "human 插话第一行")

        # E10 计数
        self.assertEqual(human_msg_count(self.bare), 1)

        # V4/V5：viewer 增量展示 human 消息
        msgs = new_messages(self.bare, "")
        self.assertIn(("human/0001.md",), [(p,) for _, p in msgs])
        content = git_show(self.bare, "HEAD", "human/0001.md")
        s = format_message("human/0001.md", content)
        self.assertIsNotNone(s)
        self.assertIn("human 插话第一行", s)

        # V6：incremental（状态 + 消息 + done）
        mode, lines, head, done = incremental(self.bare, ["a", "b"], "")
        self.assertEqual(mode, "meeting")
        self.assertTrue(any("human/0001.md" in l for l in lines))
        self.assertFalse(done)

    # ---- 链 8：清理（S16 → loop 自退的前提：repo.git 消失）----

    def test_chain8_cleanup(self):
        """cleanup 保存 result.md（若存在）→ 删目录 → repo.git 消失。"""
        # 先完成讨论（有 result.md）
        resp = FakeResponder()
        head = git_head(self.wa)
        write_protocol_signal(self.wa, "a", "pass", "round-robin", "b")
        write_protocol_signal(self.wb, "b", "pass", "round-robin", "a")
        write_protocol_signal(self.wa, "a", "pass", "round-robin", "b")
        finalize_discussion(self.wb, "b", resp, head)
        from start_discussion import check_status
        status, _ = check_status(self.base)
        self.assertEqual(status, "done")

        # cleanup：保存 + 删除
        base_name = os.path.basename(self.base)
        parent = os.path.dirname(self.base)
        sd.cleanup_discussion(self.base)
        self.assertFalse(os.path.isdir(self.base))
        self.assertTrue(os.path.exists(
            os.path.join(parent, f"{base_name}-result.md")))
        # 环境不再存在 → loop 自退条件（repo.git 消失）
        self.assertFalse(os.path.isdir(os.path.join(self.base, "repo.git")))
        # 清理结果文件
        os.remove(os.path.join(parent, f"{base_name}-result.md"))

    def test_chain8b_cleanup_no_result(self):
        """未完成讨论 cleanup：不保存 result.md（跳过），目录删除。"""
        sd.cleanup_discussion(self.base)
        self.assertFalse(os.path.isdir(self.base))
        base_name = os.path.basename(self.base)
        parent = os.path.dirname(self.base)
        self.assertFalse(os.path.exists(
            os.path.join(parent, f"{base_name}-result.md")))


if __name__ == "__main__":
    unittest.main()
