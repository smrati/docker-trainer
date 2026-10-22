"""App-level SQLite storage: progress and attempt history.

This is the *application* database and is deliberately separate from every
artefact a learner builds. Those live in throwaway build contexts under the
system temporary directory; nothing a learner types can ever touch this file.

Four tables is enough -- completion rates, weak topics and time spent are all
``GROUP BY`` away. No ORM: one file, one local writer.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.config import DATABASE_PATH

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    exercise_id  TEXT    NOT NULL,
    code         TEXT    NOT NULL,
    passed       INTEGER NOT NULL DEFAULT 0,
    checks_passed INTEGER,
    checks_total  INTEGER,
    duration_ms  REAL,
    error        TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_attempts_exercise ON attempts (exercise_id);

-- Revealing a solution is tracked separately from solving one, so the
-- dashboard can report both honestly instead of collapsing them.
CREATE TABLE IF NOT EXISTS exercise_progress (
    exercise_id    TEXT PRIMARY KEY,
    solved_at      TEXT,
    revealed_at    TEXT,
    last_code      TEXT,
    attempts_count INTEGER NOT NULL DEFAULT 0
);

-- Spaced-repetition scheduling, one row per reviewed exercise. Separate from
-- exercise_progress on purpose: an exercise can be solved but never scheduled
-- (if its solution was revealed), and scheduled but never solved is impossible.
CREATE TABLE IF NOT EXISTS review_state (
    exercise_id      TEXT PRIMARY KEY,
    stability_days   REAL    NOT NULL DEFAULT 0,
    reps             INTEGER NOT NULL DEFAULT 0,
    lapses           INTEGER NOT NULL DEFAULT 0,
    last_result      TEXT,
    last_reviewed_at TEXT,
    due_at           TEXT
);

CREATE INDEX IF NOT EXISTS idx_review_due ON review_state (due_at);
"""

# Applied in order as the schema version advances. Each entry is a list of
# statements, so a version can make more than one change.
MIGRATIONS: dict[int, list[str]] = {
    2: [
        """CREATE TABLE IF NOT EXISTS review_state (
            exercise_id      TEXT PRIMARY KEY,
            stability_days   REAL    NOT NULL DEFAULT 0,
            reps             INTEGER NOT NULL DEFAULT 0,
            lapses           INTEGER NOT NULL DEFAULT 0,
            last_result      TEXT,
            last_reviewed_at TEXT,
            due_at           TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_review_due ON review_state (due_at)",
    ],
}


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open a connection with row access by name and FK enforcement.

    ``path`` defaults to the module-level ``DATABASE_PATH`` read at call time
    rather than bound as a default argument, so tests can redirect the
    database by patching it.
    """
    path = Path(path or DATABASE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """Per-request connection.

    A fresh connection per request sidesteps sqlite3's same-thread rule
    entirely -- sync endpoints run in a threadpool, and the app database is
    small enough that reopening is free.
    """
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(path: str | Path | None = None) -> None:
    """Create tables if needed and apply migrations for a newer schema."""
    conn = connect(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            conn.executescript(SCHEMA)
            version = SCHEMA_VERSION
        elif version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema version {version} is newer than this app "
                f"understands ({SCHEMA_VERSION})"
            )
        else:
            for target in range(version + 1, SCHEMA_VERSION + 1):
                for statement in MIGRATIONS.get(target, []):
                    conn.execute(statement)
            version = SCHEMA_VERSION

        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
    finally:
        conn.close()
