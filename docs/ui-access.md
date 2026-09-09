# Local owner UI

Initialize an isolated home with `nc --home /path/to/home init`, register projects
using the CLI, then run `nc ui --home /path/to/home --port 8765`. `NC_HOME` is used
when `--home` is omitted. Its default bind is `127.0.0.1`; open
http://127.0.0.1:8765 and select a project. Port `0` is permitted for an
ephemeral local test port. Stop the foreground server with Ctrl+C. Assets ship
with the Python package; there is no frontend build or external asset service.

For remote access, keep the default loopback bind and forward the same port from
your laptop:

```sh
ssh -N -L 8765:127.0.0.1:8765 user@host
```

Then open http://127.0.0.1:8765 locally. Use the same local and remote port so
Host and Origin validation succeeds. This remains the default remote-access
method.

For an owner-controlled trusted network only, bind an explicit IPv4 address or
resolvable IPv4 hostname and authorize the exact browser hostname/address:

```sh
nc ui --host 192.0.2.10 --port 8765 --allowed-host 192.0.2.10
```

`--allowed-host` is repeatable. Each value is an exact concrete browser hostname
or IPv4 literal: no scheme, path, port, wildcard, or IPv6 syntax. IPv6 is not
supported and is rejected clearly. Requests must use exactly one `Host` header
with one configured value and the actual listening port; `Origin` must likewise
be exactly `http://HOST:PORT`. `localhost` and `127.0.0.1` remain accepted for
the default local server, and a concrete `--host` is accepted automatically.
`--host 0.0.0.0` requires at least one explicit `--allowed-host`; it is a bind
address, not a browser destination. Forwarded headers are never trusted.

Host and Origin checks, session cookies, and CSRF tokens protect browser request
shape; they do not authenticate clients or restrict who can connect. The UI has
no authentication or TLS, and network clients can exercise owner actions. Use
only an owner-controlled trusted network or an SSH tunnel. Public hosting and
reverse-proxy support are not implemented. Session cookies and CSRF tokens last
until the server stops. Mutations require POST, a session token, and matching
Host/Origin. GET pages use independent SQLite connections without schema
initialization. Busy writes return HTTP 503 with a retry hint.

## Optional systemd UI service

`deploy/neocortex-ui.service` is deliberately independent of the queue timer,
so it remains available while `STOP` pauses agent work. Install it using your
normal owner-managed procedure, then enable and start it:

```sh
sudo install -m 0644 deploy/neocortex-ui.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now neocortex-ui.service
systemctl status neocortex-ui.service
journalctl -u neocortex-ui.service -f
```

It defaults to `NC_HOME=/root/.neocortex` and `127.0.0.1:8765`. Customize the
home, bind, port, and allowed hosts with an override (repeat `--allowed-host`
as needed):

```ini
# sudo systemctl edit neocortex-ui.service
[Service]
Environment=NC_HOME=/srv/neocortex
ExecStart=
ExecStart=/opt/neocortex-runner/.venv/bin/nc ui --host 192.0.2.10 --port 8765 --allowed-host 192.0.2.10
```

After changing it, run `sudo systemctl daemon-reload` and
`sudo systemctl restart neocortex-ui.service`. To stop and remove automatic
startup, run `sudo systemctl disable --now neocortex-ui.service`. This task does
not install, enable, restart, or otherwise alter live services.

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
| Inspect/recover interrupted runs | `runs`, `recover-runs ID --reason ...` | Unfinished runs | T028 |
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

# Interrupted-run walkthrough

`nc runs` is read-only and lists every unfinished run record, including runs
from other projects and taskless planner/plan-critic roles.  `ended_at IS NULL`
means only that the record was not finalized; its ownership field is the process
evidence and must not be read as a liveness claim.  Inspect the recorded agent,
task-or-role, start time and log before taking action.

Newer records retain a dedicated cgroup created before adapter code executes.
This lets inspection refuse recovery when an adapter child survives a scheduler
exit, including a child that calls `setsid()` or changes process group. A
missing or uninspectable cgroup identity is shown as uncertain and cannot be
overridden for a new row.

For known interrupted rows, submit `nc recover-runs RUN_ID [RUN_ID ...] --reason "..."`.
This records `INTERRUPTED`, timestamps and the owner reason without requeueing,
changing a budget, deleting a worktree or replaying an outcome.  The browser has
the same selected-runs form at **Unfinished runs** (with an individual-row
shortcut).  Every submitted selection is validated as one atomic operation:
one live, unknown, stale or ambiguous row rejects the whole selection.  Live
ownership is refused.

Legacy rows, and rows made before adapter process-group recording, have
insufficient ownership evidence. Before adding
`--acknowledge-quiescence`, document verification of scheduler processes and
all adapter descendants (for example with `ps`/`pgrep` scoped to this home and
run log); an inactive systemd unit alone is not sufficient. The acknowledgement
is intentionally explicit because it is not proof supplied by the database.

Once all actual blockers are recovered, requeue is a separate explicit action.
For an exhausted task such as T024, use an owner reason and a larger budget, for
example `nc requeue neocortex-T024 --budget 8 --reason "continue after reviewed interruption"`.
Recovery itself never does this. UI deployment templates and the deployment
runbook remain queued in T027 behind T026/T024; this document only describes the
current UI deployment is documented above; it does not imply any host settings
are already installed.

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
