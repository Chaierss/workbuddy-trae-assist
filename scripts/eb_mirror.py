"""electron-builder 二进制本地镜像代理。

用途：winCodeSign-2.6.0.7z 官方包里含 macOS 符号链接，Windows 非管理员账户
解压必失败（Cannot create symbolic link : 客户端没有所需的特权），导致
electron-builder 打 NSIS 安装包时卡在 rcedit 环节。

本脚本把「已去符号链接的本地归档」按 electron-builder 期望的路径结构提供出去，
其余未命中的请求透传到 npmmirror 上游镜像。

用法：
    python scripts/eb_mirror.py 8765
    另开终端：set ELECTRON_BUILDER_BINARIES_MIRROR=http://127.0.0.1:8765/
             npx electron-builder --win nsis
"""

from __future__ import annotations

import os
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCAL_ROOT = os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "Temp", "ebmirror")
UPSTREAM = "https://cdn.npmmirror.com/binaries/electron-builder-binaries"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _local_path(self, rel: str) -> str:
        return os.path.join(LOCAL_ROOT, *rel.split("/"))

    def _serve_file(self, path: str, head_only: bool = False) -> None:
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        if head_only:
            return
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1 << 16)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _handle(self, head_only: bool) -> None:
        rel = self.path.split("?")[0].lstrip("/")
        local = self._local_path(rel)
        if os.path.isfile(local):
            sys.stderr.write(f"[hit ] {rel}\n")
            self._serve_file(local, head_only)
            return

        url = f"{UPSTREAM}/{rel}"
        sys.stderr.write(f"[pass] {rel}\n")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "eb-mirror"})
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = resp.read()
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[fail] {rel}: {exc}\n")
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        self._handle(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle(head_only=True)

    def log_message(self, *args) -> None:  # 静音默认访问日志
        return


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    sys.stderr.write(f"mirror root={LOCAL_ROOT}\nlistening on 127.0.0.1:{port}\n")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
