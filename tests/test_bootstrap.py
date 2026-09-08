"""The host bootstrap is tested only through temporary prefixes and command stubs."""

import os
import shutil
import subprocess
import textwrap
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
    stub(bin_dir, "codex", '''
if [ "${1:-}" = login ] && [ "${2:-}" = status ]; then
    [ "${CODEX_READY:-1}" = 1 ]
    exit
fi
echo version
''')
    stub(bin_dir, "claude", '''
if [ "${1:-}" = auth ] && [ "${2:-}" = status ]; then
    [ "${CLAUDE_READY:-1}" = 1 ]
    exit
fi
echo version
''')
    os_release = tmp_path / "os-release"
    os_release.write_text("ID=debian\nVERSION_CODENAME=trixie\n")
    env = os.environ | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        # The production default is the fixed PATH in neocortex.service.  The
        # isolated test substitutes its temporary service bin, ensuring that
        # bootstrap cannot accidentally validate the invoking shell PATH.
        "SERVICE_PATH": f"{tmp_path / 'runner' / '.venv' / 'bin'}:{bin_dir}",
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
    python = Path(shutil.which("python3.13") or shutil.which("python3")).resolve()
    venv = runner / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "lib/python3.13/site-packages").mkdir(parents=True)
    (venv / "bin/python").symlink_to(python)
    (venv / "pyvenv.cfg").write_text(f"home = {python.parent}\ninclude-system-site-packages = true\n")
    # Reproduce the editable import metadata produced by pip. The test host
    # deliberately has no setuptools, so using pip itself would fetch it.
    # The rerun test must not accept a checkout merely because cwd imports nc.
    site_packages = venv / "lib/python3.13/site-packages"
    (site_packages / "__editable__.neocortex-0.1.0.pth").write_text(f"{runner}\n")
    stub(venv / "bin", "nc", 'exec "$(dirname "$0")/python" -m nc.cli "$@"')
    stub(venv / "bin", "pytest", 'exec "$(dirname "$0")/python" -m pytest "$@"')
    stub(venv / "bin", "ruff", 'exec "$(dirname "$0")/python" -m ruff "$@"')


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


def test_bootstrap_rejects_non_25_armbian_before_commands(tmp_path):
    env, log = environment(tmp_path)
    Path(env["OS_RELEASE"]).write_text(
        "ID=armbian\nVERSION_ID=24.8.1\nVERSION_CODENAME=trixie\n"
    )

    result = subprocess.run([str(SCRIPT)], env=env, text=True, capture_output=True, check=False)

    assert result.returncode == 1
    assert "unsupported Armbian release 24.8.1" in result.stderr
    assert not log.exists()


def test_bootstrap_accepts_armbian_25_os_release_fields(tmp_path):
    env, _ = environment(tmp_path)
    prepared_runner(tmp_path, env)
    Path(env["OS_RELEASE"]).write_text(
        "ID=armbian\nVERSION_ID=25.11.1\nVERSION_CODENAME=trixie\n"
    )

    result = subprocess.run([str(SCRIPT)], env=env, text=True, capture_output=True, check=False)

    assert result.returncode == 0, result.stderr


def test_enable_timer_requires_clis_on_service_path(tmp_path):
    env, log = environment(tmp_path)
    prepared_runner(tmp_path, env)
    home = Path(env["NC_HOME"])
    home.mkdir()
    (home / "config.json").write_text("{}")
    # The invoking shell still has the stubs on PATH, but they are deliberately
    # absent from the simulated service PATH.
    env["SERVICE_PATH"] = str(tmp_path / "empty-service-bin")
    bin_dir = Path(env["PATH"].split(":", 1)[0])
    # Simulate npm successfully retaining its globally installed packages in a
    # non-service prefix; bootstrap must reject that layout before activation.
    stub(bin_dir, "npm", "exit 0")

    result = subprocess.run([str(SCRIPT), "--enable-timer"], env=env,
                            text=True, capture_output=True, check=False)

    assert result.returncode == 1
    assert "codex was not installed onto the service PATH" in result.stderr
    assert "systemctl enable" not in (log.read_text() if log.exists() else "")


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


def test_enable_timer_requires_vendor_readiness(tmp_path):
    env, log = environment(tmp_path)
    prepared_runner(tmp_path, env)
    home = Path(env["NC_HOME"])
    home.mkdir()
    (home / "config.json").write_text("{}")

    for variable, expected in (("CODEX_READY", "codex is not logged in"),
                               ("CLAUDE_READY", "claude is not logged in")):
        failed_env = env | {variable: "0"}
        result = subprocess.run([str(SCRIPT), "--enable-timer"], env=failed_env,
                                text=True, capture_output=True, check=False)
        assert result.returncode == 1
        assert expected in result.stderr
        assert "systemctl enable" not in (log.read_text() if log.exists() else "")


