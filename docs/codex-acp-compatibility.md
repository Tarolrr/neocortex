# Codex ACP compatibility contract (implementation gate)

Retrieved 2026-09-11. This is a design/fixture contract only: it does not
start ACP, replace `Adapter`, alter CLI pins, persisted state, scheduler
ownership, Claude paths, or role `outcome.json` processing.

## Pinned evidence and installation gate

The review target is the published `@agentclientprotocol/codex-acp` **1.11.0**
release, annotated tag `v1.11.0`, peeled commit
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

Primary protocol context: ACP [v1 overview](https://agentclientprotocol.com/protocol/v1/overview),
[initialization](https://agentclientprotocol.com/protocol/v1/initialization),
[session setup](https://agentclientprotocol.com/protocol/v1/session-setup), and
[prompt turns](https://agentclientprotocol.com/protocol/v1/prompt-turns).
The old zed-industries location redirects development to
[agentclientprotocol/codex-acp](https://github.com/agentclientprotocol/codex-acp).

## Required wire profile

Client sends ACP v1 `initialize` with exactly:

```json
{"clientCapabilities":{"_meta":{"jetbrains":{"air":{"version":1,"capabilities":["sessionFailure"]}}}}}
```

Then create one `session/new` with explicit configured `cwd`, model and
execution/approval policy; do not accept an agent-selected default. Send one
`session/prompt`, correlate the request id, session id and prompt id, service
server requests only through the existing policy owner, and bound cancel then
close. No session resume, client tools, common bus, or runtime activation is in
scope. The tagged server does **not** advertise or echo `sessionFailure` in its
initialize response: `agentCapabilities._meta` contains its `authStatus`
extension, not AIR. Typed failures are enabled solely because the server detects
the client's request `_meta.jetbrains.air` integer version >= 1 and named
`sessionFailure` capability ([initialize source](https://github.com/agentclientprotocol/codex-acp/blob/51d6247ac7448485bfcf534b813196fafc26df59/src/CodexAcpServer.ts), [detector source](https://github.com/agentclientprotocol/codex-acp/blob/51d6247ac7448485bfcf534b813196fafc26df59/src/AirExtension.ts)).

The future transport-facing interfaces are `AcpProcessFact` (pid/exit/signal,
timeout, stderr availability), `AcpPromptFact` (request/session/prompt ids,
JSON-RPC result/error, stop reason, AIR updates), and `AcpCorrelation`
(prompt fact plus one agent-authored `outcome.json`). They are facts: only the
existing role parser decides DONE/ASK/YIELD/FAIL and only owner/arbiter retain
their authority.

| Concern | ACP profile | NC status |
|---|---|---|
| sandbox/cwd | explicit `session/new` cwd and policy | map from current adapter; no widening |
| model | explicit configured model | map; reject missing/rerouted value |
| web search | no client tool grant | unsupported |
| client tools/MCP | no client tools | unsupported |
| resume | server supports it | unsupported |

## Completion, failures, and recovery

`end_turn` without an error-severity AIR failure is only a **completion
candidate**, not an NC outcome. `cancelled`, `refusal`, `max_tokens`,
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
later prompt result. Revisions update the same failure id; recovery needs a
later supported successful prompt, never an assumed reset.

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
request ids with results; fixtures cover success, typed terminal `end_turn`,
warning then success, quota-as-limit, retryable limit and service, access,
request, cancellation, and malformed AIR metadata.
