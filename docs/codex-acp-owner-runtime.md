# Owner procedure: isolated Codex ACP 1.11.0

This is an opt-in preparation procedure, not an adapter rollout.  `nc` does
not register ACP in `ADAPTERS`, add it to `arbiter.host_requirements`, alter
bootstrap defaults, or launch it from the service.  In particular an adapter
label in configuration is not treated as an executable name by this procedure.

## Evidence and install

The owner obtains the exact published tarball for
`@agentclientprotocol/codex-acp@1.11.0` from the [npm version document](https://registry.npmjs.org/@agentclientprotocol/codex-acp/1.11.0).
Its SRI must be
`sha512-opPKsRaekgdmQpOpHrR0EEDn9chgtiN+b+h0V78fTuQP84TNzB7vrn3EtKODwbiJQTBHJAlynjSFQazFfaT+VQ==`.
The published manifest is the primary artifact evidence for its dependencies
and Node `engines`; the installer records the *locally resolved* lock and
`npm ls` receipt separately.  The tagged upstream [manifest](https://github.com/agentclientprotocol/codex-acp/blob/51d6247ac7448485bfcf534b813196fafc26df59/package.json)
and [lockfile](https://github.com/agentclientprotocol/codex-acp/blob/51d6247ac7448485bfcf534b813196fafc26df59/package-lock.json)
identify the intended Codex 0.153.4 and ACP SDK 1.4.0; the source lock is not
substituted for a resolved installation.

At an idle lifecycle boundary, as the owner, run:

```sh
scripts/codex_acp_runtime.sh install /srv/neocortex/acp-1.11.0 /secure/codex-acp-1.11.0.tgz
nc acp-doctor --runtime /srv/neocortex/acp-1.11.0
```

The command rejects an existing directory, a bad tarball, anything other than
Linux amd64/arm64, missing matching `@openai/codex-linux-{x64,arm64}`, missing
resolved ACP/Codex/SDK versions, or a launcher outside the runtime.  It does
not reuse or update the global bootstrap Codex 0.86.0.  `verify` redoes the
inspection immediately before any future launch and returns an evidence object
bound to `/srv/.../bin/codex-acp`, not `codex` on PATH.  Thus a service PATH
with no ACP executable is expected and safe.

## Authentication boundary

The isolated child has a newly created private HOME.  A future explicit owner
activation may use only `prepare_private_home(EXPLICIT_AUTH_JSON, parent=...)`:
it checks and copies the owner-selected existing Codex `auth.json` with mode
0600 into that HOME.  It does not run login, mutate or enumerate the original
credential store, record credential values, or copy config.  This is a
read-only reuse boundary; an unreadable, empty, malformed, or unsupported
auth file is an actionable readiness error.  `nc acp-doctor --auth
/absolute/path/to/auth.json` checks it without copying it.

The private home is created under an owner-chosen dedicated directory and is
removed in a `finally` path with `cleanup_private_home`; setup failures remove
it too.  Cleanup refuses anything that is not its generated child directory.
Never put that parent under a repository, runtime home, or sandbox writable
root.  `CODEX_PATH`, `CODEX_CONFIG`, `CODEX_HOME`, HOME/XDG redirection and
`INITIAL_AGENT_MODE` inherited by the owner process are rejected before this
boundary; credentials are never printed in diagnostics or receipts.

## Explicit smoke and behavior

Only after offline doctor is green may an owner deliberately perform a
noninteractive, charged handshake smoke with the exact configured model.  It
must call `inspect_runtime`, prepare/clean the private HOME in `finally`, pass
the returned absolute command and evidence to the isolated client, and record
only outcome/process evidence.  That operation, model/network usability, and
live sandbox enforcement are intentionally not performed by `nc doctor` or
acceptance tests.

For the pinned `agent` profile, source-backed settings are `workspaceWrite`,
`on-request`, and `auto_review`, with network and public web disabled.  A
smoke should prove a worktree-only write/build/git operation and verify that a
permission request is denied.  It must not broaden writable roots to the ACP
runtime, repository home, or private HOME.  This differs sharply from the
existing worker environment, which currently uses `danger-full-access`; ACP
has no production transport cutover here.

Rollback is owner-only and only while ACP is idle (after normal lifecycle
STOP/cancellation/owned-process checks):

```sh
scripts/codex_acp_runtime.sh rollback /srv/neocortex/acp-1.11.0
```

No test installs npm packages, touches credentials, invokes a real model, or
claims amd64/arm64/sandbox/auth behavior has been live verified.
