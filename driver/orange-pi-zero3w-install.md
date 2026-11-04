# Experimental Orange Pi Zero 3W / Armbian core installation

This is the second milestone, based on merged PR #2. It targets **only** Orange
Pi Zero 3W / Allwinner A733, aarch64, Armbian 26.8.3 / Ubuntu resolute, kernel
`6.6.98-vendor-sun60iw2`, Python 3.14.4 and a directly connected Fusion HAT+.
The operator has verified installation and binding after reboot with both policy
flags enabled, the expected scoped permissions, and non-root Python 3.14.4
firmware/battery access and USR press/release callbacks. The same policy,
permissions and non-root readout/event checks also passed after a controlled
unbind/rebind. The single-servo experiment remains **pending operator
validation**. These are operator-provided hardware results, not cloud tests.
The first milestone's temporary build/bind/readout/cleanup results are recorded
in [the temporary bring-up guide](orange-pi-zero3w-bringup.md).

The operator separately confirmed vendor symbol `twi0` and live bus-0 node
`/soc@3000000/twi@2510000`, and availability of `dtc`, `fdtget`, `fdtoverlay` and
`update-initramfs`. Installation rechecks the installed vendor DTB and live node;
these values are not assumptions about other boards/images.
The operator also confirmed the matching `initrd.img`, the current-kernel
`uInitrd` symlink and executable `99-uboot` post-update hook. Preflight validates
that this board's legacy U-Boot wrapper contains the exact raw initramfs payload;
the same check runs after rebuilding, so a stale wrapped image blocks activation.

Audio, host GPIO, power-button/shutdown integration and runtime integration are
excluded. Do not run `install.sh`, `make install`, audio setup or automatic Pi
repair. The Raspberry Pi installer/overlay remain unchanged; driver parameters
default to their previous behavior outside this experimental installation.

## 1. Prerequisites and build only

Disconnect all actuators. Finish any temporary session first, and stop other
Fusion users/device managers. The new installer refuses pre-existing ownership.
Keep console/physical power and a way to edit the boot filesystem available.
No step automatically reboots or tests hardware.

From the local reviewed checkout:

```bash
git rev-parse HEAD
uname -m
uname -r
cat /etc/armbian-release
cat /boot/armbianEnv.txt
readlink -f /sys/bus/i2c/devices/i2c-0/of_node
fdtget -t s /boot/dtb/allwinner/sun60i-a733-orangepi-zero3w.dtb /__symbols__ twi0
command -v dtc fdtget fdtoverlay modinfo depmod cmp update-initramfs lsinitramfs udevadm
ls -l /boot/initrd.img-6.6.98-vendor-sun60iw2 /boot/uInitrd*
```

Keep the already verified `overlay_prefix=sun60i-a733`, matching `fdtfile`, and
`i2c0` in `overlays=`. The MCU-only overlay adds just the child at `0x17`; it does
not configure pinmux, audio, SPI or GPIO. It can coexist with `i2c0`, which only
enables the controller/header pins. Conflicting clients or duplicate tokens are
rejected, including a second `0x17` client in the offline-applied boot overlays.

Use the prepared matching vendor headers established in the first milestone.
No package names are guessed, packages installed, or kernels upgraded:

```bash
./driver/build-module.sh --check-only 6.6.98-vendor-sun60iw2 \
  /lib/modules/6.6.98-vendor-sun60iw2/build &&
./driver/build-module.sh 6.6.98-vendor-sun60iw2 \
  /lib/modules/6.6.98-vendor-sun60iw2/build &&
modinfo ./driver/fusion_hat.ko
```

Stop on any error. Require both `disable_auto_shutdown` and `restrict_controls`
in `modinfo`; the old first-milestone binary is insufficient. Inspect `depends`;
this kernel has no IIO, so the external ADC interface is omitted. Firmware,
battery, USR and MCU PWM do not require IIO.

## 2. Check, then install the local artifact

Select the intended non-root runtime account explicitly. Run the check with
sudo so it can also inspect an existing root-owned installation manifest:

```bash
runtime_user=$(id -un)
sudo ./driver/orange-pi-zero3w-install.py check --runtime-user "$runtime_user"
sudo ./driver/orange-pi-zero3w-install.py install --runtime-user "$runtime_user"
```

