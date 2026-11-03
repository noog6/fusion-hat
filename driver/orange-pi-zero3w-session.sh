#!/usr/bin/env bash
# Temporary session for the documented Zero 3W target. No persistent install.
set -euo pipefail

SYSFS=/sys
created_module=false
created_client=false

fail() { echo "Orange Pi Zero 3W session: $*" >&2; return 1; }

preflight() {
    local client value
    [[ $(uname -m) == aarch64 && $(uname -r) == 6.6.98-vendor-sun60iw2 ]] || {
        fail "Expected Orange Pi Zero 3W target: aarch64 / 6.6.98-vendor-sun60iw2. Stop and review this procedure for a different target."; return 1;
    }
    if [[ -d $SYSFS/module/fusion_hat ]]; then
        value=$(cat "$SYSFS/module/fusion_hat/parameters/disable_auto_shutdown" 2>/dev/null) || value=unknown
        fail "fusion_hat is already loaded (disable_auto_shutdown=$value). Refusing to reuse or unload an existing module, even if disabled."; return 1
    fi
    [[ ! -e $SYSFS/bus/i2c/devices/0-0017 ]] || {
        fail "Client 0-0017 already exists. Refusing to change ownership or remove it."; return 1;
    }
    # The driver is a singleton. Loading must not probe another existing client.
    for client in "$SYSFS"/bus/i2c/devices/*; do
        [[ -d $client ]] || continue
        if [[ $(cat "$client/name" 2>/dev/null) == fusion_hat ]] ||
           grep -zqx 'sunfounder,fusion_hat' "$client/of_node/compatible" 2>/dev/null; then
            fail "Existing Fusion HAT client at $client. Refusing to load."; return 1
        fi
    done
    [[ $(cat "$SYSFS/bus/i2c/devices/i2c-0/name") == 'SUNXI TWI(0x0000000002510000)' ]] || {
        fail "Bus 0 is not the verified Orange Pi Zero 3W TWI0 adapter."; return 1;
    }
    [[ -w $SYSFS/bus/i2c/devices/i2c-0/new_device &&
       -w $SYSFS/bus/i2c/devices/i2c-0/delete_device ]] || {
        fail "Missing writable I2C client controls. Run with sudo on the Orange Pi Zero 3W after enabling i2c0."; return 1;
    }
}

cleanup() {
    local binding
    if $created_client; then
        # No forced unbind: stop if somebody changed ownership during the session.
        [[ $(cat "$SYSFS/bus/i2c/devices/0-0017/name") == fusion_hat ]] || {
            fail "Client identity changed; leaving client/module for manual inspection."; return 1;
        }
        binding=$(readlink -f "$SYSFS/bus/i2c/devices/0-0017/driver")
        if [[ -L $SYSFS/bus/i2c/devices/0-0017/driver &&
              $binding != "$SYSFS/bus/i2c/drivers/fusion_hat" ]]; then
            fail "Client driver changed; leaving client/module for manual inspection."; return 1
        fi
        printf '0x17\n' > "$SYSFS/bus/i2c/devices/i2c-0/delete_device" || return 1
        created_client=false
        echo "Removed session-created client 0-0017."
    fi
    if $created_module; then
        # Never unload if another client has bound since we loaded the module.
        for binding in "$SYSFS"/bus/i2c/drivers/fusion_hat/*-????; do
            [[ ! -L $binding ]] || {
                fail "Another client is bound ($binding); leaving module loaded."; return 1;
            }
        done
        rmmod fusion_hat || return 1
        created_module=false
        echo "Unloaded session-created fusion_hat module."
    fi
}

finish() {
    local status=$?
    trap - EXIT INT TERM HUP
    if ! cleanup; then
        echo "Cleanup incomplete. Inspect sysfs and dmesg before any manual removal." >&2
        status=1
    fi
    exit "$status"
}

run_session() {
    local module_file=$1 vermagic
    [[ -f $module_file ]] || { fail "Module file not found: $module_file"; return 1; }
    for tool in modinfo insmod rmmod; do
        command -v "$tool" >/dev/null || { fail "Missing kmod tool: $tool"; return 1; }
    done
    [[ $(modinfo -F name "$module_file") == fusion_hat ]] || { fail "Wrong module name."; return 1; }
    vermagic=$(modinfo -F vermagic "$module_file")
    [[ ${vermagic%% *} == "$(uname -r)" ]] || { fail "Module vermagic '$vermagic' does not match the running kernel."; return 1; }
    preflight || return 1

    trap finish EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    trap 'exit 129' HUP
    insmod "$module_file" disable_auto_shutdown=1 || return 1
    created_module=true
    [[ $(cat "$SYSFS/module/fusion_hat/parameters/disable_auto_shutdown") == Y ]] || {
        fail "Shutdown opt-out is not active; refusing to create a client."; return 1;
    }
    # Recheck immediately before creation; do not run concurrent device managers.
    [[ ! -e $SYSFS/bus/i2c/devices/0-0017 ]] || { fail "Client appeared during loading."; return 1; }
    printf 'fusion_hat 0x17\n' > "$SYSFS/bus/i2c/devices/i2c-0/new_device" || return 1
    created_client=true
    [[ $(readlink -f "$SYSFS/bus/i2c/devices/0-0017/driver") == "$SYSFS/bus/i2c/drivers/fusion_hat" &&
       -r $SYSFS/class/fusion_hat/fusion_hat/firmware_version ]] || {
        fail "Client did not bind successfully. Inspect dmesg; session resources will be cleaned up."; return 1;
    }
    echo "Bound 0-0017 to fusion_hat; disable_auto_shutdown=Y."
    echo "Use a second terminal for the read-only commands in driver/orange-pi-zero3w-bringup.md."
    read -r -p "Press Enter when finished to remove this client and unload this module: "
}

main() {
    [[ $# == 1 ]] || { echo "Usage: sudo $0 /absolute/path/to/fusion_hat.ko" >&2; return 2; }
    [[ $EUID == 0 ]] || { fail "Run with sudo on the Orange Pi Zero 3W (actuators disconnected)."; return 1; }
    run_session "$1"
}

# Allow tests to source the preflight/cleanup without touching real sysfs.
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
