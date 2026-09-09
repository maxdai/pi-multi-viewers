"""meeting v2 状态机测试（全新，按设计文档 v2）。

验证新语义：
1. 发言锁：写 freezing 后不再发 message（meeting 阶段）
2. 配额耗尽 → 确定性 freezing（不经过 LLM）
3. 双条件：should_write_af（宽松）先于 can_start_rr（严格）
4. 无静默铁律：触发轮必产出（无产出重试）
5. RR 阶段消息带 next（轮转链）
6. RR 能产 pass（收尾路径）
7. 完整链路：发言 → 冻结 → af → starter pass → 全员 pass → concluded
"""

import os
import json
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meeting_fs import run_git
from tests.test_meeting_concurrency import setup_env, types_at_head


class TestFullChain(unittest.TestCase):
    """端到端完整链路（FakeAgent 多进程，2-agent）。"""

    def _run(self, name, agents, max_meeting=2, max_rr=5, timeout=120):
        from tests.test_meeting_concurrency import spawn_agents, wait_all
        base, bare, wd = setup_env(name, agents)
        procs, _ = spawn_agents(wd, agents, {}, {}, max_meeting, max_rr)
        all_exit, alive = wait_all(procs, timeout)
        return base, bare, all_exit, alive

    def test_two_agent_full_chain(self):
        """2-agent 完整链路：发言 → 配额冻结 → af → RR → concluded。"""
        base, bare, all_exit, alive = self._run("v2-chain", ["a", "b"])
        try:
            self.assertTrue(all_exit, f"进程未退出: {alive}")
            types = types_at_head(bare)
            self.assertIn("concluded", types,
                          f"应出现 concluded: {types}")
            # result.md 产物必须生成（继承主协议 7.4：收尾 = result.md + concluded）
            r = run_git(bare, "ls-tree", "-r", "--name-only", "HEAD",
                        check=False)
            self.assertIn("result.md", r.stdout,
                          "收尾未生成 result.md（设计疏漏回归）")
            # #1（review6）：正常收尾路径应为共识达成（非 quota 兜底）——
            # 判定失效恒走 quota 收尾会让此断言红（区分两路径）
            r2 = run_git(bare, "show", "HEAD:result.md", check=False)
            self.assertIn("共识达成", r2.stdout,
                          f"result.md 非共识收尾（判定失效?）: {r2.stdout[:80]}")
            self.assertIn("all-freezing", types,
                          f"应出现 af: {types}")
            self.assertIn("freezing", types,
                          f"应出现 freezing: {types}")
            # #2（review6）：per-agent 序列断言——发言锁语义 = freezing 后
            # 不再发 meeting message（确定性，非低概率联合检测）。
            # 替代原 meeting_msgs≤4（配额+锁联合行为，删锁后 ~5% 才红）
            # 实现：对每个 agent 的消息按序号排序，第一条 freezing 之后
            # 不得再有 type=message 且 mode=meeting 的消息
            import collections
            by_agent = collections.defaultdict(list)
            from meeting_fs import is_message_file
            r = run_git(bare, "ls-tree", "-r", "--name-only", "HEAD")
            for f in r.stdout.strip().splitlines():
                if is_message_file(f):
                    r3 = run_git(bare, "show", f"HEAD:{f}", check=False)
                    by_agent[f.split("/")[0]].append((int(f.split("/")[1][:4]), r3.stdout))
            for agent, msgs in by_agent.items():
                msgs.sort()
                frozen_at = None
                for seq, content in msgs:
                    t = ""
                    m = ""
                    for line in content.splitlines():
                        if line.startswith("type:"): t = line.split(":", 1)[1].strip()
                        elif line.startswith("mode:"): m = line.split(":", 1)[1].strip()
                    if frozen_at is None and t == "freezing":
                        frozen_at = seq
                    if frozen_at is not None and t == "message" and m == "meeting":
                        self.fail(
                            f"{agent} freezing（#{frozen_at}）后又发 meeting message "
                            f"（#{seq}）——发言锁失效")
            # #4（review6）：单向流 mode 链单调 + next 链完整性
            # mode 链：HEAD 树消息的 mode 按出现顺序不回退
            # （meeting→all-freezing→round-robin→concluded）——
            # 单向流设计 11.8：阶段只前进不后退
            # next 链：RR 阶段每条消息 next=order[(i+1)%n]（轮转链确定性补写）
            order = ["a", "b"]
            mode_order = ["meeting", "all-freezing", "round-robin", "concluded"]
            seen_modes = []
            from meeting_fs import is_message_file
            r = run_git(bare, "ls-tree", "-r", "--name-only", "HEAD")
            files = sorted([f for f in r.stdout.strip().splitlines()
                            if is_message_file(f)])
            for f in files:
                r3 = run_git(bare, "show", f"HEAD:{f}", check=False)
                t = m = nxt = ""
                for line in r3.stdout.splitlines():
                    if line.startswith("type:"): t = line.split(":", 1)[1].strip()
                    elif line.startswith("mode:"): m = line.split(":", 1)[1].strip()
                    elif line.startswith("next:"): nxt = line.split(":", 1)[1].strip()
                if m and m not in seen_modes:
                    # mode 首次出现必须按顺序（不回退）
                    if mode_order.index(m) < len(seen_modes):
                        self.fail(f"mode 链回退: {seen_modes} → {m}（单向流破坏）")
                    seen_modes.append(m)
                if m == "round-robin" and t == "pass":
                    # next 链：RR pass 必须带 next=下一位
                    agent = f.split("/")[0]
                    exp = order[(order.index(agent) + 1) % len(order)]
                    self.assertEqual(
                        nxt, exp,
                        f"RR pass {f} next 应为 {exp}（轮转链）实际 {nxt}")
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_two_agent_rr_pass(self):
        """RR 阶段能产 pass 并收尾（v1 的死循环已修）。"""
        base, bare, all_exit, alive = self._run("v2-rr", ["a", "b"])
        try:
            self.assertTrue(all_exit, f"进程未退出: {alive}")
            types = types_at_head(bare)
            self.assertIn("pass", types, f"RR 应产 pass: {types}")
            self.assertIn("concluded", types, f"应收尾: {types}")
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_seen_at_loop_filled(self):
        """loop 填写的 seen_at 正确性（FakeAgent 完整链路，用户 9915）。

        seen_at 由 loop 写（validate_and_fix 补全 / write_protocol_signal
        写前取 head）——FakeAgent responder 只写 type，不写 seen_at，
        所以最终消息的 seen_at 全是 loop 的产物，可直接验证（无需 LLM）。

        断言：每条消息都有非空 seen_at，且它是该消息 commit 的祖先
        （生成时 HEAD 必在历史中；并发下 rebase 会改 parent，所以用
        merge-base --is-ancestor 而非 parent 相等）。
        """
        base, bare, all_exit, alive = self._run("v2-seen", ["a", "b"])
        try:
            self.assertTrue(all_exit, f"进程未退出: {alive}")
            from meeting_fs import is_message_file
            r = run_git(bare, "ls-tree", "-r", "--name-only", "HEAD")
            msg_files = [f for f in r.stdout.strip().splitlines()
                         if is_message_file(f)]
            self.assertGreater(len(msg_files), 0, "无消息文件")
            for f in msg_files:
                # 找到引入该消息的 commit（git log -- 该文件，第一个 = 最后修改）
                rl = run_git(bare, "log", "--format=%H", "--all", "--", f,
                             check=False)
                commit = rl.stdout.strip().splitlines()[0] if rl.stdout.strip() else ""
                self.assertTrue(commit, f"找不到 {f} 的 commit")
                # 读消息 seen_at
                rs = run_git(bare, "show", f"{commit}:{f}", check=False)
                seen_at = ""
                for line in rs.stdout.splitlines():
                    if line.startswith("seen_at:"):
                        seen_at = line.split(":", 1)[1].strip()
                        break
                self.assertTrue(seen_at, f"{f} 缺 seen_at（loop 未填?）")
                # seen_at 必须是有效 commit 且是消息 commit 的祖先
                ra = run_git(bare, "merge-base", "--is-ancestor", seen_at,
                             commit, check=False)
                self.assertEqual(
                    ra.returncode, 0,
                    f"{f} 的 seen_at {seen_at[:8]} 不是 commit {commit[:8]} 的祖先"
                    f"（loop 填错）")
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_processed_complete(self):
        """完整处理验证（用户 9960/9971）：每个 agent 处理了所有其他 agent
        的讨论消息（message/freezing），无遗漏。

        复用公共断言 assert_processed_complete（与 concurrency/n_agents
        同逻辑，避免内联重复）。

        - 断言：其他 agent 的 message/freezing 全部 ∈ processed
          （讨论核心，必须处理）
        - 允许跳过：其他 agent 的 pass/concluded（收束尾——自己最后一次
          响应后他人发的流程消息，协议不唤醒，用户 9971/9982 特例）
        - 多次运行自然覆盖异常场景（上来就 freezing / freezing 后发言 /
          pass 后发言等，用户 10122/10152：随机次数够各种情况自然出现，
          实测 10168/10172 确认：首启 freezing 2/4、freezing 后又发言
          5/5、pass 后又发言 5/5——无需控制场景）
        """
        base, bare, all_exit, alive = self._run("v2-proc", ["a", "b"])
        try:
            self.assertTrue(all_exit, f"进程未退出: {alive}")
            from tests.test_meeting_concurrency import assert_processed_complete
            assert_processed_complete(self, bare, base, ["a", "b"])
        finally:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
