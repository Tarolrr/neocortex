# Exchange contract: current system and bounded revision

This is documentation only. **Current** is this branch's behavior; **proposed** is a later contract, not enforced behavior. It changes no brief, format, authority, or runtime behavior.

The investigated consequence of closing an interrupted current run is documented
in [Run 93](bugs/run-93-interrupted-claim.md).  In particular, current recovery
is an execution-record closure, not logical-work restoration.

## Current durable flows

SQLite (`SCHEMA`/ `State`, `nc/state.py`) is durable truth. `runs/<agent>_<stamp>/outcome.json` is agent-owned, one object per execution, parsed by `protocol.read_outcome` in `turn.run_turn`, `run_planner_turn`, and `run_plan_critic_turn`. Its parsed fields are `outcome`, `summary`, `memo`, `to`, `question`, `verdict`, `findings`; raw planner `proposal` and plan-critic `recommendation` are role-validated later. Missing/malformed/non-object/unknown outcome becomes synthetic `NO_OUTCOME` in a `run`, not a message. Run files/logs are outside SQLite history.

### Payload fidelity in current handoffs

`protocol.read_outcome` preserves complete parsed `summary`, `memo`, `question`,
and findings in their original list order. This is necessary because `raw` and
session logs are diagnostic evidence, not the durable source a later recipient
reads. `Scheduler._apply_verdict` writes the complete critic summary/findings to
a `review_verdict` through `State.send`; `_rework` does the same for arbiter
findings. `State.send` stores the full Unicode JSON payload with its existing
message ID, route, order, and delivery flag. Worker briefs render those verdicts
and generic inbox payloads in full; generic payload rendering is JSON inline
with `ensure_ascii=False`, so Unicode is not escaped and embedded newlines remain
represented in the JSON value. Memo sections retain their full stored text.
Planner briefs retain full durable feedback/revision records and render their
decoded string content verbatim as well, so embedded newlines are actionable
text rather than JSON escape sequences. Plan-critic findings/recommendation
persist directly in `plan_review`.

The short text slices used in scheduler incidents and host diagnostics remain
bounded display/diagnostic fields; they are not the only source of actionable
content. Explicit transport rejection protections are likewise unchanged.
Content discarded by older versions cannot be reconstructed from historical
rows or logs; this behavior preserves newly processed payloads and does not
rewrite history. Host failures still retain the prior memo and undelivered inbox
rather than treating a partial or failed session as delivery.

| Record | Producer -> consumer | Ownership and lifecycle |
| --- | --- | --- |
| `message` | `State.send`/`planner_feedback`/`cancel_task`, scheduler and owner operations -> briefs, owner inbox, handlers | State owns id, sender/recipient/task/reply link, JSON payload, `delivered`, timestamp. Delivery means inbox consumption only, not domain application. |
| task / agent | owner/import/proposal approval -> scheduler -> role briefs | Task is persistent logical work: objective, acceptance/boundaries/dependencies, status, attempts, budget, result. Agent is persistent role incarnation: role/project/task, eligibility, model, turns, memo. |
| `run` | each `run_*_turn` -> recovery/operator | One execution attempt, with role/model/outcome/detail/tokens/log and host/adapter owner identities. `ended_at IS NULL` guards active runs; `record_adapter_owner` precedes waiting and recovery does not assume ambiguous sessions died. |
| `incident` | scheduler/recovery/host checks -> owner | Operational kind/detail/created record, separate from routed incident. `resolve_incident` sets resolved, `resolved_at`, and first `resolution_note`; `resolve_open_incidents` does likewise for all open rows. |
| proposal / `plan_review` / `proposal_revision` | planner -> plan critic -> owner; owner feedback -> planner | Proposal is owner decision candidate; review is advisory evidence; revision is durable replacement lineage. |

### State and proposal lifecycles

