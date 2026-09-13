"""Offline runtime-inspection coverage; no npm, real credentials, or ACP binary."""

import base64
import hashlib
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
    node = tools / "node"
    node.write_text("""#!/bin/sh
case "$*" in
  *--version*) printf '%s\\n' v20.19.0;;
  *process.versions.node*) printf '%s' "${ACP_TEST_NODE_VERSION:-20.19.0}";;
  *node_modules/open*) printf 20;;
  *codex-acp*) printf 1.11.0;; *'@openai/codex/package.json'*) printf 0.153.4;;
  *sdk/package.json*) printf 1.4.0;; *codex-linux-*) printf '0.153.4-linux-%s' "$ACP_TEST_ARCH";; *) exit 0;;
esac
""")
    node.chmod(0o700)
    inspected_call = tmp_path / "inspect-call"
    _stub_executable(tools / "python3", """
case "$*" in
  *inspect_runtime*) printf '%s' "$*" > "$ACP_INSPECT_CALL";;
  *) exit 97;;
esac
""")
    runtime = tmp_path / "runtime"
    script = Path(__file__).parents[1] / "scripts" / "codex_acp_runtime.sh"
    environment = os.environ | {"PATH": str(tools) + os.pathsep + os.environ["PATH"],
                                "ACP_TEST_ARCH": node_arch,
                                "ACP_INSPECT_CALL": str(inspected_call)}
    subprocess.run([str(script), "install", str(runtime), str(tarball)], check=True, env=environment)
    assert "inspect_runtime" in inspected_call.read_text()
    launcher = runtime / "node_modules" / ".bin" / "codex-acp"
    assert launcher.is_symlink()
    assert launcher.resolve() == runtime / "node_modules" / "@agentclientprotocol" / "codex-acp" / "dist" / "index.js"
    assert (launcher.resolve()).is_file()
    monkeypatch.setattr(acp_runtime, "_verify_tarball", lambda _root: None)
    monkeypatch.setattr(acp_runtime, "_verify_sri_derived_contents", lambda *_args: None)
    inspected = acp_runtime.inspect_runtime(runtime)
    assert inspected.evidence.command == (str(node.resolve()), str(launcher.resolve()))


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
    entrypoint = root / "node_modules" / "@agentclientprotocol" / "codex-acp" / "dist" / "index.js"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text("console.log('fixture')\n")
    command = root / "node_modules" / ".bin" / "codex-acp"
    command.parent.mkdir(parents=True)
    command.symlink_to("../@agentclientprotocol/codex-acp/dist/index.js")
    binary = acp_runtime._platform_binary(
        root / "node_modules" / "@openai" / f"codex-linux-{arch}", arch)
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o700)
    (root / "launcher.sha256").write_text(
        acp_runtime.hashlib.sha256(command.read_bytes()).hexdigest() + "  node_modules/.bin/codex-acp\n")
    node = tmp_path / "absolute-node"
    # Behave like an absolute Node interpreter for both its version probe and
    # the later JS-entrypoint launch.  The test is about PATH independence,
    # not a shell ``test`` command's false-status propagation.
    node.write_text("#!/bin/sh\n[ \"$1\" = --version ] && printf '%s\\n' v20.19.0\nexit 0\n")
    node.chmod(0o700)
    (root / "node.json").write_text(
        '{"path":"' + str(node) + '","version":"20.19.0","sha256":"'
        + hashlib.sha256(node.read_bytes()).hexdigest() + '","minimum_major":20}')
    (root / "receipt.json").write_text('{"package":"@agentclientprotocol/codex-acp","version":"1.11.0","integrity":"' + acp_runtime.INTEGRITY + '","platform":"linux-amd64"}')
    lines = []
    for path in sorted((root / "node_modules").rglob("*")):
        if path.is_file() and ".bin" not in path.relative_to(root / "node_modules").parts:
            lines.append(acp_runtime.hashlib.sha256(path.read_bytes()).hexdigest() + "  " + str(path.relative_to(root)))
    (root / "installed.sha256").write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(acp_runtime, "_verify_tarball", lambda _root: None)
    monkeypatch.setattr(acp_runtime, "_verify_sri_derived_contents", lambda *_args: None)
    monkeypatch.setattr(acp_runtime, "_host_platform", lambda: ("linux-amd64", "x64"))
    return root


def test_inspection_binds_absolute_command_and_detects_tree_tamper(tmp_path, monkeypatch):
    root = runtime_tree(tmp_path, monkeypatch)
    runtime = acp_runtime.inspect_runtime(root)
    assert runtime.evidence.command == (
        str(tmp_path / "absolute-node"),
        str(root / "node_modules" / "@agentclientprotocol" / "codex-acp" / "dist" / "index.js"),
    )
    (root / "node_modules" / "@openai" / "codex" / "package.json").write_text("tampered")
    with pytest.raises(acp_runtime.AcpRuntimeNotReady, match="modified"):
        acp_runtime.inspect_runtime(root)


