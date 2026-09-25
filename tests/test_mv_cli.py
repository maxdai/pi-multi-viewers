"""mv_cli 解析层契约测试（in-process）。

**为什么有这个文件**：wrapper 逻辑从 364 行 bash 收敛为 Python（2026-09-12）
的直接收益——参数解析、目录决议、退出码约定现在是普通函数，可进程内测、
毫秒级、进入覆盖率。此前这层只能靠 subprocess 黑盒（tests/test_wrapper.py
的形态矩阵），且 bash 一行都进不了覆盖率。

分工：
  test_wrapper.py          行为契约（真实 subprocess 跑 mv.sh：形态矩阵 +
                           产物 + 错误路径）——端到端不变式
  本文件                    解析逻辑与退出码的**逐分支**契约（mock 掉子进程）
"""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mv_cli


def run_main(argv):
    """跑 main：返回 (rc, stdout, stderr)——SystemExit.code 也当 rc 返回
    （`fail()` 与 usage 出口都走 SystemExit）。"""
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = mv_cli.main(argv)
    except SystemExit as e:
        rc = e.code
    return rc, out.getvalue(), err.getvalue()


class TestDispatch(unittest.TestCase):
    """分发与 usage 出口（rc 2 / 0 约定）。"""

    def test_no_args_usage_to_stderr_rc2(self):
        rc, out, err = run_main([])
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("用法:", err)

    def test_help_rc0_to_stdout(self):
        for flag in ("-h", "--help"):
            rc, out, err = run_main([flag])
            self.assertEqual(rc, 0)
            self.assertIn("用法:", out)

    def test_unknown_command_usage_rc2(self):
        rc, _out, err = run_main(["--bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("用法:", err)

    def test_usage_shows_invoked_as(self):
        """展示名沿用 bash 的 `$0` 语义（shim 传 MV_INVOKED_AS）。"""
        with mock.patch.dict(os.environ, {"MV_INVOKED_AS": "/pkg/scripts/mv.sh"}):
            import importlib
            importlib.reload(mv_cli)
            try:
                rc, out, _err = run_main(["--help"])
                self.assertEqual(rc, 0)
                self.assertIn("/pkg/scripts/mv.sh --prepare", out)
            finally:
                # 还原 PROG（清除注入的变量后 reload）
                os.environ.pop("MV_INVOKED_AS", None)
                importlib.reload(mv_cli)


class TestPrepareParse(unittest.TestCase):
    """--prepare 的参数解析（含 -h 出口与"问题不能为空"）。"""

    def test_missing_topic_usage_rc2(self):
        rc, _out, err = run_main(["--prepare"])
        self.assertEqual(rc, 2)
        self.assertIn("用法:", err)

    def test_help_rc0(self):
        rc, out, _err = run_main(["--prepare", "T", "--help"])
        self.assertEqual(rc, 0)
        self.assertIn("用法:", out)

    def test_empty_topic_fails(self):
        rc, _out, err = run_main(["--prepare", ""])
        self.assertEqual(rc, 1)
        self.assertIn("问题不能为空", err)

    def test_option_missing_value(self):
        for flag, msg in (("--background", "--background 需要一个值"),
                          ("--agents", "--agents 需要一个值")):
            with self.subTest(flag=flag):
                rc, _out, err = run_main(["--prepare", "T", flag])
                self.assertEqual(rc, 1)
                self.assertIn(msg, err)

    def test_unknown_option(self):
        rc, _out, err = run_main(["--prepare", "T", "--bogus"])
        self.assertEqual(rc, 1)
        self.assertIn("未知参数: --bogus", err)

    def test_spec_gen_args_forwarded(self):
        """参数原样转发给 --spec-gen（值识别/默认值的唯一家在 python 侧）。"""
        with mock.patch.object(mv_cli, "_call", return_value=0) as call:
            rc, out, _err = run_main(["--prepare", "主题", "--background", "BG",
                                      "--agents", "x,y"])
        self.assertEqual(rc, 0)
        cmd = call.call_args.args[0]
        self.assertEqual(cmd[1], mv_cli.START_DISCUSSION)
        self.assertIn("--spec-gen", cmd)
        self.assertEqual(cmd[cmd.index("--topic") + 1], "主题")
        self.assertEqual(cmd[cmd.index("--background") + 1], "BG")
        self.assertEqual(cmd[cmd.index("--agents") + 1], "x,y")
        self.assertIn("已生成分析 spec", out)

    def test_spec_gen_failure_reports(self):
        with mock.patch.object(mv_cli, "_call", return_value=1):
            rc, _out, err = run_main(["--prepare", "T"])
        self.assertEqual(rc, 1)
        self.assertIn("spec 骨架生成失败", err)


class TestDirResolution(unittest.TestCase):
    """目录决议：显式优先 / 省略则自动发现 / 多余参数响亮失败。"""

    def test_explicit_dir_normalized(self):
        with mock.patch.object(mv_cli, "_call", return_value=0) as call, \
                mock.patch.object(mv_cli.os.path, "isdir", return_value=True):
            rc, _out, _err = run_main(["--status", "/tmp/./x"])
        self.assertEqual(rc, 0)
        cmd = call.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--dir") + 1], "/tmp/x")
        self.assertEqual(cmd[-1], "--status")

    def test_omitted_dir_uses_find_dir(self):
        with mock.patch.object(mv_cli.subprocess, "run",
                               return_value=mock.Mock(returncode=0,
                                                      stdout="/auto/dir\n")) as sp, \
                mock.patch.object(mv_cli, "_call", return_value=0) as call, \
                mock.patch.object(mv_cli.os.path, "isdir", return_value=True):
            rc, _out, _err = run_main(["--status"])
        self.assertEqual(rc, 0)
        first = sp.call_args_list[0].args[0]
        self.assertEqual(first,
                         [mv_cli.PYTHON, mv_cli.OBSERVABILITY, "--find-dir"])
        cmd = call.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--dir") + 1], "/auto/dir")

    def test_omitted_dir_discovery_failure_passthrough(self):
        """自动发现失败 → 退出码与原因**原样透传**（错误文案留一处）。"""
        with mock.patch.object(mv_cli.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout="")):
            rc, _out, _err = run_main(["--status"])
        self.assertEqual(rc, 1)

    def test_extra_args_rejected(self):
        """多余参数响亮失败（无静默铁律 / P0：wrapper 不得静默丢弃参数）。"""
        for cmd in ("--status", "--report", "--wait", "--cleanup"):
            with self.subTest(cmd=cmd):
                rc, _out, err = run_main([cmd, "a", "b"])
                self.assertEqual(rc, 1)
                self.assertIn("只接受 [dir]", err)

    def test_missing_dir_fails(self):
        with mock.patch.object(mv_cli, "_call", return_value=0):
            rc, _out, err = run_main(["--status", "/nonexistent-dir-x"])
        self.assertEqual(rc, 1)
        self.assertIn("目录不存在: /nonexistent-dir-x", err)


class TestViewParse(unittest.TestCase):
    """--view：首参判形（非 --since 即目录）+ --since 解析 + 游标输出。"""

    def test_since_missing_value(self):
        with mock.patch.object(mv_cli, "resolve_dir", return_value="/x"), \
                mock.patch.object(mv_cli, "require_dir", side_effect=lambda d: d), \
                mock.patch.object(mv_cli.os.path, "isdir", return_value=True):
            rc, _out, err = run_main(["--view", "--since"])
        self.assertEqual(rc, 1)
        self.assertIn("--since 需要一个值", err)

    def test_unknown_option(self):
        # 首参非 --since → 按**目录**判形（文档化）；未知选项在 --since 之后
        with mock.patch.object(mv_cli, "resolve_dir", return_value="/x"), \
                mock.patch.object(mv_cli, "require_dir", side_effect=lambda d: d), \
                mock.patch.object(mv_cli.os.path, "isdir", return_value=True):
            rc, _out, err = run_main(["--view", "--since", "REF", "--bogus"])
        self.assertEqual(rc, 1)
        self.assertIn("未知参数: --bogus", err)

    def test_first_arg_is_treated_as_dir(self):
        """首参不是 --since 就按目录解析（F5 修复的判形规则本身要钉住）。"""
        with mock.patch.object(mv_cli, "resolve_dir") as rd, \
                mock.patch.object(mv_cli, "require_dir", side_effect=lambda d: d), \
                mock.patch.object(mv_cli.os.path, "isdir", return_value=True), \
                mock.patch.object(mv_cli, "_call", return_value=0), \
                mock.patch.object(mv_cli.subprocess, "run",
                                  return_value=mock.Mock(stdout="h\n")):
            rc, _out, _err = run_main(["--view", "/explicit/dir"])
        self.assertEqual(rc, 0)
        self.assertEqual(rd.call_args.args[0], "/explicit/dir")

    def test_viewer_failure_propagates_without_cursor(self):
        """viewer 失败 → 透传 rc 且**不打印 HEAD 游标**。

        给失败的一轮发游标会让下一次 --since 静默跳过消息（bash 版把 rc 吞成
        0 且照打游标 = 静默失败；此处改为响亮失败）。
        """
        with mock.patch.object(mv_cli, "resolve_dir", return_value="/x"), \
                mock.patch.object(mv_cli, "require_dir", side_effect=lambda d: d), \
                mock.patch.object(mv_cli.os.path, "isdir", return_value=True), \
                mock.patch.object(mv_cli, "_call", return_value=3):
            rc, out, _err = run_main(["--view"])
        self.assertEqual(rc, 3)
        self.assertNotIn("HEAD=", out)

    def test_success_prints_cursor(self):
        with mock.patch.object(mv_cli, "resolve_dir", return_value="/x"), \
                mock.patch.object(mv_cli, "require_dir", side_effect=lambda d: d), \
                mock.patch.object(mv_cli.os.path, "isdir", return_value=True), \
                mock.patch.object(mv_cli, "_call", return_value=0), \
                mock.patch.object(mv_cli.subprocess, "run",
                                  return_value=mock.Mock(stdout="abc123\n")):
            rc, out, _err = run_main(["--view"])
        self.assertEqual(rc, 0)
        self.assertIn("HEAD=abc123", out)


class TestSayParse(unittest.TestCase):
    """--say：按参数个数区分两形态；多于 2 个参数响亮失败。"""

    def test_too_many_args(self):
        rc, _out, err = run_main(["--say", "a", "b", "c"])
        self.assertEqual(rc, 1)
        self.assertIn("文本含空格请加引号", err)

    def test_two_args_is_dir_and_text(self):
        with mock.patch.object(mv_cli, "resolve_dir", return_value="/d"), \
                mock.patch.object(mv_cli, "require_dir", side_effect=lambda d: d), \
                mock.patch.object(mv_cli.os.path, "isdir", return_value=True), \
                mock.patch.object(mv_cli, "_call", return_value=0) as call:
            rc, _out, _err = run_main(["--say", "/d", "文本"])
        self.assertEqual(rc, 0)
        self.assertEqual(call.call_args.args[0],
                         [mv_cli.PYTHON, mv_cli.HUMAN_SAYER, "/d", "文本"])

    def test_one_arg_is_text_only(self):
        with mock.patch.object(mv_cli, "resolve_dir", return_value="/d"), \
                mock.patch.object(mv_cli, "require_dir", side_effect=lambda d: d), \
                mock.patch.object(mv_cli.os.path, "isdir", return_value=True), \
                mock.patch.object(mv_cli, "_call", return_value=0) as call:
            rc, _out, _err = run_main(["--say", "只有文本"])
        self.assertEqual(rc, 0)
        self.assertEqual(call.call_args.args[0],
                         [mv_cli.PYTHON, mv_cli.HUMAN_SAYER, "/d", "只有文本"])

    def test_empty_text_fails(self):
        with mock.patch.object(mv_cli, "resolve_dir", return_value="/d"), \
                mock.patch.object(mv_cli, "require_dir", side_effect=lambda d: d), \
                mock.patch.object(mv_cli.os.path, "isdir", return_value=True):
            rc, _out, err = run_main(["--say"])
        self.assertEqual(rc, 1)
        self.assertIn("插话文本不能为空", err)


class TestStartParse(unittest.TestCase):
    """--start：spec 校验、目录名构造、余参转发、spec 删除策略。"""

    def test_missing_spec_arg(self):
        rc, _out, err = run_main(["--start"])
        self.assertEqual(rc, 1)
        self.assertIn("--start 需要 spec 目录参数", err)

    def test_spec_without_question(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            rc, _out, err = run_main(["--start", tmp])
        self.assertEqual(rc, 1)
        self.assertIn("spec 缺少 question.md", err)

    def test_dir_name_and_extra_args(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            spec = os.path.join(tmp, "mv-spec-x")
            os.makedirs(spec)
            with open(os.path.join(spec, "question.md"), "w") as f:
                f.write("# 分析主题：T\n")
            calls = []

            def fake_call(cmd):
                calls.append(cmd)
                return 0
            with mock.patch.object(mv_cli, "_call", side_effect=fake_call), \
                    mock.patch.dict(os.environ, {"PI_SESSION_ID": "sid9"}), \
                    mock.patch.object(mv_cli.os, "getcwd", return_value=tmp):
                rc, out, _err = run_main(["--start", spec, "--fork-mode",
                                          "budget"])
            self.assertEqual(rc, 0)
            # 第 1 次调用：创建（含余参转发，不静默丢弃）
            create = calls[0]
            dir_path = create[create.index("--dir") + 1]
            self.assertTrue(os.path.basename(dir_path).startswith("mv-sid9-"))
            self.assertEqual(create[create.index("--spec") + 1], spec)
            self.assertEqual(create[-2:], ["--fork-mode", "budget"])
            # 第 2 次调用：启动
            self.assertEqual(calls[1][-2:], ["--skip-setup", "--start"])
            # spec 是本工具生成形态 → 删除 + 有提示
            self.assertFalse(os.path.exists(spec))
            self.assertIn("spec 已消费，删除", out)

    def test_user_spec_preserved(self):
        """非 mv-spec-* 形态（用户自建）→ 保留不删。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            spec = os.path.join(tmp, "my-own-spec")
            os.makedirs(spec)
            with open(os.path.join(spec, "question.md"), "w") as f:
                f.write("# 分析主题：T\n")
            with mock.patch.object(mv_cli, "_call", return_value=0), \
                    mock.patch.object(mv_cli.os, "getcwd", return_value=tmp):
                rc, out, _err = run_main(["--start", spec])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.isdir(spec))
            self.assertIn("保留未删", out)


class TestViewers(unittest.TestCase):
    """`--viewers` 只读检查（建视角时用）：列出 + 用代码判据校验。

    为什么单独测：这条命令的判据全部复用 spec_gen 的单一实现（列举 / 名字 /
    集合），此前只有 prepare 路径在用它——建视角这一刻**没有分析目录**，消费
    命令的目录自动发现不适用。数量不足在这里是**提示**（建 1 个合法），不是错误。
    """

    def _proj(self, tmp, files):
        """造一个项目目录：{文件名: 内容} → <tmp>/viewers/。"""
        vdir = os.path.join(tmp, "viewers")
        os.makedirs(vdir, exist_ok=True)
        for name, body in files.items():
            with open(os.path.join(vdir, name), "w", encoding="utf-8") as f:
                f.write(body)
        return tmp

    def _run(self, tmp, argv=("--viewers",)):
        with mock.patch.object(mv_cli.os, "getcwd", return_value=tmp):
            return run_main(list(argv))

    def test_legal_set_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._proj(tmp, {"甲.md": "镜头 A\n", "乙.md": "镜头 B\n"})
            rc, out, err = self._run(tmp)
            self.assertEqual((rc, err), (0, ""))
            self.assertIn("甲.md", out)
            self.assertIn("校验：通过（2 个视角", out)

    def test_single_viewer_is_hint_not_error(self):
        """建 1 个是合法中间状态（≥2 只在启动分析时要求）。"""
        with tempfile.TemporaryDirectory() as tmp:
            self._proj(tmp, {"甲.md": "镜头 A\n"})
            rc, out, err = self._run(tmp)
            self.assertEqual((rc, err), (0, ""))
            self.assertIn("至少需要 2 个", out)
            self.assertIn("合法的中间状态", out)

    def test_empty_viewer_fails_without_double_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._proj(tmp, {"甲.md": "镜头 A\n", "乙.md": "   \n\n"})
            rc, out, err = self._run(tmp)
            self.assertEqual(rc, 1)
            self.assertIn("乙.md", err)
            self.assertIn("不能为空", err)
            self.assertNotIn("错误: 错误:", err)   # 前缀归判据实现方，不再包一层
            self.assertIn("乙.md（空", out)        # 列表先落地，错误在后

    def test_illegal_name_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._proj(tmp, {"甲.md": "x\n", "a b.md": "y\n"})
            rc, _out, err = self._run(tmp)
            self.assertEqual(rc, 1)
            self.assertIn("非法 agent 名", err)
            self.assertNotIn("错误: 错误:", err)

    def test_reserved_human_name_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._proj(tmp, {"甲.md": "x\n", "human.md": "y\n"})
            rc, _out, err = self._run(tmp)
            self.assertEqual(rc, 1)
            self.assertIn("保留名", err)

    def test_hidden_file_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._proj(tmp, {"甲.md": "x\n", "乙.md": "y\n", ".draft.md": "z\n"})
            rc, out, _err = self._run(tmp)
            self.assertEqual(rc, 0)
            self.assertNotIn("draft", out)
            self.assertIn("校验：通过（2 个视角", out)

    def test_missing_viewers_dir_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, _out, err = self._run(tmp)
            self.assertEqual(rc, 1)
            self.assertIn("未找到", err)
            self.assertIn("multi-viewers-setup", err)   # 错误里给出路

    def test_empty_dir_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._proj(tmp, {})
            rc, _out, err = self._run(tmp)
            self.assertEqual(rc, 1)
            self.assertIn("没有 *.md", err)

    def test_extra_args_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._proj(tmp, {"甲.md": "x\n", "乙.md": "y\n"})
            rc, _out, err = self._run(tmp, ("--viewers", "extra"))
            self.assertEqual(rc, 1)
            self.assertIn("不接受参数", err)

    def test_not_routed_through_dir_resolution(self):
        """--viewers 只看项目 cwd——不得走消费命令的目录自动发现。"""
        with tempfile.TemporaryDirectory() as tmp:
            self._proj(tmp, {"甲.md": "x\n", "乙.md": "y\n"})
            with mock.patch.object(mv_cli, "resolve_dir") as rd, \
                    mock.patch.object(mv_cli.os, "getcwd", return_value=tmp):
                rc, _out, _err = run_main(["--viewers"])
            self.assertEqual(rc, 0)
            rd.assert_not_called()


class TestSetViewer(unittest.TestCase):
    """`--set-viewer`：正文从 stdin 读；命名/不覆盖/空正文/写完校验全由机制保证。

    为什么这层值得测：这四条本来是 prompt 里给 LLM 的纪律（会漏），现在移到
    CLI —— 它们是"建视角"唯一可能静默出错的点（写成非法名、覆盖已有资产、
    写空文件），且**只在真跑时才炸**（分析启动时才发现）。
    """

    def _run(self, cwd, args, stdin=""):
        with mock.patch.object(mv_cli.os, "getcwd", return_value=cwd), \
                mock.patch.object(mv_cli.sys, "stdin", io.StringIO(stdin)):
            return run_main(args)

    def test_creates_and_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "viewers"))
            rc, out, _err = self._run(tmp, ["--set-viewer", "甲"], "镜头：从甲看\n")
            self.assertEqual(rc, 0, out)
            path = os.path.join(tmp, "viewers", "甲.md")
            self.assertTrue(os.path.exists(path))
            self.assertEqual(open(path, encoding="utf-8").read(), "镜头：从甲看\n")
            self.assertIn("[set-viewer] 已写入", out)
            self.assertIn("镜头：从甲看", out)          # 回显正文
            self.assertIn("校验：", out)               # 写完立即校验（同一实现）

    def test_creates_viewers_dir_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, _out, _err = self._run(tmp, ["--set-viewer", "乙"], "内容\n")
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.isdir(os.path.join(tmp, "viewers")))

    def test_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            vdir = os.path.join(tmp, "viewers")
            os.makedirs(vdir)
            old = os.path.join(vdir, "甲.md")
            with open(old, "w", encoding="utf-8") as f:
                f.write("原有内容\n")
            rc, _out, err = self._run(tmp, ["--set-viewer", "甲"], "新内容\n")
            self.assertEqual(rc, 1)
            self.assertIn("已存在，不覆盖", err)
            self.assertEqual(open(old, encoding="utf-8").read(), "原有内容\n")

    def test_rejects_invalid_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ("human", "a/b", "有 空格", "x" * 40):
                rc, _out, err = self._run(tmp, ["--set-viewer", bad], "内容\n")
                self.assertEqual(rc, 1, bad)
                self.assertIn("非法视角名", err)
            self.assertFalse(os.path.isdir(os.path.join(tmp, "viewers")))

    def test_rejects_empty_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, _out, err = self._run(tmp, ["--set-viewer", "甲"], "   \n\n")
            self.assertEqual(rc, 1)
            self.assertIn("视角内容为空", err)
            self.assertFalse(os.path.exists(os.path.join(tmp, "viewers", "甲.md")))

    def test_rejects_wrong_arity(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, _out, err = self._run(tmp, ["--set-viewer"], "内容\n")
            self.assertEqual(rc, 1)
            self.assertIn("用法", err)
            rc2, _out2, err2 = self._run(tmp, ["--set-viewer", "甲", "乙"], "内容\n")
            self.assertEqual(rc2, 1)



    def test_refuses_to_write_when_existing_set_is_invalid(self):
        """B′：viewers/ 里**已有**坏文件时，不得"先写成功、再以 rc≠0 退出"。

        半成功是职责边界问题：rc≠0 却在磁盘上留下了新文件——调用方无法从
        退出码判断"到底写没写"。所以集合级校验必须在**读 stdin / 写文件之前**。
        """
        with tempfile.TemporaryDirectory() as tmp:
            vdir = os.path.join(tmp, "viewers")
            os.makedirs(vdir)
            with open(os.path.join(vdir, "甲.md"), "w", encoding="utf-8") as f:
                f.write("")                                       # 坏：空正文
            rc, out, err = self._run(tmp, ["--set-viewer", "乙"], "镜头乙\n")
            self.assertEqual(rc, 1)
            self.assertIn("视角任务书不能为空", err)
            self.assertIn("本次未写入任何文件", err)
            self.assertFalse(os.path.exists(os.path.join(vdir, "乙.md")), "不得有半成功")
            self.assertNotIn("已写入", out)

    def test_dangling_symlink_is_not_overwritten(self):
        """`lexists`：悬空符号链接也算"已存在"（exists 会漏掉 → 变成覆盖写入）。"""
        with tempfile.TemporaryDirectory() as tmp:
            vdir = os.path.join(tmp, "viewers")
            os.makedirs(vdir)
            os.symlink(os.path.join(tmp, "nowhere"), os.path.join(vdir, "甲.md"))
            rc, _out, err = self._run(tmp, ["--set-viewer", "甲"], "内容\n")
            self.assertEqual(rc, 1)
            self.assertIn("不覆盖", err)
            self.assertTrue(os.path.islink(os.path.join(vdir, "甲.md")))
if __name__ == "__main__":
    unittest.main()


class TestMachineMarkers(unittest.TestCase):
    """pi extension 与 CLI 的**机器契约**：标记行（扩展不解析人类文案）。

    为什么锁住它：`extensions/multi-viewers/index.ts` 靠 `[prepare] spec=` /
    `[start] dir=` / `[start] watch=` 取值——人类文案可以改，这三行不能消失，
    否则扩展静默拿不到路径/观看命令（用户 2026-09-24 拍板的 extension 形态）。
    """

    def test_prepare_prints_spec_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(mv_cli, "_call", return_value=0), \
                    mock.patch.object(mv_cli.os, "getcwd", return_value=tmp):
                rc, out, _err = run_main(["--prepare", "主题"])
        self.assertEqual(rc, 0)
        markers = [l for l in out.splitlines() if l.startswith("[prepare] spec=")]
        self.assertEqual(len(markers), 1, out)
        spec = markers[0].split("=", 1)[1]
        self.assertTrue(os.path.isabs(spec), spec)
        self.assertTrue(os.path.basename(spec).startswith("mv-spec-"), spec)

    def test_start_prints_dir_and_watch_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = os.path.join(tmp, "mv-spec-x")
            os.makedirs(spec)
            with open(os.path.join(spec, "question.md"), "w") as f:
                f.write("# 分析主题：T\n")
            with mock.patch.object(mv_cli, "_call", return_value=0), \
                    mock.patch.dict(os.environ, {"PI_SESSION_ID": "sid9"}), \
                    mock.patch.object(mv_cli.os, "getcwd", return_value=tmp):
                rc, out, _err = run_main(["--start", spec])
        self.assertEqual(rc, 0)
        dir_m = [l for l in out.splitlines() if l.startswith("[start] dir=")]
        watch_m = [l for l in out.splitlines() if l.startswith("[start] watch=")]
        self.assertEqual(len(dir_m), 1, out)
        self.assertEqual(len(watch_m), 1, out)
        d = dir_m[0].split("=", 1)[1]
        w = watch_m[0].split("=", 1)[1]
        self.assertTrue(os.path.basename(d).startswith("mv-sid9-"), d)
        self.assertEqual(w, f'!!python3 "{mv_cli.HUMAN_VIEWER}" {d} --follow')
        # 人类块里那一行 = **同一字符串**（单一来源，不重复拼）
        self.assertEqual(out.count(w), 2, out)
