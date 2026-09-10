#!/usr/bin/env bash
#
# pi-multi-viewers wrapper——多视角协同分析（fork 主 session + meeting 协议）
#
# 用法：
#   ./scripts/mv.sh --prepare "<主题>" [--agents "a,b,c"|4]
#   ./scripts/mv.sh --start <spec目录>   # 可选 --fork-mode compaction|budget|full
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


usage() {
    cat <<'USAGE_EOF'
用法:
  $0 --prepare "<问题>" [--background "<背景>"] [--agents "a,b,c"|4]
  $0 --start <spec目录> [--fork-mode compaction|budget|full]
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

check_aft_bash() {
    # 预检：aft 是否关闭 bash 接管（只警告不阻断——手动跑讨论仍可用）。
    # W3 收归（e2e7 评审）：三段近重复文案合并；JSONC 解析改用 python
    # json 库（原 sed 剔注释 + grep 粗解析对字段位置/嵌套/多行值均不可靠）。
    _aft_warn() {
        echo "[aft] 警告: $1" >&2
        echo "[aft]   本工具需要 \"bash\": false（关闭 aft 对 bash 的接管），" >&2
        echo "[aft]   否则插话扩展找不到分析目录、models.md 退化为兜底值。" >&2
        echo "[aft]   修复: 在 $HOME/.config/cortexkit/aft.jsonc 中添加 " >&2
        echo "[aft]   \"bash\": false 并重启 pi。" >&2
    }
    local cfg="$HOME/.config/cortexkit/aft.jsonc"
    [ -f "$cfg" ] || cfg="$HOME/.config/cortexkit/aft.json"
    if [ ! -f "$cfg" ]; then
        _aft_warn "未找到 $HOME/.config/cortexkit/aft.jsonc"
        return
    fi
    # python 解析（jsonc：去注释后 json.loads；bash 字段 false 才算关闭）
    # 注释剥离必须**字符串感知**——朴素正则会把 `"$schema": "https://…"`
    # 里的 // 当注释吃掉 → 解析失败误报 unparseable（实测 2026-09-10）。
    local verdict
    verdict="$(python3 - "$cfg" <<'PYEOF'
import json, sys


def strip_jsonc(txt):
    """去 // 与 /* */ 注释（字符串感知：URL 里的 // 不能被吃）。"""
    out, i, n = [], 0, len(txt)
    in_str = esc = False
    while i < n:
        c = txt[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and txt[i + 1] == "/":
            while i < n and txt[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and txt[i + 1] == "*":
            i += 2
            while i + 1 < n and not (txt[i] == "*" and txt[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


try:
    raw = open(sys.argv[1], encoding="utf-8").read()
    try:
        cfg = json.loads(raw)          # 合法 JSON（最常见）直接解析
    except ValueError:
        cfg = json.loads(strip_jsonc(raw))
except Exception:
    print("unparseable")
    sys.exit(0)
print("off" if cfg.get("bash") is False else "on")
PYEOF
)"
    if [ "$verdict" != "off" ]; then
        _aft_warn "$cfg 中未设置 \"bash\": false（解析结果: $verdict）"
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

    # agents 解析（数字展开/human/名字校验）全部收归 python
    # _parse_agents（2026-09-09 结构收敛：bash = 调度与展示）
    # viewers 模式（无 --agents）：校验 + 快照已全部移入 python
    # _snapshot_viewers（铁律 1 职责边界；用户 2026-09-09：不合规根本
    # 不应该开始 spec-gen，校验在骨架生成之前失败）
    local snapshot_mode=0
    [ -z "$agents_list" ] && snapshot_mode=1

    local stamp
    stamp="$(date +%Y%m%d-%H%M%S)"
    local spec_dir="$PWD/mv-spec-${stamp}"
    # （目录不预创建——python 校验失败 = 零产物：预创建会让"不合规
    # 零产物"失效，实测 2026-09-09；成功时 gen_spec_skeleton 自建）

    # 骨架生成 = start_discussion --spec-gen（唯一实现）：agents/ 三态
    # （显式占位骨架 / viewers 校验+快照 / 无）全部在 python 侧，bash
    # 与 CLI 直用产出一致；viewers 校验失败时 python 退出非 0（不合规
    # 根本不应该开始 spec-gen）
    local skeleton_args=(--spec-gen "$spec_dir" --topic "$topic")
    [ -n "$background" ] && skeleton_args+=(--background "$background")
    if [ "$snapshot_mode" -eq 0 ]; then
        skeleton_args+=(--agents "$agents_list")
    fi
    "$PYTHON" "$START_DISCUSSION" "${skeleton_args[@]}" || {
        fail "spec 骨架生成失败（start_discussion --spec-gen，见上方错误）"
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
    shift                      # 余参 = 透传给 python 的选项（如 --fork-mode X）
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

    # fork 源解析已收归 python（resolve_fork_source：env + sessions 编码
    # glob，2026-09-09 结构收敛；解析失败 python 内部报错退出）
    # 第 1 步：创建分析环境（不启动；fork 源自动解析）
    # 配额（max-meeting/max-rr）是环境属性：唯一默认在 python argparse
    # （10/7），创建时固化 protocol.json——wrapper 不传（W1 双源分叉修复：
    # 此前 wrapper 5 与 python 7 不一致，两条入口 RR 上限差 40%）
    # 余参哑转发（P0，e2e10 评审：wrapper 不得静默丢弃参数——值识别/默认值
    # 的唯一家在 python；本层只转发）
    if ! "$PYTHON" "$START_DISCUSSION" --dir "$dir_path" --spec "$spec_dir" "$@"; then
        fail "环境创建失败，请查看上方输出"
    fi

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
            shift
            cmd_start "$@"
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