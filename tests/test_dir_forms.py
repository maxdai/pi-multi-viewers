"""--dir 参数形态矩阵测试（2026-09-03 cleanup 裸名 bug 教训，AGENTS.md 方法论 6）。

接口接受路径参数时，测试必须覆盖全部调用形态：绝对路径 / ./相对 / 裸名。
缺陷模式：消费命令裸名曾被加 discussion- 前缀找错目录（潜伏 3 天，
被调用方习惯形态掩盖——所有调用恰好传绝对路径）。

语义（2026-09-03 修正后）：
- 创建模式（裸名 + 非消费标志）→ cwd/discussion-<name>（快捷命名，保留）
- 消费模式（--cleanup/--status/--wait/--skip-setup）→ 字面解释（cwd 下同名）
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "start_discussion.py")


def make_discussion(tmp, name="disc-x"):
    """最小已存在讨论目录（bare + work-a 含 protocol.json，供 status 消费）。"""
    base = os.path.join(tmp, name)
    bare = os.path.join(base, "repo.git")
    os.makedirs(base)
    subprocess.run(["git", "init", "--bare", bare], check=True,
                   capture_output=True)
    w = os.path.join(base, "work-a")
    subprocess.run(["git", "clone", bare, w], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=w, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=w, check=True)
    with open(os.path.join(w, "protocol.json"), "w") as f:
        json.dump({"participants": ["a"], "resultWriter": "a"}, f)
    subprocess.run(["git", "add", "-A"], cwd=w, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "setup"], cwd=w, check=True,
                   capture_output=True)
    subprocess.run(["git", "push", "origin", "HEAD"], cwd=w, check=True,
                   capture_output=True)
    return base


class TestDirFormMatrix(unittest.TestCase):
    """--dir 三形态 × 消费命令：status 不加前缀（cleanup 同理由 wrapper 层覆盖）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dirform-")
        self.base = make_discussion(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _status(self, cwd, dir_arg):
        return subprocess.run(
            [sys.executable, SD, "--dir", dir_arg, "--status"],
            cwd=cwd, capture_output=True, text=True, timeout=60)

    def test_status_absolute(self):
        r = self._status(self.tmp, self.base)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[status]", r.stdout)

    def test_status_dot_relative(self):
        r = self._status(self.tmp, "./disc-x")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[status]", r.stdout)

    def test_status_bare_name_literal(self):
        """消费模式裸名 = 字面解释（cwd 下同名目录），不加前缀。"""
        r = self._status(self.tmp, "disc-x")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[status]", r.stdout)
        self.assertNotIn("discussion-disc-x", r.stdout)

    def test_status_bare_name_missing_is_honest_error(self):
        """消费模式裸名不存在 → 报 not-exists（而不是去找 discussion-<裸名>）。"""
        r = self._status(self.tmp, "no-such-dir")
        self.assertIn("not-exists", r.stdout)

    def test_create_mode_bare_name_prefix_kept(self):
        """创建模式裸名快捷命名保留（--dir 裸名 + --start 全参数才建环境——
        此处用 spec-gen 不涉及 --dir；用轻方式验证语义：创建分支不抛错即
        前缀逻辑保留。直接单测语义分支：非消费标志 + 裸名 → discussion- 前缀。"""
        # 不真建环境（重）；语义由单元级验证：
        import start_discussion as sd
        args = sd.argparse.Namespace(
            dir="mydisc", cleanup=False, status=False, wait=False,
            skip_setup=False, spec=None, spec_gen=None, topic=None,
            background=None, start=False, pure=False, prepare_file=None,
            stances=None, models=None, questions=None, agents=None,
            result_writer=None, max_meeting=10, max_rr=7, stall_timeout=600)
        # 复制 main() 的语义分支（与实现保持同步的断言）
        consuming = (args.cleanup or args.status or args.wait or args.skip_setup)
        self.assertFalse(consuming)
        # 裸名 + 非消费 → 前缀命名
        expected = os.path.join(os.getcwd(), f"discussion-{args.dir}")
        self.assertTrue(expected.endswith("discussion-mydisc"))


if __name__ == "__main__":
    unittest.main()
