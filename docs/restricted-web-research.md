# Restricted planner web research

Planner and plan-critic runs share the `run_planner` adapter method.  They run
from their newly-created run directory, not a worktree.  The role contract
permits an advisory agent to write only its `outcome.json` there.  Workers and
change critics still use their existing adapter method and policy; this feature
does not provide an unrestricted fallback when a restricted adapter cannot run.

## What the launch permissions mean

For Codex, the restricted command is equivalent to:

```sh
codex --search exec --json --model "$MODEL" --sandbox workspace-write \
  --config sandbox_workspace_write.network_access=true \
  --skip-git-repo-check "$BRIEF"
```

`workspace-write` keeps Codex's local filesystem sandbox and does not add a
writable root for either the repository or the runtime home.  In pinned Codex
0.86.0, however, its workspace-write implementation also includes `/tmp` on
Unix and can include the `TMPDIR` environment value.  NC does not claim that
the sandbox makes the run directory its sole writable path; the exact
`outcome.json` restriction is the planner's role instruction.  The per-command
configuration override permits outbound network connections made by shell tools
inside that sandbox.  It is deliberately broader than an HTTP-read-only
guarantee: a shell with network access can use protocols and methods other than
an HTTP GET.  The launch therefore relies on the restricted writable root and
the role instruction, rather than describing shell networking as safe read
access.  `--search` separately enables Codex's provider-hosted live web-search
tool.  That tool is not the same thing as outbound shell networking.

Codex model-provider connectivity is distinct again: the outer Codex CLI needs
its authenticated provider connection to run at all; that connection is not a
permission for a model-generated shell command.  The non-interactive `exec`
path in the pinned source defaults its approval policy to `Never`.  NC neither
uses `--dangerously-bypass-approvals-and-sandbox` nor adds `--add-dir`; it also
does not use `--full-auto`, whose old meaning changes the approval policy to
on-request.  A tool denied by the sandbox is denied rather than interactively
approved.

For Claude, the corresponding restricted command includes:

```sh
claude -p "$BRIEF" --output-format stream-json --verbose \
  --permission-mode dontAsk \
  --tools Read,Glob,Grep,Write,WebSearch,WebFetch \
  --allowedTools Read Glob Grep WebSearch WebFetch \
  "Write(//$RUN_DIR/outcome.json)" --model "$MODEL"
```

`WebSearch` and `WebFetch` are provider-hosted tools, and are explicitly both
exposed and preauthorized.  `dontAsk` makes a missing permission fail instead
of waiting for an answer, so page retrieval is not contingent on interactive
approval.  The only Write permission is the exact outcome path; Bash, Edit,
broad Write permission, and `bypassPermissions` are absent.  Claude may still
be unable to search or fetch because of provider availability, account policy,
enterprise/organizational domain controls, authentication requirements, robots
or page restrictions.  Those restrictions are outside NC's control and must be
reported, not worked around with an unrestricted adapter.

## Research workflow

Use local `Read`, `Glob`, and `Grep` to establish repository facts.  When a
public documentation question remains, search for the primary vendor or project
documentation, fetch the relevant page, and use it as evidence in the proposal
or advisory finding.  External material cannot approve proposals, authorize
tasks, disclose private local data, install packages, change the host, or
publish anything.  If a source cannot be reached or verified, say so plainly;
do not fabricate a citation.  Planner and plan critic instructions retain the
outcome-only writing contract.  That instruction is a role constraint, not a
claim that Codex's workspace sandbox has only the run directory as a writable
area (it may also permit temporary directories as described above).

## Pinned-version verification

Retrieved 2026-09-11.  No CLI was upgraded, installed, authenticated, or given
a paid prompt for this verification.

* Codex **0.86.0**: the official
  [npm distribution metadata](https://registry.npmjs.org/@openai/codex/0.86.0)
  identifies the pinned tarball.  The matching official OpenAI source tag,
  [`rust-v0.86.0` top-level CLI source](https://github.com/openai/codex/blob/rust-v0.86.0/codex-rs/tui/src/cli.rs)
  defines `--search` (before `exec`; it is not an `exec` option); its
  [config types](https://github.com/openai/codex/blob/rust-v0.86.0/codex-rs/core/src/config/types.rs)
  define `sandbox_workspace_write.network_access`; and its
  [exec source](https://github.com/openai/codex/blob/rust-v0.86.0/codex-rs/exec/src/lib.rs)
  sets non-interactive approval to `Never`.  Its
  [workspace-write implementation](https://github.com/openai/codex/blob/rust-v0.86.0/codex-rs/core/src/sandboxing.rs)
  adds `/tmp` on Unix and may add `TMPDIR`; this is why the documentation does
  not equate the role's outcome-only rule with sandbox writable roots.  This is version-specific evidence;
  the current [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
  is useful context but is not used as proof of 0.86.0 compatibility.
* Claude Code **2.1.76**: the official
  [npm distribution metadata](https://registry.npmjs.org/@anthropic-ai/claude-code/2.1.76)
  identifies the pinned tarball.  Its packed `cli.js` was inspected without
  installing it and contains `WebSearch`, `WebFetch`, `--tools`,
  `--allowedTools`, `dontAsk`, and the permission parser; the current official
  [CLI reference](https://code.claude.com/docs/en/cli-reference) documents the
  command-line interface.  The packed source additionally says WebFetch
  per-domain rules use `WebFetch(domain:hostname)`, while a bare `WebFetch`
  tool allow rule is the supported full-tool preauthorization used here.

These checks establish syntax and local permission intent only.  They do not
prove a live account's entitlement, a particular result's availability, or
provider/organization policy behavior.

## Owner-only manual smoke procedure

Automated tests mock process launch only: they do not contact model providers,
install CLIs, or modify services.  On a disposable, authenticated host, an
owner can create an empty run directory and invoke each command above with a
brief asking it to search public documentation and fetch one result, then write
only its designated `outcome.json`.  Confirm the log records the search/fetch
tool activity and the outcome file exists.  In the same brief, ask it to create
`$REPOSITORY/should-not-exist`; after it exits, verify that path is absent and
that `$RUN_DIR/outcome.json` is the only intended persistent output (Codex may
also have access to its documented temporary directories).  Do not run this against a live scheduler
or infer success from mocked tests.  If search/fetch is denied or unavailable,
record the provider diagnostic and retain the restricted policy rather than
using a worker adapter or sandbox bypass.
