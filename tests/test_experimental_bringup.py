"""No root/hardware required: fake sysfs/kmod plus a host C shutdown harness."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BringupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.log = self.base / "calls"

    def write(self, path, text="fixture\n"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def run_bash(self, code, input_text="\n", **env):
        return subprocess.run(
            ["bash", "-c", code], input=input_text, text=True, capture_output=True,
            env={**os.environ, "LOG": str(self.log), **env},
        )

    def assert_failed(self, result, message):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(message, result.stderr)

    def test_build_preflight_and_kernel_target(self):
        headers = self.base / "headers"
        fake_bin = self.base / "bin"
        fake_bin.mkdir()
        # Make is recorded, never actually invoked against these fake headers.
        make = self.write(fake_bin / "make", '#!/bin/bash\nprintf "%s\\n" "$@" > "$LOG"\n')
        make.chmod(0o755)
        env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}",
               "LOG": str(self.log)}
        for variable in ("ARCH", "CROSS_COMPILE", "CC"):
            env.pop(variable, None)

        def build():
            return subprocess.run(
                ["bash", str(ROOT / "driver/build-module.sh"), "test-release", str(headers)],
                env=env, text=True, capture_output=True,
            )

        self.assert_failed(build(), "Missing")
        self.assertFalse(self.log.exists())
        self.write(headers / "Makefile")
        self.write(headers / "include/config/kernel.release", "test-release\n")
        self.write(headers / "include/generated/utsrelease.h", '#define UTS_RELEASE "test-release"\n')
        self.write(headers / "include/generated/autoconf.h")
        config = self.write(headers / "include/config/auto.conf", "CONFIG_X86_64=y\nCONFIG_MODULES=y\n")
        self.assert_failed(build(), "Module.symvers")
        self.write(headers / "Module.symvers")
        self.write(headers / "include/config/kernel.release", "wrong-release\n")
        self.assert_failed(build(), "does not match requested")
        self.assertFalse(self.log.exists())
        self.write(headers / "include/config/kernel.release", "test-release\n")
        self.write(headers / "include/generated/utsrelease.h", '#define UTS_RELEASE "stale"\n')
        self.assert_failed(build(), "utsrelease.h disagrees")
        self.write(headers / "include/generated/utsrelease.h", '#define UTS_RELEASE "test-release"\n')
        config.write_text("CONFIG_OTHER_ARCH=y\nCONFIG_MODULES=y\n")
        self.assert_failed(build(), "native architecture")
        arch = {"x86_64": "CONFIG_X86_64", "aarch64": "CONFIG_ARM64"}[os.uname().machine]
        config.write_text(f"{arch}=y\nCONFIG_MODULES=y\n")
        result = build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log.read_text().splitlines(), [
            "-C", str(headers), f"M={ROOT / 'driver'}", "modules",
        ])

    def session_fixture(self):
        self.sysfs = self.base / "sys"
        self.adapter = self.sysfs / "bus/i2c/devices/i2c-0"
        self.write(self.adapter / "name", "SUNXI TWI(0x0000000002510000)\n")
        self.write(self.adapter / "new_device", "")
        self.write(self.adapter / "delete_device", "")
        self.module = self.write(self.base / "fusion_hat.ko")
        return f"""
