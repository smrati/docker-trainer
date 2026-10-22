"""Re-indent mis-indented YAML block-scalar continuation lines.

Companion to ``lint_content.py``: when the linter reports a continuation line
that is indented less than its block's first content line, this rewrites it to
the correct indent. Folded (``>-``) paragraphs are the usual casualty -- prose
gets hard-wrapped at a different indent from the first line, and YAML then
silently ends the block.

Usage:  uv run python scripts/fix_indentation.py [--check]
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "content"

BLOCK_HEADERS = {
    "id", "title", "summary", "prompt", "solution",
    "teach", "body", "description",
    "Dockerfile", "compose", "files", "command", "name",
}
BLOCK_MARKERS = {"|", "|-", ">", ">-", "|+", ">+"}
_KEY_RE = re.compile(r"^([A-Za-z_][\w]*):(?:[ \t]+|$)")


def looks_like_node(text: str) -> bool:
    return text.startswith(("-", "#")) or bool(_KEY_RE.match(text))


def _label(path: Path) -> str:
    """Repo-relative path where possible; fix_file() works on any path."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _find_blocks(lines: list[str]):
    """Yield (first_content_index, block_indent, base, end) per block scalar.

    A block scalar ends at the first line indented no further than its key --
    unless that line is not a YAML node, in which case it is a dedented
    continuation and the block keeps going. That distinction is the whole job:
    it is what stops this tool from "fixing" a file into nonsense.
    """
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
            if not cur.strip():
                j += 1
                continue
            indent = len(cur) - len(cur.lstrip())
            if indent <= base and looks_like_node(cur.strip()):
                break
            if block_indent is None and indent > base:
                block_indent = indent
            j += 1

        if block_indent is not None:
            yield i + 1, block_indent, base, j
        i = j


def fix_file(path: Path, write: bool) -> int:
    """Re-indent one file, returning the number of lines actually changed."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    changes = 0

    for first, block_indent, base, block_end in _find_blocks(lines):
        for j in range(first, block_end):
            cur = lines[j]
            if not cur.strip():
                continue
            indent = len(cur) - len(cur.lstrip())
            # A line that already sits at or past the block's indent is fine;
            # one that dedented below the block base lost its depth entirely,
            # so assume block level for it.
            if indent >= block_indent and indent > base:
                continue
            target = block_indent if indent <= base else block_indent + (indent - block_indent)
            if target == indent:
                continue
            print(f"  {_label(path)}:{j + 1}: {indent} -> {target}")
            if write:
                lines[j] = " " * target + cur.lstrip()
            changes += 1

    if write and changes:
        path.write_text("".join(lines), encoding="utf-8")
    return changes


def main() -> int:
    write = "--check" not in sys.argv
    total = 0
    for path in sorted(CONTENT.rglob("*.yaml")):
        count = fix_file(path, write=write)
        total += count
        if not count:
            print(f"ok   {path.relative_to(ROOT)}")
    print(f"\n{total} line(s) {'fixed' if write else 'would be fixed'}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())