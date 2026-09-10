"""SQLite state: the single source of truth for projects, tasks, agents and messages."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .proposals import check_proposal

SCHEMA = """
CREATE TABLE IF NOT EXISTS project (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    repo_path   TEXT NOT NULL,
    test_cmd    TEXT,
    mirror      TEXT,                     -- git remote to push accepted work to
    quota_share REAL NOT NULL DEFAULT 1.0,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS task (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES project(id),
    title        TEXT NOT NULL,
    objective    TEXT NOT NULL,
    acceptance   TEXT NOT NULL,           -- JSON list of checks
    boundaries   TEXT NOT NULL DEFAULT '[]',
    status       TEXT NOT NULL,           -- queued|in_progress|in_review|done|failed|blocked
    priority     INTEGER NOT NULL DEFAULT 100,
    branch       TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    budget_turns INTEGER NOT NULL DEFAULT 6,
    merge_commit TEXT,
    result       TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS agent (
    id         TEXT PRIMARY KEY,
    role       TEXT NOT NULL,             -- worker|critic|planner
    project_id TEXT NOT NULL REFERENCES project(id),
    task_id    TEXT REFERENCES task(id),
    state      TEXT NOT NULL,             -- runnable|waiting|blocked|done|failed
    model      TEXT NOT NULL,
    turns      INTEGER NOT NULL DEFAULT 0,
    memo       TEXT NOT NULL DEFAULT '',  -- compact decision journal carried between turns
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS message (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,            -- question|answer|review_request|review_verdict|incident|feedback
    sender      TEXT NOT NULL,            -- agent id or 'owner' or 'scheduler'
    recipient   TEXT NOT NULL,            -- agent id or 'owner'
    task_id     TEXT,
    payload     TEXT NOT NULL,            -- JSON
    in_reply_to INTEGER REFERENCES message(id),
    delivered   INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS run (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id   TEXT NOT NULL REFERENCES agent(id),
    task_id    TEXT,
    role       TEXT NOT NULL,
    model      TEXT NOT NULL,
    outcome    TEXT,                      -- DONE|ASK|YIELD|FAIL|NO_OUTCOME
    detail     TEXT,
    tokens     INTEGER,
    log_path   TEXT,
    started_at REAL NOT NULL,
    ended_at   REAL,
    -- Host evidence is deliberately independent from parsed agent outcome.
    exit_code  INTEGER,
    timed_out  INTEGER,
    terminal_category TEXT,
    terminal_diagnostic TEXT,
    host_assessment TEXT
);

CREATE TABLE IF NOT EXISTS task_seq (
    project_id TEXT PRIMARY KEY,
    last       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS incident (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    detail     TEXT NOT NULL,
    resolved   INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

-- This singleton is deliberately separate from incidents: changing backup
-- observability must not itself ask for another backup.
CREATE TABLE IF NOT EXISTS backup_state (
    id INTEGER PRIMARY KEY CHECK (id=1),
    dirty_generation INTEGER NOT NULL DEFAULT 0,
    acknowledged_generation INTEGER NOT NULL DEFAULT 0,
    last_attempt_at REAL,
    last_success_at REAL,
    last_error TEXT
);
INSERT OR IGNORE INTO backup_state(id) VALUES(1);
"""


class State:
    def __init__(self, db_path: Path, *, initialize: bool = True, timeout: float = 30):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=timeout)
        self.db.row_factory = sqlite3.Row
        if initialize:
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        if initialize:
            self.db.executescript(SCHEMA)
            self._migrate()
            self.db.commit()

    def _migrate(self) -> None:
        """Apply schema changes to new and existing databases, preserving stored history."""
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS proposal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL REFERENCES project(id),
                source TEXT NOT NULL,
                rationale TEXT NOT NULL,
                spec TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'approved', 'rejected', 'superseded')),
                created_at REAL NOT NULL,
                decided_at REAL,
                reason TEXT
            )
        """)
        # Rebuild the old CHECK constraint, preserving every column and review.
        schema = self.db.execute(
            "SELECT sql FROM sqlite_master WHERE name='proposal'"
        ).fetchone()[0]
        if "'superseded'" not in schema:
            self.db.commit()
            self.db.execute("PRAGMA foreign_keys=OFF")
            try:
                with self.db:
                    self.db.execute("BEGIN IMMEDIATE")
                    self.db.execute(
                        ("CREATE TABLE proposal_new (" + schema.split("(", 1)[1])
                        .replace("'rejected'", "'rejected', 'superseded'")
                    )
                    self.db.execute("INSERT INTO proposal_new SELECT * FROM proposal")
                    self.db.execute("DROP TABLE proposal")
                    self.db.execute("ALTER TABLE proposal_new RENAME TO proposal")
            finally:
                self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS plan_review (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_id INTEGER NOT NULL REFERENCES proposal(id),
                spec TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                findings TEXT NOT NULL DEFAULT '[]',
                recommendation TEXT NOT NULL DEFAULT '',
                UNIQUE(proposal_id, spec)
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS proposal_revision (
                original_id INTEGER PRIMARY KEY REFERENCES proposal(id),
                feedback_id INTEGER NOT NULL UNIQUE REFERENCES message(id),
                planner_id TEXT NOT NULL REFERENCES agent(id),
                replacement_id INTEGER UNIQUE REFERENCES proposal(id)
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS plan_review_attempt (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                review_id INTEGER NOT NULL REFERENCES plan_review(id),
                run_id INTEGER NOT NULL REFERENCES run(id),
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(run_id)
            )
        """)
        for table, column, decl in (
            ("proposal", "findings", "TEXT NOT NULL DEFAULT '[]'"),
            ("project", "mirror", "TEXT"),
            ("project", "planner_last_ran_at", "REAL"),
            ("project", "planner_skip_reason", "TEXT"),
            ("incident", "resolved_at", "REAL"),
            ("incident", "resolution_note", "TEXT"),
            ("task", "merge_commit", "TEXT"),
            ("task", "depends_on", "TEXT NOT NULL DEFAULT '[]'"),
            # These are deliberately additive.  A NULL owner is a legacy,
            # uncertain record, never evidence that a session is dead.
            ("run", "owner_pid", "INTEGER"),
            ("run", "owner_start", "TEXT"),
            # 0 means this row predates ownership recording.  It is the only
            # case in which an operator can attest quiescence; a newer row
            # with incomplete evidence is an ambiguous interrupted launch.
            ("run", "ownership_version", "INTEGER NOT NULL DEFAULT 0"),
            ("run", "adapter_pid", "INTEGER"),
            ("run", "adapter_start", "TEXT"),
            ("run", "adapter_pgid", "INTEGER"),
            # Version 2 records a dedicated cgroup created before adapter exec.
            # Unlike a process group, it continues to contain a child that
            # calls setsid()/setpgid() after the scheduler has died.
            ("run", "adapter_cgroup", "TEXT"),
            ("run", "interrupted_at", "REAL"),
            ("run", "recovered_at", "REAL"),
            ("run", "recovery_reason", "TEXT"),
            ("run", "exit_code", "INTEGER"),
            ("run", "timed_out", "INTEGER"),
            ("run", "terminal_category", "TEXT"),
            ("run", "terminal_diagnostic", "TEXT"),
            ("run", "host_assessment", "TEXT"),
            ("run", "defer_until", "REAL"),
        ):
            known = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if column not in known:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

        self.db.execute("""CREATE TABLE IF NOT EXISTS backup_state (
            id INTEGER PRIMARY KEY CHECK (id=1), dirty_generation INTEGER NOT NULL DEFAULT 0,
            acknowledged_generation INTEGER NOT NULL DEFAULT 0, last_attempt_at REAL,
            last_success_at REAL, last_error TEXT)""")
        self.db.execute("INSERT OR IGNORE INTO backup_state(id) VALUES(1)")

    def _backup_dirty(self) -> None:
        """Record a durable request in the caller's transaction, never by itself."""
        self.db.execute("UPDATE backup_state SET dirty_generation=dirty_generation+1 WHERE id=1")

    # --- generic helpers -------------------------------------------------
    def q(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self.db.execute(sql, tuple(params)))

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        rows = self.q(sql, params)
        return rows[0] if rows else None

    def x(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        cur = self.db.execute(sql, tuple(params))
        self.db.commit()
        return cur

    # --- projects --------------------------------------------------------
    def add_project(self, pid: str, title: str, repo_path: str, test_cmd: str | None,
                    quota_share: float = 1.0, mirror: str | None = None) -> None:
        self.x(
            "INSERT OR REPLACE INTO project(id,title,repo_path,test_cmd,mirror,quota_share,"
            "created_at) VALUES(?,?,?,?,?,?,?)",
            (pid, title, repo_path, test_cmd, mirror, quota_share, time.time()),
        )

    def set_project_test_cmd(self, pid: str, test_cmd: str) -> None:
        """Update just one project's arbiter command, preserving its other settings."""
        if self.x("UPDATE project SET test_cmd=? WHERE id=?", (test_cmd, pid)).rowcount != 1:
            raise LookupError(f"unknown project: {pid}")

    # --- tasks -----------------------------------------------------------
    def next_task_id(self, project_id: str) -> str:
        """Ids are never reused, even after a task row is deleted."""
        with self.db:
            return self._next_task_id(project_id)

    def _next_task_id(self, project_id: str) -> str:
        self.db.execute(
            "INSERT INTO task_seq(project_id,last) VALUES(?,1)"
            " ON CONFLICT(project_id) DO UPDATE SET last = last + 1",
            (project_id,),
        )
        n = self.one("SELECT last FROM task_seq WHERE project_id=?", (project_id,))["last"]
        return f"{project_id}-T{n:03d}"

    def add_task(self, project_id: str, title: str, objective: str,
                 acceptance: list[str], boundaries: list[str] | None = None,
                 priority: int = 100, budget_turns: int = 6,
                 depends_on: list[str] | None = None) -> str:
        with self.db:
            return self._add_task(project_id, title, objective, acceptance, boundaries,
                                  priority, budget_turns, depends_on)

    def _add_task(self, project_id: str, title: str, objective: str,
                  acceptance: list[str], boundaries: list[str] | None = None,
                  priority: int = 100, budget_turns: int = 6,
                  depends_on: list[str] | None = None,
                  allowed_dependencies: set[str] | None = None) -> str:
        self._validate_task_fields(project_id, title, objective, acceptance,
                                   boundaries, priority, budget_turns, depends_on,
                                   allowed_dependencies)
        tid = self._next_task_id(project_id)
        now = time.time()
        self.db.execute(
            "INSERT INTO task(id,project_id,title,objective,acceptance,boundaries,status,priority,"
            "budget_turns,depends_on,created_at,updated_at) VALUES(?,?,?,?,?,?,'queued',?,?,?,?,?)",
            (tid, project_id, title, objective, json.dumps(acceptance),
             json.dumps(boundaries or []), priority, budget_turns,
             json.dumps(depends_on or []), now, now),
        )
        return tid

    def _validate_task_fields(self, project_id: str, title: str, objective: str,
                              acceptance: list[str], boundaries: list[str] | None,
                              priority: int, budget_turns: int,
                              depends_on: list[str] | None,
                              allowed_dependencies: set[str] | None = None) -> None:
        """Reject malformed task input before allocating an id or writing state."""
        if not isinstance(project_id, str) or self.one("SELECT 1 FROM project WHERE id=?",
                                                       (project_id,)) is None:
            raise ValueError(f"unknown project: {project_id}")
        for name, value in (("title", title), ("objective", objective)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required and must be text")
        for name, value in (("acceptance", acceptance),
                            ("boundaries", [] if boundaries is None else boundaries),
                            ("depends_on", [] if depends_on is None else depends_on)):
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ValueError(f"{name} must be a list of strings")
        if type(priority) is not int:
            raise ValueError("priority must be an integer")
        if type(budget_turns) is not int or budget_turns < 1:
            raise ValueError("budget_turns must be an integer greater than zero")
        # Dependencies are task IDs, not free-form labels.  Validating this at
        # the state boundary keeps imported/browser-created tasks from becoming
        # permanently unready due to a typo or a task in another project.
        allowed = allowed_dependencies or set()
        for dependency in depends_on or []:
            row = self.one("SELECT project_id FROM task WHERE id=?", (dependency,))
            if row is None:
                if dependency not in allowed:
                    raise ValueError(f"unknown dependency: {dependency}")
            elif row["project_id"] != project_id:
                raise ValueError(f"dependency belongs to another project: {dependency}")

    def cancel_task(self, task_id: str, reason: str) -> bool:
        """Atomically retire a task and its agents, retaining existing evidence."""
        if not reason.strip():
            raise ValueError("a cancellation reason is required")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            task = self.one("SELECT status FROM task WHERE id=?", (task_id,))
            if task is None:
                raise ValueError(f"unknown task: {task_id}")
            if self.one(
                "SELECT 1 FROM run WHERE ended_at IS NULL AND"
                " (task_id=? OR agent_id IN (SELECT id FROM agent WHERE task_id=?))",
                (task_id, task_id),
            ):
                raise ValueError(f"{task_id} has an active run")
            if task["status"] == "cancelled":
                return False
            if task["status"] not in ("queued", "blocked", "failed"):
                raise ValueError(f"cannot cancel {task_id} in status {task['status']}")
            now = time.time()
            self.db.execute("UPDATE task SET status='cancelled', updated_at=? WHERE id=?",
                            (now, task_id))
            self.db.execute("UPDATE agent SET state='done', updated_at=? WHERE task_id=?",
                            (now, task_id))
            self.db.execute(
                "INSERT INTO message(kind,sender,recipient,task_id,payload,created_at)"
                " VALUES('cancellation','owner','owner',?,?,?)",
                (task_id, json.dumps({"reason": reason}, ensure_ascii=False), now),
            )
        return True

    def unmet_dependencies(self, task_id: str) -> list[str]:
        """Dependencies that are not accepted yet; a missing one never becomes met."""
        row = self.one("SELECT depends_on FROM task WHERE id=?", (task_id,))
        if row is None:
            return []
        unmet = []
        for dep in json.loads(row["depends_on"] or "[]"):
            other = self.one("SELECT status FROM task WHERE id=?", (dep,))
            if other is None or other["status"] != "done":
                unmet.append(dep)
        return unmet

    def add_task_spec(self, spec: dict) -> str:
        """Use the same task fields for file imports and proposal approvals."""
        with self.db:
            return self._add_task_spec(spec)

    def _add_task_spec(self, spec: dict) -> str:
        return self._add_task(spec["project"], spec["title"], spec["objective"],
                              spec["acceptance"], spec.get("boundaries"),
                              spec.get("priority", 100), spec.get("budget_turns", 6),
                              spec.get("depends_on"))

    # --- proposals -------------------------------------------------------
    def add_proposal(self, project_id: str, source: str, rationale: str,
                     spec: list[dict], revision_id: int | None = None) -> int:
        if not isinstance(spec, list) or any(
            not isinstance(task, dict) or task.get("project") != project_id for task in spec
        ):
            raise ValueError("proposal specs must be a list of tasks for its project")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if revision_id is not None:
                revision = self.one(
                    "SELECT r.* FROM proposal_revision r JOIN proposal p ON p.id=r.original_id"
                    " WHERE r.original_id=? AND r.planner_id=? AND p.project_id=?"
                    " AND r.replacement_id IS NULL", (revision_id, source, project_id),
                )
                if revision is None:
                    raise ValueError("revision already replaced or belongs to another planner")
            cur = self.db.execute(
                "INSERT INTO proposal(project_id,source,rationale,spec,created_at,findings)"
                " VALUES(?,?,?,?,?,?)",
                (project_id, source, rationale, json.dumps(spec, ensure_ascii=False), time.time(),
                 json.dumps(self._proposal_findings(spec))),
            )
            self._backup_dirty()
            proposal_id = int(cur.lastrowid)
            if revision_id is not None:
                self.db.execute(
                    "UPDATE proposal_revision SET replacement_id=? WHERE original_id=?",
                    (proposal_id, revision_id),
                )
        return proposal_id

    def pending_revision(self, planner_id: str) -> sqlite3.Row | None:
        return self.one(
            "SELECT r.*, p.spec, m.payload FROM proposal_revision r"
            " JOIN proposal p ON p.id=r.original_id JOIN message m ON m.id=r.feedback_id"
            " WHERE r.planner_id=? AND r.replacement_id IS NULL ORDER BY r.original_id LIMIT 1",
            (planner_id,),
        )

    def _proposal_findings(self, specs: list[dict]) -> list[str]:
        return check_proposal(specs, {r["id"] for r in self.q("SELECT id FROM task")})

    def approve_proposal(self, proposal_id: int, force: bool = False) -> list[str]:
        # Serialize decisions and commit the whole batch, including task IDs, together.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self._pending_proposal(proposal_id)
            specs = json.loads(row["spec"])
            if not isinstance(specs, list):
                raise TypeError("proposal spec must be a list")
            findings = self._proposal_findings(specs)
            self.db.execute("UPDATE proposal SET findings=? WHERE id=?",
                            (json.dumps(findings), proposal_id))
            if findings and not force:
                # Persist refreshed findings even though no tasks are created.
                self.db.commit()
                raise ValueError("proposal has findings (use --force to override):\n"
                                 + "\n".join(findings))
            ids = []
            local_dependencies = {spec["id"] for spec in specs if spec.get("id")}
            # Proposal-local IDs are resolved after rows receive their canonical
            # IDs.  A forced proposal may deliberately retain an advisory
            # unknown-dependency finding, so it is the sole internal caller
            # allowed to defer that existence check.
            if force:
                local_dependencies.update(
                    dep for spec in specs for dep in spec.get("depends_on", [])
                )
            for spec in specs:
                if spec["project"] != row["project_id"]:
                    raise ValueError("proposed task belongs to another project")
                ids.append(self._add_task(spec["project"], spec["title"], spec["objective"],
                                          spec["acceptance"], spec.get("boundaries"),
                                          spec.get("priority", 100),
                                          spec.get("budget_turns", 6),
                                          spec.get("depends_on"), local_dependencies))
            local_ids = {spec["id"]: tid for spec, tid in zip(specs, ids) if spec.get("id")}
            for spec, tid in zip(specs, ids):
                deps = [local_ids.get(dep, dep) for dep in spec.get("depends_on", [])]
                self.db.execute("UPDATE task SET depends_on=? WHERE id=?", (json.dumps(deps), tid))
            self.db.execute(
                "UPDATE proposal SET status='approved', decided_at=? WHERE id=?",
                (time.time(), proposal_id),
            )
            self._backup_dirty()
        return ids

    def reject_proposal(self, proposal_id: int, reason: str) -> None:
        if not reason.strip():
            raise ValueError("a rejection reason is required")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self._pending_proposal(proposal_id)
            self.db.execute(
                "UPDATE proposal SET status='rejected', decided_at=?, reason=? WHERE id=?",
                (time.time(), reason, proposal_id),
            )
            self._backup_dirty()

    def _pending_proposal(self, proposal_id: int) -> sqlite3.Row:
        row = self.one("SELECT * FROM proposal WHERE id=?", (proposal_id,))
        if row is None:
            raise ValueError(f"unknown proposal: {proposal_id}")
        if row["status"] != "pending":
            raise ValueError(
                f"proposal {proposal_id} is already {row['status']}; only pending proposals"
                f" can be changed. Inspect nc proposal {proposal_id} for details and revision links."
            )
        return row

    def set_task(self, task_id: str, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.db:
            self.db.execute(f"UPDATE task SET {cols} WHERE id=?", (*fields.values(), task_id))
            # An accepted task is the event which makes a repository change
            # owner-visible.  Calls that merely update another task field do not
            # cause backup churn.
            if fields.get("status") == "done" or fields.get("merge_commit") is not None:
                self._backup_dirty()

    # --- agents ----------------------------------------------------------
    def add_agent(self, agent_id: str, role: str, project_id: str, task_id: str | None,
                  model: str) -> str:
        now = time.time()
        self.x(
            "INSERT INTO agent(id,role,project_id,task_id,state,model,created_at,updated_at)"
            " VALUES(?,?,?,?,'runnable',?,?,?)",
            (agent_id, role, project_id, task_id, model, now, now),
        )
        return agent_id

    def planner_feedback(self, project_id: str | None, text: str, model: str,
                         task_id: str | None = None,
                         proposal_id: int | None = None,
                         plan_request: bool = False) -> tuple[str, int]:
        """Atomically resolve the project, store feedback, and create or wake its planner."""
        from .protocol import FEEDBACK

        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if task_id is not None and proposal_id is not None:
                raise ValueError("--task and --proposal are mutually exclusive")
            if proposal_id is not None:
                proposal = self._pending_proposal(proposal_id)
                if project_id is not None and project_id != proposal["project_id"]:
                    raise ValueError(
                        f"proposal {proposal_id} does not belong to project {project_id}"
                    )
                project_id = proposal["project_id"]
            if task_id is not None:
                task = self.one("SELECT project_id FROM task WHERE id=?", (task_id,))
                if task is None:
                    raise ValueError(f"unknown task: {task_id}")
                if project_id is not None and project_id != task["project_id"]:
                    raise ValueError(f"task {task_id} does not belong to project {project_id}")
                project_id = task["project_id"]
            if project_id is None:
                projects = self.q("SELECT id FROM project")
                if len(projects) != 1:
                    raise ValueError("specify --project (or --task) to select a project")
                project_id = projects[0]["id"]
            if self.one("SELECT id FROM project WHERE id=?", (project_id,)) is None:
                raise ValueError(f"unknown project: {project_id}")
            agent = self.one(
                "SELECT id FROM agent WHERE role='planner' AND project_id=?", (project_id,),
            )
            now = time.time()
            agent_id = agent["id"] if agent else f"planner-{project_id}"
            if agent:
                self.db.execute(
                    "UPDATE agent SET state='runnable', updated_at=? WHERE id=?", (now, agent_id),
                )
            else:
                self.db.execute(
                    "INSERT INTO agent(id,role,project_id,state,model,created_at,updated_at)"
                    " VALUES(?,'planner',?,'runnable',?,?,?)",
                    (agent_id, project_id, model, now, now),
                )
            cur = self.db.execute(
                "INSERT INTO message(kind,sender,recipient,task_id,payload,created_at)"
                " VALUES(?,'owner',?,?,?,?)",
                (FEEDBACK, agent_id, task_id,
                 json.dumps({"text": text, **({"request": "plan"} if plan_request else {})},
                            ensure_ascii=False), now),
            )
            message_id = int(cur.lastrowid)
            if proposal_id is not None:
                self.db.execute(
                    "UPDATE proposal SET status='superseded' WHERE id=?", (proposal_id,),
                )
                self.db.execute(
                    "INSERT INTO proposal_revision(original_id,feedback_id,planner_id)"
                    " VALUES(?,?,?)", (proposal_id, message_id, agent_id),
                )
            # Feedback (including a revision request) and its planner wakeup
            # form one committed owner action.
            self._backup_dirty()
            return agent_id, message_id

    def set_agent(self, agent_id: str, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE agent SET {cols} WHERE id=?", (*fields.values(), agent_id))

    # --- messages --------------------------------------------------------
    def send(self, kind: str, sender: str, recipient: str, payload: dict,
             task_id: str | None = None, in_reply_to: int | None = None) -> int:
        cur = self.x(
            "INSERT INTO message(kind,sender,recipient,task_id,payload,in_reply_to,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (kind, sender, recipient, task_id, json.dumps(payload, ensure_ascii=False),
             in_reply_to, time.time()),
        )
        return int(cur.lastrowid)

    def inbox(self, recipient: str, undelivered_only: bool = True) -> list[sqlite3.Row]:
        sql = "SELECT * FROM message WHERE recipient=?"
        if undelivered_only:
            sql += " AND delivered=0"
        return self.q(sql + " ORDER BY id", (recipient,))

    def mark_delivered(self, ids: Iterable[int]) -> None:
        for mid in ids:
            self.x("UPDATE message SET delivered=1 WHERE id=?", (mid,))

    # --- runs / incidents -------------------------------------------------
    def start_run(self, agent_id: str, task_id: str | None, role: str, model: str,
                  log_path: str) -> int:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.one(
                "SELECT 1 FROM task WHERE status='cancelled' AND"
                " (id=? OR id=(SELECT task_id FROM agent WHERE id=?))",
                (task_id, agent_id),
            ):
                raise ValueError("cannot start a run for a cancelled task")
            cur = self.db.execute(
                "INSERT INTO run(agent_id,task_id,role,model,log_path,started_at,owner_pid,owner_start,ownership_version)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (agent_id, task_id, role, model, log_path, time.time(), os.getpid(),
                 self._process_start(os.getpid()), 2),
            )
            return int(cur.lastrowid)

    @staticmethod
    def _process_start(pid: int) -> str | None:
        """Linux PID start ticks prevent PID reuse from looking like ownership."""
        try:
            # Field 22 is starttime. comm may contain spaces, hence rsplit.
            return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
        except (FileNotFoundError, IndexError, OSError):
            return None

    @staticmethod
    def _process_pgrp(pid: int) -> int | None:
        try:
            # stat field 5 (pgrp); comm may contain spaces.
            return int(Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[2])
        except (FileNotFoundError, IndexError, OSError, ValueError):
            return None

    def record_adapter_owner(self, run_id: int, pid: int) -> None:
        """Persist adapter session identity before waiting for untrusted work."""
        self.x("UPDATE run SET adapter_pid=?, adapter_start=?, adapter_pgid=?, adapter_cgroup=? "
               "WHERE id=? AND ended_at IS NULL",
               (pid, self._process_start(pid), self._process_pgrp(pid),
                self._adapter_cgroup(pid), run_id))

    @staticmethod
    def _adapter_cgroup(pid: int) -> str | None:
        """Return only cgroups created by the adapter launcher for this run."""
        try:
            for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines():
                if line.startswith("0::"):
                    path = line[3:]
                    return path if Path(path).name.startswith("neocortex-run-") else None
        except (FileNotFoundError, OSError):
            pass
        return None

    def end_run(self, run_id: int, outcome: str, detail: str = "", tokens: int | None = None) -> None:
        self.x(
            "UPDATE run SET outcome=?, detail=?, tokens=?, ended_at=? WHERE id=?",
            (outcome, detail[:4000], tokens, time.time(), run_id),
        )

    def record_host_assessment(self, run_id: int, *, exit_code: int | None,
                               timed_out: bool | None, category: str,
                               diagnostic: str, assessment: str) -> None:
        self.x(
            "UPDATE run SET exit_code=?, timed_out=?, terminal_category=?,"
            " terminal_diagnostic=?, host_assessment=? WHERE id=?",
            (exit_code, None if timed_out is None else int(timed_out), category,
             diagnostic[:1000], assessment, run_id),
        )

    def incident(self, kind: str, detail: str) -> int:
        cur = self.x(
            "INSERT INTO incident(kind,detail,created_at) VALUES(?,?,?)",
            (kind, detail[:4000], time.time()),
        )
        return int(cur.lastrowid)

    def open_incidents(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM incident WHERE resolved=0 ORDER BY id")

    def resolve_incident(self, incident_id: int, reason: str) -> bool:
        """Acknowledge one incident, preserving its first resolution and detail."""
        with self.db:
            row = self.one("SELECT resolved FROM incident WHERE id=?", (incident_id,))
            if row is None:
                raise ValueError(f"unknown incident: {incident_id}")
            if row["resolved"]:
                return False
            return bool(self.db.execute(
                "UPDATE incident SET resolved=1, resolved_at=?, resolution_note=?"
                " WHERE id=? AND resolved=0", (time.time(), reason, incident_id),
            ).rowcount)

    def resolve_open_incidents(self, reason: str) -> None:
        self.x(
            "UPDATE incident SET resolved=1, resolved_at=?, resolution_note=? WHERE resolved=0",
            (time.time(), reason),
        )
