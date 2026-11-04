"""Installation transactions use a temporary root and mocked commands only."""
import importlib.util
import json
from pathlib import Path
import shutil
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


INSTALL = load("core_install", "driver/orange-pi-zero3w-install.py")
PERMISSIONS = load("core_permissions", "driver/orange-pi-zero3w/permissions.py")


class CoreInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = []
        self.group = None
        self.fail_update = False
        self.installation = INSTALL.Installation(self.root, self.runner)
        self.boot = self.installation.boot
        self.boot.parent.mkdir(parents=True)
        self.boot.write_text("# keep this\nverbosity=1\noverlay_prefix=sun60i-a733\n"
                             "fdtfile=allwinner/sun60i-a733-orangepi-zero3w.dtb\n"
                             "overlays=i2c0 other\nrootdev=UUID=example\n")
        os_release = self.installation.path("/etc/os-release")
        os_release.parent.mkdir(parents=True)
        os_release.write_text("ID=ubuntu\nVERSION_CODENAME=resolute\n")
        payload = b"mock initramfs payload"
        (self.root / f"boot/initrd.img-{INSTALL.RELEASE}").write_bytes(payload)
        header = bytes.fromhex("27051956") + bytes(8) + len(payload).to_bytes(4, "big") + bytes(48)
        (self.root / f"boot/uInitrd-{INSTALL.RELEASE}").write_bytes(header + payload)
        (self.root / "boot/uInitrd").symlink_to(f"uInitrd-{INSTALL.RELEASE}")
        self.files = {
            "/etc/modprobe.d/fusion-hat-core.conf": (INSTALL.POLICY, 0o644),
            "/etc/initramfs-tools/hooks/fusion-hat-core": (b"owned hook", 0o755),
            f"/lib/modules/{INSTALL.RELEASE}/updates/fusion-hat-core/fusion_hat.ko": (b"owned module", 0o644),
        }
        self.patches = [
            patch.object(INSTALL.grp, "getgrnam", side_effect=self.get_group),
            patch.object(INSTALL.pwd, "getpwall", return_value=[]),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def get_group(self, name):
        if self.group is None:
            raise KeyError(name)
        return SimpleNamespace(gr_gid=4321, gr_mem=list(self.group))

    def runner(self, args):
        args = [str(value) for value in args]
        self.calls.append(args)
        if args[0] == "groupadd":
            self.group = []
        elif args[0] == "gpasswd":
            if args[1] == "-a":
                self.group.append(args[2])
            else:
                self.group.remove(args[2])
        elif args[0] == "groupdel":
            self.group = None
        elif args[0] == "update-initramfs":
            self.assertNotIn(INSTALL.TOKEN, self.boot.read_text())
            if self.fail_update:
                self.fail_update = False
                raise subprocess.CalledProcessError(1, args)
            if self.installation.path("/etc/modprobe.d/fusion-hat-core.conf").exists():
                self.assertEqual(self.installation.path("/etc/modprobe.d/fusion-hat-core.conf").read_bytes(), INSTALL.POLICY)
                self.assertTrue(self.installation.path("/etc/initramfs-tools/hooks/fusion-hat-core").exists())
        elif args[0] == "lsinitramfs":
            return "etc/modprobe.d/fusion-hat-core.conf\nlib/modules/fusion_hat.ko"
        return ""

    def test_repeat_install_and_uninstall_preserve_unrelated_changes(self):
        self.boot.chmod(0o640)
        self.installation.install(self.files, "runtime")
        self.assertEqual(self.boot.stat().st_mode & 0o777, 0o640)
        original_calls = list(self.calls)
        state = self.installation.state()
        self.installation.install(self.files, "runtime", state)
        self.assertEqual(self.calls, original_calls)
        self.assertEqual(self.boot.read_text().count(INSTALL.TOKEN), 1)
        text = self.boot.read_text().replace("verbosity=1", "verbosity=9")
        self.boot.write_text(text.replace("overlays=i2c0 other", "overlays=i2c0 other new-camera"))
        self.installation.uninstall()
        self.assertIn("overlays=i2c0 other new-camera\n", self.boot.read_text())
        self.assertIn("verbosity=9\n", self.boot.read_text())
        self.assertIn("rootdev=UUID=example", self.boot.read_text())
        self.assertEqual(self.boot.stat().st_mode & 0o777, 0o640)
        self.assertIsNone(self.group)
        self.assertFalse(self.installation.state_file.exists())
        self.assertTrue(all(not self.installation.path(name).exists() for name in self.files))

    def test_initramfs_failure_rolls_back_before_boot_activation(self):
        original = self.boot.read_text()
        self.fail_update = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.installation.install(self.files, "runtime")
        self.assertEqual(self.boot.read_text(), original)
        self.assertIsNone(self.group)
        self.assertFalse(self.installation.state_file.exists())
        self.assertTrue(all(not self.installation.path(name).exists() for name in self.files))

    def test_unowned_and_changed_files_are_not_overwritten_or_removed(self):
        target = self.installation.path("/etc/modprobe.d/fusion-hat-core.conf")
        target.parent.mkdir(parents=True)
        target.write_text("unrelated")
        with self.assertRaisesRegex(ValueError, "unowned"):
            self.installation.install(self.files, "runtime")
        self.assertFalse(self.calls)
        target.unlink()
        self.installation.install(self.files, "runtime")
        target.write_text("operator edited")
        before = self.boot.read_text()
        with self.assertRaisesRegex(ValueError, "Managed file changed"):
            self.installation.uninstall()
        self.assertEqual(target.read_text(), "operator edited")
        self.assertEqual(self.boot.read_text(), before)

    def test_rollback_does_not_remove_unowned_group_or_other_members(self):
        # A failed group creation must not delete a group another actor owns.
        self.group = ["someone-else"]
        real_runner = self.runner
        def failed_groupadd(args):
            if args[0] == "groupadd":
                raise subprocess.CalledProcessError(1, args)
            return real_runner(args)
        self.installation.run = failed_groupadd
        with self.assertRaises(subprocess.CalledProcessError):
            self.installation.install(self.files, "runtime")
        self.assertEqual(self.group, ["someone-else"])
        self.installation.run = real_runner
        self.group = None
        self.installation.install(self.files, "runtime")
        self.group.append("another-user")
        self.installation.uninstall()
        self.assertEqual(self.group, ["another-user"])

    def test_duplicate_boot_tokens_and_changed_install_inputs_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            INSTALL.edit_overlay("overlays=i2c0 i2c0\n", True)
        with self.assertRaisesRegex(ValueError, "Duplicate setting"):
            INSTALL.edit_overlay("overlays=i2c0\noverlays=other\n", True)
        self.installation.install(self.files, "runtime")
        changed = dict(self.files)
        changed["/etc/modprobe.d/fusion-hat-core.conf"] = (b"different", 0o644)
        with self.assertRaisesRegex(ValueError, "inputs changed"):
            self.installation.install(changed, "runtime", self.installation.state())

    def test_existing_bindings_and_module_configuration_conflicts(self):
        client = self.installation.path("/sys/bus/i2c/devices/0-0017")
        client.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "Existing module/client"):
            self.installation.check_conflicts(None)
        client.rmdir()
        other = self.installation.path("/sys/bus/i2c/devices/1-0017/of_node/compatible")
        other.parent.mkdir(parents=True)
        other.write_bytes(bytes([0]).join([b"vendor,other", b"sunfounder,fusion_hat", b""]))
        with self.assertRaisesRegex(ValueError, "Another Fusion client"):
            self.installation.check_conflicts(None)
        other.unlink()
        config = self.installation.path("/etc/modprobe.d/other.conf")
        config.parent.mkdir(parents=True)
        config.write_text("options fusion_hat disable_auto_shutdown=0\n")
        with self.assertRaisesRegex(ValueError, "Conflicting module"):
            self.installation.check_conflicts(None)
        config.unlink()
        module = self.installation.path("/sys/module/fusion_hat/parameters")
        module.mkdir(parents=True)
        (module / "disable_auto_shutdown").write_text("N")
        with self.assertRaisesRegex(ValueError, "unsuitable"):
            self.installation.check_conflicts({"files": {}})

    def test_missing_tools_and_headers_stop_preflight_without_writes(self):
        release = self.installation.path("/etc/armbian-release")
        release.parent.mkdir(parents=True, exist_ok=True)
        release.write_text("BOARD=orangepizero3w\nVERSION=26.8.3\n")
        def commands(args):
            if args[:2] == ["uname", "-m"]:
                return "aarch64"
            if args[:2] == ["uname", "-r"]:
                return INSTALL.RELEASE
            raise subprocess.CalledProcessError(1, args, stderr="headers missing")
        self.installation.run = commands
        with patch.object(INSTALL.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=1000)), \
             patch.object(INSTALL.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ValueError, "Missing prerequisite tool"):
                self.installation.preflight("missing-headers", "runtime")
        with patch.object(INSTALL.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=1000)), \
             patch.object(INSTALL.shutil, "which", return_value="/mock/tool"):
            with self.assertRaises(subprocess.CalledProcessError):
                self.installation.preflight("missing-headers", "runtime")
        self.assertFalse(self.installation.state_file.exists())

    def test_existing_dt_client_rejected_by_offline_overlay_check(self):
        def commands(args):
            args = [str(arg) for arg in args]
            if args[0] == "dtc":
                Path(args[args.index("-o") + 1]).write_bytes(b"mock dtbo")
            if args[:2] == ["fdtget", "-l"]:
                return "other@17"
            if args[0] == "fdtget" and args[-1] == "reg":
                return "17"
            return ""
        self.installation.run = commands
        with self.assertRaisesRegex(ValueError, "already define"):
            self.installation.overlay({"overlays": "i2c0"}, Path("vendor.dtb"))
        self.assertFalse(self.installation.state_file.exists())

    def test_vendor_symbol_and_live_node_mismatches_stop_preflight(self):
        release = self.installation.path("/etc/armbian-release")
        release.parent.mkdir(parents=True, exist_ok=True)
        release.write_text("BOARD=orangepizero3w\nVERSION=26.8.3\n")
        for target in ("/wrong-vendor-node", INSTALL.TWI):
            def commands(args):
                if args[:2] == ["uname", "-m"]:
                    return "aarch64"
                if args[:2] == ["uname", "-r"]:
                    return INSTALL.RELEASE
                if args[0] == "fdtget":
                    return target
                return ""
            self.installation.run = commands
            with patch.object(INSTALL.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=1000)), \
                 patch.object(INSTALL.shutil, "which", return_value="/mock/tool"):
                message = "symbol differs" if target != INSTALL.TWI else "Live bus-0 DT node"
                with self.assertRaisesRegex(ValueError, message):
                    self.installation.preflight("headers", "runtime")
        self.assertFalse(self.installation.state_file.exists())

    def test_stale_wrapped_initramfs_is_rejected(self):
        self.installation.verify_boot_image()
        (self.root / f"boot/initrd.img-{INSTALL.RELEASE}").write_bytes(b"updated raw image")
        with self.assertRaisesRegex(ValueError, "payload does not match"):
            self.installation.verify_boot_image()

    def test_missing_initramfs_policy_prevents_boot_activation(self):
        actual = self.runner
        def missing_policy(args):
            return "lib/modules/fusion_hat.ko" if args[0] == "lsinitramfs" else actual(args)
        self.installation.run = missing_policy
        with self.assertRaisesRegex(ValueError, "Initramfs does not contain"):
            self.installation.install(self.files, "runtime")
        self.assertNotIn(INSTALL.TOKEN, self.boot.read_text())
        self.assertFalse(self.installation.state_file.exists())

    def test_permissions_touch_only_p0_and_recheck_policy(self):
        sysfs = self.root / "sys"
        driver = sysfs / "bus/i2c/drivers/fusion_hat"
        driver.mkdir(parents=True)
        client = sysfs / "bus/i2c/devices/0-0017"
        client.mkdir(parents=True)
        (client / "driver").symlink_to(driver)
        parameters = sysfs / "module/fusion_hat/parameters"
        parameters.mkdir(parents=True)
        for name in ("disable_auto_shutdown", "restrict_controls"):
            (parameters / name).write_text("Y")
        with patch.object(PERMISSIONS.os, "chown") as chown, patch.object(PERMISSIONS.os, "chmod") as chmod:
            PERMISSIONS.apply_permissions(sysfs, 4321)
            self.assertEqual(len(chown.call_args_list), 3)
            self.assertEqual({call.args[0].name for call in chmod.call_args_list}, {"period", "duty_cycle", "enable"})
            self.assertTrue(all(call.args[0].parent.name == "pwm0" and call.args[1] == 0o660 for call in chmod.call_args_list))
            (parameters / "restrict_controls").write_text("N")
            chmod.reset_mock()
            with self.assertRaises(RuntimeError):
                PERMISSIONS.apply_permissions(sysfs, 4321)
            chmod.assert_not_called()
        rules = (INSTALL.ASSETS / "70-fusion-hat-core.rules").read_text()
        self.assertIn('ATTRS{name}=="Fusion HAT USR Custom Button"', rules)
        self.assertIn('DRIVERS=="fusion_hat"', rules)
        self.assertIn('KERNELS=="0-0017"', rules)
        self.assertNotIn('SUBSYSTEM=="i2c-dev"', rules)
        self.assertIn('ACTION=="add|bind|change"', rules)

    def test_input_permission_fallback_checks_name_and_client_ancestry(self):
        sysfs = self.root / "sys"
        driver = sysfs / "bus/i2c/drivers/fusion_hat"
        driver.mkdir(parents=True)
        client = sysfs / "bus/i2c/devices/0-0017"
        client.mkdir(parents=True)
        (client / "driver").symlink_to(driver)
        parameters = sysfs / "module/fusion_hat/parameters"
        parameters.mkdir(parents=True)
        for name in ("disable_auto_shutdown", "restrict_controls"):
            (parameters / name).write_text("Y")
        devfs = self.root / "dev"
        (devfs / "input").mkdir(parents=True)
        for number, parent, name in (
            (1, client, "Fusion HAT USR Custom Button"),
            (2, client, "Other device"),
            (3, sysfs / "devices/unrelated", "Fusion HAT USR Custom Button"),
        ):
            device = parent / f"input/input{number}"
            device.mkdir(parents=True)
            (device / "name").write_text(name)
            event = sysfs / f"class/input/event{number}"
            event.mkdir(parents=True)
            (event / "device").symlink_to(device)
            (devfs / f"input/event{number}").touch()
        with patch.object(PERMISSIONS.os, "chown"), patch.object(PERMISSIONS.os, "chmod") as chmod:
            PERMISSIONS.apply_permissions(sysfs, 4321, devfs)
            inputs = [call.args[0] for call in chmod.call_args_list if call.args[0].parent == devfs / "input"]
            self.assertEqual(inputs, [devfs / "input/event1"])

    @unittest.skipUnless(shutil.which("gcc"), "host C compiler required")
    def test_experimental_policy_fails_closed_and_pi_defaults_remain(self):
        source = self.root / "policy.c"
        source.write_text('#include <stdbool.h>\n#include <assert.h>\n' +
                          f'#include "{ROOT / "driver/core_policy.h"}"\n' + r'''
int main(void) {
    for (int disabled = 0; disabled < 2; disabled++)
        for (int restricted = 0; restricted < 2; restricted++) {
            assert(fusion_hat_core_policy_valid(false, disabled, restricted));
            assert(fusion_hat_core_policy_valid(true, disabled, restricted) ==
                   (disabled && restricted));
        }
    assert(fusion_hat_control_mode(false) == 0666);
    assert(fusion_hat_control_mode(true) == 0644);
    return 0;
}
''')
        binary = self.root / "policy"
        subprocess.run(["gcc", "-Wall", "-Wextra", "-Werror", str(source), "-o", str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    unittest.main()
