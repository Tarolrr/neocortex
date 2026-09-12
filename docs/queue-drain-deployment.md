# Queue-drain timeout deployment and feedback 204 note

## Owner deployment and rollback

`deploy/neocortex.service` sets `TimeoutStartSec=infinity`.  For a
`Type=oneshot` service, its start phase is the whole `nc run` queue drain, so
the previous 3600-second value could terminate an otherwise healthy drain.
This does **not** unbound a turn: `turn_timeout_s` still bounds each adapter
session, ACP/preflight deadlines and process ownership containment remain in
effect, and shutdown/reaping retains its bounded grace periods.  A stop still
uses systemd's effective `TimeoutStopSec` (inspect it before changing it).

The timer remains `OnUnitInactiveSec=5min`: it starts a later invocation only
after the oneshot becomes inactive.  `nc run` can also defer a classified
temporary provider condition and exit for a later timer firing.  Thus it is
wrong to say every crash costs only one timer cycle: a service-wide start
timeout, an individual turn deadline, systemd stop timeout, and provider
deferral are different paths.  In particular, a host timeout is neither
provider overload nor successful role completion.

Merging this repository does not update `/etc/systemd/system/neocortex.service`.
Likewise, `scripts/bootstrap.sh` copies units from the runner checkout but does
not automatically fast-forward an existing `/opt/neocortex-runner` checkout.
An owner must first update that checkout to the inspected commit, then safely
install the unit at an activation boundary:

```sh
# Inspect rather than interrupt an active turn.  Wait until this is inactive.
systemctl show neocortex.service -p ActiveState -p SubState
systemctl cat neocortex.service

# Once inactive, from the updated runner checkout:
sudo scripts/bootstrap.sh
sudo systemctl daemon-reload
systemctl show neocortex.service -p TimeoutStartUSec -p TimeoutStopUSec -p FragmentPath -p DropInPaths
systemctl cat neocortex.service
```

`TimeoutStartUSec=infinity` and an empty or deliberately reviewed `DropInPaths`
are the effective-property check; `systemctl cat` exposes a conflicting drop-in.
Do not restart or stop an active queue service merely to activate this change:
wait for it to become inactive, install/reload, and let the existing timer
start the next drain.  If immediate activation is required, coordinate an
intentional, quiescent maintenance boundary rather than killing a turn.

To roll back at the same safe inactive boundary, restore the previously
recorded unit content (or previous runner commit), run `sudo systemctl
daemon-reload`, then repeat `systemctl show ... -p TimeoutStartUSec -p
DropInPaths`.  Do not use rollback to classify or erase a host termination.

Manual validation, not executed by this change: arrange a harmless queue drain
that remains active for more than one hour while each individual session stays
within its configured turn deadline.  Observe `ActiveState=activating` before
and after the hour, inspect the journal for absence of a start-timeout kill,
then allow the drain to exit and confirm the timer's next invocation follows
the inactive interval.  This requires an owner-controlled environment and is
not a test to run against production work.

## Feedback 204 incident note

**Attribution:** owner feedback 204, concerning the September 11 queue-drain
window. **Collection:** 2026-09-12T21:59:45+02:00 (Europe/Belgrade), by an
isolated repository worker at commit
`c4fb55f2455c9cecb2fa0aaf51d84f1f6c68456e` (the inspected HEAD before this
change).  The requested local interval is **2026-09-11 22:45--23:55
Europe/Belgrade**, which converts to **2026-09-11 20:45--21:55 UTC** (CEST,
UTC+02:00).

No host journal, installed `/etc/systemd/system` source, effective systemd
properties/drop-ins, runner checkout, or runtime database is mounted in this
worktree, and task boundaries prohibit reading or mutating that runtime state.
Consequently this note does **not** claim to have observed journal lines in the
window, an installed unit value, a runner version, or a database correlation.
The required owner-side read-only collection is: `journalctl --since '2026-09-11
22:45:00 CEST' --until '2026-09-11 23:55:00 CEST' -u neocortex.service`,
`systemctl cat`, `systemctl show` properties above, `git -C
/opt/neocortex-runner rev-parse HEAD`, and a SQLite `mode=ro`, `query_only=ON`,
single read transaction over the affected `run`, `task`, `agent`, `message`,
and `incident` history.  Record unavailable fields as unavailable, not as a
negative result.

The tracked template at the inspected commit had `TimeoutStartSec=3600`; the
installed source and effective unit/drop-ins are unavailable here, so equality
cannot be inferred.  A consistent database snapshot is likewise unavailable,
so no affected run/task history is asserted.  In particular, current T017
blocking must not be attributed solely to the old service timeout: correlation
requires the owner-collected timestamps, journal, and read-only snapshot.
