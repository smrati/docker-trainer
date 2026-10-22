"""Turning a submission into results: build it, run it, compare it.

This is the parent half of the engine. It owns the temporary directory a
submission lives in, decides which checks run against which artefact, and
cleans up after itself.

The flow, and it is the same flow the Python trainer uses:

1. **Materialise** the submission into a throwaway directory.
2. **Build** the artefact -- an image, a compose project, or nothing at all
   for a CLI exercise.
3. **Run the checks** against it, one value per check.
4. **Compare** those values with the reference's.

Step 4 is where the grading happens, and it is a comparison between two
*runs*, never between a run and a hand-written expectation. The reference
artefact is built and checked the same way the learner's is, so the answer
is "what the reference does".

The reference is cached per exercise, because building it is the expensive
half and it never changes. A CLI exercise is the exception: its reference
has to run against the same prepared environment the learner's script runs
against, so it is run live, every time.
"""

from __future__ import annotations

import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.config import (
    BUILD_TIMEOUT_SECONDS,
    COMPOSE_RUN_TIMEOUT_SECONDS,
    RUN_TIMEOUT_SECONDS,
    SCRIPT_TIMEOUT_SECONDS,
)
from app.engine import compose as compose_engine
from app.engine import image as image_engine
from app.engine import probe
from app.engine import script as script_engine
from app.engine.cli import EngineError
from app.engine.compare import CheckResult, ComparisonResult, compare_checks
from app.engine.context import materialise

# The modes an evaluation can run in.
MODE_RUN = "run"
MODE_SUBMIT = "submit"


class ReferenceBroken(Exception):
    """The exercise's own reference artefact does not work.

    Always an authoring bug, and always the app's fault rather than the
    learner's.
    """


@dataclass
class CheckOutcome:
    """One check, run against one artefact."""

    name: str
    kind: str
    result: dict = field(default_factory=dict)
    settings: object = None

    def to_dict(self) -> dict:
        payload = {"name": self.name, "kind": self.kind, "result": self.result}
        if self.settings is not None:
            payload["settings"] = (
                self.settings.to_payload()
                if hasattr(self.settings, "to_payload")
                else self.settings
            )
        return payload


@dataclass
class Evaluation:
    """The outcome of evaluating a submission."""

    ok: bool = True
    error: str | None = None
    error_kind: str = "error"
    build_log: str = ""
    checks: list[CheckOutcome] = field(default_factory=list)
    comparison: ComparisonResult | None = None
    duration_ms: float = 0.0
    image: dict | None = None
    # The tag of the image that was built, when one was. The caller removes
    # it afterwards: a learner's image is not worth keeping.
    tag: str = ""

    def to_payload(self) -> dict:
        return {
            "ok": self.ok,
            "error": {"kind": self.error_kind, "message": self.error} if self.error else None,
            "build_log": self.build_log,
            "checks": [check.to_dict() for check in self.checks],
            "grading": self.comparison.to_payload() if self.comparison else None,
            "duration_ms": round(self.duration_ms, 2),
            "image": self.image,
        }


# --------------------------------------------------------------------------
# Reference expectations
# --------------------------------------------------------------------------


def reference_expectations(curriculum, exercise) -> dict[str, list[dict]]:
    """The reference artefact's behaviour, computed once and cached.

    Keyed by the *case's* result key, not by the build key: two cases may
    build the same image and run it with different environments, and those
    two have different answers. The image itself is still built once, because
    the image is tagged by the build key.
    """
    cached = curriculum.expectations.get(exercise.id)
    if cached is not None:
        return cached

    results: dict[str, list[dict]] = {}
    for case in exercise.cases:
        key = case.cache_key
        if key in results:
            continue
        outcome = _evaluate_artefact(exercise, exercise.solution, case, tag_is_reference=True)
        if not outcome.ok:
            # A reference that does not build is an authoring bug. It is
            # surfaced as an error rather than cached as an empty answer,
            # which would quietly mark every submission wrong.
            raise ReferenceBroken(outcome.error or "The reference solution failed.")
        results[key] = [check.to_dict() for check in outcome.checks]

    curriculum.expectations[exercise.id] = results
    return results


# --------------------------------------------------------------------------
# Artefact evaluation
# --------------------------------------------------------------------------


