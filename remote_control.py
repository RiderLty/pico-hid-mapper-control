#!/usr/bin/env python3
"""Pico 本地键鼠手柄控制器，通过 WinUSB 或上游串口固件转发输入。

Windows: py -m pip install pygame
         py remote_control.py --check
         py remote_control.py
Esc 正常转发。Ctrl+Shift+Alt+Q 释放或重新捕获输入。
启动后在同一窗口连接设备、开始或释放独占。--help 查看命令行选项。
"""
import argparse
import struct
import sys


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--transport", choices=("winusb", "serial"), help="连接方式，默认使用上次设置或 WinUSB")
    parser.add_argument("--device-index", type=int, help="WinUSB 设备索引，默认使用上次设置或 0")
    parser.add_argument("--list", action="store_true", help="列出设备，不捕获输入")
    parser.add_argument("--check", action="store_true", help="检查连接，不捕获或注入输入")
    parser.add_argument("--port", help="串口名称，例如 COM3")
    parser.add_argument("--baud", type=int, help="串口工作波特率，默认使用上次设置或 4000000")
    parser.add_argument("--deadzone", type=float, default=0.1, help="手柄摇杆死区，默认 0.1")
    args = parser.parse_args(argv)
    if not 0 <= args.deadzone < 1:
        parser.error("--deadzone 必须在 [0, 1) 范围内")
    if args.baud is not None and not 9600 <= args.baud <= 6000000:
        parser.error("--baud 必须在 9600..6000000 范围内")
    if args.device_index is not None and args.device_index < 0:
        parser.error("--device-index 不能为负数")
    try:
        if not args.list and not args.check:
            from control_app import run_app
            return run_app(args)
        args.transport = args.transport or "winusb"
        args.baud = args.baud or 4000000
        if args.transport == "winusb":
            from winusb_transport import WinUsbTransport, device_paths
            if args.list:
                paths = device_paths()
                for index, path in enumerate(paths):
                    print(f"[{index}] {path}")
                if not paths:
                    print("未找到 Pico WinUSB 设备")
                    return 1
                return 0
            transport = WinUsbTransport(args.device_index or 0)
        else:
            from serial_transport import SerialTransport, serial_ports
            if args.list:
                for name, description in serial_ports():
                    print(f"{name}: {description}")
                return 0
            if not args.port:
                parser.error("串口模式请指定 --port COM3，或在窗口内选择串口")
            transport = SerialTransport(args.port, args.baud or 4000000)
        with transport:
            if args.transport == "winusb":
                revision = transport.check_connection()
                print(f"WinUSB 已连接：2E8A:C9D1 REV_{revision:04X}，bulk OUT=0x02 / IN=0x83")
            else:
                print(f"串口已连接：{args.port} @ {args.baud}，{transport.version}")
            if args.check:
                print("连接检查通过；未捕获或注入输入。")
                return 0
        return 0
    except (OSError, ValueError, RuntimeError, struct.error) as exc:
        print(f"连接或运行失败：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
