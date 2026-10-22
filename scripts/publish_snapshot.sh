#!/usr/bin/env bash
# 把当前工作树发布成公开仓库的一个干净快照。
#
# 为什么不能直接 `git push`：
#   本地历史里躺着 6 份内部文档（商业规划 PLAN / PLAN-V2、规格 SPEC-M1a、
#   功能清单 FEATURES、闭源时期的包装说明 CLOSED-SOURCE 等）。它们在**当前工作树**
#   里已经按 .gitignore 白名单挡在外面，但历史提交里还是原文。
#   一旦仓库转 public，历史可逐条翻——等于把规划和规格一起送出去。
#
# 所以公开仓库只收「当前工作树长什么样」，不收「它是怎么长成这样的」。
# 代价是公开仓库没有细粒度历史；收益是任何一份内部文档都不会因为「当年提交过」
# 而永久留在公开历史里——删文件删不掉 git log。
#
# 为什么用 commit-tree 而不是 `checkout --orphan`：
#   orphan 要动 HEAD 和工作树，`git reset` 之后索引清空、`checkout master`
#   会因为你有一堆 untracked 文件而 abort，得 `checkout -f` 才能救回来。
#   commit-tree 只写对象，不碰工作树、不碰分支、不碰本地历史，失败也不伤任何东西。
#
# 用法：
#   scripts/publish_snapshot.sh                      # 用默认提交说明
#   scripts/publish_snapshot.sh -m "修了切号回滚"     # 自定义
#   scripts/publish_snapshot.sh --dry-run            # 只检查不推送
#   scripts/publish_snapshot.sh --rebuild            # 重建历史根（丢弃远端旧历史，强制推送）
#
# 首次发布（远端还是空仓库）和日常发布是同一套流程，脚本会自己判断有没有 parent。
# --rebuild 只在「远端历史里已经存着不该留的东西」时用：它让新快照成为新的根提交，
# 旧历史从远端分支上脱落。删文件删不掉 git log，这种情况只能重来一次。

set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

REMOTE=${PUBLISH_REMOTE:-origin}
BRANCH=${PUBLISH_BRANCH:-main}
DRY_RUN=0
REBUILD=0
MSG=""

while [ $# -gt 0 ]; do
    case "$1" in
        -m|--message) MSG=${2:-}; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --rebuild) REBUILD=1; shift ;;
        -h|--help) awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "未知参数：$1（用 -h 看用法）" >&2; exit 2 ;;
    esac
done

if [ -z "$MSG" ]; then
    MSG="发布快照 $(date +%Y-%m-%d)"
fi

# 六个核心模块。全部开源后它们必须是真实现——桩是闭源时期的产物，
# 现在如果还留着桩，说明仓库退回了「clone 下来跑不起来」的状态。
CORE_MODULES=(
    src/chaierww/adapters/base.py
    src/chaierww/adapters/trae.py
    src/chaierww/adapters/workbuddy.py
    src/chaierww/domain/switching.py
    src/chaierww/domain/trae_crypto.py
    src/chaierww/infra/secrets.py
)

die() { echo "✗ $*" >&2; exit 1; }

command -v git >/dev/null || die "找不到 git"
git remote get-url "$REMOTE" >/dev/null 2>&1 || die "没有名为 $REMOTE 的远端（先 git remote add）"

# ── 1. 用一次性索引构建快照树 ────────────────────────────────────────────
# 关键：GIT_INDEX_FILE 指到临时文件，所以 .git/index 完全不受影响。
# 放 .git/ 里而不是 /tmp：Windows 上 /tmp 展开成 C:/... 带盘符的绝对路径，
# 会被 safe-delete 一类钩子拒掉，清理静默失败、临时文件堆一堆。
SNAP_INDEX="$(git rev-parse --git-dir)/chaierww-snapshot.index"
rm -f "$SNAP_INDEX" 2>/dev/null || true
trap 'rm -f "$SNAP_INDEX" 2>/dev/null || true' EXIT

GIT_INDEX_FILE="$SNAP_INDEX" git read-tree --empty
GIT_INDEX_FILE="$SNAP_INDEX" git add -A
TREE=$(GIT_INDEX_FILE="$SNAP_INDEX" git write-tree)

FILEDB=$(git ls-tree -r --name-only "$TREE")
TOTAL=$(printf '%s\n' "$FILEDB" | grep -c . || true)

# ── 2. 闸门：宁可在这里失败，也不要把东西推出去 ──────────────────────────

# 2a. 运行时数据、依赖、缓存
BAD=$(printf '%s\n' "$FILEDB" | grep -E '(^|/)(\.venv|node_modules|\.pytest_cache|\.ruff_cache|__pycache__|logs|backups|avatars)/|\.(db|db-wal|db-shm|pyc|pyo)$|(^|/)\.env$|(^|/)accounts\.json$|_shot-[^/]*$' || true)
if [ -n "$BAD" ]; then
    echo "✗ 快照里混进了不该公开的路径：" >&2
    printf '    %s\n' "$BAD" >&2
    die "先把这些加进 .gitignore"
fi

# 2b. 内部文档：docs/ 默认不公开，白名单只有免责声明
LEAKED=$(printf '%s\n' "$FILEDB" | grep -E '^docs/.*\.md$' | grep -v '^docs/policies/DISCLAIMER\.md$' || true)
if [ -n "$LEAKED" ]; then
    echo "✗ 这些内部文档进了快照（docs/ 默认不公开）：" >&2
    printf '    %s\n' "$LEAKED" >&2
    die "确认不要公开后，从索引移除：git rm --cached <路径>"
