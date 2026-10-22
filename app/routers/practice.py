"""Write endpoints: building, running, submitting and revealing."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.curriculum.loader import Curriculum
from app.deps import get_curriculum
from app.grading import execute_only, grade
from app.progress import get_state, record_reveal

router = APIRouter(prefix="/api", tags=["practice"])

# A submission is a set of files. The cap is generous enough for a project
# with a compose file, a Dockerfile and a couple of sources, and tight enough
# that one request cannot carry a megabyte of text.
MAX_FILES = 40
MAX_FILE_CHARS = 200_000
MAX_TOTAL_CHARS = 500_000


class SubmissionRequest(BaseModel):
    files: dict[str, str] = Field(default_factory=dict)


class PlaygroundRequest(BaseModel):
    dockerfile: str = Field(default="", max_length=MAX_FILE_CHARS)
    files: dict[str, str] = Field(default_factory=dict)


def _validate(files: dict[str, str]) -> dict[str, str]:
    """Check a submission's shape before anything is built.

    Rejecting a bad submission here is much kinder than rejecting it half
    way through a build: the learner gets an immediate, specific message
    instead of a daemon error.
    """
    if len(files) > MAX_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"A submission may contain at most {MAX_FILES} files.",
        )
    total = 0
    for name, content in files.items():
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(status_code=400, detail="A file name may not be empty.")
        if len(content) > MAX_FILE_CHARS:
            raise HTTPException(
                status_code=400, detail=f"“{name}” is too large."
            )
        total += len(content)
    if total > MAX_TOTAL_CHARS:
        raise HTTPException(
            status_code=400, detail="This submission is too large to build."
        )
    return files


def _get(exercise_id: str, curriculum: Curriculum):
    try:
        return curriculum.get_exercise(exercise_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No such exercise: {exercise_id}") from None


@router.post("/exercises/{exercise_id}/run")
def run(
    exercise_id: str,
    body: SubmissionRequest,
    curriculum: Curriculum = Depends(get_curriculum),
):
    """Build and run a submission and return what it did. Never grades.

    Kept separate from `/submit` on purpose: exploring an exercise must not
    be able to mark it complete.
    """
    exercise = _get(exercise_id, curriculum)
    files = _validate(body.files)
    try:
        return execute_only(curriculum, exercise, files)
    except Exception as exc:  # EngineError carries .kind/.message
        return _error_payload(exc)


@router.post("/exercises/{exercise_id}/submit")
def submit(
    exercise_id: str,
    body: SubmissionRequest,
    curriculum: Curriculum = Depends(get_curriculum),
):
    """Grade a submission against the reference answer and record the attempt."""
    exercise = _get(exercise_id, curriculum)
    files = _validate(body.files)
    outcome = grade(curriculum, exercise, files)
    if not outcome["ok"]:
        # A failed build is a normal outcome, not a 500: the message is feedback.
        return outcome
    return {**outcome, "state": get_state(exercise_id).to_payload()}


@router.post("/exercises/{exercise_id}/solution")
def reveal_solution(exercise_id: str, curriculum: Curriculum = Depends(get_curriculum)):
    """Return the reference solution and record that it was opened.

    Free to call: the point is to record the reveal honestly rather than to
    gate it. The exercise is then reported as `revealed`, never as `solved`.
    """
    exercise = _get(exercise_id, curriculum)
    record_reveal(exercise_id)
    return {
        "solution": exercise.solution,
        "teach": exercise.teach,
        "state": get_state(exercise_id).to_payload(),
    }


@router.post("/playground/run")
def playground(body: PlaygroundRequest, curriculum: Curriculum = Depends(get_curriculum)):
    """Build a Dockerfile of your own and run a command in it.

    Ungraded and unattributed: it is a workbench, not an exercise.
    """
    from app.engine.runner import MODE_RUN, evaluate

    files = dict(body.files or {})
    files["Dockerfile"] = body.dockerfile or ""
    files = _validate(files)

    # The playground borrows the first image exercise's shape so that the
    # engine has something to build and run.
    from app.curriculum.loader import Case, Check, Exercise

    exercise = Exercise(
        id="playground",
        title="Playground",
        prompt="",
        kind="image",
        entry_file="Dockerfile",
        files={"Dockerfile": ""},
        solution={"Dockerfile": ""},
        checks=[
            Check(
                name="what the image does when it runs",
                kind="default",
                command="",
            )
        ],
        cases=[Case()],
    )

    try:
        outcome = evaluate(curriculum, exercise, files, mode=MODE_RUN)
    except Exception as exc:
        return _error_payload(exc)

    payload = outcome.to_payload()
    payload["checks"] = [
        {
            "name": check.name,
            "kind": check.kind,
            "stdout": check.result.get("stdout", ""),
            "stderr": check.result.get("stderr", ""),
            "exit_code": check.result.get("exit_code"),
            "error": check.result.get("error"),
        }
        for check in outcome.checks
    ]
    return payload


def _error_payload(exc: Exception) -> dict:
    """Shape an EngineError into the same envelope as a successful run."""
    kind = getattr(exc, "kind", "error")
    message = getattr(exc, "message", None) or str(exc)
    return {
        "ok": False,
        "error": {"kind": kind, "message": message},
        "build_log": "",
        "checks": [],
        "grading": None,
        "duration_ms": 0.0,
    }
