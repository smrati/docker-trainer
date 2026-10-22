"""Building images, and reading what came out.

Three things happen here:

* **Building.** ``docker build`` over a context directory, with a tag under
  the app's own prefix so that everything the trainer owns is visible in
  ``docker images`` and removable by a single script.
* **Caching.** The reference image for an exercise is built once and reused.
  The reference is the expensive part -- a learner rebuilding it on every
  submission would double the wait for no benefit.
* **Reading the result.** A build that fails says *why* in a way a learner
  can act on: which instruction failed, and what the shell said about it.
  Docker's own output is verbose and ordered for a human reading a terminal,
  so the important lines are pulled out of it.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from app.config import BUILD_TIMEOUT_SECONDS, IMAGE_TAG_PREFIX
from app.engine.cli import EngineError, EngineTimeout, docker

# The marker Docker puts next to the instruction that failed.
_FAILING_LINE = re.compile(r"^\s*>\s*\[\d+/\d+\]\s*(.*)$")
# The `Dockerfile:12` header that precedes the source snippet.
_LOCATION = re.compile(r"^([A-Za-z0-9_./-]+):(\d+)\s*$")
# The `ERROR: ...` summary line.
_ERROR_LINE = re.compile(r"^.*\bERROR:\s*(.+)$")
# The shell's own complaint, e.g. `0.580 /bin/sh: nope: not found`.
_SHELL_ERROR = re.compile(r"^\s*[\d.]+\s+(?:/[\w./-]+/)?(?:sh|bash):\s*(.+)$")


@dataclass
class BuildResult:
    """The outcome of one image build."""

    ok: bool = False
    tag: str = ""
    log: str = ""
    error: str | None = None
    failing_instruction: str | None = None
    location: str | None = None
    duration_ms: float = 0.0
    timed_out: bool = False

    def to_payload(self) -> dict:
        return {
            "ok": self.ok,
            "tag": self.tag,
            "log": self.log,
            "error": self.error,
            "failing_instruction": self.failing_instruction,
            "location": self.location,
            "duration_ms": round(self.duration_ms, 2),
            "timed_out": self.timed_out,
        }


def reference_tag(exercise_id: str, suffix: str = "", fingerprint: str = "") -> str:
    """The tag a reference image is cached under.

    A Docker *tag* may not contain a slash -- only a repository name may --
    so the app's namespaces are joined with a dash.

    ``fingerprint`` is a hash of the reference files. It is part of the tag
    so that editing a reference produces a *different* tag: otherwise the
    cached image from the previous version of the reference would be reused
    and the new reference would never be built.
    """
    tail = re.sub(r"[^A-Za-z0-9._-]", "-", suffix).strip("-")
    tail = f"-{tail}" if tail else ""
    mark = re.sub(r"[^A-Za-z0-9._-]", "", fingerprint)[:8]
    mark = f"-{mark}" if mark else ""
    return f"{IMAGE_TAG_PREFIX}-ref-{exercise_id}{tail}{mark}"


def learner_tag(exercise_id: str, token: str) -> str:
    """The tag a learner's image is built under.

    The token makes the tag unique per submission, so two learners (or two
    attempts) never fight over the same tag.
    """
    return f"{IMAGE_TAG_PREFIX}-work-{exercise_id}-{token}"


def image_exists(tag: str) -> bool:
    """Is this image already built?"""
    try:
        result = docker(["image", "inspect", tag], timeout=20.0)
    except EngineError:
        return False
    return result.ok


def remove_image(tag: str) -> None:
    """Delete an image, ignoring the case where it is already gone."""
    try:
        docker(["image", "rm", "--force", tag], timeout=60.0)
    except EngineError:
        pass


def build(
    context_dir: Path,
    tag: str,
    *,
    dockerfile: str = "Dockerfile",
    build_args: dict[str, str] | None = None,
    timeout: float = BUILD_TIMEOUT_SECONDS,
    extra_args: list[str] | None = None,
) -> BuildResult:
    """Build an image from a context directory.

    ``--pull`` is never passed: the base image is pulled on first use and
    cached afterwards, and forcing a pull on every submission would make the
    app depend on the network for every answer.
    """
    args = ["build", "--tag", tag, "--file", str(context_dir / dockerfile)]
    for key, value in (build_args or {}).items():
        args += ["--build-arg", f"{key}={value}"]
    if extra_args:
        args += list(extra_args)
    args.append(str(context_dir))

    try:
        result = docker(args, timeout=timeout, cwd=context_dir)
    except EngineTimeout:
        return BuildResult(
            ok=False,
            tag=tag,
            log="",
            error=(
                f"The build was stopped after {timeout:g} seconds. A build that "
                "takes this long is usually waiting on the network -- a `pip "
                "install` that never finishes, or a base image that cannot be "
                "pulled."
            ),
            duration_ms=timeout * 1000,
            timed_out=True,
        )
    except EngineError as exc:
        return BuildResult(ok=False, tag=tag, error=exc.message)

    log = result.output()
    if result.ok:
        return BuildResult(
            ok=True,
            tag=tag,
            log=log,
            duration_ms=result.duration_ms,
        )

    instruction, location, error = _extract_failure(log)
    return BuildResult(
        ok=False,
        tag=tag,
        log=log,
        error=error or "The build failed. The full output is below.",
        failing_instruction=instruction,
        location=location,
        duration_ms=result.duration_ms,
    )


def _extract_failure(log: str) -> tuple[str | None, str | None, str | None]:
    """Pull the interesting parts out of a failed build's output.

    Docker's output is thorough but long, and the three things a learner
    needs are the instruction that failed, where it is, and what the shell
    said. Everything else is build machinery.
    """
    instruction: str | None = None
    location: str | None = None
    error: str | None = None
    shell_error: str | None = None

    for line in log.splitlines():
        match = _FAILING_LINE.match(line)
        if match and instruction is None:
            # Docker writes the instruction with a trailing colon; the colon
            # is punctuation, not part of the instruction.
            instruction = match.group(1).strip().rstrip(":").strip()

        match = _LOCATION.match(line.strip())
        if match and location is None:
            location = f"{match.group(1)} line {match.group(2)}"

        match = _SHELL_ERROR.match(line)
        if match and shell_error is None:
            shell_error = match.group(1).strip()

        match = _ERROR_LINE.match(line)
        if match and error is None:
            message = match.group(1).strip()
            # The final "failed to build: failed to solve: ..." line repeats
            # what the ERROR line above already said.
            if message.startswith("failed to build:"):
                continue
            error = message

    if error is None and shell_error is not None:
        error = shell_error
    elif error is not None and shell_error and shell_error not in error:
        # The shell's own message says *why*; the ERROR line says which
        # command. A learner wants both.
        error = f"{error} ({shell_error})"

    if error is None and log.strip():
        error = "The build failed. The full output is below."
    return instruction, location, error


def inspect(tag: str, template: str) -> object:
    """Read one field out of an image's configuration.

    ``template`` is a full Go template, as ``docker image inspect --format``
    takes it: ``{{.Config.ExposedPorts}}``, ``{{len .RootFS.Layers}}``,
    ``{{json .Config.Env}}``.

    The result is parsed as JSON when it can be, and kept as text when it
    cannot -- so a check can compare either a structure or a plain string.
    """
    result = docker(
        ["image", "inspect", "--format", template, tag],
        timeout=30.0,
    )
    if not result.ok:
        raise EngineError(
            f"Could not read {template} from the built image: "
            f"{result.stderr.strip() or 'the image was not found'}",
            kind="inspect_failed",
        )

    text = result.stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def fingerprint(files: dict[str, str]) -> str:
    """A short hash of a set of files.

    Used in the reference image's tag, so that editing a reference produces
    a different tag and the stale cached image is not reused.
    """
    payload = json.dumps(files, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_or_reuse(
    context_dir: Path,
    tag: str,
    *,
    dockerfile: str = "Dockerfile",
    build_args: dict[str, str] | None = None,
    timeout: float = BUILD_TIMEOUT_SECONDS,
    reuse: bool = False,
) -> BuildResult:
    """Build an image, or reuse one that is already there.

    ``reuse`` is used for the reference image, which is built once per
    exercise and then kept. The learner's image is always rebuilt: the point
    of a submission is that it is the learner's *current* files that get
    built.
    """
    if reuse and image_exists(tag):
        return BuildResult(ok=True, tag=tag, log="", duration_ms=0.0)
    return build(
        context_dir,
        tag,
        dockerfile=dockerfile,
        build_args=build_args,
        timeout=timeout,
    )


@dataclass
class ImageInfo:
    """A summary of a built image, for the UI."""

    tag: str = ""
    size_bytes: int = 0
    layers: int = 0
    created: str = ""
    user: str = ""
    exposed_ports: dict = field(default_factory=dict)
    entrypoint: list = field(default_factory=list)
    cmd: list = field(default_factory=list)
    env: list = field(default_factory=list)
    workdir: str = ""
    labels: dict = field(default_factory=dict)


def describe_image(tag: str) -> ImageInfo:
    """Read the interesting parts of an image's configuration."""
    result = docker(["image", "inspect", tag], timeout=30.0)
    if not result.ok:
        raise EngineError(
            "Could not inspect the built image.", kind="inspect_failed"
        )
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise EngineError("The image inspection was unreadable.", kind="inspect_failed") from exc

    entry = (payload or [{}])[0]
    config = entry.get("Config") or {}
    return ImageInfo(
        tag=tag,
        size_bytes=int(entry.get("Size") or 0),
        layers=len((entry.get("RootFS") or {}).get("Layers") or []),
        created=str(entry.get("Created") or ""),
        user=str(config.get("User") or ""),
        exposed_ports=config.get("ExposedPorts") or {},
        entrypoint=list(config.get("Entrypoint") or []),
        cmd=list(config.get("Cmd") or []),
        env=list(config.get("Env") or []),
        workdir=str(config.get("WorkingDir") or ""),
        labels=config.get("Labels") or {},
    )


