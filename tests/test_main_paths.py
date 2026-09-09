"""main()/__main__ 拼接点测试——消除 L14 同类盲区（2026-09-01）。

背景（用户质疑）：L14 bug 为什么"随便测试"就碰上？因为旧测试只观察
agent_loop 内部，main/__main__ 拼接链（agent_loop 返回后的调用、CLI
分发）是结构盲区。本文件补三个拼接点：meeting_loop.__main__（rw 退出
调用 _preserve_result_md）、start_discussion.main（命令分发）、
human_sayer.main（文本/stdin/错误路径）。验证新方法（逐 API + 拼接点）
能系统覆盖这类问题。
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


class TestMeetingLoopMain(unittest.TestCase):
    """meeting_loop.__main__ 拼接链：真实 subprocess 跑生产路径。

    runpy 执行脚本时 __main__ 是新模块对象，mock 打不到脚本内名字——
    改为构造真实环境（bare 有 concluded + result.md）→ 跑
    python3 meeting_loop.py → agent_loop 检测 concluded 退出 →
    rw 调用 _preserve_result_md（L14 修复的生产路径验证）。
    """

    def _make_done_env(self):
        """构造：bare + work-b + protocol(rw=b) + b/0001 concluded + result.md。"""
        tmp = tempfile.mkdtemp(prefix="mlmain-")
        base = os.path.join(tmp, "disc")
        bare = os.path.join(base, "repo.git")
        os.makedirs(base)
        subprocess.run(["git", "init", "--bare", bare], check=True,
                       capture_output=True)
        w = os.path.join(base, "work-b")
        subprocess.run(["git", "clone", bare, w], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=w, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=w, check=True)
        with open(os.path.join(w, "protocol.json"), "w") as f:
            json.dump({"participants": ["a", "b"], "resultWriter": "b",
                       "maxMeetingRounds": 10, "maxRRRounds": 7,
                       "stallTimeoutSeconds": 600}, f)
        os.makedirs(os.path.join(w, "b"))
        with open(os.path.join(w, "b/0001.md"), "w") as f:
            f.write("---\nfrom: b\ntype: concluded\nmode: concluded\n---\n")
        with open(os.path.join(w, "result.md"), "w") as f:
            f.write("# 结论\n\n" + "内容" * 20)
        subprocess.run(["git", "add", "-A"], cwd=w, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-m", "setup"], cwd=w, check=True,
                       capture_output=True)
        subprocess.run(["git", "push", "origin", "HEAD"], cwd=w, check=True,
                       capture_output=True)
        return tmp, base, w

    def test_rw_loop_exits_and_preserves_result(self):
        """生产拼接链：concluded → agent_loop 退出 → rw 保存 result.md。

        正是 L14 修复的验证：修复前此路径 NameError（写文件后崩溃）；
        修复后正常退出 0 且 result.md 保存到父级。"""
        tmp, base, w = self._make_done_env()
        try:
            r = subprocess.run([sys.executable, "meeting_loop.py", w, "b"],
                               cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               capture_output=True, text=True, timeout=60)
            # 修复后：正常退出（无 NameError traceback）
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("NameError", r.stderr)
            # result.md 保存到父级
            saved = os.path.join(tmp, "disc-result.md")
            self.assertTrue(os.path.exists(saved))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_non_rw_loop_no_preserve(self):
        """非 rw 的 loop：concluded 退出，不保存 result.md。"""
        tmp, base, w = self._make_done_env()
        try:
            # work-a（非 rw）
            wa = os.path.join(base, "work-a")
            subprocess.run(["git", "clone", os.path.join(base, "repo.git"),
                            wa], check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=wa,
                           check=True)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=wa,
                           check=True)
            r = subprocess.run([sys.executable, "meeting_loop.py", wa, "a"],
                               cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            saved = os.path.join(tmp, "disc-result.md")
            self.assertFalse(os.path.exists(saved))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestStartDiscussionMain(unittest.TestCase):
    """start_discussion.main：命令分发。"""

    def test_main_requires_dir(self):
        """无 --dir 且非 --spec-gen → 打印错误并返回（main 用 return 非 sys.exit）。"""
        import start_discussion as sd
        with mock.patch("sys.argv", ["start_discussion.py"]):
            with mock.patch("builtins.print") as p:
                rc = sd.main()
            self.assertIsNone(rc)
            p.assert_any_call(mock.ANY)

    def test_main_status_dispatches(self):
        """--status 分发到 check_status 并打印。"""
        import start_discussion as sd
        with mock.patch("sys.argv", ["start_discussion.py", "--dir", "/x",
                                     "--status"]):
            with mock.patch("start_discussion.check_status",
                            return_value=("stopped", None)) as cs:
                with mock.patch("builtins.print"):
                    sd.main()
                    cs.assert_called_once_with("/x")

    def test_main_cleanup_dispatches(self):
        import start_discussion as sd
        with mock.patch("sys.argv", ["start_discussion.py", "--dir", "/x",
                                     "--cleanup"]):
            with mock.patch("start_discussion.cleanup_discussion") as cd:
                sd.main()
                cd.assert_called_once_with("/x")

    def test_main_setup_dispatches(self):
        """--dir + topic → setup_environment（真实构造太重，mock 验证分发）。"""
        import start_discussion as sd
        with mock.patch("sys.argv", ["start_discussion.py", "--dir", "/x",
                                     "--topic", "T", "--agents", "a,b"]):
            with mock.patch("start_discussion.setup_environment") as se:
                with mock.patch("start_discussion.os.path.isdir",
                                return_value=False):
                    sd.main()
                    se.assert_called_once()
                    args = se.call_args[0]
                    self.assertEqual(args[0].topic, "T")

    def test_main_spec_gen(self):
        """--spec-gen 生成骨架（真实调用，验证产物）。"""
        import start_discussion as sd
        with tempfile.TemporaryDirectory() as tmp:
            spec = os.path.join(tmp, "myspec")
            with mock.patch("sys.argv", ["start_discussion.py",
                                         "--spec-gen", spec,
                                         "--agents", "a,b"]):
                sd.main()
            self.assertTrue(os.path.isfile(os.path.join(spec, "question.md")))
            self.assertTrue(os.path.isdir(os.path.join(spec, "agents")))


class TestHumanSayerMain(unittest.TestCase):
    """human_sayer.main：文本参数 / stdin / 错误路径。"""

    def test_main_no_discussion(self):
        import human_sayer as hs
        with mock.patch("sys.argv", ["human_sayer.py", "/nonexistent"]):
            rc = hs.main()
            self.assertEqual(rc, 1)

    def test_main_no_work_human(self):
        import human_sayer as hs
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "disc")
            os.makedirs(os.path.join(base, "repo.git"))  # bare 存在
            with mock.patch("sys.argv", ["human_sayer.py", base, "文本"]):
                rc = hs.main()
                self.assertEqual(rc, 1)  # work-human 不存在

    def test_main_text_arg_dispatches_say(self):
        import human_sayer as hs
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "disc")
            os.makedirs(os.path.join(base, "repo.git"))
            os.makedirs(os.path.join(base, "work-human"))
            with mock.patch("sys.argv", ["human_sayer.py", base, "插话文本"]):
                with mock.patch("human_sayer.say",
                                return_value=("human/0001.md", "插话文本")) as say:
                    rc = hs.main()
                    self.assertEqual(rc, 0)
                    say.assert_called_once_with(
                        os.path.join(base, "work-human"), "插话文本")

    def test_main_empty_body_rejected(self):
        import human_sayer as hs
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "disc")
            os.makedirs(os.path.join(base, "repo.git"))
            os.makedirs(os.path.join(base, "work-human"))
            with mock.patch("sys.argv", ["human_sayer.py", base, "   "]):
                rc = hs.main()
                self.assertEqual(rc, 1)

    def test_main_stdin_body(self):
        """无文本参数 → 读 stdin。"""
        import human_sayer as hs
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "disc")
            os.makedirs(os.path.join(base, "repo.git"))
            os.makedirs(os.path.join(base, "work-human"))
            with mock.patch("sys.argv", ["human_sayer.py", base]):
                with mock.patch("sys.stdin.read",
                                return_value="stdin 内容\n第二行\n"):
                    with mock.patch("human_sayer.say",
                                    return_value=("human/0001.md", "stdin 内容")) as say:
                        rc = hs.main()
                        self.assertEqual(rc, 0)
                        say.assert_called_once_with(
                            os.path.join(base, "work-human"),
                            "stdin 内容\n第二行")


if __name__ == "__main__":
    unittest.main()
