"""Running shell scripts: the CLI exercises, and the setup around them.

A CLI exercise asks the learner to write the *commands* -- ``docker ps
--format``, ``docker inspect``, ``docker logs`` -- rather than a Dockerfile.
The script is run by ``sh`` in a throwaway working directory, with a wall
clock and an output cap.

**The trust model, stated plainly.** A CLI exercise hands the learner the
docker command line, so a learner can do anything the person running the app
can. That is a real thing and it is not hidden: this is a local, single-user
teaching application, and the person running it already has a shell. It is
*not* a boundary against someone who is not already trusted, and a deployment
that accepts submissions from untrusted people should not enable the CLI
exercise kind at all.

What the runner does anyway:

* a **minimal environment**, so no credential in the developer's shell leaks
  into a learner's script;
* a **wall clock**, so a script that waits forever is stopped;
* an **output cap**, so a script that prints forever does not exhaust memory;
* a **denylist** of the flags that grant a container the host's privileges.
  This is a denylist rather than an allow-list for the same reason the SQL
  trainer used one: an allow-list has to enumerate every flag Docker will
  ever grow, and silently blocking a legitimate command is a bad trade. It
  catches accidents; it is not a sandbox.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.config import OUTPUT_LIMIT_BYTES, SCRIPT_TIMEOUT_SECONDS
from app.engine.cli import EngineError, EngineTimeout, child_env, run

# Flags that hand a container the host's privileges. None of them has any
# teaching value in an exercise about reading containers, and all of them are
# catastrophic if used by accident.
FORBIDDEN_FLAGS = (
    "--privileged",
    "--pid=host",
    "--ipc=host",
    "--uts=host",
    "--net=host",
    "--network=host",
    "--userns=host",
    "--cap-add",
    "--cap-drop",
    "--security-opt",
    "--device",
)


class ScriptRejected(EngineError):
    """The script used something this app refuses to run."""


def check_script(script: str) -> None:
    """Refuse a script that uses a privilege-escalating flag.

    A text scan, not a parser: it catches the obvious cases and it says
    plainly that it is a guard against accidents rather than a sandbox.
    """
    for flag in FORBIDDEN_FLAGS:
        if flag in script:
            raise ScriptRejected(
                f"This exercise may not use `{flag}`. It would give a container "
                "the host's privileges, which is never what an exercise is "
                "teaching.",
                kind="forbidden_flag",
            )


def run_script(
    script: str,
    workdir: Path,
    *,
    timeout: float = SCRIPT_TIMEOUT_SECONDS,
    env: dict[str, str] | None = None,
) -> tuple[dict, str]:
    """Run a shell script and return its result plus the raw log.

    The result is ``{"stdout", "stderr", "exit_code"}``. A script that exits
    non-zero is a result, not an error.
    """
    check_script(script)

    workdir.mkdir(parents=True, exist_ok=True)
    script_path = workdir / "submission.sh"
    script_path.write_text(script, encoding="utf-8")
    script_path.chmod(0o700)

    environment = child_env(home=workdir)
    environment.update(env or {})

    try:
        result = run(
            ["sh", str(script_path)],
            timeout=timeout,
            cwd=workdir,
            env=environment,
            limit=OUTPUT_LIMIT_BYTES,
        )
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


def run_shell(
    command: str,
    workdir: Path,
    *,
    timeout: float = SCRIPT_TIMEOUT_SECONDS,
    env: dict[str, str] | None = None,
    allow_failure: bool = True,
) -> tuple[dict, str]:
    """Run one shell command.

    Used for the setup and teardown around a CLI exercise. Failures are
    returned rather than raised by default, because teardown is expected to
    fail when there was nothing to tear down.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    environment = child_env(home=workdir)
    environment.update(env or {})

    try:
        result = run(
            ["sh", "-c", command],
            timeout=timeout,
            cwd=workdir,
            env=environment,
            limit=OUTPUT_LIMIT_BYTES,
        )
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

    payload = {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.returncode,
        "timed_out": False,
        "error": None if (result.ok or allow_failure) else result.stderr.strip(),
    }
    return payload, result.output()


# Container IDs and image digests are random, so an exercise that prints them
# would never match. These patterns replace them with a stable placeholder.
_ID = re.compile(r"\b[0-9a-f]{12,64}\b")
_TIMESTAMP = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b")


def scrub(text: str) -> str:
    """Replace volatile values -- ids, digests, timestamps -- with placeholders.

    Applied to both sides of a comparison, so a learner is never marked down
    for a container id that was never going to match.
    """
    text = _TIMESTAMP.sub("<timestamp>", text)
    return _ID.sub("<id>", text)
