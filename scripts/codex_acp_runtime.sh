#!/usr/bin/env bash
# Owner-only, explicit setup/rollback of an isolated ACP runtime.  Never use
# this from bootstrap, a service unit, or an automated test.
set -euo pipefail

readonly package='@agentclientprotocol/codex-acp'
readonly version='1.11.0'
readonly integrity='sha512-opPKsRaekgdmQpOpHrR0EEDn9chgtiN+b+h0V78fTuQP84TNzB7vrn3EtKODwbiJQTBHJAlynjSFQazFfaT+VQ=='
readonly script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly reviewed_lock="$script_dir/codex_acp_runtime.lock.json"
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
  mkdir -p "$runtime"; trap 'rm -rf "$runtime"' ERR
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
  node_version="$(node -p 'process.versions.node' 2>/dev/null || true)"
  node_major="${node_version%%.*}"
  [[ "$node_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+ ]] && [[ "$node_major" -ge 16 ]] || {
    echo "unsupported Node runtime '$node_version'; pinned Codex 0.153.4 requires Node >=16" >&2; false;
  }
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
  test "$(node -p "require('$runtime/node_modules/@agentclientprotocol/codex-acp/package.json').version")" = "$version"
  test "$(node -p "require('$runtime/node_modules/@openai/codex/package.json').version")" = 0.153.4
  test "$(node -p "require('$runtime/node_modules/@agentclientprotocol/sdk/package.json').version")" = 1.4.0
  test "$(node -p "require('$runtime/node_modules/@openai/codex-linux-$node_arch/package.json').version")" = "0.153.4-linux-$node_arch"
  node -e "const p=require('$runtime/node_modules/@openai/codex/package.json'); if(!p.engines || p.engines.node !== '>=16') process.exit(1)"
  test -x "$runtime/node_modules/@openai/codex-linux-$node_arch/vendor/$binary_triple/bin/codex" || {
    echo 'published Codex platform artifact lacks expected native binary' >&2; exit 1;
  }
  printf '{"package":"%s","version":"%s","integrity":"%s","platform":"%s"}\n' "$package" "$version" "$integrity" "$platform" > "$runtime/receipt.json"
  trap - ERR; echo "installed isolated $package@$version; run verify before explicit owner smoke";;
verify)
  python -c 'from pathlib import Path; from nc.acp_runtime import inspect_runtime; import sys; r=inspect_runtime(Path(sys.argv[1])); print(r.command)' "$runtime";;
rollback)
  [[ $# -eq 2 ]] || usage
  # Do not treat a receipt filename as authority to delete.  Resolve an
  # existing directory first, reject broad filesystem targets, then require
  # the same complete use-time inspection as launch/verify.
  runtime=$(realpath -e "$runtime")
  [[ -d "$runtime" && "$runtime" != / && "$runtime" != "$PWD" ]] || {
    echo 'refusing unsafe rollback target' >&2; exit 1;
  }
  python -c 'from pathlib import Path; from nc.acp_runtime import inspect_runtime; import sys; inspect_runtime(Path(sys.argv[1]))' "$runtime" || {
    echo 'refusing rollback: target is not the verified pinned isolated runtime' >&2; exit 1;
  }
  rm -rf -- "$runtime"; echo 'isolated runtime removed (only perform while ACP is idle)';;
*) usage;; esac
