#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dim_gui.py — 亮度调节小窗体（可低于显示器硬件最低亮度）

50%-100%  : SetDeviceGammaRamp 压暗显卡输出曲线（驱动只接受到 50% 为止）
 1%-50%  : gamma 压满 50% + 全屏黑色遮罩层（鼠标点击穿透），总亮度 = 50% x 透过率

- 拖动滑条实时调整（带平滑动画），1% 为最暗（不会全黑，防止找不到窗口）
- 全局快捷键：右侧 Ctrl + R 一键恢复正常亮度（窗口无焦点也生效）
- 「重置」按钮 / Esc 键 / 关闭窗口 / 进程退出 → 自动恢复正常亮度
- 异常退出（强杀/崩溃）后下次启动自动恢复亮度
"""
import atexit
import base64
import ctypes
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk
from ctypes import (Structure, WinDLL, wintypes, byref, get_last_error,
                    c_ssize_t, POINTER)

from dim import gamma_set, gamma_restore, gamma_get

user32 = WinDLL("user32", use_last_error=True)

# ---------------- Overlay 所需常量 ----------------
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
LWA_ALPHA = 2
HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001

# ---------------- 全局热键（右侧 Ctrl + R）----------------
WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
LLKHF_EXTENDED = 0x10
VK_LCONTROL, VK_RCONTROL, VK_CONTROL, VK_R = 0xA2, 0xA3, 0x11, 0x52

HOOKPROC = ctypes.WINFUNCTYPE(c_ssize_t, ctypes.c_int,
                              ctypes.c_size_t, ctypes.c_ssize_t)


class KBDLLHOOKSTRUCT(Structure):
    _fields_ = [("vkCode", ctypes.c_uint), ("scanCode", ctypes.c_uint),
                ("flags", ctypes.c_uint), ("time", ctypes.c_uint),
                ("dwExtraInfo", ctypes.c_size_t)]


_restore_requested = threading.Event()
_rctrl_down = False
_ll_hook_handle = None
_last_vk = 0          # 诊断用：钩子看到的最后一个虚拟键码
_last_flags = 0

user32.CallNextHookEx.restype = c_ssize_t
user32.CallNextHookEx.argtypes = [c_ssize_t, ctypes.c_int,
                                  ctypes.c_size_t, c_ssize_t]


def _ll_keyboard_proc(nCode, wParam, lParam):
    """底层键盘钩子：只在「右Ctrl按住时按下R」请求恢复，不拦截任何按键。"""
    global _rctrl_down, _last_vk, _last_flags
    if nCode == 0:
        kb = ctypes.cast(lParam, POINTER(KBDLLHOOKSTRUCT)).contents
        _last_vk, _last_flags = kb.vkCode, kb.flags
        if wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
            if kb.vkCode == VK_RCONTROL or (
                    kb.vkCode == VK_CONTROL and kb.flags & LLKHF_EXTENDED):
                _rctrl_down = True
            elif kb.vkCode == VK_R and _rctrl_down:
                _restore_requested.set()
        elif wParam in (WM_KEYUP, WM_SYSKEYUP):
            if kb.vkCode in (VK_LCONTROL, VK_RCONTROL, VK_CONTROL):
                _rctrl_down = False
    return user32.CallNextHookEx(0, nCode, wParam, lParam)


_LL_PROC = HOOKPROC(_ll_keyboard_proc)  # 防止回调被 GC


def _start_hotkey_hook():
    def _run():
        global _ll_hook_handle
        user32.SetWindowsHookExW.restype = c_ssize_t
        _ll_hook_handle = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, _LL_PROC, None, 0)
        msg = wintypes.MSG()  # 64 位下 48 字节，必须用完整结构
        while user32.GetMessageW(byref(msg), None, 0, 0) > 0:
            pass  # 消息循环维持钩子线程
    t = threading.Thread(target=_run, daemon=True, name="hotkey-hook")
    t.start()


# ---------------- 系统托盘 ----------------
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 1, 2, 4, 16
WM_TRAY = 0x8000 + 1          # 托盘回调消息
WM_APP_TRAY_EXIT = 0x8000 + 2  # 让托盘线程退出
WM_NULL = 0x0000
HWND_MESSAGE = -3
TPM_RETURNCMD, TPM_RIGHTBUTTON, TPM_NONOTIFY = 0x0100, 0x0002, 0x0080
MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010


class NOTIFYICONDATAW(Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD), ("guidItem", ctypes.c_ubyte * 16),
        ("hBalloonIcon", wintypes.HICON)]


WNDPROC = ctypes.WINFUNCTYPE(c_ssize_t, wintypes.HWND, wintypes.UINT,
                             ctypes.c_size_t, c_ssize_t)


class WNDCLASSW(Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]

user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateIcon.restype = wintypes.HICON
user32.CreateIcon.argtypes = [
    wintypes.HINSTANCE, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p]
user32.DefWindowProcW.restype = c_ssize_t
user32.DefWindowProcW.argtypes = [
    wintypes.HWND, wintypes.UINT, ctypes.c_size_t, c_ssize_t]


# ---------------- 图标绘制（托盘 / 窗口标题栏 / exe 共用同一设计）----------------
ICON_DARK = (0x1e, 0x24, 0x30)    # 内芯：暗色（表示调暗）
ICON_ACCENT = (0x4a, 0x9e, 0xff)  # 外环：亮蓝


def _icon_pixel(u, v, size):
    """连续坐标 (u,v) 处的颜色；圆环设计：蓝环 + 暗芯，None 表示透明"""
    unit = size / 32.0
    r = ((u - size / 2.0) ** 2 + (v - size / 2.0) ** 2) ** 0.5
    r_out = 14 * unit
    if r > r_out + 0.5:
        return None
    if r <= 7 * unit:
        c = ICON_DARK
    else:
        t = min(1.0, (r - 7 * unit) / (5 * unit))
        c = tuple(int(a + (b - a) * t) for a, b in zip(ICON_DARK, ICON_ACCENT))
    # 外缘抗锯齿
    a = 255 if r <= r_out - 0.7 else int(255 * (r_out + 0.7 - r) / 1.4)
    return (*c, max(0, min(255, a)))


def _icon_rgba(size, ss=3):
    """生成 size x size 的 RGBA 字节串（自顶向下），ss=每像素超采样次数（抗锯齿）"""
    buf = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            rs = gs = bs = asum = cnt = 0
            for sy in range(ss):
                for sx in range(ss):
                    p = _icon_pixel(x + (sx + 0.5) / ss, y + (sy + 0.5) / ss,
                                    size)
                    if p:
                        rs += p[0]; gs += p[1]; bs += p[2]
                        asum += p[3]; cnt += 1
            o = (y * size + x) * 4
            if cnt:
                buf[o:o + 4] = bytes((rs // cnt, gs // cnt, bs // cnt,
                                      asum // ss ** 2))
    return bytes(buf)


def _png_bytes(rgba, size):
    """RGBA 数据编码为 PNG（喂给 tk.PhotoImage 设置标题栏图标）"""
    import zlib
    import struct
    raw = b"".join(b"\x00" + rgba[y * size * 4:(y + 1) * size * 4]
                   for y in range(size))

    def chunk(tag, d):
        return (struct.pack(">I", len(d)) + tag + d +
                struct.pack(">I", zlib.crc32(tag + d)))
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _build_tray_hicon():
    """用共享设计生成 32x32 托盘 HICON（BGRA 自底向上），无需外部 .ico 文件"""
    size = 32
    rgba = _icon_rgba(size)
    stride = size * 4
    rows = []
    for y in range(size - 1, -1, -1):
        row = rgba[y * stride:(y + 1) * stride]
        rows.append(bytes(x for i in range(0, stride, 4)
                          for x in (row[i + 2], row[i + 1], row[i],
                                    row[i + 3])))
    xor = b"".join(rows)
    and_mask = bytes(size // 8 * size)  # 全 0，alpha 生效
    return user32.CreateIcon(None, size, size, 1, 32, and_mask, xor)


class Tray:
    """独立线程持有隐藏窗口 + Shell_NotifyIcon，菜单选择通过队列交给 Tk 主线程。"""

    def __init__(self, tooltip, actions_q):
        self.actions = actions_q
        self.tooltip = tooltip
        self.hwnd = None
        self._created = threading.Event()
        self._keepalive = []  # 防止回调被 GC
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name="tray")
        self.thread.start()
        self._created.wait(timeout=3)

    def _run(self):
        nid_ref = []
        tray_events = []  # WNDPROC 闭包用

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_TRAY:
                m = lparam & 0xFFFF
                if m in (0x0202, 0x0203):        # 左键抬起 / 双击
                    self.actions.put("show")
                elif m == 0x0205:                # 右键抬起 → 菜单
                    self._popup_menu(hwnd)
                return 0
            if msg == WM_APP_TRAY_EXIT:
                user32.DestroyWindow(hwnd)
                return 0
            if msg == 0x0010:                    # WM_DESTROY
                nid = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW),
                                      hWnd=hwnd, uID=1)
                shell.Shell_NotifyIconW(NIM_DELETE, byref(nid))
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        proc = WNDPROC(wndproc)
        self._keepalive.append(proc)

        shell = WinDLL("shell32", use_last_error=True)
        self._keepalive.append(shell)
        g32 = WinDLL("gdi32", use_last_error=True)
        hinst = k32 = ctypes.WinDLL("kernel32").GetModuleHandleW(None)

        wc = WNDCLASSW()
        wc.lpfnWndProc = proc
        wc.lpszClassName = f"ScreenDimmerTray{os.getpid()}"
        wc.hInstance = hinst
        if not user32.RegisterClassW(byref(wc)):
            self._created.set()
            return
        self.hwnd = user32.CreateWindowExW(
            0, wc.lpszClassName, "ScreenDimmerTray", 0, 0, 0, 0, 0,
            HWND_MESSAGE, None, hinst, None)
        if not self.hwnd:
            self._created.set()
            return
        nid_ref.append(self.hwnd)

        hicon = _build_tray_hicon()
        nid = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW),
                              hWnd=self.hwnd, uID=1,
                              uFlags=NIF_MESSAGE | NIF_ICON | NIF_TIP,
                              uCallbackMessage=WM_TRAY, hIcon=hicon,
                              szTip=self.tooltip)
        self.added = bool(shell.Shell_NotifyIconW(NIM_ADD, byref(nid)))
        if not self.added:
            self._created.set()
            return

        self._created.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(byref(msg))
            user32.DispatchMessageW(byref(msg))

    def _popup_menu(self, hwnd):
        hmenu = user32.CreatePopupMenu()
        user32.AppendMenuW(hmenu, MF_STRING, 1, "显示窗口")
        user32.AppendMenuW(hmenu, MF_STRING, 2, "重置亮度")
        user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, "")
        user32.AppendMenuW(hmenu, MF_STRING, 3, "退出")
        pt = wintypes.POINT()
        user32.GetCursorPos(byref(pt))
        user32.SetForegroundWindow(hwnd)
        choice = user32.TrackPopupMenu(
            hmenu, TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_NONOTIFY,
            pt.x, pt.y, 0, hwnd, None)
        user32.PostMessageW(hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(hmenu)
        self.actions.put({1: "show", 2: "reset", 3: "exit"}.get(choice))

    def balloon(self, title, text):
        if not self.hwnd:
            return
        nid = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW),
                              hWnd=self.hwnd, uID=1,
                              uFlags=NIF_INFO, szInfo=text,
                              szInfoTitle=title, dwInfoFlags=0)
        shell = WinDLL("shell32")
        shell.Shell_NotifyIconW(NIM_MODIFY, byref(nid))

    def close(self):
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_APP_TRAY_EXIT, 0, 0)


# ---------------- 鼠标指针压暗 ----------------
# 硬件光标不经过 gamma 也不被遮罩盖住，调暗屏幕时鼠标会保持高亮。
# 做法：把系统标准光标集替换为按亮度系数压暗的副本；恢复用 SPI_SETCURSORS。
#
# 坑（踩过的坑，别再改回去）：
# 1) 32513 文本 I 形光标是「单色 + XOR」光标，LoadCursorW 拿到的是被 GDI 转换
#    过的 32bpp 位图：alpha 全 255、AND 掩码全 1 —— 形状信息全丢。照着重画
#    就会得到一个不透明的 32x32 黑方块（输入框里光标周围的黑块）。
# 2) 1bpp 位图不能用 GetDIBits 读，它返回一堆全 1 的假数据，必须用 BitBlt。
# 3) 读不出形状的光标一律跳过（保持原样），绝不硬造。
SPI_SETCURSORS = 0x0057
CURSOR_IDS = (32512, 32513, 32514, 32515, 32516, 32642, 32643, 32644,
              32645, 32646, 32648, 32649, 32650)  # 箭头/文本/忙碌/十字/调整大小等
DIB_RGB_COLORS = 0


class ICONINFO(Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                ("hbmColor", wintypes.HBITMAP)]


class _BITMAP(Structure):
    _fields_ = [("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG),
                ("bmHeight", wintypes.LONG), ("bmWidthBytes", wintypes.LONG),
                ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                ("bmBits", ctypes.c_void_p)]


class _BMIH(Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class _BMI(Structure):
    _fields_ = [("bmiHeader", _BMIH), ("bmiColors", wintypes.DWORD * 3)]


gdi32 = WinDLL("gdi32", use_last_error=True)
user32.GetIconInfo.argtypes = [wintypes.HANDLE, POINTER(ICONINFO)]
user32.LoadCursorW.restype = wintypes.HANDLE
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.CopyIcon.restype = wintypes.HANDLE
user32.CopyIcon.argtypes = [wintypes.HANDLE]
user32.SetSystemCursor.argtypes = [wintypes.HANDLE, wintypes.DWORD]
user32.DestroyCursor.argtypes = [wintypes.HANDLE]
user32.CreateIconIndirect.restype = wintypes.HANDLE
user32.CreateIconIndirect.argtypes = [POINTER(ICONINFO)]
user32.LoadImageW.restype = wintypes.HANDLE
user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                              wintypes.UINT, ctypes.c_int, ctypes.c_int,
                              wintypes.UINT]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
gdi32.GetDIBits.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.UINT,
                            wintypes.UINT, ctypes.c_void_p,
                            ctypes.c_void_p, wintypes.UINT]
gdi32.CreateDIBSection.restype = wintypes.HANDLE
gdi32.CreateDIBSection.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                   wintypes.UINT, POINTER(ctypes.c_void_p),
                                   wintypes.HANDLE, wintypes.DWORD]
gdi32.CreateCompatibleDC.restype = wintypes.HANDLE
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateBitmap.restype = wintypes.HANDLE
gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT,
                               wintypes.UINT, ctypes.c_void_p]
gdi32.SelectObject.restype = wintypes.HANDLE
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, ctypes.c_int, wintypes.HDC,
                         ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
gdi32.DeleteDC.argtypes = [wintypes.HDC]

SRCCOPY = 0x00CC0020
IMAGE_CURSOR = 2
LR_LOADFROMFILE = 0x0010
SM_CXCURSOR, SM_CYCURSOR = 13, 14

_cursor_base = {}   # OCR id -> 原始光标副本（首次压暗前抓取，保证重复调暗不叠加）
_got_base = False


def _read_dib_32(hbm, w, h):
    """读 32bpp 位图为自顶向下的 BGRA 字节串；失败返回 None"""
    if not hbm or w <= 0 or h <= 0:
        return None
    bmi = _BMI()
    bmi.bmiHeader.biSize = ctypes.sizeof(_BMIH)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h          # 自顶向下
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0      # BI_RGB
    buf = (ctypes.c_ubyte * (w * h * 4))()
    hdc = gdi32.CreateCompatibleDC(None)
    ok = gdi32.GetDIBits(hdc, hbm, 0, h, buf, byref(bmi), DIB_RGB_COLORS)
    gdi32.DeleteDC(hdc)
    return bytes(buf) if ok else None


def _blit_32(hbm, w, h):
    """把任意位图（含 1bpp 掩码）BitBlt 到 32bpp DIB，返回自顶向下 BGRA；失败 None

    注意：1bpp 位图不能用 GetDIBits 读 —— GDI 会返回一堆全 1 的假数据，
    只有 BitBlt 才能拿到真实位。
    """
    if not hbm or w <= 0 or h <= 0:
        return None
    bmi = _BMI()
    bmi.bmiHeader.biSize = ctypes.sizeof(_BMIH)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h          # 自顶向下
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0
    dst = gdi32.CreateCompatibleDC(None)
    bits = ctypes.c_void_p()
    hbm_dst = gdi32.CreateDIBSection(dst, byref(bmi), DIB_RGB_COLORS,
                                     byref(bits), None, 0)
    if not hbm_dst or not bits:
        if hbm_dst:
            gdi32.DeleteObject(hbm_dst)
        gdi32.DeleteDC(dst)
        return None
    old_dst = gdi32.SelectObject(dst, hbm_dst)
    src = gdi32.CreateCompatibleDC(None)
    old_src = gdi32.SelectObject(src, hbm)
    ok = gdi32.BitBlt(dst, 0, 0, w, h, src, 0, 0, SRCCOPY)
    gdi32.SelectObject(src, old_src)
    gdi32.SelectObject(dst, old_dst)
    data = None
    if ok:
        data = bytes((ctypes.c_ubyte * (w * h * 4)).from_address(bits.value))
    gdi32.DeleteObject(hbm_dst)
    gdi32.DeleteDC(src)
    gdi32.DeleteDC(dst)
    return data


def _mask_bits(px, w, h):
    """32bpp 图转 0/1 数组：1 = 白（1bpp 掩码里的位 1 = AND 掩码的透明位）"""
    if px is None:
        return None
    return bytes(1 if px[i * 4] > 127 else 0 for i in range(w * h))


def _read_cursor(hcursor):
    """读取光标：返回 (bgra, w, h, xhot, yhot, transparent)，读不出来返回 None

    transparent 与像素同尺寸，1 表示该像素透明。三种信息来源按可靠性排序：
    1. 32bpp 位图的真 alpha 通道（现代光标）；
    2. AND 掩码（老式 32bpp 光标，alpha 被 GDI 填成假的全 255）；
    3. 单色光标：掩码高 2h，上 AND 下 XOR。
    三者都拿不到形状 → 返回 None，调用方会放弃压暗这只光标。
    宁可不动，也绝不生成一个不透明的黑方块顶上（这是之前 I 形光标的坑）。
    """
    ii = ICONINFO()
    if not user32.GetIconInfo(hcursor, byref(ii)):
        return None
    try:
        bmm = _BITMAP()
        gdi32.GetObjectW(ii.hbmMask, ctypes.sizeof(bmm), byref(bmm))
        if ii.hbmColor:
            bmc = _BITMAP()
            if not gdi32.GetObjectW(ii.hbmColor, ctypes.sizeof(bmc),
                                    byref(bmc)):
                return None
            w, h = bmc.bmWidth, bmc.bmHeight
            if w <= 0 or h <= 0:
                return None
            pixels = _read_dib_32(ii.hbmColor, w, h)
            if pixels is None:
                return None
            n = w * h
            if any(pixels[i] < 255 for i in range(3, n * 4, 4)):
                transparent = bytes(1 if pixels[i * 4 + 3] == 0 else 0
                                    for i in range(n))
                return (pixels, w, h, ii.xHotspot, ii.yHotspot, transparent)
            bits = _mask_bits(_blit_32(ii.hbmMask, w, h), w, h)
            if bits and 0 in bits:       # 掩码里得有不透明位才算有用
                return (pixels, w, h, ii.xHotspot, ii.yHotspot, bits)
            return None
        # 单色光标：hbmColor 为 NULL，掩码高 2h（上半 AND，下半 XOR）
        w, h = bmm.bmWidth, bmm.bmHeight // 2
        if w <= 0 or h <= 0:
            return None
        full = _mask_bits(_blit_32(ii.hbmMask, w, h * 2), w, h * 2)
        if full is None:
            return None
        n = w * h
        and_bits, xor_bits = full[:n], full[n:]
        if and_bits.count(0) == 0:       # 全透明 = 数据不可信
            return None
        pixels = bytearray(n * 4)
        for i in range(n):
            v = 255 if xor_bits[i] else 0     # XOR：1=白 0=黑
            pixels[i * 4:i * 4 + 4] = bytes((v, v, v, 255))
        return (bytes(pixels), w, h, ii.xHotspot, ii.yHotspot, and_bits)
    finally:
        if ii.hbmMask:
            gdi32.DeleteObject(ii.hbmMask)
        if ii.hbmColor:
            gdi32.DeleteObject(ii.hbmColor)


def _build_and_mask(bgra, w, h):
    """按 alpha 生成 1bpp AND 掩码（1 = 透明），与新位图的透明度保持一致"""
    stride = ((w + 31) // 32) * 4
    buf = (ctypes.c_ubyte * (stride * h))()
    for y in range(h):
        row_off = (h - 1 - y) * stride         # DDB 自底向上
        for x in range(w):
            if bgra[(y * w + x) * 4 + 3] == 0:
                buf[row_off + (x >> 3)] |= 0x80 >> (x & 7)
    return gdi32.CreateBitmap(w, h, 1, 1, byref(buf))


def _make_dimmed_cursor(base_hcursor, factor):
    """从基准光标生成亮度 factor(0-1) 的压暗副本 HCURSOR；需调用方负责 SetSystemCursor"""
    got = _read_cursor(base_hcursor)
    if got is None:
        return None
    data, w, h, xh, yh, transparent = got
    n = w * h
    out = bytearray(data)
    for i in range(n):
        o = i * 4
        if transparent[i]:
            out[o:o + 4] = b"\x00\x00\x00\x00"
        else:
            out[o] = int(out[o] * factor)          # B
            out[o + 1] = int(out[o + 1] * factor)  # G
            out[o + 2] = int(out[o + 2] * factor)  # R
            if not out[o + 3]:
                out[o + 3] = 0xFF   # 老式光标 alpha 无效时补满，形状交给掩码
    # 生成新的 32bpp color 位图（自顶向下 DIB Section）
    bmi = _BMI()
    bmi.bmiHeader.biSize = ctypes.sizeof(_BMIH)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0
    hdc = gdi32.CreateCompatibleDC(None)
    bits = ctypes.c_void_p()
    hbm = gdi32.CreateDIBSection(hdc, byref(bmi), DIB_RGB_COLORS,
                                 byref(bits), None, 0)
    gdi32.DeleteDC(hdc)
    if not hbm or not bits:
        if hbm:
            gdi32.DeleteObject(hbm)
        return None
    ctypes.memmove(bits, bytes(out), n * 4)
    # 掩码也重建，保证「掩码 + alpha」两套透明度语义完全一致
    mask = _build_and_mask(out, w, h)
    if not mask:
        gdi32.DeleteObject(hbm)
        return None
    ci = ICONINFO(fIcon=0, xHotspot=xh, yHotspot=yh,
                  hbmMask=mask, hbmColor=hbm)
    hcur = user32.CreateIconIndirect(byref(ci))
    gdi32.DeleteObject(hbm)
    gdi32.DeleteObject(mask)
    return hcur


# 注册表「鼠标方案」里的值名 / 系统默认方案对应的光标文件
_SCHEME_NAMES = {32512: "Arrow", 32513: "IBeam", 32514: "Wait",
                 32515: "SizeAll", 32516: "SizeNS", 32642: "SizeNWSE",
                 32643: "SizeNESW", 32644: "SizeWE", 32645: "SizeNS",
                 32646: "SizeAll", 32648: "No", 32649: "Hand", 32650: "Pin"}
_DEFAULT_FILES = {32512: "aero_arrow.cur", 32513: "beam_r.cur",
                  32514: "aero_busy.ani", 32515: "aero_move.cur",
                  32516: "aero_ns.cur", 32642: "aero_nwse.cur",
                  32643: "aero_nesw.cur", 32644: "aero_ew.cur",
                  32645: "aero_ns.cur", 32646: "aero_move.cur",
                  32648: "aero_unavail.cur", 32649: "aero_link.cur",
                  32650: "aero_pin.cur"}


def _load_cursor_file(cid):
    """从当前鼠标方案的 .cur 文件加载（LoadCursorW 拿到的位图被 GDI 转换过、
    丢了形状信息时的兜底）。动画光标（.ani）跳过 —— 压暗静态化会丢动画。"""
    cands = []
    name = _SCHEME_NAMES.get(cid)
    if name:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Control Panel\Cursors") as k:
                path = winreg.QueryValueEx(k, name)[0]
            if path:
                cands.append(path)
        except OSError:
            pass
    fname = _DEFAULT_FILES.get(cid)
    if fname:
        cands.append(os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"), "cursors", fname))
    cx = user32.GetSystemMetrics(SM_CXCURSOR) or 32
    cy = user32.GetSystemMetrics(SM_CYCURSOR) or 32
    for p in cands:
        p = os.path.expandvars(p)
        if not p.lower().endswith(".cur") or not os.path.exists(p):
            continue
        h = user32.LoadImageW(None, p, IMAGE_CURSOR, cx, cy, LR_LOADFROMFILE)
        if h:
            return h
    return None


def _capture_base_cursors():
    """抓取每只光标的原始副本；读不出形状的光标直接跳过（不压暗，保持原样）"""
    global _got_base
    if _got_base:
        return
    for cid in CURSOR_IDS:
        h = user32.LoadCursorW(None, cid)
        base = user32.CopyIcon(h) if h else None
        if base and _read_cursor(base) is not None:
            _cursor_base[cid] = base
            continue
        if base:
            user32.DestroyCursor(base)
        alt = _load_cursor_file(cid)
        if alt:
            if _read_cursor(alt) is not None:
                _cursor_base[cid] = user32.CopyIcon(alt)
            user32.DestroyCursor(alt)
    _got_base = True


def cursor_apply(percent):
    """把系统标准光标压暗到 percent%（与滑条亮度完全同步，无下限）"""
    pct = max(1, min(100, percent))
    _capture_base_cursors()
    factor = pct / 100.0
    changed = 0
    for cid, base in _cursor_base.items():
        hcur = _make_dimmed_cursor(base, factor)
        if hcur:
            if user32.SetSystemCursor(hcur, cid):
                changed += 1
    return changed


def cursor_restore():
    """恢复：直接让系统按当前鼠标方案从注册表重载光标

    不再用「以 factor=1.0 重建光标」的方式回放 —— 重建过程本身就会把
    依靠掩码定形的光标（I 形文本光标等）弄坏，重载才是 1:1 原样。
    """
    cursor_restore_system()


def cursor_restore_system():
    """从注册表重载系统光标方案（崩溃恢复 / 命令行兜底用）"""
    user32.SystemParametersInfoW(SPI_SETCURSORS, 0, None, 0)


# ---------------- 异常退出标记 ----------------
MARKER = os.path.join(
    os.environ.get("TEMP", os.path.expanduser("~")), "screendimmer_active")


def _write_marker():
    try:
        with open(MARKER, "w") as f:
            f.write("1")
    except OSError:
        pass


def _clear_marker():
    try:
        os.remove(MARKER)
    except OSError:
        pass


# ---------------- 主题配色 ----------------
BG = "#1e1f24"        # 窗口底
FG = "#e8eaed"        # 普通文字
ACCENT = "#4a9eff"    # 高亮蓝
GREEN = "#3ddc84"     # 恢复成功绿
TROUGH = "#33363d"    # 滑条槽


def _mix(c1, c2, t):
    """两个 #rrggbb 颜色线性插值"""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(
        int(x + (y - x) * t) for x, y in zip(a, b))


