#!/usr/bin/env python3
"""Explicit non-root firmware/battery and USR evdev callback check. No PWM writes."""
import argparse
import os
from pathlib import Path
import threading


def verify_binding(sysfs=Path("/sys")):
    for parameter in ("disable_auto_shutdown", "restrict_controls"):
        if (sysfs / "module/fusion_hat/parameters" / parameter).read_text().strip() != "Y":
            raise RuntimeError(f"Required parameter {parameter} is not Y")
    if (sysfs / "bus/i2c/devices/0-0017/driver").resolve() != sysfs / "bus/i2c/drivers/fusion_hat":
        raise RuntimeError("Expected persistent bus-0 binding is absent")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--button-seconds", type=int, default=20)
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("Run as the configured runtime user without sudo")
    if not 1 <= args.button_seconds <= 120:
        parser.error("--button-seconds must be between 1 and 120")
    verify_binding()
    from fusion_hat.device import get_firmware_version, get_usr_btn
    from fusion_hat.battery import Battery
    from fusion_hat.user_button import UserButton
    battery = Battery()
    print("Firmware:", get_firmware_version())
    print("Battery:", battery.voltage, "V;", battery.status, ";", battery.capacity, "% (estimate)")
    if get_usr_btn():
        raise RuntimeError("Release USR before starting the event check")
    pressed = threading.Event()
    completed = threading.Event()
    button = UserButton()
    try:
        if button._device is None:
            raise RuntimeError("Fusion USR event device is inaccessible; check scoped udev permissions")
        print("Input:", button._device_path, button._device.name)
        def on_press():
            print("USR press event", flush=True)
            pressed.set()
        def on_release():
            print("USR release event", flush=True)
            if pressed.is_set():
                completed.set()
        button.set_on_press(on_press)
        button.set_on_release(on_release)
        print("Press and release USR once (not the power button).", flush=True)
        if not completed.wait(args.button_seconds):
            raise RuntimeError("No complete USR press/release event pair received")
        print("Python firmware/battery and USR event callbacks passed.")
    finally:
        button.stop()


if __name__ == "__main__":
    main()
