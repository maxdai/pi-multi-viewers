#!/usr/bin/env bash
# archive-result.sh —— 把一场分析的 result.md 归档进 docs/reviews/
#
# 为什么是脚本：每场分析后都要做同一串机械动作，靠人/LLM 记（实测会漏——
# 最容易漏的是索引行）。这里把**机械部分**固化成命令：
#   · 命名（<日期>-<slug>.md）
#   · 正文**逐字复制**并做字节校验（存档不改原文）
#   · 存档头骨架（TODO 标记留在文件里，判断内容仍由人写）
#   · 索引表加一行（插在最后一行存档之后）
#   · 删掉仓库根的松散副本
# **判断部分不代劳**：写什么主题/抓到什么问题/落地哪个 commit，是人的判断。
#
# 用法：
#   scripts/archive-result.sh <result.md> --slug <slug> [--date YYYY-MM-DD] [--dry-run]
# 例：
#   scripts/archive-result.sh mv-mv-main-20260925-105614-result.md \
#       --slug multi-viewers-postfix-review
#
# 退出码：0 成功（或 dry-run）；1 参数/环境错；2 目标已存在（不覆盖）

set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
REVIEWS="$HERE/docs/reviews"
INDEX="$REVIEWS/README.md"

SRC=""; SLUG=""; DATE="$(date +%F)"; DRY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --slug) SLUG="${2:-}"; shift 2 ;;
        --date) DATE="${2:-}"; shift 2 ;;
        --dry-run) DRY=1; shift ;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "错误: 未知参数 $1" >&2; exit 1 ;;
        *) SRC="$1"; shift ;;
    esac
done

[ -n "$SRC" ] || { echo "错误: 缺少 result.md 路径（--help 看用法）" >&2; exit 1; }
[ -n "$SLUG" ] || { echo "错误: 缺少 --slug <slug>" >&2; exit 1; }
[ -f "$SRC" ] || { echo "错误: 文件不存在: $SRC" >&2; exit 1; }
case "$SLUG" in *[!a-zA-Z0-9._-]*) echo "错误: slug 只允许字母/数字/._-（当前: $SLUG）" >&2; exit 1 ;; esac
[ -f "$INDEX" ] || { echo "错误: 找不到索引 $INDEX" >&2; exit 1; }

DEST="$REVIEWS/$DATE-$SLUG.md"
if [ -e "$DEST" ]; then
    echo "错误: 目标已存在（不覆盖）: $DEST" >&2; exit 2
fi

if [ "$DRY" -eq 1 ]; then
    echo "[dry-run] 将归档: $SRC → $DEST"
    echo "[dry-run] 正文 $(wc -c < "$SRC") 字节，逐字复制"
    echo "[dry-run] 将写存档头骨架（含 TODO）+ 在 $INDEX 加一行 + 删 $SRC"
    exit 0
fi

# 1) 存档头骨架 + 正文（逐字）
{
    cat <<EOF
<!-- 存档：docs/reviews/$DATE-$SLUG.md
     来源：一次真实多视角分析的 result.md 原文（未删改，仅加本头与下方说明）。
     分析场次目录已随 cleanup 删除；文中消息编号不可再核验，仅作溯源线索
     （与代码注释引用约定一致：行为以自描述为准）。 -->

# 存档说明

- **主题**：TODO（一句话）
- **场次**：\`TODO（分析目录名）\`（视角 / 档位 / 收敛方式）
- **判定/发现**：TODO
- **落地**：TODO（commit 或"未落地"）
- **备注**：TODO（可不填则删掉本行）

EOF
    cat "$SRC"
} > "$DEST"

# 2) 逐字校验（正文必须与源**逐字相同**——存档不改原文）
if ! cmp -s <(tail -c "$(wc -c < "$SRC")" "$DEST") "$SRC"; then
    echo "错误: 正文校验失败（存档与源不一致）——已保留 $DEST 供排查" >&2
    exit 1
fi
echo "[archive] 正文逐字校验通过（$(wc -c < "$SRC") 字节）"

# 3) 索引加一行（紧跟最后一行存档；TODO 留给写索引的人）
python3 - "$INDEX" "$DATE-$SLUG.md" <<'PY'
import sys
idx_path, fname = sys.argv[1], sys.argv[2]
lines = open(idx_path, encoding="utf-8").read().splitlines(True)
hits = [i for i, l in enumerate(lines) if l.startswith("| `2026-")]
if not hits:
    print("错误: 索引里找不到存档行（| `2026-…）", file=sys.stderr)
    sys.exit(1)
row = f"| `{fname}` | TODO 主题 | TODO 抓到的问题 | TODO 落地 commit |\n"
lines.insert(hits[-1] + 1, row)
open(idx_path, "w", encoding="utf-8").write("".join(lines))
print(f"[archive] 索引已加一行（{idx_path}；TODO 待补）")
PY
[ $? -eq 0 ] || exit 1

# 4) 删仓库根松散副本（**只删源文件**，且必须在写成功后）
rm -f "$SRC"
echo "[archive] 已写 $DEST；源副本已删；下一步：补 $DEST 与索引行里的 TODO"
