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


class TestStatusStalled(unittest.TestCase):
    """T3/#7（e2e7 评审）：状态全集 + 收尾中断（stalled）可区分。

    原实现"有 result.md 无 concluded"恒 running 且不看存活 → --wait
    无限轮询；现 stalled = 无 loop 存活 → --wait 有界退出。
    """

    def _bare_with_result_md(self, tmp):
        """构造：bare + result.md 提交（无 concluded）→ 无 loop 存活。"""
        base = os.path.join(tmp, "d")
        bare = os.path.join(base, "repo.git")
        os.makedirs(base)
        import subprocess
        subprocess.run(["git", "init", "--bare", bare], check=True,
                       capture_output=True)
        work = os.path.join(tmp, "w")
        subprocess.run(["git", "clone", bare, work], check=True,
                       capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=work)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=work)
        with open(os.path.join(work, "result.md"), "w") as f:
            f.write("# 结论\n")
        subprocess.run(["git", "add", "-A"], cwd=work, check=True)
        subprocess.run(["git", "commit", "-qm", "discuss: result.md"],
                       cwd=work, check=True)
        subprocess.run(["git", "push", bare, "master"], cwd=work,
                       check=True, capture_output=True)
        return base

    def test_stalled_when_no_loop(self):
        from start_discussion import check_status
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            base = self._bare_with_result_md(tmp)
            # 无 loop 存活 + row 有 result.md 无 concluded → stalled
            self.assertEqual(check_status(base), "stalled")

    def test_single_return_value(self):
        """#7：返回值不再有恒 None 的装饰性第二项。"""
        from start_discussion import check_status
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            v = check_status(os.path.join(tmp, "nope"))
            self.assertIsInstance(v, str)
            self.assertEqual(v, "not-exists")


class TestCliExitCodes(unittest.TestCase):
    """CLI 错误退出码语义（W4 修复，e2e7 评审）：错误分支必须 exit≠0
    ——此前裸 return 使 wrapper `if ! python3 …` 判据失效（静默失败：
    打印错误后继续 rm spec + 报"已启动"）。"""

    def _run(self, args, cwd=None):
        import subprocess
        ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, "start_discussion.py"), *args],
            capture_output=True, text=True, cwd=cwd or ROOT)

    def test_skip_setup_missing_env_exit1(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(["--dir", os.path.join(tmp, "nope"),
                           "--skip-setup", "--start"])
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_dir_exists_exit1(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "discuss-x")
            os.makedirs(base)
            r = self._run(["--dir", base, "--topic", "T", "--agents", "a,b",
                           "--fork-source", "/s/x.jsonl"])
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_bad_agent_name_exit1(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(["--dir", os.path.join(tmp, "d"), "--topic", "T",
                           "--agents", "a b", "--fork-source", "/s/x.jsonl"])
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_human_reserved_exit1(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(["--dir", os.path.join(tmp, "d"), "--topic", "T",
                           "--agents", "a,human", "--fork-source", "/s/x.jsonl"])
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)


class TestMeetingLoopMain(unittest.TestCase):
    """meeting_loop.__main__ 拼接链：真实 subprocess 跑生产路径。

    runpy 执行脚本时 __main__ 是新模块对象，mock 打不到脚本内名字——
    改为构造真实环境（bare 有 concluded + result.md）→ 跑
    python3 meeting_loop.py → agent_loop 检测 concluded 退出 →
    rw 调用 _preserve_result_md（L14 修复的生产路径验证）。
    """

    def setUp(self):
        self.src = os.path.join(tempfile.mkdtemp(prefix="mlsrc-"),
                                "main.jsonl")
        with open(self.src, "w") as f:
            f.write('{"type":"session","id":"src"}\n')

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
                       "stallTimeoutSeconds": 600,
                       "forkSource": self.src, "forkCwd": w}, f)
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

    def test_invalid_fork_mode_exits_nonzero(self):
        """loop 门（P0，e2e10 评审）：protocol.json 的 forkMode 非法 →
        [fatal] + 退出码非零（配置错误不进 engine 重试路径），且不生成
        fork 源。历史值（rename 前的 curated）同样被拦。"""
        tmp, base, w = self._make_done_env()
        try:
            proto_path = os.path.join(w, "protocol.json")
            with open(proto_path) as f:
                proto = json.load(f)
            proto["forkMode"] = "curated"       # rename 前的历史值
            with open(proto_path, "w") as f:
                json.dump(proto, f)
            # 协议权威在 bare HEAD（LLM 可改本地副本 → 判定不读本地）——
            # 提交后才生效
            subprocess.run(["git", "add", "-A"], cwd=w, check=True,
                           capture_output=True)
            subprocess.run(["git", "commit", "-m", "bad forkMode"], cwd=w,
                           check=True, capture_output=True)
            subprocess.run(["git", "push", "origin", "HEAD"], cwd=w,
                           check=True, capture_output=True)
            r = subprocess.run(
                [sys.executable, "meeting_loop.py", w, "b"],
                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                capture_output=True, text=True, timeout=60)
            self.assertNotEqual(r.returncode, 0)
            out = r.stdout + r.stderr
            self.assertIn("[fatal]", out)
            self.assertIn("forkMode", out)
            # 未生成 fork 源（值域门在 open 之前）
            sessions_dir = os.path.join(base, "pi-sessions")
            self.assertFalse(os.path.isdir(sessions_dir) and
                             [f for f in os.listdir(sessions_dir)
                              if f.startswith("fork-src-")])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_local_protocol_edit_ignored(self):
        """协议权威在 bare：本地副本改动（未提交）不影响 —— 防止 LLM 用
        写权限改 protocol.json 影响流程判定（engine/loop/check_status 均
        走 fs.read_protocol(bare)）。"""
        tmp, base, w = self._make_done_env()
        try:
            proto_path = os.path.join(w, "protocol.json")
            with open(proto_path) as f:
                proto = json.load(f)
            proto["resultWriter"] = "a"          # 本地改成另一个收尾者
            proto["maxMeetingRounds"] = 999      # 本地放大配额
            with open(proto_path, "w") as f:
                json.dump(proto, f)
            # 不提交——loop 读到的必须仍是 bare 的（resultWriter=b）
            import meeting_engine as me
            self.assertEqual(me.result_writer(w), "b")
            self.assertEqual(me.participants(w), ["a", "b"])
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


