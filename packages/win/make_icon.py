"""Build the desktop icon from the brand's open-book mark.

The mark is ``assets/diglota_pure_logo.svg``, rendered once to paper ink in
``packages/win/assets/diglota-mark.png``. The tile is the brand's deep teal, so
the book keeps its shape in a taskbar at 16 px without borrowing the wordmark.
Rendered large and downsampled, so the small sizes keep clean edges.

    python packages/win/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
MARK = HERE / "assets" / "diglota-mark.png"
OUT = HERE / "assets" / "diglota.ico"
PREVIEW = HERE / "assets" / "diglota-256.png"

TEAL = (31, 74, 62)      # the logo's deep teal
SIZE = 1024
RADIUS = 0.22            # corner radius, as a share of the side
MARK_WIDTH = 0.84        # how much of the tile the open book fills


def render() -> Image.Image:
    mark = Image.open(MARK).convert("RGBA")
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, SIZE - 1, SIZE - 1), radius=int(SIZE * RADIUS), fill=TEAL)

    width = int(SIZE * MARK_WIDTH)
    height = round(mark.height * width / mark.width)
    mark = mark.resize((width, height), Image.LANCZOS)
    image.alpha_composite(mark, ((SIZE - width) // 2, (SIZE - height) // 2))

    # Draw big, then let the ICO writer downsample: one crisp master, no per-size
    # hinting to get wrong.
    return image.resize((256, 256), Image.LANCZOS)


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    master = render()
    master.save(PREVIEW)
    master.save(OUT, format="ICO", sizes=[
        (16, 16), (20, 20), (24, 24), (32, 32),
        (40, 40), (48, 48), (64, 64), (128, 128), (256, 256),
    ])
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
    print(f"wrote {PREVIEW}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