Task schema declares `queued|in_progress|in_review|done|failed|blocked`; live `State.cancel_task` also writes `cancelled`. `Scheduler._spawn_for_queued_task` moves queued -> in_progress. `_on_ask` blocks; `operations.answer_message` restores in_progress; `_on_done` makes passed worker work in_review; critic application makes done, blocked, or in_progress. `_on_fail` increments attempts and eventually blocks. Owner recovery/requeue also moves blocked work to in_progress or queued and resets attempts. Cancellation is permitted only queued/blocked/failed with no active run and makes task agents done. `failed` is declared but not the ordinary scheduler terminal write.

Agent values are `runnable|waiting|blocked|done|failed`. Planner gates in `Scheduler.pick` set waiting/runnable; planner DONE is done unless a revision remains, ASK is blocked. `turns` counts executions; `budget_turns` caps them; `attempts` counts failure/rework cycles: independent values.

Proposal status is `pending|approved|rejected|superseded`: planner creates pending, owner approval creates tasks and approves, rejection records `decided_at`/reason, feedback supersedes. `plan_review` is `running` default, `done`, or `failed`, unique on `(proposal_id,spec)` and claimed before execution. `proposal_revision` has original ID, unique feedback ID, planner ID, nullable unique replacement ID.

### Declared versus actual messages

`nc.protocol` declares `feedback`, `question`, `answer`, `review_request`, `review_verdict`, `incident`. No database CHECK or payload validation exists. Live `cancellation` is undeclared: `State.cancel_task` writes it.

| Kind | Exact producer -> route -> consumer/application | Exact live payload |
| --- | --- | --- |
| feedback | `State.planner_feedback`: owner -> `planner-<project>` -> `build_planner_brief`; DONE/ASK marks delivered. Task feedback still targets planner. | `{"text":"..."}`; plan request adds `"request":"plan"`. |
| question | Planner `run_planner_turn` accepts only `to:"owner"`, so planner -> owner -> `operations.answer_message`. Worker/critic `Scheduler._on_ask` maps every `to` *except* literal `"worker"`/`"critic"` to owner. Those two literal values are stored as the recipient unchanged; they are labels, not agent IDs, and have no answering/inbox route. `operations.inbox`/`project_owner_questions` and `answerable_question` consider only owner-recipient questions; `answer_message` therefore cannot answer a literal-recipient question. `_escalate_unanswered_questions` still creates an `ask_timeout` operational incident for any blocked sender with no answer (it does not make that question owner-visible or answerable). | `{"question":"...","summary":"..."}` |
| answer | `operations.answer_message`: owner -> original sender, `in_reply_to` question -> worker inbox/planner brief; transaction delivers question and wakes sender. | `{"answer":"..."}` |
| review_verdict | critic `_apply_verdict` -> worker; arbiter `_rework` -> worker -> inbox. Arbiter, not delivery, applies acceptance. | `{"verdict":"pass|rework|reject","summary":"...","findings":["..."]}` |
| incident | `_block`: scheduler -> owner alongside `State.incident`; owner inbox/history consumes it. | `{"task":"<id>","reason":"..."}` |
| cancellation | `State.cancel_task`: owner -> owner, task-scoped -> history/owner inspection, no role brief. | `{"reason":"..."}` |
| review_request | no producer or consumer path | declared only |

`Outcome.verdict` and a `review_verdict` message differ; `NO_OUTCOME` is only a run result. Owner replacement trace: `planner_feedback` atomically stores feedback, supersedes the original proposal, wakes/creates planner, and inserts `proposal_revision(original_id,feedback_id,planner_id)`. `build_planner_brief` supplies pending revision; planner DONE -> `add_proposal(... revision_id=original)` creates replacement and fills `replacement_id`. Original/review/feedback rows remain.

## Role matrix: current, defects, proposed rules

Ack means `message.delivered`, not application receipt.

