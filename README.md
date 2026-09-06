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
  If the accepted branch no longer merges into the base branch, it aborts the
  merge, records a `merge_conflict` incident and returns the task to the worker
  with the conflicting files; this does not count as an attempt, and a new critic
  reviews the resolved result again.
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
`nc resume` clears STOP and closes all open incidents, recording the resolution
time and the note `Closed by nc resume`. Its optional `--retry` also requeues
blocked tasks. The circuit
breaker writes that same file, so a system failing every turn stays down instead
of being restarted into the same failure every few minutes.

`nc incidents` lists open incidents. Use `nc resolve <incident-id> --reason TEXT`
to acknowledge only the selected incident with an owner resolution note and time.
This does not assert repository repair, clear STOP, requeue tasks, or wake agents.
`nc incidents --all` includes closed incidents and their resolution details.
Unknown IDs fail without changing incident data; resolving an already closed
incident preserves its original resolution. Incident details and timeout
deduplication remain intact. Older closures without metadata display an unknown
time and no recorded note.

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
nc proposals                 # list status, task counts and inspection commands
nc proposal 1                # preview full JSON before deciding
nc approve 1                 # atomically queue every proposed task
nc reject 2 "Outside scope"   # retain the reason without creating tasks
```

Before approving, use the `nc proposal ID` inspection hint printed by `nc proposals`.
The JSON already contains every full task title, objective, acceptance criterion,
boundary, and dependency in `spec`, plus rationale, findings, advisory
`plan_review`, and decision details. Inspection is read-only: pending specs are
visible even when no task rows exist. Only owner approval creates queued tasks
from a proposal.

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

Pending proposals receive one advisory plan critic session when no worker or
change critic is runnable and no queued task is ready. The session inspects the
current repository and proposed tasks without the planner's rationale, feedback,
or memo. It uses `models.plan_critic`, falling back to the planner model, and the
restricted planner adapter path in a run directory, without taking a worktree.
`nc proposal ID` includes `plan_review` with findings, a recommendation and review
status (or null before review). These findings do not gate approval or modify the
proposal. Only the owner's `nc approve` creates tasks from it. Failed sessions
are recorded without retrying or tripping the task circuit breaker. A changed
proposal spec can receive another review; unchanged specs are never run twice.


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
For example, a spec ID `first` is a proposal-local reference, while approval
allocates a queued task ID such as `demo-T001`. The numeric ID in `nc proposal 1`
identifies the whole proposal. `nc why demo-T001` applies after task creation
because it looks up a task row and its execution/review evidence; it cannot
inspect a proposal-local ID. Use `nc proposal 1` to inspect those specs before
approval, then use the task IDs printed by `nc approve 1` with `nc why`.

## Owner feedback and planning requests

```bash
nc feedback "Keep configuration simple" --project neocortex
nc feedback "Cover the empty case" --task neocortex-T007
nc plan neocortex --note "Review the remaining work"
nc status                    # shows all undelivered feedback, including its text
```

Feedback uses the task's project when `--task` is supplied, or the sole registered
project when neither selector is supplied. With multiple projects, specify
`--project` or `--task`. Unknown projects/tasks and mismatched selectors fail
without storing a message or waking an agent.

Both commands atomically store an owner message of kind `feedback` (payload
`{"text": "..."}`) in the project's single planner inbox and make that agent
runnable. `plan` without a note stores "Request a planning pass." Repeated calls
preserve every message and reuse the planner, including after it has stopped.
New planners use `models.planner`, falling back to the heaviest configured model
under the local priority policy: `gpt-6-astra`, `gpt-5.6-sol`, `gpt-5.6-terra`,
`gpt-5.5`, then `gpt-5.6-luna`. Unknown names rank below these and retain
configuration order on ties; set `models.planner` explicitly for other models.
Neither command starts a session: execution belongs to the timer/scheduler.
Planner turns run only after task workers, task critics, and pending proposal reviews.
`planner_max_queued` (default 5) limits the project's queued tasks; planning waits
when the count exceeds it. `planner_max_pending_proposals` (default 1) blocks
planning when the project's pending proposal count reaches that capacity. Set it
to 0 to pause planning. These settings live in `config.json`.
Skipped planners enter `waiting`, retaining their owner feedback for the next
scheduler tick. They do not keep an otherwise idle scheduler running. Once the
gates clear, the existing trigger can run without another owner request.
Projects record `planner_last_ran_at` and `planner_skip_reason` for inspection.
A completed planning turn records a proposal for owner approval or asks the owner
a question; it does not automatically schedule another planning turn.

Use cancellation to remove superseded copies from the active task list without
losing their history:

```sh
nc cancel demo-T003 --reason "Superseded by demo-T007"
nc tasks                 # excludes cancelled tasks
nc tasks --all           # includes cancelled tasks
nc why demo-T003          # original evidence and owner cancellation reason
nc requeue demo-T003 --reason "Restore this task"
```

Only queued, blocked, and failed tasks can be cancelled; tasks with active runs
cannot be cancelled. Repeating cancellation is harmless and keeps the original
reason. Cancellation retires associated agents without deleting tasks, messages,
runs, dependencies, branches, or worktrees, or changing accepted work and merge
history. Cancelled tasks do not run or escalate unanswered questions, and neither
`nc answer` nor `nc resume --retry` restores them. Only explicit `nc requeue`
restores eligibility. A cancelled prerequisite remains unmet; use `nc why` on
the dependent task to inspect that condition.

To revise a pending proposal, inspect its full specification and advisory review
with `nc proposal ID`, then run
`nc feedback --proposal ID "Describe the revision you want"`.
You may also supply `--project PROJECT` to verify the project; `--task` and
`--proposal` are mutually exclusive. This immediately marks the original
superseded and unapprovable, preserves its specs and review, and wakes the planner.
The planner receives the original full specs and your feedback. Normal capacity
limits and worker/critic priority still apply.

Use `nc proposals` and `nc proposal ID` to follow the replacement link. Inspect
the new pending replacement and its fresh advisory review before explicitly
running `nc approve NEW_ID`; only approval creates tasks. Proposal detail includes
revision lineage and feedback on both ends. To iterate again, address feedback to
the new pending replacement. Approved, rejected, and superseded proposals cannot
receive revision requests. If planning asks a question, answer with `nc answer`;
after a failed session, `nc plan PROJECT` retries with the revision context intact.
