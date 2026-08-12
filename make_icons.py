#!/usr/bin/env python3
"""Sinh icon PNG cho PWA bằng Python thuần (không cần cài Pillow).

Mỗi trạm 1 icon: nền tối bo góc, viền màu trạm, 1 chữ cái lớn ở giữa
(B = Bếp, E = Expo) để khi thêm vào màn hình chính iPad/Android phân biệt rõ.
Xuất: icon-<station>-{180,192,512}.png vào public/.
"""
import os
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(HERE, "public")

# 5x7 bitmap font cho vài chữ cái cần dùng
FONT = {
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
}


def hex_rgb(h):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def make_png(size, bg, border, letter_color, letter):
    px = bytearray([0]) * (size * size * 4)  # RGBA, mặc định trong suốt

    def put(x, y, rgba):
        if 0 <= x < size and 0 <= y < size:
            i = (y * size + x) * 4
            px[i:i + 4] = bytes(rgba)

    r = size * 0.20  # bán kính bo góc
    inset = max(1, int(size * 0.02))
    bthick = max(2, int(size * 0.045))
    for y in range(size):
        for x in range(size):
            # tính có nằm trong hình vuông bo góc không
            dx = min(x - inset, (size - 1 - inset) - x)
            dy = min(y - inset, (size - 1 - inset) - y)
            inside = True
            edge_dist = 1e9
            if dx < r and dy < r:  # góc
                cx = inset + r if x < size / 2 else (size - 1 - inset) - r
                cy = inset + r if y < size / 2 else (size - 1 - inset) - r
                dist = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
                inside = dist <= r
                edge_dist = r - dist
            else:
                edge_dist = min(
                    x - inset, (size - 1 - inset) - x,
                    y - inset, (size - 1 - inset) - y,
                )
            if inside and edge_dist >= 0:
                col = border if edge_dist < bthick else bg
                put(x, y, (col[0], col[1], col[2], 255))

    # vẽ chữ cái ở giữa
    glyph = FONT[letter]
    gh, gw = len(glyph), len(glyph[0])
    scale = int(size * 0.62 / gh)
    ox = (size - gw * scale) // 2
    oy = (size - gh * scale) // 2
    for gy, row in enumerate(glyph):
        for gx, c in enumerate(row):
            if c == "1":
                for sy in range(scale):
                    for sx in range(scale):
                        put(ox + gx * scale + sx, oy + gy * scale + sy,
                            (letter_color[0], letter_color[1], letter_color[2], 255))

    # đóng gói PNG
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # filter none
        raw.extend(px[y * size * 4:(y + 1) * size * 4])
    comp = zlib.compress(bytes(raw), 9)

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", comp) + chunk(b"IEND", b""))


BG = hex_rgb("#0d1117")
STATIONS = {
    "kitchen": {"border": hex_rgb("#f59e0b"), "letter": "B", "lc": hex_rgb("#f59e0b")},
    "expo": {"border": hex_rgb("#22c55e"), "letter": "E", "lc": hex_rgb("#22c55e")},
}

for name, s in STATIONS.items():
    for sz in (180, 192, 512):
        data = make_png(sz, BG, s["border"], s["lc"], s["letter"])
        out = os.path.join(PUBLIC, f"icon-{name}-{sz}.png")
        with open(out, "wb") as f:
            f.write(data)
        print("wrote", out, len(data), "bytes")
