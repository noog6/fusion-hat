#!/usr/bin/env bash
# Native, build-only workflow. Never invokes the driver's top-level all/install.
set -euo pipefail

fail() { echo "Build preflight: $*" >&2; exit 1; }

if [[ $# != 2 ]]; then
    echo "Usage: $0 KERNEL_RELEASE KERNEL_BUILD_DIR" >&2
    exit 2
fi
target_release=$1
kernel_build=$2
driver_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

[[ -n $target_release && $target_release != */* ]] || fail "Invalid kernel release."
[[ -f $kernel_build/Makefile ]] || fail \
    "Missing $kernel_build/Makefile. Locate prepared headers for $target_release; see opie-bringup.md. No packages were installed."
kernel_build=$(cd -- "$kernel_build" && pwd -P)
for file in include/config/kernel.release include/generated/utsrelease.h \
            include/generated/autoconf.h include/config/auto.conf Module.symvers; do
    [[ -s $kernel_build/$file ]] || fail \
        "Missing/empty $kernel_build/$file. Use the matching, configured vendor kernel build/headers with symbol versions; see opie-bringup.md."
done
header_release=$(cat "$kernel_build/include/config/kernel.release")
[[ $header_release == "$target_release" ]] || fail \
    "Header release '$header_release' does not match requested '$target_release'. Select matching headers; do not change the running kernel to bypass this check."
grep -Fqx "#define UTS_RELEASE \"$target_release\"" \
    "$kernel_build/include/generated/utsrelease.h" || fail \
    "utsrelease.h disagrees with $target_release. Use a consistently prepared kernel build directory."

# This small workflow is native-only; cross compilation needs a separate plan.
[[ -z ${ARCH:-} && -z ${CROSS_COMPILE:-} ]] || fail \
    "ARCH/CROSS_COMPILE overrides are not supported by this native-only workflow."
case $(uname -m) in
    aarch64) config_arch=CONFIG_ARM64 ;;
    x86_64)  config_arch=CONFIG_X86_64 ;;
    *) fail "Native architecture $(uname -m) is not supported by this workflow." ;;
esac
grep -qx "$config_arch=y" "$kernel_build/include/config/auto.conf" || fail \
    "Headers do not match native architecture $(uname -m) ($config_arch)."
grep -qx 'CONFIG_MODULES=y' "$kernel_build/include/config/auto.conf" || fail \
    "Selected kernel does not enable CONFIG_MODULES."
for tool in make "${CC:-gcc}"; do
    command -v "$tool" >/dev/null || fail "Missing tool '$tool'. Provide the matching native kernel toolchain."
done

echo "Building only: release=$target_release architecture=$(uname -m) headers=$kernel_build"
# The kernel's modules target consumes obj-m; it does not run driver/Makefile all.
make -C "$kernel_build" M="$driver_dir" modules
echo "Built $driver_dir/fusion_hat.ko. Nothing installed or loaded."
