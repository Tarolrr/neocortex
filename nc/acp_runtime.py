"""Owner-operated verification for the separately pinned Codex ACP runtime.

This module deliberately has no scheduler/adapter registration.  It inspects a
runtime prepared by ``scripts/codex_acp_runtime.sh`` and returns the only launch
evidence accepted by :mod:`nc.acp_client`.  It never searches PATH, invokes npm,
or reads a user's normal Codex home.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import shutil
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .acp_client import CodexAcpLaunchEvidence

PACKAGE = "@agentclientprotocol/codex-acp"
VERSION = "1.11.0"
INTEGRITY = "sha512-opPKsRaekgdmQpOpHrR0EEDn9chgtiN+b+h0V78fTuQP84TNzB7vrn3EtKODwbiJQTBHJAlynjSFQazFfaT+VQ=="
CODEX_VERSION = "0.153.4"
SDK_VERSION = "1.4.0"
PROFILE = "agent"
_PROTECTED = frozenset(("CODEX_PATH", "CODEX_CONFIG", "CODEX_HOME", "HOME",
                        "XDG_CONFIG_HOME", "XDG_DATA_HOME", "INITIAL_AGENT_MODE"))
_INHERITED_REDIRECTION = frozenset(("CODEX_PATH", "CODEX_CONFIG", "CODEX_HOME",
                                    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "INITIAL_AGENT_MODE"))


class AcpRuntimeNotReady(RuntimeError):
    """The local pinned artifact/profile/auth boundary cannot be safely used."""


@dataclass(frozen=True)
class CodexAcpRuntime:
    root: Path
    command: Path
    platform: str
    evidence: CodexAcpLaunchEvidence


def _json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise AcpRuntimeNotReady(f"missing or invalid runtime file: {path.name}") from exc
    if not isinstance(value, dict):
        raise AcpRuntimeNotReady(f"invalid runtime object: {path.name}")
    return value


def _package_version(path: Path, expected: str, label: str) -> None:
    value = _json(path / "package.json")
    if value.get("version") != expected:
        raise AcpRuntimeNotReady(f"{label} version is not pinned at {expected}")


def _platform_package_version(path: Path, arch: str) -> None:
    """The published platform packages deliberately carry a suffixed version."""
    _package_version(path, f"{CODEX_VERSION}-linux-{arch}", f"Codex linux-{arch} binary")


def _host_platform() -> tuple[str, str]:
    if platform.system() != "Linux":
        raise AcpRuntimeNotReady("unsupported ACP runtime platform: Linux amd64 or arm64 required")
    machine = platform.machine().lower()
    aliases = {"x86_64": ("linux-amd64", "x64"), "amd64": ("linux-amd64", "x64"),
               "aarch64": ("linux-arm64", "arm64"), "arm64": ("linux-arm64", "arm64")}
    if machine not in aliases:
        raise AcpRuntimeNotReady("unsupported ACP runtime platform: Linux amd64 or arm64 required")
    return aliases[machine]


def _verify_tarball(root: Path) -> None:
    artifact = root / "codex-acp-1.11.0.tgz"
    if not artifact.is_file():
        raise AcpRuntimeNotReady("pinned ACP tarball is missing")
    digest = base64.b64encode(hashlib.sha512(artifact.read_bytes()).digest()).decode()
    if f"sha512-{digest}" != INTEGRITY:
        raise AcpRuntimeNotReady("pinned ACP tarball integrity mismatch")


def _verify_installed_tree(root: Path) -> None:
    """Verify the receipt created after resolution, not merely package versions."""
    receipt = root / "installed.sha256"
    try:
        lines = receipt.read_text().splitlines()
    except OSError as exc:
        raise AcpRuntimeNotReady("installed dependency hash receipt is missing") from exc
    if not lines:
        raise AcpRuntimeNotReady("installed dependency hash receipt is empty")
    expected: set[Path] = set()
    for line in lines:
        try:
            digest, relative = line.split("  ", 1)
        except ValueError as exc:
            raise AcpRuntimeNotReady("installed dependency hash receipt is malformed") from exc
        path = (root / relative).resolve()
        if root not in path.parents or not path.is_file() or len(digest) != 64:
            raise AcpRuntimeNotReady("installed dependency hash receipt is unsafe")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise AcpRuntimeNotReady("installed dependency tree was modified")
        expected.add(path)
    # npm's .bin entries are symlinks.  They are deliberately covered by the
    # separate launcher receipt, while this receipt covers installed files.
    actual = {
        path.resolve() for path in (root / "node_modules").rglob("*")
        if path.is_file() and ".bin" not in path.relative_to(root / "node_modules").parts
    }
    if expected != actual:
        raise AcpRuntimeNotReady("installed dependency tree has unexpected files")


def _verify_launcher(root: Path, command: Path) -> None:
    try:
        digest, relative = (root / "launcher.sha256").read_text().strip().split("  ", 1)
    except (OSError, ValueError) as exc:
        raise AcpRuntimeNotReady("ACP launcher hash receipt is missing or malformed") from exc
    if (root / relative).resolve() != command.resolve() or len(digest) != 64:
        raise AcpRuntimeNotReady("ACP launcher hash receipt is unsafe")
    if hashlib.sha256(command.read_bytes()).hexdigest() != digest:
        raise AcpRuntimeNotReady("verified ACP launcher was modified")


def inspect_runtime(root: Path, *, profile: str = "agent") -> CodexAcpRuntime:
    """Inspect an installed runtime, refusing PATH and lockfile assertions.

    The receipt is useful provenance, but the installed package tree and the
    tarball are independently checked here at every prospective launch.
    """
    root = root.resolve()
    if profile != PROFILE:
        raise AcpRuntimeNotReady("unsupported ACP profile; only the pinned agent profile is available")
    host, node_arch = _host_platform()
    receipt = _json(root / "receipt.json")
    if receipt.get("package") != PACKAGE or receipt.get("version") != VERSION:
        raise AcpRuntimeNotReady("runtime receipt does not identify codex-acp 1.11.0")
    if receipt.get("integrity") != INTEGRITY or receipt.get("platform") != host:
        raise AcpRuntimeNotReady("runtime receipt integrity or platform mismatch")
    _verify_tarball(root)
    _verify_installed_tree(root)
    modules = root / "node_modules"
    acp = modules / "@agentclientprotocol" / "codex-acp"
    _package_version(acp, VERSION, "codex-acp")
    _package_version(modules / "@openai" / "codex", CODEX_VERSION, "Codex")
    _package_version(modules / "@agentclientprotocol" / "sdk", SDK_VERSION, "ACP SDK")
    # Codex has a platform optional package; its absence is not portable and
    # must not defer a failure to a later production launch.
    binary_package = modules / "@openai" / f"codex-linux-{node_arch}"
    _platform_package_version(binary_package, node_arch)
    # npm creates this shim for a package bin.  It is intentionally an
    # absolute, installed artifact path rather than a PATH lookup.
    command = modules / ".bin" / "codex-acp"
    if not command.is_file() or not os.access(command, os.X_OK):
        raise AcpRuntimeNotReady("verified absolute codex-acp launcher is missing or not executable")
    resolved = command.resolve()
    if root not in resolved.parents:
        raise AcpRuntimeNotReady("codex-acp launcher resolves outside isolated runtime")
    _verify_launcher(root, command)
    evidence = CodexAcpLaunchEvidence(
        command=(str(command),), package=PACKAGE, package_version=VERSION,
        artifact_integrity=INTEGRITY, codex_version=CODEX_VERSION, sdk_version=SDK_VERSION,
        profile=profile,
    )
    return CodexAcpRuntime(root, command, host, evidence)


def run_verified_ordinary_turn(*, runtime_root: Path, credential_file: Path,
                               private_parent: Path, worktree: Path, model: str,
                               prompt: str, log_path: Path, timeout_s: float = 60):
    """The sole explicit ordinary-role opt-in; never resolves an adapter label.

    The generated HOME holds both the copied auth file and enforced config.
    It is removed whether setup, launch, or ACP cleanup fails.
    """
    from .acp_client import CodexAcpPolicy, run_codex_acp_turn

    reject_inherited_redirection()
    runtime = inspect_runtime(runtime_root, profile=PROFILE)
    home = prepare_private_home(credential_file, parent=private_parent)
    try:
        return run_codex_acp_turn(
            runtime.evidence.command, launch=runtime.evidence,
            policy=CodexAcpPolicy.ordinary(worktree), model=model, prompt=prompt,
            log_path=log_path, timeout_s=timeout_s, private_home=home,
        )
    finally:
        cleanup_private_home(home, expected_parent=private_parent)


def reject_inherited_redirection(environment: Mapping[str, str] | None = None) -> None:
    """Require a clean parent environment before creating an auth boundary."""
    environment = os.environ if environment is None else environment
    # HOME is normally set for every interactive owner and is deliberately
    # replaced by the private home later; it is not itself a redirection.
    names = sorted(key for key in _INHERITED_REDIRECTION if environment.get(key))
    if names:
        raise AcpRuntimeNotReady("inherited Codex/config redirection is forbidden: " + ", ".join(names))


def prepare_private_home(credential_file: Path, *, parent: Path) -> Path:
    """Make a short-lived private HOME by read-only, noninteractive reuse.

    The owner supplies the *existing* Codex ``auth.json`` explicitly.  No login
    is run and the source is never modified.  Values are intentionally neither
    returned nor written to receipts/logs.
    """
    credential_file = credential_file.resolve()
    try:
        mode = credential_file.stat().st_mode
        value = json.loads(credential_file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise AcpRuntimeNotReady("credential readiness error: explicit auth.json is unreadable") from exc
    if (not stat.S_ISREG(mode) or mode & 0o077 or not isinstance(value, dict) or not value):
        raise AcpRuntimeNotReady("credential readiness error: explicit auth.json is unsupported")
    parent.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="nc-acp-home-", dir=parent))
    try:
        codex = home / ".codex"
        codex.mkdir(mode=0o700)
        target = codex / "auth.json"
        shutil.copyfile(credential_file, target)
        target.chmod(0o600)
        return home
    except BaseException:
        shutil.rmtree(home, ignore_errors=True)
        raise


def credential_readiness(credential_file: Path) -> str:
    """Check the explicit reuse source without copying or exposing it."""
    try:
        mode = credential_file.resolve().stat().st_mode
        value = json.loads(credential_file.resolve().read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise AcpRuntimeNotReady("credential readiness error: explicit auth.json is unreadable") from exc
    if (not stat.S_ISREG(mode) or mode & 0o077 or not isinstance(value, dict) or not value):
        raise AcpRuntimeNotReady("credential readiness error: explicit auth.json is unsupported")
    return "explicit auth.json readable (values not inspected or logged)"


def cleanup_private_home(home: Path, *, expected_parent: Path) -> None:
    """Remove only a generated private home; refuse broad/unrelated paths."""
    home, expected_parent = home.resolve(), expected_parent.resolve()
    if home.parent != expected_parent or not home.name.startswith("nc-acp-home-"):
        raise AcpRuntimeNotReady("refusing unsafe private-home cleanup target")
    shutil.rmtree(home)
