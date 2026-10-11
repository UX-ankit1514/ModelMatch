"""V2's memory: a small SQLite database of routing decisions, outcomes and usage.

Only metadata is stored: task categories, model names, choices, outcomes, token
counts. Never the prompt, the task's wording, request or response bodies,
headers, tokens or keys.

If the database can't be opened, V2 keeps working without memory (fail open).
"""

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SCHEMA_VERSION = 1
KEEP_DAYS = 180

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    client TEXT NOT NULL,
    session_id TEXT NOT NULL,
    task_id TEXT,
    project_id TEXT,
    prompt_fp TEXT,
    prompt_chars INTEGER,
    analysis_source TEXT,
    analysis_model TEXT,
    analysis_latency_ms INTEGER,
    analysis_error TEXT,
    task_type TEXT,
    secondary_task_type TEXT,
    secondary_probability REAL,
    complexity TEXT,
    reasoning TEXT,
    scope TEXT,
    risk TEXT,
    expected_output TEXT,
    expected_steps TEXT,
    semantic_tier TEXT,
    semantic_confidence REAL,
    same_task_probability REAL,
    followup_kind TEXT,
    explicit_model TEXT,
    same_task INTEGER,
    current_model TEXT,
    effective_model TEXT,
    candidate_model TEXT,
    final_model TEXT,
    intervention TEXT,
    confidence REAL,
    reason_codes TEXT,
    explanation TEXT,
    signals TEXT,
    suggested_lower INTEGER DEFAULT 0,
    routing_enabled INTEGER,
    user_choice TEXT,
    accepted INTEGER,
    decided_by TEXT,
    decided_at REAL,
    ran_model TEXT,
    ran_family TEXT,
    route_failed INTEGER DEFAULT 0,
    outcome TEXT,
    outcome_at REAL,
    next_followup TEXT,
    escalated_to TEXT,
    retry_detected INTEGER,
    switched_back INTEGER,
    manual_override INTEGER,
    refusal_detected INTEGER,
    error_detected INTEGER,
    requests INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS decisions_by_session ON decisions (client, session_id, ts);
CREATE INDEX IF NOT EXISTS decisions_by_task ON decisions (client, task_type, ts);

CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    client TEXT NOT NULL,
    session_id TEXT NOT NULL,
    decision_id TEXT,
    original_model TEXT,
    sent_model TEXT,
    routed INTEGER,
    status INTEGER,
    stop_reason TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    cache_write_5m INTEGER,
    cache_write_1h INTEGER,
    requested_ttl INTEGER,
    billing_mode TEXT,
    request_class TEXT,
    latency_ms INTEGER,
    rate_limits TEXT
);
CREATE INDEX IF NOT EXISTS usage_by_session ON usage_events (client, session_id, ts);

