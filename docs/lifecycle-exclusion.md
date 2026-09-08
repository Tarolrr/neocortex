# Lifecycle exclusion

Cooperating scheduler steps and owner lifecycle operations acquire the home
lifecycle flock first. Scheduler task turns, requeue and rollback then acquire
`nc-repository.lock` in the resolved Git common directory. Symlink aliases and
linked worktrees therefore use the same repository identity, including across
separate Neocortex homes. SQLite write transactions come last. Acquisition is
nonblocking and reports a retryable LifecycleBusy error; scheduler step returns
idle on contention. Lock files must never be removed while processes may use them.

The home lock conservatively protects all tasks through selection, preparation,
the agent turn, checks after end_run, integration, mirror, cleanup and final state
updates. The repository lock currently also spans the complete task turn. This
reduces concurrency but ensures cooperating Git mutations cannot overlap. No
SQLite write transaction is held across a turn or acceptance checks. Locks are
released by context cleanup or by the kernel on process exit; executed agents do
not inherit the descriptors.

Before preparation the scheduler rereads task and agent eligibility and rejects
stale selections and active runs. An ended run record is not an ownership release.

Rollback submissions carry the merge commit displayed to the owner. The CLI
requires `nc rollback TASK --confirm-commit COMMIT`; the browser posts the commit
shown beside its Roll back button. Shared operations compare this value with the
current task record while holding lifecycle and repository exclusion, before any
Git mutation. Missing or stale confirmation requires reloading and resubmitting.
