# Isolated Codex ACP client

`nc.acp_client.run_codex_acp_turn` is an internal, directly callable one-turn
client.  It is not an adapter and is intentionally not registered in
`ADAPTERS`, scheduler preflight, defaults, or outcome processing.

Inputs are an external `codex-acp` command plus a
`CodexAcpLaunchEvidence` bound to that exact command, a model, prompt, log
path, and either `CodexAcpPolicy.ordinary(worktree)` or
`CodexAcpPolicy.restricted(run_directory)`.  The latter retains the run
directory as cwd and records workspace-write, noninteractive approvals, public
web-search/network intent, and protected repository/runtime-home contract.
It is deliberately rejected before process dispatch in this pinned profile:
the source-backed `agent` mode has `workspaceWrite.networkAccess=false`, so
selecting that required ACP mode would override the requested network setting.
This client therefore does not claim restricted Codex ACP support or fall back
to ordinary policy.  ACP advertises no client terminal/filesystem/MCP/web tool.
The `agent` mode is source-backed as `workspaceWrite`, `on-request`, and
`auto_review`; every ACP permission request is denied and every elicitation is
cancelled.  Live sandbox enforcement is explicitly unverified until owner
activation.

The client strips `CODEX_CONFIG`, `CODEX_PATH`, `INITIAL_AGENT_MODE`, inherited
`HOME`/`CODEX_HOME`, and XDG configuration homes. It gives the child a fresh,
private configuration home containing only the pinned `agent` settings:
workspace-write sandbox, on-request mode, and the ordinary policy's explicit
disabled web-search/network values.  A restricted request never reaches this
stage because the selected ACP mode cannot preserve it. This neither copies nor
changes credentials and does not authenticate; live activation must establish a
separately verified credential boundary. It sets model and mode explicitly,
validates each response, and returns decoded prompt evidence plus independent
process facts and distinct intentional-shutdown evidence.  Before it creates a process,
`CodexAcpLaunchEvidence` must match the pinned package name/version/tarball
integrity, resolved Codex 0.153.4, resolved ACP SDK 1.4.0, and the exact command;
otherwise it rejects without dispatch.  The subsequent initialize response is
the required live protocol/profile handshake.  A future isolated installer
must inspect the artifact and resolved dependencies and construct this evidence;
the client never infers it from an executable name. Prompt expiry sends
`session/cancel`, permits
ten seconds for the correlated response, then uses advertised `session/close`
for five seconds before supervisor cleanup. A future Adapter integration must first verify
the pinned package/binary and supply its launch evidence, then retain NC role
validation, owner approval,
arbiter acceptance, and outcome parsing outside this module.
