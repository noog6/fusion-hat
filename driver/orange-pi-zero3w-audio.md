# Orange Pi Zero 3W / Armbian: audio design investigation

**Status: Stage 1 blocked; no audio installer or overlay is supplied.**
This report starts at `v1` commit `eab29290b2e1895446b3461198657baed87f6920`,
which merged PR #3. It targets only Orange Pi Zero 3W / Allwinner A733,
aarch64, Armbian 26.8.3 / Ubuntu resolute, kernel
`6.6.98-vendor-sun60iw2`, with a directly mounted Fusion HAT+.
Read the [core installation guide](orange-pi-zero3w-install.md) first.

The operator has verified persistent MCU binding, non-root Python readouts,
USR events and bounded P0 servo movement. Those results do not validate audio.
Keep `i2c0`, `fusion-hat-core`, `disable_auto_shutdown=Y` and
`restrict_controls=Y` intact. Leave the speaker disabled during investigation.
This change provides read-only inspection commands and the evidence needed to
finish the design; it does not change boot configuration, permissions, ALSA
defaults, the MCU installer, or the Raspberry Pi path.

## Header routing and the documentation conflict

Orange Pi's own [wiringOP header table][opi-header] names the physical pins.
Its [Zero 3W device tree][opi-pins] supplies the I2S0 alternate functions.
SunFounder's [dedicated audio table][hat-audio] and [DIP-switch drawing][hat-switch]
identify SCLK/18, WS/19, AI/20 and AO/21. Combining these primary sources gives:

| Physical header pin | A733 pin/function | HAT connection | Direction relative to the host |
| --- | --- | --- | --- |
| 12 | PB5 / `i2s0_bclk` | SCLK, Raspberry Pi GPIO18 | Clock output, if the host is clock provider |
| 35 | PB6 / `i2s0_lrck` | WS, Raspberry Pi GPIO19 | Clock output, if the host is clock provider |
| 38 | PB8 / `i2s0_din0` | AI / microphone, Raspberry Pi GPIO20 | Input |
| 40 | PB7 / `i2s0_dout0` | AO / speaker, Raspberry Pi GPIO21 | Output |
| 7 | PB4 / `i2s0_mclk` or `pwm0_0` | HAT digital GPIO4 | No audio assignment established |

Pin 7 is labelled PWM0-0 in wiringOP; the device tree maps that function to
PB4. This is not permission to claim it for audio. No HAT MCLK connection or
requirement was established. Exclude PB4 from both active and sleep audio
pinctrl unless circuit evidence proves it is needed and available.

SunFounder's [general pin table][hat-conflict] instead labels GPIO20 as
speaker enable. That conflicts with its dedicated audio table and AI/20 switch
label. Treat pin 38 as the microphone-data candidate, **never as an output
enable GPIO**. The drawing and audio table support that interpretation, but
the exact board revision's schematic/electrical routing remains unverified.
Do not move the audio DIP switches while audio is active.

The Orange Pi schematic folder is publicly linked at
[the vendor download location][opi-schematic]. The folder was reachable, but
the schematic PDF itself could not be retrieved in this investigation; the
official board page/wiki also failed to load. The table above is corroborated
by vendor source, not a claim that the schematic or physical wiring was tested.
The SoC pin names alone would not establish physical header positions.

## HAT components and speaker control

The inspected SunFounder documentation/repository identifies an I2S speaker
path and a MEMS microphone but supplies no verified amplifier/microphone part
numbers or board schematic/BOM. The operator also has no schematic/BOM.
There is consequently no source-confirmed basis here for identifying the
devices as MAX98357A, ICS43432, INMP441, or the Google Voice HAT hardware.
Overlay names are software configurations, not component identification.

Still needed from SunFounder or revision-specific circuit/component evidence:

- Actual amplifier and microphone part numbers and HAT revision.
- I2S versus other serial format, sample precision, supported rates, BCLK/LRCLK
  polarity and ratio, and whether either component requires MCLK.
- Microphone left/right slot selection, inactive-slot behaviour, and how its
  data reaches pin 38 through the DIP switch.
- Amplifier channel selection and the electrical enable/power path controlled
  by the MCU, including any required clock/enable sequencing.

The existing [driver speaker interface][speaker] accepts only 0/1 and sends
MCU command `0x31`; it updates a software cache after a successful SMBus write.
`speaker_show()` returns that cache, not an amplifier measurement. Probe writes
0, and `restrict_controls=Y` makes the attribute root-writable. This is separate
from ALSA registration and audio samples; setting it to 1 cannot create a
sound card. The MCU command establishes the software control mechanism, not
the unprovided amplifier circuit or its timing requirements. Use the driver's
interface for later deliberate speaker control, without raw I2C access or an
invented host GPIO mapping.

