#!/bin/sh
# Reproducible owner-only bootstrap for Debian 13 / Armbian 25 (Trixie).
set -eu

RUNNER=${BOOTSTRAP_PREFIX:-/opt/neocortex-runner}
NC_HOME=${NC_HOME:-/root/.neocortex}
SYSTEMD_DIR=${SYSTEMD_DIR:-/etc/systemd/system}
APT_GET=${APT_GET:-apt-get}
SYSTEMCTL=${SYSTEMCTL:-systemctl}
OS_RELEASE=${OS_RELEASE:-/etc/os-release}
# Current Armbian images retain the Debian identity in /etc/os-release.  Their
# Armbian release is recorded separately here (and may also be exposed as
# ARMBIAN_PRETTY_NAME in os-release).
ARMBIAN_RELEASE=${ARMBIAN_RELEASE:-/etc/armbian-release}
SOURCE_REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CODEX_VERSION=${CODEX_VERSION:-0.86.0}
CLAUDE_VERSION=${CLAUDE_VERSION:-2.1.76}
# Keep this in lockstep with deploy/neocortex.service.  SERVICE_PATH is
# overrideable only by the isolated command-stub tests; production uses this
# exact service PATH when locating and invoking vendor CLIs.
SERVICE_PATH=${SERVICE_PATH:-/opt/neocortex-runner/.venv/bin:/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}
ENABLE_TIMER=false

usage() {
    echo "usage: $0 [--enable-timer]" >&2
}

case "${1:-}" in
    "") ;;
    --enable-timer) ENABLE_TIMER=true ;;
    *) usage; exit 2 ;;
esac

fail() { echo "bootstrap: $*" >&2; exit 1; }

validate_host() {
    [ -r "$OS_RELEASE" ] || fail "cannot read $OS_RELEASE"
    # shellcheck disable=SC1090
    . "$OS_RELEASE"
    armbian_version=
    armbian_codename=${VERSION_CODENAME:-}
    if [ "${ID:-}" = armbian ]; then
        # Compatibility with older images that used ID=armbian.
        armbian_version=${VERSION_ID:-}
    elif [ -n "${ARMBIAN_PRETTY_NAME:-}" ]; then
        # Newer images use ID=debian and VERSION_ID=13, but retain this field.
        case "$ARMBIAN_PRETTY_NAME" in
            Armbian\ *) armbian_version=${ARMBIAN_PRETTY_NAME#Armbian }; armbian_version=${armbian_version%% *} ;;
        esac
    fi
    if [ -r "$ARMBIAN_RELEASE" ]; then
        # Do not source this host metadata: extract only the documented fields.
        release_version=$(sed -n 's/^VERSION=//p' "$ARMBIAN_RELEASE" | sed -n '1p' | tr -d '"')
        release_codename=$(sed -n 's/^DISTRIBUTION_CODENAME=//p' "$ARMBIAN_RELEASE" | sed -n '1p' | tr -d '"')
        [ -z "$armbian_version" ] || [ -z "$release_version" ] || [ "$armbian_version" = "$release_version" ] || fail "conflicting Armbian release metadata"
        [ -z "$armbian_codename" ] || [ -z "$release_codename" ] || [ "$armbian_codename" = "$release_codename" ] || fail "conflicting Armbian base metadata"
        [ -n "$release_version" ] && armbian_version=$release_version
        [ -n "$release_codename" ] && armbian_codename=$release_codename
    fi
    if [ -n "$armbian_version" ]; then
        case "$armbian_version" in 25|25.*) ;; *) fail "unsupported Armbian release $armbian_version; supported: Armbian 25 Trixie" ;; esac
        [ "$armbian_codename" = trixie ] || fail "unsupported Armbian base ${armbian_codename:-unknown}; supported: Armbian 25 Trixie"
    else
        [ "${ID:-}" = debian ] || fail "unsupported OS ID ${ID:-unknown}; supported: Debian 13 (trixie) and Armbian 25 Trixie"
        [ "${VERSION_CODENAME:-}" = trixie ] || fail "unsupported Debian release ${VERSION_CODENAME:-unknown}; supported: Debian 13 (trixie)"
    fi
    case "$(dpkg --print-architecture)" in amd64|arm64) ;; *) fail "unsupported architecture; supported: amd64 and arm64" ;; esac
    [ -f "$SOURCE_REPO/pyproject.toml" ] || fail "source checkout lacks pyproject.toml: $SOURCE_REPO"
    if [ "${BOOTSTRAP_SKIP_ROOT_CHECK:-0}" != 1 ] && [ "$(id -u)" -ne 0 ]; then
        fail "run as root (for /opt and systemd installation)"
    fi
}

