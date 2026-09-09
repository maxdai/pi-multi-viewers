# -*- coding: utf-8 -*-
"""spec 规格目录测试（设计 16：--spec-gen 骨架 + --spec 内容注入）。

覆盖：_spec_read（跳过首行）/ gen_spec_skeleton（骨架结构）/
gen_agens_md + gen_agent_def（spec 内容注入）/ _spec_models（model+variant
双列解析）/ _resolve_spec（互斥 + participants 推断）/ _default_model
（Pi settings 解析）/ setup_environment（完整创建 + 回退）。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from start_discussion import (
    _spec_read, _spec_models, _resolve_spec, gen_spec_skeleton,
    gen_agens_md, gen_agent_def, setup_environment,
    _default_model, _strip_empty_sections,
)


class Args:
    """setup_environment 的 args 桩。"""
    def __init__(self, topic="t", background=None, questions=None, stances=None,
                 models=None, result_writer=None, max_meeting=10, max_rr=7,
                 stall_timeout=600, pure=False):
        self.topic = topic
        self.background = background
        self.questions = questions
        self.stances = stances
        self.models = models
        self.result_writer = result_writer
        self.max_meeting = max_meeting
        self.max_rr = max_rr
        self.stall_timeout = stall_timeout
        self.pure = pure


def make_spec(base, question="# question.md——说明行\n\n# 讨论主题：spec 测试",
              background="# background.md——说明行\n\n共享背景内容",
              agents={"a": "# a.md——说明行\n\na 的分工"}):
    """构造 spec 目录（可覆盖各文件内容）。"""
    os.makedirs(os.path.join(base, "agents"), exist_ok=True)
    with open(os.path.join(base, "question.md"), "w") as f:
        f.write(question)
    with open(os.path.join(base, "background.md"), "w") as f:
        f.write(background)
    for name, content in agents.items():
        with open(os.path.join(base, "agents", f"{name}.md"), "w") as f:
            f.write(content)
    return base


class TestSpecRead(unittest.TestCase):
    def test_skip_first_line(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.md")
            with open(p, "w") as f:
                f.write("# 说明行\n\n正文第一行\n正文第二行\n")
            self.assertEqual(_spec_read(d, "x.md"), "正文第一行\n正文第二行")

    def test_only_header_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.md")
            with open(p, "w") as f:
                f.write("# 说明行\n\n")
            self.assertEqual(_spec_read(d, "x.md"), "")

    def test_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(_spec_read(d, "nope.md"))


class TestStripEmptySections(unittest.TestCase):
    """_strip_empty_sections：未填可选节（占位符）注入前去掉（打磨项 2026-09-01）。"""

    def test_placeholder_sections_removed(self):
        q = "# 讨论主题：T\n\n## 初始立场（可选）\n- a: 立场\n- b: 立场\n\n## 待回答的问题（可选）\n- 问题\n"
        out = _strip_empty_sections(q)
        self.assertNotIn("初始立场", out)
        self.assertNotIn("待回答的问题", out)
        self.assertIn("# 讨论主题：T", out)

    def test_filled_section_kept(self):
        q = "# 讨论主题：T\n\n## 初始立场（可选）\n- a: 我选 Python\n\n## 待回答的问题（可选）\n- 并发场景下各自优劣势？\n"
        out = _strip_empty_sections(q)
        self.assertIn("## 初始立场（可选）", out)
        self.assertIn("我选 Python", out)
        self.assertIn("并发场景", out)

    def test_mixed_sections(self):
        """已填的保留、未填的去掉（同文件混合）。"""
        q = "# 讨论主题：T\n\n## 初始立场（可选）\n- a: 立场\n\n## 待回答的问题（可选）\n- 两种语言并发优劣？\n"
        out = _strip_empty_sections(q)
        self.assertNotIn("初始立场", out)
        self.assertIn("## 待回答的问题（可选）", out)
        self.assertIn("两种语言并发优劣", out)


class TestSpecSkeleton(unittest.TestCase):
    def test_structure(self):
        with tempfile.TemporaryDirectory() as d:
            gen_spec_skeleton(d, ["a", "b", "c"])
            for rel in ["question.md", "background.md", "models.md", "README.md",
                        "agents/a.md", "agents/b.md", "agents/c.md"]:
                self.assertTrue(os.path.isfile(os.path.join(d, rel)),
                                f"缺 {rel}")
            # question.md 第一行是说明 + 基本结构（# 讨论主题 / ## 初始立场）
            q = open(os.path.join(d, "question.md")).read()
            self.assertTrue(q.startswith("# question.md"))
            self.assertIn("# 讨论主题：请填写", q)
            self.assertIn("## 初始立场", q)
            for p in ["a", "b", "c"]:
                self.assertIn(f"- {p}: 立场", q)
            # background/agents 只有说明行 + 空正文
            b = open(os.path.join(d, "background.md")).read()
            self.assertTrue(b.startswith("# background.md"))
            a = open(os.path.join(d, "agents/a.md")).read()
            self.assertTrue(a.startswith("# a.md"))
            # README 自文档化（说明用途 + 继续运行）
            r = open(os.path.join(d, "README.md")).read()
            self.assertIn("models.md", r)
            self.assertIn("下一步", r)
            self.assertIn("--spec", r)


class TestSpecInjection(unittest.TestCase):
    def test_agens_md_spec_background(self):
        args = Args(background="CLI 背景")
        md = gen_agens_md(args, "a", ["a", "b"], spec_background="spec 背景")
        self.assertIn("spec 背景", md)
        self.assertNotIn("CLI 背景", md)   # spec 优先

    def test_agens_md_fallback_cli(self):
        args = Args(background="CLI 背景")
        md = gen_agens_md(args, "a", ["a", "b"], spec_background=None)
        self.assertIn("CLI 背景", md)

    def test_agent_def_extra(self):
        d = gen_agent_def("a", ["a", "b"], extra="a 的专属分工")
        self.assertIn("a 的专属分工", d)

    def test_agent_def_empty_extra_no_append(self):
        d1 = gen_agent_def("a", ["a", "b"], extra="")
        d2 = gen_agent_def("a", ["a", "b"], extra=None)
        self.assertEqual(d1, d2)   # 空 extra 不追加

    def test_agent_def_no_extra(self):
        d1 = gen_agent_def("a", ["a", "b"])
        d2 = gen_agent_def("a", ["a", "b"], extra=None)
        self.assertEqual(d1, d2)


class TestSpecModels(unittest.TestCase):
    def test_skeleton_has_all_default(self):
        with tempfile.TemporaryDirectory() as d:
            gen_spec_skeleton(d, ["a", "b", "c"])
            md = open(os.path.join(d, "models.md")).read()
            self.assertTrue(md.startswith("# models.md"))
            for p in ["a", "b", "c"]:
                self.assertIn(f"{p}: default", md)   # variant 隐式 max（9271）

    def test_parse_valid_and_default(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "models.md"), "w") as f:
                # 9271：单列 → variant 隐式 max；双列显式 variant 照用
                f.write("# 说明\n\na: opencode-go/gpt-5.6-luna\nb: default, high\n")
            m = _spec_models(d, ["a", "b"])
            self.assertEqual(m, {"a": ("opencode-go/gpt-5.6-luna", "max"),
                                 "b": (None, "high")})

    def test_parse_tolerant(self):
        # 空行/坏行/不在参与者/空值 → 跳过（保留默认）；带空格 → strip；
        # variant 缺省 → max
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "models.md"), "w") as f:
                f.write("# 说明\n\n\nbad line\n"
                        "c: opencode-go/x\n"           # 不在 participants
                        "a:   opencode-go/gpt-5.6-luna  \n"  # 多余空格，无 variant
                        "b: \n"                         # 空值
                        )
            m = _spec_models(d, ["a", "b"])
            self.assertEqual(m, {"a": ("opencode-go/gpt-5.6-luna", "max"),
                                 "b": (None, "max")})

    def test_models_inject_to_agent_def(self):
        # spec models（model + variant 元组）→ gen_agent_def model 正文
        # （pi-agent.json 负责 model/thinking 运行时参数，agent def 只提示模型名）
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "models.md"), "w") as f:
                f.write("# 说明\n\na: opencode-go/gpt-5.6-luna, high\nb: default, max\n")
            models = _spec_models(d, ["a", "b"])
            da = gen_agent_def("a", ["a", "b"],
                               models={"a": models["a"][0]},
                               variant=models["a"][1])
            db = gen_agent_def("b", ["a", "b"], variant=models["b"][1])
            self.assertIn("你使用模型 opencode-go/gpt-5.6-luna 参与讨论", da)
            self.assertNotIn("你使用模型", db)   # default → 无 model 正文

    def test_setup_models_via_spec(self):
        # 完整创建：models.md → agent 定义 model + variant 注入。
        # default 填默认模型（此处 mock，保证测试不依赖机器 settings）。
        tmp = tempfile.mkdtemp()
        try:
            spec = make_spec(tmp + "/spec")
            with open(tmp + "/spec/models.md", "w") as f:
                f.write("# 说明\n\na: opencode-go/gpt-5.6-luna, high\nb: default, max\n")
            base = tmp + "/env"
            from unittest import mock
            import start_discussion as sd
            with mock.patch.object(sd, "_default_model", return_value="test/default"):
                setup_environment(Args(), ["a", "b"], base, spec)
            adef = open(os.path.join(base, "work-a/.pi/agent/a.md")).read()
            self.assertIn("你使用模型 opencode-go/gpt-5.6-luna 参与讨论", adef)
            acfg = json.load(open(os.path.join(base, "work-a/pi-agent.json")))
            self.assertEqual(acfg["model"], "opencode-go/gpt-5.6-luna")
            self.assertEqual(acfg["thinking"], "high")
            bdef = open(os.path.join(base, "work-b/.pi/agent/b.md")).read()
            bcfg = json.load(open(os.path.join(base, "work-b/pi-agent.json")))
            # b=default → 填本机默认模型（Pi settings.json 读取）
            self.assertIn("你使用模型", bdef)
            self.assertTrue(bcfg["model"])
            self.assertEqual(bcfg["thinking"], "max")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_default_model_parse(self):
        # _default_model：从 Pi settings.json 读取 defaultProvider/defaultModel。
        import start_discussion as sd
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "settings.json"), "w") as f:
                json.dump({"defaultProvider": "opencode-go",
                           "defaultModel": "deepseek-v4-flash"}, f)
            with mock.patch.object(sd, "PI_AGENT_DIR", d):
                self.assertEqual(sd._default_model(), "opencode-go/deepseek-v4-flash")

    def test_default_model_missing_settings(self):
        # settings 不存在 / 损坏 → 安全失败 None
        import start_discussion as sd
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(sd, "PI_AGENT_DIR", d):
                self.assertIsNone(sd._default_model())
            with open(os.path.join(d, "settings.json"), "w") as f:
                f.write("{bad json")
            with mock.patch.object(sd, "PI_AGENT_DIR", d):
                self.assertIsNone(sd._default_model())


class TestResolveSpec(unittest.TestCase):
    """_resolve_spec：互斥校验 / spec 目录 / participants 推断 / question.md 必填（审核#5）。"""

    def test_no_spec(self):
        self.assertEqual(_resolve_spec(None, "a,b", None, None, None, None, None),
                         (None, None, None))

    def test_mutex_each_param(self):
        # 互斥：传任一内容/参与者参数都报错
        with tempfile.TemporaryDirectory() as d:
            gen_spec_skeleton(d, ["a", "b"])
            cases = [
                ("c", None, None, None, None, None, "--agents"),
                (None, "t", None, None, None, None, "--topic"),
                (None, None, "b", None, None, None, "--background"),
                (None, None, None, {"a": "x"}, None, None, "--stances"),
                (None, None, None, None, ["q"], None, "--questions"),
                (None, None, None, None, None, {"a": "m"}, "--models"),
            ]
            for agents, topic, bg, st, qs, md, expect in cases:
                _, _, err = _resolve_spec(d, agents, topic, bg, st, qs, md)
                self.assertIsNotNone(err, f"应报错: {expect}")
                self.assertIn(expect, err)

    def test_infer_participants(self):
        with tempfile.TemporaryDirectory() as d:
            gen_spec_skeleton(d, ["b", "a", "c"])
            spec_dir, parts, err = _resolve_spec(
                d, None, None, None, None, None, None)
            self.assertIsNone(err)
            self.assertEqual(spec_dir, os.path.abspath(d))
            # .order 固化顺序（审核#6）——保持 gen_spec_skeleton 传入顺序
            self.assertEqual(parts, ["b", "a", "c"])

    def test_missing_parts(self):
        # 空 agents/ → 报"没有 agent 定义文件"；缺 agents/ → "缺少 agents/"；
        # 有 agents/ 但缺 question.md → "缺少 question.md"
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "agents"))
            _, _, err = _resolve_spec(d, None, None, None, None, None, None)
            self.assertIn("没有 agent 定义文件", err)
        with tempfile.TemporaryDirectory() as d:
            _, _, err = _resolve_spec(d, None, None, None, None, None, None)
            self.assertIn("缺少 agents/", err)
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "agents"))
            open(os.path.join(d, "agents/a.md"), "w").close()
            _, _, err = _resolve_spec(d, None, None, None, None, None, None)
            self.assertIn("缺少 question.md", err)

    def test_spec_dir_not_exists(self):
        with tempfile.TemporaryDirectory() as d:
            _, _, err = _resolve_spec(
                d + "/nope", None, None, None, None, None, None)
            self.assertIn("目录不存在", err)


