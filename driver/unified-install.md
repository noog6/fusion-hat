# Unified Fusion HAT installer

`install.sh` is the common entry point in **noog6/fusion-hat**, based on `v1`.
It validates the platform before package installation or configuration changes.
It never reboots, opens an audio stream, enables the speaker, moves actuators,
downloads models or calls a live provider. `--check` checks software and state;
physical playback/capture are separate operator acceptance results.

## Supported scope

| Path | Gates and limits |
| --- | --- |
| Raspberry Pi | Raspberry Pi OS on Pi 4 Model B / Pi 5 Model B (existing guide) or Zero 2 W (operator reference), Pi device-tree identity and kernel, `aarch64` or `armv7l`. The existing Makefile/DKMS driver path is reused. New dispatcher hardware regression testing remains required. |
| Orange Pi | Only Zero 3W / Allwinner A733, directly mounted Fusion HAT+, aarch64, Armbian **26.8.3**, Ubuntu **resolute**, kernel **6.6.98-vendor-sun60iw2**, existing Python **3.14.4** venv. The existing core/audio installers remain authoritative. This is the existing experimental target, not general Armbian/Orange Pi support. |

Debian ancestry does not establish Raspberry Pi OS: 64-bit Pi OS needs its
`/etc/rpi-issue` origin or explicit Pi OS name in addition to board/kernel data.
Conflicting, missing or unsupported evidence fails closed, including when
`--platform` is explicit. Detection prints a board/OS/image/architecture/kernel
summary. Full OS/Armbian metadata and DT properties go into the diagnostic log
(or the error output if validation fails before a log is created).

