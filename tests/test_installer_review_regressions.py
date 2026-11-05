"""PR #5 regressions: privilege staging, scoped voice dependencies and Git tags."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
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


INSTALL = load('review_dispatcher', 'driver/install.py')
VOICE = load('review_voice', 'driver/voice_setup.py')
PREFLIGHT = load('review_core_preflight', 'driver/core_python_preflight.py')
USER = SimpleNamespace(pw_name='runtime', pw_uid=1001, pw_gid=1001, pw_dir='/home/runtime')


class PrivilegeStaging(unittest.TestCase):
    def setUp(self):
        self.validate_temp_parent = INSTALL.trusted_temporary_root
        # /tmp ownership is a mocked OS boundary, independent of real copy modes.
        parent = patch.object(INSTALL, 'trusted_temporary_root', return_value='/tmp')
        parent.start()
        self.addCleanup(parent.stop)

    def test_privileged_temp_parent_requires_root_and_sticky_protection(self):
        for owner, mode in ((1001, 0o41777), (0, 0o40777)):
            with patch.object(INSTALL.Path, 'stat', return_value=SimpleNamespace(st_uid=owner, st_mode=mode)):
                with self.assertRaisesRegex(ValueError, 'root-owned /tmp'):
                    self.validate_temp_parent()
        with patch.object(INSTALL.Path, 'stat', return_value=SimpleNamespace(st_uid=0, st_mode=0o41777)):
            self.assertEqual(self.validate_temp_parent(), '/tmp')

    def test_selected_other_account_cannot_change_privileged_copy_via_staging(self):
        # chown is mocked; real file copies and modes exercise the trust boundary.
        # The chosen UID differs from the process that creates privileged inputs.
        self.assertNotEqual(USER.pw_uid, os.getuid())
        with patch.object(INSTALL.os, 'chown') as chown:
            with INSTALL.staging(USER) as (privileged, user_source):
                self.assertNotEqual(privileged.parent, user_source.parent)
                self.assertEqual(privileged.parent.stat().st_mode & 0o777, 0o700)
                privileged_files = ['driver/Makefile', 'driver/voice_setup.py',
                                    'driver/build-module.sh', 'fusion_hat/scripts/setup_fusion_hat_audio.sh']
                for name in privileged_files:
                    original = (privileged / name).read_bytes()
                    (user_source / name).write_bytes(b'runtime-controlled replacement')
                    self.assertEqual((privileged / name).read_bytes(), original)
                    self.assertEqual((privileged / name).stat().st_uid, os.getuid())
                    self.assertEqual((privileged / name).stat().st_mode & 0o077, 0)
                (user_source / 'driver/fusion_hat.ko').write_bytes(b'untrusted module')
                self.assertFalse((privileged / 'driver/fusion_hat.ko').exists())
                self.assertTrue(all(Path(call.args[0]).is_relative_to(user_source.parent)
                                    for call in chown.call_args_list))
            self.assertFalse(privileged.parent.exists())
            self.assertFalse(user_source.parent.exists())

    def test_root_service_import_ignores_runtime_replacement(self):
        paths = []
        original_load = INSTALL.load
        def root_load(name, path):
            paths.append(Path(path))
            module = original_load(name, path)
            module.prepare_service = Mock()
            return module
        def hardware(args, source, user, python, *, user_source):
            (user_source / 'driver/voice_setup.py').write_text('raise AssertionError("executed as root")')
            self.assertNotEqual(source, user_source)
            self.user_source = user_source
            self.privileged = source
        with patch.object(INSTALL, 'detect', return_value='orange-pi'), \
             patch.object(INSTALL, 'source_revision', return_value=('reviewed source', None, 'a' * 40)), \
             patch.object(INSTALL, 'runtime_user', return_value=USER), \
             patch.object(INSTALL.os, 'geteuid', return_value=0), patch.object(INSTALL.os, 'chown'), \
             patch.object(INSTALL, 'selected_python', return_value='/runtime/venv/bin/python'), \
             patch.object(INSTALL, 'orange', side_effect=hardware), \
             patch.object(INSTALL, 'load', side_effect=root_load), patch.object(INSTALL, 'command') as command:
            self.assertEqual(INSTALL.main(['--voice']), 0)
        self.assertEqual(paths, [self.privileged / 'driver/voice_setup.py'])
        self.assertIn(str(self.user_source / 'driver/voice_setup.py'), command.call_args.args[0])
        self.assertIn('runtime', command.call_args.args[0])

    def test_orange_kernel_output_is_built_and_consumed_only_in_privileged_tree(self):
        with patch.object(INSTALL.os, 'chown'), INSTALL.staging(USER) as (source, user_source):
            (user_source / 'driver/fusion_hat.ko').write_bytes(b'untrusted')
            core = Mock()
            core.preflight.return_value = ({}, Path('/dtb'), None, USER)
            core.artifacts.side_effect = lambda *a: (source / 'driver/fusion_hat.ko').read_bytes()
            modules = [SimpleNamespace(Installation=lambda: core, TOKEN='fusion-hat-core'),
                       SimpleNamespace(Installation=Mock(), TOKEN='fusion-hat-audio')]
            calls = []
            def runner(args, capture=False):
                args = list(map(str, args))
                calls.append(args)
                if str(source / 'driver/build-module.sh') in args:
                    self.assertEqual(args[:2], ['env', '-i'])
                    self.assertNotIn('sudo', args)
                    self.assertNotIn(str(user_source), ' '.join(args))
                    (source / 'driver/fusion_hat.ko').write_bytes(b'privileged build output')
                return ''
            args = INSTALL.parser().parse_args(['--core'])
            args.stages = ['core']
            with patch.object(INSTALL, 'load', side_effect=modules), \
                 patch.object(INSTALL, 'trusted_headers', return_value='/trusted/headers'), \
                 patch.object(INSTALL, 'verify_owned', side_effect=[None, {'phase': 'installed'}]), \
                 patch.object(INSTALL, 'active_core', return_value=False):
                with self.assertRaises(INSTALL.Incomplete):
                    INSTALL.orange(args, source, USER, '/venv/bin/python', runner, user_source=user_source)
            self.assertEqual(core.install.call_args.args[0], b'privileged build output')
            python_setup = next(c for c in calls if str(user_source / 'driver/install-core-python.sh') in c)
            self.assertIn('sudo', python_setup)
            self.assertIn('runtime', python_setup)

    def test_header_validation_rejects_runtime_owned_or_writable_targets(self):
        # Simulate root ownership on a real fixture tree; no privileged chmod or
        # real headers are touched. Symlink targets and their parents are checked.
        with tempfile.TemporaryDirectory(dir=ROOT / 'work') as tmp:
            directory = Path(tmp)
            headers = directory / 'headers'
            headers.mkdir()
            makefile = headers / 'Makefile'
            makefile.write_text('# fixture')
            link = directory / 'build'
            link.symlink_to(headers)
            original_stat = Path.stat
            malicious = None
            def root_stat(path, *args, **kwargs):
                values = list(original_stat(path, *args, **kwargs))
                values[4] = 1001 if path == malicious else 0
                return os.stat_result(values)
            with patch.object(Path, 'stat', root_stat):
                self.assertEqual(INSTALL.trusted_headers(link), str(headers))
                malicious = makefile
                with self.assertRaisesRegex(ValueError, 'root-owned'):
                    INSTALL.trusted_headers(link)
                malicious = None
                makefile.chmod(0o666)
                with self.assertRaisesRegex(ValueError, 'not group/world writable'):
                    INSTALL.trusted_headers(link)


class VoiceProfile(unittest.TestCase):
    def setUp(self):
        self.dependencies = {
            'fusion-hat': ['evdev', 'sunfounder-voice-assistant', 'RPi.GPIO', 'luma.core', 'pillow'],
            'evdev': [], 'packaging': [],
            'sunfounder-voice-assistant': ['piper-tts', 'vosk', 'pyaudio', 'sounddevice', 'edge-tts>=6.0'],
            'piper-tts': ['onnxruntime>=1.17'], 'onnxruntime': [],
            'vosk': ['requests'], 'requests': [], 'pyaudio': [], 'sounddevice': [], 'edge-tts': [],
            'unrelated-application': ['requests<1'],
        }
        self.versions = {'edge-tts': '6.1', 'onnxruntime': '1.18', 'requests': '2.0'}

    def distribution(self, name):
        if name not in self.dependencies:
            raise VOICE.metadata.PackageNotFoundError(name)
        return SimpleNamespace(version=self.versions.get(name, '1.0'), requires=self.dependencies[name])

    def test_fresh_core_and_voice_pass_without_optional_pi_modules(self):
        self.assertEqual(VOICE.dependency_issues(self.distribution), [])
        # Neither absent Pi extras nor an unrelated app's conflicting requirement
        # may turn a complete selected hardware/voice profile into a failure.
        with patch.object(VOICE, 'dependency_issues', return_value=[]), \
             patch.object(VOICE.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='fusion-hat requires RPi.GPIO\nunrelated-application requires requests<1')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertTrue(VOICE.software_checks(Mock()))
        self.assertIn('outside the verified core/voice profile', output.getvalue())
        self.assertIn('unrelated-application requires requests<1', output.getvalue())

    def test_missing_or_conflicting_voice_transitive_dependency_fails(self):
        self.versions['onnxruntime'] = '1.0'
        self.assertIn('onnxruntime>=1.17', '\n'.join(VOICE.dependency_issues(self.distribution)))
        del self.dependencies['sounddevice']
        self.assertIn('sounddevice is not installed', '\n'.join(VOICE.dependency_issues(self.distribution)))
        with patch.object(VOICE, 'dependency_issues', return_value=['piper-tts requires onnxruntime>=1.17']), \
             patch.object(VOICE.subprocess, 'run') as pip, contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(VOICE.software_checks(Mock()))
        pip.assert_not_called()

    def test_markers_and_requested_extras_are_respected(self):
        self.dependencies['vosk'] += ['absent; python_version < "1.0"', 'requests[security]']
        self.dependencies['requests'] = ['security-addon; extra == "security"']
        self.assertIn('security-addon is not installed', '\n'.join(VOICE.dependency_issues(self.distribution)))
        self.dependencies['security-addon'] = []
        self.assertEqual(VOICE.dependency_issues(self.distribution), [])

    def test_fresh_pi_voice_installs_fusion_in_selected_venv_before_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            installed = []
            def runner(args, capture=False):
                installed.append(list(map(str, args)))
                return 'freeze inventory' if capture else ''
            real_install = VOICE.install_python
            def check():
                self.assertIn('install-core-python.sh', ' '.join(installed[0]))
                self.assertEqual(installed[0][-3:], ['--platform', 'raspberry-pi', '/voice/venv/bin/python'])
                self.assertIn('sunfounder-voice-assistant @', ' '.join(installed[1]))
                return True
            with patch.object(VOICE.os, 'geteuid', return_value=1001), \
                 patch.object(VOICE.sys, 'prefix', '/voice/venv'), patch.object(VOICE.sys, 'base_prefix', '/usr'), \
                 patch.object(VOICE.sys, 'executable', '/voice/venv/bin/python'), \
                 patch.object(VOICE.sys, 'argv', ['voice_setup.py', '--platform', 'raspberry-pi']), \
                 patch.object(VOICE, 'hardware_card', return_value=('7', 'sndrpigooglevoi')), \
                 patch.object(VOICE.Path, 'home', return_value=Path(tmp)), \
                 patch.object(VOICE, 'install_python', side_effect=lambda platform: real_install(platform, runner)), \
                 patch.object(VOICE, 'run', side_effect=runner), patch.object(VOICE, 'routing', return_value=True), \
                 patch.object(VOICE, 'software_checks', side_effect=check):
                self.assertEqual(VOICE.main(), 0)

    def test_pi_voice_preflight_accepts_other_python_but_orange_stays_pinned(self):
        with patch.object(PREFLIGHT.sys, 'prefix', '/voice/venv'), \
             patch.object(PREFLIGHT.sys, 'base_prefix', '/usr'), \
             patch.object(PREFLIGHT.sys, 'version_info', (3, 11, 2)), \
             patch.object(PREFLIGHT.importlib, 'import_module'), patch.object(PREFLIGHT.metadata, 'version', return_value='1'):
            PREFLIGHT.check_environment('raspberry-pi')
            with self.assertRaisesRegex(RuntimeError, 'Python 3.14.4'):
                PREFLIGHT.check_environment('orange-pi')


class AnnotatedTag(unittest.TestCase):
    def test_real_git_annotated_tag_bootstrap_records_peeled_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            origin, entry = directory / 'origin', directory / 'entry'
            origin.mkdir()
            entry.mkdir()
            (origin / 'driver').mkdir()
            (origin / 'driver/install.py').write_text('''import json,os,subprocess,sys
from pathlib import Path
head=subprocess.check_output(['git','-C',str(Path(__file__).parents[1]),'rev-parse','HEAD'],text=True).strip()
revision=os.environ['FUSION_HAT_BOOTSTRAP_REVISION']
Path(os.environ['TAG_REPORT']).write_text(json.dumps(dict(head=head,revision=revision)))
sys.exit(0 if head==revision else 23)
''')
            git = shutil.which('git')
            def local_git(*args):
                return subprocess.check_output([git, '-C', str(origin), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', '-c', 'commit.gpgSign=false', '-c', 'tag.gpgSign=false', *args], text=True).strip()
            local_git('init', '-q')
            local_git('add', '.')
            local_git('commit', '-qm', 'inert installer fixture')
            local_git('tag', '-a', 'review-tag', '-m', 'annotated fixture')
            tag, commit = local_git('rev-parse', 'review-tag'), local_git('rev-parse', 'review-tag^{commit}')
            self.assertNotEqual(tag, commit)
            shutil.copy(ROOT / 'install.sh', entry)
            bin_dir = directory / 'bin'
            bin_dir.mkdir()
            wrapper = bin_dir / 'git'
            wrapper.write_text(f'''#!{sys.executable}
import subprocess,sys
args=[{str(origin)!r} if a=='https://github.com/noog6/fusion-hat.git' else a for a in sys.argv[1:]]
sys.exit(subprocess.call([{git!r},*args]))
''')
            wrapper.chmod(0o755)
            report = directory / 'report.json'
            env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'], TAG_REPORT=str(report))
            env.pop('FUSION_HAT_REPOSITORY', None)
            result = subprocess.run(['bash', entry / 'install.sh', '--ref', 'review-tag'], cwd=entry, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(report.read_text()), dict(head=commit, revision=commit))


if __name__ == '__main__':
    unittest.main()