## Vendor-kernel design findings

The [Armbian family configuration at the reported build revision][armbian-source]
selects `orangepi-xunlong/linux-orangepi`, branch
`orange-pi-6.6-sun60iw2`. Inspected source revision:
`2ac08e8c7cdc28abbdc5c9a9dd812f887ae9c79f`. This identifies the appropriate
vendor source family; it does **not** prove the installed binary was built
from that exact commit. Preserve package/build provenance before implementation.

| Source at that revision | Finding and consequence |
| --- | --- |
| [`arch/arm64/boot/dts/allwinner/sun60i-a733-orangepi-zero3w.dts`][opi-machine] | `i2s0_mach` is enabled and associated with AC101; `i2s0_plat` is disabled. The CPU/codec children and AC101 routes are explicit. Do not create a second machine competing for I2S0 or enable the unrelated AC101. |
| [`arch/arm64/boot/dts/allwinner/sun60iw2p1.dtsi`][soc-i2s] | I2S0 has TX and RX DMA and labelled platform/machine nodes. A future overlay must use verified symbols/paths, not copied numeric phandles. |
| [`bsp/drivers/sound/platform/snd_sunxi_mach.c`][mach-parse] | Uses `soundcard-mach,*` properties, parses CPU before codec, and preserves `-EPROBE_DEFER`. Its `simple_dai_link_of failed` message does not identify one unique cause. |
| [Same machine driver, clock setup][mach-clock] | Programs PLL, BCLK ratio, serial format and TDM slots from the vendor properties. This is not the mainline `simple-audio-card` binding. |
| [`bsp/drivers/sound/platform/snd_sunxi_mach_utils.c`][mach-utils] | Parses slot count/width, CPU MCLK settings and named component endpoints. The driver source and matching DTS are the inspected vendor binding contract; no corresponding vendor schema was located in the searched binding directories. |
| [`bsp/drivers/sound/platform/snd_sunxi_i2s.c`][i2s-dai] | Advertises both playback and capture, multiple sample formats and up to 16 channels. Those transport capabilities are not HAT capabilities. |
| [Same I2S driver, MCLK handling][i2s-mclk] | A zero requested MCLK disables MCLK output. Whether that is correct for this HAT still depends on its components. |
| [`sound/soc/soc-utils.c`][dummy] | ASoC's dummy endpoint advertises broad playback/capture capabilities without modelling the physical devices' constraints. |

The machine driver can fall back to `snd-soc-dummy` when no usable codec
reference/name is provided. A resulting ALSA card would demonstrate software
registration only. It would not establish correct channel mapping, timing,
microphone data or intelligible capture. This report supplies no dummy-codec
overlay as a substitute for those missing facts.

The operator reports SUNXI PCM/MACH/COMMON/I2S/COMPONENTS and AC101 built in,
while SIMPLE_CARD, MAX98357A, ICS43432 and DMIC drivers are disabled. Therefore:

- A generic `simple-audio-card` overlay is not supported by the reported config.
- Disabled mainline codec options alone do not prove a kernel rebuild is needed:
  the vendor machine/I2S transport already exists. Conversely, transport and a
  dummy endpoint do not prove device-tree changes alone are sufficient.
- An I2S microphone is distinct from a PDM microphone using the SUNXI DMIC
  controller. Do not enable/reroute DMIC based on the word "digital".
- The current 4 slots x 16 bits is inherited AC101 configuration. Even when its
  total BCLK count equals 2 x 32, sample placement/channel mapping can differ.
  Neither configuration is established for the HAT by the current evidence.

The operator's live-tree/log/deferred-device observations agree with incomplete
AC101 configuration. They do not prove that association is the sole cause of
every parsing error. Armbian has a [similar AC101-disable patch for the 4 Pro][ac101-patch];
it changes a different board and is not an audio solution for the Zero 3W.

## Read-only operator inspection

Run on the stated target with actuators disconnected. These commands do not
load/unbind devices, change pinmux, enable the speaker, start streams, install
packages or edit boot configuration. Capture failures as evidence too.

```bash
uname -m
uname -r
cat /proc/version
dpkg-query -W -f='${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\n' 'linux-image*sun60iw2*'
cat /boot/armbianEnv.txt
cat /sys/module/fusion_hat/parameters/{disable_auto_shutdown,restrict_controls}
readlink -f /sys/bus/i2c/devices/0-0017/driver
cat /sys/class/fusion_hat/fusion_hat/speaker
command -v dtc fdtget fdtoverlay aplay arecord amixer
aplay -l
arecord -l
id -nG
ls -l /dev/snd
```

