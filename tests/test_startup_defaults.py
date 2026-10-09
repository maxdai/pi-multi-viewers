"""启动参数默认值链路（零 LLM）：默认值配置 → spec/startup.md → setup → protocol.json。

**这是用户 2026-09-27 点名要确认的事**：不能有任何一步"强制设置"配额、把用户
设的默认值静默覆盖掉。此前确实存在——`--start` 的 argparse默认值是常量
（15/7/600），而 `/multi-viewers` 这条路径不带 flag，于是永远写 15；本文件锁
住修复后的四层优先级：命令行显式 > spec/startup.md > 默认值配置 > 内置默认。

装置说明：`setup_environment` 与生产同一实现（只少了拉起 loop 那一步），
断言对象 = **bare HEAD 的 protocol.json**（运行期唯一权威）。
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import meeting_fs
import meeting_loop
import spec_gen
from start_discussion import setup_environment


class Args:
    """setup_environment 需要的最小 args（模仿 tests/test_spec.py 的装置）。"""
    def __init__(self, **kw):
        self.background = None; self.stances = None; self.questions = None
        self.models = None; self.result_writer = None
        self.max_meeting = None; self.max_rr = None; self.stall_timeout = None
        self.extension_policy = meeting_fs.DEFAULT_EXTENSION_POLICY
        self.topic = None; self.agents = None; self.fork_mode = None
        self.fork_source = None
        self.__dict__.update(kw)


class TestStartupDefaultsReachProtocol(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.agent_dir = os.path.join(self.tmp, "agent")
        os.makedirs(self.agent_dir)
        self._old = os.environ.get("PI_CODING_AGENT_DIR")
        os.environ["PI_CODING_AGENT_DIR"] = self.agent_dir

    def tearDown(self):
        if self._old is None:
            os.environ.pop("PI_CODING_AGENT_DIR", None)
        else:
            os.environ["PI_CODING_AGENT_DIR"] = self._old

    def _run(self, spec_dir=None, **kw):
        base = os.path.join(self.tmp, "mv-proj")
        setup_environment(Args(**kw), ["a", "b"], base, spec_dir)
        # 从 bare HEAD 读（运行期唯一权威；单一实现 meeting_fs.read_protocol）
        return meeting_fs.read_protocol(meeting_fs.bare_of_base(base))

    def test_builtin_defaults_without_any_config(self):
        """谁都没设 → 内置默认（15/7/600），且**没有任何强制覆盖**。"""
        spec = os.path.join(self.tmp, "spec"); os.makedirs(spec)
        spec_gen.gen_spec_skeleton(spec, ["a", "b"], topic="t")
        p = self._run(spec_dir=spec)
        self.assertEqual((p["maxMeetingRounds"], p["maxRRRounds"], p["stallTimeoutSeconds"]),
                         (15, 7, 600))

    def test_config_default_is_used(self):
        """设了默认值 → protocol.json 用它（用户要的语义）。"""
        meeting_fs.write_startup_config("max-meeting", 20)
        meeting_fs.write_startup_config("max-rr", 12)
        spec = os.path.join(self.tmp, "spec"); os.makedirs(spec)
        spec_gen.gen_spec_skeleton(spec, ["a", "b"], topic="t")
        p = self._run(spec_dir=spec)
        self.assertEqual((p["maxMeetingRounds"], p["maxRRRounds"]), (20, 12))
        self.assertEqual(p["stallTimeoutSeconds"], 600)

    def test_spec_beats_config(self):
        """spec/startup.md 是"本轮特别指定" → 覆盖默认值配置。"""
        meeting_fs.write_startup_config("max-meeting", 20)
        spec = os.path.join(self.tmp, "spec"); os.makedirs(spec)
        spec_gen.gen_spec_skeleton(spec, ["a", "b"], topic="t")
        with open(spec_gen.spec_startup_path(spec), "w") as f:
            f.write("max-meeting: 33\n")
        p = self._run(spec_dir=spec)
        self.assertEqual(p["maxMeetingRounds"], 33)

    def test_cli_flag_beats_everything(self):
        """命令行显式 flag 优先级最高（且只有元组里非 None 的键参与裁决）。"""
        meeting_fs.write_startup_config("max-meeting", 20)
        meeting_fs.write_startup_config("max-rr", 12)
        spec = os.path.join(self.tmp, "spec"); os.makedirs(spec)
        spec_gen.gen_spec_skeleton(spec, ["a", "b"], topic="t")
        p = self._run(spec_dir=spec, max_meeting=7)
        self.assertEqual(p["maxMeetingRounds"], 7)     # 命令行覆盖
        self.assertEqual(p["maxRRRounds"], 12)         # 未指定 → 配置默认

    def test_old_spec_without_startup_md(self):
        """旧 spec（没有 startup.md）→ 回落默认值配置，不报错。"""
        meeting_fs.write_startup_config("max-meeting", 20)
        spec = os.path.join(self.tmp, "spec"); os.makedirs(spec)
        spec_gen.gen_spec_skeleton(spec, ["a", "b"], topic="t")
        os.remove(spec_gen.spec_startup_path(spec))
        p = self._run(spec_dir=spec)
        self.assertEqual(p["maxMeetingRounds"], 20)


class TestForkModeAsStartupKey(unittest.TestCase):
    """`fork-mode` 作为启动参数键（2026-10-08）：让 `/multi-viewers`（不带 flag）
    也能用 summary —— extension 只跑 `--start <spec>`，flag 传不进去。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.agent_dir = os.path.join(self.tmp, "agent")
        os.makedirs(self.agent_dir)
        self._old = os.environ.get("PI_CODING_AGENT_DIR")
        os.environ["PI_CODING_AGENT_DIR"] = self.agent_dir
        self.spec = os.path.join(self.tmp, "spec")
        os.makedirs(self.spec)
        spec_gen.gen_spec_skeleton(self.spec, ["a", "b"], topic="t")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("PI_CODING_AGENT_DIR", None)
        else:
            os.environ["PI_CODING_AGENT_DIR"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, **kw):
        base = os.path.join(self.tmp, "mv-proj")
        fs_src = os.path.join(self.tmp, "main.jsonl")
        open(fs_src, "w").write("{}\n")
        kw.setdefault("fork_source", fs_src)
        # forkMode 只在有 forkSource 时写进 protocol；summary 分支会去生成摘要
        # ⇒ 必须挡掉那次 LLM（零 LLM 测试铁律）
        stats = {"summary_est": 1, "window_est": 1, "dropped_entries": 0,
                 "model": "m", "keep_tail": 20000, "front_tokens": 80000,
                 "input_entries": 1, "input_est": 1, "input_dropped": 0,
                 "source_est": 1, "wall_sec": 1.0}
        with mock.patch.object(meeting_loop, "generate_compact_base",
                               return_value=(stats, "")):
            setup_environment(Args(**kw), ["a", "b"], base, self.spec)
        return meeting_fs.read_protocol(meeting_fs.bare_of_base(base))

    def test_default_is_budget_and_in_spec(self):
        self.assertIn("fork-mode: budget",
                      open(spec_gen.spec_startup_path(self.spec),
                           encoding="utf-8").read())
        self.assertEqual(self._run()["forkMode"], "budget")

    def test_config_default_is_used(self):
        meeting_fs.write_startup_config("fork-mode", "summary")
        # spec/startup.md 里写着 budget（优先级高于配置）⇒ 先改 spec
        p = spec_gen.spec_startup_path(self.spec)
        open(p, "w", encoding="utf-8").write("fork-mode: summary\n")
        self.assertEqual(self._run()["forkMode"], "summary")

    def test_cli_flag_beats_spec(self):
        p = spec_gen.spec_startup_path(self.spec)
        open(p, "w", encoding="utf-8").write("fork-mode: summary\n")
        self.assertEqual(self._run(fork_mode="full")["forkMode"], "full")

    def test_enum_validation_is_single_implementation(self):
        self.assertEqual(meeting_fs.parse_startup_kv("fork-mode", "summary"),
                         ("summary", ""))
        val, err = meeting_fs.parse_startup_kv("fork-mode", "nope")
        self.assertIsNone(val)
        self.assertIn("budget/compaction/summary/full", err)
        # 坏值进配置 ⇒ 可见忽略（不阻断）
        with open(meeting_fs.startup_config_path(), "w", encoding="utf-8") as f:
            f.write('{"fork-mode": "bogus"}')
        vals, warn = meeting_fs.read_startup_config()
        self.assertEqual(vals, {})
        self.assertIn("fork-mode", warn)


if __name__ == "__main__":
    unittest.main()
