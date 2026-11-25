# ScreenDimmer — 屏幕亮度调节工具

在显示器硬件亮度已调到最低的情况下，用软件把屏幕调得**更暗**。绿色单文件
exe，无需安装 Python，双击即用。

## 功能

| 功能 | 说明 |
|---|---|
| 拖动滑条调暗 | 范围 1%–100%，点击槽位任意位置直接跳到该亮度，拖动实时生效并带平滑渐变动画，1% 接近全黑但控制窗仍可见 |
| 鼠标指针同步调暗 | 屏幕调暗时系统标准光标按相同比例压暗（不会出现屏幕全暗鼠标刺眼的情况）；恢复时 1:1 还原你原来的光标外观。注意：应用程序自带的自定义光标（如部分游戏内的光标）不受影响 |
| 系统托盘 | 主窗口**不置顶**；点「最小化」收进右下角系统托盘（点 **X** 则是真正退出并恢复亮度）。托盘图标左键打开窗口，右键菜单：显示窗口 / 重置亮度 / 退出 |
| 右侧 Ctrl + R | **全局快捷键**，一键恢复正常亮度（窗口没有焦点时也生效；特意用右 Ctrl 避免与浏览器左 Ctrl+R 刷新冲突） |
| 重置亮度按钮 | 恢复 100% |
| 自动恢复 | 点 X 关闭窗口、托盘菜单「退出」、进程退出（atexit）时自动恢复亮度 |
| 异常退出自检 | 被强杀、崩溃、断电后，下次启动程序时自动恢复亮度 |
| 应急恢复命令 | `ScreenDimmer.exe restore` 直接恢复亮度后退出，可做桌面快捷方式 |

界面滑条下方有醒目的快捷键提示条，恢复成功时窗口会闪现绿色
「✓ 已恢复正常亮度」提示。

## 使用

```
ScreenDimmer.exe            打开调节窗口
ScreenDimmer.exe restore    命令行应急恢复亮度（不开窗口）
```

滑条说明：

- **50%–100%**：通过显卡 gamma 曲线压暗，画质无损
- **1%–50%**：gamma 压满驱动允许的 50% 后，再叠加一层可点击穿透的
  全屏黑色遮罩继续压暗（总亮度 = 50% × 遮罩透过率，正好等于滑条读数）

## 原理与限制

- 核心是 `SetDeviceGammaRamp`：把显卡输出曲线整体乘一个系数。Windows
  驱动出于安全考虑**拒绝压暗超过 50%** 的曲线（实测边界正好在 50%），
  所以更暗的区间用黑遮罩补足。
- 硬件光标不经过 gamma，也不被遮罩盖住，因此鼠标指针用
  `SetSystemCursor` 单独压暗；被强杀后光标可能停在压暗状态，下次启动
  程序会自动从注册表重载光标方案，也可运行 `ScreenDimmer.exe restore`。
- 遮罩只作用于主显示器，且盖不住独占全屏的程序（部分游戏/视频播放器），
  那类程序仍受 gamma 控制。
- 修改分辨率、切换 HDR、注销重登会让 gamma 复位，重新拖一下滑条即可。

## 恢复亮度的所有途径（应急手册）

按优先级：

1. 右侧 **Ctrl + R**（任何情况下）
2. 窗口里的「重置亮度」按钮或 **Esc** 键
3. 托盘图标右键菜单 → 「重置亮度」
4. 程序被强杀后亮度卡住 → **重新双击 ScreenDimmer.exe**，它会自动恢复
5. 双击也不行 → 命令行运行 `ScreenDimmer.exe restore`
6. 终极兜底：注销重新登录 / 重启（系统会重置 gamma）

## 打包

双击 `build_exe.bat`，或在命令行运行：

```
build_exe.bat
```

脚本会自动安装缺失的 PyInstaller、探测当前 Python 环境（支持 conda）里的
`ffi.dll` 和 tcl/tk 运行库并打进包，输出 `dist/ScreenDimmer.exe`。

> 注意：bat 内容为纯 ASCII，不要往里加中文（cmd 解析 UTF-8 中文批处理
> 容易出现字节错位把内容当命令执行）。

## 开发

**环境**：Windows 10/11；Python 3.12（3.8+ 均可）；零第三方依赖——GUI 用
标准库 `tkinter`，系统交互用 `ctypes`，无需 `pip install` 任何东西。

