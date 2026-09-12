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
    agent_config_dir, build_agent_config, _strip_jsonc,
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



def _replay_visible(entries):
    """测试期 oracle：复刻 pi 的 buildSessionPath + buildContextEntries
    （`session-manager.js`，函数名已钉住）——peek 我们的产物在 pi 眼里
    **实际可见**哪些条目（不变量 I4）。

    **禁生产复刻**：这是对"我们产物结构"的回归保护，不承担检测 pi
    变更（pi 漂移时 oracle 双侧静默——该防线归真实 e2e）。
    """
    index = {}
    for e in entries:                      # last-wins（后写覆盖）
        if e.get("id"):
            index[e["id"]] = e
    current = entries[-1] if entries else None
    path = []
    while current is not None:
        path.append(current)
        pid = current.get("parentId")
        current = index.get(pid) if pid else None
    path.reverse()
    comp = None
    for e in path:
        if e.get("type") == "compaction":
            comp = e
    if comp is None:
        return path
    ci = next(i for i, e in enumerate(path) if e.get("id") == comp.get("id"))
    out, found = [comp], False
    for i in range(ci):
        e = path[i]
        if e.get("id") == comp.get("firstKeptEntryId"):
            found = True
        if found:
            out.append(e)
    out.extend(path[ci + 1:])
    return out


