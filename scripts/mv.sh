#!/usr/bin/env bash
#
# pi-multi-viewers wrapper——多视角协同分析（fork 主 session + meeting 协议）
#
# 用法：
#   ./scripts/mv.sh --prepare "<主题>" [--agents "a,b,c"|4]
#   ./scripts/mv.sh --start <spec目录>
#   ./scripts/mv.sh --status <dir>
#   ./scripts/mv.sh --wait <dir>
#   ./scripts/mv.sh --cleanup <dir>
#   ./scripts/mv.sh --view <dir> [--since <ref>]     # human-viewer 封装
#   ./scripts/mv.sh --say <dir> "<文本>"              # human-sayer 封装
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
PYTHON="${PYTHON:-python3}"
START_DISCUSSION="$ROOT_DIR/start_discussion.py"
HUMAN_VIEWER="$ROOT_DIR/human_viewer.py"
HUMAN_SAYER="$ROOT_DIR/human_sayer.py"

DEFAULT_AGENTS="a,b,c"
DEFAULT_MAX_MEETING=10
DEFAULT_MAX_RR=5

usage() {
    cat <<'USAGE_EOF'
用法:
  $0 --prepare "<问题>" [--background "<背景>"] [--agents "a,b,c"|4]
  $0 --start <spec目录>
  $0 --status <dir>
  $0 --wait <dir>
  $0 --cleanup <dir>
  $0 --view <dir> [--since <ref>]
  $0 --say <dir> "<文本>"

默认参数:
  agents=a,b,c  max-meeting=10  max-rr=5

--agents: 逗号分隔名称列表（如 "x,y"）或纯数字（如 4 → 生成 a..d）；
          human 是保留名，不能作为参与者

human 通道:
  --view: 增量查看分析进展（--since 之后的新消息+状态；末尾输出 HEAD=<hash>，主 pi 记录作下轮 --since）
  --say:  插话（写一条 human 消息，agents 可见并可回应）
USAGE_EOF
}

fail() {
    echo "错误: $*" >&2
    exit 1
}

# 检查 aft 是否关闭 bash 接管（用户 2026-08-31：0.2.0 依赖 PI_SESSION_ID 等
# 注入——aft 接管 bash 后这些变量不再注入，插话扩展找不到分析目录）
# 仅 --prepare/--start 需要（模型继承 + 目录含 sid）；不阻断（手动跑分析
# 仍可用），只给醒目警告。
check_aft_bash() {
    local cfg="$HOME/.config/cortexkit/aft.jsonc"
    local json=""
    if [ -f "$cfg" ]; then
        json="$(cat "$cfg")"
    else
        # 兼容旧路径 aft.json
        [ -f "$HOME/.config/cortexkit/aft.json" ] && json="$(cat "$HOME/.config/cortexkit/aft.json")"
    fi
    if [ -z "$json" ]; then
        echo "[aft] 警告: 未找到 $HOME/.config/cortexkit/aft.jsonc" >&2
        echo "[aft]   本工具需要 \"bash\": false（关闭 aft 对 bash 的接管），否则" >&2
        echo "[aft]   插话扩展找不到分析目录、models.md 退化为兜底值。" >&2
        echo "[aft]   修复: 在 $HOME/.config/cortexkit/aft.jsonc 中添加 \"bash\": false 并重启 pi。" >&2
        return
    fi
    # 粗解析：bash 顶层字段（jsonc 允许注释，逐行剔除）
    if ! echo "$json" | sed 's|//.*||' | grep -q '"bash"[[:space:]]*:[[:space:]]*false'; then
        echo "[aft] 警告: $HOME/.config/cortexkit/aft.jsonc 中未设置 \"bash\": false" >&2
        echo "[aft]   当前 aft 会接管 bash 工具，PI_SESSION_ID 等环境变量不注入——" >&2
        echo "[aft]   插话扩展找不到分析目录、models.md 退化为兜底值。" >&2
        echo "[aft]   修复: 添加 \"bash\": false 并重启 pi。" >&2
    fi
}

require_dir() {
    local dir="$1"
    [ -n "$dir" ] || fail "缺少目录参数"
    [ -d "$dir" ] || fail "目录不存在: $dir"
}

