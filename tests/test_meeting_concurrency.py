"""阶段 2 并发测试——多进程 FakeAgent 模拟（无 LLM）。

验证目标（协议层正确性，与 LLM 行为无关）：
1. 消息不丢：所有 FakeAgent 写的消息都进入 bare（可被其他进程看到）
2. 无死锁：所有进程都在超时内退出
3. 乱序容忍：快慢差异下并发提交不破坏 git（无分叉/无失败 push）
4. 崩溃恢复：crash 后读取点从最后 commit 的 seen_at 继续（不丢不重）

运行：python3 -m unittest tests.test_meeting_concurrency -v
"""

import os
import shutil
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meeting_fs import run_git, write_message

BASE = "/tmp/meeting-test"
SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fake_agent.py")


def setup_env(test_dir, agents):
    """创建 bare + work-* clones + 初始提交。返回 (bare_dir, work_dirs)。

    work_dirs 含固定 `human` 键 = work-human（helper 设计 §2.1：human 是
    附加的特殊 agent，另算，不占参与者名额）——human 消息一律从它写入
    （对齐阶段 2 sayer 的生产提交通道；借用参与者 work 写 human 消息是
    语义混淆，用户 2026-08-30 纠正）。
    """
    base = os.path.join(BASE, test_dir)
    if os.path.exists(base):
        shutil.rmtree(base)
    os.makedirs(base)

    # git 身份（/tmp 新仓库无全局身份；仓库级配置即可）
    def _ident(w):
        run_git(w, "config", "user.name", "meeting-test")
        run_git(w, "config", "user.email", "meeting-test@local")

    bare = os.path.join(base, "repo.git")
    run_git(base, "init", "--bare", bare)

    work_dirs = {}
    for i, a in enumerate(agents):
        w = os.path.join(base, f"work-{a}")
        run_git(base, "clone", bare, w)
        _ident(w)
        if i == 0:
            # 第一个 agent：用生产 gen_protocol 生成 setup 提交并 push
            # （review6 #6：测试环境对齐生产形状——手搓 protocol.json 导致
            # 配额固化/stall 等字段读取接线零测试）
            import json
            from start_discussion import gen_protocol
            proto = gen_protocol("test", agents, 4, 5)
            with open(os.path.join(w, "protocol.json"), "w") as f:
                json.dump(proto, f)
                f.write("\n")
            run_git(w, "add", "protocol.json")
            run_git(w, "commit", "-m", "discuss: setup")
            run_git(w, "push")
        else:
            # 后续 agent：clone 已含 setup（从 bare 继承），pull 同步即可
            run_git(w, "pull", "--rebase")
        work_dirs[a] = w

    # work-human：固定存在的提交通道（不占参与者名额，不跑 loop）
    wh = os.path.join(base, "work-human")
    run_git(base, "clone", bare, wh)
    _ident(wh)
    work_dirs["human"] = wh

    return base, bare, work_dirs


def write_msg(workdir, path, frontmatter, body="正文"):
    """测试通用：模拟 agent/human 的消息产物落 bare（write + commit + push）。

    写前 pull（对齐生产 commit_new_files 的写前同步——多 work 交替写时
    本地会落后，不 pull 则 push 被拒（fetch first）。测试失败三分类：
    这是测试装置问题，不是设计/实现问题——装置对齐生产形状后不再重踩。
    """
    run_git(workdir, "pull", "--rebase", "--autostash", check=False)
    write_message(workdir, path, frontmatter, body)
    run_git(workdir, "add", "--", path)
    run_git(workdir, "commit", "-m", f"discuss: {path}")
    run_git(workdir, "push")


def types_at_head(bare):
    """HEAD 树中消息类型分布（测试断言 helper，各测试文件共用）。"""
    from meeting_fs import is_message_file
    types = {}
    r = run_git(bare, "ls-tree", "-r", "--name-only", "HEAD")
    for f in r.stdout.strip().splitlines():
        if is_message_file(f):
            r3 = run_git(bare, "show", f"HEAD:{f}", check=False)
            for l in r3.stdout.splitlines():
                if l.startswith("type:"):
                    t = l.split(":", 1)[1].strip()
                    types[t] = types.get(t, 0) + 1
    return types


