"""Tests for schema creation and migration.

Migration bugs are the classic silent failure: the app starts, the feature
seems to work on a fresh database, and an existing user's data quietly goes
wrong. These run against real SQLite files rather than mocks.
"""

import sqlite3

import pytest

from app.db import SCHEMA_VERSION, connect, init_db


def user_version(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def tables(path):
    conn = sqlite3.connect(path)
    try:
        return {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()


def make_v1(path):
    """A schema-v1 database, as an earlier release of this app would leave."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exercise_id TEXT NOT NULL,
            code TEXT NOT NULL,
            passed INTEGER NOT NULL DEFAULT 0,
            duration_ms REAL,
            error TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE exercise_progress (
            exercise_id TEXT PRIMARY KEY,
            solved_at TEXT,
            revealed_at TEXT,
            last_code TEXT,
            attempts_count INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.execute("INSERT INTO attempts (exercise_id, code, passed) VALUES ('old', 'x', 1)")
    conn.execute(
        "INSERT INTO exercise_progress (exercise_id, solved_at) "
        "VALUES ('old', '2026-01-01 00:00:00')"
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()


class TestCreation:
    def test_a_fresh_database_has_every_table(self, tmp_path):
        path = tmp_path / "test.db"
        init_db(path)
        names = tables(path)
        assert {"attempts", "exercise_progress", "review_state"} <= names

    def test_a_fresh_database_is_at_the_current_version(self, tmp_path):
        path = tmp_path / "test.db"
        init_db(path)
        assert user_version(path) == SCHEMA_VERSION

    def test_initialising_twice_is_harmless(self, tmp_path):
        path = tmp_path / "test.db"
        init_db(path)
        init_db(path)
        assert user_version(path) == SCHEMA_VERSION

    def test_the_parent_directory_is_created(self, tmp_path):
        path = tmp_path / "nested" / "deeper" / "test.db"
        init_db(path)
        assert path.exists()


class TestMigration:
    def test_an_old_database_is_migrated(self, tmp_path):
        path = tmp_path / "test.db"
        make_v1(path)
        init_db(path)
        assert user_version(path) == SCHEMA_VERSION
        assert "review_state" in tables(path)

    def test_migration_preserves_existing_progress(self, tmp_path):
        """The point of a migration is that nothing is lost."""
        path = tmp_path / "test.db"
        make_v1(path)
        init_db(path)

        conn = sqlite3.connect(path)
        try:
            attempts = conn.execute(
                "SELECT COUNT(*) FROM attempts WHERE exercise_id = 'old'"
            ).fetchone()[0]
            solved = conn.execute(
                "SELECT COUNT(*) FROM exercise_progress "
                "WHERE exercise_id = 'old' AND solved_at IS NOT NULL"
            ).fetchone()[0]
        finally:
            conn.close()

        assert attempts == 1
        assert solved == 1

    def test_a_newer_database_is_refused(self, tmp_path):
        """Silently downgrading a database would destroy data."""
        path = tmp_path / "test.db"
        init_db(path)
        conn = sqlite3.connect(path)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 10}")
        conn.commit()
        conn.close()

        with pytest.raises(RuntimeError, match="newer than this app understands"):
            init_db(path)


class TestProgressState:
    """The three states, and the transitions between them."""

    def test_an_unknown_exercise_is_unsolved(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.db.DATABASE_PATH", tmp_path / "test.db")
        init_db()
        from app.progress import get_state

        state = get_state("nope")
        assert state.status == "unsolved"
        assert state.attempts == 0

    def test_a_pass_marks_the_exercise_solved(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.db.DATABASE_PATH", tmp_path / "test.db")
        init_db()
        from app.progress import get_state, record_attempt

        record_attempt("e", "code", True)
        assert get_state("e").status == "solved"

    def test_a_failure_leaves_it_unsolved(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.db.DATABASE_PATH", tmp_path / "test.db")
        init_db()
        from app.progress import get_state, record_attempt

        record_attempt("e", "code", False)
        assert get_state("e").status == "unsolved"
        assert get_state("e").attempts == 1

    def test_a_reveal_wins_over_a_solve(self, tmp_path, monkeypatch):
        """Revealing then passing is recorded honestly as a pass, but the
        exercise stays in the `revealed` bucket."""
        monkeypatch.setattr("app.db.DATABASE_PATH", tmp_path / "test.db")
        init_db()
        from app.progress import get_state, record_attempt, record_reveal

        record_attempt("e", "code", True)
        record_reveal("e")
        assert get_state("e").status == "revealed"

    def test_a_reveal_after_a_solve_does_not_rewrite_history(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.db.DATABASE_PATH", tmp_path / "test.db")
        init_db()
        from app.progress import get_state, record_attempt, record_reveal

        record_attempt("e", "code", True)
        record_reveal("e")
        assert get_state("e").solved_at is not None

    def test_the_attempt_count_accumulates(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.db.DATABASE_PATH", tmp_path / "test.db")
        init_db()
        from app.progress import get_state, record_attempt

        for _ in range(3):
            record_attempt("e", "code", False)
        assert get_state("e").attempts == 3

    def test_the_last_code_is_kept(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.db.DATABASE_PATH", tmp_path / "test.db")
        init_db()
        from app.progress import get_state, record_attempt

        record_attempt("e", "first", False)
        record_attempt("e", "second", False)
        assert get_state("e").last_code == "second"

    def test_the_summary_never_merges_solved_and_revealed(self, tmp_path, monkeypatch):
        """A percentage that silently includes copied answers is worse than no
        percentage."""
        monkeypatch.setattr("app.db.DATABASE_PATH", tmp_path / "test.db")
        init_db()
        from app.progress import all_states, record_attempt, record_reveal, summarise

        record_attempt("a", "code", True)
        record_attempt("b", "code", False)
        record_reveal("c")

        summary = summarise(all_states(), ["a", "b", "c", "d"])
        assert summary["solved"] == 1
        assert summary["revealed"] == 1
        assert summary["unsolved"] == 2
        assert summary["total"] == 4


class TestConnections:
    def test_connect_returns_rows_by_name(self, tmp_path):
        conn = connect(tmp_path / "test.db")
        try:
            conn.execute("CREATE TABLE t (a TEXT)")
            conn.execute("INSERT INTO t VALUES ('x')")
            conn.commit()
            assert conn.execute("SELECT a FROM t").fetchone()["a"] == "x"
        finally:
            conn.close()

    def test_foreign_keys_are_on(self, tmp_path):
        conn = connect(tmp_path / "test.db")
        try:
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        finally:
            conn.close()