**从源码运行**：

```
python dim_gui.py            窗体程序
python dim.py 30             命令行调光器（30%）
python gen_icon.py           重新生成 icon.ico（改过 _icon_pixel 后）
build_exe.bat                打包
```

**代码要点**（改代码前值得知道的实现细节）：

| 模块 | 关键点 |
|---|---|
| gamma 调光 | `SetDeviceGammaRamp`，驱动拒绝压暗超过 50% 的曲线，50% 以下由遮罩补足 |
| 遮罩 | `Toplevel` 全屏黑窗 + `WS_EX_TRANSPARENT\|WS_EX_LAYERED`（点击穿透），`SetLayeredWindowAttributes` 控制透明度 |
| 全局热键 | 独立线程装 `WH_KEYBOARD_LL` 底层键盘钩子，右 Ctrl（VK 0xA3）按下时才响应 R；事件经 `threading.Event` 交给 Tk 主线程轮询 |
| 托盘 | 独立线程的隐藏消息窗口 + `Shell_NotifyIcon`，菜单动作经 `queue.Queue` 回到主线程 |
| 光标压暗 | 开局抓取基准光标：优先用注册表鼠标方案里的 `.cur` 文件（`LoadImageW`），读不出形状才回退 `LoadCursorW`；压暗时缩放 RGB 后 `CreateIconIndirect` + `SetSystemCursor`；恢复用 `SPI_SETCURSORS` 从注册表重载方案（用户自定义方案也一并还原） |
| 光标形状来源 | 按 alpha → AND 掩码 → 单色 AND/XOR 三级取值；1bpp 掩码必须靠 `BitBlt` 读（`GetDIBits` 会返回全 1 假数据），形状读不出来就跳过这只光标，绝不硬造（详见下面坑位清单） |
| 动画 | 亮度值每 16ms 向目标插值 0.3，收敛后补一次最终应用（`_dirty` 标记） |
| 异常自检 | 调暗中在 `%TEMP%\screendimmer_active` 留标记，正常退出删除；下次启动发现标记即恢复亮度+重载光标方案 |

**ctypes 坑位清单**（本项目踩过）：64 位下给 Windows API 传句柄/参数前必须设
`restype`/`argtypes`（否则 int 截断触发 `int too long to convert`）；
`wintypes` 没有 `WNDCLASSW`/`INPUT`（前者需自定义，后者按 40 字节对齐
union）；`GetMessageW` 的 MSG 缓冲必须用完整结构（48 字节），缓冲不足会写越界静默破坏消息泵。

**光标坑位（差点毁掉文本光标）**：
- `IDC_IBEAM`(32513) 是单色+XOR 光标，`LoadCursorW` 返回的是被 GDI 转换过的版本：
  alpha 全 255、AND 掩码全 1，形状信息全丢。照着重画 = 输入框里光标周围一个不透明黑方块。
- `GetDIBits` 读 1bpp 掩码不可靠（返回全 1 假数据），必须 `BitBlt` 到 32bpp DIB 再读。
- 恢复千万别用「factor=1.0 重建」回放，直接 `SPI_SETCURSORS` 从注册表重载才是原样。

版本号只存在于 Release Tag 上（exe 内部未嵌版本信息），以 Releases 页为准。

## 文件结构

```
dim.py          gamma 读写核心（SetDeviceGammaRamp 封装 + 命令行版调光器）
dim_gui.py      窗体程序（滑条动画 / 全局热键 / 托盘 / 鼠标指针同步调暗 / 异常自检）
gen_icon.py     图标生成脚本（多尺寸 icon.ico，exe 与窗口/托盘共用设计）
icon.ico        生成的图标文件（打包时 --icon 内嵌进 exe）
build_exe.bat   打包脚本（双击运行）
dist/ScreenDimmer.exe   成品
```

## 已知注意事项

- 窗口标题栏/托盘/exe 文件图标为同一设计（蓝色调光环）；改图标设计请修
  改 `dim_gui.py` 里的 `_icon_pixel`，然后运行 `python gen_icon.py` 重新
  生成 icon.ico，再打包
- 快捷键提示：如热键无效，检查是否按的是**右侧** Ctrl（左侧不触发）
- 若 gamma 被驱动拒绝（个别 HDR 环境窗口会显示红字提示），50%–100%
  区间会失效，可改用遮罩方案或关闭 HDR
