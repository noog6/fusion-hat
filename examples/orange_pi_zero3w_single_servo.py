#!/usr/bin/env python3
"""Explicit, bounded P0/50 Hz test. One unloaded servo; other actuators disconnected."""
import argparse
import os
from pathlib import Path
import signal
import time


def exercise(min_us, max_us, center_us, step_us, dwell, confirmed,
             sysfs=Path("/sys"), servo_factory=None, sleep=time.sleep):
    if not confirmed:
        raise ValueError("Confirm the servo manufacturer's suitable 50 Hz pulse range first")
    if not (500 <= min_us < center_us < max_us <= 2500 and
            1 <= step_us <= 100 and min_us <= center_us - step_us and
            center_us + step_us <= max_us and 0.2 <= dwell <= 2):
        raise ValueError("Require 500..2500 us envelope, center +/- step within confirmed range, step 1..100 us, dwell 0.2..2 s")
    for parameter in ("disable_auto_shutdown", "restrict_controls"):
        if (sysfs / "module/fusion_hat/parameters" / parameter).read_text().strip() != "Y":
            raise RuntimeError(f"Required module parameter {parameter} is not Y")
    if (sysfs / "bus/i2c/devices/0-0017/driver").resolve() != sysfs / "bus/i2c/drivers/fusion_hat":
        raise RuntimeError("Expected persistent bus-0 binding is absent")
    base = sysfs / "class/fusion_hat/fusion_hat/pwm"
    # P0..P3 share one timer. Constructors change its period before returning.
    for channel in range(4):
        if (base / f"pwm{channel}/enable").read_text().strip() != "0":
            raise RuntimeError(f"Timer group P0..P3 is in use (P{channel}); no output changed")
    if (base / "pwm0/duty_cycle").read_text().strip() != "0":
        raise RuntimeError("P0 has a cached pulse. Disconnect the servo and rebind using the guide before testing")
    if servo_factory is None:
        from fusion_hat.servo import Servo
        servo_factory = Servo
    servo = None
    try:
        # Construction enables output and initializes period/duty. The cleanup
        # fallback must therefore work even if construction raises midway.
        servo = servo_factory(0)
        if servo.freq() != 50:
            raise RuntimeError("Servo frequency is not 50 Hz")
        for pulse in (center_us, center_us + step_us, center_us,
                      center_us - step_us, center_us):
            servo.pulse_width(pulse)
            print(f"P0: 50 Hz, {pulse} us", flush=True)
            sleep(dwell)
    finally:
        try:
            if servo is None:
                raise RuntimeError("Constructor did not return")
            try:
                servo.pulse_width(0)
            finally:
                servo.enable(False)
        except Exception:
            # No object reference exists if __init__ failed after enabling.
            (base / "pwm0/enable").write_text("0")
        if (base / "pwm0/enable").read_text().strip() != "0":
            raise RuntimeError("P0 disable readback failed; disconnect servo power")
        print("P0 reports disabled. Verify the physical control pulses stopped.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("min-us", "max-us", "center-us"):
        parser.add_argument("--" + option, type=int, required=True)
    parser.add_argument("--step-us", type=int, default=50)
    parser.add_argument("--dwell", type=float, default=0.5)
    parser.add_argument("--confirm-servo-range", action="store_true", required=True)
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("Run as the configured runtime user without sudo")
    def terminate(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try:
        exercise(args.min_us, args.max_us, args.center_us, args.step_us,
                 args.dwell, args.confirm_servo_range)
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