def _evaluate_artefact(
    exercise,
    files: dict[str, str],
    case,
    *,
    tag_is_reference: bool = False,
    root: Path | None = None,
) -> Evaluation:
    """Build an artefact and run every check against it.

    ``files`` is the artefact: the reference's own files, or the learner's
    submission.
    """
    owned_root = root is None
    root = root or Path(tempfile.mkdtemp(prefix="dockertrainer-"))
    context_dir = root / "context"
    started = time.perf_counter()
    outcome: Evaluation | None = None

    try:
        try:
            materialise(files, context_dir)
        except EngineError as exc:
            return Evaluation(ok=False, error=exc.message, error_kind=exc.kind)

        if exercise.kind == "image":
            outcome = _evaluate_image(exercise, context_dir, case, tag_is_reference)
        elif exercise.kind == "compose":
            outcome = _evaluate_compose(exercise, context_dir, case, tag_is_reference)
        else:
            outcome = _evaluate_cli(exercise, context_dir)

        outcome.duration_ms = (time.perf_counter() - started) * 1000
        return outcome
    finally:
        if outcome is not None and outcome.tag and not tag_is_reference:
            # The learner's image is thrown away; only the reference is
            # worth keeping, and it is kept by tag.
            image_engine.remove_image(outcome.tag)
        if owned_root:
            shutil.rmtree(root, ignore_errors=True)


def _evaluate_image(exercise, context_dir: Path, case, tag_is_reference: bool) -> Evaluation:
    """Build the image and run the checks inside it."""
    if tag_is_reference:
        tag = image_engine.reference_tag(
            exercise.id,
            case.build_key,
            image_engine.fingerprint(exercise.solution),
        )
    else:
        # A unique tag per submission, so a stale image from an earlier
        # attempt can never be mistaken for this one.
        tag = image_engine.learner_tag(exercise.id, uuid.uuid4().hex[:10])

    build = image_engine.build_or_reuse(
        context_dir,
        tag,
        dockerfile=exercise.entry_file,
        build_args=case.build_args,
        timeout=BUILD_TIMEOUT_SECONDS,
        reuse=tag_is_reference,
    )
    if not build.ok:
        return Evaluation(
            ok=False,
            error=build.error or "The build failed.",
            error_kind="build_failed",
            build_log=build.log,
        )

    outcomes: list[CheckOutcome] = []
    for check in exercise.checks:
        if check.kind == "inspect":
            try:
                value = image_engine.inspect(tag, check.template)
            except EngineError as exc:
                return Evaluation(
                    ok=False,
                    error=exc.message,
                    error_kind=exc.kind,
                    build_log=build.log,
                )
            outcomes.append(
                CheckOutcome(
                    check.name, check.kind, {"value": value}, check.settings
                )
            )
            continue

        if check.kind in ("run", "script"):
            result, _log = probe.run_and_capture(
                tag,
                check.command,
                env=case.env,
                network=case.network,
                timeout=RUN_TIMEOUT_SECONDS,
            )
            outcomes.append(
                CheckOutcome(check.name, check.kind, result, check.settings)
            )
            continue

        if check.kind == "default":
            # The image's own entrypoint and command, the way `docker run
            # <image>` would use them.
            result, _log = probe.run_default(
                tag,
                env=case.env,
                network=case.network,
                timeout=RUN_TIMEOUT_SECONDS,
            )
            outcomes.append(
                CheckOutcome(check.name, check.kind, result, check.settings)
            )
            continue

        # A check kind that does not belong to an image exercise is an
        # authoring mistake; the loader refuses the exercise.
        return Evaluation(
            ok=False,
            error=f"A '{check.kind}' check cannot run against an image.",
            error_kind="bad_check",
            build_log=build.log,
        )

    info = None
    try:
        described = image_engine.describe_image(tag)
        info = {
            "tag": described.tag,
            "size_bytes": described.size_bytes,
            "layers": described.layers,
            "user": described.user,
            "exposed_ports": described.exposed_ports,
            "entrypoint": described.entrypoint,
            "cmd": described.cmd,
            "env": described.env,
            "workdir": described.workdir,
        }
    except EngineError:
        info = None

    return Evaluation(
        ok=True,
        build_log=build.log,
        checks=outcomes,
        image=info,
        tag=tag,
    )


def _evaluate_compose(exercise, context_dir: Path, case, tag_is_reference: bool) -> Evaluation:
    """Validate the project, build it, and run the checks in its services."""
    compose_file = compose_engine.find_compose_file_on_disk(context_dir)
    if compose_file is None:
        return Evaluation(
            ok=False,
            error=(
                "There is no compose file in your submission. It must be named "
                "compose.yaml, compose.yml, docker-compose.yaml or "
                "docker-compose.yml."
            ),
            error_kind="compose_missing",
        )

    try:
        return _compose_checks(exercise, context_dir, compose_file, case)
    finally:
        # The project is torn down whatever happened. A compose run leaves a
        # network and a volume behind, and without this they accumulate for
        # as long as the app is running.
        compose_engine.down(context_dir, compose_file)