| Role | Current accepted outcome/payload; authority | Host, inbox, memo, wake, attempts/budget | Defect | Proposed rule |
| --- | --- | --- | --- | --- |
| Worker | parser accepts DONE/ASK/YIELD/FAIL/NO_OUTCOME; DONE is only claim, arbiter requires commits/checks then critic; no verdict | `run_turn` parses despite nonzero exit/timeout. Every non-FAIL, including NO_OUTCOME, acks rendered inbox. memo retained; ASK blocks; YIELD runnable; failure/no outcome increments attempts; turns vs budget | `ASK to:"worker"` or `"critic"` is persisted to that literal label, not an agent: no owner inbox or answer path. It leaves the sender/task blocked; timeout may create only an operational incident. NO_OUTCOME may lose messages; exit conflicts with outcome | accept questions only to owner until an explicit agent-ID route exists; classify host before application; receipt then ack |
| Change critic | brief requires DONE + pass/rework/reject, summary/findings; arbiter alone merges/reworks/blocks | `build_brief` fetches critic inbox IDs, but CRITIC has no `inbox_section`; `run_turn` still acks unseen IDs, including NO_OUTCOME. Memo/turn updated; invalid verdict incident/retry | generic `OUTCOME_CONTRACT` permits DONE without verdict; invisible inbox silently delivered; `ASK to:"worker"`/`"critic"` is likewise stranded rather than peer-routed or owner-routed; generic exit policy | role schema wins; render only deliverable messages; until peer routing exists, accept questions only to owner; receipt after arbiter application |
| Planner | DONE raw proposal must be 1--5 valid specs; ASK owner/question; YIELD protocol FAIL; cannot create/approve | nonzero/timeout fails before parse; brief renders inbox and DONE/ASK acks; memo, transactional feedback wake; capacity/work gates; no task attempts/budget | differing host/validation policy, generic JSON inbox | typed proposal/feedback + receipts, preserve gates |
| Plan critic | DONE requires string recommendation and list[string] findings; advisory only | `run_plan_critic_turn` parses an available outcome without testing nonzero exit/timeout; therefore valid DONE advice can mark the review done after either host condition. No inbox/memo; one-turn agent/run | Host checks are inconsistent: only planner rejects nonzero/timeout before parse; generic worker/change-critic and plan critic apply an available outcome (generic only annotates timeout when it is NO_OUTCOME). Pre-execution unique claim means crash/invalid unchanged spec cannot rerun | classify host result before outcome application for every role; retryable logical review attempts; only completed advice unique |

Critics are role-specific work in one lifecycle: eligible -> claimed -> attempt -> advisory result -> arbiter/owner application. Change-critic advice has arbiter task effects; plan-critic advice informs owner only. Neither has owner/arbiter authority. Proposed one `(proposal,spec_hash)` may have deferred attempts 1..n but at most one completed advisory review.

## Recommendation: typed exchange, not a universal bus

Keep durable work, routed messages, attempts, incidents, outcome files, checks, and worktrees separate; not every table/file becomes a message. A common envelope is worthwhile only for new routed facts and durable application receipts. `role` is authority/brief; `kind` schema; `status` lifecycle; `outcome` attempt result; `delivered` inbox visibility; `applied` receipt; correlation joins records; `attempt_no` distinguishes executions. They are independent dimensions, not duplicate enums.

```json
{"id":"msg:481","schema":1,"kind":"feedback","sender":"owner","recipient":"planner:neocortex","sender_request_id":"owner-1","correlation":{"project_id":"neocortex","proposal_id":17},"payload":{"text":"Split migration","request":"plan"}}
```

```json
{"id":"attempt:902","work_id":"review:17:sha256:abc","role":"plan_critic","attempt_no":2,"status":"retryable","execution_id":"run:1441","reason":"adapter_terminal:provider_rate_limited"}
```

```json
{"id":"review:55","proposal_id":17,"spec_hash":"sha256:abc","kind":"plan_advice","status":"completed","findings":[],"recommendation":"Owner may inspect and decide"}
```

### Proposed validation, routing, retry, compatibility

Schema 1 requires nonempty id/kind/sender/recipient/sender_request_id, integer `schema=1`, valid correlation. Invalid JSON/type, unknown kind/version, absent required key, unauthorized route, or keys outside `extensions` creates protocol incident with no apply/ack. Required schemas/routes: feedback `text:string`, owner -> planner (optional request/project/task/proposal); question `question:string`, worker/planner/critic -> owner (summary/task optional; peer labels are rejected until real agent-ID routing is introduced); answer `answer:string`, owner -> referenced sender (reply correlation required); review verdict enum + `summary:string, findings:string[]`, critic/arbiter -> worker; incident `reason:string` plus task for task incidents, scheduler -> owner; cancellation `reason:string`, owner -> owner/history; plan advice recommendation string/findings string[], plan critic -> owner record with proposal/spec hash/attempt.

