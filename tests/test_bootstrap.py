"""The host bootstrap is tested only through temporary prefixes and command stubs."""

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "bootstrap.sh"


def stub(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text("#!/bin/sh\nset -eu\n" + body + "\n")
    path.chmod(0o755)
    return path


def environment(tmp_path: Path) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls"
    for command in ("apt-get", "systemctl"):
        stub(bin_dir, command, f'echo "{command} $*" >> "{log}"')
    stub(bin_dir, "dpkg", "echo amd64")
    stub(bin_dir, "dpkg-query", "echo installed")
    stub(bin_dir, "npm", "[ \"$1\" = list ] || exit 1")
    for command in ("codex", "claude"):
        stub(bin_dir, command, "echo version")
    os_release = tmp_path / "os-release"
    os_release.write_text("ID=debian\nVERSION_CODENAME=trixie\n")
    env = os.environ | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "BOOTSTRAP_SKIP_ROOT_CHECK": "1",
        "BOOTSTRAP_PREFIX": str(tmp_path / "runner"),
        "NC_HOME": str(tmp_path / "home"),
        "SYSTEMD_DIR": str(tmp_path / "units"),
        "OS_RELEASE": str(os_release),
    }
    return env, log


def prepared_runner(tmp_path: Path, env: dict[str, str]) -> None:
    runner = Path(env["BOOTSTRAP_PREFIX"])
    subprocess.run(["git", "clone", str(ROOT), str(runner)], check=True, capture_output=True)
    venv_bin = runner / ".venv/bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(shutil.which("python3.13") or shutil.which("python3"))
    # The symlinked interpreter sees the test environment's pinned tools.
    stub(venv_bin, "nc", "mkdir -p \"$NC_HOME\"; [ \"${1:-}\" != init ] || echo '{}' > \"$NC_HOME/config.json\"")


def test_bootstrap_rerun_preserves_config_and_does_not_reinstall(tmp_path):
    env, log = environment(tmp_path)
    prepared_runner(tmp_path, env)
    home = Path(env["NC_HOME"])
    home.mkdir()
    config = home / "config.json"
    config.write_text('{"adapter": "preserve-me"}')

    first = subprocess.run([str(SCRIPT)], env=env, text=True, capture_output=True, check=False)
    first_calls = log.read_text()
    second = subprocess.run([str(SCRIPT)], env=env, text=True, capture_output=True, check=False)

    assert first.returncode == second.returncode == 0
    assert config.read_text() == '{"adapter": "preserve-me"}'
    assert first_calls == "systemctl daemon-reload\n"
    assert log.read_text() == first_calls  # rerun made no package/vendor/unit calls


def test_bootstrap_rejects_unsupported_host_before_commands(tmp_path):
    env, log = environment(tmp_path)
    unsupported = Path(env["OS_RELEASE"])
    unsupported.write_text("ID=ubuntu\nVERSION_CODENAME=noble\n")

    result = subprocess.run([str(SCRIPT)], env=env, text=True, capture_output=True, check=False)

    assert result.returncode == 1
    assert "unsupported OS ID ubuntu" in result.stderr
    assert not log.exists()


def test_enable_timer_respects_stop(tmp_path):
    env, log = environment(tmp_path)
    prepared_runner(tmp_path, env)
    home = Path(env["NC_HOME"])
    home.mkdir()
    (home / "config.json").write_text("{}")
    (home / "STOP").write_text("owner stop")

    result = subprocess.run([str(SCRIPT), "--enable-timer"], env=env, text=True, capture_output=True,
                            check=False)

    assert result.returncode == 1
    assert "STOP exists" in result.stderr
    assert "systemctl enable" not in (log.read_text() if log.exists() else "")
