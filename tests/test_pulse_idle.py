"""Native PulseAudio policy, user persistence and recovery with audio mocked."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


POLICY = load('tested_pulse_idle', 'driver/pulse_idle.py')
VOICE = load('idle_shared_voice', 'driver/voice_setup.py')


class Server:
    def __init__(self, home):
        self.home, self.calls = home, []
        self.info = dict(server_name='pulseaudio', server_version='17.0')
        properties = {'alsa.id': 'fusionhataudio', 'alsa.card': '7'}
        self.sink = dict(name='alsa_output.hat', index=10, properties=dict(properties))
        self.source = dict(name='alsa_input.hat', index=20, properties=dict(properties))
        self.monitor = dict(name='alsa_output.hat.monitor', index=30, monitor_of_sink=10, properties=dict(properties))
        self.hdmi = dict(name='alsa_output.hdmi', index=11, properties={'alsa.card': '8'})
        self.modules = [dict(name=POLICY.MODULE, index=13, argument='timeout=7')]
        self.next_module, self.streams = 14, {'sink-inputs': [], 'source-outputs': []}
        self.enabled = self.running = False
        self.dropins = ''
        self.failure = None
        self.policy = POLICY

    def __call__(self, args, capture=False):
        args = list(map(str, args))
        self.calls.append(args)
        if self.failure and self.failure(args):
            if args[:3] == ['systemctl', '--user', 'restart']:
                self.running = False
            self.failure = None  # A failure must not sabotage rollback too.
            raise subprocess.CalledProcessError(1, args, stderr='mock policy boundary failed')
        if args[:4] == ['pactl', '-f', 'json', 'info']:
            return json.dumps(self.info)
        if args[:4] == ['pactl', '-f', 'json', 'list']:
            # PulseAudio 17 module JSON has no index, unlike endpoints/streams.
            json_modules = [{key: value for key, value in module.items() if key != 'index'} for module in self.modules]
            return json.dumps(dict(sinks=[self.sink, self.hdmi], sources=[self.source, self.monitor], modules=json_modules,
                                   **self.streams)[args[4]])
        if args == ['pactl', 'list', 'short', 'modules']:
            return ''.join(f'{module["index"]}\t{module["name"]}\t{module["argument"]}\t\n' for module in self.modules)
        if args[0] == 'pacmd':
            endpoint = self.sink if args[1] == 'update-sink-proplist' else self.source
            assert endpoint['name'] == args[2]
            endpoint['properties'][POLICY.PROPERTY] = json.loads(args[3].split('=', 1)[1])
        elif args[:2] == ['pactl', 'unload-module']:
            self.modules = [module for module in self.modules if str(module['index']) != args[2]]
        elif args[:2] == ['pactl', 'load-module']:
            self.modules.append(dict(name=args[2], index=self.next_module, argument=' '.join(args[3:])))
            self.next_module += 1
            return str(self.next_module - 1)
        elif args[:2] == ['systemctl', '--user']:
            command = args[2]
            if command == 'show':
                if 'DropInPaths' in args:
                    return self.dropins
                return str(POLICY.layout(self.home)['unit']) if POLICY.layout(self.home)['unit'].exists() else ''
            if command == 'is-enabled':
                return 'enabled' if self.enabled else 'disabled'
            if command == 'is-active':
                return 'active' if self.running else 'inactive'
            if command == 'enable':
                self.enabled = True
            if command == 'disable':
                self.enabled = False
                if '--now' in args:
                    self.running = False
            if command == 'stop':
                self.running = False
            if command == 'restart':
                assert args[3] == POLICY.UNIT  # Never restart the audio server.
                assert POLICY.owned(POLICY.layout(self.home))
                self.policy.apply(self, self.home)
                self.running = True
        else:
            raise AssertionError('Unexpected command: ' + str(args))
        return ''


class PulseIdle(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.server = Server(self.home)
        self.paths = POLICY.layout(self.home)
        self.identity = ['1234', 'start-ticks', 'boot-identity']
        self.original_identity = POLICY.service_identity
        for item in (patch.object(POLICY, 'shared', return_value=VOICE),
                     patch.object(VOICE, 'hardware_card', return_value=('7', 'fusionhataudio')),
                     patch.object(POLICY, 'service_identity', side_effect=lambda runner: list(self.identity))):
            item.start()
            self.addCleanup(item.stop)

    def apply(self):
        POLICY.apply(self.server, self.home)

    def configure(self, check=False):
        POLICY.configure(check, self.server, self.home)

    def test_only_hat_properties_change_and_global_module_arguments_survive(self):
        self.apply()
        self.assertEqual(self.server.sink['properties'][POLICY.PROPERTY], '0')
        self.assertEqual(self.server.source['properties'][POLICY.PROPERTY], '0')
        self.assertNotIn(POLICY.PROPERTY, self.server.hdmi['properties'])
        self.assertNotIn(POLICY.PROPERTY, self.server.monitor['properties'])
        self.assertEqual(self.server.modules[0]['argument'], 'timeout=7')
        self.assertEqual(self.paths['active'].stat().st_mode & 0o777, 0o600)
        self.assertFalse(any('default' in ' '.join(command) or 'rate' in ' '.join(command) for command in self.server.calls))

    def test_real_module_contract_does_not_depend_on_missing_json_index(self):
        fixtures = ROOT / 'tests/fixtures'
        document = json.loads((fixtures / 'pulseaudio17-modules.json').read_text())
        self.assertNotIn('index', document[0])
        listing = (fixtures / 'pulseaudio17-modules.tsv').read_text()
        self.server.modules = POLICY.list_modules(Mock(return_value=listing.strip()))
        self.assertEqual(self.server.modules[0]['name'], document[0]['name'])
        self.assertEqual(self.server.modules[0]['argument'], document[0]['argument'])
        self.assertNotIn('index', json.loads(self.server(['pactl', '-f', 'json', 'list', 'modules'], capture=True))[0])
        self.configure()
        self.configure(check=True)
        self.assertEqual(self.server.modules[0]['argument'], '')
        self.assertEqual(POLICY.snapshot(self.server)['module'], self.server.modules[0]['index'])

    def test_short_module_parser_empty_arguments_and_multi_digit_ids(self):
        runner = Mock(return_value='0\tmodule-device-restore\t\t\n21\tmodule-suspend-on-idle\ttimeout=7\t\n')
        result = POLICY.list_modules(runner)
        self.assertEqual(result, [dict(index=0, name='module-device-restore', argument=''),
                                  dict(index=21, name=POLICY.MODULE, argument='timeout=7')])

    def test_bad_module_listing_stops_before_policy_files_or_live_changes(self):
        for text in ('module-suspend-on-idle\t13\t', 'bad\tmodule-suspend-on-idle\t', '13'):
            def runner(args, capture=False):
                if args == ['pactl', 'list', 'short', 'modules']:
                    return text
                return self.server(args, capture)
            with self.subTest(text=text), self.assertRaisesRegex(RuntimeError, 'Unexpected pactl short module'):
                POLICY.configure(runner=runner, home=self.home)
            self.assertFalse(self.paths['manifest'].exists())
        self.assertFalse(any(command[0] == 'pacmd' for command in self.server.calls))

    def test_current_receipt_skips_reload_even_with_active_hat_client(self):
        self.apply()
        self.server.calls.clear()
        self.server.streams['source-outputs'] = [dict(source=20)]
        self.apply()
        self.assertFalse(any(command[0] == 'pacmd' or 'unload-module' in command for command in self.server.calls))

    def test_receipt_cannot_hide_a_new_server_instance(self):
        self.configure()
        self.identity[1] = 'new-start-ticks'
        with self.assertRaisesRegex(RuntimeError, 'pending for this server'):
            self.configure(check=True)
        self.server.calls.clear()
        self.configure()
        self.assertTrue(any('unload-module' in command for command in self.server.calls))
        self.configure(check=True)

    def test_installed_helper_reapplies_to_new_server_without_reinstalling_files(self):
        self.configure()
        files = {key: self.paths[key].read_bytes() for key in ('helper', 'shared', 'unit', 'manifest')}
        deployed = load('deployed_idle_helper', str(self.paths['helper']))
        deployed_voice = deployed.shared()  # Real copied helper import, no venv dependency.
        self.identity[:] = ['5678', 'new-start', 'boot-identity']
        for endpoint in (self.server.sink, self.server.source):
            endpoint['properties'].pop(POLICY.PROPERTY)
        self.server.modules = [dict(name=POLICY.MODULE, index=13, argument='timeout=7')]
        self.server.running = False
        self.server.policy = deployed
        with patch.object(deployed, 'shared', return_value=deployed_voice), \
             patch.object(deployed_voice, 'hardware_card', return_value=('7', 'fusionhataudio')), \
             patch.object(deployed, 'service_identity', return_value=list(self.identity)):
            self.server(['systemctl', '--user', 'restart', POLICY.UNIT])
        self.configure(check=True)
        self.assertEqual(files, {key: self.paths[key].read_bytes() for key in files})
        self.assertEqual(self.server.modules[0]['argument'], 'timeout=7')

    def test_hat_streams_preserved_until_operator_stops_apps(self):
        self.server.streams['source-outputs'] = [dict(source=20)]
        with self.assertRaisesRegex(RuntimeError, 'Stop HAT audio applications'):
            self.apply()
        self.assertFalse(any(command[0] == 'pacmd' for command in self.server.calls))

    def test_active_hat_monitor_consumer_is_preserved(self):
        self.server.streams['source-outputs'] = [dict(source=30)]
        with self.assertRaisesRegex(RuntimeError, 'Stop HAT audio applications'):
            self.apply()
        self.assertFalse(any(command[0] == 'pacmd' for command in self.server.calls))

    def test_pipewire_or_unvalidated_server_rejected_before_persistent_writes(self):
        for name, version in (('PulseAudio (on PipeWire)', '17.0'), ('pulseaudio', '16.1')):
            self.server.info.update(server_name=name, server_version=version)
            with self.subTest(name=name, version=version), self.assertRaisesRegex(RuntimeError, 'native PulseAudio 17'):
                self.configure()
            self.assertFalse(self.paths['unit'].exists())
        self.assertFalse(any(command[0] == 'pacmd' or command[:3] == ['systemctl', '--user', 'enable'] for command in self.server.calls))

    def test_monitor_and_ambiguous_identity_rejected_without_writes(self):
        self.server.source['name'] += '.monitor'
        with self.assertRaisesRegex(RuntimeError, 'exactly one'):
            self.apply()
        self.assertFalse(any(command[0] == 'pacmd' for command in self.server.calls))

    def test_duplicate_idle_modules_do_not_get_replaced(self):
        self.server.modules.append(dict(name=POLICY.MODULE, index=18, argument=''))
        with self.assertRaisesRegex(RuntimeError, 'exactly one idle module'):
            self.apply()
        self.assertFalse(any('unload-module' in command for command in self.server.calls))

    def test_property_failure_restores_prior_custom_value(self):
        self.server.sink['properties'][POLICY.PROPERTY] = '12'
        self.server.failure = lambda args: args[:2] == ['pacmd', 'update-source-proplist']
        with self.assertRaisesRegex(RuntimeError, 'prior HAT properties/module policy restored'):
            self.apply()
        self.assertEqual(self.server.sink['properties'][POLICY.PROPERTY], '12')
        self.assertEqual(self.server.modules[0]['index'], 13)
        self.assertFalse(self.paths['active'].exists())

    def test_malformed_server_data_after_property_changes_rolls_back(self):
        before = POLICY.snapshot(self.server)
        with patch.object(POLICY, 'snapshot', side_effect=[before, KeyError('index')]):
            with self.assertRaisesRegex(RuntimeError, 'prior HAT properties/module policy restored'):
                self.apply()
        self.assertEqual(self.server.sink['properties'][POLICY.PROPERTY], '')
        self.assertEqual(self.server.source['properties'][POLICY.PROPERTY], '')
        self.assertEqual(self.server.modules[0]['index'], before['module'])
        self.assertFalse(self.paths['active'].exists())

    def test_module_load_failure_restores_inheritance_and_original_arguments(self):
        self.server.failure = lambda args: args[:2] == ['pactl', 'load-module']
        with self.assertRaisesRegex(RuntimeError, 'prior HAT properties/module policy restored'):
            self.apply()
        self.assertEqual(self.server.modules[0]['argument'], 'timeout=7')
        self.assertEqual(self.server.source['properties'][POLICY.PROPERTY], '')
        self.assertFalse(self.paths['active'].exists())

    def test_receipt_write_failure_rolls_back_loaded_module_and_properties(self):
        with patch.object(POLICY, 'json_write', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(RuntimeError, 'prior HAT properties/module policy restored'):
                self.apply()
        self.assertEqual(len(self.server.modules), 1)
        self.assertEqual(self.server.modules[0]['argument'], 'timeout=7')
        self.assertEqual(self.server.sink['properties'][POLICY.PROPERTY], '')

    def test_owned_persistence_and_read_only_check_then_remove(self):
        self.configure()
        for key in ('helper', 'shared', 'unit', 'manifest', 'active'):
            self.assertEqual(self.paths[key].stat().st_mode & 0o777, 0o600)
        saved = {key: path.read_bytes() for key, path in self.paths.items()}
        self.assertEqual(json.loads(saved['manifest'])['prior']['source'], dict(name='alsa_input.hat', value=None))
        self.server.calls.clear()
        self.configure(check=True)
        self.assertEqual(saved, {key: path.read_bytes() for key, path in self.paths.items()})
        self.assertFalse(any(command[0] == 'pacmd' or command[:3] in (['systemctl', '--user', 'enable'], ['systemctl', '--user', 'restart']) for command in self.server.calls))
        unit = self.paths['unit'].read_text()
        self.assertIn('WantedBy=pulseaudio.service', unit)
        self.assertIn('PartOf=pulseaudio.service', unit)
        self.assertIn('Requisite=pulseaudio.service', unit)
        self.assertNotIn('ExecStop=', unit)
        self.assertNotIn('User=root', unit)
        POLICY.remove(self.server, self.home)
        self.assertTrue(all(not path.exists() for path in self.paths.values()))
        self.assertFalse(self.server.enabled)

    def test_original_timeout_receipt_survives_reinstallation(self):
        self.server.source['properties'][POLICY.PROPERTY] = '12'
        self.configure()
        prior = json.loads(self.paths['manifest'].read_text())['prior']
        self.configure()
        self.assertEqual(json.loads(self.paths['manifest'].read_text())['prior'], prior)
        self.assertEqual(prior['source']['value'], '12')
        POLICY.remove(self.server, self.home)
        receipt, = self.paths['manifest'].parent.glob('pulse-idle-removed-*.json')
        self.assertEqual(json.loads(receipt.read_text())['prior'], prior)
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)

    def test_preexisting_or_modified_user_files_are_preserved(self):
        self.paths['unit'].parent.mkdir(parents=True)
        self.paths['unit'].write_text('user work')
        with self.assertRaisesRegex(RuntimeError, 'Unowned'):
            self.configure()
        self.assertEqual(self.paths['unit'].read_text(), 'user work')
        self.paths['unit'].unlink()
        self.configure()
        self.paths['unit'].write_text('user edits')
        with self.assertRaisesRegex(RuntimeError, 'file changed'):
            self.configure()
        with self.assertRaisesRegex(RuntimeError, 'file changed'):
            POLICY.remove(self.server, self.home)
        self.assertEqual(self.paths['unit'].read_text(), 'user edits')

    def test_new_install_failure_rolls_back_files_and_enablement(self):
        self.server.failure = lambda args: args[:3] == ['systemctl', '--user', 'restart']
        with self.assertRaisesRegex(RuntimeError, 'Persistent idle-policy setup failed'):
            self.configure()
        self.assertFalse(self.server.enabled)
        self.assertTrue(all(not path.exists() for path in self.paths.values()))

    def test_failed_owned_update_restores_previous_content(self):
        self.configure()
        saved = {key: self.paths[key].read_bytes() for key in ('helper', 'shared', 'unit', 'manifest')}
        self.server.failure = lambda args: args[:3] == ['systemctl', '--user', 'restart']
        with patch.object(POLICY, 'unit_text', return_value=b'updated owned unit\n'):
            with self.assertRaisesRegex(RuntimeError, 'Persistent idle-policy setup failed'):
                self.configure()
        self.assertEqual(saved, {key: self.paths[key].read_bytes() for key in saved})
        self.assertTrue(self.server.enabled)
        self.assertTrue(self.server.running)
        POLICY.owned(self.paths)

    def test_failed_update_preserves_disabled_but_running_unit_state(self):
        self.configure()
        self.server.enabled = False
        self.server.failure = lambda args: args[:3] == ['systemctl', '--user', 'restart']
        with self.assertRaisesRegex(RuntimeError, 'Persistent idle-policy setup failed'):
            self.configure()
        self.assertFalse(self.server.enabled)
        self.assertTrue(self.server.running)

    def test_custom_unit_dropins_are_not_overridden(self):
        self.configure()
        saved = self.paths['manifest'].read_bytes()
        self.server.dropins = '/home/runtime/custom-idle.conf'
        with self.assertRaisesRegex(RuntimeError, 'drop-ins require review'):
            self.configure()
        self.assertEqual(self.paths['manifest'].read_bytes(), saved)

    def test_unit_failure_does_not_make_pulse_depend_on_helper_success(self):
        unit = POLICY.unit_text().decode()
        self.assertNotIn('RequiredBy=', unit)
        self.assertNotIn('BindsTo=', unit)
        self.assertIn('WantedBy=pulseaudio.service', unit)

    def test_root_entry_point_rejected_before_any_server_access(self):
        with patch.object(POLICY.os, 'geteuid', return_value=0), patch.object(POLICY, 'snapshot') as access, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                POLICY.main(['apply'])
        self.assertEqual(caught.exception.code, 1)
        access.assert_not_called()

    def test_child_commands_use_local_runtime_user_pulse_socket(self):
        with patch.object(POLICY.subprocess, 'run', return_value=Mock(stdout='ok')) as child:
            self.assertEqual(POLICY.run(['pactl', 'info'], capture=True), 'ok')
        self.assertEqual(child.call_args.kwargs['env']['PULSE_SERVER'], f'unix:/run/user/{os.getuid()}/pulse/native')

    def test_voice_preflight_reports_bad_server_data_without_traceback_or_pip(self):
        errors = io.StringIO()
        with patch.object(VOICE.os, 'geteuid', return_value=1000), \
             patch.object(VOICE.sys, 'prefix', '/venv'), patch.object(VOICE.sys, 'base_prefix', '/usr'), \
             patch.object(VOICE.sys, 'argv', ['voice_setup.py', '--platform', 'orange-pi', '--hat-idle-release']), \
             patch.object(VOICE, 'hardware_card', return_value=('7', 'fusionhataudio')), \
             patch.object(VOICE, 'configure_idle_policy', side_effect=KeyError('index')), \
             patch.object(VOICE, 'install_python') as install, contextlib.redirect_stderr(errors):
            self.assertEqual(VOICE.main(), 3)
        self.assertIn('Voice stage incomplete:', errors.getvalue())
        self.assertNotIn('Traceback', errors.getvalue())
        install.assert_not_called()

    def test_missing_managed_pulse_service_is_actionable(self):
        # Exercise the real session/unit identity gate without hardware access.
        with self.assertRaisesRegex(RuntimeError, 'systemd user pulseaudio.service'):
            self.original_identity(Mock(return_value='0'))


if __name__ == '__main__':
    unittest.main()
