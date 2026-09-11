"""mv.sh wrapper 冒烟测试——W1-W7（API 清单基准，2026-09-01）。

wrapper 是 bash 脚本：用 subprocess 真实执行 + 断言输出/退出码。
--start 启动真实 loop 太重（e2e 覆盖），冒烟只测参数/错误路径与
--prepare 产物；--view/--say/--status/--cleanup 错误路径（目录缺失）。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRAPPER = os.path.join(HERE, "scripts", "mv.sh")


def run_wrapper(args, cwd=None, env=None):
    return subprocess.run([WRAPPER] + args, cwd=cwd or HERE,
                          capture_output=True, text=True, env=env)


class TestPrepare(unittest.TestCase):
    """W1。"""

    def test_prepare_generates_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run_wrapper(["--prepare", "测试主题", "--agents", "2"], cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            specs = [d for d in os.listdir(tmp)
                     if d.startswith("mv-spec-")]
            self.assertEqual(len(specs), 1)
            spec = os.path.join(tmp, specs[0])
            for f in ("question.md", "background.md", "models.md"):
                self.assertTrue(os.path.isfile(os.path.join(spec, f)))
            # agents 目录
            agents = sorted(os.listdir(os.path.join(spec, "agents")))
            self.assertEqual(agents, [".order", "a.md", "b.md"])
            with open(os.path.join(spec, "agents", ".order")) as f:
                self.assertEqual(f.read().split(), ["a", "b"])

    def test_prepare_agents_number_and_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run_wrapper(["--prepare", "T", "--agents", "4"], cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            spec = os.path.join(tmp, [d for d in os.listdir(tmp)
                                      if d.startswith("mv-spec-")][0])
            with open(os.path.join(spec, "agents", ".order")) as f:
                self.assertEqual(f.read().split(), ["a", "b", "c", "d"])
            shutil.rmtree(spec)
            r = run_wrapper(["--prepare", "T", "--agents", "x,y"], cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            spec = os.path.join(tmp, [d for d in os.listdir(tmp)
                                      if d.startswith("mv-spec-")][0])
            with open(os.path.join(spec, "agents", ".order")) as f:
                self.assertEqual(f.read().split(), ["x", "y"])

    def test_prepare_rejects_illegal_name_early(self):
        """R8：非法名在 prepare 就拦（此前 --agents "a/b" → FileNotFoundError
        traceback + spec 半成品落盘；"a b,c" → 骨架成功落盘直到 start 才拒）。"""
        with tempfile.TemporaryDirectory() as tmp:
            r = run_wrapper(["--prepare", "T", "--agents", "a/b"], cwd=tmp)
            self.assertNotEqual(r.returncode, 0)
            out = r.stdout + r.stderr
            self.assertIn("非法 agent 名", out)
            self.assertNotIn("Traceback", out)
            self.assertEqual([d for d in os.listdir(tmp)
                              if d.startswith("mv-spec-")], [])   # 零产物
        with tempfile.TemporaryDirectory() as tmp:
            r = run_wrapper(["--prepare", "T", "--agents", "a b,c"], cwd=tmp)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("非法 agent 名", r.stdout + r.stderr)
            self.assertEqual([d for d in os.listdir(tmp)
                              if d.startswith("mv-spec-")], [])

    def test_prepare_rejects_human(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run_wrapper(["--prepare", "T", "--agents", "a,human"], cwd=tmp)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("human", r.stderr + r.stdout)
            self.assertIn("保留名", r.stderr + r.stdout)

    def test_start_forwards_fork_mode(self):
        """P0（e2e10 评审）：--start 的余参必须转发给 python——此前 wrapper
        静默丢弃（README 教的操作实际无效）。用非法值验证转发（argparse
        早失败是 python 侧行为，wrapper 不解析值）。"""
        with tempfile.TemporaryDirectory() as tmp:
            spec = os.path.join(tmp, "mv-spec-x")
            os.makedirs(spec)
            with open(os.path.join(spec, "question.md"), "w") as f:
                f.write("# 分析主题：T\n")
            r = run_wrapper(["--start", spec, "--fork-mode", "bogus"], cwd=tmp)
            self.assertNotEqual(r.returncode, 0)
            out = r.stdout + r.stderr
            self.assertIn("--fork-mode", out)      # 参数确实到达 python
            self.assertIn("invalid choice", out)   # python 侧早失败

    def test_prepare_no_topic(self):
        r = run_wrapper(["--prepare"])
        self.assertNotEqual(r.returncode, 0)

    def test_prepare_background_written(self):
        """viewers 模式 prepare：viewers 合规才生成 spec，快照进 agents/。"""
        with tempfile.TemporaryDirectory() as tmp:
            vd = os.path.join(tmp, "viewers")
            os.makedirs(vd)
            for name, brief in ("性能", "性能视角正文"), ("可读性", "可读性视角正文"):
                with open(os.path.join(vd, f"{name}.md"), "w") as f:
                    f.write(brief)
            r = run_wrapper(["--prepare", "T", "--background", "背景内容"],
                            cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            spec = os.path.join(tmp, [d for d in os.listdir(tmp)
                                      if d.startswith("mv-spec-")][0])
            with open(os.path.join(spec, "background.md")) as f:
                self.assertIn("背景内容", f.read())
            # viewers 快照：spec agents/ 含视角文件 + .order
            agents_dir = os.path.join(spec, "agents")
            self.assertTrue(os.path.isdir(agents_dir))
            with open(os.path.join(agents_dir, "性能.md")) as f:
                self.assertIn("性能视角正文", f.read())
            with open(os.path.join(agents_dir, ".order")) as f:
                self.assertEqual([l.strip() for l in f if l.strip()],
                                 ["可读性", "性能"])

    def test_prepare_rejects_missing_viewers(self):
        """viewers/ 缺失 → prepare 失败（校验前移：不生成 spec）。"""
        with tempfile.TemporaryDirectory() as tmp:
            r = run_wrapper(["--prepare", "T"], cwd=tmp)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("viewers", r.stderr + r.stdout)
            self.assertEqual([d for d in os.listdir(tmp)
                              if d.startswith("mv-spec-")], [])

    def test_prepare_rejects_empty_viewer(self):
        """空视角任务书 → 报错不生成 spec（无 lenses 的 agent 会让多视角
        退化成同名随机视角——静默退化，与无静默铁律相悖）。"""
        with tempfile.TemporaryDirectory() as tmp:
            vd = os.path.join(tmp, "viewers")
            os.makedirs(vd)
            with open(os.path.join(vd, "性能.md"), "w") as f:
                f.write("性能视角正文")
            with open(os.path.join(vd, "占位.md"), "w") as f:
                f.write("")                      # 空文件
            with open(os.path.join(vd, "空白.md"), "w") as f:
                f.write("\n\n  \n")            # 纯空白
            r = run_wrapper(["--prepare", "T"], cwd=tmp)
            self.assertNotEqual(r.returncode, 0)
            out = r.stdout + r.stderr
            self.assertIn("视角任务书不能为空", out)
            self.assertIn("占位", out)
            self.assertIn("空白", out)
            # 零产物（校验在骨架生成之前）
            self.assertEqual([d for d in os.listdir(tmp)
                              if d.startswith("mv-spec-")], [])

    def test_prepare_rejects_single_viewer(self):
        """viewers/ 仅 1 个视角 → 失败（meeting 至少 2 agents）。"""
        with tempfile.TemporaryDirectory() as tmp:
            vd = os.path.join(tmp, "viewers")
            os.makedirs(vd)
            with open(os.path.join(vd, "唯一.md"), "w") as f:
                f.write("x")
            r = run_wrapper(["--prepare", "T"], cwd=tmp)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("至少需要 2 个", r.stderr + r.stdout)

    def test_prepare_rejects_human_viewer(self):
        """viewers/ 含 human.md → 失败（保留名）。"""
        with tempfile.TemporaryDirectory() as tmp:
            vd = os.path.join(tmp, "viewers")
            os.makedirs(vd)
            for name in ("性能", "human"):
                with open(os.path.join(vd, f"{name}.md"), "w") as f:
                    f.write("x")
            r = run_wrapper(["--prepare", "T"], cwd=tmp)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("human", r.stderr + r.stdout)


class TestErrorPaths(unittest.TestCase):
    """W2-W6 错误路径（目录缺失）。"""

    def test_start_requires_spec(self):
        r = run_wrapper(["--start"])
        self.assertNotEqual(r.returncode, 0)

    def test_start_missing_spec_dir(self):
        r = run_wrapper(["--start", "/nonexistent-spec"])
        self.assertNotEqual(r.returncode, 0)

    def test_view_requires_dir(self):
        r = run_wrapper(["--view"])
        self.assertNotEqual(r.returncode, 0)

    def test_say_requires_dir_and_text(self):
        r = run_wrapper(["--say"])
        self.assertNotEqual(r.returncode, 0)

    def test_status_missing_dir(self):
        r = run_wrapper(["--status", "/nonexistent-disc"])
        self.assertNotEqual(r.returncode, 0)

    def test_cleanup_missing_dir(self):
        r = run_wrapper(["--cleanup", "/nonexistent-disc"])
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
