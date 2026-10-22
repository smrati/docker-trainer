"""Tests for the review endpoint.

The review queue is the app's memory of what you have forgotten. Three
things must be true of it: it must never claim you have retained something
you only copied, it must never 500 because an exercise was renamed, and it
must always say when the next thing is due -- an empty queue that says
"nothing" and stops reads as a dead end rather than as "come back Thursday".
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("app.db.DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr("app.config.DATABASE_PATH", tmp_path / "test.db")
    with TestClient(app) as c:
        yield c


class TestTheQueue:
    def test_it_starts_empty(self, client):
        data = client.get("/api/review").json()
        assert data["counts"]["due_now"] == 0
        assert data["due"] == []

    def test_it_reports_every_concept(self, client):
        data = client.get("/api/review").json()
        tags = {concept["tag"] for concept in data["concepts"]}
        assert "dockerfile" in tags
        assert "compose" in tags

    def test_every_concept_carries_a_label_and_a_description(self, client):
        data = client.get("/api/review").json()
        for concept in data["concepts"]:
            assert concept["label"], concept["tag"]
            assert concept["description"], concept["tag"]

    def test_a_revealed_exercise_is_not_scheduled(self, client):
        """Opening the answer and reproducing it proves you can copy, not
        that you retained anything."""
        client.post("/api/exercises/f-run-a-container/solution")
        data = client.get("/api/review").json()
        assert data["counts"]["scheduled"] == 0

    def test_a_stale_row_does_not_break_the_page(self, client):
        """An exercise that has been renamed leaves a row behind. Dropping it
        is kinder than failing the whole page."""
        from app.db import get_conn
        from app.srs import format_ts, utcnow

        with get_conn() as conn:
            conn.execute(
                "INSERT INTO review_state (exercise_id, stability_days, reps, lapses, "
                "last_result, last_reviewed_at, due_at) VALUES (?, 1, 1, 0, 'pass', ?, ?)",
                (
                    "an-exercise-that-no-longer-exists",
                    format_ts(utcnow()),
                    format_ts(utcnow()),
                ),
            )

        response = client.get("/api/review")
        assert response.status_code == 200
        data = response.json()
        assert data["counts"]["stale_entries"] == 1
        assert "an-exercise-that-no-longer-exists" not in [
            entry["exercise_id"] for entry in data["due"]
        ]

    def test_the_next_due_date_is_reported(self, client):
        """Without this the page says "nothing due" and stops."""
        data = client.get("/api/review").json()
        assert "next_due_at" in data["counts"]
        assert "next_due_in_days" in data["counts"]

    def test_the_review_counts_add_up(self, client):
        counts = client.get("/api/review").json()["counts"]
        assert counts["solved"] >= 0
        assert counts["scheduled"] >= counts["due_now"]
        assert counts["due_now"] + counts["due_soon"] <= counts["scheduled"] + counts["due_now"]
