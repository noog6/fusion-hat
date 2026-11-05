"""Console summaries and private diagnostic logs for the unified installer."""
from collections import deque
from contextlib import redirect_stdout
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile


class InstallOutput:
    def __init__(self, directory, verbose=False):
        # The caller validates the root-owned temporary parent. mkstemp creates
        # a new, mode-600 file; never follow a caller-selected log path as root.
        descriptor, name = tempfile.mkstemp(prefix='fusion-hat-install-', suffix='.log', dir=directory)
        self.path = Path(name)
        self.file = os.fdopen(descriptor, 'w', buffering=1)
        self.console = sys.stdout
        self.verbose = verbose

    def write(self, text):
        self.file.write(text)
        if self.verbose:
            self.console.write(text)
            self.console.flush()
        return len(text)

    def flush(self):
        self.file.flush()

    def report(self, text):
        self.file.write(text + '\n')
        print(text, file=self.console, flush=True)

    def details(self):
        return redirect_stdout(self)

    def _child_line(self, line):
        self.write(line)
        if self.verbose:
            return
        # Keep routing identity, explicit changes and recovery instructions
        # visible even when compiler/pip/device enumeration goes to the log.
        if line.startswith(('Verified HAT source:', 'User default routing', 'Prior routing saved:',
                            'Recovery commands,', '  pactl ', 'Defaults preserved.', 'HAT idle ')):
            print(line, file=self.console, end='', flush=True)
        if line.startswith('Whole-venv pip check diagnostics remain'):
            self.report('Note: whole-venv pip check reports conflicts outside the verified core/voice profile. '
                        'See the log for exact diagnostics; the entire venv is not verified conflict-free.')

    def command(self, args, capture=False):
        args = [str(a) for a in args]
        self.write('+ ' + shlex.join(args) + '\n')
        if capture:
            result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.write(result.stdout)
            self.write(result.stderr)
            if result.returncode:
                raise subprocess.CalledProcessError(result.returncode, args, result.stdout, result.stderr)
            return result.stdout.strip()
        tail = deque(maxlen=30)
        with subprocess.Popen(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as process:
            for line in process.stdout:
                tail.append(line)
                self._child_line(line)
            status = process.wait()
        if status:
            raise subprocess.CalledProcessError(status, args, stderr=''.join(tail))
        return ''

    def close(self):
        self.file.close()
