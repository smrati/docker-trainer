"""Spaced repetition endpoints: the due queue and per-concept strength."""

from collections import OrderedDict
from datetime import timedelta

from fastapi import APIRouter, Depends

from app.curriculum.loader import Curriculum
from app.deps import get_curriculum
from app.progress import all_review_states, solved_exercise_ids
from app.srs import (
    DUE_SOON_DAYS,
    QueueEntry,
    build_queue,
    concept_breakdown,
    parse_ts,
    utcnow,
)

router = APIRouter(prefix="/api", tags=["review"])

# Human labels for the controlled tag vocabulary. Kept here rather than in
# the curriculum so that authoring content never has to carry display text,
# and a typo in a tag cannot produce a blank heading.
CONCEPT_LABELS = {
    "images": ("Images and containers", "Images, tags, layers, and the container lifecycle"),
    "dockerfile": ("Dockerfile basics", "FROM, RUN, COPY, WORKDIR and how instructions read"),
    "commands": ("CMD and ENTRYPOINT", "What runs, in what form, and with which arguments"),
    "layers": ("Layers and caching", "The layer model, build cache, and what invalidates it"),
    "config": ("Configuration", "ENV, ARG, build arguments, and runtime overrides"),
    "files": ("Files and the build context", "COPY, ADD, .dockerignore, and WORKDIR"),
    "multi-stage": ("Multi-stage builds", "Building once, shipping only what is needed"),
    "optimisation": ("Image size and speed", "Base images, layer counts, and build performance"),
    "compose": ("Compose files", "Services, the compose file, and project layout"),
    "networks": ("Networking", "Container networks, published ports, and DNS"),
    "volumes": ("Volumes and mounts", "Named volumes, bind mounts, and persistent data"),
    "cli": ("The docker command line", "ps, logs, exec, inspect, and what they tell you"),
    "security": ("Security", "Non-root users, secrets, and least privilege"),
    "healthchecks": ("Healthchecks", "HEALTHCHECK, health states, and waiting for readiness"),
    "buildkit": ("BuildKit", "Cache mounts, build secrets, and parallel stages"),
    "reproducibility": ("Reproducibility", "Pinning digests, and builds that can be repeated"),
    "limits": ("Resource limits", "Memory, CPU, and stopping a runaway container"),
    "operations": ("Running containers", "Starting, stopping, inspecting and cleaning up"),
}


def _describe(exercise, entry: QueueEntry) -> dict:
    """One queue row: what to review, and how overdue it is."""
    return {
        **entry.to_payload(),
        "title": exercise.title,
        "difficulty": exercise.difficulty,
        "tags": exercise.tags,
    }


@router.get("/review")
def get_review(curriculum: Curriculum = Depends(get_curriculum)):
    """Everything due now, what is coming up, and per-concept strength.

    Concepts are ranked by actionable work first (how many exercises are due),
    then by historical accuracy. Deliberately not a weighted composite score:
    a hidden formula cannot be explained to a learner.
    """
    now = utcnow()
    states = all_review_states()
    queue = build_queue(states, now)

    due_ids = [entry.exercise_id for entry in queue]
    # An exercise that has been renamed or removed since it was scheduled
    # leaves a row behind. Dropping it is kinder than failing the whole
    # page: the curriculum moves, and a review queue that 409s because of
    # an old row is a queue nobody can use. The count is reported, so the
    # drop is visible rather than silent.
    missing = [eid for eid in due_ids if eid not in curriculum.exercises]
    due_ids = [eid for eid in due_ids if eid in curriculum.exercises]
    queue = [entry for entry in queue if entry.exercise_id in curriculum.exercises]

    exercises = OrderedDict((eid, curriculum.exercises[eid]) for eid in due_ids)
    concepts = concept_breakdown(curriculum.exercises, solved_exercise_ids(), states, now)

    due_soon = [
        entry.to_payload() | {"title": curriculum.exercises[entry.exercise_id].title}
        for entry in _due_soon(curriculum, states, now)
    ]

    scheduled = [s for s in states.values() if s.is_scheduled]
    reviews = sum(s.reps + s.lapses for s in states.values())
    next_due = _next_due(states, now)

    return {
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
        "counts": {
            "due_now": len(queue),
            "due_soon": len(due_soon),
            "scheduled": len(scheduled),
            "reviews_logged": reviews,
            "solved": len(solved_exercise_ids()),
            "next_due_at": next_due[0],
            "next_due_in_days": next_due[1],
            # Rows left behind by an exercise that no longer exists.
            "stale_entries": len(missing),
        },
        "due": [
            _describe(exercises[eid], entry)
            for eid, entry in zip(due_ids, queue, strict=True)
        ],
        "due_soon": due_soon,
        "concepts": [
            {
                **{k: v for k, v in concept.items() if k != "exercise_ids"},
                "label": CONCEPT_LABELS.get(concept["tag"], (concept["tag"], ""))[0],
                "description": CONCEPT_LABELS.get(concept["tag"], ("", ""))[1],
            }
            for concept in concepts
        ],
    }


def _next_due(states, now) -> tuple[str | None, float | None]:
    """The earliest future due date, so an empty queue can still say something.

    Without this the review page says "nothing due" and stops, which reads as
    a dead end rather than as "come back Thursday".
    """
    upcoming = [
        due
        for state in states.values()
        if (due := parse_ts(state.due_at)) is not None and due > now
    ]
    if not upcoming:
        return None, None
    soonest = min(upcoming)
    return soonest.strftime("%Y-%m-%d %H:%M:%S"), round((soonest - now).total_seconds() / 86400, 2)


def _due_soon(curriculum, states, now) -> list[QueueEntry]:
    """Scheduled exercises coming due within the next day.

    ``days_overdue`` is negative for these, which reads correctly in the UI as
    "due in N days" without needing a second code path.
    """
    horizon = now + timedelta(days=DUE_SOON_DAYS)
    entries: list[QueueEntry] = []

    for state in states.values():
        if not state.is_scheduled or state.exercise_id not in curriculum.exercises:
            continue
        due_at = parse_ts(state.due_at)
        if due_at is None or not (now < due_at <= horizon):
            continue
        lead = -(due_at - now).total_seconds() / 86400
        entries.append(QueueEntry(state.exercise_id, state.due_at, lead, state))

    entries.sort(key=lambda entry: entry.due_at)
    return entries
