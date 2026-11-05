#!/usr/bin/env bash
# Unified entry point. Bootstrap needs existing bash, git and python3; no apt here.
# curl -fsSL https://raw.githubusercontent.com/noog6/fusion-hat/v1/install.sh | sudo bash -s -- [options]
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

# A local source tree always wins, including uncommitted work. Never pull/reset it.
installer_dir=''
if [[ -n ${BASH_SOURCE[0]:-} && -f ${BASH_SOURCE[0]} ]]; then
    installer_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
fi
if [[ ! -f ${installer_dir:-/nonexistent}/driver/install.py && -f $PWD/driver/install.py && -f $PWD/pyproject.toml ]]; then
    installer_dir=$PWD
fi
if [[ -n $installer_dir && -f $installer_dir/driver/install.py ]]; then
    exec python3 "$installer_dir/driver/install.py" "$@"
fi
# Piped/saved standalone script: resolve the requested ref ONCE, then execute
# every installer and build asset from that immutable Git object.
repository=${FUSION_HAT_REPOSITORY:-noog6/fusion-hat}
requested_ref=${FUSION_HAT_BRANCH:-v1}
args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
    case ${args[i]} in
        --repo=*) repository=${args[i]#--repo=} ;;
        --ref=*) requested_ref=${args[i]#--ref=} ;;
        --repo|--ref)
            ((i+1<${#args[@]})) || { echo "${args[i]} requires a value" >&2; exit 2; }
            if [[ ${args[i]} == --repo ]]; then repository=${args[i+1]}; else requested_ref=${args[i+1]}; fi
            i=$((i+1)) ;;
        --help|-h)
            echo 'Standalone bootstrap: --repo OWNER/REPO --ref BRANCH|TAG|SHA (defaults: noog6/fusion-hat v1).'
            echo 'Stages: --core --audio --voice; selection: --runtime-user USER --venv /path or --python /venv/bin/python.'
            echo 'Also: --check --verbose --platform auto|raspberry-pi|orange-pi --headers PATH --install-pulseaudio --set-audio-defaults --hat-idle-release.'
            echo 'See driver/unified-install.md in the selected repository/ref for all options.'
            exit 0 ;;
    esac
done
[[ $repository =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ && $repository != *..* ]] || { echo 'Use --repo OWNER/REPO' >&2; exit 2; }
[[ -n $requested_ref && $requested_ref != -* ]] || { echo 'Invalid --ref' >&2; exit 2; }
for tool in git python3; do command -v "$tool" >/dev/null || { echo "Bootstrap requires existing $tool; install it explicitly or use a checkout." >&2; exit 1; }; done
bootstrap_dir=$(mktemp -d "${TMPDIR:-/tmp}/fusion-hat-bootstrap.XXXXXXXX")
trap 'rm -rf -- "$bootstrap_dir"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
git init -q "$bootstrap_dir/source"
git -C "$bootstrap_dir/source" fetch -q --depth=1 "https://github.com/$repository.git" "$requested_ref"
revision=$(git -C "$bootstrap_dir/source" rev-parse 'FETCH_HEAD^{commit}')
git -C "$bootstrap_dir/source" checkout -q --detach "$revision"
export FUSION_HAT_BOOTSTRAP_REPOSITORY="$repository" FUSION_HAT_BOOTSTRAP_REVISION="$revision"
python3 "$bootstrap_dir/source/driver/install.py" "$@"
