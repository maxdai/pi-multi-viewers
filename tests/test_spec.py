# -*- coding: utf-8 -*-
"""spec 规格目录测试（设计 16：--spec-gen 骨架 + --spec 内容注入）。

覆盖：_spec_read（跳过首行）/ gen_spec_skeleton（骨架结构）/
gen_agents_md + gen_agent_def（spec 内容注入）/ _spec_models（model+variant
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
from unittest import mock

import meeting_fs
import spec_gen
from start_discussion import (
    _spec_read, _spec_models, _resolve_spec, gen_spec_skeleton,
    gen_agents_md, gen_agent_def, setup_environment,
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


def make_spec(base, question="# question.md——说明行\n\n# 分析主题：spec 测试",
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
        q = "# 分析主题：T\n\n## 初始立场（可选）\n- a: 立场\n- b: 立场\n\n## 待回答的问题（可选）\n- 问题\n"
        out = _strip_empty_sections(q)
        self.assertNotIn("初始立场", out)
        self.assertNotIn("待回答的问题", out)
        self.assertIn("# 分析主题：T", out)

    def test_filled_section_kept(self):
        q = "# 分析主题：T\n\n## 初始立场（可选）\n- a: 我选 Python\n\n## 待回答的问题（可选）\n- 并发场景下各自优劣势？\n"
        out = _strip_empty_sections(q)
        self.assertIn("## 初始立场（可选）", out)
        self.assertIn("我选 Python", out)
        self.assertIn("并发场景", out)

    def test_mixed_sections(self):
        """已填的保留、未填的去掉（同文件混合）。"""
        q = "# 分析主题：T\n\n## 初始立场（可选）\n- a: 立场\n\n## 待回答的问题（可选）\n- 两种语言并发优劣？\n"
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
            self.assertIn("# 分析主题：请填写", q)
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
            self.assertIn("--start", r)


class TestSpecInjection(unittest.TestCase):
    def test_agens_md_spec_background(self):
        args = Args(background="CLI 背景")
        md = gen_agents_md(args, "a", ["a", "b"], spec_background="spec 背景")
        self.assertIn("spec 背景", md)
        self.assertNotIn("CLI 背景", md)   # spec 优先

    def test_agens_md_fallback_cli(self):
        args = Args(background="CLI 背景")
        md = gen_agents_md(args, "a", ["a", "b"], spec_background=None)
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


class TestCurrentSessionFile(unittest.TestCase):
    """R6：session 文件查找的单点规则（sid 优先，兜底目录内最后）。"""

    def _fake_sessions(self, tmp, names):
        sdir = os.path.join(tmp, "sessions")
        os.makedirs(sdir, exist_ok=True)
        for n in names:
            open(os.path.join(sdir, n), "w").close()
        return sdir

    def test_sid_match_wins_over_last(self):
        """有 sid → 匹配该 sid 的文件（即使它不在字典序末尾）。"""
        import start_discussion as sd
        with tempfile.TemporaryDirectory() as tmp:
            sdir = self._fake_sessions(tmp, ["a_AAA.jsonl", "b_ZZZ.jsonl"])
            with mock.patch.dict(os.environ, {
                    "PI_SESSION_FILE": "", "PI_SESSION_ID": "AAA"}):
                with mock.patch.object(spec_gen, "pi_sessions_dir",
                                       return_value=sdir):
                    self.assertTrue(sd.current_session_file()
                                    .endswith("a_AAA.jsonl"))

    def test_fallback_last_when_no_sid(self):
        """无 sid → 目录内字典序最后（与旧兜底同规则）。"""
        import start_discussion as sd
        with tempfile.TemporaryDirectory() as tmp:
            sdir = self._fake_sessions(tmp, ["a_AAA.jsonl", "b_ZZZ.jsonl"])
            with mock.patch.dict(os.environ, {
                    "PI_SESSION_FILE": "", "PI_SESSION_ID": ""}):
                with mock.patch.object(spec_gen, "pi_sessions_dir",
                                       return_value=sdir):
                    self.assertTrue(sd.current_session_file()
                                    .endswith("b_ZZZ.jsonl"))

    def test_session_file_env_wins(self):
        """PI_SESSION_FILE（pi 直接给的路径）最精确，优先。"""
        import start_discussion as sd
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "given.jsonl")
            open(p, "w").close()
            with mock.patch.dict(os.environ, {"PI_SESSION_FILE": p}):
                self.assertEqual(sd.current_session_file(), p)

    def test_no_sessions_returns_empty(self):
        """目录不存在/无文件 → 空串（调用方自决是否接受）。"""
        import start_discussion as sd
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {
                    "PI_SESSION_FILE": "", "PI_SESSION_ID": ""}):
                with mock.patch.object(spec_gen, "pi_sessions_dir",
                                       return_value=os.path.join(tmp, "no")):
                    self.assertEqual(sd.current_session_file(), "")


class TestSpecModels(unittest.TestCase):
    def test_skeleton_prefills_pi_model(self):
        """PI_MODEL/PI_PROVIDER/PI_REASONING_LEVEL 注入 → models.md 预填
        （对齐旧 wrapper read_pi_model_thinking 语义，2026-09-09）。"""
        with tempfile.TemporaryDirectory() as tmp:
            env = {"PI_MODEL": "deepseek-v4-flash", "PI_PROVIDER": "deepseek",
                   "PI_REASONING_LEVEL": "max", "PI_SESSION_FILE": ""}
            with mock.patch.dict(os.environ, env):
                d = os.path.join(tmp, "spec")
                gen_spec_skeleton(d, ["a"])
                with open(os.path.join(d, "models.md")) as f:
                    t = f.read()
                self.assertIn("a: deepseek/deepseek-v4-flash, max", t)

    def test_skeleton_has_all_default(self):
        """无 env/session 探测 → 'agent: default' 兜底行（探测逻辑单独测）。"""
        with tempfile.TemporaryDirectory() as tmp:
            env = {"PI_MODEL": "", "PI_PROVIDER": "",
                   "PI_REASONING_LEVEL": "", "PI_SESSION_FILE": ""}
            with mock.patch.dict(os.environ, env):
                with mock.patch("spec_gen._default_model",
                                return_value=None):
                    # 探测入口直接归零（比 mock 文件系统更精确：被测的是
                    # "探测不到时兜底"，不是 session 查找本身——后者由
                    # TestCurrentSessionFile 覆盖）
                    with mock.patch("spec_gen.current_session_file",
                                    return_value=""):
                        d = os.path.join(tmp, "spec")
                        gen_spec_skeleton(d, ["a", "b", "c"])
                        with open(os.path.join(d, "models.md")) as f:
                            t = f.read()
                        self.assertIn("a: default", t)


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
                               )
            db = gen_agent_def("b", ["a", "b"])
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
            with mock.patch.object(spec_gen, "_default_model", return_value="test/default"):
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
            with mock.patch.object(spec_gen, "PI_AGENT_DIR", d):
                self.assertEqual(sd._default_model(), "opencode-go/deepseek-v4-flash")

    def test_default_model_missing_settings(self):
        # settings 不存在 / 损坏 → 安全失败 None
        import start_discussion as sd
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(spec_gen, "PI_AGENT_DIR", d):
                self.assertIsNone(sd._default_model())
            with open(os.path.join(d, "settings.json"), "w") as f:
                f.write("{bad json")
            with mock.patch.object(spec_gen, "PI_AGENT_DIR", d):
                self.assertIsNone(sd._default_model())

    # ---- _join_model_ref：契约拼接（不按值形状猜，2026-09-10） ----

    def test_join_model_ref_matrix(self):
        """三态矩阵：纯 id / 含斜杠 id（聚合 provider 命名空间）/ 已带前缀。

        含斜杠 id 是真实形态（commandcode-goat 的 model id 即
        'deepseek/deepseek-v4-flash'）——旧实现按 "是否含 '/'" 猜完整
        ref，把 provider 丢掉 → 静默解析到同名的另一个 provider。
        """
        import start_discussion as sd
        cases = [
            # (provider, model_id, 期望)
            ("opencode-go", "deepseek-v4-flash",
             "opencode-go/deepseek-v4-flash"),          # 纯 id → 补前缀
            ("commandcode-goat", "deepseek/deepseek-v4-flash",
             "commandcode-goat/deepseek/deepseek-v4-flash"),  # 含斜杠 id
            # id 恰好以 "<provider>/" 开头：仍按契约拼（不按形状猜——
            # 删掉幂等特判后此形态也是正确结果）
            ("commandcode-goat", "commandcode-goat/deepseek/x",
             "commandcode-goat/commandcode-goat/deepseek/x"),
            ("", "deepseek-v4-flash", "deepseek-v4-flash"),   # 无 provider
            ("p", "", ""),                                     # 无 id
        ]
        for provider, model_id, want in cases:
            with self.subTest(provider=provider, model_id=model_id):
                self.assertEqual(sd._join_model_ref(provider, model_id), want)

    def test_default_model_slash_id(self):
        # settings 的 defaultModel 本身含 '/'（命名空间 id）→ 仍按契约补 provider
        import start_discussion as sd
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "settings.json"), "w") as f:
                json.dump({"defaultProvider": "commandcode-goat",
                           "defaultModel": "deepseek/deepseek-v4-flash"}, f)
            with mock.patch.object(spec_gen, "PI_AGENT_DIR", d):
                self.assertEqual(
                    sd._default_model(),
                    "commandcode-goat/deepseek/deepseek-v4-flash")

    def test_detect_env_slash_model_id(self):
        # env 分支：PI_MODEL 是含 '/' 的 id → provider 不被丢弃
        import start_discussion as sd
        env = {"PI_MODEL": "deepseek/deepseek-v4-flash",
               "PI_PROVIDER": "commandcode-goat",
               "PI_REASONING_LEVEL": "max"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(
                sd._detect_pi_model_thinking(),
                ("commandcode-goat/deepseek/deepseek-v4-flash", "max"))

    def test_detect_session_file_joins_provider(self):
        # session 兜底分支：model_change 是 provider + modelId 两字段 → 拼接
        import start_discussion as sd
        with tempfile.TemporaryDirectory() as d:
            sf = os.path.join(d, "s.jsonl")
            with open(sf, "w") as f:
                f.write(json.dumps({"type": "model_change",
                                    "provider": "commandcode-goat",
                                    "modelId": "deepseek/deepseek-v4-flash"}) + "\n")
                f.write(json.dumps({"type": "thinking_level_change",
                                    "thinkingLevel": "low"}) + "\n")
            env = {k: v for k, v in os.environ.items()
                   if k not in ("PI_PROVIDER", "PI_MODEL", "PI_REASONING_LEVEL")}
            env["PI_SESSION_FILE"] = sf
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(
                    sd._detect_pi_model_thinking(),
                    ("commandcode-goat/deepseek/deepseek-v4-flash", "low"))


class TestResolveSpec(unittest.TestCase):
    """_resolve_spec：互斥校验 / spec 目录 / participants 推断 / question.md 必填（审核#5）。"""

    def test_no_spec(self):
        self.assertEqual(_resolve_spec(None, "a,b", None, None, None, None, None),
                         (None, None, None, None))

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
                _, _, _, err = _resolve_spec(d, agents, topic, bg, st, qs, md)
                self.assertIsNotNone(err, f"应报错: {expect}")
                self.assertIn(expect, err)

    def test_infer_participants(self):
        with tempfile.TemporaryDirectory() as d:
            gen_spec_skeleton(d, ["b", "a", "c"])
            spec_dir, parts, briefs, err = _resolve_spec(
                d, None, None, None, None, None, None)
            self.assertEqual(briefs, {})
            self.assertIsNone(err)
            self.assertEqual(spec_dir, os.path.abspath(d))
            # .order 固化顺序（审核#6）——保持 gen_spec_skeleton 传入顺序
            self.assertEqual(parts, ["b", "a", "c"])

    def test_missing_parts(self):
        # 空 agents/ → 报"没有 agent 定义文件"；缺 agents/ → "缺少 agents/"；
        # 有 agents/ 但缺 question.md → "缺少 question.md"
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "agents"))
            _, _, _, err = _resolve_spec(d, None, None, None, None, None, None)
            self.assertIn("没有 agent 定义文件", err)
        # 缺 agents/ 且无 viewers/ → 明确报错（fork-only 下不再有其它回退）
        with tempfile.TemporaryDirectory() as d:
            _, _, _, err = _resolve_spec(d, None, None, None, None, None, None,
                                         viewers_dir=os.path.join(d, "viewers"))
            self.assertIn("viewers/", err)
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "agents"))
            open(os.path.join(d, "agents/a.md"), "w").close()
            _, _, _, err = _resolve_spec(d, None, None, None, None, None, None)
            self.assertIn("缺少 question.md", err)

    def test_viewers_discovery(self):
        """viewers 模式：无 agents/ + cwd/viewers/*.md → 文件名即 agent 名。"""
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "viewers"))
            for name, brief in [("苏晚", "命运视角"), ("林然", "性格视角")]:
                open(os.path.join(d, "viewers", f"{name}.md"), "w").write(brief)
            open(os.path.join(d, "question.md"), "w").write("# t\n正文")  # 主题必填不变
            spec_dir, parts, briefs, err = _resolve_spec(
                d, None, None, None, None, None, None,
                viewers_dir=os.path.join(d, "viewers"))
            self.assertIsNone(err)
            # 排序 = 文件名排序（决定 starter/RR 轮转）
            self.assertEqual(parts, ["林然", "苏晚"])
            self.assertEqual(briefs["林然"], "性格视角")
            self.assertEqual(briefs["苏晚"], "命运视角")

    def test_hidden_agent_md_excluded(self):
        """R1：列举规则单点化——隐藏文件（.draft.md）不得成为参与者
        （名为 .draft 的点号名不在名字规则的禁止集内，会静默混入）。"""
        with tempfile.TemporaryDirectory() as d:
            ad = os.path.join(d, "agents")
            os.makedirs(ad)
            for n in ("a", "b", ".draft"):
                with open(os.path.join(ad, f"{n}.md"), "w") as f:
                    f.write(f"{n} 视角内容")
            with open(os.path.join(d, "question.md"), "w") as f:
                f.write("# 分析主题：T\n\n任务正文\n")
            sd, parts, briefs, err = _resolve_spec(
                d, None, None, None, None, None, None)
            self.assertIsNone(err)
            self.assertEqual(parts, ["a", "b"])

    def test_viewers_empty_brief_rejected(self):
        """空视角任务书 → 报错（无 lenses 的 agent 会让多视角退化成
        同名随机视角——静默退化）。覆盖 _resolve_spec 的 viewers 回退路径。"""
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "viewers"))
            with open(os.path.join(d, "viewers", "性能.md"), "w") as f:
                f.write("性能视角正文")
            with open(os.path.join(d, "viewers", "占位.md"), "w") as f:
                f.write("   \n\n")          # 纯空白
            with open(os.path.join(d, "question.md"), "w") as f:
                f.write("# 分析主题：T\n")
            sd, parts, briefs, err = _resolve_spec(
                d, None, None, None, None, None, None,
                viewers_dir=os.path.join(d, "viewers"))
            self.assertIsNone(sd)
            self.assertIn("视角任务书不能为空", err)
            self.assertIn("占位", err)

    def test_viewers_min_two(self):
        """meeting 至少两个 LLM agents（用户 2026-09-09）：viewers 仅 1 个
        .md → 专属错误（非通用"未找到"）。"""
        with tempfile.TemporaryDirectory() as tmp:
            spec = os.path.join(tmp, "spec")
            # 注意：不建 agents/（agents/ 存在 = 显式模式，不会走 viewers 分支）
            os.makedirs(spec)
            vd = os.path.join(tmp, "viewers")
            os.makedirs(vd)
            with open(os.path.join(vd, "唯一.md"), "w") as f:
                f.write("唯一视角")
            sd, parts, briefs, err = _resolve_spec(
                spec, None, None, None, None, None, None, viewers_dir=vd)
            self.assertIsNone(sd)
            self.assertIn("仅发现 1 个视角", err)
            self.assertIn("至少需要 2 个", err)

    def test_viewers_human_reserved(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "viewers"))
            open(os.path.join(d, "viewers/human.md"), "w").close()
            _, _, _, err = _resolve_spec(
                d, None, None, None, None, None, None,
                viewers_dir=os.path.join(d, "viewers"))
            self.assertIn("human", err)

    def test_spec_agents_override_viewers(self):
        """spec 显式 agents/ 优先于 viewers 发现。"""
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "agents"))
            open(os.path.join(d, "agents/z.md"), "w").close()
            open(os.path.join(d, "question.md"), "w").write("# t\n正文")
            os.makedirs(os.path.join(d, "viewers"))
            open(os.path.join(d, "viewers/林然.md"), "w").close()
            spec_dir, parts, briefs, err = _resolve_spec(
                d, None, None, None, None, None, None,
                viewers_dir=os.path.join(d, "viewers"))
            self.assertIsNone(err)
            self.assertEqual(parts, ["z"])
            self.assertEqual(briefs, {})

    def test_spec_dir_not_exists(self):
        with tempfile.TemporaryDirectory() as d:
            _, _, _, err = _resolve_spec(
                d + "/nope", None, None, None, None, None, None)
            self.assertIn("目录不存在", err)


