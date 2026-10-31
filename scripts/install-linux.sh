#!/usr/bin/env bash
# chaierww 源码安装（Linux）。
#
# 只做三件事：确认环境够用 → 建 .venv 并 pip install -e . → 确认前端产物存在。
# 不 sudo、不改系统包、不下载安装包；重复执行等于重装，可以安全地跑第二遍。
#
# 用法：
#   bash scripts/install-linux.sh [--python /path/to/python3.11] [--skip-web] [--link] [--systemd]
#   bash scripts/install-linux.sh --uninstall-systemd
#
#   --python             指定解释器（系统默认版本 < 3.11 时用）
#   --skip-web           明确表示前端产物自己处理；此时缺 web/dist 会直接报错，不再尝试构建
#   --link               在 ~/.local/bin/chaierww 建软链
#   --systemd            安装后写成 systemd --user 服务并 enable --now（退出 SSH 也不停）
#   --uninstall-systemd  只删这个 user 服务（disable + 删 unit），不动仓库与数据
#
# 为什么 CentOS 7 要特殊对待：glibc 2.17，桌面包起不来（Electron / Python 二进制
# 链接的是 2.31+），只能走这条路；而它的系统 Python 只有 3.6/3.8，需要另装 3.11。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON_ARG=""
SKIP_WEB=0
LINK=0
SYSTEMD=0
UNINSTALL_SYSTEMD=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --python) PYTHON_ARG="${2:-}"; [ -n "$PYTHON_ARG" ] || { echo "✗ --python 需要跟一个路径" >&2; exit 2; }; shift 2 ;;
    --skip-web) SKIP_WEB=1; shift ;;
    --link) LINK=1; shift ;;
    --systemd) SYSTEMD=1; shift ;;
    --uninstall-systemd) UNINSTALL_SYSTEMD=1; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "✗ 不认识的参数：$1（可用 --python / --skip-web / --link / --systemd / --uninstall-systemd）" >&2; exit 2 ;;
  esac
done

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/chaierww.service"
CHA_HOME="${CHAIERWW_HOME:-$HOME/.chaierww}"

say() { printf '%s\n' "$*"; }
die() { printf '✗ %s\n' "$*" >&2; exit 1; }

# 版本比较：version_at_least 3.11.8 3.11
version_at_least() {
  [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]
}

# ---------------------------------------------------------------------------
say "chaierww 源码安装"
say "仓库：$ROOT"

if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  say "系统：${PRETTY_NAME:-未知}（内核 $(uname -r)，$(uname -m)）"
  if [ "${ID:-}" = "centos" ] && [ "${VERSION_ID:-}" = "7" ]; then
    say ""
    say "注意：CentOS 7（glibc 2.17）跑不了桌面包，只能用本脚本（源码跑后端）。"
    say "      Python 需要 3.11+，系统自带不够：见 docs/LINUX.md 第 3 节。"
    say "      前端不要在 CentOS 7 上构建（Node 20 要求 glibc ≥ 2.28）。"
    say ""
  fi
fi

# ---------------------------------------------------------------------------
# 1. 解释器
if [ -n "$PYTHON_ARG" ]; then
  CANDIDATES=("$PYTHON_ARG")
else
  CANDIDATES=(python3.13 python3.12 python3.11 python3)
fi

PY=""
for candidate in "${CANDIDATES[@]}"; do
  if command -v "$candidate" >/dev/null 2>&1; then
    version="$("$candidate" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || true)"
    if [ -n "$version" ] && version_at_least "$version" "3.11"; then
      PY="$candidate"
      break
    fi
    say "  跳过 $candidate（版本 $version < 3.11）"
  fi
done

if [ -z "$PY" ]; then
  say ""
  die "找不到 Python ≥ 3.11。
  CentOS 7：用 python-build-standalone 的 3.11 预编译包解压到 ~/.local/python311，
            然后重跑：bash scripts/install-linux.sh --python ~/.local/python311/bin/python3
  其它系统：用包管理器装 python3.11（Ubuntu 20.04 需 ppa:deadsnakes/ppa）
  详见 docs/LINUX.md 第 2 节。"
fi

say "Python：$PY（$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')）"

# ---------------------------------------------------------------------------
# 2. 前端产物（web/dist）
if [ -f "web/dist/index.html" ]; then
  say "前端产物：web/dist 已就绪"
elif [ "$SKIP_WEB" -eq 1 ]; then
  die "指定了 --skip-web，但 web/dist/index.html 不存在。
  后端会起来，但页面是空的（只有 API）。把产物放进来，或去掉 --skip-web 让脚本构建。"
else
  NODE_OK=0
  if command -v node >/dev/null 2>&1; then
    node_version="$(node -v | sed 's/^v//')"
    node_major="${node_version%%.*}"
    node_minor="$(printf '%s' "$node_version" | cut -d. -f2)"
    if [ "${node_major:-0}" -ge 22 ] || { [ "${node_major:-0}" -eq 20 ] && [ "${node_minor:-0}" -ge 19 ]; }; then
      NODE_OK=1
    fi
    say "Node：v$node_version"
  fi

  if [ "$NODE_OK" -eq 1 ] && command -v npm >/dev/null 2>&1; then
    say "前端产物：缺失，开始构建（npm ci && npm run build）"
    ( cd web && npm ci && npm run build )
    [ -f "web/dist/index.html" ] || die "构建结束但 web/dist/index.html 仍不存在，请检查上面的 npm 输出。"
  else
    say ""
    die "缺前端产物，且本机没有可用的 Node（需要 ^20.19 或 ≥22.12）。
  老系统（CentOS 7 等）不要在这台机器上装 Node，改为在别的机器构建后拷贝：
    # 在有 Node 的机器上
    cd web && npm ci && npm run build
    # 拷到这台机器（web/dist 只有几 MB）
    rsync -a web/dist user@这台机器:$ROOT/web/
  或者用 CHAIERWW_WEBUI=/绝对路径/dist 直接指过去。详见 docs/LINUX.md 的 B3 节。"
  fi
