#!/usr/bin/env bash
# 残留检查器——测试/验证结束后的清理验收（一条命令代替人工逐项记忆）
#
# 用法: ./scripts/check-residue.sh [--verbose]
# 退出码: 0 = 干净；1 = 有残留（列出清单）
#
# 检查三类测试残留形态（2026-09-09 建立——此前靠人工记忆逐项检查，
# 反复漏检：afk-e2e 引导 session 漏删即反例）：
#   1. 主 pi 侧 session：~/.pi/agent/sessions/<编码目录>/*.jsonl 中
#      近 24h 创建且 cwd 不在白名单（真实项目）的——测试引导 session
#      散落形态（--tmp-xxx-- 等）
#   2. 讨论进程：meeting_loop / pi --mode json 子进程
#   3. 讨论环境目录：$PWD 下 discuss-* 残留（另有 gitignore 兜底不入库）
#      + 结构识别（2026-09-10 补）：含 repo.git/ 与 pi-sessions/ 的目录
#      ——测试脚手架常用 /tmp/mv-*/disc 等非 discuss-* 命名，纯命名匹配
#      会漏检（e2e8 残留即此盲区），改为按环境结构识别
#
# 白名单：cwd 为真实项目目录的 session 不算残留（mv-main 等长期会话）。
WHITELIST=(
    "/root/pi-multi-viewers"
    "/root/pi-agents-helper"
    "/root/pi-agents-meeting-discuss"
    "/root/research"
    "/root/book"
    "/root/.dsh"
)

# 测试引导 session 登记日志（meeting_fs.build_bootstrap 每次创建追加）：
# 报告对照此日志标注归属——已登记 = 本流程测试产物（可删）；未登记 =
# 非本流程创建（真实会话，勿删）。不靠人猜（2026-09-09 用户：建立时
# 记录 log 才方便回查，人工确认是推卸责任）。
REGISTRY="$HOME/.pi/pi-multi-viewers-test-sessions.log"
is_registered() {
    # $1 = session 文件路径；按 header 的 session_id 对照登记日志
    # （build_bootstrap 登记 id；文件路径会因 --session 续写/复制变化，
    # id 才是稳定标识）。登记过 = 本流程测试产物。
    local sid
    sid=$(head -1 "$1" | python3 -c "import json,sys; print(json.loads(sys.stdin.readline()).get('id',''))" 2>/dev/null)
    [ -n "$sid" ] && grep -q "\"session_id\": \"$sid\"" "$REGISTRY" 2>/dev/null
}

RESIDUE=0

say() { [ "$1" = "--verbose" ] || [ -n "$VERBOSE" ] && echo "$2"; }

