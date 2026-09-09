# Exchange contract: current system and bounded revision

This is documentation only. **Current** is this branch's behavior; **proposed** is a later contract, not enforced behavior. It changes no brief, format, authority, or runtime behavior.

## Current durable flows

SQLite (`SCHEMA`/ `State`, `nc/state.py`) is durable truth. `runs/<agent>_<stamp>/outcome.json` is agent-owned, one object per execution, parsed by `protocol.read_outcome` in `turn.run_turn`, `run_planner_turn`, and `run_plan_critic_turn`. Its parsed fields are `outcome`, `summary`, `memo`, `to`, `question`, `verdict`, `findings`; raw planner `proposal` and plan-critic `recommendation` are role-validated later. Missing/malformed/non-object/unknown outcome becomes synthetic `NO_OUTCOME` in a `run`, not a message. Run files/logs are outside SQLite history.

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
| question | worker/critic `Scheduler._on_ask`, or planner `run_planner_turn`: sender -> owner -> `operations.answer_message`; escalation observes unanswered. | `{"question":"...","summary":"..."}` |
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
| Worker | parser accepts DONE/ASK/YIELD/FAIL/NO_OUTCOME; DONE is only claim, arbiter requires commits/checks then critic; no verdict | `run_turn` parses despite nonzero exit/timeout. Every non-FAIL, including NO_OUTCOME, acks rendered inbox. memo retained; ASK blocks; YIELD runnable; failure/no outcome increments attempts; turns vs budget | NO_OUTCOME may lose messages; exit conflicts with outcome | classify host before application; receipt then ack |
| Change critic | brief requires DONE + pass/rework/reject, summary/findings; arbiter alone merges/reworks/blocks | `build_brief` fetches critic inbox IDs, but CRITIC has no `inbox_section`; `run_turn` still acks unseen IDs, including NO_OUTCOME. Memo/turn updated; invalid verdict incident/retry | generic `OUTCOME_CONTRACT` permits DONE without verdict; invisible inbox silently delivered; generic exit policy | role schema wins; render only deliverable messages; receipt after arbiter application |
| Planner | DONE raw proposal must be 1--5 valid specs; ASK owner/question; YIELD protocol FAIL; cannot create/approve | nonzero/timeout fails before parse; brief renders inbox and DONE/ASK acks; memo, transactional feedback wake; capacity/work gates; no task attempts/budget | differing host/validation policy, generic JSON inbox | typed proposal/feedback + receipts, preserve gates |
| Plan critic | DONE requires string recommendation and list[string] findings; advisory only | nonzero/timeout fails; no inbox/memo; one-turn agent/run | pre-execution unique claim means crash/invalid unchanged spec cannot rerun | retryable logical review attempts; only completed advice unique |

Critics are role-specific work in one lifecycle: eligible -> claimed -> attempt -> advisory result -> arbiter/owner application. Change-critic advice has arbiter task effects; plan-critic advice informs owner only. Neither has owner/arbiter authority. Proposed one `(proposal,spec_hash)` may have deferred attempts 1..n but at most one completed advisory review.

## Recommendation: typed exchange, not a universal bus

Keep durable work, routed messages, attempts, incidents, outcome files, checks, and worktrees separate; not every table/file becomes a message. A common envelope is worthwhile only for new routed facts and durable application receipts. `role` is authority/brief; `kind` schema; `status` lifecycle; `outcome` attempt result; `delivered` inbox visibility; `applied` receipt; correlation joins records; `attempt_no` distinguishes executions. They are independent dimensions, not duplicate enums.

```json
{"id":"msg:481","schema":1,"kind":"feedback","sender":"owner","recipient":"planner:neocortex","sender_request_id":"owner-1","correlation":{"project_id":"neocortex","proposal_id":17},"payload":{"text":"Split migration","request":"plan"}}
```

```json
{"id":"attempt:902","work_id":"review:17:sha256:abc","role":"plan_critic","attempt_no":2,"status":"deferred","execution_id":"run:1441","reason":"host timeout"}
```

```json
{"id":"review:55","proposal_id":17,"spec_hash":"sha256:abc","kind":"plan_advice","status":"completed","findings":[],"recommendation":"Owner may inspect and decide"}
```

### Proposed validation, routing, retry, compatibility

