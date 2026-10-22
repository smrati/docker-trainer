"""Tests for the HTTP API.

These exercise the contract the frontend depends on, so a rename or shape
change breaks here rather than silently in the browser.

The endpoints that really build an image are marked ``slow`` and skipped
when the daemon is unreachable. The read-only endpoints are never skipped:
they are the ones a learner hits first, and they are the ones that must work
even on a machine with no Docker at all.
"""

import pytest
from fastapi.testclient import TestClient

from app.engine.cli import daemon_available
from app.main import app

# An exercise with a Dockerfile, one check, and one case: the cheapest
# possible real build.
EXERCISE = "f-run-a-container"

DOCKERFILE = 'FROM alpine:3.20\n\nCMD ["echo", "hello from docker"]\n'

needs_docker = pytest.mark.skipif(
    not daemon_available(), reason="no Docker daemon on this machine"
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A client backed by a throwaway app database."""
    monkeypatch.setattr("app.db.DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr("app.config.DATABASE_PATH", tmp_path / "test.db")
    with TestClient(app) as c:
        yield c


class TestHealth:
    def test_reports_the_loaded_curriculum(self, client):
        data = client.get("/api/health").json()
        assert data["status"] == "ok"
        assert data["tracks"] == 3
        assert data["exercises"] >= 60

    def test_reports_the_docker_daemon(self, client):
        """The daemon's state is reported rather than enforced: the lessons
        still work without it."""
        data = client.get("/api/health").json()
        assert "available" in data["docker"]
        assert isinstance(data["docker"]["available"], bool)

    def test_counts_the_checks(self, client):
        data = client.get("/api/health").json()
        assert data["checks"] >= data["exercises"]


class TestCurriculum:
    def test_the_tree_lists_tracks_and_exercises(self, client):
        data = client.get("/api/curriculum").json()
        assert len(data["tracks"]) == 3
        foundations = data["tracks"][0]
        assert foundations["difficulty"] == "beginner"
        assert foundations["exercise_count"] == sum(
            lesson["exercise_count"] for lesson in foundations["lessons"]
        )
        first = foundations["lessons"][0]["exercises"][0]
        assert set(first) >= {"id", "title", "status", "attempts", "kind"}

    def test_the_summary_starts_empty(self, client):
        summary = client.get("/api/curriculum").json()["summary"]
        assert summary["solved"] == 0
        assert summary["revealed"] == 0
        assert summary["total"] > 0

    def test_an_unknown_lesson_is_404(self, client):
        assert client.get("/api/lessons/nope").status_code == 404

    def test_an_unknown_exercise_is_404(self, client):
        assert client.get("/api/exercises/nope").status_code == 404

    def test_a_404_names_what_was_missing(self, client):
        response = client.get("/api/exercises/nope")
        assert "nope" in response.json()["detail"]


class TestLesson:
    def test_a_lesson_carries_its_text_and_exercises(self, client):
        data = client.get("/api/lessons/f-images-and-containers").json()
        assert data["title"]
        assert data["body"]
        assert data["exercises"]

    def test_a_lesson_never_exposes_a_solution(self, client):
        data = client.get("/api/lessons/f-images-and-containers").json()
        assert "solution" not in data
        for exercise in data["exercises"]:
            assert "solution" not in exercise


class TestExerciseDetail:
    def test_it_never_exposes_the_solution(self, client):
        data = client.get(f"/api/exercises/{EXERCISE}").json()
        assert "solution" not in data
        assert data["files"]
        assert data["hint_count"] >= 1

    def test_it_exposes_the_starter_files(self, client):
        """The editor needs them, and a learner should never face an empty
        editor."""
        data = client.get(f"/api/exercises/{EXERCISE}").json()
        assert "Dockerfile" in data["files"]
        assert data["entry_file"] == "Dockerfile"

    def test_it_names_the_checks_without_their_answers(self, client):
        """Knowing what is checked is the specification; knowing the expected
        value would be the answer."""
        data = client.get(f"/api/exercises/{EXERCISE}").json()
        assert data["checks"]
        for check in data["checks"]:
            assert set(check) == {"name", "kind"}

    def test_it_describes_the_cases(self, client):
        data = client.get(f"/api/exercises/{EXERCISE}").json()
        assert data["case_count"] >= 1
        assert data["cases"][0]["label"]


class TestHints:
    def test_a_hint_is_returned_by_index(self, client):
        data = client.get(f"/api/exercises/{EXERCISE}/hints/0").json()
        assert data["index"] == 0
        assert data["text"]
        assert data["total"] >= 1

    def test_an_out_of_range_hint_is_404(self, client):
        assert client.get(f"/api/exercises/{EXERCISE}/hints/99").status_code == 404

    def test_a_negative_hint_is_404(self, client):
        assert client.get(f"/api/exercises/{EXERCISE}/hints/-1").status_code == 404


class TestReveal:
    def test_revealing_returns_the_solution_and_records_the_reveal(self, client):
        response = client.post(f"/api/exercises/{EXERCISE}/solution")
        assert response.status_code == 200
        data = response.json()
        assert data["solution"]
        assert data["state"]["status"] == "revealed"

    def test_revealing_is_not_a_solve(self, client):
        client.post(f"/api/exercises/{EXERCISE}/solution")
        summary = client.get("/api/curriculum").json()["summary"]
        assert summary["solved"] == 0
        assert summary["revealed"] == 1


class TestProgress:
    def test_progress_starts_empty(self, client):
        data = client.get("/api/progress").json()
        assert data["summary"]["solved"] == 0
        assert data["recent_attempts"] == []

    def test_progress_can_be_reset(self, client):
        client.post(f"/api/exercises/{EXERCISE}/solution")
        assert client.delete("/api/progress").json()["reset"] is True
        assert client.get("/api/progress").json()["summary"]["revealed"] == 0


class TestSubmissionShape:
    """The shape of a submission is checked before anything is built."""

    def test_too_many_files_are_refused(self, client):
        files = {f"f{i}": "" for i in range(100)}
        response = client.post(f"/api/exercises/{EXERCISE}/run", json={"files": files})
        assert response.status_code == 400
        assert "at most" in response.json()["detail"]

    def test_an_oversized_file_is_refused(self, client):
        response = client.post(
            f"/api/exercises/{EXERCISE}/run",
            json={"files": {"Dockerfile": "x" * 300_000}},
        )
        assert response.status_code == 400

    def test_an_empty_submission_is_accepted_by_the_shape_check(self, client):
        """An empty submission is a valid *shape*; it is graded as wrong."""
        response = client.post(f"/api/exercises/{EXERCISE}/run", json={"files": {}})
        assert response.status_code == 200


class TestReview:
    def test_the_review_page_reports_counts_and_concepts(self, client):
        data = client.get("/api/review").json()
        assert data["counts"]["due_now"] == 0
        assert data["concepts"]
        for concept in data["concepts"]:
            assert concept["label"]
            assert concept["description"]

    def test_every_concept_has_a_label(self, client):
        data = client.get("/api/review").json()
        for concept in data["concepts"]:
            assert concept["label"] != concept["tag"] or concept["description"]


@needs_docker
@pytest.mark.slow
class TestRealBuilds:
    """These really build an image. They need a daemon and take seconds."""

    def test_running_builds_the_image_and_reports_its_checks(self, client):
        response = client.post(
            f"/api/exercises/{EXERCISE}/run",
            json={"files": {"Dockerfile": DOCKERFILE}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["checks"]
        assert data["checks"][0]["stdout"].strip() == "hello from docker"

    def test_running_records_no_progress(self, client):
        """Exploring must never mark anything as complete."""
        client.post(
            f"/api/exercises/{EXERCISE}/run",
            json={"files": {"Dockerfile": DOCKERFILE}},
        )
        assert client.get("/api/progress").json()["recent_attempts"] == []

    def test_a_correct_submission_passes(self, client):
        response = client.post(
            f"/api/exercises/{EXERCISE}/submit",
            json={"files": {"Dockerfile": DOCKERFILE}},
        )
        data = response.json()
        assert data["ok"] is True
        assert data["grading"]["passed"] is True
        assert data["state"]["status"] == "solved"

    def test_a_wrong_submission_fails_with_a_diff(self, client):
        response = client.post(
            f"/api/exercises/{EXERCISE}/submit",
            json={"files": {"Dockerfile": 'FROM alpine:3.20\nCMD ["echo", "wrong"]\n'}},
        )
        data = response.json()
        assert data["ok"] is True
        assert data["grading"]["passed"] is False
        assert data["grading"]["checks"]
        assert data["grading"]["detail"]

    def test_a_submission_is_recorded(self, client):
        client.post(
            f"/api/exercises/{EXERCISE}/submit",
            json={"files": {"Dockerfile": DOCKERFILE}},
        )
        attempts = client.get("/api/progress").json()["recent_attempts"]
        assert len(attempts) == 1
        assert attempts[0]["exercise_id"] == EXERCISE

    def test_a_failed_build_is_feedback_not_a_crash(self, client):
        response = client.post(
            f"/api/exercises/{EXERCISE}/submit",
            json={"files": {"Dockerfile": "FROM alpine:3.20\nRUN nonexistent-command\n"}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert data["error"]["kind"] == "build_failed"
        assert data["error"]["message"]
        assert data["result"]["build_log"]

    def test_a_path_escaping_the_context_is_refused(self, client):
        response = client.post(
            f"/api/exercises/{EXERCISE}/submit",
            json={"files": {"../escape": "x"}},
        )
        data = response.json()
        assert data["ok"] is False
        assert data["error"]["kind"] == "invalid_path"

    def test_a_correct_submission_lands_in_the_review_schedule(self, client):
        client.post(
            f"/api/exercises/{EXERCISE}/submit",
            json={"files": {"Dockerfile": DOCKERFILE}},
        )
        review = client.get("/api/review").json()
        assert review["counts"]["scheduled"] == 1