Require both MCU flags `Y` and the existing Fusion binding. The reported
speaker cache value is `0`. Missing ALSA tools are a prerequisite to resolve
explicitly; do not run the Raspberry Pi audio setup to obtain them.

```bash
dtb=/boot/dtb/allwinner/sun60i-a733-orangepi-zero3w.dtb
sha256sum "$dtb"
for symbol in i2s0_plat i2s0_mach i2s0_cpu i2s0_codec i2s0_pins_a i2s0_pins_b i2s0_pins_c i2s0_pins_d i2s0_pins_e; do
  printf '%s: ' "$symbol"
  fdtget -t s "$dtb" /__symbols__ "$symbol"
done
headers=$(readlink -f "/lib/modules/$(uname -r)/build")
grep -E '^(CONFIG_SND_SOC_SUNXI_(PCM|MACH|COMMON|I2S|COMPONENTS)|CONFIG_SND_SIMPLE_CARD|CONFIG_SND_SOC_(AC101|MAX98357A|ICS43432|DMIC|SUNXI_DMIC))=' \
  "$headers/include/config/auto.conf"
```

A missing configuration line does not itself establish the running binary's
configuration; compare the matching `/boot/config-$(uname -r)` if available.
Missing symbols require inspecting the actual DTB rather than substituting
live numeric phandles. Retain that DTB and the enabled vendor/user overlays for
offline composition tests once a design is supported.

Read pin ownership and ASoC registration if debugfs is already available:

```bash
sudo grep -E 'PB[4-8]([^0-9]|$)' /sys/kernel/debug/pinctrl/*/pinmux-pins
sudo cat /sys/kernel/debug/asoc/dais
sudo cat /sys/kernel/debug/devices_deferred
sudo dmesg | grep -Ei 'asoc|snd|i2s|dai|ac101|defer'
```

Do not mount debugfs or change permissions automatically. If these files are
absent/unreadable, ownership remains unknown; no output is not proof that pins
are free. Check enabled overlays, GPIO consumers, PWM/UART/TWI alternatives
and live ownership. MCU TWI0 uses PB2/PB3, but that does not exclude other users
of PB4..PB8. The vendor DTS contains alternative PWM functions on PB6/PB7 and
TWI on PB7/PB8; inactive pinctrl definitions are not themselves active owners.

## Implementation gate and subsequent operator handoff

The smallest next step is to obtain revision-specific HAT circuit/component
evidence and datasheets, plus the read-only target provenance/pin-ownership
output above. Confirm the audio DIP routing and current installed DTB. No
kernel rebuild has been shown necessary; no compatible installable audio design
has yet been established. An executable overlay with guessed parts/timing would
skip the requested first stage.

Once that evidence is available, the next change must:

1. Select a supported clock/slot/sample/channel configuration and model both
   physical endpoints' constraints. Decide whether the existing transport is
   sufficient or an additional small codec/component driver is required.
   Stop for explicit review if a kernel rebuild is required.
2. Supply a separate opt-in audio overlay and owned check/install/remove
   transaction. Disable or replace the AC101 machine association without two
   users of I2S0; preserve HDMI, MCU policy, unrelated overlays and permissions.
   Compile/apply against the retained target DTB and existing overlays offline.
3. Validate conflicts, missing dependencies/symbols, repeated installation,
   boot-token preservation and rollback ownership with focused tests. Install
   only reviewed local artifacts, activate the boot token last, never activate
   devices or reboot automatically. Removal/recovery removes only audio-owned
   files/token and retains `i2c0`/`fusion-hat-core` and both MCU policy flags.
4. After explicit reboot, enumerate `aplay -l` and `arecord -l`, select the
   verified HAT card ID/device explicitly, and verify both streams and non-root
   permissions. Grant only that card's required ALSA nodes and the Fusion
   speaker attribute if additional permissions are needed; no blanket group
   expansion or global ALSA default replacement.
5. Provide a short digitally attenuated test sound, deliberately enable the
   speaker through its existing sysfs interface, play as the runtime user and
   disable the speaker on completion/error/interrupt. Record a short spoken
   phrase with the documented supported rate/format/channels and explicit HAT
   capture device. Verify intelligibility and audible playback separately.
   Exact stream commands await those supported parameters; arbitrary 48 kHz,
   S16_LE, mono defaults are not established by an ALSA card listing.
6. Recheck persistence, cleanup and MCU operation. Record actual audible
   playback and intelligible capture only after operator results arrive.