Do not run `install` if `check` failed. Check compiles and offline-applies the DT
overlay in a temporary directory; it does not mutate installation/boot state.
Missing prerequisites and existing conflicting module/options/ownership stop
the operation with an explanation. Resolve these explicitly rather than using
forced unbind/unload or overwriting configuration.

The operator verified the new driver build and installer/offline overlay check
on this target. An initial initramfs-hook failure rolled back; after the hook
fix, installation and binding after an explicit reboot passed. The hook handles
initramfs-tools' already-copied-file status only after verifying the staged
policy matches exactly, while keeping genuine copy/module errors fatal.
Missing firmware warnings from unrelated built-in drivers are separate from a
fatal `fusion-hat-core` hook error. On failure, preserve the full output,
including the hook's module/policy-specific diagnostic, and inspect rollback
status before rebooting.

The installation owns only:

- `updates/fusion-hat-core/fusion_hat.ko` under the **current kernel's** module tree;
- the vendor-prefix `sun60i-a733-fusion-hat-core.dtbo` in the verified overlay directory;
- `/etc/modprobe.d/fusion-hat-core.conf`, an initramfs hook, one udev rule file and
  the `/usr/libexec/fusion-hat-core-permissions` helper;
- the `fusion-hat-core` overlay token, dedicated `fusionhat-core` group/membership
  and `/var/lib/fusion-hat-core` manifest/recovery snapshot.

It uses the local `.ko` and overlay source. It never clones/replaces the checkout.
The policy file specifies `disable_auto_shutdown=1 restrict_controls=1`. The
initramfs hook copies that policy and the module/dependencies; `depmod` and
`update-initramfs` run and the resulting archive is checked **before** the boot
token is added. Thus DT-triggered modprobe uses the policy even in early boot.
The experimental DT node also makes the driver refuse initialization before any
hardware access unless both flags are active. A missing policy therefore fails
closed instead of starting shutdown polling or world-writable outputs.

Only P0's three PWM controls are granted to `fusionhat-core`. Other PWM channels,
LED and speaker controls stay root-writable. The input rule grants only the
Fusion driver's named USR event device, not every input device. No access to
`/dev/i2c-*` or general GPIO is added. Udev applies rules to recreated input
devices and runs the P0 helper on each successful I2C bind, after probe has
created the attributes. Root coldplug add/change events also apply it if the
driver already bound in the initramfs. The helper rechecks driver/flags and grants
only P0 and the named input device below that exact client. Read-only firmware
and battery sysfs need no new grants.

Same inputs and owned files produce a no-op on repeated installation. Changed
inputs or operator-edited managed files are refused. To replace this experimental
installation, uninstall, reboot, then check/install the reviewed replacement.
Existing unrelated boot settings and overlay tokens are preserved.

This is **not DKMS**: a kernel update does not rebuild/reinstall the module. This
entry point deliberately rejects a different kernel/image. Review and build for
that new target separately, or roll back/uninstall before changing kernels. A
stale overlay alone is not a promise of working hardware on a new kernel.

## 3. Explicit reboot and persistence/permission checks

Review the boot overlay token, modprobe policy and install output. Then reboot
explicitly using your normal procedure. Log in again so group membership takes
effect. Leave actuators disconnected.

```bash
id -nG
cat /sys/module/fusion_hat/parameters/disable_auto_shutdown
cat /sys/module/fusion_hat/parameters/restrict_controls
readlink -f /sys/bus/i2c/devices/0-0017/driver
cat /sys/class/fusion_hat/fusion_hat/firmware_version
stat -Lc '%a %U %G %n' /sys/class/fusion_hat/fusion_hat/pwm/pwm0/{enable,period,duty_cycle}
stat -Lc '%a %U %G %n' /sys/class/fusion_hat/fusion_hat/pwm/pwm1/enable
```

Require group `fusionhat-core`, both flags `Y`, the Fusion driver link, firmware
`1.0.2`, P0 attributes `660 root fusionhat-core`, and P1 `644 root root`.
Check `sudo dmesg` for probe/I2C/udev errors. Parameter readback does not test
low-battery or power-button behavior. Automatic host shutdown remains disabled;
independent HAT firmware protections and physical power removal remain active.

