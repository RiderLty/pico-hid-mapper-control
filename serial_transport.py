"""Serial adapter for the upstream firmware's MAKCU 55 AA extension frames."""
import struct
import time


def serial_ports():
    try:
        from serial.tools import list_ports
    except ImportError as exc:
        raise RuntimeError("串口模式需要 pyserial：py -m pip install pyserial") from exc
    return [(port.device, port.description) for port in list_ports.comports()]


class SerialTransport:
    def __init__(self, port, baudrate=4000000):
        try:
            import serial
        except ImportError as exc:
            raise RuntimeError("串口模式需要 pyserial：py -m pip install pyserial") from exc
        self.path = port
        self._port = serial.Serial(port, 115200, timeout=0, write_timeout=1)
        try:
            # Upstream powers on at 115200; if already at target speed this
            # switch is noise. Wait for its half-frame timeout before probing.
            self._port.write(b"\xde\xad\x05\x00\xa5" + struct.pack("<I", baudrate))
            self._port.flush()
            time.sleep(0.08)
            self._port.baudrate = baudrate
            time.sleep(0.08)
            self._port.reset_input_buffer()
            self._port.write(b"km.version()\r\n")
            self._port.flush()
            reply = bytearray()
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                reply.extend(self._port.read(self._port.in_waiting or 1))
                if b"km.MAKCU" in reply or b"UNLICENSED" in reply:
                    self.version = bytes(reply).decode("ascii", errors="replace").strip()
                    break
                time.sleep(0.01)
            else:
                raise OSError("串口未回复 MAKCU 握手；请确认端口、波特率，以及固件支持串口（当前免费版已移除）。")
        except BaseException:
            self.close()
            raise

    def write(self, data):
        if len(data) != 64 or data[:2] != b"\x55\xaa" or not 1 <= data[2] <= 61:
            raise ValueError("串口控制帧必须是有效的 64B 55 AA 帧")
        frame = data[:3 + data[2]]
        written = self._port.write(frame)
        if written != len(frame):
            raise OSError("串口写入不完整")
        return 64  # logical report size, transport padding was removed

    def read(self, size=64):
        return self._port.read(min(size, self._port.in_waiting))

    def close(self):
        self._port.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
