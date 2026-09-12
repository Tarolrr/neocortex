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
window. **Collection:** 2026-09-12T22:11:51+02:00 (Europe/Belgrade), by this
worker using read-only journal, systemd, Git, and SQLite access; tracked
template inspected at `4c95348` (before this evidence-note edit). The requested
local interval is **2026-09-11 22:45--23:55
Europe/Belgrade**, which converts to **2026-09-11 20:45--21:55 UTC** (CEST,
UTC+02:00).

The read-only journal records `neocortex.service` starting at **22:46:48
CEST**, then at **23:46:49 CEST** reporting “start operation timed out”,
terminating its main process with `TERM`, and failing with result `timeout`.
It starts again at **23:52:18 CEST** and deactivates successfully at **23:54:41
CEST**.  This is direct evidence of the service-wide start timeout in the
requested window, not evidence of provider overload or successful completion.

At collection, the tracked `deploy/neocortex.service` specifies
`TimeoutStartSec=infinity`; the installed `/etc/systemd/system/neocortex.service`
is readable and still specifies `TimeoutStartSec=3600`. `systemctl show`
reports `TimeoutStartUSec=1h`, `TimeoutStopUSec=1min 30s`, and
`FragmentPath=/etc/systemd/system/neocortex.service`; `DropInPaths=` was empty,
and `systemctl cat` showed no drop-ins. Thus the effective one-hour setting
matches the installed source, not the updated tracked template. The installed
runner resolves to `929bbd1b643e8aafde9a69c70670c3c50a1abed2` (its working tree
also has a modified `neocortex.egg-info/SOURCES.txt`), so it is not assumed to
contain this change.

A read-only SQLite snapshot of `/root/.neocortex/state.db` used
`PRAGMA query_only=ON` and one `BEGIN`/`COMMIT` transaction. It records T016
as done at 23:13:16 CEST; T017 run 238 began at **23:46:41 CEST**, eight seconds
before the journal timeout, and is recorded `INTERRUPTED` with no timeout,
terminal-category, or host-assessment value. T017 is currently `blocked` and
was later requeued by the owner. This is a temporal correlation, not proof that
the start timeout alone caused current T017 blocking: the snapshot's later
interruption/recovery fields and the task history require separate diagnosis.
