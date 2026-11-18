"""Protocol, serial framing and local capture regressions, without real input."""
import os
from concurrent.futures import Future
from dataclasses import asdict
import json
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from control_core import ControlSender, controller_report
from serial_transport import SerialTransport
from windows_keyboard import KeyboardPolicy, hid_usage
from control_app import ConnectionSettings, RemoteControlApp, load_settings, open_device, settings_path
from winusb_transport import WinUsbDevice, WinUsbTransport


class FakeTransport:
    def __init__(self):
        self.frames = []
        self.closed = False

    def write(self, data):
        self.frames.append(bytes(data))
        return len(data)

    def read(self, size):
        return b""

    def close(self):
        self.closed = True


def decoded(frame):
    assert len(frame) == 64 and frame[:2] == b"\x55\xaa"
    return frame[3:3 + frame[2]]


class PyctrlTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        self.sender = ControlSender(self.transport)

    def test_escape_modifiers_mouse_and_release(self):
        self.sender.sendKey(41, True)  # Esc must reach firmware
        self.sender.sendKey(41, True)  # suppress repeat
        self.sender.sendKey(231, True)  # right GUI
        self.sender.sendMouseMove(-300, 700)
        self.sender.sendMouseBTN(3, True)  # pygame right -> firmware 1
        self.sender.sendWheel(-2)
        self.sender.close()
        frames = list(map(decoded, self.transport.frames))
        self.assertEqual(frames[:5], [b"\xfc\xfc\x29\x01", b"\xfc\xfc\xe7\x01",
                                      b"\xfc\xff" + struct.pack("<iii", -300, 700, 0),
                                      b"\xfc\xfe\x01\x01",
                                      b"\xfc\xff" + struct.pack("<iii", 0, 0, -2)])
        self.assertIn(b"\xfc\xfc\x29\x00", frames)
        self.assertIn(b"\xfc\xfc\xe7\x00", frames)
        self.assertIn(b"\xfc\xfe\x01\x00", frames)
        self.assertTrue(self.transport.closed)

    def test_gamepad_uses_firmware_subcommand_and_sdl_mapping(self):
        buttons = [False] * 15
        buttons[0] = buttons[11] = True  # A and d-pad up
        report = controller_report([12000, -32000, 20, 0, 20000, -20], buttons)
        self.assertEqual(report, (12000, -32000, 0, 0, 20000, 0,
                                  (1 << 12) | 1 | (1 << 16)))
        self.sender.send_gamepad(report)
        self.sender.send_gamepad(report)
        self.sender.release_all()
        frames = list(map(decoded, self.transport.frames))
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0], b"\xfc\xf9" + struct.pack("<hhhhhhI", *report))
        self.assertEqual(frames[1], b"\xfc\xf9" + bytes(16))

    def test_serial_strips_padding_and_rejects_bad_frames(self):
        serial = SerialTransport.__new__(SerialTransport)
        serial._port = FakeTransport()
        self.sender.sendKey(41, True)
        frame = self.transport.frames[0]
        self.assertEqual(serial.write(frame), 64)
        self.assertEqual(serial._port.frames, [b"\x55\xaa\x04\xfc\xfc\x29\x01"])
        for bad in (bytes(64), frame[:20], b"\x55\xaa\xff" + bytes(61)):
            with self.assertRaises(ValueError):
                serial.write(bad)

    def test_oversize_protocol_payload_is_rejected(self):
        with self.assertRaises(ValueError):
            self.sender.protocol.send_frame(0xFC, bytes(61))
        self.assertFalse(self.transport.frames)

    def test_serial_boot_baud_switch_and_version_probe(self):
        class Port(FakeTransport):
            def __init__(self, port, baud, **kwargs):
                super().__init__()
                self.baudrate = baud
                self.initial_baud = baud
                self.in_waiting = 20

            def flush(self):
                pass

            def reset_input_buffer(self):
                pass

            def read(self, size):
                return b"km.MAKCU_V1.0\r\n"

        with patch.dict(sys.modules, {"serial": SimpleNamespace(Serial=Port)}), \
                patch("serial_transport.time.sleep"):
            with SerialTransport("COM3", 4000000) as transport:
                port = transport._port
                self.assertEqual(port.initial_baud, 115200)
                self.assertEqual(port.baudrate, 4000000)
                self.assertEqual(port.frames[0], b"\xde\xad\x05\x00\xa5" + struct.pack("<I", 4000000))
                self.assertEqual(port.frames[1], b"km.version()\r\n")
                self.assertIn("MAKCU", transport.version)
            self.assertTrue(port.closed)

    def test_windows_physical_scan_codes_ignore_ime_virtual_key(self):
        for vk in (0x41, 0xE5, 0):  # A, VK_PROCESSKEY (IME), missing VK
            self.assertEqual(hid_usage(0x1E, False, vk), 4)
        for scan, extended, vk, usage in (
                (0x01, False, 0x1B, 41), (0x5B, True, 0x5B, 227),
                (0x5C, True, 0x5C, 231), (0x1D, False, 0x11, 224),
                (0x1D, True, 0x11, 228), (0x38, True, 0x12, 230),
                (0x1C, False, 0x0D, 40), (0x1C, True, 0x0D, 88),
                (0x48, False, 0x68, 96), (0x48, True, 0x26, 82),
                (0x45, False, 0x13, 72), (0x37, True, 0x2C, 70)):
            self.assertEqual(hid_usage(scan, extended, vk), usage)
        self.assertIsNone(hid_usage(0x7F))

    def test_windows_ime_and_win_keys_are_forwarded_and_suppressed(self):
        policy = KeyboardPolicy()
        policy.enabled = True
        for scan, extended, vk in ((0x1E, False, 0xE5),
                                   (0x5B, True, 0x5B), (0x5C, True, 0x5C)):
            for down in (True, False):
                self.assertTrue(policy.feed(scan, extended, vk, down, True))
        self.assertEqual(list(policy.drain()), [
            ("key", usage, down) for usage in (4, 227, 231) for down in (True, False)])
        for scan, extended, vk, down in ((0x5B, True, 0x5B, True),
                                         (0x31, False, 0x4E, True),
                                         (0x31, False, 0x4E, False),
                                         (0x5B, True, 0x5B, False)):  # Win+N
            self.assertTrue(policy.feed(scan, extended, vk, down, True))
        for _, usage, down in policy.drain():
            self.sender.sendKey(usage, down)
        self.assertEqual(list(map(decoded, self.transport.frames)), [
            b"\xfc\xfc\xe3\x01", b"\xfc\xfc\x11\x01",
            b"\xfc\xfc\x11\x00", b"\xfc\xfc\xe3\x00"])

    def test_windows_focus_loss_releases_and_other_windows_pass_through(self):
        policy = KeyboardPolicy()
        policy.enabled = True
        self.assertTrue(policy.feed(0x5B, True, 0x5B, True, True))
        self.assertFalse(policy.feed(0x1E, False, 0x41, True, False))
        self.assertTrue(policy.feed(0x5B, True, 0x5B, False, False))
        self.assertEqual(list(policy.drain()), [("key", 227, True), ("focus_lost",)])
        self.assertFalse(policy.enabled)
        self.assertFalse(policy.pressed)

    def test_windows_release_chord_repeat_and_recapture_modifiers(self):
        policy = KeyboardPolicy()
        policy.enabled = True
        policy.feed(0x1D, False, 0x11, True, True)
        policy.feed(0x2A, False, 0x10, True, True)
        policy.feed(0x38, False, 0x12, True, True)
        self.assertTrue(policy.feed(0x10, False, 0xE5, True, True))
        policy.feed(0x10, False, 0xE5, True, True)  # autorepeat cannot retoggle
        policy.feed(0x10, False, 0xE5, False, True)
        policy.feed(0x1D, False, 0x11, False, True)
        policy.feed(0x2A, False, 0x10, False, True)
        policy.feed(0x38, False, 0x12, False, True)
        self.assertEqual(list(policy.drain()), [
            ("key", 224, True), ("key", 225, True), ("key", 226, True), ("toggle", False)])
        # Recapture's modifiers initially reached the local OS: let their ups
        # reach it too, preventing locally stuck modifiers after recapture.
        self.assertFalse(policy.feed(0x1D, False, 0x11, True, True))
        self.assertFalse(policy.feed(0x2A, False, 0x10, True, True))
        self.assertFalse(policy.feed(0x38, False, 0x12, True, True))
        self.assertTrue(policy.feed(0x10, False, 0x51, True, True))
        self.assertTrue(policy.feed(0x10, False, 0x51, True, True))
        self.assertTrue(policy.feed(0x10, False, 0x51, False, True))
        self.assertFalse(policy.feed(0x1D, False, 0x11, False, True))
        self.assertFalse(policy.feed(0x2A, False, 0x10, False, True))
        self.assertFalse(policy.feed(0x38, False, 0x12, False, True))
        self.assertEqual(list(policy.drain()), [("toggle", True)])

    def test_windows_toggle_requires_all_three_modifiers_and_accepts_right_side(self):
        for modifiers in (((0x1D, False), (0x38, False)),
                          ((0x1D, False), (0x2A, False)),
                          ((0x2A, False), (0x38, False))):
            with self.subTest(modifiers=modifiers):
                policy = KeyboardPolicy()
                policy.enabled = True
                for scan, extended in modifiers:
                    policy.feed(scan, extended, 0, True, True)
                policy.feed(0x10, False, 0x51, True, True)
                self.assertTrue(policy.enabled)
                self.assertIn(("key", 20, True), list(policy.drain()))
        policy = KeyboardPolicy()
        policy.enabled = True
        for scan, extended in ((0x1D, True), (0x36, False), (0x38, True)):
            policy.feed(scan, extended, 0, True, True)
        # The former Ctrl+Alt+F12 shortcut now forwards F12 normally.
        policy.feed(0x58, False, 0x7B, True, True)
        policy.feed(0x58, False, 0x7B, False, True)
        policy.feed(0x10, False, 0x51, True, True)
        events = list(policy.drain())
        self.assertIn(("key", 69, True), events)
        self.assertIn(("key", 69, False), events)
        self.assertEqual(events[-1], ("toggle", False))



