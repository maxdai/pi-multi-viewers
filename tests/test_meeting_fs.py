"""meeting_fs 单元测试——F1-F22 全覆盖（API 清单基准，2026-09-01）。

真实 git 环境（tmp bare + clone）：I/O 层必须真 git 验证（mock 会掩盖
git 行为差异）。覆盖：正常路径 + 边界（空/缺失/畸形）+ 异常（git 失败）。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meeting_fs import (
    build_fork_source,
    build_bootstrap,
    preserve_result_md,
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
        # 目录名对齐 agent 名校验（2026-09-09）：viewers 中文视角名合法；
        # 禁 / 与空白——A/数字开头同样合法（校验只拦空名/分隔符/空白/超长）
        self.assertTrue(is_message_file("A/0001.md"))
        self.assertTrue(is_message_file("1a/0001.md"))
        self.assertTrue(is_message_file("性能/0001.md"))
        self.assertFalse(is_message_file("a/README.md"))
        self.assertFalse(is_message_file("a/0001.txt"))
        self.assertFalse(is_message_file("a/12345.md"))  # 5 位
        self.assertFalse(is_message_file("a b/0001.md"))  # 空白非法

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


class TestActiveForkSource(unittest.TestCase):
    """fork 源生成（compaction 边界裁剪 + header 重写）。"""

    def _make_src(self, tmp, with_compaction=True):
        src = os.path.join(tmp, "src.jsonl")
        with open(src, "w") as f:
            f.write('{"type":"session","id":"src","version":3}\n')
            f.write('{"type":"message","id":"m1","parentId":null,'
                    '"timestamp":"2026-09-09T00:00:00.000Z",'
                    '"message":{"role":"user","content":"旧"}}\n')
            if with_compaction:
                f.write('{"type":"compaction","id":"c1","parentId":"m1",'
                        '"timestamp":"2026-09-09T00:01:00.000Z",'
                        '"summary":"摘要","firstKeptEntryId":"k1"}\n')
                f.write('{"type":"message","id":"k1","parentId":"c1",'
                        '"timestamp":"2026-09-09T00:02:00.000Z",'
                        '"message":{"role":"assistant","content":"新"}}\n')
        return src

    def test_compaction_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self._make_src(tmp)
            out = os.path.join(tmp, "sub", "fork-src.jsonl")
            n, err = build_fork_source(src, out, "uuid-x", "/proj",
                                       mode="compaction")
            self.assertIsNone(err)
            lines = [json.loads(x) for x in open(out)]
            self.assertEqual(len(lines), 3)  # header + compaction + kept
            self.assertEqual(lines[0]["id"], "uuid-x")
            self.assertEqual(lines[0]["cwd"], "/proj")
            self.assertEqual(lines[0]["forkSourceMode"], "compaction")  # 可核查标记
            self.assertEqual(lines[1]["type"], "compaction")
            self.assertEqual(lines[2]["id"], "k1")  # 旧历史 m1 被裁掉

    def test_no_compaction_bootstrap_fallback(self):
        """无 compaction（引导 session）→ 全量兜底（引导本就干净）。"""
        with tempfile.TemporaryDirectory() as tmp:
            src = self._make_src(tmp, with_compaction=False)
            out = os.path.join(tmp, "out.jsonl")
            n, err = build_fork_source(src, out, "u", "/p",
                                       mode="compaction")
            self.assertIsNone(err)
            lines = [json.loads(x) for x in open(out)]
            self.assertEqual(len(lines), 2)  # header + 全量 1 条
            self.assertEqual(lines[1]["id"], "m1")
            self.assertEqual(lines[0]["forkSourceMode"], "full")  # 全量兜底标记

    def test_full_mode(self):
        """mode="full"：有 compaction 的源也全量（forkMode 参数化，
        用户 2026-09-10——验证全量历史+切换叙事场景）。"""
        with tempfile.TemporaryDirectory() as tmp:
            src = self._make_src(tmp)  # 含 compaction + keep1
            out = os.path.join(tmp, "full.jsonl")
            n, err = build_fork_source(src, out, "u", "/p",
                                              mode="full")
            self.assertIsNone(err)
            lines = [json.loads(x) for x in open(out)]
            self.assertEqual(len(lines), 4)  # header + m1 + c1 + k1（全量）
            self.assertEqual(lines[0]["forkSourceMode"], "full")
            self.assertEqual(lines[1]["id"], "m1")  # 旧历史保留

    def test_budget_trim(self):
        """budget：预算裁剪——只保留最近窗口，旧条目丢弃并生成上下文说明。

        对齐 pi 自身 compaction 的不变量（摘要 + 最近窗口）——长会话 fork
        唯一可行形态（原始条目会超模型窗口，实测 2026-09-10）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "big.jsonl")
            with open(src, "w", encoding="utf-8") as f:
                f.write('{"type":"session","id":"s","version":3}\n')
                big = "字" * 9000     # 每条 ~3k tokens
                for i in range(10):
                    f.write(json.dumps({
                        "type": "message", "id": f"m{i}", "parentId": None,
                        "timestamp": "2026-09-10T00:00:00.000Z",
                        "message": {"role": "user", "content": [
                            {"type": "text", "text": f"#{i} " + big}]}},
                        ensure_ascii=False) + "\n")
                f.write(json.dumps({
                    "type": "compaction", "id": "c1", "parentId": "m9",
                    "timestamp": "2026-09-10T00:01:00.000Z",
                    "summary": "早期摘要", "firstKeptEntryId": "m0"},
                    ensure_ascii=False) + "\n")
            out = os.path.join(tmp, "budget.jsonl")
            n, err = build_fork_source(src, out, "u", "/p",
                                              mode="budget", keep_tokens=10000)
            self.assertIsNone(err)
            lines = [json.loads(x) for x in open(out)]
            self.assertEqual(lines[0]["forkSourceMode"], "budget")
            self.assertLessEqual(lines[0]["forkSourceTokensEst"], 10000)
            kept = [e for e in lines[1:] if e.get("type") == "message"]
            self.assertLess(len(kept), 11)              # 确实裁掉了旧条目
            self.assertIn("m9", kept[-1]["id"])         # 最近一条必留
            preface = kept[0]["message"]["content"][0]["text"]
            self.assertIn("已省略", preface)             # 省略说明
            self.assertIn("早期摘要", preface)           # compaction 摘要带上
            self.assertGreater(lines[0]["forkSourceDropped"], 0)   # 丢弃数可核查
            # P4：保留区首条 parentId 接回 preface（一条链）
            self.assertEqual(kept[1]["parentId"], kept[0]["id"])

    def test_budget_folds_thinking_and_tool_results(self):
        """budget 折叠：thinking 丢弃、旧工具输出换省略标记、保留当次输出。"""
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "s.jsonl")
            with open(src, "w", encoding="utf-8") as f:
                f.write('{"type":"session","id":"s","version":3}\n')
                f.write(json.dumps({
                    "type": "message", "id": "a1", "parentId": None,
                    "message": {"role": "assistant", "content": [
                        {"type": "thinking", "thinking": "推理痕迹"},
                        {"type": "toolCall", "id": "t1", "name": "bash",
                         "arguments": {"command": "x" * 3000}},
                        {"type": "text", "text": "结论"}]}},
                    ensure_ascii=False) + "\n")
                for i in range(20):   # 超 _TOOL_RESULT_KEEP → 靠前的应被省略
                    f.write(json.dumps({
                        "type": "message", "id": f"r{i}", "parentId": "a1",
                        "message": {"role": "toolResult", "toolName": "bash",
                                    "content": [{"type": "text",
                                                 "text": f"输出{i}"}]}},
                        ensure_ascii=False) + "\n")
            out = os.path.join(tmp, "c.jsonl")
            _, err = build_fork_source(src, out, "u", "/p",
                                              mode="budget")
            self.assertIsNone(err)
            msgs = [json.loads(x) for x in open(out)][1:]
            asst = next(e for e in msgs if e["id"] == "a1")
            blocks = asst["message"]["content"]
            self.assertFalse(any(b.get("type") == "thinking" for b in blocks))
            call = next(b for b in blocks if b.get("type") == "toolCall")
            # 长字符串被截断（结构保留：键名不变、JSON 仍是合法对象）
            self.assertLess(len(call["arguments"]["command"]), 3000)
            self.assertIn("截断", call["arguments"]["command"])
            results = [e for e in msgs if e["id"].startswith("r")]
            elided = [e for e in results
                      if "已省略" in e["message"]["content"][0]["text"]]
            full = [e for e in results
                    if e["message"]["content"][0]["text"].startswith("输出")]
            self.assertTrue(elided and full)          # 旧的省略、近的保留
            self.assertEqual(full[-1]["id"], "r19")   # 最新一条保留

    def test_budget_cut_avoids_orphan_tool_result(self):
        """边界对齐：保留区不得以 toolResult 开头（其 toolCall 已裁掉 →
        provider 报 "role 'tool' must be a response to ..."，e2e 实测）。"""
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "s.jsonl")
            with open(src, "w", encoding="utf-8") as f:
                f.write('{"type":"session","id":"s","version":3}\n')
                # 大块旧历史 + 一组 toolCall/toolResult（预算只容得下尾部）
                big = "字" * 3000
                for i in range(8):
                    f.write(json.dumps({
                        "type": "message", "id": f"m{i}", "parentId": None,
                        "message": {"role": "user", "content": [
                            {"type": "text", "text": big}]}},
                        ensure_ascii=False) + "\n")
                f.write(json.dumps({
                    "type": "message", "id": "a1", "parentId": "m7",
                    "message": {"role": "assistant", "content": [
                        {"type": "toolCall", "id": "t1", "name": "bash",
                         "arguments": {"command": "echo hi"}}]}},
                    ensure_ascii=False) + "\n")
                f.write(json.dumps({
                    "type": "message", "id": "r1", "parentId": "a1",
                    "message": {"role": "toolResult", "toolCallId": "t1",
                                "toolName": "bash", "content": [
                                    {"type": "text", "text": "hi"}]}},
                    ensure_ascii=False) + "\n")
            out = os.path.join(tmp, "o.jsonl")
            # 预算小到只能容纳尾部（toolCall/toolResult 对）
            _, err = build_fork_source(src, out, "u", "/p",
                                              mode="budget", keep_tokens=10)
            self.assertIsNone(err)
            msgs = [json.loads(x) for x in open(out, encoding="utf-8")][1:]
            msgs = [e for e in msgs if e.get("type") == "message"]
            self.assertTrue(msgs, "保留区不应为空")
            # 首条不得是孤儿 toolResult；每个 toolResult 都应有前置 toolCall
            self.assertNotEqual(msgs[0].get("message", {}).get("role"),
                                "toolResult")
            calls = set()
            for e in msgs:
                m = e.get("message") or {}
                c = m.get("content")
                if isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "toolCall":
                            calls.add(b.get("id"))
                if m.get("role") == "toolResult":
                    self.assertIn(m.get("toolCallId"), calls)   # 无孤儿

    def test_budget_bootstrap_no_compaction(self):
        """budget 遇无 compaction 的源（引导 session）：不崩、可用。"""
        with tempfile.TemporaryDirectory() as tmp:
            src = self._make_src(tmp, with_compaction=False)
            out = os.path.join(tmp, "o.jsonl")
            n, err = build_fork_source(src, out, "u", "/p",
                                              mode="budget")
            self.assertIsNone(err)
            lines = [json.loads(x) for x in open(out)]
            self.assertEqual(lines[0]["forkSourceMode"], "budget")

    def test_invalid_mode_rejected(self):
        """值域守卫（P0，e2e10 评审）：非法/历史 forkMode 就地报错、
        不生成产物（非法值曾静默落到"边界后全量、不折叠、无预算"分支
        → 930k tokens 超窗，且被 engine 异常边界吞成廉价重试）。"""
        with tempfile.TemporaryDirectory() as tmp:
            src = self._make_src(tmp)
            out = os.path.join(tmp, "o.jsonl")
            for bad in ("bogus", "curated", "active", ""):
                n, err = build_fork_source(src, out, "u", "/p", mode=bad)
                self.assertEqual(n, 0)
                self.assertIn("未知 forkMode", err)
                self.assertIn("budget", err)      # 错误文本枚举合法值
                self.assertFalse(os.path.exists(out))   # 无半成品

    def test_bad_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            n, err = build_fork_source(
                os.path.join(tmp, "nope.jsonl"),
                os.path.join(tmp, "out.jsonl"), "u", "/p")
            self.assertEqual(n, 0)
            self.assertIn("读取失败", err)


