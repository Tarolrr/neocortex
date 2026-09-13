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
The published manifest is the primary artifact evidence for its dependencies;
the published [Codex 0.153.4 version document](https://registry.npmjs.org/@openai/codex/0.153.4)
states Node `>=16` and declares the platform optional packages. The installer
uses the reviewed `scripts/codex_acp_runtime.lock.json` with `npm ci`; it never
generates a lock during installation. Because `npm ci` does not install the
root project into `node_modules`, the script then extracts the SRI-verified
published tarball itself to `node_modules/@agentclientprotocol/codex-acp` and
creates its local `node_modules/.bin/codex-acp` link to its `dist/index.js`.
That lock contains exact resolved URLs
and SRI entries, including both published Linux packages
`0.153.4-linux-x64` and `0.153.4-linux-arm64`. The installer selects and
checks only the host package. It records `npm ls` separately. The
tagged upstream [manifest](https://github.com/agentclientprotocol/codex-acp/blob/51d6247ac7448485bfcf534b813196fafc26df59/package.json)
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
not reuse or update the global bootstrap Codex 0.86.0.  Before `npm ci`, it
validates the reviewed non-development graph's effective floor: its exact
[`open@11.0.1` published manifest](https://registry.npmjs.org/open/11.0.1)
has `engines.node: >=20`, so it rejects Node
below 20 even though Codex's own published manifest says `>=16`. npm engine
warnings are not accepted as a successful install. It records the resolved,
absolute Node executable, version, and digest. The published Linux packages contain the
native executables at `vendor/x86_64-unknown-linux-musl/bin/codex` (amd64) and
`vendor/aarch64-unknown-linux-musl/bin/codex` (arm64), rather than a generic
`bin/codex`; installation and verification require that exact host path.
`verify` redoes the
inspection immediately before any future launch: it hashes the installed
`package-lock.json` against the committed reviewed-lock digest and requires
matching reviewed-lock SRI/version entries for ACP, Codex, SDK, and the
selected platform package. Runtime-local receipts are tree-mutation
tripwires, not the trust anchor. It returns an evidence object
bound to that install-recorded absolute Node plus the authenticated
`dist/index.js` entry point. It does not execute the `.bin` shim, because its
`/usr/bin/env node` shebang would select Node from service PATH. Thus a service
PATH with no ACP executable (or no `node`) is expected and safe after the
absolute interpreter is verified again at launch.

## Authentication boundary

The isolated child has a newly created private HOME.  Codex's
[file-auth storage source](https://github.com/openai/codex/blob/main/codex-rs/login/src/auth/storage.rs)
reads `auth.json` from `CODEX_HOME`; that is the source-backed boundary used
here. The explicit `nc acp-ordinary-smoke` opt-in uses only
`prepare_private_home(EXPLICIT_AUTH_JSON, parent=...)`:
it checks and copies the owner-selected existing Codex `auth.json` with mode
0600 to `CODEX_HOME/auth.json`, the exact upstream lookup path. It accepts
only a nonempty `OPENAI_API_KEY` record or OAuth `tokens.access_token` plus
`tokens.refresh_token`, so unsupported auth storage fails offline. It does not
run login, mutate or enumerate the original
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
noninteractive, charged handshake smoke with the exact configured model. Make
a disposable worktree and an adjacent, owner-only credential parent (not below
the worktree, its repository, or the runtime), then use this exact prompt:

```sh
nc acp-ordinary-smoke --runtime /srv/neocortex/acp-1.11.0 --auth /secure/auth.json \
  --private-parent /secure/nc-acp-homes --worktree /srv/project-worktree \
  --model EXACT_MODEL --prompt 'In this worktree only: create .nc-acp-smoke; run the documented build command if one exists; run git status --short; then request permission to read /etc/hostname. Do not use network or web. Report each action and result.' --log /secure/acp-smoke.log
```

This is the implemented ordinary-role opt-in, not adapter registration and not
an adapter-name executable lookup. It inspects the runtime again, prepares and
cleans the same private HOME for auth and enforced config in `finally`, and
passes only the verified absolute command/evidence to the client. That
operation, model/network usability, and live sandbox enforcement are
intentionally not performed by `nc doctor` or acceptance tests.

For the pinned `agent` profile, source-backed settings are `workspaceWrite`,
`on-request`, and `auto_review`, with network and public web disabled. Treat
the paid smoke as a pass only if the log and filesystem show all of these:

1. `.nc-acp-smoke` exists inside the selected worktree, the configured build
   command either completed or was correctly reported absent, and `git status
   --short` was run.
2. The `/etc/hostname` request was denied (not approved); no file outside the
   worktree was written. Inspect the runtime and private-parent paths before
   and after: neither may gain a smoke marker or config/credential residue.
3. The transcript contains no successful network/web action. If the model
   attempts either, or the permission request is silently allowed, treat it as
   a failed live-sandbox verification and remove the disposable worktree only
   after normal lifecycle cleanup.

This records the exact-profile handshake and each pass/fail observation while
keeping execution opt-in. It must not broaden writable roots to the ACP
runtime, repository home, or private HOME. This differs sharply from the
existing worker environment, which currently uses `danger-full-access`; ACP
has no production transport cutover here.

Rollback is owner-only and only while ACP is idle (after normal lifecycle
STOP/cancellation/owned-process checks):

```sh
scripts/codex_acp_runtime.sh rollback /srv/neocortex/acp-1.11.0
```

Rollback intentionally does not require launch verification: it is the
recovery path for an interrupted install or a runtime whose dependency,
binary, cache, or receipt has been damaged.  It resolves the supplied existing
directory, rejects `/` and the current worktree, and requires the private,
mode-restricted install identity created at that exact absolute location by
`install` (including its owner UID). A reviewed lock or tarball is public and
cannot authorize deletion; copying either into an unrelated directory is
refused. The identity is written before fallible installation steps, so a
partial install left by interruption can still be removed at the idle boundary.

No test installs npm packages, touches credentials, invokes a real model, or
claims amd64/arm64/sandbox/auth behavior has been live verified.
