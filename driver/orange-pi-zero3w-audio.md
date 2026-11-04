# Experimental Orange Pi Zero 3W / Armbian audio

This explicitly opt-in audio experiment builds on merged PR #3 at `v1` commit
`eab2929`. Target only: Orange Pi Zero 3W / A733, aarch64, Armbian 26.8.3 /
Ubuntu resolute, kernel `6.6.98-vendor-sun60iw2`, directly mounted Fusion HAT+.
Read the [core guide](orange-pi-zero3w-install.md) first. Orange Pi audio remains
pending operator validation. Missing chip part numbers do **not** block this
bounded experiment: the working Pi implementation and live reference data
establish one operating profile, without identifying the chips.

## Evidence and selected profile

The operator's Raspberry Pi Zero 2 W reference runs Raspberry Pi OS, kernel
`6.18.50+rpt-rpi-v8`, with `dtoverlay=sunfounder-fusionhat`. Loaded modules include
Google Voice HAT codec, RPi simple soundcard and BCM2835 I2S. Both ALSA directions
enumerate on `sndrpigooglevoi`, device 0. Initial `hw_params` files were closed;
that alone established no active stream configuration.

The operator then captured directly from `hw:CARD=sndrpigooglevoi,DEV=0`, with
the speaker off. ALSA reported exact rate 48000, S32_LE, two channels, msbits 32.
The WAV contains 240000 frames, 4-byte samples, 2 channels and 48000 Hz: five
seconds. Reported normalized channel levels were:

| Capture channel | Peak | RMS |
| --- | --- | --- |
| 0 | 0.3223552704 | 0.0051210992 |
| 1 | 0.3142986298 | 0.0137149733 |

This establishes actual nonzero capture data. Intelligibility, physical clock
measurements and true microphone precision have not been reported. Keep both
channels; levels do not identify a chip or prove which channel should be used.

The fork's [Pi overlay](sunfounder-fusionhat-overlay.dts) connects that software
implementation. In the inspected Raspberry Pi 6.18 source family at revision
`a4985478de14b3a83c8e04fed68377c1bdae735d`:

- [The codec driver][pi-codec] restricts both directions to 48000 / S32_LE /
  two channels. Chip-related names inside that driver do not identify this HAT.
- [The machine driver][pi-machine] selects I2S, normal bit/frame polarity and
  CPU-provided clocks, with BCLK ratio sample width x 2.
- The existing [`fusion_hat/device.py`](../fusion_hat/device.py) clock check also
  generates 48000 / 32-bit / stereo playback for the Fusion Pi path.

That inspected revision is 6.18.60, not the reference binary's exact revision;
the live reference capture independently confirms the selected PCM profile.

| Property | Selected configuration |
| --- | --- |
| PCM | 48000 Hz, S32_LE, two interleaved channels; 32-bit container, not an ADC precision claim |
| Serial format | I2S, normal bit/frame polarity, one-bit I2S offset |
| Clock provider | Host for BCLK and LRCLK |
| Serial frame | Two 32-bit slots, 64 BCLKs per frame |
| Expected clocks | LRCLK 48 kHz, BCLK 3.072 MHz: derived settings, not measured waveforms |
| External MCLK | Not routed; Pi audio uses four serial signals, PB4 is excluded |
| Playback | Identical, low-amplitude samples in both slots |
| Capture | Both slots, unchanged |
| Speaker | Existing MCU sysfs switch, command `0x31`; no new host GPIO mapping |

## Routing and remaining unknowns

Orange Pi's [vendor wiring table][opi-header] gives physical header positions;
its [Zero 3W pinctrl][opi-pins] gives the alternate functions. SunFounder's
[audio table][hat-audio] and [DIP drawing][hat-switch] identify SCLK/18, WS/19,
AI/20 and AO/21:

| Physical pin | A733 pin/function | HAT signal | Host direction |
| --- | --- | --- | --- |
| 12 | PB5 / `i2s0_bclk` | SCLK | Output |
| 35 | PB6 / `i2s0_lrck` | WS | Output |
| 38 | PB8 / `i2s0_din0` | AI / microphone | Input |
| 40 | PB7 / `i2s0_dout0` | AO / speaker | Output |
| 7 | PB4 / `i2s0_mclk` or `pwm0_0` | HAT digital GPIO4 | Untouched |

