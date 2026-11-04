"""Execute the hook with helper-contract fixtures and temporary files only."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class InitramfsHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.destination = self.directory / "staging"
        self.destination.mkdir()
        self.policy = self.directory / "source/fusion-hat-core.conf"
        self.policy.parent.mkdir()
        self.policy.write_text("options fusion_hat disable_auto_shutdown=1 restrict_controls=1\n")
        self.staged_policy = self.destination / str(self.policy).lstrip("/")
        helpers = self.directory / "hook-functions"
        # Model the documented 0=copied, 1=already exists, >1=error contract.
        # No update-initramfs, kernel module commands or system paths are used.
        helpers.write_text(r'''
manual_add_modules() {
    local optional_helper_variable
    : "${optional_helper_variable}"
    [ "${TEST_MODULE_FAIL:-0}" = 0 ]
}
copy_file() {
    [ -f "$2" ] || return 2
    [ ! -e "$DESTDIR$2" ] || return 1
    [ "${TEST_COPY_FAIL:-0}" = 0 ] || return 3
    mkdir -p "$(dirname "$DESTDIR$2")"
    cp "$2" "$DESTDIR$2"
}
''')
        text = (ROOT / "driver/orange-pi-zero3w/initramfs-hook").read_text()
        text = text.replace(". /usr/share/initramfs-tools/hook-functions", ". " + shlex.quote(str(helpers)))
        text = text.replace("policy=/etc/modprobe.d/fusion-hat-core.conf", "policy=" + shlex.quote(str(self.policy)))
        self.hook = self.directory / "hook"
        self.hook.write_text(text)

    def run_hook(self, *args, **overrides):
        env = {**os.environ, "DESTDIR": str(self.destination), "version": "test-kernel", **overrides}
        return subprocess.run(["sh", str(self.hook), *args], env=env,
                              text=True, capture_output=True)

    def test_fresh_policy_copy_and_optional_helper_variables(self):
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.staged_policy.read_bytes(), self.policy.read_bytes())

    def test_already_copied_identical_policy_is_success(self):
        self.staged_policy.parent.mkdir(parents=True)
        self.staged_policy.write_bytes(self.policy.read_bytes())
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_existing_different_policy_is_not_accepted_or_overwritten(self):
        self.staged_policy.parent.mkdir(parents=True)
        self.staged_policy.write_text("options fusion_hat disable_auto_shutdown=0\n")
        result = self.run_hook()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("differs", result.stderr)
        self.assertIn("disable_auto_shutdown=0", self.staged_policy.read_text())

    def test_staged_symlink_is_not_accepted_as_embedded_policy(self):
        self.staged_policy.parent.mkdir(parents=True)
        self.staged_policy.symlink_to(self.policy)
        result = self.run_hook()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("differs", result.stderr)

    def test_missing_source_and_real_copy_failure_remain_fatal(self):
        failed_copy = self.run_hook(TEST_COPY_FAIL="1")
        self.assertEqual(failed_copy.returncode, 3)
        self.assertIn("copying shutdown policy failed", failed_copy.stderr)
        self.policy.unlink()
        missing = self.run_hook()
        self.assertEqual(missing.returncode, 2)
        self.assertFalse(self.staged_policy.exists())

    def test_module_helper_failure_is_identified(self):
        result = self.run_hook(TEST_MODULE_FAIL="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("adding the module", result.stderr)
        self.assertFalse(self.staged_policy.exists())

    def test_prereqs_does_not_copy_or_need_staging_environment(self):
        result = self.run_hook("prereqs", DESTDIR="", version="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.staged_policy.exists())


if __name__ == "__main__":
    unittest.main()