fi

# ---------------------------------------------------------------------------
# 3. 虚拟环境 + 安装
if [ -x ".venv/bin/python" ]; then
  venv_version="$(.venv/bin/python -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || true)"
  if [ -z "$venv_version" ] || ! version_at_least "$venv_version" "3.11"; then
    say "已有 .venv 的 Python（$venv_version）不满足 ≥3.11，重建"
    rm -rf .venv
  else
    say "虚拟环境：复用 .venv（Python $venv_version）"
  fi
fi
[ -x ".venv/bin/python" ] || { say "虚拟环境：创建 .venv"; "$PY" -m venv .venv; }

say "安装依赖：pip install -e ."
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install -e .

# ---------------------------------------------------------------------------
# 4. 可选软链
if [ "$LINK" -eq 1 ]; then
  mkdir -p "$HOME/.local/bin"
  ln -sf "$ROOT/.venv/bin/chaierww" "$HOME/.local/bin/chaierww"
  say "软链：$HOME/.local/bin/chaierww"
fi

# ---------------------------------------------------------------------------
# 5. 可选 systemd --user 常驻
#
# 判定标准只认「user bus 真的能用」：`systemctl --user show-environment` 是条纯读命令，
# 能用就说明 daemon + DBUS 会话都在。精简镜像 / CentOS 7 的 SSH 会话常常没有，
# 这时写 unit 只会让 `enable --now` 失败并把脚本带挂，所以直接给 nohup 的替代方案。
systemd_user_ready() {
  command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1
}

if [ "$UNINSTALL_SYSTEMD" -eq 1 ]; then
  if systemd_user_ready; then
    systemctl --user disable --now chaierww.service 2>/dev/null || true
  fi
  if [ -f "$UNIT" ]; then
    rm -f "$UNIT"
    say "已删除服务文件：$UNIT"
  else
    say "没有找到服务文件（$UNIT），无需删除"
  fi
  if systemd_user_ready; then
    systemctl --user daemon-reload 2>/dev/null || true
  fi
  say "仓库与数据目录未改动（${CHA_HOME}）"
  exit 0
fi

if [ "$SYSTEMD" -eq 1 ]; then
  say ""
  if ! systemd_user_ready; then
    say "✗ 当前环境没有可用的 systemd user 会话（systemctl --user 不可用）。"
    say "  常见原因：精简镜像没有 systemd，或 SSH 会话没有 DBUS 会话。"
    say ""
    say "改为前台后台跑（不会开机自启）："
    say "  mkdir -p \"${CHA_HOME}/logs\""
    say "  nohup \"$ROOT/.venv/bin/chaierww\" >> \"${CHA_HOME}/logs/console.log\" 2>&1 &"
    say "  或者用 screen / tmux 挂着；日志另有 ${CHA_HOME}/logs/ 下的按天文件。"
  else
    mkdir -p "$UNIT_DIR"
    cat > "$UNIT" <<EOF
[Unit]
Description=chaierww 本地多账号切换工具
After=network.target

[Service]
Type=simple
WorkingDirectory=$ROOT
ExecStart=$ROOT/.venv/bin/chaierww
Environment=CHAIERWW_HOME=$CHA_HOME
Environment=CHAIERWW_PORT=${CHAIERWW_PORT:-57910}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
    say "服务文件：$UNIT"
    systemctl --user daemon-reload
    systemctl --user enable --now chaierww.service
    say "已 enable --now chaierww.service"
    if command -v loginctl >/dev/null 2>&1; then
      say ""
      say "提示：要让服务在退出 SSH 后继续运行，执行一次（每台机器只需一次）："
      say "  loginctl enable-linger $USER"
    fi
    say ""
    say "查看状态 / 日志："
    say "  systemctl --user status chaierww"
    say "  journalctl --user -u chaierww -f"
    say "改端口：编辑 unit 里的 CHAIERWW_PORT 后 systemctl --user restart chaierww"
  fi
fi

# ---------------------------------------------------------------------------
say ""
say "安装完成。启动："
if [ "$LINK" -eq 1 ]; then
  say "  chaierww            （需要 ~/.local/bin 在 PATH 里）"
else
  say "  .venv/bin/chaierww"
fi
say "然后浏览器打开 http://127.0.0.1:${CHAIERWW_PORT:-57910}/"
say ""
say "远程机器请用 SSH 隧道，不要改监听地址（接口守卫只认回环）："
say "  ssh -L ${CHAIERWW_PORT:-57910}:127.0.0.1:${CHAIERWW_PORT:-57910} user@这台机器"
say ""
say "数据目录：$CHA_HOME"
say "完整说明：docs/LINUX.md"
