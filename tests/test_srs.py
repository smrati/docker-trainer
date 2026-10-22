"""Tests for spaced repetition.

The scheduling model is deliberately small, and the small parts are the ones
that matter: a failure must come back *immediately*, and a revealed
exercise must never be scheduled at all. Both are decisions about what the
app is willing to claim about somebody's memory.
"""

from datetime import datetime, timedelta

from app.srs import (
    INITIAL_INTERVAL_DAYS,
    MAX_INTERVAL_DAYS,
    ReviewState,
    apply_review,
    build_queue,
    concept_breakdown,
    parse_ts,
    schedule_fail,
    schedule_pass,
    utcnow,
)


class TestScheduling:
    def test_the_first_pass_is_due_tomorrow(self):
        state = schedule_pass(ReviewState("e"), datetime(2026, 1, 1))
        assert state.reps == 1
        assert state.stability_days == INITIAL_INTERVAL_DAYS
        assert state.due_at == "2026-01-02 00:00:00"

    def test_the_interval_widens_with_each_pass(self):
        state = ReviewState("e")
        intervals = []
        for _ in range(4):
            state = schedule_pass(state, datetime(2026, 1, 1))
            intervals.append(state.stability_days)
        assert intervals == sorted(intervals)
        assert intervals[1] > intervals[0]

    def test_the_interval_is_capped(self):
        """Otherwise a consistently-passed exercise would schedule itself
        centuries out and never be seen again."""
        state = ReviewState("e", stability_days=MAX_INTERVAL_DAYS, reps=1)
        state = schedule_pass(state, datetime(2026, 1, 1))
        assert state.stability_days == MAX_INTERVAL_DAYS

    def test_a_failure_is_due_immediately(self):
        """'Review' should mean 'redo what you got wrong'."""
        state = schedule_fail(ReviewState("e", stability_days=40.0), datetime(2026, 1, 1))
        assert state.due_at == "2026-01-01 00:00:00"
        assert state.stability_days == 0.0
        assert state.lapses == 1

    def test_a_failure_resets_the_ladder(self):
        """The next pass earns a full day rather than topping up a long
        interval."""
        state = ReviewState("e", stability_days=40.0, reps=3)
        state = schedule_fail(state, datetime(2026, 1, 1))
        state = schedule_pass(state, datetime(2026, 1, 2))
        assert state.stability_days == INITIAL_INTERVAL_DAYS

    def test_apply_review_picks_the_right_direction(self):
        state = ReviewState("e")
        assert apply_review(state, True).last_result == "pass"
        assert apply_review(state, False).last_result == "fail"

    def test_a_state_is_only_scheduled_once_it_has_a_due_date(self):
        assert not ReviewState("e").is_scheduled
        assert ReviewState("e", due_at="2026-01-01 00:00:00").is_scheduled

    def test_accuracy_is_none_before_any_review(self):
        assert ReviewState("e").accuracy() is None

    def test_accuracy_counts_passes_over_attempts(self):
        state = ReviewState("e", reps=3, lapses=1)
        assert state.accuracy() == 0.75


class TestTimestamps:
    def test_a_valid_timestamp_parses(self):
        assert parse_ts("2026-01-02 03:04:05") == datetime(2026, 1, 2, 3, 4, 5)

    def test_a_missing_timestamp_is_none(self):
        assert parse_ts(None) is None
        assert parse_ts("") is None

    def test_a_malformed_timestamp_is_none(self):
        """One hand-edited row should cost that exercise its place in the
        queue, not take down the whole review page."""
        assert parse_ts("not a date") is None
        assert parse_ts("2026-13-45 99:99:99") is None


class TestQueue:
    def _states(self):
        return {
            "overdue": ReviewState("overdue", due_at="2026-01-01 00:00:00"),
            "due": ReviewState("due", due_at="2026-01-10 00:00:00"),
            "later": ReviewState("later", due_at="2026-02-01 00:00:00"),
        }

    def test_only_due_entries_are_queued(self):
        now = datetime(2026, 1, 10, 12, 0, 0)
        queue = build_queue(self._states(), now)
        assert {entry.exercise_id for entry in queue} == {"overdue", "due"}

    def test_the_most_overdue_comes_first(self):
        now = datetime(2026, 1, 10, 12, 0, 0)
        queue = build_queue(self._states(), now)
        assert queue[0].exercise_id == "overdue"

    def test_an_unscheduled_exercise_is_never_queued(self):
        assert build_queue({"e": ReviewState("e")}, datetime(2026, 1, 1)) == []


class TestConceptBreakdown:
    def _exercises(self):
        class Exercise:
            def __init__(self, id, tags):
                self.id = id
                self.tags = tags

        return {
            "a": Exercise("a", ["dockerfile"]),
            "b": Exercise("b", ["dockerfile"]),
            "c": Exercise("c", ["compose"]),
        }

    def test_every_tag_gets_a_bucket(self):
        rows = concept_breakdown(self._exercises(), set(), {}, datetime(2026, 1, 1))
        assert {row["tag"] for row in rows} == {"dockerfile", "compose"}

    def test_a_bucket_counts_its_exercises(self):
        rows = concept_breakdown(self._exercises(), set(), {}, datetime(2026, 1, 1))
        by_tag = {row["tag"]: row for row in rows}
        assert by_tag["dockerfile"]["total"] == 2
        assert by_tag["compose"]["total"] == 1

    def test_a_solved_exercise_is_counted(self):
        rows = concept_breakdown(self._exercises(), {"a"}, {}, datetime(2026, 1, 1))
        by_tag = {row["tag"]: row for row in rows}
        assert by_tag["dockerfile"]["solved"] == 1

    def test_an_untouched_concept_is_labelled(self):
        rows = concept_breakdown(self._exercises(), set(), {}, datetime(2026, 1, 1))
        assert all(row["state"] == "untouched" for row in rows)

    def test_actionable_work_comes_first(self):
        """Concepts are ranked by how much is due, not by a hidden score."""
        states = {"a": ReviewState("a", due_at="2026-01-01 00:00:00", reps=1)}
        rows = concept_breakdown(self._exercises(), {"a"}, states, datetime(2026, 1, 10))
        by_tag = {row["tag"]: row for row in rows}
        assert by_tag["dockerfile"]["due_now"] == 1
        assert rows[0]["tag"] == "dockerfile"


def test_utcnow_has_no_microseconds():
    """Timestamps are stored in a format SQLite can compare as strings."""
    assert utcnow().microsecond == 0


def test_a_due_date_in_the_future_is_not_due():
    state = ReviewState("e", due_at="2026-06-01 00:00:00")
    assert not state.is_due(datetime(2026, 1, 1))


def test_a_due_date_in_the_past_is_due():
    state = ReviewState("e", due_at="2025-06-01 00:00:00")
    assert state.is_due(datetime(2026, 1, 1))


def test_the_queue_tolerates_a_hand_edited_row():
    states = {"broken": ReviewState("broken", due_at="garbage")}
    assert build_queue(states, datetime(2026, 1, 1)) == []


def test_the_horizon_is_a_day():
    from app.srs import DUE_SOON_DAYS

    assert DUE_SOON_DAYS == 1.0
    assert timedelta(days=DUE_SOON_DAYS)
