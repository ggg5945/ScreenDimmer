#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_icon.py — 用 dim_gui 的共享图标设计生成多尺寸 icon.ico（exe 打包用）

用法: python gen_icon.py   （输出 icon.ico，build_exe.bat 会自动传给 PyInstaller）
"""
import os
import struct

from dim_gui import _icon_rgba


def bmp_entry(rgba, size):
    """单尺寸 ICO 条目：BITMAPINFOHEADER + BGRA(自底向上) + AND 掩码"""
    stride = size * 4
    rows = []
    for y in range(size - 1, -1, -1):          # 自底向上
        row = rgba[y * stride:(y + 1) * stride]
        rows.append(bytes(x for i in range(0, stride, 4)
                          for x in (row[i + 2], row[i + 1], row[i], row[i + 3])))
    xor = b"".join(rows)
    and_stride = ((size + 31) // 32) * 4       # 4 字节对齐
    and_mask = bytes(and_stride * size)        # 全 0，alpha 生效
    header = struct.pack("<IiiHHIIiiII",
                         40, size, size * 2, 1, 32, 0,
                         len(xor) + len(and_mask), 0, 0, 0, 0)
    return header + xor + and_mask


def main():
    sizes = [16, 24, 32, 48, 64, 128, 256]
    entries = []          # (dir_entry_bytes, data)
    body = b""
    for s in sizes:
        ss = 4 if s <= 32 else 2      # 小尺寸多超采样，抗锯齿更干净
        data = bmp_entry(_icon_rgba(s, ss=ss), s)
        b = s if s < 256 else 0
        dir_entry = struct.pack("<BBBBHHII", b, b, 0, 0, 1, 32,
                                len(data), 6 + 16 * len(sizes) + len(body))
        entries.append(dir_entry)
        body += data

    ico = (struct.pack("<HHH", 0, 1, len(sizes)) + b"".join(entries) + body)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
    with open(out, "wb") as f:
        f.write(ico)
    print(f"[icon] 已生成 {out} ({len(ico)} bytes, 尺寸 {sizes})")


if __name__ == "__main__":
    main()
