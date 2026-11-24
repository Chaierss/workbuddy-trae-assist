"""把 README 的静态截图拼成一张会动的预览图（docs/images/demo.gif）。

为什么不是录屏：仓库里的截图已经由 `scripts/shots.py --demo` 脱敏并统一尺寸，
直接拿它们做一段「缓慢推进 + 交叉淡入」的轮播，既不会出现真实账号信息，
也不用把 Electron 窗口、桌面通知一起录进来。要换素材只改 `SOURCES`。

用法：

    python scripts/demo_gif.py
    python scripts/demo_gif.py --width 900 --out docs/images/demo.gif

依赖：Pillow（与 build_icons.py 相同）。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

# 顺序即播放顺序：总览 → 号池 → 活动目录。三张都是脱敏后的演示数据。
SOURCES = [
    ("overview.png", "总览"),
    ("accounts.png", "号池"),
    ("activity.png", "活动"),
]

# 画布比例固定 16:10，和 GitHub 首图在窄屏下的裁切习惯接近。
ASPECT = 16 / 10


def cover(image: Image.Image, size: tuple[int, int], zoom: float) -> Image.Image:
    """按「填满画布」缩放，再以顶部为锚点裁切：顶栏和第一屏内容不会被裁掉。"""
    width, height = size
    scale = max(width / image.width, height / image.height) * zoom
    resized = image.resize(
        (max(width, round(image.width * scale)), max(height, round(image.height * scale))),
        Image.LANCZOS,
    )
    left = (resized.width - width) // 2
    # 垂直方向靠上裁：截图顶部是标题与结论条，比底部更有信息量。
    top = 0
    return resized.crop((left, top, left + width, top + height))


def build(
    out: Path,
    width: int,
    fps: int,
    hold: float,
    fade: float,
    zoom: float,
) -> None:
    height = round(width / ASPECT)
    size = (width, height)
    hold_frames = max(2, round(hold * fps))
    fade_frames = max(2, round(fade * fps))

    frames: list[Image.Image] = []
    previous: Image.Image | None = None

    for name, _label in SOURCES:
        source = Image.open(DOCS / "images" / name).convert("RGB")

        # 每张图在停留期间从 1.00 缓慢推到 1.00 + zoom，避免「幻灯片」的死板感。
        current_frames = [
            cover(source, size, 1.0 + zoom * index / max(1, hold_frames - 1))
            for index in range(hold_frames)
        ]

        if previous is not None:
            for index in range(1, fade_frames + 1):
                frames.append(
                    Image.blend(previous, current_frames[0], index / (fade_frames + 1))
                )

        frames.extend(current_frames)
        previous = current_frames[-1]

    # 用第一帧生成统一调色板，避免逐帧自适应导致整段颜色忽明忽暗。
    palette = frames[0].quantize(colors=128, method=Image.Quantize.MEDIANCUT)
    quantized = [
        frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames
    ]

    out.parent.mkdir(parents=True, exist_ok=True)
    quantized[0].save(
        out,
        save_all=True,
        append_images=quantized[1:],
        duration=round(1000 / fps),
        loop=0,
        optimize=True,
        disposal=2,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 README 动图（截图轮播）")
    parser.add_argument("--out", default=str(DOCS / "images" / "demo.gif"))
    parser.add_argument("--width", type=int, default=880, help="输出宽度")
    parser.add_argument("--fps", type=int, default=10, help="帧率")
    parser.add_argument("--hold", type=float, default=1.4, help="每张停留秒数")
    parser.add_argument("--fade", type=float, default=0.3, help="交叉淡入秒数")
    parser.add_argument("--zoom", type=float, default=0.025, help="停留期间的推进幅度")
    args = parser.parse_args()

    missing = [name for name, _ in SOURCES if not (DOCS / "images" / name).exists()]
    if missing:
        print(f"缺少截图：{', '.join(missing)}；先跑 python scripts/shots.py --demo")
        return 1

    out = Path(args.out)
    build(out, args.width, args.fps, args.hold, args.fade, args.zoom)

    with Image.open(out) as gif:
        count = getattr(gif, "n_frames", 1)
    size_mb = out.stat().st_size / (1024 * 1024)
    print(f"完成：{out}  {args.width}x{round(args.width / ASPECT)}  {count} 帧  {size_mb:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
