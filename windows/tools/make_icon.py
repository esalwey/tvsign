"""Generates windows/assets/tvsign.ico (16-256 px), tray.png and the installer bitmaps.

Same TV glyph as the web UI favicon (screen + stand, 24-unit grid), white on a blue rounded square.
Usage: python tools/make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "assets"
BLUE_TOP, BLUE_BOTTOM = (0x2E, 0x93, 0xFF), (0x00, 0x62, 0xD6)
S = 1024                      # supersampled canvas


def glyph(size: int, background: bool = True) -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    if background:
        grad = Image.new("RGBA", (S, S))
        gd = ImageDraw.Draw(grad)
        for y in range(S):
            k = y / (S - 1)
            gd.line([(0, y), (S, y)], fill=tuple(int(a + (b - a) * k) for a, b in zip(BLUE_TOP, BLUE_BOTTOM)) + (255,))
        mask = Image.new("L", (S, S), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.225), fill=255)
        img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    u = S / 24                 # favicon grid unit
    # small sizes get a thicker stroke so the glyph stays readable
    stroke = int(u * (2.6 if size <= 24 else 2.2 if size <= 48 else 1.8))
    white = (255, 255, 255, 255)
    def p(x, y):
        c = 12 * u
        scale = 0.78 if background else 1.0   # tray-only glyph fills the square
        return (c + (x * u - c) * scale, c + (y * u - c) * scale)
    x0, y0 = p(2.5, 3.6)
    x1, y1 = p(21.5, 16.1)
    d.rounded_rectangle([x0, y0, x1, y1], radius=int(2.2 * u), outline=white, width=stroke)
    sx0, sy = p(8, 20.2)
    sx1, _ = p(16, 20.2)
    d.line([(sx0, sy), (sx1, sy)], fill=white, width=stroke)
    cx, cy0 = p(12, 16.1)
    d.line([(cx, cy0), (cx, sy)], fill=white, width=stroke)
    r = stroke / 2
    for (x, y) in ((sx0, sy), (sx1, sy)):
        d.ellipse([x - r, y - r, x + r, y + r], fill=white)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    frames = [glyph(s) for s in sizes]
    frames[-1].save(OUT / "tvsign.ico", format="ICO", sizes=[(s, s) for s in sizes],
                    append_images=frames[:-1])
    glyph(64).save(OUT / "tray.png")
    glyph(256).save(OUT / "tvsign-256.png")
    # Inno Setup wizard bitmaps (BMP, no alpha): small 55x55 on white
    small = Image.new("RGB", (55, 55), (255, 255, 255))
    g = glyph(55)
    small.paste(g, (0, 0), g)
    small.save(OUT / "wizard-small.bmp")
    print("icons written to", OUT)


if __name__ == "__main__":
    main()
