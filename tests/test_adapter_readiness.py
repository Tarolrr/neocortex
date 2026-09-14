"""Configured transport readiness remains separate from CLI PATH checks."""

from types import SimpleNamespace

from nc import adapter_readiness
from nc.config import Config


def test_codex_acp_requirement_is_not_an_executable_and_checks_runtime_auth(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    auth = tmp_path / "auth.json"
    cfg = Config(home=tmp_path, adapter="codex-acp", acp_runtime=str(runtime), acp_auth=str(auth))
    calls = []

    def inspect(path, *, profile):
        calls.append(("runtime", path, profile))
        return SimpleNamespace(platform="linux-amd64", command="/isolated/node /isolated/index.js")

    monkeypatch.setattr(adapter_readiness, "reject_inherited_redirection", lambda: calls.append(("env",)))
    monkeypatch.setattr(adapter_readiness, "inspect_runtime", inspect)
    monkeypatch.setattr(adapter_readiness, "credential_readiness",
                        lambda path: calls.append(("auth", path)) or "explicit auth.json readable")

    requirement = adapter_readiness.configured_requirements(cfg)[0]
    reports, errors = requirement.readiness()

    assert requirement.executable is None
    assert not errors
    assert "codex-acp artifact/profile: ready" in reports[0]
    assert calls == [("env",), ("runtime", runtime, "agent"), ("auth", auth)]


def test_codex_acp_requirement_fails_closed_without_owner_paths(tmp_path):
    cfg = Config(home=tmp_path, adapter="codex-acp")
    requirement = adapter_readiness.configured_requirements(cfg)[0]
    _reports, errors = requirement.readiness()
    assert errors == ["codex-acp readiness: configure acp_runtime as an absolute isolated runtime path"]
