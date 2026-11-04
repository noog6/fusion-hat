# Experimental Orange Pi Zero 3W / Armbian audio

This explicitly opt-in audio experiment builds on merged PR #3 at `v1` commit
`eab2929`. Target only: Orange Pi Zero 3W / A733, aarch64, Armbian 26.8.3 /
Ubuntu resolute, kernel `6.6.98-vendor-sun60iw2`, directly mounted Fusion HAT+.
Read the [core guide](orange-pi-zero3w-install.md) first. Audible manual and
updated-helper playback are operator-confirmed at the bounded 5% test level.
Microphone intelligibility and physical/interruption cleanup remain pending.
Missing chip part numbers do
**not** block this bounded experiment: the working Pi implementation and live reference data
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

The operator also reports audible speaker playback on the reference Pi through
its existing embodied-runtime application. A standalone command reproducing
that result has not yet been supplied. Read-only inspection of the application's
[WAV playback path][reference-playback] at commit
`4216a0db49d24746583a8dffe5796a2e54bbce8f` shows MCU speaker enable **before**
`aplay --quiet`, using the configured ALSA default. The original experiment
opened an explicit hardware PCM and enabled the MCU switch after RUNNING.
The existing [Fusion Python functions](../fusion_hat/device.py) write the same
speaker sysfs `1`/`0` values. Actual installed library behaviour and ALSA routing
on the reference remain to be checked; the inspected application revision is
not a claim about its deployed revision. The later Orange Pi manual success
below supplies direct target evidence; no application integration is added.

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

On the Orange Pi target, the operator then generated a persistent three-second
WAV: 48000 Hz, signed 32-bit little-endian, identical stereo channels, 440 Hz
from 0.5 to 2.5 seconds with 20 ms boundary fades. The initial 1% peak version
was barely audible. Multiplying those samples by five produced the 5% test.
Without sudo, the operator enabled the MCU speaker attribute, ran
`aplay -v audio-test-5pct.wav`, and disabled it through shell EXIT cleanup.
**Playback at 5% was explicitly confirmed audible; cached speaker readback was
`0` afterward.** The default route resolved to a Plug PCM backed by hardware
card `fusionhat-audio`, device 0, RW_INTERLEAVED / S32_LE / 2 channels / exact
rate 48000. This confirms that manual path, not a global-default configuration
change or a fixed card index. The operator subsequently replayed that persistent
5% tone through the updated helper's explicit HAT PCM and confirmed it was
quiet but audible. This validates that playback path without attributing the
original failure to any single changed variable.

| Property | Selected configuration |
| --- | --- |
| PCM | 48000 Hz, S32_LE, two interleaved channels; 32-bit container, not an ADC precision claim |
| Serial format | I2S, normal bit/frame polarity, one-bit I2S offset |
| Clock provider | Host for BCLK and LRCLK |
| Serial frame | Two 32-bit slots, 64 BCLKs per frame |
| Expected clocks | LRCLK 48 kHz, BCLK 3.072 MHz: derived settings, not measured waveforms |
| External MCLK | Not routed; Pi audio uses four serial signals, PB4 is excluded |
| Test playback | Identical slots; 5% digital peak (-26.02 dBFS), three-second faded tone |
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
| Actual Orange Pi clock waveforms | Not measured; derived settings only |
| Audible playback | Manual and updated explicit-device helper 5% tone confirmed quiet but audible |
| Intelligible capture | Nonzero PCM data reported; listening confirmation pending |
| Pi GPIO16 SDMODE dependency on this HAT revision | Not transplanted; manual Orange Pi playback succeeded with the existing MCU control |

The Pi codec delays GPIO16 amplifier enable by 5 ms. That does not establish a
need to claim Orange Pi physical pin 36. The updated helper follows the successful
manual sequence: arm cleanup, enable the existing MCU switch, start `aplay`, then
mute after it finishes. This sequence is verified for the manual experiment,
not established as universally necessary. The original helper also differed in
amplitude, duration and buffering; no single variable is identified as the cause
of its reported silence. Use the confirmed bounded 5% level for this experiment.
No new pins, clock routing or global ALSA changes follow from this result.

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

After login, return to the checkout and reactivate the intended venv. The
reported test checkout is `~/workarea/fusion-hat`; adjust that path if yours is
elsewhere. A script path relative to the home directory will not find the example.

