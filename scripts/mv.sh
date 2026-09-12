#!/usr/bin/env bash
#
# mv.sh —— 多视角协同分析的**稳定入口**（shim）。
#
# 逻辑在 mv_cli.py（见该文件首注释：为什么这层从 364 行 bash 收敛为 Python——
# bash 陷阱产出的真实 bug 与"零行覆盖"的盲区）。本文件只做一件事：找到仓库
# 根、把展示名（$0 语义）与参数交给 mv_cli.py。
#
# **路径不能变**：prompt / README / 用户习惯都引用 scripts/mv.sh。
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# MV_INVOKED_AS：usage 与启动块里展示用户实际敲的那条命令（沿用 bash 的
# $0 语义；直接跑 mv_cli.py 时它会退回默认名）
MV_INVOKED_AS="${MV_INVOKED_AS:-$0}" \
exec "${PYTHON:-python3}" "$ROOT_DIR/mv_cli.py" "$@"