class ImmediateExecutor:
    def submit(self, fn, *args):
        future = Future()
        try:
            future.set_result(fn(*args))
        except Exception as exc:
            future.set_exception(exc)
        return future

    def shutdown(self, **kwargs):
        pass


class WindowTests(unittest.TestCase):
    def setUp(self):
        os.environ["SDL_VIDEODRIVER"] = "dummy"
        os.environ["SDL_AUDIODRIVER"] = "dummy"
        os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
        try:
            import pygame
        except ImportError:
            self.skipTest("pygame is not installed")
        self.pg = pygame
        # SDL dummy drivers disagree about focus between platforms. Model
        # background focus by default; auto-capture tests opt in explicitly.
        focus = patch.object(pygame.key, 'get_focused', return_value=False)
        focus.start()
        self.addCleanup(focus.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / 'pico.config.ini'
        self.transport = FakeTransport()
        self.sender = ControlSender(self.transport)
        self.opener = Mock(return_value=(self.sender, 'WinUSB 测试设备'))
        self.usb_a = WinUsbDevice('pico-a', 'Pico 控制器', 'USB 端口 1')
        self.usb_b = WinUsbDevice('pico-b', 'Pico 控制器', 'USB 端口 2')
        self.scanner = Mock(return_value=[self.usb_a])
        self.args = SimpleNamespace(transport=None, device_index=None, port=None, baud=None, deadzone=0.1)
        self.app = None

    def tearDown(self):
        if self.app is not None:
            self.app.close()

    def make_app(self, **kwargs):
        options = dict(keyboard_factory=lambda hwnd: None, opener=self.opener,
                       executor=ImmediateExecutor(), config_path=self.config,
                       device_scanner=self.scanner, scan_executor=ImmediateExecutor())
        options.update(kwargs)
        with patch('serial_transport.serial_ports', return_value=[('COM7', '测试串口')]):
            self.app = RemoteControlApp(self.args, **options)
        self.app.poll_devices()
        self.app.poll_devices()
        return self.app

    def click(self, name):
        position = self.app.buttons[name].center
        self.app.handle_event(self.pg.event.Event(self.pg.MOUSEBUTTONDOWN, button=1, pos=position))
        self.app.handle_event(self.pg.event.Event(self.pg.MOUSEBUTTONUP, button=1, pos=position))

    def connected_app(self, **kwargs):
        app = self.make_app(**kwargs)
        app.auto_connect = False
        self.click('connect')
        app.poll_connection()
        self.assertIs(app.sender, self.sender)
        return app

    def test_one_window_connect_capture_disconnect_and_switch_to_serial(self):
        with patch.object(self.pg.display, 'set_mode', wraps=self.pg.display.set_mode) as set_mode:
            app = self.connected_app()
            screen = app.screen
            self.assertFalse(app.grabbed)
            self.assertFalse(self.transport.frames)
            self.click('capture')
            self.assertTrue(app.grabbed)
            self.assertFalse(self.transport.frames)  # activation click is never forwarded
            app.handle_event(self.pg.event.Event(self.pg.KEYDOWN, key=self.pg.K_ESCAPE, scancode=41, mod=0))
            app.handle_event(self.pg.event.Event(self.pg.KEYDOWN, key=self.pg.K_F1, scancode=58, mod=0))
            app.handle_event(self.pg.event.Event(self.pg.WINDOWFOCUSLOST))
            self.assertFalse(app.grabbed)
            self.assertFalse(self.sender.keys)
            app.handle_event(self.pg.event.Event(self.pg.KEYDOWN, key=self.pg.K_q, scancode=20,
                                               mod=self.pg.KMOD_CTRL | self.pg.KMOD_SHIFT | self.pg.KMOD_ALT))
            self.assertTrue(app.grabbed)
            app.handle_event(self.pg.event.Event(self.pg.WINDOWFOCUSLOST))
            self.click('connect')
            self.assertTrue(self.transport.closed)
            self.assertIsNone(app.sender)
            self.assertIs(app.screen, screen)
            second = FakeTransport()
            self.opener.return_value = (ControlSender(second), '串口 COM7')
            self.click('tab_serial')
            self.click('connect')
            app.poll_connection()
            self.assertEqual(self.opener.call_args.args[0].transport, 'serial')
            self.assertEqual(self.opener.call_args.args[0].port, 'COM7')
            self.assertFalse(app.grabbed)
            self.assertIs(app.screen, screen)
            self.assertEqual(set_mode.call_count, 1)
        frames = list(map(decoded, self.transport.frames))
        for usage in (41, 58):
            self.assertIn(bytes((0xFC, 0xFC, usage, 1)), frames)
            self.assertIn(bytes((0xFC, 0xFC, usage, 0)), frames)

    def test_auto_connect_retry_and_manual_disconnect_pause(self):
        current = [0]
        self.opener.side_effect = [OSError('设备未插入'), (self.sender, 'WinUSB')]
        app = self.make_app(now=lambda: current[0])
        app.poll_connection()  # default auto-connect submits the first attempt
        app.poll_connection()  # failed; schedule retry
        self.assertEqual(self.opener.call_count, 1)
        self.assertFalse(app.error)
        self.assertNotIn('设备未插入', app.message)
        current[0] = 0.49
        app.poll_connection()
        self.assertEqual(self.opener.call_count, 1)
        current[0] = 0.5
        app.poll_connection()
        app.poll_connection()
        self.assertIs(app.sender, self.sender)
        self.assertFalse(app.grabbed)
        self.assertFalse(self.transport.frames)
        self.click('connect')
        current[0] = 100
        app.poll_connection()
        self.assertEqual(self.opener.call_count, 2)
        self.assertFalse(app.auto_connect)

    def test_native_ime_win_escape_and_release_hotkey_in_same_window(self):
        class FakeKeyboard:
            def __init__(self):
                self.policy = KeyboardPolicy()
                self.closed = False

            def set_enabled(self, enabled, reset=False):
                self.policy.enabled = enabled
                if reset:
                    self.policy.pressed.clear()

            def drain(self):
                return self.policy.drain()

            def close(self):
                self.closed = True

        keyboard = FakeKeyboard()
        app = self.connected_app(keyboard_factory=lambda hwnd: keyboard)
        self.click('capture')
        for scan, extended, vk in ((0x1E, False, 0xE5), (0x5B, True, 0x5B), (0x01, False, 0x1B)):
            keyboard.policy.feed(scan, extended, vk, True, True)
        app.poll_inputs()
        # A duplicate SDL event must not produce an extra transmission.
        app.handle_event(self.pg.event.Event(self.pg.KEYDOWN, key=self.pg.K_a, scancode=4, mod=0))
        for scan in (0x1D, 0x2A, 0x38, 0x10):
            keyboard.policy.feed(scan, False, 0, True, True)
        app.poll_inputs()
        self.assertFalse(app.grabbed)
        frames = list(map(decoded, self.transport.frames))
        for usage in (4, 227, 41, 224, 225, 226):
            self.assertEqual(frames.count(bytes((0xFC, 0xFC, usage, 1))), 1)
            self.assertEqual(frames.count(bytes((0xFC, 0xFC, usage, 0))), 1)
        self.assertFalse(any(frame[:3] == b'\xfc\xfc\x14' for frame in frames))
        app.close()
        self.assertTrue(keyboard.closed)

    def test_transfer_failure_releases_local_capture_and_keeps_window(self):
        app = self.connected_app()
        self.click('capture')
        self.sender.sendKey(227, True)
        with patch.object(self.transport, 'write', side_effect=OSError('已拔出')):
            app.fail_connection(OSError('已拔出'))
        self.assertFalse(app.grabbed)
        self.assertIsNone(app.sender)
        self.assertTrue(self.transport.closed)
        self.assertTrue(app.running)
        self.assertEqual(app.message, '连接已断开')
        self.assertNotIn('已拔出', app.message)
        self.assertTrue(app.enabled('connect'))

    def test_close_during_pending_handshake_disposes_late_result(self):
        future = Future()
        future.set_running_or_notify_cancel()
        executor = Mock()
        executor.submit.return_value = future
        app = self.make_app(executor=executor)
        app.connect('winusb')
        app.close()
        future.set_result((self.sender, 'WinUSB'))
        self.assertTrue(self.transport.closed)
        self.assertFalse(self.transport.frames)

    def test_remember_successful_serial_connection_and_auto_setting(self):
        app = self.make_app()
        app.fields['port'].value = 'COM42'
        app.fields['baud'].value = '2000000'
        app.connect('serial')
        app.poll_connection()
        settings, auto = load_settings(self.config)
        self.assertEqual(settings, ConnectionSettings('serial', 0, 'COM42', 2000000, 'pico-a'))
        self.assertTrue(auto)
        self.assertFalse(app.grabbed)

    def test_failed_winusb_probe_closes_handle(self):
        transport = Mock()
        transport.check_connection.side_effect = OSError('配置描述符读取失败')
        with patch('winusb_transport.WinUsbTransport', return_value=transport):
            with self.assertRaises(OSError):
                open_device(ConnectionSettings())
        transport.close.assert_called_once()

    def test_startup_enables_auto_connect_and_connection_captures_when_focused(self):
        self.config.write_text('[connection]\ntransport = winusb\nauto_connect = false\n', encoding='utf-8')
        app = self.make_app()
        self.assertTrue(app.auto_connect)
        with patch.object(self.pg.key, 'get_focused', return_value=True):
            app.poll_connection()
            app.poll_connection()
        self.assertIs(app.sender, self.sender)
        self.assertTrue(app.grabbed)
        self.assertFalse(self.transport.frames)
        app.handle_event(self.pg.event.Event(self.pg.WINDOWFOCUSLOST))
        self.assertFalse(app.grabbed)
        with patch.object(self.pg.key, 'get_focused', return_value=True):
            app.poll_connection()
        self.assertFalse(app.grabbed)  # releasing remains effective until the user recaptures

    def test_connection_in_background_waits_for_focus_before_auto_capture(self):
        app = self.make_app()
        with patch.object(self.pg.key, 'get_focused', return_value=False):
            app.poll_connection()
            app.poll_connection()
            app.handle_event(self.pg.event.Event(self.pg.WINDOWFOCUSLOST))
        self.assertFalse(app.grabbed)
        with patch.object(self.pg.key, 'get_focused', return_value=True):
            app.poll_connection()
        self.assertTrue(app.grabbed)

    def test_winusb_list_selection_remains_on_same_device_after_reorder(self):
        self.scanner.return_value = [self.usb_a, self.usb_b]
        app = self.make_app()
        app.auto_connect = False
        rect, device, present = list(app.visible_device_rows())[1]
        app.handle_event(self.pg.event.Event(self.pg.MOUSEBUTTONUP, button=1, pos=rect.center))
        self.assertEqual(app.selected_path, 'pico-b')
        self.scanner.return_value = [self.usb_b, self.usb_a]
        self.click('refresh')
        app.poll_devices()
        self.assertEqual(app.selected_path, 'pico-b')
        self.click('connect')
        app.poll_connection()
        self.assertEqual(self.opener.call_args.args[0].device_path, 'pico-b')
        self.assertEqual(app.connected_path, 'pico-b')

    def test_missing_selected_device_does_not_connect_to_another_device(self):
        app = self.make_app()
        self.scanner.return_value = [self.usb_b]
        self.click('refresh')
        app.poll_devices()
        app.poll_connection()
        self.opener.assert_not_called()
        self.assertEqual(app.selected_path, 'pico-a')
        self.assertIsNone(app.selected_device)
        self.assertFalse(app.enabled('connect'))
        self.assertEqual(app.device_rows()[0][1], False)
        app.select_device('pico-b')
        app.poll_connection()
        app.poll_connection()
        self.assertEqual(self.opener.call_args.args[0].device_path, 'pico-b')

    def test_serial_mode_never_falls_back_to_winusb(self):
        app = self.make_app()
        self.opener.side_effect = OSError('串口未连接')
        self.click('tab_serial')
        scan_count = self.scanner.call_count
        app.poll_connection()
        app.poll_connection()
        self.assertEqual(app.settings.transport, 'serial')
        self.assertEqual(self.opener.call_args.args[0].transport, 'serial')
        self.assertEqual(self.scanner.call_count, scan_count)
        self.assertIsNone(app.sender)

    def test_device_scan_is_asynchronous_and_does_not_open_a_connection(self):
        scan_future = Future()
        scan_future.set_running_or_notify_cancel()
        scan_executor = Mock()
        scan_executor.submit.return_value = scan_future
        app = self.make_app(scan_executor=scan_executor)
        self.assertEqual(app.devices, [])
        self.assertTrue(app.enabled('tab_serial'))
        self.opener.assert_not_called()
        scan_future.set_result([self.usb_a])
        app.poll_devices()
        self.assertEqual(app.selected_device, self.usb_a)
        self.opener.assert_not_called()

    def test_selected_winusb_path_disappearing_never_falls_back_to_index(self):
        api = Mock()
        api.paths.return_value = ['pico-b']
        with patch('winusb_transport._WindowsApi', return_value=api):
            with self.assertRaises(OSError):
                WinUsbTransport(0, device_path='pico-a')
        api.create_file.assert_not_called()

    def test_winusb_opens_selected_path_instead_of_current_list_index(self):
        api = Mock()
        api.paths.return_value = ['pico-a', 'pico-b']
        api.create_file.return_value = 12345

        def initialize(file, handle):
            handle._obj.value = 99
            return True

        def interface(handle, alternate, info):
            info._obj.number = 1
            info._obj.interface_class = 0xFF
            info._obj.endpoints = 2
            return True

        def pipe(handle, alternate, index, info):
            info._obj.address = (0x02, 0x83)[index]
            info._obj.type = 2
            info._obj.packet_size = 64
            return True

        api.initialize.side_effect = initialize
        api.query_interface.side_effect = interface
        api.query_pipe.side_effect = pipe
        with patch('winusb_transport._WindowsApi', return_value=api):
            with WinUsbTransport(0, device_path='PICO-B') as transport:
                self.assertEqual(transport.path, 'pico-b')
                self.assertEqual(api.create_file.call_args.args[0], 'pico-b')
        api.close_file.assert_called_once_with(12345)

    def test_late_device_manual_connection_captures_after_first_click(self):
        self.scanner.return_value = []
        app = self.make_app()
        app.auto_connect = False
        self.assertFalse(app.enabled('connect'))
        self.scanner.return_value = [self.usb_a]
        self.click('refresh')
        app.poll_devices()
        with patch.object(self.pg.key, 'get_focused', return_value=True), \
                patch.object(self.pg.mouse, 'get_pressed', return_value=(False, False, False)):
            self.click('connect')
            app.poll_connection()
        self.assertTrue(app.grabbed)
        self.opener.assert_called_once()
        self.assertFalse(self.transport.closed)
        self.assertFalse(self.transport.frames)

    def test_auto_connection_between_click_press_and_release_does_not_disconnect(self):
        app = self.make_app()
        position = app.buttons['connect'].center
        with patch.object(self.pg.key, 'get_focused', return_value=True), \
                patch.object(self.pg.mouse, 'get_pressed', return_value=(False, False, False)):
            app.handle_event(self.pg.event.Event(self.pg.MOUSEBUTTONDOWN, button=1, pos=position))
            app.poll_connection()  # automatic connection starts during the click
            app.poll_connection()  # and finishes before mouse release
            self.assertIs(app.sender, self.sender)
            self.assertFalse(app.grabbed)  # do not swallow the activation release
            app.handle_event(self.pg.event.Event(self.pg.MOUSEBUTTONUP, button=1, pos=position))
            app.poll_connection()
        self.assertTrue(app.grabbed)
        self.assertIs(app.sender, self.sender)
        self.assertTrue(app.auto_connect)
        self.assertFalse(self.transport.closed)
        self.assertFalse(self.transport.frames)

    def test_auto_capture_waits_for_physical_mouse_release(self):
        app = self.make_app()
        with patch.object(self.pg.key, 'get_focused', return_value=True), \
                patch.object(self.pg.mouse, 'get_pressed', return_value=(True, False, False)):
            app.poll_connection()
            app.poll_connection()
        self.assertFalse(app.grabbed)
        with patch.object(self.pg.key, 'get_focused', return_value=True), \
                patch.object(self.pg.mouse, 'get_pressed', return_value=(False, False, False)):
            app.poll_connection()
        self.assertTrue(app.grabbed)
        self.assertFalse(self.transport.frames)

    def test_ini_saves_serial_selection_even_before_device_is_connected(self):
        app = self.make_app()
        self.click('tab_serial')
        app.fields['port'].value = 'COM42'
        app.fields['baud'].value = '2000000'
        app.active_field = 'port'
        app.blur_fields()
        settings, _ = load_settings(self.config)
        self.assertEqual(settings.transport, 'serial')
        self.assertEqual(settings.port, 'COM42')
        self.assertEqual(settings.baud, 2000000)
        self.opener.assert_not_called()
        self.assertIn('[connection]', self.config.read_text(encoding='utf-8'))

    def test_saved_serial_port_connects_after_startup_when_device_arrives(self):
        current = [0]
        self.config.write_text('[connection]\ntransport = serial\nport = COM42\nbaud = 2000000\n',
                               encoding='utf-8')
        self.opener.side_effect = [OSError('端口尚未插入'), (self.sender, 'COM42')]
        app = self.make_app(now=lambda: current[0])
        self.assertEqual(app.fields['port'].value, 'COM42')  # do not substitute the first listed COM7
        app.poll_connection()
        app.poll_connection()
        self.assertIsNone(app.sender)
        current[0] = 0.5
        with patch.object(self.pg.key, 'get_focused', return_value=True), \
                patch.object(self.pg.mouse, 'get_pressed', return_value=(False, False, False)):
            app.poll_connection()
            app.poll_connection()
        self.assertIs(app.sender, self.sender)
        self.assertTrue(app.grabbed)
        for call in self.opener.call_args_list:
            self.assertEqual(call.args[0].transport, 'serial')
            self.assertEqual(call.args[0].port, 'COM42')
            self.assertEqual(call.args[0].baud, 2000000)

    def test_frozen_config_is_beside_executable_instead_of_working_directory(self):
        executable = str(Path(self.temp.name) / 'portable' / 'control.exe')
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', executable):
            self.assertEqual(settings_path(), Path(executable).resolve().parent / 'pico.config.ini')

    def test_ini_preserves_literal_winusb_path_and_rejects_invalid_settings(self):
        self.config.write_text('[connection]\ntransport = winusb\ndevice_path = usb%20#pico\n', encoding='utf-8')
        settings, _ = load_settings(self.config)
        self.assertEqual(settings.device_path, 'usb%20#pico')
        self.config.write_text('[connection]\ntransport = serial\nport = COM42\nbaud = invalid\n', encoding='utf-8')
        self.assertEqual(load_settings(self.config)[0], ConnectionSettings())

    def test_existing_json_settings_migrate_on_first_launch(self):
        legacy = Path(self.temp.name) / 'settings.json'
        legacy.write_text(json.dumps({'connection': asdict(ConnectionSettings('serial', 0, 'COM42', 2000000))}),
                          encoding='utf-8')
        with patch('control_app.settings_path', return_value=self.config), \
                patch('control_app.legacy_settings_path', return_value=legacy):
            app = self.make_app(config_path=None)
        self.assertEqual(app.settings.transport, 'serial')
        self.assertEqual(app.settings.port, 'COM42')
        self.assertTrue(self.config.exists())
        self.assertEqual(load_settings(self.config)[0].port, 'COM42')


class WinUsbReadTests(unittest.TestCase):
    def setUp(self):
        self.transport = WinUsbTransport.__new__(WinUsbTransport)
        self.transport._api = Mock()
        self.transport._handle = 99
        self.transport._pending_input = b''
        self.transport._read_recovery_available = True
        self.stall = OSError('IN pipe stalled')
        self.stall.winerror = 31

    def test_read_recovers_stalled_pipe_once_without_replaying_input(self):
        with patch.object(self.transport, '_read_once', side_effect=[self.stall, b'', self.stall]):
            self.assertEqual(self.transport.read(), b'')
            with self.assertRaises(OSError):
                self.transport.read()
        self.transport._api.reset_pipe.assert_called_once_with(99, 0x83)
        self.transport._api.write_pipe.assert_not_called()

    def test_persistent_failure_and_device_removal_are_reported(self):
        with patch.object(self.transport, '_read_once', side_effect=self.stall):
            with self.assertRaises(OSError):
                self.transport.read()
        self.transport._api.reset_pipe.assert_called_once()
        removed = OSError('device removed')
        removed.winerror = 433
        self.transport._api.reset_pipe.reset_mock()
        with patch.object(self.transport, '_read_once', side_effect=removed):
            with self.assertRaises(OSError):
                self.transport.read()
        self.transport._api.reset_pipe.assert_not_called()

    def test_connection_probe_checks_in_pipe_and_preserves_device_events(self):
        def descriptor(handle, kind, index, language, buffer, length, received):
            buffer[:] = bytes.fromhex('12010002000000408a2ed1c9020201020301')
            received._obj.value = 18
            return True

        def control(handle, setup, buffer, length, received, overlapped):
            received._obj.value = 2
            return True

        self.transport._api.get_descriptor.side_effect = descriptor
        self.transport._api.control.side_effect = control
        event = b'\x55\xaa\x02\x01\x03'
        with patch.object(self.transport, 'write') as write, \
                patch.object(self.transport, '_read_once', return_value=event) as read:
            self.assertEqual(self.transport.check_connection(), 0x0202)
            self.assertEqual(self.transport.read(), event)
            read.assert_called_once()
            write.assert_called_once_with(bytes(64))
        with patch.object(self.transport, 'write'), \
                patch.object(self.transport, '_read_once', side_effect=self.stall):
            with self.assertRaises(OSError):
                self.transport.check_connection()


if __name__ == "__main__":
    unittest.main()
