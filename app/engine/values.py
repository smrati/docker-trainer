"""Comparable values: the JSON that crosses the boundary between the engine
and the grader.

Grading compares a learner's artefact with the reference artefact's, and the
two are produced by two different Docker invocations. Something has to cross
that boundary, and it has to be comparable *semantically*.

The rules, and why each exists:

* **Dictionary key order is ignored.** ``{"a": 1, "b": 2}`` and
  ``{"b": 2, "a": 1}`` are the same configuration. JSON object order is not a
  property anyone is being taught.
* **``true`` never matches ``1``.** JSON has two distinct boolean and number
  types, and conflating them would let ``"privileged": true`` match
  ``"privileged": 1``.
* **``null`` matches only ``null``.** The same rule the Python trainer uses
  for ``None``.
* **Numbers compare numerically**, with a tolerance. An image's size is a
  measurement, not an answer, and two correct Dockerfiles can differ by a few
  bytes.
* **List order is sensitive by default.** In a compose file, the order of
  ``ports`` decides which one is published first. A check that genuinely does
  not care sets ``order_sensitive: false``.

Everything here is plain JSON: no envelope format is needed, because JSON
already distinguishes ``null``, ``true``, ``1`` and ``1.0``.
"""

from __future__ import annotations

import json

# The longest rendering of a value shown in a diff before it is cut short.
# A diff that dumps a thousand lines is not a diff anyone reads.
MAX_DESCRIBE_CHARS = 4000


def canonical(value) -> str:
    """A stable string form of a value.

    Used for equality of dict *keys* and for anything that needs a hashable
    identity. Sorted keys, so two equal values always produce the same
    string.
    """
    return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)


def describe(value, *, limit: int = MAX_DESCRIBE_CHARS) -> str:
    """A human-readable rendering, for diffs and error messages.

    This is what a learner reads in "expected 'v2.0', got 'v1.0'" -- so it
    aims at something a Docker reader recognises, not at Python repr.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        if len(value) > limit:
            return value[:limit] + f"... ({len(value) - limit} more characters)"
        return value
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    if isinstance(value, list):
        return "[" + ", ".join(describe(item, limit=limit) for item in value) + "]"
    if isinstance(value, dict):
        inner = ", ".join(
            f"{describe(key, limit=limit)}: {describe(item, limit=limit)}"
            for key, item in value.items()
        )
        return "{" + inner + "}"
    text = str(value)
    if len(text) > limit:
        return text[:limit] + f"... ({len(text) - limit} more characters)"
    return text


def truncate(text: str, limit: int = MAX_DESCRIBE_CHARS) -> str:
    """Cut a long string short, saying so rather than pretending it is whole."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... ({len(text) - limit} more characters)"


def normalise_lines(text: str) -> list[str]:
    """Split into lines and ignore trailing whitespace and trailing blanks.

    Forgiving about the accidents a learner makes (a stray newline, a trailing
    space) without forgiving a wrong answer (an extra line).
    """
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def json_equal(expected, actual) -> bool:
    """Compare two JSON documents by value.

    ``1`` and ``1.0`` are equal here, because JSON does not promise that a
    number has a particular representation and a container's JSON output
    should not be graded on it.
    """
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected is actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return float(expected) == float(actual)
    if type(expected) is not type(actual):
        return False
    if isinstance(expected, list):
        return len(expected) == len(actual) and all(
            json_equal(a, b) for a, b in zip(expected, actual, strict=True)
        )
    if isinstance(expected, dict):
        return expected.keys() == actual.keys() and all(
            json_equal(expected[key], actual[key]) for key in expected
        )
    return expected == actual
