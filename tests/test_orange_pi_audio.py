"""Audio transactions use temporary roots; no hardware commands are executed."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AUDIO = load('audio_install_test', 'driver/orange-pi-zero3w-audio-install.py')
PERMS = load('audio_permissions_test', 'driver/orange-pi-zero3w/audio-permissions.py')
TOOLS = bool(shutil.which('dtc') and shutil.which('fdtoverlay'))


class Transactions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.group, self.calls, self.fail_reload = None, [], False
        self.gid = 2345
        self.install = AUDIO.Installation(self.root, self.runner)
        self.write('/boot/armbianEnv.txt', '# preserve\nverbosity=1\noverlay_prefix=sun60i-a733\n'
                   'fdtfile=allwinner/sun60i-a733-orangepi-zero3w.dtb\n'
                   'overlays=i2c0 fusion-hat-core other\nuser_overlays=extra\nrootdev=UUID=test\n')
        self.write('/etc/armbian-release', 'BOARD=orangepizero3w\nVERSION=26.8.3\n')
        self.write('/etc/os-release', 'ID=ubuntu\nVERSION_CODENAME=resolute\n')
        self.write('/var/lib/fusion-hat-core/state.json', json.dumps({'phase': 'installed', 'files': {}}))
        self.write('/sys/kernel/debug/asoc/dais', 'sunxi-snd-plat-i2s\nsnd-soc-dummy-dai\n')
        self.write('/sys/kernel/debug/pinctrl/controller/pinmux-pins',
                   ''.join(f'pin {n + 32} (PB{n}): UNCLAIMED\n' for n in range(4, 9)))
        self.write('/sys/module/fusion_hat/parameters/disable_auto_shutdown', 'Y\n')
        self.write('/sys/module/fusion_hat/parameters/restrict_controls', 'Y\n')
        self.write('/sys/class/fusion_hat/fusion_hat/speaker', '0\n')
        driver = self.root / 'sys/bus/i2c/drivers/fusion_hat'
        driver.mkdir(parents=True)
        client = self.root / 'sys/bus/i2c/devices/0-0017'
        client.mkdir(parents=True)
        (client / 'driver').symlink_to(driver, target_is_directory=True)
        self.write('/boot/dtb/allwinner/sun60i-a733-orangepi-zero3w.dtb', 'vendor')
        self.files = {'/boot/dtb/allwinner/overlay/sun60i-a733-fusion-hat-audio.dtbo': (b'dtbo', 0o644),
                      '/etc/udev/rules.d/75-fusion-hat-audio.rules': (b'rule', 0o644)}
        self.user = patch.object(AUDIO.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=1000))
        self.user.start()
        self.addCleanup(self.user.stop)
        self.groups = patch.object(AUDIO.grp, 'getgrnam', side_effect=self.get_group)
        self.groups.start()
        self.addCleanup(self.groups.stop)

    def write(self, path, text):
        p = self.root / path.lstrip('/')
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def get_group(self, name):
        if self.group is None:
            raise KeyError(name)
        return SimpleNamespace(gr_gid=self.gid, gr_mem=list(self.group))

    def runner(self, args):
        args = [str(a) for a in args]
        self.calls.append(args)
        if args == ['uname', '-m']:
            return 'aarch64'
        if args == ['uname', '-r']:
            return AUDIO.CORE.RELEASE
        if args[0] == 'groupadd':
            self.group = []
        elif args[0] == 'gpasswd':
            if args[1] == '-a':
                self.group.append(args[2])
            else:
                self.group.remove(args[2])
        elif args[0] == 'groupdel':
            self.group = None
        elif args[0] == 'udevadm' and self.fail_reload:
            self.fail_reload = False
            raise subprocess.CalledProcessError(1, args)
        return ''

    def test_repeat_install_preserves_core_and_unrelated_edits_on_remove(self):
        core = self.root / 'var/lib/fusion-hat-core/state.json'
        before_core, before = core.read_bytes(), self.install.boot.read_text()
        self.install.install(self.files, 'runtime', 'dtb')
        self.assertTrue(self.install.boot.read_text().endswith('rootdev=UUID=test\n'))
        self.assertIn('overlays=i2c0 fusion-hat-core other fusion-hat-audio\n', self.install.boot.read_text())
        count = len(self.calls)
        self.install.install(self.files, 'runtime', 'dtb', self.install.state())
        self.assertEqual(len(self.calls), count)
        self.install.boot.write_text(self.install.boot.read_text().replace('verbosity=1', 'verbosity=7'))
        self.group.append('unrelated-user')
        self.install.uninstall()
        self.assertEqual(self.install.boot.read_text(), before.replace('verbosity=1', 'verbosity=7'))
        self.assertEqual(core.read_bytes(), before_core)
        self.assertEqual(self.group, ['unrelated-user'])
        self.assertTrue(list(self.install.state_dir.glob('armbianEnv.before.*')))
        self.install.uninstall()
        self.assertFalse(any(c[0] in ('modprobe', 'insmod', 'aplay', 'arecord', 'reboot', 'update-initramfs') for c in self.calls))

    def test_failure_before_activation_rolls_back_only_owned_files(self):
        original = self.install.boot.read_bytes()
        self.fail_reload = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.install.install(self.files, 'runtime', 'dtb')
        self.assertEqual(self.install.boot.read_bytes(), original)
        self.assertIsNone(self.group)
        self.assertIsNone(self.install.state())
        self.assertFalse(any(self.install.path(p).exists() for p in self.files))

    def test_existing_or_edited_files_and_changed_group_are_not_taken_over(self):
        owned = self.write(next(iter(self.files)), 'unowned')
        with self.assertRaisesRegex(ValueError, 'unowned'):
            self.install.install(self.files, 'runtime', 'dtb')
        self.assertEqual(owned.read_text(), 'unowned')
        owned.unlink()
        self.install.install(self.files, 'runtime', 'dtb')
        owned.write_bytes(b'edited')
        boot = self.install.boot.read_bytes()
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.install.uninstall()
        self.assertEqual(self.install.boot.read_bytes(), boot)
        owned.write_bytes(b'dtbo')
        self.gid += 1
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.install.uninstall()
        self.assertTrue(owned.exists())
        self.assertEqual(self.install.boot.read_bytes(), boot)

    def test_failed_rollback_is_explicit_and_can_be_retried(self):
        runner = self.install.run
        def failures(args):
            if args[0] == 'udevadm':
                raise subprocess.CalledProcessError(1, args)
            return runner(args)
        self.install.run = failures
        with self.assertRaisesRegex(ValueError, 'rollback incomplete'):
            self.install.install(self.files, 'runtime', 'dtb')
        self.assertIsNotNone(self.install.state())
        self.assertNotIn(AUDIO.TOKEN, AUDIO.CORE.settings(self.install.boot.read_text())['overlays'].split())
        self.install.run = runner
        self.install.uninstall()
        self.assertIsNone(self.install.state())

    def test_preflight_missing_tools_policy_and_busy_or_unavailable_pins(self):
        with patch.object(AUDIO.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(ValueError, 'Missing prerequisite'):
                self.install.preflight('runtime')
        with patch.object(AUDIO.shutil, 'which', return_value='/mock/tool'):
            self.install.preflight('runtime')
            pins = self.root / 'sys/kernel/debug/pinctrl/controller/pinmux-pins'
            good = pins.read_text()
            pins.write_text(good.replace('(PB5): UNCLAIMED', '(PB5): other-device'))
            with self.assertRaisesRegex(ValueError, 'PB5'):
                self.install.preflight('runtime')
            pins.write_text('')
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                self.install.preflight('runtime')
            pins.write_text(good)
            flag = self.root / 'sys/module/fusion_hat/parameters/disable_auto_shutdown'
            flag.write_text('N')
            with self.assertRaisesRegex(ValueError, 'policy flag'):
                self.install.preflight('runtime')
            flag.write_text('Y')
            self.write('/sys/kernel/debug/asoc/dais', 'sunxi-snd-plat-i2s\n')
            with self.assertRaisesRegex(ValueError, 'DAI'):
                self.install.preflight('runtime')
        self.assertIsNone(self.install.state())

    def test_conflicting_boot_group_inputs_and_policy_preservation(self):
        for text in ('overlays=i2c0 i2c0\n', 'overlays=i2c0\noverlays=other\n'):
            with self.assertRaises(ValueError):
                AUDIO.edit_token(text, True)
        with patch.object(AUDIO.shutil, 'which', return_value='/mock/tool'):
            self.group = ['someone']
            with self.assertRaisesRegex(ValueError, 'Unowned.*group'):
                self.install.preflight('runtime')
            self.group = None
            self.install.boot.write_text(self.install.boot.read_text().replace('fusion-hat-core other', 'fusion-hat-core fusion-hat-audio'))
            with self.assertRaisesRegex(ValueError, 'Unowned audio'):
                self.install.preflight('runtime')
        self.assertEqual((self.root / 'sys/module/fusion_hat/parameters/disable_auto_shutdown').read_text(), 'Y\n')
        self.assertEqual((self.root / 'sys/module/fusion_hat/parameters/restrict_controls').read_text(), 'Y\n')


@unittest.skipUnless(TOOLS, 'dtc/fdtoverlay required for real composition tests')
class Composition(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        p = Path(cls.temp.name)
        def run(args):
            subprocess.run([str(a) for a in args], check=True, capture_output=True)
        run(['dtc', '-@', '-I', 'dts', '-O', 'dtb', '-o', p / 'base.dtb', ROOT / 'tests/fixtures/orange_pi_zero3w_audio_base.dts'])
        run(['dtc', '-@', '-I', 'dts', '-O', 'dtb', '-o', p / 'audio.dtbo', AUDIO.ASSETS / 'fusion-hat-audio-overlay.dts'])
        run(['fdtoverlay', '-i', p / 'base.dtb', '-o', p / 'after.dtb', p / 'audio.dtbo'])
        cls.data = (p / 'base.dtb').read_bytes()
        cls.before = AUDIO.tree(cls.data)
        cls.after = AUDIO.tree((p / 'after.dtb').read_bytes())

    def test_real_composition_preserves_mcu_hdmi_and_mclk(self):
        AUDIO.validate_composition(self.before, self.after)
        self.assertEqual(self.before['/soc@3000000/hdmi'], self.after['/soc@3000000/hdmi'])
        mcu = '/soc@3000000/twi@2510000/fusion_hat@17'
        self.assertEqual(self.before[mcu], self.after[mcu])
        self.assertNotIn(b'PB4\0', b''.join(value for props in self.after.values() for value in props.values()))
        # The old codec reference remains inside a disabled machine, not a
        # silently ineffective /delete-property/ overlay directive.
        self.assertEqual(self.after[AUDIO.OLD]['status'], b'disabled\0')
        self.assertEqual(self.after[AUDIO.OLD + '/soundcard-mach,codec'], self.before[AUDIO.OLD + '/soundcard-mach,codec'])

    def test_unrelated_changes_wrong_slots_and_mclk_are_rejected(self):
        cases = [('/soc@3000000/hdmi', 'test-preserve', bytes(4)),
                 (AUDIO.AUDIO, 'soundcard-mach,slot-width', struct.pack('>I', 16)),
                 (AUDIO.AUDIO + '/soundcard-mach,cpu', 'soundcard-mach,mclk-fs', struct.pack('>I', 256))]
        for path, key, value in cases:
            changed = copy.deepcopy(self.after)
            changed[path][key] = value
            with self.subTest(path=path, key=key), self.assertRaises(ValueError):
                AUDIO.validate_composition(self.before, changed)

    def test_enabled_controller_competing_card_and_pin_conflicts(self):
        changed = copy.deepcopy(self.before)
        changed[AUDIO.PLAT]['status'] = b'okay\0'
        with self.assertRaisesRegex(ValueError, 'already enabled'):
            AUDIO.check_before(changed)
        changed = copy.deepcopy(self.before)
        changed['/another-card'] = {'sound-dai': changed[AUDIO.PLAT]['phandle']}
        with self.assertRaisesRegex(ValueError, 'Competing'):
            AUDIO.check_before(changed)
        changed = copy.deepcopy(self.before)
        changed['/soc@3000000/pinctrl@2000000/conflicting'] = {'phandle': struct.pack('>I', 9999), 'pins': b'PB5\0'}
        changed['/other-user'] = {'pinctrl-0': struct.pack('>I', 9999)}
        with self.assertRaisesRegex(ValueError, 'pin conflict'):
            AUDIO.check_before(changed)
        changed['/other-user']['status'] = b'disabled\0'
        AUDIO.check_before(changed)
        changed['/gpio-user'] = {'enable-gpios': self.before['/soc@3000000/pinctrl@2000000']['phandle'] +
                                struct.pack('>III', 1, 5, 0)}
        with self.assertRaisesRegex(ValueError, 'GPIO conflict'):
            AUDIO.check_before(changed)

    def test_truncated_fdt_and_wrong_vendor_symbol_fail(self):
        for data in (b'', self.data[:50], b'bad!' + self.data[4:]):
            with self.assertRaises(ValueError):
                AUDIO.tree(data)
        changed = copy.deepcopy(self.before)
        changed['/__symbols__']['i2s0_plat'] = b'/wrong\0'
        with self.assertRaisesRegex(ValueError, 'symbol'):
            AUDIO.check_before(changed)

    def test_artifacts_refuse_missing_enabled_overlay_then_compose_all_inputs(self):
        p = Path(self.temp.name)
        root = p / 'artifact-root'
        dtb = root / 'boot/vendor.dtb'
        dtb.parent.mkdir(parents=True)
        dtb.write_bytes(self.data)
        install = AUDIO.Installation(root)
        config = {'overlays': 'i2c0 fusion-hat-core', 'user_overlays': 'other'}
        with self.assertRaisesRegex(ValueError, 'Missing enabled overlay'):
            install.artifacts(config, dtb)
        empty = p / 'empty.dts'
        empty.write_text('/dts-v1/; /plugin/; / { };\n')
        for target in ('/boot/dtb/allwinner/overlay/sun60i-a733-i2c0.dtbo',
                       '/boot/dtb/allwinner/overlay/sun60i-a733-fusion-hat-core.dtbo',
                       '/boot/overlay-user/other.dtbo'):
            output = root / target.lstrip('/')
            output.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(['dtc', '-@', '-I', 'dts', '-O', 'dtb', '-o', str(output), str(empty)],
                           capture_output=True, check=True)
        files = install.artifacts(config, dtb)
        self.assertEqual(len(files), 3)
        self.assertTrue(all(name not in files for name in ('/etc/asound.conf', '/etc/modprobe.d/fusion-hat-core.conf')))


class Permissions(unittest.TestCase):
    def test_grants_only_speaker_and_only_with_audio_dt_binding_and_both_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def put(path, data):
                p = root / path
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
                return p
            status = put('firmware/devicetree/base/fusion-hat-audio/status', b'okay\0')
            flag = put('module/fusion_hat/parameters/disable_auto_shutdown', b'Y\n')
            put('module/fusion_hat/parameters/restrict_controls', b'Y\n')
            speaker = put('class/fusion_hat/fusion_hat/speaker', b'0\n')
            decoy = put('class/fusion_hat/fusion_hat/pwm/pwm0/enable', b'0\n')
            decoy_mode = decoy.stat().st_mode & 0o777
            driver = root / 'bus/i2c/drivers/fusion_hat'
            driver.mkdir(parents=True)
            client = root / 'bus/i2c/devices/0-0017'
            client.mkdir(parents=True)
            (client / 'driver').symlink_to(driver, target_is_directory=True)
            with patch.object(PERMS.os, 'chown') as chown:
                PERMS.apply(root, 2345)
                chown.assert_called_once_with(speaker, 0, 2345)
                self.assertEqual(speaker.stat().st_mode & 0o777, 0o660)
                self.assertEqual(speaker.read_bytes(), b'0\n')
                self.assertEqual(decoy.stat().st_mode & 0o777, decoy_mode)
                chown.reset_mock()
                flag.write_text('N')
                PERMS.apply(root, 2345)
                chown.assert_not_called()
                flag.write_text('Y')
                status.unlink()
                PERMS.apply(root, 2345)
                chown.assert_not_called()
        rules = (AUDIO.ASSETS / '75-fusion-hat-audio.rules').read_text()
        self.assertIn('KERNELS=="fusion-hat-audio", DRIVERS=="sunxi-snd-mach"', rules)
        self.assertIn('KERNEL=="controlC*|pcmC*D0p|pcmC*D0c"', rules)
        self.assertNotIn('GROUP="audio"', rules)
        self.assertNotIn('GROUP="fusionhat-core"', rules)


if __name__ == '__main__':
    unittest.main()
