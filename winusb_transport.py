"""Windows WinUSB transport for the Pico vendor interface, using only ctypes."""
import ctypes as ct
from dataclasses import dataclass
import re
import sys
import uuid

VID, PID = 0x2E8A, 0xC9D1
INTERFACE_GUID = "3F2D7A90-1B64-4B9C-AD6E-6C7C76C9D101"
BYTE, WORD, DWORD, BOOL = ct.c_ubyte, ct.c_uint16, ct.c_uint32, ct.c_int32
HANDLE = ct.c_void_p
ERROR_NO_MORE_ITEMS, ERROR_SEM_TIMEOUT = 259, 121


class GUID(ct.Structure):
    _fields_ = [("data", BYTE * 16)]

    def __init__(self, value):
        super().__init__()
        self.data[:] = uuid.UUID(value).bytes_le


class InterfaceData(ct.Structure):
    _fields_ = [("size", DWORD), ("guid", GUID), ("flags", DWORD),
                ("reserved", ct.c_size_t)]


class DeviceInfoData(ct.Structure):
    _fields_ = [("size", DWORD), ("class_guid", GUID), ("dev_inst", DWORD),
                ("reserved", ct.c_size_t)]


@dataclass(frozen=True)
class WinUsbDevice:
    path: str
    name: str = "Pico 控制器"
    location: str = ""


class UsbInterface(ct.Structure):
    _pack_ = 1
    _fields_ = [(name, BYTE) for name in
                ("length", "type", "number", "alternate", "endpoints",
                 "interface_class", "subclass", "protocol", "string")]


class PipeInfo(ct.Structure):
    _fields_ = [("type", ct.c_int32), ("address", BYTE),
                ("packet_size", WORD), ("interval", BYTE)]


class SetupPacket(ct.Structure):
    _pack_ = 1
    _fields_ = [("request_type", BYTE), ("request", BYTE), ("value", WORD),
                ("index", WORD), ("length", WORD)]


