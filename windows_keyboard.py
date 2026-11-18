"""Physical Windows keyboard capture, before IME/SDL translation.

The hook thread only queues events. USB/serial writes belong to the main loop.
"""
import ctypes
from ctypes import wintypes
import queue
import sys
import threading


# PC set-1 scan codes -> USB HID keyboard usages (not layout-dependent VKs).
SCAN_CODES = {
    0x01: 41, 0x0C: 45, 0x0D: 46, 0x0E: 42, 0x0F: 43,
    0x1A: 47, 0x1B: 48, 0x1C: 40, 0x1D: 224,
    0x27: 51, 0x28: 52, 0x29: 53, 0x2A: 225, 0x2B: 49,
    0x33: 54, 0x34: 55, 0x35: 56, 0x36: 229, 0x37: 85,
    0x38: 226, 0x39: 44, 0x3A: 57, 0x45: 83, 0x46: 71,
    0x47: 95, 0x48: 96, 0x49: 97, 0x4A: 86, 0x4B: 92,
    0x4C: 93, 0x4D: 94, 0x4E: 87, 0x4F: 89, 0x50: 90,
    0x51: 91, 0x52: 98, 0x53: 99, 0x56: 100, 0x57: 68, 0x58: 69,
    0x70: 136, 0x73: 135, 0x79: 138, 0x7B: 139, 0x7D: 137,
}
SCAN_CODES.update(zip(range(0x02, 0x0C), range(30, 40)))
SCAN_CODES.update(zip(range(0x10, 0x1A), (20, 26, 8, 21, 23, 28, 24, 12, 18, 19)))
SCAN_CODES.update(zip(range(0x1E, 0x27), (4, 22, 7, 9, 10, 11, 13, 14, 15)))
SCAN_CODES.update(zip(range(0x2C, 0x33), (29, 27, 6, 25, 5, 17, 16)))
SCAN_CODES.update(zip(range(0x3B, 0x45), range(58, 68)))
SCAN_CODES.update(zip(range(0x64, 0x6F), range(104, 115)))  # F13..F23
SCAN_CODES[0x76] = 115  # F24
EXTENDED_CODES = {
    0x1C: 88, 0x1D: 228, 0x35: 84, 0x37: 70, 0x38: 230,
    0x47: 74, 0x48: 82, 0x49: 75, 0x4B: 80, 0x4D: 79,
    0x4F: 77, 0x50: 81, 0x51: 78, 0x52: 73, 0x53: 76,
    0x5B: 227, 0x5C: 231, 0x5D: 101,
}


def hid_usage(scan, extended=False, vk=0):
    # Pause's E1 sequence and PrintScreen vary between Windows keyboard drivers.
    if vk == 0x13:
        return 72
    if vk == 0x2C:
        return 70
    return (EXTENDED_CODES if extended else SCAN_CODES).get(scan)


class KeyboardPolicy:
    """Hook routing, also testable without installing a global Windows hook."""
    def __init__(self):
        self.enabled = False
        self.events = queue.SimpleQueue()
        self.pressed = set()
        self.swallowed = set()
        self.toggle_keys = set()

    def feed(self, scan, extended, vk, down, focused):
        usage = hid_usage(scan, extended, vk)
        token = (scan, extended, vk if not scan else 0)
        was_swallowed = token in self.swallowed
        was_toggle = token in self.toggle_keys
        if not down:
            self.swallowed.discard(token)
            self.toggle_keys.discard(token)
        if not focused:
            self.pressed.clear()
            if self.enabled:
                self.enabled = False
                self.events.put(("focus_lost",))
            # Complete any suppressed press even after focus changes.
            return was_swallowed

        repeated = usage in self.pressed
        if down and usage is not None:
            self.pressed.add(usage)
        elif not down:
            self.pressed.discard(usage)

        if was_toggle:
            return True
        if (down and not repeated and usage == 20
                and self.pressed & {224, 228} and self.pressed & {225, 229}
                and self.pressed & {226, 230}):
            self.enabled = not self.enabled
            self.events.put(("toggle", self.enabled))
            self.swallowed.add(token)
            self.toggle_keys.add(token)
            return True
        if self.enabled and (down or was_swallowed):
            if usage is not None:
                self.events.put(("key", usage, down))
            if down:
                self.swallowed.add(token)
            return True
        # Let an uncaptured modifier's release reach Windows after recapture.
        return was_swallowed

    def drain(self):
        while True:
            try:
                yield self.events.get_nowait()
            except queue.Empty:
                return


