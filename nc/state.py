"""SQLite state: the single source of truth for projects, tasks, agents and messages."""

from __future__ import annotations

import json
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
    state      TEXT NOT NULL,             -- runnable|blocked|done|failed
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
    ended_at   REAL
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
"""


class State:
    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        """Apply additive schema changes to new and existing databases."""
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS proposal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL REFERENCES project(id),
                source TEXT NOT NULL,
                rationale TEXT NOT NULL,
                spec TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'approved', 'rejected')),
                created_at REAL NOT NULL,
                decided_at REAL,
                reason TEXT
            )
        """)
        for table, column, decl in (
            ("proposal", "findings", "TEXT NOT NULL DEFAULT '[]'"),
            ("project", "mirror", "TEXT"),
            ("task", "merge_commit", "TEXT"),
            ("task", "depends_on", "TEXT NOT NULL DEFAULT '[]'"),
        ):
            known = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if column not in known:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

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
                  depends_on: list[str] | None = None) -> str:
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
                     spec: list[dict]) -> int:
        if not isinstance(spec, list) or any(
            not isinstance(task, dict) or task.get("project") != project_id for task in spec
        ):
            raise ValueError("proposal specs must be a list of tasks for its project")
        cur = self.x(
            "INSERT INTO proposal(project_id,source,rationale,spec,created_at,findings)"
            " VALUES(?,?,?,?,?,?)",
            (project_id, source, rationale, json.dumps(spec, ensure_ascii=False), time.time(),
             json.dumps(self._proposal_findings(spec))),
        )
        return int(cur.lastrowid)

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
            for spec in specs:
                if spec["project"] != row["project_id"]:
                    raise ValueError("proposed task belongs to another project")
                ids.append(self._add_task_spec(spec))
            local_ids = {spec["id"]: tid for spec, tid in zip(specs, ids) if spec.get("id")}
            for spec, tid in zip(specs, ids):
                deps = [local_ids.get(dep, dep) for dep in spec.get("depends_on", [])]
                self.db.execute("UPDATE task SET depends_on=? WHERE id=?", (json.dumps(deps), tid))
            self.db.execute(
                "UPDATE proposal SET status='approved', decided_at=? WHERE id=?",
                (time.time(), proposal_id),
            )
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

    def _pending_proposal(self, proposal_id: int) -> sqlite3.Row:
        row = self.one("SELECT * FROM proposal WHERE id=?", (proposal_id,))
        if row is None:
            raise ValueError(f"unknown proposal: {proposal_id}")
        if row["status"] != "pending":
            raise ValueError(f"proposal {proposal_id} is already {row['status']}")
        return row

    def set_task(self, task_id: str, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE task SET {cols} WHERE id=?", (*fields.values(), task_id))

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
                         task_id: str | None = None) -> tuple[str, int]:
        """Atomically resolve the project, store feedback, and create or wake its planner."""
        from .protocol import FEEDBACK

        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
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
                 json.dumps({"text": text}, ensure_ascii=False), now),
            )
            return agent_id, int(cur.lastrowid)

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
        cur = self.x(
            "INSERT INTO run(agent_id,task_id,role,model,log_path,started_at)"
            " VALUES(?,?,?,?,?,?)",
            (agent_id, task_id, role, model, log_path, time.time()),
        )
        return int(cur.lastrowid)

    def end_run(self, run_id: int, outcome: str, detail: str = "", tokens: int | None = None) -> None:
        self.x(
            "UPDATE run SET outcome=?, detail=?, tokens=?, ended_at=? WHERE id=?",
            (outcome, detail[:4000], tokens, time.time(), run_id),
        )

    def incident(self, kind: str, detail: str) -> int:
        cur = self.x(
            "INSERT INTO incident(kind,detail,created_at) VALUES(?,?,?)",
            (kind, detail[:4000], time.time()),
        )
        return int(cur.lastrowid)

    def open_incidents(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM incident WHERE resolved=0 ORDER BY id")
