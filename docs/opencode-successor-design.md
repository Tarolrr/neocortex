# OpenCode-based experimental successor

## Decision

Build a **separate TypeScript coordinator** that consumes OpenCode as an
external dependency through its headless server and supported JS/TS SDK (or the
documented HTTP interface). It is an experiment developed through the existing
Neocortex workflow, not a migration of this Python runner and not a change to
its deployment. Start with one configured execution slot, then raise it only
after an evidenced limiter and recovery behaviour work in an owner pilot.

This recommends upstream reuse over legacy-code preservation, per owner
feedback #345. OpenCode already supplies the expensive and fast-moving parts:
sessions, messages, context management, model/provider integration, tool
execution, permissions, and session inspection. The coordinator supplies only
governance OpenCode does not document as providing: owner authority, task
dependencies, durable owner correspondence, durable claims, and independent
acceptance/merge. A fork or duplicated OpenCode subsystem is rejected unless a
specific missing capability is demonstrated; none is demonstrated here.

OpenCode's SDK is expressly a type-safe JS/TS client for its server and can
create or connect to a server; its generated types and structured JSON output
are useful integration boundaries. [SDK](https://opencode.ai/docs/sdk/)

## Evidence and limits

Observations below were made 2026-09-14 from the live documentation and the
`dev` source URLs linked here. The latter are mutable development observations,
not release guarantees or a dependency commitment. No exact version lock,
platform certification, full upstream audit, live credentials, or JS toolchain
was used.

* The [server API](https://opencode.ai/docs/server/) documents session creation,
  status, messages, async prompting, abort, session message retrieval, SSE
  events, agents, provider/auth endpoints, and permission responses. It also
  labels experimental tools as experimental.
* The [agents](https://opencode.ai/docs/agents/) and
  [permissions](https://opencode.ai/docs/permissions/) docs describe configured
  agents and allow/ask/deny tool rules. Permission prompting is an application
  control, not an OS sandbox. `--auto` approves requests not explicitly denied.
* The [CLI](https://opencode.ai/docs/cli/) documents TUI sessions and continuing
  by session ID; use it for inspection where it supports the need instead of
  rebuilding a transcript viewer. [ACP support](https://opencode.ai/docs/acp/)
  is OpenCode acting as an ACP-compatible subprocess for an editor. It is not
  this coordinator's transport and says nothing about codex-acp AIR or vendor
  subscription/auth equivalence.
* Mutable source observations: [`session.ts`](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/opencode/src/session/session.ts)
  maps persisted session rows with IDs, directory, parent, agent/model, version,
  token/cost and summaries; [`prompt.ts`](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/opencode/src/session/prompt.ts)
  composes prompt, agent, provider, permission, compaction, tools and event
  services and has cancellation; [`task.ts`](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/opencode/src/tool/task.ts)
  supports resumed task IDs and background subagent work. These support reuse,
  not a stability promise.
* Local inspection found `nc/state.py` persists projects, proposals/tasks,
  messages and runs; `nc/scheduler.py` has repository/lifecycle exclusions;
  `nc/arbiter.py` runs checks; and `nc/cli.py` separates owner proposal approval
  from task work. The successor preserves the invariants, not that schema/code.

Missing evidence is **unknown**, not evidence of absence: production event
delivery/reconnect semantics, exact cancellation acknowledgement, authentication
storage/rotation, provider errors, background-task limiting and API stability
need a later small smoke experiment.

## Options considered

| Option | Assessment | Decision |
| --- | --- | --- |
| OpenCode dependency + small coordinator | Reuses agent loop and adds only governance. | **Choose** |
| Plugin/configuration only | Can configure roles/tools but cannot authoritatively add owner-approved task identity, cross-session dependency claims, or deterministic independent merge. | Insufficient alone; may complement it |
| Fork OpenCode | Carries session/provider/tool maintenance. | No, absent a concrete proved capability gap |
| Retain current runner | Already governs work but duplicates adapter/agent-loop concerns and does not test the OpenCode direction. | Keep unchanged as current system; not successor |

## Capability and ownership matrix

`Native` means documented upstream behaviour; `configurable` means use upstream
configuration; `extension/custom` means coordinator-owned; `unknown` means
not established by the cited evidence.

| Capability | Status | Owner / boundary |
| --- | --- | --- |
| Role configuration | configurable | OpenCode agents/config; coordinator records selected role |
| Sessions and context | native | OpenCode session/message/compaction; record IDs only |
| Tool execution | native | OpenCode tools and permissions; coordinator never reimplements loop |
| Provider/auth integration | native | OpenCode provider/auth; credential custody and rotation policy are owner/unknown |
| Structured results/errors | native + custom | SDK schema output; coordinator persists requested transition separately from transport/execution failure |
| Task dependencies | extension/custom | Governance store determines eligible/blocked tasks |
| Durable owner questions/approvals | extension/custom | Governance message/proposal records; upstream prompt is delivery channel |
| Review/merge | extension/custom | Independent critic, deterministic checks and serialized repository merger |
| Concurrency | configurable + custom | Coordinator slot/claims; upstream background-subagent limit is unknown |
| Restart recovery | extension/custom | Governance reconciliation, with upstream session/message lookup; stream semantics unknown |

## Minimal architecture and data flow

The TypeScript coordinator has five small components:

1. **Governance store** — initially SQLite is reasonable, but its schema need
   not copy `nc/state.py`. It is authoritative for project, owner-approved
   proposal, task, attempt, owner message, review and acceptance/merge state.
2. **Dispatcher** — transactionally claims an eligible task, allocates exactly
   one mutable worktree execution, binds role/repository/worktree/session, and
   invokes the SDK client.
3. **OpenCode adapter** — starts/connects to the headless server, creates or
   resumes sessions, sends prompts, reads structured output/status/messages and
   requests abort. It records external IDs and facts but does not infer success
   from an event.
4. **Acceptance coordinator** — checks the candidate commit, runs the configured
   commands, asks an independent critic against that same candidate commit, and
   serializes integration per repository.
5. **Owner interface** — narrow project/proposal/task/approval/inbox/status
   operations only. OpenCode CLI/TUI remains the session inspector.

Entities: `project`; `proposal` (owner approval evidence); `task` (objective,
dependencies, acceptance and phase); `execution_attempt` (claim token,
repository/worktree, role, candidate commit, upstream session/message IDs and
external-effect reconciliation fields); `owner_message` (question/answer,
delivery and correlation); and `review` (checks, critic identity/verdict and
candidate commit). Upstream owns transcripts and execution. Governance owns
approval, phase and acceptance truth: there are not two authoritative copies.

`owner -> approved proposal -> task/dependency readiness -> durable claim ->
attempt + worktree + OpenCode session -> structured worker request -> checks +
critic -> serialized merge -> durable accepted result`. Questions travel
`worker request -> owner_message -> owner answer -> resumed upstream message`.

## Authority and lifecycle

Only an owner approval turns a proposal into executable tasks. A worker's
validated structured result may request `waiting`, `review`, `blocked`, or
`rework`; it never approves itself. A session history, an idle event, a clean
process exit, or an upstream task completion does not establish acceptance.

```text
queued --dispatcher claim--> running --ASK--> waiting --owner answer--> queued/running
  |                         | result request                         |
  |                         +--> review --checks+critic--> accepted --merger--> accepted
  |                                              \--> rework --> queued
  +--owner cancel--> cancelled       --unresolved dependency/error--> blocked
```

Owner authorizes proposal approval, cancellation and answers. Dispatcher only
claims/releases. Worker requests transitions. Independent critic can request
rework/reject; acceptance coordinator records deterministic evidence. The
serialized merger, only after required passing review, authorizes the merge.
STOP/cancellation intent remains durable: stop dispatching, request abort where
safe, reconcile an in-flight attempt before any replacement; never blindly
replay a prompt or merge.

More precisely: dispatcher changes `queued -> running`; worker may only request
`running -> waiting/review/blocked`; owner answer permits resumption and owner
cancellation permits `cancelled`; the critic/acceptance coordinator can record
`review -> rework` or `blocked`; only passing commands plus required independent
review permit the merger to record `accepted`. This keeps acceptance authority
outside both the worker and OpenCode.

## Execution, acceptance, and recovery

1. In one durable transaction, select a dependency-ready approved task and
   write a unique claim/lease plus attempt. Exclude another live claim and one
   mutating execution per worktree.
2. Establish a worktree at a recorded base SHA, record role and session ID
   before/with dispatch, then prompt through the SDK. Persist message IDs,
   dispatch request key/time, status observations and candidate SHA when known.
3. Parse a schema-constrained worker result into a *transition request*; store
   it independently from HTTP/process/provider failure diagnostics. On ASK,
   store a durable owner question, move to waiting and release the execution
   slot. Answer delivery resumes the bound session with a correlated message.
4. For review, capture the candidate commit. Run project acceptance commands in
   its worktree and run an independent critic against the same immutable SHA.
   Neither worker nor critic merges.
5. Acquire a per-repository acceptance/merge lock. Re-read candidate and base;
   after conflicts, changed base, or changed candidate, rerun the relevant
   checks/critic rather than accepting stale evidence. Persist merge commit and
   acceptance decision atomically as far as Git/store reconciliation permits.

On restart, inspect incomplete attempts by claim, worktree/branch, session and
message IDs, recorded dispatch key, process/abort evidence, candidate/base SHA,
and review/merge IDs. Query upstream status/messages where available; reconcile
instead of resending. An interrupted call with unknown delivery is `uncertain`,
not permission to prompt again. Reconcile Git before merge; a discovered merge
commit is recorded after verification. Disconnected SSE is advisory only.

Worked examples (design traces, not a demand for an exhaustive new test suite):

* **Success:** claim T7 at base A; bind S9/M1; worker requests review at commit
  C; checks pass and independent critic passes C; merge lock observes base A,
  merges C to D and writes accepted(D).
* **ASK/resume:** S9 requests a database choice; coordinator records Q4 and
  changes T7 to waiting, releasing capacity. Owner answers A4; dispatcher
  records message M2 delivered to S9, reclaims T7, and later sends it to review.
* **Crash/uncertain execution:** process dies after outbound prompt intent but
  before response persistence. Restart sees claim, S9 and uncertain M1; it
  retrieves session/messages, records what occurred, and only resumes or
  abandons with an explicit reconciled decision—no duplicate prompt.
* **Critic rework:** C passes commands but critic identifies an unmet boundary;
  review records rework(C), task returns queued with fresh attempt evidence,
  while C remains inspectable and unmerged.

## Concurrency, permissions and credentials

Expose `execution_concurrency`, initially `1`, with no hardware-target claim,
fixed maximum, RAM budget or CPU certification. Durable claim uniqueness plus a
worktree mutation lock prevents duplicate mutation; repository acceptance/merge
is always serialized even if worker slots rise. A waiting owner question frees a
slot. Increase concurrency only after testing claims, worktree isolation and
recovery.

OpenCode's task source supports background/subagent work. The top-level slot
does not automatically bound that. Initially disable uncontrolled delegation in
roles/configuration, or route it through a documented, measured limiter before
counting it against the configured limit. This is deliberately an unresolved
integration check, not a guessed implementation.

Use narrowly scoped OpenCode permission rules (`allow`/`ask`/`deny`) and
repository/worktree isolation. They do not become an OS sandbox merely by
prompting or configuration; host sandboxing, network access and secret mounts
need explicit owner policy. OpenCode provider credentials belong to the owner
and OpenCode's supported auth path, not this store. Do not conflate provider
use with installed vendor CLI reuse, nor assume Codex subscription credentials
work through OpenCode or its ACP mode.

## Staged implementation

1. **Minimal session execution:** TypeScript package, one local headless
   OpenCode session, role/config selection, ID recording and CLI/TUI inspection.
2. **Governance and recovery:** SQLite proposals/tasks/claims/questions,
   structured transition requests, STOP/cancel reconciliation and restart cases.
3. **Independent review/merge:** candidate-bound checks, critic, repository lock
   and recheck-on-change merger.
4. **Owner pilot:** a separate small repository, configured concurrency one,
   explicit credentials/permissions, and observation before expansion.

Unexecuted smoke steps: install nothing here; owner later verifies a supported
OpenCode server/SDK can create, prompt, retrieve and abort a session; validates
provider authentication/error categories with non-production credentials;
disconnects/reconnects event consumption; verifies background delegation policy;
and kills/restarts during dispatch, ASK, review and merge reconciliation.

Open assumptions remain: exact supported release/version selection, event and
abort acknowledgement semantics, provider credential lifecycle, SDK error shape
across versions, and a safe evidenced subagent limiter. These are reasons for a
small smoke, not reasons to prebuild a fork or transport.
