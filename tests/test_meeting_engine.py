"""meeting_engine 单元测试——E1-E20 逐 API 覆盖（API 清单基准，2026-09-01）。

E21 agent_loop 是组合对象（分支行为由 test_meeting_v2 集成全链 +
test_flow_composition 组合链覆盖），本文件不重复整体测试，但覆盖
其关键判定依赖（E7/E8/E9/E10/E17 等）。
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

from meeting_engine import (
    participants, result_writer, _each_agent_messages, _cat_batch,
    _each_agent_last, aggregate_mode, rr_next_speaker, rr_active_count,
    _meeting_speak_count, human_msg_count, _produced, write_af_if_no_rr,
    write_protocol_signal, respond_with_fallback, commit_new_files,
    _is_committed, _stall_elapsed, _result_md_valid, finalize_discussion,
    _commit_result_md,
    setup_commit,
    new_messages_with_meta,
    read_point,
)
from meeting_fs import git_head, git_pull, git_commit, write_message, next_msg_id


def make_env(participants_=("a", "b"), rw="b"):
    """建讨论环境（bare + work + protocol.json + setup commit）。"""
    tmp = tempfile.mkdtemp(prefix="eng-test-")
    base = os.path.join(tmp, "disc")
    bare = os.path.join(base, "repo.git")
    os.makedirs(base)
    subprocess.run(["git", "init", "--bare", bare], check=True,
                   capture_output=True)
    works = {}
    for p in participants_:
        w = os.path.join(base, f"work-{p}")
        subprocess.run(["git", "clone", bare, w], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=w, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=w, check=True)
        works[p] = w
    proto = {"mode": "meeting", "protocol_version": 2, "topic": "t",
             "participants": list(participants_), "resultWriter": rw,
             "maxMeetingRounds": 10, "maxRRRounds": 7}
    with open(os.path.join(works[participants_[0]], "protocol.json"), "w") as f:
        json.dump(proto, f)
    subprocess.run(["git", "add", "-A"], cwd=works[participants_[0]],
                   check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "discuss: setup"],
                   cwd=works[participants_[0]], check=True, capture_output=True)
    subprocess.run(["git", "push", "origin", "HEAD"], cwd=works[participants_[0]],
                   check=True, capture_output=True)
    for p in participants_[1:]:
        subprocess.run(["git", "pull"], cwd=works[p], check=True,
                       capture_output=True)
    return tmp, base, bare, works


def write_and_commit(work, agent, n, fm_extra=None, body="正文"):
    """写一条消息并 commit+push（生产写路径形状，写前 pull——测试方法论 1）。"""
    subprocess.run(["git", "pull", "--rebase", "--autostash"], cwd=work,
                   check=True, capture_output=True)
    fm = {"from": agent, "type": "message", "mode": "meeting"}
    if fm_extra:
        fm.update(fm_extra)
    mid = f"{n:04d}"
    path = f"{agent}/{mid}.md"
    write_message(work, path, fm, body)
    git_commit(work, [path], f"discuss: {agent}/{mid}")
    subprocess.run(["git", "push", "origin", "HEAD"], cwd=work, check=True,
                   capture_output=True)
    return path


class TestProtocolRead(unittest.TestCase):
    """E1-E2。"""

    def test_participants_and_result_writer(self):
        tmp, base, bare, works = make_env()
        try:
            self.assertEqual(participants(works["a"]), ["a", "b"])
            self.assertEqual(result_writer(works["a"]), "b")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_result_writer_default_last(self):
        # protocol 无 resultWriter → 最后参与者
        tmp, base, bare, works = make_env(participants_=("a", "b", "c"))
        try:
            proto_path = os.path.join(works["a"], "protocol.json")
            with open(proto_path) as f:
                proto = json.load(f)
            del proto["resultWriter"]
            with open(proto_path, "w") as f:
                json.dump(proto, f)
            subprocess.run(["git", "commit", "-am", "x"], cwd=works["a"],
                           check=True, capture_output=True)
            subprocess.run(["git", "push"], cwd=works["a"], check=True,
                           capture_output=True)
            self.assertEqual(result_writer(works["a"]), "c")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestBareRead(unittest.TestCase):
    """E3-E6。"""

    def test_each_agent_messages_empty(self):
        tmp, base, bare, works = make_env()
        try:
            self.assertEqual(_each_agent_messages(bare, ["a", "b"]),
                             {"a": [], "b": []})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_each_agent_messages_assembly_and_order(self):
        tmp, base, bare, works = make_env()
        try:
            write_and_commit(works["a"], "a", 1)
            write_and_commit(works["b"], "b", 1)
            write_and_commit(works["a"], "a", 2)
            msgs = _each_agent_messages(bare, ["a", "b"])
            self.assertEqual([m["type"] for m in msgs["a"]],
                             ["message", "message"])
            self.assertEqual(len(msgs["b"]), 1)
            self.assertIn("from", msgs["a"][0])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_cat_batch_chinese_content(self):
        """E4：二进制字节读——中文消息不破坏块对齐（R1 根因回归）。"""
        tmp, base, bare, works = make_env()
        try:
            write_and_commit(works["a"], "a", 1, body="中文正文内容测试\n第二行")
            write_and_commit(works["b"], "b", 1, body="hello ascii")
            paths = ["a/0001.md", "b/0001.md"]
            contents = _cat_batch(bare, paths)
            self.assertIn("中文正文内容测试", contents["a/0001.md"])
            self.assertIn("hello ascii", contents["b/0001.md"])
            # missing 路径跳过
            self.assertEqual(_cat_batch(bare, ["a/9999.md"]), {})
            self.assertEqual(_cat_batch(bare, []), {})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_each_agent_last(self):
        tmp, base, bare, works = make_env()
        try:
            # 无消息 → None
            self.assertEqual(_each_agent_last(bare, ["a", "b"]),
                             {"a": None, "b": None})
            write_and_commit(works["a"], "a", 1,
                             fm_extra={"next": "b", "seen_at": "h1"})
            lasts = _each_agent_last(bare, ["a", "b"])
            self.assertEqual(lasts["a"], {"type": "message", "mode": "meeting",
                                          "next": "b"})
            self.assertEqual(lasts["b"], None)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_aggregate_mode_engine(self):
        tmp, base, bare, works = make_env()
        try:
            self.assertEqual(aggregate_mode(bare, ["a", "b"]), "meeting")
            write_and_commit(works["a"], "a", 1,
                             fm_extra={"type": "freezing", "mode": "meeting"})
            write_and_commit(works["b"], "b", 1,
                             fm_extra={"type": "freezing", "mode": "meeting"})
            self.assertEqual(aggregate_mode(bare, ["a", "b"]), "all-freezing")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestRR(unittest.TestCase):
    """E7-E8。"""

    def test_rr_next_speaker_head_participant(self):
        """HEAD 是参与者消息 → 原语义（git log -1 + 最后消息文件 next）。"""
        tmp, base, bare, works = make_env()
        try:
            write_and_commit(works["a"], "a", 1,
                             fm_extra={"mode": "round-robin", "type": "pass",
                                       "next": "b"})
            self.assertEqual(rr_next_speaker(bare, ["a", "b"]), "b")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_rr_next_speaker_head_human_fallback(self):
        """HEAD 是 human 插话 → 逐 commit 回退找最近参与者消息的 next。"""
        tmp, base, bare, works = make_env(participants_=("a", "b", "c"))
        try:
            write_and_commit(works["a"], "a", 1,
                             fm_extra={"mode": "round-robin", "type": "pass",
                                       "next": "b"})
            # human 插话（work-human 不存在则直接用目录写）
            human_dir = os.path.join(base, "work-human")
            os.makedirs(human_dir, exist_ok=True)
            subprocess.run(["git", "clone", bare, human_dir], check=True,
                           capture_output=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=human_dir,
                           check=True)
            subprocess.run(["git", "config", "user.email", "t@t"],
                           cwd=human_dir, check=True)
            subprocess.run(["git", "pull"], cwd=human_dir, check=True,
                           capture_output=True)
            write_and_commit(human_dir, "human", 1)
            # HEAD 是 human → 回退找到 a 的 next=b
            self.assertEqual(rr_next_speaker(bare, ["a", "b", "c"]), "b")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_rr_next_speaker_no_participant_msg(self):
        """往前无参与者消息（只有 human）→ None。"""
        tmp, base, bare, works = make_env()
        try:
            human_dir = os.path.join(base, "work-human")
            os.makedirs(human_dir, exist_ok=True)
            subprocess.run(["git", "clone", bare, human_dir], check=True,
                           capture_output=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=human_dir,
                           check=True)
            subprocess.run(["git", "config", "user.email", "t@t"],
                           cwd=human_dir, check=True)
            write_and_commit(human_dir, "human", 1)
            self.assertIsNone(rr_next_speaker(bare, ["a", "b"]))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_rr_next_speaker_bad_frontmatter_none(self):
        """HEAD 参与者消息但 frontmatter 不可用 → None。"""
        tmp, base, bare, works = make_env()
        try:
            # 写一条无 frontmatter 的消息
            os.makedirs(os.path.join(works["a"], "a"))
            with open(os.path.join(works["a"], "a/0001.md"), "w") as f:
                f.write("no frontmatter here\n")
            git_commit(works["a"], ["a/0001.md"], "discuss: a/0001")
            subprocess.run(["git", "push"], cwd=works["a"], check=True,
                           capture_output=True)
            self.assertIsNone(rr_next_speaker(bare, ["a", "b"]))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_rr_active_count(self):
        tmp, base, bare, works = make_env()
        try:
            write_and_commit(works["a"], "a", 1,
                             fm_extra={"mode": "round-robin", "type": "pass"})
            write_and_commit(works["a"], "a", 2,
                             fm_extra={"mode": "round-robin", "type": "pass"})
            write_and_commit(works["a"], "a", 3,
                             fm_extra={"mode": "meeting", "type": "message"})
            msgs = _each_agent_messages(bare, ["a", "b"])
            self.assertEqual(rr_active_count(msgs, ["a", "b"]), 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestQuota(unittest.TestCase):
    """E9-E11。"""

    def test_meeting_speak_count(self):
        tmp, base, bare, works = make_env()
        try:
            write_and_commit(works["a"], "a", 1)  # message/meeting
            write_and_commit(works["a"], "a", 2,
                             fm_extra={"type": "freezing", "mode": "meeting"})
            write_and_commit(works["a"], "a", 3,
                             fm_extra={"type": "message", "mode": "round-robin"})
            msgs = _each_agent_messages(bare, ["a", "b"])
            # 只数 mode==meeting 且 type==message
            self.assertEqual(_meeting_speak_count(msgs, "a"), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_human_msg_count(self):
        tmp, base, bare, works = make_env()
        try:
            self.assertEqual(human_msg_count(bare), 0)
            human_dir = os.path.join(base, "work-human")
            os.makedirs(human_dir, exist_ok=True)
            subprocess.run(["git", "clone", bare, human_dir], check=True,
                           capture_output=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=human_dir,
                           check=True)
            subprocess.run(["git", "config", "user.email", "t@t"],
                           cwd=human_dir, check=True)
            write_and_commit(human_dir, "human", 1)
            write_and_commit(human_dir, "human", 2)
            write_and_commit(works["a"], "a", 1)
            self.assertEqual(human_msg_count(bare), 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_produced(self):
        tmp, base, bare, works = make_env()
        try:
            self.assertFalse(_produced(works["a"], "a", 0))
            write_and_commit(works["a"], "a", 1)
            self.assertTrue(_produced(works["a"], "a", 0))
            self.assertFalse(_produced(works["a"], "a", 1))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestProtocolSignal(unittest.TestCase):
    """E12-E13。"""

    def test_write_protocol_signal_freezing(self):
        tmp, base, bare, works = make_env()
        try:
            head = git_head(works["a"])
            write_protocol_signal(works["a"], "a", "freezing", "meeting")
            msgs = _each_agent_messages(bare, ["a", "b"])
            fm = msgs["a"][-1]
            self.assertEqual(fm["type"], "freezing")
            self.assertEqual(fm["mode"], "meeting")
            self.assertEqual(fm["from"], "a")
            self.assertEqual(fm["to"], "all")
            # 首条 loop 消息 seen_at 兜底 = 讨论起点（仓库根 commit）
            # 2026-09-03 改：原 head 兜底会把从未读过的并发消息虚假已读
            self.assertEqual(fm["seen_at"], head)  # make_env 后 head 即 setup
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_write_protocol_signal_no_history_falls_to_setup(self):
        """无历史兜底到讨论起点而非 head（2026-09-03 修复）。

        场景：a 首启即全崩（从未 responder 成功，read_point=""），
        代写 freezing 时 b 已 push 并发消息 M——
        旧行为 seen_at=含 M 的 head → M 被虚假已读永不处理（flaky 根因）；
        新行为 seen_at=setup 起点 → M 仍在下次唤醒 meta 中补处理。
        """
        tmp, base, bare, works = make_env()
        try:
            # b 先写并发消息 M（推进 head）
            write_message(works["b"], "b/0001.md",
                          {"from": "b", "type": "message"}, "M")
            commit_new_files(works["b"], "b", git_head(works["b"]), "meeting")
            m_head = git_head(bare)  # bare 已含 M（a 未 pull，work-a head 旧）
            # a 首次写协议信号（无历史）
            write_protocol_signal(works["a"], "a", "freezing", "meeting")
            msgs = _each_agent_messages(bare, ["a", "b"])
            fm = msgs["a"][-1]
            # seen_at = 起点（setup commit），不是含 M 的 head
            setup = setup_commit(works["a"])
            self.assertEqual(fm["seen_at"], setup)
            self.assertNotEqual(fm["seen_at"], m_head)
            # M 仍可被 a 看到（read_point=setup → meta 含 M）
            meta = new_messages_with_meta(works["a"],
                                          read_point(works["a"], "a"), "a")
            self.assertIn("b/0001.md", [m["path"] for m in meta])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_write_protocol_signal_pass_with_next(self):
        tmp, base, bare, works = make_env()
        try:
            write_protocol_signal(works["a"], "a", "pass", "round-robin", "b")
            msgs = _each_agent_messages(bare, ["a", "b"])
            fm = msgs["a"][-1]
            self.assertEqual(fm["mode"], "round-robin")
            self.assertEqual(fm["next"], "b")
            # bare 可见（push 成功）
            self.assertEqual(_each_agent_last(bare, ["a", "b"])["a"]["type"],
                             "pass")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_write_af_if_no_rr_writes(self):
        tmp, base, bare, works = make_env()
        try:
            write_af_if_no_rr(bare, works["a"], "a", ["a", "b"])
            msgs = _each_agent_messages(bare, ["a", "b"])
            self.assertEqual(msgs["a"][-1]["type"], "all-freezing")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_write_af_if_no_rr_skips_when_rr_started(self):
        """bare 已有 RR（任一最后一条 mode==round-robin）→ 放弃补写。"""
        tmp, base, bare, works = make_env()
        try:
            write_and_commit(works["a"], "a", 1,
                             fm_extra={"mode": "round-robin", "type": "pass"})
            write_af_if_no_rr(bare, works["b"], "b", ["a", "b"])
            msgs = _each_agent_messages(bare, ["a", "b"])
            # b 不应写 af
            self.assertEqual(len(msgs["b"]), 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestRespondFallback(unittest.TestCase):
    """E14 respond_with_fallback。"""

    class WritingResp:
        """responder：每次都写一条消息（产出）。"""

        def __call__(self, workdir, agent, head, meta, is_first, rr_turn,
                     retry, finalizing=False, finalize_reason="consensus"):
            mid = next_msg_id(workdir, agent)
            write_message(workdir, f"{agent}/{mid}.md",
                          {"from": agent, "type": "message"}, "r")
            return True

    class SilentResp:
        """responder：从不写（无产出 → 重试 → 代写）。"""

        def __init__(self):
            self.calls = 0

        def __call__(self, *a, **k):
            self.calls += 1
            return True

    def test_produced_returns_true(self):
        tmp, base, bare, works = make_env()
        try:
            head = git_head(works["a"])
            ok = respond_with_fallback(works["a"], "a", self.WritingResp(),
                                       head, [], True, False, 0, ["a", "b"])
            self.assertTrue(ok)
            msgs = _each_agent_messages(bare, ["a", "b"])
            self.assertEqual(len(msgs["a"]), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_production_retries_then_loop_write_meeting(self):
        """无产出 → 重试 MAX_RETRY → 代写 freezing → 返回 False。"""
        tmp, base, bare, works = make_env()
        try:
            head = git_head(works["a"])
            resp = self.SilentResp()
            ok = respond_with_fallback(works["a"], "a", resp, head, [],
                                       True, False, 0, ["a", "b"])
            self.assertFalse(ok, "代写 = 无产出（不耗配额）")
            self.assertEqual(resp.calls, 1 + 3)  # 1 次 + MAX_RETRY 重试
            msgs = _each_agent_messages(bare, ["a", "b"])
            self.assertEqual(msgs["a"][-1]["type"], "freezing")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_production_rr_writes_pass_with_next(self):
        """RR 轮次无产出 → 代写 pass（带 next 轮转链）。"""
        tmp, base, bare, works = make_env()
        try:
            head = git_head(works["a"])
            resp = self.SilentResp()
            ok = respond_with_fallback(works["a"], "a", resp, head, [],
                                       False, True, 0, ["a", "b"])
            self.assertFalse(ok)
            msgs = _each_agent_messages(bare, ["a", "b"])
            fm = msgs["a"][-1]
            self.assertEqual(fm["type"], "pass")
            self.assertEqual(fm["mode"], "round-robin")
            self.assertEqual(fm["next"], "b")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestCommitNewFiles(unittest.TestCase):
    """E15-E16。"""
    def test_commit_normal(self):
        tmp, base, bare, works = make_env()
        try:
            head = git_head(works["a"])
            # responder 写最小 frontmatter
            write_message(works["a"], "a/0001.md",
                          {"from": "a", "type": "message"}, "正文")
            ok = commit_new_files(works["a"], "a", head, "meeting")
            self.assertTrue(ok)
            msgs = _each_agent_messages(bare, ["a", "b"])
            fm = msgs["a"][0]
            self.assertEqual(fm["from"], "a")
            self.assertEqual(fm["mode"], "meeting")
            self.assertEqual(fm["seen_at"], head)
            self.assertEqual(fm["to"], "all")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_commit_rr_adds_next(self):
        """RR 阶段 pass/message 无条件补 next（轮转链）。"""
        tmp, base, bare, works = make_env()
        try:
            head = git_head(works["a"])
            write_message(works["a"], "a/0001.md",
                          {"from": "a", "type": "pass"}, "p")
            commit_new_files(works["a"], "a", head, "round-robin")
            msgs = _each_agent_messages(bare, ["a", "b"])
            self.assertEqual(msgs["a"][0]["next"], "b")
            # 违规 message 也补 next（防御）
            write_message(works["b"], "b/0001.md",
                          {"from": "b", "type": "message"}, "m")
            commit_new_files(works["b"], "b", head, "round-robin")
            msgs = _each_agent_messages(bare, ["a", "b"])
            self.assertEqual(msgs["b"][0]["next"], "a")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_commit_no_frontmatter_deletes(self):
        """无 frontmatter/块不完整 → 删除等待重写（不提交坏消息）。

        返回值：new_files 非空即 True（即使全部被校验拦截删除——返回值
        被忽略，调用点用 _produced 判定产出；设计注释明确）。"""
        tmp, base, bare, works = make_env()
        try:
            head = git_head(works["a"])
            os.makedirs(os.path.join(works["a"], "a"))
            with open(os.path.join(works["a"], "a/0001.md"), "w") as f:
                f.write("no frontmatter\n")
            ok = commit_new_files(works["a"], "a", head, "meeting")
            self.assertTrue(ok, "new_files 非空即 True（即使全部被拦截）")
            # 文件被删除（不滞留）
            self.assertFalse(os.path.exists(
                os.path.join(works["a"], "a/0001.md")))
            self.assertEqual(_each_agent_messages(bare, ["a", "b"])["a"], [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_commit_invalid_type_deletes(self):
        """type 非法（引擎专用 type 走 LLM 路径）→ 删除拦截。"""
        tmp, base, bare, works = make_env()
        try:
            head = git_head(works["a"])
            write_message(works["a"], "a/0001.md",
                          {"from": "a", "type": "concluded"}, "x")
            commit_new_files(works["a"], "a", head, "meeting")
            self.assertFalse(os.path.exists(
                os.path.join(works["a"], "a/0001.md")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_commit_no_new_files(self):
        tmp, base, bare, works = make_env()
        try:
            self.assertFalse(commit_new_files(works["a"], "a",
                                              git_head(works["a"]), "meeting"))
            self.assertFalse(commit_new_files(works["a"], "nope",
                                              git_head(works["a"]), "meeting"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_is_committed(self):
        tmp, base, bare, works = make_env()
        try:
            self.assertFalse(_is_committed(works["a"], "a/0001.md"))
            write_and_commit(works["a"], "a", 1)
            self.assertTrue(_is_committed(works["a"], "a/0001.md"))
            # 已提交后覆盖修改 → 仍视为已提交（消息不可变，不重提交）
            with open(os.path.join(works["a"], "a/0001.md"), "a") as f:
                f.write("modified\n")
            self.assertTrue(_is_committed(works["a"], "a/0001.md"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestStall(unittest.TestCase):
    """E17-E18。"""

    def test_stall_no_messages(self):
        """讨论未开始（无消息文件）→ 不累计。"""
        tmp, base, bare, works = make_env()
        try:
            h = git_head(works["a"])
            sec, nh, nt = _stall_elapsed(bare, ["a", "b"], None, 0.0, h)
            self.assertEqual(sec, 0.0)
            self.assertEqual(nh, h)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_stall_accumulates_and_resets(self):
        tmp, base, bare, works = make_env()
        try:
            write_and_commit(works["a"], "a", 1)
            h = git_head(works["a"])
            # HEAD 未变 → 累计
            t0 = 100.0
            sec, nh, nt = _stall_elapsed(bare, ["a", "b"], h, t0, h)
            self.assertGreater(sec, 0)
            self.assertEqual(nh, h)
            # HEAD 变 → 重置
            write_and_commit(works["b"], "b", 1)
            h2 = git_head(bare)  # bare 的最新 HEAD（work-a 未 pull）
            sec2, nh2, nt2 = _stall_elapsed(bare, ["a", "b"], h, t0, h2)
            self.assertEqual(sec2, 0.0)
            self.assertEqual(nh2, h2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_result_md_valid(self):
        tmp = tempfile.mkdtemp(prefix="resmd-")
        try:
            p = os.path.join(tmp, "result.md")
            self.assertFalse(_result_md_valid(p))  # 不存在
            with open(p, "w") as f:
                f.write("短")
            self.assertFalse(_result_md_valid(p))  # < 50 字符
            with open(p, "w") as f:
                f.write("x" * 60)
            self.assertTrue(_result_md_valid(p))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestFinalize(unittest.TestCase):
    """E19-E20。"""

    class Resp:
        def __init__(self, ok_result=True):
            self.ok_result = ok_result
            self.final_calls = 0

        def __call__(self, workdir, agent, head, meta, is_first, rr_turn,
                     retry, finalizing=False, finalize_reason="consensus"):
            if finalizing:
                self.final_calls += 1
                if self.ok_result:
                    with open(os.path.join(workdir, "result.md"), "w") as f:
                        f.write("# 结论\n\n" + "内容" * 30)
                return True
            return True

    def test_finalize_normal(self):
        tmp, base, bare, works = make_env()
        try:
            resp = self.Resp()
            head = git_head(works["b"])
            ok = finalize_discussion(works["b"], "b", resp, head)
            self.assertTrue(ok)
            # result.md 提交 + concluded 落盘
            msgs = _each_agent_messages(bare, ["a", "b"])
            self.assertEqual(msgs["b"][-1]["type"], "concluded")
            r = subprocess.run(["git", "show", "HEAD:result.md"], cwd=bare,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0)
            self.assertIn("结论", r.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_finalize_retry_then_fallback(self):
        """responder 一直不写有效 result.md → 重试 → loop 兜底代写。"""
        tmp, base, bare, works = make_env()
        try:
            resp = self.Resp(ok_result=False)

            class NoWriteResp:
                def __call__(self, *a, **k):
                    return True

            ok = finalize_discussion(works["b"], "b", NoWriteResp(),
                                     git_head(works["b"]))
            self.assertTrue(ok)
            msgs = _each_agent_messages(bare, ["a", "b"])
            self.assertEqual(msgs["b"][-1]["type"], "concluded")
            r = subprocess.run(["git", "show", "HEAD:result.md"], cwd=bare,
                               capture_output=True, text=True)
            self.assertIn("兜底代写", r.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_commit_result_md_idempotent(self):
        """result.md 已提交无改动 → 跳过 commit（不抛 RuntimeError）。"""
        tmp, base, bare, works = make_env()
        try:
            with open(os.path.join(works["b"], "result.md"), "w") as f:
                f.write("# 结论\n\n" + "内容" * 30)
            _commit_result_md(works["b"], "b", "discuss: result.md")
            _commit_result_md(works["b"], "b", "discuss: result.md")  # 幂等
            msgs = _each_agent_messages(bare, ["a", "b"])
            self.assertEqual(msgs["b"], [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
