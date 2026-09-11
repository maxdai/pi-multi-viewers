# -*- coding: utf-8 -*-
"""start_discussion.py 生成函数单测（审计#4：环境生成层质量防线）。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from start_discussion import (
    gen_protocol, gen_question, gen_agent_def, gen_agents_md,
    run_cmd, _resolve_path, _default_model, check_status,
)


class Args:
    """gen_agents_md 的 args 桩（只读 background）。"""
    def __init__(self, background=None):
        self.background = background


class TestGenProtocol(unittest.TestCase):
    def test_result_writer_default(self):
        # 默认 resultWriter = 最后一位参与者
        p = gen_protocol("t", ["a", "b", "c"], 5, 5)
        self.assertEqual(p["resultWriter"], "c")
        self.assertEqual(p["participants"], ["a", "b", "c"])
        self.assertEqual(p["maxMeetingRounds"], 5)
        self.assertEqual(p["maxRRRounds"], 5)
        self.assertEqual(p["stallTimeoutSeconds"], 600)
        self.assertEqual(p["commitPolicy"], "one-message-per-commit")

    def test_result_writer_explicit(self):
        p = gen_protocol("t", ["a", "b", "c"], 5, 5, result_writer="a")
        self.assertEqual(p["resultWriter"], "a")

    def test_pure_flag(self):
        p = gen_protocol("t", ["a", "b"], 5, 5, pure=True)
        self.assertEqual(p.get("pure"), True)
        p2 = gen_protocol("t", ["a", "b"], 5, 5)
        self.assertNotIn("pure", p2)


    def test_fork_fields(self):
        """fork 模式（多视角）：--fork-source → protocol.json 写入
        forkSource/forkCwd；不传则两字段不出现（legacy 兼容）。"""
        p = gen_protocol("t", ["a", "b"], 5, 5,
                         fork_source="/x/sessions/main.jsonl",
                         fork_cwd="/proj")
        self.assertEqual(p["forkSource"], "/x/sessions/main.jsonl")
        self.assertEqual(p["forkCwd"], "/proj")
        p2 = gen_protocol("t", ["a", "b"], 5, 5)
        self.assertNotIn("forkSource", p2)
        self.assertNotIn("forkCwd", p2)


class TestGenQuestion(unittest.TestCase):
    def test_topic_only(self):
        q = gen_question("主题", None, None, None)
        self.assertIn("# 分析主题：主题", q)
        self.assertNotIn("初始立场", q)
        self.assertNotIn("待回答的问题", q)

    def test_with_stances_and_questions(self):
        q = gen_question("主题", {"a": "立场A"}, None, ["问题1", "问题2"])
        self.assertIn("## 初始立场", q)
        self.assertIn("- a: 立场A", q)
        self.assertIn("## 待回答的问题", q)
        self.assertIn("- 问题1", q)
        self.assertIn("- 问题2", q)


class TestGenAgentDef(unittest.TestCase):
    def test_no_model_no_stance(self):
        d = gen_agent_def("a", ["a", "b"])
        self.assertIn("你是 a", d)
        self.assertNotIn("你使用模型", d)          # 无明确模型不提示
        self.assertNotIn("你的立场", d)          # 无立场不指向不存在节
        self.assertIn("按 AGENTS.md", d)

    def test_default_variant_not_in_prompt(self):
        # Pi 适配：variant 不再写进 agent prompt，由 pi-agent.json 的
        # thinking 字段承载；这里确保 prompt 不含 opencode frontmatter。
        d = gen_agent_def("a", ["a", "b"])
        self.assertNotIn("variant:", d)
        self.assertNotIn("mode: primary", d)

    def test_with_model(self):
        d = gen_agent_def("a", ["a", "b"], models={"a": "opencode-go/x"})
        self.assertIn("你使用模型 opencode-go/x 参与讨论", d)

    def test_model_per_agent(self):
        # 只给 a 配 model，b 不配（per-agent 粒度）
        da = gen_agent_def("a", ["a", "b"], models={"a": "m1"})
        db = gen_agent_def("b", ["a", "b"], models={"a": "m1"})
        self.assertIn("你使用模型 m1", da)
        self.assertNotIn("你使用模型", db)

    def test_stance_ref_per_agent(self):
        # 只给 a 配立场，b 不指向不存在的立场节（审核#8）
        da = gen_agent_def("a", ["a", "b"], stances={"a": "立场A"})
        db = gen_agent_def("b", ["a", "b"], stances={"a": "立场A"})
        self.assertIn("你的立场和观点见 question.md", da)
        self.assertNotIn("你的立场和观点见 question.md", db)


class TestGenAgensMd(unittest.TestCase):
    def test_declares_project_rules_precedence(self):
        """A4：协议含"项目规则 vs 本协议"的优先级声明——主项目 AGENTS.md
        会被 fork 模式自动发现（cwd 祖先链），其流程性条款（改动先 commit /
        阶段性 push）与讨论协议"不要执行 git 操作"冲突，必须显式消解。"""
        md = gen_agents_md(Args(), "a", ["a", "b"])
        self.assertIn("上下文中的项目规则", md)
        self.assertIn("优先于", md)

    def test_background_default(self):
        md = gen_agents_md(Args(), "a", ["a", "b"])
        self.assertIn("## 背景", md)
        self.assertIn("（无）", md)
        self.assertIn("参与者：a、b", md)

    def test_background_custom(self):
        md = gen_agents_md(Args("审核代码"), "b", ["a", "b", "c"])
        self.assertIn("审核代码", md)
        self.assertIn("参与者：a、b、c", md)

    def test_main_pi_cwd_injected(self):
        """主 pi cwd 程序化注入（2026-09-02）：传入时出现节，未传时隐藏。"""
        md = gen_agents_md(Args(), "a", ["a", "b"], main_pi_cwd="/x/proj")
        self.assertIn("主 pi 工作目录", md)
        self.assertIn("/x/proj", md)
        # 未传（手动场景）→ 节隐藏
        md2 = gen_agents_md(Args(), "a", ["a", "b"])
        self.assertNotIn("主 pi 工作目录", md2)
        self.assertNotIn("（未提供）", md2)

    def test_no_prepare_section(self):
        """prepare 机制已移除（fork 模式上下文自带）：模板与产物均无引用节。"""
        md = gen_agents_md(Args(), "a", ["a", "b"], main_pi_cwd="/x")
        self.assertNotIn("讨论背景文件", md)
        self.assertNotIn("discuss_prepare", md)
        self.assertNotIn("PREPARE_FILE_SECTION", md)
        self.assertNotIn("{BACKGROUND}", md)


class TestMisc(unittest.TestCase):
    """S1 run / S11 _resolve_path / S3 _default_model / S17 check_status。"""

    def test_run_success_and_failure(self):
        r = run_cmd(["echo", "hi"])
        self.assertEqual(r.returncode, 0)
        with self.assertRaises(RuntimeError):
            run_cmd(["false"])
        r2 = run_cmd(["false"], check=False)
        self.assertNotEqual(r2.returncode, 0)

    def test_resolve_path(self):
        self.assertTrue(_resolve_path("/abs/path").startswith("/"))
        self.assertTrue(_resolve_path("~/x").startswith(os.path.expanduser("~")))
        self.assertTrue(_resolve_path("rel").endswith("rel"))

    def test_default_model(self):
        m = _default_model()  # 本机有 pi settings → 非 None 或 None 均合理
        self.assertTrue(m is None or isinstance(m, str))

    def test_check_status_not_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = check_status(os.path.join(tmp, "nope"))
            self.assertEqual(status, "not-exists")

    def test_check_status_stopped(self):
        # 有 bare 无 result.md 无 loop 进程 → stopped
        tmp = tempfile.mkdtemp(prefix="status-")
        try:
            base = os.path.join(tmp, "d")
            bare = os.path.join(base, "repo.git")
            os.makedirs(base)
            subprocess.run(["git", "init", "--bare", bare], check=True,
                           capture_output=True)
            status = check_status(base)
            self.assertEqual(status, "stopped")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)



class TestCheckStatusConcluded(unittest.TestCase):
    """B2：check_status 的 concluded 判定 = 状态机同一定义（aggregate_mode），
    不是 `git grep` 全文匹配——后者会被 result.md/消息正文里的
    `type: concluded` 行误触发（误报 done → --wait 落无上界轮询）。"""

    def _env(self, tmp, with_result_md, msg_type, result_body_extra=""):
        base = os.path.join(tmp, "disc")
        bare = os.path.join(base, "repo.git")
        os.makedirs(base)
        subprocess.run(["git", "init", "--bare", bare], check=True,
                       capture_output=True)
        w = os.path.join(base, "work-a")
        subprocess.run(["git", "clone", bare, w], check=True, capture_output=True)
        for k, v in (("user.name", "t"), ("user.email", "t@t")):
            subprocess.run(["git", "config", k, v], cwd=w, check=True)
        with open(os.path.join(w, "protocol.json"), "w") as f:
            json.dump({"participants": ["a"], "resultWriter": "a"}, f)
        os.makedirs(os.path.join(w, "a"))
        with open(os.path.join(w, "a/0001.md"), "w") as f:
            f.write(f"---\nfrom: a\ntype: {msg_type}\n---\n\n正文\n")
        if with_result_md:
            # 正文里**含** `type: concluded` 行（代码块/协议片段引用）——
            # 全文 grep 会命中，聚合判定不会
            with open(os.path.join(w, "result.md"), "w") as f:
                # 正文需 >50 字节（is_finished 的"产物有效"判据——
                # concluded 且 result.md 有效才算收尾完成）
                f.write("# 结论\n\n```\ntype: concluded\n```\n"
                        "本报告正文用于满足有效性阈值，非空且具实质内容。\n"
                        + result_body_extra)
        subprocess.run(["git", "add", "-A"], cwd=w, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "setup"], cwd=w, check=True,
                       capture_output=True)
        subprocess.run(["git", "push", "origin", "HEAD"], cwd=w, check=True,
                       capture_output=True)
        return base

    def test_result_md_text_with_concluded_not_done(self):
        """result.md 正文含 `type: concluded` 行、但消息未 concluded →
        不得报 done（旧实现 git grep 全文会命中 → 误报）。"""
        import start_discussion as sd
        with tempfile.TemporaryDirectory() as tmp:
            base = self._env(tmp, with_result_md=True, msg_type="message")
            self.assertEqual(sd.check_status(base), "stalled")   # 无 loop

    def test_message_concluded_gives_done(self):
        """消息 type=concluded（协议信号）→ done。"""
        import start_discussion as sd
        with tempfile.TemporaryDirectory() as tmp:
            base = self._env(tmp, with_result_md=True, msg_type="concluded")
            self.assertEqual(sd.check_status(base), "done")

    def test_no_result_md_not_done(self):
        """无 result.md → 非 done（stopped/stalled 分支）。"""
        import start_discussion as sd
        with tempfile.TemporaryDirectory() as tmp:
            base = self._env(tmp, with_result_md=False, msg_type="message")
            self.assertIn(sd.check_status(base), ("stopped", "stalled"))


class TestLoopPids(unittest.TestCase):
    """A2：loop 存活判据 = argv 精确匹配（/proc 扫描），不是命令行文本正则。

    生产形态的 loop 是 `python3 <base>/meeting_loop.py <workdir> <agent>`
    ——argv 里恰有等于 `<base>/meeting_loop.py` 的元素。
    """

    def _fake_loop(self, base, arg_extra=("w", "a")):
        """起一个假 loop（内容仅 sleep），argv 与生产形态同构。"""
        os.makedirs(base, exist_ok=True)
        loop_py = os.path.join(base, "meeting_loop.py")
        with open(loop_py, "w", encoding="utf-8") as f:
            f.write("import time\ntime.sleep(30)\n")
        p = subprocess.Popen([sys.executable, loop_py, *arg_extra],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        time.sleep(0.3)      # 等 /proc 就绪
        return p

    def test_detects_real_loop_argv(self):
        """生产形态的 loop 被检出（存活性判据有效）。"""
        import start_discussion as sd
        with tempfile.TemporaryDirectory() as base:
            p = self._fake_loop(base)
            try:
                self.assertIn(str(p.pid), sd._loop_pids(base))
                self.assertTrue(sd._loops_alive(base))
            finally:
                p.terminate()
                p.wait(timeout=5)

    def test_text_mention_not_detected(self):
        """命令行**提到**该路径但不是独立 argv（bash -c 包装、探针命令）
        → 不检出——根治自匹配（文本正则会把调用者自身算进来）。"""
        import start_discussion as sd
        with tempfile.TemporaryDirectory() as base:
            loop_py = os.path.join(base, "meeting_loop.py")
            # 该进程的 argv = [bash, -c, "sleep 3  # <loop_py>"]——文本含
            # loop_py，但没有等于它的独立 argv 元素
            p = subprocess.Popen(["bash", "-c", f"sleep 3  # {loop_py}"],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            try:
                time.sleep(0.3)
                self.assertEqual(sd._loop_pids(base), [])
                self.assertFalse(sd._loops_alive(base))
            finally:
                p.terminate()
                p.wait(timeout=5)

    def test_dead_loop_not_detected(self):
        """已退出进程不残留判定（僵尸/回收后不再计入）。"""
        import start_discussion as sd
        with tempfile.TemporaryDirectory() as base:
            p = self._fake_loop(base)
            p.terminate()
            p.wait(timeout=5)
            time.sleep(0.2)
            self.assertEqual(sd._loop_pids(base), [])


if __name__ == "__main__":
    unittest.main()
