# -*- coding: utf-8 -*-
"""绘制 c/w 字母标，并生成 Web 与桌面端所需的全部图标。

源：本脚本里的 `draw_mark()`——一条开口的 c 加一条折线 w，墨绿 #1F3A33。
所有产物都由它按目标尺寸重新绘制 / 缩放，不存在「先存一张大图再到处缩」的糊边。

产物：

| 文件 | 用途 |
| --- | --- |
| `web/public/android-chrome-512x512.png` | Web 端应用图标（纸色圆角块 + 字标） |
| `web/public/android-chrome-192x192.png` / `apple-touch-icon.png` | 侧栏品牌块、iOS 主屏 |
| `web/public/android-chrome-192x192-dark.png` | 应用内深色主题的侧栏品牌块（墨绿底反白） |
| `web/public/favicon-16x16.png` / `favicon-32x32.png` / `favicon.ico` | 浏览器标签页 |
| `build/icon.ico` | Windows 应用图标（16–256 多尺寸） |
| `build/icon.icns` | macOS 应用图标 |
| `build/icon.png` | 1024×1024，Linux 与兜底 |
| `build/icons/*.png` | Linux 桌面图标集（16–512） |
| `assets/tray-light.ico` / `tray-dark.ico` | Windows 托盘（16/20/24/32/48，浅/深任务栏各一套） |
| `assets/tray-light.png` / `tray-dark.png` | Linux 托盘（32） |
| `assets/trayTemplate.png` / `@2x` | macOS 菜单栏模板图（自动适配深浅色） |

**为什么托盘图在 `assets/` 而不是 `build/`**：`build/` 是 electron-builder 的
buildResources 目录，**打包时会被排除在 app.asar 之外**。托盘图是运行时读的，
放进去就会「开发时看得见、装完就没了」（踩过这个坑：装完的版本托盘是一块空白）。
应用图标只在打包时被工具读取，所以留在 `build/` 里没问题。

托盘图的两个决定：

1. **就是字标本身，不另画简化标记**。托盘图直接由同一份 c/w 字形缩放而来，
   跟应用图标、任务栏图标、网页 favicon 是同一个图形。小尺寸下它必然糊，
   但「糊掉的 logo」和「另一个形状」是两回事——用户认的是形状要一致。
2. **按系统深浅色出两套**。字标本身是纯色 + 透明底：浅色任务栏用黑字形，
   深色任务栏必须用白字形，否则就是一块看不见的黑影。Windows / Linux 没有
   「模板图自动反色」这回事，只能由主进程按 `nativeTheme.shouldUseDarkColors`
   挑一个，并在系统主题变化时换图（见 main.js 的 trayIcon）。

   | 文件 | 用在 |
   | --- | --- |
   | `assets/tray-light.ico` | 浅色任务栏（黑字形） |
   | `assets/tray-dark.ico` | 深色任务栏（白字形） |
   | `assets/tray-light.png` / `tray-dark.png` | Linux 托盘（32px） |
   | `assets/trayTemplate.png` / `@2x` | macOS 菜单栏（黑字形 + alpha，系统自动反色） |

macOS 例外：菜单栏图标走模板图，系统按菜单栏明暗自动反色，不需要两套。

应用图标采用「纸色圆角块 + 墨绿字标」：任务栏、深色桌面与浅色桌面上都能看清；
托盘与应用内小图标仍用纯字形，跟随主题染色。

用法：`python desktop/scripts/build_icons.py`
"""

from __future__ import annotations

