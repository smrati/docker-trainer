"""Running a command inside a built image.

This is the "what does this container actually do?" half of grading. A
Dockerfile is a *description* of a container; the only honest way to know
what the container does is to run it and watch.

What the container gets, and why:

* **No network** by default. A check has no business reaching the internet,
  and a build context has no business being able to send anything out.
* **A memory cap, a CPU quota and a PID limit.** A learner's image that
  allocates a gigabyte dies with an OOM; one that forks dies with the
  container's PID limit. Neither takes the machine with it.
* **Its own ``USER``.** The container runs as whoever the image says it
  should, which is exactly what an exercise about ``USER`` is testing.
* **Its own ``WORKDIR``**, because a Dockerfile that sets ``WORKDIR`` should
  have its commands run there.

The command is run through ``sh -c``, so a check can be a shell line --
``ls /app`` or ``cat /etc/os-release | head -1`` -- and the shell is the
image's own, not one the app supplies.
"""

from __future__ import annotations

from app.config import (
    CONTAINER_CPUS,
    CONTAINER_MEMORY_BYTES,
    CONTAINER_PIDS_LIMIT,
    DEFAULT_RUN_NETWORK,
    RUN_TIMEOUT_SECONDS,
)
from app.engine.cli import EngineError, EngineTimeout, docker


def run_in_container(
    image: str,
    command: str,
    *,
    env: dict[str, str] | None = None,
    network: str | None = None,
    timeout: float = RUN_TIMEOUT_SECONDS,
    workdir: str | None = None,
    user: str | None = None,
) -> dict:
    """Run one shell command inside a built image.

    Returns ``{"stdout": ..., "exit_code": ...}``. A command that fails is a
    *result*, not an error: the exit code is part of what is being compared.
    """
    args = [
        "run",
        "--rm",
        "--network",
        network or DEFAULT_RUN_NETWORK,
        "--memory",
        f"{CONTAINER_MEMORY_BYTES // (1024 * 1024)}m",
        "--cpus",
        str(CONTAINER_CPUS),
        "--pids-limit",
        str(CONTAINER_PIDS_LIMIT),
    ]
    for key, value in (env or {}).items():
        args += ["--env", f"{key}={value}"]
    if workdir:
        args += ["--workdir", workdir]
    if user:
        args += ["--user", user]
    # The image's own ENTRYPOINT is replaced, so a check always runs the
    # command it was asked to run rather than the image's default program.
    args += ["--entrypoint", "sh", image, "-c", command]

    try:
        result = docker(args, timeout=timeout)
    except EngineTimeout as exc:
        return {
            "stdout": "",
            "stderr": exc.message,
            "exit_code": None,
            "timed_out": True,
            "error": exc.message,
        }
    except EngineError as exc:
        return {
            "stdout": "",
            "stderr": exc.message,
            "exit_code": None,
            "timed_out": False,
            "error": exc.message,
        }

    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.returncode,
        "timed_out": False,
        "error": None,
    }


def run_default(
    image: str,
    *,
    env: dict[str, str] | None = None,
    network: str | None = None,
    timeout: float = RUN_TIMEOUT_SECONDS,
) -> tuple[dict, str]:
    """Run the image with *its own* entrypoint and command.

    This is what ``docker run <image>`` does: no command is given, so the
    image's ``ENTRYPOINT`` and ``CMD`` decide what happens. It is the only
    way to find out what an image actually does when it is started.

    The difference from :func:`run_in_container` is the whole point: this
    one does *not* override the entrypoint.
    """
    args = [
        "run",
        "--rm",
        "--network",
        network or DEFAULT_RUN_NETWORK,
        "--memory",
        f"{CONTAINER_MEMORY_BYTES // (1024 * 1024)}m",
        "--cpus",
        str(CONTAINER_CPUS),
        "--pids-limit",
        str(CONTAINER_PIDS_LIMIT),
    ]
    for key, value in (env or {}).items():
        args += ["--env", f"{key}={value}"]
    args.append(image)

    try:
        result = docker(args, timeout=timeout)
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


def run_and_capture(
    image: str,
    command: str,
    *,
    env: dict[str, str] | None = None,
    network: str | None = None,
    timeout: float = RUN_TIMEOUT_SECONDS,
) -> tuple[dict, str]:
    """Run a command and return the result plus the raw docker log.

    The log is kept separately because a container that failed to *start* --
    no shell in the image, a bad entrypoint -- says so on stderr in a way
    the command's own output never will.
    """
    args = [
        "run",
        "--rm",
        "--network",
        network or DEFAULT_RUN_NETWORK,
        "--memory",
        f"{CONTAINER_MEMORY_BYTES // (1024 * 1024)}m",
        "--cpus",
        str(CONTAINER_CPUS),
        "--pids-limit",
        str(CONTAINER_PIDS_LIMIT),
    ]
    for key, value in (env or {}).items():
        args += ["--env", f"{key}={value}"]
    args += ["--entrypoint", "sh", image, "-c", command]

    try:
        result = docker(args, timeout=timeout)
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
