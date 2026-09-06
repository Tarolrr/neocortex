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
nc project neocortex /root/neocortex --test-cmd "pytest -q" --mirror origin

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

## Ordering tasks

A task can name tasks it builds on, and no worker is spawned for it until every
one of them is accepted:

```bash
nc task --project neocortex --title "..." --objective "..." \
  --after neocortex-T009 --after neocortex-T010
```

In a JSON pool the same field is `"depends_on": ["neocortex-T009"]`. A
dependency that is blocked, failed or absent stays unmet, so the queue moves on
to independent work instead of starting a task whose premise does not exist yet.
`nc tasks` shows `waits-for=` for anything still waiting.

To restart a task the reviewer or the owner stopped:

```bash
nc requeue neocortex-T009            # queued again, same branch and history
nc requeue neocortex-T009 --fresh    # also drops its branch and worktree, so
                                     # the next attempt starts from the base
nc requeue neocortex-T009 --budget 12  # and give it more turns than it had
```

Use `--fresh` when the task's premise changed — for example when it must now be
built on work merged after its branch was created; use `--budget` when the task
turned out to be larger than the estimate it was queued with.

A task is blocked after `max_attempts` failed turns or rework cycles (3 by
default, set it in `$NC_HOME/config.json`) and after `budget_turns` turns of its
own. Both are deliberately small: an agent that cannot converge in a few rounds
usually needs the task rewritten rather than more attempts. Raise them for work
that is genuinely large, not to push a task through a review it keeps failing.

## Reviewing and undoing accepted work

Run `nc why neocortex-T007` to see a task's status, acceptance criteria, every
run (agent, role, outcome, duration and log path), every message about it, and
the stored acceptance check output from `$NC_HOME/checks/neocortex-T007.txt`.
Missing check output is reported explicitly; an unknown task exits with status 1.

A project with a `mirror` remote gets its history pushed there right after the
arbiter merges — the task branch as `nc/<task-id>` and the runner's base branch
as `nc/main`, so the mirror can never fight the forge's own base branch. The
diff is reviewable on the forge while the queue keeps running; a failed push is
an incident, never a block.
To undo one task:

```bash
nc rollback neocortex-T007    # reverts its merge commit, pushes the mirror,
                              # and leaves the task blocked
```

Run `nc gc` to reclaim disk space by removing worktrees under `$NC_HOME/work`
for `done` and `blocked` tasks and pruning Git's stale worktree records. It prints
each removed path and keeps task branches intact. Uncommitted files in those
worktrees are discarded; queued, in-progress and in-review tasks are left alone.

## Configuration

`$NC_HOME/config.json`, created by `nc init`:

| key | meaning |
| --- | --- |
| `adapter` | default adapter: `codex` or `claude` |
| `adapters` | optional adapter per role (`worker`, `critic`); omitted roles use `adapter` |
| `models` | model per role (`worker`, `critic`) |
| `turn_timeout_s` | hard limit for one agent session |
| `max_consecutive_failures` | circuit breaker threshold |
| `min_free_mb` | preflight refuses to start below this |

For a Codex worker and an independent Claude critic, set these keys (model
names must suit the selected vendor):

```json
{
  "adapter": "codex",
  "adapters": {"worker": "codex", "critic": "claude"},
  "models": {"worker": "gpt-6-astra", "critic": "sonnet"}
}
```

Preflight probes the worker's selected adapter and model.

The Claude adapter's command line was verified against the installed
`/root/.local/bin/claude`, version **2.1.220 (Claude Code)**, using `--version`
and `--help` on 2026-09-06:

```bash
claude -p "<prompt>" --permission-mode bypassPermissions --model "<model>"
```

`-p` selects non-interactive output, `bypassPermissions` is a supported
permission mode, and `--model` accepts a model name or alias. The adapter omits
`--model` when its model string is empty and falls back to `~/.local/bin/claude`
when the binary is absent from `PATH`. Verification used no paid session.

`nc stop` writes a `STOP` file in `$NC_HOME` and `nc run` then exits immediately;
`nc resume` (optionally `--retry` to requeue blocked tasks) clears it. The circuit
breaker writes that same file, so a system failing every turn stays down instead
of being restarted into the same failure every few minutes.

## Unattended operation

`nc run` drains the queue and exits, so it is a natural oneshot unit:

```bash
cp deploy/neocortex.{service,timer} /etc/systemd/system/
systemctl enable --now neocortex.timer
journalctl -u neocortex -f
```

The timer starts a run every 5 minutes of inactivity. A failing turn costs one
cycle; three in a row trip the breaker and stop the timer's work until `nc resume`.

When agents work on Neocortex itself, run the scheduler from a second checkout
(`/opt/neocortex-runner`) so a merge never rewrites the code of the process
performing it. Promote a reviewed state with
`git -C /opt/neocortex-runner pull --ff-only /root/neocortex main`.

## Development

```bash
pytest -q
ruff check .
```

## Proposal approval

Proposals hold suggested tasks outside the queue until the owner decides:

```bash
nc proposals                 # list all proposals and their status
nc proposal 1                # full rationale, task specs and decision details
nc approve 1                 # atomically queue every proposed task
nc reject 2 "Outside scope"   # retain the reason without creating tasks
```

Producers use `State.add_proposal(project_id, source, rationale, spec)` with a
JSON-serializable list of task specs. Each spec uses the `nc task --file` fields:
`project`, `title`, `objective`, `acceptance`, and optional `boundaries`, `priority`
(default 100), and `budget_turns` (default 6). Tasks must belong to the proposal's
project. Creating a proposal stores it as `pending` and creates no tasks.
Approval preserves those fields and records `approved` with a decision timestamp;
rejection records `rejected`, the timestamp and reason. Decisions are final:
repeated approval or rejection exits with status 1 and creates nothing. Failed
batch approval leaves the proposal pending and rolls back every task in the batch.
Existing state databases gain the proposal table automatically on opening.

Proposals store deterministic `findings` at creation and refresh them before
approval. Both `nc proposal` and `nc proposals` show the findings. Unknown
dependencies, cycles, missing acceptance commands starting with `$`, path-only
boundaries (such as `src/api.py` or `Do not modify src/api.py`), and direct textual
acceptance/boundary conflicts block approval. Invariants such as “Public API must
remain backward compatible” are valid boundaries. Text checks recognize simple
action/target conflicts (“Change the public API” versus “Do not change the public
API”); they cannot prove arbitrary natural-language requirements consistent.
Commands are never executed by these checks, and no model is called.
`nc approve 1 --force` overrides findings and prints each override. Even a clean
proposal stays pending until the owner approves it; checks never edit its spec.

A proposed task may declare an optional unique `id` for other tasks in the same
proposal to reference through `depends_on`. These local IDs must not collide with
existing task IDs. Approval resolves local references to the newly allocated task
IDs, including forward references. Other dependencies must name existing tasks.
