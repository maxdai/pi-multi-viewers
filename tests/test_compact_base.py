"""定向摘要 fork 模式（决策 24）的单元测试——**零 LLM**。

覆盖三层：
  ① meeting_fs：`finalize_compaction_base`（剥快照 + 统计）与 `summary` 模式的切片
  ② meeting_loop：`generate_compact_base` 的 RPC 交互（假 Popen 回放三态）+ 首唤取源
  ③ 指令措辞：`build_summary_instructions` 必须带主题、且带"只陈述事实"的反框架句
"""

import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import meeting_fs
import meeting_loop


def _entry(eid, parent, role="user", text="x" * 300, ts="2026-01-01T00:00:00Z"):
    return {"type": "message", "id": eid, "parentId": parent, "timestamp": ts,
            "message": {"role": role, "content": [{"type": "text", "text": text}]}}


def _write_session(path, n_msgs=5, compaction=True, anchor="m3", system=True):
    entries = [{"type": "session", "version": 3, "id": "s1",
                "timestamp": "2026-01-01T00:00:00Z", "cwd": "/proj"}]
    parent = None
    for i in range(n_msgs):
        entries.append(_entry(f"m{i}", parent))
        parent = f"m{i}"
    if compaction:
        comp = {"type": "compaction", "id": "c1", "parentId": parent,
                "timestamp": "2026-01-02T00:00:00Z", "summary": "S" * 600,
                "firstKeptEntryId": anchor, "tokensBefore": 999}
        if system:
            comp["systemMessage"] = {"role": "system", "content": "",
                                     "sections": {"preamble": "主 pi 的 prompt"}}
        entries.append(comp)
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return entries


