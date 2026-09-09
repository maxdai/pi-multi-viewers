"""cmd_cleanup 清理 discuss_prepare_<sid>.md（2026-09-02 两 prompt 拆分第一步）。

不变式：cleanup 后无 discuss_prepare 文件（prepare prompt 的产物生命周期
由 cleanup 闭合）。用真实 subprocess 跑 wrapper（生产调用链）。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

WRAPPER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "scripts", "discuss.sh")


class TestCleanupRemovesPrepareFile(unittest.TestCase):
    """cleanup 删除 discuss_prepare_<PI_SESSION_ID>.md；幂等。"""

    def _make_discussion(self, tmp):
        """最小讨论目录（check_status 可跑通 cleanup 全链）。"""
        base = os.path.join(tmp, "disc-x")
        bare = os.path.join(base, "repo.git")
        os.makedirs(os.path.join(base, "work-b"))
        subprocess.run(["git", "init", "--bare", bare], check=True,
                       capture_output=True)
        w = os.path.join(base, "work-a")
        subprocess.run(["git", "clone", bare, w], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=w, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=w, check=True)
        with open(os.path.join(w, "protocol.json"), "w") as f:
            json.dump({"participants": ["a", "b"], "resultWriter": "b"}, f)
        subprocess.run(["git", "add", "-A"], cwd=w, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "setup"], cwd=w, check=True,
                       capture_output=True)
        subprocess.run(["git", "push", "origin", "HEAD"], cwd=w, check=True,
                       capture_output=True)
        return base

    def _cleanup(self, cwd, base):
        return subprocess.run(
            [WRAPPER, "--cleanup", base], cwd=cwd, capture_output=True,
            text=True, timeout=60,
            env={**os.environ, "PI_SESSION_ID": self.sid})

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="prep-clean-")
        self.sid = "test-sid-1234"
        self.base = self._make_discussion(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_prepare_file_removed(self):
        """cleanup 删除同 session 的 discuss_prepare 文件。"""
        pf = os.path.join(self.tmp, f"discuss_prepare_{self.sid}.md")
        with open(pf, "w") as f:
            f.write("# 讨论主题\nT\n\n# 背景\nB\n")
        r = self._cleanup(self.tmp, self.base)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(pf))
        self.assertIn(f"已删除 discuss_prepare_{self.sid}.md", r.stdout)

    def test_cleanup_bare_name_normalized(self):
        """裸目录名（无路径符）cleanup：readlink -f 规范化后正常删除
        （实测 2026-09-03：裸名被 start_discussion 加 discussion- 前缀，
        目录残留）。"""
        pf = os.path.join(self.tmp, f"discuss_prepare_{self.sid}.md")
        with open(pf, "w") as f:
            f.write("x")
        base_name = os.path.basename(self.base)
        r = subprocess.run(
            [WRAPPER, "--cleanup", base_name], cwd=self.tmp,
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "PI_SESSION_ID": self.sid})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("已删除目录", r.stdout)
        self.assertFalse(os.path.isdir(self.base))
        self.assertFalse(os.path.exists(pf))

    def test_no_prepare_file_idempotent(self):
        """无 prepare 文件：cleanup 正常完成，不报错。"""
        r = self._cleanup(self.tmp, self.base)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("已删除 discuss_prepare", r.stdout)

    def test_other_session_file_untouched(self):
        """只删当前 session 的文件，其它 session 的不动。"""
        other = os.path.join(self.tmp, "discuss_prepare_other-sid.md")
        with open(other, "w") as f:
            f.write("x")
        self._cleanup(self.tmp, self.base)
        self.assertTrue(os.path.exists(other))


if __name__ == "__main__":
    unittest.main()
