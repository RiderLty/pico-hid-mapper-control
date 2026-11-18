"""Shared keyboard, mouse and gamepad protocol sender for the local app."""
from pathlib import Path
import sys

# Share the same firmware protocol with existing HID/Python tools.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pio_device_protocol import PioDeviceProtocol

DOWN, UP = 1, 0
MOUSE_BUTTONS = {1: 0, 2: 2, 3: 1, 6: 3, 7: 4}
# SDL A/B/X/Y, Back/Guide/Start, sticks, shoulders, d-pad -> firmware bits.
CONTROLLER_BUTTON_BITS = (12, 13, 14, 15, 5, 10, 4, 6, 7, 8, 9, 0, 1, 2, 3)


def controller_report(axes, buttons, deadzone=0.1):
    """SDL standard axes: LX, LY, RX, RY, LT, RT; triggers are 0..32767."""
    sticks = [0 if abs(value) < deadzone * 32767 else max(-32767, min(32767, value))
              for value in axes[:4]]
    triggers = [max(0, min(32767, value)) for value in axes[4:6]]
    mask = sum(1 << bit for pressed, bit in zip(buttons, CONTROLLER_BUTTON_BITS) if pressed)
    if triggers[0] > 0:
        mask |= 1 << 16
    if triggers[1] > 0:
        mask |= 1 << 17
    return (*sticks, *triggers, mask)


class ControlSender:
    def __init__(self, transport):
        self.transport = transport
        self.protocol = PioDeviceProtocol(transport, report_id_pad=b"")
        self.keys = set()
        self.buttons = set()
        self.gamepad = None

    def sendKey(self, scancode, downup):
        # SDL scancodes use HID usages here, including modifiers 224..231.
        if not 4 <= scancode <= 231:
            return
        down = bool(downup)
        if down == (scancode in self.keys):
            return
        self.protocol.keyboard_key(scancode, down)
        if down:
            self.keys.add(scancode)
        else:
            self.keys.discard(scancode)

    def sendMouseMove(self, x=None, y=None):
        if x or y:
            self.protocol.mouse_move(x or 0, y or 0)

    def sendMouseBTN(self, btn, downup):
        if btn not in MOUSE_BUTTONS:
            return
        button, down = MOUSE_BUTTONS[btn], bool(downup)
        if down == (button in self.buttons):
            return
        self.protocol.mouse_button(button, down)
        if down:
            self.buttons.add(button)
        else:
            self.buttons.discard(button)

    def sendWheel(self, value):
        if value:
            self.protocol.mouse_move(0, 0, value)

    def send_gamepad(self, report):
        if report != self.gamepad:
            self.protocol.gamepad_report(*report)
            self.gamepad = report

    def poll(self):
        return self.protocol.poll()

    def release_all(self):
        # Attempt all releases even if one transfer fails (USB unplugged).
        error = None
        for code in list(self.keys):
            try:
                self.sendKey(code, UP)
            except OSError as exc:
                error = exc
        for button in list(self.buttons):
            try:
                self.protocol.mouse_button(button, False)
                self.buttons.discard(button)
            except OSError as exc:
                error = exc
        if self.gamepad is not None and any(self.gamepad):
            try:
                self.send_gamepad((0, 0, 0, 0, 0, 0, 0))
            except OSError as exc:
                error = exc
        if error:
            raise error

    def close(self):
        try:
            self.release_all()
        finally:
            self.transport.close()