Order: validate -> route/authorize -> persist idempotently on `(sender,kind,sender_request_id)` -> atomically domain apply plus receipt -> ack. Retry returns receipt; it cannot repeat wake, merge, rework, supersession, or replacement. Mixed migration: schema-1 readers retain schema-0 legacy semantics; writers dual-write legacy until all readers upgrade; old readers ignore additive tables; unknown newer values quarantine undelivered.

Crash sequences: pre-commit leaves pending; post-commit/pre-response retry finds receipt then acks. Owner feedback receipt supersedes proposal and creates revision; correlated planner replacement is idempotent. A plan review may become retryable only on terminal adapter-specific temporary-provider evidence; a later attempt may crash and release its expired claim, and a subsequent attempt may complete; later attempts see completed. A host timeout is instead interrupted/retryable after positive ownership proof, or actionably blocked if ownership is ambiguous. Host ownership checks decide whether an active run can be reclaimed.

## Proposed interruption-recovery contract (run 93)

**Proposed, not current implementation.**  This contract fixes the separation
between a durable logical claim and an execution attempt without weakening STOP,
cancellation, owner blocks, arbiter-only acceptance, or process ownership.

### Definitions and required transaction

Logical work is the durable task, planner revision, or `(proposal, spec-hash)`
review.  An attempt is a fenced execution of that work.  A run is its host
observation.  Closing a run records an attempt fact; stopping a process is an
ownership action; restoring eligibility changes logical state; accepting work is
the later arbiter/owner domain action.  None implies another.

Every claim has `work_id`, phase, `attempt_no`, monotonic fence token, status
(`claimed|running|result_persisted|applied|acknowledged|interrupted|retryable|
blocked|cancelled`), and a durable recovery reason/owner action where blocked.
`interrupted` is terminal for an execution attempt; `retryable` is a logical-work
state, not a state to which a closed attempt is reopened. Claim,
agent eligibility and active-fence creation commit together.  Result persistence,
role-specific application, receipt and acknowledgement are separately
idempotent transactions keyed by `(work_id, attempt_no, fence)`.  A completion
or acknowledgement with a stale fence is rejected and recorded; it can never
alter memo, inbox, task/review/proposal state, budgets, or acceptance.

**Normative budget boundary.** Every role has a turn budget.  Charge exactly
one turn, exactly once, in the same transaction that records successful adapter
launch/ownership and changes its attempt from `claimed` to `running`.  Do not
charge a merely claimed or pre-launch attempt.  A launched attempt retains that
charge if it is interrupted, cancelled after launch, provider-retryable, or
later rejected; each separately launched retry charges one more turn.  Recovery,
restart reconciliation, duplicate submissions and stale completions never
charge or refund a turn.  This rule applies uniformly to workers, change
critics, planners and plan critics; `attempt_no`/failure history are preserved
and are not budget counters.  Only an explicit, audited owner action may add
budget capacity; it records the amount and reason and neither resets spent
turns nor alters prior attempts.

Ownership proof is a matching scheduler identity plus the dedicated adapter
cgroup/process identity observed empty or otherwise positively quiescent.  A
missing parent PID alone proves nothing.  Live or ambiguous descendants remain
blocked with an owner-visible action to inspect/stop under lifecycle authority;
recovery never bypasses them.  All recovery, claim, apply and owner lifecycle
operations take the lifecycle exclusion, re-read state, and use an immediate
transaction.  Repeating recovery finds the existing recovery receipt and is a
no-op: no new attempt, message, wake, or budget charge.

### Run-93 recovery sequence

1. Stop scheduling/claiming under lifecycle exclusion; inspect run 93 and prove
   or retain ambiguity of scheduler and adapter ownership.  Do not classify the
   host timeout as a provider deferral.