`fusion_hat/scripts/setup_fusion_hat_audio.sh` was inspected, not run. Its
explicit card selection and separate speaker control are useful concepts, but
it loads the Pi `googlevoicehat-soundcard` overlay, installs PulseAudio, replaces
`/etc/asound.conf` defaults, changes mixer controls and starts audio tests.
Even `--skip-test` does not make it a read-only prerequisite check. None of
those assumptions/actions belongs in this stage. No STT/TTS, wake-word,
voice-assistant, runtime, servo, shutdown or general host-GPIO work is included.

## Validation limits

Primary source files, vendor pin tables, HAT drawings/docs and installer logic
were inspected in the cloud. No target DTB was available locally; overlay tools
were absent there. No overlay was compiled or applied, no kernel/audio module
was built, and no board-side inspection, installer, playback or capture was run
in the cloud. The pre-existing operator audio observations are not new tests.
This documentation change has no installation/rollback code to unit-test;
its shell examples are syntax-checked without executing hardware commands.
Playback, intelligible microphone capture and audio persistence remain pending.

[opi-header]: https://github.com/orangepi-xunlong/wiringOP/blob/a7921ca2f5c124203be10d3418db70a2e15f3c10/gpio/readall.c#L797
[opi-pins]: https://github.com/orangepi-xunlong/linux-orangepi/blob/2ac08e8c7cdc28abbdc5c9a9dd812f887ae9c79f/arch/arm64/boot/dts/allwinner/sun60i-a733-orangepi-zero3w.dts#L910
[opi-machine]: https://github.com/orangepi-xunlong/linux-orangepi/blob/2ac08e8c7cdc28abbdc5c9a9dd812f887ae9c79f/arch/arm64/boot/dts/allwinner/sun60i-a733-orangepi-zero3w.dts#L2986
[soc-i2s]: https://github.com/orangepi-xunlong/linux-orangepi/blob/2ac08e8c7cdc28abbdc5c9a9dd812f887ae9c79f/arch/arm64/boot/dts/allwinner/sun60iw2p1.dtsi#L4771
[hat-audio]: https://github.com/sunfounder/fusion-hat/blob/4bd1018ad5a70ee113160536f969cdadd9f22918/docs/source/hardware/interfaces.rst#L337
[hat-switch]: https://github.com/sunfounder/fusion-hat/blob/4bd1018ad5a70ee113160536f969cdadd9f22918/docs/source/hardware/img/audio_fh_v0.png
[hat-conflict]: https://github.com/sunfounder/fusion-hat/blob/4bd1018ad5a70ee113160536f969cdadd9f22918/docs/source/hardware/interfaces.rst#L186
[opi-schematic]: https://drive.google.com/drive/folders/199S7SkbqVDjMpPT7tkcbBX6kmbfnz94r
[speaker]: https://github.com/noog6/fusion-hat/blob/eab29290b2e1895446b3461198657baed87f6920/driver/speaker.c#L17
[armbian-source]: https://github.com/armbian/build/blob/fd4ebfd1e/config/sources/families/sun60iw2.conf#L26
[mach-parse]: https://github.com/orangepi-xunlong/linux-orangepi/blob/2ac08e8c7cdc28abbdc5c9a9dd812f887ae9c79f/bsp/drivers/sound/platform/snd_sunxi_mach.c#L343
[mach-clock]: https://github.com/orangepi-xunlong/linux-orangepi/blob/2ac08e8c7cdc28abbdc5c9a9dd812f887ae9c79f/bsp/drivers/sound/platform/snd_sunxi_mach.c#L90
[mach-utils]: https://github.com/orangepi-xunlong/linux-orangepi/blob/2ac08e8c7cdc28abbdc5c9a9dd812f887ae9c79f/bsp/drivers/sound/platform/snd_sunxi_mach_utils.c#L450
[i2s-dai]: https://github.com/orangepi-xunlong/linux-orangepi/blob/2ac08e8c7cdc28abbdc5c9a9dd812f887ae9c79f/bsp/drivers/sound/platform/snd_sunxi_i2s.c#L1680
[i2s-mclk]: https://github.com/orangepi-xunlong/linux-orangepi/blob/2ac08e8c7cdc28abbdc5c9a9dd812f887ae9c79f/bsp/drivers/sound/platform/snd_sunxi_i2s.c#L825
[dummy]: https://github.com/orangepi-xunlong/linux-orangepi/blob/2ac08e8c7cdc28abbdc5c9a9dd812f887ae9c79f/sound/soc/soc-utils.c#L187
[ac101-patch]: https://github.com/armbian/build/blob/fd4ebfd1e/patch/kernel/archive/sun60iw2-opi-vendor/0004-orangepi4pro-disable-unpopulated-ac101-audio.patch
