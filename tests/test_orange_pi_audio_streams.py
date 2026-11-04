"""Mocked subprocess/sysfs tests only; no real PCM or speaker is opened."""
import array
import importlib.util
from pathlib import Path
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
        self.status.write_text('state: RUNNING\nowner_pid   : 1234\n')
        self.time = patch.object(STREAM.time, 'monotonic', side_effect=lambda: self.clock)
        self.time.start()
        self.addCleanup(self.time.stop)

    def sleep(self, delay):
        self.clock += delay

    def test_tone_is_bounded_stereo_with_silent_preroll(self):
        data = STREAM.tone()
        self.assertEqual(len(data), STREAM.RATE * 2)
        self.assertEqual(data[0::2], data[1::2])
        self.assertLessEqual(max(abs(v) for v in data), STREAM.PEAK)
        self.assertFalse(any(data[:int(.3 * STREAM.RATE) * 2]))

    def test_replay_can_monitor_active_left_capture_in_both_slots_without_changing_source(self):
        source = array.array('i', [100000, 0, -200000, 0, 300000, 0])
        before = source.tobytes()
        monitor = STREAM.replay_samples(source, 0)
        self.assertEqual(monitor[::2], monitor[1::2])
        self.assertTrue(any(monitor[1::2]))
        self.assertEqual(max(abs(v) for v in monitor), STREAM.PEAK)
        self.assertEqual(source.tobytes(), before)
        self.assertFalse(any(STREAM.replay_samples(source)[1::2]))
        with self.assertRaisesRegex(ValueError, 'silent'):
            STREAM.replay_samples(source, 1)

    def test_replay_selected_right_channel_is_duplicated_and_bounded(self):
        source = array.array('i', [0, -(2 ** 31), 0, 2 ** 31 - 1])
        monitor = STREAM.replay_samples(source, 1)
        self.assertEqual(monitor[::2], monitor[1::2])
        self.assertLessEqual(max(abs(v) for v in monitor), STREAM.PEAK)

    def test_normal_playback_uses_explicit_pcm_and_mutes_before_pcm_close(self):
        proc = Process(self.events)
        def factory(args, **kwargs):
            self.assertEqual(args[2:4], ['-D', 'hw:CARD=test,DEV=0'])
            self.assertTrue(kwargs['start_new_session'])
            with wave.open(args[-1], 'rb') as w:
                self.assertEqual((w.getframerate(), w.getsampwidth(), w.getnchannels()), (48000, 4, 2))
                self.assertEqual(w.getnframes(), 72000)  # test plus trailing mute window
            return proc
        STREAM.playback(STREAM.tone(), 'hw:CARD=test,DEV=0', self.speaker,
                        self.status, factory=factory, sleep=self.sleep)
        self.assertIn('speaker=1', self.events)
        self.assertLess(self.events.index('speaker=0'), self.events.index('wait'))
        self.assertEqual(self.speaker.value, '0')

    def test_ctrl_c_error_and_failed_process_creation_leave_speaker_off(self):
        for error in (KeyboardInterrupt(), RuntimeError('PCM failed')):
            with self.subTest(error=type(error).__name__):
                self.clock, self.events[:] = 0, []
                proc = Process(self.events)
                def interrupted_sleep(delay):
                    self.clock += delay
                    if self.speaker.value == '1':
                        raise error
                with self.assertRaises(type(error)):
                    STREAM.playback(STREAM.tone(), 'hw:CARD=test,DEV=0', self.speaker,
                                    self.status, factory=lambda *a, **k: proc, sleep=interrupted_sleep)
                self.assertEqual(self.speaker.value, '0')
                self.assertLess(self.events.index('speaker=0'), self.events.index('terminate'))
        def failed(*args, **kwargs):
            raise FileNotFoundError('aplay unavailable')
        with self.assertRaises(FileNotFoundError):
            STREAM.playback(STREAM.tone(), 'hw:CARD=test,DEV=0', self.speaker,
                            self.status, factory=failed, sleep=self.sleep)
        self.assertEqual(self.speaker.value, '0')

    def test_nonrunning_pcm_never_enables_speaker(self):
        self.status.write_text('state: PREPARED\n')
        proc = Process(self.events)
        with self.assertRaisesRegex(RuntimeError, 'RUNNING'):
            STREAM.playback(STREAM.tone(), 'hw:CARD=test,DEV=0', self.speaker,
                            self.status, factory=lambda *a, **k: proc, sleep=self.sleep)
        self.assertNotIn('speaker=1', self.events)
        self.assertIn('terminate', self.events)

    def test_another_process_running_pcm_never_enables_speaker(self):
        self.status.write_text('state: RUNNING\nowner_pid   : 9999\n')
        proc = Process(self.events)
        with self.assertRaisesRegex(RuntimeError, 'Own playback'):
            STREAM.playback(STREAM.tone(), 'hw:CARD=test,DEV=0', self.speaker,
                            self.status, factory=lambda *a, **k: proc, sleep=self.sleep)
        self.assertNotIn('speaker=1', self.events)
        self.assertIn('terminate', self.events)

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
