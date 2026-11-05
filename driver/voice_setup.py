#!/usr/bin/env python3
"""Non-streaming voice dependency, user service and explicit routing setup."""
import argparse
import datetime
import importlib.util
import json
from importlib import metadata
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

VOICE_REVISION = '32d2dae40d47fd6c4fe4180db28cd47e83efd851'
VOICE_REQUIREMENT = 'sunfounder-voice-assistant @ git+https://github.com/sunfounder/sunfounder-voice-assistant.git@' + VOICE_REVISION


class Pending(RuntimeError):
    """A user session/service action is required before voice can complete."""


def run(args, capture=False):
    print('+ ' + shlex.join([str(a) for a in args]), flush=True)
    result = subprocess.run([str(a) for a in args], check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else ''


def available(args, runner=run):
    try:
        return runner(args, capture=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None


def session_ready(user, root=Path('/')):
    directory = root / f'run/user/{user.pw_uid}'
    bus = directory / 'bus'
    return directory.is_dir() and directory.stat().st_uid == user.pw_uid and bus.is_socket()


def service_choice(units, packages):
    """Never install PulseAudio over an existing or partially configured stack."""
    if 'pipewire-pulse.service' in units or 'pipewire-pulse' in packages:
        return 'pipewire-pulse'
    if 'pulseaudio.service' in units:
        return 'pulseaudio'
    if {'pipewire', 'wireplumber', 'pulseaudio'}.intersection(packages):
        return 'existing-incomplete'
    return None


def prepare_service(user, allow_install, check, runner, user_command):
    session = lambda args: user_command(user, args, session=True)
    # Inspect package metadata even with a working server; do not infer that all
    # Armbian installations need PulseAudio or replace a PipeWire server.
    packages = set()
    for name in ('pipewire-pulse', 'pipewire', 'wireplumber', 'pulseaudio', 'pulseaudio-utils'):
        status = available(['dpkg-query', '-W', '-f=${Status}', name], runner)
        if status == 'install ok installed':
            packages.add(name)
    print('Installed user audio stack packages: ' + ', '.join(sorted(packages)))
    if not session_ready(user):
        print(f'Log in as {user.pw_name} to establish /run/user/{user.pw_uid}/bus. Then, in that user session, inspect:')
        print('  systemctl --user list-unit-files pulseaudio.service pulseaudio.socket pipewire-pulse.service pipewire-pulse.socket')
        print('  pactl info')
        raise Pending('Required runtime-user session is unavailable. Voice stage is incomplete; rerun the printed resume command after login.')
    info = available(session(['pactl', 'info']), runner)
    if info is not None:
        print('Reusing existing PulseAudio-compatible server:\n' + info)
        return
    units = available(session(['systemctl', '--user', 'list-unit-files', '--no-legend']), runner)
    if units is None:
        raise Pending('User systemd manager is unavailable. In the runtime login session run systemctl --user daemon-reload and pactl info, then rerun.')
    choice = service_choice(units, packages)
    if choice == 'existing-incomplete':
        raise Pending('An existing audio stack has no usable Pulse server/unit. Inspect systemctl --user status and journalctl --user; repair it explicitly. No replacement was installed.')
    if choice is None:
        if not allow_install or check:
            raise Pending('No PulseAudio-compatible service found. On the observed Orange Pi image, --voice --install-pulseaudio permits apt-get install pulseaudio pulseaudio-utils; inspect apt-cache policy and existing services first. This is optional, not a universal Armbian requirement.')
        runner(['apt-get', 'update'])
        runner(['apt-get', 'install', '-y', 'pulseaudio', 'pulseaudio-utils'])
        choice = 'pulseaudio'
    if check:
        raise Pending(f'User audio service is inactive. In the runtime login session run: systemctl --user daemon-reload && systemctl --user start {choice}.socket {choice}.service; then pactl info and rerun --check.')
    runner(session(['systemctl', '--user', 'daemon-reload']))
    runner(session(['systemctl', '--user', 'start', choice + '.socket', choice + '.service']))
    runner(session(['pactl', 'info']), capture=True)
    print('User audio service available: ' + choice + '. Login/reboot persistence must be checked separately.')


def hardware_card(platform, sysfs=Path('/sys'), dev=Path('/dev')):
    cards = []
    for card in (sysfs / 'class/sound').glob('card[0-9]*'):
        if platform == 'orange-pi':
            match = (card / 'device/of_node').resolve() == sysfs / 'firmware/devicetree/base/fusion-hat-audio'
            driver = sysfs / 'bus/platform/drivers/sunxi-snd-mach'
        else:
            # Pi's existing machine card identity, corroborated by its DT node.
            node = card / 'device/of_node'
            compatible = (node / 'compatible').read_bytes().split(b'\0') if (node / 'compatible').exists() else []
            match = (card / 'id').read_text().strip() == 'sndrpigooglevoi' and b'googlevoicehat,googlevoicehat-soundcard' in compatible
            driver = sysfs / 'bus/platform/drivers/snd-rpi-simple'
        if match:
            if (card / 'device/driver').resolve() != driver:
                raise RuntimeError('HAT audio card has unexpected driver; inspect sysfs before routing.')
            cards.append(card)
    if len(cards) != 1:
        raise RuntimeError('Expected one DT-owned HAT card. Inspect aplay -l, arecord -l and /sys/class/sound/card*/device/of_node; reboot/login may be pending.')
    card = cards[0]
    card_id = (card / 'id').read_text().strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]+', card_id):
        raise RuntimeError('Invalid HAT card ID')
    # Indices are used only to validate kernel node permissions and cross-check
    # server metadata. Selection always uses verified stable server names.
    number = card.name[4:]
    for name in (f'controlC{number}', f'pcmC{number}D0p', f'pcmC{number}D0c'):
        if not os.access(dev / 'snd' / name, os.R_OK | os.W_OK):
            raise RuntimeError('HAT audio permission missing: ' + name + '; log out/in after scoped group changes and inspect udev rules.')
    print(f'HAT audio card: {card_id}; read/write node permissions passed.')
    return number, card_id


def select_endpoint(items, number, card_id, source=False):
    candidates = []
    for item in items:
        name = item.get('name', '')
        properties = item.get('properties', {})
        # Monitor sources can expose the same ALSA properties as the real mic.
        if source and (name.endswith('.monitor') or properties.get('device.class') == 'monitor' or
                       item.get('monitor_of_sink') not in (None, '', 4294967295, '4294967295', -1)):
            continue
        by_id = properties.get('alsa.id') == card_id or properties.get('alsa.card.id') == card_id
        by_index = str(properties.get('alsa.card', '')) == number
        by_path = properties.get('device.bus_path') == 'platform-fusion-hat-audio' or \
                  properties.get('sysfs.path', '').endswith('/fusion-hat-audio/sound/card' + number)
        # Require two independent links to the sysfs-verified hardware. A name
        # containing "fusion" alone is never sufficient; Pi uses ALSA ID/index.
        if (by_id and by_index) or (by_path and by_index):
            if not name or '\n' in name:
                raise RuntimeError('Invalid server endpoint name')
            candidates.append(name)
    if len(candidates) != 1:
        raise RuntimeError('Expected exactly one verified HAT ' + ('microphone source' if source else 'sink') +
                           '; inspect pactl -f json list sources/sinks and sysfs identity. No numeric device selection or default change was made.')
    return candidates[0]


def routing(number, card_id, change=False, check=False, runner=run, state_home=None):
    sink = select_endpoint(json.loads(runner(['pactl', '-f', 'json', 'list', 'sinks'], capture=True)), number, card_id)
    source = select_endpoint(json.loads(runner(['pactl', '-f', 'json', 'list', 'sources'], capture=True)), number, card_id, source=True)
    previous = dict(source=runner(['pactl', 'get-default-source'], capture=True),
                    sink=runner(['pactl', 'get-default-sink'], capture=True))
    print(f'Verified HAT source: {source}; sink: {sink}. Current defaults: {previous}')
    intended = dict(source=source, sink=sink)
    if previous == intended:
        print('User default routing already selects the HAT microphone and speaker.')
        return True
    if not change or check:
        print('Defaults preserved. To select this HAT explicitly, rerun --voice --set-audio-defaults.')
        print('Remaining commands, in the runtime user session (record previous defaults first):')
        print('  ' + shlex.join(['pactl', 'set-default-source', source]))
        print('  ' + shlex.join(['pactl', 'set-default-sink', sink]))
        return False
    directory = (state_home or Path.home() / '.local/state') / 'fusion-hat'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    receipt = directory / ('routing-' + stamp + '.json')
    with receipt.open('x') as output:
        os.chmod(receipt, 0o600)
        json.dump(dict(previous=previous, selected=intended), output, indent=2)
    print('Prior routing saved: ' + str(receipt))
    print('Recovery commands, in this same user session:')
    for kind in ('source', 'sink'):
        print('  ' + shlex.join(['pactl', 'set-default-' + kind, previous[kind]]))
    # A failure after either write leaves an explicit recovery receipt.
    for kind in ('source', 'sink'):
        runner(['pactl', 'set-default-' + kind, intended[kind]])
    actual = {kind: runner(['pactl', 'get-default-' + kind], capture=True) for kind in ('source', 'sink')}
    if actual != intended:
        raise RuntimeError('Routing did not persist immediately. Use the saved recovery commands and inspect user audio policy.')
    return True


def dependency_issues(distribution=metadata.distribution):
    """Validate the core/voice profile and its active transitive requirements.

    Fusion's legacy broad metadata also describes optional GPIO/display modules.
    Neither managed hardware path uses those modules for core + voice. Only that
    distribution gets a scoped requirement set; every voice dependency retains
    its own complete metadata, version constraints, markers and requested extras.
    """
    from packaging.markers import default_environment
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    pending = [(Requirement(name), 'selected core/voice profile') for name in
               ('fusion_hat', 'evdev', 'sunfounder-voice-assistant', 'packaging')]
    seen, issues = set(), []
    environment = default_environment()
    while pending:
        requirement, parent = pending.pop()
        name = canonicalize_name(requirement.name)
        try:
            installed = distribution(name)
        except metadata.PackageNotFoundError:
            issues.append(f'{parent} requires {requirement}; {name} is not installed')
            continue
        if requirement.specifier and not requirement.specifier.contains(installed.version, prereleases=True):
            issues.append(f'{parent} requires {requirement}; installed {name}=={installed.version}')
        key = name, frozenset(requirement.extras)
        if key in seen:
            continue
        seen.add(key)
        requirements = ('evdev', 'sunfounder-voice-assistant') if name == 'fusion-hat' else installed.requires or ()
        for value in requirements:
            dependency = Requirement(value)
            if dependency.marker and not any(dependency.marker.evaluate(dict(environment, extra=extra))
                                             for extra in ('', *requirement.extras)):
                continue
            pending.append((dependency, f'{name}=={installed.version}'))
    return sorted(set(issues))


def software_checks(runner=run):
    issues = dependency_issues()
    if issues:
        print('Required core/voice dependency failures:')
        for issue in issues:
            print('  ' + issue)
        return False
    # No stream opens, hardware writes, model creation/download or provider calls.
    runner([sys.executable, '-c',
            'import importlib.metadata as m,json; '
            'd=m.distribution("sunfounder-voice-assistant"); '
            'u=json.loads(d.read_text("direct_url.json") or "{}"); '
            f'assert u.get("vcs_info",{{}}).get("commit_id")=="{VOICE_REVISION}", "Voice dependency revision differs; rerun --voice without --check to install the documented pin"; '
            'import sunfounder_voice_assistant, pyaudio, sounddevice as sd; '
            'from sunfounder_voice_assistant.voice_assistant import VoiceAssistant; '
            'from fusion_hat.battery import Battery; from fusion_hat.user_button import UserButton; '
            'print("voice",m.version("sunfounder-voice-assistant")); '
            'print("PortAudio",sd.get_portaudio_version()); print(sd.query_hostapis()); print(sd.query_devices()); '
            'p=pyaudio.PyAudio(); print("PyAudio devices",p.get_device_count()); p.terminate()'])
    result = subprocess.run([sys.executable, '-m', 'pip', 'check'], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(result.stdout)
    if result.returncode:
        print('Whole-venv pip check diagnostics remain, outside the verified core/voice profile. '
              'Fusion declares optional Pi GPIO/display dependencies omitted by core-only installation; '
              'other applications may also have conflicts. Review the exact diagnostics before using '
              'those features/applications. They do not fail this stage when all required core/voice '
              'dependencies, imports and enumeration pass. No claim is made that the entire venv is conflict-free.')
    return True


def install_python(platform, runner=run):
    # A Pi hardware install may have used the user site. Install the local Fusion
    # core into this selected voice venv before checking imports on either path.
    source = Path(__file__).resolve().parents[1]
    runner(['bash', source / 'driver/install-core-python.sh', '--platform', platform, sys.executable])
    runner([sys.executable, '-m', 'pip', 'install', 'packaging', VOICE_REQUIREMENT])


def configure_idle_policy(check):
    spec = importlib.util.spec_from_file_location('fusion_user_idle_policy', Path(__file__).with_name('pulse_idle.py'))
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    policy.configure(check=check)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--platform', choices=('orange-pi', 'raspberry-pi'), required=True)
    p.add_argument('--check', action='store_true')
    p.add_argument('--hardware-check', action='store_true')
    p.add_argument('--set-audio-defaults', action='store_true')
    p.add_argument('--hat-idle-release', action='store_true')
    args = p.parse_args()
    if os.geteuid() == 0:
        p.exit(1, 'Run voice/audio checks as the non-root runtime user in the selected venv/session.\n')
    try:
        if args.hat_idle_release and args.platform != 'orange-pi':
            raise RuntimeError('--hat-idle-release is limited to the verified Orange Pi/native PulseAudio path.')
        number, card_id = hardware_card(args.platform)
        if args.hardware_check:
            return 0
        if sys.prefix == sys.base_prefix:
            raise RuntimeError('Voice setup requires an existing selected venv.')
        if args.hat_idle_release:
            configure_idle_policy(args.check)
        if not args.check:
            install_python(args.platform)
            # Record exactly what the resolver installed, including transitive
            # dependencies. This is an inventory, not a portable full lockfile.
            inventory = Path.home() / '.local/state/fusion-hat'
            inventory.mkdir(parents=True, exist_ok=True, mode=0o700)
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
            with (inventory / ('voice-' + stamp + '.txt')).open('x') as output:
                os.chmod(output.name, 0o600)
                output.write('Voice source revision: ' + VOICE_REVISION + '\n')
                output.write(run([sys.executable, '-m', 'pip', 'freeze'], capture=True) + '\n')
        run(['pactl', 'info'], capture=True)
        routed = routing(number, card_id, args.set_audio_defaults, args.check)
        metadata_ok = software_checks()
        if not routed or not metadata_ok:
            print('Voice stage incomplete: review routing and/or the exact dependency diagnostics above; software enumeration is not physical audio validation.')
            return 3
        print('Voice software checks passed. Physical speech and post-login/unattended persistence require operator acceptance.')
        return 0
    except (OSError, ImportError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print('Voice stage incomplete: ' + str(error), file=sys.stderr)
        print('To provision missing Fusion/core/voice packages in this selected venv, rerun --voice without --check.', file=sys.stderr)
        print('For PulseAudio_Initialize / sounddevice import failures, check pactl info and systemctl --user status in the runtime session before changing PortAudio or reinstalling hardware. For native-extension build failures inspect the compiler error, selected Python development headers and apt-cache policy portaudio19-dev.', file=sys.stderr)
        return 3


if __name__ == '__main__':
    sys.exit(main())