class WindowsKeyboard:
    def __init__(self, hwnd):
        if sys.platform != "win32":
            raise RuntimeError("Windows keyboard capture requires Windows")
        self.hwnd = hwnd
        self.policy = KeyboardPolicy()
        self._ready = threading.Event()
        self._error = None
        self._thread = threading.Thread(target=self._run, name="keyboard-hook", daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("Windows 键盘钩子启动超时")
        if self._error:
            raise self._error

    def set_enabled(self, enabled, reset=False):
        self.policy.enabled = enabled
        if reset:
            self.policy.pressed.clear()

    def drain(self):
        return self.policy.drain()

    def _run(self):
        try:
            self._run_hook()
        except Exception as exc:
            self._error = exc
            self.policy.events.put(("error", exc))
            self._ready.set()

    def _run_hook(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        result_type = ctypes.c_ssize_t
        hook_proc = ctypes.WINFUNCTYPE(result_type, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

        class KeyData(ctypes.Structure):
            _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.c_size_t)]

        user32.SetWindowsHookExW.argtypes = (ctypes.c_int, hook_proc, wintypes.HINSTANCE, wintypes.DWORD)
        user32.SetWindowsHookExW.restype = wintypes.HANDLE
        user32.CallNextHookEx.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user32.CallNextHookEx.restype = result_type
        user32.UnhookWindowsHookEx.argtypes = (wintypes.HANDLE,)
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
        user32.GetMessageW.restype = ctypes.c_int
        user32.PeekMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT)
        user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
        user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
        user32.DispatchMessageW.restype = result_type
        user32.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user32.PostThreadMessageW.restype = wintypes.BOOL
        kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self._user32 = user32
        self._thread_id = kernel32.GetCurrentThreadId()

        @hook_proc
        def callback(code, message, address):
            try:
                if code == 0 and message in (0x100, 0x101, 0x104, 0x105):
                    key = ctypes.cast(address, ctypes.POINTER(KeyData)).contents
                    if self.policy.feed(key.scanCode, bool(key.flags & 1), key.vkCode,
                                        message in (0x100, 0x104),
                                        bool(self.hwnd) and user32.GetForegroundWindow() == self.hwnd):
                        return 1
            except Exception as exc:
                self.policy.enabled = False
                self.policy.events.put(("error", exc))
            return user32.CallNextHookEx(None, code, message, address)

        handle = None
        try:
            # Create the thread message queue before exposing it to close().
            msg = wintypes.MSG()
            user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
            handle = user32.SetWindowsHookExW(13, callback, kernel32.GetModuleHandleW(None), 0)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            self._ready.set()
            while True:
                status = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if status == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                if status == 0:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:
            self._error = exc
            self.policy.events.put(("error", exc))
        finally:
            self.policy.enabled = False
            if handle:
                user32.UnhookWindowsHookEx(handle)
            self._ready.set()

    def close(self):
        self.policy.enabled = False
        if self._thread.is_alive():
            if not self._user32.PostThreadMessageW(self._thread_id, 0x12, 0, 0):  # WM_QUIT
                raise ctypes.WinError(ctypes.get_last_error())
            self._thread.join(5)
            if self._thread.is_alive():
                raise RuntimeError("Windows 键盘钩子未能退出")