Find the USR event node and verify its ownership as the runtime user:

```bash
for event in /sys/class/input/event*; do
  if [ "$(cat "$event/device/name")" = 'Fusion HAT USR Custom Button' ]; then
    stat -c '%a %U %G %n' "/dev/input/${event##*/}"
  fi
done
```

Require `660 root fusionhat-core`. If available, run `evtest /dev/input/eventN`
**without sudo**, substituting the discovered node, and verify `EV_KEY/BTN_0`
press/release values `1/0`. Do not use the power button.

Before attaching a servo, verify that device recreation reapplies permissions.
With no users and all actuators still disconnected, explicitly unbind/rebind the
managed client (removal zeros PWM and changes LED state):

```bash
printf '0-0017\n' | sudo tee /sys/bus/i2c/drivers/fusion_hat/unbind &&
printf '0-0017\n' | sudo tee /sys/bus/i2c/drivers/fusion_hat/bind &&
sudo udevadm settle
```

Repeat flag/binding/P0 ownership checks and rediscover the potentially new input
event number. No one-time chmod or general `input`/`i2c` group membership is a
substitute for these rules.

With checkout `95836ba`, the operator completed this unbind/rebind procedure and
reported both flags still `Y`, the expected Fusion binding, P0 and named USR
permissions `660 root fusionhat-core`, and P1 `644 root root`. The non-root
Python check then returned firmware `1.0.2`, battery `8.38 V`, `Charging`,
estimated `99%`, and actual USR press/release callbacks again. This verifies
permission reapplication for that device recreation on the stated target.

## 4. Python 3.14.4 in the existing venv, as the runtime user

Set the **absolute interpreter path** for the existing runtime venv. Do not use
sudo or global pip:

```bash
venv_python=/absolute/path/to/existing/.venv/bin/python
./driver/install-core-python.sh "$venv_python"
"$venv_python" examples/orange_pi_zero3w_core_check.py --button-seconds 20
```

The helper checks for a venv and Python 3.14.4, and requires the selected
interpreter's `Python.h` before pip runs if an installed `evdev` cannot be
imported. It installs only `evdev` plus build
tools (`setuptools`, `wheel`), then installs the local library with `--no-deps
--no-build-isolation`. It leaves broad upstream metadata unchanged: optional
voice/audio/display dependencies can still be reported missing by `pip check`.
Do not install the full dependency list to satisfy this core-only experiment.
Existing packages are not deliberately upgraded. Removing the core Python
installation later uses the same interpreter's `-m pip uninstall fusion_hat`;
dependencies and unrelated packages are left alone.

`evdev` is a native extension. If pip cannot build it, save the actual compiler
error and verify the selected interpreter's development headers/toolchain; the
installer does not guess apt packages. An operator-reported target attempt found
`Python.h` missing from `/usr/include/python3.14`; this is a Python development
header prerequisite, separate from the matching kernel headers already used to
build the driver. Obtain the development package matching the selected base
interpreter after inspecting installed/candidate versions. Driver reinstallation
and reboot are not needed to retry this venv-only step.
The reported installed interpreter and both available development packages were
`3.14.4-1ubuntu0.2`. For that observed package state, the explicit prerequisite
installation is:

```bash
sudo apt-get install python3.14-dev=3.14.4-1ubuntu0.2 \
  libpython3.14-dev=3.14.4-1ubuntu0.2
```

This is a separate operator action, never run by the helper. Recheck package
versions first if the interpreter or apt candidates have changed; do not upgrade
the interpreter/kernel to bypass a mismatch. Then rerun the venv helper and
readout/event check above as the non-root runtime user.

After installing those matching development packages, the operator successfully
built and installed `evdev 2.0.0` (`cp314`, aarch64) and the local `fusion_hat
1.14.0` package in the existing Python 3.14.4 venv from commit `41115e5`.
Core imports passed without constructing devices. The non-root readout check
reported firmware `1.0.2`, battery `8.39 V`, `Charging`, estimated `99%`, and actual
USR press and release callbacks from the Fusion input device. The same check
subsequently passed after device recreation as recorded above. Servo operation
remains unverified.