2. Atomically mark its run/attempt interrupted, retain log path, partial
   worktree, memo, inbox, revision lineage, usage/history and role phase, and
   release only its matching fence after ownership proof.
3. If STOP, cancellation, an owner block, or ambiguous descendants apply, write
   logical `blocked` with that durable reason and exact owner recovery path;
   do not wake it.  Deliberate cancellation remains cancelled and STOP remains
   stopped.
4. Otherwise leave T008's **interrupted existing attempt** terminally
   `interrupted`; atomically set its logical work to `retryable`, retain its
   phase and partial state, clear only that attempt's active fence, and make
   exactly its worker eligible for a retry claim. Do not create a next attempt
   here, reset attempts/budget, or discard its branch.
5. The scheduler, not recovery, may select that retryable work. In one separate
   claim transaction it rechecks STOP/cancellation/owner-block and eligibility,
   creates the next attempt and its new fence, and changes the worker to claimed
   or running. A failed recheck leaves it retryable or records the specified
   actionable block; it never creates a second active attempt.
6. On the next completion, persist result first, then apply it once (arbiter for
   worker acceptance), write receipt, then acknowledge rendered inbox.  Restart
   reconciliation completes any missing later idempotent step; it never reruns
   an accepted result.

The recovery and scheduler transitions are deliberately separate and each is
idempotent. `retryable` has no active fence and is eligible but unclaimed;
`claimed`/`running` has exactly one new active fence. Recovery never creates a
future attempt, and the scheduler never closes the interrupted attempt.

| Actor / boundary | prior durable state | one transaction writes | resulting eligibility / fence |
| --- | --- | --- | --- |
| recovery after positive quiescence | attempt N `running`, logical work in its existing phase | mark run and attempt N `interrupted`; preserve state; release fence N; logical work `retryable` | exact role eligible; no active fence; no attempt N+1 |
| recovery with STOP/cancel/owner block/ambiguous survivor | attempt N nonterminal | close only if ownership allows; otherwise retain evidence; write or retain durable block reason/action | not eligible; no active fence only after positive quiescence |
| scheduler claim | `retryable`, no active fence, role eligible | recheck exclusion and logical state; create attempt N+1/fence N+1; mark claim | one active fence, one claimed/running role |
| duplicate recovery or scheduler race | recovery receipt or active fence exists | find receipt / conditional claim fails | no new attempt, wake, or budget charge/refund |

| Event/role | durable logical work and partial state | eligibility / retry | feedback and budget |
| --- | --- | --- | --- |
| worker interrupted | preserve branch, memo, undelivered inbox and phase | retry existing phase after ownership proof; otherwise actionable block | a running attempt has already charged one turn; a retry charges only at its own launch; no reset |
| change critic interrupted | preserve worker's review phase and critic memo/inbox | release unique review claim; one next critic attempt, never concurrent | same launch-only charge; no duplicated verdict/rework/acceptance |
| planner interrupted | preserve proposal, feedback, revision lineage and planner memo | retry planner phase; do not recreate/supersede proposal | same launch-only charge; feedback receipt prevents duplicate wake/revision |
| plan critic interrupted | preserve proposal/spec and advisory history | release recoverable unique claim; only one completed advisory result | same launch-only charge; no repeated completed advice; owner still decides |

Thus a crash before launch consumes no turn; a crash during execution consumes
the one launch charge, and recovery never resets turns/attempts.  A provider
retry classification requires terminal,
adapter-specific evidence of a temporary provider category; host death,
timeout, SIGTERM, local launcher failure, and unknown evidence are not that.
This remains compatible with T008's host classification and T009's local
provider retry without duplicating either implementation.

### Restart reconciliation and acceptance tests

On startup, enumerate nonterminal fences and reconcile each in one transaction:
unlaunched claim -> terminal interrupted attempt plus logical work retryable;
positively quiesced running attempt -> terminal interrupted attempt plus logical
work retryable; ambiguous/live
ownership -> actionable block; result-persisted -> apply once; applied ->
acknowledge once.  STOP/cancelled/owner-blocked states win every
branch.  Validate injected crashes before launch, during execution, between
persist/apply/ack, duplicate recovery submission, stale completion, process
survivor, each of the four roles, and restart after every boundary.  Assertions:
no two live fences for work, no stranded `in_progress`/`blocked` without reason
and action, no replay of accepted/advisory result, preserved memo/inbox/revision
and worktree, and deterministic budget/history.