def _compose_checks(
    exercise,
    context_dir: Path,
    compose_file: str,
    case,
) -> Evaluation:
    """The compose checks themselves, without the teardown."""
    config_result = compose_engine.config(context_dir, compose_file)
    if not config_result.ok:
        return Evaluation(
            ok=False,
            error=config_result.error or "The compose file is not valid.",
            error_kind="compose_invalid",
            build_log=config_result.log,
        )

    build_result = compose_engine.build(context_dir, compose_file)
    build_log = build_result.log
    if not build_result.ok:
        return Evaluation(
            ok=False,
            error="The compose project failed to build. The output is below.",
            error_kind="build_failed",
            build_log=build_log,
        )

    outcomes: list[CheckOutcome] = []
    for check in exercise.checks:
        if check.kind == "config":
            value = compose_engine.parse_config(config_result.stdout)
            outcomes.append(
                CheckOutcome(
                    check.name, check.kind, {"value": value}, check.settings
                )
            )
            continue

        if check.kind == "service":
            result, _log = compose_engine.run_service(
                context_dir,
                compose_file,
                check.service,
                check.command,
                timeout=COMPOSE_RUN_TIMEOUT_SECONDS,
                env=case.env,
            )
            outcomes.append(
                CheckOutcome(check.name, check.kind, result, check.settings)
            )
            continue

        return Evaluation(
            ok=False,
            error=f"A '{check.kind}' check cannot run against a compose project.",
            error_kind="bad_check",
            build_log=build_log,
        )

    return Evaluation(ok=True, build_log=build_log, checks=outcomes)


def _evaluate_cli(exercise, context_dir: Path) -> Evaluation:
    """Run the learner's script against a prepared Docker environment."""
    for command in exercise.setup:
        script_engine.run_shell(command, context_dir, timeout=SCRIPT_TIMEOUT_SECONDS)

    try:
        script_text = (context_dir / exercise.entry_file).read_text(encoding="utf-8")
    except OSError:
        script_text = ""

    result, log = script_engine.run_script(
        script_text, context_dir, timeout=SCRIPT_TIMEOUT_SECONDS
    )

    for command in exercise.teardown:
        script_engine.run_shell(command, context_dir, timeout=SCRIPT_TIMEOUT_SECONDS)

    return Evaluation(
        ok=True,
        build_log=log,
        checks=[
            CheckOutcome(
                "the script's output",
                "script",
                result,
                exercise.checks[0].settings if exercise.checks else None,
            )
        ],
    )


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def evaluate(
    curriculum,
    exercise,
    files: dict[str, str],
    *,
    mode: str = MODE_SUBMIT,
) -> Evaluation:
    """Evaluate a submission.

    ``MODE_RUN`` builds the artefact and runs the checks without comparing
    anything and without touching the reference. Exploring must never be
    mistaken for answering.

    ``MODE_SUBMIT`` also runs the reference and compares.
    """
    if mode == MODE_RUN:
        case = exercise.cases[0] if exercise.cases else None
        if case is None:
            return Evaluation(
                ok=False, error="This exercise has no cases.", error_kind="bad_exercise"
            )
        return _evaluate_artefact(exercise, files, case)

    # --- the reference -----------------------------------------------------
    expectations = reference_expectations(curriculum, exercise)

    # --- the learner's artefact -------------------------------------------
    all_results: list[CheckResult] = []
    all_checks: list[CheckOutcome] = []
    build_log = ""
    total = 0
    passed = 0
    started = time.perf_counter()

    for case in exercise.cases:
        expected = expectations.get(case.cache_key)
        if expected is None:  # pragma: no cover - the cache is filled above
            return Evaluation(
                ok=False,
                error="This exercise has no reference answer.",
                error_kind="reference_error",
            )

        evaluated = _evaluate_artefact(exercise, files, case)
        if not evaluated.ok:
            return evaluated
        build_log = evaluated.build_log or build_log

        actual = [check.to_dict() for check in evaluated.checks]
        comparison = compare_checks(expected, actual, exercise.settings)

        if not comparison.passed:
            evaluated.comparison = comparison
            evaluated.build_log = build_log
            return evaluated

        all_results.extend(comparison.checks)
        all_checks.extend(evaluated.checks)
        passed += comparison.passed_count
        total += comparison.total_count

    # Every case passed. One summary comparison carries the whole run.
    summary = ComparisonResult(
        passed=True,
        reason=None,
        detail=None,
        checks=all_results,
        passed_count=passed,
        total_count=total,
    )

    return Evaluation(
        ok=True,
        build_log=build_log,
        checks=all_checks,
        comparison=summary,
        duration_ms=(time.perf_counter() - started) * 1000,
    )
