"""Offline runtime-inspection coverage; no npm, real credentials, or ACP binary."""

import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from nc import acp_runtime


def _stub_executable(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\nset -eu\n" + body)
    path.chmod(0o700)


def test_owner_installer_unpacks_verified_root_artifact_and_launcher(tmp_path, monkeypatch):
    """Exercise the shell layout, rather than inventing its post-install paths."""
    source = tmp_path / "source" / "package"
    (source / "dist").mkdir(parents=True)
    (source / "package.json").write_text('{"version":"1.11.0"}')
    (source / "dist" / "index.js").write_text("#!/usr/bin/env node\n")
    tarball = tmp_path / "codex-acp.tgz"
    with tarfile.open(tarball, "w:gz") as archive:
        archive.add(source, arcname="package")
    tools = tmp_path / "tools"
    tools.mkdir()
    _, node_arch = acp_runtime._host_platform()
    _stub_executable(tools / "openssl", "printf x")
    # The script forms SRI by piping openssl's bytes to base64.
    _stub_executable(tools / "base64", "cat >/dev/null\nprintf '%s' 'opPKsRaekgdmQpOpHrR0EEDn9chgtiN+b+h0V78fTuQP84TNzB7vrn3EtKODwbiJQTBHJAlynjSFQazFfaT+VQ=='")
    _stub_executable(tools / "npm", """
prefix="${@: -1}"
if [ "$1" = ls ]; then printf '{}\\n'; exit 0; fi
for spec in '@agentclientprotocol/sdk:1.4.0' '@openai/codex:0.153.4' "@openai/codex-linux-${ACP_TEST_ARCH}:0.153.4-linux-${ACP_TEST_ARCH}"; do
  name="${spec%%:*}"; version="${spec#*:}"; dir="$prefix/node_modules/$name"
  mkdir -p "$dir"; printf '{"version":"%s"}\\n' "$version" > "$dir/package.json"
done
    case "$ACP_TEST_ARCH" in x64) triple=x86_64-unknown-linux-musl;; arm64) triple=aarch64-unknown-linux-musl;; esac
    mkdir -p "$prefix/node_modules/@openai/codex-linux-${ACP_TEST_ARCH}/vendor/$triple/bin"
    printf '#!/bin/sh\\n' > "$prefix/node_modules/@openai/codex-linux-${ACP_TEST_ARCH}/vendor/$triple/bin/codex"
    chmod +x "$prefix/node_modules/@openai/codex-linux-${ACP_TEST_ARCH}/vendor/$triple/bin/codex"
""")
    _stub_executable(tools / "node", """
case "$*" in
  *process.versions.node*) printf '%s' "${ACP_TEST_NODE_VERSION:-20.19.0}";;
  *codex-acp*) printf 1.11.0;; *'@openai/codex/package.json'*) printf 0.153.4;;
  *sdk/package.json*) printf 1.4.0;; *codex-linux-*) printf '0.153.4-linux-%s' "$ACP_TEST_ARCH";; *) exit 0;;
esac
""")
    runtime = tmp_path / "runtime"
    script = Path(__file__).parents[1] / "scripts" / "codex_acp_runtime.sh"
    environment = os.environ | {"PATH": str(tools) + os.pathsep + os.environ["PATH"],
                                "ACP_TEST_ARCH": node_arch}
    subprocess.run([str(script), "install", str(runtime), str(tarball)], check=True, env=environment)
    launcher = runtime / "node_modules" / ".bin" / "codex-acp"
    assert launcher.is_symlink()
    assert launcher.resolve() == runtime / "node_modules" / "@agentclientprotocol" / "codex-acp" / "dist" / "index.js"
    assert (launcher.resolve()).is_file()
    monkeypatch.setattr(acp_runtime, "_verify_tarball", lambda _root: None)
    inspected = acp_runtime.inspect_runtime(runtime)
    assert inspected.evidence.command == (str(launcher),)


def runtime_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, arch: str = "x64") -> Path:
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "package-lock.json").write_bytes(acp_runtime._REVIEWED_LOCK.read_bytes())
    for package, version in (("@agentclientprotocol/codex-acp", "1.11.0"),
                             ("@agentclientprotocol/sdk", "1.4.0"),
                             ("@openai/codex", "0.153.4"),
                             (f"@openai/codex-linux-{arch}", f"0.153.4-linux-{arch}")):
        path = root / "node_modules" / package
        path.mkdir(parents=True, exist_ok=True)
        (path / "package.json").write_text('{"version": "' + version + '"}')
    command = root / "node_modules" / ".bin" / "codex-acp"
    command.parent.mkdir(parents=True)
    command.write_text("#!/bin/sh\n")
    command.chmod(0o700)
    binary = acp_runtime._platform_binary(
        root / "node_modules" / "@openai" / f"codex-linux-{arch}", arch)
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o700)
    (root / "launcher.sha256").write_text(
        acp_runtime.hashlib.sha256(command.read_bytes()).hexdigest() + "  node_modules/.bin/codex-acp\n")
    (root / "receipt.json").write_text('{"package":"@agentclientprotocol/codex-acp","version":"1.11.0","integrity":"' + acp_runtime.INTEGRITY + '","platform":"linux-amd64"}')
    lines = []
    for path in sorted((root / "node_modules").rglob("*")):
        if path.is_file() and ".bin" not in path.relative_to(root / "node_modules").parts:
            lines.append(acp_runtime.hashlib.sha256(path.read_bytes()).hexdigest() + "  " + str(path.relative_to(root)))
    (root / "installed.sha256").write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(acp_runtime, "_verify_tarball", lambda _root: None)
    monkeypatch.setattr(acp_runtime, "_host_platform", lambda: ("linux-amd64", "x64"))
    return root