def spawn_agents(work_dirs, agents, sleep_map, crash_map, max_meeting=4,
                 max_rr=5):
    """spawn FakeAgent 进程（v2 双配额）。返回 (procs, logs)。"""
    procs = {}
    logs = {}
    for a in agents:
        w = work_dirs[a]
        logf = os.path.join(os.path.dirname(w), f"fake-{a}.log")
        logs[a] = logf
        mn, mx = sleep_map.get(a, (0.5, 3.0))
        cr = crash_map.get(a, 0.0)
        with open(logf, "w") as f:
            p = subprocess.Popen(
                [sys.executable, SCRIPT, w, a, str(mn), str(mx), str(cr),
                 str(max_meeting), str(max_rr)],
                stdout=f, stderr=subprocess.STDOUT,
            )
        procs[a] = p
    return procs, logs


def wait_all(procs, timeout=220):
    """等待所有进程退出。返回 (all_exited, alive_list)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        alive = [a for a, p in procs.items() if p.poll() is None]
        if not alive:
            return True, []
        time.sleep(0.5)
    return False, alive


def count_messages(bare, agents):
    """bare 中所有消息文件数。"""
    r = run_git(bare, "ls-tree", "-r", "--name-only", "HEAD")
    files = r.stdout.strip().splitlines() if r.stdout.strip() else []
    return len([f for f in files if any(f.startswith(a + "/") for a in agents)])


def assert_processed_complete(tc, bare, base, agents):
    """断言每个 agent 完整处理了其他 agent 的讨论消息（用户 9960/9971）。

    processed/<agent>.json = FakeAgent 每次唤醒收到的 meta paths（去重）。
    断言：其他 agent 的 message/freezing 全部 ∈ processed（讨论核心，必须
    处理）；允许跳过 pass/concluded（收束尾——自己最后一次响应后他人发的
    流程消息，协议不唤醒，用户 9971/9982 特例）。

    tc: TestCase 实例（用它的 assertEqual/assertTrue）
    """
    import json as _json
    from meeting_fs import is_message_file
    r = run_git(bare, "ls-tree", "-r", "--name-only", "HEAD")
    files = [f for f in r.stdout.strip().splitlines()
             if is_message_file(f)]
    msgs = {}
    for f in files:
        r3 = run_git(bare, "show", f"HEAD:{f}", check=False)
        t = ""
        for l in r3.stdout.splitlines():
            if l.startswith("type:"):
                t = l.split(":", 1)[1].strip()
                break
        msgs[f] = t
    for a in agents:
        pf = os.path.join(base, "processed", f"{a}.json")
        tc.assertTrue(os.path.exists(pf),
                      f"{a} 无 processed 记录（从未被唤醒?）")
        with open(pf) as f:
            rec = set(_json.load(f))
        others = [f for f in files
                  if not f.startswith(a + "/")
                  and msgs[f] in ("message", "freezing")]
        missing = [f for f in others if f not in rec]
        tc.assertEqual(
            missing, [],
            f"{a} 遗漏了其他 agent 的讨论消息: {missing}"
            f"（processed={sorted(rec)}）")


class TestConcurrency(unittest.TestCase):

    def _run_scenario(self, name, agents, sleep_map, crash_map,
                      max_meeting=4, max_rr=5, timeout=120):
        base, bare, work_dirs = setup_env(name, agents)
        procs, logs = spawn_agents(work_dirs, agents, sleep_map, crash_map,
                                   max_meeting, max_rr)
        all_exit, alive = wait_all(procs, timeout)
        if not all_exit:
            # 超时：终止残留子进程（防泄漏干扰后续测试）
            for p in procs.values():
                p.terminate()
            time.sleep(1)
            for p in procs.values():
                if p.poll() is None:
                    p.kill()
        return base, bare, procs, all_exit, alive

    def test_balanced_concurrency(self):
        """场景 1：均衡并发（全体 0.5-3s）——基本并发正确性。"""
        agents = ["a", "b", "c"]
        base, bare, procs, all_exit, alive = self._run_scenario(
            "balanced", agents, {}, {}, max_meeting=3, max_rr=8)
        try:
            self.assertTrue(all_exit, f"进程未退出: {alive}")
            # 完整处理验证（用户 9960）：每个 agent 处理了所有其他 agent 的
            # message/freezing（收束尾 pass/concluded 允许跳过）
            assert_processed_complete(self, bare, base, agents)
            # 消息不丢：每个 agent 在 bare 至少有 1 条消息
            r = run_git(bare, "ls-tree", "-r", "--name-only", "HEAD")
            files = r.stdout.strip().splitlines()
            from meeting_fs import is_message_file
            for a in agents:
                a_files = [f for f in files if f.startswith(f"{a}/")
                           and is_message_file(f)]
                self.assertGreaterEqual(len(a_files), 1,
                                        f"{a} 没有任何消息: {a_files}")
            self.assertGreaterEqual(count_messages(bare, agents), 3)
            # 收尾：result.md 由 responder 写（非兜底代写，审核#3）——
            # 验证正常收尾路径（fake 内容 >50 字节，不应触发 loop 兜底）
            r = run_git(bare, "show", "HEAD:result.md", check=False)
            if r.returncode == 0:
                self.assertNotIn("兜底代写", r.stdout)
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_fast_slow_out_of_order(self):
        """场景 2：快慢对比（a 快 0.2-0.8s、b/c 慢 3-5s）——稳定乱序并发。"""
        agents = ["a", "b", "c"]
        sleep_map = {"a": (0.2, 0.8), "b": (2.0, 4.0), "c": (2.0, 4.0)}
        base, bare, procs, all_exit, alive = self._run_scenario(
            "fastslow", agents, sleep_map, {}, max_meeting=3, max_rr=10)
        try:
            self.assertTrue(all_exit, f"进程未退出: {alive}")
            # 完整处理验证（用户 9960）
            assert_processed_complete(self, bare, base, agents)
            # git 完整性：无分叉（pull --rebase 保证线性；若有并发写问题会失败）
            r = run_git(bare, "log", "--oneline", "--all")
            self.assertNotIn("Merge", r.stdout, "出现合并提交（并发写分叉）")
            # 乱序发生：快慢差异下应有交错提交（a 的首条 vs b 的首条顺序不定）
            # 关键验证：git 完整性（无分叉）+ 所有消息可读（协议正确性）
            # 注：响应次数与速度无关——慢 agent 处理积压可能响应更多（真实并发行为）
            r = run_git(bare, "log", "--format=%H %s")
            commits = r.stdout.strip().splitlines()
            # 消息 commit（非 setup）应 ≥ 3（三方都有发言）
            msg_commits = [c for c in commits if "discuss:" in c and "/" in c.split("discuss:")[1]]
            self.assertGreaterEqual(len(msg_commits), 3, "至少三方各一条消息")
            # 消息完整性：每个消息文件都可读（frontmatter 完整）
            from meeting_fs import git_show, parse_frontmatter
            for a in agents:
                r = run_git(bare, "ls-tree", "-r", "--name-only", "HEAD")
                for f in (r.stdout.strip().splitlines() or []):
                    if f.startswith(a + "/"):
                        fm = parse_frontmatter(git_show(bare, "HEAD", f))
                        self.assertIn("type", fm, f"{f} 缺 type")
                        self.assertIn("from", fm, f"{f} 缺 from")
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_crash_recovery(self):
        """场景 3：崩溃恢复（a 崩溃率 30%——模拟 OOM 杀 agent）。"""
        agents = ["a", "b", "c"]
        crash_map = {"a": 0.3}
        base, bare, procs, all_exit, alive = self._run_scenario(
            "crash", agents, {}, crash_map, max_meeting=3, max_rr=8, timeout=220)
        try:
            self.assertTrue(all_exit, f"进程未退出: {alive}")
            # 完整处理验证（用户 9960）——注意：crash 场景下崩溃轮次
            # 的 responder 未完成（模拟崩溃未写文件），但成功轮次仍应
            # 覆盖其他 agent 的 message/freezing
            assert_processed_complete(self, bare, base, agents)
            # 崩溃 agent 应最终成功提交了至少 1 条（重试兜底）
            r = run_git(bare, "ls-tree", "-r", "--name-only", "HEAD")
            a_files = sorted([f for f in (r.stdout.strip().splitlines() or []) if f.startswith("a/")])
            self.assertGreaterEqual(len(a_files), 1,
                                    f"a 从未成功提交任何消息: {a_files}")
            # 崩溃不丢消息：a 的每次提交序号连续（无跳号）
            # 注：不强制 a 必须崩过（30% 崩溃率下少量唤醒可能恰好不崩）
            seqs = [int(f.split("/")[1].split(".")[0]) for f in a_files]
            self.assertEqual(seqs, list(range(1, len(seqs) + 1)),
                             f"崩溃导致消息序号不连续: {a_files}")
        finally:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
