# Neocortex

A multi-agent development runner for a machine that can only run one agent at a
time. Agents are coroutines, not processes: a turn ends with `DONE`, `ASK`,
`YIELD` or `FAIL`, and an agent waiting for an answer holds no memory and no
process. That is what makes "many agents" affordable on an Orange Pi with 1 GB
of RAM.

Design rationale: <docs/design-v2.md>. Why the previous prototype stalled:
<docs/prototype-analysis.md>.

## The loop

```
queued task -> worker turn(s) -> arbiter runs acceptance checks -> critic reviews the diff
            -> arbiter merges, or sends the worker back with findings, or escalates to you
```

- **Worker** implements one task in its own git worktree and branch. It can
  never accept its own work: its `DONE` means "ready for review".
- **Arbiter** is plain code. It runs the acceptance criteria that are shell
  commands, merges the branch when the critic passes it, and counts attempts.
- **Critic** is a separate session that sees the diff, the acceptance criteria
  and the check output — deliberately *not* the worker's account of what it did.
- **Scheduler** picks one runnable agent per turn, preflights the model before
  starting, and trips a circuit breaker after repeated failed turns.

Everything lives in SQLite (`$NC_HOME/state.db`), so state survives between
turns, restarts and days.

## Usage

```bash
pip install -e .
export NC_HOME=~/.neocortex
nc init
nc project neocortex /root/neocortex --test-cmd "pytest -q"

nc task --project neocortex \
  --title "Add a health command" \
  --objective "nc health should print the DB path and the number of open incidents." \
  --accept '$ nc health' \
  --accept 'The output contains the database path' \
  --boundary 'Do not touch the scheduler'

nc preflight     # is the model actually usable right now?
nc step          # exactly one agent turn
nc run           # turns until nothing is runnable
nc status
```

An acceptance criterion starting with `$` is a shell command run in the
worktree, and it must exit 0 before the critic is ever invoked. Everything else
is prose the critic checks against the diff.

When an agent needs you:

```bash
nc inbox                       # questions and incidents addressed to the owner
nc answer 7 "use port 8080"    # answers and makes that agent runnable again
```

## Configuration

`$NC_HOME/config.json`, created by `nc init`:

| key | meaning |
| --- | --- |
| `adapter` | `codex` or `claude` |
| `models` | model per role (`worker`, `critic`) |
| `turn_timeout_s` | hard limit for one agent session |
| `max_consecutive_failures` | circuit breaker threshold |
| `min_free_mb` | preflight refuses to start below this |

Creating a file named `STOP` in `$NC_HOME` stops the loop after the current turn.

## Development

```bash
pytest -q
ruff check .
```
