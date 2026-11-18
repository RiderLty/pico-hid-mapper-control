"""One SDL window for connections, settings and explicit input capture."""
from concurrent.futures import ThreadPoolExecutor
import configparser
from dataclasses import asdict, dataclass
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import time

from control_core import ControlSender, controller_report
from app_info import APP_TITLE, ICON_PATH
from winusb_transport import WinUsbDevice, winusb_devices

FONT_NAMES = ("microsoftyahei", "microsoftyaheiui", "simhei", "pingfangsc",
              "hiraginosansgb", "notosanscjksc", "wenquanyimicrohei")
RECONNECT_INTERVAL = 0.5


@dataclass
class ConnectionSettings:
    transport: str = "winusb"
    device_index: int = 0
    port: str = ""
    baud: int = 4000000
    device_path: str = ""


def settings_path():
    # __file__ points into PyInstaller's temporary extraction directory; user
    # settings must instead stay beside the executable and survive each launch.
    folder = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    return folder / "pico.config.ini"


def legacy_settings_path():
    base = Path(os.environ["APPDATA"]) if os.environ.get("APPDATA") else Path.home() / ".config"
    return base / "PicoRemoteControl" / "settings.json"


def load_settings(path):
    try:
        config = configparser.ConfigParser(interpolation=None)
        config.read_string(path.read_text(encoding="utf-8-sig"))
        section = config["connection"]
        settings = ConnectionSettings(transport=section.get("transport", "winusb").strip().lower(),
                                      device_index=section.getint("device_index", 0),
                                      port=section.get("port", "").strip(),
                                      baud=section.getint("baud", 4000000),
                                      device_path=section.get("device_path", "").strip())
        return validate_settings(settings), True
    except (OSError, ValueError, TypeError, KeyError, configparser.Error):
        return ConnectionSettings(), True


def validate_settings(settings):
    if (settings.transport not in ("winusb", "serial") or type(settings.device_index) is not int
            or settings.device_index < 0 or not isinstance(settings.port, str)
            or type(settings.baud) is not int or not 9600 <= settings.baud <= 6000000
            or not isinstance(settings.device_path, str)):
        raise ValueError("invalid connection settings")
    return settings


def load_legacy_settings():
    try:
        data = json.loads(legacy_settings_path().read_text(encoding="utf-8"))
        return validate_settings(ConnectionSettings(**data["connection"]))
    except (OSError, ValueError, TypeError, KeyError):
        return ConnectionSettings()


def open_device(settings):
    if settings.transport == "winusb":
        from winusb_transport import WinUsbTransport
        transport = WinUsbTransport(settings.device_index, device_path=settings.device_path or None)
    else:
        from serial_transport import SerialTransport
        transport = SerialTransport(settings.port, settings.baud)
    try:
        if settings.transport == "winusb":
            revision = transport.check_connection()
            label = f"WinUSB · REV_{revision:04X}"
        else:
            label = f"串口 · {settings.port} · {settings.baud}"
        return ControlSender(transport), label
    except BaseException:
        transport.close()
        raise


class Field:
    def __init__(self, pygame, rect, label, value, choices=()):
        self.rect = pygame.Rect(rect)
        self.label, self.value = label, str(value)
        self.choices = list(choices)
        self.selected = False
        self.expanded = False
        self.scroll = 0
        self.replace_on_input = False

    def option_rects(self):
        for index, value in enumerate(self.choices[self.scroll:self.scroll + 6]):
            yield self.rect.move(0, (index + 1) * self.rect.height), value

    def draw(self, app, enabled):
        pg, rect = app.pg, self.rect
        app.text(self.label, rect.x, rect.y - 26, small=True)
        pg.draw.rect(app.screen, (33, 43, 57) if enabled else (28, 33, 43), rect, border_radius=5)
        pg.draw.rect(app.screen, (75, 160, 240) if self.selected else (66, 79, 98), rect, 1, border_radius=5)
        clip = app.screen.get_clip()
        app.screen.set_clip(rect.inflate(-14, -4))
        app.text(self.value, rect.x + 9, rect.y + 4, small=True)
        app.screen.set_clip(clip)
        if self.choices:
            pg.draw.polygon(app.screen, (180, 193, 209),
                            ((rect.right - 18, rect.y + 13), (rect.right - 8, rect.y + 13),
                             (rect.right - 13, rect.y + 19)))

    def draw_options(self, app):
        if self.expanded:
            for rect, value in self.option_rects():
                app.pg.draw.rect(app.screen, (42, 56, 74), rect)
                app.pg.draw.rect(app.screen, (81, 99, 123), rect, 1)
                app.text(value, rect.x + 9, rect.y + 4, small=True)


