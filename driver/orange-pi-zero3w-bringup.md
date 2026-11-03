# Orange Pi Zero 3W / Armbian: experimental MCU readout (awaiting hardware validation)

This procedure targets only the Orange Pi Zero 3W (Allwinner A733, aarch64),
Armbian v26.8.3 / Ubuntu resolute, kernel `6.6.98-vendor-sun60iw2`. It does not
establish support for other Orange Pi boards or Armbian images. Use this local
checkout; do **not** run `install.sh`, `make all`, `make install`, or `fusion_hat
doctor --fix`. The existing Raspberry Pi installer and overlay are unchanged.

The known hardware result is a user-reported firmware 1.0.2 read at address
`0x17` on bus 0. Target compilation, module loading and driver readouts are still
pending. No audio, host GPIO, actuator or runtime integration is included here.

## 1. Prerequisites on the Orange Pi Zero 3W (read-only)

Disconnect all motor/servo loads before loading **or removing** the driver. Stop
other Fusion HAT users and do not run competing device/driver management during
this session. Keep physical power access available: The board is powered through the
HAT. Probe turns the LED and speaker off and starts battery/button work. PWM probe
only initializes software state; it does not guarantee zero hardware outputs.
Removal writes zero to all PWM channels and turns the LED off. Do not construct
Python PWM/motor/servo objects: PWM construction enables a channel.

From the repository root:

```bash
uname -m
uname -r
cat /etc/armbian-release
cat /boot/armbianEnv.txt
cat /sys/bus/i2c/devices/i2c-0/name
ls -l /dev/i2c-0
readlink -f "/lib/modules/$(uname -r)/build"
ls -l "/lib/modules/$(uname -r)/build/Makefile"
dpkg-query -W -f='${binary:Package}\t${Version}\n' 'linux-image*' 'linux-headers*'
apt-cache search --names-only 'linux-headers'
command -v make gcc modinfo insmod rmmod
```

Expect `aarch64`, the kernel above, and `SUNXI TWI(0x0000000002510000)`. The
previously enabled `overlay_prefix=sun60i-a733` / `overlays=i2c0` must remain;
this procedure adds no boot overlay. Root-only `/dev/i2c-0` mode `0600` is fine:
the readout interfaces below do not require opening it.

If headers are missing, use the package inventory/search to identify **matching
vendor headers**, or obtain the configured vendor kernel build tree. Do not
guess a package name or upgrade the kernel. A generic source archive or just
`modules_prepare` may lack `Module.symvers` and is insufficient. The selected
tree needs the same configuration, generated headers, symbol versions and
release as the running kernel. A matching release string alone is not proof.

## 2. Build only, without sudo

Explicitly select the prepared headers/build directory. Substitute a verified
path if the normal symlink is missing:

```bash
./driver/build-module.sh 6.6.98-vendor-sun60iw2 \
  /lib/modules/6.6.98-vendor-sun60iw2/build
modinfo ./driver/fusion_hat.ko
modinfo -F vermagic ./driver/fusion_hat.ko
modinfo -F depends ./driver/fusion_hat.ko
```

The script checks release/configuration/native architecture and invokes only
the kernel's external-module `modules` target. It does not install packages,
compile/install a DT overlay, run DKMS, install the module or load anything.
`make`/compiler errors must be resolved before continuing; save the complete
output. Do not bypass failed preflight checks. Cross compilation is out of scope.
The first vermagic field must be `6.6.98-vendor-sun60iw2`. If compilation reveals
a target API difference, review that error before changing compatibility code.

## 3. Temporary load/bind in terminal A

Load the standard IIO dependency if it is a module (leave shared dependencies
loaded afterward); inspect the `depends` output for any others. A built-in IIO
subsystem needs no separate load. If `insmod` reports unknown symbols, inspect
`sudo dmesg` and resolve the dependency/configuration rather than forcing a load.

```bash
sudo modprobe industrialio
sudo ./driver/orange-pi-zero3w-session.sh "$PWD/driver/fusion_hat.ko"
```

