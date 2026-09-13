#!/usr/bin/env bash
# Owner-only, explicit setup/rollback of an isolated ACP runtime.  Never use
# this from bootstrap, a service unit, or an automated test.
set -euo pipefail

readonly package='@agentclientprotocol/codex-acp'
readonly version='1.11.0'
readonly integrity='sha512-opPKsRaekgdmQpOpHrR0EEDn9chgtiN+b+h0V78fTuQP84TNzB7vrn3EtKODwbiJQTBHJAlynjSFQazFfaT+VQ=='
readonly script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly reviewed_lock="$script_dir/codex_acp_runtime.lock.json"
readonly node_floor=20
usage() { echo "usage: $0 install RUNTIME_DIR TARBALL | verify RUNTIME_DIR | rollback RUNTIME_DIR" >&2; exit 2; }
[[ $# -ge 2 ]] || usage
action=$1; runtime=$2
case "$(uname -s):$(uname -m)" in Linux:x86_64|Linux:amd64) platform=linux-amd64; node_arch=x64;; Linux:aarch64|Linux:arm64) platform=linux-arm64; node_arch=arm64;; *) echo 'unsupported platform: Linux amd64/arm64 only' >&2; exit 1;; esac
case "$node_arch" in
  x64) binary_triple=x86_64-unknown-linux-musl;;
  arm64) binary_triple=aarch64-unknown-linux-musl;;