if __name__ == "__main__":
    unittest.main()

class TestBootstrap(unittest.TestCase):
    """空白引导 session（2026-09-09）：零 LLM 造合法 fork 源。"""

    def test_generates_header_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "bootstrap.jsonl")
            p, err = build_bootstrap(out, "/proj", session_id="bs-1")
            self.assertIsNone(err)
            lines = [json.loads(x) for x in open(out)]
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["type"], "session")
            self.assertEqual(lines[0]["id"], "bs-1")
            self.assertEqual(lines[0]["cwd"], "/proj")

    def test_random_id_when_unspecified(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "b.jsonl")
            p, _ = build_bootstrap(out, "/p")
            h = json.loads(open(out).readline())
            self.assertRegex(h["id"], r"^[0-9a-f-]{36}$")

    def test_registry_logged(self):
        """创建即登记（防误删，2026-09-09）：登记日志含 id/path/cwd。"""
        import meeting_fs
        orig = meeting_fs.SESSION_REGISTRY
        try:
            with tempfile.TemporaryDirectory() as tmp:
                meeting_fs.SESSION_REGISTRY = os.path.join(tmp, "reg.log")
                out = os.path.join(tmp, "b.jsonl")
                meeting_fs.build_bootstrap(out, "/p", session_id="reg-1")
                rec = json.loads(open(os.path.join(tmp, "reg.log")).readline())
                self.assertEqual(rec["session_id"], "reg-1")
                self.assertEqual(rec["path"], out)
                self.assertEqual(rec["cwd"], "/p")
        finally:
            meeting_fs.SESSION_REGISTRY = orig

