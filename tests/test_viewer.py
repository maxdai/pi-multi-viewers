"""human_viewer 单元测试——V1-V9 补缺（API 清单基准，2026-09-01）。

TestViewer（test_human.py）已覆盖 format_message/incremental/follow，
本文件补：_protocol/participants_from_bare/result_path 边界 + main 入口。
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

from human_viewer import (_protocol, participants_from_bare, result_path,
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
            self.assertEqual(_protocol(bare)["participants"], ["a", "b"])
            self.assertEqual(participants_from_bare(bare), ["a", "b"])
            self.assertTrue(result_path(base, bare).endswith("work-b/result.md"))
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
            self.assertEqual(_protocol(bare), {})
            self.assertIsNone(participants_from_bare(bare))
            self.assertEqual(result_path(base, bare), "")
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
            self.assertEqual(_protocol(bare), {})
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


if __name__ == "__main__":
    unittest.main()
