#!/usr/bin/python3
"""Coldplug/rebind grant for the opt-in audio experiment's speaker switch only."""
import grp
import os
from pathlib import Path


def apply(sysfs=Path('/sys'), gid=None):
    audio = sysfs / 'firmware/devicetree/base/fusion-hat-audio/status'
    if not audio.is_file() or audio.read_bytes().rstrip(b'\0') != b'okay':
        return
    if (sysfs / 'bus/i2c/devices/0-0017/driver').resolve() != sysfs / 'bus/i2c/drivers/fusion_hat':
        return
    for flag in ('disable_auto_shutdown', 'restrict_controls'):
        if (sysfs / 'module/fusion_hat/parameters' / flag).read_text().strip() != 'Y':
            return
    speaker = sysfs / 'class/fusion_hat/fusion_hat/speaker'
    if gid is None:
        gid = grp.getgrnam('fusionhat-audio').gr_gid
    os.chown(speaker, 0, gid)
    speaker.chmod(0o660)


if __name__ == '__main__':
    apply()
