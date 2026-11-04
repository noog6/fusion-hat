"""Core import paths and event/servo cleanup with simulated devices only."""
import importlib
import importlib.util
from pathlib import Path
import queue
import sys
import tempfile
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def example(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "examples" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SERVO_TEST = example("orange_pi_zero3w_single_servo")
CORE_CHECK = example("orange_pi_zero3w_core_check")


class CorePythonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sysfs = Path(self.temp.name)
        parameters = self.sysfs / "module/fusion_hat/parameters"
        parameters.mkdir(parents=True)
        for name in ("disable_auto_shutdown", "restrict_controls"):
            (parameters / name).write_text("Y")
        driver = self.sysfs / "bus/i2c/drivers/fusion_hat"
        driver.mkdir(parents=True)
        client = self.sysfs / "bus/i2c/devices/0-0017"
        client.mkdir(parents=True)
        (client / "driver").symlink_to(driver)
        self.pwm = self.sysfs / "class/fusion_hat/fusion_hat/pwm"
        for channel in range(4):
            path = self.pwm / f"pwm{channel}"
            path.mkdir(parents=True)
            (path / "enable").write_text("0")
            (path / "duty_cycle").write_text("0")

    def test_core_imports_and_scoped_usr_discovery_callbacks_and_stop(self):
        fake_evdev = ModuleType("evdev")
        fake_evdev.ecodes = SimpleNamespace(EV_KEY=1, BTN_0=256)
        events = queue.Queue()
        unopened = []
        other = SimpleNamespace(name="Unrelated device", close=Mock())
        class Input:
            name = "Fusion HAT USR Custom Button"
            path = "/dev/input/event42"
            def read(self):
                while True:
                    try:
                        event = events.get_nowait()
                    except queue.Empty:
                        return
                    if event is None:
                        return
                    yield event
            def close(self):
                events.put(None)
        device = Input()
        def open_device(path):
            unopened.append(path)
            if path == "denied":
                raise PermissionError("unrelated input is inaccessible")
            return other if path == "other" else device
        fake_evdev.InputDevice = open_device
        fake_evdev.list_devices = lambda: ["denied", "other", "fusion"]
        def ready(*args):
            if events.empty():
                threading.Event().wait(0.005)
                return [], [], []
            return [device], [], []
        with patch.dict(sys.modules, {"evdev": fake_evdev}):
            module = importlib.import_module("fusion_hat.user_button")
            with patch.object(module, "evdev", fake_evdev), patch.object(module, "InputDevice", open_device), \
                 patch.object(module.select, "select", side_effect=ready), \
                 patch.object(module, "raise_if_fusion_hat_not_ready"):
                # Import paths do not need voice/audio/RPi.GPIO dependencies.
                for name, symbol in (("battery", "Battery"), ("pwm", "PWM"), ("servo", "Servo")):
                    self.assertTrue(hasattr(importlib.import_module("fusion_hat." + name), symbol))
                button = module.UserButton()
                pressed, released = threading.Event(), threading.Event()
                button.set_on_press(pressed.set)
                button.set_on_release(released.set)
                try:
                    events.put(SimpleNamespace(type=1, code=256, value=1))
                    self.assertTrue(pressed.wait(1))
                    self.assertTrue(button.get_state())
                    self.assertTrue(button.is_pressed())
                    events.put(SimpleNamespace(type=1, code=256, value=0))
                    self.assertTrue(released.wait(1))
                    self.assertFalse(button.get_state())
                finally:
                    button.stop()
                    button._listener_thread.join(1)
                self.assertFalse(button._listener_thread.is_alive())
                self.assertFalse(button._is_task_running)
                self.assertEqual(unopened, ["denied", "other", "fusion"])
                other.close.assert_called_once()

    def test_existing_firmware_and_battery_api_units(self):
        device = importlib.import_module("fusion_hat.device")
        battery = importlib.import_module("fusion_hat.battery")
        base = self.sysfs / "class/fusion_hat/fusion_hat"
        (base / "firmware_version").write_text("1.0.2\n")
        (base / "button").write_text("0\n")
        power = self.sysfs / "class/power_supply/fusion-hat"
        power.mkdir(parents=True)
        for name, value in (("voltage_now", "8385000"), ("status", "Charging"), ("capacity", "99")):
            (power / name).write_text(value)
        with patch.object(device, "DEVICE_PATH", str(base) + "/"), \
             patch.object(device, "is_driver_loaded", return_value=True), \
             patch.object(battery.Battery, "PATH", str(power)):
            self.assertEqual(device.get_firmware_version(), "1.0.2")
            instance = battery.Battery()
            self.assertEqual(instance.voltage, 8.38)  # existing API rounds to 2 decimals
            self.assertEqual(instance.status, "Charging")
            self.assertEqual(instance.capacity, 99)
        CORE_CHECK.verify_binding(self.sysfs)

    def fake_servo(self, fail_constructor=False):
        pwm = self.pwm
        pulses = []
        class Servo:
            def __init__(self, channel):
                self.channel = channel
                (pwm / "pwm0/enable").write_text("1")
                if fail_constructor:
                    raise RuntimeError("constructor failed after enable")
            def freq(self):
                return 50
            def pulse_width(self, pulse):
                pulses.append(pulse)
                (pwm / "pwm0/duty_cycle").write_text(str(pulse))
            def enable(self, value):
                (pwm / "pwm0/enable").write_text("1" if value else "0")
        return Servo, pulses

    def exercise(self, factory, sleep=lambda _: None, confirmed=True):
        return SERVO_TEST.exercise(1000, 2000, 1500, 50, 0.5, confirmed,
                                   self.sysfs, factory, sleep)

    def test_servo_limits_and_cleanup_on_normal_exception_and_ctrl_c(self):
        for failure in (None, RuntimeError, KeyboardInterrupt):
            with self.subTest(failure=failure):
                factory, pulses = self.fake_servo()
                def sleep(_):
                    if failure:
                        raise failure("interrupted")
                if failure:
                    with self.assertRaises(failure):
                        self.exercise(factory, sleep)
                else:
                    self.exercise(factory, sleep)
                    self.assertEqual(pulses, [1500, 1550, 1500, 1450, 1500, 0])
                self.assertEqual(pulses[-1], 0)
                self.assertEqual((self.pwm / "pwm0/enable").read_text(), "0")
                self.assertEqual((self.pwm / "pwm0/duty_cycle").read_text(), "0")

    def test_servo_constructor_failure_still_disables_output(self):
        factory, _ = self.fake_servo(fail_constructor=True)
        with self.assertRaisesRegex(RuntimeError, "constructor failed"):
            self.exercise(factory)
        self.assertEqual((self.pwm / "pwm0/enable").read_text(), "0")

    def test_servo_rejects_unconfirmed_range_busy_timer_and_stale_pulse(self):
        factory = Mock()
        with self.assertRaises(ValueError):
            self.exercise(factory, confirmed=False)
        (self.pwm / "pwm2/enable").write_text("1")
        with self.assertRaisesRegex(RuntimeError, "in use"):
            self.exercise(factory)
        (self.pwm / "pwm2/enable").write_text("0")
        (self.pwm / "pwm0/duty_cycle").write_text("1700")
        with self.assertRaisesRegex(RuntimeError, "cached pulse"):
            self.exercise(factory)
        factory.assert_not_called()
        with self.assertRaises(ValueError):
            SERVO_TEST.exercise(1490, 1510, 1500, 50, 0.5, True, self.sysfs, factory)


if __name__ == "__main__":
    unittest.main()
