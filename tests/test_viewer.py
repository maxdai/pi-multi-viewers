"""human_viewer 单元测试——V1-V9 补缺（API 清单基准，2026-09-01）。

TestViewer（test_human.py）已覆盖 format_message/incremental/follow，
本文件补：read_protocol/participants_from_bare/result_path 边界 + main 入口。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import human_viewer
from meeting_core import (aggregate_mode as core_aggregate_mode,
                          frozen_agents, meeting_speak_count)
from meeting_engine import each_agent_messages
from human_viewer import progress_text
import meeting_fs
from human_viewer import (participants_from_bare, result_path,
                          new_messages, format_message, incremental, main)


def make_discussion(participants=("a", "b"), rw="b"):
    """建讨论环境（bare + work + protocol.json + setup commit）。"""
    tmp = tempfile.mkdtemp(prefix="viewer-")
    base = os.path.join(tmp, "disc")
    bare = os.path.join(base, "repo.git")
    os.makedirs(base)
    subprocess.run(["git", "init", "--bare", bare], check=True,
                   capture_output=True)
    w = os.path.join(base, "work-a")
    subprocess.run(["git", "clone", bare, w], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=w, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=w, check=True)
    proto = {"mode": "meeting", "participants": list(participants),
             "resultWriter": rw}
    with open(os.path.join(w, "protocol.json"), "w") as f:
        json.dump(proto, f)
    subprocess.run(["git", "add", "-A"], cwd=w, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "setup"], cwd=w, check=True,
                   capture_output=True)
    subprocess.run(["git", "push", "origin", "HEAD"], cwd=w, check=True,
                   capture_output=True)
    return tmp, base, bare


class TestProtocol(unittest.TestCase):
    """V1-V3。"""

    def test_protocol_ok(self):
        tmp, base, bare = make_discussion()
        try:
            self.assertEqual(meeting_fs.read_protocol(bare)["participants"], ["a", "b"])
            self.assertEqual(participants_from_bare(bare), ["a", "b"])
            self.assertEqual(result_path(base), f"{base}-result.md")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_protocol_missing(self):
        tmp, base, bare = make_discussion()
        try:
            # 删除 protocol.json（HEAD 无该文件）
            w = os.path.join(base, "work-a")
            subprocess.run(["git", "rm", "-q", "protocol.json"], cwd=w,
                           check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "rm"], cwd=w, check=True,
                           capture_output=True)
            subprocess.run(["git", "push", "origin", "HEAD"], cwd=w,
                           check=True, capture_output=True)
            self.assertEqual(meeting_fs.read_protocol(bare), {})
            self.assertIsNone(participants_from_bare(bare))
            # result_path 是固定位（不依赖 protocol）——路径本身恒定
            self.assertEqual(result_path(base), f"{base}-result.md")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_protocol_bad_json(self):
        tmp, base, bare = make_discussion()
        try:
            w = os.path.join(base, "work-a")
            with open(os.path.join(w, "protocol.json"), "w") as f:
                f.write("not json{{{")
            subprocess.run(["git", "commit", "-am", "bad"], cwd=w, check=True,
                           capture_output=True)
            subprocess.run(["git", "push", "origin", "HEAD"], cwd=w,
                           check=True, capture_output=True)
            self.assertEqual(meeting_fs.read_protocol(bare), {})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestNewMessages(unittest.TestCase):
    """V4。"""

    def test_new_messages_since_and_all(self):
        tmp, base, bare = make_discussion()
        try:
            w = os.path.join(base, "work-a")
            # 写一条消息并 push
            os.makedirs(os.path.join(w, "a"))
            with open(os.path.join(w, "a/0001.md"), "w") as f:
                f.write("---\nfrom: a\ntype: message\nsummary: 第一条\n---\n正文\n")
            subprocess.run(["git", "add", "-A"], cwd=w, check=True,
                           capture_output=True)
            subprocess.run(["git", "commit", "-m", "m1"], cwd=w, check=True,
                           capture_output=True)
            subprocess.run(["git", "push", "origin", "HEAD"], cwd=w,
                           check=True, capture_output=True)
            h1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=bare,
                                capture_output=True, text=True).stdout.strip()
            # 第二条
            with open(os.path.join(w, "a/0002.md"), "w") as f:
                f.write("---\nfrom: a\ntype: message\n---\n第二条\n")
            subprocess.run(["git", "add", "-A"], cwd=w, check=True,
                           capture_output=True)
            subprocess.run(["git", "commit", "-m", "m2"], cwd=w, check=True,
                           capture_output=True)
            subprocess.run(["git", "push", "origin", "HEAD"], cwd=w,
                           check=True, capture_output=True)
            # 全量
            all_msgs = new_messages(bare, "")
            self.assertEqual(len(all_msgs), 2)
            self.assertEqual(all_msgs[0][1], "a/0001.md")
            # since h1 → 只有第二条（拓扑序）
            since_msgs = new_messages(bare, h1)
            self.assertEqual([p for _, p in since_msgs], ["a/0002.md"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestFormat(unittest.TestCase):
    """V5。"""

    def test_format_message_no_frontmatter(self):
        self.assertIsNone(format_message("a/0001.md", "no frontmatter"))

    def test_format_message_without_body(self):
        s = format_message("a/0001.md", "---\nfrom: a\ntype: message\n---\n")
        self.assertIsNotNone(s)
        self.assertIn("[a/0001.md]", s)
        self.assertIn("---", s)


class TestMain(unittest.TestCase):
    """V9。"""

    def test_main_no_discussion(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("sys.argv", ["human_viewer.py",
                                         os.path.join(tmp, "nope")]):
                rc = main()
            self.assertEqual(rc, 1)

    def test_main_normal(self):
        tmp, base, bare = make_discussion()
        try:
            with mock.patch("sys.argv", ["human_viewer.py", base]):
                rc = main()
            self.assertEqual(rc, 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_main_broken_pipe(self):
        """管道消费者提前关闭 → __main__ 静默退出 0（不打印 traceback）。

        BrokenPipeError 处理在 __main__ 块（main() 不捕获）——用 runpy
        以 __main__ 执行验证。"""
        import runpy
        tmp, base, bare = make_discussion()
        try:
            with mock.patch("sys.argv", ["human_viewer.py", base]):
                with mock.patch("sys.stdout") as m:
                    m.write.side_effect = BrokenPipeError
                    with self.assertRaises(SystemExit) as cm:
                        runpy.run_path("human_viewer.py", run_name="__main__")
            self.assertEqual(cm.exception.code, 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)



class TestProgressText(unittest.TestCase):
    """进度行（三样观测面）：状态名用协议术语，不翻译。"""

    def _env(self, agents=("a", "b"), max_meeting=10):
        tmp = tempfile.mkdtemp(prefix="prog-")
        base = os.path.join(tmp, "mv-x-1")
        bare = os.path.join(base, "repo.git")
        os.makedirs(base)
        subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
        w = os.path.join(base, "work-a")
        subprocess.run(["git", "clone", "-q", bare, w], check=True,
                       capture_output=True)
        for k, v in (("user.name", "t"), ("user.email", "t@t")):
            subprocess.run(["git", "config", k, v], cwd=w, check=True)
        with open(os.path.join(w, "protocol.json"), "w") as f:
            json.dump({"participants": list(agents), "resultWriter": agents[-1],
                       "maxMeetingRounds": max_meeting}, f)
        subprocess.run(["git", "add", "-A"], cwd=w, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-qm", "setup"], cwd=w, check=True,
                       capture_output=True)
        subprocess.run(["git", "push", "-q", "origin", "HEAD"], cwd=w,
                       check=True, capture_output=True)
        return tmp, base, bare, w

    def _msg(self, w, ag, n, typ, mode="meeting", nxt=None):
        os.makedirs(os.path.join(w, ag), exist_ok=True)
        fm = f"---\nfrom: {ag}\ntype: {typ}\nmode: {mode}\nseen_at: 1\nto: all\n"
        if nxt:
            fm += f"next: {nxt}\n"
        fm += "---\n\n正文\n"
        with open(os.path.join(w, ag, f"{n:04d}.md"), "w") as f:
            f.write(fm)
        subprocess.run(["git", "add", "-A"], cwd=w, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-qm", f"discuss: {ag}/{n:04d}"],
                       cwd=w, check=True, capture_output=True)
        subprocess.run(["git", "push", "-q", "origin", "HEAD"], cwd=w,
                       check=True, capture_output=True)

    def test_terms_are_protocol_names(self):
        """三样观测面用协议术语（meeting / freezing / rr），不用中文译名。"""
        tmp, base, bare, w = self._env()
        try:
            self._msg(w, "a", 1, "message", "meeting")
            self._msg(w, "b", 1, "freezing", "meeting")
            msgs = each_agent_messages(bare, ["a", "b"])
            lasts = {a: (msgs[a][-1] if msgs[a] else None) for a in ["a", "b"]}
            mode = core_aggregate_mode(lasts)
            txt = progress_text(bare, ["a", "b"], msgs, lasts, mode, 10)
            self.assertIn("meeting 1/10", txt)       # a 的配额消耗
            self.assertIn("freezing 1/2（b）", txt)  # 冻结集合（用协议名）
            self.assertNotIn("冻结", txt)            # 不出现中文译名
            self.assertNotIn("rr →", txt)            # 非 RR 阶段不显示 rr
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_rr_position_only_in_rr(self):
        """RR 阶段显示 `rr → <agent>`（权威实现，取 next 链）。"""
        tmp, base, bare, w = self._env()
        try:
            self._msg(w, "a", 1, "pass", "round-robin", nxt="b")
            msgs = each_agent_messages(bare, ["a", "b"])
            lasts = {a: (msgs[a][-1] if msgs[a] else None) for a in ["a", "b"]}
            mode = core_aggregate_mode(lasts)
            self.assertEqual(mode, "round-robin")
            txt = progress_text(bare, ["a", "b"], msgs, lasts, mode, 10)
            self.assertIn("rr → b", txt)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_quota_without_limit(self):
        """未传配额上限 → 只显示消耗数（不猜、不硬编码默认值）。"""
        tmp, base, bare, w = self._env()
        try:
            self._msg(w, "a", 1, "message", "meeting")
            msgs = each_agent_messages(bare, ["a"])
            lasts = {"a": msgs["a"][-1] if msgs["a"] else None}
            txt = progress_text(bare, ["a"], msgs, lasts,
                                core_aggregate_mode(lasts), None)
            self.assertIn("meeting 1 ｜", txt)
            self.assertNotIn("meeting 1/", txt)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestObserverPollInterval(unittest.TestCase):
    """观察端刷新节奏：消费端常量（不绑定状态机的节奏）。"""

    def test_value_and_lower_bound(self):
        """当前 2.0s；下界 ≥1s（更低会变成无谓 CPU 开销）。"""
        self.assertEqual(human_viewer.OBSERVER_POLL_INTERVAL, 2.0)
        self.assertGreaterEqual(human_viewer.OBSERVER_POLL_INTERVAL, 1.0)

    def test_independent_from_engine_interval(self):
        """有意独立：`follow` 的默认参数**不是** meeting_engine.POLL_INTERVAL
        对象——改 loop 节奏不得静默改变观察契约（共享值 ≠ 共享概念）。"""
        import inspect
        import meeting_engine
        sig = inspect.signature(human_viewer.follow)
        default = sig.parameters["poll_interval"].default
        self.assertEqual(default, human_viewer.OBSERVER_POLL_INTERVAL)
        # 值当前相等（巧合），但改了 engine 的值不该影响 viewer 的默认值
        orig = meeting_engine.POLL_INTERVAL
        try:
            meeting_engine.POLL_INTERVAL = 99.0
            self.assertEqual(sig.parameters["poll_interval"].default, 2.0)
        finally:
            meeting_engine.POLL_INTERVAL = orig


class TestFollowPrintsReport(unittest.TestCase):
    """follow 结束时自动附观测报告（用户通道自带，不依赖任何 LLM 动作）。"""

    def test_report_lines_in_output(self):
        """done → 输出含【分析报告】与流程段（真实 bare，最小构造）。"""
        import threading
        import human_viewer as hv
        tmp = tempfile.mkdtemp(prefix="frep-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        base = os.path.join(tmp, "disc")
        bare = os.path.join(base, "repo.git")
        os.makedirs(base)
        subprocess.run(["git", "init", "--bare", bare], check=True,
                       capture_output=True)
        w = os.path.join(base, "work-a")
        subprocess.run(["git", "clone", bare, w], check=True,
                       capture_output=True)
        for k, v in (("user.name", "t"), ("user.email", "t@t")):
            subprocess.run(["git", "config", k, v], cwd=w, check=True)
        with open(os.path.join(w, "protocol.json"), "w") as f:
            json.dump({"participants": ["a", "b"], "resultWriter": "b"}, f)
        os.makedirs(os.path.join(w, "a"))
        with open(os.path.join(w, "a/0001.md"), "w") as f:
            f.write("---\nfrom: a\ntype: message\nmode: meeting\n---\n\n正文\n")
        with open(os.path.join(w, "result.md"), "w") as f:
            f.write("# 结论\n\n" + "内容" * 40)
        subprocess.run(["git", "add", "-A"], cwd=w, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-m", "discuss: a/0001"], cwd=w,
                       check=True, capture_output=True)
        subprocess.run(["git", "push", "origin", "HEAD"], cwd=w, check=True,
                       capture_output=True)

        out = []

        class FakeOut:
            def write(self, s):
                out.append(s)

            def flush(self):
                pass

        def _run():
            old = sys.stdout
            sys.stdout = FakeOut()
            try:
                hv.follow(base, bare, ["a", "b"], poll_interval=0.05)
            finally:
                sys.stdout = old

        t = threading.Thread(target=_run)
        t.start()
        # 写 concluded（b 收尾）→ follow 退出
        subprocess.run(["git", "pull", "--rebase", "--autostash"], cwd=w,
                       check=False, capture_output=True)
        os.makedirs(os.path.join(w, "b"), exist_ok=True)
        with open(os.path.join(w, "b/0001.md"), "w") as f:
            f.write("---\nfrom: b\ntype: concluded\nmode: concluded\n---\n\n")
        subprocess.run(["git", "add", "-A"], cwd=w, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-m", "discuss: b/0001"], cwd=w,
                       check=True, capture_output=True)
        subprocess.run(["git", "push", "origin", "HEAD"], cwd=w, check=True,
                       capture_output=True)
        t.join(timeout=15)
        self.assertFalse(t.is_alive(), "follow 未退出")
        txt = "".join(out)
        self.assertIn("【分析已结束】", txt)
        self.assertIn("【分析报告】", txt)
        self.assertIn("流程：", txt)


if __name__ == "__main__":
    unittest.main()