The [general HAT table][hat-conflict] instead calls GPIO20 speaker enable.
The dedicated audio table, AI/20 drawing and working reference capture support
using pin 38 as input. Never use it as an output-enable GPIO. No chip identity
follows from these facts. The [vendor schematic folder][opi-schematic] was
reachable but its PDF could not be retrieved; no schematic inspection is claimed.

The corrected operator check used `find` under sudo. PB4..PB8 all reported
**UNCLAIMED** on the Orange Pi. The earlier non-root wildcard expansion error
was not evidence that pinctrl support was missing. Registered DAIs were
`sunxi-snd-plat-i2s`, `sunxi-snd-codec-hdmi`, `snd-soc-dummy-dai`.

| Remaining question | Effect on the bounded experiment |
| --- | --- |
| Actual installed DTB symbols, enabled overlays and conflicts | Must pass board-side offline `check` before installation |
| Live PB5..PB8 ownership | Must be free at first install; reported free and rechecked |
| Audio DIP routing / speaker connection | Operator verifies while audio is inactive; switch position cannot be established in software |
| Chip identities, full rate range, true ADC precision | Not prerequisites for this one reproduced profile |
| Capture slot/downmix choice or amplifier channel selection | Retain both capture slots and duplicate playback samples |
| Actual Orange Pi clocks, audible playback and intelligible capture | Experimental outcomes, not already established facts |
| Pi GPIO16 SDMODE dependency on this HAT revision | Not transplanted; test existing MCU control with clocks running, stop on failure |

The Pi codec delays GPIO16 amplifier enable by 5 ms. That does not establish a
need to claim Orange Pi physical pin 36. Here playback starts muted, waits for
PCM RUNNING plus 50 ms, then enables the existing MCU switch. Normal completion
mutes during trailing silence before PCM closure. This is a bounded sequencing
choice to validate, not a datasheet guarantee. Do not increase amplitude or add
other pins/clocks to work around an initial failure.

## Implementation and ownership

The [Armbian family at the reported build revision][armbian-source] selects
Orange Pi's `orange-pi-6.6-sun60iw2` kernel source. Inspected vendor revision:
`2ac08e8c7cdc28abbdc5c9a9dd812f887ae9c79f`; exact installed-binary commit
provenance remains unconfirmed. Relevant source paths are:

- [Zero 3W DTS][opi-machine]: old AC101 machine enabled, I2S0 platform disabled,
  header pinctrl groups.
- [SoC DTSI][soc-i2s]: labelled platform/machine nodes and TX/RX DMA.
- [SUNXI machine parser][mach-parse] and [utilities][mach-utils]: vendor
  `soundcard-mach,*` properties, clock/slot setup and dummy endpoint fallback.
- [SUNXI I2S driver][i2s-dai]: playback/capture support and [zero-MCLK handling][i2s-mclk].

The new overlay disables `i2s0_mach`, enables I2S0 with PB5..PB8 only, and adds
one clean root machine `/fusion-hat-audio` using the existing dummy transport
endpoint. It is not a model/identification of physical codecs. A fresh machine
avoids inherited AC101 references/routes: overlay `/delete-property/` would not
remove those base-tree properties. No competing sound card uses I2S0.

Mainline SIMPLE_CARD/MAX98357A/ICS43432/DMIC options are not needed for this
transport experiment. No kernel module/rebuild is introduced. The dummy
endpoint advertises broad PCM capabilities; **only 48000 / S32_LE / stereo is
covered here**, fixed by the explicit test helper. Card appearance alone is not
hardware success. I2S is not the separate SUNXI PDM/DMIC path.

The installer compiles/applies all enabled vendor/user overlays offline in a
temporary directory. A property comparison rejects changes outside the audio
platform, old machine status and new audio nodes/symbols. MCU, HDMI, PB4 and
unrelated DT state must remain unchanged. It refuses busy/unknown pins, an
unowned enabled controller or audio nodes, competing I2S0 references, missing
tools/symbols/DAIs, unsuitable MCU policy and edited/unowned managed files.

