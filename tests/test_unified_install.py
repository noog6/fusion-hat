"""Unified dispatcher tests. Commands, hardware roots and privilege are mocked."""
import contextlib
import io
import importlib.util
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


INSTALL = load('unified_install', 'driver/install.py')
VOICE = load('voice_setup', 'driver/voice_setup.py')
USER = SimpleNamespace(pw_name='runtime', pw_uid=1001, pw_gid=1001, pw_dir='/home/runtime')


class Platforms(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.arch, self.kernel = 'aarch64', INSTALL.CORE.RELEASE

    def write(self, path, text):
        target = self.root / path.lstrip('/')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def runner(self, args):
        self.assertIn(args, (['uname', '-m'], ['uname', '-r']))
        return self.arch if args[-1] == '-m' else self.kernel

    def orange(self):
        self.write('/boot/armbianEnv.txt', 'fdtfile=allwinner/sun60i-a733-orangepi-zero3w.dtb\noverlay_prefix=sun60i-a733\n')
        self.write('/etc/os-release', 'ID=ubuntu\nVERSION_CODENAME=resolute\n')
        self.write('/etc/armbian-release', 'BOARD=orangepizero3w\nVERSION=26.8.3\n')
        self.write('/sys/firmware/devicetree/base/model', 'Orange Pi Zero3W\0')
        self.write('/sys/firmware/devicetree/base/compatible', 'xunlong,orangepi-zero3w\0allwinner,sun60i-a733\0')

    def pi(self, board='4-model-b'):
        self.kernel = '6.12.47+rpt-rpi-v8'
        self.write('/etc/os-release', 'ID=debian\nNAME="Debian GNU/Linux"\n')
        self.write('/etc/rpi-issue', 'Raspberry Pi reference 2025-10-01')
        self.write('/sys/firmware/devicetree/base/model', {'4-model-b': 'Raspberry Pi 4 Model B', '5-model-b': 'Raspberry Pi 5 Model B', 'model-zero-2-w': 'Raspberry Pi Zero 2 W'}[board] + ' Rev 1.0\0')
        self.write('/sys/firmware/devicetree/base/compatible', 'raspberrypi,' + board + '\0brcm,bcm2835\0')

    def test_supported_boards_and_explicit_selection(self):
        self.orange()
        self.assertEqual(INSTALL.detect(self.root, self.runner), 'orange-pi')
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            INSTALL.detect(self.root, self.runner, 'raspberry-pi')
        (self.root / 'etc/armbian-release').unlink()
        for board in ('4-model-b', '5-model-b', 'model-zero-2-w'):
            self.pi(board)
            self.assertEqual(INSTALL.detect(self.root, self.runner, 'raspberry-pi'), 'raspberry-pi')

    def test_vendor_zero3w_generic_identity_is_tightly_gated(self):
        self.orange()
        self.write('/sys/firmware/devicetree/base/model', 'sun60iw2\0')
        self.write('/sys/firmware/devicetree/base/board', 'A733\0')
        self.write('/sys/firmware/devicetree/base/compatible', 'xunlong,orangepi-4-pro\0arm,sun60iw2p1\0')
        self.assertEqual(INSTALL.detect(self.root, self.runner), 'orange-pi')
        self.write('/boot/armbianEnv.txt', 'fdtfile=allwinner/sun60i-a733-orangepi-4-pro.dtb\n')
        with self.assertRaisesRegex(ValueError, 'boot DTB'):
            INSTALL.detect(self.root, self.runner)
        self.write('/etc/armbian-release', 'BOARD=orangepi4pro\nVERSION=26.8.3\n')
        with self.assertRaisesRegex(ValueError, 'Expected Orange Pi Zero 3W'):
            INSTALL.detect(self.root, self.runner)

    def test_generic_debian_and_conflicting_evidence_rejected(self):
        self.pi()
        (self.root / 'etc/rpi-issue').unlink()
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            INSTALL.detect(self.root, self.runner)
        self.pi()
        self.write('/etc/armbian-release', 'BOARD=some-board\n')
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            INSTALL.detect(self.root, self.runner)

    def test_unknown_armbian_or_mismatched_target_rejected(self):
        self.orange()
        for file, value in (
            ('/etc/armbian-release', 'BOARD=orangepizero3\nVERSION=26.8.3\n'),
            ('/etc/os-release', 'ID=ubuntu\nVERSION_CODENAME=noble\n'),
            ('/sys/firmware/devicetree/base/model', 'Raspberry Pi 5\0'),
            ('/sys/firmware/devicetree/base/compatible', 'xunlong,orangepi-zero3\0'),
        ):
            self.orange()
            self.write(file, value)
            with self.assertRaises(ValueError):
                INSTALL.detect(self.root, self.runner, 'orange-pi')
        self.orange()
        self.kernel = '6.12-something'
        with self.assertRaisesRegex(ValueError, 'Only Orange'):
            INSTALL.detect(self.root, self.runner)

    def test_invalid_platform_exits_before_any_commands_or_source_access(self):
        with patch.object(INSTALL, 'detect', side_effect=ValueError('unsupported board')), \
             patch.object(INSTALL, 'source_revision') as source, patch.object(INSTALL, 'command') as cmd:
            self.assertEqual(INSTALL.main([]), 1)
        source.assert_not_called()
        cmd.assert_not_called()


class ProvenanceAndUser(unittest.TestCase):
    def test_local_checkout_wins_and_overrides_fail_without_git_mutation(self):
        args = INSTALL.parser().parse_args([])
        calls = []
        def git(args):
            calls.append(args)
            return 'a' * 40 if args[-1] == 'HEAD' else ' M install.sh'
        with patch.dict(os.environ, {}, clear=True):
            label, repo, revision = INSTALL.source_revision(args, runner=git)
            self.assertIn('local modifications included', label)
            self.assertIsNone(repo)
            args.repo = 'someone/else'
            with self.assertRaisesRegex(ValueError, 'authoritative'):
                INSTALL.source_revision(args, runner=git)
        self.assertTrue(all(a[5] in ('rev-parse', 'status') for a in calls))

    def test_bootstrap_revision_and_resume_are_pinned_with_options(self):
        args = INSTALL.parser().parse_args(['--voice', '--set-audio-defaults', '--install-pulseaudio', '--headers', '/my headers'])
        args.stages = ['voice']
        with patch.dict(os.environ, {'FUSION_HAT_BOOTSTRAP_REPOSITORY': 'noog6/fusion-hat', 'FUSION_HAT_BOOTSTRAP_REVISION': 'a' * 40}):
            _, repo, revision = INSTALL.source_revision(args, runner=lambda _: 'a' * 40)
            result = INSTALL.resume(args, USER, '/home/runtime/my venv/bin/python', repo, revision)
            self.assertIn('/' + 'a' * 40 + '/install.sh', result)
            for option in ('--voice', '--set-audio-defaults', '--install-pulseaudio', "'/my headers'", "'/home/runtime/my venv/bin/python'"):
                self.assertIn(option, result)
            with self.assertRaisesRegex(ValueError, 'mismatch'):
                INSTALL.source_revision(args, runner=lambda _: 'b' * 40)

    def test_root_needs_explicit_user_and_sudo_user_is_used(self):
        with patch.object(INSTALL.os, 'geteuid', return_value=0), patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'Root invocation'):
                INSTALL.runtime_user()
        with patch.object(INSTALL.os, 'geteuid', return_value=0), \
             patch.dict(os.environ, {'SUDO_USER': 'runtime'}), \
             patch.object(INSTALL.pwd, 'getpwnam', return_value=USER):
            self.assertEqual(INSTALL.runtime_user(), USER)
        with patch.object(INSTALL.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=0)):
            with self.assertRaisesRegex(ValueError, 'non-root'):
                INSTALL.runtime_user('root')

    def test_user_environment_does_not_inherit_root_audio_or_venv(self):
        with patch.object(INSTALL.os, 'geteuid', return_value=0):
            cmd = INSTALL.user_command(USER, ['pactl', 'info'], session=True)
        self.assertEqual(cmd[:6], ['sudo', '-H', '-u', 'runtime', '--', 'env'])
        self.assertIn('-i', cmd)
        self.assertEqual(cmd[cmd.index('-C') + 1], USER.pw_dir)
        self.assertIn('HOME=/home/runtime', cmd)
        self.assertIn('XDG_RUNTIME_DIR=/run/user/1001', cmd)
        self.assertIn('DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus', cmd)
        self.assertNotIn('VIRTUAL_ENV', ' '.join(cmd))

    def test_explicit_venv_symlink_not_resolved_and_wrong_python_rejected(self):
        args = INSTALL.parser().parse_args(['--venv', '/home/runtime/venv'])
        args.stages = ['core']
        runner = Mock(return_value=json.dumps(dict(prefix='/venv', base='/usr', version=[3, 14, 4])))
        with patch.object(INSTALL.os, 'access', return_value=True):
            value = INSTALL.selected_python(args, USER, 'orange-pi', runner)
            self.assertEqual(value, '/home/runtime/venv/bin/python')
            runner.return_value = json.dumps(dict(prefix='/usr', base='/usr', version=[3, 14, 4]))
            with self.assertRaisesRegex(ValueError, 'not a venv'):
                INSTALL.selected_python(args, USER, 'orange-pi', runner)
            runner.return_value = json.dumps(dict(prefix='/venv', base='/usr', version=[3, 12, 0]))
            with self.assertRaisesRegex(ValueError, '3.14.4'):
                INSTALL.selected_python(args, USER, 'orange-pi', runner)
        args.venv = None
        with self.assertRaisesRegex(ValueError, 'Select the existing'):
            INSTALL.selected_python(args, USER, 'orange-pi', runner)

    def test_actual_shell_checkout_preserved_when_platform_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'fusion-hat'
            repo.mkdir()
            shutil.copy(ROOT / 'install.sh', repo)
            shutil.copytree(ROOT / 'driver', repo / 'driver', ignore=shutil.ignore_patterns('__pycache__'))
            sentinel = repo / 'local-work.txt'
            sentinel.write_text('keep my changes')
            before = {str(p.relative_to(repo)): p.read_bytes() for p in repo.rglob('*') if p.is_file()}
            result = subprocess.run(['bash', repo / 'install.sh'], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Detected:', result.stdout)
            self.assertIn('Unsupported', result.stderr)
            for name, content in before.items():
                self.assertEqual((repo / name).read_bytes(), content)


class OrangeStages(unittest.TestCase):
    def setUp(self):
        self.args = INSTALL.parser().parse_args([])
        self.args.stages = ['core', 'audio']
        self.core, self.audio = Mock(), Mock()
        self.core.preflight.return_value = ({}, Path('/mock.dtb'), None, USER)
        self.audio.preflight.return_value = ({}, Mock(read_bytes=lambda: b'dtb'), None)
        self.core_module = SimpleNamespace(Installation=lambda: self.core, TOKEN='fusion-hat-core', digest=INSTALL.CORE.digest)
        self.audio_module = SimpleNamespace(Installation=lambda: self.audio, TOKEN='fusion-hat-audio')
        self.patches = [patch.object(INSTALL, 'load', side_effect=[self.core_module, self.audio_module]),
                        patch.object(INSTALL.shutil, 'copyfile'), patch.object(INSTALL, 'active_core', return_value=True),
                        patch.object(INSTALL, 'trusted_headers', return_value='/trusted/headers')]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.runner = Mock()

    def test_fresh_core_stops_before_audio_until_reboot(self):
        state = {'phase': 'installed'}
        with patch.object(INSTALL, 'verify_owned', side_effect=[None, state]), \
             patch.object(INSTALL, 'active_core', return_value=False):
            with self.assertRaisesRegex(INSTALL.Incomplete, 'reboot required'):
                INSTALL.orange(self.args, ROOT, USER, '/venv/bin/python', self.runner)
        self.core.install.assert_called_once()
        self.audio.preflight.assert_not_called()
        self.assertTrue(any('build-module.sh' in ' '.join(map(str, call.args[0])) for call in self.runner.call_args_list))
        self.assertTrue(any('install-core-python.sh' in ' '.join(map(str, call.args[0])) for call in self.runner.call_args_list))

    def test_core_active_audio_installed_but_not_active_requires_reboot(self):
        self.args.stages = ['audio']
        with patch.object(INSTALL, 'verify_owned', return_value={'phase': 'installed'}), \
             patch.object(INSTALL.Path, 'exists', return_value=False):
            with self.assertRaisesRegex(INSTALL.Incomplete, 'Audio files installed; reboot'):
                INSTALL.orange(self.args, ROOT, USER, None, self.runner)
        self.core.preflight.assert_not_called()
        self.audio.preflight.assert_called_once_with('runtime')
        self.audio.install.assert_called_once()

    def test_active_rerun_delegates_idempotence_and_permissions_check(self):
        self.core.preflight.return_value = ({}, Path('/mock.dtb'), {'phase': 'installed'}, USER)
        with patch.object(INSTALL, 'verify_owned', return_value={'phase': 'installed'}), \
             patch.object(INSTALL.Path, 'exists', return_value=True):
            INSTALL.orange(self.args, ROOT, USER, '/venv/bin/python', self.runner)
        self.core.install.assert_called_once()
        self.audio.install.assert_called_once()
        commands = [' '.join(map(str, c.args[0])) for c in self.runner.call_args_list]
        self.assertFalse(any('build-module.sh' in c for c in commands))
        self.assertIn('orange_pi_zero3w_audio.py check', commands[-1])

    def test_preflight_failure_cannot_build_install_or_run_audio(self):
        self.core.preflight.side_effect = ValueError('conflicting module configuration')
        with patch.object(INSTALL, 'verify_owned', return_value=None):
            with self.assertRaisesRegex(ValueError, 'conflicting'):
                INSTALL.orange(self.args, ROOT, USER, '/venv/bin/python', self.runner)
        self.core.install.assert_not_called()
        self.runner.assert_not_called()
        self.audio.preflight.assert_not_called()

    def test_check_does_not_install_python_or_files(self):
        self.args.check = True
        self.core.preflight.return_value = ({}, Path('/mock.dtb'), {'phase': 'installed'}, USER)
        with patch.object(INSTALL, 'verify_owned', return_value={'phase': 'installed'}), \
             patch.object(INSTALL.Path, 'exists', return_value=True):
            INSTALL.orange(self.args, ROOT, USER, '/venv/bin/python', self.runner)
        self.core.install.assert_not_called()
        self.audio.install.assert_not_called()
        self.assertFalse(any('pip' in map(str, c.args[0]) for c in self.runner.call_args_list))


class AudioServicesAndRouting(unittest.TestCase):
    def endpoint(self, name, monitor=False):
        return dict(name=name, monitor_of_sink=0 if monitor else 4294967295,
                    properties={'alsa.card': '7', 'device.bus_path': 'platform-fusion-hat-audio'})

    def test_real_microphone_selected_excluding_monitors(self):
        actual = self.endpoint('alsa_input.platform-fusion-hat-audio.stereo-fallback')
        monitor = self.endpoint('alsa_output.platform-fusion-hat-audio.stereo-fallback.monitor', True)
        self.assertEqual(VOICE.select_endpoint([monitor, actual], '7', 'fusionhataudio', True), actual['name'])
        with self.assertRaisesRegex(RuntimeError, 'exactly one'):
            VOICE.select_endpoint([monitor], '7', 'fusionhataudio', True)
        impostor = self.endpoint('hat-ish')
        impostor['properties']['alsa.card'] = '8'
        with self.assertRaises(RuntimeError):
            VOICE.select_endpoint([impostor], '7', 'fusionhataudio')
        with self.assertRaises(RuntimeError):
            VOICE.select_endpoint([actual, actual], '7', 'fusionhataudio', True)

    def test_existing_stack_detection_is_conservative(self):
        self.assertEqual(VOICE.service_choice('pipewire-pulse.service enabled', {'pulseaudio'}), 'pipewire-pulse')
        self.assertEqual(VOICE.service_choice('pulseaudio.service static', set()), 'pulseaudio')
        self.assertEqual(VOICE.service_choice('', {'wireplumber'}), 'existing-incomplete')
        self.assertIsNone(VOICE.service_choice('', set()))

    def test_reuses_working_server_without_start_or_apt(self):
        calls = []
        def runner(args, capture=False):
            calls.append(args)
            return 'PulseAudio (on PipeWire)' if args[:2] == ['pactl', 'info'] else ''
        with patch.object(VOICE, 'session_ready', return_value=True):
            VOICE.prepare_service(USER, True, False, runner, lambda u, a, session: a)
        self.assertFalse(any(c[0] in ('apt-get', 'systemctl') for c in calls))

    def test_missing_session_is_incomplete_with_exact_session_commands(self):
        output = io.StringIO()
        with patch.object(VOICE, 'session_ready', return_value=False), contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, 'session is unavailable'):
                VOICE.prepare_service(USER, True, False, Mock(return_value=''), lambda *a, **kw: [])
        self.assertIn('systemctl --user list-unit-files', output.getvalue())
        self.assertIn('pactl info', output.getvalue())

    def test_explicit_pulse_install_and_start_runs_in_user_context(self):
        calls = []
        def runner(args, capture=False):
            calls.append(args)
            if args[:2] == ['user', 'pactl'] and not any(c[0] == 'apt-get' for c in calls):
                raise subprocess.CalledProcessError(1, args)
            return ''
        with patch.object(VOICE, 'session_ready', return_value=True):
            VOICE.prepare_service(USER, True, False, runner, lambda u, a, session: ['user', *a])
        self.assertIn(['apt-get', 'install', '-y', 'pulseaudio', 'pulseaudio-utils'], calls)
        self.assertIn(['user', 'systemctl', '--user', 'start', 'pulseaudio.socket', 'pulseaudio.service'], calls)

    def test_no_stack_replacement_even_with_install_option(self):
        calls = []
        def runner(args, capture=False):
            calls.append(args)
            if args[0] == 'dpkg-query':
                return 'install ok installed' if args[-1] == 'wireplumber' else ''
            if args[0] == 'pactl':
                raise subprocess.CalledProcessError(1, args)
            return ''
        with patch.object(VOICE, 'session_ready', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'No replacement'):
                VOICE.prepare_service(USER, True, False, runner, lambda u, a, session: a)
        self.assertFalse(any(c[0] == 'apt-get' for c in calls))

    def test_routing_opt_in_receipt_and_partial_failure_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            defaults = dict(source='hdmi.monitor', sink='hdmi')
            calls = []
            fail_sink = False
            def runner(args, capture=False):
                calls.append(args)
                if args[1:4] == ['-f', 'json', 'list']:
                    return json.dumps([self.endpoint('hat.source' if args[-1] == 'sources' else 'hat.sink')])
                if args[1].startswith('get-default-'):
                    return defaults[args[1][12:]]
                if args[1].startswith('set-default-'):
                    if fail_sink and args[1] == 'set-default-sink':
                        raise subprocess.CalledProcessError(1, args)
                    defaults[args[1][12:]] = args[2]
                return ''
            self.assertFalse(VOICE.routing('7', 'fusionhataudio', runner=runner, state_home=Path(tmp)))
            self.assertFalse(any('set-default' in c[1] for c in calls))
            self.assertTrue(VOICE.routing('7', 'fusionhataudio', change=True, runner=runner, state_home=Path(tmp)))
            receipt = next(Path(tmp).glob('fusion-hat/routing-*.json'))
            self.assertEqual(json.loads(receipt.read_text())['previous'], dict(source='hdmi.monitor', sink='hdmi'))
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
            defaults.update(source='hdmi.monitor', sink='hdmi')
            fail_sink = True
            with self.assertRaises(subprocess.CalledProcessError):
                VOICE.routing('7', 'fusionhataudio', change=True, runner=runner, state_home=Path(tmp))
            self.assertEqual(len(list(Path(tmp).glob('fusion-hat/routing-*.json'))), 2)

