#!/usr/bin/env bash
#
# pi-multi-viewers wrapper——多视角协同分析（fork 主 session + meeting 协议）
#
# 用法：
#   ./scripts/mv.sh --prepare "<主题>" [--agents "a,b,c"|4]
#   ./scripts/mv.sh --start <spec目录>   # 可选 --fork-mode compaction|budget|full
#   ./scripts/mv.sh --status <dir>
#   ./scripts/mv.sh --report <dir>
#   ./scripts/mv.sh --wait <dir>
#   ./scripts/mv.sh --cleanup <dir>
#   ./scripts/mv.sh --view <dir> [--since <ref>]     # human-viewer 封装
#   ./scripts/mv.sh --say <dir> "<文本>"              # human-sayer 封装
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
PYTHON="${PYTHON:-python3}"
START_DISCUSSION="$ROOT_DIR/start_discussion.py"
OBSERVABILITY="$ROOT_DIR/observability.py"
HUMAN_VIEWER="$ROOT_DIR/human_viewer.py"
HUMAN_SAYER="$ROOT_DIR/human_sayer.py"


usage() {
    cat <<'USAGE_EOF'
用法:
  $0 --prepare "<问题>" [--background "<背景>"] [--agents "a,b,c"|4]
  $0 --start <spec目录> [--fork-mode compaction|budget|full]
  $0 --status  [dir]
  $0 --report  [dir]
  $0 --wait    [dir]
  $0 --cleanup [dir]
  $0 --view    [dir] [--since <ref>]
  $0 --say     [dir] "<文本>"

消费命令的 <dir> 可省略（自动发现本 session 当前分析——按 cwd 下
mv-<PI_SESSION_ID>-* 最新；找不到则取最新 mv-* 并警告）

默认参数:
  agents=a,b,c  max-meeting=10  max-rr=7   # 配额默认值的权威在 python argparse（wrapper 不传）

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



require_dir() {
    local dir="$1"
    [ -n "$dir" ] || fail "缺少目录参数"
    [ -d "$dir" ] || fail "目录不存在: $dir"
}

# 目录解析：显式参数优先；**省略则自动发现**（python observability
# --find-dir——单一实现，extension 同一入口）。为什么允许省略：唯一知道
# 路径的是 --start 的输出，此前消费命令都要求把它传给每个命令，等于让
# 调用方（主 pi）把长绝对路径记在 LLM 上下文里再复用——改错/截断/相对
# 路径都出过（参数形态标准化就是为此）。发现逻辑下沉后路径不经 LLM 记忆。
# **调用方必须 `|| exit $?`**：本函数的 exit 发生在命令替换的子 shell 里，
# 不检查返回值会让空结果继续往下走（表现为双重错误消息：python 的原因 +
# require_dir 的"缺少目录参数"——实测踩到）。
resolve_dir() {
    local dir="${1:-}"
    if [ -z "$dir" ]; then
        # 省略 = 自动发现；**失败原因由 python 给出**（错误文案留一处——
        # e2e16 F7：两条文案没有信息增益，只用工具层那条更贴近原因）。
        dir="$("$PYTHON" "$OBSERVABILITY" --find-dir)" || exit 1
    fi
    normalize_dir "$dir"
}

# 目录参数规范化：裸名（无路径符）会被 start_discussion 加 discussion-
# 前缀导致找错目录（实测 2026-09-03）——所有消费命令入口统一转绝对路径
normalize_dir() {
    readlink -f "$1"
}

cmd_status() {
    local dir
    dir="$(resolve_dir "${1:-}")" || exit $?
    require_dir "$dir"
    "$PYTHON" "$START_DISCUSSION" --dir "$dir" --status
}

cmd_report() {
    local dir
    dir="$(resolve_dir "${1:-}")" || exit $?
    require_dir "$dir"
    "$PYTHON" "$START_DISCUSSION" --dir "$dir" --report
}

cmd_wait() {
    local dir
    dir="$(resolve_dir "${1:-}")" || exit $?
    require_dir "$dir"
    "$PYTHON" "$START_DISCUSSION" --dir "$dir" --wait
}

cmd_cleanup() {
    local dir
    dir="$(resolve_dir "${1:-}")" || exit $?
    require_dir "$dir"
    "$PYTHON" "$START_DISCUSSION" --dir "$dir" --cleanup
}

cmd_view() {
    local dir since=""
    # 首参：非空且非 --since → 显式目录（**F5 回归修复**：此前无条件 shift
    # 后总走自动发现，显式目录被静默丢弃——多分析并存/跨目录调用会看错对象）。
    # 判据与 status/report/wait/cleanup 的 `resolve_dir "${1:-}"` 一致。
    # 注意与 --say 的区别：--say 按**参数个数**区分（文本可含空格/以 -- 开头，
    # 不能按值判形）；--view 的 --since 是本命令自己的选项名，可以判形。
    if [ -n "${1:-}" ] && [ "${1:-}" != "--since" ]; then
        dir="$(resolve_dir "$1")" || exit $?
        shift
    else
        dir="$(resolve_dir "")" || exit $?
    fi
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
    # 两种形态（**按参数个数区分，不是按值猜语义**）：
    #   --say <dir> "<文本>"   显式目录（兼容原有调用）
    #   --say "<文本>"         目录自动发现（本 session 当前分析）
    local dir text
    if [ "$#" -ge 2 ]; then
        dir="$(resolve_dir "$1")" || exit $?
        text="$2"
    else
        dir="$(resolve_dir "")" || exit $?
        text="${1:-}"
    fi
    require_dir "$dir"
    [ -d "$dir/work-human" ] || fail "分析缺少 work-human: $dir"
    [ -n "$text" ] || fail "插话文本不能为空"
    "$PYTHON" "$HUMAN_SAYER" "$dir" "$text"
}

# 定位主 pi 的 session 文件：优先 PI_SESSION_FILE，否则用当前 cwd 编码路径查找
# 读取主 pi 的 model/thinking（用户 2026-08-31：aft 不再替换 bash 后
# 环境变量可用且是当前生效值——优先环境变量，session 文件解析仅为兜底）
cmd_prepare() {
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
    local spec_dir="$1"
    shift                      # 余参 = 透传给 python 的选项（如 --fork-mode X）
    require_dir "$spec_dir"
    [ -f "$spec_dir/question.md" ] || fail "spec 缺少 question.md: $spec_dir"

    local session_id="${PI_SESSION_ID:-}"
    local stamp
    stamp="$(date +%Y%m%d-%H%M%S)"
    # 目录前缀 mv-：与 pi-agents-helper 的 discuss-<sid>-* **命名空间隔离**
    # ——两个系统的 extension 都按"同 sid 最新目录"发现目标，共用前缀会
    # 在同 session 并发两套时互相插错（且 human 消息格式兼容 → 静默写错）。
    # 前缀也是产品语言的统一（本项目用"分析"，不再沿用"discuss"）。
    local dir_name="mv"
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

    # spec 已被消费：只删**本工具生成的形态**（mv-spec-*）——用户自建的
    # spec 目录不动（可能是有价值的视角快照）。删除前提示一行（此前静默
    # 删除：spec 是视角任务书/背景/models 在用户侧的唯一副本，删了不可恢复）
    case "$(basename "$spec_dir")" in
        mv-spec-*)
            echo "[start] spec 已消费，删除（本工具生成形态）: $spec_dir"
            rm -rf "$spec_dir"
            ;;
        *)
            echo "[start] spec 已消费（保留未删——非 mv-spec-* 形态）: $spec_dir"
            ;;
    esac

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
            shift
            cmd_status "${1:-}"
            exit $?
            ;;
        --report)
            shift
            cmd_report "${1:-}"
            exit $?
            ;;
        --wait)
            shift
            cmd_wait "${1:-}"
            exit $?
            ;;
        --cleanup)
            shift
            cmd_cleanup "${1:-}"
            exit $?
            ;;
        --view)
            shift
            cmd_view "$@"
            exit $?
            ;;
        --say)
            shift
            cmd_say "$@"
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