# 目录参数规范化：裸名（无路径符）会被 start_discussion 加 discussion-
# 前缀导致找错目录（实测 2026-09-03）——所有消费命令入口统一转绝对路径
normalize_dir() {
    readlink -f "$1"
}

cmd_status() {
    local dir
    dir="$(normalize_dir "$1")"
    require_dir "$dir"
    "$PYTHON" "$START_DISCUSSION" --dir "$dir" --status
}

cmd_wait() {
    local dir
    dir="$(normalize_dir "$1")"
    require_dir "$dir"
    "$PYTHON" "$START_DISCUSSION" --dir "$dir" --wait
}

cmd_cleanup() {
    local dir
    dir="$(normalize_dir "$1")"
    require_dir "$dir"
    "$PYTHON" "$START_DISCUSSION" --dir "$dir" --cleanup
}

cmd_view() {
    local dir since=""
    dir="$(normalize_dir "$1")"
    shift
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --since)
                [ "$#" -ge 2 ] || fail "--since 需要一个值"
                since="$2"
                shift 2
                ;;
            *)
                fail "未知参数: $1（--view 只接受 --since）"
                ;;
        esac
    done
    require_dir "$dir"
    [ -d "$dir/repo.git" ] || fail "分析不存在: $dir（无 repo.git）"
    if [ -n "$since" ]; then
        "$PYTHON" "$HUMAN_VIEWER" "$dir" --since "$since"
    else
        "$PYTHON" "$HUMAN_VIEWER" "$dir"
    fi
    # HEAD 游标（主 pi 记录作下轮 --since；无状态，不写游标文件——
    # 与 --follow 的 .viewer-cursor 互不干扰）
    echo "HEAD=$(git -C "$dir/repo.git" rev-parse HEAD)"
}

cmd_say() {
    local dir text="${2:-}"
    dir="$(normalize_dir "$1")"
    require_dir "$dir"
    [ -d "$dir/work-human" ] || fail "分析缺少 work-human: $dir"
    [ -n "$text" ] || fail "插话文本不能为空"
    "$PYTHON" "$HUMAN_SAYER" "$dir" "$text"
}

# 定位主 pi 的 session 文件：优先 PI_SESSION_FILE，否则用当前 cwd 编码路径查找
# 读取主 pi 的 model/thinking（用户 2026-08-31：aft 不再替换 bash 后
# 环境变量可用且是当前生效值——优先环境变量，session 文件解析仅为兜底）
# agents 列表（DEFAULT_AGENTS 逗号分隔 → 行分隔写入 .order；
# --agents 可覆盖：名称列表 "a,b,c" 或纯数字 "4"（生成 a..<n>））
cmd_prepare() {
    check_aft_bash
    local topic="" background="" agents_list=""
    if [ "$#" -lt 1 ]; then
        usage >&2
        exit 2
    fi
    topic="$1"
    shift
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --background)
                [ "$#" -ge 2 ] || fail "--background 需要一个值"
                background="$2"
                shift 2
                ;;
            --agents)
                [ "$#" -ge 2 ] || fail "--agents 需要一个值（逗号分隔名称列表或数字）"
                agents_list="$2"
                shift 2
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                fail "未知参数: $1（--prepare 只接受 <问题>、--background、--agents）"
                ;;
        esac
    done
    [ -n "$topic" ] || fail "问题不能为空"

    # --agents 未传 = viewers 模式骨架（无 agents/ 目录——启动时从
    # 项目 cwd/viewers/ 发现视角；--agents 显式指定 = 覆盖 viewers）
    # 数字 → 生成 a..<n> 名称列表
    if [[ "$agents_list" =~ ^[0-9]+$ ]]; then
        local n="$agents_list" name="" list=""
        [ "$n" -ge 2 ] || fail "agents 数量至少为 2（meeting 至少两个 LLM agents）"
        [ "$n" -le 26 ] || fail "agents 数量最多 26（a..z）"
        for ((i = 0; i < n; i++)); do
            name=$(printf "\\$(printf '%03o' $((97 + i)))")
            list="${list:+$list,}$name"
        done
        agents_list="$list"
    fi
    # 转行分隔 + 校验保留名 human
    local agents_lines agents_name
    agents_lines="$(echo "$agents_list" | tr ',' '\n' | sed '/^[[:space:]]*$/d')"
    for agents_name in $agents_lines; do
        [ "$agents_name" != "human" ] || fail "human 是保留名，不能作为参与者"
    done

    local stamp
    stamp="$(date +%Y%m%d-%H%M%S)"
    local spec_dir="$PWD/mv-spec-${stamp}"
    mkdir -p "$spec_dir"
    local agents_lines="" agents_name=""
    if [ -n "$agents_list" ]; then
        mkdir -p "$spec_dir/agents"
        agents_lines="$(echo "$agents_list" | tr ',' '\n' | sed '/^[[:space:]]*$/d')"
    fi

    # 骨架生成 = start_discussion --spec-gen（单一事实源，2026-09-09 收敛：
    # 此前 bash/python 双实现并存——models 主 pi 预填等语义已在 python 侧
    # 对齐；agents 空列表 = viewers 模式骨架，由 python 决定不建 agents/）
    local skeleton_args=(--spec-gen "$spec_dir" --topic "$topic")
    [ -n "$background" ] && skeleton_args+=(--background "$background")
    [ -n "$agents_lines" ] && skeleton_args+=(--agents "$agents_list")
    "$PYTHON" "$START_DISCUSSION" "${skeleton_args[@]}" || {
        fail "spec 骨架生成失败（start_discussion --spec-gen）"
    }

    cat <<OUTPUT_EOF