class Overlay:
    """全屏黑色遮罩，鼠标/键盘点击穿透，用 SetLayeredWindowAttributes 控制透明度。"""

    def __init__(self, master):
        self._alpha = None
        self.win = tk.Toplevel(master)
        self.win.overrideredirect(True)
        self.win.config(bg="black")
        self.win.attributes("-topmost", True)
        self.win.withdraw()
        self._hwnd_ready = False
        self._hwnd = None
        self.win.bind("<Map>", self._on_map)

    def _on_map(self, _e):
        self._setup_hwnd()
        self.win.attributes("-topmost", True)
        self._reapply_alpha()

    def _setup_hwnd(self):
        if self._hwnd_ready:
            return
        hwnd = user32.GetParent(self.win.winfo_id())
        if not hwnd:
            hwnd = self.win.winfo_id()
        self._hwnd = hwnd
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(
            hwnd, GWL_EXSTYLE,
            style | WS_EX_LAYERED | WS_EX_TRANSPARENT |
            WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE)
        self._hwnd_ready = True

    def _reapply_alpha(self):
        if self._hwnd and self._alpha is not None:
            user32.SetLayeredWindowAttributes(
                self._hwnd, 0, int(self._alpha * 255), LWA_ALPHA)

    def show(self, alpha):
        alpha = max(0.0, min(0.98, alpha))
        self._alpha = alpha
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        self.win.geometry(f"{sw}x{sh}+0+0")
        self.win.deiconify()
        self.win.lift()
        self._setup_hwnd()
        self._reapply_alpha()
        master_hwnd = user32.GetParent(self.win.master.winfo_id())
        if master_hwnd:
            user32.SetWindowPos(master_hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE)
            user32.SetWindowPos(master_hwnd, -2, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE)
            user32.SetWindowPos(master_hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE)

    def hide(self):
        self._alpha = None
        self.win.withdraw()


