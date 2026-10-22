"""Auto-quote plain YAML scalars that the parser will reject.

Authoring prose in YAML means writing things like:

    - `COUNT(*)` is the function you want.

...which YAML cannot parse, because a plain scalar cannot begin with a
reserved indicator. The fix is to wrap the value in double quotes and escape
any quotes inside it:

    - "`COUNT(*)` is the function you want."

`lint_content.py` reports the problem; this applies the fix across every
curriculum file so authors do not have to find the offending lines by hand.
It only touches single-line list items and `key: value` pairs -- never block
scalars, never indentation, and never anything already quoted.

Usage:  uv run python scripts/fix_quoting.py [--check]
"""

import argparse
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "content"

# Same set the linter uses: leading characters that break a plain scalar.
RESERVED_FIRST = set("`@%*&!|>},?#")

# Key lines that introduce a block scalar must never be touched.
BLOCK_MARKERS = {"|", "|-", ">", ">-", "|+", ">+"}
BLOCK_HEADERS = {
    "id", "title", "summary", "prompt", "solution",
    "teach", "body", "description", "tags",
    "Dockerfile", "compose", "files", "command", "name",
}
_KEY_RE = re.compile(r"^([A-Za-z_][\w]*):(?:[ \t]+|$)")


def quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# A quoted scalar needs at least an opening and a closing quote.
MIN_QUOTED_LENGTH = 2


def is_properly_quoted(value: str) -> bool:
    """True only if the value is one complete quoted scalar.

    Starting with a quote is not enough. YAML reads `- '%m' is the padded
    month` as the string `%m` followed by trailing junk, and fails -- which
    is why checking only `value[0]` misses exactly the case that matters.
    """
    if len(value) < MIN_QUOTED_LENGTH or value[0] not in "\"'":
        return False
    if value[-1] != value[0]:
        return False
    # A quote preceded by an odd number of backslashes is escaped, so the
    # scalar is not actually closed.
    backslashes = 0
    index = len(value) - 2
    while index >= 0 and value[index] == "\\":
        backslashes += 1
        index -= 1

    closed = backslashes % 2 == 0
    return closed


def needs_quoting(value: str) -> bool:
    """Does this plain scalar need wrapping in quotes to be parseable?"""
    if not value or is_properly_quoted(value):
        return False
    # A leading quote that does not close is not a scalar at all: YAML reads
    # `- '%m' is the month` as the string `%m` plus trailing junk.
    if value[0] in "\"'":
        return True
    if value[0] in RESERVED_FIRST:
        return True
    return ": " in value or value.endswith(":")


def looks_like_nested_mapping(body: str) -> bool:
    """Is `- key: value` really a list item containing a mapping?

    A regex cannot answer this: `- id: f-foo` is a mapping, and
    `- Numerator: count delivered.` is a scalar that happens to contain a
    colon. Guessing wrong either leaves the file unparseable or -- far worse
    -- quotes a real mapping and corrupts it.

    So ask the parser. If the body parses as a mapping, it is one.
    """
    try:
        return isinstance(yaml.safe_load(body), dict)
    except yaml.YAMLError:
        return False


def fix_line(line: str) -> tuple[str, bool]:
    """Return (repaired_line, changed) for one physical line.

    Handles two shapes only: a `- value` list item and a `key: value` pair.
    Block scalars, nested mappings and anything already quoted are left
    untouched -- a repair tool that rewrites what it does not understand will
    quietly corrupt content.
    """
    stripped = line.strip()
    indent = line[: len(line) - len(line.lstrip())]

    if not stripped:
        return line, False

    if stripped.startswith("- "):
        body = stripped[2:].strip()
        if looks_like_nested_mapping(body) or not needs_quoting(body):
            return line, False
        return f"{indent}- {quote(body)}\n", True

    match = _KEY_RE.match(stripped)
    if not match:
        return line, False

    key = match.group(1)
    value = stripped.split(":", 1)[1].strip()
    is_block_header = key in BLOCK_HEADERS and value in BLOCK_MARKERS
    if is_block_header or not needs_quoting(value):
        return line, False

    return f"{indent}{key}: {quote(value)}\n", True


def fix_file(path: Path, write: bool) -> list[int]:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    changed: list[int] = []
    in_block = False
    block_base = 0

    for index, line in enumerate(lines):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        if in_block:
            if not stripped or indent > block_base:
                continue
            in_block = False

        match = _KEY_RE.match(stripped)
        if match and stripped.split(":", 1)[1].strip() in BLOCK_MARKERS:
            in_block, block_base = True, indent
            continue

        repaired, did = fix_line(line if line.endswith("\n") else line + "\n")
        if did:
            if not line.endswith("\n"):
                repaired = repaired.rstrip("\n")
            changed.append(index + 1)
            lines[index] = repaired

    if write and changed:
        path.write_text("".join(lines), encoding="utf-8")
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    total = 0
    for path in sorted(CONTENT.rglob("*.yaml")):
        changed = fix_file(path, write=not args.check)
        total += len(changed)
        if changed:
            verb = "would quote" if args.check else "quoted"
            print(f"{path.relative_to(ROOT)}: {verb} line(s) {changed}")
        else:
            print(f"ok  {path.relative_to(ROOT)}")

    print(f"\n{total} line(s) {'need quoting' if args.check else 'quoted'}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())