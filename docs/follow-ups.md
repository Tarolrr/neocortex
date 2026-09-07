# Deferred feature and review record

Every additional deferred review finding must include its reviewer/source, reason,
affected behavior, suggested next proposal, and a linked contextual code TODO.
Correctness defects in current scope must be fixed in the current batch.

| ID | Source | Reason for deferral | Affected behavior | Suggested next proposal / context |
| --- | --- | --- | --- | --- |
| FU-001 | neocortex-T023 task scope | Explicit phase-one boundary | Browser scheduler start/stop/resume, global health and preflight administration unavailable | Propose local scheduler administration; [UI navigation TODO](../nc/ui.py) |
| FU-002 | neocortex-T023 task scope | Explicit phase-one boundary | Browser incident listing and resolution unavailable; rollback still records incidents | Propose incident read/resolve workflows; [UI navigation TODO](../nc/ui.py) |
| FU-003 | neocortex-T023 task scope | Explicit phase-one boundary | Browser selects existing projects; registration/configuration stays CLI-only | Propose project administration with repository validation; [UI project-page TODO](../nc/ui.py) |

Task lifecycle and repository coordination for cancellation, requeue (including
fresh and budget), and rollback are assigned to ui-tasks in this batch and are
not deferred under FU-001. No additional reviewer findings have been deferred.