The session refuses an existing `fusion_hat` module (including old versions
without the new parameter), any client at `0-0017`, or another existing Fusion
HAT client. It never unbinds/replaces existing ownership. It checks the target
kernel, architecture, adapter and module vermagic, then runs `insmod` on this
local file with `disable_auto_shutdown=1`. Only after verifying the parameter is
`Y` does it write `fusion_hat 0x17` to bus 0's `new_device`. It verifies the
client's driver link and firmware sysfs interface. The old `i2c_bus` parameter
does not select/create a client and is not used here.

`disable_auto_shutdown` is a read-only, load-time boolean, default `N`, preserving
the previous Raspberry Pi default. `Y` skips automatic shutdown polling/actions
and guards the exported shutdown execution function. Battery and USR-button
work continue. This does **not** disable independent HAT firmware protections,
physical power removal, or shutdown initiated elsewhere on the host. It does
not make testing the power button safe or implement orderly power-off.

## 4. Read existing interfaces in terminal B

Wait at least one second for the periodic battery update, then:

```bash
cat /sys/module/fusion_hat/parameters/disable_auto_shutdown
readlink -f /sys/bus/i2c/devices/0-0017/driver
cat /sys/class/fusion_hat/fusion_hat/version
cat /sys/class/fusion_hat/fusion_hat/firmware_version
cat /sys/class/power_supply/fusion-hat/voltage_now
cat /sys/class/power_supply/fusion-hat/status
cat /sys/class/power_supply/fusion-hat/capacity
watch -n 0.2 cat /sys/class/fusion_hat/fusion_hat/button
```

Success: parameter `Y`, driver link `/sys/bus/i2c/drivers/fusion_hat`, firmware
`1.0.2`, plausible nonzero battery voltage, and USR state `0` released / `1`
pressed. `voltage_now` is **microvolts** (divide by 1,000,000 for volts),
`status` is `Charging`, `Discharging` or `Full`, and `capacity` is an estimated
percentage using the driver's 6.4–8.4 V linear model, not a measured fuel gauge.
Missing files, zeros or stale values do not count as a successful battery read;
inspect `sudo dmesg` for I2C errors. Existing error handling can leave cached
battery values after failed reads.

For input events, find the event node by its actual name, not a fixed number:

```bash
for event in /sys/class/input/event*; do
  if [ "$(cat "$event/device/name")" = 'Fusion HAT USR Custom Button' ]; then
    echo "/dev/input/${event##*/}"
  fi
done
sudo evtest /dev/input/eventN
```

Replace `eventN` with the printed node. If `evtest` is unavailable, the sysfs
state check still works; obtain an input-event reader separately. Press/release
the **USR** button and expect `EV_KEY / BTN_0` values `1` / `0` with sync events.
The driver polls at 20 ms. This is **not the power button**. No Python package
installation or venv change is required for this milestone. Runtime-user input
permissions can be addressed after these root-assisted checks.

Do not run raw I2C transfers/scans while the kernel driver owns the device, and
do not write PWM controls or use the actuator examples in the main driver README.

## 5. Cleanup

Stop `watch`/`evtest` in terminal B. Press Enter in terminal A (or send Ctrl-C).
The session deletes only its created bus-0 client and then unloads only its
created module, in that order. It checks for changed ownership/other bound
clients and stops if cleanup would affect them. It also attempts cleanup on
ordinary failures, EOF, SIGINT, SIGTERM and SIGHUP. It does not unload shared
dependencies, modify boot files or remove a pre-existing module/client.

```bash
ls -ld /sys/bus/i2c/devices/0-0017 /sys/module/fusion_hat
```

Both should be absent after successful cleanup. If the session was killed with
SIGKILL or reported incomplete cleanup, inspect ownership and logs first. Only
if these are still the resources this session created, with no other clients or
users, the manual equivalents are:

```bash
printf '0x17\n' | sudo tee /sys/bus/i2c/devices/i2c-0/delete_device
sudo rmmod fusion_hat
```

Never apply those manual commands to resources that predated the session. Do
not use forced module removal. Removal has the output side effects described
above; keep actuators disconnected throughout.

Record the checkout SHA, build output, `uname -a`, parameter/binding/readouts,
USR events, relevant `dmesg`, and cleanup result. The hardware milestone remains
pending until these pass on the Orange Pi Zero 3W; a cloud check against another kernel cannot
establish vendor-kernel or Raspberry Pi compatibility.