class _WindowsApi:
    def __init__(self):
        if sys.platform != "win32":
            raise OSError("WinUSB 模式需要 Windows；其他平台可使用 --transport serial 连接上游串口固件")
        setup = ct.WinDLL("setupapi", use_last_error=True)
        kernel = ct.WinDLL("kernel32", use_last_error=True)
        usb = ct.WinDLL("winusb", use_last_error=True)

        def bind(dll, name, result, *args):
            func = getattr(dll, name)
            func.restype, func.argtypes = result, list(args)
            return func

        ptr, dword_ptr = ct.c_void_p, ct.POINTER(DWORD)
        self.get_devices = bind(setup, "SetupDiGetClassDevsW", HANDLE,
                                ct.POINTER(GUID), ct.c_wchar_p, HANDLE, DWORD)
        self.enum_interface = bind(setup, "SetupDiEnumDeviceInterfaces", BOOL,
                                   HANDLE, ptr, ct.POINTER(GUID), DWORD,
                                   ct.POINTER(InterfaceData))
        self.get_detail = bind(setup, "SetupDiGetDeviceInterfaceDetailW", BOOL,
                               HANDLE, ct.POINTER(InterfaceData), ptr, DWORD,
                               dword_ptr, ptr)
        self.destroy_devices = bind(setup, "SetupDiDestroyDeviceInfoList", BOOL, HANDLE)
        self.get_property = bind(setup, "SetupDiGetDeviceRegistryPropertyW", BOOL,
                                 HANDLE, ct.POINTER(DeviceInfoData), DWORD,
                                 dword_ptr, ptr, DWORD, dword_ptr)
        self.create_file = bind(kernel, "CreateFileW", HANDLE, ct.c_wchar_p,
                                DWORD, DWORD, ptr, DWORD, DWORD, HANDLE)
        self.close_file = bind(kernel, "CloseHandle", BOOL, HANDLE)
        self.initialize = bind(usb, "WinUsb_Initialize", BOOL, HANDLE, ct.POINTER(HANDLE))
        self.free = bind(usb, "WinUsb_Free", BOOL, HANDLE)
        self.query_interface = bind(usb, "WinUsb_QueryInterfaceSettings", BOOL,
                                    HANDLE, BYTE, ct.POINTER(UsbInterface))
        self.query_pipe = bind(usb, "WinUsb_QueryPipe", BOOL, HANDLE, BYTE, BYTE,
                               ct.POINTER(PipeInfo))
        self.set_policy = bind(usb, "WinUsb_SetPipePolicy", BOOL,
                               HANDLE, BYTE, DWORD, DWORD, ptr)
        self.write_pipe = bind(usb, "WinUsb_WritePipe", BOOL,
                               HANDLE, BYTE, ptr, DWORD, dword_ptr, ptr)
        self.read_pipe = bind(usb, "WinUsb_ReadPipe", BOOL,
                              HANDLE, BYTE, ptr, DWORD, dword_ptr, ptr)
        self.reset_pipe = bind(usb, "WinUsb_ResetPipe", BOOL, HANDLE, BYTE)
        self.control = bind(usb, "WinUsb_ControlTransfer", BOOL,
                            HANDLE, SetupPacket, ptr, DWORD, dword_ptr, ptr)
        self.get_descriptor = bind(usb, "WinUsb_GetDescriptor", BOOL,
                                   HANDLE, BYTE, BYTE, WORD, ptr, DWORD, dword_ptr)

    @staticmethod
    def check(ok):
        if not ok:
            raise ct.WinError(ct.get_last_error())

    def property_text(self, devices, device, property_id):
        buffer = ct.create_unicode_buffer(1024)
        if self.get_property(devices, ct.byref(device), property_id, None,
                             buffer, ct.sizeof(buffer), None):
            return buffer.value.strip()
        return ""

    def devices(self):
        guid = GUID(INTERFACE_GUID)
        devices = self.get_devices(ct.byref(guid), None, None, 0x12)  # PRESENT | DEVICEINTERFACE
        if devices == ct.c_void_p(-1).value:
            self.check(False)
        found = []
        try:
            index = 0
            while True:
                info = InterfaceData()
                info.size = ct.sizeof(info)
                if not self.enum_interface(devices, None, ct.byref(guid), index, ct.byref(info)):
                    if ct.get_last_error() == ERROR_NO_MORE_ITEMS:
                        break
                    self.check(False)
                size = DWORD()
                self.get_detail(devices, ct.byref(info), None, 0, ct.byref(size), None)
                if size.value < 6:
                    self.check(False)
                detail = ct.create_string_buffer(size.value)
                # The structure's cbSize is 8 on Win64, 6 on Win32. The UTF-16
                # DevicePath starts at byte 4 on both (it is an inline WCHAR[]).
                DWORD.from_buffer(detail).value = 8 if ct.sizeof(HANDLE) == 8 else 6
                device = DeviceInfoData()
                device.size = ct.sizeof(device)
                self.check(self.get_detail(devices, ct.byref(info), detail, size,
                                           ct.byref(size), ct.byref(device)))
                path = ct.wstring_at(ct.addressof(detail) + 4)
                if "vid_2e8a&pid_c9d1&mi_01" in path.lower():
                    name = (self.property_text(devices, device, 0xC)
                            or self.property_text(devices, device, 0))
                    if not name or name.lower() in ("usb device", "usb设备", "usb 设备"):
                        name = "Pico 控制器"
                    location = self.property_text(devices, device, 0xD)
                    port = re.fullmatch(r"Port_#(\d+)\.Hub_#(\d+)", location, re.IGNORECASE)
                    if port:
                        location = f"USB 端口 {int(port[2])}-{int(port[1])}"
                    elif not location or len(location) > 50:
                        location = ""
                    found.append(WinUsbDevice(path, name, location))
                index += 1
        finally:
            self.destroy_devices(devices)
        return sorted(found, key=lambda device: device.path.casefold())

    def paths(self):
        return [device.path for device in self.devices()]


def device_paths():
    return _WindowsApi().paths()


def winusb_devices():
    """List present Pico interfaces without opening or claiming the devices."""
    return _WindowsApi().devices()


