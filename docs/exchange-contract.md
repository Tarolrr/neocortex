# Exchange contract: current system and bounded revision

This is documentation only. **Current** describes this branch; **proposed** is a later-adoption contract, not enforced behavior. It does not alter briefs, formats, or owner authority.

## Current inventory

SQLite is durable truth (`State` and `SCHEMA` in `nc/state.py`). Run files are attempt evidence, not a durable bus.

| Item | Producers -> consumers | Payload, owner, lifecycle |
| --- | --- | --- |
| `runs/<agent>_<stamp>/outcome.json` | Every role writes; `protocol.read_outcome`, called from `turn.run_turn`, `run_planner_turn`, and `run_plan_critic_turn`, reads. | Agent-owned, one JSON object per execution: `outcome`, `summary`, `memo`, `to`, `question`, `verdict`, `findings`, raw extras. Missing/malformed/non-object/unknown is synthetic `NO_OUTCOME`, stored on `run` and handled by scheduler. Logs/files are outside SQLite snapshots. |
| `message` | `State.send`, `State.planner_feedback`, scheduler -> `_inbox_lines`/`build_planner_brief`; `mark_delivered` acks. | State-owned JSON payload plus sender/recipient/task/reply link. `delivered` is inbox-consumed, not business-effect-applied. |
| task/agent | Owner/import/proposal approval creates task; `_spawn_for_queued_task` creates/wakes worker; scheduler transitions. | Task owns objective, acceptance/boundaries, status, attempts, budget; agent is persistent role incarnation with state, turns, memo. |
| `run` | All `run_*_turn` functions call `start_run`, ownership recording, `end_run`; operator consumes. | One execution attempt: role/model/outcome/detail/tokens/log/ownership. `ended_at IS NULL` is an active-run guard, not task state. |
| `incident` | Scheduler/recovery/host checks -> `State.incident`; owner resolves. | Separate owner-visible operational record. `_block` also sends an `INCIDENT` message. |
| proposal/`plan_review`/`proposal_revision` | Planner DONE -> `add_proposal`; plan critic writes review; owner decides; feedback -> `planner_feedback`. | Proposal stores source/rationale/spec/findings/status; review snapshot/findings/recommendation; revision original/feedback/planner/replacement lineage. |

Owner revision trace: `State.planner_feedback` atomically inserts planner `feedback`, supersedes the pending original, wakes/creates planner, and inserts `proposal_revision(original_id, feedback_id, planner_id)`. `build_planner_brief` reads `pending_revision`; planner DONE calls `add_proposal(... revision_id=original_id)`, which creates the replacement and fills `replacement_id`. Original spec, advisory review, and feedback remain.

### Declared message kinds versus actual paths

`nc.protocol` declares `feedback`, `question`, `answer`, `review_request`, `review_verdict`, `incident`. The `message` comment echoes these but no database CHECK or payload validation enforces them.

| Kind | Current producer | Consumer/application |
| --- | --- | --- |
| feedback | `State.planner_feedback` from `nc feedback`/`nc plan`; revision feedback too. | Planner brief. Task-scoped feedback still targets planner, not worker. |
| question | `Scheduler._on_ask`; separate planner path in `run_planner_turn`. | Owner inbox; answer operation replies. `_escalate_unanswered_questions` observes it. |
| answer | Owner answer operation, replying to question. | Wakes sender; `_inbox_lines` formats it. |
| review_verdict | `Scheduler._rework`. | Worker brief. This is distinct from critic outcome verdict. |
| incident | `Scheduler._block`. | Owner inbox; `incident` table remains operational truth. |
| review_request | None. | Declared only. |

Critic work is scheduler-created after checks (`_on_done`) and its outcome is consumed by `_apply_verdict`; it is not a review-request/message pair. `NO_OUTCOME` is not a message. Current defect: `run_turn` marks inbox IDs delivered for NO_OUTCOME because it excludes only FAIL, then `_on_fail` counts a failed task attempt.

## Role matrix

“Ack” means current `message.delivered`, not an application receipt.

