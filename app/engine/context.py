"""Turning a submission into a build context.

A submission is a map of path -> text. Before it can be built it has to
exist on disk, in a directory the daemon can read, with the paths the
learner meant.

Three rules, and each one has bitten somebody:

1. **Paths are relative.** ``/etc/passwd`` and ``../secrets`` are refused
   outright. A build context is copied into the daemon, so an absolute path
   is not a file the learner wrote -- it is a file the *host* has.
2. **Paths stay inside the context.** ``a/../../b`` is refused, because
   ``a/..`` is not a directory the engine created.
3. **Text only.** Every file is UTF-8 text. A binary blob in a build
   context is a mistake, not a feature, and it is easier to say so than to
   base64 it.
"""

from __future__ import annotations

from pathlib import Path

from app.config import MAX_FILE_BYTES, MAX_SUBMISSION_FILES
from app.engine.cli import EngineError


class InvalidSubmission(EngineError):
    """The submission cannot be turned into a build context."""


def validate_path(relative: str) -> Path:
    """Check one submitted path and return it as a :class:`Path`.

    The returned path is relative and safe to join onto a root.
    """
    if not relative or not relative.strip():
        raise InvalidSubmission("A file name may not be empty.", kind="invalid_path")
    if relative.startswith("/") or (len(relative) > 1 and relative[1] == ":"):
        raise InvalidSubmission(
            f"“{relative}” is an absolute path. Every file in a build context "
            "is addressed relative to the context directory.",
            kind="invalid_path",
        )
    if "\\" in relative:
        raise InvalidSubmission(
            f"“{relative}” uses a backslash. Paths in a build context always "
            "use forward slashes.",
            kind="invalid_path",
        )

    parts = [part for part in relative.split("/") if part not in ("", ".")]
    if not parts:
        raise InvalidSubmission(f"“{relative}” is not a usable file name.", kind="invalid_path")
    if any(part == ".." for part in parts):
        raise InvalidSubmission(
            f"“{relative}” points outside the build context.",
            kind="invalid_path",
        )

    path = Path(*parts)
    if path.name in ("", ".", ".."):
        raise InvalidSubmission(f"“{relative}” is not a usable file name.", kind="invalid_path")
    return path


def materialise(files: dict[str, str], root: Path) -> list[str]:
    """Write a submission into ``root`` and return the paths written.

    Existing content in ``root`` is not cleared: the caller owns the
    directory and decides whether it is fresh.
    """
    if len(files) > MAX_SUBMISSION_FILES:
        raise InvalidSubmission(
            f"This exercise accepts at most {MAX_SUBMISSION_FILES} files, and "
            f"the submission has {len(files)}.",
            kind="too_many_files",
        )

    written: list[str] = []
    root.mkdir(parents=True, exist_ok=True)

    for relative, content in files.items():
        path = validate_path(relative)
        target = root / path

        # Belt and braces: validate_path already refuses `..` and absolute
        # paths, but the resolved target is checked too, so a future change
        # to the rules cannot quietly open a hole.
        resolved_root = root.resolve()
        try:
            resolved_target = target.resolve()
        except OSError:  # pragma: no cover - defensive
            resolved_target = target
        if resolved_target != resolved_root and resolved_root not in resolved_target.parents:
            raise InvalidSubmission(
                f"“{relative}” points outside the build context.",
                kind="invalid_path",
            )

        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise InvalidSubmission(
                f"“{relative}” is larger than {MAX_FILE_BYTES // 1024} KB.",
                kind="file_too_large",
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(path.as_posix())

    return sorted(written)


def read_context(root: Path, relative: str) -> str:
    """Read one file back out of a context directory."""
    path = validate_path(relative)
    target = root / path
    if not target.is_file():
        raise InvalidSubmission(f"There is no “{relative}” in this exercise.", kind="missing_file")
    return target.read_text(encoding="utf-8")
