"""TokenLens 图标生成器（Windows 高兼容版）。

从 assets/logo-source.jpg 裁出圆角卡片，导出多尺寸 .ico。
**ICO 内部全部用未压缩的 32-bit BMP(DIB) 帧**——不用 Pillow 默认的 PNG 帧，
因为 Windows 资源管理器对 PNG 帧的小尺寸解码不稳定（会显示成空白页图标）。

同时输出 web/ 下的 png 版本（页面/文档用）。
"""

from __future__ import annotations

import os
import struct
import sys

from PIL import Image, ImageDraw, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
SRC = os.path.join(ROOT, "assets", "logo-source.jpg")
SIZES = [16, 24, 32, 48, 64, 128, 256]


def _tile_bbox(img: Image.Image):
    """以四角背景色为基准，找出与背景差异明显的区域（即那张圆角卡片）。"""
    w, h = img.size
    small = img.convert("RGB").resize((256, 256))
    px = small.load()
    corners = [px[2, 2], px[253, 2], px[2, 253], px[253, 253]]
    bg = tuple(sum(c[i] for c in corners) / 4.0 for i in range(3))
    xs, ys = [], []
    for y in range(256):
        for x in range(256):
            r, g, b = px[x, y]
            dist = ((r - bg[0]) ** 2 + (g - bg[1]) ** 2 + (b - bg[2]) ** 2) ** 0.5
            if dist > 14:
                xs.append(x)
                ys.append(y)
    if not xs:
        side = int(min(w, h) * 0.74)
        left, top = (w - side) // 2, (h - side) // 2
        return (left, top, left + side, top + side)
    sx, sy = w / 256.0, h / 256.0
    pad = 1.5 * sx
    return (max(0, int(min(xs) * sx - pad)), max(0, int(min(ys) * sy - pad)),
            min(w, int((max(xs) + 1) * sx + pad)), min(h, int((max(ys) + 1) * sy + pad)))


def _rounded(img: Image.Image, radius_ratio: float = 0.225) -> Image.Image:
    size = img.size[0]
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1],
                                           radius=int(size * radius_ratio), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(size * 0.002))
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img.convert("RGBA"), (0, 0), mask)
    return out


def _edge_fix(img: Image.Image) -> Image.Image:
    """给白底图标描一圈极淡的薄荷色内描边，深色桌面上不会糊成一片白。"""
    size = img.size[0]
    stroke = max(1, size // 64)
    overlay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    r = int(size * 0.225)
    d.rounded_rectangle([stroke // 2, stroke // 2, size - 1 - stroke // 2, size - 1 - stroke // 2],
                        radius=r, outline=(126, 200, 165, 150), width=stroke)
    return Image.alpha_composite(img, overlay)


def from_source(src: str) -> Image.Image:
    img = Image.open(src).convert("RGB")
    tile = img.crop(_tile_bbox(img))
    side = min(tile.size)
    tile = tile.crop(((tile.size[0] - side) // 2, (tile.size[1] - side) // 2,
                      (tile.size[0] + side) // 2, (tile.size[1] + side) // 2))
    return _edge_fix(_rounded(tile.resize((1024, 1024), Image.LANCZOS)))


def procedural() -> Image.Image:
    import math
    size = 1024
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i in range(size):
        t = i / size
        d.line([(0, i), (size, i)],
               fill=(int(0x35 + 0x17 * t), int(0xE0 - 0x17 * t), int(0xA1 + 0x4F * t), 255))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1],
                                           radius=int(size * .24), fill=255)
    img.putalpha(mask)
    r = ImageDraw.Draw(img)
    cx = cy = size / 2
    ro, ri = size * 0.335, size * 0.235
    r.ellipse([cx - ro, cy - ro, cx + ro, cy + ro], fill=(7, 17, 14, 255))
    r.ellipse([cx - ri, cy - ri, cx + ri, cy + ri], fill=(0, 0, 0, 0))
    r.rectangle([cx - size * .036, cy - size * .30, cx + size * .036, cy + size * .30],
                fill=(7, 17, 14, 255))
    return img


def _bmp_frame(img: Image.Image) -> bytes:
    """一张图 -> ICO 的 BMP(DIB) 帧（32bpp BGRA + AND 掩码）。"""
    w, h = img.size
    px = img.convert("RGBA").load()
    # XOR 位图：自下而上，每行 BGRA
    xor = bytearray()
    for y in range(h - 1, -1, -1):
        for x in range(w):
            r, g, b, a = px[x, y]
            xor += bytes((b, g, r, a))
    # AND 掩码：1bpp，按 alpha 判定，行按 4 字节对齐
    row_bytes = ((w + 31) // 32) * 4
    and_mask = bytearray()
    for y in range(h - 1, -1, -1):
        bits = bytearray(row_bytes)
        for x in range(w):
            if px[x, y][3] < 128:
                bits[x // 8] |= 0x80 >> (x % 8)
        and_mask += bits
    header = struct.pack("<IiiHHIIiiII",
                         40,          # biSize
                         w, h * 2,    # biWidth, biHeight(含掩码)
                         1, 32,       # biPlanes, biBitCount
                         0,           # biCompression = BI_RGB
                         len(xor) + len(and_mask),
                         0, 0, 0, 0)
    return bytes(header) + bytes(xor) + bytes(and_mask)


def write_ico(path: str, base: Image.Image, sizes=SIZES):
    frames = []
    for s in sizes:
        im = base.resize((s, s), Image.LANCZOS) if base.size[0] != s else base
        frames.append((s, _bmp_frame(im)))
    count = len(frames)
    out = bytearray(struct.pack("<HHH", 0, 1, count))
    offset = 6 + 16 * count
    for s, data in frames:
        out += struct.pack("<BBBBHHII", 0 if s >= 256 else s, 0 if s >= 256 else s,
                           0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for _, data in frames:
        out += data
    with open(path, "wb") as fh:
        fh.write(bytes(out))
    return len(out)


def main():
    os.makedirs(WEB, exist_ok=True)
    base = from_source(SRC) if os.path.exists(SRC) else procedural()
    print("source:", SRC if os.path.exists(SRC) else "(procedural fallback)")

    base.resize((512, 512), Image.LANCZOS).save(os.path.join(WEB, "logo-512.png"))
    base.resize((256, 256), Image.LANCZOS).save(os.path.join(WEB, "icon-256.png"))
    base.resize((64, 64), Image.LANCZOS).save(os.path.join(WEB, "icon-64.png"))
    base.resize((32, 32), Image.LANCZOS).save(os.path.join(WEB, "favicon-32.png"))

    for name in ("icon.ico", "app-icon-v3.ico"):
        n = write_ico(os.path.join(WEB, name), base)
        print(f"  {name}: {n/1024:.0f} KB, sizes {SIZES}, BMP frames")

    # 资源管理器对非系统盘（如 D:\code\...）的自定义 .ico 读取不稳定，会显示成空白页。
    # 统一安装一份到用户应用数据目录（C 盘），快捷方式指向这里。
    appdata = os.environ.get("LOCALAPPDATA")
    if appdata:
        import shutil
        target_dir = os.path.join(appdata, "TokenLens")
        os.makedirs(target_dir, exist_ok=True)
        dst = os.path.join(target_dir, "app.ico")
        shutil.copyfile(os.path.join(WEB, "app-icon-v3.ico"), dst)
        print("  installed ->", dst)
    print("done")


if __name__ == "__main__":
    sys.exit(main())