| Role | Current accepted outcome/payload and domain authority | Host, ack, memo, wake, attempts/budgets | Defect | Proposed rule |
| --- | --- | --- | --- | --- |
| Worker | Generic DONE requires commits; arbiter then checks/reviews. ASK/YIELD/FAIL/NO_OUTCOME accepted. No verdict. | `run_turn` parses output without requiring successful adapter exit. Non-FAIL acks inbox; memo copied. ASK blocks task/agent; YIELD runnable; failure increments task attempts; `turns` checked against `budget_turns`. | Generic DONE resembles acceptance; nonzero exit may coexist with outcome; NO_OUTCOME acknowledged. | Classify host success before apply; acknowledge only after durable application. |
| Change critic | DONE must include `pass|rework|reject`; arbiter alone merges, blocks, reworks. | Same wrapper/ack/memo/turn budget. Invalid verdict creates protocol incident and critic retry. | Generic `OUTCOME_CONTRACT` permits DONE without verdict while `roles.CRITIC` requires it. Exit check differs from planner. | Role schema; arbiter idempotently applies advisory review. |
| Planner | DONE requires 1–5 validated proposal specs; ASK only owner; YIELD protocol failure. Cannot create tasks. | Explicit nonzero-exit/timeout failure. Ack only DONE/ASK; concurrent wake protected by `updated_at`; done/blocked. Work/pending-review/capacity gates control eligibility. | Different host-exit policy; untyped planner inbox. | Typed planner request/proposal; retain gates as scheduling policy. |
| Plan critic | DONE + string recommendation + string findings; advisory only, no verdict/replacement. | Explicit exit check; no inbox/memo/task budget; one-turn agent done. | Pre-execution unique `(proposal_id,spec)` claim means crash/invalid output prevents another unchanged-spec review. | Logical review can have retries; only completed advice is unique. |

`task.attempts`, `agent.turns`, `budget_turns`, task status, agent state, and run outcome are independent: failure/rework cycles, executions, limit, work progress, eligibility, one-process result. `delivered`, `in_reply_to`, proposal status, role, kind, and outcome are also independent dimensions, not duplicate enums.

## Bounded recommendation

Do not build a universal bus or message-wrap every table/file. Existing durable aggregates already fit: work (`task`/`agent`), routing (`message`), execution evidence (`run`), operations (`incident`), proposal lineage. A common envelope earns complexity only for typed routed facts; logs, checks, worktrees, task rows, and outcome files remain artifacts.

```json
{"id":"msg:481","kind":"feedback","schema":1,"sender":"owner","recipient":"planner-neocortex","correlation":{"project_id":"neocortex","proposal_id":17},"payload":{"text":"Split migration"}}
```

```json
{"id":"attempt:902","work_id":"task:neocortex-T007","role":"worker","attempt_no":2,"execution_id":"run:1441","result":{"kind":"DONE","summary":"document added","memo":"checks passed"}}
```

```json
{"id":"review:55","proposal_id":17,"spec_hash":"sha256:...","kind":"plan_advice","status":"completed","findings":[],"recommendation":"Owner may inspect and decide"}
```

Identity is immutable per message/run/review; correlation names task/proposal/reply/execution. `kind` chooses payload schema; `role` controls brief/authority; `status` is lifecycle; `outcome` is attempt result.

Proposed order: validate route/version then payload; persist with idempotency `(sender,kind,sender_request_id)`; claim, apply transition, and write application receipt atomically; then ack. Retry returns the stored result and cannot repeat merge/wake/replacement. Pre-commit crash stays pending; post-commit is applied; expired claims may be released. Recovery retains cautious ownership checks and does not assume an ambiguous session died.

Sequence: worker DONE ends a run; arbiter validates commits/checks and creates change-review work; completed critic advice is applied once (pass -> arbiter merge/task done; rework -> worker verdict+wake; reject -> block+incident). Proposal owner feedback first applies supersession+revision link; planner emits correlated replacement. A plan critic may have deferred, failed, then completed attempts, but only one completed advisory review per proposal/spec hash. Critics are role-specific work in the same lifecycle—eligible, claimed, attempted, completed advice—never owner or arbiter.

## Future migration; separate immediate fixes

First separately deliver immediately actionable host-session classification and local deferral work: consistent exit/timeout treatment before outcome parsing, and retryable deferred plan-review representation. This task implements neither. Any contract/runtime migration needs a later proposal.

Future staged migration: (1) add nullable/additive envelope/application and execution-attempt tables/indexes behind feature-flag dual write/read; (2) stopped-system manual procedure: stop scheduler/UI/backup writers, make verified SQLite backup, run `PRAGMA integrity_check`/`foreign_key_check`, transactionally backfill legacy primary IDs as correlations and spec hashes, mark history schema-0/applied without inventing acks; (3) verify counts/IDs for message, run, incident, proposal, plan_review, proposal_revision and sample replacement lineage before dual-read; (4) rollback by stopping writers, disabling flag and restoring backup if needed. Additive data can remain unused; IDs/history are never rewritten/deleted. This is feasible owner/agent procedure, not authorization to execute it: a future proposal must define exact DDL, compatibility window, rollback test, and authority boundaries.