def test_inspection_binds_absolute_command_and_detects_tree_tamper(tmp_path, monkeypatch):
    root = runtime_tree(tmp_path, monkeypatch)
    runtime = acp_runtime.inspect_runtime(root)
    assert runtime.evidence.command == (str(root / "node_modules" / ".bin" / "codex-acp"),)
    (root / "node_modules" / "@openai" / "codex" / "package.json").write_text("tampered")
    with pytest.raises(acp_runtime.AcpRuntimeNotReady, match="modified"):
        acp_runtime.inspect_runtime(root)


def test_inspection_accepts_actual_arm64_platform_artifact(tmp_path, monkeypatch):
    root = runtime_tree(tmp_path, monkeypatch, arch="arm64")
    monkeypatch.setattr(acp_runtime, "_host_platform", lambda: ("linux-arm64", "arm64"))
    (root / "receipt.json").write_text('{"package":"@agentclientprotocol/codex-acp","version":"1.11.0","integrity":"' + acp_runtime.INTEGRITY + '","platform":"linux-arm64"}')
    assert acp_runtime.inspect_runtime(root).platform == "linux-arm64"


def test_inspection_rejects_legacy_nonpublished_platform_binary_layout(tmp_path, monkeypatch):
    root = runtime_tree(tmp_path, monkeypatch)
    binary = acp_runtime._platform_binary(
        root / "node_modules" / "@openai" / "codex-linux-x64", "x64")
    binary.unlink()
    legacy = root / "node_modules" / "@openai" / "codex-linux-x64" / "bin" / "codex"
    legacy.parent.mkdir()
    legacy.write_text("#!/bin/sh\n")
    legacy.chmod(0o700)
    # Regenerate the tree receipt so this specifically proves layout checking.
    lines = []
    for path in sorted((root / "node_modules").rglob("*")):
        if path.is_file() and ".bin" not in path.relative_to(root / "node_modules").parts:
            lines.append(acp_runtime.hashlib.sha256(path.read_bytes()).hexdigest()
                         + "  " + str(path.relative_to(root)))
    (root / "installed.sha256").write_text("\n".join(lines) + "\n")
    with pytest.raises(acp_runtime.AcpRuntimeNotReady, match="platform binary"):
        acp_runtime.inspect_runtime(root)


