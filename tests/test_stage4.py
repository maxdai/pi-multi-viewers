"""阶段 4 测试：to 定向 + seen_at 陈旧检测（meeting_fs 层，确定性）。

另含两个历史 bug 回归类：
- TestParseFrontmatter（review5 A1）：frontmatter 块完整性（无开/缺闭合/正文不吞）
- TestCatBatch（用户 9343）：_cat_batch 二进制读中文不挂起
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meeting_fs import (
    git_head, git_pull, git_commit, git_push,
    write_message, read_message, commit_message,
    new_messages_with_meta,
)
from meeting_core import has_new_messages_for_me
from tests.test_meeting_concurrency import setup_env


class TestStage4(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp()
        _, self.bare, self.wd = setup_env("stage4", ["a", "b", "c"])
        self.agents = ["a", "b", "c"]
        for ag in self.agents:
            git_pull(self.wd[ag])
        self.setup_head = git_head(self.wd["a"])   # 真实 setup commit（初始读取点）

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def _push(self, agent, msg_id, fm):
        path = f"{agent}/{msg_id}.md"
        write_message(self.wd[agent], path, fm, "body")
        git_commit(self.wd[agent], [path], commit_message(agent, msg_id))
        git_push(self.wd[agent])
        for ag in self.agents:
            git_pull(self.wd[ag])

    def _meta_from(self, ag, rp):
        """从读取点 rp 取新消息元数据。"""
        return new_messages_with_meta(self.wd[ag], rp, git_head(self.wd[ag]))

    def test_to_all_default(self):
        """to 缺省 = all → 全员触发（除作者自己）。"""
        self._push("a", "0001", {"from": "a", "type": "message", "mode": "meeting",
                                 "seen_at": self.setup_head})
        for ag, expect in [("a", False), ("b", True), ("c", True)]:
            meta = self._meta_from(ag, self.setup_head)
            hit = has_new_messages_for_me(meta, ag)
            self.assertEqual(hit, expect, f"{ag} 缺省 to 结果 {hit} != {expect}")

    def test_to_all_explicit(self):
        """to: all → 全员触发。"""
        self._push("a", "0001", {"from": "a", "type": "message", "mode": "meeting",
                                 "seen_at": self.setup_head, "to": "all"})
        for ag, expect in [("a", False), ("b", True), ("c", True)]:
            meta = self._meta_from(ag, self.setup_head)
            hit = has_new_messages_for_me(meta, ag)
            self.assertEqual(hit, expect, f"{ag} to:all 结果 {hit} != {expect}")

    def test_stale_detection(self):
        """陈旧检测：消息 A 的 seen_at 之后有新提交 → A 标注 stale。"""
        # a 发第一条（seen_at = setup_head）
        self._push("a", "0001", {"from": "a", "type": "message", "mode": "meeting",
                                 "seen_at": self.setup_head})
        # b 在 a/0001 之后发第二条 → a/0001 的 seen_at(setup_head) 之后有新提交 → stale
        h2 = git_head(self.wd["b"])
        self._push("b", "0001", {"from": "b", "type": "message", "mode": "meeting",
                                 "seen_at": h2})
        # c 的视角：读取点 setup_head → 新消息 a/0001（stale=True，后有 b/0001）
        #   + b/0001（stale=False，最新）
        meta = self._meta_from("c", self.setup_head)
        for m in meta:
            if m["from"] == "a":
                self.assertTrue(m["stale"], f"a/0001 应在 setup 后有新提交 → stale，实际 {m}")
            elif m["from"] == "b":
                self.assertFalse(m["stale"], f"b/0001 是最新 → 不应 stale，实际 {m}")

    def test_stale_trigger_log(self):
        """陈旧消息不阻止触发（触发 = 存在别人的消息，stale 只是标注）。"""
        self._push("a", "0001", {"from": "a", "type": "message", "mode": "meeting",
                                 "seen_at": self.setup_head})
        self._push("b", "0001", {"from": "b", "type": "message", "mode": "meeting",
                                 "seen_at": git_head(self.wd["b"])})
        meta = self._meta_from("c", self.setup_head)
        self.assertTrue(has_new_messages_for_me(meta, "c"))


if __name__ == "__main__":
    unittest.main()


class TestParseFrontmatter(unittest.TestCase):
    """parse_frontmatter 根治（review5 A1 用户方案：先边界后解析）。

    旧实现"只查开头、遍历到文件尾"——缺闭合 --- 时返回部分解析结果，
    调用方 if not fm 判不出"完整 vs 残缺"→ 误当成功（A1 根因）。
    新实现：None = 无 frontmatter 或块不完整（不可用）；完整 → dict。
    """

    def test_complete_block(self):
        from meeting_fs import parse_frontmatter
        fm = parse_frontmatter("---\ntype: message\nfrom: a\n---\n正文\n")
        self.assertIsNotNone(fm)
        self.assertEqual(fm["type"], "message")
        self.assertEqual(fm["from"], "a")

    def test_no_opening(self):
        from meeting_fs import parse_frontmatter
        # 无开 ---（纯正文）→ None
        self.assertIsNone(parse_frontmatter("type: message\n正文\n"))

    def test_missing_closing(self):
        from meeting_fs import parse_frontmatter
        # 有开无闭（A1 根因场景）→ None（不可用，不再返回部分解析）
        self.assertIsNone(parse_frontmatter("---\ntype: message\nfrom: a\n正文\n"))

    def test_body_not_eaten(self):
        from meeting_fs import parse_frontmatter
        # 正文里的 key: value 行不应被当字段吞掉（A1 附带损坏）
        fm = parse_frontmatter("---\ntype: message\n---\n正文 key: value\n")
        self.assertNotIn("正文 key", fm)
        self.assertEqual(fm["type"], "message")


class TestCatBatch(unittest.TestCase):
    """_cat_batch 二进制读取（用户 9343 现场死锁修复）。

    根因：text=True 模式 read(size) 读字符数，cat-file 的 size 是字节数——
    中文 UTF-8 3 字节/字符 → 错位 → readline 阻塞挂起（真实讨论中文，
    FakeAgent 测试 ASCII 单字节所以之前没抓到——R1 根因复发）。
    修复：二进制按字节读 + decode + try/finally 保证 close/wait。
    """

    def _mk(self):
        import tempfile, shutil, subprocess
        d = tempfile.mkdtemp()
        try:
            subprocess.run(["git", "init", "-q", "--bare", d + "/repo.git"], check=True)
            w = d + "/work"
            subprocess.run(["git", "clone", "-q", d + "/repo.git", w], check=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=w, check=True)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=w, check=True)
            body = "这是中文内容测试消息，" * 10 + "\n"
            os.makedirs(w + "/a", exist_ok=True)
            with open(w + "/a/0001.md", "w") as f:
                f.write("---\ntype: message\nfrom: a\n---\n\n" + body)
            subprocess.run(["git", "add", "."], cwd=w, check=True)
            subprocess.run(["git", "commit", "-qm", "s1"], cwd=w, check=True)
            subprocess.run(["git", "push", "-q", "origin", "master"], cwd=w, check=True)
            return d
        except Exception:
            shutil.rmtree(d, ignore_errors=True)
            raise

    def test_cn_content_and_missing(self):
        import shutil
        from meeting_engine import _cat_batch
        d = self._mk()
        try:
            r = _cat_batch(d + "/repo.git", ["a/0001.md"])
            self.assertIn("这是中文内容测试消息", r["a/0001.md"])
            # missing path 跳过不异常
            r2 = _cat_batch(d + "/repo.git", ["a/0001.md", "x/no.md"])
            self.assertNotIn("x/no.md", r2)
        finally:
            shutil.rmtree(d, ignore_errors=True)


def _push_simple(wd, agent, msg_id):
    """写一条简单 message + commit + push（TestNextMsgId/TestStallElapsed 共用）。"""
    path = f"{agent}/{msg_id}.md"
    write_message(wd, path, {"type": "message", "from": agent}, "body")
    git_commit(wd, [path], commit_message(agent, msg_id))
    git_push(wd)


class TestNextMsgId(unittest.TestCase):
    """next_msg_id 接线（review6 #9：msg_path 双套序号，R1 家族）。

    loop 算 msg_path 告诉 LLM（meeting_loop.py:236-239）与 fake 自算
    （fake_agent.py:98）用同一函数 next_msg_id（meeting_fs:188）——
    序号计算逻辑一致，但此前零直接测试。验证：
    1. 正常递增：已有 0001-0003 → 下一个 0004
    2. 跳号：LLM 跳号（0001,0003）→ max+1=0004（非 len+1）
    3. 未提交孤儿不计入：已提交 0001，工作区有未提交 0005 → 仍 0002
    """

    def setUp(self):
        self.base = tempfile.mkdtemp()
        _, self.bare, self.wd = setup_env("stage4-nid", ["a", "b", "c"])
        self.agents = ["a", "b", "c"]
        for ag in self.agents:
            git_pull(self.wd[ag])

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_normal_increment(self):
        """正常递增：0001-0003 → 0004。"""
        from meeting_fs import next_msg_id
        w = self.wd["a"]
        for i in range(1, 4):
            _push_simple(w, "a", f"{i:04d}")
        self.assertEqual(next_msg_id(w, "a"), "0004", "正常递增应为 max+1")

    def test_skip_max_plus_one(self):
        """跳号：0001,0003 → max+1=0004（非 len+1=3，审核 G4）。"""
        from meeting_fs import next_msg_id
        w = self.wd["a"]
        _push_simple(w, "a", "0001")
        _push_simple(w, "a", "0003")
        self.assertEqual(next_msg_id(w, "a"), "0004", "跳号应 max+1")

    def test_uncommitted_orphan_ignored(self):
        """未提交孤儿不计入：已提交 0001，未提交 0005 → 仍 0002。

        next_msg_id 基于 git_ls_files（已提交）——孤儿唯一来源是 commit
        异常路径，覆盖无害（同一 agent 自己文件），序号复用正确语义。
        """
        from meeting_fs import next_msg_id
        w = self.wd["a"]
        _push_simple(w, "a", "0001")
        # 写一个未提交孤儿
        os.makedirs(w + "/a", exist_ok=True)
        with open(w + "/a/0005.md", "w") as f:
            f.write("---\ntype: message\nfrom: a\n---\n\norphan")
        self.assertEqual(next_msg_id(w, "a"), "0002", "孤儿不计入")

    def _push(self, agent, msg_id):
        path = f"{agent}/{msg_id}.md"
        write_message(self.wd[agent], path,
                      {"type": "message", "from": agent}, "body")
        git_commit(self.wd[agent], [path], commit_message(agent, msg_id))
        git_push(self.wd[agent])


class TestStallElapsed(unittest.TestCase):
    """stall 无进展判定（review6 #10：纯逻辑层验证）。

    _stall_elapsed（engine:417）是无进展累计——stall 的核心判定。
    直接测纯函数（注入真实 bare + head），比端到端触发稳定得多：
    端到端触发 stall 需要 loop 代写也失败（进程被杀），FakeAgent
    进程活着就有代写兜底（实测 silent responder 走完共识收尾）。

    验证（review5 M1 语义）：
    1. 讨论未开始（仅 setup）→ 不累计（返回 0 + 重置起点）
    2. head 变化（有新 commit）→ 重置（返回 0）
    3. head 不变（无进展）→ 累计时间差
    """

    def setUp(self):
        self.base = tempfile.mkdtemp()
        _, self.bare, self.wd = setup_env("stage4-stall", ["a", "b"])
        from meeting_fs import git_head
        self.head = git_head(self.wd["a"])

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_no_messages_no_accumulate(self):
        """讨论未开始（仅 setup commit，无消息文件）→ 不累计。"""
        from meeting_engine import _stall_elapsed
        import time
        # 讨论未开始：无消息文件 → 返回 0 + 重置（起点 = 当前时间）
        s, nh, nt = _stall_elapsed(self.bare, ["a", "b"], self.head,
                                   time.time() - 1000, self.head)
        self.assertEqual(s, 0.0, "讨论未开始不应累计")
        self.assertEqual(nh, self.head, "head 不变")

    def test_head_change_resets(self):
        """head 变化（有新 commit）→ 重置为 0。"""
        from meeting_engine import _stall_elapsed
        import time
        # 先造一条消息（讨论开始）
        _push_simple(self.wd["a"], "a", "0001")
        new_head = git_head(self.wd["a"])
        # head 变化：传入旧 head + 很久前的起点 → 应重置（有进展）
        s, nh, nt = _stall_elapsed(self.bare, ["a", "b"], self.head,
                                   time.time() - 1000, new_head)
        self.assertEqual(s, 0.0, "head 变化应重置")
        self.assertEqual(nh, new_head)

    def test_head_unchanged_accumulates(self):
        """head 不变（无进展）→ 累计时间差。"""
        from meeting_engine import _stall_elapsed
        import time
        _push_simple(self.wd["a"], "a", "0001")
        new_head = git_head(self.wd["a"])
        # 起点在 100s 前，head 一直没变 → 应累计 ~100s
        s, nh, nt = _stall_elapsed(self.bare, ["a", "b"], new_head,
                                   time.time() - 100, new_head)
        self.assertGreater(s, 90, f"无进展应累计时间差，实际 {s}")
        self.assertEqual(nh, new_head)
