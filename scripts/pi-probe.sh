#!/usr/bin/env bash
#
# pi-probe.sh —— LLM 探针（把临时 pi 运行变成**可追溯、经同意的测试产物**）
#
# 它管三件事（都是吃过的教训）：
#
# 1. **闸门：没有当次同意就不跑**（2026-09-13 教训）
#    我在未征得用户同意的情况下连跑 6 个真实 pi 进程（合计 ~9.5 分钟，其中一个
#    458s），理由是"在后台跑、不占用户终端"。判据本应是**预估时长**（用户规则：
#    秒级冒烟免批；分钟级/多轮必须当次确认），且**不因后台运行而豁免**。
#    现在：不带 `--approved "<凭据>"` 直接**拒绝执行**并提示先去问。
#
# 2. **登记：跑过的 LLM 运行留下账本**
#    `~/.pi/pi-multi-viewers-llm-runs.log` 每行一次运行：起止时间、时长、退出码、
#    凭据、命令摘要。汇报时给账本，不靠记忆。
#
# 3. **收尾提醒：跑完先汇报，再决定下一步**
#    结束时打印一行显式提醒——2026-09-13 的第二个错误正是"跑完没汇报就接着跑
#    第二批实验"。
#
# 另外它把新建的 session 登记进 `~/.pi/pi-multi-viewers-test-sessions.log`
# （与 build_bootstrap 同一份），使 check-residue 能判定归属（否则白名单 cwd
# 下的探针 session 完全不可见）。
#
# 用法:
#   ./scripts/pi-probe.sh --approved "用户 2026-09-13 15:20 同意跑 AFT 隔离实验" \
#       --print "ok"
#   ./scripts/pi-probe.sh --approved "..." --no-extensions --print "ok"
#
# 退出码 = pi 的退出码（闸门拒绝时为 2）。
set -u

SESS_DIR="$HOME/.pi/agent/sessions"
REGISTRY="$HOME/.pi/pi-multi-viewers-test-sessions.log"
LEDGER="$HOME/.pi/pi-multi-viewers-llm-runs.log"

usage() {
    cat >&2 <<'EOS'
用法: pi-probe.sh --approved "<凭据>" [pi 参数...]

  --approved "<凭据>"   必填：用户**当次**同意的凭据（原话/时间/授权范围）。
                        没有它就说明还没问——先去问，不要绕过这里直接调 pi。

判据（用户规则）：预估**时长**——秒级冒烟免批；预计 ≥30s、多轮唤醒、
整场分析一律**当次**确认，且不因"后台跑、不占用户终端"豁免。
EOS
}

APPROVED=""
if [ "$#" -gt 0 ] && [ "$1" = "--approved" ]; then
    APPROVED="${2:-}"
    shift 2 2>/dev/null || true
fi
if [ -z "$APPROVED" ]; then
    echo "拒绝执行：没有 --approved（= 未取得用户当次同意）。" >&2
    usage
    exit 2
fi
if [ "$#" -eq 0 ]; then
    echo "拒绝执行：缺少 pi 参数。" >&2
    usage
    exit 2
fi

cmd_brief="pi $*"

BEFORE="$(mktemp)"
AFTER="$(mktemp)"
trap 'rm -f "$BEFORE" "$AFTER"' EXIT
find "$SESS_DIR" -name '*.jsonl' -print 2>/dev/null | sort > "$BEFORE"

T0=$(date +%s)
pi "$@"
rc=$?
T1=$(date +%s)

# 账本（运行即登记：时长 / 退出码 / 凭据 / 命令摘要）
python3 - "$LEDGER" "$T0" "$T1" "$rc" "$APPROVED" "$cmd_brief" <<'PYEOF'
import json, sys
from datetime import datetime, timezone
ledger, t0, t1, rc, approved, cmd = sys.argv[1:7]
rec = {
    "started": datetime.fromtimestamp(int(t0), timezone.utc).isoformat(),
    "seconds": int(t1) - int(t0),
    "rc": int(rc),
    "approved": approved,
    "cmd": cmd[:400],
}
with open(ledger, "a", encoding="utf-8") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
print(f"[pi-probe] 账本已记：{rec['seconds']}s rc={rec['rc']} | 同意凭据：{approved[:60]}")
PYEOF

# session 登记（归属判定用）
find "$SESS_DIR" -name '*.jsonl' -print 2>/dev/null | sort > "$AFTER"
NEW=$(comm -13 "$BEFORE" "$AFTER")
if [ -n "$NEW" ]; then
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
    "origin": "pi-probe",
}
with open(registry, "a", encoding="utf-8") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
print(f"[pi-probe] session 已登记: {path}")
print(f"[pi-probe]   session_id={sid} cwd={cwd}（用完删掉该文件）")
PYEOF
    done
else
    echo "[pi-probe] 未新建 session（用了 --session/--session-id 续接既有会话？）"
fi

# 收尾提醒（2026-09-13 教训：跑完先汇报，别接着跑下一批）
echo "[pi-probe] ⚠ 先把本次结果汇报给用户并停下等他指示——不要连续追加第二批实验。"
exit "$rc"
