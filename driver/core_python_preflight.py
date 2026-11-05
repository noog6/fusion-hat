"""Prerequisites for the selected core-only venv; never installs packages."""
import importlib
from importlib import metadata
from pathlib import Path
import sys
import sysconfig


def check_environment(platform="orange-pi"):
    if sys.prefix == sys.base_prefix:
        raise RuntimeError("Select an existing venv interpreter")
    if platform == "orange-pi" and tuple(sys.version_info[:3]) != (3, 14, 4):
        raise RuntimeError("This experimental path targets Python 3.14.4")
    try:
        importlib.import_module("evdev")
        metadata.version("evdev")
    except (ImportError, metadata.PackageNotFoundError):
        header = Path(sysconfig.get_path("include")) / "Python.h"
        if not header.is_file():
            raise RuntimeError(
                f"Missing Python development header: {header}. "
                "evdev needs to build its native extension. Obtain development headers "
                "matching the selected base interpreter before retrying; inspect the "
                "board's installed packages and apt candidates rather than guessing a package. "
                "No pip installation has run."
            )


if __name__ == "__main__":
    print(sys.version)
    try:
        check_environment(sys.argv[1] if len(sys.argv) > 1 else "orange-pi")
    except RuntimeError as error:
        raise SystemExit(f"Core Python preflight: {error}")
