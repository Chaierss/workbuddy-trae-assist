"""把 chaierww 后端打包成独立可执行文件（供 Electron 桌面端内嵌）。

用法：
    python scripts/build_backend.py

产出（按当前平台）：
    Windows   desktop/backend/dist/chaierww-backend.exe
    macOS     desktop/backend/dist/chaierww-backend
    Linux     desktop/backend/dist/chaierww-backend

注意：PyInstaller **不能交叉编译**。要出 macOS 的二进制就得在 macOS 上跑，
要出 Linux 的就得在 Linux 上跑（CI 里用矩阵跑。本地在 Windows 上只能打 Windows 的）。

依赖：
    pip install pyinstaller
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

# 本脚本会 print 中文。Windows 控制台代码页不是 UTF-8 时会在第一次 print 时抛
# UnicodeEncodeError，CI 上直接红。切成 UTF-8，改不动就保持原样。
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass

ROOT = Path(__file__).resolve().parents[2]  # desktop/scripts/x.py → 项目根
DESKTOP = ROOT / "desktop"
DIST = DESKTOP / "backend" / "dist"
BUILD = DESKTOP / "backend" / "build"

# 打包完成后，前端产物由 Electron 通过 CHAIERWW_WEBUI 环境变量指向，
# 不随可执行文件内嵌；这里只打包后端逻辑。
ENTRY = ROOT / "src" / "chaierww" / "main.py"

# main.js 里按平台找这个名字，两边必须一致。
BINARY_NAME = "chaierww-backend"
BINARY_FILE = f"{BINARY_NAME}.exe" if sys.platform == "win32" else BINARY_NAME


def ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("未安装 pyinstaller，正在安装到当前环境…")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])


def build() -> None:
    ensure_pyinstaller()
    if not ENTRY.is_file():
        raise SystemExit(f"找不到后端入口：{ENTRY}")
    DIST.mkdir(parents=True, exist_ok=True)

    # 关键：隐藏导入 cryptography（DPAPI 依赖），以及 uvicorn 的动态模块。
    hidden = [
        "cryptography",
        "cryptography.hazmat.primitives.ciphers",
        "cryptography.hazmat.backends",
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        # FastAPI / pydantic 的动态依赖，onefile 下常被漏掉
        "fastapi",
        "starlette",
        "pydantic",
        "anyio",
        "httptools",
        "h11",
        "websockets",
    ]

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        BINARY_NAME,
        "--console",  # 保留控制台便于排障；发布版可改 --windowed
        # 源码在 src/ 下，需把 src 加入搜索路径，否则 `import chaierww` 失败
        "--paths",
        str(ROOT / "src"),
        "--distpath",
        str(DIST),
        "--workpath",
        str(BUILD),
        "--specpath",
        str(BUILD),
    ]
    for h in hidden:
        cmd += ["--hidden-import", h]

    cmd += [str(ENTRY)]

    print("执行：", " ".join(cmd))
    subprocess.check_call(cmd, cwd=str(ROOT))

    # 清理 PyInstaller 的中间产物，只保留 exe。
    if BUILD.is_dir():
        shutil.rmtree(BUILD, ignore_errors=True)

    exe = DIST / BINARY_FILE
    if not exe.is_file():
        raise SystemExit(f"打包失败：未生成 {exe}")
    print(f"\n✅ 打包完成：{exe}（{exe.stat().st_size / 1024 / 1024:.1f} MB）")


if __name__ == "__main__":
    build()
