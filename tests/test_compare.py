"""Tests for the comparison rules.

Grading is semantic, and the rules that make it semantic are the whole
difference between "your answer is wrong" and "your answer means something
different". These tests pin each rule down, because a rule that quietly
changes is a rule that quietly starts accepting wrong answers.
"""

import pytest

from app.engine.compare import (
    ComparisonSettings,
    compare_check,
    compare_checks,
    compare_output,
    compare_values,
)
from app.engine.values import describe, json_equal, normalise_lines


class TestValueComparison:
    def test_equal_values_match(self):
        assert compare_values({"a": 1}, {"a": 1}).passed

    def test_different_values_do_not(self):
        result = compare_values({"a": 1}, {"a": 2})
        assert not result.passed
        assert "expected 1, got 2" in result.detail

    def test_dictionary_key_order_is_ignored(self):
        """JSON object order is not a property anyone is being taught."""
        assert compare_values({"a": 1, "b": 2}, {"b": 2, "a": 1}).passed

    def test_true_never_matches_one(self):
        """JSON's `true` is not `1`, and a boolean field must not accept one."""
        result = compare_values(True, 1)
        assert not result.passed
        assert result.reason == "type"

    def test_false_never_matches_zero(self):
        assert not compare_values(False, 0).passed

    def test_null_matches_only_null(self):
        assert compare_values(None, None).passed
        assert not compare_values(None, 0).passed
        assert not compare_values(None, "").passed
        assert not compare_values(None, False).passed

    def test_one_matches_one_point_zero(self):
        """The storage detail is not the skill."""
        assert compare_values(1, 1.0).passed

    def test_floats_compare_with_a_tolerance(self):
        assert compare_values(0.1 + 0.2, 0.3).passed

    def test_a_real_difference_still_fails(self):
        assert not compare_values(1.0, 1.5).passed

    def test_a_tolerance_can_be_widened(self):
        settings = ComparisonSettings(float_tolerance=1.0)
        assert compare_values(10.0, 10.5, settings).passed

    def test_a_relative_tolerance_scales_with_the_value(self):
        """An image's size is a measurement, and two correct builds differ."""
        settings = ComparisonSettings(relative_tolerance=0.05)
        assert compare_values(1_000_000, 1_040_000, settings).passed
        assert not compare_values(1_000_000, 2_000_000, settings).passed

    def test_strings_compare_exactly(self):
        assert compare_values("abc", "abc").passed
        assert not compare_values("abc", "abcd").passed

    def test_list_order_is_sensitive_by_default(self):
        """In a compose file, the order of `ports` decides which is first."""
        assert not compare_values([1, 2], [2, 1]).passed

    def test_list_order_can_be_ignored(self):
        settings = ComparisonSettings(order_sensitive=False)
        assert compare_values([1, 2], [2, 1], settings).passed

    def test_lists_of_different_lengths_fail(self):
        result = compare_values([1, 2], [1, 2, 3])
        assert not result.passed
        assert result.reason == "length"

    def test_a_missing_key_is_reported_by_name(self):
        result = compare_values({"a": 1, "b": 2}, {"a": 1})
        assert not result.passed
        assert result.reason == "missing_key"
        assert "b" in result.detail

    def test_an_extra_key_is_reported_by_name(self):
        result = compare_values({"a": 1}, {"a": 1, "b": 2})
        assert not result.passed
        assert result.reason == "extra_key"

    def test_the_failing_path_is_named(self):
        """A compose configuration that differs in one port must say where."""
        result = compare_values(
            {"services": {"web": {"ports": [{"target": 80}]}}},
            {"services": {"web": {"ports": [{"target": 8080}]}}},
        )
        assert not result.passed
        assert "services.web.ports[0].target" in result.detail

    def test_the_failing_sub_values_are_carried(self):
        """The diff shows the part that differs, not two enormous blobs."""
        result = compare_values(
            {"services": {"web": {"ports": [{"target": 80}]}}},
            {"services": {"web": {"ports": [{"target": 8080}]}}},
        )
        assert result.expected == 80
        assert result.actual == 8080

    def test_ignored_paths_are_skipped(self):
        settings = ComparisonSettings(ignore_paths=["services.web.build"])
        assert compare_values(
            {"services": {"web": {"build": {"context": "a"}}}},
            {"services": {"web": {"build": {"context": "b"}}}},
            settings,
        ).passed

    def test_an_ignored_path_covers_everything_below_it(self):
        settings = ComparisonSettings(ignore_paths=["meta"])
        assert compare_values(
            {"meta": {"created": "1", "author": "a"}, "x": 1},
            {"meta": {"created": "2", "author": "b"}, "x": 1},
            settings,
        ).passed

    def test_an_ignored_path_does_not_hide_its_siblings(self):
        settings = ComparisonSettings(ignore_paths=["meta.created"])
        assert not compare_values(
            {"meta": {"created": "1", "author": "a"}},
            {"meta": {"created": "2", "author": "b"}},
            settings,
        ).passed


