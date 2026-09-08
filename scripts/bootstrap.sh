#!/bin/sh
# Reproducible owner-only bootstrap for Debian 13 / Armbian 25 (Trixie).
set -eu

RUNNER=${BOOTSTRAP_PREFIX:-/opt/neocortex-runner}
NC_HOME=${NC_HOME:-/root/.neocortex}
SYSTEMD_DIR=${SYSTEMD_DIR:-/etc/systemd/system}
APT_GET=${APT_GET:-apt-get}
SYSTEMCTL=${SYSTEMCTL:-systemctl}
OS_RELEASE=${OS_RELEASE:-/etc/os-release}
SOURCE_REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CODEX_VERSION=${CODEX_VERSION:-0.86.0}
CLAUDE_VERSION=${CLAUDE_VERSION:-2.1.76}
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
    case "${ID:-}" in debian|armbian) ;; *) fail "unsupported OS ID ${ID:-unknown}; supported: Debian 13 (trixie) and Armbian 25 Trixie" ;; esac
    [ "${VERSION_CODENAME:-}" = trixie ] || fail "unsupported release ${VERSION_CODENAME:-unknown}; supported: Debian 13 (trixie) and Armbian 25 Trixie"
    case "$(dpkg --print-architecture)" in amd64|arm64) ;; *) fail "unsupported architecture; supported: amd64 and arm64" ;; esac
    [ -f "$SOURCE_REPO/pyproject.toml" ] || fail "source checkout lacks pyproject.toml: $SOURCE_REPO"
    if [ "${BOOTSTRAP_SKIP_ROOT_CHECK:-0}" != 1 ] && [ "$(id -u)" -ne 0 ]; then
        fail "run as root (for /opt and systemd installation)"
    fi
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
    if npm list -g --depth=0 "$package@$version" >/dev/null 2>&1 && command -v "$executable" >/dev/null 2>&1; then
        return 0
    fi
    npm install -g "$package@$version"
    command -v "$executable" >/dev/null 2>&1 || fail "$executable was not installed onto PATH"
}

install_runner() {
    if [ -e "$RUNNER" ]; then
        git -C "$RUNNER" rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail "$RUNNER exists but is not a git checkout"
    else
        git clone "$SOURCE_REPO" "$RUNNER"
    fi
    # Run the import from the runner so the invoking checkout cannot mask a
    # missing editable installation through its current working directory.
    if [ ! -x "$RUNNER/.venv/bin/python" ] || [ ! -x "$RUNNER/.venv/bin/nc" ] ||
       ! (cd "$RUNNER" && ./.venv/bin/python -c '
import nc
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
origin = pathlib.Path(nc.__file__).resolve()
raise SystemExit(sys.version_info[:2] != (3, 13) or not origin.is_relative_to(root))
' "$RUNNER") ||
       ! "$RUNNER/.venv/bin/python" -c 'import pytest, ruff'; then
        rm -rf "$RUNNER/.venv"
        python3.13 -m venv "$RUNNER/.venv"
        "$RUNNER/.venv/bin/pip" install -e "$RUNNER"
        "$RUNNER/.venv/bin/pip" install "pytest==8.3.5" "ruff==0.9.10"
    fi
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
        command -v codex >/dev/null 2>&1 || fail "codex is unavailable; run codex login first"
        command -v claude >/dev/null 2>&1 || fail "claude is unavailable; run claude login first"
        codex login status >/dev/null 2>&1 || fail "codex is not logged in; run codex login first"
        claude auth status >/dev/null 2>&1 || fail "claude is not logged in; run claude login first"
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
echo "bootstrap: ready; run 'codex login' and 'claude login', then $0 --enable-timer"
