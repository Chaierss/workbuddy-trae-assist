# -*- coding: utf-8 -*-
"""从 Web 端的应用图标生成桌面端各平台所需的图标。

源：`web/public/android-chrome-512x512.png`（499×499 RGBA，黑色字形 + 透明底）

产物（都在 desktop/build/ 下）：

| 文件 | 用途 |
| --- | --- |
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

1. **就是 logo 本身，不另画简化标记**。托盘图直接由同一份源字形缩放而来，
   跟应用图标、任务栏图标、网页 favicon 是同一个图形。16px 下它必然糊，
   但「糊掉的 logo」和「另一个形状」是两回事——用户认的是形状要一致。
2. **按系统深浅色出两套**。logo 是纯黑字形 + 透明底：浅色任务栏用黑字形，
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

用法：`python desktop/scripts/build_icons.py`
"""

from __future__ import annotations

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
SRC = ROOT / "web" / "public" / "android-chrome-512x512.png"
OUT = ROOT / "desktop" / "build"
# 托盘图必须放在 buildResources 之外，否则会被排除在 app.asar 外（详见文件头说明）。
TRAY_OUT = ROOT / "desktop" / "assets"

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



def load_glyph() -> Image.Image:
    """载入源图并裁到内容边界，去掉四周留白。"""
    img = Image.open(SRC).convert("RGBA")
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)
    return img


def tray_glyph(size: int) -> Image.Image:
    """把 logo 缩放成目标尺寸，留着透明底（颜色由调用方染色）。

    `pad_ratio` 给小一点（0.04）：托盘格子本身已经很小，四周再留白等于自废武功。

    小尺寸不做「加粗/腐蚀」之类的加工——试过：MaxFilter 加粗之后 16px 反而
    从「糊掉的 logo」变成「一坨实心块」，形状信息全丢，不如老老实实高质量缩放。
    """
    pad_ratio = 0.04
    inner = max(1, int(round(size * (1 - pad_ratio * 2))))
    if _GLYPH is None:  # 只在 main() 之前被调用才会发生
        raise SystemExit("源字形尚未载入")
    mark = _GLYPH.resize((inner, inner), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.alpha_composite(mark, ((size - inner) // 2, (size - inner) // 2))
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


def compose_app(size: int, glyph: Image.Image, pad_ratio: float = 0.1) -> Image.Image:
    """应用图标：透明底 + 原字形（保持与 Web 端 favicon 完全一致）。"""
    inner = int(size * (1 - pad_ratio * 2))
    mark = glyph.resize((inner, inner), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.alpha_composite(mark, ((size - inner) // 2, (size - inner) // 2))
    return canvas


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


def installer_header_bmp(glyph: Image.Image) -> Image.Image:
    """安装器内页页眉图（150×57）：墨绿品牌块 + 反白 logo 与字标。

    位图占据页眉右侧整块，与左侧纸色的标题区形成一条清晰分界。所以要整块
    铺满墨绿、不留纸色边——留边会变成「贴上去的矩形补丁」。
    """
    canvas = Image.new("RGB", (150, 57), INK)
    font = _wordmark_font(15)

    logo = 28
    mark = tint(glyph.resize((logo, logo), Image.LANCZOS), PAPER)
    if font is None:
        canvas.paste(mark, ((150 - logo) // 2, (57 - logo) // 2), mask=mark.getchannel("A"))
        return canvas

    canvas.paste(mark, (12, (57 - logo) // 2), mask=mark.getchannel("A"))
    draw = ImageDraw.Draw(canvas)
    # anchor=lm：以左侧垂直中点对齐，避免不同字体基线差异把字标顶偏。
    draw.text((48, 29), "chaierww", font=font, fill=PAPER, anchor="lm")
    return canvas


def installer_sidebar_bmp(glyph: Image.Image) -> Image.Image:
    """安装器欢迎/完成页侧栏图（164×314）：整块墨绿品牌面板。

    侧栏是这两个页面唯一的大块视觉，做成满幅色块才撑得起品牌感；
    文字只用英文，避免位图依赖中文字体。底部几条细线承接「账簿」主题。
    """
    canvas = Image.new("RGB", (164, 314), INK)

    logo = 80
    mark = tint(glyph.resize((logo, logo), Image.LANCZOS), PAPER)
    canvas.paste(mark, ((164 - logo) // 2, 62), mask=mark.getchannel("A"))

    draw = ImageDraw.Draw(canvas)
    wordmark = _wordmark_font(16)
    if wordmark is not None:
        draw.text((82, 168), "chaierww", font=wordmark, fill=PAPER, anchor="mm")
        tagline = _wordmark_font(8)
        if tagline is not None:
            draw.text(
                (82, 186),
                "LOCAL · MULTI-ACCOUNT",
                font=tagline,
                fill=(198, 214, 203),
                anchor="mm",
            )

    # 一条亮线 + 三条暗线：远看是呼吸留白，近看仍是账簿的横格。
    draw.line([(40, 232), (124, 232)], fill=(198, 214, 203), width=1)
    for y in (250, 268, 286):
        draw.line([(32, y), (132, y)], fill=(63, 92, 82), width=1)
    return canvas


def main() -> None:
    global _GLYPH

    if not SRC.is_file():
        raise SystemExit(f"找不到源图标：{SRC}")

    OUT.mkdir(parents=True, exist_ok=True)
    glyph = load_glyph()
    _GLYPH = glyph
    print(f"源字形：{glyph.size[0]}×{glyph.size[1]}")

    # ---- 应用图标 ----
    compose_app(1024, glyph).save(OUT / "icon.png", "PNG")
    print("✅ icon.png（1024，macOS / Linux 兜底）")

    write_ico(OUT / "icon.ico", [compose_app(s, glyph) for s in WIN_ICO_SIZES])
    print(f"✅ icon.ico（{', '.join(map(str, WIN_ICO_SIZES))}）")

    try:
        compose_app(1024, glyph).save(
            OUT / "icon.icns", "ICNS", sizes=[(s, s) for s in MAC_ICNS_SIZES]
        )
        print("✅ icon.icns（macOS）")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️  icon.icns 生成失败（Pillow 版本限制），交给 electron-builder 从 icon.png 转：{exc}")

    # ---- Linux 图标集 ----
    linux_dir = OUT / "icons"
    linux_dir.mkdir(exist_ok=True)
    for size in LINUX_SIZES:
        compose_app(size, glyph).save(linux_dir / f"{size}x{size}.png", "PNG")
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
