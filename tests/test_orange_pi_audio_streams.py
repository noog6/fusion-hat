"""Mocked subprocess/sysfs tests only; no real PCM or speaker is opened."""
import array
from contextlib import redirect_stdout
import importlib.util
import io
import math
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('audio_stream_test', ROOT / 'examples/orange_pi_zero3w_audio.py')
STREAM = importlib.util.module_from_spec(spec)
spec.loader.exec_module(STREAM)


class Process:
    def __init__(self, events, result=0, error=None):
        self.events, self.result, self.error = events, result, error
        self.pid = 1234
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.events.append('wait')
        if self.error:
            error, self.error = self.error, None
            if callable(error):
                error()
                return self.wait(timeout)
            raise error
        self.returncode = self.result
        return self.returncode

    def terminate(self):
        self.events.append('terminate')
        self.returncode = -15

    def kill(self):
        self.events.append('kill')
        self.returncode = -9


class Speaker:
    def __init__(self, events):
        self.events = events
        self.value = '0'

    def read_text(self):
        return self.value

    def write_text(self, value):
        self.events.append('speaker=' + value)
        self.value = value


class Streams(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.events, self.clock = [], 0
        self.speaker = Speaker(self.events)
        self.status = self.path / 'status'
        self.status.write_text('closed\n')
        self.wav = self.path / 'test.wav'
        STREAM.write_wav(self.wav, STREAM.tone())
        self.time = patch.object(STREAM.time, 'monotonic', side_effect=lambda: self.clock)
        self.time.start()
        self.addCleanup(self.time.stop)

    def sleep(self, delay):
        self.clock += delay

    def factory(self, proc, state='state: RUNNING\nowner_pid   : 1234\n'):
        def create(args, **kwargs):
            self.assertEqual(self.speaker.value, '1')
            self.events.append('create')
            self.status.write_text(state)
            return proc
        return create

    def test_persistent_tone_format_duration_identical_channels_fades_and_peak(self):
        with wave.open(str(self.wav), 'rb') as w:
            self.assertEqual((w.getframerate(), w.getsampwidth(), w.getnchannels(), w.getcomptype()),
                             (48000, 4, 2, 'NONE'))
            self.assertEqual(w.getnframes(), 144000)
        data = STREAM.wav_samples(self.wav)
        self.assertEqual(len(data), 144000 * 2)
        self.assertEqual(data[0::2], data[1::2])
        highest = max(abs(v) for v in data)
        self.assertLessEqual(highest, .05 * (2 ** 31 - 1))
        self.assertGreater(highest, .0499 * (2 ** 31 - 1))
        self.assertFalse(any(data[:24000 * 2]))
        self.assertFalse(any(data[120000 * 2:]))
        mono = data[::2]
        self.assertEqual((mono[24000], mono[119999]), (0, 0))
        for distance, expected_gain in ((240, .25), (480, .5), (720, .75), (960, 1.0)):
            for frame in (24000 + distance, 119999 - distance):
                # Independently recover the envelope from the known 440 Hz carrier.
                carrier = math.sin(2 * math.pi * 440 * (frame / 48000 - .5))
                gain = mono[frame] / (STREAM.PEAK * carrier)
                self.assertAlmostEqual(gain, expected_gain, places=6)

    def test_generate_action_is_hardware_independent_and_refuses_overwrite(self):
        path = self.path / 'persistent.wav'
        output = io.StringIO()
        with patch.object(sys, 'argv', ['audio.py', 'generate', '--output', str(path)]), \
             patch.object(STREAM, 'context', side_effect=AssertionError('hardware access')), \
             patch.object(STREAM.os, 'geteuid', side_effect=AssertionError('root check')), \
             patch.object(STREAM.subprocess, 'Popen', side_effect=AssertionError('audio process')), \
             patch.object(STREAM.signal, 'signal', side_effect=AssertionError('signal handler')), \
             redirect_stdout(output):
            STREAM.main()
            saved = path.read_bytes()
            with self.assertRaises(FileExistsError):
                STREAM.main()
            self.assertEqual(path.read_bytes(), saved)
        for parameter in (str(path.resolve()), '48000', 'S32_LE', '144000', '3 seconds',
                          '440 Hz', '0.5 to 2.5', '20 ms', '5%'):
            self.assertIn(parameter, output.getvalue())

    def test_replay_can_monitor_active_left_capture_in_both_slots_without_changing_source(self):
        source = array.array('i', [100000, 0, -200000, 0, 300000, 0])
        before = source.tobytes()
        monitor = STREAM.replay_samples(source, 0)
        self.assertEqual(monitor[::2], monitor[1::2])
        self.assertTrue(any(monitor[1::2]))
        self.assertEqual(max(abs(v) for v in monitor), STREAM.PEAK)
        self.assertEqual(source.tobytes(), before)
        self.assertEqual(len(monitor), len(source))
        self.assertFalse(any(STREAM.replay_samples(source)[1::2]))
        with self.assertRaisesRegex(ValueError, 'silent'):
            STREAM.replay_samples(source, 1)

    def test_replay_selected_right_channel_is_duplicated_and_bounded(self):
        source = array.array('i', [0, -(2 ** 31), 0, 2 ** 31 - 1])
        monitor = STREAM.replay_samples(source, 1)
        self.assertEqual(monitor[::2], monitor[1::2])
        self.assertLessEqual(max(abs(v) for v in monitor), STREAM.PEAK)

    def test_normal_playback_enables_before_creation_and_passes_persistent_bytes_unchanged(self):
        proc = Process(self.events)
        saved = self.wav.read_bytes()
        def factory(args, **kwargs):
            self.assertEqual(args, ['aplay', '-v', '-D', 'hw:CARD=test,DEV=0', str(self.wav)])
            self.assertTrue(kwargs['start_new_session'])
            return self.factory(proc)(args, **kwargs)
        STREAM.playback(self.wav, 'hw:CARD=test,DEV=0', self.speaker,
                        self.status, factory=factory, sleep=self.sleep)
        self.assertEqual(self.events, ['speaker=1', 'create', 'wait', 'speaker=0'])
        self.assertEqual(self.wav.read_bytes(), saved)
        self.assertEqual(self.speaker.value, '0')

    def test_ctrl_c_error_and_failed_process_creation_leave_speaker_off(self):
        for error in (KeyboardInterrupt(), RuntimeError('PCM failed'), subprocess.TimeoutExpired('aplay', 5)):
            with self.subTest(error=type(error).__name__):
                self.clock, self.events[:] = 0, []
                self.status.write_text('closed\n')
                proc = Process(self.events, error=error)
                with self.assertRaises(type(error)):
                    STREAM.playback(self.wav, 'hw:CARD=test,DEV=0', self.speaker,
                                    self.status, factory=self.factory(proc), sleep=self.sleep)
                self.assertEqual(self.speaker.value, '0')
                self.assertLess(self.events.index('speaker=0'), self.events.index('terminate'))
        def failed(*args, **kwargs):
            self.assertEqual(self.speaker.value, '1')
            raise FileNotFoundError('aplay unavailable')
        self.status.write_text('closed\n')
        self.events[:] = []
        with self.assertRaises(FileNotFoundError):
            STREAM.playback(self.wav, 'hw:CARD=test,DEV=0', self.speaker,
                            self.status, factory=failed, sleep=self.sleep)
        self.assertEqual(self.events, ['speaker=1', 'speaker=0'])
        self.assertEqual(self.speaker.value, '0')

    def test_nonzero_playback_exit_mutes(self):
        proc = Process(self.events, result=7)
        with self.assertRaisesRegex(RuntimeError, 'status 7'):
            STREAM.playback(self.wav, 'hw:CARD=test,DEV=0', self.speaker,
                            self.status, factory=self.factory(proc), sleep=self.sleep)
        self.assertEqual(self.speaker.value, '0')
        self.assertNotIn('terminate', self.events)  # Already exited; no other process is stopped.

    def test_nonrunning_own_pcm_times_out_mutes_and_stops_child(self):
        proc = Process(self.events)
        with self.assertRaisesRegex(RuntimeError, 'RUNNING'):
            STREAM.playback(self.wav, 'hw:CARD=test,DEV=0', self.speaker,
                            self.status, factory=self.factory(proc, 'state: PREPARED\n'), sleep=self.sleep)
        self.assertEqual(self.speaker.value, '0')
        self.assertIn('terminate', self.events)

    def test_existing_open_pcm_is_refused_before_enable_or_creation(self):
        for state in ('state: PREPARED\n', 'state: RUNNING\nowner_pid   : 9999\n'):
            self.status.write_text(state)
            with self.assertRaisesRegex(RuntimeError, 'already open'):
                STREAM.playback(self.wav, 'hw:CARD=test,DEV=0', self.speaker, self.status,
                                factory=lambda *a, **k: self.fail('must not start aplay'))
            self.assertEqual(self.events, [])

    def test_foreign_owner_after_creation_is_refused_and_only_own_child_stopped(self):
        proc = Process(self.events)
        with self.assertRaisesRegex(RuntimeError, 'Own playback'):
            STREAM.playback(self.wav, 'hw:CARD=test,DEV=0', self.speaker, self.status,
                            factory=self.factory(proc, 'state: RUNNING\nowner_pid   : 9999\n'), sleep=self.sleep)
        self.assertEqual(self.speaker.value, '0')
        self.assertIn('terminate', self.events)

    def test_early_aplay_failure_mutes(self):
        proc = Process(self.events)
        proc.returncode = 2
        with self.assertRaisesRegex(RuntimeError, 'status 2'):
            STREAM.playback(self.wav, 'hw:CARD=test,DEV=0', self.speaker, self.status,
                            factory=self.factory(proc, 'closed\n'), sleep=self.sleep)
        self.assertEqual(self.speaker.value, '0')

    def test_main_ctrl_c_and_sigterm_mute_and_stop_owned_child(self):
        previous = signal.getsignal(signal.SIGTERM)
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum):
                self.status.write_text('closed\n')
                self.events[:] = []
                proc = Process(self.events, error=lambda: signal.raise_signal(signum))
                with patch.object(sys, 'argv', ['audio.py', 'playback', '--input', str(self.wav)]), \
                     patch.object(STREAM.os, 'geteuid', return_value=1000), \
                     patch.object(STREAM, 'context', return_value=('hw:CARD=test,DEV=0', self.speaker, self.status)), \
                     patch.object(STREAM.subprocess, 'Popen', side_effect=self.factory(proc)):
                    with self.assertRaises(SystemExit) as exit:
                        STREAM.main()
                self.assertEqual(exit.exception.code, 130)
                self.assertEqual(self.speaker.value, '0')
                self.assertLess(self.events.index('speaker=0'), self.events.index('terminate'))
                self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_main_replay_keeps_raw_recording_and_passes_bounded_selected_channel_copy(self):
        raw = self.path / 'microphone.wav'
        STREAM.write_wav(raw, array.array('i', [2 ** 30, 0, -2 ** 29, 0] * (48000 * 5 // 2)))
        saved = raw.read_bytes()
        def inspect_copy(path, pcm, speaker, status):
            data = STREAM.wav_samples(path)
            self.assertEqual(len(data), 5 * 48000 * 2)
            self.assertEqual(data[::2], data[1::2])
            self.assertEqual(max(abs(v) for v in data), STREAM.PEAK)
            self.assertEqual(pcm, 'hw:CARD=test,DEV=0')
        with patch.object(sys, 'argv', ['audio.py', 'replay', '--input', str(raw), '--channel', '0']), \
             patch.object(STREAM.os, 'geteuid', return_value=1000), \
             patch.object(STREAM, 'context', return_value=('hw:CARD=test,DEV=0', self.speaker, self.status)), \
             patch.object(STREAM, 'playback', side_effect=inspect_copy) as play:
            STREAM.main()
        play.assert_called_once()
        self.assertEqual(raw.read_bytes(), saved)

    def test_overlevel_or_overlong_input_is_refused_before_enable(self):
        path = self.path / 'invalid.wav'
        for samples in (array.array('i', [STREAM.PEAK + 1, 0]),
                        array.array('i', [0] * (5 * STREAM.RATE + 1) * 2)):
            path.unlink(missing_ok=True)
            STREAM.write_wav(path, samples)
            with self.assertRaises(ValueError):
                STREAM.playback(path, 'hw:CARD=test,DEV=0', self.speaker, self.status,
                                factory=lambda *a, **k: self.fail('must not start aplay'))
            self.assertEqual(self.events, [])

    def test_capture_is_fixed_profile_and_refuses_existing_output(self):
        output = self.path / 'capture.wav'
        proc = Process(self.events)
        def factory(args, **kwargs):
            self.assertEqual(args, ['arecord', '-v', '-D', 'hw:CARD=test,DEV=0',
                                   '-t', 'wav', '-f', 'S32_LE', '-r', '48000', '-c', '2', '-d', '5', '-'])
            with wave.open(kwargs['stdout'], 'wb') as w:
                w.setparams((2, 4, 48000, 0, 'NONE', 'not compressed'))
                w.writeframes(array.array('i', [1000, 0] * (5 * STREAM.RATE)).tobytes())
            return proc
        STREAM.capture(output, 'hw:CARD=test,DEV=0', factory=factory)
        saved = output.read_bytes()
        with self.assertRaises(FileExistsError):
            STREAM.capture(output, 'hw:CARD=test,DEV=0', factory=factory)
        self.assertEqual(output.read_bytes(), saved)
        self.assertNotIn('speaker=1', self.events)

    def test_capture_interrupt_stops_only_its_process(self):
        proc = Process(self.events, error=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            STREAM.capture(self.path / 'partial.wav', 'hw:CARD=test,DEV=0', factory=lambda *a, **k: proc)
        self.assertIn('terminate', self.events)

    def test_wrong_or_truncated_wav_is_rejected(self):
        path = self.path / 'wrong.wav'
        with wave.open(str(path), 'wb') as w:
            w.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            w.writeframes(bytes(100))
        with self.assertRaisesRegex(ValueError, '48 kHz'):
            STREAM.wav_samples(path)
        path.write_bytes(self.wav.read_bytes()[:-8])
        with self.assertRaisesRegex(ValueError, 'Truncated'):
            STREAM.wav_samples(path)

    def test_card_discovery_requires_owned_machine_and_nonroot_access(self):
        sysfs, dev, procfs = [self.path / p for p in ('sys', 'dev', 'proc')]
        def write(path, value):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)
        def link(path, target):
            path.parent.mkdir(parents=True, exist_ok=True)
            target.mkdir(parents=True, exist_ok=True)
            path.symlink_to(target, target_is_directory=True)
        link(sysfs / 'bus/i2c/devices/0-0017/driver', sysfs / 'bus/i2c/drivers/fusion_hat')
        for flag in ('disable_auto_shutdown', 'restrict_controls'):
            write(sysfs / 'module/fusion_hat/parameters' / flag, 'Y')
        write(sysfs / 'class/fusion_hat/fusion_hat/speaker', '0')
        card = sysfs / 'class/sound/card2'
        card.mkdir(parents=True)
        write(card / 'id', 'sndi2s0')
        link(card / 'device/of_node', sysfs / 'firmware/devicetree/base/fusion-hat-audio')
        link(card / 'device/driver', sysfs / 'bus/platform/drivers/sunxi-snd-mach')
        with patch.object(STREAM.os, 'access', return_value=True):
            pcm, _, status = STREAM.context(sysfs, procfs, dev)
            self.assertEqual(pcm, 'hw:CARD=sndi2s0,DEV=0')
            self.assertEqual(status, procfs / 'asound/card2/pcm0p/sub0/status')
        with patch.object(STREAM.os, 'access', return_value=False):
            with self.assertRaises(PermissionError):
                STREAM.context(sysfs, procfs, dev)
        (card / 'device/of_node').unlink()
        link(card / 'device/of_node', sysfs / 'firmware/devicetree/base/hdmi')
        with self.assertRaisesRegex(RuntimeError, 'one opt-in'):
            STREAM.context(sysfs, procfs, dev)


if __name__ == '__main__':
    unittest.main()
