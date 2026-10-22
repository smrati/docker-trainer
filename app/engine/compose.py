"""Compose: reading a project's configuration, and running one of its services.

Two things happen here.

**Reading the configuration.** ``docker compose config`` validates a compose
file and prints it back in a canonical form. That canonical form is what
makes a compose exercise gradeable: two compose files that differ only in
formatting, key order or quoting produce *identical* JSON, so a learner is
never marked down for writing it differently from the reference.

The one thing that is not canonical is the build context path, which compose
reports as an absolute path into whatever temporary directory the project
happened to live in. That is stripped before comparison -- otherwise every
submission would differ from every other one for a reason that has nothing
to do with the answer.

**Running a service.** ``docker compose run`` starts one service from the
project and runs a command in it. It is how a compose exercise checks that
the service it defines actually does what it says.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.config import (
    COMPOSE_BUILD_TIMEOUT_SECONDS,
    COMPOSE_PROJECT_NAME,
    COMPOSE_RUN_TIMEOUT_SECONDS,
)
from app.engine.cli import EngineError, EngineTimeout, docker

# The file names compose accepts, in the order they are looked for.
COMPOSE_FILENAMES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)


def find_compose_file(files: dict[str, str]) -> str | None:
    """Which of the submitted files is the compose file?"""
    for name in COMPOSE_FILENAMES:
        if name in files:
            return name
    # A compose file in a subdirectory is unusual but legal.
    for name in sorted(files):
        base = name.rsplit("/", 1)[-1]
        if base in COMPOSE_FILENAMES:
            return name
    return None


def find_compose_file_on_disk(root: Path) -> str | None:
    """Find the compose file in a materialised project directory.

    The *submission* decides this, not the exercise: a learner may name
    their file ``docker-compose.yml`` instead of ``compose.yaml``, and the
    grader has to grade what they actually wrote.
    """
    for name in COMPOSE_FILENAMES:
        if (root / name).is_file():
            return name
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.name in COMPOSE_FILENAMES:
            return path.relative_to(root).as_posix()
    return None


def _base_args(context_dir: Path, compose_file: str, project: str) -> list[str]:
    return [
        "compose",
        "--project-name",
        project,
        "--project-directory",
        str(context_dir),
        "--file",
        str(context_dir / compose_file),
    ]


@dataclass
class ComposeResult:
    """The outcome of one compose invocation."""

    ok: bool = False
    stdout: str = ""
    stderr: str = ""
    log: str = ""
    error: str | None = None
    duration_ms: float = 0.0

    def to_payload(self) -> dict:
        return {
            "ok": self.ok,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "log": self.log,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 2),
        }


def config(
    context_dir: Path,
    compose_file: str,
    *,
    project: str = COMPOSE_PROJECT_NAME,
    timeout: float = 60.0,
) -> ComposeResult:
    """Validate a compose file and return its canonical configuration."""
    args = [
        *_base_args(context_dir, compose_file, project),
        "config",
        "--format",
        "json",
        # Paths are left as the compose file wrote them, so a build context
        # stays relative instead of becoming an absolute path into a
        # temporary directory.
        "--no-path-resolution",
    ]
    try:
        result = docker(args, timeout=timeout, cwd=context_dir)
    except EngineTimeout as exc:
        return ComposeResult(ok=False, error=exc.message)
    except EngineError as exc:
        return ComposeResult(ok=False, error=exc.message)

    if not result.ok:
        return ComposeResult(
            ok=False,
            stdout=result.stdout,
            stderr=result.stderr,
            log=result.output(),
            error=_config_error(result.stderr or result.stdout),
            duration_ms=result.duration_ms,
        )

    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise EngineError(
            "The compose configuration could not be read as JSON.",
            kind="compose_invalid",
        ) from exc

    return ComposeResult(
        ok=True,
        stdout=json.dumps(normalise(payload, context_dir), indent=2, sort_keys=True),
        stderr=result.stderr,
        log=result.output(),
        duration_ms=result.duration_ms,
    )


def _config_error(text: str) -> str:
    """Turn compose's complaint into something a learner can act on."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        if "error" in line.lower() or "invalid" in line.lower():
            return line
    return lines[-1] if lines else "The compose file is not valid."