class DispatchAndFailure(unittest.TestCase):
    def setUp(self):
        # The cloud /tmp mount has a synthetic owner; privileged metadata is mocked.
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temporary = Path(temporary.name)
        parent = patch.object(INSTALL, 'trusted_temporary_root', return_value=temporary.name)
        parent.start()
        self.addCleanup(parent.stop)

    def test_pi_and_orange_dispatch_from_snapshot_preserve_checkout(self):
        # Both branches are reached through main with all privilege/hardware
        # boundaries mocked, including chown. The actual copy is exercised.
        current_user = pwd.getpwuid(os.geteuid())
        for platform in ('orange-pi', 'raspberry-pi'):
            with self.subTest(platform=platform), \
                 patch.object(INSTALL, 'detect', return_value=platform), \
                 patch.object(INSTALL, 'source_revision', return_value=('local revision', None, 'a' * 40)), \
                 patch.object(INSTALL, 'runtime_user', return_value=current_user), \
                 patch.object(INSTALL.os, 'geteuid', return_value=0), \
                 patch.object(INSTALL.os, 'chown'), \
                 patch.object(INSTALL, 'selected_python', return_value='/venv/bin/python'), \
                 patch.object(INSTALL, 'orange') as orange, patch.object(INSTALL, 'raspberry') as pi:
                invoked = orange if platform == 'orange-pi' else pi
                def stage(args, source, user, python, *, user_source):
                    self.assertNotEqual(source, ROOT)
                    self.assertEqual((source / 'driver/Makefile').read_bytes(), (ROOT / 'driver/Makefile').read_bytes())
                    self.assertFalse((source / 'work').exists())
                    (source / 'build-artifact').write_text('temporary')
                invoked.side_effect = stage
                self.assertEqual(INSTALL.main(['--core']), 0)
                invoked.assert_called_once()
                (pi if platform == 'orange-pi' else orange).assert_not_called()
                self.assertFalse(invoked.call_args.args[1].exists())
                self.assertFalse((ROOT / 'build-artifact').exists())

    def test_reboot_pause_reports_exact_resume_without_failure_warning_or_voice(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(INSTALL, 'detect', return_value='orange-pi'), \
             patch.object(INSTALL, 'source_revision', return_value=('local revision', None, 'a' * 40)), \
             patch.object(INSTALL, 'runtime_user', return_value=USER), \
             patch.object(INSTALL.os, 'geteuid', return_value=0), patch.object(INSTALL.os, 'chown'), \
             patch.object(INSTALL, 'selected_python', return_value='/home/runtime/venv/bin/python'), \
             patch.object(INSTALL, 'orange', side_effect=INSTALL.RebootRequired('core')), \
             patch.object(INSTALL, 'command') as cmd, contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(INSTALL.main(['--core', '--audio', '--voice', '--set-audio-defaults']), 3)
        cmd.assert_not_called()
        self.assertIn('reboot required', output.getvalue())
        for flag in ('--core', '--audio', '--voice', '--set-audio-defaults', '--runtime-user runtime', '--python /home/runtime/venv/bin/python'):
            self.assertIn(flag, output.getvalue())
        self.assertIn('REBOOT REQUIRED', output.getvalue())
        self.assertIn('Waiting for activation before continuing: audio, voice.', output.getvalue())
        self.assertIn('sudo reboot', output.getvalue())
        self.assertNotIn('manifest', output.getvalue())
        self.assertNotIn('failed', output.getvalue().lower())
        self.assertEqual(errors.getvalue(), '')
        self.assertIsNone(INSTALL.OUTPUT)

    def test_failure_retains_diagnostics_and_resume_without_claiming_reboot(self):
        errors = io.StringIO()
        with patch.object(INSTALL, 'detect', return_value='orange-pi'), \
             patch.object(INSTALL, 'source_revision', return_value=('local revision', None, 'a' * 40)), \
             patch.object(INSTALL, 'runtime_user', return_value=USER), \
             patch.object(INSTALL.os, 'geteuid', return_value=0), patch.object(INSTALL.os, 'chown'), \
             patch.object(INSTALL, 'selected_python', return_value='/venv/bin/python'), \
             patch.object(INSTALL, 'orange', side_effect=ValueError('untrusted header input')), \
             contextlib.redirect_stderr(errors):
            self.assertEqual(INSTALL.main(['--core', '--audio', '--verbose']), 1)
        self.assertIn('INSTALLATION FAILED: untrusted header input', errors.getvalue())
        self.assertIn('manifest/rollback', errors.getvalue())
        self.assertIn('--verbose', errors.getvalue())
        self.assertNotIn('sudo reboot', errors.getvalue())
        log, = self.temporary.glob('*.log')
        self.assertIn('ValueError: untrusted header input', log.read_text())

    def test_audio_pause_preserves_core_progress_and_waits_for_voice(self):
        output, errors = io.StringIO(), io.StringIO()
        def audio_pending(*args, **kwargs):
            INSTALL.report('Core: files present; active binding and both MCU policy checks passed.')
            print('noisy package diagnostics')
            raise INSTALL.RebootRequired('audio')
        with patch.object(INSTALL, 'detect', return_value='orange-pi'), \
             patch.object(INSTALL, 'source_revision', return_value=('local revision', None, 'a' * 40)), \
             patch.object(INSTALL, 'runtime_user', return_value=USER), \
             patch.object(INSTALL.os, 'geteuid', return_value=0), patch.object(INSTALL.os, 'chown'), \
             patch.object(INSTALL, 'selected_python', return_value='/venv/bin/python'), \
             patch.object(INSTALL, 'orange', side_effect=audio_pending), \
             patch.object(INSTALL, 'load') as voice, \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(INSTALL.main(['--core', '--audio', '--voice']), 3)
        self.assertIn('Core: files present; active binding', output.getvalue())
        self.assertIn('Audio files installed; reboot required', output.getvalue())
        self.assertIn('Waiting for activation before continuing: voice.', output.getvalue())
        self.assertNotIn('noisy package diagnostics', output.getvalue())
        self.assertNotIn('Source snapshot SHA256:', output.getvalue())
        log, = self.temporary.glob('*.log')
        self.assertIn('noisy package diagnostics', log.read_text())
        self.assertEqual(errors.getvalue(), '')
        voice.assert_not_called()


class Bootstrap(unittest.TestCase):
    def test_bootstrap_default_and_override_fetch_once_and_clean_up(self):
        for options, expected in (([], ('noog6/fusion-hat', 'v1')), (['--repo', 'other/fork', '--ref', 'topic'], ('other/fork', 'topic')), (['--repo=other/fork', '--ref=topic'], ('other/fork', 'topic'))):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                shutil.copy(ROOT / 'install.sh', directory)
                bin_dir = directory / 'bin'
                bin_dir.mkdir()
                log = directory / 'calls'
                report = directory / 'report.json'
                # Fake Git supplies only an inert dispatcher that records source
                # revision and argv; no remote request or install is performed.
                fake = bin_dir / 'git'
                fake.write_text('''#!/usr/bin/env python3
import json,os,pathlib,sys
args=sys.argv[1:]
with open(os.environ['TEST_GIT_LOG'],'a') as f: f.write(json.dumps(args)+'\\n')
if args[0]=='init':
    root=pathlib.Path(args[-1]); (root/'driver').mkdir(parents=True)
    (root/'driver/install.py').write_text("import json,os,sys; from pathlib import Path; Path(os.environ['TEST_REPORT']).write_text(json.dumps(dict(path=__file__,repo=os.environ['FUSION_HAT_BOOTSTRAP_REPOSITORY'],revision=os.environ['FUSION_HAT_BOOTSTRAP_REVISION'],args=sys.argv[1:])))")
elif 'rev-parse' in args: print('a'*40)
''')
                fake.chmod(0o755)
                env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'], TEST_GIT_LOG=str(log), TEST_REPORT=str(report), TMPDIR=tmp)
                for name in ('FUSION_HAT_BRANCH', 'FUSION_HAT_REPOSITORY', 'FUSION_HAT_BOOTSTRAP_REVISION', 'FUSION_HAT_BOOTSTRAP_REPOSITORY'):
                    env.pop(name, None)
                result = subprocess.run(['bash', directory / 'install.sh', *options], cwd=tmp, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = [json.loads(line) for line in log.read_text().splitlines()]
                fetches = [c for c in calls if 'fetch' in c]
                self.assertEqual(len(fetches), 1)
                self.assertEqual(fetches[0][-2:], ['https://github.com/' + expected[0] + '.git', expected[1]])
                data = json.loads(report.read_text())
                self.assertEqual(data['repo'], expected[0])
                self.assertEqual(data['revision'], 'a' * 40)
                self.assertEqual(data['args'], options)
                self.assertFalse(Path(data['path']).exists())
                self.assertFalse(list(directory.glob('fusion-hat-bootstrap.*')))

    def test_piped_invocation_from_checkout_uses_local_source(self):
        result = subprocess.run(['bash', '-s', '--', '--help'], input=(ROOT / 'install.sh').read_text(), cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Platform-aware orchestration', result.stdout)
        self.assertNotIn('Standalone bootstrap:', result.stdout)

class PiWorkflow(unittest.TestCase):
    def test_fresh_pi_stages_boot_then_rerun_retains_driver_and_uses_safe_audio(self):
        args = INSTALL.parser().parse_args([])
        args.stages = ['core', 'audio']
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / 'boot/firmware/config.txt'
            config.parent.mkdir(parents=True)
            config.write_text('# existing\ndtparam=i2c_arm=on\n')
            calls = []
            installed = False
            def runner(command, capture=False):
                calls.append(list(map(str, command)))
                if command[:2] == ['modinfo', '-n']:
                    if not installed:
                        raise subprocess.CalledProcessError(1, command)
                    return '/lib/modules/current/updates/dkms/fusion_hat.ko'
                return '# completion' if capture else ''
            with patch.object(INSTALL.shutil, 'which', return_value='/usr/bin/raspi-config'):
                with self.assertRaisesRegex(INSTALL.Incomplete, 'reboot explicitly'):
                    INSTALL.raspberry(args, ROOT, USER, None, runner, root)
                self.assertIn(INSTALL.kernel_build(['make', '-C', str(ROOT / 'driver'), 'BOOT_ONLY=1', 'install']), calls)
                self.assertFalse(any('setup_fusion_hat_audio.sh' in ' '.join(c) for c in calls))
                self.assertEqual(config.read_text().count('dtoverlay=sunfounder-fusionhat'), 1)
                self.assertEqual(config.with_name('config.txt.fusion-hat.before').read_text(), '# existing\ndtparam=i2c_arm=on\n')
                (root / 'sys/module/fusion_hat').mkdir(parents=True)
                firmware = root / 'sys/class/fusion_hat/fusion_hat/firmware_version'
                firmware.parent.mkdir(parents=True)
                firmware.write_text('1.0.2')
                installed = True
                calls.clear()
                INSTALL.raspberry(args, ROOT, USER, None, runner, root)
                self.assertFalse(any('make' in c for c in calls))
                audio = next(c for c in calls if 'setup_fusion_hat_audio.sh' in ' '.join(c))
                self.assertIn('--non-actuating', audio)
                self.assertIn('--runtime-user', audio)
                self.assertIn('--keep-defaults', audio)
                self.assertEqual(config.read_text().count('dtoverlay=sunfounder-fusionhat'), 1)


class StateAndCheckFailures(unittest.TestCase):
    def test_active_core_distinguishes_reboot_from_policy_and_binding_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertFalse(INSTALL.active_core(root))
            module = root / 'sys/module/fusion_hat/parameters'
            module.mkdir(parents=True)
            for flag in ('disable_auto_shutdown', 'restrict_controls'):
                (module / flag).write_text('Y')
            with self.assertRaisesRegex(ValueError, 'partially active'):
                INSTALL.active_core(root)
            driver = root / 'sys/bus/i2c/drivers/fusion_hat'
            driver.mkdir(parents=True)
            client = root / 'sys/bus/i2c/devices/0-0017'
            (client / 'of_node').mkdir(parents=True)
            (client / 'driver').symlink_to(driver)
            with self.assertRaisesRegex(ValueError, 'not created'):
                INSTALL.active_core(root)
            (client / 'of_node/sunfounder,experimental-core').touch()
            self.assertTrue(INSTALL.active_core(root))
            (module / 'restrict_controls').write_text('N')
            with self.assertRaisesRegex(ValueError, 'policy is unsuitable'):
                INSTALL.active_core(root)

    def test_preparing_manifest_fails_closed(self):
        installation = Mock(spec=INSTALL.CORE.Installation)
        installation.state.return_value = dict(phase='preparing', user='runtime', files={})
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            INSTALL.verify_owned(installation, USER, 'fusion-hat-core')
        installation.state.return_value = dict(phase='installed', user='somebody', files={})
        with self.assertRaisesRegex(ValueError, 'another user'):
            INSTALL.verify_owned(installation, USER, 'fusion-hat-core')

    def test_check_never_starts_existing_inactive_service(self):
        calls = []
        def runner(args, capture=False):
            calls.append(args)
            if args[0] == 'pactl':
                raise subprocess.CalledProcessError(1, args)
            if args[0] == 'systemctl':
                return 'pulseaudio.service enabled'
            return ''
        with patch.object(VOICE, 'session_ready', return_value=True):
            with self.assertRaisesRegex(VOICE.Pending, 'systemctl --user daemon-reload'):
                VOICE.prepare_service(USER, True, True, runner, lambda u, a, session: a)
        self.assertFalse(any('start' in c or c[0] == 'apt-get' for c in calls))

    def test_voice_pin_matches_project_metadata(self):
        self.assertIn(VOICE.VOICE_REVISION, (ROOT / 'pyproject.toml').read_text())


class HardwareIdentity(unittest.TestCase):
    def test_card_requires_dt_driver_and_non_root_node_access(self):
        for platform in ('orange-pi', 'raspberry-pi'):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                sysfs, dev = root / 'sys', root / 'dev'
                card = sysfs / 'class/sound/card7'
                (card / 'device').mkdir(parents=True)
                card_id = 'fusionhataudio' if platform == 'orange-pi' else 'sndrpigooglevoi'
                (card / 'id').write_text(card_id)
                node = sysfs / ('firmware/devicetree/base/fusion-hat-audio' if platform == 'orange-pi' else 'firmware/devicetree/base/sound')
                node.mkdir(parents=True)
                (node / 'compatible').write_bytes(b'googlevoicehat,googlevoicehat-soundcard\0')
                (card / 'device/of_node').symlink_to(node)
                driver = sysfs / ('bus/platform/drivers/sunxi-snd-mach' if platform == 'orange-pi' else 'bus/platform/drivers/snd-rpi-simple')
                driver.mkdir(parents=True)
                (card / 'device/driver').symlink_to(driver)
                (dev / 'snd').mkdir(parents=True)
                for name in ('controlC7', 'pcmC7D0p', 'pcmC7D0c'):
                    (dev / 'snd' / name).touch()
                self.assertEqual(VOICE.hardware_card(platform, sysfs, dev), ('7', card_id))
                with patch.object(VOICE.os, 'access', return_value=False):
                    with self.assertRaisesRegex(RuntimeError, 'permission missing'):
                        VOICE.hardware_card(platform, sysfs, dev)
                (card / 'device/driver').unlink()
                (card / 'device/driver').symlink_to(sysfs / 'bus/platform/drivers/unrelated')
                with self.assertRaisesRegex(RuntimeError, 'unexpected driver'):
                    VOICE.hardware_card(platform, sysfs, dev)

    def test_bootstrap_failure_cleans_private_download_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            shutil.copy(ROOT / 'install.sh', directory)
            bin_dir = directory / 'bin'
            bin_dir.mkdir()
            git = bin_dir / 'git'
            git.write_text('#!/bin/bash\nif [[ $1 == init ]]; then mkdir -p "${@: -1}"; else exit 17; fi\n')
            git.chmod(0o755)
            env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'], TMPDIR=tmp)
            result = subprocess.run(['bash', directory / 'install.sh'], cwd=tmp, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 17)
            self.assertFalse(list(directory.glob('fusion-hat-bootstrap.*')))


class ForkUpdate(unittest.TestCase):
    def setUp(self):
        self.cli = load('fusion_hat._cli', 'fusion_hat/_cli.py')

    def test_cli_update_passes_active_venv_and_propagates_exit(self):
        with patch('subprocess.run', side_effect=[SimpleNamespace(stdout=b'installer'), SimpleNamespace(returncode=3)]) as run, \
             patch('sys.prefix', '/runtime/venv'), patch('sys.base_prefix', '/usr'), \
             patch('sys.executable', '/runtime/venv/bin/python'):
            self.assertEqual(self.cli.update(), 3)
        self.assertEqual(run.call_args_list[0].args[0], ['curl', '-fsSL', 'https://raw.githubusercontent.com/noog6/fusion-hat/v1/install.sh'])
        self.assertEqual(run.call_args_list[1].args[0], ['sudo', 'bash', '-s', '--', '--python', '/runtime/venv/bin/python'])
        self.assertEqual(run.call_args_list[1].kwargs['input'], b'installer')

    def test_cli_explicit_selector_overrides_active_and_console_status_is_returned(self):
        with patch('sys.argv', ['fusion_hat', 'update', '--venv', '/different venv', '--runtime-user', 'runtime']), \
             patch('subprocess.run', side_effect=[SimpleNamespace(stdout=b'script'), SimpleNamespace(returncode=19)]) as run:
            self.assertEqual(self.cli.main(), 19)
        self.assertEqual(run.call_args_list[1].args[0][-4:], ['--python', '/different venv/bin/python', '--runtime-user', 'runtime'])

    def test_cli_download_failure_never_starts_installer(self):
        with patch('subprocess.run', side_effect=subprocess.CalledProcessError(7, ['curl'])) as run:
            self.assertEqual(self.cli.update(python='/venv/bin/python'), 7)
        self.assertEqual(run.call_count, 1)


if __name__ == '__main__':
    unittest.main()