```bash
cd ~/workarea/fusion-hat
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
After a stream closes, PB5..PB8 can report `io_disabled` while remaining owned
by `2532000.i2s0_plat`. This is the overlay's sleep state: the matching
[ASoC PCM cleanup][pcm-sleep] selects sleep when the component becomes inactive,
and [PCM open][pcm-open] selects default again. An idle pin dump therefore
does not establish whether PB7 was muxed to I2S during playback.
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

## 5. Generate a persistent test WAV, play it, then review the recording

The hardware-independent `generate` action uses only Python's standard library.
It requires no HAT, root, sysfs, ALSA device or audio program. It creates a new
file exclusively and prints its path and signal parameters. Choose a fresh
path; existing files are refused. This directory is retained in your home:

```bash
audio_test_dir=$(mktemp -d "$HOME/fusion-hat-audio-test.XXXXXX")
audio_test="$audio_test_dir/audio-test-5pct.wav"
"$venv_python" examples/orange_pi_zero3w_audio.py generate --output "$audio_test"
"$venv_python" examples/orange_pi_zero3w_audio.py playback --input "$audio_test"
cat /sys/class/fusion_hat/fusion_hat/speaker
```

The WAV is 144000 frames / three seconds at 48000 Hz / S32_LE / stereo. Both
channels are identical. A 440 Hz tone runs from 0.5 to 2.5 seconds, with 20 ms
linear fades; digital peak is bounded to 5% (-26.02 dBFS). This matches the
successful signal parameters; the original operator-generated bytes are not
claimed identical to this new generator. The helper and standalone commands
can now use the **same persistent file**. `playback` without `--input` generates
the same signal in a temporary file instead.

Before enable, the helper validates the fixed WAV format, duration (at most
five seconds), peak (at most 5%), speaker-off state and that the playback PCM
is closed. After arming cleanup it writes speaker `1`, then starts
`aplay -v -D <discovered-hw-PCM> <file>` with normal ALSA buffering. It keeps the
`RUNNING`/`owner_pid` check as a diagnostic of its own child, rather than a
condition for delayed speaker enable. A busy preflight PCM is refused;
ownership/open failures after launch mute and stop only its child. The check
does not lock out a concurrent external process; stop other audio users first.
The WAV is not rewritten, normalized or padded for `playback --input`.

For an optional standalone comparison with identical bytes, use the explicit
card ID printed by the helper's `check` (the reported target ID is
`fusionhataudio`; substitute if different). Do not select card index 0 or change
the system default. This bounded shell sequence mirrors the successful manual
test; keep other audio users stopped:

```bash
hat_pcm=hw:CARD=fusionhataudio,DEV=0
(
  speaker=/sys/class/fusion_hat/fusion_hat/speaker
  [ "$(cat "$speaker")" = 0 ] || exit 1
  trap 'printf "0\n" > "$speaker"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  printf '1\n' > "$speaker" || exit 1
  aplay -v -D "$hat_pcm" "$audio_test"
)
cat /sys/class/fusion_hat/fusion_hat/speaker
```

Review the **existing raw microphone recording separately**, using the retained
`$recording` directory from step 4. Replay prepares a temporary monitor copy
normalized to a maximum 5% peak, without added pre-roll, so the five-second
duration limit also applies to playback. It does not modify the raw WAV:

```bash
"$venv_python" examples/orange_pi_zero3w_audio.py replay --input "$recording/microphone.wav" --channel 0
cat /sys/class/fusion_hat/fusion_hat/speaker
```

`--channel 0` duplicates that captured channel into both output slots for
monitoring; `--channel 1` selects the other one. This does not change the raw
stereo recording or establish a hardware downmix policy. Without the option,
replay preserves stereo, which can be silent if the amplifier uses the empty
slot. The operator confirmed the persistent 5% tone was quiet but audible
through the updated helper. Report whether the recorded phrase is intelligible and
speaker readback is `0` after each action. A tone does not validate the mic.
The latest capture's channel 0 peak/RMS were 0.339319/0.015674. Normalizing its
peak to 5% reduces all samples by about 16.6 dB and gives an estimated monitor
RMS of 0.00231 (0.23% of full scale). This explains why replay may be much quieter
than the tone; it does not establish whether speech was captured. Listen to the
unchanged raw WAV on a known working computer/audio device to check microphone
intelligibility independently of this bounded HAT monitor level. Keep that
outcome separate from audible tone playback; no automatic gain increase follows.

Normal completion, exceptions, Ctrl-C and SIGTERM attempt speaker-off and stop
only the helper's child process. Child sessions prevent terminal Ctrl-C from
reaching the child before the parent attempts mute. On normal completion, mute
follows `aplay` completion, as in the successful manual test; continuous clocks
through mute are not promised. Readback is cached driver
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
permissions and signal behaviour were not tested by that check.

The operator subsequently installed from the checkout, rebooted and reported:

- Both MCU flags `Y`, Fusion binding and firmware `1.0.2` persisted.
- `fusionhataudio` enumerated for playback and capture; HDMI remained listed.
- The intended user had `fusionhat-audio` membership. Speaker, controlC0,
  PCM0 playback and capture nodes were `660 root fusionhat-audio` after reboot.
- Non-root capture negotiated 48000 / S32_LE / stereo and returned five seconds.
  Channel 0 peak/RMS were 0.314636/0.014814; channel 1 was zero.
- The original 1% short-tone/replay helper completed with cached speaker `0`,
  but both were reported silent. This historical result motivated the persistent
  test file and changes to sequencing, level, duration and buffering; it does
  not isolate any one variable as the cause.
- The later manual three-second faded tone was barely audible at 1% and
  explicitly audible at 5%. MCU enable preceded `aplay -v`; shell EXIT cleanup
  disabled it, with cached readback `0`. The default Plug PCM reached the
  intended HAT at exact 48000 / S32_LE / stereo. This is operator hardware
  evidence for manual playback; the subsequent helper result is recorded below.
- The operator then used the updated helper's `playback --input` with that
  persistent 5% WAV and explicitly confirmed the tone was quiet but audible.
- A new non-root five-second capture returned channel 0 peak/RMS
  0.339319/0.015674 and channel 1 0.314299/0.014056. Both slots had data.
  Selected-channel-0 replay reached the explicit HAT PCM at exact 48000 /
  S32_LE / stereo, reported its own RUNNING owner and completed with speaker
  cached readback `0`. That replay was not heard; speech intelligibility
  remains unverified. Nonzero levels are not a substitute for listening.
- Mixer controls reported clock delay 0, clock keep off, loopback off, RX sync
  off, TX hub off and TX trigger bypass off. These match normal DMA operation;
  no playback-volume/mute control was exposed. The operator reported that the
  audio switch position matches the working Raspberry Pi reference.
- After the streams closed, PB4 remained unclaimed and PB5..PB8 reported
  `io_disabled`, owned by `2532000.i2s0_plat`. This matches the sleep-state
  selection described above; active-stream pin state remains to be checked.
- The identical 1% tone was also reported silent through the existing
  Raspberry Pi driver, despite operator-confirmed application playback there.
  The subsequent Orange Pi manual success is stronger target playback evidence;
  no further reference-system change is a prerequisite for this helper update.

The startup `No 'sound-dai' property` message is consistent with the intentional
dummy endpoint fallback. Missing clock-post-delay/default-format warnings and
duplicate debugfs entries were also reported, including duplicate entries for
HDMI. Card registration/capture and later manual audible playback succeeded;
these messages alone do not identify the original helper failure.

Focused tests cover real composition, MCU/HDMI/PB4 preservation, conflicts,
missing prerequisites, MCU policy gates, repeat installation, ownership,
rollback, scoped grants, explicit card/profile selection, persistent WAV signal
properties, generation without hardware, overwrite refusal and bounded replay.
Stream tests mock PCM/speaker/subprocesses, including creation failure, nonzero
exit, ownership failure and Ctrl-C/SIGTERM cleanup; real signals are delivered
only to the mocked test process. These are not hardware interruption tests.
The full suite passed 73 tests, including 30 audio cases, with no skips. Actual
CLI generation and refusal to overwrite also passed without hardware. Python
compilation, nine Bash guide blocks (syntax only) and whitespace/naming checks
passed. Cloud Python is 3.12/x86_64; target Python 3.14
capture/stream completion has the operator results above. No hardware installer,
PCM stream or speaker operation was run in the cloud.

Reference capture and Orange Pi pin/DAI results above are operator-provided
hardware evidence. Target binding, permission metadata and stream completion
after reboot and audible manual/updated-helper playback are reported above.
Intelligible microphone speech, physical waveforms/cleanup, hardware
interruption cleanup and permission reapplication after later audio-device
recreation remain pending. Speaker-off cached readback after replay is reported;
continue checking it after each action. A cached value is not physical cleanup.
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
[pcm-sleep]: https://github.com/orangepi-xunlong/linux-orangepi/blob/2ac08e8c7cdc28abbdc5c9a9dd812f887ae9c79f/sound/soc/soc-pcm.c#L717
[pcm-open]: https://github.com/orangepi-xunlong/linux-orangepi/blob/2ac08e8c7cdc28abbdc5c9a9dd812f887ae9c79f/sound/soc/soc-pcm.c#L798
[pi-codec]: https://github.com/raspberrypi/linux/blob/a4985478de14b3a83c8e04fed68377c1bdae735d/sound/soc/bcm/googlevoicehat-codec.c#L137
[pi-machine]: https://github.com/raspberrypi/linux/blob/a4985478de14b3a83c8e04fed68377c1bdae735d/sound/soc/bcm/rpi-simple-soundcard.c#L124
[reference-playback]: https://github.com/noog6/embodied-runtime/blob/4216a0db49d24746583a8dffe5796a2e54bbce8f/embodied_runtime/voice.py#L679
