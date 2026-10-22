"""The one place that shells out to the docker CLI.

Everything the engine does to a Docker daemon goes through :func:`run`. There
is exactly one place that knows how a subprocess is spawned, how its output is
capped, and how it is killed when it overruns, so there is exactly one place
to audit.

Three rules, and they are not negotiable:

1. **No shell.** Every call is a fixed argv list. A learner's Dockerfile
   content is passed as an argument, never interpolated into a command line.
2. **A minimal environment.** The child sees ``PATH``, ``HOME`` and
   ``DOCKER_HOST`` and nothing else, so a credential in the developer's shell
   does not leak into a learner's build.
3. **A wall clock.** Every call has a deadline, and the whole process group is
   killed when it passes. A build that hangs dies with its children.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import OUTPUT_LIMIT_BYTES

# The docker binary. Overridable so a test can point at a stub.
DOCKER_BINARY = os.environ.get("DOCKER_BINARY", "docker")


class EngineError(Exception):
    """A user-facing failure. The message is safe to show in the UI."""

    def __init__(self, message: str, *, kind: str = "error") -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind


class EngineTimeout(EngineError):
    """The command ran past its wall clock and was stopped."""


@dataclass
class CommandResult:
    """The outcome of one docker invocation."""

    argv: list[str] = field(default_factory=list)
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    duration_ms: float = 0.0
    timed_out: bool = False
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def output(self) -> str:
        """stdout and stderr together, for a single log blob."""
        parts = [part for part in (self.stdout, self.stderr) if part]
        return "\n".join(parts)

    def to_payload(self) -> dict:
        return {
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": round(self.duration_ms, 2),
            "timed_out": self.timed_out,
            "truncated": self.truncated,
        }


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------


def child_env(home: str | Path | None = None) -> dict[str, str]:
    """A minimal environment for a docker subprocess.

    ``DOCKER_HOST`` is kept because a developer on a remote or rootless
    daemon needs it; everything else in the developer's environment is
    dropped.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(home or os.environ.get("HOME", "/tmp")),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "DOCKER_BUILDKIT": "1",
    }
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY"):
        if name in os.environ:
            env[name] = os.environ[name]
    return env


# --------------------------------------------------------------------------
# Process plumbing
# --------------------------------------------------------------------------


def _kill(proc: subprocess.Popen) -> None:
    """Kill the child's whole process group.

    ``start_new_session=True`` gives the child its own process group, so this
    takes any grandchildren with it -- which is the difference between "the
    build stopped" and "the build is still running".
    """
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:  # pragma: no cover - the process is already gone
            pass


def _pump(stream, limit: int, state: dict) -> None:
    """Read a pipe into memory, up to ``limit`` bytes.

    Reading stops at the limit rather than growing without bound: a learner's
    container that prints a gigabyte must not be able to exhaust the server's
    memory before the wall clock notices.
    """
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            if total + len(chunk) > limit:
                keep = limit - total
                if keep > 0:
                    chunks.append(chunk[:keep])
                state["overflow"] = True
                break
            chunks.append(chunk)
            total += len(chunk)
    except (OSError, ValueError):
        pass
    finally:
        state["data"] = b"".join(chunks)


def run(
    argv: list[str],
    *,
    timeout: float,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
    limit: int = OUTPUT_LIMIT_BYTES,
) -> CommandResult:
    """Run a command, capturing its output under a wall clock.

    Raises :class:`EngineTimeout` when the command overruns; returns a
    :class:`CommandResult` with a non-zero ``returncode`` for anything that
    merely failed. A command that exits badly is feedback, not an exception.
    """
    started = time.perf_counter()

    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            argv,
            cwd=str(cwd) if cwd else None,
            env=env if env is not None else child_env(),
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=(os.name == "posix"),
        )
    except FileNotFoundError as exc:
        raise EngineError(
            "The docker command was not found on this machine. Docker is "
            "required to build and run the exercises.",
            kind="docker_missing",
        ) from exc
    except OSError as exc:
        raise EngineError(f"Could not start docker: {exc}", kind="docker_error") from exc

    out_state: dict = {"overflow": False, "data": b""}
    err_state: dict = {"overflow": False, "data": b""}
    threads = [
        threading.Thread(
            target=_pump, args=(proc.stdout, limit, out_state), daemon=True
        ),
        threading.Thread(
            target=_pump, args=(proc.stderr, limit, err_state), daemon=True
        ),
    ]
    for thread in threads:
        thread.start()

    payload = stdin.encode("utf-8") if stdin is not None else None

    timed_out = False
    deadline = time.monotonic() + timeout
    while proc.poll() is None:
        if out_state["overflow"] or err_state["overflow"]:
            _kill(proc)
            break
        if time.monotonic() > deadline:
            timed_out = True
            _kill(proc)
            break
        time.sleep(0.02)

    try:
        if payload is not None and proc.stdin:
            try:
                proc.stdin.write(payload)
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - the kill should work
        _kill(proc)
        proc.wait()

    for thread in threads:
        thread.join(timeout=2)

    duration_ms = (time.perf_counter() - started) * 1000

    if timed_out:
        raise EngineTimeout(
            f"The command was stopped after {timeout:g} seconds. Look for "
            "something that waits for input, or a container that never exits.",
            kind="timeout",
        )

    truncated = bool(out_state["overflow"] or err_state["overflow"])
    return CommandResult(
        argv=list(argv),
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=out_state["data"].decode("utf-8", errors="replace"),
        stderr=err_state["data"].decode("utf-8", errors="replace"),
        duration_ms=duration_ms,
        truncated=truncated,
    )


def docker(
    args: list[str],
    *,
    timeout: float,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
) -> CommandResult:
    """Run ``docker <args>``."""
    return run([DOCKER_BINARY, *args], timeout=timeout, cwd=cwd, env=env, stdin=stdin)


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------


def docker_binary() -> str | None:
    """The path to the docker binary, or None if it is not installed."""
    return shutil.which(DOCKER_BINARY)


def daemon_available() -> bool:
    """Is the docker daemon actually reachable?

    A missing binary and an unreachable daemon are different problems with
    different messages, so they are checked separately.
    """
    if docker_binary() is None:
        return False
    try:
        result = docker(["version", "--format", "{{.Server.Version}}"], timeout=10.0)
    except EngineError:
        return False
    return result.ok


def daemon_version() -> str | None:
    """The server version, or None when the daemon is unreachable."""
    try:
        result = docker(["version", "--format", "{{.Server.Version}}"], timeout=10.0)
    except EngineError:
        return None
    return result.stdout.strip() if result.ok else None


def compose_available() -> bool:
    """Is the compose plugin installed?"""
    if docker_binary() is None:
        return False
    try:
        result = docker(["compose", "version"], timeout=15.0)
    except EngineError:
        return False
    return result.ok
