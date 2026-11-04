"""Host checks for optional IIO; these are not vendor-kernel module builds."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class OptionalIIOTests(unittest.TestCase):
    def test_version_lookup_from_driver_and_kernel_working_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            target = directory / "version.mk"
            target.write_text("print-version:\n\t@printf '%s\\n' '$(DRIVER_VERSION)'\n")
            version = next(line.split('"')[1] for line in
                           (ROOT / "driver/main.h").read_text().splitlines()
                           if line.startswith("#define VERSION "))
            for cwd in (directory, ROOT / "driver"):
                with self.subTest(cwd=cwd):
                    result = subprocess.run(
                        ["make", "--no-print-directory", "-f", str(ROOT / "driver/Makefile"),
                         "-f", str(target), "print-version"],
                        cwd=cwd, text=True, capture_output=True, check=True,
                    )
                    self.assertEqual(result.stdout, version + "\n")
                    self.assertEqual(result.stderr, "")

    @unittest.skipUnless(shutil.which("gcc"), "host gcc is needed for ADC behavior harness")
    def test_adc_with_iio_disabled_builtin_and_modular(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            for header in ("module.h", "iio/iio.h", "i2c.h", "mutex.h"):
                path = directory / "linux" / header
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("")
            source = directory / "adc_test.c"
            source.write_text(r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <errno.h>
#include <string.h>
#define FUSION_HAT_H
/* Model the kernel's reachability rule for a module. No IIO API stubs exist
 * in the disabled case, so accidental references fail compilation/linking. */
#if defined(CONFIG_IIO) || (defined(CONFIG_IIO_MODULE) && defined(MODULE))
#define IS_REACHABLE(option) 1
#else
#define IS_REACHABLE(option) 0
#endif
#define EXPORT_SYMBOL(...)
#define mutex_lock(lock) ((void)(lock))
#define mutex_unlock(lock) ((void)(lock))
#define dev_err(device, ...) ((void)(device))
#define dev_info(device, ...) ((void)(device), info_count++)
#define CMD_READ_ADC_BASE 0x10
#define ADC_REFERENCE_VOLTAGE 3300
#define ADC_MAX_VALUE 4095
struct i2c_client { int dev; };
struct fusion_hat_dev { struct i2c_client *client; int lock; struct iio_dev *iio_devs[4]; };
static int info_count, reads, read_result, last_reg;
static int fusion_hat_i2c_read_word(struct i2c_client *client, uint8_t reg,
                                   uint16_t *value, bool big_endian) {
    (void)client;
    assert(big_endian);
    reads++;
    last_reg = reg;
    *value = 1234;
    return read_result;
}
#if IS_REACHABLE(CONFIG_IIO)
#define BIT(bit) (1U << (bit))
#define ARRAY_SIZE(array) (sizeof(array) / sizeof((array)[0]))
#define IIO_VOLTAGE 0
#define IIO_CHAN_INFO_RAW 1
#define IIO_CHAN_INFO_SCALE 2
#define IIO_VAL_INT 1
#define IIO_VAL_FRACTIONAL 2
#define INDIO_DIRECT_MODE 1
struct iio_chan_spec {
    int type, indexed, channel;
    unsigned int info_mask_separate;
    const char *datasheet_name;
};
struct iio_dev;
struct iio_info {
    int (*read_raw)(struct iio_dev *, const struct iio_chan_spec *, int *, int *, long);
};
struct iio_dev {
    const char *name;
    struct { void *parent; } dev;
    const struct iio_info *info;
    int modes, num_channels;
    const struct iio_chan_spec *channels;
    void *drvdata;
};
static struct iio_dev iio;
static int allocated, registered, unregistered, freed, register_result;
static struct iio_dev *iio_device_alloc(void *parent, int size) {
    (void)parent; (void)size;
    allocated++;
    return &iio;
}
static void *iio_device_get_drvdata(struct iio_dev *dev) { return dev->drvdata; }
static void iio_device_set_drvdata(struct iio_dev *dev, void *data) { dev->drvdata = data; }
static int iio_device_register(struct iio_dev *dev) {
    assert(dev == &iio); registered++; return register_result;
}
static void iio_device_free(struct iio_dev *dev) { assert(dev == &iio); freed++; }
static void iio_device_unregister(struct iio_dev *dev) { assert(dev == &iio); unregistered++; }
#endif
''' + f'#include "{ROOT / "driver/adc.c"}"\n' + r'''
int main(void) {
    struct i2c_client client = {0};
    struct fusion_hat_dev dev = {.client = &client};
    uint16_t value = 0;
    /* The raw MCU ADC helper remains available in every configuration. */
    assert(fusion_hat_read_adc(&client, 2, &value) == 0);
    assert(value == 1234 && last_reg == 0x14 && reads == 1);
    assert(fusion_hat_read_adc(&client, 4, &value) == -EINVAL);
    read_result = -EIO;
    assert(fusion_hat_read_adc(&client, 0, &value) == -EIO);
    read_result = 0;
    assert(fusion_hat_adc_probe(&dev) == 0);
#if IS_REACHABLE(CONFIG_IIO)
    assert(info_count == 0);
    assert(allocated == 1 && registered == 1 && dev.iio_devs[0] == &iio);
    assert(strcmp(iio.name, "fusion-hat") == 0 && iio.num_channels == 4);
    for (int channel = 0; channel < 4; channel++) {
        int val = 0, val2 = 0;
        assert(iio.channels[channel].channel == channel);
        assert(iio.info->read_raw(&iio, &iio.channels[channel], &val, &val2,
                                  IIO_CHAN_INFO_RAW) == IIO_VAL_INT);
        assert(val == 1234 && last_reg == 0x10 + channel * 2);
        assert(iio.info->read_raw(&iio, &iio.channels[channel], &val, &val2,
                                  IIO_CHAN_INFO_SCALE) == IIO_VAL_FRACTIONAL);
        assert(val == 3300 && val2 == 4096);
    }
    fusion_hat_adc_remove(&dev);
    assert(unregistered == 1 && freed == 1 && dev.iio_devs[0] == NULL);
    /* Registration failure remains an error when IIO is enabled. */
    register_result = -EIO;
    assert(fusion_hat_adc_probe(&dev) == -EIO);
    assert(freed == 2 && dev.iio_devs[0] == NULL);
    fusion_hat_adc_remove(&dev);
    assert(freed == 2);
#else
    assert(info_count == 1 && dev.iio_devs[0] == NULL);
    fusion_hat_adc_remove(&dev);
#endif
    return 0;
}
''')
            for name, flags in (
                ("disabled", []),
                ("builtin", ["-DCONFIG_IIO=1"]),
                ("modular", ["-DCONFIG_IIO_MODULE=1"]),
            ):
                with self.subTest(iio=name):
                    binary = directory / name
                    subprocess.run(
                        ["gcc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-DMODULE=1",
                         *flags, f"-I{directory}", str(source), "-o", str(binary)], check=True,
                    )
                    subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    unittest.main()
