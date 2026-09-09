# -*- coding: utf-8 -*-
"""start_discussion.py 生成函数单测（审计#4：环境生成层质量防线）。"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from start_discussion import (
    gen_protocol, gen_question, gen_agent_def, gen_agens_md,
    run, _resolve_path, _default_model, check_status,
)


class Args:
    """gen_agens_md 的 args 桩（只读 background）。"""
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


class TestGenQuestion(unittest.TestCase):
    def test_topic_only(self):
        q = gen_question("主题", None, None, None)
        self.assertIn("# 讨论主题：主题", q)
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
    def test_background_default(self):
        md = gen_agens_md(Args(), "a", ["a", "b"])
        self.assertIn("## 背景", md)
        self.assertIn("（无）", md)
        self.assertIn("参与者：a、b", md)

    def test_background_custom(self):
        md = gen_agens_md(Args("审核代码"), "b", ["a", "b", "c"])
        self.assertIn("审核代码", md)
        self.assertIn("参与者：a、b、c", md)

    def test_main_pi_cwd_injected(self):
        """主 pi cwd 程序化注入（2026-09-02）：传入时出现节，未传时隐藏。"""
        md = gen_agens_md(Args(), "a", ["a", "b"], main_pi_cwd="/x/proj")
        self.assertIn("主 pi 工作目录", md)
        self.assertIn("/x/proj", md)
        # 未传（手动场景）→ 节隐藏
        md2 = gen_agens_md(Args(), "a", ["a", "b"])
        self.assertNotIn("主 pi 工作目录", md2)
        self.assertNotIn("（未提供）", md2)

    def test_prepare_file_reference(self):
        """背景文件引用节（2026-09-02 两 prompt 拆分）：有则节含绝对路径，
        无则空占位（无节、无残留标记）。"""
        md = gen_agens_md(Args(), "a", ["a", "b"], main_pi_cwd="/x",
                          prepare_file="/abs/discuss_prepare_s1.md")
        self.assertIn("## 讨论背景文件", md)
        self.assertIn("/abs/discuss_prepare_s1.md", md)
        # 无 → 无节且无占位符残留
        md2 = gen_agens_md(Args(), "a", ["a", "b"], main_pi_cwd="/x")
        self.assertNotIn("讨论背景文件", md2)
        self.assertNotIn("PREPARE_FILE_SECTION", md2)
        self.assertNotIn("{", md2)


class TestMisc(unittest.TestCase):
    """S1 run / S11 _resolve_path / S3 _default_model / S17 check_status。"""

    def test_run_success_and_failure(self):
        r = run(["echo", "hi"])
        self.assertEqual(r.returncode, 0)
        with self.assertRaises(RuntimeError):
            run(["false"])
        r2 = run(["false"], check=False)
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
            status, _ = check_status(os.path.join(tmp, "nope"))
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
            status, _ = check_status(base)
            self.assertEqual(status, "stopped")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
