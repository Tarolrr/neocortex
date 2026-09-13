"""Offline runtime-inspection coverage; no npm, real credentials, or ACP binary."""

from pathlib import Path

import pytest

from nc import acp_runtime


def runtime_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, arch: str = "x64") -> Path:
    root = tmp_path / "runtime"
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


def test_inspection_rejects_unpinned_profile(tmp_path, monkeypatch):
    with pytest.raises(acp_runtime.AcpRuntimeNotReady, match="profile"):
        acp_runtime.inspect_runtime(runtime_tree(tmp_path, monkeypatch), profile="arbitrary")


def test_private_home_is_explicit_and_cleanup_is_scoped(tmp_path):
    source = tmp_path / "auth.json"
    source.write_text('{"tokens": "fixture-only"}')
    source.chmod(0o600)
    parent = tmp_path / "private"
    home = acp_runtime.prepare_private_home(source, parent=parent)
    assert (home / ".codex" / "auth.json").read_text() == source.read_text()
    acp_runtime.cleanup_private_home(home, expected_parent=parent)
    assert not home.exists()
    with pytest.raises(acp_runtime.AcpRuntimeNotReady):
        acp_runtime.cleanup_private_home(tmp_path, expected_parent=parent)


def test_redirection_is_rejected():
    with pytest.raises(acp_runtime.AcpRuntimeNotReady, match="CODEX_PATH"):
        acp_runtime.reject_inherited_redirection({"CODEX_PATH": "/elsewhere"})
