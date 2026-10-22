"""Grading a submission end to end.

Three steps, in this order:

1. **Check the source-level rules.** Some exercises are about a construct
   rather than a result -- "do not use ``latest``", "declare a ``USER``" --
   and a result-based grader cannot see those. They are opt-in per exercise.
2. **Run the learner's artefact and the reference artefact** over the same
   checks. The reference's behaviour **is** the expected answer. This is what
   makes grading semantic rather than textual: there is no hand-written
   expected output anywhere in this app to drift out of date, and any correct
   implementation passes however it was written.
3. **Compare the two** and record the attempt.

The reference is built once per exercise and cached. It is trusted content,
but it still goes through the daemon like everything else, because a
reference that hangs must not hang the server.
"""

from __future__ import annotations

import hashlib
import json
import threading

from app.engine.compare import ComparisonResult
from app.engine.runner import MODE_RUN, MODE_SUBMIT, evaluate
from app.progress import (
    clear_review_state,
    get_review_state,
    get_state,
    record_attempt,
    save_review_state,
)
from app.srs import apply_review

# Computing a reference's expectations takes an image build, so it is done
# once per exercise and cached on the curriculum. The lock keeps two
# concurrent submissions from computing the same thing twice.
_EXPECTATION_LOCK = threading.Lock()


def _source_problem(files: dict[str, str], exercise) -> str | None:
    """Check the submitted files against the exercise's source-level rules.

    Two kinds of rule, both opt-in per exercise:

    * ``forbid`` -- a substring the submission may not contain. Checked
      against the *text*, case-insensitively, and it fires before anything
      is built so the learner is told why rather than being shown a
      wrong answer.
    * ``require`` -- a substring the submission must contain, for exercises
      where the construct is the point.
    """
    settings = exercise.settings
    haystack = "\n".join(files.values()).lower()

    for needle in settings.forbid:
        if needle.lower() in haystack:
            return (
                f"This exercise asks you to solve it without `{needle}`. "
                "Remove it and try again."
            )

    for needle in settings.require:
        if needle.lower() not in haystack:
            return f"This exercise needs your files to include `{needle}`."

    return None


def submission_text(files: dict[str, str]) -> str:
    """How a submission is stored in the attempt history.

    JSON, so the history keeps every file the learner submitted rather
    than only the one that mattered.
    """
    return json.dumps(files, indent=2, sort_keys=True, ensure_ascii=False)


def execute_only(curriculum, exercise, files: dict[str, str]) -> dict:
    """Build and run a submission without grading it.

    This is the `run` endpoint -- exploring must never mark anything as
    complete, or a learner could stumble onto the right answer and have it
    silently counted as a solve.
    """
    outcome = evaluate(curriculum, exercise, files, mode=MODE_RUN)
    payload = outcome.to_payload()
    payload["checks"] = [
        {
            "name": check.name,
            "kind": check.kind,
            "stdout": check.result.get("stdout", ""),
            "stderr": check.result.get("stderr", ""),
            "exit_code": check.result.get("exit_code"),
            "value": check.result.get("value"),
            "error": check.result.get("error"),
        }
        for check in outcome.checks
    ]
    return payload


def _update_schedule(exercise_id: str, passed: bool) -> dict:
    """Fold a graded submission into the spaced-repetition schedule.

    Three rules, each one there to avoid putting a lie in the review queue:

    * **A pass schedules** the exercise for a future review.
    * **A failure only reschedules something already scheduled.** Failing an
      exercise you are currently learning is not forgetting, it is learning.
      Scheduling it would fill the queue with material you have never held,
      which is where spaced repetition is least useful.
    * **A revealed exercise is unscheduled.** Once you have seen the answer,
      any later pass stops being evidence of retention.
    """
    progress = get_state(exercise_id)
    existing = get_review_state(exercise_id)

    if progress.revealed_at:
        clear_review_state(exercise_id)
        return {"scheduled": False, "reason": "revealed"}

    if passed:
        state = apply_review(existing, True)
        save_review_state(state)
        return {
            "scheduled": True,
            "due_at": state.due_at,
            "interval_days": round(state.stability_days, 2),
            "reps": state.reps,
            "lapses": state.lapses,
        }

    if existing.is_scheduled:
        # You used to have this one and have just lost it: due right now.
        state = apply_review(existing, False)
        save_review_state(state)
        return {
            "scheduled": True,
            "due_at": state.due_at,
            "due_now": True,
            "reps": state.reps,
            "lapses": state.lapses,
        }

    return {"scheduled": False, "reason": "not_yet_scheduled"}


def grade(curriculum, exercise, files: dict[str, str]) -> dict:
    """Run, compare against the reference, and record the attempt."""
    # --- source-level rules ----------------------------------------------
    # Checked before anything is built: an exercise that says "without using
    # latest" should say so, rather than quietly failing a correct-looking
    # answer.
    problem = _source_problem(files, exercise)
    if problem:
        record_attempt(exercise.id, submission_text(files), False, error=problem)
        comparison = ComparisonResult(
            passed=False,
            reason="source",
            detail=problem,
            checks=[],
        )
        review = _update_schedule(exercise.id, False)
        return {
            "ok": True,
            "error": None,
            "result": None,
            "grading": comparison.to_payload(),
            "review": review,
        }

    # --- build, run, compare ----------------------------------------------
    outcome = evaluate(curriculum, exercise, files, mode=MODE_SUBMIT)

    if not outcome.ok:
        record_attempt(
            exercise.id,
            submission_text(files),
            False,
            error=outcome.error,
            duration_ms=outcome.duration_ms,
        )
        return {
            "ok": False,
            "error": {"kind": outcome.error_kind, "message": outcome.error},
            "result": {
                "build_log": outcome.build_log,
                "checks": [],
                "image": outcome.image,
            },
            "grading": None,
            "review": None,
        }

    comparison = outcome.comparison
    record_attempt(
        exercise.id,
        submission_text(files),
        comparison.passed if comparison else False,
        checks_passed=comparison.passed_count if comparison else 0,
        checks_total=comparison.total_count if comparison else 0,
        duration_ms=outcome.duration_ms,
    )

    review = _update_schedule(exercise.id, bool(comparison and comparison.passed))

    return {
        "ok": True,
        "error": None,
        "result": {
            "build_log": outcome.build_log,
            "checks": [
                {
                    "name": check.name,
                    "kind": check.kind,
                    "stdout": check.result.get("stdout", ""),
                    "stderr": check.result.get("stderr", ""),
                    "exit_code": check.result.get("exit_code"),
                    "value": check.result.get("value"),
                    "error": check.result.get("error"),
                }
                for check in outcome.checks
            ],
            "image": outcome.image,
        },
        "grading": comparison.to_payload() if comparison else None,
        "review": review,
    }


def reference_digest(exercise) -> str:
    """A short fingerprint of an exercise's reference artefact.

    Used by the tests to notice when a reference changes under a cached
    expectation.
    """
    payload = json.dumps(exercise.solution, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]
