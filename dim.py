#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dim.py — Windows 软件亮度调节（可以比显示器硬件最低亮度更暗）

原理：
  1) Gamma Ramp（SetDeviceGammaRamp）：直接压低显卡输出曲线，属于真正的像素级变暗，
     可以压到硬件亮度 0 以下的效果，HDR 关闭时效果最好。
  2) 若 Gamma 被驱动拒绝（部分 HDR/独显直连环境），回退到全屏半透明黑色遮罩窗口。

用法：
  python dim.py 30        设为 30%（0-100，100 为正常亮度）
  python dim.py 100       或 --restore 恢复正常
  python dim.py get       查看当前 gamma 状态
  python dim.py 30 --overlay   强制用遮罩方式
"""
import sys
import ctypes
from ctypes import c_ushort, Structure, WinDLL, byref, get_last_error

user32 = WinDLL("user32", use_last_error=True)
gdi32 = WinDLL("gdi32", use_last_error=True)


class RAMP(Structure):
    _fields_ = [("red", c_ushort * 256),
                ("green", c_ushort * 256),
                ("blue", c_ushort * 256)]


def _hdc():
    return user32.GetDC(None)


def gamma_get():
    h = _hdc()
    r = RAMP()
    ok = gdi32.GetDeviceGammaRamp(h, byref(r))
    user32.ReleaseDC(None, h)
    if not ok:
        return None
    return [r.green[i] for i in (0, 64, 128, 255)]


def gamma_set(brightness):
    """brightness: 0.0 ~ 1.0"""
    b = max(0.0, min(1.0, brightness))
    h = _hdc()
    r = RAMP()
    for i in range(256):
        v = int(round(65535.0 * (i / 255.0) * b))
        r.red[i] = r.green[i] = r.blue[i] = v
    ok = gdi32.SetDeviceGammaRamp(h, byref(r))
    err = get_last_error()
    user32.ReleaseDC(None, h)
    return ok, err


def gamma_restore():
    h = _hdc()
    r = RAMP()
    for i in range(256):
        v = int(round(65535.0 * i / 255.0))
        r.red[i] = r.green[i] = r.blue[i] = v
    ok = gdi32.SetDeviceGammaRamp(h, byref(r))
    user32.ReleaseDC(None, h)
    return ok


def overlay_set(percent):
    """全屏黑色遮罩，alpha = 1 - percent/100。需要保持进程运行。"""
    import tkinter as tk
    root = tk.Tk()
    root.attributes("-fullscreen", True)
    root.attributes("-topmost", True)
    root.config(bg="black")
    alpha = max(0.0, min(0.95, 1.0 - percent / 100.0))
    root.attributes("-alpha", alpha)
    root.overrideredirect(True)

    def quit_(event):
        root.destroy()
    root.bind("<Escape>", quit_)
    print("遮罩已生效，按 Esc 恢复。关闭本进程也会恢复。")
    root.mainloop()


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    mode_overlay = "--overlay" in args
    args = [a for a in args if a != "--overlay"]

    if args[0] == "get":
        cur = gamma_get()
        print("当前 green ramp [0,64,128,255] =", cur)
        return
    if args[0] in ("--restore", "100"):
        if gamma_restore():
            print("已恢复正常亮度 (100%)")
        else:
            print("恢复失败，错误码", get_last_error())
        return

    try:
        percent = int(args[0])
    except ValueError:
        print("参数应为 0-100 的整数，或 get / --restore")
        return
    if not 0 <= percent <= 100:
        print("范围 0-100")
        return

    if mode_overlay:
        overlay_set(percent)
        return

    ok, err = gamma_set(percent / 100.0)
    cur = gamma_get()
    if ok and cur:
        # 读回验证：中点应约为 32768*percent/100
        expect = int(round(32768 * percent / 100.0))
        drift = abs(cur[2] - expect)
        if drift > 4096:
            print(f"Gamma 设置返回成功但读回偏差大（mid={cur[2]}, 期望≈{expect}），"
                  f"驱动可能做了限制，建议改用 --overlay")
        else:
            print(f"已设为 {percent}%（读回 mid={cur[2]}）")
    else:
        print(f"Gamma 被拒绝（err={err}），改用遮罩方式…")
        overlay_set(percent)


if __name__ == "__main__":
    main()