CREATE TABLE IF NOT EXISTS tasks (
    client TEXT NOT NULL,
    session_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    task_type TEXT,
    tier TEXT,
    model TEXT,
    source TEXT,
    created_at REAL,
    last_validated REAL,
    confidence REAL,
    PRIMARY KEY (client, session_id)
);
"""

def _json(value: Any) -> Optional[str]:
    return None if value is None else json.dumps(value, separators=(",", ":"), default=str)


class Store:
    """Thread-safe wrapper around one SQLite connection. Every method fails soft."""

    def __init__(self, path: Optional[Path]):
        self.path = path
        self._lock = threading.Lock()
        self._db: Optional[sqlite3.Connection] = None
        self.error: Optional[str] = None
        self._columns: Dict[str, List[str]] = {}
        self._open()

    # -- setup ------------------------------------------------------------------

    def _open(self) -> None:
        try:
            if self.path is None:
                self._db = sqlite3.connect(":memory:", check_same_thread=False)
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5)
                self._db.execute("PRAGMA journal_mode=WAL")
                self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.row_factory = sqlite3.Row
            self._db.executescript(_SCHEMA)
            row = self._db.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                self._db.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            self._db.commit()
            for table in ("decisions", "usage_events", "tasks"):
                self._columns[table] = [r["name"] for r in self._db.execute("PRAGMA table_info({})".format(table))]
            self._prune()
            if self.path is not None:
                try:
                    self.path.chmod(0o600)
                except OSError:
                    pass
        except (sqlite3.Error, OSError) as exc:
            self.error = "{}: {}".format(type(exc).__name__, exc)
            self._db = None

    @property
    def ok(self) -> bool:
        return self._db is not None

    def _prune(self) -> None:
        cutoff = time.time() - KEEP_DAYS * 86400
        self._db.execute("DELETE FROM decisions WHERE ts < ?", (cutoff,))
        self._db.execute("DELETE FROM usage_events WHERE ts < ?", (cutoff,))
        self._db.commit()

    def _run(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Cursor]:
        if self._db is None:
            return None
        with self._lock:
            try:
                cursor = self._db.execute(sql, tuple(params))
                self._db.commit()
                return cursor
            except sqlite3.Error as exc:
                self.error = "{}: {}".format(type(exc).__name__, exc)
                return None

    def _rows(self, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        if self._db is None:
            return []
        with self._lock:
            try:
                return [dict(r) for r in self._db.execute(sql, tuple(params)).fetchall()]
            except sqlite3.Error as exc:
                self.error = "{}: {}".format(type(exc).__name__, exc)
                return []

    def _insert(self, table: str, row: Dict[str, Any], replace: bool = False) -> None:
        columns = [c for c in row if c in self._columns.get(table, ())]
        if not columns:
            return
        values = [_json(row[c]) if isinstance(row[c], (dict, list, tuple)) else row[c] for c in columns]
        self._run("INSERT {}INTO {} ({}) VALUES ({})".format("OR REPLACE " if replace else "", table,
                                                              ",".join(columns), ",".join("?" * len(columns))),
                  values)

    # -- decisions --------------------------------------------------------------

    def insert_decision(self, row: Dict[str, Any]) -> None:
        self._insert("decisions", row)

    def update_decision(self, decision_id: str, **fields: Any) -> None:
        columns = [c for c in fields if c in self._columns.get("decisions", ())]
        if not columns:
            return
        values = [_json(fields[c]) if isinstance(fields[c], (dict, list, tuple)) else fields[c] for c in columns]
        self._run("UPDATE decisions SET {} WHERE id = ?".format(", ".join(c + " = ?" for c in columns)),
                  values + [decision_id])

    def decision(self, decision_id: str) -> Optional[Dict[str, Any]]:
        rows = self._rows("SELECT * FROM decisions WHERE id = ?", (decision_id,))
        return _decode(rows[0]) if rows else None

    def last_decision(self, client: str, session_id: str) -> Optional[Dict[str, Any]]:
        rows = self._rows("SELECT * FROM decisions WHERE client = ? AND session_id = ? ORDER BY ts DESC LIMIT 1",
                          (client, session_id))
        return _decode(rows[0]) if rows else None

    def decisions_for(self, client: str, task_type: str, since: float) -> List[Dict[str, Any]]:
        rows = self._rows("SELECT * FROM decisions WHERE client = ? AND task_type = ? AND ts >= ? ORDER BY ts DESC",
                          (client, task_type, since))
        return [_decode(r) for r in rows]

    def decisions_since(self, since: float, client: Optional[str] = None) -> List[Dict[str, Any]]:
        if client:
            rows = self._rows("SELECT * FROM decisions WHERE ts >= ? AND client = ? ORDER BY ts", (since, client))
        else:
            rows = self._rows("SELECT * FROM decisions WHERE ts >= ? ORDER BY ts", (since,))
        return [_decode(r) for r in rows]

    # -- usage ------------------------------------------------------------------

    def add_usage(self, row: Dict[str, Any]) -> None:
        self._insert("usage_events", row)
        decision_id = row.get("decision_id")
        if decision_id:
            self._run("UPDATE decisions SET requests = requests + 1, input_tokens = input_tokens + ?, "
                      "output_tokens = output_tokens + ?, cache_read_tokens = cache_read_tokens + ?, "
                      "cache_write_tokens = cache_write_tokens + ? WHERE id = ?",
                      (row.get("input_tokens") or 0, row.get("output_tokens") or 0,
                       row.get("cache_read_tokens") or 0, row.get("cache_write_tokens") or 0, decision_id))

    def usage_for_session(self, client: str, session_id: str, since: float) -> List[Dict[str, Any]]:
        return self._rows("SELECT * FROM usage_events WHERE client = ? AND session_id = ? AND ts >= ? ORDER BY ts",
                          (client, session_id, since))

    def usage_for_decision(self, decision_id: str) -> List[Dict[str, Any]]:
        return self._rows("SELECT * FROM usage_events WHERE decision_id = ? ORDER BY ts", (decision_id,))

    def usage_since(self, since: float, client: Optional[str] = None) -> List[Dict[str, Any]]:
        if client:
            return self._rows("SELECT * FROM usage_events WHERE ts >= ? AND client = ?", (since, client))
        return self._rows("SELECT * FROM usage_events WHERE ts >= ?", (since,))

    # -- the current task (and its routing lease) per session --------------------

    def task(self, client: str, session_id: str) -> Optional[Dict[str, Any]]:
        rows = self._rows("SELECT * FROM tasks WHERE client = ? AND session_id = ?", (client, session_id))
        return rows[0] if rows else None

    def set_task(self, client: str, session_id: str, **fields: Any) -> None:
        row = dict(fields, client=client, session_id=session_id)
        self._insert("tasks", row, replace=True)

    def clear_task(self, client: str, session_id: str) -> None:
        self._run("DELETE FROM tasks WHERE client = ? AND session_id = ?", (client, session_id))


def _decode(row: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("reason_codes", "signals"):
        value = row.get(key)
        if isinstance(value, str):
            try:
                row[key] = json.loads(value)
            except ValueError:
                pass
    return row