class TestSpecSetup(unittest.TestCase):
    def test_topic_fixed_from_spec(self):
        """topic 固化（e2e7 评审 W）：spec 模式从 question.md 提取主题
        行固化进 protocol.json.topic（此前恒空串）。"""
        tmp = tempfile.mkdtemp()
        try:
            spec = make_spec(tmp + "/spec")
            base = tmp + "/env"
            args = Args(result_writer="c")
            setup_environment(args, ["a", "b", "c"], base, spec)
            proto = json.load(open(os.path.join(base, "work-a/protocol.json")))
            self.assertNotEqual(proto.get("topic", ""), "")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_topic_missing_fails_fast(self):
        """spec 无主题行 → fail-fast（ValueError，不静默空串）。"""
        tmp = tempfile.mkdtemp()
        try:
            spec = tmp + "/spec"
            os.makedirs(spec)
            with open(os.path.join(spec, "question.md"), "w") as f:
                f.write("# question.md——说明行\n\n无主题行内容\n")
            with open(os.path.join(spec, "background.md"), "w") as f:
                f.write("# bg——说明行\n\n\n")
            os.makedirs(os.path.join(spec, "agents"))
            for p in "ab":
                with open(os.path.join(spec, "agents", f"{p}.md"), "w") as f:
                    f.write("# 说明\n\n分工\n")
            base = tmp + "/env"
            args = Args()
            with self.assertRaises(ValueError):
                setup_environment(args, ["a", "b"], base, spec)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_full_spec_setup(self):
        tmp = tempfile.mkdtemp()
        try:
            spec = make_spec(tmp + "/spec")
            base = tmp + "/env"
            args = Args(result_writer="c")
            setup_environment(args, ["a", "b", "c"], base, spec)
            # question.md：跳过首行，spec 内容注入
            q = open(os.path.join(base, "work-a/question.md")).read()
            self.assertIn("# 分析主题：spec 测试", q)
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
            # .pi/settings.json 屏蔽已移除（fork-only 后无 pi 进程读 work
            # 内项目级 settings；用户 2026-09-09 判定）
            self.assertFalse(os.path.exists(
                os.path.join(base, "work-a/.pi/settings.json")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_spec_partial_fallback(self):
        # spec 缺 background.md/agents/b.md → 逐文件回退（不报错）
        tmp = tempfile.mkdtemp()
        try:
            os.makedirs(tmp + "/spec/agents", exist_ok=True)
            with open(tmp + "/spec/question.md", "w") as f:
                f.write("# question.md——说明行\n\n# 分析主题：只有问题")
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
            self.assertIn("# 分析主题：只有问题", q)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestModelsVariantSemantics(unittest.TestCase):
    """models.md 的 variant 槽语义（e2e17 评审 §7.2/§7.10）。

    此前 `default` 在 model 槽 = 继承本机，在 variant 槽 = **max**——相邻
    两行同词反义，按上一行直觉读下一行必错。别名已删：variant 槽写
    `default` 不再被重解释（原值透传 → 可见失败），缺省 = DEFAULT_THINKING。
    """

    def _spec(self, tmp, body):
        d = os.path.join(tmp, "s")
        os.makedirs(os.path.join(d, "agents"))
        for n in ("甲", "乙"):
            with open(os.path.join(d, "agents", n + ".md"), "w") as f:
                f.write("视角内容")
        with open(os.path.join(d, "question.md"), "w") as f:
            f.write("# 分析主题：T\n")
        with open(os.path.join(d, "models.md"), "w") as f:
            f.write("# 说明行\n" + body)
        return d

    def test_variant_default_not_alias(self):
        import start_discussion as sd
        with tempfile.TemporaryDirectory() as tmp:
            d = self._spec(tmp, "甲: m, default\n乙: m\n")
            r = sd._spec_models(d, ["甲", "乙"])
            self.assertEqual(r["甲"][1], "default",
                             "variant 槽的 default 不再是 max 的别名")
            self.assertEqual(r["乙"][1], meeting_fs.DEFAULT_THINKING,
                             "缺省档位引用同一常量")

    def test_variant_constant_single_source(self):
        """默认档位的**值**只有一个声明点（回归 = 代码里再现字面量）。

        源码文本检查（非行为检查）在这里是恰当的：要防的正是"又写了一遍
        字面量"——行为测试看不到重复声明。只查**非注释行**（注释里保留
        `(None, "max")` 作为历史说明是有意的）。
        """
        import start_discussion as sd
        bad = []
        for i, line in enumerate(open(sd.__file__, encoding="utf-8"), 1):
            if line.lstrip().startswith("#"):
                continue
            if '"max"' in line or "'max'" in line:
                bad.append(f"{i}: {line.rstrip()}")
        self.assertEqual(bad, [],
                         "档位字面量应只存在于 meeting_fs.DEFAULT_THINKING")
        src = open(sd.__file__, encoding="utf-8").read()
        # 四个引用点（解析缺省 / CLI --models / 归一循环 / json 兜底）+ 文档
        self.assertGreaterEqual(src.count("DEFAULT_THINKING"), 4,
                                "引用点：解析缺省 / CLI / 归一循环 / json 兜底")


class TestSkeletonModelsMd(unittest.TestCase):
    """骨架 models.md：**两个槽都显式写出**（含探测失败路径）。"""

    def test_skeleton_writes_explicit_variant(self):
        import spec_gen
        with tempfile.TemporaryDirectory() as tmp:
            vd = os.path.join(tmp, "viewers")
            os.makedirs(vd)
            for n in ("甲", "乙"):
                with open(os.path.join(vd, n + ".md"), "w") as f:
                    f.write("视角内容")
            spec = os.path.join(tmp, "spec")
            with mock.patch.object(spec_gen, "_detect_pi_model_thinking",
                                   return_value=("", "")):
                spec_gen.gen_spec_skeleton(spec, None, topic="T",
                                           viewers_dir=vd)
            with open(os.path.join(spec, "models.md")) as f:
                lines = [l for l in f.read().splitlines() if ":" in l
                         and not l.startswith("#")]
            self.assertTrue(lines)
            for l in lines:
                self.assertIn(",", l,
                              "variant 槽必须显式写出（留空会静默取档）")
                self.assertTrue(l.endswith(meeting_fs.DEFAULT_THINKING), l)

    def test_detection_failure_warns(self):
        """探测失败**可见**（stderr 一行），但不阻断（终端直用时本就没有
        PI_* 环境变量——报错会断掉合法路径）。"""
        import spec_gen, io
        from contextlib import redirect_stderr
        with tempfile.TemporaryDirectory() as tmp:
            vd = os.path.join(tmp, "viewers")
            os.makedirs(vd)
            for n in ("甲", "乙"):
                with open(os.path.join(vd, n + ".md"), "w") as f:
                    f.write("视角内容")
            spec = os.path.join(tmp, "spec")
            err = io.StringIO()
            with mock.patch.object(spec_gen, "_detect_pi_model_thinking",
                                   return_value=("", "")), \
                    redirect_stderr(err):
                spec_gen.gen_spec_skeleton(spec, None, topic="T",
                                           viewers_dir=vd)
            self.assertIn("未探测到主 pi 的 thinking 档位", err.getvalue())
            self.assertTrue(os.path.isdir(spec))     # 不阻断



if __name__ == "__main__":
    unittest.main()