class TestChineseAgentPaths(unittest.TestCase):
    """中文 agent 名 + git 路径（2026-09-09 引号 bug 回归）。

    git ls-files 默认 quotepath 转义非 ASCII（输出 "\\346\\200..."
    带引号）→ 带引号路径读不到文件 → list_my_messages 恒空 → is_first
    恒 True → 无限首启 + 配额绕过（freezing 卡死根因，e2e6 实测）。
    修复：ls-files 加 -z（NUL 分隔不做转义）。viewers 模式中文视角名
    是产品核心，此测试防潜伏回归。
    """

    def _mk_env(self):
        tmp = tempfile.mkdtemp(prefix="cn-")
        work = os.path.join(tmp, "work")
        os.makedirs(os.path.join(work, "性能"))
        # work 需要是真 git 仓库（git_ls_files 操作它）
        subprocess.run(["git", "init", "-q"], cwd=work, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=work)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=work)
        # 提交一条中文路径消息
        msg = os.path.join(work, "性能", "0001.md")
        with open(msg, "w") as f:
            f.write("---\nfrom: 性能\ntype: message\n---\n\n正文\n")
        subprocess.run(["git", "add", "-A"], cwd=work, check=True)
        subprocess.run(["git", "commit", "-qm", "discuss: 性能/0001"], cwd=work)
        return tmp, work

    def test_list_my_messages_chinese(self):
        tmp, work = self._mk_env()
        try:
            msgs = list_my_messages(work, "性能")
            self.assertEqual(len(msgs), 1)
            self.assertEqual(msgs[0]["from"], "性能")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_next_msg_id_chinese(self):
        tmp, work = self._mk_env()
        try:
            self.assertEqual(next_msg_id(work, "性能"), "0002")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_is_committed_chinese(self):
        tmp, work = self._mk_env()
        try:
            from meeting_engine import _is_committed
            self.assertTrue(_is_committed(work, "性能/0001.md"))
            self.assertFalse(_is_committed(work, "性能/0002.md"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

class TestPreserveResultMd(unittest.TestCase):
    """T2 合并（e2e7 评审）：两个触发点共享的保存实现。"""

    def _mk(self, tmp, with_result=True):
        base = os.path.join(tmp, "discuss-x")
        bare = os.path.join(base, "repo.git")
        os.makedirs(base)
        subprocess.run(["git", "init", "--bare", bare], check=True,
                       capture_output=True)
        if with_result:
            work = os.path.join(tmp, "w")
            subprocess.run(["git", "clone", bare, work], check=True,
                           capture_output=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=work)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=work)
            with open(os.path.join(work, "result.md"), "w") as f:
                f.write("# 结论\n")
            subprocess.run(["git", "add", "-A"], cwd=work, check=True)
            subprocess.run(["git", "commit", "-qm", "discuss: result.md"],
                           cwd=work, check=True)
            subprocess.run(["git", "push", bare, "master"], cwd=work,
                           check=True, capture_output=True)
        return base

    def test_saves_to_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._mk(tmp)
            dest = preserve_result_md(base)
            self.assertIsNotNone(dest)
            self.assertTrue(dest.endswith("discuss-x-result.md"))
            self.assertIn("# 结论", open(dest).read())

    def test_no_result_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._mk(tmp, with_result=False)
            self.assertIsNone(preserve_result_md(base))

    def test_no_bare_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(preserve_result_md(os.path.join(tmp, "nope")))