class TestBuildReport(unittest.TestCase):
    """--report（观测面唯一机器消费出口）：各段取数 + fail-open。"""

    def _env(self, tmp, with_loop_log=True, with_session=True, commits=()):
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
            json.dump({"participants": ["a", "b"], "resultWriter": "b",
                       "maxMeetingRounds": 10}, f)
        subprocess.run(["git", "add", "-A"], cwd=w, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-qm", "discuss: setup"], cwd=w,
                       check=True, capture_output=True)
        for subj in commits:      # 追加消息 commit（subject = discuss: x/N）
            with open(os.path.join(w, "dummy"), "w") as f:
                f.write(subj)
            subprocess.run(["git", "add", "-A"], cwd=w, check=True,
                           capture_output=True)
            subprocess.run(["git", "commit", "-qm", subj], cwd=w, check=True,
                           capture_output=True)
        subprocess.run(["git", "push", "-q", "origin", "HEAD"], cwd=w,
                       check=True, capture_output=True)
        if with_loop_log:
            with open(os.path.join(base, "loop-a.log"), "w") as f:
                f.write("[2026-09-11T12:00:00.000] a: 唤醒 pi (session=s1)\n")
                f.write("[2026-09-11T12:00:42.100] a: pi 完成"
                        "（session=s1 elapsed_ms=42100 rc=0）\n")
        if with_session:
            with open(os.path.join(base, "status-a.json"), "w") as f:
                json.dump({"sessionID": "sid-a"}, f)
            os.makedirs(os.path.join(base, "pi-sessions"))
            with open(os.path.join(base, "pi-sessions/fork-src-sid-a.jsonl"),
                      "w") as f:
                f.write(json.dumps({"type": "message", "message": {
                    "role": "assistant", "stopReason": "toolUse",
                    "usage": {"input": 274, "cacheRead": 183552,
                              "output": 1200}}}) + "\n")
        return base

    def test_sections(self):
        """四段齐备：流程/配额/进程（登记字段）/LLM（session 字段）。"""
        import start_discussion as sd
        with tempfile.TemporaryDirectory() as tmp:
            base = self._env(tmp, commits=["discuss: a/0001",
                                           "discuss: b/0001",
                                           "discuss: human/0001"])
            txt = "\n".join(sd.build_report(base))
            self.assertIn("流程：2 agents | 消息 2", txt)
            self.assertIn("a 1 / b 1", txt)
            self.assertIn("human 插话 1 条", txt)
            # 登记字段 elapsed_ms=42100 → 人类可读"进程跨度 总 42s"
            self.assertIn("进程跨度 总 42s", txt)
            self.assertIn("rc≠0 0 次", txt)
            self.assertIn("cacheRead 183.6k", txt)
            self.assertIn("三者不可互替", txt)     # 跨度分标

    def test_fail_open_missing_dir(self):
        """目录不存在 → n/a（不抛异常、不报错）。"""
        import start_discussion as sd
        with tempfile.TemporaryDirectory() as tmp:
            txt = "\n".join(sd.build_report(os.path.join(tmp, "nope")))
            self.assertIn("n/a", txt)

    def test_fail_open_missing_logs_and_sessions(self):
        """缺 loop log / session → 对应段 n/a，其余段仍输出（段级隔离）。"""
        import start_discussion as sd
        with tempfile.TemporaryDirectory() as tmp:
            base = self._env(tmp, with_loop_log=False, with_session=False)
            txt = "\n".join(sd.build_report(base))
            self.assertIn("进程（loop log 登记字段）", txt)
            self.assertIn("n/a", txt)
            self.assertIn("LLM（session 文档化字段）", txt)


