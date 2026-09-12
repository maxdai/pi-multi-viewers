#!/usr/bin/env bash
#
# pi-probe.sh —— LLM 探针（把临时 pi 运行变成**可追溯的测试产物**）
#
# 为什么需要（2026-09-12 实测教训）：
#   裸跑 `pi --print "..."` 会在 `~/.pi/agent/sessions/<cwd 编码目录>/` 留下
#   一个**未登记**的 session。check-residue.sh 对这类文件的归属判定是
#   "登记日志里有没有"——没有登记：
#     - cwd 不在白名单 → 报 [残留-1][未登记-非本流程创建，勿删]（人得自己认领）
#     - cwd 在白名单（真实项目）→ **直接跳过**，连报都不报
#   那天 4 个探针 session 就是这样留下的：其中一个跑在 /root/pi-agents-helper
#   （白名单）→ 完全不可见；另外几个在 /tmp 下 → 报出来了，但被调用方
#   `grep 残留-2|残留-3` 滤掉（调用纪律另见表）。
#
#   本脚本 = 跑 pi **并登记**它新建的 session（写进与 build_bootstrap 同一份
#   登记日志 `~/.pi/pi-multi-viewers-test-sessions.log`）→ check-residue 会
#   报"[已登记-测试产物，可删]"，归属不再需要人猜。
#
# 用法:
#   ./scripts/pi-probe.sh [pi 参数...]        # 例: ./scripts/pi-probe.sh --print "ok"
#   ./scripts/pi-probe.sh --no-extensions --print "ok"
#
# 退出码 = pi 的退出码。新建的 session 路径会打印出来（用完记得删）。
set -u

SESS_DIR="$HOME/.pi/agent/sessions"
REGISTRY="$HOME/.pi/pi-multi-viewers-test-sessions.log"

if [ "$#" -eq 0 ]; then
    echo "用法: $0 [pi 参数...]   （例: $0 --print \"ok\"）" >&2
    exit 2
fi

BEFORE="$(mktemp)"
AFTER="$(mktemp)"
trap 'rm -f "$BEFORE" "$AFTER"' EXIT
find "$SESS_DIR" -name '*.jsonl' -print 2>/dev/null | sort > "$BEFORE"

pi "$@"
rc=$?

find "$SESS_DIR" -name '*.jsonl' -print 2>/dev/null | sort > "$AFTER"
NEW=$(comm -13 "$BEFORE" "$AFTER")
if [ -z "$NEW" ]; then
    echo "[pi-probe] 未新建 session（用了 --session/--session-id 续接既有会话？）"
    exit "$rc"
fi

echo "$NEW" | while IFS= read -r f; do
    [ -n "$f" ] || continue
    python3 - "$f" "$REGISTRY" <<'PYEOF'
import json, os, sys
from datetime import datetime, timezone

path, registry = sys.argv[1], sys.argv[2]
sid = cwd = ""
with open(path, encoding="utf-8") as f:
    for line in f:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "session":
            sid = ev.get("id") or ""
            cwd = ev.get("cwd") or ""
            break
rec = {
    "created": datetime.now(timezone.utc).isoformat(),
    "session_id": sid,
    "path": os.path.abspath(path),
    "cwd": cwd,
    "origin": "pi-probe",     # 与 build_bootstrap 的引导 session 区分
}
with open(registry, "a", encoding="utf-8") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
print(f"[pi-probe] 已登记: {path}")
print(f"[pi-probe]   session_id={sid} cwd={cwd}")
print(f"[pi-probe]   （用完删掉该文件；check-residue 会报它）")
PYEOF
done
exit "$rc"