It owns only the audio DTBO, one udev rule, one speaker-permission helper, its
boot token, dedicated `fusionhat-audio` group/membership and manifest/recovery
snapshots under `/var/lib/fusion-hat-audio`. Files/rules precede boot activation;
the audio token is added last. Identical reinstall is a no-op; changed inputs
require removal and reboot first. Rollback/removal preserves unrelated changes,
`i2c0`/`fusion-hat-core`, both MCU flags, core files and HDMI configuration.

It never installs packages, upgrades the kernel, runs Pi repair/audio setup,
changes global ALSA defaults, loads/unbinds modules, applies live overlays,
starts streams or reboots. Existing built-in SUNXI drivers need no new initramfs
content. Remove experimental audio before changing kernel/image; compatibility
with an updated kernel is not promised.

Udev grants only PCM0 playback/capture and card control below the new machine,
plus the speaker switch after verifying the MCU binding, both flags and active
audio DT node. Rules cover coldplug/recreation, without blanket audio/input/I2C
access. P0 and other controls retain their existing grants. ALSA card indices
may change; the helper discovers the DT-owned card and selects its explicit ID.
HDMI configuration is preserved, not its numeric enumeration order.

## 1. Prerequisites and offline check

Stop audio users and disconnect actuators. Verify the HAT's four audio DIP
connections while inactive; remove power before changing switches/wiring.
Keep the speaker off. From the reviewed checkout on the Orange Pi:

```bash
git fetch origin
git switch experiment/orange-pi-zero3w-audio
git pull --ff-only
uname -m
uname -r
cat /boot/armbianEnv.txt
cat /sys/module/fusion_hat/parameters/{disable_auto_shutdown,restrict_controls}
readlink -f /sys/bus/i2c/devices/0-0017/driver
cat /sys/class/fusion_hat/fusion_hat/speaker
command -v dtc fdtoverlay aplay arecord udevadm
sudo find /sys/kernel/debug/pinctrl -maxdepth 2 -type f \
  -name pinmux-pins -exec grep -HE 'PB[4-8]([^0-9]|$)' {} +
sudo cat /sys/kernel/debug/asoc/dais
runtime_user=$(id -un)
sudo ./driver/orange-pi-zero3w-audio-install.py check --runtime-user "$runtime_user"
```

Require the target kernel, both flags `Y`, existing Fusion binding, speaker
cache `0`, free PB5..PB8 and successful offline composition. Debugfs must
already expose ownership/DAI data; absent data is not assumed safe. Resolve
failed prerequisites explicitly. No kernel headers or Python packages are
installed or needed for this overlay-only step.

## 2. Install, review, reboot explicitly

Only after `check` succeeds:

```bash
sudo ./driver/orange-pi-zero3w-audio-install.py install --runtime-user "$runtime_user"
cat /boot/armbianEnv.txt
```

Review the token/output, then reboot through the normal operator procedure and
log in again for group membership. No script reboots or tests audio automatically.

## 3. Enumeration and non-root access

```bash
id -nG
cat /sys/module/fusion_hat/parameters/{disable_auto_shutdown,restrict_controls}
readlink -f /sys/bus/i2c/devices/0-0017/driver
cat /sys/class/fusion_hat/fusion_hat/firmware_version
aplay -l
arecord -l
stat -Lc '%a %U %G %n' /sys/class/fusion_hat/fusion_hat/speaker
for card in /sys/class/sound/card[0-9]*; do
  if [ "$(readlink -f "$card/device/of_node")" = /sys/firmware/devicetree/base/fusion-hat-audio ]; then
    cat "$card/id"
    number=${card##*card}
    stat -c '%a %U %G %n' "/dev/snd/controlC$number" "/dev/snd/pcmC${number}D0p" "/dev/snd/pcmC${number}D0c"
  fi
done
venv_python="${VIRTUAL_ENV:?Activate the intended venv}/bin/python"
"$venv_python" examples/orange_pi_zero3w_audio.py check
```

Expect one new card with both directions, HDMI still available, MCU binding and
flags intact, and named PCM/control nodes and speaker `660 root fusionhat-audio`.
The vendor may call the card `sndi2s0`; selection uses actual DT ancestry/ID.
The helper rejects other cards and requires non-root access. Its `check` action
opens no PCM and enables no speaker. Python uses only the standard library;
no pip, audio server, voice packages or default-device replacement is needed.
For failures preserve the output and relevant logs:

