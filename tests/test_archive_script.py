"""`scripts/archive-result.sh` 的行为测试（真 subprocess 跑脚本）。

为什么这层要有测试：脚本层是**没有行覆盖工具**的地方（F5 那类回归就出在
bash 里），而归档脚本做的是**不可逆**的事——删源副本、加索引行。因此用真仓库
装置测它：正文必须逐字（存档不改原文）、索引行必须落在最后一行存档之后、
源副本只在写成功后删、目标已存在时**拒绝覆盖**（rc 2）。
"""

import os
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "archive-result.sh")
BODY = "# 结果标题\n\n正文一行\n"


class TestArchiveScript(unittest.TestCase):
    """注意：脚本用**自身所在位置**推导仓库根，所以每个用例都把脚本**复制**到
    临时仓库里再跑（`--repo` 这类测试专用参数是被否决过的设计）。跑真本仓的
    那次实测把两份存档写进了本仓库——所以这里必须先复制。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "docs", "reviews"))
        os.makedirs(os.path.join(self.tmp, "scripts"))
        self.script = os.path.join(self.tmp, "scripts", "archive-result.sh")
        shutil.copy2(SCRIPT, self.script)     # 复制而非加测试专用参数
        self.index = os.path.join(self.tmp, "docs", "reviews", "README.md")
        with open(self.index, "w", encoding="utf-8") as f:
            f.write("| 文件 | 主题 |\n|---|---|\n| `2026-09-25-old.md` | a | b |\n")
        self.src = os.path.join(self.tmp, "mv-mv-main-1-result.md")
        with open(self.src, "w", encoding="utf-8") as f:
            f.write(BODY)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_script(self, *args):
        return subprocess.run(["bash", self.script, *args], capture_output=True, text=True)

    def dest(self, slug="test-slug", date="2026-09-26"):
        return os.path.join(self.tmp, "docs", "reviews", f"{date}-{slug}.md")

    def test_dry_run_writes_nothing(self):
        r = self.run_script(self.src, "--slug", "s1", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[dry-run]", r.stdout)
        self.assertFalse(os.path.exists(self.dest("s1")))
        self.assertTrue(os.path.exists(self.src))          # 源不动
        with open(self.index, encoding="utf-8") as f:
            self.assertEqual(len(f.read().splitlines()), 3)

    def test_archives_verbatim_and_indexes(self):
        r = self.run_script(self.src, "--slug", "s2", "--date", "2026-09-26")
        self.assertEqual(r.returncode, 0, r.stderr)
        dest = self.dest("s2")
        self.assertTrue(os.path.exists(dest))
        body = open(dest, encoding="utf-8").read()
        self.assertTrue(body.endswith(BODY), "正文必须逐字保留在文件尾部")
        self.assertIn("TODO", body)                        # 存档头骨架留给写的人
        self.assertFalse(os.path.exists(self.src))         # 写成功后才删源副本
        idx = open(self.index, encoding="utf-8").read()
        self.assertIn("`2026-09-26-s2.md`", idx)
        # 索引行紧跟最后一行存档（不是插在表头下）
        rows = [l for l in idx.splitlines() if l.startswith("| `2026-")]
        self.assertEqual(rows[0].startswith("| `2026-09-25-old.md`"), True)
        self.assertEqual(rows[-1].startswith("| `2026-09-26-s2.md`"), True)

    def test_refuses_existing_target(self):
        with open(self.dest("s3"), "w", encoding="utf-8") as f:
            f.write("已有\n")
        r = self.run_script(self.src, "--slug", "s3", "--date", "2026-09-26")
        self.assertEqual(r.returncode, 2)
        self.assertIn("不覆盖", r.stderr)
        with open(self.dest("s3"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "已有\n")
        self.assertTrue(os.path.exists(self.src))          # 拒绝时不删源

    def test_bad_args_are_loud(self):
        for args in ([], [self.src], [self.src, "--slug", "bad slug"],
                     ["/no/such/file", "--slug", "x"]):
            r = self.run_script(*args)
            self.assertEqual(r.returncode, 1, args)
            self.assertTrue(r.stderr.strip(), args)        # 有明确错误信息


if __name__ == "__main__":
    unittest.main()
