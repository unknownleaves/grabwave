"""Draws the Grabwave icon (original artwork) and writes assets/grabwave.png, .ico and .icns.
Run:  python make_icon.py     (needs Pillow; the build script does this for you)"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent / "assets"
S = 1024  # draw large, then downsample for smooth edges


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def draw() -> Image.Image:
    # background: rounded square with a diagonal purple gradient
    top, bottom = (138, 108, 255), (70, 40, 220)
    grad = Image.new("RGB", (S, S))
    px = grad.load()
    for y in range(S):
        for x in range(S):
            px[x, y] = lerp(top, bottom, (x * 0.35 + y * 0.65) / S)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((32, 32, S - 32, S - 32), radius=230, fill=255)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)

    # soft highlight: fades from the top edge to nothing
    fade = Image.new("L", (S, S), 0)
    fp = fade.load()
    for y in range(S):
        v = max(0, int(46 * (1 - y / 560)))
        for x in range(S):
            fp[x, y] = v
    hl = Image.new("RGBA", (S, S), (255, 255, 255, 0))
    hl.putalpha(Image.composite(fade, Image.new("L", (S, S), 0), mask))
    img = Image.alpha_composite(img, hl)

    white = (255, 255, 255, 255)
    d = ImageDraw.Draw(img)
    cx = S // 2
    # play-triangle "video" seed at the top, pointing down into the tray (a download arrow)
    shaft_w = 150
    d.rounded_rectangle((cx - shaft_w // 2, 215, cx + shaft_w // 2, 520), radius=40, fill=white)
    d.polygon([(cx - 245, 480), (cx + 245, 480), (cx, 745)], fill=white)
    # round the arrow head corners a little
    head = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(head).polygon([(cx - 245, 480), (cx + 245, 480), (cx, 745)], fill=white)
    head = head.filter(ImageFilter.GaussianBlur(10))
    head_mask = head.getchannel("A").point(lambda v: 255 if v > 128 else 0)
    img.paste(Image.new("RGBA", (S, S), white), (0, 0), head_mask)
    # tray
    d.rounded_rectangle((250, 790, S - 250, 850), radius=30, fill=white)
    d.rounded_rectangle((250, 700, 310, 850), radius=30, fill=white)
    d.rounded_rectangle((S - 310, 700, S - 250, 850), radius=30, fill=white)

    return img


def main():
    OUT.mkdir(exist_ok=True)
    img = draw()
    img.resize((512, 512), Image.LANCZOS).save(OUT / "grabwave.png")
    sizes = [16, 24, 32, 48, 64, 128, 256]
    img.resize((256, 256), Image.LANCZOS).save(OUT / "grabwave.ico", sizes=[(s, s) for s in sizes])
    try:  # macOS icon
        img.save(OUT / "grabwave.icns", format="ICNS")
    except Exception as e:  # noqa: BLE001
        print("icns not written:", e)
    print("wrote", OUT / "grabwave.png", OUT / "grabwave.ico", OUT / "grabwave.icns")


if __name__ == "__main__":
    main()
