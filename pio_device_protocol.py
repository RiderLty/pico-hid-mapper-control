"""
PIO Device 上位机控制协议（平台无关）。

通过一个实现了 write(data: bytes) -> int 和 read(size: int) -> bytes 方法的对象构造，
内部直接读写 HID 报告，不依赖具体的 hidapi / pyusb / hidraw 库。

上位机 → Pico（OUT 端点）：[0x55][0xAA][LEN][CMD][PAYLOAD ...]  payload 长度 = LEN-1
Pico → 上位机（IN 端点）：[0x55][0xAA][LEN][EVT][DATA ...]

业务方法（each one → one CMD）：
    touch / mouse_report / keyboard_report
    mouse_move / mouse_button / keyboard_key / set_orientation
    vmouse / gamepad_report

事件系统：
    on(evt) 注册 handler / poll() 轮询并自动分发

report_id_pad 在构造时指定（Windows: b'\\x00', 其它: b''），后续方法无需传。
"""
from __future__ import annotations

import struct


class PioDeviceProtocol:
    """PIO device 上位机控制协议。用 hid.Device 实例（或任何有 write/read 方法的对象）构造。"""

    H0, H1 = 0x55, 0xAA
    REPORT_SIZE = 64

    # -- OUT CMD --
    CMD_TOUCH      = 0xFF
    CMD_MOUSE      = 0xFE
    CMD_KEYBOARD   = 0xFD
    CMD_CORE_INPUT = 0xFC
    CMD_VMOUSE     = 0xFB
    CMD_GAMEPAD    = 0xF9
    CMD_DS5        = 0xF8   # DS5 裸报文直通：payload = dualsense_input_report body[0..39]，全零=断开

    # -- 触屏 action --
    ACT_DOWN, ACT_MOVE, ACT_UP = 1, 2, 3

    # -- IN EVT --
    EVT_ORIENTATION = 0x01

    def __init__(self, dev, report_id_pad: bytes = b'\x00'):
        self._dev = dev
        self._pad = report_id_pad
        self._handlers: dict[int, list] = {}

    @property
    def device(self):
        """底层设备对象，用于 nonblocking / close 等原生操作"""
        return self._dev

    # ---- 事件 ----
    def on(self, evt: int, callback=None):
        """注册事件处理器。可作装饰器或直接传 callback。
        handler 签名: callback(evt: int, data: bytes)"""
        if callback is not None:
            self._handlers.setdefault(evt, []).append(callback)
            return callback
        return lambda cb: self.on(evt, cb)

    def _dispatch(self, evt: int, data: bytes):
        for cb in self._handlers.get(evt, []):
            cb(evt, data)

    # ---- 命令帧发送 ----
    def send_frame(self, cmd: int, payload: bytes) -> int:
        """构造 [55 AA][LEN][CMD][PAYLOAD] → 对齐 64B → 发送"""
        if len(payload) > self.REPORT_SIZE - 4:
            raise ValueError("控制帧 payload 不能超过 60 字节")
        frame = bytes([self.H0, self.H1, 1 + len(payload), cmd]) + payload
        raw = frame.ljust(self.REPORT_SIZE, b'\x00')
        return self._dev.write(self._pad + raw)

    def poll(self) -> int:
        """读取 IN 端点，解析 [55 AA][LEN][EVT][DATA] 并分发 handler。
        返回收到的帧数。无数据时返回 0。"""
        data = self._dev.read(self.REPORT_SIZE + 1)    # +1：Windows Report ID 占位
        if not data:
            return 0
        count = 0
        i = 0
        while i < len(data) - 3:
            if data[i] == self.H0 and data[i + 1] == self.H1:
                L = data[i + 2]
                if L >= 1 and i + 3 + L <= len(data):
                    self._dispatch(data[i + 3], data[i + 4:i + 4 + (L - 1)])
                    count += 1
                    i += 3 + L
                    continue
            i += 1
        return count

    # ---- 业务方法 ----
    def touch(self, cid: int, action: int, x: int, y: int) -> int:
        """CMD 0xFF 直接触屏输出"""
        payload = struct.pack("<BBii", action, cid, x, y)
        return self.send_frame(self.CMD_TOUCH, payload)

    def mouse_report(self, buttons: int, dx: int, dy: int, wheel: int = 0) -> int:
        """CMD 0xFE 鼠标报文（mouse_report_x8, 8B）"""
        payload = struct.pack("<BBhhbB", 0, buttons, dx, dy, wheel, 0)
        return self.send_frame(self.CMD_MOUSE, payload)

    def keyboard_report(self, modifiers: int, keys: bytes | list) -> int:
        """CMD 0xFD 键盘报文（keyboard_report_x8, 8B）"""
        k = bytes(keys)[:6].ljust(6, b'\x00')
        payload = struct.pack("<BB", modifiers, 0) + k
        return self.send_frame(self.CMD_KEYBOARD, payload)

    def mouse_move(self, dx: int, dy: int, wheel: int = 0) -> int:
        """0xFC subcmd 0xFF → core_input_mouse_move"""
        payload = b'\xff' + struct.pack("<iii", dx, dy, wheel)
        return self.send_frame(self.CMD_CORE_INPUT, payload)

    def mouse_button(self, button: int, down: bool) -> int:
        """0xFC subcmd 0xFE → core_input_mouse_button"""
        payload = bytes([0xFE, button, 1 if down else 0])
        return self.send_frame(self.CMD_CORE_INPUT, payload)

    def keyboard_key(self, keycode: int, down: bool) -> int:
        """0xFC subcmd 0xFC → core_input_keyboard"""
        payload = bytes([0xFC, keycode, 1 if down else 0])
        return self.send_frame(self.CMD_CORE_INPUT, payload)

    def set_orientation(self, o: int) -> int:
        """0xFC subcmd 0xFB → core_input_orientation"""
        payload = bytes([0xFB, o])
        return self.send_frame(self.CMD_CORE_INPUT, payload)

    def vmouse(self, x: int, y: int, show: bool, down: bool) -> int:
        """CMD 0xFB vmouse 状态输出 → 转发网络"""
        state = (1 if show else 0) | (2 if down else 0)
        payload = struct.pack("<iiB", x, y, state)
        return self.send_frame(self.CMD_VMOUSE, payload)

    def gamepad_report(self, ls_x: int, ls_y: int, rs_x: int, rs_y: int,
                       lt: int, rt: int, buttons: int) -> int:
        """CMD 0xFC / subcmd 0xF9 手柄报文 16B → core_input_gamepad_abs/btn

        ls_x/y, rs_x/y: 摇杆 -32767~32767
        lt, rt:          扳机 0~32767
        buttons:         按钮位掩码（bit→code 映射见 pio_device.c 硬编码表）
        """
        payload = struct.pack("<hhhhhhI", ls_x, ls_y, rs_x, rs_y, lt, rt, buttons)
        return self.send_frame(self.CMD_CORE_INPUT, bytes([self.CMD_GAMEPAD]) + payload)
