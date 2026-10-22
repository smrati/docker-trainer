"""Lint the curriculum YAML for authoring hazards.

Plain scalars in YAML have sharp edges, and the failure modes are bad:

* A scalar cannot *begin* with a reserved indicator -- `` ` `` and `` * ``
  are the ones this content hits constantly, because inline code and
  ``**bold**`` markdown both start with them.
* A plain scalar cannot contain ``": "`` -- YAML reads that as a nested
  mapping, so "The catch: it truncates" fails to parse.
* Inside a ``|`` or ``>-`` block, indentation is fixed by the first content
  line. Any later line indented less silently terminates the block, and the
  next line is parsed as YAML -- an error reported far from the cause.

All three produce parser errors that point at the wrong line, so they are
worth catching mechanically instead of one confusing traceback at a time.

Run directly:  ``uv run python scripts/lint_content.py``
"""

import re
import sys
from pathlib import Path

import yaml

# Shared with fix_quoting.py so both agree on what "already quoted" means.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fix_quoting import is_properly_quoted, looks_like_nested_mapping  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "content"

BLOCK_HEADERS = {
    "id", "title", "summary", "prompt", "solution",
    "teach", "body", "description",
    "Dockerfile", "compose", "files", "command", "name",
}
BLOCK_MARKERS = {"|", "|-", ">", ">-", "|+", ">+"}

# Characters that make a *plain* scalar invalid when it appears first. `` ` ``
# and ``*`` are the ones this content actually hits, via inline code and bold.
# ``[`` and ``{`` are deliberately absent: they begin YAML flow collections,
# which parse correctly (``tags: [ctes, group-by]``). Flagging them would be
# the linter second-guessing valid syntax.
RESERVED_FIRST = set("`@%*&!|>},?#")

_KEY_RE = re.compile(r"^([A-Za-z_][\w]*):(?:[ \t]+|$)")
_NESTED_MAPPING_RE = re.compile(r"^[A-Za-z_][\w-]*:(?:[ \t]|$)")


def check_block_indentation(path: Path, issues: list) -> None:
    """Report content lines indented less than their block's first content line."""
    lines = path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        m = _KEY_RE.match(stripped)
        marker = stripped.split(":", 1)[1].strip() if m else ""
        if not (m and m.group(1) in BLOCK_HEADERS and marker in BLOCK_MARKERS):
            i += 1
            continue

        base = len(line) - len(line.lstrip())
        block_indent = None
        j = i + 1
        while j < len(lines):
            cur = lines[j]
            if not cur.strip():          # blank / whitespace-only: harmless
                j += 1
                continue
            indent = len(cur) - len(cur.lstrip())
            if indent <= base:
                # The block ended. If the line that ended it is not the start
                # of a new YAML node, it was almost certainly meant as a
                # continuation that was dedented one step too far.
                text = cur.strip()
                looks_like_node = (
                    text.startswith(("- ", "#", "-")) or _KEY_RE.match(text)
                )
                if not looks_like_node:
                    issues.append(
                        f"line {j + 1}: dedented to {indent} (block base {base}) "
                        f"but is not a new YAML node -- likely a mis-indented "
                        f"continuation of the block above: {text[:60]!r}"
                    )
                break
            if block_indent is None:
                block_indent = indent
            elif indent < block_indent:
                issues.append(
                    f"line {j + 1}: indented {indent}, less than block indent "
                    f"{block_indent} -- terminates the block early: {cur.strip()[:60]!r}"
                )
            j += 1
        i = j


def check_plain_scalars(path: Path, issues: list) -> None:
    """Report list items and inline values that YAML cannot read as scalars.

    Whether `- key: value` is a nested mapping or a string containing a colon
    is decided by asking the parser, not by a regex: a regex misses
    `two words: prose` while catching `OneWord: prose`, which is worse than
    useless. When it *is* a mapping this function stays silent, because a
    mapping here is indistinguishable from a legitimate `- id: f-foo` and
    guessing would corrupt real content.

    Whatever survives that filter and still trips a rule below is a genuine
    error. `check_parses` is the final backstop.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    in_block = False
    block_base = 0

    for n, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip())

        # Track whether we are inside a block scalar, skipping its content.
        if in_block:
            if indent > block_base:
                continue
            in_block = False

        m = _KEY_RE.match(stripped)
        if m and stripped.split(":", 1)[1].strip() in {"|", "|-", ">", ">-", "|+", ">+"}:
            in_block, block_base = True, indent
            continue

        # Candidate plain scalar: either a `- value` list item or `key: value`.
        if stripped.startswith("- "):
            value = stripped[2:].strip()
            label = "list item"
        elif m:
            value = stripped.split(":", 1)[1].strip()
            label = f"key {m.group(1)!r}"
        else:
            continue

        if not value:
            continue
        # A nested mapping is a different kind of node entirely.
        if looks_like_nested_mapping(value):
            continue
        # A flow collection -- a list or a mapping written inline -- is
        # parsed by the YAML rules for flow context, where ": " is an
        # ordinary character. `[{"id": 1, "total": 10.5}]` is a list of one
        # dictionary, not a nested mapping, and it is exactly what the
        # exercise cases are written as.
        if value[0] in "[{":
            continue
        # A block scalar header -- `|`, `>-` and friends -- is a node
        # indicator, not a value. The content follows on the next lines.
        if value in BLOCK_MARKERS:
            continue
        # Only a *complete* quoted scalar is unambiguous. `- '%m' is the
        # month` starts with a quote but is not one, and YAML chokes on it.
        if is_properly_quoted(value):
            continue
        if value[0] in "\"'":
            issues.append(
                f"line {n}: {label} starts with an unterminated quote: {value[:50]!r}"
            )
            continue
        if value[0] in RESERVED_FIRST:
            issues.append(f"line {n}: {label} starts with reserved {value[0]!r}: {value[:60]!r}")
        elif value.count(": ") > 0 or value.endswith(":"):
            # One ": " is enough. YAML reads `title: The catch: it truncates`
            # as a nested mapping and refuses it outright, so the threshold
            # is one, not two.
            issues.append(f"line {n}: {label} contains ': ' inside a plain scalar: {value[:60]!r}")


def check_parses(path: Path, issues: list) -> None:
    try:
        yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"line {mark.line + 1}, col {mark.column + 1}" if mark else "unknown position"
        issues.append(f"YAML PARSE ERROR at {where}: {exc}")


def lint_file(path: Path) -> list[str]:
    issues: list[str] = []
    check_parses(path, issues)
    check_block_indentation(path, issues)
    check_plain_scalars(path, issues)
    return issues


def main() -> int:
    failures = 0
    for path in sorted(CONTENT.rglob("*.yaml")):
        issues = lint_file(path)
        rel = path.relative_to(ROOT)
        if issues:
            failures += len(issues)
            print(f"FAIL {rel}")
            for issue in issues:
                print(f"     {issue}")
        else:
            print(f"ok   {rel}")

    if failures:
        print(f"\n{failures} error(s) found.")
        return 1
    print("\nAll curriculum files pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())