```bash
sudo dmesg | grep -Ei 'asoc|snd|i2s|dai|ac101|fusion|defer'
```

## 4. Capture first, speaker off

Speak a short phrase during the five-second capture:

```bash
recording=$(mktemp -d)
printf 'Recording directory: %s\n' "$recording"
"$venv_python" examples/orange_pi_zero3w_audio.py capture --output "$recording/microphone.wav"
```

The fixed command is `arecord -D <discovered-hw-PCM> -t wav -f S32_LE -r 48000
-c 2 -d 5`. No plug resampling/downmix hides mismatches. The helper creates the
file exclusively, preserves raw stereo samples, requires five seconds and
reports channel peak/RMS. Negotiation errors or zero/missing data need inspection;
nonzero data alone is not intelligibility. Partial recordings are retained.

## 5. Short low-volume playback, then recording review

The explicit tone uses 440 Hz, 1% digital peak (-40 dBFS), identical slots,
300 ms silent pre-roll and a trailing mute window:

```bash
"$venv_python" examples/orange_pi_zero3w_audio.py playback
cat /sys/class/fusion_hat/fusion_hat/speaker
"$venv_python" examples/orange_pi_zero3w_audio.py replay --input "$recording/microphone.wav"
cat /sys/class/fusion_hat/fusion_hat/speaker
```

The helper requires PCM RUNNING with `owner_pid` equal to its own `aplay` child,
waits another 50 ms, enables the existing speaker
switch, then attempts mute before normal PCM closure. Replay bounds peak to
1% and retains both slots. Expect an audible short tone, intelligible speech on
replay, and speaker readback `0` after each action. Do not raise amplitude to
bypass a failed initial result. The retained WAV can also be heard on a known
working system to check intelligibility independently of Orange Pi playback.

Normal completion, exceptions, Ctrl-C and SIGTERM attempt speaker-off and stop
only the helper's child process. Child sessions prevent terminal Ctrl-C from
stopping clocks before the parent attempts mute. Readback is cached driver
state, not a physical measurement. SIGKILL, power loss, failed I2C writes and
external processes can defeat cleanup; stop tests and remove speaker power if
uncertain. Do not use raw I2C, global mixer repair or Pi setup.

Report audible playback, intelligibility, both channel levels, errors and
cleanup separately. A scope/analyzer can compare expected BCLK/LRCLK with the
reference (3.072 MHz / 48 kHz); no waveform measurement is inferred from software.
After another explicit reboot, repeat non-stream checks and a bounded test to
verify audio persistence.

## 6. Removal/recovery

Stop audio users and deliberately leave the speaker off; remove audio only:

```bash
printf '0\n' | sudo tee /sys/class/fusion_hat/fusion_hat/speaker
sudo ./driver/orange-pi-zero3w-audio-install.py uninstall
cat /boot/armbianEnv.txt
```

Reboot explicitly. Uninstall removes no live device/module; existing sessions
retain group access until logout/reboot. Only owned files/token and added group
membership are removed; later unrelated group members are preserved. Edited
files or changed group identity cause refusal before destructive cleanup.
Snapshots remain at `/var/lib/fusion-hat-audio/armbianEnv.before.*`; never restore
a whole snapshot over later unrelated edits.

If boot fails, use the console or mount the boot filesystem elsewhere and remove
only `fusion-hat-audio` from `overlays=`. Preserve `i2c0`, `fusion-hat-core` and
both core policy flags. Boot with actuators disconnected, then uninstall audio.
The old deferred AC101 warning may return with the original configuration;
removal does not repair that separate pre-existing state.

## Validation limits

The overlay compiled with DTC 1.7.2 and applied to a small DT fixture and a
Zero 3W DTB built from the inspected vendor source, alongside the MCU overlay.
Protected-property comparisons passed. That source-built DTB is **not** the
exact installed Armbian artifact; board-side `check` validates that artifact
and its actual enabled overlays. No new kernel module is built here.

The operator then ran `check --runtime-user` from commit `fbd3f02` on the
Orange Pi target. It reported that audio prerequisites and offline composition
passed against the installed DTB and enabled overlays, with no installation or
activation performed. This clears that preflight gate; runtime audio binding,
permissions and signal behaviour remain untested.

