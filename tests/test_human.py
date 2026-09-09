"""human 插话功能单元测试（pi-agents-helper 阶段 1）。

覆盖（docs/pi-helper-design.md §7）：
1. human_msg_count：bare 中 human 消息计数（配额增量输入）
2. rr_next_speaker 跳过非参与者消息（human 不参与轮转，不断链）
3. 保留名校验：--agents 含 human → 报错
4. viewer：增量输出（--since）、消息格式化、状态/done 检测
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import threading
import time

from meeting_fs import (run_git, write_message, list_my_messages,
                        next_msg_id)
from meeting_engine import (rr_next_speaker, human_msg_count, aggregate_mode,
                            agent_loop)
from tests.test_meeting_concurrency import setup_env, write_msg, types_at_head

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def human_msg(n, body="插话", mode="meeting", summary=None):
    """标准 human 消息 (path, frontmatter, body)——frontmatter 只此一份。"""
    fm = {"from": "human", "type": "message", "mode": mode,
          "seen_at": "", "to": "all"}
    if summary:
        fm["summary"] = summary
    return f"human/{n:04d}.md", fm, body


class TestHumanMsgCount(unittest.TestCase):
    def test_empty(self):
        base, bare, _ = setup_env("human-count-empty", ["a", "b"])
        self.addCleanup(shutil.rmtree, base)
        self.assertEqual(human_msg_count(bare), 0)

    def test_two_human_messages(self):
        base, bare, wd = setup_env("human-count-2", ["a", "b"])
        self.addCleanup(shutil.rmtree, base)
        write_msg(wd["human"], *human_msg(1, "插话一"))
        write_msg(wd["human"], *human_msg(2, "插话二"))
        self.assertEqual(human_msg_count(bare), 2)

    def test_participant_messages_not_counted(self):
        base, bare, wd = setup_env("human-count-agent", ["a", "b"])
        self.addCleanup(shutil.rmtree, base)
        write_msg(wd["a"], "a/0001.md",
                   {"from": "a", "type": "message", "mode": "meeting",
                    "seen_at": "", "to": "all"}, "a 发言")
        self.assertEqual(human_msg_count(bare), 0)

    def test_non_message_files_not_counted(self):
        """human/ 下非 4 位数字 md（README 等）不计入。"""
        base, bare, wd = setup_env("human-count-junk", ["a", "b"])
        self.addCleanup(shutil.rmtree, base)
        write_msg(wd["human"], "human/README.md",
                  {"from": "human", "type": "message"}, "说明文件")
        write_msg(wd["human"], *human_msg(1, "真插话"))
        self.assertEqual(human_msg_count(bare), 1)


class TestRRNextSpeakerSkipHuman(unittest.TestCase):
    def _rr_setup(self, name):
        """RR 场景：a 写 pass（next=b）后可选写 human 消息。"""
        base, bare, wd = setup_env(name, ["a", "b"])
        self.addCleanup(shutil.rmtree, base)
        write_msg(wd["a"], "a/0001.md",
                   {"from": "a", "type": "pass", "mode": "round-robin",
                    "next": "b", "seen_at": "", "to": "all"}, "a pass")
        return base, bare, wd

    def test_normal_no_human(self):
        """回归：HEAD 是参与者消息 → 直接读其 next。"""
        _, bare, _ = self._rr_setup("rr-normal")
        self.assertEqual(rr_next_speaker(bare, ["a", "b"]), "b")

    def test_human_on_head(self):
        """human 插话在 HEAD → 跳过，找 a 的 pass 的 next。"""
        _, bare, wd = self._rr_setup("rr-human-head")
        write_msg(wd["human"], *human_msg(1, "人插话"))
        self.assertEqual(rr_next_speaker(bare, ["a", "b"]), "b")

    def test_multiple_human(self):
        """连续 2 条 human 插话 → 逐 commit 跳过。"""
        _, bare, wd = self._rr_setup("rr-human-multi")
        write_msg(wd["human"], *human_msg(1, "插话一"))
        write_msg(wd["human"], *human_msg(2, "插话二"))
        self.assertEqual(rr_next_speaker(bare, ["a", "b"]), "b")

    def test_human_between_rounds(self):
        """轮次之间插话：a pass → human → b pass(next=a) → 找 b 的 next。"""
        _, bare, wd = self._rr_setup("rr-human-between")
        write_msg(wd["human"], *human_msg(1, "插话"))
        write_msg(wd["b"], "b/0001.md",
                   {"from": "b", "type": "pass", "mode": "round-robin",
                    "next": "a", "seen_at": "", "to": "all"}, "b pass")
        self.assertEqual(rr_next_speaker(bare, ["a", "b"]), "a")

    def test_human_mode_not_affect_aggregate(self):
        """human 消息不进入聚合判定（mode 仍由参与者决定）。"""
        _, bare, wd = self._rr_setup("rr-human-agg")
        write_msg(wd["human"], *human_msg(1, "插话"))
        # 参与者最后一条仍是 a 的 pass（round-robin）→ 聚合仍 round-robin
        self.assertEqual(aggregate_mode(bare, ["a", "b"]), "round-robin")

    def test_multi_msg_file_same_commit_takes_last(self):
        """原语义回归（用户 2026-08-30 严格核对）：同 commit 多消息文件
        取最后一个（msg_files[-1]）——我的首版实现取第一个，不等价。"""
        base, bare, wd = setup_env("rr-multi-file", ["a", "b"])
        self.addCleanup(shutil.rmtree, base)
        # 一个 commit 写两个消息文件（a/0001 next=b、a/0002 next=a）
        write_message(wd["a"], "a/0001.md",
                      {"from": "a", "type": "pass", "mode": "round-robin",
                       "next": "b", "seen_at": "", "to": "all"}, "一")
        write_message(wd["a"], "a/0002.md",
                      {"from": "a", "type": "pass", "mode": "round-robin",
                       "next": "a", "seen_at": "", "to": "all"}, "二")
        run_git(wd["a"], "add", "--", "a/0001.md", "a/0002.md")
        run_git(wd["a"], "commit", "-m", "discuss: a/0002")
        run_git(wd["a"], "push")
        # 原语义：取最后一个消息文件（a/0002）的 next = a
        self.assertEqual(rr_next_speaker(bare, ["a", "b"]), "a")

    def test_human_after_multi_msg_commit(self):
        """human 在 HEAD 时回退路径同样取该 commit 最后一个消息文件。"""
        base, bare, wd = setup_env("rr-multi-human", ["a", "b"])
        self.addCleanup(shutil.rmtree, base)
        write_message(wd["a"], "a/0001.md",
                      {"from": "a", "type": "pass", "mode": "round-robin",
                       "next": "b", "seen_at": "", "to": "all"}, "一")
        write_message(wd["a"], "a/0002.md",
                      {"from": "a", "type": "pass", "mode": "round-robin",
                       "next": "a", "seen_at": "", "to": "all"}, "二")
        run_git(wd["a"], "add", "--", "a/0001.md", "a/0002.md")
        run_git(wd["a"], "commit", "-m", "discuss: a/0002")
        run_git(wd["a"], "push")
        write_msg(wd["human"], *human_msg(1, "插话"))
        # 回退路径：a 的 commit 取最后一个（a/0002）的 next = a
        self.assertEqual(rr_next_speaker(bare, ["a", "b"]), "a")


class TestReservedName(unittest.TestCase):
    def _cli(self, agents):
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run(
                [sys.executable, "start_discussion.py",
                 "--dir", os.path.join(tmp, "disc"),
                 "--agents", agents, "--topic", "t"],
                capture_output=True, text=True,
                cwd=ROOT)
            return r.stdout

    def test_human_in_agents(self):
        out = self._cli("a,human")
        self.assertIn("保留名", out)
        self.assertIn("human", out)

    def test_human_alone(self):
        out = self._cli("human")
        self.assertIn("保留名", out)

    def test_normal_agents_ok(self):
        # 正常参与者不报保留名错误（后续可能报 topic/环境错误，但不含保留名）
        out = self._cli("a,b")
        self.assertNotIn("保留名", out)

    def test_human_in_spec_agents(self):
        """spec/agents/ 推断含 human → 同样报错（两个入口都拦）。"""
        with tempfile.TemporaryDirectory() as tmp:
            spec = os.path.join(tmp, "spec")
            os.makedirs(os.path.join(spec, "agents"))
            with open(os.path.join(spec, "question.md"), "w") as f:
                f.write("# 用途注释（首行跳过）\n# 讨论主题\n")
            for name in ("a", "human"):
                with open(os.path.join(spec, "agents", f"{name}.md"), "w") as f:
                    f.write(f"{name} 定义\n")
            r = subprocess.run(
                [sys.executable, "start_discussion.py",
                 "--dir", os.path.join(tmp, "disc"), "--spec", spec],
                capture_output=True, text=True,
                cwd=ROOT)
            self.assertIn("保留名", r.stdout)
            self.assertIn("human", r.stdout)


class TestViewer(unittest.TestCase):
    def _env(self, name):
        base, bare, wd = setup_env(name, ["a", "b"])
        self.addCleanup(shutil.rmtree, base)
        return base, bare, wd

    def test_format_message(self):
        from human_viewer import format_message
        content = ("---\nfrom: a\ntype: message\nsummary: 观点\n---\n"
                   "正文第一行\n正文第二行\n")
        s = format_message("a/0001.md", content)
        self.assertIn("[a/0001.md] a (message): 观点", s)
        self.assertIn("正文第一行", s)
        self.assertIn("正文第二行", s)

    def test_incremental_all(self):
        """无 since：输出全部消息（参与者 + human）+ 状态。"""
        from human_viewer import incremental
        base, bare, wd = self._env("viewer-all")
        write_msg(wd["a"], "a/0001.md",
                   {"from": "a", "type": "message", "mode": "meeting",
                    "seen_at": "", "to": "all", "summary": "a 观点"}, "a 正文")
        write_msg(wd["human"], *human_msg(1, "人正文", summary="人插话"))
        mode, lines, _, done = incremental(bare, ["a", "b"], None)
        self.assertEqual(mode, "meeting")
        self.assertEqual(len(lines), 2)
        self.assertTrue(any("[a/0001.md] a (message): a 观点" in s
                            for s in lines), lines)
        self.assertTrue(any("[human/0001.md] human (message): 人插话" in s
                            for s in lines), lines)
        self.assertFalse(done)

    def test_incremental_since(self):
        """--since 增量：只含 since 之后的消息。"""
        from human_viewer import incremental, new_messages
        base, bare, wd = self._env("viewer-since")
        write_msg(wd["a"], "a/0001.md",
                   {"from": "a", "type": "message", "mode": "meeting",
                    "seen_at": "", "to": "all"}, "a 正文")
        head1 = run_git(bare, "rev-parse", "HEAD").stdout.strip()
        write_msg(wd["human"], *human_msg(1, "人正文"))
        msgs = new_messages(bare, head1)
        self.assertEqual([p for _, p in msgs], ["human/0001.md"])
        mode, lines, _, _ = incremental(bare, ["a", "b"], head1)
        self.assertEqual(mode, "meeting")
        self.assertEqual(len(lines), 1)
        self.assertIn("[human/0001.md]", lines[0])

    def test_done_detection(self):
        """concluded → done=True（viewer 退出条件）。"""
        from human_viewer import incremental
        base, bare, wd = self._env("viewer-done")
        write_msg(wd["a"], "a/0001.md",
                   {"from": "a", "type": "concluded", "mode": "concluded",
                    "seen_at": "", "to": "all"}, "收尾")
        mode, _, _, done = incremental(bare, ["a", "b"], None)
        self.assertEqual(mode, "concluded")
        self.assertTrue(done)

    def test_follow_cursor(self):
        """--follow 游标：增量后写游标，重启从游标继续。"""
        from human_viewer import follow, _read_cursor
        base, bare, wd = self._env("viewer-cursor")
        write_msg(wd["a"], "a/0001.md",
                   {"from": "a", "type": "message", "mode": "meeting",
                    "seen_at": "", "to": "all"}, "a 正文")
        head1 = run_git(bare, "rev-parse", "HEAD").stdout.strip()
        # follow 在后台线程跑一轮（poll 间隔小），主线程写 concluded → 退出
        out = []

        def _run():
            class FakeOut:
                def write(self, s):
                    out.append(s)

                def flush(self):
                    pass
            old = sys.stdout
            sys.stdout = FakeOut()
            try:
                follow(base, bare, ["a", "b"], poll_interval=0.05)
            finally:
                sys.stdout = old

        t = threading.Thread(target=_run)
        t.start()
        write_msg(wd["a"], "a/0002.md",
                   {"from": "a", "type": "concluded", "mode": "concluded",
                    "seen_at": "", "to": "all"}, "收尾")
        t.join(timeout=10)
        self.assertFalse(t.is_alive(), "follow 未在 concluded 后退出")
        # 游标已持久化且为最终 HEAD
        self.assertEqual(_read_cursor(base),
                         run_git(bare, "rev-parse", "HEAD").stdout.strip())


class TestFakeAgentWithHuman(unittest.TestCase):
    """FakeAgent 多进程：human 插话注入（meeting + RR 阶段），讨论仍收敛。

    检测力：若 rr_next_speaker 未跳过 human（轮转链断）→ 讨论死锁 →
    wait_all 超时 → 本测试失败。
    """

    def test_discussion_converges_with_human_interjections(self):
        from tests.test_meeting_concurrency import spawn_agents, wait_all
        base, bare, wd = setup_env("human-e2e", ["a", "b"])
        self.addCleanup(shutil.rmtree, base)
        procs, _ = spawn_agents(wd, ["a", "b"], {}, {},
                                max_meeting=4, max_rr=5)
        try:
            # meeting 阶段注入（真实 sayer 写路径，等首启发言落 bare）
            time.sleep(5)
            from human_sayer import say
            say(wd["human"], "meeting 阶段插话")
            # 稍后注入第二条（可能已进入 RR 阶段）
            time.sleep(8)
            say(wd["human"], "第二条插话")
            all_exit, alive = wait_all(procs, timeout=180)
            self.assertTrue(all_exit, f"进程未退出: {alive}")
            types = types_at_head(bare)
            self.assertIn("concluded", types,
                          f"讨论未收敛（轮转链被 human 打断?）: {types}")
            # human 消息确实存在且被看到（讨论继续推进到收尾 = 触发正常）
            self.assertIn("message", types)
        finally:
            for p in procs.values():
                if p.poll() is None:
                    p.terminate()


class TestQuotaIncrement(unittest.TestCase):
    """配额增量公式的行为验证（helper 设计 §4.1）。

    max_meeting=1 + human 插话（h_count=1）→ 配额上限 1+1=2：
    agent 首启 1 条 message 后不被冻结，能响应 human 写第 2 条。
    若公式未生效（quota=1）→ 首启后 ⑤.2 即冻结 → 无法响应 → 超时失败。
    """

    def test_quota_increment_lets_agent_respond(self):
        """配额增量公式的行为验证（helper 设计 §4.1）。

        真实讨论形态：2 个 LLM agents（a/b）+ human 通道，全部确定性。
        max_meeting=1 + human/0001 先注入（h_count=1 → 配额上限 2）：
        - 增量生效：各 agent 首启 1 条后不冻结（1 < 2）→ 响应对方/互触发
          各写第 2 条 → 配额尽 → 冻结 → af → RR → 完整收敛
        - 增量失效：各 agent 首启 1 条后 ⑤.2 立即冻结（1 >= 1）→
          双方冻结 → 无人互响应 → message 数 = 1 → 断言失败
        完全确定无竞态（⑤.2 在触发判断之前；human 先注入使首启即含增量）。
        """
        base, bare, wd = setup_env("quota-inc", ["a", "b"])
        self.addCleanup(shutil.rmtree, base)

        def responder(workdir, agent, head, meta, is_first, rr_turn, retry,
                      finalizing=False, finalize_reason=None):
            mid = next_msg_id(workdir, agent)
            if rr_turn:
                write_message(workdir, f"{agent}/{mid}.md",
                              {"type": "pass", "summary": "pass"},
                              "无异议")
            else:
                write_message(workdir, f"{agent}/{mid}.md",
                              {"type": "message", "summary": "测试响应"},
                              "正文")
            return True

        # human 先注入（h_count=1）——配额增量使各 agent 配额上限 = 2
        write_msg(wd["human"], *human_msg(1, "人插话"))

        # 2 个 LLM agent 的 loop 全部启动（真实形态）
        threads = []
        for ag in ("a", "b"):
            t = threading.Thread(
                target=agent_loop,
                args=(wd[ag], ag, responder),
                kwargs={"max_meeting": 1, "max_rr": 5,
                        "poll_interval": 0.1},
                daemon=True)
            t.start()
            threads.append(t)
        try:
            # 等完整收敛（concluded）或超时
            deadline = time.time() + 90
            from meeting_engine import aggregate_mode
            while time.time() < deadline:
                if aggregate_mode(bare, ["a", "b"]) == "concluded":
                    break
                time.sleep(0.5)
            # 断言 1：讨论收敛（完整链路走通）
            self.assertEqual(
                aggregate_mode(bare, ["a", "b"]), "concluded",
                "讨论未收敛（配额增量失效会导致双方提前冻结但无 af 级联?）")
            # 断言 2：各 agent 的 message 数 = 2（首启 + 互响应）——
            # 配额增量允许的响应；增量失效则首启后即冻结，message = 1
            for ag in ("a", "b"):
                msgs = [fm for fm in list_my_messages(wd[ag], ag)
                        if fm.get("type") == "message"]
                self.assertGreaterEqual(
                    len(msgs), 2,
                    f"配额增量未生效：{ag} 首启 1 条后即被冻结"
                    f"（quota 未含 h_count），应能写第 2 条 message")
        finally:
            pass  # daemon 线程随测试进程退出


class TestSayer(unittest.TestCase):
    """human-sayer 写消息路径（helper 设计 §5.3）。

    装置：setup_env 已固定创建 work-human（阶段 1 修正，human 另算）。
    """

    def _env(self, name):
        base, bare, wd = setup_env(name, ["a", "b"])
        self.addCleanup(shutil.rmtree, base)
        return base, bare, wd

    def test_say_writes_full_frontmatter(self):
        """frontmatter 确定性补全：from/type/mode/seen_at/to/summary。"""
        from human_sayer import say
        from meeting_fs import git_show, parse_frontmatter
        base, bare, wd = self._env("sayer-basic")
        write_msg(wd["a"], "a/0001.md",
                  {"from": "a", "type": "message", "mode": "meeting",
                   "seen_at": "", "to": "all"}, "a 发言")
        a_head = run_git(bare, "log", "-1", "--format=%H", "--",
                         "a/0001.md").stdout.strip()
        path, summ = say(wd["human"], "第一行摘要\n第二行正文")
        self.assertEqual(path, "human/0001.md")
        self.assertEqual(summ, "第一行摘要")
        fm = parse_frontmatter(git_show(bare, "HEAD", path))
        self.assertEqual(fm.get("from"), "human")
        self.assertEqual(fm.get("type"), "message")
        self.assertEqual(fm.get("mode"), "meeting")
        self.assertEqual(fm.get("seen_at"), a_head)   # 写入时 HEAD
        self.assertEqual(fm.get("to"), "all")
        self.assertEqual(fm.get("summary"), "第一行摘要")

    def test_say_sequence(self):
        """两次插话 → 序号递增（max+1，对齐 next_msg_id 语义）。"""
        from human_sayer import say
        base, bare, wd = self._env("sayer-seq")
        p1, _ = say(wd["human"], "一")
        p2, _ = say(wd["human"], "二")
        self.assertEqual(p1, "human/0001.md")
        self.assertEqual(p2, "human/0002.md")

    def test_say_multiline_body(self):
        """多行正文完整保留（复制粘贴场景）。"""
        from human_sayer import say
        from meeting_fs import git_show
        base, bare, wd = self._env("sayer-multi")
        body = "第一行\n第二行\n\n第三段"
        path, summ = say(wd["human"], body)
        self.assertEqual(summ, "第一行")
        content = git_show(bare, "HEAD", path)
        self.assertIn("第二行", content)
        self.assertIn("第三段", content)

    def test_summary_truncated(self):
        """summary 首行截取（SUMMARY_MAX=60）。"""
        from human_sayer import say
        base, bare, wd = self._env("sayer-trunc")
        _, summ = say(wd["human"], "x" * 100 + "\n正文")
        self.assertEqual(len(summ), 60)

    def test_empty_text_rejected(self):
        """空插话 → 明确报错（CLI）。"""
        base, bare, wd = self._env("sayer-empty")
        r = subprocess.run(
            [sys.executable, os.path.join(ROOT, "human_sayer.py"),
             base, "   "], capture_output=True, text=True)
        self.assertIn("错误", r.stdout + r.stderr)
        self.assertNotEqual(r.returncode, 0)

    def test_nonexistent_discussion_rejected(self):
        """讨论不存在 → 明确报错（CLI）。"""
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run(
                [sys.executable, os.path.join(ROOT, "human_sayer.py"),
                 os.path.join(tmp, "nope"), "插话"],
                capture_output=True, text=True)
        self.assertIn("错误", r.stdout + r.stderr)
        self.assertNotEqual(r.returncode, 0)

    def test_concurrent_sayers_no_sequence_collision(self):
        """两个 sayer 并发（独立进程）→ flock 串行 + 序号不冲突。"""
        base, bare, wd = self._env("sayer-conc")
        procs = [subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "human_sayer.py"),
             base, f"并发插话{i}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for i in range(2)]
        outs = [p.communicate()[0] for p in procs]
        self.assertTrue(all("已发送 human/000" in o for o in outs), outs)
        # 两条消息都存在且序号连续（0001/0002 各一条）
        from meeting_fs import is_message_file
        r = run_git(bare, "ls-tree", "-r", "--name-only", "HEAD")
        human_msgs = sorted(f for f in r.stdout.splitlines()
                            if is_message_file(f) and f.startswith("human/"))
        self.assertEqual(human_msgs, ["human/0001.md", "human/0002.md"])

    def test_interactive_multiline_submit(self):
        """交互模式：多行累积 + 空行提交 → 一条消息。"""
        import io
        import contextlib
        from human_sayer import interactive
        base, bare, wd = self._env("sayer-interactive")
        # 模拟 stdin：两行内容 + 空行提交 + EOF
        fake_in = io.StringIO("第一行\n第二行\n\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            old_in = sys.stdin
            sys.stdin = fake_in
            try:
                interactive(wd["human"])
            finally:
                sys.stdin = old_in
        self.assertIn("已发送 human/0001.md", out.getvalue())
        from meeting_fs import git_show
        content = git_show(bare, "HEAD", "human/0001.md")
        self.assertIn("第一行", content)
        self.assertIn("第二行", content)

    def test_interactive_empty_only(self):
        """交互模式：只有空行/EOF → 不发送。"""
        import io
        import contextlib
        from human_sayer import interactive
        base, bare, wd = self._env("sayer-interactive-empty")
        fake_in = io.StringIO("\n\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            old_in = sys.stdin
            sys.stdin = fake_in
            try:
                interactive(wd["human"])
            finally:
                sys.stdin = old_in
        self.assertNotIn("已发送", out.getvalue())

    def test_setup_creates_work_human(self):
        """真实 setup 创建 work-human（clone + git 身份）。"""
        with tempfile.TemporaryDirectory() as tmp:
            disc = os.path.join(tmp, "disc")
            r = subprocess.run(
                [sys.executable, "start_discussion.py",
                 "--dir", disc, "--agents", "a,b", "--topic", "t"],
                capture_output=True, text=True, cwd=ROOT)
            wh = os.path.join(disc, "work-human")
            self.assertTrue(os.path.isdir(wh), r.stdout)
            self.assertTrue(os.path.isdir(os.path.join(wh, "human")))
            # clone 自带 protocol.json（参与者/配额单一事实源）
            self.assertTrue(os.path.exists(os.path.join(wh, "protocol.json")))
            # git 身份已配置（提交需要）
            name = run_git(wh, "config", "user.name").stdout.strip()
            self.assertTrue(name)


if __name__ == "__main__":
    unittest.main()
