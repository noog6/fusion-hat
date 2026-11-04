#!/usr/bin/env python3
"""Explicit ALSA tests for the opt-in 48 kHz / S32_LE / stereo experiment."""
import argparse
import array
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import wave

RATE = 48000
PEAK = int(0.05 * (2 ** 31 - 1))  # 5% (-26.02 dBFS), operator-confirmed test level.
MAX_SECONDS = 5


def context(sysfs=Path('/sys'), procfs=Path('/proc'), dev=Path('/dev')):
    if (sysfs / 'bus/i2c/devices/0-0017/driver').resolve() != sysfs / 'bus/i2c/drivers/fusion_hat':
        raise RuntimeError('Verified MCU binding is absent')
    for flag in ('disable_auto_shutdown', 'restrict_controls'):
        if (sysfs / 'module/fusion_hat/parameters' / flag).read_text().strip() != 'Y':
            raise RuntimeError('Required MCU flag is not Y: ' + flag)
    candidates = []
    for card in (sysfs / 'class/sound').glob('card[0-9]*'):
        if (card / 'device/of_node').resolve() == sysfs / 'firmware/devicetree/base/fusion-hat-audio':
            candidates.append(card)
    if len(candidates) != 1:
        raise RuntimeError('Expected one opt-in Fusion audio card; inspect aplay -l / arecord -l')
    card = candidates[0]
    if (card / 'device/driver').resolve() != sysfs / 'bus/platform/drivers/sunxi-snd-mach':
        raise RuntimeError('Audio card has an unexpected driver')
    number = card.name[4:]
    card_id = (card / 'id').read_text().strip()
    if not number.isdigit() or not re.fullmatch(r'[A-Za-z0-9_-]+', card_id):
        raise RuntimeError('Invalid ALSA card identification')
    for name in (f'controlC{number}', f'pcmC{number}D0p', f'pcmC{number}D0c'):
        if not os.access(dev / 'snd' / name, os.R_OK | os.W_OK):
            raise PermissionError(f'Non-root audio access unavailable: {name}; verify udev/group after login')
    speaker = sysfs / 'class/fusion_hat/fusion_hat/speaker'
    if speaker.read_text().strip() != '0':
        raise RuntimeError('Speaker must already be off; stop other audio users before this experiment')
    if not os.access(speaker, os.W_OK):
        raise PermissionError('Speaker switch is not writable; verify scoped audio permissions')
    pcm = 'hw:CARD=' + card_id + ',DEV=0'
    return pcm, speaker, procfs / f'asound/card{number}/pcm0p/sub0/status'


def stop(proc):
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def owns_running_pcm(status, pid):
    value = status.read_text()
    owner = re.search(r'^owner_pid\s*:\s*(\d+)', value, re.MULTILINE)
    return 'state: RUNNING' in value and owner is not None and int(owner[1]) == pid


def tone():
    """Three seconds, 440 Hz from 0.5 to 2.5 s, with 20 ms linear fades."""
    result = array.array('i')
    start, end, fade = RATE // 2, RATE * 5 // 2, RATE // 50
    for i in range(3 * RATE):
        sample = 0
        if start <= i < end:
            gain = min(1, (i - start) / fade, (end - 1 - i) / fade)
            sample = int(PEAK * gain * math.sin(2 * math.pi * 440 * (i - start) / RATE))
        result.extend((sample, sample))
    return result


def write_wav(path, samples):
    """Create only a new file; never replace a recording or prior test WAV."""
    encoded = array.array('i', samples)
    if sys.byteorder != 'little':
        encoded.byteswap()
    with Path(path).open('xb') as destination:
        with wave.open(destination, 'wb') as output:
            output.setparams((2, 4, RATE, 0, 'NONE', 'not compressed'))
            output.writeframes(encoded.tobytes())


def generate(path):
    write_wav(path, tone())
    print('Generated:', Path(path).resolve())
    print('48000 Hz / S32_LE / 2 identical interleaved channels / 144000 frames / 3 seconds')
    print('440 Hz from 0.5 to 2.5 seconds; 20 ms linear fades; peak <= 5% (-26.02 dBFS)')


def wav_samples(path):
    with wave.open(str(path), 'rb') as source:
        if (source.getframerate(), source.getsampwidth(), source.getnchannels(), source.getcomptype()) != (RATE, 4, 2, 'NONE'):
            raise ValueError('Require an uncompressed 48 kHz / signed 32-bit LE / stereo WAV')
        count = source.getnframes()
        if not 0 < count <= MAX_SECONDS * RATE:
            raise ValueError('WAV must contain at most five seconds')
        data = source.readframes(count)
        if len(data) != count * 8:
            raise ValueError('Truncated WAV data')
    samples = array.array('i', data)
    if sys.byteorder != 'little':
        samples.byteswap()
    return samples


def summarize(samples):
    for channel in range(2):
        values = samples[channel::2]
        peak = max(abs(v) for v in values) / 2 ** 31
        rms = math.sqrt(sum(v * v for v in values) / len(values)) / 2 ** 31
        print(f'Capture channel {channel}: peak={peak:.6f} rms={rms:.6f}')
    print('Levels are diagnostic only; listen to confirm intelligible speech.')