service_command() {
    PATH="$SERVICE_PATH" command -v "$1" >/dev/null 2>&1
}

service_run() {
    PATH="$SERVICE_PATH" "$@"
}

missing_packages() {
    for package in git sqlite3 python3.13 python3.13-venv python3-venv nodejs npm; do
        dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null | grep -qx installed || echo "$package"
    done
}

ensure_packages() {
    packages=$(missing_packages)
    [ -n "$packages" ] || return 0
    $APT_GET update
    # Deliberate splitting: package names above are fixed, never user input.
    $APT_GET install -y $packages
}

ensure_npm_package() {
    package=$1 version=$2 executable=$3
    if npm list -g --depth=0 "$package@$version" >/dev/null 2>&1 && service_command "$executable"; then
        return 0
    fi
    npm install -g "$package@$version"
    service_command "$executable" || fail "$executable was not installed onto the service PATH: $SERVICE_PATH"
}

runner_install_is_valid() {
    # Do not run this probe from the checkout: Python puts the current working
    # directory on sys.path, which would make a missing editable install look
    # healthy.  An import from / with PYTHONPATH removed must resolve nc back
    # into this checkout through the venv's editable-install metadata.
    # The service invokes these through SERVICE_PATH, not by absolute path.
    # Check both the runner launchers and their discoverability so a venv with
    # importable modules but missing console scripts is repaired on rerun.
    [ -x "$RUNNER/.venv/bin/python" ] && [ -x "$RUNNER/.venv/bin/nc" ] &&
       [ -x "$RUNNER/.venv/bin/pytest" ] && [ -x "$RUNNER/.venv/bin/ruff" ] &&
       service_command python && service_command nc &&
       service_command pytest && service_command ruff &&
       (cd / && env -u PYTHONPATH "$RUNNER/.venv/bin/python" -c '
import nc
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
origin = pathlib.Path(nc.__file__).resolve()
raise SystemExit(sys.version_info[:2] != (3, 13) or not origin.is_relative_to(root))
' "$RUNNER") &&
       "$RUNNER/.venv/bin/python" -c 'import pytest, ruff'
}

install_runner() {
    if [ -e "$RUNNER" ]; then
        git -C "$RUNNER" rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail "$RUNNER exists but is not a git checkout"
    else
        git clone "$SOURCE_REPO" "$RUNNER"
    fi
    if ! runner_install_is_valid; then
        rm -rf "$RUNNER/.venv"
        python3.13 -m venv "$RUNNER/.venv"
        "$RUNNER/.venv/bin/pip" install -e "$RUNNER"
        "$RUNNER/.venv/bin/pip" install "pytest==8.3.5" "ruff==0.9.10"
    fi
    runner_install_is_valid || fail "runner editable installation did not validate"
}

install_units() {
    changed=false
    for unit in neocortex.service neocortex.timer; do
        target="$SYSTEMD_DIR/$unit"
        if [ ! -f "$target" ] || ! cmp -s "$SOURCE_REPO/deploy/$unit" "$target"; then
            install -D -m 0644 "$SOURCE_REPO/deploy/$unit" "$target"
            changed=true
        fi
    done
    if [ "$changed" = true ]; then "$SYSTEMCTL" daemon-reload; fi
    if [ "$ENABLE_TIMER" = true ]; then
        [ ! -e "$NC_HOME/STOP" ] || fail "STOP exists; run nc resume before enabling the timer"
        service_command codex || fail "codex is unavailable on the service PATH; run codex login first"
        service_command claude || fail "claude is unavailable on the service PATH; run claude auth login first"
        service_run codex login status >/dev/null 2>&1 || fail "codex is not logged in; run codex login first"
        service_run claude auth status >/dev/null 2>&1 || fail "claude is not logged in; run claude auth login first"
        "$SYSTEMCTL" enable --now neocortex.timer
    fi
}

validate_host                    # no deployment mutation precedes this check
ensure_packages
ensure_npm_package @openai/codex "$CODEX_VERSION" codex
ensure_npm_package @anthropic-ai/claude-code "$CLAUDE_VERSION" claude
install_runner
mkdir -p "$NC_HOME"
if [ ! -f "$NC_HOME/config.json" ]; then "$RUNNER/.venv/bin/nc" init; else "$RUNNER/.venv/bin/nc" health >/dev/null; fi
install_units
echo "bootstrap: ready; run 'codex login' and 'claude auth login', then $0 --enable-timer"