class WinUsbTransport:
    """64-byte write/read interface accepted by PioDeviceProtocol (no HID ID)."""
    def __init__(self, device_index=0, timeout_ms=1000, *, device_path=None):
        self._api = _WindowsApi()
        self._file, self._handle = None, HANDLE()
        self._pending_input = b""
        self._read_recovery_available = True
        paths = self._api.paths()
        if not paths:
            raise OSError("未找到 Pico WinUSB 接口。请连接 PIO 口，确认固件 REV_0202 或更新。")
        if device_path is not None:
            matches = [path for path in paths if path.casefold() == device_path.casefold()]
            if not matches:
                raise OSError("所选设备已断开")
            self.path = matches[0]
        else:
            if device_index < 0 or device_index >= len(paths):
                raise ValueError(f"设备索引 {device_index} 无效，当前有 {len(paths)} 个设备")
            self.path = paths[device_index]
        self._file = self._api.create_file(self.path, 0xC0000000, 3, None, 3,
                                           0x40000000, None)  # OVERLAPPED
        if self._file == ct.c_void_p(-1).value:
            self._file = None
            self._api.check(False)
        try:
            self._api.check(self._api.initialize(self._file, ct.byref(self._handle)))
            interface = UsbInterface()
            self._api.check(self._api.query_interface(self._handle, 0, ct.byref(interface)))
            if interface.number != 1 or interface.interface_class != 0xFF:
                raise OSError("打开的接口不是 Pico vendor interface 1")
            pipes = {}
            for index in range(interface.endpoints):
                pipe = PipeInfo()
                self._api.check(self._api.query_pipe(self._handle, 0, index, ct.byref(pipe)))
                pipes[pipe.address] = (pipe.type, pipe.packet_size)
            if pipes.get(0x02) != (2, 64) or pipes.get(0x83) != (2, 64):
                raise OSError(f"WinUSB bulk 端点不匹配：{pipes}")
            for address, timeout in ((0x02, timeout_ms), (0x83, 1)):
                value = DWORD(timeout)
                self._api.check(self._api.set_policy(self._handle, address, 3,
                                                      ct.sizeof(value), ct.byref(value)))
        except BaseException:
            self.close()
            raise

    def write(self, data):
        if not self._handle:
            raise OSError("WinUSB 已关闭")
        if len(data) != 64:
            raise ValueError("Pico WinUSB 每次必须发送 64 字节，不添加 HID Report ID")
        buffer = (BYTE * 64).from_buffer_copy(data)
        sent = DWORD()
        self._api.check(self._api.write_pipe(self._handle, 0x02, buffer, 64, ct.byref(sent), None))
        if sent.value != 64:
            raise OSError(f"WinUSB 只发送了 {sent.value}/64 字节")
        return sent.value

    def read(self, size=64):
        if not self._handle:
            raise OSError("WinUSB 已关闭")
        if self._pending_input:
            data, self._pending_input = self._pending_input, b""
            return data
        try:
            return self._read_once()
        except OSError as exc:
            # Windows can retain a stalled IN pipe across a rapid unplug/open.
            # Recover once on this handle; never replay OUT input commands or
            # turn persistent failures / device removal into a successful read.
            if getattr(exc, "winerror", None) != 31 or not self._read_recovery_available:
                raise
            self._read_recovery_available = False
            self._api.check(self._api.reset_pipe(self._handle, 0x83))
            return self._read_once()

    def _read_once(self):
        buffer = (BYTE * 64)()
        received = DWORD()
        if not self._api.read_pipe(self._handle, 0x83, buffer, 64, ct.byref(received), None):
            if ct.get_last_error() == ERROR_SEM_TIMEOUT:
                return b""
            self._api.check(False)
        return bytes(buffer[:received.value])

    def check_connection(self):
        """Read identity/status and send harmless zero padding; no input commands."""
        descriptor = (BYTE * 18)()
        received = DWORD()
        self._api.check(self._api.get_descriptor(self._handle, 1, 0, 0,
                                                  descriptor, 18, ct.byref(received)))
        raw = bytes(descriptor)
        if received.value != 18 or int.from_bytes(raw[8:10], "little") != VID or int.from_bytes(raw[10:12], "little") != PID:
            raise OSError("设备描述符不匹配")
        status = (BYTE * 2)()
        setup = SetupPacket(0x80, 0, 0, 0, 2)
        self._api.check(self._api.control(self._handle, setup, status, 2, ct.byref(received), None))
        if received.value != 2:
            raise OSError("GET_STATUS 长度错误")
        self.write(bytes(64))
        # Validate both directions before publishing a connected session. Keep
        # any real device event for the protocol reader rather than discarding it.
        self._pending_input = self.read()
        return int.from_bytes(raw[12:14], "little")

    def close(self):
        if self._handle:
            self._api.free(self._handle)
            self._handle = HANDLE()
        if self._file is not None:
            self._api.close_file(self._file)
            self._file = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