source {shlex.quote(str(ROOT / 'driver/opie-session.sh'))}
SYSFS={shlex.quote(str(self.sysfs))}
uname() {{ if [[ $1 == -m ]]; then echo aarch64; else echo 6.6.98-vendor-sun60iw2; fi; }}
modinfo() {{
    if [[ $2 == name ]]; then echo fusion_hat;
    else echo "${{VERMAGIC:-6.6.98-vendor-sun60iw2}} SMP mod_unload aarch64"; fi
}}
insmod() {{
    echo "insmod $*" >> "$LOG"
    mkdir -p "$SYSFS/module/fusion_hat/parameters"
    echo "${{PARAM:-Y}}" > "$SYSFS/module/fusion_hat/parameters/disable_auto_shutdown"
}}
rmmod() {{ echo "rmmod $*" >> "$LOG"; rm -r "$SYSFS/module/fusion_hat"; }}
printf() {{
    builtin printf "$@"
    if [[ $1 == 'fusion_hat 0x17\\n' ]]; then
        echo create-client >> "$LOG"
        mkdir -p "$SYSFS/bus/i2c/devices/0-0017"
        echo fusion_hat > "$SYSFS/bus/i2c/devices/0-0017/name"
        if [[ ${{BIND:-yes}} == yes ]]; then
            mkdir -p "$SYSFS/bus/i2c/drivers/fusion_hat" "$SYSFS/class/fusion_hat/fusion_hat"
            ln -s "$SYSFS/bus/i2c/drivers/fusion_hat" "$SYSFS/bus/i2c/devices/0-0017/driver"
            echo 1.0.2 > "$SYSFS/class/fusion_hat/fusion_hat/firmware_version"
        fi
    elif [[ $1 == '0x17\\n' ]]; then
        echo delete-client >> "$LOG"
        rm -r "$SYSFS/bus/i2c/devices/0-0017"
    fi
}}
"""

    def test_session_refuses_existing_client_without_mutation(self):
        setup = self.session_fixture()
        self.write(self.sysfs / "bus/i2c/devices/0-0017/name", "unrelated\n")
        result = self.run_bash(setup + f"run_session {shlex.quote(str(self.module))}")
        self.assert_failed(result, "already exists")
        self.assertFalse(self.log.exists())
        self.assertEqual((self.adapter / "new_device").read_text(), "")

    def test_session_refuses_existing_module_regardless_of_configuration(self):
        setup = self.session_fixture()
        parameter = self.sysfs / "module/fusion_hat/parameters/disable_auto_shutdown"
        for value in (None, "N", "Y"):
            with self.subTest(value=value):
                parameter.parent.mkdir(parents=True, exist_ok=True)
                if value is not None:
                    parameter.write_text(value + "\n")
                result = self.run_bash(setup + f"run_session {shlex.quote(str(self.module))}")
                self.assert_failed(result, f"disable_auto_shutdown={value or 'unknown'}")
                self.assertFalse(self.log.exists())

    def test_session_refuses_other_fusion_client(self):
        setup = self.session_fixture()
        # DT compatible matching also covers clients with a different name.
        self.write(self.sysfs / "bus/i2c/devices/1-0017/of_node/compatible", "sunfounder,fusion_hat\0")
        result = self.run_bash(setup + f"run_session {shlex.quote(str(self.module))}")
        self.assert_failed(result, "Existing Fusion HAT client")
        self.assertFalse(self.log.exists())

    def test_wrong_adapter_and_vermagic_are_read_only_failures(self):
        setup = self.session_fixture()
        result = self.run_bash(setup + f"run_session {shlex.quote(str(self.module))}", VERMAGIC="wrong")
        self.assert_failed(result, "vermagic")
        (self.adapter / "name").write_text("other controller\n")
        result = self.run_bash(setup + f"run_session {shlex.quote(str(self.module))}")
        self.assert_failed(result, "not the verified Opie")
        self.assertFalse(self.log.exists())

    def test_session_verifies_opt_out_before_creating_client(self):
        setup = self.session_fixture()
        result = self.run_bash(setup + f"run_session {shlex.quote(str(self.module))}", PARAM="N")
        self.assert_failed(result, "Shutdown opt-out is not active")
        self.assertEqual(self.log.read_text().splitlines(), [
            f"insmod {self.module} disable_auto_shutdown=1", "rmmod fusion_hat",
        ])
        self.assertEqual((self.adapter / "new_device").read_text(), "")

    def test_successful_session_and_failed_binding_cleanup_in_order(self):
        for bind in ("yes", "no"):
            with self.subTest(bind=bind):
                setup = self.session_fixture()
                self.log.unlink(missing_ok=True)
                result = self.run_bash(setup + f"run_session {shlex.quote(str(self.module))}", BIND=bind)
                if bind == "yes":
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("disable_auto_shutdown=Y", result.stdout)
                else:
                    self.assert_failed(result, "did not bind successfully")
                self.assertEqual(self.log.read_text().splitlines(), [
                    f"insmod {self.module} disable_auto_shutdown=1",
                    "create-client", "delete-client", "rmmod fusion_hat",
                ])
                self.assertFalse((self.sysfs / "bus/i2c/devices/0-0017").exists())
                self.assertFalse((self.sysfs / "module/fusion_hat").exists())

    def test_cleanup_leaves_unowned_resources_and_changed_driver(self):
        setup = self.session_fixture()
        client = self.sysfs / "bus/i2c/devices/0-0017"
        self.write(client / "name", "fusion_hat\n")
        other_driver = self.sysfs / "bus/i2c/drivers/other"
        other_driver.mkdir(parents=True)
        (client / "driver").symlink_to(other_driver)
        result = self.run_bash(setup + "cleanup")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(client.exists())
        result = self.run_bash(setup + "created_client=true; created_module=true; cleanup")
        self.assert_failed(result, "Client driver changed")
        self.assertTrue(client.exists())
        self.assertFalse(self.log.exists())

    def test_session_cleans_up_on_eof_and_termination(self):
        for termination in ("eof", "term"):
            with self.subTest(termination=termination):
                setup = self.session_fixture()
                self.log.unlink(missing_ok=True)
                if termination == "term":
                    setup += '\nread() { kill -TERM "$$"; }\n'
                result = self.run_bash(
                    setup + f"run_session {shlex.quote(str(self.module))}", input_text="",
                )
                self.assertEqual(result.returncode, 1 if termination == "eof" else 143, result.stderr)
                self.assertEqual(self.log.read_text().splitlines()[-2:], [
                    "delete-client", "rmmod fusion_hat",
                ])
                self.assertFalse((self.sysfs / "bus/i2c/devices/0-0017").exists())
                self.assertFalse((self.sysfs / "module/fusion_hat").exists())

    def test_cleanup_refuses_to_unload_module_with_another_bound_client(self):
        setup = self.session_fixture()
        driver = self.sysfs / "bus/i2c/drivers/fusion_hat"
        driver.mkdir(parents=True)
        (driver / "1-0017").symlink_to(self.adapter)
        result = self.run_bash(setup + "created_module=true; cleanup")
        self.assert_failed(result, "Another client is bound")
        self.assertFalse(self.log.exists())

    @unittest.skipUnless(shutil.which("gcc"), "host gcc is needed for shutdown behavior harness")
    def test_real_shutdown_code_with_stubbed_kernel_calls(self):
        # Compile the actual shutdown.c into a host harness, not a kernel module.
        for header in ("module", "i2c", "kernel", "workqueue", "mutex", "device", "suspend"):
            self.write(self.base / f"linux/{header}.h", "")
        source = self.write(self.base / "shutdown_test.c", r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#define FUSION_HAT_H
#define module_param(name, type, perm) _Static_assert((perm) == 0444, "load-time only")
#define MODULE_PARM_DESC(...)
#define MODULE_LICENSE(...)
#define MODULE_DESCRIPTION(...)
#define MODULE_AUTHOR(...)
#define EXPORT_SYMBOL(...)
#define dev_info(device, ...) ((void)(device))
#define dev_err(device, ...) ((void)(device))
#define mutex_lock(lock) ((void)(lock))
#define mutex_unlock(lock) ((void)(lock))
#define SHUTDOWN_REQUEST_NONE 0
#define SHUTDOWN_REQUEST_BATTERY 1
#define SHUTDOWN_REQUEST_BUTTON 2
#define CMD_READ_SHUTDOWN_STATUS 0x26
struct i2c_client { int dev; };
struct fusion_hat_dev { struct i2c_client *client; int lock; };
static int reads, poweroffs, request, read_result;
static void fake_power_off(void) { poweroffs++; }
static void (*pm_power_off)(void) = fake_power_off;
static int fusion_hat_i2c_read_byte(struct i2c_client *client, uint8_t cmd, uint8_t *value) {
    (void)client;
    assert(cmd == CMD_READ_SHUTDOWN_STATUS);
    reads++;
    *value = request;
    return read_result;
}
''' + f'#include "{ROOT / "driver/shutdown.c"}"\n' + r'''
int main(void) {
    struct i2c_client client = {0};
    struct fusion_hat_dev dev = {.client = &client};
    assert(!disable_auto_shutdown); /* preserve existing default */
    for (int reason = 0; reason <= 3; reason++) {
        request = reason;
        disable_auto_shutdown = true;
        reads = poweroffs = 0;
        fusion_hat_shutdown_request_work(&dev);
        fusion_hat_execute_shutdown(&dev, reason); /* exported entry point */
        assert(reads == 0 && poweroffs == 0);
        disable_auto_shutdown = false;
        fusion_hat_shutdown_request_work(&dev);
        assert(reads == 1 && poweroffs == (reason != 0));
    }
    poweroffs = 0;
    read_result = -1;
    fusion_hat_shutdown_request_work(&dev);
    assert(poweroffs == 0);
    pm_power_off = NULL;
    fusion_hat_execute_shutdown(&dev, SHUTDOWN_REQUEST_BUTTON);
    assert(poweroffs == 0);
    return 0;
}
''')
        binary = self.base / "shutdown_test"
        subprocess.run(["gcc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                        f"-I{self.base}", str(source), "-o", str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    unittest.main()
