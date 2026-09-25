"""`cleanup_discussion` 的**源码结构契约**：该函数内不得直接调用 `print(`。

为什么需要这种断言（2026-09-25 评审批 F4）：契约原本只写在 docstring 里
（"该函数内不得裸 print，可 grep 校验"）——**是契约，没有机制**。行为用例
（`test_main_paths.TestCleanupPrintsReport.test_display_failure_does_not_block_cleanup`
等）覆盖的是**已存在的发射点**，而"以后还会加发射点"是已知趋势（O(发射点数)），
所以这里改判**结构**：只要函数被找到、其 AST 子树内没有 `Call(Name('print'))`，
就与发射点数量无关（O(1)）。

形态要点（三方裁决，勿改动）：
- **契约收窄**为"不得直接调用 `print(`"（不假装锁 `sys.stdout.write` 之类；
  已核实这两个函数体内 `sys.*` 属性访问 0 处，属理论缺口）。
- **找不到 `cleanup_discussion` 即红** ✗（改名/搬家后退化成"永远绿的假契约"
  比没有更糟）。
- 本仓**首个源码结构断言**（`tests/` 无先例）；与函数名耦合是显式成本。
- 残差：若将来把报告段提成独立助手函数，本断言的范围需随之扩展（否则
  新函数里的裸 print 逃过检查）。
"""

import ast
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET_FUNC = "cleanup_discussion"
TARGET_FILE = os.path.join(REPO, "start_discussion.py")


def _function_node(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


class TestCleanupPrintContract(unittest.TestCase):
    def setUp(self):
        with open(TARGET_FILE, encoding="utf-8") as f:
            self.tree = ast.parse(f.read(), filename=TARGET_FILE)

    def test_target_function_exists(self):
        """找不到目标函数 → 红（防"假契约"：改名后断言静默变空）。"""
        self.assertIsNotNone(
            _function_node(self.tree, TARGET_FUNC),
            f"{TARGET_FILE} 里找不到 {TARGET_FUNC}——改名/搬家后必须同步本断言"
            f"（否则契约形同虚设）",
        )

    def test_no_bare_print_inside(self):
        """`cleanup_discussion` 内不得直接 `Call(Name('print'))`。

        范围 = 该函数的整个 AST 子树（含内层 try/except 与循环）。
        """
        func = _function_node(self.tree, TARGET_FUNC)
        bad = []
        for node in ast.walk(func):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "print"):
                bad.append(node.lineno)
        self.assertEqual(
            bad, [],
            f"{TARGET_FUNC} 内出现裸 print( 调用（行 {bad}）——"
            f"清理路径的 stdout 必须走 _print_best_effort（见其 docstring）",
        )


if __name__ == "__main__":
    unittest.main()