Migration requires an owner-approved stopped-system backup and additive
attempt/fence/receipt records.  Dual-read legacy rows as explicitly
`ownership-unknown`; do not silently auto-recover them.  Rollback disables the
new claimant before restoring the recorded backup; it must not erase historical
run/receipt evidence.  This is the only migration/rollback change required by
this revised contract.

## Future migration; separate immediate fixes

Session-classification (consistent exit/timeout treatment) and local plan-review deferral are immediately actionable but separate; neither is implemented here. Envelope/runtime migration needs a later proposal.

This is an unexecuted stopped-system owner/agent runbook for that later proposal.  It
must be run by the deployment owner (root, or an account with passwordless/suitable
`sudo` authority for these units and write access to `NC_DB`) during a maintenance
window.  Stop both timers *and* any already-started services before touching the
database: stopping a timer prevents the next activation but does not stop a service
that it has already activated.

```sh
NC_DB="${NC_HOME:-/root/.neocortex}/state.db"
test -f "$NC_DB" && sqlite3 "$NC_DB" 'PRAGMA integrity_check; PRAGMA foreign_key_check;'
sudo systemctl stop neocortex.timer neocortex-backup.timer
sudo systemctl stop neocortex.service neocortex-backup.service
sqlite3 "$NC_DB" ".backup '${NC_DB}.pre-envelope-$(date -u +%Y%m%dT%H%M%SZ).sqlite'"
sqlite3 "$NC_DB" 'PRAGMA wal_checkpoint(TRUNCATE); PRAGMA integrity_check;'
sqlite3 "$NC_DB" 'SELECT "message",count(*) FROM message UNION ALL SELECT "run",count(*) FROM run UNION ALL SELECT "incident",count(*) FROM incident UNION ALL SELECT "proposal",count(*) FROM proposal UNION ALL SELECT "plan_review",count(*) FROM plan_review UNION ALL SELECT "proposal_revision",count(*) FROM proposal_revision;'
sqlite3 "$NC_DB" 'SELECT original_id,feedback_id,planner_id,replacement_id FROM proposal_revision ORDER BY original_id;'
```

In one `BEGIN IMMEDIATE` transaction, future additive DDL creates `exchange_envelope(id TEXT PRIMARY KEY,legacy_table TEXT,legacy_id INTEGER,schema INTEGER NOT NULL,correlation TEXT NOT NULL,UNIQUE(legacy_table,legacy_id))`, `exchange_receipt(envelope_id TEXT PRIMARY KEY,applied_at REAL NOT NULL,result TEXT NOT NULL)`, and `execution_attempt(id TEXT PRIMARY KEY,logical_work_id TEXT NOT NULL,attempt_no INTEGER NOT NULL,status TEXT NOT NULL,run_id INTEGER,UNIQUE(logical_work_id,attempt_no))`. Backfill schema-0 envelopes only for legacy routed message/fact rows (initially `message` IDs); run records stay runs, and plan-review work stays in its dedicated logical-work/review records, linked by correlation when a routed fact refers to either. Never rewrite legacy IDs, payloads, delivered flags, proposals, or revision links. Commit only after checks; otherwise rollback.

Restart gates: both PRAGMAs pass; six baseline counts and saved revision tuples are unchanged; envelope count equals the chosen routed-message/fact population; no duplicate legacy map. Enable dual read/write behind a flag, smoke-read, then resume normal scheduling with `sudo systemctl start neocortex.service neocortex-backup.service` followed by `sudo systemctl start neocortex.timer neocortex-backup.timer`. Rollback: stop the same timers and services, disable flag, move failed DB aside, restore exact recorded backup with `sqlite3 "$NC_DB" ".restore 'BACKUP_PATH'"`, rerun PRAGMAs/counts, then start those same services followed by timers. IDs/history are preserved; this document does not authorize migration.
