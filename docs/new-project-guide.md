# Owner guide: onboarding an independent project

This is an **unexecuted** walkthrough for the existing Neocortex runner. It
does not register a project, create tasks, access a remote, install OpenCode or
credentials, or activate a service. Substitute real values first:

```bash
PROJECT_ID="example-successor"
REPO_PATH="/absolute/path/to/your/independent-repository"
TEST_COMMAND='your real project test command'
```

This guide documents current `nc` commands, verified against `nc/cli.py` and
the README on 2026-09-14. Names such as a future `opencode-successor dispatch`
are proposed design concepts only, not commands you can run today.

## Prepare the repository

Create or choose an independent repository with a committed Git base—not an
uncommitted directory or the running Neocortex checkout. Ensure its checks can
run from a Git worktree and put project instructions in `AGENTS.md` (including
the real test command, boundaries, and any safe tooling prerequisites). Prepare
the host prerequisites yourself: Git, the project test toolchain, and later any
chosen OpenCode/Node/credential setup. This documentation neither assumes nor
installs a JS toolchain, remote, provider account, or credentials.

Run the following only after replacing the placeholders and reviewing them:

```bash
git -C "$REPO_PATH" status
git -C "$REPO_PATH" log -1 --oneline
git -C "$REPO_PATH" worktree list

nc project "$PROJECT_ID" "$REPO_PATH" --test-cmd "$TEST_COMMAND"
nc doctor --project "$PROJECT_ID"
```

`nc project ID REPO [--test-cmd TEST_COMMAND] [--mirror REMOTE]` registers or
updates a project. `--mirror REMOTE` is optional and names an existing Git
remote to which accepted work may be pushed; omit it unless you intentionally
prepared that remote/policy. Registration **does not create implementation
tasks**. `nc doctor --project ID` checks service-host tools and that project's
base checkout; it is not an OpenCode live smoke.

Do not use `nc project` just to change an existing project's test command;
current README guidance is `nc project-test-cmd PROJECT_ID TEST_COMMAND`.

## Plan, inspect, approve

Ask the current planner for a proposal, then inspect before giving the only
authority that creates executable tasks:

```bash
nc plan "$PROJECT_ID" --note 'Use the initial planning brief below.'
nc proposals
nc proposal PROPOSAL_ID
nc approve PROPOSAL_ID
```

`nc approve PROPOSAL_ID` is explicit owner approval. Only this approval creates
queued tasks; a planner proposal and a worker result do not. Do not invent an
ID: take `PROPOSAL_ID` from `nc proposals`. The runner's `nc run` runs until idle
or a stop condition and may drain eligible tasks across all registered projects;
use it only with that scope understood. `nc step` runs one agent turn.

During work, use the current governance commands:

```bash
nc inbox
nc answer MESSAGE_ID 'your durable answer'
nc status
nc why TASK_ID
```

`nc inbox` shows owner questions/incidents; `nc answer` answers a particular
message and wakes its agent; `nc status` summarizes work; `nc why` shows an
existing task's dependencies, runs and review evidence. Pending proposal-local
specifications are inspected with `nc proposal`, not `nc why`. Owner STOP and
cancellation remain explicit controls (`nc stop`, `nc cancel TASK_ID --reason
...`) and must be reconciled before replacement work.

## Copyable initial planning brief

```text
Plan an independent experimental TypeScript coordinator that consumes OpenCode
through its supported headless server and SDK/HTTP interface. Do not migrate or
modify the current Python Neocortex runner. Reuse upstream sessions, messages,
context, tool execution, providers and CLI/TUI inspection. Add only governance
with durable owner-approved proposals/tasks, dependencies, questions/answers,
claims, candidate-bound checks, independent critic review and serialized merge.
Keep the external OpenCode server/TypeScript SDK baseline aligned with T028 and
the earlier planner proposal; this is a refinement, not another architecture
study. OpenCode owns session/context/message/tool history and Git owns commits
and refs; the coordinator owns approvals, readiness, claims, owner delivery,
review and acceptance. Record a coordinator project ID separately from upstream
project/directory identity and bind every session ID to its server/store
namespace. Drop duplicate session.log/transcript browsing, coordinator context
management, per-turn memo as sole memory, and agent-written outcome.json; use
upstream session inspection/export and CLI/TUI/web, treating exports as
diagnostics only. Do not invent a deep link from task to session; show the bound
namespace/session ID and directory. Attachment can mutate a session, so manual
continuation requires coordinated exclusion.

Use SDK schema-constrained structured output as the sole agent result path; the
host binds identity, validates semantics, deduplicates and persists it. Results
can request completion-for-review, owner question or unfinished progress, never
approval, merge or acceptance. Treat missing/invalid output, validation error,
provider failure and uncertain execution separately. Because SDK docs currently
show format/outputFormat inconsistently, label exact syntax pseudocode and defer
version/live behaviour to a focused smoke. Use a custom tool only for a proven
native-result gap; plugins may host logic but configuration alone is not durable
governance.

Use durable outbound intent, claim/attempt identity and message correlation;
never assume cross-store atomicity, API idempotency, or lease expiry means work
stopped. Reconcile crashes, duplicate observations, stale attempts, deleted
sessions, uncertain prompt/answer delivery and Git merge-before-DB-recording.
Unsettled cases stay uncertain/blocked with no automatic replay/replacement.
For ASK, durably store answer intent/exclusion before dispatch and acknowledge
delivery only with evidence; release capacity only after prior execution settles.
Pre-merge checks/critic are review evidence only. Record accepted only after
serialized merge, verified integration and required independent checks/review;
revalidate candidate/base on change. Start configurable execution concurrency at
one, preserve STOP/cancel and worktree isolation. Do not install software, use
paid agents, register anything, or expose secrets in planning. Propose staged
work only: minimal session execution; governance/recovery; independent
review/merge; owner pilot, with focused smoke for result/error retrieval,
restart correlation and session inspection/control.
```

The proposed successor's future milestones are intentionally small: (1) minimal
session execution, (2) governance/recovery, (3) independent review/merge, and
(4) owner pilot. They are not tasks created by this document. For architecture,
authority and recovery rationale, read the [OpenCode successor design](opencode-successor-design.md).
