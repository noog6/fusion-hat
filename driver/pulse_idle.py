#!/usr/bin/env python3
"""User-owned HAT idle-release policy for the verified Orange Pi PulseAudio path."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time

MODULE = 'module-suspend-on-idle'
PROPERTY = MODULE + '.timeout'
UNIT = 'fusion-hat-pulse-idle.service'


def run(args, capture=False):
    result = subprocess.run(list(map(str, args)), check=True, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            env={**os.environ, 'LC_ALL': 'C',
                                 'PULSE_SERVER': f'unix:/run/user/{os.getuid()}/pulse/native'})
    return result.stdout.strip() if capture else ''


def shared():
    spec = importlib.util.spec_from_file_location('idle_voice_helpers', Path(__file__).with_name('voice_setup.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def layout(home=None):
    home = Path(home or Path.home())
    helper = home / '.local/libexec/fusion-hat-pulse-idle'
    state = home / '.local/state/fusion-hat'
    return dict(helper=helper / 'pulse_idle.py', shared=helper / 'voice_setup.py',
                unit=home / '.config/systemd/user' / UNIT,
                manifest=state / 'pulse-idle-files.json', active=state / 'pulse-idle-active.json')


def private_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise RuntimeError('Refusing a symlink at policy file: ' + str(path))
    descriptor, temporary = tempfile.mkstemp(prefix='.pulse-idle-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def private_json(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError('Policy state must be a private, user-owned regular file: ' + str(path))
    return json.loads(path.read_text())


def json_write(path, value):
    private_write(path, (json.dumps(value, indent=2) + '\n').encode())


def owned(paths):
    if not paths['manifest'].exists():
        return None
    state = private_json(paths['manifest'])
    if state.get('version') != 1 or set(state.get('files', {})) != {'helper', 'shared', 'unit'}:
        raise RuntimeError('Invalid idle-policy ownership manifest; preserve it and inspect before retrying.')
    for key, digest in state['files'].items():
        path = paths[key]
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                stat.S_IMODE(info.st_mode) != 0o600 or hashlib.sha256(path.read_bytes()).hexdigest() != digest):
            raise RuntimeError('Owned idle-policy file changed; preserve/review before replacing: ' + str(path))
    return state


def service_identity(runner):
    pid = runner(['systemctl', '--user', 'show', 'pulseaudio.service', '-p', 'MainPID', '--value'], capture=True)
    if not pid.isdecimal() or int(pid) <= 0:
        raise RuntimeError('Use the existing systemd user pulseaudio.service for persistent HAT idle release; no daemon will be replaced automatically.')
    process = Path('/proc') / pid
    if process.stat().st_uid != os.getuid():
        raise RuntimeError('PulseAudio service does not belong to the runtime user.')
    # Start ticks plus boot identity disambiguate PID reuse and server restarts.
    start = (process / 'stat').read_text().rsplit(')', 1)[1].split()[19]
    return [pid, start, Path('/proc/sys/kernel/random/boot_id').read_text().strip()]


def snapshot(runner=run):
    info = json.loads(runner(['pactl', '-f', 'json', 'info'], capture=True))
    if info.get('server_name') != 'pulseaudio' or not str(info.get('server_version', '')).startswith('17.'):
        raise RuntimeError('HAT idle release is validated only for native PulseAudio 17 on Orange Pi. Keep the existing Pi/PipeWire/other-server policy.')
    voice = shared()
    number, card_id = voice.hardware_card('orange-pi')
    result = dict(server=service_identity(runner))
    for kind in ('sink', 'source'):
        items = json.loads(runner(['pactl', '-f', 'json', 'list', kind + 's'], capture=True))
        name = voice.select_endpoint(items, number, card_id, source=kind == 'source')
        if not re.fullmatch(r'[A-Za-z0-9_.:-]+', name):
            raise RuntimeError('Unexpected HAT endpoint name; inspect before writing PulseAudio CLI properties.')
        item = next(item for item in items if item['name'] == name)
        previous = item.get('properties', {}).get(PROPERTY)
        if previous is not None and not re.fullmatch(r'-?\d+|', str(previous)):
            raise RuntimeError('Custom HAT idle property requires review: ' + str(previous))
        result[kind] = dict(name=name, index=item['index'], previous=previous)
        if kind == 'source':
            result['source_indexes'] = [item['index']] + [entry['index'] for entry in items
                if str(entry.get('monitor_of_sink')) == str(result['sink']['index'])]
    modules = json.loads(runner(['pactl', '-f', 'json', 'list', 'modules'], capture=True))
    matches = [module for module in modules if module['name'] == MODULE]
    if len(matches) != 1:
        raise RuntimeError('Expected exactly one idle module; inspect pactl list short modules before configuring policy.')
    module = matches[0]
    arguments = shlex.split(module.get('argument') or '')
    if len(arguments) > 1 or any(not re.fullmatch(r'timeout=\d+', value) for value in arguments):
        raise RuntimeError('Unexpected idle-module arguments; no policy was replaced.')
    result.update(module=module['index'], arguments=arguments)
    return result


def signature(state):
    return {key: state[key] for key in ('server', 'module', 'arguments')} | {
        kind: state[kind]['name'] for kind in ('sink', 'source')}


def active(state, paths):
    return (all(str(state[kind]['previous']) == '0' for kind in ('sink', 'source')) and
            paths['active'].exists() and private_json(paths['active']) == signature(state))


def set_property(kind, name, value, runner):
    # An empty value restores inheritance when the prior property was absent.
    argument = PROPERTY + '=' + json.dumps('' if value is None else str(value))
    runner(['pacmd', 'update-' + kind + '-proplist', name, argument])


def apply(runner=run, home=None):
    paths = layout(home)
    before = snapshot(runner)
    if active(before, paths):
        print('HAT idle release: already active for this PulseAudio instance.')
        return
    # Reloading policy is deferred until the operator has released HAT streams.
    for kind, field in (('sink', 'sink-inputs'), ('source', 'source-outputs')):
        streams = json.loads(runner(['pactl', '-f', 'json', 'list', field], capture=True))
        indices = before['source_indexes'] if kind == 'source' else [before['sink']['index']]
        if any(str(stream.get(kind)) in set(map(str, indices)) for stream in streams):
            raise RuntimeError('Stop HAT audio applications, then rerun idle-policy setup; active streams were preserved.')
    unloaded, loaded_index = False, None
    try:
        for kind in ('sink', 'source'):
            set_property(kind, before[kind]['name'], '0', runner)
        updated = snapshot(runner)
        if (signature(updated) != signature(before) or
                any(updated[kind]['index'] != before[kind]['index'] for kind in ('sink', 'source'))):
            raise RuntimeError('PulseAudio changed during configuration; retry after inspecting the user service.')
        if not all(str(updated[kind]['previous']) == '0' for kind in ('sink', 'source')):
            raise RuntimeError('PulseAudio did not accept both HAT idle properties.')
        runner(['pactl', 'unload-module', str(before['module'])])
        unloaded = True
        loaded_index = runner(['pactl', 'load-module', MODULE, *before['arguments']], capture=True)
        if not loaded_index.isdecimal():
            raise RuntimeError('PulseAudio returned an invalid idle-module identifier.')
        after = snapshot(runner)
        if (after['server'] != before['server'] or after['arguments'] != before['arguments'] or
                not all(str(after[kind]['previous']) == '0' for kind in ('sink', 'source'))):
            raise RuntimeError('HAT idle-policy verification failed after reload.')
        json_write(paths['active'], signature(after))
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        failures = []
        for kind in ('sink', 'source'):
            try:
                set_property(kind, before[kind]['name'], before[kind]['previous'], runner)
            except (OSError, RuntimeError, subprocess.CalledProcessError) as rollback:
                failures.append(str(rollback))
        if unloaded:
            try:
                modules = json.loads(runner(['pactl', '-f', 'json', 'list', 'modules'], capture=True))
                for module in modules:
                    if module['name'] == MODULE and str(module['index']) == loaded_index:
                        runner(['pactl', 'unload-module', str(module['index'])])
                    elif module['name'] == MODULE:
                        raise RuntimeError('An unrecognized idle-module instance was preserved; inspect it before restarting audio.')
                runner(['pactl', 'load-module', MODULE, *before['arguments']], capture=True)
            except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as rollback:
                failures.append(str(rollback))
        raise RuntimeError(str(error) +
                           ('; rollback errors: ' + '; '.join(failures) if failures else '; prior HAT properties/module policy restored') +
                           '. Recovery: exit audio apps, then systemctl --user restart pulseaudio.service.') from error
    print('HAT idle release: active; other devices retain their timeout policy. No sample-rate or routing configuration was written.')


def unit_text():
    return ('[Unit]\nDescription=Fusion HAT PulseAudio idle release\n'
            'After=pulseaudio.service\nRequisite=pulseaudio.service\nPartOf=pulseaudio.service\n\n'
            '[Service]\nType=oneshot\nRemainAfterExit=yes\n'
            'ExecStart=/usr/bin/python3 "%h/.local/libexec/fusion-hat-pulse-idle/pulse_idle.py" apply\n\n'
            '[Install]\nWantedBy=pulseaudio.service\n').encode()


def unit_identity(paths, runner):
    try:
        fragment = runner(['systemctl', '--user', 'show', UNIT, '-p', 'FragmentPath', '--value'], capture=True)
        dropins = runner(['systemctl', '--user', 'show', UNIT, '-p', 'DropInPaths', '--value'], capture=True)
    except subprocess.CalledProcessError:
        fragment, dropins = '', ''
    if fragment and Path(fragment).resolve() != paths['unit'].resolve():
        raise RuntimeError('An unowned unit named ' + UNIT + ' exists; no unit was replaced.')
    if dropins:
        raise RuntimeError('Custom HAT idle unit drop-ins require review and were preserved: ' + dropins)
    return fragment


def status(runner=run, home=None):
    paths = layout(home)
    if not owned(paths):
        raise RuntimeError('Persistent HAT idle policy is absent; rerun --voice --hat-idle-release without --check.')
    if not unit_identity(paths, runner):
        raise RuntimeError('HAT idle unit is not loaded; run systemctl --user daemon-reload and enable --now ' + UNIT)
    for command, expected in (('is-enabled', 'enabled'), ('is-active', 'active')):
        if runner(['systemctl', '--user', command, UNIT], capture=True) != expected:
            raise RuntimeError('HAT idle unit needs activation. Run systemctl --user daemon-reload and systemctl --user enable --now ' + UNIT)
    if not active(snapshot(runner), paths):
        raise RuntimeError('HAT idle policy is pending for this server. Stop audio apps, then systemctl --user restart ' + UNIT)
    print('HAT idle release: persistent files, user unit and current-server policy checks passed.')


def configure(check=False, runner=run, home=None):
    paths = layout(home)
    previous = owned(paths)
    if check:
        return status(runner, home)
    initial = snapshot(runner)  # Native server, session/unit and hardware identity before writes.
    fragment = unit_identity(paths, runner)
    if not previous:
        if any(paths[key].exists() or paths[key].is_symlink() for key in ('helper', 'shared', 'unit', 'manifest')):
            raise RuntimeError('Unowned HAT idle-policy files exist; preserve/review them before installation.')
        if fragment:
            raise RuntimeError('An unowned unit named ' + UNIT + ' already exists; no unit was replaced.')
    contents = dict(helper=Path(__file__).read_bytes(),
                    shared=Path(__file__).with_name('voice_setup.py').read_bytes(), unit=unit_text())
    prior = previous.get('prior') if previous else {
        kind: dict(name=initial[kind]['name'], value=initial[kind]['previous']) for kind in ('sink', 'source')}
    manifest = dict(version=1, prior=prior,
                    files={key: hashlib.sha256(data).hexdigest() for key, data in contents.items()})
    expected_files = {**contents, 'manifest': (json.dumps(manifest, indent=2) + '\n').encode()}
    backups = {key: paths[key].read_bytes() if paths[key].exists() else None for key in (*contents, 'manifest')}
    written = []
    enabled = running = False
    if previous:
        try:
            enabled = runner(['systemctl', '--user', 'is-enabled', UNIT], capture=True) == 'enabled'
        except subprocess.CalledProcessError:
            pass
        try:
            running = runner(['systemctl', '--user', 'is-active', UNIT], capture=True) == 'active'
        except subprocess.CalledProcessError:
            pass
    try:
        for key, data in contents.items():
            private_write(paths[key], data)
            written.append(key)
        json_write(paths['manifest'], manifest)
        written.append('manifest')
        runner(['systemctl', '--user', 'daemon-reload'])
        runner(['systemctl', '--user', 'enable', UNIT])
        runner(['systemctl', '--user', 'restart', UNIT])
        status(runner, home)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        failures = []
        if not enabled:
            for command in ('disable', 'stop'):
                try:
                    runner(['systemctl', '--user', command, UNIT])
                except (OSError, subprocess.CalledProcessError) as rollback:
                    failures.append(str(rollback))
        for key in reversed(written):
            try:
                if paths[key].is_symlink() or paths[key].read_bytes() != expected_files[key]:
                    raise RuntimeError('Concurrent policy-file change preserved: ' + str(paths[key]))
                if backups[key] is None:
                    paths[key].unlink(missing_ok=True)
                else:
                    private_write(paths[key], backups[key])
            except (OSError, RuntimeError) as rollback:
                failures.append(str(rollback))
        try:
            runner(['systemctl', '--user', 'daemon-reload'])
            if previous and running and not failures:
                owned(paths)
                runner(['systemctl', '--user', 'restart', UNIT])
        except (OSError, RuntimeError, subprocess.CalledProcessError) as rollback:
            failures.append(str(rollback))
        raise RuntimeError('Persistent idle-policy setup failed: ' + str(error) +
                           ('; file/unit rollback errors: ' + '; '.join(failures) if failures else '') +
                           '. Inspect journalctl --user -u ' + UNIT + ' -b --no-pager. '
                           'Temporary live properties may remain. Exit audio apps and restart pulseaudio.service to clear them.') from error
    print('HAT idle policy saved. Prior endpoint timeout values recorded in ' + str(paths['manifest']))
    print('HAT idle recovery: /usr/bin/python3 ' + shlex.quote(str(paths['helper'])) + ' remove')


def remove(runner=run, home=None):
    paths = layout(home)
    state = owned(paths)
    if not state:
        print('No owned persistent HAT idle policy is installed.')
        return
    receipt = paths['manifest'].with_name(f'pulse-idle-removed-{time.time_ns()}.json')
    json_write(receipt, state)
    runner(['systemctl', '--user', 'disable', '--now', UNIT])
    # Keep recovery code/state if disabling the unit failed; never delete changes.
    for key in ('helper', 'shared', 'unit', 'manifest', 'active'):
        paths[key].unlink(missing_ok=True)
    runner(['systemctl', '--user', 'daemon-reload'])
    print('Owned idle-policy files removed; prior policy receipt kept: ' + str(receipt))
    print('Temporary properties remain until you exit audio apps and run: systemctl --user restart pulseaudio.service')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('apply', 'check', 'remove'))
    args = parser.parse_args(argv)
    if os.geteuid() == 0:
        parser.exit(1, 'Run HAT idle policy as the non-root runtime user.\n')
    try:
        if args.action == 'apply':
            if not owned(layout()):
                raise RuntimeError('Install the owned policy through install.sh --voice --hat-idle-release first.')
            apply()
        elif args.action == 'check':
            status()
        else:
            remove()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError) as error:
        print('HAT idle policy incomplete: ' + str(error), file=sys.stderr)
        print('Inspect: journalctl --user -u ' + UNIT + ' -b --no-pager', file=sys.stderr)
        return 3
    return 0


if __name__ == '__main__':
    sys.exit(main())