def test_public_inspected_launch_command_ignores_service_path(tmp_path, monkeypatch):
    runtime = acp_runtime.inspect_runtime(runtime_tree(tmp_path, monkeypatch))
    result = subprocess.run(runtime.evidence.command, env={"PATH": "/nonexistent"},
                            text=True, capture_output=True, check=False)
    assert result.returncode == 0


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


def test_owner_installer_rejects_node_18_for_locked_transitive_open_engine(tmp_path, monkeypatch):
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
    # The root Codex manifest says >=16; this fixture proves the installer
    # also enforces the reviewed non-dev open@11.0.1 engines.node >=20 floor.
    _stub_executable(tools / "node", """
case "$*" in
  *process.versions.node*) printf '%s' 18.19.0;;
  *node_modules/open*) printf '%s' 20;;
  *) exit 0;;
esac
""")
    _stub_executable(tools / "npm", "exit 99")
    runtime = tmp_path / "runtime"
    script = Path(__file__).parents[1] / "scripts" / "codex_acp_runtime.sh"
    environment = os.environ | {"PATH": str(tools) + os.pathsep + os.environ["PATH"]}
    result = subprocess.run([str(script), "install", str(runtime), str(tarball)],
                            env=environment, text=True, capture_output=True, check=False)
    assert result.returncode != 0
    assert "requires Node >=20" in result.stderr
    assert not runtime.exists()


def test_owner_installer_rejects_modified_reviewed_lock_before_npm(tmp_path: Path) -> None:
    """An edited repository lock must never become npm's install input."""
    source_script = Path(__file__).parents[1] / "scripts" / "codex_acp_runtime.sh"
    source_lock = Path(__file__).parents[1] / "scripts" / "codex_acp_runtime.lock.json"
    staged = tmp_path / "scripts"
    staged.mkdir()
    script = staged / source_script.name
    script.write_text(source_script.read_text())
    script.chmod(0o700)
    (staged / source_lock.name).write_bytes(source_lock.read_bytes() + b"\n")
    tools = tmp_path / "tools"
    tools.mkdir()
    npm_called = tmp_path / "npm-called"
    _stub_executable(tools / "npm", 'touch "$ACP_NPM_CALLED"')
    runtime = tmp_path / "runtime"
    tarball = tmp_path / "unused.tgz"
    tarball.touch()
    result = subprocess.run(
        [str(script), "install", str(runtime), str(tarball)],
        env=os.environ | {"PATH": str(tools) + os.pathsep + os.environ["PATH"],
                          "ACP_NPM_CALLED": str(npm_called)},
        text=True, capture_output=True, check=False,
    )
    assert result.returncode != 0
    assert "dependency lock was modified" in result.stderr
    assert not npm_called.exists()
    assert not runtime.exists()


def test_owner_rollback_refuses_a_crafted_receipt_directory(tmp_path: Path) -> None:
    runtime = tmp_path / "not-a-runtime"
    runtime.mkdir()
    (runtime / "receipt.json").write_text("{}")
    script = Path(__file__).parents[1] / "scripts" / "codex_acp_runtime.sh"
    result = subprocess.run([str(script), "rollback", str(runtime)], text=True,
                            capture_output=True, check=False)
    assert result.returncode != 0
    assert runtime.exists()
    assert "not an owner-established" in result.stderr


def test_owner_rollback_refuses_copied_reviewed_lock(tmp_path: Path) -> None:
    runtime = tmp_path / "unrelated"
    runtime.mkdir()
    (runtime / "package-lock.json").write_bytes(acp_runtime._REVIEWED_LOCK.read_bytes())
    script = Path(__file__).parents[1] / "scripts" / "codex_acp_runtime.sh"
    result = subprocess.run([str(script), "rollback", str(runtime)], text=True,
                            capture_output=True, check=False)
    assert result.returncode != 0
    assert runtime.exists()
    assert "not an owner-established" in result.stderr