# --- 1. 主 pi 侧近期 session（非白名单 cwd） ---
SESS_DIR="$HOME/.pi/agent/sessions"
NOW=$(date +%s)
for d in "$SESS_DIR"/*/; do
    [ -d "$d" ] || continue
    # 编码目录名还原 cwd 近似判断（--tmp-xxx-- / --root-xxx-- 形态）
    enc=$(basename "$d")
    for f in "$d"*.jsonl; do
        [ -f "$f" ] || continue
        age=$((NOW - $(stat -c %Y "$f")))
        if [ "$age" -gt 86400 ]; then
            continue  # 超过 24h，不是本次测试产物
        fi
        # 解析 header cwd（第一行 JSON 的 cwd 字段）
        cwd=$(head -1 "$f" | python3 -c "import json,sys; print(json.loads(sys.stdin.readline()).get('cwd',''))" 2>/dev/null)
        whitelisted=0
        for w in "${WHITELIST[@]}"; do
            case "$cwd" in
                "$w"*|"$w") whitelisted=1 ;;
            esac
        done
        if [ "$whitelisted" = "0" ]; then
            # 归属判定（2026-09-09）：登记日志回查——已登记=测试产物
            # （可删），未登记=非本流程创建（勿删，提示加白名单）。
            # 首条消息摘要仅作补充信息（不再作为删除判断依据）。
            if is_registered "$f"; then
                owner="[已登记-测试产物，可删]"
            else
                owner="[未登记-非本流程创建，勿删]"
                RESIDUE=1
            fi
            first_user=$(python3 -c "
import json, sys
try:
    for l in open('$f'):
        e = json.loads(l)
        if e.get('type') == 'message' and e.get('message', {}).get('role') == 'user':
            c = e['message'].get('content')
            t = c if isinstance(c, str) else ''.join(x.get('text','') for x in (c or []) if isinstance(x, dict) and x.get('type') == 'text')
            print((t or '')[:60].replace('\\n', ' '))
            break
except Exception:
    print('')
" 2>/dev/null)
            echo "[残留-1] session（24h 内 cwd=$cwd，创建 $(stat -c %y "$f" | cut -d. -f1)）$owner"
            echo "        首条消息: ${first_user:-（无 user 消息）}"
            echo "        路径: $f"
        fi
    done
done

# --- 2. 讨论/pi 进程 ---
# 判据 = **argv 逐项精确匹配**（读 /proc/<pid>/cmdline，NUL 分隔的原始 argv），
# 不是命令行文本匹配：文本匹配会把"命令行里恰好提到 meeting_loop.py"的
# 调用者自身也算进来（bash -c / grep 都在此列），过度依赖 grep -v 白名单
# 过滤又会漏检（任何命令行含 "bash -c" 的真实进程）。
# argv 精确匹配无此二难：`python3 .../meeting_loop.py <work> <agent>` 的
# argv[1] 恰等于脚本路径；调用者的 -c 脚本只是**一个** argv 元素，不等。
# 与 start_discussion._loop_pids 同一判据（python 侧用于存活检测）。
_proc_argv_has() {   # $1=pid 目录，$2=精确参数
    local d="$1" arg
    while IFS= read -r -d '' arg; do
        [ "$arg" = "$2" ] && return 0
    done < "$d/cmdline" 2>/dev/null
    return 1
}
for d in /proc/[0-9]*; do
    [ -r "$d/cmdline" ] || continue
    pid="${d#/proc/}"
    # meeting_loop：argv 里出现以 meeting_loop.py 结尾的**独立参数**
    while IFS= read -r -d '' arg; do
        case "$arg" in
            */meeting_loop.py)
                echo "[残留-2] meeting_loop 进程 PID=$pid: $(ps -p "$pid" -o cmd= 2>/dev/null)"
                RESIDUE=1
                break
                ;;
        esac
    done < "$d/cmdline" 2>/dev/null
    # pi 进程：argv 里同时有 --mode 与 json 两个独立参数（fork 模式唤醒形态）
    if _proc_argv_has "$d" "--mode" && _proc_argv_has "$d" "json"; then
        echo "[残留-2] pi 进程 PID=$pid"
        RESIDUE=1
    fi
done

# --- 3. 讨论环境目录残留 ---
# 3a. 命名形态：$PWD 下 discuss-*（兜底：半创建、尚无 repo.git 的环境）
for d in discuss-*/; do
    [ -d "$d" ] || continue
    echo "[残留-3] 讨论目录: $PWD/$d"
    RESIDUE=1
done

# 3b. 结构形态：任何含 repo.git/ + pi-sessions/ 的目录 = 讨论环境
#（扫描 $PWD 与 /tmp，深度 ≤3；与 3a 去重）
# 去重用空格包裹的字符串匹配（不用 declare -A：避免 bash 4+ 依赖，
# 路径不含空格——本仓库路径约定）
SEEN_ENVS=""
for base in "$PWD" "${TMPDIR:-/tmp}"; do
    [ -d "$base" ] || continue
    while IFS= read -r g; do
        d=$(dirname "$g")
        [ -d "$d/pi-sessions" ] || continue
        case " $SEEN_ENVS " in *" $d "*) continue ;; esac
        SEEN_ENVS="$SEEN_ENVS $d"
        echo "[残留-3] 讨论环境: $d（$(du -sh "$d" 2>/dev/null | cut -f1)）"
        RESIDUE=1
    done < <(find "$base" -maxdepth 3 -type d -name repo.git 2>/dev/null)
done

if [ "$RESIDUE" = "0" ]; then
    echo "干净：无测试残留"
fi
exit "$RESIDUE"
