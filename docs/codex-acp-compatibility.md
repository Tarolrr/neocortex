# Codex ACP compatibility contract (implementation gate)

Retrieved 2026-09-11. This is a design/fixture contract only: it does not
start ACP, replace `Adapter`, alter CLI pins, persisted state, scheduler
ownership, Claude paths, or role `outcome.json` processing.

## Pinned evidence and installation gate

The review target is the published `@agentclientprotocol/codex-acp` **1.11.0**
release, lightweight tag `v1.11.0` resolving directly to commit
`51d6247ac7448485bfcf534b813196fafc26df59`. This is deliberately neither
`main`, `latest`, nor a preview. The release's npm tarball integrity is
`sha512-opPKsRaekgdmQpOpHrR0EEDn9chgtiN+b+h0V78fTuQP84TNzB7vrn3EtKODwbiJQTBHJAlynjSFQazFfaT+VQ==`.

* **Source inspection:** the [tagged package manifest](https://github.com/agentclientprotocol/codex-acp/blob/51d6247ac7448485bfcf534b813196fafc26df59/package.json) declares ACP SDK `^1.4.0` and Codex `^0.153.4`; the [tagged lockfile](https://github.com/agentclientprotocol/codex-acp/blob/51d6247ac7448485bfcf534b813196fafc26df59/package-lock.json) resolves exactly `@agentclientprotocol/sdk` **1.4.0** and `@openai/codex` **0.153.4**.
* **Published-artifact evidence:** [npm's version document](https://registry.npmjs.org/@agentclientprotocol/codex-acp/1.11.0) names 1.11.0 and the integrity above, but retains the manifest's ranges. The lockfile is source evidence, not proof that an arbitrary npm install chose those versions.
* **Live verification (required before any future activation):** install only the tarball whose integrity matches above into an isolated directory; inspect its resolved `@openai/codex` and SDK versions and run `codex-acp --version`/a handshake. Record platform and output. It has **not** been run here. Bootstrap's Codex **0.86.0** is incompatible/unproven for this profile and must not be silently reused.

Reject before `session/prompt` unless package name/version/integrity, resolved
Codex 0.153.4, resolved SDK 1.4.0, and the negotiated profile all match. A
future implementation must generate/validate App Server types from that same
Codex binary; OpenAI's [App Server documentation](https://learn.chatgpt.com/docs/app-server)
states generated schemas are specific to the Codex version.

Primary protocol context (the following protocol and App Server pages are
mutable documentation, so are retrieval-date-only evidence): ACP [v1 overview](https://agentclientprotocol.com/protocol/v1/overview),
[initialization](https://agentclientprotocol.com/protocol/v1/initialization),
[session setup](https://agentclientprotocol.com/protocol/v1/session-setup), and
[prompt turns](https://agentclientprotocol.com/protocol/v1/prompt-turns).
The old zed-industries location is a mutable redirect (retrieved 2026-09-11) to
[agentclientprotocol/codex-acp](https://github.com/agentclientprotocol/codex-acp).

## Required wire profile

Client sends ACP v1 `initialize` with exactly:

```json
{"clientCapabilities":{"_meta":{"jetbrains":{"air":{"version":1,"capabilities":["sessionFailure"]}}}}}
```

Then create one `session/new` with only its ACP fields: absolute `cwd` and
`mcpServers` (empty for this profile). The agent, not NC, allocates its
`sessionId`. Inspect advertised `configOptions` and `modes`, then set the
configured model with `session/set_config_option` (`configId: "model"`) and
the configured execution/approval mode with `session/set_config_option`
(`configId: "mode"`; `session/set_mode` is also advertised). Reject before a
prompt if either selected value is absent or rejected; do not accept a default.
Send `session/prompt`, correlate request id/session id/prompt id, and service
server requests only through the existing policy owner. No session resume,
client tools, common bus, or runtime activation is in scope.

The tagged server returns AIR at **top-level** `InitializeResponse._meta`
(`jetbrains.air.version: 1`, capabilities including `sessionFailure`), while
`agentCapabilities._meta` carries only its `authStatus` extension. It enables
typed failures when the client request has integer AIR version >= 1 and names
`sessionFailure` ([initialize source](https://github.com/agentclientprotocol/codex-acp/blob/51d6247ac7448485bfcf534b813196fafc26df59/src/CodexAcpServer.ts), [detector source](https://github.com/agentclientprotocol/codex-acp/blob/51d6247ac7448485bfcf534b813196fafc26df59/src/AirExtension.ts)).

Pinned server-requests are `session/request_permission` (MCP/tool approval)
and `elicitation/create` (form or URL), with `elicitation/complete` as its
follow-up notification; these are the client-directed request paths in the
tagged implementation ([handler source](https://github.com/agentclientprotocol/codex-acp/blob/51d6247ac7448485bfcf534b813196fafc26df59/src/CodexElicitationHandler.ts)).
This profile advertises neither elicitation nor MCP servers, so an unexpected
permission request receives the ACP v1 fail-closed **cancelled** outcome and
an unexpected elicitation receives **cancel**: never open a URL, solicit
input, or persist a choice. The selected
tagged mode is exactly `agent`: its [mode definition](https://github.com/agentclientprotocol/codex-acp/blob/51d6247ac7448485bfcf534b813196fafc26df59/src/AgentMode.ts)
sets Codex `approvalPolicy: "on-request"`, `approvalsReviewer: "auto_review"`,
and `sandboxPolicy: "workspaceWrite"`. It is **not** Codex `never`, and its
auto-review setting grants NC no authority. This is the pinned mode with a
workspace-write sandbox; `agent-full-access` is the only tagged `never` mode
and it selects `dangerFullAccess`, so it is rejected for NC's no-widened-
sandbox profile. Validate the selected `agent` config response and this
source-pinned policy tuple before a prompt; the tuple is not an ACP wire echo.
For `agent`, every `session/request_permission` is fail-closed with the ACP
v1 **cancelled** outcome even if Codex asks after auto-review/on-request, and every elicitation is
**cancel**. An unavailable policy-owner response, unknown/malformed request,
or attempt to broaden cwd/sandbox is likewise fail-closed and non-completing.

Cancellation is bounded: send `session/cancel`; wait at most **10 seconds**
for the correlated prompt response; then, if `sessionCapabilities.close` was
advertised, issue `session/close` and wait at most **5 seconds**. If either
bound expires, close stdin/stdout, TERM the ACP process group, wait **5
seconds**, then KILL if still alive. Record all timeout/exit/signal facts; no
late response is a completion. This is transport cleanup, not authority.

The future transport-facing interfaces are `AcpProcessFact` (pid/exit/signal,
stderr availability, and ordered `timeout_phases`), `AcpPromptFact`
(request/session/prompt ids, lossless JSON-RPC result/error, raw AIR
observations and validated AIR updates),
and `AcpCorrelation`
(prompt fact plus one agent-authored `outcome.json`). They are facts: only the
existing role parser decides DONE/ASK/YIELD/FAIL and only owner/arbiter retain
their authority.

| Concern | ACP profile | NC status |
|---|---|---|
| worker | worktree cwd only; `agent` source tuple: workspaceWrite / on-request / auto_review | configured worker model only; ACP permission requests always denied | rejected: no web-search client tool |
| critic | worktree cwd only; `agent` source tuple: workspaceWrite / on-request / auto_review | configured critic model only; ACP permission requests always denied | rejected: no web-search client tool |
| owner/planner | no ACP role mapping | reject before `session/new` | rejected |
| model | selected `model` option must return configured value | reject missing, different, or unadvertised value | n/a |
| client tools/MCP | empty `mcpServers`; no client tool capability | reject required MCP/client tools | unsupported |
| resume | server supports it | unsupported |

## Completion, failures, and recovery

Only a schema-valid, correlated successful prompt response with `end_turn` and
no error-severity AIR update is a **completion candidate**, not an NC outcome.
`cancelled`, `refusal`, `max_tokens`,
`max_turn_requests`, and every unknown stop reason are non-completions. A
JSON-RPC error is a transport failure, likewise non-completion. A valid
successful prompt candidate is correlated with the independently parsed
`outcome.json`; current scheduler/state remains the sole persistence owner.

For this source's emitted records, AIR `sessionFailure` has `id`, `revision`,
`category`, `severity`, `title`, and `actions` (with optional `details`). Its
declared categories are `connection`, `access`, `limit`, `request`, `service`,
and `unknown`; `severity` is required in emitted records. When consuming a
generic AIR record, omitted severity is conservatively `error`, as the tagged
source does ([failure construction source](https://github.com/agentclientprotocol/codex-acp/blob/51d6247ac7448485bfcf534b813196fafc26df59/src/CodexEventHandler.ts)). Terminal failures occur at
`PromptResponse._meta.jetbrains.air.sessionFailure`; recoverable warnings occur
in `session/update` `params.update._meta.jetbrains.air.sessionFailure` before a
  later prompt result. Retain every raw AIR observation in observation order,
including malformed values, alongside only complete validated records. A
validated record requires a nonempty string `id`, positive integer `revision`,
known category/severity, string `title`, and an actions array containing only
`retry`, `new_session`, or `login` (optional `details` is a string). Revisions
update the same incident; they do **not** signal recovery. In 1.11.0, turn
progress or a successful turn clears an active retry warning internally
(`completeRetryIncidentOnTurnProgress`/`completeSuccessfulTurn`/`clearSessionFailure`)
without emitting a synthetic “Recovered” revision. Thus recovery evidence is a
warning followed by progress or success with that warning cleared, never an
invented AIR clear/update. `timed_out` is true iff `timeout_phases` is nonempty;
the ordered values record each expired `cancel_response`, `session_close`,
`term_grace`, or `kill_grace` bound.

Conservative lossy mapping: the Codex `quota_exhausted` kind is emitted as
`limit` with no actions, and is only an unknown quota/account condition;
`limit` without retry -> unknown (not subscription, identity, or reset time);
`limit` with retry -> retryable throttling only; `service` with retry ->
retryable service condition (not uniquely overload); `access`/`request` ->
authentication/permission/invalid-request evidence as named; `connection` ->
local-or-remote connection failure, including App Server death. Never infer
from text, and AIR `actions` are display data, not scheduler/owner
authority. Process exit/signal/timeout remains an independent process fact.

Fixtures in `tests/fixtures/acp-*.synthetic.json` are invented, attributed
schema examples—not captured incidents. Each is JSON-RPC-shaped and correlates
request ids with results. Their `pinned_mode_observation` is deliberately
fixture metadata (not a claimed ACP field): it makes the selected `agent`
source tuple and fail-closed permission disposition testable. Fixtures cover success, typed terminal `end_turn`,
warning then success, quota-as-limit, retryable limit and service, access,
request, cancellation, malformed AIR metadata, a same-incident revision, and
retry recovery through turn progress (with no invented recovery revision).