def test_owner_installer_rejects_node_older_than_pinned_requirement(tmp_path, monkeypatch):
    source = tmp_path / "source" / "package"
    (source / "dist").mkdir(parents=True)
    (source / "package.json").write_text('{"version":"1.11.0"}')
    (source / "dist" / "index.js").write_text("#!/usr/bin/env node\n")
    tarball = tmp_path / "codex-acp.tgz"
    with tarfile.open(tarball, "w:gz") as archive:
        archive.add(source, arcname="package")
    tools = tmp_path / "tools"
    tools.mkdir()
    _stub_executable(tools / "openssl", "printf x")
    _stub_executable(tools / "base64", "cat >/dev/null\nprintf '%s' 'opPKsRaekgdmQpOpHrR0EEDn9chgtiN+b+h0V78fTuQP84TNzB7vrn3EtKODwbiJQTBHJAlynjSFQazFfaT+VQ=='")
    _stub_executable(tools / "node", "printf '%s' 15.0.0")
    _stub_executable(tools / "npm", "exit 99")
    runtime = tmp_path / "runtime"
    script = Path(__file__).parents[1] / "scripts" / "codex_acp_runtime.sh"
    environment = os.environ | {"PATH": str(tools) + os.pathsep + os.environ["PATH"]}
    result = subprocess.run([str(script), "install", str(runtime), str(tarball)],
                            env=environment, text=True, capture_output=True, check=False)
    assert result.returncode != 0
    assert "requires Node >=16" in result.stderr
    assert not runtime.exists()


def test_inspection_rejects_unpinned_profile(tmp_path, monkeypatch):
    with pytest.raises(acp_runtime.AcpRuntimeNotReady, match="profile"):
        acp_runtime.inspect_runtime(runtime_tree(tmp_path, monkeypatch), profile="arbitrary")


def test_inspection_rejects_rewritten_installed_lock(tmp_path, monkeypatch):
    root = runtime_tree(tmp_path, monkeypatch)
    (root / "package-lock.json").write_text("{}")
    with pytest.raises(acp_runtime.AcpRuntimeNotReady, match="reviewed exact lock"):
        acp_runtime.inspect_runtime(root)


def test_private_home_is_explicit_and_cleanup_is_scoped(tmp_path):
    source = tmp_path / "auth.json"
    source.write_text('{"tokens": {"access_token": "fixture", "refresh_token": "fixture"}}')
    source.chmod(0o600)
    parent = tmp_path / "private"
    home = acp_runtime.prepare_private_home(source, parent=parent)
    assert (home / "auth.json").read_text() == source.read_text()
    acp_runtime.cleanup_private_home(home, expected_parent=parent)
    assert not home.exists()
    with pytest.raises(acp_runtime.AcpRuntimeNotReady):
        acp_runtime.cleanup_private_home(tmp_path, expected_parent=parent)


def test_redirection_is_rejected():
    with pytest.raises(acp_runtime.AcpRuntimeNotReady, match="CODEX_PATH"):
        acp_runtime.reject_inherited_redirection({"CODEX_PATH": "/elsewhere"})


def test_explicit_smoke_rejects_inherited_redirection_before_runtime_or_auth(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", "/redirected")
    with pytest.raises(acp_runtime.AcpRuntimeNotReady, match="CODEX_HOME"):
        acp_runtime.run_verified_ordinary_turn(
            runtime_root=tmp_path / "missing", credential_file=tmp_path / "auth.json",
            private_parent=tmp_path / "homes", worktree=tmp_path, model="m", prompt="p",
            log_path=tmp_path / "log")


def test_credential_readiness_rejects_unrecognised_json(tmp_path: Path) -> None:
    source = tmp_path / "auth.json"
    source.write_text('{"garbage": true}')
    source.chmod(0o600)
    with pytest.raises(acp_runtime.AcpRuntimeNotReady, match="unsupported"):
        acp_runtime.credential_readiness(source)
