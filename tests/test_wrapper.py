"""mv.sh wrapper 冒烟测试——W1-W7（API 清单基准，2026-09-01）。

wrapper 是 bash 脚本：用 subprocess 真实执行 + 断言输出/退出码。
--start 启动真实 loop 太重（e2e 覆盖），冒烟只测参数/错误路径与
--prepare 产物；--view/--say/--status/--cleanup 错误路径（目录缺失）。
check_aft_bash 用 HOME 隔离（不碰真实配置）。
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

    def test_prepare_rejects_human(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run_wrapper(["--prepare", "T", "--agents", "a,human"], cwd=tmp)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("human", r.stderr + r.stdout)
            self.assertIn("保留名", r.stderr + r.stdout)

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


class TestCheckAftBash(unittest.TestCase):
    """W7：HOME 隔离测试三态（存在 false / 缺失 / 存在 true）。"""

    def _run(self, config_text=None, use_new_path=True):
        with tempfile.TemporaryDirectory() as home:
            cfg_dir = os.path.join(home, ".config", "cortexkit")
            os.makedirs(cfg_dir)
            if config_text is not None:
                name = "aft.jsonc" if use_new_path else "aft.json"
                with open(os.path.join(cfg_dir, name), "w") as f:
                    f.write(config_text)
            env = dict(os.environ, HOME=home)
            with tempfile.TemporaryDirectory() as tmp:
                return run_wrapper(["--prepare", "T", "--agents", "2"],
                                   cwd=tmp, env=env)

    def test_bash_false_no_warning(self):
        r = self._run('{"bash": false}')
        self.assertNotIn("[aft] 警告", r.stderr)

    def test_missing_config_warns(self):
        r = self._run(None)
        self.assertIn("[aft] 警告", r.stderr)
        # 不阻断（prepare 仍成功）
        self.assertEqual(r.returncode, 0)

    def test_bash_true_warns(self):
        r = self._run('{"bash": true}')
        self.assertIn("[aft] 警告", r.stderr)

    def test_legacy_aft_json_path(self):
        r = self._run('{"bash": false}', use_new_path=False)
        self.assertNotIn("[aft] 警告", r.stderr)


if __name__ == "__main__":
    unittest.main()
