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
| `subscription_limit` | A resettable product/subscription allowance only when a terminal CLI diagnostic explicitly says so. It is not inferred from HTTP 429. |
| `throttled` | Rate limiting. A 429 alone has no reset schedule. |
| `overloaded`, `transient` | Provider capacity or transport failures; neither is authorization. |
| `authentication`, `permission` | Login/key/authorization failure. |
| `invalid_request` | Invalid model or request. |
| `billing_credits` | Exhausted API billing/credits. `insufficient_quota` without a specific terminal diagnostic is not proof of a subscription resettable limit. |
| `local_error` | Launcher, filesystem, or outcome-reading exception on this host. |
| `host_timeout` | Runner deadline/kill. |
| `protocol` | Reserved for a structured adapter terminal protocol defect. |
| `unknown` | Unsupported or insufficient evidence. |

The real adapters request machine-readable streams: Codex uses `codex exec
--json` (the non-interactive guide) and Claude uses `-p --output-format
stream-json --verbose` ([Claude CLI reference](https://code.claude.com/docs/en/cli-reference),
retrieved 2026-09-10). The adapter accepts only a final JSON error event:
Codex `{"type":"error","message":...}` or Claude
`{"type":"result","is_error":true,"result":...}`. That structured
terminal evidence wins even with exit zero. The JSONL fixtures are **synthetic
and unverified terminal-format examples**, not captures and
not owner incidents. The pinned-version distribution metadata establishes only
that bootstrap pins Codex 0.86.0 and Claude Code 2.1.76; it does **not**
support the event grammar above. No retained capture provenance or
version-specific primary-source terminal-event schema is currently available,
so these examples must not be read as version-specific evidence. They exercise
temporary and permanent classifications without a paid call.

The old `Codex API Error: <code>` / `Claude API Error: <code>` text envelopes
are **synthetic, unverified** and are no longer classified. Any arbitrary
line, including a prompt, quotation, tool output, agent summary, or truncated
JSON stream, is `unknown` after a bad exit. A recovered intermediate error
followed by a successful final event and exit zero is success.

Fixtures in tests are synthetic unless a test says otherwise. No historical
owner incident is called verified without its original run log and terminal
evidence; no such incident is asserted here.

Execution precedence is: timeout, structured terminal failure, nonzero exit,
then parsed outcome. A host failure records usage and run evidence but cannot
acknowledge inbox/memo, create a proposal or question, apply a task verdict,
or complete an advisory plan review.