class TestForkSourceInvariants(unittest.TestCase):
    """不变量 I1–I5 与 e2e11 结构修复（P1/P2/P3/台账）的回归保护。

    测试名按不变量 ID 命名（契约↔断言可 grep 对齐）。
    """

    def _write(self, tmp, lines):
        src = os.path.join(tmp, "src.jsonl")
        with open(src, "w", encoding="utf-8") as f:
            for e in lines:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        return src

    def _read(self, path):
        with open(path, encoding="utf-8") as f:
            return [json.loads(x) for x in f]

    def _msg(self, mid, parent, role, text):
        return {"type": "message", "id": mid, "parentId": parent,
                "timestamp": "2026-09-10T00:00:00.000Z",
                "message": {"role": role, "content": [
                    {"type": "text", "text": text}]}}

    def _chain_src(self, tmp, n=6, anchor_at=2, extra=None):
        """构造真实顺序的源：message 链 + compaction 追加在锚点之后。"""
        lines = [{"type": "session", "id": "src", "version": 3}]
        prev = None
        for i in range(n):
            m = self._msg(f"m{i}", prev, "user", f"内容{i}")
            lines.append(m)
            prev = f"m{i}"
        comp = {"type": "compaction", "id": "c1", "parentId": prev,
                "timestamp": "2026-09-10T00:01:00.000Z",
                "summary": "旧摘要", "firstKeptEntryId": f"m{anchor_at}"}
        lines.append(comp)
        lines.extend(extra or [])
        return self._write(tmp, lines)

    # ---- 配置条目不随 fork 携带（三模式统一）----
    def test_config_entries_stripped(self):
        """`thinking_level_change` 不进 fork 源——pi 才会补写**本场生效值**。

        pi 侧逻辑：`if (!hasThinkingEntry) appendThinkingLevelChange(...)`。
        旧会话的档位条目被复制进来 → pi 认为"已有档位条目"→ 不写本场的 →
        session 里就没有"本场生效档位"这个事实（报告无法做
        「声明值 vs 生效值」对照；2026-09-12 实测）。
        `model_change` **不剔除**（无 --model 的路径靠它回填主 pi 模型）。
        """
        extra = [
            {"type": "thinking_level_change", "id": "t1", "parentId": "m5",
             "timestamp": "2026-09-10T00:02:00.000Z", "thinkingLevel": "max"},
            {"type": "model_change", "id": "mo1", "parentId": "t1",
             "timestamp": "2026-09-10T00:02:01.000Z",
             "provider": "p", "modelId": "m"},
        ]
        for mode in ("budget", "compaction", "full"):
            with self.subTest(mode=mode):
                with tempfile.TemporaryDirectory() as tmp:
                    src = self._chain_src(tmp, extra=extra)
                    out = os.path.join(tmp, "out.jsonl")
                    build_fork_source(src, out, "newid", "/tmp")
                    types = [e.get("type") for e in self._read(out)]
                    self.assertNotIn("thinking_level_change", types)
                    self.assertIn("model_change", types)

    # ---- I1 产物内 id 唯一 ----
    def test_i1_ids_unique(self):
        for mode in ("budget", "compaction", "full"):
            with self.subTest(mode=mode):
                with tempfile.TemporaryDirectory() as tmp:
                    src = self._chain_src(tmp)
                    out = os.path.join(tmp, "o.jsonl")
                    _, err = build_fork_source(src, out, "u", "/p", mode=mode)
                    self.assertIsNone(err)
                    ids = [e.get("id") for e in self._read(out)[1:]]
                    self.assertEqual(len(ids), len(set(ids)), f"{mode}: id 重复")

    # ---- I2 链覆盖全部条目（强形式）----
    def test_i2_chain_covers_all(self):
        """链覆盖全部条目；除**链首**外每条 parentId 指向产物内条目。

        链首例外（compaction 模式）：窗口从锚点起，锚点的父在窗口外
        ——replay 走到它就停（悬空与 None 同效），这是压缩态边界的语义，
        不是缺陷（budget 模式下规范化会把链首显式置 None）。
        """
        for mode in ("budget", "compaction", "full"):
            with self.subTest(mode=mode):
                with tempfile.TemporaryDirectory() as tmp:
                    src = self._chain_src(tmp)
                    out = os.path.join(tmp, "o.jsonl")
                    _, err = build_fork_source(src, out, "u", "/p", mode=mode)
                    self.assertIsNone(err)
                    body = self._read(out)[1:]
                    ids = {e.get("id") for e in body}
                    by_id = {e.get("id"): e for e in body}
                    # 从末条上溯：链覆盖全部条目；只有链首可指向窗口外
                    seen, cur, head = set(), body[-1], None
                    while cur is not None:
                        seen.add(cur.get("id"))
                        pid = cur.get("parentId")
                        if pid and pid not in by_id:
                            head = cur.get("id")    # 链首：父在窗口外
                            break
                        cur = by_id.get(pid) if pid else None
                    self.assertEqual(seen, ids, f"{mode}: 链未覆盖全部条目")
                    dangling = [e.get("id") for e in body
                                if e.get("parentId") is not None
                                and e.get("parentId") not in ids]
                    self.assertLessEqual(len(dangling), 1, f"{mode}: 多处悬空")
                    if dangling:
                        self.assertEqual(dangling[0], head,
                                         f"{mode}: 悬空出现在非链首处")

    # ---- I3 replay 使用的锚点必须在产物内（compaction 模式）----
    def test_i3_replay_anchor_inside(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self._chain_src(tmp)
            out = os.path.join(tmp, "o.jsonl")
            _, err = build_fork_source(src, out, "u", "/p", mode="compaction")
            self.assertIsNone(err)
            body = self._read(out)[1:]
            ids = {e.get("id") for e in body}
            for e in body:
                if e.get("type") == "compaction":
                    self.assertIn(e.get("firstKeptEntryId"), ids)

    # ---- I4 声明集合 == pi replay 可见集合 ----
    def test_i4_declared_set_equals_replay_visible(self):
        """核心：budget 产物含 compaction（"刚压缩完就 fork"）时，replay
        会静默丢弃锚点之前的条目（含我们的 preface）——规范化修复后
        可见集合必须等于产物集合。"""
        with tempfile.TemporaryDirectory() as tmp:
            src = self._chain_src(tmp)      # 窗口含 compaction（尾部）
            out = os.path.join(tmp, "o.jsonl")
            _, err = build_fork_source(src, out, "u", "/p",
                                       mode="budget", keep_tokens=100000)
            self.assertIsNone(err)
            body = self._read(out)[1:]
            ids = {e.get("id") for e in body}
            visible = {e.get("id") for e in _replay_visible(body)}
            self.assertEqual(visible, ids)  # I4：集合身份

    def test_i4_full_matches_source_visibility(self):
        """full 模式是**源的忠实拷贝**：其 replay 可见集合必须与源自身一致
        （源里有 compaction 就有可见性边界——我们不构造窗口也不改写历史）。"""
        with tempfile.TemporaryDirectory() as tmp:
            src = self._chain_src(tmp)
            out = os.path.join(tmp, "o.jsonl")
            _, err = build_fork_source(src, out, "u", "/p", mode="full")
            self.assertIsNone(err)
            src_entries = self._read(src)[1:]
            body = self._read(out)[1:]
            self.assertEqual({e.get("id") for e in _replay_visible(body)},
                             {e.get("id") for e in _replay_visible(src_entries)})

    def test_i4_compaction_mode_visible(self):
        """compaction 模式：锚点条目在被保留区 → 可见集合 = 产物集合。"""
        with tempfile.TemporaryDirectory() as tmp:
            src = self._chain_src(tmp)
            out = os.path.join(tmp, "o.jsonl")
            _, err = build_fork_source(src, out, "u", "/p", mode="compaction")
            self.assertIsNone(err)
            body = self._read(out)[1:]
            ids = {e.get("id") for e in body}
            self.assertEqual({e.get("id") for e in _replay_visible(body)}, ids)

    # ---- I5 记账闭合 ----
    def test_i5_accounting_closure(self):
        """源保留区条目数 =（产物非 preface 条目数）+ forkSourceDropped。"""
        with tempfile.TemporaryDirectory() as tmp:
            src = self._chain_src(tmp, n=8, anchor_at=1)
            out = os.path.join(tmp, "o.jsonl")
            _, err = build_fork_source(src, out, "u", "/p",
                                       mode="budget", keep_tokens=1)
            self.assertIsNone(err)
            lines = self._read(out)
            hdr, body = lines[0], lines[1:]
            src_entries = self._read(src)
            anchor = src_entries.index(
                next(e for e in src_entries if e.get("id") == "m1"))
            window_n = len(src_entries) - anchor          # 源保留区
            preface_n = sum(
                1 for e in body
                if e.get("id") and "[上下文说明]" in json.dumps(e, ensure_ascii=False))
            self.assertEqual(window_n,
                             len(body) - preface_n + hdr["forkSourceDropped"])

    # ---- P2：窗口含 compaction → 产物无 compaction（否则 replay 丢前缀）----
    def test_p2_no_compaction_in_budget_product(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self._chain_src(tmp)
            out = os.path.join(tmp, "o.jsonl")
            _, err = build_fork_source(src, out, "u", "/p", mode="budget")
            self.assertIsNone(err)
            body = self._read(out)[1:]
            self.assertFalse([e for e in body if e.get("type") == "compaction"])
            # 摘要进 preface（信息损失有界）
            self.assertIn("旧摘要", body[0]["message"]["content"][0]["text"])

    def test_p2_degenerate_window_only_compaction(self):
        """退化情形：窗口只容下 compaction → 一律移除（含该条），由 preface
        顶替内容；**不得**保留 comp（保留则 replay 仍按其锚点丢前缀，破坏
        I4——初版守卫“保留该条”正是此误，由本测试推翻）。"""
        with tempfile.TemporaryDirectory() as tmp:
            lines = [{"type": "session", "id": "src", "version": 3},
                     self._msg("m0", None, "user", "旧"),
                     self._msg("k0", "m0", "assistant", "保留"),
                     {"type": "compaction", "id": "c1", "parentId": "k0",
                      "summary": "摘要", "firstKeptEntryId": "k0"}]
            src = self._write(tmp, lines)
            out = os.path.join(tmp, "o.jsonl")
            _, err = build_fork_source(src, out, "u", "/p",
                                       mode="budget", keep_tokens=1)
            self.assertIsNone(err)
            body = self._read(out)[1:]
            types = [e.get("type") for e in body]
            self.assertEqual(types.count("compaction"), 0)   # 一律移除
            self.assertTrue(body, "产物不得为空（preface 顶替）")
            self.assertIn("上下文说明",
                          body[0]["message"]["content"][0]["text"])
            self.assertEqual({e.get("id") for e in _replay_visible(body)},
                             {e.get("id") for e in body})     # I4

    # ---- 台账：边界对齐不得双重计数 ----
    def test_accounting_no_double_count_scenario_b(self):
        """场景 B：big / a1(toolCall) / r1(toolResult)，预算只容 r1 →
        对齐回扩纳入 a1——计数必须恰好等于源条目数。"""
        with tempfile.TemporaryDirectory() as tmp:
            lines = [{"type": "session", "id": "src", "version": 3},
                     self._msg("big", None, "user", "字" * 3000),
                     {"type": "message", "id": "a1", "parentId": "big",
                      "message": {"role": "assistant", "content": [
                          {"type": "toolCall", "id": "t1", "name": "bash",
                           "arguments": {"command": "x"}}]}},
                     {"type": "message", "id": "r1", "parentId": "a1",
                      "message": {"role": "toolResult", "toolCallId": "t1",
                                  "toolName": "bash", "content": [
                                      {"type": "text", "text": "out"}]}}]
            src = self._write(tmp, lines)
            out = os.path.join(tmp, "o.jsonl")
            _, err = build_fork_source(src, out, "u", "/p",
                                       mode="budget", keep_tokens=5)
            self.assertIsNone(err)
            hdr, body = (lambda L: (L[0], L[1:]))(self._read(out))
            preface_n = 1 if "[上下文说明]" in json.dumps(body[0], ensure_ascii=False) else 0
            window_n = 3                     # 源无 compaction → 窗口 = 全部
            self.assertEqual(window_n,
                             len(body) - preface_n + hdr["forkSourceDropped"])

    def test_accounting_no_double_count_scenario_c(self):
        """场景 C：big / 孤儿 r2（无前置 toolCall）→ 孤儿丢弃且只计一次。"""
        with tempfile.TemporaryDirectory() as tmp:
            lines = [{"type": "session", "id": "src", "version": 3},
                     self._msg("big", None, "user", "字" * 3000),
                     {"type": "message", "id": "r2", "parentId": "big",
                      "message": {"role": "toolResult", "toolCallId": "tx",
                                  "toolName": "bash", "content": [
                                      {"type": "text", "text": "out"}]}}]
            src = self._write(tmp, lines)
            out = os.path.join(tmp, "o.jsonl")
            _, err = build_fork_source(src, out, "u", "/p",
                                       mode="budget", keep_tokens=1)
            self.assertIsNone(err)
            hdr, body = (lambda L: (L[0], L[1:]))(self._read(out))
            preface_n = 1 if "[内容已省略]" not in json.dumps(body[0], ensure_ascii=False) else 0
            self.assertEqual(2, len(body) + hdr["forkSourceDropped"])

    # ---- P3：分派与值集合的结构耦合 ----
    def test_p3_new_mode_value_fails_loud(self):
        """FORK_MODES 扩了新值但分派没跟上 → 必须报错（不得落入混合分支）。"""
        import meeting_fs as mf
        old = mf.FORK_MODES
        mf.FORK_MODES = old + ("smart",)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                src = self._chain_src(tmp)
                out = os.path.join(tmp, "o.jsonl")
                n, err = build_fork_source(src, out, "u", "/p", mode="smart")
                self.assertEqual(n, 0)
                self.assertIn("尚未实现分派", err)
                self.assertFalse(os.path.exists(out))
        finally:
            mf.FORK_MODES = old

    def test_p3_per_value_fingerprints(self):
        """逐值指纹：三种模式各自的产物特征（防分派静默走错）。"""
        with tempfile.TemporaryDirectory() as tmp:
            src = self._chain_src(tmp)
            outs = {}
            for mode in ("budget", "compaction", "full"):
                out = os.path.join(tmp, f"{mode}.jsonl")
                _, err = build_fork_source(src, out, "u", "/p", mode=mode)
                self.assertIsNone(err)
                outs[mode] = self._read(out)
            # budget：有预算指纹与丢弃数
            self.assertIn("forkSourceTokensEst", outs["budget"][0])
            self.assertIn("forkSourceDropped", outs["budget"][0])
            # compaction：包含 compaction 条目（自然位置）且无预算指纹
            self.assertNotIn("forkSourceTokensEst", outs["compaction"][0])
            self.assertTrue([e for e in outs["compaction"][1:]
                             if e.get("type") == "compaction"])
            # full：首条 = 源首条（m0），且无折叠/无预算指纹
            self.assertNotIn("forkSourceTokensEst", outs["full"][0])
            self.assertEqual(outs["full"][1]["id"], "m0")



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
                # 真实顺序（pi appendCompaction 把 compaction 追加在当前叶
                # 之后 → comp_idx > kept_idx，实测真实源 10/10 如此）：锚点
                # 条目在前，compaction 条目在其后
                f.write('{"type":"message","id":"k1","parentId":"m1",'
                        '"timestamp":"2026-09-09T00:02:00.000Z",'
                        '"message":{"role":"assistant","content":"新"}}\n')
                f.write('{"type":"compaction","id":"c1","parentId":"k1",'
                        '"timestamp":"2026-09-09T00:03:00.000Z",'
                        '"summary":"摘要","firstKeptEntryId":"k1"}\n')
        return src

    def test_compaction_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self._make_src(tmp)
            out = os.path.join(tmp, "sub", "fork-src.jsonl")
            n, err = build_fork_source(src, out, "uuid-x", "/proj",
                                       mode="compaction")
            self.assertIsNone(err)
            lines = [json.loads(x) for x in open(out)]
            self.assertEqual(len(lines), 3)  # header + k1 + compaction
            self.assertEqual(lines[0]["id"], "uuid-x")
            self.assertEqual(lines[0]["cwd"], "/proj")
            self.assertEqual(lines[0]["forkSourceMode"], "compaction")  # 可核查标记
            self.assertEqual(lines[1]["id"], "k1")   # 旧历史 m1 被裁掉
            # compaction 条目在其**自然位置**（= 锚点之后）——不补副本
            # （补副本恒为重复且引入 last-wins 顺序依赖，e2e11 实测）
            self.assertEqual(lines[2]["type"], "compaction")

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



class TestBareOf(unittest.TestCase):
    """bare 路径推导的两个具名入口（不合并成"按形状猜"的单一函数）。"""

    def test_bare_of_base(self):
        from meeting_fs import bare_of_base
        self.assertEqual(bare_of_base("/tmp/disc"), "/tmp/disc/repo.git")

    def test_bare_of_workdir(self):
        from meeting_fs import bare_of_workdir
        self.assertEqual(bare_of_workdir("/tmp/disc/work-a"),
                         "/tmp/disc/repo.git")
        # 中文视角名同样成立（路径拼接不含名字语义）
        self.assertEqual(bare_of_workdir("/tmp/disc/work-性能"),
                         "/tmp/disc/repo.git")



class TestAgentConfig(unittest.TestCase):
    """agent 进程的作用域配置（XDG_CONFIG_HOME）——AFT 语义搜索关闭。

    e2e18 实测：AFT 语义搜索让每个 pi 进程多活 ~57s（61.0s vs 3.3–4.4s），
    而 agent 进程退出在唤醒关键路径上。做法 = 不改用户配置，给 agent 一份
    作用域配置（见 meeting_fs.build_agent_config）。
    """

    def _src(self, tmp, aft=None, mc=None):
        """伪造"用户级配置目录"（XDG_CONFIG_HOME 的形状）。"""
        home = os.path.join(tmp, "confighome")
        d = os.path.join(home, "cortexkit")
        os.makedirs(d, exist_ok=True)
        if aft is not None:
            with open(os.path.join(d, "aft.jsonc"), "w") as f:
                f.write(aft)
        if mc is not None:
            with open(os.path.join(d, "magic-context.jsonc"), "w") as f:
                f.write(mc)
        return home

    def test_disables_semantic_and_keeps_other_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self._src(tmp, aft='{"tool_surface": "all", '
                                      '"experimental_search_index": true, '
                                      '"experimental_semantic_search": true}')
            base = os.path.join(tmp, "base")
            cfg_dir, warns = build_agent_config(base, source_config_home=home)
            self.assertEqual(cfg_dir, agent_config_dir(base))
            self.assertEqual(warns, [])
            with open(os.path.join(cfg_dir, "cortexkit", "aft.jsonc")) as f:
                cfg = json.loads(_strip_jsonc(f.read()))
            self.assertIs(cfg["experimental_semantic_search"], False)
            self.assertIs(cfg["experimental_search_index"], True)   # 保留
            self.assertEqual(cfg["tool_surface"], "all")            # 保留

    def test_disables_legacy_key_too(self):
        """用户配置用旧键名（semantic_search）时**一并关闭**：不依赖 AFT
        到底认哪个名字。"""
        with tempfile.TemporaryDirectory() as tmp:
            home = self._src(tmp, aft='{"semantic_search": true, '
                                      '"search_index": true, "bash": false}')
            base = os.path.join(tmp, "base")
            cfg_dir, _ = build_agent_config(base, source_config_home=home)
            with open(os.path.join(cfg_dir, "cortexkit", "aft.jsonc")) as f:
                cfg = json.loads(_strip_jsonc(f.read()))
            self.assertIs(cfg["semantic_search"], False)            # 旧名
            self.assertIs(cfg["experimental_semantic_search"], False)
            self.assertIs(cfg["search_index"], True)
            self.assertIs(cfg["bash"], False)

    def test_tolerates_jsonc_comments_and_trailing_comma(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self._src(tmp, aft='{\n  // 用户注释\n'
                                      '  "search_index": true, /* 块注释 */\n'
                                      '  "note": "含 // 的字符串",\n}\n')
            base = os.path.join(tmp, "base")
            cfg_dir, warns = build_agent_config(base, source_config_home=home)
            self.assertEqual(warns, [])
            with open(os.path.join(cfg_dir, "cortexkit", "aft.jsonc")) as f:
                cfg = json.loads(_strip_jsonc(f.read()))
            self.assertEqual(cfg["note"], "含 // 的字符串")   # 字符串内 // 保留
            self.assertIs(cfg["search_index"], True)

    def test_unparsable_config_warns_but_still_disables(self):
        """坏配置：**可见**警告 + 仍写出开关（目标达成，但如实告知其余键
        没套用——不静默降级）。"""
        with tempfile.TemporaryDirectory() as tmp:
            home = self._src(tmp, aft='{ 这不是 JSON')
            base = os.path.join(tmp, "base")
            cfg_dir, warns = build_agent_config(base, source_config_home=home)
            self.assertTrue(warns and "无法解析" in warns[0])
            with open(os.path.join(cfg_dir, "cortexkit", "aft.jsonc")) as f:
                cfg = json.loads(_strip_jsonc(f.read()))
            self.assertIs(cfg["experimental_semantic_search"], False)

    def test_missing_user_configs_still_makes_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "base")
            cfg_dir, warns = build_agent_config(
                base, source_config_home=os.path.join(tmp, "none"))
            self.assertEqual(warns, [])
            self.assertTrue(os.path.isfile(
                os.path.join(cfg_dir, "cortexkit", "aft.jsonc")))
            self.assertFalse(os.path.exists(
                os.path.join(cfg_dir, "cortexkit", "magic-context.jsonc")))

    def test_magic_context_copied_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = '{"historian": {"pi": {"thinking_level": "low"}}}\n'
            home = self._src(tmp, aft='{}', mc=body)
            base = os.path.join(tmp, "base")
            cfg_dir, _ = build_agent_config(base, source_config_home=home)
            with open(os.path.join(cfg_dir, "cortexkit",
                                   "magic-context.jsonc")) as f:
                self.assertEqual(f.read(), body)      # 逐字（不解析、不改）

    def test_user_config_not_modified(self):
        """**主 pi 不受影响**：源配置必须逐字节不变（只读 + 拷贝）。"""
        with tempfile.TemporaryDirectory() as tmp:
            aft = '{"semantic_search": true}'
            home = self._src(tmp, aft=aft, mc="{}\n")
            base = os.path.join(tmp, "base")
            build_agent_config(base, source_config_home=home)
            with open(os.path.join(home, "cortexkit", "aft.jsonc")) as f:
                self.assertEqual(f.read(), aft)

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
