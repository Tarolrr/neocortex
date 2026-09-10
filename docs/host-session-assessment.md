# Host-session assessment

Retrieved 2026-09-09. This is an operational taxonomy for the runner, not a
provider retry policy and not an agent outcome protocol.

Primary sources (retrieved 2026-09-09): [OpenAI API error
codes](https://developers.openai.com/api/docs/guides/error-codes),
[ChatGPT/Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode),
[the publisher's Codex 0.86.0 distribution metadata](https://registry.npmjs.org/@openai/codex/0.86.0),
and [the publisher's Claude Code 2.1.76 distribution metadata](https://registry.npmjs.org/@anthropic-ai/claude-code/2.1.76).
The first source documents API response classes; it does not turn CLI text into
an API response. The second documents non-interactive Codex invocation. The
two versioned publisher artifacts are the version-specific evidence for the
versions pinned by `scripts/bootstrap.sh`; neither artifact documents a stable
human terminal-error grammar.

`run.outcome` remains the parsed agent-authored file result (`DONE`, `ASK`,
`YIELD`, `FAIL`, or `NO_OUTCOME`). `run.host_assessment`, exit code, timeout,
terminal category, bounded sanitized diagnostic, and log path are independent
host evidence. Old rows display as `legacy/unknown`.

| Category | Meaning and evidence |
| --- | --- |
| `subscription_limit` | A resettable product/subscription allowance only when a terminal CLI diagnostic explicitly identifies the subscription/product plan **and** a reset or cadence. Bare `usage limit`, `plan limit`, `insufficient_quota`, or HTTP 429 do not establish it. |
| `throttled` | Rate limiting. A 429 alone has no reset schedule. |
| `overloaded`, `transient` | Provider capacity or transport failures; neither is authorization. |
| `authentication`, `permission` | Login/key/authorization failure. |
| `invalid_request` | Invalid model or request. |
| `billing_credits` | Exhausted API billing/credits. `insufficient_quota` without a specific terminal diagnostic is not proof of a subscription resettable limit. |
| `local_error` | Launcher, filesystem, or outcome-reading exception on this host. |
| `host_timeout` | Runner deadline or observed host-signal kill (including a negative POSIX signal exit status). |
| `protocol` | Reserved for a structured adapter terminal protocol defect. |
| `unknown` | Unsupported or insufficient evidence. |

The real adapters request machine-readable streams: Codex uses `codex exec
--json` (the non-interactive guide) and Claude uses `-p --output-format
stream-json --verbose` ([Claude CLI reference](https://code.claude.com/docs/en/cli-reference),
retrieved 2026-09-10). The adapters accept only a complete final, top-level
terminal record: Codex `error`/`turn.failed`, or Claude `result` with
`is_error: true`. Its direct `code`, `message`, `error`, and `result` fields
are bounded/sanitized and categorized. A later terminal success wins; a
truncated final record, a non-JSON tail, a tool record, or a quoted
JSON-looking string is unsupported evidence and remains `unknown` after a bad
exit. Thus a structured terminal error also fails a zero-exit session.

The versioned publisher artifacts prove that the installed bootstrap targets
are Codex **0.86.0** and Claude Code **2.1.76**; they do not publish a stable
terminal-event grammar for those exact releases. The parser is therefore a
conservative compatibility implementation of the two requested machine-stream
formats, not an assertion that every future or historical version emits every
accepted shape. The JSONL fixtures are **synthetic**: they exercise those
envelopes but are not retained captures and do not independently verify either
pinned release. Any unrecognized shape is explicitly **unverified** and is
not categorized as a provider failure.

The old `Codex API Error: <code>` / `Claude API Error: <code>` text envelopes
are **synthetic, unverified** and are no longer classified. Any arbitrary
line, including a prompt, quotation, tool output, agent summary, or truncated
JSON stream, is `unknown` after a bad exit. A recovered intermediate error
followed by a successful final event and exit zero is success.

Fixtures in tests are synthetic unless a test says otherwise. No historical
owner incident is called verified without its original run log and terminal
evidence; no such incident is asserted here.

Execution precedence is: timeout/host-signal kill, structured terminal failure, nonzero exit,
then parsed outcome. A host failure records usage and run evidence but cannot
acknowledge inbox/memo, create a proposal or question, apply a task verdict,
or complete an advisory plan review.

## Timer deferral and exit semantics

`nc run` exits successfully when the scheduler itself is healthy, including
when it defers a classified resettable allowance, throttle, overload, or
transient provider failure to the next timer firing.  That exit status is not
the child CLI exit code and is not an agent outcome.  The child run retains its
exit/usage/diagnostic evidence; the logical worker, critic, planner, or plan
review remains eligible with the same memo, undelivered feedback, worktree and
phase.  There is no in-process retry loop.

The durable mapping is deliberately small: a task plus its role agent is
logical work, each `run` is an execution attempt, and messages/proposal
revisions are feedback.  Plan advice uses one `(proposal, spec)` `plan_review`
identity; `plan_review_attempt` links its distinct attempts to runs.  A
temporary provider failure changes that review to `retryable`; only one later
attempt may complete it.  This is local scheduler state, not adoption of a
common bus or queue.
