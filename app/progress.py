"""Progress and attempt history.

Three states, not two, and the distinction is the point: a solution being
*revealed* is not the same as an exercise being *solved*. Collapsing them
would let a copied answer inflate a completion percentage, which is the
number a learner most trusts.

    unsolved  -- never completed, never revealed
    revealed  -- solution opened (explicitly not a solve)
    solved    -- passed, with no prior reveal
"""

from dataclasses import dataclass

from app.db import get_conn
from app.srs import ReviewState, apply_review


@dataclass
class ExerciseState:
    exercise_id: str
    status: str
    attempts: int
    solved_at: str | None
    revealed_at: str | None
    last_code: str | None

    def to_payload(self) -> dict:
        return {
            "exercise_id": self.exercise_id,
            "status": self.status,
            "attempts": self.attempts,
            "solved_at": self.solved_at,
            "revealed_at": self.revealed_at,
            "last_code": self.last_code,
        }


def record_attempt(
    exercise_id: str,
    code: str,
    passed: bool,
    *,
    checks_passed: int | None = None,
    checks_total: int | None = None,
    duration_ms: float | None = None,
    error: str | None = None,
) -> None:
    """Record a submission and update the exercise's state.

    A pass only marks the exercise solved if the solution was never revealed.
    Revealing then passing is recorded honestly as a pass, but the exercise
    stays in the `revealed` bucket.
    """
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO attempts "
            "(exercise_id, code, passed, checks_passed, checks_total, duration_ms, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                exercise_id,
                code,
                int(passed),
                checks_passed,
                checks_total,
                duration_ms,
                error,
            ),
        )
        conn.execute(
            """
            INSERT INTO exercise_progress (exercise_id, attempts_count, last_code)
            VALUES (?, 1, ?)
            ON CONFLICT(exercise_id) DO UPDATE SET
                attempts_count = attempts_count + 1,
                last_code = excluded.last_code
            """,
            (exercise_id, code),
        )
        if passed:
            conn.execute(
                """
                UPDATE exercise_progress
                   SET solved_at = COALESCE(solved_at, datetime('now'))
                 WHERE exercise_id = ?
                """,
                (exercise_id,),
            )


