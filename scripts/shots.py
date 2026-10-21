# -*- coding: utf-8 -*-
"""生成 README / 官网用的界面截图，并在截图前完成脱敏。

为什么在页面里脱敏，而不是事后按坐标打码：
原先 `mask_screenshots.py` 用写死的像素矩形（基于某一版布局）去打码，界面一改
坐标就全废，只能重新量一遍。这里改成「先改 DOM 再截图」——
账号名、账号 ID、本机路径、邮箱、首字母头像都在浏览器里被替换成圆点或模糊，
布局不动、也不存在坐标漂移问题。

用法：

    # 1. 起后端（另开一个终端）
    python -m chaierww.main

    # 2. 截图
    python scripts/shots.py                    # 浅色，输出到 docs/images/
    python scripts/shots.py --theme dark       # 深色，输出到 docs/images-dark/
    python scripts/shots.py --only overview    # 只截某几张

依赖：playwright（`pip install playwright && playwright install chromium`）。
原始截图不含敏感信息，产物可直接进公开仓库。
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


def find_chromium() -> str | None:
    """复用本机已装的 Chromium，省掉 `playwright install` 的一次下载。

    优先级：CHAIERWW_CHROME 环境变量 → ms-playwright 缓存 → 系统 Chrome/Edge。
    """
    env = os.environ.get("CHAIERWW_CHROME")
    if env and Path(env).is_file():
        return env

    cache = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    hits = sorted(glob.glob(str(cache / "chromium-*" / "chrome-win64" / "chrome.exe")))
    if hits:
        return hits[-1]

    for candidate in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ):
        if Path(candidate).is_file():
            return candidate
    return None


# 视口统一 1440 宽，截完再缩到 OUTPUT_WIDTH。
VIEWPORT_W = 1440

# 每张图：路由 + 视口高度 + 额外等待（毫秒）。
# 高度按各页首屏信息量给，别让下面留一大块空白。
SHOTS: list[dict[str, object]] = [
    {"name": "overview", "path": "/overview", "height": 1120},
    {"name": "accounts", "path": "/accounts", "height": 980},
    {"name": "activity", "path": "/activity", "height": 780},
    {"name": "usage", "path": "/usage", "height": 960},
    {"name": "gateway", "path": "/gateway", "height": 860},
    {"name": "settings-backup", "path": "/settings?tab=backup", "height": 940},
    {"name": "settings-env", "path": "/settings?tab=env", "height": 640},
]

# 脱敏脚本：在页面里跑，把敏感文本换成圆点、把首字母头像糊掉。
REDACT_JS = r"""
(payload) => {
  const explicit = (payload.explicit || []).filter(Boolean)
    .sort((a, b) => b.length - a.length);

  const dots = (s) => "\u2022".repeat(Math.min(Math.max(s.length, 3), 24));

  // 首字母头像：圆形小色块里只有一个字，模糊掉
  const blurStyle = document.createElement("style");
  blurStyle.textContent = ".__shot-blur{filter:blur(7px)!important;}";
  document.head.appendChild(blurStyle);

  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const parent = node.parentElement;
      if (!parent) return NodeFilter.FILTER_REJECT;
      const tag = parent.tagName;
      if (tag === "SCRIPT" || tag === "STYLE" || tag === "TEXTAREA") {
        return NodeFilter.FILTER_REJECT;
      }
      return node.nodeValue.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
    },
  });

  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);

  let replaced = 0;
  for (const node of nodes) {
    const before = node.nodeValue;
    let text = before;

    for (const secret of explicit) {
      if (secret && text.includes(secret)) text = text.split(secret).join(dots(secret));
    }
    // 路径里的用户名：C:\Users\xxxx → C:\Users\•••••
    text = text.replace(/([A-Za-z]:\\Users\\)([^\\\s]+)/g, (m, head) => head + "\u2022\u2022\u2022\u2022\u2022");
    // 邮箱
    text = text.replace(/[\w.+-]+@[\w-]+\.[\w.-]+/g, (m) => dots(m));
    // 6 位以上连续数字：账号 ID / 手机号
    text = text.replace(/\b\d{6,}\b/g, (m) => dots(m));

    if (text !== before) {
      node.nodeValue = text;
      replaced += 1;
    }
  }

  // 圆形首字母头像（一个字的方块，宽高接近、圆角够大）
  let blurred = 0;
  for (const el of document.querySelectorAll("body *")) {
    if (el.children.length > 0) continue;
    const label = (el.textContent || "").trim();
    if (!label || label.length > 2) continue;
    const box = el.getBoundingClientRect();
    if (box.width < 22 || box.width > 60) continue;
    if (Math.abs(box.width - box.height) > 4) continue;
    const radius = parseFloat(getComputedStyle(el).borderRadius) || 0;
    if (radius < 8) continue;
    el.classList.add("__shot-blur");
    blurred += 1;
  }

  // 底部渐隐：页面比视口长时，硬切会在卡片中间截断，加一层背景渐变收尾
  const rgba = (value, alpha) => {
    const probe = document.createElement("canvas").getContext("2d");
    probe.fillStyle = value;
    const hex = probe.fillStyle;
    if (hex.startsWith("#")) {
      const n = parseInt(hex.slice(1), 16);
      return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
    }
    return hex.replace(/rgba?\(([^)]+)\)/, (m, inner) => {
      const parts = inner.split(",").slice(0, 3).map((s) => s.trim());
      return `rgba(${parts.join(",")},${alpha})`;
    });
  };
  const bg = getComputedStyle(document.body).backgroundColor;
  const sidebar = document.querySelector("aside");
  const left = sidebar ? Math.round(sidebar.getBoundingClientRect().right) : 0;
  const fade = document.createElement("div");
  fade.style.cssText = [
    "position:fixed", "right:0", "bottom:0", "height:72px", "z-index:2147483000",
    "pointer-events:none", `left:${left}px`,
    `background:linear-gradient(to bottom, ${rgba(bg, 0)} 0%, ${rgba(bg, 0.92)} 62%, ${bg} 100%)`,
  ].join(";");
  document.body.appendChild(fade);

  return { replaced, blurred };
}
"""


def collect_secrets(page) -> list[str]:
    """从接口里取真实账号字段，作为脱敏词表。

    直接读数据库或配置文件都不合适（截图脚本不该碰数据目录），
    走已经渲染出这些数据的同一个接口最省事。
    注意 `/api/accounts` 必须带 scope，所以先取 `/api/scopes`。
    """
    try:
        data = page.evaluate(
            """async () => {
                const getJson = async (url) => {
                    const res = await fetch(url, { credentials: 'include' });
                    return res.ok ? res.json() : null;
                };
                const scopeBody = await getJson('/api/scopes');
                const scopes = (scopeBody && scopeBody.scopes) || [];
                const out = [];
                for (const scope of scopes) {
                    if (!scope.implemented) continue;
                    const body = await getJson(`/api/accounts?scope=${encodeURIComponent(scope.key)}`);
                    const list = (body && (body.accounts || body.items)) || [];
                    for (const acc of list) {
                        for (const key of ['displayName', 'externalId', 'note']) {
                            const value = acc && acc[key];
                            if (typeof value === 'string' && value.trim().length >= 2) {
                                out.push(value.trim());
                            }
                        }
                    }
                }
                return out;
            }"""
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  ! 取账号词表失败（继续用正则兜底）：{exc}")
        return []
    return data or []


def main() -> int:
    parser = argparse.ArgumentParser(description="chaierww 界面截图（含脱敏）")
    parser.add_argument("--base", default="http://127.0.0.1:57910", help="后端地址")
    parser.add_argument("--theme", default="light", choices=["light", "dark"], help="界面主题")
    parser.add_argument("--out", default=str(DOCS / "images"), help="输出目录")
    parser.add_argument("--only", default="", help="只截这些（逗号分隔的 name）")
    parser.add_argument("--width", type=int, default=1600, help="最终图片宽度")
    parser.add_argument("--chrome", default="", help="指定浏览器可执行文件（默认自动探测）")
    args = parser.parse_args()

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    targets = [s for s in SHOTS if not only or s["name"] in only]
    if not targets:
        print(f"没有匹配的截图项：{args.only}")
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    executable = args.chrome or find_chromium()
    if executable:
        print(f"浏览器：{executable}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=executable)
        try:
            for shot in targets:
                name = str(shot["name"])
                width, height = VIEWPORT_W, int(shot["height"])  # type: ignore[arg-type]
                page = browser.new_page(
                    viewport={"width": width, "height": height},
                    device_scale_factor=2,
                    locale="zh-CN",
                    color_scheme="dark" if args.theme == "dark" else "light",
                )
                # 主题 / 语言都存 localStorage，必须在首屏脚本前落一次
                page.add_init_script(
                    "localStorage.setItem('chaierww.theme', '%s');"
                    "localStorage.setItem('chaierww.locale', 'zh');" % args.theme
                )

                url = f"{args.base.rstrip('/')}{shot['path']}"
                page.goto(url, wait_until="networkidle", timeout=45000)
                page.wait_for_timeout(1400)  # 等图表动画 / 骨架屏结束

                secrets = collect_secrets(page)
                stats = page.evaluate(REDACT_JS, {"explicit": secrets})
                page.wait_for_timeout(120)

                raw = out_dir / f".raw-{name}.png"
                page.screenshot(path=str(raw))

                # 2x 截图缩到目标宽度，边缘更干净、文件也小
                with Image.open(raw) as img:
                    target_h = round(img.height * args.width / img.width)
                    img.resize((args.width, target_h), Image.LANCZOS).save(
                        out_dir / f"{name}.png", "PNG", optimize=True
                    )
                raw.unlink(missing_ok=True)

                size_kb = (out_dir / f"{name}.png").stat().st_size // 1024
                print(
                    f"  {name:16s} {args.width}x{target_h}  "
                    f"{size_kb:4d} KB  脱敏 {stats['replaced']} 处 / 头像 {stats['blurred']} 个"
                )
                page.close()
        finally:
            browser.close()

    print(f"完成：{len(targets)} 张 -> {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