class TestStartDiscussionMain(unittest.TestCase):
    """start_discussion.main：命令分发。"""

    def test_main_requires_dir(self):
        """无 --dir 且非 --spec-gen → 打印错误并 sys.exit(1)（CLI 错误语义
        统一非零退出码，wrapper || 可捕获——2026-09-09 收敛）。"""
        import start_discussion as sd
        with mock.patch("sys.argv", ["start_discussion.py"]):
            with mock.patch("builtins.print") as p:
                with self.assertRaises(SystemExit) as cm:
                    sd.main()
            self.assertEqual(cm.exception.code, 1)
            p.assert_any_call(mock.ANY)

    def test_main_status_dispatches(self):
        """--status 分发到 check_status 并打印。"""
        import start_discussion as sd
        with mock.patch("sys.argv", ["start_discussion.py", "--dir", "/x",
                                     "--status"]):
            with mock.patch("start_discussion.check_status",
                            return_value="stopped") as cs:
                with mock.patch("builtins.print"):
                    sd.main()
                    cs.assert_called_once_with("/x")

    def _wait_with_state(self, tmp, state):
        """--wait 在给定 check_status 下的终态输出（真实 subprocess 太重，
        仅驱动 main 的等待分支）。"""
        import start_discussion as sd
        import human_viewer
        base = os.path.join(tmp, "disc-x")
        os.makedirs(base, exist_ok=True)
        out = []
        with mock.patch("sys.argv", ["start_discussion.py", "--dir", base,
                                     "--wait"]):
            with mock.patch("start_discussion.check_status",
                            return_value=state):
                # --wait 前置会读 bare 的参与者列表（本测试不建 bare）
                with mock.patch.object(human_viewer,
                                       "participants_from_bare",
                                       return_value=[]):
                    with mock.patch("builtins.print",
                                    side_effect=lambda *a, **k: out.append(
                                        " ".join(str(x) for x in a))):
                        rc = sd.main()
        return rc, "\n".join(out), base

    def test_wait_stopped_single_message(self):
        """--wait 在 stopped 态：一条动作完整的提示（§3.4-P4）。

        此前按 `glob(loop-*.log)` 存在性分叉成两条文案——那是拿日志当判定
        输入（唯一实例），且两条给出的动作都不可执行。现在**不看日志**，
        只给一条含可执行动作（`--skip-setup --start` / `--cleanup`）的提示。
        """
        with tempfile.TemporaryDirectory() as tmp:
            rc, txt, base = self._wait_with_state(tmp, "stopped")
            self.assertEqual(rc, 1)
            self.assertIn("未在运行且未收尾", txt)
            self.assertIn("--skip-setup --start", txt)
            self.assertIn("--cleanup", txt)
            # loop-*.log 存在与否不改变输出（零判定输入）
            with open(os.path.join(base, "loop-a.log"), "w") as f:
                f.write("x")
            rc2, txt2, _ = self._wait_with_state(tmp, "stopped")
            self.assertEqual(txt2, txt)

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
                                     "--topic", "T", "--agents", "a,b",
                                     "--fork-source", "/s/main.jsonl"]):
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
