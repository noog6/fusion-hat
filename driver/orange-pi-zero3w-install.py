#!/usr/bin/python3
"""Experimental, local-artifact installation for one board/OS/kernel. No live load."""
import argparse
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import tempfile

RELEASE = "6.6.98-vendor-sun60iw2"
TWI = "/soc@3000000/twi@2510000"
TOKEN = "fusion-hat-core"
GROUP = "fusionhat-core"
HERE = Path(__file__).resolve().parent
ASSETS = HERE / "orange-pi-zero3w"
POLICY = b"# Experimental Orange Pi Zero 3W core installation\noptions fusion_hat disable_auto_shutdown=1 restrict_controls=1\n"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def run(args):
    return subprocess.run([str(x) for x in args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


def settings(text):
    result = {}
    for line in text.splitlines():
        if line and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            if key in result:
                raise ValueError(f"Duplicate setting: {key}")
            result[key] = value.strip().strip('"')
    return result


def edit_overlay(text, add):
    """Edit only the owned token, retaining other settings and overlay order."""
    config = settings(text)
    tokens = config.get("overlays", "").split()
    if len(tokens) != len(set(tokens)):
        raise ValueError("Duplicate overlay tokens; resolve them before installation")
    if add and TOKEN not in tokens:
        tokens.append(TOKEN)
    if not add:
        tokens = [token for token in tokens if token != TOKEN]
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith("overlays="):
            lines[index] = "overlays=" + " ".join(tokens) + ("\n" if line.endswith("\n") else "")
            return "".join(lines)
    raise ValueError("Missing overlays= setting; enable and verify i2c0 first")


def atomic_write(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        output.write(data)
    try:
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class Installation:
    def __init__(self, root=Path("/"), runner=run):
        self.root = Path(root)
        self.run = runner
        self.boot = self.path("/boot/armbianEnv.txt")
        self.state_dir = self.path("/var/lib/fusion-hat-core")
        self.state_file = self.state_dir / "state.json"

    def path(self, absolute):
        return self.root / str(absolute).lstrip("/")

    def state(self):
        return json.loads(self.state_file.read_text()) if self.state_file.exists() else None

    def save(self, state):
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write(self.state_file, json.dumps(state, indent=2).encode(), 0o600)

    def assert_owned(self, state):
        for name, entry in state["files"].items():
            path = self.path(name)
            if path.is_symlink() or (path.exists() and
               (digest(path.read_bytes()) != entry["sha256"] or path.stat().st_mode & 0o777 != entry["mode"])):
                raise ValueError(f"Managed file changed: {name}. Preserve/resolve it before uninstalling; no overwrite.")

    def check_conflicts(self, state):
        own = set(state["files"]) if state else set()
        for directory in ("/etc/modprobe.d", "/run/modprobe.d", "/usr/lib/modprobe.d",
                          "/lib/modprobe.d", "/etc/modules-load.d", "/usr/lib/modules-load.d"):
            for path in self.path(directory).glob("*.conf"):
                if "/" + str(path.relative_to(self.root)) in own:
                    continue
                for line in path.read_text().splitlines():
                    if re.search(r"\bfusion[-_]hat\b", line.split("#", 1)[0]):
                        raise ValueError(f"Conflicting module configuration in {path}; remove/resolve it explicitly")
        for name in ("/etc/modules", "/etc/initramfs-tools/modules", "/proc/cmdline"):
            path = self.path(name)
            if path.exists() and re.search(r"\bfusion[-_]hat\b", path.read_text()):
                raise ValueError(f"Conflicting early module configuration: {name}")
        for path in self.path(f"/lib/modules/{RELEASE}").rglob("fusion_hat.ko*"):
            if "/" + str(path.relative_to(self.root)) not in own:
                raise ValueError(f"Another Fusion module exists: {path}; no replacement performed")
        module = self.path("/sys/module/fusion_hat")
        client = self.path("/sys/bus/i2c/devices/0-0017")
        if not state and (module.exists() or client.exists()):
            raise ValueError("Existing module/client: finish the temporary session or resolve ownership first")
        if state and module.exists():
            for parameter in ("disable_auto_shutdown", "restrict_controls"):
                if (module / "parameters" / parameter).read_text().strip() != "Y":
                    raise ValueError(f"Already-loaded module has unsuitable {parameter}; do not force unload")
        if state and client.exists():
            if (client / "driver").resolve() != self.path("/sys/bus/i2c/drivers/fusion_hat"):
                raise ValueError("Existing client has different ownership")
            if not (client / "of_node/sunfounder,experimental-core").exists():
                raise ValueError("Client was not created by the experimental core overlay")
        for other in self.path("/sys/bus/i2c/devices").glob("*-????"):
            if other == client:
                continue
            name = other / "name"
            compatible = other / "of_node/compatible"
            if (name.exists() and name.read_text().strip() == "fusion_hat") or \
               (compatible.exists() and b"sunfounder,fusion_hat" in compatible.read_bytes().split(b"\0")):
                raise ValueError(f"Another Fusion client exists: {other}")

    def verify_boot_image(self):
        """This board's verified boot layout uses a 64-byte legacy U-Boot wrapper."""
        expected = self.path(f"/boot/uInitrd-{RELEASE}")
        if self.path("/boot/uInitrd").resolve() != expected:
            raise ValueError("uInitrd does not point to the current kernel image; review boot configuration first")
        payload = self.path(f"/boot/initrd.img-{RELEASE}").read_bytes()
        wrapped = expected.read_bytes()
        if (len(wrapped) < 64 or wrapped[:4] != bytes.fromhex("27051956") or
            int.from_bytes(wrapped[12:16], "big") != len(payload) or wrapped[64:] != payload):
            raise ValueError("uInitrd payload does not match initrd.img; resolve the Armbian U-Boot hook before activation")

    def preflight(self, headers, runtime_user):
        if self.run(["uname", "-m"]) != "aarch64" or self.run(["uname", "-r"]) != RELEASE:
            raise ValueError(f"Only Orange Pi Zero 3W / aarch64 / {RELEASE} is supported here")
        board = settings(self.path("/etc/armbian-release").read_text())
        if board.get("BOARD") != "orangepizero3w" or board.get("VERSION") != "26.8.3":
            raise ValueError("Expected Orange Pi Zero 3W / Armbian 26.8.3; review a different image separately")
        operating_system = settings(self.path("/etc/os-release").read_text())
        if operating_system.get("ID") != "ubuntu" or operating_system.get("VERSION_CODENAME") != "resolute":
            raise ValueError("This experimental procedure targets Ubuntu resolute only")
        user = pwd.getpwnam(runtime_user)
        if user.pw_uid == 0:
            raise ValueError("Select a non-root runtime user")
        for tool in ("dtc", "fdtget", "fdtoverlay", "modinfo", "depmod", "cmp", "update-initramfs",
                     "lsinitramfs", "udevadm", "groupadd", "groupdel", "gpasswd"):
            if not shutil.which(tool):
                raise ValueError(f"Missing prerequisite tool: {tool}; no packages are installed automatically")
        self.run([HERE / "build-module.sh", "--check-only", RELEASE, headers])
        config = settings(self.boot.read_text())
        if config.get("overlay_prefix") != "sun60i-a733" or config.get("fdtfile") != "allwinner/sun60i-a733-orangepi-zero3w.dtb":
            raise ValueError("Unexpected overlay_prefix/fdtfile in armbianEnv.txt")
        if "i2c0" not in config.get("overlays", "").split():
            raise ValueError("Enable and verify i2c0 first; this installer does not configure header pinmux")
        edit_overlay(self.boot.read_text(), True)  # Validate duplicates before mutations.
        state = self.state()
        if TOKEN in config["overlays"].split() and not state:
            raise ValueError("Core overlay token already exists without installer ownership")
        if state:
            self.assert_owned(state)
            group = grp.getgrnam(GROUP)
            if group.gr_gid != state.get("group_gid") or runtime_user not in group.gr_mem:
                raise ValueError("Managed runtime group/membership changed; resolve ownership before reinstalling")
        else:
            try:
                grp.getgrnam(GROUP)
            except KeyError:
                pass
            else:
                raise ValueError(f"Group {GROUP} already exists without installer ownership")
        self.check_conflicts(state)
        dtb = self.path("/boot/dtb/" + config["fdtfile"])
        if self.run(["fdtget", "-t", "s", dtb, "/__symbols__", "twi0"]) != TWI:
            raise ValueError("Installed vendor DTB twi0 symbol differs from the verified target")
        live = self.path("/sys/bus/i2c/devices/i2c-0/of_node").resolve()
        if live != self.path("/sys/firmware/devicetree/base" + TWI):
            raise ValueError("Live bus-0 DT node differs from the installed vendor twi0")
        if self.path("/sys/bus/i2c/devices/i2c-0/name").read_text().strip() != "SUNXI TWI(0x0000000002510000)":
            raise ValueError("Bus 0 is not the verified TWI0 adapter")
        if not self.path(f"/boot/initrd.img-{RELEASE}").is_file():
            raise ValueError("Missing matching initrd.img; resolve the Armbian initramfs setup first")
        if not os.access(self.path("/etc/initramfs/post-update.d/99-uboot"), os.X_OK):
            raise ValueError("Missing executable Armbian 99-uboot initramfs hook")
        self.verify_boot_image()
        return config, dtb, state, user

    def overlay(self, config, dtb):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            overlay = directory / "core.dtbo"
            self.run(["dtc", "-@", "-I", "dts", "-O", "dtb", "-o", overlay,
                      ASSETS / "fusion-hat-core-overlay.dts"])
            existing = []
            for token in config.get("overlays", "").split():
                if token != TOKEN:
                    if not re.fullmatch(r"[A-Za-z0-9_-]+", token):
                        raise ValueError("Unsupported overlay token")
                    existing.append(self.path(f"/boot/dtb/allwinner/overlay/sun60i-a733-{token}.dtbo"))
            for token in config.get("user_overlays", "").split():
                if not re.fullmatch(r"[A-Za-z0-9_-]+", token):
                    raise ValueError("Unsupported user overlay token")
                existing.append(self.path(f"/boot/overlay-user/{token}.dtbo"))
            before = directory / "before.dtb"
            self.run(["fdtoverlay", "-i", dtb, "-o", before, *existing])
            for child in self.run(["fdtget", "-l", before, TWI]).split():
                try:
                    address = self.run(["fdtget", "-t", "x", before, TWI + "/" + child, "reg"])
                except subprocess.CalledProcessError:
                    continue
                if address == "17":
                    raise ValueError("Existing boot overlays/DTB already define a TWI0 client at 0x17")
            after = directory / "after.dtb"
            self.run(["fdtoverlay", "-i", before, "-o", after, overlay])
            if self.run(["fdtget", "-t", "x", after, TWI + "/fusion_hat@17", "reg"]) != "17":
                raise ValueError("Offline overlay validation failed")
            self.run(["fdtget", after, TWI + "/fusion_hat@17", "sunfounder,experimental-core"])
            return overlay.read_bytes()

    def artifacts(self, config, dtb):
        module = HERE / "fusion_hat.ko"
        if not module.is_file():
            raise ValueError("Build driver/fusion_hat.ko from this checkout before installation")
        vermagic = self.run(["modinfo", "-F", "vermagic", module]).split()
        if self.run(["modinfo", "-F", "name", module]) != "fusion_hat" or \
           not vermagic or vermagic[0] != RELEASE or "aarch64" not in vermagic:
            raise ValueError("Local module has the wrong name or kernel release")
        parameters = self.run(["modinfo", "-F", "parm", module])
        if "disable_auto_shutdown:" not in parameters or "restrict_controls:" not in parameters:
            raise ValueError("Rebuild the local module: required policy parameters are missing")
        return {
            f"/lib/modules/{RELEASE}/updates/fusion-hat-core/fusion_hat.ko": (module.read_bytes(), 0o644),
            "/boot/dtb/allwinner/overlay/sun60i-a733-fusion-hat-core.dtbo": (self.overlay(config, dtb), 0o644),
            "/etc/modprobe.d/fusion-hat-core.conf": (POLICY, 0o644),
            "/etc/udev/rules.d/70-fusion-hat-core.rules": ((ASSETS / "70-fusion-hat-core.rules").read_bytes(), 0o644),
            "/usr/libexec/fusion-hat-core-permissions": ((ASSETS / "permissions.py").read_bytes(), 0o755),
            "/etc/initramfs-tools/hooks/fusion-hat-core": ((ASSETS / "initramfs-hook").read_bytes(), 0o755),
        }

    def install(self, files, runtime_user, state=None):
        """All validation precedes this transaction; boot activation is the last write."""
        expected = {name: {"sha256": digest(data), "mode": mode} for name, (data, mode) in files.items()}
        if state:
            self.assert_owned(state)
            if state["files"] != expected or state["user"] != runtime_user:
                raise ValueError("Installation inputs changed; uninstall/rollback and reboot before replacing an installation")
            if TOKEN not in settings(self.boot.read_text()).get("overlays", "").split():
                raise ValueError("Managed boot token removed; resolve or uninstall before reinstalling")
            if any(not self.path(name).is_file() for name in expected):
                raise ValueError("Managed files missing; uninstall the partial installation before retrying")
            print("Identical installation already present; no changes made.")
            return
        for name in files:
            if self.path(name).exists() or self.path(name).is_symlink():
                raise ValueError(f"Refusing to overwrite unowned file: {name}")
        state = {"user": runtime_user, "files": expected, "phase": "preparing",
                 "group_created": False, "membership_added": False}
        self.save(state)
        atomic_write(self.state_dir / "armbianEnv.before", self.boot.read_bytes(), 0o600)
        try:
            self.run(["groupadd", "--system", GROUP])
            state["group_created"] = True
            state["group_gid"] = grp.getgrnam(GROUP).gr_gid
            self.save(state)
            self.run(["gpasswd", "-a", runtime_user, GROUP])
            state["membership_added"] = True
            self.save(state)
            # Policy and hook precede depmod/initramfs, and all precede boot activation.
            for name, (data, mode) in files.items():
                atomic_write(self.path(name), data, mode)
            self.run(["depmod", "-a", RELEASE])
            self.run(["update-initramfs", "-u", "-k", RELEASE])
            self.verify_boot_image()
            listing = self.run(["lsinitramfs", self.path(f"/boot/initrd.img-{RELEASE}")])
            if "etc/modprobe.d/fusion-hat-core.conf" not in listing or "fusion_hat.ko" not in listing:
                raise ValueError("Initramfs does not contain the shutdown policy and module; boot activation refused")
            atomic_write(self.boot, edit_overlay(self.boot.read_text(), True).encode(), self.boot.stat().st_mode & 0o777)
            state["phase"] = "installed"
            self.save(state)
            self.run(["udevadm", "control", "--reload-rules"])
        except Exception as error:
            detail = getattr(error, "stderr", None) or str(error)
            print(f"Installation interrupted: {detail}. Rolling back owned boot/files. Inspect any rollback error before rebooting.")
            self.uninstall()
            raise
        print("Installed for this kernel only. No module loaded and no reboot performed. Reboot explicitly, then log in again.")

    def uninstall(self):
        state = self.state()
        if not state:
            print("No owned installation; nothing removed.")
            return
        self.assert_owned(state)
        # Remove only our overlay token, keeping unrelated edits/settings.
        atomic_write(self.boot, edit_overlay(self.boot.read_text(), False).encode(), self.boot.stat().st_mode & 0o777)
        for name in state["files"]:
            self.path(name).unlink(missing_ok=True)
        self.run(["depmod", "-a", RELEASE])
        self.run(["update-initramfs", "-u", "-k", RELEASE])
        self.verify_boot_image()
        self.run(["udevadm", "control", "--reload-rules"])
        try:
            group = grp.getgrnam(GROUP)
        except KeyError:
            group = None
        if group and state.get("group_created") and group.gr_gid == state.get("group_gid"):
            if state.get("membership_added") and state["user"] in group.gr_mem:
                self.run(["gpasswd", "-d", state["user"], GROUP])
            group = grp.getgrnam(GROUP)
            if not group.gr_mem and not any(user.pw_gid == group.gr_gid for user in pwd.getpwall()):
                self.run(["groupdel", GROUP])
        self.state_file.unlink()
        print("Owned installation removed. Existing live module/client and shared dependencies were not unloaded. Reboot explicitly.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "install", "uninstall"))
    parser.add_argument("--headers", default=f"/lib/modules/{RELEASE}/build")
    parser.add_argument("--runtime-user")
    args = parser.parse_args()
    installation = Installation()
    if args.action == "uninstall":
        if os.geteuid() != 0:
            parser.error("uninstall requires sudo")
        installation.uninstall()
        return
    if not args.runtime_user:
        parser.error("--runtime-user is required")
    config, dtb, state, _ = installation.preflight(args.headers, args.runtime_user)
    files = installation.artifacts(config, dtb)
    if args.action == "check":
        print("Prerequisites, local artifact and offline DT overlay checks passed; no installation performed.")
    else:
        if os.geteuid() != 0:
            parser.error("install requires sudo")
        installation.install(files, args.runtime_user, state)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", None) or str(error)
        raise SystemExit(f"Core installation stopped: {detail}")
