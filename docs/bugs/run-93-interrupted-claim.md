# Run 93: closing an interrupted execution strands its logical claim

**Status:** confirmed current-behaviour defect; no runtime change in this report.

**Investigated:** 2026-09-10T08:10:36Z (UTC).  The investigator opened
`/root/.neocortex/state.db` as `file:...?...mode=ro`, set `query_only=ON`, and
read the rows below in one `BEGIN`/`COMMIT` read transaction.  This is a
historical incident report: values described as *later state* were observed at
that collection time, not asserted to have existed at the interruption.

## Evidence and timeline

The deployed unit, read without changing it, had `TimeoutStartSec=3600`.  Its
comment says a crashed turn costs one cycle, but that is not true for a claimed
worker under the current recovery code.  The journal records systemd's
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
| 2026-09-10 08:10:36 | Read-only snapshot: T008 `in_progress`; its worker `blocked`; no run after 93; T009 remains `queued` and depends on T008 and T007. |

Run 93's `session.log` was an empty, zero-byte file.  `brief.md` exists and
contains the T008 brief.  Therefore it supplies no terminal adapter diagnostic,
outcome file, exit status, or direct child-process termination evidence.  The
database recovery reason is an owner-entered audit reason, **not independently
verified termination evidence**.  The incident table has no `task_id` column,
so no task-filtered incident query is possible; the read transaction found no
separate incident that establishes why NC or a descendant exited.

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

Minimal reproducible state transition (isolated database fixture; no live
command was executed):

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
the attempt.  It has the side effect of blocking the agent.  It does **not**
resume work.  `nc resume --retry` separately resolves every open incident and
moves **all** blocked tasks to `in_progress`, resets attempts to zero, and makes
their conventional worker runnable; it can therefore resume T008 but is broad
and has those side effects.  `nc requeue T008` is also separate, requires no
unfinished run, queues the task/reset attempts and allows spawn; fresh requeue
can discard a worktree/branch after confirmation.  Neither action was run for
this investigation.

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