class TestFinalizeCompactionBase(unittest.TestCase):
    """`finalize_compaction_base`：只做两件事（剥快照 + 统计），其余不动。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mvbase-")
        self.path = os.path.join(self.tmp, "base.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_strips_snapshot_and_counts(self):
        _write_session(self.path)
        stats, err = meeting_fs.finalize_compaction_base(self.path)
        self.assertEqual(err, "")
        # ① 剥掉快照
        with open(self.path, encoding="utf-8") as f:
            after = [json.loads(l) for l in f if l.strip()]
        comp = [e for e in after if e.get("type") == "compaction"][0]
        self.assertNotIn("systemMessage", comp)
        # ② 统计：摘要 600 字符 / 窗口 m3+m4 = 600 字符（est = 字符/3）
        self.assertEqual(stats["summary_est"], 200)
        self.assertEqual(stats["window_est"], 200)
        self.assertEqual(stats["dropped_entries"], 4)   # m0..m3（锚点 m3 的索引）
        self.assertEqual(stats["anchor"], "m3")
        self.assertEqual(stats["tokens_before"], 999)

    def test_entry_count_and_anchor_unchanged(self):
        """不删条目、不改锚点——远端历史留在文件里（可审计、可被工具读）。"""
        before = _write_session(self.path)
        meeting_fs.finalize_compaction_base(self.path)
        with open(self.path, encoding="utf-8") as f:
            after = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(before), len(after))
        comp = [e for e in after if e.get("type") == "compaction"][0]
        self.assertEqual(comp["firstKeptEntryId"], "m3")

    def test_no_compaction_is_error(self):
        _write_session(self.path, compaction=False)
        stats, err = meeting_fs.finalize_compaction_base(self.path)
        self.assertIsNone(stats)
        self.assertIn("没有 compaction", err)

    def test_missing_file_is_error(self):
        stats, err = meeting_fs.finalize_compaction_base(
            os.path.join(self.tmp, "nope.jsonl"))
        self.assertIsNone(stats)
        self.assertIn("读取失败", err)

    def test_anchor_missing_counts_all(self):
        """锚点不在源中 ⇒ 窗口为空、被覆盖条目 = 全部（对齐 pi 的良性语义）。"""
        _write_session(self.path, anchor="zzz")
        stats, err = meeting_fs.finalize_compaction_base(self.path)
        self.assertEqual(err, "")
        self.assertEqual(stats["window_est"], 0)
        self.assertEqual(stats["dropped_entries"], 6)


class TestSummaryForkMode(unittest.TestCase):
    """`build_fork_source(mode="summary")`：与 compaction 同一条切片，标记不同。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mvsum-")
        self.src = os.path.join(self.tmp, "base.jsonl")
        self.out = os.path.join(self.tmp, "fork.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_slice_matches_compaction(self):
        _write_session(self.src)
        n, err = meeting_fs.build_fork_source(self.src, self.out, "sid-x", "/proj",
                                              mode="summary")
        self.assertFalse(err)
        with open(self.out, encoding="utf-8") as f:
            out = [json.loads(l) for l in f if l.strip()]
        header = out[0]
        self.assertEqual(header["forkSourceMode"], "summary")
        # 锚点起（含 compaction 条目本身）= m3, m4, c1
        self.assertEqual([e["id"] for e in out[1:]], ["m3", "m4", "c1"])
        # 指纹：有 est、无 dropped（丢弃发生在 base 生成那步，账记在别处）
        self.assertIn("forkSourceTokensEst", header)
        self.assertNotIn("forkSourceDropped", header)

    def test_keyword_in_domain(self):
        self.assertIn("summary", meeting_fs.FORK_MODES)


def _append_compaction(session_file):
    """模拟 pi 的产出一半：把最后一条消息之前的窗口压成一条 compaction 条目。"""
    with open(session_file, encoding="utf-8") as f:
        entries = [json.loads(l) for l in f if l.strip()]
    body = [e for e in entries if e.get("type") == "session"]
    msgs = [e for e in entries if e.get("type") != "session"]
    if not msgs:
        return
    body.append({"type": "compaction", "id": "fake-c", "parentId": msgs[-1]["id"],
                 "timestamp": "2026-01-03T00:00:00Z", "summary": "## Goal\n" + "s" * 300,
                 "firstKeptEntryId": msgs[-1]["id"], "tokensBefore": 123})
    with open(session_file, "w", encoding="utf-8") as f:
        for e in body:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


class _Hang:
    """永不产出、永不 EOF 的 stdout（造"真超时"用）。"""

    def __iter__(self):
        return self

    def __next__(self):
        while True:
            time.sleep(0.05)


class _FakeRpc:
    """假 `pi --mode rpc`：按 **stdin 里收到的请求**给出对应响应。

    为什么要这样：生产现在对同一个 pi 进程序列发**两类**请求——
    `get_session_stats`（限幅复测，零 LLM）与 `compact`（真摘要）。假进程必须在
    spawn 之后才看得到请求（生产在 Popen 返回后才写 stdin）⇒ stdout 做成惰性的，
    迭代时再决定吐什么。装置对齐生产，否则测的是想象出来的协议。
    """

    def __init__(self, pi_tokens=1000, compact_ok=True, compact_error="",
                 hang_on=None, session=None, no_output=False):
        self.no_output = no_output
        self.pi_tokens = pi_tokens
        self.compact_ok = compact_ok
        self.compact_error = compact_error
        self.hang_on = hang_on          # None / "stats" / "compact"
        self.session = session
        self.stdin = io.StringIO()
        self.stderr = io.StringIO("")
        self.stdout = _LazyOut(self)
        self.returncode = 0
        self.killed = False
        self.requests = []              # 记录收到过哪些请求（断言用）
        self.spawned = []               # 每次 Popen 造出的实例（共享登记表）

    def clone(self):
        """新进程（生产每次请求都 spawn 一个 pi）——共用登记表，便于断言。"""
        inst = _FakeRpc(self.pi_tokens, self.compact_ok, self.compact_error,
                        self.hang_on, self.session, self.no_output)
        inst.spawned = self.spawned
        self.spawned.append(inst)
        return inst

    def _lines(self):
        # 等请求真的到达（生产在 spawn **之后**才写 stdin；真实 pi 也不会在收到
        # 请求前就作答）——不等的话假进程会"抢答"空响应，测出来的是装置的竞态
        deadline = time.monotonic() + 2.0
        while not self.stdin.getvalue() and time.monotonic() < deadline:
            time.sleep(0.005)
        req = self.stdin.getvalue()
        self.requests.append(req)
        if self.no_output:
            return []
        kind = "compact" if '"compact"' in req else "?"
        if self.hang_on == kind:
            while True:
                time.sleep(0.05)
        if kind == "compact":
            if self.session is not None:
                _append_compaction(self.session)
            return [json.dumps({"command": "compact", "success": self.compact_ok,
                                "error": self.compact_error}) + "\n"]
        return []

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class _LazyOut:
    """惰性 stdout：迭代时才向 owner 要内容（那时 stdin 已被生产写入）。"""

    def __init__(self, owner):
        self.owner = owner

    def __iter__(self):
        return iter(self.owner._lines())


class _Hang:
    """永不产出、永不 EOF 的 stdout（造"真超时"用）。"""

    def __iter__(self):
        return self

    def __next__(self):
        while True:
            time.sleep(0.05)


class TestGenerateCompactBase(unittest.TestCase):
    """`generate_compact_base`：限幅迭代 + RPC 三态（成功 / compact 失败 / 超时）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mvgen-")
        self.main = os.path.join(self.tmp, "main.jsonl")
        self.out = os.path.join(self.tmp, "base.jsonl")
        _write_session(self.main, n_msgs=3, anchor="m1")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _patch(self, fake):
        return mock.patch.object(meeting_loop.subprocess, "Popen",
                                 side_effect=lambda *a, **kw: fake.clone())

    def test_success_writes_stats_and_strips(self):
        fake = _FakeRpc(pi_tokens=1000, session=self.out)
        with self._patch(fake):
            stats, err = meeting_loop.generate_compact_base(
                self.main, self.out, "m1", "测试主题")
        self.assertEqual(err, "")
        self.assertEqual(stats["model"], "m1")
        self.assertEqual(stats["thinking"], "off")      # 摘要不推理
        self.assertEqual(stats["front_tokens"],
                         meeting_fs.DEFAULT_SUMMARY_FRONT_TOKENS)
        # base 落盘 + 快照已剥（finalize 真跑了）
        with open(self.out, encoding="utf-8") as f:
            base = [json.loads(l) for l in f if l.strip()]
        comp = [e for e in base if e.get("type") == "compaction"][0]
        self.assertNotIn("systemMessage", comp)
        with open(os.path.join(self.tmp, meeting_fs.COMPACT_BASE_STATS),
                  encoding="utf-8") as f:
            st = json.load(f)
        self.assertIn("instructions", st)
        self.assertIn("input_est", st)                  # 限幅记账
        self.assertIn("input_dropped", st)

    def test_compact_failure_is_reported(self):
        fake = _FakeRpc(pi_tokens=1000, compact_ok=False,
                        compact_error="Nothing to compact (session too small)",
                        session=self.out)
        with self._patch(fake):
            stats, err = meeting_loop.generate_compact_base(
                self.main, self.out, "m1", "t")
        self.assertIsNone(stats)
        self.assertIn("Nothing to compact", err)

    def test_compact_timeout_is_reported(self):
        """限幅复测照常答，**compact 那一步**挂住 ⇒ 报超时并 kill。"""
        fake = _FakeRpc(pi_tokens=1000, hang_on="compact", session=self.out)
        with mock.patch.object(meeting_loop, "COMPACT_TIMEOUT_SEC", 0.2), \
                self._patch(fake):
            stats, err = meeting_loop.generate_compact_base(
                self.main, self.out, "m1", "t")
        self.assertIsNone(stats)
        self.assertIn("超时", err)
        self.assertTrue(any(p.killed for p in fake.spawned))

    def test_no_response_is_failure(self):
        """进程退出却没给响应（崩溃/换协议）⇒ 立刻失败，不等满超时。"""
        fake = _FakeRpc(pi_tokens=1000, session=self.out, no_output=True)
        with self._patch(fake):
            stats, err = meeting_loop.generate_compact_base(
                self.main, self.out, "m1", "t")
        self.assertIsNone(stats)
        self.assertIn("摘要生成失败", err)

    def test_instructions_carry_topic_and_anti_framing(self):
        text = meeting_loop.build_summary_instructions("某主题")
        self.assertIn("某主题", text)
        self.assertIn("只陈述事实", text)
        self.assertIn("不要替读者做取舍排序", text)


class TestFirstWakeSourceSelection(unittest.TestCase):
    """首唤取源：summary 有 base → 用 base；无 base → **可见地**回落 budget。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mvwake-")
        self.workdir = os.path.join(self.tmp, "work-a")
        self.session_dir = os.path.join(self.tmp, "pi-sessions")
        os.makedirs(self.workdir)
        os.makedirs(self.session_dir)
        self.main = os.path.join(self.tmp, "main.jsonl")
        _write_session(self.main, n_msgs=5)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self):
        return meeting_loop._prepare_fork_session(
            self.workdir, "a", "sid1", self.main, self.tmp, self.session_dir,
            "summary", "主题")

    def test_uses_base_when_present(self):
        base = os.path.join(self.tmp, meeting_fs.COMPACT_BASE_NAME)
        _write_session(base)
        out = self._run()
        with open(out, encoding="utf-8") as f:
            header = json.loads(f.readline())
        self.assertEqual(header["forkSourceMode"], "summary")
        self.assertEqual(header["parentSession"], base)

    def test_falls_back_visibly_without_base(self):
        with mock.patch.object(meeting_loop, "log") as lg:
            out = self._run()
        with open(out, encoding="utf-8") as f:
            header = json.loads(f.readline())
        self.assertEqual(header["forkSourceMode"], "budget")
        joined = " ".join(str(c) for c in lg.call_args_list)
        self.assertIn("base 不存在", joined)