import math
import struct
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _use_utf8_console() -> None:
    """把 stdout/stderr 切成 UTF-8。

    为什么需要：Windows 上 Python 的 stdout 默认跟随控制台代码页（cp936 / cp1252），
    这个脚本会 print 中文，在 GitHub Actions 的 Windows runner 上直接抛
    `UnicodeEncodeError: 'charmap' codec can't encode characters`，构建第一步就红。
    在本地 Windows 终端里通常不炸（代码页是 936），所以这个问题只在 CI 暴露。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):  # 已被重定向到不支持改编码的对象
            pass


_use_utf8_console()

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web" / "public"
OUT = ROOT / "desktop" / "build"
# 托盘图必须放在 buildResources 之外，否则会被排除在 app.asar 外（详见文件头说明）。
TRAY_OUT = ROOT / "desktop" / "assets"

# 字标画布与超采样倍数：先用 4 倍尺寸画，再缩到目标尺寸，边缘才干净。
MARK_CANVAS = 1024
SUPERSAMPLE = 4

# 裁掉留白后的源字形，main() 里赋值；托盘渲染靠它按各尺寸缩放。
_GLYPH: Image.Image | None = None

WIN_ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
WIN_TRAY_SIZES = [16, 20, 24, 32, 40, 48]
LINUX_SIZES = [16, 24, 32, 48, 64, 128, 256, 512]
MAC_ICNS_SIZES = [16, 32, 64, 128, 256, 512, 1024]


def write_ico(path: Path, images: list[Image.Image]) -> None:
    """手写 ICO 容器，每帧都是独立渲染的 PNG。

    为什么不用 `Image.save(..., sizes=[...])`：Pillow 的 ICO 写入器是拿**同一张图**
    缩放成多个尺寸，小图放进大会糊，大图缩到 16 也会丢细节。这里每帧都由
    `compose_*` 按目标尺寸单独渲染，再按 ICO 结构拼起来，每个尺寸都是清晰的。
    """
    blobs: list[bytes] = []
    for img in images:
        buf = BytesIO()
        img.save(buf, "PNG", optimize=True)
        blobs.append(buf.getvalue())

    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    entries = bytearray()
    for img, blob in zip(images, blobs):
        w, h = img.size
        entries += struct.pack(
            "<BBBBHHII",
            0 if w >= 256 else w,
            0 if h >= 256 else h,
            0, 0, 1, 32, len(blob), offset,
        )
        offset += len(blob)

    path.write_bytes(header + bytes(entries) + b"".join(blobs))



def _draw_mark(size: int = MARK_CANVAS) -> Image.Image:
    """画 c/w 字标：左侧开口圆环的 c，右侧三条折线的 w，圆头圆角。

    为什么用几何描边而不是字体：字标必须是自有形状，换机器、换字体都不会变；
    而且圆头端点、开口角度、字距都能逐个调，缩小后比现成字体更稳。
    """
    big = size * SUPERSAMPLE
    canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    k = big / MARK_CANVAS
    stroke = 112 * k

    def cap(x: float, y: float) -> None:
        rad = stroke / 2
        draw.ellipse((x - rad, y - rad, x + rad, y + rad), fill=INK)

    # c：开口圆环，缺口朝向 w；arc 的线宽向圆心内缩，圆头落在描边中线上。
    cx, cy, r = 292 * k, 512 * k, 150 * k
    start, end = 48, 312
    draw.arc((cx - r, cy - r, cx + r, cy + r), start=start, end=end, fill=INK, width=int(stroke))
    for angle in (start, end):
        rr = r - stroke / 2
        cap(cx + rr * math.cos(math.radians(angle)), cy + rr * math.sin(math.radians(angle)))

    # w：三条折线，与 c 之间留一个很窄的呼吸位，缩小后仍是一个整体。
    points = [
        (508 * k, 392 * k),
        (572 * k, 646 * k),
        (636 * k, 486 * k),
        (700 * k, 646 * k),
        (764 * k, 392 * k),
    ]
    draw.line(points, fill=INK, width=int(stroke), joint="curve")
    for x, y in points:
        cap(x, y)

    return canvas.resize((size, size), Image.LANCZOS)


def load_glyph() -> Image.Image:
    """画一份字标并裁到内容边界，去掉四周留白。"""
    img = _draw_mark()
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)
    return img


def tray_glyph(size: int) -> Image.Image:
    """把字标等比缩进目标尺寸，留着透明底（颜色由调用方染色）。

    字标是横宽形（约 1.85:1），不能按正方形拉伸，否则 c 会被压扁；
    这里按宽度铺满、垂直居中，小尺寸下至少保住横向的字母轮廓。

    小尺寸不做「加粗/腐蚀」之类的加工——试过：MaxFilter 加粗之后 16px 反而
    从「糊掉的 logo」变成「一坨实心块」，形状信息全丢，不如老老实实高质量缩放。
    """
    if _GLYPH is None:  # 只在 main() 之前被调用才会发生
        raise SystemExit("源字形尚未载入")
    inner_w = max(1, int(round(size * 0.92)))
    inner_h = max(1, int(round(_GLYPH.height * inner_w / _GLYPH.width)))
    mark = _GLYPH.resize((inner_w, inner_h), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.alpha_composite(mark, ((size - inner_w) // 2, (size - inner_h) // 2))
    return canvas


def tint(img: Image.Image, color: tuple[int, int, int]) -> Image.Image:
    """保留 alpha 形状，把颜色换成指定色（黑/白两套托盘图靠它）。"""
    out = Image.new("RGBA", img.size, (*color, 0))
    out.putalpha(img.getchannel("A"))
    return out


def compose_tray(size: int, color: tuple[int, int, int]) -> Image.Image:
    """托盘图：logo 字形 + 指定颜色，透明底。"""
    return tint(tray_glyph(size), color)


BLACK = (0, 0, 0)
WHITE = (255, 255, 255)

# 安装器「纸本账簿」配色：与应用内米白纸面 + 墨绿字标一致。
# MUI_BGCOLOR 同时作用于页眉条、欢迎页与完成页，页眉 / 侧栏位图必须用同一个纸色。
PAPER = (250, 247, 240)  # #FAF7F0
INK = (31, 58, 51)  # #1F3A33
HAIRLINE = (231, 224, 210)  # #E7E0D2


def rounded_plate(size: int, color: tuple[int, int, int] = PAPER) -> Image.Image:
    """圆角块：应用图标与 favicon 的底，圆角比例固定，各尺寸看起来一致。"""
    big = size * SUPERSAMPLE
    canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    radius = int(big * 0.23)
    draw.rounded_rectangle((0, 0, big - 1, big - 1), radius=radius, fill=color)
    return canvas.resize((size, size), Image.LANCZOS)


def compose_app(size: int, dark: bool = False) -> Image.Image:
    """应用图标：纸色圆角块 + 墨绿字标（宽度占 74%，四周留呼吸位）。

    `dark=True` 出的是应用内深色主题用的小图标：墨绿底 + 纸色字标，
    避免一块亮纸色在深色侧栏里抢走视线；系统图标仍用浅色版。
    """
    if _GLYPH is None:
        raise SystemExit("源字形尚未载入")
    plate = rounded_plate(size, INK if dark else PAPER)
    target_w = max(1, int(round(size * 0.74)))
    target_h = max(1, int(round(_GLYPH.height * target_w / _GLYPH.width)))
    mark = _GLYPH.resize((target_w, target_h), Image.LANCZOS)
    if dark:
        mark = tint(mark, PAPER)
    plate.alpha_composite(mark, ((size - target_w) // 2, (size - target_h) // 2))
    return plate


def flatten_bmp(img: Image.Image, background: tuple[int, int, int]) -> Image.Image:
    """RGBA → RGB：把透明底换成纯色，供 NSIS 的 BMP（24-bit、无 alpha）用。"""
    canvas = Image.new("RGB", img.size, background)
    canvas.paste(img, mask=img.getchannel("A"))
    return canvas


def _wordmark_font(size: int) -> ImageFont.FreeTypeFont | None:
    """找一个可用的半粗西文字体画字标；找不到就退回纯 logo，不硬塞默认点阵字。"""
    candidates = (
        Path(r"C:\Windows\Fonts\seguisb.ttf"),
        Path(r"C:\Windows\Fonts\segoeuib.ttf"),
        Path(r"C:\Windows\Fonts\segoeui.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    )
    for path in candidates:
        if not path.is_file():
            continue
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return None


def fit_glyph(glyph: Image.Image, height: int) -> Image.Image:
    """按高度等比缩放字标，宽度跟着长宽比走（字标是横宽的，不能塞进正方形）。"""
    width = max(1, int(round(glyph.width * height / glyph.height)))
    return glyph.resize((width, height), Image.LANCZOS)


def installer_header_bmp(glyph: Image.Image) -> Image.Image:
    """安装器内页页眉图（150×57）：纸色底 + 墨色 logo 与字标。

    以前这里是整块墨绿品牌块，深色矩形压在白/纸色页眉上，会把安装器切成
    「色块 + 白页」两截。现在改成与 MUI_BGCOLOR 同色的纸底，只留 logo 和
    字标，页眉与页面自然连成一张纸。
    """
    canvas = Image.new("RGB", (150, 57), PAPER)
    font = _wordmark_font(13)

    mark = tint(fit_glyph(glyph, 22), INK)
    left = 14
    canvas.paste(mark, (left, (57 - mark.height) // 2), mask=mark.getchannel("A"))
    if font is None:
        return canvas

    draw = ImageDraw.Draw(canvas)
    # anchor=lm：以左侧垂直中点对齐，避免不同字体基线差异把字标顶偏。
    draw.text((left + mark.width + 8, 29), "chaierww", font=font, fill=INK, anchor="lm")
    return canvas


def installer_sidebar_bmp(glyph: Image.Image) -> Image.Image:
    """安装器欢迎/完成页侧栏图（164×314）：留白极简。

    只在左上角放一枚小 logo 与字标，其余全部交给纸色留白；不再画标语、
    账簿横线或右边界。第一屏的视觉重心交给右侧标题和正文，侧栏只负责
    「这是谁的安装器」。文字只用英文，避免位图依赖中文字体。
    """
    canvas = Image.new("RGB", (164, 314), PAPER)

    mark = tint(fit_glyph(glyph, 34), INK)
    left, top = 18, 52
    canvas.paste(mark, (left, top), mask=mark.getchannel("A"))

    draw = ImageDraw.Draw(canvas)
    wordmark = _wordmark_font(10)
    if wordmark is not None:
        draw.text((left, top + mark.height + 12), "chaierww", font=wordmark, fill=INK)
    return canvas


def write_web_icons() -> None:
    """把同一套应用图标写回 Web 端，favicon / 主屏图标不会与桌面端分叉。"""
    WEB.mkdir(parents=True, exist_ok=True)
    compose_app(512).save(WEB / "android-chrome-512x512.png", "PNG", optimize=True)
    compose_app(192).save(WEB / "android-chrome-192x192.png", "PNG", optimize=True)
    compose_app(192, dark=True).save(
        WEB / "android-chrome-192x192-dark.png", "PNG", optimize=True
    )
    compose_app(180).save(WEB / "apple-touch-icon.png", "PNG", optimize=True)
    compose_app(32).save(WEB / "favicon-32x32.png", "PNG", optimize=True)
    compose_app(16).save(WEB / "favicon-16x16.png", "PNG", optimize=True)
    write_ico(WEB / "favicon.ico", [compose_app(s) for s in (16, 32, 48)])


def main() -> None:
    global _GLYPH

    OUT.mkdir(parents=True, exist_ok=True)
    glyph = load_glyph()
    _GLYPH = glyph
    print(f"源字形：{glyph.size[0]}×{glyph.size[1]}")

    # ---- Web 端图标（侧栏品牌块、favicon、iOS 主屏）----
    write_web_icons()
    print("✅ web/public：android-chrome 512/192、apple-touch-icon、favicon 32/16/ico")

    # ---- 应用图标 ----
    compose_app(1024).save(OUT / "icon.png", "PNG")
    print("✅ icon.png（1024，macOS / Linux 兜底）")

    write_ico(OUT / "icon.ico", [compose_app(s) for s in WIN_ICO_SIZES])
    print(f"✅ icon.ico（{', '.join(map(str, WIN_ICO_SIZES))}）")

    try:
        compose_app(1024).save(
            OUT / "icon.icns", "ICNS", sizes=[(s, s) for s in MAC_ICNS_SIZES]
        )
        print("✅ icon.icns（macOS）")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️  icon.icns 生成失败（Pillow 版本限制），交给 electron-builder 从 icon.png 转：{exc}")

    # ---- Linux 图标集 ----
    linux_dir = OUT / "icons"
    linux_dir.mkdir(exist_ok=True)
    for size in LINUX_SIZES:
        compose_app(size).save(linux_dir / f"{size}x{size}.png", "PNG")
    print(f"✅ icons/（{', '.join(f'{s}x{s}' for s in LINUX_SIZES)}）")

    # ---- 托盘：Windows（浅色任务栏黑字形 / 深色任务栏白字形）----
    TRAY_OUT.mkdir(parents=True, exist_ok=True)
    write_ico(
        TRAY_OUT / "tray-light.ico",
        [compose_tray(s, BLACK) for s in WIN_TRAY_SIZES],
    )
    write_ico(
        TRAY_OUT / "tray-dark.ico",
        [compose_tray(s, WHITE) for s in WIN_TRAY_SIZES],
    )
    print(f"✅ assets/tray-light.ico / tray-dark.ico（{', '.join(map(str, WIN_TRAY_SIZES))}）")

    # ---- 托盘：Linux ----
    compose_tray(32, BLACK).save(TRAY_OUT / "tray-light.png", "PNG")
    compose_tray(32, WHITE).save(TRAY_OUT / "tray-dark.png", "PNG")
    print("✅ assets/tray-light.png / tray-dark.png（32×32，Linux 托盘）")

    # ---- 托盘：macOS 模板图 ----
    # 模板图只需要黑色 + alpha，系统按菜单栏明暗自动反色。
    for suffix, size in (("", 16), ("@2x", 32)):
        tray_glyph(size).save(TRAY_OUT / f"trayTemplate{suffix}.png", "PNG")
    print("✅ assets/trayTemplate.png / @2x（macOS 菜单栏模板图）")

    # ---- NSIS 安装器位图（150×57 页眉 / 164×314 侧栏，24-bit 无 alpha）----
    installer_header_bmp(glyph).save(OUT / "installerHeader.bmp", "BMP")
    installer_sidebar_bmp(glyph).save(OUT / "installerSidebar.bmp", "BMP")
    print("✅ installerHeader.bmp（150×57）/ installerSidebar.bmp（164×314）")


if __name__ == "__main__":
    main()