class TestSpecSetup(unittest.TestCase):
    def test_full_spec_setup(self):
        tmp = tempfile.mkdtemp()
        try:
            spec = make_spec(tmp + "/spec")
            base = tmp + "/env"
            args = Args(result_writer="c")
            setup_environment(args, ["a", "b", "c"], base, spec)
            # question.md：跳过首行，spec 内容注入
            q = open(os.path.join(base, "work-a/question.md")).read()
            self.assertIn("# 讨论主题：spec 测试", q)
            self.assertNotIn("# question.md——说明行", q)
            # AGENTS.md 背景：spec 内容注入
            md = open(os.path.join(base, "work-b/AGENTS.md")).read()
            self.assertIn("共享背景内容", md)
            # agent 定义：extra 追加
            adef = open(os.path.join(base, "work-a/.pi/agent/a.md")).read()
            self.assertIn("a 的分工", adef)
            # b 无 extra 文件 → 无追加
            bdef = open(os.path.join(base, "work-b/.pi/agent/b.md")).read()
            self.assertNotIn("b 的分工", bdef)
            # protocol.json participants + rw
            proto = json.load(open(os.path.join(base, "work-a/protocol.json")))
            self.assertEqual(proto["participants"], ["a", "b", "c"])
            self.assertEqual(proto["resultWriter"], "c")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_spec_partial_fallback(self):
        # spec 缺 background.md/agents/b.md → 逐文件回退（不报错）
        tmp = tempfile.mkdtemp()
        try:
            os.makedirs(tmp + "/spec/agents", exist_ok=True)
            with open(tmp + "/spec/question.md", "w") as f:
                f.write("# question.md——说明行\n\n# 讨论主题：只有问题")
            with open(tmp + "/spec/agents/a.md", "w") as f:
                f.write("# a.md——说明行\n\na 的分工")
            base = tmp + "/env"
            args = Args()
            setup_environment(args, ["a", "b"], base, tmp + "/spec")
            # background 缺失 → 占位（无）；b 无 extra → 不追加
            md = open(os.path.join(base, "work-a/AGENTS.md")).read()
            self.assertIn("（无）", md)
            bdef = open(os.path.join(base, "work-b/.pi/agent/b.md")).read()
            self.assertNotIn("b 的分工", bdef)
            # question.md 正常注入
            q = open(os.path.join(base, "work-a/question.md")).read()
            self.assertIn("# 讨论主题：只有问题", q)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
