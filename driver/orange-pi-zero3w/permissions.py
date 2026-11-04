#!/usr/bin/python3
"""udev bind helper: grant the dedicated group only the P0 PWM attributes."""
import grp
import os
from pathlib import Path


def apply_permissions(sysfs=Path("/sys"), gid=None, devfs=Path("/dev")):
    expected = sysfs / "bus/i2c/drivers/fusion_hat"
    if (sysfs / "bus/i2c/devices/0-0017/driver").resolve() != expected:
        raise RuntimeError("Fusion client is not bound on bus 0")
    for parameter in ("disable_auto_shutdown", "restrict_controls"):
        if (sysfs / "module/fusion_hat/parameters" / parameter).read_text().strip() != "Y":
            raise RuntimeError(f"Required module parameter {parameter} is not Y")
    if gid is None:
        gid = grp.getgrnam("fusionhat-core").gr_gid
    for attribute in ("period", "duty_cycle", "enable"):
        path = sysfs / "class/fusion_hat/fusion_hat/pwm/pwm0" / attribute
        os.chown(path, 0, gid)
        os.chmod(path, 0o660)
    # Also covers an input add event processed before the parent's bind event.
    client = (sysfs / "bus/i2c/devices/0-0017").resolve()
    for event in (sysfs / "class/input").glob("event*"):
        device = (event / "device").resolve()
        if not device.is_relative_to(client):
            continue
        if (device / "name").read_text().strip() != "Fusion HAT USR Custom Button":
            continue
        node = devfs / "input" / event.name
        if node.exists():
            os.chown(node, 0, gid)
            os.chmod(node, 0o660)


if __name__ == "__main__":
    apply_permissions()
