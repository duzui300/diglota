"""Draw the desktop icon and write it as a multi-size Windows .ico.

The mark is the app's own two colours: a deep teal ground (the Spanish ink the
reader already reads) and a paper-coloured serif D. Nothing else -- at 16 px in a
taskbar there is room for one letterform, and a letterform set in the app's reading
serif is the most it can honestly say. Rendered large and downsampled, so the small
sizes keep clean edges.

    python packages/win/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
OUT = HERE / "assets" / "diglota.ico"
PREVIEW = HERE / "assets" / "diglota-256.png"

TEAL = (35, 94, 88)      # --es-ink, paper theme
PAPER = (244, 239, 230)  # --page, paper theme
SIZE = 1024
RADIUS = 0.22            # corner radius, as a share of the side

FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/palab.ttf"),   # Palatino Linotype Bold
    Path("C:/Windows/Fonts/georgiab.ttf"),  # Georgia Bold
    Path("C:/Windows/Fonts/timesbd.ttf"),
)


def font_path() -> Path:
    for path in FONT_CANDIDATES:
        if path.is_file():
            return path
    raise SystemExit("no serif bold font found; add one to FONT_CANDIDATES")


def render() -> Image.Image:
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, SIZE - 1, SIZE - 1), radius=int(SIZE * RADIUS), fill=TEAL)

    # Sized by the glyph's own bounding box rather than the em square, so the D
    # fills the tile the same way at any font -- and optically raised by 1.5%,
    # because a capital's visual weight sits above its geometric centre.
    target = int(SIZE * 0.62)
    font = ImageFont.truetype(str(font_path()), target)
    left, top, right, bottom = draw.textbbox((0, 0), "D", font=font)
    glyph_w, glyph_h = right - left, bottom - top
    x = (SIZE - glyph_w) / 2 - left
    y = (SIZE - glyph_h) / 2 - top - SIZE * 0.015
    draw.text((x, y), "D", font=font, fill=PAPER)

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
