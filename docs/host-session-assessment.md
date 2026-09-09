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

The current Codex and Claude adapters provide process exit, timeout, and a
combined terminal log, but no structured vendor terminal event. Therefore a
log fallback is used only after a nonzero exit and only for an adapter-specific
final envelope (`Codex API Error: <code>` or `Claude API Error: <code>`).
Those envelopes and their tests are **synthetic, unverified terminal forms**:
they are deliberately narrow compatibility fixtures, not claims about what
Codex 0.86.0 or Claude 2.1.76 emits. Any other line, including a prompt,
quotation, tool output, truncated stream, or agent summary, is `unknown`.
Successful exit logs are never searched. A future adapter may supply
`SessionResult.terminal_category` and
`terminal_diagnostic`; that structured evidence wins even with exit zero. A
recovered intermediate error followed by exit zero and no structured terminal
failure is successful.

Fixtures in tests are synthetic unless a test says otherwise. No historical
owner incident is called verified without its original run log and terminal
evidence; no such incident is asserted here.

Execution precedence is: timeout, structured terminal failure, nonzero exit,
then parsed outcome. A host failure records usage and run evidence but cannot
acknowledge inbox/memo, create a proposal or question, apply a task verdict,
or complete an advisory plan review.
