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
PEAK = int(0.01 * (2 ** 31 - 1))  # -40 dBFS; identical slots avoid channel guessing.


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
    result = array.array('i')
    for i in range(RATE):
        sample = int(PEAK * math.sin(2 * math.pi * 440 * i / RATE)) if RATE * .3 <= i < RATE * .9 else 0
        result.extend((sample, sample))
    return result


def wav_samples(path):
    with wave.open(str(path), 'rb') as source:
        if (source.getframerate(), source.getsampwidth(), source.getnchannels(), source.getcomptype()) != (RATE, 4, 2, 'NONE'):
            raise ValueError('Require an uncompressed 48 kHz / signed 32-bit LE / stereo WAV')
        count = source.getnframes()
        if not 0 < count <= 5 * RATE:
            raise ValueError('Recording must contain at most five seconds')
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


def playback(samples, pcm, speaker, status, factory=subprocess.Popen, sleep=time.sleep):
    # Guard and cleanup precede every process/enable operation.
    if speaker.read_text().strip() != '0':
        raise RuntimeError('Speaker must already be off')
    proc = None
    try:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bounded.wav'
            with wave.open(str(path), 'wb') as output:
                output.setparams((2, 4, RATE, 0, 'NONE', 'not compressed'))
                encoded = array.array('i', samples)
                # Keep clocks running through a trailing half-second of silence,
                # allowing the normal path to mute before aplay closes the PCM.
                encoded.extend([0] * RATE)
                if sys.byteorder != 'little':
                    encoded.byteswap()
                output.writeframes(encoded.tobytes())
            proc = factory(['aplay', '-q', '-D', pcm, '--buffer-time=100000',
                            '--period-time=20000', str(path)], start_new_session=True)
            deadline = time.monotonic() + 2
            while not owns_running_pcm(status, proc.pid):
                if proc.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError('Own playback PCM did not reach RUNNING; speaker remains off')
                sleep(.01)
            # Pi codec uses a 5 ms amp delay. Leave 50 ms after RUNNING here;
            # the fixed test sound also begins with 300 ms of digital silence.
            sleep(.05)
            if proc.poll() is not None or not owns_running_pcm(status, proc.pid):
                raise RuntimeError('Playback ended before speaker enable')
            speaker.write_text('1')
            mute_at = time.monotonic() + len(samples) / (2 * RATE) - .05
            while time.monotonic() < mute_at:
                if proc.poll() is not None:
                    raise RuntimeError('Playback stopped early; muting speaker')
                sleep(.01)
            speaker.write_text('0')
            result = proc.wait(timeout=2)
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
    parser.add_argument('action', choices=('check', 'capture', 'playback', 'replay'))
    parser.add_argument('--output', type=Path)
    parser.add_argument('--input', type=Path)
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('Run the ALSA checks as the configured runtime user, without sudo')
    if args.action == 'capture' and args.output is None:
        parser.error('capture requires --output for a new WAV file')
    if args.action == 'replay' and args.input is None:
        parser.error('replay requires --input')
    def terminate(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try:
        pcm, speaker, status = context()
        print('Explicit PCM:', pcm, '; profile: 48000 Hz / S32_LE / 2 channels')
        if args.action == 'capture':
            print('Speak a short phrase during the five-second recording; speaker remains off.')
            capture(args.output, pcm)
        elif args.action in ('playback', 'replay'):
            samples = tone()
            if args.action == 'replay':
                samples = wav_samples(args.input)
                highest = max(abs(v) for v in samples)
                if not highest:
                    raise ValueError('Recording is silent')
                # Bound replay to the same peak as the test tone; add pre-roll.
                samples = array.array('i', [0] * int(.3 * RATE) * 2) + \
                          array.array('i', (int(v * PEAK / highest) for v in samples))
            playback(samples, pcm, speaker, status)
        else:
            print('Non-root device access and speaker-off checks passed; no stream started.')
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == '__main__':
    main()
