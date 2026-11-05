"""Diagnostic output exercised with harmless child processes, never hardware."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('install_output', Path(__file__).resolve().parents[1] / 'driver/install_output.py')
OUTPUT = importlib.util.module_from_spec(spec)
spec.loader.exec_module(OUTPUT)


class InstallerOutput(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.console = io.StringIO()

    def output(self, verbose=False):
        with contextlib.redirect_stdout(self.console):
            output = OUTPUT.InstallOutput(self.temporary.name, verbose=verbose)
        self.addCleanup(output.close)
        return output

    def test_default_keeps_diagnostics_private_but_reports_progress(self):
        output = self.output()
        self.assertEqual(output.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(output.path.stat().st_uid, os.getuid())
        with output.details():
            print('transaction details')
            output.command([sys.executable, '-c', 'print("pip build details")'])
            output.report('[1/3] Core: preparing files')
        self.assertEqual(self.console.getvalue(), '[1/3] Core: preparing files\n')
        self.assertIn('transaction details', output.path.read_text())
        self.assertIn('pip build details', output.path.read_text())

    def test_verbose_mirrors_details_and_commands_to_console_and_log(self):
        output = self.output(verbose=True)
        with output.details():
            print('transaction details')
            output.command([sys.executable, '-c', 'print("build details")'])
        for text in ('transaction details', 'build details', '+ '):
            self.assertIn(text, self.console.getvalue())
            self.assertIn(text, output.path.read_text())

    def test_capture_preserves_machine_readable_stdout_and_logs_stderr(self):
        output = self.output()
        result = output.command([sys.executable, '-c', 'import json,sys; print(json.dumps({"version":314},separators=(",",":"))); print("diagnostic",file=sys.stderr)'], capture=True)
        self.assertEqual(result, '{"version":314}')
        self.assertEqual(self.console.getvalue(), '')
        self.assertIn('diagnostic', output.path.read_text())

    def test_child_failure_retains_actionable_tail_and_exit_status(self):
        output = self.output()
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            output.command([sys.executable, '-c', 'import sys; print("install the matching development headers",file=sys.stderr); sys.exit(3)'])
        self.assertEqual(caught.exception.returncode, 3)
        self.assertIn('install the matching development headers', caught.exception.stderr)
        self.assertIn('install the matching development headers', output.path.read_text())

    def test_routing_recovery_and_profile_conflicts_remain_visible(self):
        output = self.output()
        output.command([sys.executable, '-c', 'print("Verified HAT source: microphone; sink: speaker"); print("Prior routing saved: /home/runtime/receipt.json"); print("Recovery commands, in this same user session:"); print("  pactl set-default-source old-source"); print("Whole-venv pip check diagnostics remain, outside the verified core/voice profile.")'])
        text = self.console.getvalue()
        self.assertIn('Verified HAT source: microphone', text)
        self.assertIn('Prior routing saved:', text)
        self.assertIn('pactl set-default-source old-source', text)
        self.assertIn('not verified conflict-free', text)


if __name__ == '__main__':
    unittest.main()
