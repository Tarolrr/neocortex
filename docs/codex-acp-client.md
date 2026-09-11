# Isolated Codex ACP client

`nc.acp_client.run_codex_acp_turn` is an internal, directly callable one-turn
client.  It is not an adapter and is intentionally not registered in
`ADAPTERS`, scheduler preflight, defaults, or outcome processing.

Inputs are a verified external `codex-acp` command, a model, prompt, log path,
and either `CodexAcpPolicy.ordinary(worktree)` or
`CodexAcpPolicy.restricted(run_directory)`.  The latter retains the run
directory as cwd and records workspace-write, noninteractive approvals, public
web-search/network intent, and protected repository/runtime-home contract;
ACP advertises no client terminal/filesystem/MCP/web tool.  Source-backed mode
is `agent` (`workspaceWrite`, `on-request`, `auto_review`), while every ACP
permission request is denied and every elicitation is cancelled.  Live
sandbox enforcement is explicitly unverified until owner activation.

The client strips `CODEX_CONFIG`, `CODEX_PATH`, `INITIAL_AGENT_MODE`, inherited
`HOME`/`CODEX_HOME`, and XDG configuration homes. It gives the child a fresh,
private configuration home containing only the pinned `agent` settings:
workspace-write sandbox, on-request mode, and the selected policy's explicit
web-search/network values. This neither copies nor changes credentials and
does not authenticate; live activation must establish a separately verified
credential boundary. It sets model and mode explicitly, validates each response,
and returns decoded prompt evidence plus independent process facts and distinct
intentional-shutdown evidence.  Prompt expiry sends `session/cancel`, permits
ten seconds for the correlated response, then uses advertised `session/close`
for five seconds before supervisor cleanup. A future Adapter integration must first verify
the pinned package/binary and then retain NC role validation, owner approval,
arbiter acceptance, and outcome parsing outside this module.