def prune(*, dry_run: bool = False) -> list[str]:
    """Remove every image this app built.

    Only images under the app's own prefixes are touched, so a learner's own
    images are never at risk.
    """
    removed: list[str] = []
    result = docker(["images", "--format", "{{.Repository}}:{{.Tag}}"], timeout=60.0)
    if not result.ok:
        return removed

    prefixes = (f"{IMAGE_TAG_PREFIX}-ref-", f"{IMAGE_TAG_PREFIX}-work-")
    for line in result.stdout.splitlines():
        tag = line.strip()
        if not tag.startswith(prefixes):
            continue
        removed.append(tag)
        if not dry_run:
            remove_image(tag)
    return removed


def disk_usage() -> dict:
    """How much space the trainer's images and build cache are using.

    ``docker system df`` reports sizes as human-readable strings ("82.69MB"),
    so they are parsed rather than cast.
    """
    result = docker(["system", "df", "--format", "{{json .}}"], timeout=60.0)
    if not result.ok:
        return {}

    usage: dict = {"images": 0, "image_bytes": 0, "build_cache_bytes": 0}
    for line in result.stdout.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        kind = entry.get("Type")
        if kind == "Images":
            usage["images"] = int(entry.get("TotalCount") or 0)
            usage["image_bytes"] = _parse_size(entry.get("Size"))
        elif kind == "Build Cache":
            usage["build_cache_bytes"] = _parse_size(entry.get("Size"))
    usage["total_bytes"] = usage["image_bytes"] + usage["build_cache_bytes"]
    return usage


def _parse_size(text: str | None) -> int:
    """Parse docker's human-readable size into bytes."""
    if not text:
        return 0
    match = re.match(r"^\s*([0-9.]+)\s*([KMGT]?B)?\s*$", text, re.IGNORECASE)
    if not match:
        return 0
    value = float(match.group(1))
    unit = (match.group(2) or "B").upper()
    factors = {
        "B": 1,
        "KB": 1024,
        "MB": 1024**2,
        "GB": 1024**3,
        "TB": 1024**4,
    }
    return int(value * factors.get(unit, 1))


def local_base_images() -> list[str]:
    """The base images already on this machine, for the lessons to mention."""
    result = docker(["images", "--format", "{{.Repository}}:{{.Tag}}"], timeout=30.0)
    if not result.ok:
        return []
    return sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})


def context_size(root: Path) -> int:
    """How many bytes a build context occupies on disk."""
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def which(binary: str) -> str | None:
    """Where a binary lives, if it is installed."""
    return shutil.which(binary)
