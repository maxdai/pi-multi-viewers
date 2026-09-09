#!/usr/bin/env bash
# run_tests.sh —— 测试运行 + 观察分离（用户 2026-09-01 定）
#
# 问题：全量 unittest 250s+，同一输出多次观察（Ran/OK/FAILED/详情）重跑
# 浪费——每次"观察"（管道 grep）都绑一次完整运行。
# 方案（层 1，默认）：每次真跑 + tee tests/.cache/last.log；所有观察点
#   从落盘文件读（grep xxx last.log），观察不再触发运行。
#   真跑保留：源码/环境可能变，真跑是唯一可靠验证（不默认跳过）。
# 方案（层 2，显式 --reuse）：源码指纹未变 + 上次 OK → 跳过运行直接
#   输出缓存。风险：环境因素/flaky 不体现在指纹里，会掩盖真实失败——
#   因此默认关闭，仅快查场景显式开启；最终回归/诊断必须真跑。
#
# 用法：
#   ./tests/run_tests.sh                      # 真跑 + tee last.log
#   ./tests/run_tests.sh tests.test_viewer    # 单文件（参数参与指纹）
#   ./tests/run_tests.sh --reuse              # 指纹未变 + 上次 OK → 跳过
#   ./tests/run_tests.sh --force              # 强制真跑（清 reuse 命中）
# 观察（跑完后，0 秒）：
#   grep "^Ran" tests/.cache/last.log
#   grep -E "^(FAIL|ERROR):" tests/.cache/last.log

set -u

HERE="$(cd "$(dirname "$0")/.." && pwd)"
CACHE_DIR="${TEST_CACHE_DIR:-$HERE/tests/.cache}"
mkdir -p "$CACHE_DIR"
LAST_LOG="$CACHE_DIR/last.log"
FP_FILE="$CACHE_DIR/last-fingerprint"

FORCE=0
REUSE=0
ARGS=()
for a in "$@"; do
    case "$a" in
        --force) FORCE=1 ;;
        --reuse) REUSE=1 ;;
        *) ARGS+=("$a") ;;
    esac
done
if [ "${#ARGS[@]}" -eq 0 ]; then
    ARGS=(discover tests)
fi

# 源码指纹（内容 md5：mtime 变内容不变不失效）+ 命令参数
FILES="$(cd "$HERE" && find . \( -name '*.py' -o -name '*.sh' -o -name '*.tpl' \) \
    -not -path './.git/*' -not -path './tests/.cache/*' | sort)"
SRC_HASH="$(echo "$FILES" | xargs md5sum 2>/dev/null | md5sum | cut -d' ' -f1)"
ARG_HASH="$(echo "${ARGS[*]}" | md5sum | cut -d' ' -f1)"

last_ok() {
    grep -qE '^OK( \(expected failures=[0-9]+\))?$' "$LAST_LOG"
}

if [ "$FORCE" -eq 1 ]; then
    echo "[run_tests] --force 强制真跑" >&2
elif [ "$REUSE" -eq 1 ] && [ -f "$LAST_LOG" ] && [ -f "$FP_FILE" ] \
        && [ "$(cat "$FP_FILE")" = "${SRC_HASH}_${ARG_HASH}" ] && last_ok; then
    echo "[run_tests] --reuse 命中（源码/参数未变，上次 OK）——直接输出缓存；真跑用 --force" >&2
    cat "$LAST_LOG"
    exit 0
elif [ "$REUSE" -eq 1 ] && [ -f "$LAST_LOG" ]; then
    echo "[run_tests] --reuse 未命中（源码变/上次有错）——真跑" >&2
fi

cd "$HERE"
python3 -m unittest "${ARGS[@]}" 2>&1 | tee "$LAST_LOG"
rc="${PIPESTATUS[0]}"
echo "${SRC_HASH}_${ARG_HASH}" > "$FP_FILE"
echo "[run_tests] 完整输出已落盘: $LAST_LOG（观察用 grep xxx $LAST_LOG，0 秒）" >&2
exit "$rc"
