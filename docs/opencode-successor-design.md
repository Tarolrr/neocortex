# OpenCode-based experimental successor

## Decision and evidence boundary

This is a bounded refinement of the T028/earlier planner proposal, not a new
architecture study or implementation plan. The baseline remains an external
OpenCode server and its TypeScript SDK (or documented server interface), with a
small TypeScript coordinator. The current Python runner, its `outcome.json`
protocol, historical artifacts, deployed services, credentials and queued or
blocked work are unchanged.

The coordinator adds governance; OpenCode remains the execution, context and
history owner. Git remains the authority for commits and refs. Observations are
from official documentation viewed 2026-09-14 (SDK page revision 2026-09-13);
unobserved runtime behaviour is **unknown**, not absent. The
[SDK](https://opencode.ai/docs/sdk/) documents a type-safe server client,
sessions, messages, prompt, abort, and SSE. The [CLI](https://opencode.ai/docs/cli/)
documents TUI, `attach`, `session`, and `export`; the [web UI](https://opencode.ai/docs/web/)
is an upstream UI surface. [Custom tools](https://opencode.ai/docs/custom-tools/)
are an extension mechanism. URLs are capability evidence, not a release lock,
security claim, or proof of delivery semantics.

## Ownership contract

Coordinator IDs are local opaque IDs. `coordinator_project_id` identifies its
governance record; it is neither OpenCode's project ID nor a directory. Store
canonical repository identity, worktree path/base/ref, and the OpenCode
server/store namespace (base URL or configured instance/store identity) with
every upstream session ID: a bare session ID is not interpretable across stores.

| Record | Authoritative writer and minimum durable data | References, not copied authority |
| --- | --- | --- |
| Project | Coordinator: project ID, repo identity, checks, owner policy | OpenCode project/directory and server namespace |
| Proposal | Coordinator: ID, text/version, owner approval identity/time | Planner session/message IDs; output is evidence, not approval |
| Task | Coordinator: approved proposal ID, objective, dependencies, phase, acceptance/boundaries | Project and current attempt/claim IDs |
| Execution attempt | Coordinator: immutable attempt/claim ID, role, worktree/base/candidate SHA, state, outbound intents | `(namespace, session ID, message ID)` and Git refs |
| Owner message | Coordinator: message/correlation ID, question/answer, delivery state/evidence | Bound attempt/session/message IDs |
| Review/merge evidence | Coordinator: immutable check-output digest, critic verdict/reviewer, candidate/base, decision; Git writes commit/ref facts | Git candidate/merge SHA and ref observations |

OpenCode owns session/context/message/tool history; Git owns commit/ref facts;
the coordinator owns approvals, dependency readiness, claims, owner delivery,
review and acceptance decisions. Persisted observations (timestamps, probes,
IDs, bounded errors, candidate SHA) and small immutable decision evidence are
not a second transcript or authoritative session copy. Optional exports are
diagnostic snapshots. Retain the coordinator DB and decision/diagnostic evidence
under normal owner backup policy, and retain/backup the OpenCode server/store
long enough to reconcile active attempts; this does not design a backup system.

## Reuse and deliberately dropped surfaces

Local inspection on 2026-09-14 of `nc/turn.py`, `nc/protocol.py`, `nc/roles.py`
and CLI/TUI-facing `nc/cli.py` found per-turn briefs/`outcome.json`, role
templates, durable inbox/outcomes, and owner governance commands. Keep their
governance intent, not their session implementation.

| Current/surface | Successor decision | Reason/boundary |
| --- | --- | --- |
| `turn.py` transcript `session.log` | Drop | OpenCode session history is authoritative. |
| Session browser/context management | Drop | Use OpenCode inspection/export and CLI/TUI/web, not a duplicate UI. |
| Per-turn memo as sole agent memory | Drop | Upstream session/context is agent memory; coordinator retains governance facts. |
| Agent-written `outcome.json` | Drop | Native schema-constrained SDK result is the sole result path. |
| `protocol.py` question/answer and `roles.py` role intent | Keep narrowly | Coordinator owns durable owner correspondence and role/task binding. |
| `nc/cli.py` approval, inbox/status/why/stop/cancel ideas | Keep narrowly | Governance operations and coordinator diagnostics only. |
| Acceptance command output; critic and merge evidence | Keep | Required candidate-bound evidence upstream history cannot replace. |

The task screen shows server namespace, session ID and configured directory; an
owner uses those with documented CLI/TUI/web session selection/attachment. No
unsupported task-to-session deep link is invented. Attachment is interactive and
can mutate a session, not guaranteed read-only inspection. Manual continuation
of managed work requires coordinator exclusion first and then reconciliation.

Plugins can host custom logic, so it is too strong to say they cannot implement
governance. Configuration alone does not establish durable cross-store authority.
Use a custom tool only if a concrete native-final-result gap is demonstrated; it
must still feed one authoritative result path with host-side identity/dedup.

## Native structured result contract

Default transport is SDK schema-constrained structured output, never an
agent-written file. The SDK documents the model-facing `StructuredOutput` tool,
`structured_output` result, bounded validation retries, and
`StructuredOutputError` when retries are exhausted. The host binds
role/task/attempt/session/message identity in its prompt, validates schema **and
semantics**, and persists a transition request. The agent supplies no authority.

The docs show `body.format` in the structured-output example but
`body.outputFormat` in the sessions API table. Exact invocation below is
pseudocode, not a selected-runtime claim. A later compatibility smoke chooses
the installed SDK generated type and verifies behaviour; version selection stays
later work.

```ts
// Pseudocode: use the release type and its field name (format vs outputFormat).
const ResultSchema = {
  type: "object", additionalProperties: false,
  required: ["task_id", "attempt_id", "session_id", "message_id",
             "result_correlation", "kind"],
  properties: {
    task_id: { type: "string" }, attempt_id: { type: "string" },
    session_id: { type: "string" }, message_id: { type: "string" },
    result_correlation: { type: "string" },
    kind: { enum: ["completion_for_review", "owner_question", "unfinished_progress"] },
    candidate_sha: { type: "string" }, evidence: { type: "array", items: { type: "string" } },
    question: { type: "string" }, question_correlation: { type: "string" },
    progress: { type: "string" }, blocker: { type: "string" }
  },
  allOf: [
    { if: { properties: { kind: { const: "completion_for_review" } } }, then: { required: ["candidate_sha", "evidence"] } },
    { if: { properties: { kind: { const: "owner_question" } } }, then: { required: ["question", "question_correlation"] } },
    { if: { properties: { kind: { const: "unfinished_progress" } } }, then: { required: ["progress"] } }
  ]
}
const reply = await client.session.prompt({ path: { id: sessionId }, body: {
  parts: [{ type: "text", text: hostEnvelope }],
  FORMAT_FIELD: { type: "json_schema", retryCount: 2, schema: ResultSchema }
}})
// Require structured_output and matching task/attempt/session/message identity;
// validate transition semantics and apply once by result-correlation ID.
```

For example, the returned `reply.structured_output` is respectively
`{task_id:"T7",attempt_id:"A3",session_id:"S9",message_id:"M12",result_correlation:"R1",kind:"completion_for_review",candidate_sha:"abc123",evidence:["pytest -q: pass"]}`,
`{task_id:"T7",attempt_id:"A3",session_id:"S9",message_id:"M12",result_correlation:"R2",kind:"owner_question",question:"Choose migration window?",question_correlation:"Q4"}`, or
`{task_id:"T7",attempt_id:"A3",session_id:"S9",message_id:"M12",result_correlation:"R3",kind:"unfinished_progress",progress:"migration started",blocker:"awaiting access"}`.
The host compares every echoed binding to its dispatch record, checks the SHA
and permitted state transition, and stores only the validated result request.

Planner and critic calls have narrower, role-specific schemas and explicit host
bindings: `ProposalSchema` requires `{project_id, proposal_id, planner_run_id,
tasks:[{key,title,dependencies,acceptance}]}`; `CriticVerdictSchema` requires
`{task_id, attempt_id, candidate_sha, verdict:"pass"|"rework"|"reject",
findings:[...]}`. The host supplies and compares the planner's project/proposal
scope and run ID, and the critic's task/attempt/candidate SHA, rejecting an
output that does not echo or match its scope; neither schema can create tasks,
approve, merge, or accept. Missing output, invalid schema/semantic identity,
`StructuredOutputError`, provider/transport failure, and failed/uncertain
execution are distinct. A syntactically valid payload never overrides failed or
uncertain execution, authorizes a task, merges, or accepts. Validation retries
are bounded; stale/duplicate result correlations are rejected.

## Lifecycle, review and integration

Only owner approval creates executable tasks. A worker result can request
waiting, review, blocked or progress; it cannot accept or merge. A passing
pre-merge check/critic verdict is **review evidence**, not acceptance.

```text
queued -> claimed/running -> waiting -> answer-intent-dispatched -> running
                         \-> review evidence -> merge lock -> integrated+verified -> accepted
review evidence --rework/failure--> queued or blocked
unsettled outbound/execution --> uncertain/blocked (no replacement)
```

For ASK, durably write answer intent and exclusion before dispatch; mark delivery
acknowledged only with evidence. Waiting releases capacity only after preceding
execution is settled. Review captures immutable candidate/base SHA, runs checks
and an independent critic against that candidate, then takes a serialized
repository merge lock. Revalidate candidate/base and rerun affected evidence on
change. Record `accepted` only after Git integration is verified and required
independent checks/review are recorded. STOP/cancel is durable, requests abort
where safe, and prevents replacement until settlement.

## Cross-store reconciliation

Do not assume atomic SQLite/OpenCode/Git transactions, API idempotency, or that
lease expiry proves execution stopped.

1. In one coordinator transaction create immutable claim/attempt and durable
   outbound intent with correlation key and `prepared` state; exclude concurrent
   worktree mutation.
2. Create a session or send prompt/owner answer, append namespace/session/message
   IDs and response facts, then mark intent observed/acknowledged. Record repeated
   observations idempotently by identity; apply a validated result at most once
   by attempt plus correlation.
3. On restart reconcile every nonterminal intent with namespace session/message
   inspection, worktree and Git refs. Before-session-create crash may create only
   after confirming no bound session. After-create-before-record crash is
   uncertain and searched by recorded correlation/metadata before human decision.
   Prompt or answer delivery uncertainty remains uncertain, never replayable.
4. A stale claim blocks replacement until session/Git/worktree evidence settles;
   expiry only starts reconciliation. Missing/deleted upstream sessions are
   unrecoverable evidence and block/require owner resolution, not silent
   recreation. If Git merge precedes DB recording, verify target ref/merge commit
   and required evidence, then append integration/acceptance; otherwise uncertain.

Where evidence cannot establish delivery or continued execution, retain explicit
`uncertain`/`blocked`, preserve claim/exclusion, and prohibit automatic replay
or replacement until reconciliation makes an owner/coordinator decision.

## Focused later smoke items

No installation, model call, registration or activation occurs here. Later smoke
work verifies only: selected SDK result/error retrieval
(`structured_output`/ `StructuredOutputError`), restart correlation across
create/prompt/answer crashes and duplicate observations, and session
inspection/control including attachment mutation and abort evidence. This is not
version certification or exhaustive audit. `pytest -q` and `ruff check .`
validate repository health only, not OpenCode deployability.