class TestBuildSummaryInput(unittest.TestCase):
    """`meeting_fs.build_summary_input`：交给摘要器的**有界**输入。

    这是本轮的核心防线——pi 的 compact 会通读"上一次 compaction 之后的全部
    消息"，主 session 上实测 est≈208k tokens（一次调用 >900s 不返回）。这里
    只保留最近 `window_tokens` 的**可见**条目，其余如实记账。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mvsin-")
        self.src = os.path.join(self.tmp, "main.jsonl")
        self.out = os.path.join(self.tmp, "base.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, n=6, with_compaction=True, extra=()):
        entries = [{"type": "session", "version": 3, "id": "s1",
                    "timestamp": "2026-01-01T00:00:00Z", "cwd": "/proj"}]
        parent = None
        for i in range(n):
            e = _entry(f"m{i}", parent)
            entries.append(e)
            parent = f"m{i}"
        for e in extra:                      # 插在 compaction 之后
            entries.append(e)
        if with_compaction:
            entries.append({"type": "compaction", "id": "c1", "parentId": parent,
                            "timestamp": "2026-01-02T00:00:00Z", "summary": "S",
                            "firstKeptEntryId": "m3", "tokensBefore": 9})
        with open(self.src, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        return entries

    def _load(self):
        with open(self.out, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]

    def test_window_bounds_and_accounts(self):
        self._write(n=6)
        stats, err = meeting_fs.build_summary_input(self.src, self.out, 350)
        self.assertEqual(err, "")
        out = self._load()
        header, body = out[0], out[1:]
        # 每条 300 字符 ≈ 100 est；350 预算 ⇒ 只留最后 3 条（m3,m4,m5）
        self.assertEqual([e["id"] for e in body], ["m3", "m4", "m5"])
        self.assertEqual(stats["dropped"], 0)          # 可见集合只有 m3..m5（锚点 m3）
        self.assertTrue(header["summaryInput"])
        self.assertEqual(header["summaryInputWindowTokens"], 350)

    def test_chain_is_valid_and_head_is_none(self):
        self._write(n=6)
        meeting_fs.build_summary_input(self.src, self.out, 200)
        body = self._load()[1:]
        self.assertIsNone(body[0]["parentId"])
        ids = {e["id"] for e in body}
        for e in body:
            self.assertIn(e["parentId"], ids | {None})   # 链不断（I2）

    def test_keeps_only_context_participating_entries(self):
        extra = [
            _entry("sys1", "m5", role="system", text="主 pi 的 system prompt"),
            {"type": "custom", "id": "cu1", "parentId": "m5",
             "timestamp": "2026-01-02T00:00:00Z", "customType": "x", "data": {}},
            {"type": "thinking_level_change", "id": "tlc1", "parentId": "m5",
             "timestamp": "2026-01-02T00:00:00Z", "level": "high"},
            {"type": "custom_message", "id": "cm1", "parentId": "m5",
             "timestamp": "2026-01-02T00:00:00Z", "customType": "note",
             "content": "note", "display": True},
        ]
        self._write(n=6, extra=extra)
        meeting_fs.build_summary_input(self.src, self.out, 10 ** 6)
        body = self._load()[1:]
        kinds = [e["id"] for e in body]
        self.assertNotIn("sys1", kinds)        # system 不进摘要输入
        self.assertNotIn("cu1", kinds)         # 非上下文条目
        self.assertNotIn("tlc1", kinds)
        self.assertIn("cm1", kinds)            # custom_message 是上下文

    def test_context_edit_hides_target(self):
        """`context_edit(replacement=null)` = 从上下文隐去 ⇒ 目标条目也要去掉。"""
        extra = [{"type": "context_edit", "id": "ce1", "parentId": "m5",
                  "timestamp": "2026-01-02T00:00:00Z", "targetId": "m5",
                  "replacement": None}]
        self._write(n=6, extra=extra)
        meeting_fs.build_summary_input(self.src, self.out, 10 ** 6)
        body = self._load()[1:]
        self.assertNotIn("m5", [e["id"] for e in body])
        ids = {e["id"] for e in body}
        for e in body:                          # 隐去后链仍完整
            self.assertIn(e["parentId"], ids | {None})

    def test_no_compaction_means_whole_file(self):
        self._write(n=4, with_compaction=False)
        stats, err = meeting_fs.build_summary_input(self.src, self.out, 10 ** 6)
        self.assertEqual(err, "")
        self.assertEqual([e["id"] for e in self._load()[1:]],
                         ["m0", "m1", "m2", "m3"])
        self.assertEqual(stats["dropped"], 0)

    def test_reports_source_size_for_visible_accounting(self):
        self._write(n=6)
        stats, err = meeting_fs.build_summary_input(self.src, self.out, 100)
        self.assertEqual(err, "")
        self.assertEqual(stats["window_tokens"], 100)
        self.assertGreaterEqual(stats["source_est"], stats["est"])


class TestSummaryProcessShape(unittest.TestCase):
    """摘要进程的命令形状与作用域配置（`--thinking off` / cwd 级项目设置）。

    生产现在对同一会话发**两次**请求（先 `get_session_stats` 复测限幅、再
    `compact`）⇒ 断言"最后一次 spawn"是 compact、且两次的形状都对。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mvshape-")
        self.main = os.path.join(self.tmp, "main.jsonl")
        self.out = os.path.join(self.tmp, "base.jsonl")
        _write_session(self.main, n_msgs=4)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cmd_and_scoped_settings(self):
        seen = []
        fake = _FakeRpc(pi_tokens=1000, session=self.out)

        def _spawn(cmd, **kw):
            seen.append((cmd, kw))
            return fake.clone()

        with mock.patch.object(meeting_loop.subprocess, "Popen",
                               side_effect=_spawn):
            stats, err = meeting_loop.generate_compact_base(
                self.main, self.out, "m1", "t", front_tokens=5000, keep_tail=200)
        self.assertEqual(err, "")
        self.assertEqual(len(seen), 1)                  # 只有 compact 一次
        cfg = os.path.join(self.tmp, meeting_loop.COMPACT_CWD_NAME)
        for cmd, kw in seen:
            self.assertEqual(cmd[cmd.index("--thinking") + 1], "off")  # 不推理
            self.assertEqual(cmd[cmd.index("--session") + 1], self.out)
            self.assertEqual(kw["cwd"], cfg)            # 作用域仅限本进程
        with open(os.path.join(cfg, ".pi", "settings.json"), encoding="utf-8") as f:
            st = json.load(f)
        self.assertEqual(st["compaction"]["keepRecentTokens"], 200)
        self.assertEqual(stats["keep_tail"], 200)
        self.assertEqual(stats["front_tokens"], 5000)


if __name__ == "__main__":
    unittest.main()
