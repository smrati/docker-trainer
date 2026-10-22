"""Loading and validating curriculum content.

Curriculum lives as YAML files under ``content/`` rather than in the app
database, so it is reviewed and diffed like code, and a typo in an exercise
is a test failure rather than a bug a learner discovers.

Directory layout::

    content/
      foundations/
        track.yaml
      intermediate/
        track.yaml
      advanced/
        track.yaml

Validation is strict and happens at startup: an exercise with no checks, a
duplicate id, or a reference artefact missing the file it needs is a broken
app, not something to discover on click.

Two things are checked here and two things are checked in the test suite,
and the split is deliberate:

* **At startup** -- everything cheap and structural: duplicate ids, missing
  fields, a check with no command, a reference artefact with no Dockerfile.
* **In the tests** -- everything that needs a daemon: that every reference
  artefact actually builds, that every reference check actually runs, and
  that the starter does not contain the answer. Building eighty images at
  startup would add minutes to every page load.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.config import CONTENT_DIR
from app.engine.compare import CHECK_KINDS, RUN_CHECKS, ComparisonSettings

# The kinds of exercise there are.
#
# * ``image``   -- the learner writes a Dockerfile; the grader builds it and
#                  runs checks inside the container.
# * ``compose`` -- the learner writes a compose project; the grader reads its
#                  canonical configuration and runs checks in its services.
# * ``cli``     -- the learner writes shell commands; the grader runs them
#                  against a prepared Docker environment.
EXERCISE_KINDS = ("image", "compose", "cli")

# The file a submission must contain, per kind, unless the exercise names
# its own entry file.
DEFAULT_ENTRY_FILES = {
    "image": "Dockerfile",
    "compose": "compose.yaml",
    "cli": "solve.sh",
}


class ContentError(Exception):
    """A curriculum authoring mistake. Always names the offending file."""


@dataclass(frozen=True)
class Hint:
    text: str


@dataclass
class Case:
    """One variant of the build or the run.

    A case is the Docker analogue of a Python trainer's test case: the same
    artefact, exercised under different conditions. A build argument changes
    what gets baked into the image; an environment variable changes what the
    container sees at runtime.
    """

    name: str = "default"
    build_args: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    network: str | None = None

    @property
    def label(self) -> str:
        parts = []
        if self.build_args:
            parts.append(
                "build args: "
                + ", ".join(f"{k}={v}" for k, v in sorted(self.build_args.items()))
            )
        if self.env:
            parts.append(
                "env: " + ", ".join(f"{k}={v}" for k, v in sorted(self.env.items()))
            )
        if self.network:
            parts.append(f"network: {self.network}")
        return ", ".join(parts) if parts else "no arguments"

    @property
    def build_key(self) -> str:
        """A key that changes only when the *build* changes.

        Two cases with the same build key produce the same image, so the
        image is built once and used twice. The key is a short hash, because
        it ends up in an image tag and a Docker tag may only contain
        letters, digits, dots, dashes and underscores.
        """
        if not self.build_args:
            return "default"
        payload = ",".join(f"{k}={v}" for k, v in sorted(self.build_args.items()))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:10]

    @property
    def cache_key(self) -> str:
        """A key that identifies the case's *result*.

        Unlike :attr:`build_key`, this includes the environment and the
        network, because two cases that build the same image but run it with
        different environments produce different answers. Keying the
        reference's expectations by the build alone would compare the second
        case against the first one's output.
        """
        payload = "|".join(
            [
                self.build_key,
                ",".join(f"{k}={v}" for k, v in sorted(self.env.items())),
                self.network or "",
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


@dataclass
class Check:
    """One thing the grader does to an artefact and compares.

    Every check produces a single comparable value, and that value is
    produced by running the check against the *reference* artefact and
    against the *learner's* artefact. The reference's behaviour is the
    expected answer.
    """

    name: str
    kind: str
    command: str = ""
    template: str = ""
    service: str = ""
    settings: ComparisonSettings = field(default_factory=ComparisonSettings)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "command": self.command,
            "template": self.template,
            "service": self.service,
            "settings": self.settings,
        }


@dataclass
class Exercise:
    id: str
    title: str
    prompt: str
    kind: str = "image"
    files: dict[str, str] = field(default_factory=dict)
    solution: dict[str, str] = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)
    cases: list[Case] = field(default_factory=list)
    setup: list[str] = field(default_factory=list)
    teardown: list[str] = field(default_factory=list)
    entry_file: str = ""
    starter_files: dict[str, str] = field(default_factory=dict)
    hints: list[str] = field(default_factory=list)
    teach: str = ""
    difficulty: str = "beginner"
    settings: ComparisonSettings = field(default_factory=ComparisonSettings)
    tags: list[str] = field(default_factory=list)

    @property
    def is_function(self) -> bool:
        return self.kind == "image"

    @property
    def check_count(self) -> int:
        return len(self.checks)

    @property
    def file_names(self) -> list[str]:
        return sorted(self.files)


@dataclass
class Lesson:
    id: str
    title: str
    summary: str
    body: str
    exercises: list[Exercise] = field(default_factory=list)

    @property
    def exercise_count(self) -> int:
        return len(self.exercises)


@dataclass
class Track:
    id: str
    title: str
    description: str
    difficulty: str
    lessons: list[Lesson] = field(default_factory=list)
    order: int = 0

    @property
    def exercise_count(self) -> int:
        return sum(lesson.exercise_count for lesson in self.lessons)


@dataclass
class Curriculum:
    version: int
    tracks: list[Track]
    exercises: dict[str, Exercise]
    lessons: dict[str, Lesson]
    # Reference expectations, computed lazily and cached here. Keyed by
    # exercise id.
    expectations: dict[str, list[dict]] = field(default_factory=dict)

    def get_exercise(self, exercise_id: str) -> Exercise:
        try:
            return self.exercises[exercise_id]
        except KeyError:
            raise KeyError(f"Unknown exercise: {exercise_id}") from None

    def get_lesson(self, lesson_id: str) -> Lesson:
        try:
            return self.lessons[lesson_id]
        except KeyError:
            raise KeyError(f"Unknown lesson: {lesson_id}") from None


CURRICULUM_VERSION = 1


# --------------------------------------------------------------------------
# Loading helpers
# --------------------------------------------------------------------------


def _require(data: dict, key: str, path: Path, kind: type | tuple = str):
    if key not in data or data[key] in (None, ""):
        raise ContentError(f"{path}: missing required field '{key}'")
    value = data[key]
    if not isinstance(value, kind):
        expected = (
            kind.__name__
            if isinstance(kind, type)
            else " or ".join(k.__name__ for k in kind)
        )
        raise ContentError(f"{path}: field '{key}' must be {expected}")
    return value


def _parse_files(raw, path: Path, field: str) -> dict[str, str]:
    """Parse a map of path -> text.

    A bare list of paths is also accepted, for an exercise whose file is
    empty. Writing ``Dockerfile: ""`` in YAML is easy to get wrong -- the
    empty value has to be quoted -- so the list form saves the author a
    footgun.
    """
    if raw is None:
        return {}
    if isinstance(raw, list):
        return {str(name): "" for name in raw}
    if not isinstance(raw, dict):
        raise ContentError(f"{path}: '{field}' must be a mapping of path to content")

    files: dict[str, str] = {}
    for name, content in raw.items():
        if not isinstance(name, str):
            raise ContentError(f"{path}: '{field}' keys must be strings")
        if content is None:
            files[name] = ""
            continue
        if not isinstance(content, str):
            raise ContentError(f"{path}: '{field}[{name}]' must be a string")
        files[name] = content
    return files


def _parse_check(raw: dict, path: Path, base: ComparisonSettings) -> Check:
    if not isinstance(raw, dict):
        raise ContentError(f"{path}: each check must be a mapping")

    kind = raw.get("kind")
    if kind not in CHECK_KINDS:
        raise ContentError(
            f"{path}: 'kind' must be one of {', '.join(CHECK_KINDS)}"
        )

    name = raw.get("name") or ""
    if not isinstance(name, str):
        raise ContentError(f"{path}: 'name' must be a string")

    command = raw.get("command") or ""
    template = raw.get("template") or ""
    service = raw.get("service") or ""

    if kind in RUN_CHECKS and not isinstance(command, str):
        raise ContentError(f"{path}: 'command' must be a string")
    if kind == "inspect" and not isinstance(template, str):
        raise ContentError(f"{path}: 'template' must be a string")
    if kind == "service" and not isinstance(service, str):
        raise ContentError(f"{path}: 'service' must be a string")

    if kind == "inspect" and not template.strip():
        raise ContentError(f"{path}: an inspect check needs a 'template'")
    if kind == "service" and not service.strip():
        raise ContentError(f"{path}: a service check needs a 'service'")
    if kind in ("run", "service", "script") and not command.strip():
        raise ContentError(f"{path}: a {kind} check needs a 'command'")

    try:
        settings = ComparisonSettings.from_dict(raw.get("settings"), base)
    except ValueError as exc:
        raise ContentError(f"{path}: {exc}") from exc

    return Check(
        name=name or f"{kind} check",
        kind=kind,
        command=command,
        template=template,
        service=service,
        settings=settings,
    )


def _parse_case(raw, path: Path, index: int) -> Case:
    if not isinstance(raw, dict):
        raise ContentError(f"{path}: each case must be a mapping")

    build_args = raw.get("build_args") or {}
    env = raw.get("env") or {}
    if not isinstance(build_args, dict):
        raise ContentError(f"{path}: 'build_args' must be a mapping")
    if not isinstance(env, dict):
        raise ContentError(f"{path}: 'env' must be a mapping")

    name = raw.get("name") or f"case {index}"
    return Case(
        name=str(name),
        build_args={str(k): str(v) for k, v in build_args.items()},
        env={str(k): str(v) for k, v in env.items()},
        network=raw.get("network"),
    )


def _parse_exercise(raw: dict, path: Path, default_difficulty: str) -> Exercise:
    if not isinstance(raw, dict):
        raise ContentError(f"{path}: each exercise must be a mapping")

    exercise_id = _require(raw, "id", path)
    hints = raw.get("hints", []) or []
    if not isinstance(hints, list):
        raise ContentError(f"{path}: 'hints' must be a list of strings")

    kind = raw.get("kind", "image")
    if kind not in EXERCISE_KINDS:
        raise ContentError(
            f"{path}: 'kind' must be one of {', '.join(EXERCISE_KINDS)}"
        )

    try:
        settings = ComparisonSettings.from_dict(raw.get("settings"))
    except ValueError as exc:
        raise ContentError(f"{path}: {exc}") from exc

    files = _parse_files(raw.get("files"), path, "files")
    solution = _parse_files(raw.get("solution"), path, "solution")
    entry_file = raw.get("entry_file") or DEFAULT_ENTRY_FILES[kind]

    checks_raw = raw.get("checks") or []
    if not isinstance(checks_raw, list):
        raise ContentError(f"{path}: 'checks' must be a list")
    checks = [
        _parse_check(item, path / f"check {i}", settings)
        for i, item in enumerate(checks_raw, start=1)
    ]

    if not checks and kind == "cli":
        # A CLI exercise has exactly one thing to check: what the script
        # printed. The check is generated rather than written, because there
        # is only one possible answer to "what did the script do".
        checks = [
            Check(
                name="the script's output",
                kind="script",
                command=f"sh {entry_file}",
                settings=settings,
            )
        ]

    cases_raw = raw.get("cases") or []
    if not isinstance(cases_raw, list):
        raise ContentError(f"{path}: 'cases' must be a list")
    cases = [
        _parse_case(item, path / f"case {i}", i)
        for i, item in enumerate(cases_raw, start=1)
    ]

    setup = raw.get("setup") or []
    teardown = raw.get("teardown") or []
    if not isinstance(setup, list) or not all(isinstance(x, str) for x in setup):
        raise ContentError(f"{path}: 'setup' must be a list of strings")
    if not isinstance(teardown, list) or not all(isinstance(x, str) for x in teardown):
        raise ContentError(f"{path}: 'teardown' must be a list of strings")

    return Exercise(
        id=exercise_id,
        title=_require(raw, "title", path),
        prompt=_require(raw, "prompt", path),
        kind=kind,
        files=files,
        solution=solution,
        checks=checks,
        cases=cases,
        setup=list(setup),
        teardown=list(teardown),
        entry_file=str(entry_file),
        starter_files=dict(files),
        hints=[str(h) for h in hints],
        teach=raw.get("teach", "") or "",
        difficulty=raw.get("difficulty", default_difficulty),
        settings=settings,
        tags=[str(t) for t in (raw.get("tags", []) or [])],
    )


def _parse_lesson(raw: dict, path: Path, default_difficulty: str) -> Lesson:
    if not isinstance(raw, dict):
        raise ContentError(f"{path}: each lesson must be a mapping")

    lesson_id = _require(raw, "id", path)
    exercises = [
        _parse_exercise(item, path / f"exercise {i}", default_difficulty)
        for i, item in enumerate(raw.get("exercises", []) or [], start=1)
    ]
    if not exercises:
        raise ContentError(f"{path}: lesson '{lesson_id}' has no exercises")

    return Lesson(
        id=lesson_id,
        title=_require(raw, "title", path),
        summary=raw.get("summary", "") or "",
        body=raw.get("body", "") or "",
        exercises=exercises,
    )


def _parse_track(raw: dict, path: Path) -> Track:
    if not isinstance(raw, dict):
        raise ContentError(f"{path}: each track must be a mapping")

    track_id = _require(raw, "id", path)
    difficulty = raw.get("difficulty", "beginner")
    lessons = [
        _parse_lesson(item, path / f"lesson {i}", difficulty)
        for i, item in enumerate(raw.get("lessons", []) or [], start=1)
    ]
    if not lessons:
        raise ContentError(f"{path}: track '{track_id}' has no lessons")

    return Track(
        id=track_id,
        title=_require(raw, "title", path),
        description=raw.get("description", "") or "",
        difficulty=difficulty,
        lessons=lessons,
        order=raw.get("order", 0),
    )


def _validate(curriculum: Curriculum) -> None:
    """Reject duplicate ids and missing pieces.

    Duplicate exercise ids are checked across *all* tracks, not just within
    one: ids key both the progress table and the API, so a collision would
    make one exercise silently overwrite the other's progress.
    """
    seen_exercises: dict[str, str] = {}
    seen_lessons: dict[str, str] = {}

    for track in curriculum.tracks:
        for lesson in track.lessons:
            if lesson.id in seen_lessons:
                raise ContentError(
                    f"duplicate lesson id '{lesson.id}' in "
                    f"'{seen_lessons[lesson.id]}' and track '{track.id}'"
                )
            seen_lessons[lesson.id] = track.id

            for exercise in lesson.exercises:
                if exercise.id in seen_exercises:
                    raise ContentError(
                        f"duplicate exercise id '{exercise.id}' in "
                        f"'{seen_exercises[exercise.id]}' and lesson '{lesson.id}'"
                    )
                seen_exercises[exercise.id] = lesson.id

                if not exercise.checks:
                    raise ContentError(
                        f"exercise '{exercise.id}' has no checks -- without them "
                        "there is nothing to grade against"
                    )

                if not exercise.files:
                    raise ContentError(
                        f"exercise '{exercise.id}' has no starter files -- a "
                        "learner should never face an empty editor"
                    )

                if not exercise.solution:
                    raise ContentError(
                        f"exercise '{exercise.id}' has no reference solution"
                    )

                if exercise.entry_file not in exercise.solution:
                    raise ContentError(
                        f"exercise '{exercise.id}': the reference solution has no "
                        f"'{exercise.entry_file}', which is the file this "
                        "exercise is about"
                    )

                if exercise.entry_file not in exercise.files:
                    raise ContentError(
                        f"exercise '{exercise.id}': the starter has no "
                        f"'{exercise.entry_file}'"
                    )

                if exercise.kind == "cli" and not exercise.setup:
                    raise ContentError(
                        f"exercise '{exercise.id}': a CLI exercise needs 'setup' "
                        "commands, or there is nothing for the script to look at"
                    )


def load_curriculum(content_dir: Path | str = CONTENT_DIR) -> Curriculum:
    """Read every track file and return a validated curriculum."""
    content_dir = Path(content_dir)
    if not content_dir.is_dir():
        raise ContentError(f"content directory not found: {content_dir}")

    tracks: list[Track] = []
    seen_track_ids: set[str] = set()
    for track_file in sorted(content_dir.glob("*/track.yaml")):
        track = _parse_track(
            yaml.safe_load(track_file.read_text(encoding="utf-8")), track_file
        )
        if track.id in seen_track_ids:
            raise ContentError(f"duplicate track id '{track.id}' in {track_file}")
        seen_track_ids.add(track.id)
        tracks.append(track)

    if not tracks:
        raise ContentError(f"no track files found under {content_dir}")

    tracks.sort(key=lambda t: (t.order, t.title.lower()))

    exercises: dict[str, Exercise] = {}
    lessons: dict[str, Lesson] = {}
    for track in tracks:
        for lesson in track.lessons:
            lessons[lesson.id] = lesson
            for exercise in lesson.exercises:
                exercises[exercise.id] = exercise

    curriculum = Curriculum(
        version=CURRICULUM_VERSION,
        tracks=tracks,
        exercises=exercises,
        lessons=lessons,
    )
    _validate(curriculum)
    return curriculum
