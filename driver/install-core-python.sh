#!/usr/bin/env bash
# Run as the runtime user, using an explicitly selected existing venv.
set -euo pipefail
[[ $# == 1 && $1 == /* && -x $1 ]] || { echo "Usage: $0 /absolute/venv/bin/python" >&2; exit 2; }
[[ $EUID != 0 ]] || { echo "Run as the intended runtime user, without sudo." >&2; exit 1; }
venv_python=$1
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
"$venv_python" "$repo/driver/core_python_preflight.py"
# No RPi.GPIO, voice/audio/display packages or global Python changes are needed.
"$venv_python" -m pip install setuptools wheel evdev
"$venv_python" -m pip install --no-deps --no-build-isolation "$repo"
"$venv_python" -c 'from fusion_hat.battery import Battery; from fusion_hat.user_button import UserButton; from fusion_hat.pwm import PWM; from fusion_hat.servo import Servo; from fusion_hat.device import get_firmware_version; print("Core imports passed (no devices instantiated)")'