@pytest.mark.parametrize("damage", ["tampered-dependency", "partial-install"])
def test_owner_rollback_removes_damaged_or_partial_pinned_layout(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str) -> None:
    """Recovery authenticates immutable inputs, not launch readiness."""
    if damage == "tampered-dependency":
        runtime = runtime_tree(tmp_path, monkeypatch)
        (runtime / "node_modules" / "@openai" / "codex" / "package.json").write_text("tampered")
    else:
        # An interrupted install can stop after its immutable lock is copied.
        runtime = tmp_path / "partial-runtime"
        runtime.mkdir()
        (runtime / "package-lock.json").write_bytes(acp_runtime._REVIEWED_LOCK.read_bytes())
    runtime.chmod(0o700)
    (runtime / ".nc-acp-owner-install.json").write_text(
        f"runtime={runtime.resolve()}\nuid={os.getuid()}\nformat=1\n")
    (runtime / ".nc-acp-owner-install.json").chmod(0o600)
    script = Path(__file__).parents[1] / "scripts" / "codex_acp_runtime.sh"
    result = subprocess.run([str(script), "rollback", str(runtime)], text=True,
                            capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert not runtime.exists()


def test_inspection_rejects_unpinned_profile(tmp_path, monkeypatch):
    with pytest.raises(acp_runtime.AcpRuntimeNotReady, match="profile"):
        acp_runtime.inspect_runtime(runtime_tree(tmp_path, monkeypatch), profile="arbitrary")


def test_inspection_rejects_rewritten_installed_lock(tmp_path, monkeypatch):
    root = runtime_tree(tmp_path, monkeypatch)
    (root / "package-lock.json").write_text("{}")
    with pytest.raises(acp_runtime.AcpRuntimeNotReady, match="reviewed exact lock"):
        acp_runtime.inspect_runtime(root)


def test_sri_tree_check_rejects_extra_empty_package_directory(tmp_path, monkeypatch):
    """A rewritten local hash receipt cannot authorize an extra empty path."""
    root = tmp_path / "runtime"
    package = root / "node_modules" / "known"
    package.mkdir(parents=True)
    (package / "package.json").write_text('{"bin": {}}')
    artifact = tmp_path / "known.tgz"
    monkeypatch.setattr(acp_runtime, "_tar_contents", lambda _artifact: {"package.json": "x"})
    acp_runtime._verify_complete_node_modules_tree(root, {"node_modules/known": artifact})
    (root / "node_modules" / "unreviewed").mkdir()
    with pytest.raises(acp_runtime.AcpRuntimeNotReady, match="unexpected directories"):
        acp_runtime._verify_complete_node_modules_tree(root, {"node_modules/known": artifact})


def _fixture_package_tarball(path: Path, files: dict[str, str]) -> None:
    """Make a tiny npm-style package artifact for offline tree verification."""
    source = path.parent / (path.name + "-source") / "package"
    for relative, contents in files.items():
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)
    with tarfile.open(path, "w:gz") as archive:
        archive.add(source, arcname="package")


def test_sri_tree_check_accepts_scoped_root_npm_bin_link_end_to_end(tmp_path: Path) -> None:
    """Exercise real tar/SRI derivation for npm's scoped root .bin layout."""
    root = tmp_path / "runtime"
    acp_dir = root / "node_modules" / "@agentclientprotocol" / "codex-acp"
    dependency = root / "node_modules" / "dependency"
    root_artifact = root / "codex-acp-1.11.0.tgz"
    _fixture_package_tarball(root_artifact, {
        "package.json": '{"bin": {"codex-acp": "dist/index.js"}}',
        "dist/index.js": "#!/usr/bin/env node\n",
    })
    dependency_artifact = tmp_path / "dependency.tgz"
    _fixture_package_tarball(dependency_artifact, {"package.json": '{"version": "1.0.0"}'})
    for artifact, directory in ((root_artifact, acp_dir), (dependency_artifact, dependency)):
        with tarfile.open(artifact, "r:gz") as archive:
            archive.extractall(directory.parent, filter="data")
        (directory.parent / "package").rename(directory)
    launcher = root / "node_modules" / ".bin" / "codex-acp"
    launcher.parent.mkdir()
    launcher.symlink_to("../@agentclientprotocol/codex-acp/dist/index.js")
    digest = hashlib.sha512(dependency_artifact.read_bytes()).digest()
    sri = "sha512-" + base64.b64encode(digest).decode()
    encoded = digest.hex()
    cache = root / "npm-cache" / "_cacache" / "content-v2" / "sha512" / encoded[:2] / encoded[2:4] / encoded[4:]
    cache.parent.mkdir(parents=True)
    cache.write_bytes(dependency_artifact.read_bytes())
    lock = {"packages": {"node_modules/dependency": {"integrity": sri}}}
    acp_runtime._verify_sri_derived_contents(root, lock)


def test_private_parent_rejects_linked_worktree_common_gitdir(tmp_path: Path) -> None:
    """A linked worktree's relative gitdir/commondir protects shared metadata."""
    common = tmp_path / "repository" / ".git"
    gitdir = common / "worktrees" / "linked"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    worktree = tmp_path / "linked"
    worktree.mkdir()
    # This is relative to the .git file, as Git permits for linked worktrees.
    (worktree / ".git").write_text("gitdir: ../repository/.git/worktrees/linked\n")
    with pytest.raises(acp_runtime.AcpRuntimeNotReady, match="overlaps"):
        acp_runtime._require_private_parent_isolated(
            common / "private-homes", worktree, tmp_path / "runtime")


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


def test_private_home_refuses_a_protected_overlap(tmp_path: Path) -> None:
    source = tmp_path / "auth.json"
    source.write_text('{"OPENAI_API_KEY": "fixture"}')
    source.chmod(0o600)
    with pytest.raises(acp_runtime.AcpRuntimeNotReady, match="overlaps"):
        acp_runtime.prepare_private_home(source, parent=tmp_path / "worktree" / "homes",
                                         disallow_within=(tmp_path / "worktree",))


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