class App:
    MIN_PERCENT = 1

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("屏幕亮度")
        self.root.resizable(False, False)   # 不置顶，普通窗口
        self.root.config(bg=BG)
        # 标题栏图标与托盘/exe 一致（同一绘制代码生成 PNG）
        try:
            self._icon_img = tk.PhotoImage(
                data=base64.b64encode(_png_bytes(_icon_rgba(64, ss=3), 64)))
            self.root.iconphoto(True, self._icon_img)
        except Exception:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self._real_exit)  # 点X=真正退出
        self.root.bind("<Unmap>", self._on_unmap)
        self.root.bind("<Escape>", lambda e: self.reset(flash=True))
        # 窗口内 Ctrl+R 兜底（万一钩子不可用）
        self.root.bind("<Control-KeyPress-r>", lambda e: self.reset(flash=True))

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=BG)
        style.configure("TButton", background="#3a3d45", foreground=FG,
                        borderwidth=0, focusthickness=0, padding=(14, 6))
        style.map("TButton",
                  background=[("active", ACCENT), ("pressed", "#2f78c2")],
                  foreground=[("active", "#ffffff")])
        style.configure("Dim.Horizontal.TScale",
                        troughcolor=TROUGH, background=ACCENT,
                        bordercolor=BG, lightcolor=ACCENT,
                        darkcolor=TROUGH, sliderthickness=18)

        frm = ttk.Frame(self.root, padding=20)
        frm.pack()

        self.label = tk.Label(frm, text="100%", font=("Segoe UI", 16, "bold"),
                              fg=FG, bg=BG)
        self.label.pack()

        self.scale = ttk.Scale(frm, from_=self.MIN_PERCENT, to=100,
                               orient="horizontal", length=360,
                               style="Dim.Horizontal.TScale",
                               command=self.on_slide)
        self.scale.set(100)
        self.scale.pack(pady=(8, 4))
        self.scale.bind("<Button-1>", self._on_trough_click, add="+")

        btn = ttk.Button(frm, text="重 置 亮 度", command=self.reset)
        btn.pack(pady=(6, 0))

        # 明显的快捷键提示条
        tip = tk.Label(frm, text="⌨  快捷键：右侧 Ctrl + R  一键恢复正常亮度",
                       font=("Microsoft YaHei UI", 10), fg=ACCENT, bg=BG)
        tip.pack(pady=(12, 0))

        self.overlay = Overlay(self.root)

        # 上次异常退出的自检：先恢复亮度再进入界面
        self._crash_recovered = os.path.exists(MARKER)
        if self._crash_recovered:
            gamma_restore()
            cursor_restore_system()  # 强杀后系统光标可能停在压暗状态，从注册表重载
            _clear_marker()
            self._flash("✓ 已恢复上次异常退出的亮度")
        # 开局抓基准光标副本（后续会话内恢复靠它 1:1 重放）
        _capture_base_cursors()

        # 平滑动画状态
        self.current = 100.0
        self.target = 100.0
        self._dirty = False
        self._flash_token = 0
        self._cursor_pct = None
        self._tray_hint_shown = False
        self._restored = False
        atexit.register(self.restore_once)

        # 系统托盘（独立线程 + 消息队列）
        self.tray_q = queue.Queue()
        self.tray = Tray("屏幕亮度（右Ctrl+R恢复，左键打开窗口）", self.tray_q)

        self.root.after(16, self._tick)
        self.root.after(50, self._poll_hotkey)
        self.root.after(50, self._poll_tray)
        _start_hotkey_hook()

    # ---------------- 亮度应用与动画 ----------------
    def _apply_percent(self, p):
        p = max(self.MIN_PERCENT, min(100, int(round(p))))
        if p >= 50:
            ok, _err = gamma_set(p / 100.0)
            self.overlay.hide()
            self.root.attributes("-topmost", False)  # 正常状态不置顶
            if not ok:
                self.label.config(text=f"{p}% (gamma被拒绝)", fg="#ff6b6b")
                return False
        else:
            ok, _err = gamma_set(0.5)
            self.overlay.show(1 - 2 * p / 100.0)
            if not ok:
                self.label.config(text=f"{p}% (gamma异常)", fg="#ff6b6b")
                return False
        # 鼠标指针跟随屏幕亮度同步压暗/恢复
        if self._cursor_pct != p:
            self._cursor_pct = p
            if p >= 100:
                cursor_restore()
            else:
                cursor_apply(p)
        return True

    def _tick(self):
        # 滑条目标值平滑逼近，产生亮度渐变特效；收敛后补一次最终应用
        diff = self.target - self.current
        if abs(diff) > 0.5:
            self.current += diff * 0.3
            self._apply_percent(self.current)
        elif self._dirty:
            self.current = self.target
            self._apply_percent(self.current)
            self._dirty = False
        self.root.after(16, self._tick)

    def _poll_hotkey(self):
        if _restore_requested.is_set():
            _restore_requested.clear()
            self.reset(flash=True)
        self.root.after(50, self._poll_hotkey)

    def _set_label(self, p):
        t = 1 - p / 100.0  # 越暗越偏蓝高亮
        self.label.config(text=f"{p}%", fg=_mix(FG, ACCENT, t * 0.8))

    def _flash(self, msg):
        self._flash_token += 1
        token = self._flash_token

        def restore_text():
            if token == self._flash_token:
                self._set_label(int(round(self.target)))
        self.label.config(text=msg, fg=GREEN)
        self.root.after(1500, restore_text)

    # ---------------- 用户操作 ----------------
    def _on_trough_click(self, event):
        """点击滑条槽任意位置直接跳到该亮度（点在滑块上则走默认拖拽）"""
        if "slider" in self.scale.identify(event.x, event.y):
            return
        w = self.scale.winfo_width()
        inset = 16  # clam 主题 + sliderthickness=18 的槽边距（实测标定）
        span = max(1, w - 2 * inset)
        x = max(inset, min(w - inset, event.x))
        val = self.MIN_PERCENT + (x - inset) / span * (100 - self.MIN_PERCENT)
        self.scale.set(val)
        self.on_slide(val)

    def on_slide(self, value):
        self.target = float(value)
        self._dirty = True
        p = int(round(self.target))
        self._set_label(p)
        if p < 100:
            _write_marker()
        else:
            _clear_marker()

    def reset(self, flash=False):
        self.scale.set(100)
        self.on_slide(100)
        if flash:
            self._flash("✓ 已恢复正常亮度 (100%)")

    def restore_once(self):
        if not self._restored:
            self._restored = True
            _clear_marker()
            try:
                gamma_restore()
            except Exception:
                pass
            try:
                cursor_restore()
            except Exception:
                pass

    def _poll_tray(self):
        try:
            while True:
                action = self.tray_q.get_nowait()
                if action == "show":
                    self._show_window()
                elif action == "reset":
                    self._show_window()
                    self.reset(flash=True)
                elif action == "exit":
                    self._real_exit()
        except queue.Empty:
            pass
        self.root.after(50, self._poll_tray)

    def _show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _on_unmap(self, event):
        # 最小化（含点关闭前先 iconify 的情况）→ 收进托盘
        if event.widget is self.root and self.root.state() == "iconic":
            self._hide_to_tray()

    def _hide_to_tray(self):
        self.root.withdraw()
        if not self._tray_hint_shown:
            self._tray_hint_shown = True
            self.tray.balloon("ScreenDimmer", "已最小化到托盘，左键点击图标打开窗口。点 X 关闭 = 退出并恢复亮度。")

    def _real_exit(self):
        self.restore_once()
        self.tray.close()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("restore", "--restore"):
        gamma_restore()
        cursor_restore_system()
        _clear_marker()
        print("已恢复亮度")
    else:
        App().run()