Schema 1 requires nonempty id/kind/sender/recipient/sender_request_id, integer `schema=1`, valid correlation. Invalid JSON/type, unknown kind/version, absent required key, unauthorized route, or keys outside `extensions` creates protocol incident with no apply/ack. Required schemas/routes: feedback `text:string`, owner -> planner (optional request/project/task/proposal); question `question:string`, worker/planner/critic -> owner (summary/task optional); answer `answer:string`, owner -> referenced sender (reply correlation required); review verdict enum + `summary:string, findings:string[]`, critic/arbiter -> worker; incident `reason:string` plus task for task incidents, scheduler -> owner; cancellation `reason:string`, owner -> owner/history; plan advice recommendation string/findings string[], plan critic -> owner record with proposal/spec hash/attempt.

Order: validate -> route/authorize -> persist idempotently on `(sender,kind,sender_request_id)` -> atomically domain apply plus receipt -> ack. Retry returns receipt; it cannot repeat wake, merge, rework, supersession, or replacement. Mixed migration: schema-1 readers retain schema-0 legacy semantics; writers dual-write legacy until all readers upgrade; old readers ignore additive tables; unknown newer values quarantine undelivered.

Crash sequences: pre-commit leaves pending; post-commit/pre-response retry finds receipt then acks. Owner feedback receipt supersedes proposal and creates revision; correlated planner replacement is idempotent. Plan review attempt 1 may defer, attempt 2 crash/release expired claim, attempt 3 complete; later attempts see completed. Host ownership checks decide whether an active run can be reclaimed.

## Future migration; separate immediate fixes

Session-classification (consistent exit/timeout treatment) and local plan-review deferral are immediately actionable but separate; neither is implemented here. Envelope/runtime migration needs a later proposal.

This is an unexecuted stopped-system owner/agent runbook for that later proposal:

```sh
NC_DB="${NC_HOME:-/root/.neocortex}/state.db"
test -f "$NC_DB" && sqlite3 "$NC_DB" 'PRAGMA integrity_check; PRAGMA foreign_key_check;'
systemctl stop neocortex-scheduler neocortex-ui neocortex-backup
sqlite3 "$NC_DB" ".backup '${NC_DB}.pre-envelope-$(date -u +%Y%m%dT%H%M%SZ).sqlite'"
sqlite3 "$NC_DB" 'PRAGMA wal_checkpoint(TRUNCATE); PRAGMA integrity_check;'
sqlite3 "$NC_DB" 'SELECT "message",count(*) FROM message UNION ALL SELECT "run",count(*) FROM run UNION ALL SELECT "incident",count(*) FROM incident UNION ALL SELECT "proposal",count(*) FROM proposal UNION ALL SELECT "plan_review",count(*) FROM plan_review UNION ALL SELECT "proposal_revision",count(*) FROM proposal_revision;'
sqlite3 "$NC_DB" 'SELECT original_id,feedback_id,planner_id,replacement_id FROM proposal_revision ORDER BY original_id;'
```

In one `BEGIN IMMEDIATE` transaction, future additive DDL creates `exchange_envelope(id TEXT PRIMARY KEY,legacy_table TEXT,legacy_id INTEGER,schema INTEGER NOT NULL,correlation TEXT NOT NULL,UNIQUE(legacy_table,legacy_id))`, `exchange_receipt(envelope_id TEXT PRIMARY KEY,applied_at REAL NOT NULL,result TEXT NOT NULL)`, and `execution_attempt(id TEXT PRIMARY KEY,logical_work_id TEXT NOT NULL,attempt_no INTEGER NOT NULL,status TEXT NOT NULL,run_id INTEGER,UNIQUE(logical_work_id,attempt_no))`. Backfill schema-0 envelopes for legacy message/run IDs and review work/spec hashes, never rewriting legacy IDs, payloads, delivered flags, proposals, or revision links. Commit only after checks; otherwise rollback.

Restart gates: both PRAGMAs pass; six baseline counts and saved revision tuples are unchanged; envelope count equals chosen population; no duplicate legacy map. Enable dual read/write behind a flag, smoke-read, start services one at a time. Rollback: stop writers, disable flag, move failed DB aside, restore exact recorded backup with `sqlite3 "$NC_DB" ".restore 'BACKUP_PATH'"`, rerun PRAGMAs/counts, then restart. IDs/history are preserved; this document does not authorize migration.