fi

# 2c. 六个核心模块必须是真实现
#     历史上它们是转出式占位文件（2.4~2.7 KB，只 import 私有包再转出符号），
#     真实现最小 5.5 KB。用体积做下限比认字符串硬：改几行注释骗不过去。
CORE_MIN_BYTES=4096
for mod in "${CORE_MODULES[@]}"; do
    printf '%s\n' "$FILEDB" | grep -qx "$mod" || die "缺核心模块：$mod"
    size=$(git cat-file -s "$TREE:$mod")
    [ "$size" -ge "$CORE_MIN_BYTES" ] \
        || die "$mod 只有 $size 字节，像是退回了占位实现（clone 下来跑不起来）"
done

# 2d. 私钥与 token 的粗筛
if printf '%s\n' "$FILEDB" | grep -qE '\.(pem|key|p12|pfx)$'; then
    die "快照里出现密钥文件（.pem/.key/.p12/.pfx）"
fi

# 2e. 敏感内容扫描：上面几条挡的是「路径」，这条挡的是「值」。
#     路径闸门挡不住的三种情况：写死在脚本里的真实账号标识、调试时留下的
#     本机绝对路径、误提交的 token。扫的是快照树本身（git grep <tree>），
#     不碰工作树也不碰本地历史。
#     加新规则时注意别写成会误伤 package-lock.json 的宽泛模式 ——
#     JWT 用三段式、Key 用长度下限，就是为此。
#     也别把「某个具体账号 / 设备 ID / 人名」写成黑名单：这类值一旦清理掉、
#     测试里改成了哨兵值，规则就会反过来拦住正常内容（2e 曾这样自噬过）。
#     规则只拦「形态」，具体值的把关交给提交前 review 与 check_public_docs.py。
SENSITIVE=(
    'eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.'  # JWT 三段式（排除 integrity 哈希）
    'sk-[A-Za-z0-9]{24,}'                             # 网关 / 上游 API Key
    '[0-9]{11}@(163|qq|gmail|outlook|foxmail)\.'      # 手机号邮箱
    'C:\\+Users\\+[0-9]{4,}'                          # 本机路径带数字用户名（如 C:\Users\19951）
)

for pat in "${SENSITIVE[@]}"; do
    # 排除本脚本：规则表本身就写着这些敏感串，不排除会自己命中自己。
    # 只排除这一个路径，脚本其余内容照常受检。
    HITS=$(git grep -n -I -E -e "$pat" "$TREE" \
        -- ':(exclude)scripts/publish_snapshot.sh' 2>/dev/null | head -5 || true)
    if [ -n "$HITS" ]; then
        echo "✗ 快照里出现敏感内容（规则：$pat）：" >&2
        printf '    %s\n' "$HITS" >&2
        die "先清掉这些内容，别靠「删文件」——历史改不掉，只能改内容"
    fi
done

echo "✓ 敏感内容扫描通过"

echo "✓ 闸门通过：$TOTAL 个文件，6 个核心模块均为真实现"

# ── 3. 接上远端已有历史（有就挂 parent，没有就是首个提交）────────────────
git fetch -q "$REMOTE" "$BRANCH" 2>/dev/null || true

PARENT=""
if [ "$REBUILD" = "1" ]; then
    echo "  --rebuild：不挂父提交，远端历史将只剩这一个提交"
elif git rev-parse --verify -q "refs/remotes/$REMOTE/$BRANCH" >/dev/null; then
    PARENT=$(git rev-parse "refs/remotes/$REMOTE/$BRANCH")
    if [ "$(git rev-parse "$PARENT^{tree}")" = "$TREE" ]; then
        echo "✓ 内容与远端 $BRANCH 完全一致，没有需要发布的改动"
        exit 0
    fi
    echo "  父提交：$(git log -1 --format='%h %s' "$PARENT")"
else
    echo "  远端 $BRANCH 还不存在，这将是首个提交"
fi

# ── 4. 生成提交对象 ────────────────────────────────────────────────────
if [ -n "$PARENT" ]; then
    COMMIT=$(git commit-tree "$TREE" -p "$PARENT" -m "$MSG")
else
    COMMIT=$(git commit-tree "$TREE" -m "$MSG")
fi

echo "  快照提交：$(git log -1 --format='%h %s' "$COMMIT")"

if [ "$DRY_RUN" = "1" ]; then
    echo "（--dry-run）未推送。确认无误后去掉 --dry-run 重跑。"
    exit 0
fi

# ── 5. 推送并回读验证 ──────────────────────────────────────────────────
# --rebuild 生成的是新的根提交，与远端现有历史无祖先关系，普通推送必被拒。
if [ "$REBUILD" = "1" ]; then
    echo "  ⚠ --rebuild：强制推送，远端旧历史将被丢弃（本地不受影响）"
    git push --force "$REMOTE" "$COMMIT:refs/heads/$BRANCH"
else
    git push "$REMOTE" "$COMMIT:refs/heads/$BRANCH"
fi

git fetch -q "$REMOTE" "$BRANCH"
ACTUAL=$(git rev-parse "refs/remotes/$REMOTE/$BRANCH")
[ "$ACTUAL" = "$COMMIT" ] || die "推送后远端指向 $ACTUAL，与预期 $COMMIT 不符"

echo "✓ 已发布：$REMOTE/$BRANCH = ${COMMIT:0:7}（$TOTAL 个文件）"
echo "  本地分支与工作树均未改动。"
