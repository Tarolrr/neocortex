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
