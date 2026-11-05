#!/usr/bin/python3
"""Opt-in, boot-only SUNXI audio experiment. No stream, live load or reboot."""
import argparse
import grp
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import struct
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
ASSETS = HERE / 'orange-pi-zero3w'
TOKEN = 'fusion-hat-audio'
GROUP = 'fusionhat-audio'
PLAT = '/soc@3000000/i2s0_plat@2532000'
OLD = '/soc@3000000/i2s0_mach'
AUDIO = '/fusion-hat-audio'
PINS = {'PB5', 'PB6', 'PB7', 'PB8'}
spec = importlib.util.spec_from_file_location('fusion_core_install', HERE / 'orange-pi-zero3w-install.py')
CORE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(CORE)


def cells(value):
    if len(value) % 4:
        raise ValueError('Malformed DT cells')
    return struct.unpack('>' + 'I' * (len(value) // 4), value)


def strings(value):
    return value.rstrip(b'\0').decode().split('\0')


def tree(data):
    """Read FDT properties for composition checks; no live tree mutation."""
    if len(data) < 40:
        raise ValueError('Truncated DTB header')
    magic, total, start, names, _, version, _, _, size_names, size_struct = struct.unpack_from('>10I', data)
    if magic != 0xd00dfeed or version < 17 or total > len(data) or \
       start < 40 or names < 40 or start + size_struct > total or names + size_names > total:
        raise ValueError('Invalid DTB header')
    end, names_end, pos = start + size_struct, names + size_names, start
    nodes, stack = {}, []
    try:
        while pos + 4 <= end:
            tag = struct.unpack_from('>I', data, pos)[0]
            pos += 4
            if tag == 1:
                stop = data.index(b'\0', pos, end)
                name = data[pos:stop].decode()
                if '/' in name or (not stack and (name or nodes)):
                    raise ValueError('Invalid DT node name')
                path = (stack[-1].rstrip('/') + '/' + name) if stack else '/'
                if path in nodes:
                    raise ValueError('Duplicate DT node')
                nodes[path] = {}
                stack.append(path)
                pos = (stop + 4) & ~3
            elif tag == 2:
                stack.pop()
            elif tag == 3:
                length, offset = struct.unpack_from('>II', data, pos)
                pos += 8
                if not stack or pos + length > end or offset >= size_names:
                    raise ValueError('Invalid DT property')
                stop = data.index(b'\0', names + offset, names_end)
                name = data[names + offset:stop].decode()
                if name in nodes[stack[-1]]:
                    raise ValueError('Duplicate DT property')
                nodes[stack[-1]][name] = data[pos:pos + length]
                pos = (pos + length + 3) & ~3
            elif tag == 9:
                if stack or '/' not in nodes:
                    raise ValueError('Unbalanced DT nodes')
                return nodes
            elif tag != 4:
                raise ValueError('Invalid DT tag')
    except (IndexError, struct.error, UnicodeError) as error:
        raise ValueError('Malformed DTB') from error
    raise ValueError('Missing DT end tag')


def enabled(nodes, path):
    while path:
        if nodes.get(path, {}).get('status', b'okay\0') not in (b'okay\0', b'ok\0'):
            return False
        if path == '/':
            break
        path = path.rsplit('/', 1)[0] or '/'
    return True


def handles(nodes):
    return {cells(props['phandle'])[0]: path for path, props in nodes.items() if 'phandle' in props}


def check_before(nodes):
    symbols = nodes.get('/__symbols__', {})
    for name, path in [('i2s0_plat', PLAT), ('i2s0_mach', OLD)]:
        if symbols.get(name) != path.encode() + b'\0':
            raise ValueError(f'Vendor DT symbol {name} differs from the verified target')
    pio = strings(symbols.get('pio', b''))[0]
    if not pio or pio not in nodes:
        raise ValueError('Missing vendor pio symbol')
    if nodes[PLAT].get('compatible') != b'allwinner,sunxi-snd-plat-i2s\0' or \
       nodes[OLD].get('compatible') != b'allwinner,sunxi-snd-mach\0':
        raise ValueError('Unexpected vendor audio binding')
    if enabled(nodes, PLAT):
        raise ValueError('I2S0 already enabled outside this owned audio installation')
    if any(p == AUDIO or p.startswith(AUDIO + '/') or
           p.startswith(pio + '/fusion-hat-audio-') for p in nodes):
        raise ValueError('Existing unowned audio nodes')
    mcu = nodes.get(CORE.TWI + '/fusion_hat@17', {})
    if mcu.get('compatible') != b'sunfounder,fusion_hat\0' or \
       mcu.get('reg') != struct.pack('>I', 0x17) or 'sunfounder,experimental-core' not in mcu:
        raise ValueError('Expected experimental MCU node is missing from boot composition')
    refs = handles(nodes)
    pio_handle = cells(nodes[pio]['phandle'])[0]
    for path, props in nodes.items():
        if not enabled(nodes, path) or path == OLD or path.startswith(OLD + '/'):
            continue
        for ref in cells(props.get('pinctrl-0', b'')):
            if PINS.intersection(strings(nodes.get(refs.get(ref), {}).get('pins', b''))):
                raise ValueError(f'Boot pin conflict: {path}')
        if cells(nodes[PLAT]['phandle'])[0] in cells(props.get('sound-dai', b'')):
            raise ValueError(f'Competing I2S0 association: {path}')
        for name, value in props.items():
            if name != 'gpios' and not name.endswith('-gpios'):
                continue
            gpio_cells = cells(value)
            if 'gpio-hog' in props and path.rsplit('/', 1)[0] == pio:
                gpio_cells = tuple(c for i in range(0, len(gpio_cells), 3)
                                   for c in (pio_handle, *gpio_cells[i:i + 3]))
            for i in range(len(gpio_cells) - 2):
                if gpio_cells[i:i + 2] == (pio_handle, 1) and gpio_cells[i + 2] in (5, 6, 7, 8):
                    raise ValueError(f'Boot GPIO conflict: {path}')
    return pio


def validate_composition(before, after):
    pio = check_before(before)
    platform_changes = {'status', 'tx-pin', 'rx-pin', 'clk-mode', 'pinctrl-used',
                        'pinctrl-names', 'pinctrl-0', 'pinctrl-1'}
    for path in before.keys() | after.keys():
        a, b = before.get(path, {}), after.get(path, {})
        if path == AUDIO or path.startswith(AUDIO + '/') or path.startswith(pio + '/fusion-hat-audio-'):
            continue
        changed = {key for key in a.keys() | b.keys() if a.get(key) != b.get(key)}
        permitted = platform_changes if path == PLAT else {'status'} if path == OLD else set()
        if path == '/__symbols__':
            permitted = {key for key in b if key.startswith('fusion_audio_') and key not in a}
        if changed - permitted or (path in before) != (path in after):
            raise ValueError(f'Overlay changed unrelated DT state: {path}')
    audio = after[AUDIO]
    cpu = after[AUDIO + '/soundcard-mach,cpu']
    codec = after[AUDIO + '/soundcard-mach,codec']
    expected = {'compatible': b'allwinner,sunxi-snd-mach\0', 'status': b'okay\0',
                'soundcard-mach,format': b'i2s\0', 'soundcard-mach,slot-num': struct.pack('>I', 2),
                'soundcard-mach,slot-width': struct.pack('>I', 32)}
    if any(audio.get(k) != v for k, v in expected.items()) or codec:
        raise ValueError('Unexpected audio profile/codec endpoint')
    if any(k in audio for k in ('soundcard-mach,frame-inversion', 'soundcard-mach,bitclock-inversion',
                               'soundcard-mach,playback-only', 'soundcard-mach,capture-only')):
        raise ValueError('Unexpected polarity or stream restriction')
    if audio.get('soundcard-mach,frame-master') != cpu.get('phandle') or \
       audio.get('soundcard-mach,bitclock-master') != cpu.get('phandle') or \
       cpu.get('sound-dai') != after[PLAT].get('phandle') or \
       cpu.get('soundcard-mach,mclk-fs') != bytes(4) or \
       cpu.get('soundcard-mach,pll-fs') != struct.pack('>I', 1) or 'soundcard-mach,mclk-fp' in cpu:
        raise ValueError('Unexpected clock provider/MCLK or CPU endpoint')
    if any(after[PLAT].get(k) != bytes(4) for k in ('tx-pin', 'rx-pin', 'clk-mode')):
        raise ValueError('Unexpected data pins or preconfigured clock mode')
    if after[OLD].get('status') != b'disabled\0' or after[PLAT].get('status') != b'okay\0':
        raise ValueError('Old/new controller ownership validation failed')
    refs = handles(after)
    active = [after[refs[h]] for h in cells(after[PLAT]['pinctrl-0'])]
    expected_pins = {('PB5', 'i2s0_bclk'), ('PB6', 'i2s0_lrck'),
                     ('PB7', 'i2s0_dout0'), ('PB8', 'i2s0_din0')}
    if len(active) != 4 or {(strings(p['pins'])[0], strings(p['function'])[0]) for p in active} != expected_pins or \
       any(len(strings(p['pins'])) != 1 for p in active):
        raise ValueError('Unexpected active audio pins')
    sleep = [after[refs[h]] for h in cells(after[PLAT]['pinctrl-1'])]
    if len(sleep) != 1 or set(strings(sleep[0]['pins'])) != PINS or \
       sleep[0]['function'] != b'io_disabled\0':
        raise ValueError('Unexpected sleep pins; PB4 must remain untouched')


def edit_token(text, add):
    config = CORE.settings(text)
    tokens = config.get('overlays', '').split()
    if len(set(tokens)) != len(tokens):
        raise ValueError('Duplicate overlay tokens')
    if add and TOKEN not in tokens:
        tokens.append(TOKEN)
    if not add:
        tokens = [p for p in tokens if p != TOKEN]
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith('overlays='):
            lines[i] = 'overlays=' + ' '.join(tokens) + ('\n' if line.endswith('\n') else '')
            return ''.join(lines)
    raise ValueError('Missing overlays=; preserve the working core boot configuration')


class Installation:
    def __init__(self, root=Path('/'), runner=CORE.run):
        self.root, self.run = Path(root), runner
        self.boot = self.path('/boot/armbianEnv.txt')
        self.state_dir = self.path('/var/lib/fusion-hat-audio')
        self.state_file = self.state_dir / 'state.json'

    def path(self, path):
        return self.root / str(path).lstrip('/')

    def state(self):
        return json.loads(self.state_file.read_text()) if self.state_file.exists() else None

    def save(self, state):
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        CORE.atomic_write(self.state_file, json.dumps(state, indent=2).encode(), 0o600)

    def assert_owned(self, state):
        if state.get('group_created'):
            try:
                group = grp.getgrnam(GROUP)
            except KeyError:
                group = None
            if group and group.gr_gid != state['group_gid']:
                raise ValueError('Audio group identity changed; resolve before modifying configuration')
        for name, entry in state['files'].items():
            p = self.path(name)
            if p.is_symlink() or (p.exists() and
               (CORE.digest(p.read_bytes()) != entry['sha256'] or p.stat().st_mode & 0o777 != entry['mode'])):
                raise ValueError(f'Managed audio file changed: {name}; preserve/resolve before removal')

    def preflight(self, runtime_user):
        CORE.Installation(self.root, self.run).compatibility()
        if pwd.getpwnam(runtime_user).pw_uid == 0:
            raise ValueError('Select a non-root runtime user')
        for tool in ('dtc', 'fdtoverlay', 'aplay', 'arecord', 'udevadm', 'groupadd', 'groupdel', 'gpasswd'):
            if not shutil.which(tool):
                raise ValueError('Missing prerequisite tool: ' + tool)
        config = CORE.settings(self.boot.read_text())
        tokens = config.get('overlays', '').split()
        if config.get('overlay_prefix') != 'sun60i-a733' or \
           config.get('fdtfile') != 'allwinner/sun60i-a733-orangepi-zero3w.dtb' or \
           not {'i2c0', 'fusion-hat-core'}.issubset(tokens):
            raise ValueError('Preserve the verified vendor DTB, i2c0 and fusion-hat-core boot setup')
        edit_token(self.boot.read_text(), True)
        state = self.state()
        if state:
            self.assert_owned(state)
            try:
                group = grp.getgrnam(GROUP)
            except KeyError:
                raise ValueError('Managed audio group missing; inspect/remove before reinstalling')
            if runtime_user not in group.gr_mem:
                raise ValueError('Managed runtime group membership missing; inspect/remove first')
        else:
            if TOKEN in tokens or self.path('/sys/firmware/devicetree/base' + AUDIO).exists():
                raise ValueError('Unowned audio overlay/client already present; do not force ownership')
            try:
                grp.getgrnam(GROUP)
            except KeyError:
                pass
            else:
                raise ValueError('Unowned fusionhat-audio group already exists')
            files = list(self.path('/sys/kernel/debug/pinctrl').glob('*/pinmux-pins'))
            ownership = '\n'.join(p.read_text() for p in files)
            for pin in sorted(PINS):
                found = re.findall(r'\(' + pin + r'\):\s*(.*)', ownership)
                if found != ['UNCLAIMED']:
                    raise ValueError(f'{pin} live ownership is busy/unavailable; inspect pinmux-pins as root')
        core = CORE.Installation(self.root, self.run)
        core_state = core.state()
        if not core_state or core_state.get('phase') != 'installed':
            raise ValueError('Install and validate the owned MCU core first')
        core.assert_owned(core_state)
        for flag in ('disable_auto_shutdown', 'restrict_controls'):
            if self.path('/sys/module/fusion_hat/parameters/' + flag).read_text().strip() != 'Y':
                raise ValueError('Required MCU policy flag is not Y: ' + flag)
        if self.path('/sys/bus/i2c/devices/0-0017/driver').resolve() != self.path('/sys/bus/i2c/drivers/fusion_hat'):
            raise ValueError('Verified MCU binding is missing')
        if self.path('/sys/class/fusion_hat/fusion_hat/speaker').read_text().strip() != '0':
            raise ValueError('Speaker is enabled; stop audio and leave it off before audio setup')
        dais = self.path('/sys/kernel/debug/asoc/dais').read_text().splitlines()
        if not {'sunxi-snd-plat-i2s', 'snd-soc-dummy-dai'}.issubset(dais):
            raise ValueError('Required registered SUNXI I2S/dummy DAI is missing')
        dtb = self.path('/boot/dtb/' + config['fdtfile'])
        if state and state['dtb_sha256'] != CORE.digest(dtb.read_bytes()):
            raise ValueError('Installed vendor DTB changed; review/remove before replacing audio')
        return config, dtb, state

    def artifacts(self, config, dtb):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            overlay, before, after = [directory / p for p in ('audio.dtbo', 'before.dtb', 'after.dtb')]
            self.run(['dtc', '-@', '-I', 'dts', '-O', 'dtb', '-o', overlay,
                      ASSETS / 'fusion-hat-audio-overlay.dts'])
            existing = []
            for key, prefix, suffix in [('overlays', '/boot/dtb/allwinner/overlay/sun60i-a733-', '.dtbo'),
                                        ('user_overlays', '/boot/overlay-user/', '.dtbo')]:
                tokens = config.get(key, '').split()
                if len(tokens) != len(set(tokens)):
                    raise ValueError('Duplicate ' + key)
                for token in tokens:
                    if not re.fullmatch(r'[A-Za-z0-9_-]+', token):
                        raise ValueError('Unsupported overlay token')
                    if key == 'overlays' and token == TOKEN:
                        continue
                    p = self.path(prefix + token + suffix)
                    if not p.is_file():
                        raise ValueError('Missing enabled overlay: ' + str(p))
                    existing.append(p)
            self.run(['fdtoverlay', '-i', dtb, '-o', before, *existing])
            check_before(tree(before.read_bytes()))
            self.run(['fdtoverlay', '-i', before, '-o', after, overlay])
            validate_composition(tree(before.read_bytes()), tree(after.read_bytes()))
            return {
                '/boot/dtb/allwinner/overlay/sun60i-a733-fusion-hat-audio.dtbo': (overlay.read_bytes(), 0o644),
                '/etc/udev/rules.d/75-fusion-hat-audio.rules': ((ASSETS / '75-fusion-hat-audio.rules').read_bytes(), 0o644),
                '/usr/libexec/fusion-hat-audio-permissions': ((ASSETS / 'audio-permissions.py').read_bytes(), 0o755),
            }

    def install(self, files, runtime_user, dtb_digest, state=None):
        expected = {name: {'sha256': CORE.digest(data), 'mode': mode} for name, (data, mode) in files.items()}
        if state:
            self.assert_owned(state)
            if state['files'] != expected or state['user'] != runtime_user or state['dtb_sha256'] != dtb_digest:
                raise ValueError('Audio inputs changed; uninstall and reboot before replacing')
            if TOKEN not in CORE.settings(self.boot.read_text()).get('overlays', '').split() or \
               any(not self.path(name).is_file() for name in expected):
                raise ValueError('Managed audio configuration is incomplete; inspect/remove first')
            print('Identical audio installation already present; no changes made.')
            return
        for name in files:
            if self.path(name).exists() or self.path(name).is_symlink():
                raise ValueError('Refusing unowned audio file: ' + name)
        boot = self.boot.read_bytes()
        state = {'files': expected, 'user': runtime_user, 'dtb_sha256': dtb_digest,
                 'group_created': False, 'membership_added': False, 'phase': 'preparing'}
        self.save(state)
        backup = self.state_dir / ('armbianEnv.before.' + CORE.digest(boot)[:16])
        try:
            if backup.exists() and backup.read_bytes() != boot:
                raise ValueError('Recovery snapshot was edited; preserve/resolve before installation')
            if not backup.exists():
                CORE.atomic_write(backup, boot, 0o600)
            self.run(['groupadd', '--system', GROUP])
            state['group_created'] = True
            state['group_gid'] = grp.getgrnam(GROUP).gr_gid
            self.save(state)
            self.run(['gpasswd', '-a', runtime_user, GROUP])
            state['membership_added'] = True
            self.save(state)
            for name, (data, mode) in files.items():
                CORE.atomic_write(self.path(name), data, mode)
            self.run(['udevadm', 'control', '--reload-rules'])
            if self.boot.read_bytes() != boot:
                raise ValueError('Boot configuration changed during installation; retry after inspection')
            CORE.atomic_write(self.boot, edit_token(boot.decode(), True).encode(), self.boot.stat().st_mode & 0o777)
            state['phase'] = 'installed'
            self.save(state)
        except Exception as original:
            print('Audio installation interrupted; rolling back owned configuration. Inspect errors before rebooting.')
            try:
                self.uninstall()
            except Exception as rollback:
                raise ValueError(f'Audio rollback incomplete: {rollback}. Original failure: {original}. '
                                 'Inspect the audio manifest before rebooting; retry uninstall after resolving the error.') from rollback
            raise
        print('Audio installed for explicit reboot. No streams, speaker enable, live overlay or reboot performed.')

    def uninstall(self):
        state = self.state()
        if not state:
            print('No owned audio installation; nothing removed.')
            return
        self.assert_owned(state)
        CORE.atomic_write(self.boot, edit_token(self.boot.read_text(), False).encode(), self.boot.stat().st_mode & 0o777)
        for name in state['files']:
            self.path(name).unlink(missing_ok=True)
        self.run(['udevadm', 'control', '--reload-rules'])
        try:
            group = grp.getgrnam(GROUP)
        except KeyError:
            group = None
        if group and state.get('group_created'):
            if group.gr_gid != state['group_gid']:
                raise ValueError('Audio group identity changed; resolve before removal')
            if state.get('membership_added') and state['user'] in group.gr_mem:
                self.run(['gpasswd', '-d', state['user'], GROUP])
            if not grp.getgrnam(GROUP).gr_mem:
                self.run(['groupdel', GROUP])
        self.state_file.unlink()
        print('Owned audio configuration removed; MCU/HDMI preserved. Stop audio and reboot explicitly; live devices were not removed.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'install', 'uninstall'))
    parser.add_argument('--runtime-user')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run check/install/uninstall with sudo; streams run separately without sudo')
    install = Installation()
    try:
        if args.action == 'uninstall':
            install.uninstall()
        else:
            if not args.runtime_user:
                parser.error('--runtime-user is required')
            config, dtb, state = install.preflight(args.runtime_user)
            files = install.artifacts(config, dtb)
            if args.action == 'check':
                print('Audio prerequisites and offline composition passed. No installation/activation performed.')
            else:
                install.install(files, args.runtime_user, CORE.digest(dtb.read_bytes()), state)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, 'Audio installation stopped: ' + (getattr(error, 'stderr', None) or str(error)) + '\n')


if __name__ == '__main__':
    main()
