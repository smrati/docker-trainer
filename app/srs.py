"""Spaced repetition.

The scheduling model is deliberately small. Each solved exercise becomes a
card whose interval grows as you keep passing it:

    first pass          -> due in 1 day
    second pass         -> due in 2.2 days
    third               -> 4.8 days
    ...                 -> capped at MAX_INTERVAL_DAYS

A failure is not a longer interval, it is **due immediately**. That is the
whole design: "review" should mean "redo what you got wrong", which is the
same feedback loop the grader already gives. A failure therefore resets
stability to zero, so the next pass starts the ladder again at 1 day.

Two things are deliberately *not* scheduled:

* **Revealed exercises.** Opening the answer and then reproducing it proves
  you can copy, not that you retained anything. This follows the same
  reasoning as the solved/revealed split: a metric that cannot tell the
  difference is not worth having.
* **`run`, as opposed to `submit`.** Exploring an exercise is not recall.

Why exercises *and* concepts: the interval answers "what do I redo", but a
working developer thinks in topics ("I'm rusty on window functions").
:func:`concept_breakdown` aggregates per-concept strength from the same
per-exercise state, so the queue and the topic view never disagree.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# Interval assigned after the first (or a post-lapse) successful review.
INITIAL_INTERVAL_DAYS = 1.0

# Multiplier applied to the current interval on each subsequent success.
INTERVAL_GROWTH = 2.2

# Ceiling on any interval. Without it, a consistently-passed exercise would
# schedule itself centuries out and never be seen again. Six months is a
# sensible maintenance cadence for material you already know.
MAX_INTERVAL_DAYS = 180.0

# A concept with this much passing accuracy is treated as "needs work" below it.
WEAK_ACCURACY = 0.6
LEARNING_ACCURACY = 0.85

# Timestamps are stored in SQLite's format ("YYYY-MM-DD HH:MM:SS", UTC) so that
# string comparison and SQL date functions both behave.
TS_FORMAT = "%Y-%m-%d %H:%M:%S"

PASS = "pass"
FAIL = "fail"


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0, tzinfo=None)


def format_ts(moment: datetime) -> str:
    return moment.strftime(TS_FORMAT)


def parse_ts(value: str | None) -> datetime | None:
    """Parse a stored timestamp, returning None if it is absent or malformed.

    Tolerant on purpose. These strings come out of a local database file, and
    one hand-edited or truncated row should cost that exercise its place in the
    queue -- not take down the whole review page with a 500.
    """
    if not value:
        return None
    try:
        return datetime.strptime(value, TS_FORMAT)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class ReviewState:
    """Per-exercise scheduling state."""

    exercise_id: str
    stability_days: float = 0.0
    reps: int = 0
    lapses: int = 0
    last_result: str | None = None
    last_reviewed_at: str | None = None
    due_at: str | None = None

    @property
    def is_scheduled(self) -> bool:
        return self.due_at is not None

    def accuracy(self) -> float | None:
        """Share of reviews passed, or None if never attempted."""
        total = self.reps + self.lapses
        return self.reps / total if total else None

    def is_due(self, now: datetime) -> bool:
        due = parse_ts(self.due_at)
        return due is not None and due <= now


def schedule_pass(state: ReviewState, now: datetime | None = None) -> ReviewState:
    """Record a successful review and push the next one further out."""
    now = now or utcnow()
    if state.reps == 0 or state.stability_days <= 0:
        # First ever success, or the first after a lapse: restart the ladder.
        interval = INITIAL_INTERVAL_DAYS
    else:
        interval = state.stability_days * INTERVAL_GROWTH

    interval = min(interval, MAX_INTERVAL_DAYS)
    return ReviewState(
        exercise_id=state.exercise_id,
        stability_days=interval,
        reps=state.reps + 1,
        lapses=state.lapses,
        last_result=PASS,
        last_reviewed_at=format_ts(now),
        due_at=format_ts(now + timedelta(days=interval)),
    )


def schedule_fail(state: ReviewState, now: datetime | None = None) -> ReviewState:
    """Record a failed review: due immediately, ladder reset.

    The next pass therefore gets a full 1-day interval rather than the tail
    of whatever long interval a previous success had built up.
    """
    now = now or utcnow()
    return ReviewState(
        exercise_id=state.exercise_id,
        stability_days=0.0,
        reps=state.reps,
        lapses=state.lapses + 1,
        last_result=FAIL,
        last_reviewed_at=format_ts(now),
        due_at=format_ts(now),
    )


def apply_review(state: ReviewState, passed: bool, now: datetime | None = None) -> ReviewState:
    return schedule_pass(state, now) if passed else schedule_fail(state, now)


# --------------------------------------------------------------------------
# Queue and concept aggregation
# --------------------------------------------------------------------------

DUE_SOON_DAYS = 1.0


@dataclass(frozen=True)
class QueueEntry:
    exercise_id: str
    due_at: str
    days_overdue: float
    state: ReviewState

    def to_payload(self) -> dict:
        return {
            "exercise_id": self.exercise_id,
            "due_at": self.due_at,
            "days_overdue": round(self.days_overdue, 2),
            "reps": self.state.reps,
            "lapses": self.state.lapses,
            "last_result": self.state.last_result,
            "interval_days": self.state.stability_days,
        }


def build_queue(states: dict[str, ReviewState], now: datetime | None = None) -> list[QueueEntry]:
    """Everything due now, most overdue first."""
    now = now or utcnow()
    due: list[QueueEntry] = []

    for state in states.values():
        due_at = parse_ts(state.due_at)
        if due_at is None or due_at > now:
            continue
        overdue = (now - due_at).total_seconds() / 86400
        due.append(QueueEntry(state.exercise_id, state.due_at, overdue, state))

    due.sort(key=lambda entry: (-entry.days_overdue, entry.exercise_id))
    return due


def _concept_state(solved: int, due: int, accuracy: float | None, total: int) -> str:
    if due > 0:
        return "due"
    if accuracy is None and solved == 0:
        return "untouched"
    if accuracy is not None and accuracy < WEAK_ACCURACY:
        return "weak"
    if accuracy is not None and accuracy < LEARNING_ACCURACY:
        return "learning"
    return "strong"


def concept_breakdown(
    exercises: dict,
    solved_ids: set[str],
    review_states: dict[str, ReviewState],
    now: datetime | None = None,
) -> list[dict]:
    """Aggregate per-concept strength from per-exercise state.

    Ranking is by what is *actionable* -- how many exercises are due -- then by
    historical accuracy. No weighted composite score: a hidden formula is
    impossible to explain to a learner, and impossible to tune sensibly without
    real usage data.
    """
    now = now or utcnow()
    soon_cutoff = now + timedelta(days=DUE_SOON_DAYS)

    buckets: dict[str, dict] = {}
    for exercise in exercises.values():
        for tag in exercise.tags:
            bucket = buckets.setdefault(
                tag,
                {
                    "tag": tag,
                    "total": 0,
                    "solved": 0,
                    "due_now": 0,
                    "due_soon": 0,
                    "scheduled": 0,
                    "reps": 0,
                    "lapses": 0,
                    "exercise_ids": [],
                },
            )
            bucket["total"] += 1
            bucket["exercise_ids"].append(exercise.id)
            if exercise.id in solved_ids:
                bucket["solved"] += 1

            state = review_states.get(exercise.id)
            if state is None or not state.is_scheduled:
                continue
            bucket["scheduled"] += 1
            bucket["reps"] += state.reps
            bucket["lapses"] += state.lapses

            due_at = parse_ts(state.due_at)
            if due_at is not None and due_at <= now:
                bucket["due_now"] += 1
            elif due_at is not None and due_at <= soon_cutoff:
                bucket["due_soon"] += 1

    rows = []
    for bucket in buckets.values():
        reviews = bucket["reps"] + bucket["lapses"]
        accuracy = bucket["reps"] / reviews if reviews else None
        rows.append(
            {
                **bucket,
                "accuracy": round(accuracy, 3) if accuracy is not None else None,
                "reviews": reviews,
                "mastered": bucket["scheduled"] - bucket["due_now"] - bucket["due_soon"],
                "state": _concept_state(
                    bucket["solved"], bucket["due_now"], accuracy, bucket["total"]
                ),
            }
        )

    # Actionable work first (what is due), then history (pass rate). An
    # unattempted concept has no accuracy and sorts after anything measured.
    rows.sort(
        key=lambda r: (
            -r["due_now"],
            r["accuracy"] is None,
            r["accuracy"] if r["accuracy"] is not None else 0,
            r["tag"],
        )
    )
    return rows