class TestOutputComparison:
    def test_exact_mode_requires_an_exact_match(self):
        assert compare_output("a\n", "a\n", "exact").passed
        assert not compare_output("a\n", "a \n", "exact").passed

    def test_lines_mode_ignores_trailing_whitespace(self):
        """A stray trailing space is an accident, not a wrong answer."""
        assert compare_output("a  \n", "a\n", "lines").passed

    def test_lines_mode_ignores_trailing_blank_lines(self):
        assert compare_output("a\n\n\n", "a\n", "lines").passed

    def test_lines_mode_still_catches_an_extra_line(self):
        assert not compare_output("a\nb\n", "a\n", "lines").passed

    def test_json_mode_compares_by_value(self):
        assert compare_output('{"a":1,"b":2}', '{"b":2,"a":1}', "json").passed

    def test_json_mode_rejects_invalid_json(self):
        result = compare_output("not json", "not json", "json")
        assert not result.passed
        assert "not valid JSON" in result.detail


class TestCheckComparison:
    def test_a_run_check_compares_stdout_and_exit_code(self):
        expected = {"stdout": "hello\n", "exit_code": 0}
        actual = {"stdout": "hello\n", "exit_code": 0}
        assert compare_check(0, "greeting", "run", expected, actual).passed

    def test_a_different_exit_code_fails_first(self):
        """The exit code is the more important of the two."""
        result = compare_check(
            0,
            "greeting",
            "run",
            {"stdout": "", "exit_code": 0},
            {"stdout": "", "exit_code": 1},
        )
        assert not result.passed
        assert "exited with 1" in result.detail

    def test_a_value_check_compares_the_value(self):
        result = compare_check(
            0, "ports", "inspect", {"value": {"80/tcp": {}}}, {"value": {"8080/tcp": {}}}
        )
        assert not result.passed
        assert "80/tcp" in result.expected

    def test_a_value_check_is_not_confused_by_the_wrapper(self):
        """The paths in a failure read `services.web`, not `value.services.web`."""
        result = compare_check(
            0,
            "config",
            "config",
            {"value": {"services": {"web": {"image": "a"}}}},
            {"value": {"services": {"web": {"image": "b"}}}},
        )
        assert not result.passed
        assert "value." not in result.detail
        assert "services.web.image" in result.detail


class TestCompareChecks:
    def _expected(self):
        return [
            {"name": "first", "kind": "run", "result": {"stdout": "a\n", "exit_code": 0}},
            {"name": "second", "kind": "run", "result": {"stdout": "b\n", "exit_code": 0}},
        ]

    def test_all_matching_checks_pass(self):
        result = compare_checks(self._expected(), self._expected())
        assert result.passed
        assert result.passed_count == 2
        assert result.total_count == 2

    def test_one_failing_check_fails_the_submission(self):
        actual = self._expected()
        actual[1]["result"]["stdout"] = "different\n"
        result = compare_checks(self._expected(), actual)
        assert not result.passed
        assert result.passed_count == 1
        assert result.total_count == 2

    def test_the_failure_names_the_check(self):
        actual = self._expected()
        actual[1]["result"]["stdout"] = "different\n"
        result = compare_checks(self._expected(), actual)
        assert "second" in result.detail

    def test_a_check_that_did_not_run_fails(self):
        result = compare_checks(self._expected(), self._expected()[:1])
        assert not result.passed
        assert any(check.detail == "this check did not run" for check in result.checks)

    def test_an_empty_comparison_is_not_a_pass(self):
        """Zero checks must never read as success -- and must not crash."""
        result = compare_checks([], [])
        assert not result.passed
        assert result.reason == "no_checks"


class TestSettings:
    def test_a_check_inherits_from_the_exercise(self):
        base = ComparisonSettings(order_sensitive=False, scrub=True)
        child = ComparisonSettings.from_dict({"output_mode": "json"}, base)
        assert child.order_sensitive is False
        assert child.scrub is True
        assert child.output_mode == "json"

    def test_a_check_overrides_the_exercise(self):
        base = ComparisonSettings(order_sensitive=False)
        child = ComparisonSettings.from_dict({"order_sensitive": True}, base)
        assert child.order_sensitive is True

    def test_an_unknown_output_mode_is_refused(self):
        with pytest.raises(ValueError, match="output_mode"):
            ComparisonSettings.from_dict({"output_mode": "yaml"})

    def test_a_non_list_of_strings_is_refused(self):
        with pytest.raises(ValueError, match="ignore_paths"):
            ComparisonSettings.from_dict({"ignore_paths": [1, 2]})

    def test_scrub_replaces_ids_on_both_sides(self):
        """Container ids are random, so they can never match."""
        settings = ComparisonSettings(scrub=True)
        expected = {"stdout": "abc123def456789\n", "exit_code": 0}
        actual = {"stdout": "aaa111bbb222ccc\n", "exit_code": 0}
        assert compare_check(0, "names", "run", expected, actual, settings=settings).passed


class TestValues:
    def test_describe_renders_json_flavoured(self):
        assert describe({"a": 1}) == "{a: 1}"
        assert describe([1, "a"]) == "[1, a]"
        assert describe(None) == "null"
        assert describe(True) == "true"

    def test_json_equal_ignores_object_order(self):
        assert json_equal({"a": 1, "b": 2}, {"b": 2, "a": 1})

    def test_json_equal_does_not_confuse_booleans_with_numbers(self):
        assert not json_equal(True, 1)

    def test_normalise_lines_drops_trailing_blanks(self):
        assert normalise_lines("a\n\n\n") == ["a"]