def replay_samples(samples, channel=None):
    """Prepare a bounded monitor copy; never alter the raw capture."""
    if channel not in (None, 0, 1):
        raise ValueError('Select capture channel 0 or 1')
    if channel is not None:
        samples = array.array('i', (v for sample in samples[channel::2] for v in (sample, sample)))
    highest = max(abs(v) for v in samples)
    if not highest:
        raise ValueError('Selected recording/channel is silent')
    return array.array('i', (int(v * PEAK / highest) for v in samples))


def playback(path, pcm, speaker, status, factory=None, sleep=time.sleep):
    # Validate without rewriting the WAV: standalone aplay can use identical bytes.
    samples = wav_samples(path)
    if max(abs(v) for v in samples) > PEAK:
        raise ValueError('Playback WAV exceeds the experimental 5% peak bound; use generate or replay')
    if speaker.read_text().strip() != '0':
        raise RuntimeError('Speaker must already be off')
    if status.read_text().strip() != 'closed':
        raise RuntimeError('Playback PCM is already open; stop its owner before this experiment')
    proc = None
    try:
        # Reproduce the successful manual sequence, not a universal hardware rule.
        # Cleanup is armed before enabling, including failure to create aplay.
        speaker.write_text('1')
        proc = (factory or subprocess.Popen)(['aplay', '-v', '-D', pcm, str(path)],
                                             start_new_session=True)
        deadline = time.monotonic() + 2
        while not owns_running_pcm(status, proc.pid):
            result = proc.poll()
            if result is not None:
                raise RuntimeError(f'aplay exited with status {result} before own PCM RUNNING was observed')
            if time.monotonic() >= deadline:
                raise RuntimeError('Own playback PCM did not reach RUNNING; muting speaker')
            sleep(.01)
        print(f'Own playback PCM RUNNING; owner_pid={proc.pid}')
        result = proc.wait(timeout=len(samples) / (2 * RATE) + 2)
        if result:
            raise RuntimeError(f'aplay exited with status {result}')
    finally:
        try:
            speaker.write_text('0')
        finally:
            stop(proc)
    if speaker.read_text().strip() != '0':
        raise RuntimeError('Speaker off readback failed; disconnect speaker power before continuing')
    print('Playback completed; speaker reports off. Verify audible output and physical cleanup separately.')


def capture(output, pcm, factory=subprocess.Popen):
    proc = None
    # Exclusive creation preserves existing recordings, even on errors.
    with Path(output).open('xb') as destination:
        try:
            proc = factory(['arecord', '-v', '-D', pcm, '-t', 'wav', '-f', 'S32_LE',
                            '-r', str(RATE), '-c', '2', '-d', '5', '-'],
                           stdout=destination, start_new_session=True)
            result = proc.wait(timeout=9)
            if result:
                raise RuntimeError(f'arecord exited with status {result}; partial recording retained')
        finally:
            stop(proc)
    samples = wav_samples(output)
    if len(samples) != 5 * RATE * 2:
        raise RuntimeError('Capture did not return five seconds; inspect the retained recording and ALSA logs')
    summarize(samples)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('generate', 'check', 'capture', 'playback', 'replay'))
    parser.add_argument('--output', type=Path)
    parser.add_argument('--input', type=Path)
    parser.add_argument('--channel', type=int, choices=(0, 1),
                        help='Replay one captured channel in both output slots; raw WAV is unchanged')
    args = parser.parse_args()
    if args.action in ('generate', 'capture') and args.output is None:
        parser.error(args.action + ' requires --output for a new WAV file')
    if args.output is not None and args.action not in ('generate', 'capture'):
        parser.error('--output applies only to generate or capture')
    if args.input is not None and args.action not in ('playback', 'replay'):
        parser.error('--input applies only to playback or replay')
    if args.action == 'replay' and args.input is None:
        parser.error('replay requires --input')
    if args.channel is not None and args.action != 'replay':
        parser.error('--channel applies only to replay')
    # Generation is stdlib file I/O only, even on a host without ALSA/sysfs.
    if args.action == 'generate':
        generate(args.output)
        return
    if os.geteuid() == 0:
        parser.error('Run the ALSA checks as the configured runtime user, without sudo')
    def terminate(signum, frame):
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        pcm, speaker, status = context()
        print('Explicit PCM:', pcm, '; profile: 48000 Hz / S32_LE / 2 channels')
        if args.action == 'capture':
            print('Speak a short phrase during the five-second recording; speaker remains off.')
            capture(args.output, pcm)
        elif args.action in ('playback', 'replay'):
            if args.action == 'playback' and args.input is not None:
                playback(args.input, pcm, speaker, status)
            else:
                samples = tone() if args.action == 'playback' else replay_samples(wav_samples(args.input), args.channel)
                if args.action == 'replay':
                    print(f'Monitor copy: peak <= 5%; capture channel={args.channel}; raw WAV unchanged.')
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / 'bounded.wav'
                    write_wav(path, samples)
                    playback(path, pcm, speaker, status)
        else:
            print('Non-root device access and speaker-off checks passed; no stream started.')
    except KeyboardInterrupt:
        raise SystemExit(130)
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == '__main__':
    main()
