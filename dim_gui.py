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


def _build_tray_hicon():
    """程序绘制一个 32x32 32bpp 托盘图标（调光滑块风格：蓝环+暗芯），无需外部 .ico 文件。"""
    g32 = WinDLL("gdi32", use_last_error=True)
    size = 32
    px = bytearray()
    for y in range(size):
        for x in range(size):
            dx, dy = x - 15.5, y - 15.5
            r = (dx * dx + dy * dy) ** 0.5
            if r <= 14:
                if r <= 7:      # 内芯：暗色（表示调暗）
                    px += bytes((0x30, 0x24, 0x1e, 0xff))   # BGRA
                elif r >= 12:   # 外环：亮蓝
                    px += bytes((0xff, 0x9e, 0x4a, 0xff))
                else:           # 过渡环
                    t = (r - 7) / 5
                    px += bytes((int(0x30 + (0xff - 0x30) * t),
                                 int(0x24 + (0x9e - 0x24) * t),
                                 int(0x1e + (0x4a - 0x1e) * t), 0xff))
            else:
                px += bytes((0, 0, 0, 0))
    # BGRA 自底向上
    stride = size * 4
    rows = [bytes(px[y * stride:(y + 1) * stride]) for y in range(size)]
    xor = b"".join(reversed(rows))
    and_mask = bytes(size // 8 * size)  # 全 0，alpha 生效
    hicon = user32.CreateIcon(None, size, size, 1, 32, and_mask, xor)
    return hicon


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
            _clear_marker()
            self._flash("✓ 已恢复上次异常退出的亮度")

        # 平滑动画状态
        self.current = 100.0
        self.target = 100.0
        self._dirty = False
        self._flash_token = 0
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
        _clear_marker()
        print("已恢复亮度")
    else:
        App().run()