class RemoteControlApp:
    def __init__(self, args, *, keyboard_factory=None, opener=open_device,
                 executor=None, config_path=None, now=time.monotonic,
                 device_scanner=winusb_devices, scan_executor=None):
        import pygame
        self.pg = pygame
        self.args, self.opener, self.now = args, opener, now
        self.config_path = config_path if config_path is not None else settings_path()
        saved, _ = load_settings(self.config_path)
        if config_path is None and not self.config_path.exists():
            saved = load_legacy_settings()
        self._saved_connection = None
        self.auto_connect = True
        self.logger = logging.Logger("pico.control")
        self.log_handler = None
        self.last_error = None
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.log_handler = RotatingFileHandler(self.config_path.with_name("controller.log"),
                                                 maxBytes=262144, backupCount=2, encoding="utf-8")
            self.log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self.logger.addHandler(self.log_handler)
        except OSError:
            self.logger.addHandler(logging.NullHandler())
        self.settings = ConnectionSettings(args.transport or saved.transport,
                                          saved.device_index if args.device_index is None else args.device_index,
                                          saved.port if args.port is None else args.port,
                                          saved.baud if args.baud is None else args.baud,
                                          saved.device_path if args.device_index is None else "")
        self.executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="device-connect")
        self.scan_executor = scan_executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="device-scan")
        self.device_scanner = device_scanner
        self.scan_pending = None
        self.next_scan = 0
        self.devices = []
        self.selected_path = self.settings.device_path
        self.selected_name = "上次选择的设备"
        self.connected_path = ""
        self.device_scroll = 0
        self.pending = None
        self.sender = None
        self.label = "已断开"
        self.message = "等待设备连接"
        self.error = False
        self.grabbed = False
        self.capture_on_focus = False
        self.running = True
        self.keyboard = None
        self.keyboard_error = False
        self.controller = None
        self.controller_module = None
        self.active_field = None
        self.pressed_button = None
        self.pressed_connect_intent = None
        self.ui_mouse_down = False
        self.retry_at = 0
        self.closed = False
        self.fields = {}
        pygame.init()
        try:
            self.screen = pygame.display.set_mode((780, 620))
            pygame.display.set_caption(APP_TITLE)
            pygame.display.set_icon(pygame.image.load(str(ICON_PATH)))
            pygame.key.stop_text_input()
            self.font = pygame.font.SysFont(FONT_NAMES, 22)
            self.small_font = pygame.font.SysFont(FONT_NAMES, 18)
            self.title_font = pygame.font.SysFont(FONT_NAMES, 28)
            self.clock = pygame.time.Clock()
            self.fields = {
                "port": Field(pygame, (42, 234, 410, 38), "端口", self.settings.port),
                "baud": Field(pygame, (478, 234, 240, 38), "波特率", self.settings.baud,
                              ("115200", "1000000", "2000000", "4000000", "6000000")),
            }
            self.buttons = {name: pygame.Rect(rect) for name, rect in {
                "auto": (568, 22, 188, 36), "tab_winusb": (146, 106, 134, 36),
                "tab_serial": (288, 106, 134, 36), "refresh": (636, 106, 104, 36),
                "connect": (570, 420, 170, 40), "capture": (38, 506, 280, 46),
            }.items()}
            if keyboard_factory is None and os.name == "nt":
                from windows_keyboard import WindowsKeyboard
                keyboard_factory = WindowsKeyboard
            if keyboard_factory is not None:
                self.keyboard = keyboard_factory(pygame.display.get_wm_info().get("window"))
            from pygame._sdl2 import controller
            self.controller_module = controller
            controller.init()
            self.find_controller()
            self.refresh_ports()
            self.save_settings()
            if self.settings.transport == "winusb":
                self.poll_devices()
        except BaseException:
            self.close()
            raise

    def save_settings(self):
        # Save choices even if hardware is absent or a connection fails. A
        # later launch must retry the selected serial port, not the last device
        # that happened to finish connecting successfully.
        if self.fields:
            self.settings.port = self.fields["port"].value.strip()
            try:
                baud = int(self.fields["baud"].value)
                if 9600 <= baud <= 6000000:
                    self.settings.baud = baud
            except ValueError:
                pass  # retain the last valid value while a field is being edited
        data = asdict(self.settings)
        if data == self._saved_connection:
            return
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            config = configparser.ConfigParser(interpolation=None)
            config["connection"] = {key: str(value) for key, value in data.items()}
            temporary = self.config_path.with_name(self.config_path.name + ".tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                config.write(stream)
            temporary.replace(self.config_path)
            self._saved_connection = data
        except OSError as exc:
            self.log_error("Save settings", exc)

    def log_error(self, context, exc):
        detail = f"{context}: {exc}"
        if detail != self.last_error:
            self.logger.warning(detail)
            self.last_error = detail

    def refresh_ports(self):
        try:
            from serial_transport import serial_ports
            self.fields["port"].choices = [port for port, description in serial_ports()]
            self.fields["port"].scroll = 0
            if not self.fields["port"].value and self.fields["port"].choices:
                self.fields["port"].value = self.fields["port"].choices[0]
        except (OSError, RuntimeError) as exc:
            self.log_error("Refresh serial ports", exc)
            self.message, self.error = "暂时无法刷新端口", True

    def find_controller(self):
        if self.controller is None and self.controller_module is not None:
            for index in range(self.controller_module.get_count()):
                if self.controller_module.is_controller(index):
                    self.controller = self.controller_module.Controller(index)
                    break

    @property
    def selected_device(self):
        return next((device for device in self.devices
                     if device.path.casefold() == self.selected_path.casefold()), None)

    def poll_devices(self):
        if self.scan_pending is not None and self.scan_pending.done():
            future, self.scan_pending = self.scan_pending, None
            try:
                found = future.result()
                self.devices = list({device.path.casefold(): device for device in found}.values())
                if not self.selected_path and self.devices:
                    index = min(self.settings.device_index, len(self.devices) - 1)
                    self.selected_path = self.devices[index].path
                if self.selected_device is not None:
                    self.selected_name = self.selected_device.name
                self.device_scroll = min(self.device_scroll, max(0, len(self.device_rows()) - 3))
            except Exception as exc:
                self.devices = []
                self.log_error("Scan WinUSB devices", exc)
            self.next_scan = self.now() + RECONNECT_INTERVAL
        if self.scan_pending is None and self.now() >= self.next_scan:
            self.scan_pending = self.scan_executor.submit(self.device_scanner)

    def device_rows(self):
        rows = [(device, True) for device in self.devices]
        if self.selected_path and self.selected_device is None:
            rows.insert(0, (WinUsbDevice(self.selected_path, self.selected_name), False))
        return rows

    def visible_device_rows(self):
        for index, (device, present) in enumerate(self.device_rows()[self.device_scroll:self.device_scroll + 3]):
            yield self.pg.Rect(38, 190 + index * 58, 704, 52), device, present

    def select_device(self, path):
        if not self.editable:
            return
        device = next((item for item in self.devices if item.path == path), None)
        if device is not None:
            self.selected_path, self.selected_name = device.path, device.name
            self.settings.device_path = device.path
            self.retry_at = 0
            self.message, self.error = "已选择设备", False
            self.save_settings()

    def select_transport(self, transport):
        if not self.editable:
            return
        self.blur_fields()
        self.settings.transport = transport
        self.retry_at = 0
        self.message, self.error = "等待设备连接", False
        if transport == "winusb":
            self.next_scan = 0
            self.poll_devices()
        else:
            self.refresh_ports()
        self.save_settings()

    @property
    def editable(self):
        return self.sender is None and self.pending is None

    def enabled(self, name):
        if self.grabbed:
            return False
        if name in ("tab_winusb", "tab_serial"):
            return self.editable
        if name == "refresh":
            return self.settings.transport == "winusb" or self.editable
        if name == "connect":
            return self.sender is not None or (self.editable and
                    (self.settings.transport == "serial" or self.selected_device is not None))
        if name == "capture":
            return self.sender is not None and not self.keyboard_error
        return True

    def connect(self, transport, *, automatic=False):
        if not self.editable:
            return
        try:
            index = self.settings.device_index
            baud = int(self.fields["baud"].value) if transport == "serial" else self.settings.baud
            port = self.fields["port"].value.strip()
            if transport == "winusb" and self.selected_device is None:
                self.message, self.error = "未发现所选设备", False
                self.retry_at = self.now() + RECONNECT_INTERVAL
                return
            if index < 0 or not 9600 <= baud <= 6000000 or (transport == "serial" and not port):
                raise ValueError("请检查设备索引、串口和波特率。")
            self.settings = ConnectionSettings(transport, index, port, baud, self.selected_path)
            self.save_settings()
            self.pending = self.executor.submit(self.opener, self.settings)
            self.message = "正在查找设备" if automatic else "连接中"
            self.error = False
            self.blur_fields()
        except (ValueError, RuntimeError) as exc:
            self.log_error("Connection settings", exc)
            self.message, self.error = "请选择端口" if transport == "serial" and not self.fields["port"].value.strip() else "请检查连接设置", True
            self.retry_at = self.now() + RECONNECT_INTERVAL

    def poll_connection(self):
        if self.settings.transport == "winusb":
            self.poll_devices()
        if self.pending is not None and self.pending.done():
            future, self.pending = self.pending, None
            try:
                self.sender, detail = future.result()
                self.connected_path = self.settings.device_path if self.settings.transport == "winusb" else ""
                self.label = "已连接 · WinUSB" if self.settings.transport == "winusb" else "已连接 · 串口"
                self.message, self.error = "已连接", False
                self.last_error = None
                self.logger.info("Connected: %s", detail)
                self.capture_on_focus = True
                self.save_settings()
            except Exception as exc:
                self.log_error("Connect device", exc)
                self.label = "已断开"
                self.message, self.error = ("等待设备连接" if self.auto_connect else "未连接，请重试"), False
            self.retry_at = self.now() + RECONNECT_INTERVAL
        if (self.auto_connect and self.editable and self.active_field is None and self.now() >= self.retry_at
                and (self.settings.transport == "serial" or self.selected_device is not None)):
            self.connect(self.settings.transport, automatic=True)
        self.try_auto_capture()

    def try_auto_capture(self):
        if (self.capture_on_focus and self.sender is not None and self.pg.key.get_focused()
                and not self.ui_mouse_down and not any(self.pg.mouse.get_pressed())):
            self.set_capture(True)

    def blur_fields(self):
        if self.active_field is not None:
            self.save_settings()
        self.active_field = None
        for field in self.fields.values():
            field.selected = field.expanded = False
        self.pg.key.stop_text_input()

    def set_capture(self, enabled, *, update_keyboard=True, reset_keyboard=False,
                    preserve_auto_capture=False):
        enabled = bool(enabled and self.sender is not None and not self.keyboard_error)
        if not preserve_auto_capture:
            self.capture_on_focus = False
        if self.keyboard is not None and (update_keyboard or not enabled):
            self.keyboard.set_enabled(enabled, reset=reset_keyboard)
        self.grabbed = enabled
        self.pg.event.set_grab(enabled)
        self.pg.mouse.set_visible(not enabled)
        self.pg.mouse.get_rel()
        self.blur_fields()
        if self.sender is not None:
            self.message = "独占中" if enabled else "键鼠已释放"
        if not enabled and self.sender is not None:
            self.sender.release_all()

    def disconnect(self, *, manual=True):
        if manual:
            self.auto_connect = False
            self.save_settings()
        error = None
        try:
            self.set_capture(False, reset_keyboard=True)
        except OSError as exc:
            error = exc
        finally:
            sender, self.sender = self.sender, None
            if sender is not None:
                try:
                    sender.close()
                except OSError as exc:
                    error = exc
        if error:
            self.log_error("Disconnect device", error)
        self.label = "已断开"
        self.connected_path = ""
        self.message = "键鼠已释放"
        self.error = error is not None
        self.retry_at = self.now() + RECONNECT_INTERVAL

    def fail_connection(self, exc):
        self.disconnect(manual=False)
        self.log_error("Device disconnected", exc)
        self.message, self.error = ("已断开，正在检测" if self.auto_connect else "连接已断开"), False

    def action(self, name):
        if not self.enabled(name):
            return
        if name in ("tab_winusb", "tab_serial"):
            self.select_transport(name[4:])
        elif name == "refresh":
            if self.settings.transport == "winusb":
                self.next_scan = 0
                self.poll_devices()
            else:
                self.refresh_ports()
        elif name == "connect":
            if self.sender is not None:
                self.disconnect()
            else:
                self.connect(self.settings.transport)
        elif name == "capture":
            self.set_capture(True)
        elif name == "auto":
            self.auto_connect = not self.auto_connect
            self.retry_at = 0
            self.save_settings()

    def handle_ui(self, event):
        pg = self.pg
        if event.type == pg.MOUSEWHEEL:
            if self.settings.transport == "winusb":
                self.device_scroll = max(0, min(max(0, len(self.device_rows()) - 3), self.device_scroll - event.y))
            for field in self.fields.values() if self.settings.transport == "serial" else ():
                if field.expanded:
                    field.scroll = max(0, min(max(0, len(field.choices) - 6), field.scroll - event.y))
            return
        if event.type == pg.MOUSEBUTTONDOWN and event.button == 1:
            self.ui_mouse_down = True
            self.pressed_button = next((name for name, rect in self.buttons.items()
                                        if rect.collidepoint(event.pos) and self.enabled(name)), None)
            self.pressed_connect_intent = ("disconnect" if self.sender is not None else "connect") if self.pressed_button == "connect" else None
        if event.type == pg.MOUSEBUTTONUP and event.button == 1:
            self.ui_mouse_down = False
            for field in self.fields.values():
                if field.expanded:
                    for rect, value in field.option_rects():
                        if rect.collidepoint(event.pos):
                            field.value = value
                            self.blur_fields()
                            return
            self.blur_fields()
            if self.settings.transport == "winusb" and self.editable:
                for rect, device, present in self.visible_device_rows():
                    if present and rect.collidepoint(event.pos):
                        self.select_device(device.path)
                        self.pressed_button = None
                        return
            if self.editable and self.settings.transport == "serial":
                for name, field in self.fields.items():
                    if field.rect.collidepoint(event.pos):
                        self.active_field = name
                        field.selected = True
                        field.replace_on_input = True
                        field.expanded = bool(field.choices and event.pos[0] >= field.rect.right - 28)
                        pg.key.start_text_input()
                        pg.key.set_text_input_rect(field.rect)
                        return
            name, self.pressed_button = self.pressed_button, None
            intent, self.pressed_connect_intent = self.pressed_connect_intent, None
            if name and self.buttons[name].collidepoint(event.pos):
                # Auto-connect may finish between mouse-down and mouse-up.
                # A click intended to connect must never turn into disconnect.
                if name == "connect" and intent == "connect" and self.sender is not None:
                    self.capture_on_focus = True
                elif name != "connect" or intent != "disconnect" or self.sender is not None:
                    self.action(name)
        if self.active_field is not None and self.editable:
            field = self.fields[self.active_field]
            if event.type == pg.TEXTINPUT:
                text = event.text
                if self.active_field == "baud":
                    text = "".join(char for char in text if char.isascii() and char.isdigit())
                if text:
                    field.value = (("" if field.replace_on_input else field.value) + text)[:64]
                    field.replace_on_input = False
            elif event.type == pg.KEYDOWN:
                if event.key == pg.K_BACKSPACE:
                    field.value = "" if field.replace_on_input else field.value[:-1]
                    field.replace_on_input = False
                elif event.key == pg.K_a and event.mod & pg.KMOD_CTRL:
                    field.replace_on_input = True
                elif event.key == pg.K_RETURN:
                    self.connect("serial")
                elif event.key == pg.K_TAB:
                    names = list(self.fields)
                    name = names[(names.index(self.active_field) + 1) % len(names)]
                    self.blur_fields()
                    self.active_field = name
                    self.fields[name].selected = True
                    self.fields[name].replace_on_input = True
                    pg.key.start_text_input()

    def handle_event(self, event):
        pg = self.pg
        if event.type == pg.QUIT:
            self.running = False
            return
        if event.type == pg.WINDOWFOCUSLOST:
            self.ui_mouse_down = False
            self.pressed_button = self.pressed_connect_intent = None
            self.set_capture(False, reset_keyboard=True, preserve_auto_capture=not self.grabbed)
            return
        if event.type == pg.WINDOWFOCUSGAINED:
            self.try_auto_capture()
            return
        if event.type == pg.CONTROLLERDEVICEADDED:
            self.find_controller()
        if (self.keyboard is None and event.type == pg.KEYDOWN and event.scancode == 20
                and event.mod & pg.KMOD_CTRL and event.mod & pg.KMOD_SHIFT
                and event.mod & pg.KMOD_ALT and not getattr(event, "repeat", False)):
            self.set_capture(not self.grabbed)
            return
        if not self.grabbed:
            self.handle_ui(event)
        elif event.type in (pg.KEYDOWN, pg.KEYUP):
            if self.keyboard is None:
                self.sender.sendKey(event.scancode, event.type == pg.KEYDOWN)
        elif event.type in (pg.MOUSEBUTTONDOWN, pg.MOUSEBUTTONUP):
            if event.button not in (4, 5):
                self.sender.sendMouseBTN(event.button, event.type == pg.MOUSEBUTTONDOWN)
        elif event.type == pg.MOUSEMOTION:
            self.sender.sendMouseMove(*event.rel)
        elif event.type == pg.MOUSEWHEEL:
            self.sender.sendWheel(-event.y if event.flipped else event.y)

    def poll_inputs(self):
        if self.keyboard is not None:
            for event in self.keyboard.drain():
                if event[0] == "error":
                    self.keyboard_error = True
                    self.set_capture(False)
                    self.log_error("Keyboard capture", event[1])
                    self.message, self.error = "键鼠已释放，请重新打开程序", True
                elif event[0] == "toggle":
                    self.set_capture(event[1], update_keyboard=False)
                elif event[0] == "focus_lost":
                    self.set_capture(False, reset_keyboard=True)
                elif event[0] == "key" and self.grabbed:
                    self.sender.sendKey(event[1], event[2])
        if self.controller is not None:
            if not self.controller.attached():
                if self.sender is not None:
                    self.sender.send_gamepad((0, 0, 0, 0, 0, 0, 0))
                self.controller.quit()
                self.controller = None
                self.find_controller()
            elif self.grabbed:
                self.sender.send_gamepad(controller_report(
                    [self.controller.get_axis(i) for i in range(6)],
                    [self.controller.get_button(i) for i in range(15)], self.args.deadzone))
        if self.sender is not None:
            self.sender.poll()

    def text(self, value, x, y, *, small=False, color=(225, 232, 242), font=None):
        self.screen.blit((font or (self.small_font if small else self.font)).render(value, True, color), (x, y))

    def wrapped_text(self, value, x, y, width, color, lines=2):
        line, count = "", 0
        for char in value:
            if self.small_font.size(line + char)[0] > width:
                self.text(line, x, y + count * 24, small=True, color=color)
                line, count = "", count + 1
                if count >= lines:
                    return
            line += char
        self.text(line, x, y + count * 24, small=True, color=color)

    def draw(self):
        pg = self.pg
        self.screen.fill((20, 26, 35))
        self.text(APP_TITLE, 24, 18, font=self.title_font)
        for rect in ((22, 94, 736, 294), (22, 406, 736, 66), (22, 490, 736, 80)):
            pg.draw.rect(self.screen, (28, 36, 48), rect, border_radius=9)
        self.text("连接方式", 38, 109)
        if self.settings.transport == "winusb":
            self.text(f"已发现 {len(self.devices)} 台设备", 42, 158, small=True, color=(153, 171, 193))
            rows = list(self.visible_device_rows())
            if not rows:
                self.text("未发现设备", 326, 233, color=(173, 189, 209))
                self.text("接入设备后自动显示", 306, 268, small=True, color=(126, 146, 171))
            for rect, device, present in rows:
                selected = device.path.casefold() == self.selected_path.casefold()
                connected = self.sender is not None and device.path.casefold() == self.connected_path.casefold()
                pg.draw.rect(self.screen, (38, 63, 91) if selected else (35, 45, 60), rect, border_radius=7)
                if selected:
                    pg.draw.rect(self.screen, (73, 149, 230), rect, 1, border_radius=7)
                pg.draw.circle(self.screen, (106, 209, 166) if connected else (75, 156, 235) if selected and present else (104, 123, 147),
                               (rect.x + 17, rect.y + 17), 5)
                clip = self.screen.get_clip()
                self.screen.set_clip(pg.Rect(rect.x + 32, rect.y, 520, rect.height))
                self.text(device.name, rect.x + 32, rect.y + 1, color=(229, 236, 247) if present else (133, 151, 174))
                self.text(device.location or ("已选择的设备" if selected else "WinUSB 设备"),
                          rect.x + 32, rect.y + 28, small=True, color=(153, 171, 193))
                self.screen.set_clip(clip)
                status = "已连接" if connected else "已断开" if not present else "已选择" if selected else "可连接"
                self.text(status, rect.right - 84, rect.y + 15, small=True,
                          color=(116, 216, 174) if connected else (153, 184, 220))
            if len(self.device_rows()) > 3:
                self.text("滚轮查看更多设备", 42, 364, small=True, color=(126, 146, 171))
        else:
            self.text("选择串口", 42, 166)
            for field in self.fields.values():
                field.draw(self, self.editable)
        state = "连接中" if self.pending is not None else "已连接" if self.sender is not None else "检测中" if self.auto_connect else "已断开"
        status = f"{'WinUSB' if self.settings.transport == 'winusb' else '串口'} · {state}"
        pg.draw.circle(self.screen, (106, 209, 166) if self.sender is not None else (75, 156, 235) if self.auto_connect else (104, 123, 147),
                       (43, 428), 5)
        self.text(status, 58, 412)
        self.wrapped_text(self.message, 58, 442, 490,
                          (255, 159, 150) if self.error else (153, 171, 193), lines=1)
        labels = {"auto": f"自动连接：{'开' if self.auto_connect else '关'}", "tab_winusb": "WinUSB",
                  "tab_serial": "串口", "refresh": "刷新", "connect": "断开连接" if self.sender is not None else "连接设备",
                  "capture": "独占中 · 快捷键释放" if self.grabbed else "开始独占键鼠"}
        for name, rect in self.buttons.items():
            enabled = self.enabled(name)
            selected_tab = name == f"tab_{self.settings.transport}"
            color = (44, 118, 198) if selected_tab or (enabled and name == "capture") else (45, 64, 88) if enabled else (37, 44, 56)
            pg.draw.rect(self.screen, color, rect, border_radius=6)
            font = self.small_font if name in ("auto", "refresh") else self.font
            label = font.render(labels[name], True, (234, 241, 250) if enabled or selected_tab else (128, 146, 168))
            self.screen.blit(label, label.get_rect(center=(rect.centerx + (8 if name.startswith("tab_") else 0), rect.centery)))
            if name.startswith("tab_"):
                pg.draw.circle(self.screen, (214, 231, 251), (rect.x + 15, rect.centery), 6, 0 if selected_tab else 1)
        self.text("Ctrl+Shift+Alt+Q", 350, 498)
        self.text("释放 / 独占", 350, 531, small=True, color=(153, 171, 193))
        if self.settings.transport == "serial":
            for field in self.fields.values():
                field.draw_options(self)
        pg.display.flip()

    def run(self):
        last_draw = -1
        try:
            while self.running:
                try:
                    self.poll_inputs()
                    for event in self.pg.event.get():
                        self.handle_event(event)
                        if not self.running:
                            break
                    if self.running:
                        # Handle focus and the activation click before accepting
                        # a completed connection and capturing local input.
                        self.poll_connection()
                except (OSError, ValueError) as exc:
                    self.fail_connection(exc)
                current = self.now()
                if current - last_draw >= 1 / 60:
                    self.draw()
                    last_draw = current
                self.clock.tick(1000 if self.grabbed else 120)
        finally:
            self.close()
        return 0

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.save_settings()
        # A connection handshake may finish after the window closes. Dispose
        # of its result on the worker; never open another window or capture.
        if self.pending is not None:
            def dispose(future):
                try:
                    sender, label = future.result()
                    sender.close()
                except Exception:
                    pass
            self.pending.add_done_callback(dispose)
            self.pending.cancel()
            self.pending = None
        self.executor.shutdown(wait=False, cancel_futures=True)
        if self.scan_pending is not None:
            self.scan_pending.cancel()
            self.scan_pending = None
        self.scan_executor.shutdown(wait=False, cancel_futures=True)
        try:
            if self.keyboard is not None:
                self.keyboard.close()
        finally:
            try:
                self.disconnect(manual=False)
            finally:
                if self.controller is not None:
                    self.controller.quit()
                self.pg.event.set_grab(False)
                self.pg.mouse.set_visible(True)
                self.pg.quit()
                if self.log_handler is not None:
                    self.log_handler.close()


def run_app(args, **kwargs):
    return RemoteControlApp(args, **kwargs).run()