esac
case "$action" in
install)
  [[ $# -eq 3 ]] || usage; tarball=$(realpath "$3")
  [[ ! -e "$runtime" ]] || { echo 'runtime already exists; choose a new idle-boundary directory' >&2; exit 1; }
  actual="sha512-$(openssl dgst -sha512 -binary "$tarball" | base64 -w0)"
  [[ "$actual" == "$integrity" ]] || { echo 'tarball integrity mismatch' >&2; exit 1; }
  mkdir -m 700 "$runtime"; trap 'rm -rf "$runtime"' ERR
  # This private, path-bound record is deliberately created before any
  # fallible work.  It is the rollback authority for an interrupted install;
  # a reviewed lock/tarball alone is public and never authorizes deletion.
  runtime_abs="$(realpath "$runtime")"
  printf 'runtime=%s\nuid=%s\nformat=1\n' "$runtime_abs" "$(id -u)" > "$runtime/.nc-acp-owner-install.json"
  chmod 600 "$runtime/.nc-acp-owner-install.json"
  cp "$tarball" "$runtime/codex-acp-1.11.0.tgz"
  # Never generate a lock here: published dependency ranges would make the
  # registry state part of this installation.  npm ci receives the reviewed,
  # exact lock shipped with this script and the package manifest in the SRI-
  # checked tarball.  npm ci deliberately does *not* install its root project,
  # so install its exact dependencies first, then unpack that verified root
  # artifact and create the same local bin link npm would create for it.
  test -f "$reviewed_lock" || { echo 'reviewed ACP lock is missing' >&2; exit 1; }
  tar -xOf "$runtime/codex-acp-1.11.0.tgz" package/package.json > "$runtime/package.json"
  # npm only warns about engines by default.  Gate the executable that will
  # perform this exact install before it can create a successful receipt.
  node_path="$(command -v node 2>/dev/null || true)"
  [[ -n "$node_path" ]] || { echo 'Node executable is unavailable' >&2; false; }
  node_path="$(realpath "$node_path")"
  node_version="$("$node_path" -p 'process.versions.node' 2>/dev/null || true)"
  node_major="${node_version%%.*}"
  # The reviewed production (non-dev) lock contains open@11.0.1 with the
  # published engine >=20.  npm ci only warns about engines, so enforce this
  # effective locked-graph floor before it can produce an install receipt.
  lock_floor="$("$node_path" -e "const l=require(process.argv[1]); const p=l.packages; let floor=0; for (const [k,v] of Object.entries(p)) { if (!k || v.dev || !v.engines || typeof v.engines.node !== 'string') continue; for (const m of v.engines.node.matchAll(/>=\\s*(\\d+)/g)) floor=Math.max(floor, +m[1]); } if (floor !== $node_floor || p['node_modules/open']?.version !== '11.0.1' || p['node_modules/open']?.engines?.node !== '>=20') process.exit(1); process.stdout.write(String(floor))" "$reviewed_lock" 2>/dev/null || true)"
  [[ "$lock_floor" == "$node_floor" && "$node_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+ ]] && [[ "$node_major" -ge "$node_floor" ]] || {
    echo "unsupported Node runtime '$node_version'; reviewed non-dev dependency graph requires Node >=$node_floor" >&2; false;
  }
  node_digest="$(sha256sum "$node_path" | awk '{print $1}')"
  printf '{"path":"%s","version":"%s","sha256":"%s","minimum_major":%s}\n' "$node_path" "$node_version" "$node_digest" "$node_floor" > "$runtime/node.json"
  cp "$reviewed_lock" "$runtime/package-lock.json"
  # Keep immutable content-addressed tarballs.  Use-time inspection compares
  # the extracted files to these bytes using the reviewed lock SRI values.
  npm ci --ignore-scripts --omit=dev --cache "$runtime/npm-cache" --prefix "$runtime"
  acp_dir="$runtime/node_modules/@agentclientprotocol/codex-acp"
  mkdir -p "$acp_dir" "$runtime/node_modules/.bin"
  tar -xzf "$runtime/codex-acp-1.11.0.tgz" -C "$acp_dir" --strip-components=1
  test -f "$acp_dir/dist/index.js" || { echo 'verified ACP artifact lacks dist/index.js' >&2; exit 1; }
  chmod +x "$acp_dir/dist/index.js"
  ln -s ../@agentclientprotocol/codex-acp/dist/index.js "$runtime/node_modules/.bin/codex-acp"
  npm ls --all --json --prefix "$runtime" > "$runtime/resolved-dependencies.json"
  (cd "$runtime" && find node_modules -type f -print0 | sort -z | xargs -0 sha256sum) > "$runtime/installed.sha256"
  (cd "$runtime" && sha256sum node_modules/.bin/codex-acp) > "$runtime/launcher.sha256"
  test "$("$node_path" -p "require('$runtime/node_modules/@agentclientprotocol/codex-acp/package.json').version")" = "$version"
  test "$("$node_path" -p "require('$runtime/node_modules/@openai/codex/package.json').version")" = 0.153.4
  test "$("$node_path" -p "require('$runtime/node_modules/@agentclientprotocol/sdk/package.json').version")" = 1.4.0
  test "$("$node_path" -p "require('$runtime/node_modules/@openai/codex-linux-$node_arch/package.json').version")" = "0.153.4-linux-$node_arch"
  "$node_path" -e "const p=require('$runtime/node_modules/@openai/codex/package.json'); if(!p.engines || p.engines.node !== '>=16') process.exit(1)"
  test -x "$runtime/node_modules/@openai/codex-linux-$node_arch/vendor/$binary_triple/bin/codex" || {
    echo 'published Codex platform artifact lacks expected native binary' >&2; exit 1;
  }
  printf '{"package":"%s","version":"%s","integrity":"%s","platform":"%s"}\n' "$package" "$version" "$integrity" "$platform" > "$runtime/receipt.json"
  trap - ERR; echo "installed isolated $package@$version; run verify before explicit owner smoke";;
verify)
  python -c 'from pathlib import Path; from nc.acp_runtime import inspect_runtime; import sys; r=inspect_runtime(Path(sys.argv[1])); print(r.command)' "$runtime";;
rollback)
  [[ $# -eq 2 ]] || usage
  # Rollback is deliberately less strict than launch inspection: it can remove
  # a damaged/interrupted install, but only at the owner-established location.
  runtime=$(realpath -e "$runtime")
  [[ -d "$runtime" && "$runtime" != / && "$runtime" != "$PWD" ]] || {
    echo 'refusing unsafe rollback target' >&2; exit 1;
  }
  identity="$runtime/.nc-acp-owner-install.json"
  [[ -f "$identity" && ! -L "$identity" && "$(stat -c '%u:%a' "$runtime")" =~ ^$(id -u):7[0-7][0-7]$ && "$(stat -c '%u:%a' "$identity")" =~ ^$(id -u):[46]00$ ]] || {
    echo 'refusing rollback: target is not an owner-established isolated runtime' >&2; exit 1;
  }
  expected_identity="$(printf 'runtime=%s\nuid=%s\nformat=1' "$runtime" "$(id -u)")"
  [[ "$(cat "$identity")" == "$expected_identity" ]] || {
    echo 'refusing rollback: runtime identity does not bind this location' >&2; exit 1;
  }
  rm -rf -- "$runtime"; echo 'isolated runtime removed (only perform while ACP is idle)';;
*) usage;; esac
