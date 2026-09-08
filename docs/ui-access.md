# Local owner UI

Initialize an isolated home with `nc --home /path/to/home init`, register projects
using the CLI, then run `nc ui --home /path/to/home --port 8765`. `NC_HOME` is used
when `--home` is omitted. Open http://127.0.0.1:8765 and select a project.
Stop the foreground server with Ctrl+C. Assets ship with the Python package;
there is no frontend build or external asset service.

The server binds only to IPv4 loopback. For a remote host, run the UI there and
forward the same port from your laptop:

```sh
ssh -N -L 8765:127.0.0.1:8765 user@host
```

Then open http://127.0.0.1:8765 locally. Use the same local and remote port so
Host and Origin validation succeeds. Do not publish the port through a reverse
proxy. This console trusts local users; it is not a multi-user authentication
service. Session cookies and CSRF tokens last until the server stops. Mutations
require POST, a session token, and matching Host/Origin. GET pages use independent
SQLite connections without schema initialization. Busy writes return HTTP 503
with a retry hint.

# Phase-one action mapping

CLI and HTTP handlers use `nc.operations`, backed by State and arbiter. No
browser route constructs shell commands, parses CLI output, or starts agent
sessions. Feedback queues planner work for the scheduler to handle later.

| Owner action | CLI | Browser | Assignment |
| --- | --- | --- | --- |
| Create/import task | `task`, `task --file` | Project → New task / Import JSON | ui-tasks |
| List/explain | `tasks`, `why` | Project tasks / task detail | ui-tasks |
| Cancel | `cancel` | Task → Cancel | ui-tasks |
| Requeue, fresh branch, budget | `requeue --fresh --budget` | Task → Requeue | ui-tasks |
| Rollback | `rollback` | Accepted task → Roll back | ui-tasks |
| Feedback/plan, proposal revision | `feedback`, `plan` | Project → Feedback / plan, proposal feedback | phase one |
| Questions/answers | `inbox`, `answer` | Inbox → Answer | phase one |
| Proposal list/detail | `proposals`, `proposal` | Project → Proposals | phase one |
| Approve/force/reject | `approve --force`, `reject` | Proposal detail forms | phase one |
| Scheduler administration | `run`, `step`, `stop`, `resume`, health/preflight | Deferred | FU-001 |
| Incident administration | `incidents`, `resolve` | Deferred | FU-002 |
| Project administration | `project` | Selection only | FU-003 |

Required task lifecycle serialization, active-run guards, repository coordination,
and fresh/requeue/rollback correctness belong to **ui-tasks in this batch**.
They are not covered by the scheduler-administration deferral. See the repository
[follow-up record](follow-ups.md) for deferred scope; that record must not be used
to defer failures of current acceptance criteria.

Task lifecycle changes take a nonblocking home-local lock shared with scheduler
turns. A turn owns this lock from selection through worktree preparation and
outcome integration; cancel, requeue and rollback report a retry message while
it is busy. This deliberately serializes lifecycle changes across projects on
this single-worker host without holding a SQLite transaction during agent work.
Fresh requeue checks Git cleanup before resetting task and agent state.

Fresh requeue and rollback reserve the SQLite write before touching Git, so
contention leaves the repository untouched. Rollback records its task transition
and incident together, and only accepted tasks can be rolled back. Answers share
the lifecycle lock to avoid racing scheduler outcome integration.

To check packaging in a disposable environment, build and install a wheel, then
run that environment's Python with `-I scripts/check_installed_ui.py`. The check
loads the installed package and requests its CSS over a temporary loopback
server; run it from a writable directory for its temporary isolated home.

Task import accepts either pasted JSON or a UTF-8 JSON upload (one object or an
array). The complete form is limited to 1 MiB. Upload filenames are ignored;
only submitted bytes are parsed and validated on the server. Supplying both
nonempty pasted and uploaded content produces an error without creating tasks.
Stored check evidence is read only from regular, non-symlink task files in the
configured checks directory; unavailable or unsafe evidence displays as absent.

Repeatable import/evidence smoke steps in a disposable project:

1. Open the project's task list. Confirm the project filter is applied, then
   toggle “include cancelled” and confirm a cancelled task appears with its
   status and unmet dependencies. Open both tasks and verify the detail shows
   multiline objective, acceptance, boundaries, result, runs, messages, and
   “no stored check output” when evidence is missing.
2. Choose New task and submit every field: project, title, multiline objective,
   acceptance, boundaries, priority, turn budget, and dependencies. Try an
   empty title, zero budget, unknown dependency, and cross-project dependency;
   each must display a validation error without creating a task.
3. Open a blocked task and use Requeue with a reason and changed budget; confirm
   the queued result and displayed budget. Choose Fresh requeue: inspect the
   discarded-work summary, submit its confirmation, and verify the worktree is
   removed. Change the task in another tab before submitting the old token and
   verify the stale-confirmation error. While a scheduler turn is paused, submit
   Fresh requeue and verify its retryable busy error and that no worktree or
   task state changes.
4. Open an active task, choose Cancel, enter a reason, and submit; verify the
   cancelled status and retained history. Use the cancelled-list toggle to find
   it. Try cancellation/requeue while a run is active and verify the visible
   rejection leaves it unchanged.
5. Open an accepted task, choose Roll back, inspect the displayed merge commit,
   and explicitly confirm it. Verify the success message and blocked result.
   Submit a stale commit from a second tab and verify the confirmation error;
   while another task's integration is paused, submit rollback and verify the
   retryable busy error and unchanged repository/task state. Retry after release
   and verify coherent history and the success message.
6. Choose Import JSON. Paste a spec with all
   fields (`project`, `title`, `objective`, `acceptance`, `boundaries`, `priority`,
   `budget_turns`, `depends_on`); submit and inspect the resulting task detail.
7. Upload the equivalent UTF-8 JSON object, then an array of two objects. Check
   the imported count and multiline objective, acceptance, and boundaries.
8. Submit malformed JSON, a zero budget, a different project, and both pasted
   and uploaded JSON. Each must show a validation error and create no tasks.
9. Open a task without check evidence: it must show “no stored check output”.
   In the disposable home, create `checks/TASK_ID.txt` and reload to see it.
   Replace that file with a symlink to external text and reload: external text
   must not appear. Repeat with the checks directory itself as a symlink.