已生成分析 spec:
  $spec_dir

请查看/编辑该目录，补充背景、各 agent 视角等。
编辑完成后，告诉我"继续"，我会自动启动分析。
OUTPUT_EOF
}

cmd_start() {
    check_aft_bash
    local spec_dir="$1"
    require_dir "$spec_dir"
    [ -f "$spec_dir/question.md" ] || fail "spec 缺少 question.md: $spec_dir"

    local session_id="${PI_SESSION_ID:-}"
    local stamp
    stamp="$(date +%Y%m%d-%H%M%S)"
    local dir_name="discuss"
    if [ -n "$session_id" ]; then
        dir_name="${dir_name}-${session_id}"
    fi
    dir_name="${dir_name}-${stamp}"
    local dir_path="$PWD/$dir_name"

    # 第 1 步：创建分析环境（不启动）
    # fork 模式（多视角）：主 pi 触发时（PI_SESSION_ID 存在）解析当前
    # session 文件绝对路径 → protocol.json → 各 agent 首唤 --fork 挂载
    # 主 session 全量上下文。解析不到（手动 shell 跑 wrapper）不传参，
    # 退化为 legacy 形态。session 目录名编码（pi config.js）：
    # "--" + 去首尾斜杠后内斜杠换 "-" + "--"（/root/x → --root-x--）
    local fork_args=()
    if [ -n "${PI_SESSION_ID:-}" ]; then
        local tmp="${PWD#/}"; tmp="${tmp%/}"
        local enc="--${tmp//\//-}--"
        local fork_src
        fork_src="$(ls -t "$HOME/.pi/agent/sessions/$enc/"*"$PI_SESSION_ID".jsonl 2>/dev/null | head -1)"
        if [ -n "$fork_src" ]; then
            fork_args=(--fork-source "$fork_src")
            echo "[start] fork 源：$fork_src"
        else
            fail "未找到主 session 文件（$enc/*_$PI_SESSION_ID.jsonl）——fork-only 模式必须挂载主 session（无 session 时先在项目目录跑一次 pi --print 造引导 session）"
        fi
    else
        fail "PI_SESSION_ID 未注入——多视角分析必须在主 pi session 内经 wrapper 启动（见 README 环境要求）"
    fi
    if ! "$PYTHON" "$START_DISCUSSION" --dir "$dir_path" --spec "$spec_dir" --max-meeting "$DEFAULT_MAX_MEETING" --max-rr "$DEFAULT_MAX_RR" "${fork_args[@]+"${fork_args[@]}"}"; then
        fail "环境创建失败，请查看上方输出"
    fi

    # 第 2 步：在每个 work 目录写入项目级 .pi/settings.json，屏蔽 magic-context 和 aft
    # （pi config 权威写法：项目 delta = {source, autoload:false} + 资源
    # patterns 逐文件禁用——两包各只有 1 个扩展 dist/index.js → "-dist/index.js"。
    # 实测 2026-09-03 两次纠错：① 原 autoload:false 无 patterns = delta 空
    # 操作（用户级包照常全量加载，aft 进程照常 spawn）② 无 autoload:false
    # 的资源 [] 形态 = project wins → 触发项目级 npm install 到 .pi/npm
    # （每 work 一次 install magic-context）。delta 不装项目副本）
    # 注：fork 模式（--fork-source 传入，agent cwd=主项目）下此文件
    # 不生效（pi 读的是主项目 .pi/settings.json）——保留仅为 legacy
    # 形态兼容；多视角产品语义下上下文工具激活是特性，非问题。
    local agent
    # agents 列表来自 spec（--agents 参数化后不能硬编码 a b c——
    # 自定义数量/名称时硬编码会漏写 work-d/work-x 的 settings，
    # 2026-09-03 顺手修复）
    for agent in $(cat "$spec_dir/agents/.order" 2>/dev/null); do
        local workdir="$dir_path/work-$agent"
        if [ -d "$workdir" ]; then
            mkdir -p "$workdir/.pi"
            cat > "$workdir/.pi/settings.json" <<'SETTINGS_EOF'
{
  "packages": [
    {
      "source": "npm:@cortexkit/pi-magic-context",
      "autoload": false,
      "extensions": ["-dist/index.js"]
    },
    {
      "source": "npm:@cortexkit/aft-pi",
      "autoload": false,
      "extensions": ["-dist/index.js"]
    }
  ]
}
SETTINGS_EOF
        fi
    done

    # 临时 spec 已被消费，删除
    rm -rf "$spec_dir"

    # 第 3 步：启动已有环境
    if ! "$PYTHON" "$START_DISCUSSION" --dir "$dir_path" --skip-setup --start; then
        fail "启动失败，请查看上方输出"
    fi

    cat <<OUTPUT_EOF
多视角分析已启动
目录: $dir_path

观看分析（复制执行，不进 LLM；Ctrl-C 中断后可插话再续看）:
!!python3 "$ROOT_DIR/human_viewer.py" $dir_path --follow

查看进展: $0 --view $dir_path
插话: $0 --say $dir_path "<文本>"
查看状态: $0 --status $dir_path
等待完成: $0 --wait $dir_path
清理: $0 --cleanup $dir_path

说明:
- human 通道：--view 增量查看（主 pi 记录末尾 HEAD 作下轮 --since）；
  --say 插话（agents 可见并可回应；已冻结 agent 不响应）
- 完成后 result.md 自动保存到固定位置：$dir_path-result.md（与分析目录同级——resultWriter loop 退出时保存；cleanup 也会保存）
- 读取 result.md 摘要后请执行 --cleanup 清理分析目录
OUTPUT_EOF
}

if [ "$#" -ge 1 ]; then
    case "$1" in
        --prepare)
            shift
            cmd_prepare "$@"
            exit $?
            ;;
        --start)
            [ "$#" -ge 2 ] || fail "--start 需要 spec 目录参数"
            cmd_start "$2"
            exit $?
            ;;
        --status)
            [ "$#" -ge 2 ] || fail "--status 需要分析目录参数"
            cmd_status "$2"
            exit $?
            ;;
        --wait)
            [ "$#" -ge 2 ] || fail "--wait 需要分析目录参数"
            cmd_wait "$2"
            exit $?
            ;;
        --cleanup)
            [ "$#" -ge 2 ] || fail "--cleanup 需要分析目录参数"
            cmd_cleanup "$2"
            exit $?
            ;;
        --view)
            [ "$#" -ge 2 ] || fail "--view 需要分析目录参数"
            shift
            cmd_view "$@"
            exit $?
            ;;
        --say)
            [ "$#" -ge 3 ] || fail "--say 需要分析目录和文本参数"
            cmd_say "$2" "$3"
            exit $?
            ;;
        -h|--help)
            usage
            exit 0
            ;;
    esac
fi

usage >&2
exit 2