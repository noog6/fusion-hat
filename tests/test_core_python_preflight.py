"""Python extension prerequisites, without pip or hardware access."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("python_preflight", ROOT / "driver/core_python_preflight.py")
PREFLIGHT = importlib.util.module_from_spec(spec)
spec.loader.exec_module(PREFLIGHT)


class PythonPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.include = Path(self.temp.name)
        patches = (
            patch.object(PREFLIGHT.sys, "prefix", "/selected/venv"),
            patch.object(PREFLIGHT.sys, "base_prefix", "/selected/base"),
            patch.object(PREFLIGHT.sys, "version_info", (3, 14, 4)),
            patch.object(PREFLIGHT.sysconfig, "get_path", return_value=str(self.include)),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_missing_header_stops_before_package_installation(self):
        with patch.object(PREFLIGHT.importlib, "import_module", side_effect=ImportError):
            with self.assertRaisesRegex(RuntimeError, "Missing Python development header") as error:
                PREFLIGHT.check_environment()
        self.assertIn(str(self.include / "Python.h"), str(error.exception))
        self.assertIn("No pip installation has run", str(error.exception))

    def test_matching_header_allows_extension_build(self):
        (self.include / "Python.h").write_text("/* header fixture */")
        with patch.object(PREFLIGHT.importlib, "import_module", side_effect=ImportError):
            PREFLIGHT.check_environment()

    def test_existing_importable_evdev_does_not_need_build_headers(self):
        with patch.object(PREFLIGHT.importlib, "import_module"), \
             patch.object(PREFLIGHT.metadata, "version", return_value="2.0.0"):
            PREFLIGHT.check_environment()

    def test_system_python_and_wrong_version_are_rejected(self):
        with patch.object(PREFLIGHT.sys, "prefix", "/selected/base"):
            with self.assertRaisesRegex(RuntimeError, "existing venv"):
                PREFLIGHT.check_environment()
        with patch.object(PREFLIGHT.sys, "version_info", (3, 13, 5)):
            with self.assertRaisesRegex(RuntimeError, "Python 3.14.4"):
                PREFLIGHT.check_environment()


if __name__ == "__main__":
    unittest.main()
