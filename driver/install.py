#!/usr/bin/env python3
"""Platform-aware orchestration; platform installers retain hardware ownership."""
import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CORE = load('fusion_core_installer', HERE / 'orange-pi-zero3w-install.py')


class Incomplete(RuntimeError):
    """Files/prerequisites may be ready, but activation or user action is pending."""


def command(args, capture=False):
    print('+ ' + shlex.join([str(a) for a in args]), flush=True)
    result = subprocess.run([str(a) for a in args], check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else ''


def read(path):
    return path.read_text().strip().strip('\0') if path.exists() else ''


def detect(root=Path('/'), runner=CORE.run, selected='auto'):
    path = lambda p: root / p.lstrip('/')
    operating_system = CORE.settings(read(path('/etc/os-release')))
    arm = CORE.settings(read(path('/etc/armbian-release')))
    model = read(path('/sys/firmware/devicetree/base/model'))
    compatible = read(path('/sys/firmware/devicetree/base/compatible')).split('\0')
    board = read(path('/sys/firmware/devicetree/base/board'))
    arch, kernel = runner(['uname', '-m']), runner(['uname', '-r'])
    print('Detected: ' + json.dumps(dict(os=operating_system, armbian=arm, model=model,
                                        compatible=compatible, board=board, architecture=arch, kernel=kernel)), flush=True)
    pi_board = any(c.startswith('raspberrypi,') for c in compatible)
    orange_named = 'xunlong,orangepi-zero3w' in compatible and bool(re.search(r'Orange\s*Pi\s+Zero\s*3W', model, re.I))
    # The inspected Zero 3W vendor DTS inherits a generic SoC model and a
    # misleading 4-pro compatible. Never use that alias without all other gates.
    orange_vendor = set(compatible) == {'xunlong,orangepi-4-pro', 'arm,sun60iw2p1'} and model == 'sun60iw2' and board == 'A733'
    orange_board = orange_named or orange_vendor
    if (pi_board and (arm or orange_board)) or (orange_board and 'Raspberry Pi' in model):
        raise ValueError('Conflicting board/OS evidence. Check /etc/*release and the booted device tree; no platform override bypasses validation.')
    if orange_board:
        # Single authority for the exact image, architecture and kernel used by
        # both existing Orange Pi installers. Their full preflights still run.
        CORE.Installation(root, runner).compatibility()
        boot = CORE.settings(read(path('/boot/armbianEnv.txt')))
        if boot.get('fdtfile') != 'allwinner/sun60i-a733-orangepi-zero3w.dtb' or boot.get('overlay_prefix') != 'sun60i-a733':
            raise ValueError('Orange Pi DT identity conflicts with boot DTB selection; restore/review the verified Zero 3W boot configuration.')
        if orange_vendor:
            print('Vendor generic/4-pro DT alias accepted only with A733, exact Zero 3W image and boot DTB gates; this does not support Orange Pi 4 Pro.')
        result = 'orange-pi'
    else:
        pi_os = operating_system.get('ID') == 'raspbian' or (
            operating_system.get('ID') == 'debian' and (
                'Raspberry Pi' in operating_system.get('NAME', '') or
                'Raspberry Pi reference' in read(path('/etc/rpi-issue'))))
        known_pi = {'raspberrypi,4-model-b': 'Raspberry Pi 4 Model B',
                    'raspberrypi,5-model-b': 'Raspberry Pi 5 Model B',
                    'raspberrypi,model-zero-2-w': 'Raspberry Pi Zero 2 W'}
        matches = [name for key, name in known_pi.items() if key in compatible]
        identity_matches = len(matches) == 1 and model.startswith(matches[0])
        pi_kernel = re.search(r'(rpt-rpi|-(?:v7l?|v8)\+)', kernel)
        if pi_os and pi_board and identity_matches and arch in ('aarch64', 'armv7l') and pi_kernel:
            result = 'raspberry-pi'
        else:
            raise ValueError('Unsupported or ambiguous platform. Require Raspberry Pi OS on Pi 4B/5/Zero 2 W with a Pi kernel, or the verified Orange Pi Zero 3W Armbian image. Review model, compatible, OS origin and kernel; generic Debian/Ubuntu/Armbian is insufficient.')
    if selected not in ('auto', result):
        raise ValueError(f'--platform {selected} conflicts with detected {result}; select the matching platform or correct the image.')
    print('Validated platform: ' + result)
    return result


def runtime_user(name=None):
    name = name or (os.environ.get('SUDO_USER') if os.geteuid() == 0 else pwd.getpwuid(os.geteuid()).pw_name)
    if not name:
        raise ValueError('Root invocation needs --runtime-user USER; no login account is guessed.')
    user = pwd.getpwnam(name)
    if user.pw_uid == 0 or not Path(user.pw_dir).is_absolute():
        raise ValueError('Select a non-root --runtime-user with an absolute home directory.')
    return user


def user_command(user, args, session=False):
    # Never inherit root's HOME, venv, Python path, Pulse socket or DBus address.
    env = ['env', '-i', '-C', user.pw_dir, f'PATH={user.pw_dir}/.local/bin:/usr/local/bin:/usr/bin:/bin', f'HOME={user.pw_dir}',
           f'USER={user.pw_name}', f'LOGNAME={user.pw_name}', 'LANG=C.UTF-8']
    if session:
        env += [f'XDG_RUNTIME_DIR=/run/user/{user.pw_uid}',
                f'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{user.pw_uid}/bus']
    return (['sudo', '-H', '-u', user.pw_name, '--'] if os.geteuid() != user.pw_uid else []) + env + [str(a) for a in args]


def selected_python(args, user, platform, runner=command):
    if args.venv and args.python:
        raise ValueError('Choose --venv or --python, not both.')
    value = args.python or (str(Path(args.venv) / 'bin/python') if args.venv else None)
    if not value and (platform == 'orange-pi' and ('core' in args.stages or 'voice' in args.stages)):
        raise ValueError('Select the existing runtime venv with --venv /absolute/path or --python /absolute/venv/bin/python (Orange Pi core targets Python 3.14.4).')
    if not value:
        return None
    # Do not resolve symlinks: a venv interpreter commonly links to system Python.
    if not Path(value).is_absolute() or not os.access(value, os.X_OK):
        raise ValueError('Selected venv interpreter must be an absolute executable path: ' + value)
    probe = 'import sys,json; print(json.dumps(dict(prefix=sys.prefix,base=sys.base_prefix,version=list(sys.version_info[:3]))))'
    info = json.loads(runner(user_command(user, [value, '-c', probe]), capture=True))
    if info['prefix'] == info['base']:
        raise ValueError('Selected interpreter is not a venv. Create/select a user-owned venv explicitly.')
    if platform == 'orange-pi' and info['version'] != [3, 14, 4]:
        raise ValueError('The Orange Pi Python path is validated only with Python 3.14.4.')
    runner(user_command(user, [value, '-c',
           'import os,sys; assert os.stat(sys.prefix).st_uid == os.getuid() and os.access(sys.prefix, os.W_OK), "venv must be owned and writable by runtime user"']))
    print(f'Runtime user: {user.pw_name}; Python: {value}; environment: {info}')
    return value


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--platform', choices=('auto', 'raspberry-pi', 'orange-pi'), default='auto')
    for stage in ('core', 'audio', 'voice'):
        p.add_argument('--' + stage, action='store_true', help='select this stage (default: core + audio)')
    p.add_argument('--check', action='store_true', help='read-only non-streaming verification; exit 3 if activation/setup is incomplete')
    p.add_argument('--runtime-user')
    p.add_argument('--venv')
    p.add_argument('--python')
    p.add_argument('--headers', default=f'/lib/modules/{CORE.RELEASE}/build')
    p.add_argument('--repo', help='bootstrap OWNER/REPO; rejected in a local checkout')
    p.add_argument('--ref', help='bootstrap branch/tag/SHA; rejected in a local checkout')
    p.add_argument('--install-pulseaudio', action='store_true', help='allow PulseAudio packages only if no other stack is present')
    p.add_argument('--set-audio-defaults', action='store_true', help='save prior routing and explicitly select verified HAT source/sink')
    p.add_argument('--plain-text', action='store_true', help='legacy option; output is always plain text')
    p.add_argument('--skip-test', action='store_true', help='legacy option; no automatic stream tests are ever run')
    p.add_argument('--test', action='store_true', help='legacy option; print operator test instructions, without playing/recording')
    return p


def source_revision(args, source=SOURCE, runner=CORE.run):
    bootstrap_repo = os.environ.get('FUSION_HAT_BOOTSTRAP_REPOSITORY')
    git = ['git', '-c', 'safe.directory=' + str(source), '-C', source]
    revision = runner([*git, 'rev-parse', 'HEAD'])
    if bootstrap_repo:
        if revision != os.environ.get('FUSION_HAT_BOOTSTRAP_REVISION'):
            raise ValueError('Bootstrap revision mismatch; download a complete, consistent source revision again.')
        return f'{bootstrap_repo}@{revision}', bootstrap_repo, revision
    if args.repo or args.ref or os.environ.get('FUSION_HAT_REPOSITORY') or os.environ.get('FUSION_HAT_BRANCH'):
        raise ValueError('Local checkout is authoritative: --repo/--ref and FUSION_HAT_REPOSITORY/FUSION_HAT_BRANCH are bootstrap-only. Unset them or invoke a standalone downloaded script.')
    dirty = runner([*git, 'status', '--porcelain', '--untracked-files=no'])
    return f'{source}@{revision}' + (' (local modifications included)' if dirty else ''), None, revision


def resume(args, user, python, repo, revision):
    options = ['--platform', args.platform, '--runtime-user', user.pw_name]
    for stage in args.stages:
        options.append('--' + stage)
    if python:
        options += ['--python', python]
    options += ['--headers', args.headers]
    for flag in ('check', 'install_pulseaudio', 'set_audio_defaults', 'plain_text', 'skip_test', 'test'):
        if getattr(args, flag):
            options.append('--' + flag.replace('_', '-'))
    if repo:
        options += ['--repo', repo, '--ref', revision]
        return f'curl -fsSL https://raw.githubusercontent.com/{repo}/{revision}/install.sh | sudo bash -s -- ' + shlex.join(options)
    return shlex.join(['sudo', 'bash', str(SOURCE / 'install.sh'), *options])


def active_core(root=Path('/')):
    sysfs = root / 'sys'
    client = sysfs / 'bus/i2c/devices/0-0017'
    if not (sysfs / 'module/fusion_hat').exists() and not client.exists():
        return False
    for flag in ('disable_auto_shutdown', 'restrict_controls'):
        if read(sysfs / 'module/fusion_hat/parameters' / flag) != 'Y':
            raise ValueError('Loaded core policy is unsuitable: ' + flag + '. Inspect the core guide; do not force unload or bypass policy.')
    if (client / 'driver').resolve() != sysfs / 'bus/i2c/drivers/fusion_hat':
        raise ValueError('Core is partially active without the expected MCU binding; inspect dmesg and the core guide.')
    if not (client / 'of_node/sunfounder,experimental-core').exists():
        raise ValueError('Loaded MCU was not created by the owned experimental overlay.')
    return True


def verify_owned(installation, user, token):
    state = installation.state()
    if not state:
        return None
    installation.assert_owned(state)
    if state.get('phase') != 'installed' or state.get('user') != user.pw_name:
        raise ValueError(f'{token} manifest is incomplete or belongs to another user; inspect its guide and rollback state before retrying.')
    for name in state['files']:
        if not installation.path(name).is_file():
            raise ValueError(f'{token} managed file missing: {name}; inspect/uninstall the partial installation first.')
    if token not in CORE.settings(installation.boot.read_text()).get('overlays', '').split():
        raise ValueError(f'{token} owned boot token missing; inspect/uninstall before retrying.')
    return state


def orange(args, source, user, python, runner=command, user_source=None):
    user_source = user_source or source
    core_module = load('selected_core', source / 'driver/orange-pi-zero3w-install.py')
    audio_module = load('selected_audio', source / 'driver/orange-pi-zero3w-audio-install.py')
    core, audio = core_module.Installation(), audio_module.Installation()
    # Always validate existing ownership, even for an audio-only/voice-only run.
    state = verify_owned(core, user, core_module.TOKEN)
    if 'core' in args.stages:
        config, dtb, state, _ = core.preflight(args.headers, user.pw_name)
        if args.check:
            core.overlay(config, dtb)
        else:
            module = source / 'driver/fusion_hat.ko'
            if state:
                # Retain the installed driver; this command is not an upgrade.
                installed = f'/lib/modules/{CORE.RELEASE}/updates/fusion-hat-core/fusion_hat.ko'
                shutil.copyfile(core.path(installed), module)
                print('Retaining the owned installed module; no driver upgrade requested.')
            else:
                headers = trusted_headers(args.headers)
                runner(kernel_build(['bash', source / 'driver/build-module.sh', CORE.RELEASE, headers]))
            core.install(core.artifacts(config, dtb), user.pw_name, state)
            runner(user_command(user, ['bash', user_source / 'driver/install-core-python.sh', python]))
        if args.check:
            if not state:
                raise Incomplete('Core files are absent. Run --core without --check first; see the core prerequisites guide.')
            runner(user_command(user, [python, user_source / 'driver/core_python_preflight.py']))
            runner(user_command(user, [python, '-c', 'from fusion_hat.battery import Battery; from fusion_hat.user_button import UserButton; from fusion_hat.servo import Servo; print("Core imports passed; no devices instantiated")']))
        state = verify_owned(core, user, core_module.TOKEN)
    if not state:
        raise Incomplete('Core files are absent. Run with --core first; matching prepared vendor headers and i2c0 are prerequisites (see core guide).')
    if not active_core():
        raise Incomplete('Core files installed; reboot required, then rerun this command after login. No audio preflight was bypassed.')
    print('Core binding and both MCU policy flags passed.')
    if 'audio' in args.stages:
        config, dtb, audio_state = audio.preflight(user.pw_name)
        files = audio.artifacts(config, dtb)
        if not args.check:
            audio.install(files, user.pw_name, core_module.digest(dtb.read_bytes()), audio_state)
    if {'audio', 'voice'}.intersection(args.stages):
        if not verify_owned(audio, user, audio_module.TOKEN):
            raise Incomplete('Audio files are absent. Run with --audio after core activation.')
        if not Path('/sys/firmware/devicetree/base/fusion-hat-audio').exists():
            raise Incomplete('Audio files installed; reboot required, then rerun this command after login.')
        runner(user_command(user, [python or '/usr/bin/python3', user_source / 'examples/orange_pi_zero3w_audio.py', 'check']))
        print('Audio card identity and non-root permissions passed; no PCM stream opened.')


def raspberry(args, source, user, python, runner=command, root=Path('/'), user_source=None):
    user_source = user_source or source
    path = lambda p: root / p.lstrip('/')
    config = next((p for p in (path('/boot/firmware/config.txt'), path('/boot/config.txt')) if p.is_file()), None)
    if not config or not shutil.which('raspi-config'):
        raise ValueError('Raspberry Pi OS boot config or raspi-config is missing. Restore the supported OS tools before installing.')
    installed = None
    try:
        installed = runner(['modinfo', '-n', 'fusion_hat'], capture=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    print(f'Pi driver file: {installed or "absent"}; boot configuration: {config}')
    if args.check and not installed:
        raise Incomplete('Pi driver files are absent. Run --core without --check first.')
    if not args.check and 'core' in args.stages:
        packages = ['git', 'python3', 'python3-pip', 'i2c-tools', 'espeak', 'libsdl2-dev', 'libsdl2-mixer-dev',
                    'portaudio19-dev', 'sox', 'libsox-fmt-mp3', 'libttspico-utils', 'dkms']
        runner(['apt-get', 'update'])
        runner(['apt-get', 'install', '-y', *packages])
        if installed:
            print('Pi driver already installed; retaining ' + installed + '. This is not a driver upgrade.')
        else:
            if path('/sys/module/fusion_hat').exists():
                raise ValueError('A live Pi module has no installed artifact; resolve its ownership before installing.')
            headers = path('/lib/modules/' + CORE.run(['uname', '-r']) + '/build')
            build_options = ['KDIR=' + trusted_headers(headers)] if headers.exists() else []
            runner(kernel_build(['make', '-C', source / 'driver', *build_options, 'all']))
            runner(kernel_build(['make', '-C', source / 'driver', *build_options, 'BOOT_ONLY=1', 'install']))
        # Keep overlay scope deterministic even if the existing file ends inside
        # a model-specific section. Back up exact prior content once per change.
        text = config.read_text()
        if not re.search(r'^dtoverlay=sunfounder-fusionhat\s*$', text, re.M):
            backup = config.with_name(config.name + '.fusion-hat.before')
            if backup.exists():
                raise ValueError(f'Backup already exists: {backup}. Review it before changing {config}.')
            shutil.copy2(config, backup)
            with config.open('a') as output:
                output.write('\n[all]\ndtoverlay=sunfounder-fusionhat\n')
        # Preserve Pi's non-voice Python capabilities. The metadata's voice
        # application requirement is installed only by the explicit venv stage.
        executable = python or '/usr/bin/python3'
        pip_flags = [] if python else ['--user', '--break-system-packages']
        if not python:
            # Preserve the previous Pi OS compatibility cleanup, before any
            # user-site dependency resolution. No library is installed as root.
            runner(['/usr/bin/python3', '-m', 'pip', 'uninstall', '-y', 'RPi.GPIO', '--break-system-packages'])
        def pip_command(values):
            return user_command(user, values)
        runner(pip_command([executable, '-m', 'pip', 'install', '--no-deps', str(user_source), *pip_flags]))
        dependencies = ('from importlib.metadata import requires; import subprocess,sys; '
                        'deps=[r for r in requires("fusion_hat") if not r.lower().startswith("sunfounder-voice-assistant")]; '
                        'subprocess.run([sys.executable,"-m","pip","install",*sys.argv[1:],*deps],check=True)')
        runner(pip_command([executable, '-c', dependencies, *pip_flags]))
        uninstall_flags = [] if python else ['--break-system-packages']
        runner(pip_command([executable, '-m', 'pip', 'uninstall', '-y', 'RPi.GPIO', *uninstall_flags]))
        print('Pi Python installed for ' + user.pw_name + ': ' + (python or user.pw_dir + '/.local/bin/fusion_hat (system interpreter, user-site packages)'))
        # Completion generated by the runtime account must never be installed
        # as shell code in /etc. Keep it entirely in that account's data directory.
        completion_script = ('from pathlib import Path; import subprocess,sys; '
                             'p=Path.home()/".local/share/bash-completion/completions"; '
                             'p.mkdir(parents=True,exist_ok=True); '
                             'text=subprocess.check_output([sys.argv[1],"-s","bash","fusion_hat"],text=True); '
                             '(p/"fusion_hat").write_text(text)')
        register = str(Path(python).parent / 'register-python-argcomplete') if python else 'register-python-argcomplete'
        runner(user_command(user, [executable, '-c', completion_script, register]))
    if not path('/sys/module/fusion_hat').exists() or not path('/sys/class/fusion_hat/fusion_hat/firmware_version').exists():
        raise Incomplete('Pi driver is not active. Review the installed files and dmesg; reboot explicitly, then rerun this command.')
    if {'core', 'audio'}.intersection(args.stages):
        runner(user_command(user, [python or '/usr/bin/python3', '-c', 'from fusion_hat.battery import Battery; print("Core import passed")']))
    if 'audio' in args.stages:
        if not args.check:
            audio_args = ['bash', source / 'fusion_hat/scripts/setup_fusion_hat_audio.sh', '--skip-test', '--non-actuating', '--runtime-user', user.pw_name, '--user-source', str(user_source)]
            if not args.set_audio_defaults:
                audio_args.append('--keep-defaults')
            runner(audio_args)
        # Verified card identity and permissions are also used by voice checks.
        runner(user_command(user, ['/usr/bin/python3', user_source / 'driver/voice_setup.py', '--hardware-check', '--platform', 'raspberry-pi']))


def copy_inputs(original, target):
    """Independent copies: never promote Python staging back to root inputs."""
    target.mkdir()
    for name in ('driver', 'fusion_hat', 'examples', 'pyproject.toml', 'README.md', 'LICENSE'):
        item, destination = original / name, target / name
        if item.is_symlink() or (item.is_dir() and any(p.is_symlink() for p in item.rglob('*'))):
            raise ValueError('Installer source inputs must not contain symlinks: ' + str(item))
        if item.is_dir():
            shutil.copytree(item, destination, ignore=shutil.ignore_patterns(
                '*.egg-info', '__pycache__', '*.o', '*.ko', '*.ko.*', '*.cmd',
                '*.dtbo', '*.mod', '*.mod.c', 'Module.symvers', 'modules.order', '.tmp_versions'))
        else:
            shutil.copy2(item, destination)


def trusted_headers(path):
    """Resolve once; reject writable build inputs, including symlink targets."""
    path = Path(path).resolve(strict=True)
    seen = set()
    pending = [path]
    while pending:
        item = pending.pop()
        resolved = item.resolve(strict=True)
        if resolved in seen:
            continue
        seen.add(resolved)
        for component in (resolved, *resolved.parents):
            info = component.stat()
            # /tmp may be sticky, but headers/build inputs must not depend on
            # any group/world writable location, even a sticky directory.
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError('Kernel headers/build inputs must be root-owned and not group/world writable: ' + str(component))
        if resolved.is_dir():
            pending.extend(resolved.iterdir())
    return str(path)


def kernel_build(args):
    # No user toolchain, make startup files, Python path or shell startup files.
    return ['env', '-i', 'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
            'HOME=/root', 'LANG=C.UTF-8', *map(str, args)]


def trusted_temporary_root():
    temporary_root = Path('/tmp').stat()
    if temporary_root.st_uid != 0 or (temporary_root.st_mode & 0o022 and not temporary_root.st_mode & 0o1000):
        raise ValueError('Privileged staging requires root-owned /tmp with sticky protection or no group/world write access.')
    return '/tmp'


@contextmanager
def staging(user, original=SOURCE):
    # /tmp's root-owned sticky directory protects these root-owned entries;
    # do not place privileged inputs under a runtime-user-controlled TMPDIR.
    directory = trusted_temporary_root()
    with tempfile.TemporaryDirectory(prefix='fusion-hat-root-', dir=directory) as privileged, \
         tempfile.TemporaryDirectory(prefix='fusion-hat-user-', dir=directory) as unprivileged:
        root_directory, user_directory = Path(privileged), Path(unprivileged)
        source, user_source = root_directory / 'source', user_directory / 'source'
        copy_inputs(original, source)
        digest = hashlib.sha256()
        for path in sorted(source.rglob('*')):
            if path.is_file():
                digest.update(str(path.relative_to(source)).encode() + b'\0' + path.read_bytes())
        print('Source snapshot SHA256: ' + digest.hexdigest())
        # Root owns every privileged file because it created independent copies.
        # Explicit modes discard source group/world write bits. Parent remains 700.
        for path in (source, *source.rglob('*')):
            path.chmod(0o700 if path.is_dir() or path.stat().st_mode & 0o111 else 0o600)
        copy_inputs(source, user_source)
        for path in (user_source, *user_source.rglob('*'), user_directory):
            os.chown(path, user.pw_uid, user.pw_gid)
        yield source, user_source


def main(argv=None):
    args = parser().parse_args(argv)
    args.stages = [s for s in ('core', 'audio', 'voice') if getattr(args, s)] or ['core', 'audio']
    resume_command = None
    try:
        platform = detect(selected=args.platform)
        label, repo, revision = source_revision(args)
        print('Source: ' + label)
        user = runtime_user(args.runtime_user)
        if os.geteuid() != 0:
            raise ValueError('Run this entry point through sudo; Python/audio commands will run as ' + user.pw_name + '.')
        if args.install_pulseaudio and 'voice' not in args.stages:
            raise ValueError('--install-pulseaudio requires --voice.')
        if args.set_audio_defaults and 'voice' not in args.stages:
            raise ValueError('--set-audio-defaults requires --voice (routing is separate from hardware setup).')
        python = selected_python(args, user, platform)
        if 'voice' in args.stages and not python:
            raise ValueError('--voice requires --venv or --python; no root/system voice environment is selected.')
        resume_command = resume(args, user, python, repo, revision)
        print('Resume command: ' + resume_command)
        with staging(user) as (source, user_source):
            (orange if platform == 'orange-pi' else raspberry)(args, source, user, python, user_source=user_source)
            if 'voice' in args.stages:
                # Service/package selection happens separately from user setup.
                voice = load('fusion_voice_setup', source / 'driver/voice_setup.py')
                try:
                    voice.prepare_service(user, args.install_pulseaudio, args.check, command, user_command)
                except voice.Pending as error:
                    raise Incomplete(str(error)) from error
                options = ['--platform', platform]
                if args.check:
                    options.append('--check')
                if args.set_audio_defaults:
                    options.append('--set-audio-defaults')
                command(user_command(user, [python, user_source / 'driver/voice_setup.py', *options], session=True))
        print('Selected stages: files installed and non-actuating software checks passed. Physical playback/capture require operator confirmation.')
        if args.test:
            print('Operator tests: driver/unified-install.md acceptance steps; no playback/recording was performed here.')
        return 0
    except (Incomplete, OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError) as error:
        print('Installation incomplete: ' + (getattr(error, 'stderr', None) or str(error)), file=sys.stderr)
        print('Completed earlier stages may remain installed. Preserve the full error and inspect platform manifests/rollback diagnostics before retrying; see driver/unified-install.md.', file=sys.stderr)
        if resume_command:
            print('After resolving the prerequisite/session/reboot, rerun: ' + resume_command, file=sys.stderr)
        return 3 if isinstance(error, Incomplete) or (isinstance(error, subprocess.CalledProcessError) and error.returncode == 3) else 1


if __name__ == '__main__':
    sys.exit(main())
