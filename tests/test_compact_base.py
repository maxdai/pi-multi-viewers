"""定向摘要 fork 模式（决策 24）的单元测试——**零 LLM**。

覆盖三层：
  ① meeting_fs：`finalize_compaction_base`（剥快照 + 统计）与 `summary` 模式的切片
  ② meeting_loop：`generate_compact_base` 的 RPC 交互（假 Popen 回放三态）+ 首唤取源
  ③ 指令措辞：`build_summary_instructions` 必须带主题、且带"只陈述事实"的反框架句
"""

import json
import os
import shutil
import sys
import tempfile
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


class TestGenerateCompactBase(unittest.TestCase):
    """`generate_compact_base`：RPC 三态（成功 / compact 失败 / 超时）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mvgen-")
        self.main = os.path.join(self.tmp, "main.jsonl")
        self.out = os.path.join(self.tmp, "base.jsonl")
        _write_session(self.main, n_msgs=3, anchor="m1")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    class _Proc:
        def __init__(self, out, rc=0, timeout=False):
            self._out, self._rc, self._timeout = out, rc, timeout
            self.returncode = rc

        def communicate(self, payload=None, timeout=None):
            # 只在**第一次**抛（生产在超时后 kill + 再 communicate 收尸）
            if self._timeout:
                self._timeout = False
                raise __import__("subprocess").TimeoutExpired("pi", timeout)
            return self._out, ""

        def kill(self):
            self.returncode = -9

    def _patch_proc(self, proc):
        return mock.patch.object(meeting_loop.subprocess, "Popen",
                                 return_value=proc)

    def test_success_writes_stats_and_strips(self):
        ok = json.dumps({"id": "compact", "type": "response",
                         "command": "compact", "success": True})
        with self._patch_proc(self._Proc(ok + "\n")):
            stats, err = meeting_loop.generate_compact_base(
                self.main, self.out, "m1", "high", "测试主题")
        self.assertEqual(err, "")
        self.assertEqual(stats["model"], "m1")
        # base 落盘 + 快照已剥（finalize 真跑了）
        with open(self.out, encoding="utf-8") as f:
            base = [json.loads(l) for l in f if l.strip()]
        comp = [e for e in base if e.get("type") == "compaction"][0]
        self.assertNotIn("systemMessage", comp)
        # 记账文件
        with open(os.path.join(self.tmp, meeting_fs.COMPACT_BASE_STATS),
                  encoding="utf-8") as f:
            st = json.load(f)
        self.assertEqual(st["model"], "m1")
        self.assertIn("instructions", st)

    def test_compact_failure_is_reported(self):
        bad = json.dumps({"id": "compact", "type": "response",
                          "command": "compact", "success": False,
                          "error": "Nothing to compact (session too small)"})
        with self._patch_proc(self._Proc(bad + "\n", rc=1)):
            stats, err = meeting_loop.generate_compact_base(
                self.main, self.out, "m1", "high", "t")
        self.assertIsNone(stats)
        self.assertIn("compact 未成功", err)
        self.assertIn("Nothing to compact", err)

    def test_timeout_is_reported(self):
        with self._patch_proc(self._Proc("", timeout=True)):
            stats, err = meeting_loop.generate_compact_base(
                self.main, self.out, "m1", "high", "t")
        self.assertIsNone(stats)
        self.assertIn("超时", err)

    def test_no_response_is_failure(self):
        with self._patch_proc(self._Proc("not json\n")):
            stats, err = meeting_loop.generate_compact_base(
                self.main, self.out, "m1", "high", "t")
        self.assertIsNone(stats)
        self.assertIn("compact 未成功", err)

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


if __name__ == "__main__":
    unittest.main()
