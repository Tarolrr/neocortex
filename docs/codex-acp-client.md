# Isolated Codex ACP client

`nc.acp_client.run_codex_acp_turn` is an internal, directly callable one-turn
client.  It is not an adapter and is intentionally not registered in
`ADAPTERS`, scheduler preflight, defaults, or outcome processing.

Inputs are an external `codex-acp` command plus a
`CodexAcpLaunchEvidence` bound to that exact command **and selected profile**, a model, prompt, log
path, and either `CodexAcpPolicy.ordinary(worktree)` or the explicit requested
`CodexAcpPolicy.restricted(run_directory)`.  The restricted input retains the
run directory as cwd and requires workspace-write, noninteractive approvals,
public web search/network, and protected repository/runtime homes. The pinned
`@agentclientprotocol/codex-acp` 1.11.0 source only establishes `agent`, whose
effective tuple is `workspaceWrite.networkAccess=false`; it does not establish
a restricted public-web/network mode. Consequently restricted dispatch is
currently rejected before process launch. A server-advertised string or a
launch-evidence profile name is not proof of sandbox, approval, or network
semantics. `agent` is never a restricted fallback. Live sandbox enforcement
for any future supported restricted profile remains explicitly unverified.
ACP advertises no client terminal/filesystem/MCP/web tool.
The `agent` mode is source-backed as `workspaceWrite`, `on-request`, and
`auto_review`; every ACP permission request is denied and every elicitation is
cancelled.  Live sandbox enforcement is explicitly unverified until owner
activation.

For an ordinary dispatch, the client strips `CODEX_CONFIG`, `CODEX_PATH`,
`INITIAL_AGENT_MODE`, inherited `HOME`/`CODEX_HOME`, and XDG configuration
homes. It gives an ordinary child a fresh, private configuration home
containing only the pinned workspace-write sandbox, on-request mode, and
disabled web-search/network values. Restricted work is rejected before a
private config or child is created.
This neither copies nor changes
credentials and does not authenticate; live activation must establish a
separately verified credential boundary. It sets model and mode explicitly,
validates each response, and returns decoded prompt evidence plus independent
process facts and distinct intentional-shutdown evidence.  Before it creates a process,
`CodexAcpLaunchEvidence` must match the pinned package name/version/tarball
integrity, resolved Codex 0.153.4, resolved ACP SDK 1.4.0, the exact command,
and the selected profile;
otherwise it rejects without dispatch.  The subsequent initialize response is
the required live protocol/profile handshake.  A future isolated installer
must inspect the artifact and resolved dependencies and construct this evidence;
the child preserves the explicitly supplied `PATH` (future deployment should
bind an absolute executable or a verified PATH resolution); it never prepends
a runtime-home bin directory or infers an executable from its name. Prompt expiry sends
`session/cancel`, permits
ten seconds for the correlated response, then uses advertised `session/close`
for five seconds before supervisor cleanup. A future Adapter integration must first verify
the pinned package/binary and supply its launch evidence, then retain NC role
validation, owner approval,
arbiter acceptance, and outcome parsing outside this module.