The inspected vendor Zero 3W source at
[`2ac08e8`](https://github.com/orangepi-xunlong/linux-orangepi/blob/2ac08e8c7cdc28abbdc5c9a9dd812f887ae9c79f/arch/arm64/boot/dts/allwinner/sun60i-a733-orangepi-zero3w.dts)
inherits model `sun60iw2` and uses `xunlong,orangepi-4-pro`, `arm,sun60iw2p1`
with `board=A733`. These misleading vendor strings are accepted **only together
with** the exact Zero 3W Armbian metadata, kernel and
`fdtfile=allwinner/sun60i-a733-orangepi-zero3w.dtb` /
`overlay_prefix=sun60i-a733`. They do not establish support for 4 Pro. A named
Zero 3W DT identity also needs the same image/boot gates. Existing full
preflights still verify the installed vendor DTB, live TWI, ownership and policy.
An unrecognized image needs separate review; do not edit metadata to bypass a gate.

## Commands and source provenance

Use your reviewed checkout (local changes are included):

```bash
# Raspberry Pi: system interpreter, intended user-site Python packages by default.
sudo bash ./install.sh

# Orange Pi: select the existing runtime venv and account explicitly.
sudo bash ./install.sh --runtime-user "$(id -un)" \
  --venv /absolute/path/to/embodied-runtime/.venv
```

The script's source directory wins; a piped script invoked from a checkout's
root also uses that checkout. No pull, reset, checkout deletion, replacement or
build cleanup occurs there. Root creates a private, root-owned source snapshot
for privileged scripts, platform imports and kernel builds. A separate runtime-user
copy is used only for Python packaging, imports and audio checks; root never
executes that copy or promotes its build outputs into the privileged tree. Both
directories are removed on exit. Only code/package/build inputs are copied, not the
repository's `.git`, work directories or venv. Kernel modules are built as root
in the protected tree, with a clean build environment. Existing headers, every
symlink hop and every directory used to reach them must be root-owned, with no
group/world writable directories or regular files. Resolve a failure with an
administrator-reviewed header installation, not by giving the runtime user
access to privileged builds.
The installer reports Git HEAD and local modification status. The private
diagnostic log also records the snapshot's SHA256 and full detection evidence.
Keep that log for review.
Existing owned drivers are retained on rerun; a new source snapshot does not
silently upgrade an already installed kernel module. Replacing an Orange Pi
installation requires its documented uninstall/reboot procedure.

Standalone downloaded/piped bootstrap needs existing `bash`, `git`, `python3`
and HTTPS Git access. It does not install prerequisites before validation:

```bash
curl -fsSL https://raw.githubusercontent.com/noog6/fusion-hat/v1/install.sh \
  | sudo bash -s -- --runtime-user "$(id -un)" --venv /absolute/path/to/.venv

# Select a reviewed fork/ref explicitly, while running outside a checkout:
curl -fsSL https://raw.githubusercontent.com/noog6/fusion-hat/v1/install.sh \
  | sudo bash -s -- --repo noog6/fusion-hat --ref REVIEWED_COMMIT_SHA \
      --runtime-user "$(id -un)" --venv /absolute/path/to/.venv
```

Bootstrap defaults to **noog6/fusion-hat**, ref `v1`. `--repo OWNER/REPO` and
`--ref BRANCH|TAG|SHA` override `FUSION_HAT_REPOSITORY` and legacy
`FUSION_HAT_BRANCH`, respectively. Set variables inside sudo's environment if
using them; sudo commonly strips caller variables. Local checkout invocations
reject these source overrides instead of switching trees. The old
`INSTALLER_BRANCH` shared-utility override is no longer used: neither path
sources remote shared installer code.

Bootstrap fetches the ref once, peels it to a commit (including annotated tags),
checks out that immutable commit SHA in a private
`mktemp` directory, and uses that revision's dispatcher, scripts and artifacts
for the invocation. Cleanup runs on exit/signals. Resume commands pin that SHA,
so a moving branch cannot change the resumed installation. A failed bootstrap
can be retried; no user checkout is removed. Local resume commands use the
original absolute checkout path; retain/review that checkout until completion.

`fusion_hat update` also stays on this fork. Run it from the intended account:
it passes its active venv interpreter automatically, or accepts an explicit
selector. Its exit status is the installer's status (including staged exit 3);
a failed download never starts the installer.

```bash
/absolute/path/to/.venv/bin/fusion_hat update
fusion_hat update --python /absolute/path/to/.venv/bin/python --runtime-user "$(id -un)"
# Equivalent explicit environment selector:
fusion_hat update --venv /absolute/path/to/.venv --runtime-user "$(id -un)"
```

## Stage selection and activation

No stage flags means **`--core --audio`**. Supplying any stage flag selects only
the listed stages; add `--voice` explicitly if desired.

| Option | Behavior |
| --- | --- |
| `--core` | Driver/boot configuration and Python library. Orange Pi uses `build-module.sh`, its existing transaction installer and `install-core-python.sh`. Prepared matching vendor headers and verified `i2c0` are prerequisites, not guessed apt packages. |
| `--audio` | Platform audio hardware configuration, identity and non-root permissions. Orange Pi delegates to the existing overlay installer; no Python voice dependencies or audio server are installed. Pi uses the existing script's new non-actuating mode, configuring only named `fusion_hat` ALSA PCMs, preserving defaults/stack. |
| `--voice` | Installs Fusion core and pinned voice dependencies in the selected venv, verifies their required dependency graph, existing user service, PortAudio enumeration and routing. Requires active core/audio hardware first. |
| `--check` | Read-only state/software verification (temporary offline DT composition is allowed). No apt/pip install, service start or default changes. Missing installation/activation remains incomplete. Run an installation command without `--check` to complete missing files. |
| `--runtime-user USER` | Intended non-root account. Defaults to the invoking user through `SUDO_USER`; direct root login must specify it. No first-user/session guessing. |
| `--venv /absolute/path` or `--python /absolute/venv/bin/python` | Existing user-writable venv; mutually exclusive. Required for Orange Pi core Python and all voice setups. Interpreter symlinks retain venv identity. No venv is created in root's home. |
| `--headers /path` | Prepared current-kernel Orange Pi headers/build tree; default `/lib/modules/6.6.98-vendor-sun60iw2/build`. |
| `--install-pulseaudio` | With `--voice`, permit PulseAudio + utilities only when no existing audio stack is detected. Not a general Armbian requirement. |
| `--set-audio-defaults` | With `--voice`, explicitly select the verified HAT microphone/source and sink, saving previous routing first. Without it, different defaults remain unchanged and voice routing is reported incomplete. |
| `--platform auto\|raspberry-pi\|orange-pi` | Optional selection; still must pass detection and compatibility checks. |
| `--verbose` | Also show detailed commands, package/build output and device enumeration on the console. These details are always saved in a private diagnostic log. Resume commands retain this option. |
| `--plain-text`, `--skip-test`, `--test` | Accepted legacy arguments. Output is plain text and stream tests are always operator actions. `--test` prints the acceptance-guide location; it does not play/record. |

Fresh Orange Pi flow:

1. Validate platform, intended user/venv, core prerequisites/conflicts; build in
   the snapshot, install owned core files and core-only Python. If the core is
   inactive: **“reboot required, then rerun this command”**, with a complete resume
   command retaining selected options. Audio preflight is not bypassed.
2. Operator reviews the manifests/boot settings, reboots explicitly and logs in.
   Rerun the printed command. Core install recognizes identical owned files,
   binding and both policy flags. Audio preflight validates the active core,
   pin ownership, speaker-off state, DAIs and offline DT composition. It installs
   only owned audio files/token. Inactive audio requires another explicit reboot.
3. After reboot/login, rerun again. Identical hardware files are retained; the
   helper checks the DT-owned audio card and scoped non-root access without
   opening a PCM. Python packaging is checked/reinstalled by the existing helper
   on installation runs. Optional voice setup can now proceed.

These are **two separate reboot boundaries** on a fresh Orange Pi: first core,
then audio. Repeating the command before the requested reboot safely detects
the same pending activation; it cannot advance voice setup. An expected pause
prints, for example:

```text
[1/3] Core: preparing files and Python environment
Core: files present; active binding and both MCU policy checks passed.
[2/3] Audio: preparing audio hardware configuration
REBOOT REQUIRED: Audio files installed; reboot required, then rerun this command after login.
Waiting for activation before continuing: voice.
This is an expected pause. No reboot was performed.
Next: run sudo reboot, log in again, then rerun:
<complete command with the same revision, user, Python and selected options>
```

Normal output shows stages, activation/check results, routing identity/recovery
and any whole-venv dependency warning. Compiler, pip and individual command
details go to the printed root-only `/tmp/fusion-hat-install-*.log` (mode 600).
Use `sudo cat /tmp/fusion-hat-install-PRINTED_NAME.log` to inspect it, or
`sudo tail -f /tmp/fusion-hat-install-PRINTED_NAME.log` in another session during
a long build. Add `--verbose` to mirror details on the console. The log is retained
on exit; `/tmp` may be cleared on reboot, so save it beforehand if needed. Errors
include actionable details and the complete resume command. Expected reboot
pauses do not print transaction-failure/rollback warnings.

Pi driver installation uses `make ... BOOT_ONLY=1 install` to defer live module
load/unload until the operator's reboot. The Makefile's standalone defaults and
DKMS behavior remain available. Existing installed Pi modules are retained on
rerun. Pi Python installation runs as the intended user and retains the existing non-voice dependency metadata
(including GPIO/display/audio libraries); the voice application dependency is
deferred to `--voice` in the explicitly selected venv. Without an explicit venv,
packages use the system interpreter with `--user`, and the CLI is
`~/.local/bin/fusion_hat`; open a new login shell for the standard user-local PATH.
No new Python library or voice package is installed into root's home or globally.
The previous Pi OS `RPi.GPIO` system-package cleanup remains a privileged step;
venv selection confines Python package cleanup to that venv. Completion is
generated as the runtime account under
`~/.local/share/bash-completion/completions/fusion_hat`; user-generated shell
code is never copied into a system completion directory. After activation, non-streaming audio setup preserves the existing stack
and defaults. The standalone legacy `fusion_hat speaker setup` remains an
explicit operator command and can open streams/change global audio configuration;
it is not the unified Orange Pi path.

Exit `0`: selected files and software checks passed. Exit `3`: staged
installation/activation/routing or voice checks remain incomplete. Exit `1/2`:
invalid arguments, unsupported platform, prerequisite or transaction failure.
A later failure does not roll back successful earlier stages. Existing platform
transactions retain their own rollback, manifests, conflict checks and recovery.
A successful software check never asserts physical playback/capture success.

## Optional voice setup

After core/audio activation, from the reviewed checkout:

```bash
sudo bash ./install.sh --voice --runtime-user "$(id -un)" \
  --python /absolute/path/to/embodied-runtime/.venv/bin/python

# Only after choosing to change this user's defaults:
sudo bash ./install.sh --voice --set-audio-defaults --runtime-user "$(id -un)" \
  --python /absolute/path/to/embodied-runtime/.venv/bin/python
```

The root process inspects installed packages and user systemd units. A working
PulseAudio-compatible server (including PipeWire's Pulse server) is reused.
Existing units can be started as the intended user. An incomplete existing stack
requires explicit repair; it is not replaced. If no stack is present, the
observed Orange Pi solution can be requested with **`--voice
--install-pulseaudio`**. That permits `apt-get install pulseaudio
pulseaudio-utils` and user `daemon-reload` / start of `pulseaudio.socket` and
`pulseaudio.service`. No user service is enabled for unattended boot or lingering
by this installer. A missing DBus/user session prints remaining session commands
and the complete resume command; log in as that user and rerun. Never run
`sudo systemctl --user` or configure root's PulseAudio instance.

Before voice imports, the existing core Python helper installs the local Fusion
package with `--no-deps`, plus `evdev` and build tools, into the selected voice
venv on **both** platforms. This also handles a Pi whose earlier core installation
used the system interpreter/user site. The helper retains the exact Orange Pi
Python 3.14.4 check; its explicit Pi mode accepts the selected supported Python.
The voice checker also installs `packaging` to evaluate dependency requirements.
The selected interpreter installs
`sunfounder-voice-assistant` at inspected upstream commit
**`32d2dae40d47fd6c4fe4180db28cd47e83efd851`**. Its metadata declares
`piper-tts`, `vosk`, `pyaudio`, `sounddevice`, `edge-tts>=6.0`. This pins the
application source, not every transitive dependency. Each installation saves
`pip freeze` plus the source revision under
`~/.local/state/fusion-hat/voice-*.txt`; checks verify the installed VCS revision.
No model or provider objects are created. Host dependencies for particular TTS
engines are still operator choices; inspect upstream metadata/engine requirements
before installing them. Native PyAudio builds may need `portaudio19-dev` and
headers matching the selected Python; read the actual compiler diagnostic and
`apt-cache policy` rather than substituting mismatched development packages.

Fusion's existing broad dependency metadata includes Pi GPIO and display
packages, even for its core-only `--no-deps` installation. Core + voice uses
Fusion's sysfs/evdev core and the pinned voice application; it does not require
those optional GPIO/display modules on either managed hardware path. The checker
verifies Fusion, `evdev`, `packaging`, the voice application and its **entire active
transitive dependency graph**, including version constraints, Python/platform
markers and requested extras. Missing/conflicting requirements in that graph
fail the voice stage. The installed voice VCS revision and imports are also checked.

A whole-venv `pip check` still prints its exact diagnostics, including optional
Fusion modules and unrelated applications, but those diagnostics do not fail an
otherwise complete core/voice stage. Review them before using the affected
features or other applications. A successful voice stage does not claim that
the entire venv is conflict-free. Do not install the entire Pi GPIO/display list
on Orange Pi just to suppress unrelated metadata diagnostics.

Routing discovers the HAT by DT ancestry/driver and read/write device access,
then corroborates Pulse endpoint properties against that actual ALSA card.
A name containing “fusion” alone is insufficient. Microphone selection excludes
monitor sources. No numeric ALSA/PortAudio IDs are used to select devices;
kernel card numbers only cross-check identity/permissions. Ambiguous endpoints
are refused. Default changes require `--set-audio-defaults`, do not change
volume/mute, and record previous + selected names in a private
`~/.local/state/fusion-hat/routing-*.json` receipt **before** either write.
Recovery commands are printed even if the second write fails. Recover as that
same user with the saved names:

```bash
pactl set-default-source SAVED_PREVIOUS_SOURCE_NAME
pactl set-default-sink SAVED_PREVIOUS_SINK_NAME
```

On the observed target the verified names were
`alsa_input.platform-fusion-hat-audio.stereo-fallback` and
`alsa_output.platform-fusion-hat-audio.stereo-fallback`; they are discovered,
not hardcoded as universal names. HDMI initially owned both defaults.
Changing the defaults does not promise to move already-open application streams;
restart the intended application explicitly after reviewing routing.

## Troubleshooting and recovery

- **Fresh image version:** the operator's reflash started with BSP/image/DTB/
  bootloader packages at 26.8.1, while separately installing 26.8.3 headers did
  not upgrade those packages. The validated baseline needs
  `armbian-bsp-cli-orangepizero3w-vendor`, `linux-image-vendor-sun60iw2`,
  `linux-dtb-vendor-sun60iw2`, `linux-headers-vendor-sun60iw2` and
  `linux-u-boot-orangepizero3w-vendor` at **26.8.3**, followed by an explicit
  reboot. Review `apt-cache policy` and an `apt-get -s install` simulation before
  selecting that cohort. A blanket upgrade can move outside the validated
  kernel/image. `/etc/os-release` may separately report Armbian 26.8.14;
  `/etc/armbian-release`, board, boot selection and kernel are still checked.
- **Writable generated vendor headers:** on the fresh 26.8.3 header package,
  five generated regular files were root:root mode 664. The root-build input
  guard correctly rejected them. The operator verified ownership and removed
  only group-write permission on these files (no automatic permission rewrite
  or validator bypass):

  ```bash
  header_tree=/usr/src/linux-headers-6.6.98-vendor-sun60iw2
  sudo stat -c '%F | mode=%a | owner=%U:%G | %n' \
    "$header_tree/include/config/"{kernel.release,auto.conf,auto.conf.cmd} \
    "$header_tree/include/generated/"{autoconf.h,rustc_cfg}
  # Only after confirming these are root:root regular files with mode 664:
  sudo chmod g-w -- \
    "$header_tree/include/config/"{kernel.release,auto.conf,auto.conf.cmd} \
    "$header_tree/include/generated/"{autoconf.h,rustc_cfg}
  sudo find "$header_tree" -xdev \
    \( ! -uid 0 -o \( ! -type l -perm /022 \) \) \
    -printf '%y %m %u:%g %p\n'
  ```

  That final audit was empty on the operator's image; the installer's full
  symlink-chain/ancestor checks still run. Investigate any other findings rather
  than applying recursive chmod/chown or weakening the trust checks. Package
  reinstall/header preparation may recreate generated files and need review.
- **Unsupported/conflicting platform:** save the printed detection evidence;
  compare `/etc/os-release`, `/etc/armbian-release`, DT root properties,
  `uname -m/-r` and boot DTB selection. Explicit platform flags cannot force it.
- **Core/header/overlay conflict:** use the [core guide](orange-pi-zero3w-install.md).
  Core manifests remain in `/var/lib/fusion-hat-core`. No forced module unload,
  unbind, policy relaxation, ownership takeover or kernel upgrade is performed.
- **Audio prerequisites or activation:** use the [audio guide](orange-pi-zero3w-audio.md).
  Preserve `i2c0`, `fusion-hat-core` and both policy flags. Audio manifests remain
  in `/var/lib/fusion-hat-audio`. Root-owned manifest access is why main checks
  run with sudo; user imports/access/server commands use the runtime account.
- **Permission/session changes:** log out/in after group changes. Inspect `id`,
  the scoped rules and the actual user service's logs; a running old user manager
  may retain old groups. Missing HAT endpoints are not fixed by broad audio/I2C
  group grants or changing numeric IDs.
- **`PulseAudio_Initialize: Can't connect to server` / sounddevice import:** in
  the runtime user's login session run `pactl info` and
  `systemctl --user status pulseaudio.socket pulseaudio.service pipewire-pulse.service`.
  On the observed PulseAudio setup the remaining commands were
  `systemctl --user daemon-reload` and
  `systemctl --user start pulseaudio.socket pulseaudio.service`. Check service
  availability before attributing initialization failure to PortAudio or
  rebuilding/reinstalling hardware. A different installed stack needs its own
  repair, not an automatic PulseAudio replacement.
- **Wrong route:** inspect `pactl -f json list sources` / `sinks` and
  `pactl get-default-source` / `get-default-sink`. Require the real mic rather
  than its monitor. Rerun with explicit routing opt-in or use the saved recovery
  receipt. Verify again after reboot/login.
- **Partial installation:** retain the complete error and rollback diagnostic.
  Do not reboot after an unresolved rollback failure. Use the owning installer's
  `uninstall` action documented in its guide; remove audio before core and reboot
  explicitly between replacement attempts. No unified destructive uninstall or
  whole-boot-snapshot restoration is added. Pi config changes preserve a
  `config.txt.fusion-hat.before` backup; inspect/merge it, preserving later edits.

## Operator acceptance still required

All installation checks are non-actuating: compatibility, manifests, policy /
MCU binding, imports/venv, audio identity/access, user service/routing and
PortAudio initialization/device enumeration. No playback, recording, model
download, live-provider call, motor/servo test or robot deployment is automatic.

**Raspberry Pi (new dispatcher regression acceptance):**

1. On each intended supported Pi/OS/kernel, save `git rev-parse HEAD`, detection,
   config backup and DKMS/module output. Use a checkout containing local work
   and verify it remains intact. Run `--core`; inspect boot overlay, installed
   module and DKMS registration before an explicit reboot/login.
2. Rerun default `--core --audio`, then the same command with `--check`. Require
   core imports, active `/sys/class/fusion_hat/fusion_hat/firmware_version`, the
   intended I2C driver binding, Pi audio DT/card identity and non-root PCM access.
   Confirm rerun retains the installed driver, no streams open, unrelated boot
   entries/audio stack/defaults remain intact, and completion works in a new
   shell. Exercise both system-interpreter/user-site compatibility (`~/.local/bin/fusion_hat`)
   and an explicit venv. Verify Python package and CLI ownership belongs to the
   runtime user, with no new root-owned venv/home configuration.
3. If voice is selected, inspect user service and defaults before opting into
   changes; require pinned dependency/PortAudio enumeration and verified real
   mic + sink. Run the explicit existing operator speaker test only after
   reviewing its behavior (`fusion_hat speaker test`, from the installed
   environment). Perform a deliberate microphone test using the stable ALSA card
   name and listen to it on a known working output. Record physical audibility,
   intelligibility, errors and cleanup separately. Repeat after reboot/login.

**Orange Pi (unified flow acceptance on the exact target):**

1. With prepared matching headers and verified `i2c0`, run default core/audio
   with the existing Python 3.14.4 venv. Confirm core-only files + Python, MCU
   policy file and initramfs checks, owned manifest, unchanged local checkout,
   and complete pinned/options-preserving resume command. Require the first
   reboot boundary before audio preflight. Reboot explicitly and log in.
2. Rerun, require active binding and flags `Y/Y`, then successful existing audio
   preflight/transaction and second reboot boundary. Reboot/login and rerun;
   require identical owned stages recognized and scoped card/speaker access.
   Run the same command with `--check`. Inspect both manifests and
   `/boot/armbianEnv.txt`; unrelated overlays/HDMI remain preserved.
3. For an already installed target, run `--core --audio --check` first and
   confirm no replacement/module upgrade. Run `--voice` with the selected venv,
   reuse the existing service, review routing, then explicitly opt into HAT
   defaults if desired. Check the saved prior-route receipt/recovery commands,
   exact pip diagnostics, pinned dependency and non-streaming PortAudio devices.
4. Follow [audio guide sections 4–5](orange-pi-zero3w-audio.md#4-capture-first-speaker-off)
   explicitly: capture with speaker off, generate/play a bounded persistent WAV,
   listen to selected-channel replay and verify cleanup. These operator actions
   use the helper's discovered card name, not numeric IDs. Record audibility,
   speech intelligibility and cached speaker-off readback separately; physical
   cleanup needs physical evidence. Use the existing core guide's readout/USR
   checks if desired; no motor/servo acceptance is part of this change.
5. Reboot/login and repeat service/routing/`--check`, then explicitly start the
   existing runtime for two spoken turns and return to wake listening if desired.
   Do not change that runtime's code/configuration here. Test wake activation
   after reboot and unattended startup before login separately; neither follows
   from a successful console-started voice session.

Existing operator evidence (not validation of this new dispatcher): manual and
helper audio tests worked on the stated Orange Pi target. Runtime **R11**
completed two spoken turns with clear audible output. **R13** detected alias
“oh be”, completed two voice turns and resumed wake listening. After reboot
and SSH login, **R15** completed another console-started two-turn session
without manual service startup or routing commands. Those earlier sessions did
not establish wake activation after reboot; the later fresh-image R2 below does
for an operator-started session after login. Unattended startup before login
remains **unverified**. “Opie” recognition has been inconsistent;
several-second playback overhead remains unresolved. Earlier
bounded helper recordings/replays were quiet; later runtime speech was reported
loud and clear. No single unmeasured change is asserted as the cause.

Subsequent operator checks of unified revision **0107a9e** on the existing
installation passed core/audio and voice `--check`, followed by a successful
embodied-runtime wake trigger and voice in both directions. The operator also
reported audio working after reboot. This does not establish wake activation
after reboot or unattended startup before login.

On the fresh-image attempt, after the package/header prerequisites above were
resolved, the operator built and installed core, rebooted, and passed the active
binding and both MCU policy checks. Audio files then installed successfully;
an immediate rerun recognized the identical files and correctly requested its
own reboot. After that reboot, the operator's installation log passed active
audio identity/access checks, installed and started PulseAudio for `pi`, installed
the pinned voice dependency in the selected venv, and passed imports and
PortAudio/PyAudio enumeration (20 devices). The explicit routing option saved
the previous defaults and selected the verified HAT microphone and speaker.
Whole-venv pip diagnostics remained for the omitted broad Fusion dependencies;
the required core/voice profile passed.

With the backed-up Opie configuration restored, the operator then started
`python main.py --config=./config/opie-agentic.toml`. Fresh-image **R1** on
2026-11-05 detected alias **“be”**, transcribed “can you hear me now”, completed
response playback, transcribed the follow-up “yes i can hear you too”, completed
a second response playback, closed at the two-turn limit and reacquired the
microphone for wake listening. The Vosk model downloaded during this explicitly
started runtime session, not during installation checks.

R1 still showed variable timing: the first response reported 2,275 ms of audio
and 4,540 ms of playback; the second reported 9,051 ms synthesis, 3,204 ms of audio
and 3,296 ms playback. These observations do not establish the cause of latency
or resolution of the earlier playback-overhead issue.

The operator then exited R1 cleanly: exit code 0, two voice turns, zero voice
failures, zero provider failures/runtime errors and no remaining non-daemon
threads. After `sudo reboot` and SSH login, the operator started the same command
for **R2**, with no manual audio-service startup or routing commands shown.
R2 detected **“opie”**, transcribed two requests, completed both response playback
calls, closed at the two-turn limit, resumed wake listening, and remained
running. The status report showed uptime 1 minute 55 seconds, corroborating the
reboot. **Wake activation and bidirectional voice after reboot/login are now
observed on this fresh installation**, with the runtime launched manually.
This single “opie” detection does not establish consistent recognition.

R2 synthesis took 468/475 ms; reported audio lengths were 2,554/3,111 ms and
playback elapsed times were 5,217/5,504 ms. Playback still exceeded clip duration
by about 2.4–2.7 seconds; the cause and resolution remain outside this change.

**Unattended startup before login remains unverified.** These are
operator-provided logs/results for 0107a9e, not hardware validation of the later
console/logging changes. This fresh-image installation also included the package
alignment and manually reviewed header permissions above; it does not establish
an untouched-image one-command installation.

Cloud tests mock privileged operations and hardware/session boundaries. Real
DTC fixture composition checks preserve MCU/HDMI/PB4, but do not validate the
installed target DTB or hardware. Raspberry Pi regression acceptance and the
remaining Orange Pi operator checks above remain required. Camera, robot body, wake matching,
TTS behavior and motor/servo behavior are outside this change.

## Repository verification

The implementation was checked on cloud Python 3.12 / x86_64: **134 unittest
cases passed, no skips**, including the existing core/audio transaction, rollback,
policy, permission, stream-boundary and real DTC fixture composition cases. New
cases exercise supported/rejected identities (including the vendor alias), both
dispatch paths, bootstrap repo/ref/defaults and cleanup, checkout preservation,
reboot/rerun stages, runtime user/venv handling, session/stack selection, verified
microphone/sink routing, recovery receipts and partial failures. Review regression
cases also cover separate privileged/user copies for a different selected account,
protected kernel build/output paths, untrusted-header rejection, fresh Pi voice
venv provisioning, optional-versus-required dependency failures and active
markers/extras, real annotated-tag peeling, and CLI interpreter/exit propagation.
Header regression fixtures explicitly model ownership/permissions in independent
temporary directories. They reject writable intermediate symlink directories
and runtime-owned hops, including the replaceable-Makefile layout, while
accepting trusted relative/absolute chains and preserving symlink-before-`..`
semantics. The complete suite also passes from a clean tracked-file export
without `work/`, beneath a writable parent directory.
Output regressions cover expected reboot pauses versus real failures, retained
resume options, private log permissions, quiet/verbose command output, captured
probe results, actionable child failures and visible routing recovery/warnings.
Privileged and hardware boundaries are mocked; these results are not board validation.

`bash -n`, ShellCheck 0.10, Python compilation and `git diff --check` passed.
Bash syntax checks passed for executable guide examples (36 blocks; existing
illustrative CLI/output blocks are excluded). Sphinx HTML built with the same
four existing diagnostics as the workspace's earlier build: `_config` import
warnings and rotary-encoder docstring indentation errors. No changed guide
introduced a new build diagnostic. Reproduce the unit suite with:

```bash
# Provide dtc and fdtoverlay for the existing real composition cases.
python3 -m unittest discover -s tests -v
bash -n install.sh driver/install-core-python.sh driver/build-module.sh \
  fusion_hat/scripts/setup_fusion_hat_audio.sh
```