def record_reveal(exercise_id: str) -> None:
    """Note that the learner opened the solution.

    Idempotent, and it never clears an existing `solved_at`: revealing a
    solution after already solving the exercise should not rewrite history.

    It *does* drop the exercise from the review schedule, immediately. Once
    the answer has been seen, any later pass stops being evidence of
    retention, so leaving it queued would keep asking a question whose answer
    the learner now has.
    """
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO exercise_progress (exercise_id, revealed_at)
            VALUES (?, datetime('now'))
            ON CONFLICT(exercise_id) DO UPDATE SET
                revealed_at = COALESCE(exercise_progress.revealed_at, datetime('now'))
            """,
            (exercise_id,),
        )
        conn.execute("DELETE FROM review_state WHERE exercise_id = ?", (exercise_id,))


def _status(solved_at: str | None, revealed_at: str | None) -> str:
    # Reveal wins over solve: if the answer was ever opened, the exercise is
    # not counted as independently solved, whatever happened afterwards.
    if revealed_at:
        return "revealed"
    if solved_at:
        return "solved"
    return "unsolved"


def get_state(exercise_id: str) -> ExerciseState:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT solved_at, revealed_at, last_code, attempts_count "
            "FROM exercise_progress WHERE exercise_id = ?",
            (exercise_id,),
        ).fetchone()
        if row is None:
            return ExerciseState(exercise_id, "unsolved", 0, None, None, None)
        return ExerciseState(
            exercise_id=exercise_id,
            status=_status(row["solved_at"], row["revealed_at"]),
            attempts=row["attempts_count"],
            solved_at=row["solved_at"],
            revealed_at=row["revealed_at"],
            last_code=row["last_code"],
        )


def all_states() -> dict[str, ExerciseState]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT exercise_id, solved_at, revealed_at, last_code, attempts_count "
            "FROM exercise_progress"
        ).fetchall()
        return {
            row["exercise_id"]: ExerciseState(
                exercise_id=row["exercise_id"],
                status=_status(row["solved_at"], row["revealed_at"]),
                attempts=row["attempts_count"],
                solved_at=row["solved_at"],
                revealed_at=row["revealed_at"],
                last_code=row["last_code"],
            )
            for row in rows
        }


def summarise(states: dict[str, ExerciseState], all_exercise_ids) -> dict:
    """Aggregate progress for the dashboard.

    Solved and revealed are reported separately and never added together into
    a single headline figure -- a percentage that silently includes copied
    answers is worse than no percentage.
    """
    exercise_ids = list(all_exercise_ids)
    buckets = {"solved": 0, "revealed": 0, "unsolved": 0, "attempted": 0}

    for exercise_id in exercise_ids:
        state = states.get(exercise_id)
        if state is None:
            buckets["unsolved"] += 1
            continue
        buckets[state.status] += 1
        if state.attempts > 0:
            buckets["attempted"] += 1

    total = len(exercise_ids)
    return {
        "total": total,
        "solved": buckets["solved"],
        "revealed": buckets["revealed"],
        "attempted": buckets["attempted"],
        "unsolved": buckets["unsolved"],
        "solve_rate": round(buckets["solved"] / total, 3) if total else 0.0,
    }


def recent_attempts(limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT exercise_id, code, passed, checks_passed, checks_total, "
            "duration_ms, error, created_at "
            "FROM attempts ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def reset_progress() -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM attempts")
        conn.execute("DELETE FROM exercise_progress")
        conn.execute("DELETE FROM review_state")


# --------------------------------------------------------------------------
# Spaced repetition
# --------------------------------------------------------------------------


def get_review_state(exercise_id: str) -> ReviewState:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM review_state WHERE exercise_id = ?", (exercise_id,)
        ).fetchone()
    if row is None:
        return ReviewState(exercise_id=exercise_id)
    return ReviewState(
        exercise_id=row["exercise_id"],
        stability_days=row["stability_days"],
        reps=row["reps"],
        lapses=row["lapses"],
        last_result=row["last_result"],
        last_reviewed_at=row["last_reviewed_at"],
        due_at=row["due_at"],
    )


def all_review_states() -> dict[str, ReviewState]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM review_state").fetchall()
        return {
            row["exercise_id"]: ReviewState(
                exercise_id=row["exercise_id"],
                stability_days=row["stability_days"],
                reps=row["reps"],
                lapses=row["lapses"],
                last_result=row["last_result"],
                last_reviewed_at=row["last_reviewed_at"],
                due_at=row["due_at"],
            )
            for row in rows
        }


def save_review_state(state: ReviewState) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO review_state
                (exercise_id, stability_days, reps, lapses,
                 last_result, last_reviewed_at, due_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(exercise_id) DO UPDATE SET
                stability_days   = excluded.stability_days,
                reps             = excluded.reps,
                lapses           = excluded.lapses,
                last_result      = excluded.last_result,
                last_reviewed_at = excluded.last_reviewed_at,
                due_at           = excluded.due_at
            """,
            (
                state.exercise_id,
                state.stability_days,
                state.reps,
                state.lapses,
                state.last_result,
                state.last_reviewed_at,
                state.due_at,
            ),
        )


def schedule_review(exercise_id: str, passed: bool, now=None) -> ReviewState:
    """Fold a submission into the schedule and persist the new state."""
    state = apply_review(get_review_state(exercise_id), passed, now)
    save_review_state(state)
    return state


def clear_review_state(exercise_id: str) -> None:
    """Drop an exercise from the schedule (used when a solution is revealed)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM review_state WHERE exercise_id = ?", (exercise_id,))


def solved_exercise_ids() -> set[str]:
    """Exercises independently solved -- never revealed.

    Only these are worth scheduling: a revealed exercise cannot show that
    anything was retained, so scheduling it would inflate the schedule with
    items that prove nothing.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT exercise_id FROM exercise_progress "
            "WHERE solved_at IS NOT NULL AND revealed_at IS NULL"
        ).fetchall()
    return {row["exercise_id"] for row in rows}