def test_bootstrap_repairs_runner_missing_editable_install(tmp_path):
    env, log = environment(tmp_path)
    runner = Path(env["BOOTSTRAP_PREFIX"])
    subprocess.run(["git", "clone", str(ROOT), str(runner)], check=True, capture_output=True)
    venv_bin = runner / ".venv/bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(shutil.which("python3.13") or shutil.which("python3"))

    # This Python 3.13 environment can import pytest and ruff, but deliberately
    # lacks nc and its service-PATH launchers. The venv stub makes the repair
    # observable without network use.
    bin_dir = Path(env["PATH"].split(":", 1)[0])
    stub(bin_dir, "python3.13", f'''
echo "python3.13 $*" >> "{log}"
if [ "${{1:-}}" = -m ] && [ "${{2:-}}" = venv ]; then
    venv="$3"
    mkdir -p "$venv/bin" "$venv/lib/python3.13/site-packages"
    ln -s /usr/bin/python3.13 "$venv/bin/python"
    printf '%s\\n' 'home = /usr/bin' 'include-system-site-packages = true' > "$venv/pyvenv.cfg"
    printf '%s\\n' '#!/bin/sh' 'echo "pip $*" >> "{log}"' 'if [ "$1" = install ] && [ "$2" = -e ]; then echo "$3" > "$(dirname "$0")/../lib/python3.13/site-packages/__editable__.neocortex-0.1.0.pth"; for tool in nc pytest ruff; do printf "%s\\n" "#!/bin/sh" "exit 0" > "$(dirname "$0")/$tool"; chmod +x "$(dirname "$0")/$tool"; done; printf "%s\\n" "#!/bin/sh" "mkdir -p \\\"\\$NC_HOME\\\"" "if [ \\\"\\${{1:-}}\\\" = init ]; then printf \\\"{{}}\\\" > \\\"\\$NC_HOME/config.json\\\"; fi" > "$(dirname "$0")/nc"; fi' > "$venv/bin/pip"
    chmod +x "$venv/bin/pip"
fi
''')

    result = subprocess.run([str(SCRIPT)], env=env, text=True, capture_output=True, check=False)

    assert result.returncode == 0, result.stderr
    calls = log.read_text()
    assert "python3.13 -m venv" in calls
    assert "pip install -e" in calls
    assert (Path(env["NC_HOME"]) / "config.json").exists()


def test_bootstrap_repairs_runner_missing_console_launcher(tmp_path):
    env, log = environment(tmp_path)
    prepared_runner(tmp_path, env)
    runner = Path(env["BOOTSTRAP_PREFIX"])
    (runner / ".venv/bin/pytest").unlink()
    bin_dir = Path(env["PATH"].split(":", 1)[0])
    stub(bin_dir, "python3.13", f'''
if [ "${{1:-}}" = -m ] && [ "${{2:-}}" = venv ]; then
    venv="$3"
    mkdir -p "$venv/bin" "$venv/lib/python3.13/site-packages"
    ln -s /usr/bin/python3.13 "$venv/bin/python"
    printf '%s\\n' 'home = /usr/bin' 'include-system-site-packages = true' > "$venv/pyvenv.cfg"
    printf '%s\\n' '#!/bin/sh' 'echo "pip $*" >> "{log}"' 'if [ "$1" = install ] && [ "$2" = -e ]; then echo "$3" > "$(dirname "$0")/../lib/python3.13/site-packages/__editable__.neocortex-0.1.0.pth"; for tool in nc pytest ruff; do printf "%s\\n" "#!/bin/sh" "exit 0" > "$(dirname "$0")/$tool"; chmod +x "$(dirname "$0")/$tool"; done; fi' > "$venv/bin/pip"
    chmod +x "$venv/bin/pip"
fi
''')

    result = subprocess.run([str(SCRIPT)], env=env, text=True, capture_output=True, check=False)

    assert result.returncode == 0, result.stderr
    assert "pip install -e" in log.read_text()
    assert (runner / ".venv/bin/pytest").is_file()


def test_bare_pytest_prefers_each_checkout_over_runner_editable_install(tmp_path):
    """The retained pytest pythonpath makes a checkout win over runner's editable nc."""
    runner = tmp_path / "runner"
    subprocess.run(["git", "clone", str(ROOT), str(runner)], check=True, capture_output=True)
    venv = tmp_path / "venv"
    system_python = Path(shutil.which("python3.13") or shutil.which("python3")).resolve()
    (venv / "bin").mkdir(parents=True)
    (venv / "lib/python3.13/site-packages").mkdir(parents=True)
    (venv / "bin/python").symlink_to(system_python)
    (venv / "pyvenv.cfg").write_text(
        f"home = {system_python.parent}\ninclude-system-site-packages = true\n"
    )
    python = venv / "bin/python"
    # Model the path metadata produced by the runner's editable pip install.
    # Avoid invoking a build backend here: bootstrap's installer is covered by
    # the isolated-command test above.
    (venv / "lib/python3.13/site-packages/__editable__.neocortex-0.1.0.pth").write_text(f"{runner}\n")
    pytest = venv / "bin/pytest"
    pytest.write_text("#!" + str(python) + "\nfrom pytest import console_main\nraise SystemExit(console_main())\n")
    pytest.chmod(0o755)
    other = tmp_path / "other-worktree"
    subprocess.run(["git", "-C", runner, "worktree", "add", "--detach", str(other)],
                   check=True, capture_output=True)
    probe = tmp_path / "test_checkout_import.py"
    probe.write_text(textwrap.dedent("""\
        from pathlib import Path
        import nc

        def test_checkout_owns_nc():
            assert Path(nc.__file__).resolve().is_relative_to(Path.cwd().resolve())
    """))
    env = os.environ | {"PATH": f"{venv / 'bin'}:{os.environ['PATH']}"}
    env.pop("PYTHONPATH", None)

    for checkout in (ROOT, other):
        result = subprocess.run(["pytest", "-q", "-c", str(checkout / "pyproject.toml"), str(probe)], cwd=checkout,
                                env=env, text=True, capture_output=True, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
