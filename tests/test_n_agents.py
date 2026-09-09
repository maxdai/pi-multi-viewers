"""N-agent 充分性测试（用户要求：LLM 阶段受内存限制最多 3 agent，
必须用 FakeAgent 测更多个数——N=4/5 验证协议在更大规模下正确）。

验证点：
1. 并发正确性：N 个 agent 并发发言，消息不丢、无死锁、每个 agent 都发言
2. 冻结级联：N 大时"全员 freezing → af → RR"仍能启动（或 max_rounds 兜底）
3. 完整处理：每个 agent 处理了其他 agent 的全部 message/freezing（
   assert_processed_complete，收束尾 pass/concluded 允许跳过）
"""

import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meeting_fs import run_git
from tests.test_meeting_concurrency import (
    setup_env, count_messages, spawn_agents, wait_all,
    assert_processed_complete,
)


class TestNAgents(unittest.TestCase):

    def _run(self, name, agents, sleep_map=None, crash_map=None,
             max_meeting=4, max_rr=10, timeout=300):
        """跑场景，返回 (base, bare, all_exit, alive)。由调用者清理。"""
        base, bare, work_dirs = setup_env(name, agents)
        procs, _ = spawn_agents(work_dirs, agents, sleep_map or {},
                                crash_map or {}, max_meeting, max_rr)
        all_exit, alive = wait_all(procs, timeout)
        return base, bare, all_exit, alive

    def _types_seen(self, bare):
        """bare 中出现的所有消息 type。"""
        types = set()
        from meeting_fs import is_message_file
        r = run_git(bare, "log", "--format=%H")
        commits = r.stdout.strip().splitlines()
        for c in commits:
            r2 = run_git(bare, "ls-tree", "-r", "--name-only", c)
            files = [l for l in r2.stdout.splitlines()
                     if is_message_file(l)]
            for f in files:
                r3 = run_git(bare, "show", f"{c}:{f}", check=False)
                for l in r3.stdout.splitlines():
                    if l.startswith("type:"):
                        types.add(l.split(":", 1)[1].strip())
        return types

    def _agent_files(self, bare, agents):
        r = run_git(bare, "ls-tree", "-r", "--name-only", "HEAD")
        files = r.stdout.strip().splitlines() or []
        return {a: [f for f in files if f.startswith(f"{a}/")]
                for a in agents}

    def test_four_agent_concurrency(self):
        """4-agent 均衡并发：全员发言、消息不丢、无死锁。"""
        agents = ["a", "b", "c", "d"]
        base, bare, all_exit, alive = self._run(
            "n4", agents, max_meeting=4, max_rr=10, timeout=240)
        try:
            self.assertTrue(all_exit, f"4-agent 有进程未退出: {alive}")
            self.assertEqual(alive, [], f"4-agent 有残留进程: {alive}")
            # 完整处理验证（用户 9960）
            assert_processed_complete(self, bare, base, agents)
            total = count_messages(bare, agents)
            self.assertGreaterEqual(total, 4, f"4-agent 消息不足: {total}")
            for a, files in self._agent_files(bare, agents).items():
                self.assertTrue(files, f"agent {a} 无消息")
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_five_agent_concurrency(self):
        """5-agent 均衡并发：全参与、无死锁。"""
        agents = ["a", "b", "c", "d", "e"]
        base, bare, all_exit, alive = self._run(
            "n5", agents, max_meeting=4, max_rr=10, timeout=240)
        try:
            self.assertTrue(all_exit, f"5-agent 有进程未退出: {alive}")
            self.assertEqual(alive, [])
            # 完整处理验证（用户 9960）
            assert_processed_complete(self, bare, base, agents)
            total = count_messages(bare, agents)
            self.assertGreaterEqual(total, 5, f"5-agent 消息不足: {total}")
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_four_agent_cascade(self):
        """4-agent 冻结级联：freezing/af 能启动。"""
        agents = ["a", "b", "c", "d"]
        base, bare, all_exit, alive = self._run(
            "n4c", agents, max_meeting=4, max_rr=10, timeout=300)
        try:
            self.assertTrue(all_exit)
            # 完整处理验证（用户 9960）
            assert_processed_complete(self, bare, base, agents)
            types = self._types_seen(bare)
            self.assertIn("message", types, "N=4 应有消息发言")
            self.assertTrue(
                types.intersection({"freezing", "all-freezing"}),
                f"N=4 未出现冻结级联: {types}")
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_five_agent_cascade(self):
        """5-agent 冻结级联：更大规模下级联仍能启动。"""
        agents = ["a", "b", "c", "d", "e"]
        base, bare, all_exit, alive = self._run(
            "n5c", agents, max_meeting=4, max_rr=12, timeout=360)
        try:
            self.assertTrue(all_exit)
            # 完整处理验证（用户 9960）
            assert_processed_complete(self, bare, base, agents)
            types = self._types_seen(bare)
            self.assertIn("message", types, "N=5 应有消息发言")
            self.assertTrue(
                types.intersection({"freezing", "all-freezing"}),
                f"N=5 未出现冻结级联: {types}")
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_four_agent_crash(self):
        """4-agent + 崩溃叠加：崩溃 agent 恢复、其余不卡死。"""
        agents = ["a", "b", "c", "d"]
        crash_map = {"a": 0.3}
        base, bare, all_exit, alive = self._run(
            "n4cr", agents, {}, crash_map, max_meeting=4, max_rr=10, timeout=300)
        try:
            self.assertTrue(all_exit, f"崩溃+N 有进程未退出: {alive}")
            self.assertEqual(alive, [])
            # 完整处理验证（用户 9960）——崩溃场景下成功轮次仍应覆盖
            assert_processed_complete(self, bare, base, agents)
            total = count_messages(bare, agents)
            self.assertGreaterEqual(total, 4, f"崩溃+N 消息不足: {total}")
            # a（崩溃率 30%）仍应至少成功发言一次
            files_a = self._agent_files(bare, agents)["a"]
            self.assertTrue(files_a, "崩溃 agent a 从未成功提交")
        finally:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
