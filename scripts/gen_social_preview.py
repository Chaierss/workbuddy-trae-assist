"""生成 GitHub social preview 图（1280×640）。

这张图出现在每条分享卡片上（推文、群聊、Slack 预览），对 star 转化比
README 正文更直接——但很多人忘了设，默认就是仓库头像的模糊拉伸。

视觉语言跟官网首屏（ProjPitch.vue）一致：纯深色底、单一暖白、无渐变。
两个不同点，都是因为**载体从竖屏换成了 1280×640 的横屏**：
1. 「点一下」放大到 200px——小缩略图里三个字必须一眼可辨
2. 右侧叠一枚淡到几乎只是形状的 logo，否则右半张是空的
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1280, 640
BG = (15, 17, 22)
FG = (245, 243, 239)
MUTED = (150, 145, 139)
FAINT = (110, 104, 99)
RULE = (38, 41, 48)
GHOST = (52, 56, 64)  # logo 烙印：只比底色亮一点，够看出形状，不抢字

ROOT = Path(__file__).resolve().parent.parent
LOGO = ROOT / "web" / "public" / "android-chrome-512x512.png"
OUT = ROOT / "docs" / "images" / "social-preview.png"

BOLD = "C:/Windows/Fonts/msyhbd.ttc"
REG = "C:/Windows/Fonts/msyh.ttc"


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def tinted(src: Path, color: tuple[int, int, int]) -> Image.Image:
    """把 logo 重染成单色，alpha 原样保留。

    不能直接 ImageOps.invert：原图是透明底（透明像素 RGB=0），反色会把
    这些位置翻成 255，边缘抗锯齿渗出一圈白边。按 alpha 蒙版重建就没这问题。
    """
    a = Image.open(src).convert("RGBA").getchannel("A")
    flat = Image.new("RGB", a.size, color)
    return Image.merge("RGBA", (*flat.split(), a))


def main() -> None:
    canvas = Image.new("RGB", (W, H), BG)

    # 先贴烙印，再画字——保证字永远压在图形之上
    size = 380
    ghost = tinted(LOGO, GHOST).resize((size, size), Image.LANCZOS)
    canvas.paste(ghost, (W - size - 84, (H - size) // 2 + 10), ghost)

    draw = ImageDraw.Draw(canvas)
    left, right = 96, W - 96

    draw.text((left, 64), "chaierww", font=font(BOLD, 22), fill=MUTED)

    # 第一层：一串动作。刻意用小字——它是陪衬，不是主角
    draw.text(
        (left, 186),
        "换一个号：退出 · 扫码 · 等同步 · 找回会话",
        font=font(REG, 28),
        fill=MUTED,
    )

    # 第二层：一个词。这块是整张图唯一需要被记住的东西
    draw.text((left, 232), "点一下", font=font(BOLD, 200), fill=FG)

    draw.line([(left, 530), (right, 530)], fill=RULE, width=1)

    # 底部只留一行：产品定位 + 隐私承诺。分两行在 640 高里会挤成一条
    base = 585
    draw.text(
        (left, base),
        "WorkBuddy / Trae 多账号本地切换  ·  凭据只在本机，不经云端",
        font=font(REG, 21),
        fill=MUTED,
        anchor="ls",
    )
    draw.text(
        (right, base),
        "github.com/Chaierss/workbuddy-trae-assist",
        font=font(REG, 17),
        fill=FAINT,
        anchor="rs",
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(OUT, "PNG", optimize=True)
    print(f"✓ docs/images/social-preview.png  ({OUT.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
