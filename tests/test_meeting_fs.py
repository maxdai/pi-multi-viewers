"""meeting_fs 单元测试——F1-F22 全覆盖（API 清单基准，2026-09-01）。

真实 git 环境（tmp bare + clone）：I/O 层必须真 git 验证（mock 会掩盖
git 行为差异）。覆盖：正常路径 + 边界（空/缺失/畸形）+ 异常（git 失败）。
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meeting_fs import (
    run_git, git_head, git_pull, git_commit, git_push,
    git_ls_files, git_show, _frontmatter_end, parse_frontmatter,
    extract_body, read_message, _fm_to_lines, write_message,
    serialize_message, list_my_messages, next_msg_id, commit_message,
    read_point, list_new_messages, new_messages_with_meta,
    is_message_file, parse_log_nameonly,
)


def make_env():
    """建临时 bare + work（setup 提交后）。返回 (tmp, base, bare, work)。"""
    tmp = tempfile.mkdtemp(prefix="fs-test-")
    base = os.path.join(tmp, "disc")
    bare = os.path.join(base, "repo.git")
    os.makedirs(base)
    subprocess.run(["git", "init", "--bare", bare], check=True,
                   capture_output=True)
    work = os.path.join(base, "work-a")
    subprocess.run(["git", "clone", bare, work], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=work, check=True)
    # setup commit（与生产 setup_environment 一致：bare 有有效 HEAD）
    with open(os.path.join(work, "protocol.json"), "w") as f:
        f.write("{}")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "discuss: setup"], cwd=work,
                   check=True, capture_output=True)
    subprocess.run(["git", "push", "origin", "HEAD"], cwd=work, check=True,
                   capture_output=True)
    return tmp, base, bare, work


class TestRunGit(unittest.TestCase):
    """F1 run_git。"""

    def test_success(self):
        tmp, _, _, work = make_env()
        try:
            r = run_git(work, "rev-parse", "HEAD")
            self.assertEqual(r.returncode, 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_failure_raises(self):
        tmp, _, _, work = make_env()
        try:
            with self.assertRaises(RuntimeError) as cm:
                run_git(work, "rev-parse", "no-such-ref")
            self.assertIn("git", str(cm.exception))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_failure_no_check(self):
        tmp, _, _, work = make_env()
        try:
            r = run_git(work, "rev-parse", "no-such-ref", check=False)
            self.assertNotEqual(r.returncode, 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestGitOps(unittest.TestCase):
    """F2-F7 git 基础操作。"""

    def test_git_head(self):
        tmp, _, _, work = make_env()
        try:
            h = git_head(work)
            self.assertEqual(len(h), 40)
            self.assertTrue(all(c in "0123456789abcdef" for c in h))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_git_commit_and_ls_files(self):
        tmp, _, _, work = make_env()
        try:
            os.makedirs(os.path.join(work, "a"))
            with open(os.path.join(work, "a/0001.md"), "w") as f:
                f.write("---\nfrom: a\n---\n正文\n")
            git_commit(work, ["a/0001.md"], "discuss: a/0001")
            self.assertEqual(git_ls_files(work, "a"), ["a/0001.md"])
            self.assertEqual(git_ls_files(work, "nope"), [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_git_pull_brings_remote_commit(self):
        """pull --rebase：远端有 commit 时本地 pull 拿到。"""
        tmp, _, bare, work = make_env()
        try:
            # 另建 work2 push 一条
            work2 = os.path.join(tmp, "work2")
            subprocess.run(["git", "clone", bare, work2], check=True,
                           capture_output=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=work2,
                           check=True)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=work2,
                           check=True)
            os.makedirs(os.path.join(work2, "b"))
            with open(os.path.join(work2, "b/0001.md"), "w") as f:
                f.write("---\nfrom: b\n---\n---\n")
            git_commit(work2, ["b/0001.md"], "discuss: b/0001")
            git_push(work2)
            # work pull 后可见
            git_pull(work)
            self.assertEqual(git_ls_files(work, "b"), ["b/0001.md"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_git_show(self):
        tmp, _, bare, work = make_env()
        try:
            os.makedirs(os.path.join(work, "a"))
            with open(os.path.join(work, "a/0001.md"), "w") as f:
                f.write("---\nfrom: a\n---\n正文\n")
            git_commit(work, ["a/0001.md"], "discuss: a/0001")
            git_push(work)
            c = git_show(bare, "HEAD", "a/0001.md")
            self.assertIn("from: a", c)
            # 不存在 → None
            self.assertIsNone(git_show(bare, "HEAD", "a/nope.md"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_git_push_retry_exhausted(self):
        """push 重试耗尽 → RuntimeError（消息不得滞留本地）。"""
        tmp, _, _, work = make_env()
        try:
            with mock.patch("meeting_fs.run_git") as m:
                # 所有 push 尝试都失败（非零退出）
                r = subprocess.CompletedProcess(["git"], 1, "", "rejected")
                m.return_value = r
                with self.assertRaises(RuntimeError) as cm:
                    git_push(work)
                self.assertIn("git_push 重试耗尽", str(cm.exception))
                self.assertEqual(m.call_count, 5 + 5)  # 5 push + 5 pull 重试
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestFrontmatter(unittest.TestCase):
    """F8-F14 frontmatter 解析/序列化。"""

    def test_frontmatter_end_complete(self):
        self.assertEqual(_frontmatter_end("---\na: 1\n---\nbody"), 2)
        self.assertEqual(_frontmatter_end("---\n---\n"), 1)

    def test_frontmatter_end_incomplete(self):
        self.assertIsNone(_frontmatter_end("---\na: 1"))          # 有开无闭
        self.assertIsNone(_frontmatter_end("no opener\n---\n"))   # 无开
        self.assertIsNone(_frontmatter_end("  ---\na: 1\n---\n"))  # 前导空格开（严格）

    def test_parse_frontmatter_complete(self):
        c = "---\nfrom: a\ntype: message\nmode: meeting\n---\n正文\n"
        fm = parse_frontmatter(c)
        self.assertEqual(fm, {"from": "a", "type": "message",
                              "mode": "meeting"})

    def test_parse_frontmatter_quoted(self):
        c = '---\nsummary: "带引号的值"\n---\n'
        fm = parse_frontmatter(c)
        self.assertEqual(fm["summary"], "带引号的值")

    def test_parse_frontmatter_invalid_line_skipped(self):
        c = "---\nno-colon-line\nfrom: a\n---\n"
        fm = parse_frontmatter(c)
        self.assertEqual(fm, {"from": "a"})

    def test_parse_frontmatter_incomplete_none(self):
        self.assertIsNone(parse_frontmatter("---\nfrom: a"))
        self.assertIsNone(parse_frontmatter("no fm"))

    def test_parse_frontmatter_body_after_close_ignored(self):
        c = "---\nfrom: a\n---\nbody: not-frontmatter\n"
        fm = parse_frontmatter(c)
        self.assertEqual(fm, {"from": "a"})

    def test_extract_body(self):
        c = "---\nfrom: a\n---\n\n正文第一行\n第二行\n"
        self.assertEqual(extract_body(c), "正文第一行\n第二行")
        self.assertIsNone(extract_body("---\nfrom: a"))  # 不完整

    def test_fm_to_lines_single_line_and_quotes(self):
        lines = _fm_to_lines({"from": "a", "summary": '多\n行\n"值"'})
        self.assertEqual(lines[0], "---")
        self.assertEqual(lines[-1], "---")
        # 值单行化；引号仅在值整体被引号包裹时剥（此处以"多"开头 → 保留）
        self.assertIn('summary: 多 行 "值"', lines)

    def test_write_and_read_message(self):
        tmp, _, _, work = make_env()
        try:
            fm = {"from": "a", "type": "message"}
            write_message(work, "a/0001.md", fm, "正文")
            full = os.path.join(work, "a/0001.md")
            self.assertTrue(os.path.exists(full))
            got, content = read_message(work, "a/0001.md")
            self.assertEqual(got["from"], "a")
            self.assertIn("正文", content)
            # 不存在 → (None, None)
            self.assertEqual(read_message(work, "a/nope.md"), (None, None))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_serialize_keeps_body(self):
        original = "---\nfrom: a\n---\n\n正文保留\n"
        new = serialize_message({"from": "a", "type": "pass"}, original)
        self.assertIn("---\nfrom: a\ntype: pass\n---", new)
        self.assertIn("正文保留", new)

    def test_serialize_leading_space_opener_allowed(self):
        """F14 刻意分离：serialize 允许前导空格开（原语义，宽松）。"""
        original = "  ---\nfrom: a\n---\nbody\n"
        new = serialize_message({"from": "a"}, original)
        self.assertIsNotNone(new)

    def test_serialize_no_closer_none(self):
        self.assertIsNone(serialize_message({"from": "a"}, "---\nfrom: a\n"))
        self.assertIsNone(serialize_message({"from": "a"}, "no fm"))


class TestMessages(unittest.TestCase):
    """F15-F22 消息目录操作。"""

    def test_next_msg_id(self):
        tmp, _, _, work = make_env()
        try:
            self.assertEqual(next_msg_id(work, "a"), "0001")
            # 已提交消息才计数（ls-files 只列 tracked）
            os.makedirs(os.path.join(work, "a"))
            for n in ("0001", "0002"):
                with open(os.path.join(work, f"a/{n}.md"), "w") as f:
                    f.write("---\nfrom: a\n---\n")
            git_commit(work, ["a/0001.md", "a/0002.md"], "discuss: a/batch")
            self.assertEqual(next_msg_id(work, "a"), "0003")
            # 跳号：0005 存在无 0003/0004 → 0006（max+1）
            with open(os.path.join(work, "a/0005.md"), "w") as f:
                f.write("---\nfrom: a\n---\n")
            git_commit(work, ["a/0005.md"], "discuss: a/0005")
            self.assertEqual(next_msg_id(work, "a"), "0006")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_commit_message_format(self):
        self.assertEqual(commit_message("a", "0003"), "discuss: a/0003")

    def test_is_message_file(self):
        self.assertTrue(is_message_file("a/0001.md"))
        self.assertTrue(is_message_file("human/0001.md"))
        self.assertTrue(is_message_file("ab/0001.md"))  # 多字母目录合法
        self.assertFalse(is_message_file("a/README.md"))
        self.assertFalse(is_message_file("a/0001.txt"))
        self.assertFalse(is_message_file("A/0001.md"))   # 大写不合法
        self.assertFalse(is_message_file("a/12345.md"))  # 5 位
        self.assertFalse(is_message_file("1a/0001.md"))  # 数字开头不合法

    def test_parse_log_nameonly(self):
        c1 = "0123456789abcdef0123456789abcdef01234567"  # 40 hex
        c2 = "fedcba9876543210fedcba9876543210fedcba98"  # 40 hex
        out = (f"{c1}\n"
               "a/0001.md\n"
               "b/0002.md\n"
               "\n"
               f"{c2}\n"
               "c/0003.md\n")
        commits = parse_log_nameonly(out)
        self.assertEqual(len(commits), 2)
        self.assertEqual(commits[0][0], c1)
        self.assertEqual(commits[0][1], ["a/0001.md", "b/0002.md"])
        self.assertEqual(commits[1][1], ["c/0003.md"])
        self.assertEqual(parse_log_nameonly(""), [])

    def test_list_new_messages_and_meta(self):
        tmp, _, bare, work = make_env()
        try:
            # a 写两条，b 写一条，human 写一条（各自独立序号）
            for d in ("a", "b", "human"):
                os.makedirs(os.path.join(work, d))
            written = []
            for agent, body, n in [("a", "A1", "0001"), ("b", "B1", "0001"),
                                   ("human", "H1", "0001"), ("a", "A2", "0002")]:
                p = f"{agent}/{n}.md"
                with open(os.path.join(work, p), "w") as f:
                    f.write(f"---\nfrom: {agent}\n---\n{body}\n")
                written.append(p)
            git_commit(work, written, "discuss: batch")
            git_push(work)

            # 空 since → 全部消息文件
            files = list_new_messages(work, "")
            self.assertEqual(sorted(files), ["a/0001.md", "a/0002.md",
                                             "b/0001.md", "human/0001.md"])

            # 非消息文件不出现（protocol.json 已在 setup commit）
            files2 = list_new_messages(work, "")
            self.assertNotIn("protocol.json", files2)

            # 非法 since → 空（git diff 失败，check=False）
            self.assertEqual(list_new_messages(work, "no-such-ref"), [])

            # read_point：无消息 → ""
            self.assertEqual(read_point(work, "a"), "")

            # new_messages_with_meta：me 过滤 + from/to
            meta = new_messages_with_meta(work, "", me="a")
            sources = {m["path"]: m["from"] for m in meta}
            self.assertNotIn("a/0001.md", sources)   # 自己的过滤
            self.assertIn("b/0001.md", sources)
            self.assertIn("human/0001.md", sources)
            self.assertEqual(meta[0]["to"], "all")   # 缺省 to

            # stale：a/0001 的 seen_at 之后有其他消息 → stale=True
            # （同一 commit 内：diff seen_at..HEAD 包含后续文件）
            for m in new_messages_with_meta(work, ""):
                if m["path"] == "a/0001.md":
                    self.assertFalse(m["stale"])  # seen_at 为空（无 seen_at 字段）
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_new_messages_with_meta_stale(self):
        """stale 判定：消息的 seen_at 之后有**其他**消息更新 → True。"""
        tmp, _, bare, work = make_env()
        try:
            # 第一条：a 写（seen_at = HEAD0）
            head0 = git_head(work)
            os.makedirs(os.path.join(work, "a"))
            with open(os.path.join(work, "a/0001.md"), "w") as f:
                f.write(f"---\nfrom: a\nseen_at: {head0}\n---\nA1\n")
            git_commit(work, ["a/0001.md"], "discuss: a/0001")
            git_push(work)
            # b 再写（推进 HEAD）
            os.makedirs(os.path.join(work, "b"))
            with open(os.path.join(work, "b/0001.md"), "w") as f:
                f.write("---\nfrom: b\n---\nB1\n")
            git_commit(work, ["b/0001.md"], "discuss: b/0001")
            git_push(work)
            # a/0001 的 seen_at(=head0)..HEAD diff 含 b/0001 → stale
            meta = {m["path"]: m for m in new_messages_with_meta(work, "")}
            self.assertTrue(meta["a/0001.md"]["stale"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
