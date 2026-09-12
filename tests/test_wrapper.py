"""mv.sh wrapper 测试（API 清单基准）——分三层：

1. **产物层**（TestPrepare）：--prepare 生成的 spec 内容与校验拒绝；
2. **命令级错误路径**（TestErrorPaths）：--start 的参数缺失/目录不存在；
3. **消费命令形态矩阵**（TestCommandFormMatrix）：6 个消费命令 ×
   {显式目录, 省略, 无环境, 别的 session} 的全部形态——**契约写成表**。

wrapper 是 bash 脚本：用 subprocess 真实执行 + 断言输出/退出码（bash 层
无行覆盖工具，行为断言是唯一手段）。--start 启动真实 loop 太重（e2e 覆盖），
冒烟只到参数/错误路径与 --prepare 产物。

**断言强度规则**（2026-09-11）：wrapper 测试禁止"仅断言退出码"——必须
断言具体行为（输出内容 / 文件副作用）。只断言非零退出的测试在契约变更后
会静默变空洞（实例：test_view_requires_dir 测的"目录必传"契约废除后仍绿，
看起来在覆盖 --view 实则什么都没测；F5 回归正是从此类缺口漏过）。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRAPPER = os.path.join(HERE, "scripts", "mv.sh")

# 诱饵环境的可判别标记：正牌环境没有它——viewer 输出里出现它就说明
# "用了自动发现而不是显式目录"（F5 形态）
DECOY_MARKER = "DECOY-诱饵标记"


def run_wrapper(args, cwd=None, env=None):
    return subprocess.run([WRAPPER] + args, cwd=cwd or HERE,
                          capture_output=True, text=True, env=env)


def run_with_sid(args, cwd, sid="sidZ"):
    """按指定 sid 运行 wrapper。

    必须清掉继承的 PI_SESSION_ID / PI_SESSION_FILE——测试进程可能跑在 pi
    会话里，继承会让"目录自动发现"指向真实分析目录（测试串到生产现场）。
    """
    env = {**os.environ, "PI_SESSION_ID": sid}
    env.pop("PI_SESSION_FILE", None)
    return subprocess.run(["bash", WRAPPER] + args, cwd=cwd, env=env,
                          capture_output=True, text=True)


def make_env(tmp, name, with_human=False, conclude=False):
    """构造最小分析环境：<tmp>/<name>/repo.git + work-a（+ work-human）。

    判据 = 含 `repo.git`（与 engine / observability.find_current_dir 一致：
    bare 是分析存在的唯一标志）——形态矩阵的前置条件 'env'/'env_human'
    由此构造。

    conclude=True → 追加"收尾"信号（concluded 消息 + result.md + 诱饵标记
    正文），使 `check_status` 返回 **done**（而正牌环境是 stopped）：
    这是**诱饵环境**的关键——两个同构环境的 HEAD/状态完全相同就无法
    区分"用了显式目录"还是"自动发现恰好找到同一个"（而后者正是 F5
    回归的形态）。
    """
    base = os.path.join(tmp, name)
    os.makedirs(base)
    subprocess.run(["git", "init", "-q", "--bare",
                    os.path.join(base, "repo.git")], check=True)
    w = os.path.join(base, "work-a")
    subprocess.run(["git", "clone", "-q", os.path.join(base, "repo.git"), w],
                   check=True, capture_output=True)
    for k, v in (("user.name", "t"), ("user.email", "t@t")):
        subprocess.run(["git", "config", k, v], cwd=w, check=True)
    with open(os.path.join(w, "protocol.json"), "w") as f:
        json.dump({"participants": ["a", "b"], "resultWriter": "b"}, f)
    subprocess.run(["git", "add", "-A"], cwd=w, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-qm", "discuss: setup"], cwd=w,
                   check=True, capture_output=True)
    subprocess.run(["git", "push", "-q", "origin", "HEAD"], cwd=w,
                   check=True, capture_output=True)
    if with_human:
        wh = os.path.join(base, "work-human")
        subprocess.run(["git", "clone", "-q",
                        os.path.join(base, "repo.git"), wh], check=True,
                       capture_output=True)
        for k, v in (("user.name", "t"), ("user.email", "t@t")):
            subprocess.run(["git", "config", k, v], cwd=wh, check=True)
    if conclude:
        os.makedirs(os.path.join(w, "a"), exist_ok=True)
        with open(os.path.join(w, "a", "0001.md"), "w") as f:
            f.write("---\nfrom: a\ntype: concluded\nmode: concluded\n---\n\n"
                    f"{DECOY_MARKER}\n")
        with open(os.path.join(w, "result.md"), "w") as f:
            f.write("# 诱饵结论\n\n" + "x" * 60)
        subprocess.run(["git", "add", "-A"], cwd=w, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-qm", "discuss: 诱饵收尾"], cwd=w,
                       check=True, capture_output=True)
        subprocess.run(["git", "push", "-q", "origin", "HEAD"], cwd=w,
                       check=True, capture_output=True)
    return base


class TestPrepare(unittest.TestCase):
    """W1。"""

    def test_prepare_generates_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run_wrapper(["--prepare", "测试主题", "--agents", "2"], cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            specs = [d for d in os.listdir(tmp)
                     if d.startswith("mv-spec-")]
            self.assertEqual(len(specs), 1)
            spec = os.path.join(tmp, specs[0])
            for f in ("question.md", "background.md", "models.md"):
                self.assertTrue(os.path.isfile(os.path.join(spec, f)))
            # agents 目录
            agents = sorted(os.listdir(os.path.join(spec, "agents")))
            self.assertEqual(agents, [".order", "a.md", "b.md"])
            with open(os.path.join(spec, "agents", ".order")) as f:
                self.assertEqual(f.read().split(), ["a", "b"])

    def test_prepare_agents_number_and_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run_wrapper(["--prepare", "T", "--agents", "4"], cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            spec = os.path.join(tmp, [d for d in os.listdir(tmp)
                                      if d.startswith("mv-spec-")][0])
            with open(os.path.join(spec, "agents", ".order")) as f:
                self.assertEqual(f.read().split(), ["a", "b", "c", "d"])
            shutil.rmtree(spec)
            r = run_wrapper(["--prepare", "T", "--agents", "x,y"], cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            spec = os.path.join(tmp, [d for d in os.listdir(tmp)
                                      if d.startswith("mv-spec-")][0])
            with open(os.path.join(spec, "agents", ".order")) as f:
                self.assertEqual(f.read().split(), ["x", "y"])

    def test_prepare_rejects_illegal_name_early(self):
        """R8：非法名在 prepare 就拦（此前 --agents "a/b" → FileNotFoundError
        traceback + spec 半成品落盘；"a b,c" → 骨架成功落盘直到 start 才拒）。"""
        with tempfile.TemporaryDirectory() as tmp:
            r = run_wrapper(["--prepare", "T", "--agents", "a/b"], cwd=tmp)
            self.assertNotEqual(r.returncode, 0)
            out = r.stdout + r.stderr
            self.assertIn("非法 agent 名", out)
            self.assertNotIn("Traceback", out)
            self.assertEqual([d for d in os.listdir(tmp)
                              if d.startswith("mv-spec-")], [])   # 零产物
        with tempfile.TemporaryDirectory() as tmp:
            r = run_wrapper(["--prepare", "T", "--agents", "a b,c"], cwd=tmp)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("非法 agent 名", r.stdout + r.stderr)
            self.assertEqual([d for d in os.listdir(tmp)
                              if d.startswith("mv-spec-")], [])

    def test_prepare_rejects_human(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run_wrapper(["--prepare", "T", "--agents", "a,human"], cwd=tmp)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("human", r.stderr + r.stdout)
            self.assertIn("保留名", r.stderr + r.stdout)

    def test_start_forwards_fork_mode(self):
        """P0（e2e10 评审）：--start 的余参必须转发给 python——此前 wrapper
        静默丢弃（README 教的操作实际无效）。用非法值验证转发（argparse
        早失败是 python 侧行为，wrapper 不解析值）。"""
        with tempfile.TemporaryDirectory() as tmp:
            spec = os.path.join(tmp, "mv-spec-x")
            os.makedirs(spec)
            with open(os.path.join(spec, "question.md"), "w") as f:
                f.write("# 分析主题：T\n")
            r = run_wrapper(["--start", spec, "--fork-mode", "bogus"], cwd=tmp)
            self.assertNotEqual(r.returncode, 0)
            out = r.stdout + r.stderr
            self.assertIn("--fork-mode", out)      # 参数确实到达 python
            self.assertIn("invalid choice", out)   # python 侧早失败

    def test_prepare_no_topic(self):
        """缺主题 → 用法帮助（rc 2；断言内容，不只退出码——强度规则）。"""
        r = run_wrapper(["--prepare"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("用法:", r.stderr)
        self.assertIn("--prepare", r.stderr)

    def test_prepare_background_written(self):
        """viewers 模式 prepare：viewers 合规才生成 spec，快照进 agents/。"""
        with tempfile.TemporaryDirectory() as tmp:
            vd = os.path.join(tmp, "viewers")
            os.makedirs(vd)
            for name, brief in ("性能", "性能视角正文"), ("可读性", "可读性视角正文"):
                with open(os.path.join(vd, f"{name}.md"), "w") as f:
                    f.write(brief)
            r = run_wrapper(["--prepare", "T", "--background", "背景内容"],
                            cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            spec = os.path.join(tmp, [d for d in os.listdir(tmp)
                                      if d.startswith("mv-spec-")][0])
            with open(os.path.join(spec, "background.md")) as f:
                self.assertIn("背景内容", f.read())
            # viewers 快照：spec agents/ 含视角文件 + .order
            agents_dir = os.path.join(spec, "agents")
            self.assertTrue(os.path.isdir(agents_dir))
            with open(os.path.join(agents_dir, "性能.md")) as f:
                self.assertIn("性能视角正文", f.read())
            with open(os.path.join(agents_dir, ".order")) as f:
                self.assertEqual([l.strip() for l in f if l.strip()],
                                 ["可读性", "性能"])

    def test_prepare_rejects_missing_viewers(self):
        """viewers/ 缺失 → prepare 失败（校验前移：不生成 spec）。"""
        with tempfile.TemporaryDirectory() as tmp:
            r = run_wrapper(["--prepare", "T"], cwd=tmp)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("viewers", r.stderr + r.stdout)
            self.assertEqual([d for d in os.listdir(tmp)
                              if d.startswith("mv-spec-")], [])

    def test_prepare_rejects_empty_viewer(self):
        """空视角任务书 → 报错不生成 spec（无 lenses 的 agent 会让多视角
        退化成同名随机视角——静默退化，与无静默铁律相悖）。"""
        with tempfile.TemporaryDirectory() as tmp:
            vd = os.path.join(tmp, "viewers")
            os.makedirs(vd)
            with open(os.path.join(vd, "性能.md"), "w") as f:
                f.write("性能视角正文")
            with open(os.path.join(vd, "占位.md"), "w") as f:
                f.write("")                      # 空文件
            with open(os.path.join(vd, "空白.md"), "w") as f:
                f.write("\n\n  \n")            # 纯空白
            r = run_wrapper(["--prepare", "T"], cwd=tmp)
            self.assertNotEqual(r.returncode, 0)
            out = r.stdout + r.stderr
            self.assertIn("视角任务书不能为空", out)
            self.assertIn("占位", out)
            self.assertIn("空白", out)
            # 零产物（校验在骨架生成之前）
            self.assertEqual([d for d in os.listdir(tmp)
                              if d.startswith("mv-spec-")], [])

    def test_prepare_rejects_single_viewer(self):
        """viewers/ 仅 1 个视角 → 失败（meeting 至少 2 agents）。"""
        with tempfile.TemporaryDirectory() as tmp:
            vd = os.path.join(tmp, "viewers")
            os.makedirs(vd)
            with open(os.path.join(vd, "唯一.md"), "w") as f:
                f.write("x")
            r = run_wrapper(["--prepare", "T"], cwd=tmp)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("至少需要 2 个", r.stderr + r.stdout)

    def test_prepare_rejects_human_viewer(self):
        """viewers/ 含 human.md → 失败（保留名）。"""
        with tempfile.TemporaryDirectory() as tmp:
            vd = os.path.join(tmp, "viewers")
            os.makedirs(vd)
            for name in ("性能", "human"):
                with open(os.path.join(vd, f"{name}.md"), "w") as f:
                    f.write("x")
            r = run_wrapper(["--prepare", "T"], cwd=tmp)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("human", r.stderr + r.stdout)


class TestErrorPaths(unittest.TestCase):
    """W2-W6 错误路径（命令级参数缺失）。

    **断言强度规则**（e2e16 自审）：断言 = 退出码 + **具体报错内容**。
    只断言"非零退出"的测试在契约变更后会静默变空洞——本类原有四条
    （test_view_requires_dir / test_say_requires_dir_and_text /
    test_status_missing_dir / test_cleanup_missing_dir）就是实例：
    "目录必传"契约废除后它们仍然绿（无参 = 自动发现失败才非零），
    看起来在覆盖 --view/--say 实则测不到任何现行契约。这四条已由
    `TestCommandFormMatrix`（命令 × 形态的表）取代。
    """

    def test_start_requires_spec(self):
        r = run_wrapper(["--start"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("--start 需要 spec 目录参数", r.stderr)

    def test_start_missing_spec_dir(self):
        r = run_wrapper(["--start", "/nonexistent-spec"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("目录不存在", r.stderr)


class TestCommandFormMatrix(unittest.TestCase):
    """消费命令形态矩阵（表驱动）——F5 回归的系统性答案。

    背景（2026-09-11，e2e16 自审）：`--view <dir>` 的显式目录被静默丢弃，
    395 个测试全绿没挡住。诊断出的根因**不是"漏写一条用例"**，而是结构
    性缺口：
      ① wrapper 测试按功能增量堆叠，没有"命令 × 形态"的系统性盘点；
      ② bash 层无行覆盖工具——缺口不出现在覆盖率里（实测 Python 层 96%，
         但 wrapper 完全测不到）；
      ③ 幸存的旧测试用**弱断言**（仅退出码），契约变更后静默变空洞。

    本类把契约写成**一张表**：每行 = 一个形态单元格。新增命令/形态必须
    补行（结构性强制，不靠记忆）。每格断言三件事：
      **退出码 + 输出内容 + 副作用**（如 --cleanup 真的删了目录）

    前置条件（precondition）：
      env       = 存在本 session 的分析环境（`mv-sidZ-*`，含 repo.git）
      env_human = 同上 + work-human（--say 需要）
      env_decoy = 正牌环境（stopped）+ **更晚时间戳的诱饵环境**（done，
                  带 DECOY 标记）—— 显式目录格子用它：若实现忽略了显式
                  目录而走自动发现，就会选中诱饵，断言必然失败（这正是
                  F5 回归的形态；两个同构环境无法区分，必须让诱饵可判别）
      other     = 只存在**别的** session 的分析（`mv-other-*`）
                 —— 不得降级取用（破坏性操作不能作用于猜测目录）
      none      = 空目录（无分析）
    `{dir}` 占位（参数与期望文本） = 正牌环境路径；`none` 时指向不存在路径。

    副作用断言（effect）：
      dir_gone   = 正牌目录被删（--cleanup）
      human_sent = 正牌环境的 bare 里出现 human/0001.md（--say 写对了地方）
      no_decoy   = 输出里**不出现** DECOY 标记（没走自动发现选到诱饵）
    """

    SID = "sidZ"
    ENV_NAME = f"mv-{SID}-20260101-000000"
    DECOY_NAME = f"mv-{SID}-20260909-000000"          # 更晚时间戳 → 自动发现会选它
    OTHER_NAME = "mv-other-20260101-000000"

    # (args, precondition, expect_rc, 输出须含, effect)
    # 显式目录格子用 env_decoy：单 env 时"显式"与"自动发现"结果相同，
    # 无法区分；诱饵（更晚 + done + 标记）使两者可判别——F5 类 bug 必失败。
    MATRIX = [
        # ---- --status ----
        (["--status", "{dir}"], "env_decoy", 0, "[status] stopped", None),
        (["--status"], "env", 0, "[status] stopped", None),
        (["--status"], "none", 1, "未找到本 session 的分析目录", None),
        (["--status"], "other", 1, "未找到本 session 的分析目录", None),
        (["--status", "{dir}"], "none", 1, "目录不存在", None),
        # ---- --report ----
        (["--report", "{dir}"], "env_decoy", 0, "[报告] {dir}", None),
        (["--report"], "env", 0, "[报告]", None),
        (["--report"], "none", 1, "未找到本 session 的分析目录", None),
        (["--report", "{dir}"], "none", 1, "目录不存在", None),
        # ---- --view（F5 回归点：显式形态必须有格且有判别力）----
        (["--view", "{dir}"], "env_decoy", 0, "HEAD=", "no_decoy"),
        (["--view"], "env", 0, "HEAD=", None),
        (["--view"], "none", 1, "未找到本 session 的分析目录", None),
        (["--view", "{dir}"], "none", 1, "目录不存在", None),
        # ---- --say（两形态按参数个数区分，不按值猜语义）----
        (["--say", "{dir}", "文本"], "env_decoy", 0, "human/0001",
         "human_sent"),
        (["--say", "文本"], "env_human", 0, "human/0001", "human_sent"),
        (["--say"], "env_human", 1, "插话文本不能为空", None),
        (["--say", "文本"], "none", 1, "未找到本 session 的分析目录", None),
        (["--say", "{dir}", "文本"], "none", 1, "目录不存在", None),
        # ---- --wait（stopped 环境立即返回，不阻塞）----
        (["--wait", "{dir}"], "env_decoy", 1, "未在运行且未收尾", None),
        (["--wait"], "env", 1, "未在运行且未收尾", None),
        (["--wait"], "none", 1, "未找到本 session 的分析目录", None),
        (["--wait", "{dir}"], "none", 1, "目录不存在", None),
        # ---- --cleanup（破坏性操作：副作用必须断言）----
        (["--cleanup", "{dir}"], "env_decoy", 0, "已删除目录", "dir_gone"),
        (["--cleanup"], "env", 0, "已删除目录", "dir_gone"),
        (["--cleanup"], "none", 1, "未找到本 session 的分析目录", None),
        (["--cleanup", "{dir}"], "none", 1, "目录不存在", None),
    ]

    def test_form_matrix(self):
        for args, precondition, want_rc, want_substr, effect in self.MATRIX:
            with self.subTest(cmd=args[0], args=args, precondition=precondition):
                with tempfile.TemporaryDirectory() as tmp:
                    dirpath = os.path.join(tmp, self.ENV_NAME)
                    if precondition == "env":
                        make_env(tmp, self.ENV_NAME)
                    elif precondition == "env_human":
                        make_env(tmp, self.ENV_NAME, with_human=True)
                    elif precondition == "env_decoy":
                        make_env(tmp, self.ENV_NAME, with_human=True)
                        make_env(tmp, self.DECOY_NAME, conclude=True)
                    elif precondition == "other":
                        make_env(tmp, self.OTHER_NAME)
                    argv = [a.replace("{dir}", dirpath) for a in args]
                    r = run_with_sid(argv, tmp, self.SID)
                    out = r.stdout + r.stderr
                    self.assertEqual(
                        r.returncode, want_rc,
                        f"{argv} [{precondition}]\n{out}")
                    self.assertIn(want_substr.replace("{dir}", dirpath), out)
                    if effect == "dir_gone":
                        self.assertFalse(os.path.isdir(dirpath),
                                         "--cleanup 未删除目录")
                    elif effect == "human_sent":
                        names = subprocess.run(
                            ["git", "-C", os.path.join(dirpath, "repo.git"),
                             "ls-tree", "-r", "--name-only", "HEAD"],
                            capture_output=True, text=True).stdout
                        self.assertIn("human/", names,
                                      "--say 写到了别处（未用显式目录）")
                    elif effect == "no_decoy":
                        self.assertNotIn(DECOY_MARKER, out,
                                         "走了自动发现（选到诱饵环境）")


class TestDirOptional(unittest.TestCase):
    """目录可省略的**命名回归测试**（矩阵之外的补充断言）。

    形态矩阵（TestCommandFormMatrix）已覆盖本类的多数场景；这里保留两个
    有**额外断言价值**的用例：
      - 无 sid 匹配时只输出一条错误（e2e16 F7：错误文案留一处）
      - --view 显式目录优先（F5 命名回归——矩阵也有该格，此处保留可读的
        回归档案：两个并存分析下断言用的是哪一个）
    其余（--status 省略/无环境、--say 两形态）由矩阵表统一覆盖，不再重复。
    """

    SID = "sidZ"
    ENV_NAME = "mv-sidZ-20260101-000000"

    def test_status_no_sid_match_single_error(self):
        """sid 不匹配 → **rc 1 + 只一条错误**（不降级取最新——e2e16 F1/F2/F7）。

        降级兜底会让破坏性操作（--cleanup/--say）作用于猜测目录；删除后
        最坏失败 = 响亮报错要求显式目录。错误文案只由 python 给出
        （wrapper 不另打一条，e2e16 F7）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            make_env(tmp, "mv-other-20260101-000000")
            r = run_with_sid(["--status"], tmp, self.SID)
            self.assertEqual(r.returncode, 1)
            self.assertIn("请显式传目录参数", r.stderr)
            self.assertEqual(r.stderr.count("错误:"), 1, r.stderr)

    def test_view_explicit_dir_not_ignored(self):
        """F5 回归：--view <dir> 必须用显式目录（不是"恰好"自动发现）。

        此前 cmd_view 无条件 shift 后总走自动发现 → 显式目录被**静默丢弃**
        （多分析并存/跨目录调用看错对象）；测试只覆盖无参形态是回归漏网的
        直接原因（e2e16 评审 F5）。判据必须**可判别**：这里给 A 加一条
        独有消息并断言 HEAD/内容来自 A（两个同构环境无法区分）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            a = make_env(tmp, self.ENV_NAME)
            make_env(tmp, "mv-sidZ-20260202-000000")     # 更新的并存分析
            os.makedirs(os.path.join(a, "work-a", "a"), exist_ok=True)
            with open(os.path.join(a, "work-a", "a", "0001.md"), "w") as f:
                f.write("---\nfrom: a\ntype: message\nmode: meeting\n---\n\n"
                        "A 的消息\n")
            for cmd in (["add", "-A"], ["commit", "-qm", "discuss: a/0001"],
                        ["push", "-q", "origin", "HEAD"]):
                subprocess.run(["git"] + cmd, cwd=os.path.join(a, "work-a"),
                               check=True, capture_output=True)
            r = run_with_sid(["--view", a], tmp, self.SID)
            self.assertEqual(r.returncode, 0, r.stderr)
            head_a = subprocess.run(
                ["git", "-C", os.path.join(a, "repo.git"), "rev-parse", "HEAD"],
                capture_output=True, text=True).stdout.strip()
            self.assertIn(f"HEAD={head_a}", r.stdout, "应使用显式目录 A")
            self.assertIn("A 的消息", r.stdout)


if __name__ == "__main__":
    unittest.main()
