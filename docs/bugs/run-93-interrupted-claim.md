# Run 93: closing an interrupted execution strands its logical claim

**Status:** confirmed current-behaviour defect; no runtime change in this report.

**Investigated:** 2026-09-10T08:10:36Z (UTC).  The investigator opened
`/root/.neocortex/state.db` as `file:...?...mode=ro`, set `query_only=ON`, and
read the rows below in one `BEGIN`/`COMMIT` read transaction.  This is a
historical incident report: values described as *later state* were observed at
that collection time, not asserted to have existed at the interruption.

## Evidence and timeline

The deployed unit source was read as `/etc/systemd/system/neocortex.service`
via `systemctl cat neocortex.service`, without changing it. It had
`TimeoutStartSec=3600`. The tracked
[`deploy/neocortex.service`](https://github.com/Tarolrr/neocortex/blob/735907fe6afb6f635e3e02cfaa1e361d06c70128/deploy/neocortex.service)
also specifies 3600 seconds, but differs from that deployed source in its
`PATH` (it includes the runner venv) and its updated explanatory comment. The
deployed comment says a crashed turn costs one cycle; that is not true for a
claimed worker under the current recovery code. The journal records systemd's
`start operation timed out. Terminating`, then main process `status=15/TERM`
and `result 'timeout'` at **2026-09-09T21:42:09Z**.  This is evidence that the
*service main process* was terminated for its start timeout; it is not, by
itself, proof that every separately launched adapter descendant had died.

| UTC time | Sanitized durable/journal observation |
| --- | --- |
| 2026-09-09 21:42:07 | Journal logged critic T008-2 DONE; T008's task `updated_at` is this second. |
| 2026-09-09 21:42:08 | Run 93 began for `worker-neocortex-T008`; its brief was written. |
| 2026-09-09 21:42:09 | systemd logged timeout and SIGTERM of the service main process. |
| 2026-09-09 21:47:25 onward | Later timer-triggered services logged `no runnable agents`; they did not run T008. |
| 2026-09-09 22:01:57 | Later owner recovery closed run 93: `outcome=INTERRUPTED`, `ended_at=interrupted_at=recovered_at`; reason recorded as `nc killed by sysctl by timeout, wrong command by owner`. |
| 2026-09-10 06:18:33 | Later owner feedback message 83 (delivered, `owner` to `planner-neocortex`) reported that run 93 had left a task “running” with no new runner and requested this investigation/protocol revision. |
| 2026-09-10 08:10:36 | Read-only snapshot: T008 `in_progress`; its worker `blocked`; no run after 93; T009 remains `queued` and depends on T008 and T007. |

Run 93's `session.log` was an empty, zero-byte file.  `brief.md` exists and
contains the T008 brief.  Therefore it supplies no terminal adapter diagnostic,
outcome file, exit status, or direct child-process termination evidence.  The
database recovery reason is an owner-entered audit reason, **not independently
verified termination evidence**.  The incident table has no `task_id` column,
so no task-filtered incident query is possible; the read transaction found no
separate incident that establishes why NC or a descendant exited.

Related message history was read in that same transaction: four T008
worker-directed `review_verdict` messages (IDs 76--79) were present. IDs 76
and 77 were delivered; IDs 78 and 79 were undelivered. They are historical
critic/arbiter feedback, not termination evidence, and no separate T008 worker
message establishes an exit cause.

Feedback 83 is a later (2026-09-10T06:18:33Z) delivered owner `feedback`
message, addressed to `planner-neocortex` with no task ID. Sanitized, it says
that NC and its children, including runner 93, had exited; that interrupting
the run left the task running without a new runner; and it asks for log/state/
code investigation and a protocol revision. This is the owner's incident
report and explains the requested investigation. It is **not** independent
proof of how NC or an adapter terminated, and is deliberately distinguished
from the incident-time journal and from the later 08:10:36Z database snapshot.

The full investigated code commit is
[`735907fe6afb6f635e3e02cfaa1e361d06c70128`](https://github.com/Tarolrr/neocortex/blob/735907fe6afb6f635e3e02cfaa1e361d06c70128/nc/operations.py#L175).
Code references in this report are pinned to that commit: current recovery
[`recover_runs`](https://github.com/Tarolrr/neocortex/blob/735907fe6afb6f635e3e02cfaa1e361d06c70128/nc/operations.py#L175),
scheduler selection/spawn [`pick`/`_spawn_for_queued_task`](https://github.com/Tarolrr/neocortex/blob/735907fe6afb6f635e3e02cfaa1e361d06c70128/nc/scheduler.py#L122),
owner retry/requeue [`retry_blocked_tasks`/`requeue_task`](https://github.com/Tarolrr/neocortex/blob/735907fe6afb6f635e3e02cfaa1e361d06c70128/nc/operations.py#L365),
and UI recovery [`runs` handlers](https://github.com/Tarolrr/neocortex/blob/735907fe6afb6f635e3e02cfaa1e361d06c70128/nc/ui.py#L683).

## Expected versus actual

**Expected:** once authorized recovery has proved the interrupted claim is no
longer executing, every logical claim must become either safely eligible in its
existing phase or durably blocked with a specific reason and an actionable
owner path.  Closing an attempt must not make the task invisible.

**Actual:** `recover_runs` takes the lifecycle lock, rechecks owner/cgroup
evidence, then in one transaction only ends the selected run as `INTERRUPTED`
and sets its agent to `blocked`.  It neither changes a worker task from
`in_progress`, nor makes an agent runnable, nor emits a recovery incident or
applies/replays an outcome.  The web form calls the same function.  Scheduler
selection requires `agent.state='runnable'`; automatic spawn considers only
`task.status='queued'`.  Consequently an `in_progress` T008 with a blocked
worker matches neither path.

The following is an isolated, disposable-database reproduction. It is not a
command against `/root/.neocortex/state.db`, does not contact an adapter, and
does not start a worker. It precisely creates the three durable rows, applies
the current recovery transition, and invokes `Scheduler.pick()`:

```sh
repro_dir=$(mktemp -d)
REPRO_DIR="$repro_dir" python3 - <<'PY'
import os
from pathlib import Path

from nc.config import Config
from nc.operations import recover_runs
from nc.scheduler import Scheduler
from nc.state import State

home = Path(os.environ["REPRO_DIR"]) / "home"
home.mkdir()
cfg = Config(home=home)
state = State(cfg.db_path)
try:
    state.add_project("p", "P", str(home), None)
    task = state.add_task("p", "interrupted claim", "reproduce", [])
    worker = state.add_agent("worker-p-T001", "worker", "p", task, "model")
    # Model the already-claimed run; no Scheduler spawn is involved here.
    state.set_task(task, status="in_progress")
    run = state.start_run(worker, task, "worker", "model", "unused.log")
    # A legacy record with no ownership evidence needs explicit, verified
    # quiescence; this makes the fixture eligible for current recovery.
    state.x("UPDATE run SET owner_pid=NULL, owner_start=NULL, ownership_version=0 WHERE id=?", (run,))
    recover_runs(state, [run], "fixture: verified quiescent", True)
    print(tuple(state.one("SELECT outcome, ended_at IS NOT NULL FROM run WHERE id=?", (run,))))
    print(tuple(state.one("SELECT state FROM agent WHERE id=?", (worker,))))
    print(tuple(state.one("SELECT status FROM task WHERE id=?", (task,))))
    print(Scheduler(cfg, state).pick())
finally:
    state.db.close()
PY
```

Expected output is an interrupted, ended run; a `blocked` worker; an
`in_progress` task; and `None` from `pick()`. The final `None` is meaningful:
`pick()` tries queued-task activation before returning, but the only task is
not queued. The relevant checked-in recovery precedent is
[`test_legacy_requires_explicit_quiescence_and_preserves_task`](https://github.com/Tarolrr/neocortex/blob/735907fe6afb6f635e3e02cfaa1e361d06c70128/tests/test_recovery.py#L65),
which confirms that recovery preserves task state; this recipe adds the
scheduler selection observation without modifying the test suite.

The resulting transition is:

| Step | run | worker agent | task | `Scheduler.pick()` result |
| --- | --- | --- | --- | --- |
| claim | open | runnable | in_progress | worker is selectable |
| process interrupted; authorized `recover_runs` | ended/INTERRUPTED | blocked | in_progress | none |
| next scheduler tick | unchanged | blocked | in_progress | none; spawn only scans queued tasks |

This explains the later journal's idle result.  It does not show a scheduler
selection bug independent of the stored state: selection is behaving as coded.

## Impact and safe current owner actions

T008 is stranded until an owner performs another lifecycle action.  T009 is
queued with dependencies `["neocortex-T008", "neocortex-T007"]`; thus T008
cannot become an accepted prerequisite for T009.  This report does not make
T009 or its local provider-retry work depend on implementing recovery changes.

Current `recover-runs` is safe only as a *record closure*: it refuses live or
ambiguous current ownership, needs an explicit quiescence acknowledgement only
for legacy evidence, retains the contemporaneous `detail`, and does not replay
the attempt. It has the side effect of blocking the agent. It does **not**
resume work. `nc resume --retry` separately removes STOP, resolves every open
incident, and moves only tasks already in `blocked` to `in_progress`, resetting
their attempts and making their conventional workers runnable. It does **not**
select or wake the observed T008: T008 is `in_progress` and only its agent is
blocked. For this exact observed state, the existing eligibility-restoring owner
action is `nc requeue neocortex-T008` (after confirming there is no unfinished
run). It queues T008, resets its attempts, writes its result as owner-requeued,
marks its task agents blocked, and marks owner-directed task messages delivered;
the scheduler can subsequently make the queued work claimable/spawn it. A
`--fresh` requeue can additionally discard a worktree/branch after confirmation.
Neither action was run for this investigation.

## Findings and hypotheses

Confirmed: service timeout/TERM of NC; the run's later closed state; empty
session log; blocked worker/in-progress task/no later T008 run; T009 dependency;
and the current state-machine gap above.  The unit's configured timeout was
3600 seconds, and its journal time is consistent with a one-hour service.

Hypotheses only: NC may have been stopped by systemd while it was between run
creation and adapter completion; the adapter may have been terminated with the
service or may have outlived it.  Missing parent PID is not death proof, and no
available cgroup membership snapshot or process-exit journal closes that gap.
This is a host timeout/termination case, not evidence of a temporary provider
failure; it must not automatically be deferred as such.

## Proposed repair and validation

The proposed protocol is in [the exchange contract](../exchange-contract.md#proposed-interruption-recovery-contract-run-93).  It is a future
owner-approved runtime/schema proposal, not an authorization to change live
state.  Future tests must cover restart/reconciliation after: (1) crash before
adapter launch, (2) during an owned execution, (3) after result persistence but
before application, and (4) after application but before acknowledgement; each
must prove one fenced attempt, preserved partial state/inbox/memo, no duplicate
acceptance/advice, and either retryability or a durable actionable block.