Focused tests cover real composition, MCU/HDMI/PB4 preservation, conflicts,
missing prerequisites, MCU policy gates, repeat installation, ownership,
rollback, scoped grants, explicit card/profile selection and mocked stream
cleanup including Ctrl-C. The existing suite is run too. Cloud Python is
3.12/x86_64; target Python 3.14/audio remains pending. No hardware installer,
PCM stream or speaker operation was run in the cloud.

Reference capture and Orange Pi pin/DAI results above are operator-provided
hardware evidence. Target audio binding/permissions, audible playback,
intelligible capture, physical timing/cleanup and persistence remain pending.
The Pi implementation/installer/overlay are unchanged; Raspberry Pi hardware
regression testing was not performed. No STT/TTS, wake-word, runtime, servo,
automatic shutdown or general-purpose host GPIO changes are included.

[opi-header]: https://github.com/orangepi-xunlong/wiringOP/blob/a7921ca2f5c124203be10d3418db70a2e15f3c10/gpio/readall.c#L797
[opi-pins]: https://github.com/orangepi-xunlong/linux-orangepi/blob/2ac08e8c7cdc28abbdc5c9a9dd812f887ae9c79f/arch/arm64/boot/dts/allwinner/sun60i-a733-orangepi-zero3w.dts#L910
[opi-machine]: https://github.com/orangepi-xunlong/linux-orangepi/blob/2ac08e8c7cdc28abbdc5c9a9dd812f887ae9c79f/arch/arm64/boot/dts/allwinner/sun60i-a733-orangepi-zero3w.dts#L2986
[soc-i2s]: https://github.com/orangepi-xunlong/linux-orangepi/blob/2ac08e8c7cdc28abbdc5c9a9dd812f887ae9c79f/arch/arm64/boot/dts/allwinner/sun60iw2p1.dtsi#L4771
[hat-audio]: https://github.com/sunfounder/fusion-hat/blob/4bd1018ad5a70ee113160536f969cdadd9f22918/docs/source/hardware/interfaces.rst#L337
[hat-switch]: https://github.com/sunfounder/fusion-hat/blob/4bd1018ad5a70ee113160536f969cdadd9f22918/docs/source/hardware/img/audio_fh_v0.png
[hat-conflict]: https://github.com/sunfounder/fusion-hat/blob/4bd1018ad5a70ee113160536f969cdadd9f22918/docs/source/hardware/interfaces.rst#L186
[opi-schematic]: https://drive.google.com/drive/folders/199S7SkbqVDjMpPT7tkcbBX6kmbfnz94r
[armbian-source]: https://github.com/armbian/build/blob/fd4ebfd1e/config/sources/families/sun60iw2.conf#L26
[mach-parse]: https://github.com/orangepi-xunlong/linux-orangepi/blob/2ac08e8c7cdc28abbdc5c9a9dd812f887ae9c79f/bsp/drivers/sound/platform/snd_sunxi_mach.c#L343
[mach-utils]: https://github.com/orangepi-xunlong/linux-orangepi/blob/2ac08e8c7cdc28abbdc5c9a9dd812f887ae9c79f/bsp/drivers/sound/platform/snd_sunxi_mach_utils.c#L450
[i2s-dai]: https://github.com/orangepi-xunlong/linux-orangepi/blob/2ac08e8c7cdc28abbdc5c9a9dd812f887ae9c79f/bsp/drivers/sound/platform/snd_sunxi_i2s.c#L1680
[i2s-mclk]: https://github.com/orangepi-xunlong/linux-orangepi/blob/2ac08e8c7cdc28abbdc5c9a9dd812f887ae9c79f/bsp/drivers/sound/platform/snd_sunxi_i2s.c#L825
[pi-codec]: https://github.com/raspberrypi/linux/blob/a4985478de14b3a83c8e04fed68377c1bdae735d/sound/soc/bcm/googlevoicehat-codec.c#L137
[pi-machine]: https://github.com/raspberrypi/linux/blob/a4985478de14b3a83c8e04fed68377c1bdae735d/sound/soc/bcm/rpi-simple-soundcard.c#L124
