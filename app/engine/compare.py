"""Comparing what a learner's artefact does with what the reference does.

The comparison is **semantic**, never textual. A learner is not told their
Dockerfile is wrong because it is spelled differently from the reference -- they
are told it is wrong because the image it produces *behaves* differently.

Two kinds of check, and each has its own comparison:

* **A run check** (``run``, ``service``, ``script``) executes a command and
  captures stdout and an exit code. The exit code must match exactly. The
  stdout is compared per the check's ``output_mode``:

  - ``lines`` (default) -- trailing whitespace on a line and trailing blank
    lines are ignored, but the content must match exactly.
  - ``exact`` -- byte for byte.
  - ``json`` -- both sides are parsed as JSON and compared by value, so
    ``{"a":1,"b":2}`` and ``{"b":2,"a":1}`` are the same answer.

* **A value check** (``inspect``, ``config``) produces a JSON value, compared
  by the rules in :mod:`app.engine.values`: dictionary key order is ignored,
  ``true`` is not ``1``, ``null`` matches only ``null``, and numbers compare
  numerically with a tolerance.

On failure the diff is the product. Being told "incorrect" without being
shown which check failed is the worst experience this app could deliver, so
every failed grade says exactly what differs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.config import DEFAULT_FLOAT_TOLERANCE, DEFAULT_RELATIVE_TOLERANCE, DIFF_CHECK_LIMIT
from app.engine.script import scrub
from app.engine.values import describe, json_equal, normalise_lines, truncate

# The kinds of check. A check that produced something else would need its own
# comparison rules, and this module refuses to invent one.
#
# ``default`` is the odd one out: it runs the image with *its own* entrypoint
# and command, the way ``docker run <image>`` would, rather than overriding
# them. It is the only way to check what an image actually does when it is
# started with no arguments.
RUN_CHECKS = ("run", "default", "service", "script")
VALUE_CHECKS = ("inspect", "config")
CHECK_KINDS = RUN_CHECKS + VALUE_CHECKS

OUTPUT_MODES = ("exact", "lines", "json")


@dataclass
class ComparisonSettings:
    """Per-check grading rules, and the source-level rules of an exercise.

    Grading a container is not quite grading a query. A check can tell you
    that the image does the wrong thing; it cannot tell you that you wrote
    it the wrong *way*. So two source-level rules exist, and both are opt-in
    per exercise:

    * ``forbid`` -- a substring the submission may not contain, checked
      case-insensitively against every submitted file. ``latest`` is the
      obvious one: an image tagged ``latest`` is not reproducible, and an
      exercise about reproducibility says so.
    * ``require`` -- a substring the submission must contain, for exercises
      where the construct is the point ("declare a ``USER``").

    Neither is inferred: an exercise that does not ask for them is graded on
    behaviour alone.
    """

    order_sensitive: bool = True
    float_tolerance: float | None = None
    relative_tolerance: float | None = None
    ignore_paths: list[str] = field(default_factory=list)
    output_mode: str = "lines"
    scrub: bool = False
    forbid: list[str] = field(default_factory=list)
    require: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(
        cls,
        data: dict | None,
        base: ComparisonSettings | None = None,
    ) -> ComparisonSettings:
        """Build settings from a mapping, optionally on top of ``base``.

        ``base`` is how an exercise's own rules reach its checks: a check
        that only says ``{"output_mode": "json"}`` inherits everything else
        from the exercise rather than resetting it to the defaults.
        """
        data = data or {}
        base = base or cls()
        tolerance = data.get("float_tolerance")
        relative = data.get("relative_tolerance")
        ignore = data.get("ignore_paths")
        mode = data.get("output_mode")
        forbid = data.get("forbid")
        require = data.get("require")
        if ignore is not None and (
            not isinstance(ignore, list) or not all(isinstance(x, str) for x in ignore)
        ):
            raise ValueError("'ignore_paths' must be a list of strings")
        if forbid is not None and (
            not isinstance(forbid, list) or not all(isinstance(x, str) for x in forbid)
        ):
            raise ValueError("'forbid' must be a list of strings")
        if require is not None and (
            not isinstance(require, list) or not all(isinstance(x, str) for x in require)
        ):
            raise ValueError("'require' must be a list of strings")
        if mode is not None and mode not in OUTPUT_MODES:
            raise ValueError(f"'output_mode' must be one of {', '.join(OUTPUT_MODES)}")
        return cls(
            order_sensitive=bool(data.get("order_sensitive", base.order_sensitive)),
            float_tolerance=(
                float(tolerance) if tolerance is not None else base.float_tolerance
            ),
            relative_tolerance=(
                float(relative) if relative is not None else base.relative_tolerance
            ),
            ignore_paths=list(ignore if ignore is not None else base.ignore_paths),
            output_mode=mode or base.output_mode,
            scrub=bool(data.get("scrub", base.scrub)),
            forbid=list(forbid if forbid is not None else base.forbid),
            require=list(require if require is not None else base.require),
        )

    def to_payload(self) -> dict:
        return {
            "order_sensitive": self.order_sensitive,
            "float_tolerance": self.float_tolerance,
            "relative_tolerance": self.relative_tolerance,
            "ignore_paths": list(self.ignore_paths),
            "output_mode": self.output_mode,
            "scrub": self.scrub,
            "forbid": list(self.forbid),
            "require": list(self.require),
        }


@dataclass
class ValueResult:
    """The verdict on one value."""

    passed: bool
    reason: str | None = None
    detail: str | None = None
    path: str = ""
    # The sub-values at the point of failure, so a diff can show the part
    # that differs rather than two enormous blobs.
    expected: object = None
    actual: object = None


@dataclass
class CheckResult:
    """The verdict on one check."""

    index: int
    name: str
    kind: str
    passed: bool
    expected: str = ""
    actual: str = ""
    detail: str | None = None

    def to_payload(self) -> dict:
        return {
            "index": self.index,
            "name": self.name,
            "kind": self.kind,
            "passed": self.passed,
            "expected": self.expected,
            "actual": self.actual,
            "detail": self.detail,
        }


@dataclass
class ComparisonResult:
    """The verdict on a whole submission."""

    passed: bool
    reason: str | None = None
    detail: str | None = None
    checks: list[CheckResult] = field(default_factory=list)
    passed_count: int = 0
    total_count: int = 0

    def to_payload(self) -> dict:
        return {
            "passed": self.passed,
            "reason": self.reason,
            "detail": self.detail,
            "checks": [check.to_payload() for check in self.checks],
            "passed_count": self.passed_count,
            "total_count": self.total_count,
        }


# --------------------------------------------------------------------------
# Value comparison
# --------------------------------------------------------------------------


def _fail(
    reason: str,
    detail: str,
    path: str,
    expected: object = None,
    actual: object = None,
) -> ValueResult:
    # The path is part of the message: "expected 80, got 8080" is much less
    # useful than "services.web.ports: expected 80, got 8080" when the
    # values being compared are a whole compose configuration.
    if path:
        detail = f"{path}: {detail}"
    return ValueResult(
        passed=False,
        reason=reason,
        detail=detail,
        path=path,
        expected=expected,
        actual=actual,
    )


def _is_ignored(path: str, ignored: list[str]) -> bool:
    """Is this path covered by one of the ignore rules?

    A rule matches the path itself and everything below it, so ``Size``
    ignores ``Size`` and ``Size.Inner``.
    """
    for rule in ignored:
        if path == rule or path.startswith((rule + ".", rule + "[")):
            return True
    return False


def _numbers_equal(a: float, b: float, settings: ComparisonSettings) -> bool:
    absolute = (
        settings.float_tolerance
        if settings.float_tolerance is not None
        else DEFAULT_FLOAT_TOLERANCE
    )
    relative = (
        settings.relative_tolerance
        if settings.relative_tolerance is not None
        else DEFAULT_RELATIVE_TOLERANCE
    )
    if a == b:
        return True
    difference = abs(a - b)
    if difference <= absolute:
        return True
    # A measurement is allowed to differ by a share of its own magnitude.
    scale = max(abs(a), abs(b))
    return scale > 0 and difference <= relative * scale


def compare_values(
    expected,
    actual,
    settings: ComparisonSettings | None = None,
    path: str = "",
) -> ValueResult:
    """Compare two JSON values."""
    settings = settings or ComparisonSettings()

    if _is_ignored(path, settings.ignore_paths):
        return ValueResult(passed=True)

    # --- null -------------------------------------------------------------
    if expected is None or actual is None:
        if expected is None and actual is None:
            return ValueResult(passed=True)
        return _fail(
            "type",
            f"expected {describe(expected)}, got {describe(actual)}",
            path,
            expected,
            actual,
        )

    # --- booleans ---------------------------------------------------------
    # Checked before numbers because JSON's `true` is not `1`, and a check
    # that read `privileged: true` must never accept `privileged: 1`.
    if isinstance(expected, bool) or isinstance(actual, bool):
        if expected is actual:
            return ValueResult(passed=True)
        return _fail(
            "type",
            f"expected {describe(expected)}, got {describe(actual)}",
            path,
            expected,
            actual,
        )

    # --- numbers ----------------------------------------------------------
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        if _numbers_equal(float(expected), float(actual), settings):
            return ValueResult(passed=True)
        return _fail(
            "value",
            f"expected {describe(expected)}, got {describe(actual)}",
            path,
            expected,
            actual,
        )

    # --- strings ----------------------------------------------------------
    if isinstance(expected, str) and isinstance(actual, str):
        if expected == actual:
            return ValueResult(passed=True)
        return _fail(
            "value",
            f"expected {describe(expected)}, got {describe(actual)}",
            path,
            expected,
            actual,
        )

    # --- lists ------------------------------------------------------------
    if isinstance(expected, list) and isinstance(actual, list):
        if settings.order_sensitive:
            if len(expected) != len(actual):
                return _fail(
                    "length",
                    f"expected {len(expected)} item(s), got {len(actual)}",
                    path,
                    expected,
                    actual,
                )
            for index, (exp, act) in enumerate(zip(expected, actual, strict=True)):
                child_path = f"{path}[{index}]"
                result = compare_values(exp, act, settings, child_path)
                if not result.passed:
                    return result
            return ValueResult(passed=True)

        # Order-insensitive: compare as multisets, keyed by canonical form so
        # the comparison is stable.
        if len(expected) != len(actual):
            return _fail(
                "length",
                f"expected {len(expected)} item(s), got {len(actual)}",
                path,
                expected,
                actual,
            )
        remaining = list(actual)
        for item in expected:
            key = json.dumps(item, sort_keys=True, default=str)
            for index, candidate in enumerate(remaining):
                if json.dumps(candidate, sort_keys=True, default=str) == key:
                    remaining.pop(index)
                    break
            else:
                return _fail("value", f"missing {describe(item)}", path, item, None)
        return ValueResult(passed=True)

    # --- dictionaries -----------------------------------------------------
    if isinstance(expected, dict) and isinstance(actual, dict):
        expected_keys = {
            key
            for key in expected
            if not _is_ignored(f"{path}.{key}", settings.ignore_paths)
        }
        actual_keys = {
            key
            for key in actual
            if not _is_ignored(f"{path}.{key}", settings.ignore_paths)
        }

        missing = sorted(expected_keys - actual_keys)
        if missing:
            return _fail(
                "missing_key",
                f"missing key(s): {', '.join(missing)}",
                path,
                expected,
                actual,
            )

        extra = sorted(actual_keys - expected_keys)
        if extra:
            return _fail(
                "extra_key",
                f"unexpected key(s): {', '.join(extra)}",
                path,
                expected,
                actual,
            )

        for key in expected_keys:
            child_path = f"{path}.{key}" if path else str(key)
            result = compare_values(expected[key], actual[key], settings, child_path)
            if not result.passed:
                return result
        return ValueResult(passed=True)

    # --- a type mismatch --------------------------------------------------
    return _fail(
        "type",
        f"expected {describe(expected)}, got {describe(actual)}",
        path,
        expected,
        actual,
    )


# --------------------------------------------------------------------------
# Output comparison
# --------------------------------------------------------------------------


def compare_output(expected: str, actual: str, mode: str = "lines") -> ValueResult:
    """Compare two captured stdout streams."""
    if mode == "exact":
        if expected == actual:
            return ValueResult(passed=True)
        return _fail("value", "the output does not match", "")

    if mode == "json":
        try:
            left = json.loads(expected)
            right = json.loads(actual)
        except (TypeError, ValueError):
            return _fail("value", "the output is not valid JSON", "")
        if json_equal(left, right):
            return ValueResult(passed=True)
        return _fail(
            "value",
            f"expected {truncate(expected.strip(), 400)}, "
            f"got {truncate(actual.strip(), 400)}",
            "",
        )

    if normalise_lines(expected) == normalise_lines(actual):
        return ValueResult(passed=True)
    return _fail("value", "the output does not match", "")


# --------------------------------------------------------------------------
# Check comparison
# --------------------------------------------------------------------------


def compare_check(
    index: int,
    name: str,
    kind: str,
    expected,
    actual,
    *,
    settings: ComparisonSettings | None = None,
) -> CheckResult:
    """Compare one check's reference behaviour with the learner's."""
    settings = settings or ComparisonSettings()

    if kind in RUN_CHECKS:
        exp_out = (expected or {}).get("stdout", "") or ""
        act_out = (actual or {}).get("stdout", "") or ""
        exp_code = (expected or {}).get("exit_code")
        act_code = (actual or {}).get("exit_code")

        if settings.scrub:
            # Container ids and timestamps are random. Replacing them on both
            # sides keeps a learner from being marked down for something
            # that was never going to match.
            exp_out = scrub(exp_out)
            act_out = scrub(act_out)

        if exp_code != act_code:
            return CheckResult(
                index=index,
                name=name,
                kind=kind,
                passed=False,
                expected=f"exit {exp_code}",
                actual=f"exit {act_code}",
                detail=(
                    f"the command exited with {act_code}, expected {exp_code}"
                    if exp_code is not None and act_code is not None
                    else "the command's exit code differed"
                ),
            )

        result = compare_output(exp_out, act_out, settings.output_mode)
        return CheckResult(
            index=index,
            name=name,
            kind=kind,
            passed=result.passed,
            expected=truncate(exp_out.strip()),
            actual=truncate(act_out.strip()),
            detail=result.detail,
        )

    # A value check carries its value under "value", so the comparison
    # unwraps it: the paths in a failure then read "services.web.ports"
    # rather than "value.services.web.ports", which is what an exercise
    # author writes an `ignore_paths` rule against.
    exp_value = (expected or {}).get("value") if isinstance(expected, dict) else expected
    act_value = (actual or {}).get("value") if isinstance(actual, dict) else actual

    result = compare_values(exp_value, act_value, settings)
    # The failing sub-values, not the whole blobs: a compose configuration
    # that differs in one port should show that port, not four hundred
    # lines of JSON.
    return CheckResult(
        index=index,
        name=name,
        kind=kind,
        passed=result.passed,
        expected=truncate(describe(result.expected)),
        actual=truncate(describe(result.actual)),
        detail=result.detail,
    )


def _settings_from(raw, default: ComparisonSettings | None) -> ComparisonSettings:
    """The settings for one check.

    A check's own settings win over the exercise's, because a check knows
    more about itself than the exercise does: a check that compares an
    image's size wants a tolerance, and a check that compares a port list
    does not.
    """
    if isinstance(raw, ComparisonSettings):
        return raw
    if isinstance(raw, dict):
        try:
            return ComparisonSettings.from_dict(raw)
        except ValueError:
            return default or ComparisonSettings()
    return default or ComparisonSettings()


def compare_checks(
    expected_checks: list[dict],
    actual_checks: list[dict],
    default_settings: ComparisonSettings | None = None,
) -> ComparisonResult:
    """Compare every check and summarise.

    ``expected_checks`` and ``actual_checks`` are lists of
    ``{"name", "kind", "result"}``. The reference's names define the order,
    so a learner cannot reorder the checks to make a diff read better.
    """
    results: list[CheckResult] = []

    for index, expected in enumerate(expected_checks):
        name = expected.get("name") or f"check {index + 1}"
        kind = expected.get("kind", "run")
        settings = _settings_from(expected.get("settings"), default_settings)
        actual = actual_checks[index] if index < len(actual_checks) else None

        if actual is None:
            results.append(
                CheckResult(
                    index=index,
                    name=name,
                    kind=kind,
                    passed=False,
                    expected=truncate(describe(expected.get("result"))),
                    actual="—",
                    detail="this check did not run",
                )
            )
            continue

        results.append(
            compare_check(
                index,
                name,
                kind,
                expected.get("result"),
                actual.get("result"),
                settings=settings,
            )
        )

    passed_count = sum(1 for check in results if check.passed)
    total = len(results)

    if total == 0:
        # No checks means nothing was verified, which is not a pass. An
        # exercise with no checks is an authoring bug, and saying so is
        # better than reporting success.
        return ComparisonResult(
            passed=False,
            reason="no_checks",
            detail="This exercise has no checks, so there is nothing to compare.",
            checks=results,
            passed_count=0,
            total_count=0,
        )

    if passed_count == total:
        return ComparisonResult(
            passed=True,
            reason=None,
            detail=None,
            checks=results,
            passed_count=passed_count,
            total_count=total,
        )

    failures = [check for check in results if not check.passed]
    first = failures[0]
    summary = (
        f"{passed_count} of {total} check{'s' if total != 1 else ''} passed. "
        f"“{first.name}”: {first.detail or 'the result differs'}."
    )
    if len(failures) > DIFF_CHECK_LIMIT:
        summary += (
            f" ({len(failures)} checks differ, so the shape is off rather than "
            "a near miss.)"
        )

    return ComparisonResult(
        passed=False,
        reason="checks",
        detail=summary,
        checks=results,
        passed_count=passed_count,
        total_count=total,
    )
