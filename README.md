# pico-hid-mapper 控制端（Windows）

Pico HID Mapper 的 Windows 本地控制端：在本地窗口捕获键盘、鼠标和 SDL 支持的手柄，
通过 WinUSB 或串口向 Pico 发送同一套 `55 AA` 控制帧。Esc 会正常发送给 Pico。

本仓库是从 [pico-hid-mapper](https://github.com/RiderLty/pico-hid-mapper) 主仓库的
`pyctrl/` 目录抽出并独立的版本，方便单独维护、打包 Windows 单文件程序。
固件、Lua 脚本、Web 控制台等仍在主仓库。

## 快速开始

需要 Windows 10/11 和 Python 3.14（打包环境同理）。

```powershell
py -m pip install -r requirements.txt
py remote_control.py
```

启动后只有一个控制窗口，连接、断开和独占键鼠都在窗口内完成。
安装依赖后双击 `start.pyw` 或 `start.bat` 启动，不额外打开控制台。
命令行启动可查看详细错误。

## 打包单文件 EXE

双击 `build.bat`。首次会创建独立的 `.venv-build` 打包环境并安装依赖，
完成后生成 `dist/pico-hid-mapper-control.exe`，可单独复制到其他 Windows 电脑运行，
无需安装 Python。`icon.png` 同时用于窗口图标和 EXE 图标，替换后重新打包即可。

实际执行的命令：

```powershell
pyinstaller --noconfirm --clean --onefile --windowed ^
    --name pico-hid-mapper-control ^
    --paths "." ^
    --hidden-import pygame._sdl2.controller ^
    --hidden-import serial.tools.list_ports ^
    --icon "icon.png" --add-data "icon.png;." ^
    --distpath "dist" --workpath "build" --specpath "build" ^
    "start.pyw"
```

## 功能

- **自动连接**每次启动默认开启：首次尝试 WinUSB，此后记住上次选择的方式、串口和波特率，
  即使设备尚未插入也会保存设置，之后插入所选串口设备即可自动连接。
  未找到设备或设备拔出时每 0.5 秒重试（单次连接耗时另计）；手动断开会关闭本次运行的自动连接，
  可在窗口右上角重新开启。连接成功后自动独占键鼠；窗口在后台时，切回窗口才开始独占。
- **连接方式二选一**：上方选择 WinUSB 或串口，自动连接只检测当前选中的方式，
  不会在两种方式之间自动切换。连接后先断开，才能切换方式或选择其他设备。
- **WinUSB**：列表显示当前识别到的设备名称、USB 位置和状态；点击一行选择设备，
  下方点击“连接设备”。列表每 0.5 秒自动更新，也可点击刷新；设备较多时滚轮浏览。
  记住的是所选设备的接口路径，插拔导致列表重排时不会连到另一台设备。
  所选设备拔出后保留为“已断开”，等待原设备重新接入，或手动选择列表中另一台。
  连接开发板的 **PIO USB 口**，使用 REV_0202 或更新固件，Windows 自动绑定 WinUSB，
  无需 Zadig/libusb。命令行 `--device-index` 仍可用于首次预选设备。
- **串口**：在串口页选择端口及波特率，再点击“连接设备”。端口列表可刷新，也可以直接输入。
- **开始独占键鼠**：手动释放后点击，可再次隐藏鼠标并转发键鼠和手柄。
  断开后可在原窗口重新连接或切换通信方式。

- **Ctrl+Shift+Alt+Q**：释放或重新捕获输入；该组合的 Q 不转发。
- **Esc、F1**：作为普通按键转发。
- Windows 使用原生低级键盘钩子，按物理扫描码转发，支持中文输入法开启时
  捕获按键、左右 **Win** 键及其组合；捕获时这些按键不触发本机输入法或开始菜单。
  目标电脑上的文字输入由目标电脑自己的键盘布局和输入法处理。
- 窗口失焦自动停止捕获，并发送已按下键、鼠标按钮和手柄的释放状态。
- 键盘钩子只在此窗口位于前台且开启捕获时拦截输入；释放后正常操作本机。
  Windows 保留的安全快捷键（例如 Ctrl+Alt+Delete）无法通过此方式接管。
- 界面只显示检测中、已连接、已断开等简短状态；关闭窗口释放输入并断开设备。
- 手柄采用 SDL GameController 标准映射，首个受支持手柄生效；未被 SDL
  识别的通用摇杆需要先配置 SDL 控制器映射。

## 命令行

预填连接设置或检查设备：

```powershell
py remote_control.py --list
py remote_control.py --check
py remote_control.py --transport winusb
py remote_control.py --transport serial --list
py remote_control.py --transport serial --port COM3 --baud 4000000
```

`--check` 不打开捕获窗口、不注入键鼠；WinUSB 检查设备描述符、GET_STATUS
及 64B 全零数据写入。串口检查 MAKCU 版本握手。
其余连接参数预填单窗口里的设置；自动连接成功后会开始独占。

## 配置文件

连接设置自动保存在程序同目录的 `pico.config.ini`。EXE 运行时放在 EXE 旁，
脚本运行时放在仓库根目录；从快捷方式或其他工作目录启动也不会改变位置。
旧版 `settings.json` 在首次启动时自动迁移。切换连接方式、修改串口设置后会保存，
无需先连接成功。此文件不保存按键记录，例如：

```ini
[connection]
transport = serial
port = COM3
baud = 4000000
device_index = 0
device_path =
```

`transport` 可填写 `winusb` 或 `serial`。`device_path` 由选择 WinUSB 设备时自动保存。
详细错误写入同目录的 `controller.log`，日志自动轮转，重复的检测错误仅记录一次。

## 串口与固件

**串口需要上游 `pico-hid-mapper` 的 UART/MAKCU 固件。当前免费版已删除串口
接收后端，因此同一块免费版固件请选 WinUSB。** 串口经 `DE AD` 握手从默认
115200 切换到所选工作速率，再验证 `km.version()`；发送时去掉 64B 的尾部
零填充。若设备当前处于其他非默认工作速率，选择那个速率或重启设备后连接。

WinUSB 直接通过 Python 标准库 ctypes 调用 Windows 系统 DLL。串口使用
pyserial。命令帧统一复用 `pio_device_protocol.py`（本仓库已内置，原来位于主仓库
`pytester/pio_device_protocol.py`），与主仓库保持同一套 `55 AA` 帧格式。

## 测试

协议、串口分帧和本地捕获的回归测试（只模拟输入，不操作真实键鼠），需要 Windows：

```powershell
py -m unittest discover -s tests -p test_pyctrl.py -v
```

## 目录结构

| 文件 | 说明 |
| --- | --- |
| `control_app.py` | 单窗口界面：连接、设置、捕获循环 |
| `control_core.py` | 键鼠/手柄到 `55 AA` 帧的转换与发送 |
| `winusb_transport.py` | WinUSB（ctypes）传输与设备枚举 |
| `serial_transport.py` | 串口传输与 `DE AD` 握手 |
| `windows_keyboard.py` | 低级键盘钩子与扫描码映射 |
| `remote_control.py` | 命令行入口与参数 |
| `pio_device_protocol.py` | 与固件共用的 64B 控制帧协议 |
| `app_info.py` | 应用名与图标路径 |
| `start.pyw` / `start.bat` | 无控制台启动 |
| `build.bat` | PyInstaller 单文件打包 |

## 许可

GPL-3.0，见 [LICENSE](LICENSE)。与主仓库 `pico-hid-mapper` 一致。
