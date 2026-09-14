"""Capability-aware configured-adapter readiness declarations."""

from __future__ import annotations

from pathlib import Path

from .acp_runtime import (
    PROFILE,
    AcpRuntimeNotReady,
    credential_readiness,
    inspect_runtime,
    reject_inherited_redirection,
)
from .arbiter import AdapterRequirement
from .config import Config

CODEX_ACP_ADAPTER = "codex-acp"


def configured_requirements(cfg: Config) -> list[AdapterRequirement]:
    """Return host capabilities for configured adapters, once per label.

    ACP remains an explicit opt-in transport, not an ``Adapter`` registration.
    Its label selects an offline pinned-runtime check; it is never looked up
    as an executable on the service PATH.
    """
    labels = {cfg.adapter, *cfg.adapters.values()}
    requirements = []
    for label in sorted(labels):
        if label == CODEX_ACP_ADAPTER:
            requirements.append(AdapterRequirement(label, readiness=lambda: _acp_ready(cfg)))
        else:
            requirements.append(AdapterRequirement(label, executable=label))
    return requirements


def _acp_ready(cfg: Config) -> tuple[list[str], list[str]]:
    if not cfg.acp_runtime:
        return [], ["codex-acp readiness: configure acp_runtime as an absolute isolated runtime path"]
    if not cfg.acp_auth:
        return [], ["codex-acp readiness: configure acp_auth as an explicit auth.json path"]
    runtime_path, auth_path = Path(cfg.acp_runtime), Path(cfg.acp_auth)
    if not runtime_path.is_absolute() or not auth_path.is_absolute():
        return [], ["codex-acp readiness: acp_runtime and acp_auth must be absolute paths"]
    try:
        reject_inherited_redirection()
        runtime = inspect_runtime(runtime_path, profile=PROFILE)
        auth = credential_readiness(auth_path)
    except AcpRuntimeNotReady as exc:
        return [], [f"codex-acp readiness: {exc}"]
    return [
        f"codex-acp artifact/profile: ready ({runtime.platform}; {runtime.command})",
        f"codex-acp auth: {auth}",
    ], []