Core imports are `fusion_hat.device`, `battery`, `user_button`, `pwm` and `servo`.
They do not require `RPi.GPIO` or voice/audio packages. The readout script does
not instantiate PWM/Servo. It prints firmware and `Battery.voltage` in **volts**
(rounded by the existing API), charging/status/capacity, then requires actual
evdev press/release callbacks from USR. Permission failure or no complete event
pair is a failure, not an unpressed-button success.

## 5. Separate, explicit single-servo experiment

Use **one unloaded servo on P0**, with all other actuators disconnected and no
other hardware processes. Confirm the servo's manufacturer-specified suitable
**50 Hz pulse range**, neutral pulse, power/wiring and mechanical clearance.
The existing `Servo` class defines a 500–2500 us mapping at 50 Hz, with 1500 us
at its nominal center. These are library defaults; actual servo travel varies.
The first test uses only center +/- 50 us, not the range endpoints.

P0..P3 share a timer, as do P4..P7 and P8..P11. A frequency write changes its
whole timer group. The script refuses any enabled channel in P0..P3 and requires
P0's cached duty to be zero. If not zero, disconnect the servo and use the
rebind procedure above before retrying. It cannot detect an uncooperative root
process racing the test: keep all other users stopped.

Only after confirming that **these example values suit your servo**, explicitly
invoke (substitute the verified values otherwise):

```bash
"$venv_python" examples/orange_pi_zero3w_single_servo.py \
  --min-us 500 --max-us 2500 --center-us 1500 --step-us 50 \
  --dwell 0.5 --confirm-servo-range
```

This uses the existing `Servo` interface, with pulse widths instead of angle
assumptions: 1500, 1550, 1500, 1450, 1500 us with the example arguments, at 50 Hz.
The min/max arguments declare the confirmed envelope; they are not commanded
positions. The global envelope is 500..2500 us, step at most 100 us, and each
dwell 0.2..2 seconds.
Those code limits are not proof that any particular servo is suitable.

PWM/Servo construction **enables output before returning** and changes the timer
period. Cleanup is established before construction: normal completion,
exceptions, Ctrl-C and SIGTERM attempt to zero the pulse and disable P0; a direct
selected-channel sysfs fallback covers failure midway through construction.
Verify `pwm0/enable` reads `0` and confirm physical control pulses stop using a
scope/analyzer or suitable servo tester. Driver readback alone does not prove a
successful I2C write or the physical waveform. SIGKILL, a crash in native code,
power loss or failed I2C/sysfs writes cannot guarantee cleanup; disconnect power
if cleanup is uncertain. Do not test stall/load, broad sweeps or multiple servos.

## 6. Uninstall, rollback and boot recovery

Disconnect actuators and stop all hardware users first:

```bash
sudo ./driver/orange-pi-zero3w-install.py uninstall
```

This removes only recorded files whose contents/modes still match, the owned
overlay token and membership it added. Other boot edits/tokens, unrelated files,
group members and shared dependencies are preserved. It rebuilds the initramfs,
does not unload the currently live driver, and does not reboot. Reboot explicitly
to deactivate the DT client. Group access in existing login sessions lasts until
logout/reboot. The recovery boot snapshot is retained under
`/var/lib/fusion-hat-core/armbianEnv.before`; do not blindly restore it over
subsequent unrelated boot edits.

If installation reports interrupted rollback or a managed file was edited,
inspect the manifest and errors before rebooting. Resolve edits explicitly;
the tool refuses to discard them. Re-run uninstall after resolving the cause.

If the board cannot boot, mount its boot/root filesystems from a recovery system
or use its boot console. Remove only `fusion-hat-core` from `overlays=` in
`/boot/armbianEnv.txt`, preserving `i2c0` and other settings. The MCU overlay has
no role in powering the board; do not alter the power wiring as a software fix.
Boot with actuators disconnected, then run uninstall to remove the owned files
and rebuild the initramfs. If the boot configuration itself was damaged, compare
the retained snapshot and restore only the necessary lines. Never force-load an
old/incorrect module or remove the shutdown policy to make binding succeed.

Record the source SHA, installation/reboot logs, flags, permissions after boot
and rebind, non-root Python/event results, confirmed servo limits, observed
motion and physical output cleanup. This milestone remains pending until those
results are supplied; mocks and host compilation do not establish hardware or
Raspberry Pi regression coverage.
