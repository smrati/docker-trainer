"""Read endpoints: curriculum browsing, exercises, progress."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException

from app.curriculum.loader import Curriculum
from app.deps import get_curriculum
from app.progress import all_states, get_state, recent_attempts, summarise

router = APIRouter(prefix="/api", tags=["content"])


def _exercise_summary(exercise, state) -> dict:
    return {
        "id": exercise.id,
        "title": exercise.title,
        "difficulty": exercise.difficulty,
        "kind": exercise.kind,
        "status": state.status if state else "unsolved",
        "attempts": state.attempts if state else 0,
    }


def _get_exercise(exercise_id: str, curriculum: Curriculum):
    try:
        return curriculum.get_exercise(exercise_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No such exercise: {exercise_id}") from None


def parse_submission(text: str | None) -> dict[str, str]:
    """Read a stored submission back into its files.

    The attempt history stores a submission as JSON. A row that does not
    parse is dropped rather than raising: a hand-edited database should
    cost one exercise its last answer, not break the exercise page.
    """
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): str(value) for key, value in payload.items()}


@router.get("/curriculum")
def get_curriculum_tree(curriculum: Curriculum = Depends(get_curriculum)):
    """Tracks -> lessons -> exercises, with each exercise's status attached.

    The whole tree comes back in one request: the app is small and offline,
    and a lesson list that has to be stitched together from a dozen calls
    makes the first paint feel slow for no benefit.
    """
    states = all_states()

    tracks = []
    for track in curriculum.tracks:
        lessons = []
        for lesson in track.lessons:
            lessons.append(
                {
                    "id": lesson.id,
                    "title": lesson.title,
                    "summary": lesson.summary,
                    "exercise_count": lesson.exercise_count,
                    "exercises": [
                        _exercise_summary(ex, states.get(ex.id)) for ex in lesson.exercises
                    ],
                }
            )
        tracks.append(
            {
                "id": track.id,
                "title": track.title,
                "description": track.description,
                "difficulty": track.difficulty,
                "exercise_count": track.exercise_count,
                "lessons": lessons,
            }
        )

    return {
        "tracks": tracks,
        "summary": summarise(states, curriculum.exercises.keys()),
    }


@router.get("/lessons/{lesson_id}")
def get_lesson(lesson_id: str, curriculum: Curriculum = Depends(get_curriculum)):
    """Full lesson text plus its exercises.

    Exercise solutions are excluded here -- they arrive only from the explicit
    reveal endpoint, which is what lets the reveal be recorded.
    """
    try:
        lesson = curriculum.get_lesson(lesson_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No such lesson: {lesson_id}") from None

    states = all_states()
    return {
        "id": lesson.id,
        "title": lesson.title,
        "summary": lesson.summary,
        "body": lesson.body,
        "exercises": [
            {**_exercise_summary(ex, states.get(ex.id)), "prompt": ex.prompt}
            for ex in lesson.exercises
        ],
    }


@router.get("/exercises/{exercise_id}")
def get_exercise(exercise_id: str, curriculum: Curriculum = Depends(get_curriculum)):
    """An exercise's prompt, starter files and checks. Never the solution.

    The starter files are included so the editor can open them, and the
    checks are included so a learner can see *what* is being checked -- the
    names of the checks, never their expected values. Knowing that the
    exercise checks "the image declares port 8080" is not the answer; it is
    the specification.
    """
    exercise = _get_exercise(exercise_id, curriculum)
    state = get_state(exercise_id)

    last_files = parse_submission(state.last_code)

    return {
        "id": exercise.id,
        "title": exercise.title,
        "prompt": exercise.prompt,
        "kind": exercise.kind,
        "entry_file": exercise.entry_file,
        "files": exercise.files,
        "file_names": exercise.file_names,
        "difficulty": exercise.difficulty,
        "teach": exercise.teach,
        "hint_count": len(exercise.hints),
        "checks": [
            {"name": check.name, "kind": check.kind} for check in exercise.checks
        ],
        "check_count": len(exercise.checks),
        "cases": [
            {"index": index, "name": case.name, "label": case.label}
            for index, case in enumerate(exercise.cases)
        ],
        "case_count": len(exercise.cases),
        "status": state.status,
        "attempts": state.attempts,
        "last_files": last_files,
        "solved_at": state.solved_at,
        "revealed_at": state.revealed_at,
    }


@router.get("/exercises/{exercise_id}/hints/{index}")
def get_hint(exercise_id: str, index: int, curriculum: Curriculum = Depends(get_curriculum)):
    """One hint by 0-based index."""
    exercise = _get_exercise(exercise_id, curriculum)

    if not 0 <= index < len(exercise.hints):
        raise HTTPException(status_code=404, detail="No such hint.")
    return {"index": index, "text": exercise.hints[index], "total": len(exercise.hints)}


@router.get("/progress")
def get_progress(curriculum: Curriculum = Depends(get_curriculum)):
    """Dashboard data.

    Solved and revealed are reported side by side and never combined into a
    single completion figure.
    """
    states = all_states()
    return {
        "summary": summarise(states, curriculum.exercises.keys()),
        "exercises": [
            {
                **states[eid].to_payload(),
                "title": curriculum.exercises[eid].title,
                "difficulty": curriculum.exercises[eid].difficulty,
            }
            for eid in curriculum.exercises
            if eid in states
        ],
        "recent_attempts": recent_attempts(),
    }


@router.delete("/progress")
def reset_progress():
    from app.progress import reset_progress as _reset

    _reset()
    return {"reset": True}
