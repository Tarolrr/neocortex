# Neocortex worktree contract

The supported runner host provides `git`, `sqlite3`, `python3.13`,
`python3-venv`, and the `.venv/bin/python` launcher.  Its service PATH also
provides `python`, `pytest`, `ruff`, `codex`, and `claude`.

Run the canonical checks from the repository/worktree root:

```sh
pytest -q
ruff check .
```

The arbiter runs shell acceptance checks and a project's configured test command
from the task worktree root.  Workers may write only in their assigned worktree;
they must never install packages or modify `/root/neocortex` or
`/opt/neocortex-runner`.  Host deployment is an owner operation using
`scripts/bootstrap.sh`.