def normalise(payload, context_dir: Path):
    """Strip environment-specific values out of a compose configuration.

    Only one thing is environment-specific: a path that points into the
    temporary directory the project was built in. Compose reports a build
    context as an absolute path, and two identical compose files in two
    different directories would otherwise compare unequal.
    """
    root = str(context_dir.resolve())
    return _rewrite(payload, root)


def _rewrite(value, root: str):
    if isinstance(value, str):
        if value == root:
            return "."
        if value.startswith(root + "/"):
            return value[len(root) + 1 :]
        return value
    if isinstance(value, list):
        return [_rewrite(item, root) for item in value]
    if isinstance(value, dict):
        return {key: _rewrite(item, root) for key, item in value.items()}
    return value


def build(
    context_dir: Path,
    compose_file: str,
    *,
    project: str = COMPOSE_PROJECT_NAME,
    timeout: float = COMPOSE_BUILD_TIMEOUT_SECONDS,
) -> ComposeResult:
    """Build every service in the project that has a ``build:`` section."""
    args = [*_base_args(context_dir, compose_file, project), "build"]
    try:
        result = docker(args, timeout=timeout, cwd=context_dir)
    except EngineTimeout as exc:
        return ComposeResult(ok=False, error=exc.message)
    except EngineError as exc:
        return ComposeResult(ok=False, error=exc.message)

    return ComposeResult(
        ok=result.ok,
        stdout=result.stdout,
        stderr=result.stderr,
        log=result.output(),
        error=None if result.ok else "The compose build failed. The output is below.",
        duration_ms=result.duration_ms,
    )


def run_service(
    context_dir: Path,
    compose_file: str,
    service: str,
    command: str,
    *,
    project: str = COMPOSE_PROJECT_NAME,
    timeout: float = COMPOSE_RUN_TIMEOUT_SECONDS,
    env: dict[str, str] | None = None,
) -> tuple[dict, str]:
    """Run a command in one service of the project.

    ``--no-deps`` keeps the check to the service under test, and ``-T``
    turns off the TTY that compose would otherwise insist on attaching.
    """
    args = [
        *_base_args(context_dir, compose_file, project),
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "--entrypoint",
        "sh",
    ]
    for key, value in (env or {}).items():
        args += ["--env", f"{key}={value}"]
    args += [service, "-c", command]

    try:
        result = docker(args, timeout=timeout, cwd=context_dir)
    except EngineTimeout as exc:
        return (
            {
                "stdout": "",
                "stderr": exc.message,
                "exit_code": None,
                "timed_out": True,
                "error": exc.message,
            },
            exc.message,
        )
    except EngineError as exc:
        return (
            {
                "stdout": "",
                "stderr": exc.message,
                "exit_code": None,
                "timed_out": False,
                "error": exc.message,
            },
            exc.message,
        )

    return (
        {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
            "timed_out": False,
            "error": None,
        },
        result.output(),
    )


def down(context_dir: Path, compose_file: str, *, project: str = COMPOSE_PROJECT_NAME) -> None:
    """Tear the project down: stop its containers and remove its volumes.

    Errors are ignored. A teardown that fails has usually already done the
    important part, and a project that was never up is not a problem.
    """
    try:
        docker(
            [
                *_base_args(context_dir, compose_file, project),
                "down",
                "--volumes",
                "--remove-orphans",
            ],
            timeout=90.0,
            cwd=context_dir,
        )
    except EngineError:
        pass


def parse_config(text: str) -> dict:
    """Parse the JSON a successful ``compose config`` produced."""
    try:
        payload = json.loads(text)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}
