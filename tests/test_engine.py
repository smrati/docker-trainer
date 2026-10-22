"""Tests for the engine.

The engine is the part of this app that touches the Docker daemon and the
filesystem, so its rules are the ones that matter most: a path that escapes
the build context, a script that reaches for the host's privileges, a
configuration that carries one machine's paths into another's comparison.

Nothing here needs a daemon. The daemon-facing code is covered by
``scripts/smoke.py`` and by the slow API tests.
"""

from pathlib import Path

import pytest

from app.engine import image as image_engine
from app.engine.compare import ComparisonSettings
from app.engine.compose import find_compose_file_on_disk, normalise
from app.engine.context import InvalidSubmission, materialise, validate_path
from app.engine.script import check_script, scrub
from app.engine.values import canonical, describe, json_equal


class TestPathValidation:
    def test_a_plain_name_is_fine(self):
        assert validate_path("Dockerfile") == Path("Dockerfile")

    def test_a_nested_name_is_fine(self):
        assert validate_path("app/server.py") == Path("app/server.py")

    def test_an_absolute_path_is_refused(self):
        """A build context is copied into the daemon; an absolute path is a
        file the *host* has, not one the learner wrote."""
        with pytest.raises(InvalidSubmission, match="absolute path"):
            validate_path("/etc/passwd")

    def test_a_parent_path_is_refused(self):
        with pytest.raises(InvalidSubmission, match="outside the build context"):
            validate_path("../secrets")

    def test_a_parent_path_in_the_middle_is_refused(self):
        with pytest.raises(InvalidSubmission, match="outside the build context"):
            validate_path("app/../../secrets")

    def test_a_backslash_is_refused(self):
        """Paths in a build context always use forward slashes."""
        with pytest.raises(InvalidSubmission, match="backslash"):
            validate_path("app\\server.py")

    def test_an_empty_name_is_refused(self):
        with pytest.raises(InvalidSubmission):
            validate_path("")

    def test_a_windows_drive_letter_is_refused(self):
        with pytest.raises(InvalidSubmission, match="absolute path"):
            validate_path("C:/windows")

    def test_a_current_directory_is_stripped(self):
        assert validate_path("./Dockerfile") == Path("Dockerfile")


class TestMaterialise:
    def test_files_are_written(self, tmp_path):
        written = materialise({"Dockerfile": "FROM alpine\n"}, tmp_path)
        assert written == ["Dockerfile"]
        assert (tmp_path / "Dockerfile").read_text() == "FROM alpine\n"

    def test_nested_files_are_written(self, tmp_path):
        materialise({"app/server.py": "print(1)\n"}, tmp_path)
        assert (tmp_path / "app" / "server.py").read_text() == "print(1)\n"

    def test_a_refused_path_writes_nothing(self, tmp_path):
        with pytest.raises(InvalidSubmission):
            materialise({"../escape": "x"}, tmp_path)
        assert not (tmp_path.parent / "escape").exists()

    def test_too_many_files_are_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.engine.context.MAX_SUBMISSION_FILES", 3)
        with pytest.raises(InvalidSubmission, match="at most 3 files"):
            materialise({f"f{i}": "" for i in range(4)}, tmp_path)

    def test_an_oversized_file_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.engine.context.MAX_FILE_BYTES", 10)
        with pytest.raises(InvalidSubmission, match="larger than"):
            materialise({"big": "x" * 100}, tmp_path)

    def test_empty_content_is_allowed(self, tmp_path):
        materialise({"Dockerfile": ""}, tmp_path)
        assert (tmp_path / "Dockerfile").read_text() == ""


class TestComposeNormalisation:
    def test_the_project_directory_is_stripped(self, tmp_path):
        """Two identical projects in two different directories must compare
        equal, so the directory itself cannot be part of the answer."""
        config = {"services": {"web": {"build": {"context": str(tmp_path.resolve())}}}}
        result = normalise(config, tmp_path)
        assert result["services"]["web"]["build"]["context"] == "."

    def test_a_path_below_the_project_is_made_relative(self, tmp_path):
        root = tmp_path.resolve()
        config = {"services": {"web": {"build": {"context": str(root / "app")}}}}
        result = normalise(config, tmp_path)
        assert result["services"]["web"]["build"]["context"] == "app"

    def test_other_paths_are_left_alone(self, tmp_path):
        config = {"services": {"web": {"image": "nginx:alpine"}}}
        result = normalise(config, tmp_path)
        assert result["services"]["web"]["image"] == "nginx:alpine"

    def test_nested_structures_are_walked(self, tmp_path):
        root = tmp_path.resolve()
        config = {"services": {"web": {"volumes": [{"source": str(root / "data")}]}}}
        result = normalise(config, tmp_path)
        assert result["services"]["web"]["volumes"][0]["source"] == "data"


class TestComposeFileDiscovery:
    def test_the_canonical_name_is_found(self, tmp_path):
        (tmp_path / "compose.yaml").write_text("services: {}\n")
        assert find_compose_file_on_disk(tmp_path) == "compose.yaml"

    def test_the_legacy_name_is_found(self, tmp_path):
        (tmp_path / "docker-compose.yml").write_text("services: {}\n")
        assert find_compose_file_on_disk(tmp_path) == "docker-compose.yml"

    def test_a_nested_compose_file_is_found(self, tmp_path):
        nested = tmp_path / "deploy"
        nested.mkdir()
        (nested / "compose.yaml").write_text("services: {}\n")
        assert find_compose_file_on_disk(tmp_path) == "deploy/compose.yaml"

    def test_nothing_is_found_in_an_empty_project(self, tmp_path):
        assert find_compose_file_on_disk(tmp_path) is None


class TestScriptGuards:
    def test_privileged_is_refused(self):
        with pytest.raises(Exception, match="privileged"):
            check_script("docker run --privileged alpine:3.20")

    def test_host_networking_is_refused(self):
        with pytest.raises(Exception, match="--net=host"):
            check_script("docker run --net=host alpine:3.20")

    def test_host_pid_namespace_is_refused(self):
        with pytest.raises(Exception, match="--pid=host"):
            check_script("docker run --pid=host alpine:3.20")

    def test_capabilities_are_refused(self):
        with pytest.raises(Exception, match="--cap-add"):
            check_script("docker run --cap-add=SYS_ADMIN alpine:3.20")

    def test_an_ordinary_script_is_allowed(self):
        check_script("docker ps --format '{{.Names}}'")


class TestScrub:
    def test_a_container_id_is_replaced(self):
        assert scrub("abc123def456789\n") == "<id>\n"

    def test_a_longer_digest_is_replaced(self):
        digest = "a" * 64
        assert scrub(digest) == "<id>"

    def test_a_timestamp_is_replaced(self):
        assert scrub("2024-01-02T03:04:05Z") == "<timestamp>"

    def test_ordinary_text_is_left_alone(self):
        assert scrub("hello world\n") == "hello world\n"

    def test_a_short_word_that_looks_hex_is_left_alone(self):
        """`face` is a word, not an id."""
        assert scrub("face\n") == "face\n"


class TestImageHelpers:
    def test_a_reference_tag_has_no_slash(self):
        """A Docker tag may not contain a slash; a repository name may."""
        tag = image_engine.reference_tag("f-example", "default")
        assert "/" not in tag
        assert tag.startswith("dockertrainer-ref-")

    def test_a_learner_tag_has_no_slash(self):
        assert "/" not in image_engine.learner_tag("f-example", "abc123")

    def test_the_fingerprint_changes_with_the_files(self):
        one = image_engine.fingerprint({"Dockerfile": "FROM alpine\n"})
        two = image_engine.fingerprint({"Dockerfile": "FROM debian\n"})
        assert one != two

    def test_the_fingerprint_is_stable(self):
        files = {"Dockerfile": "FROM alpine\n"}
        assert image_engine.fingerprint(files) == image_engine.fingerprint(files)

    def test_the_fingerprint_lands_in_the_tag(self):
        """Otherwise a cached image from the previous reference would be
        reused and the new reference would never be built."""
        files = {"Dockerfile": "FROM alpine\n"}
        tag = image_engine.reference_tag("f-example", "default", image_engine.fingerprint(files))
        assert image_engine.fingerprint(files)[:8] in tag

    def test_a_build_failure_is_summarised(self):
        """Docker's output is long; the three useful lines are extracted."""
        log = """#5 ERROR: process "/bin/sh -c nope" did not complete successfully: exit code: 127
------
 > [2/2] RUN nope:
0.580 /bin/sh: nope: not found
------
Dockerfile:2
--------------------
   1 |     FROM alpine:3.20
   2 | >>> RUN nope
--------------------
ERROR: failed to build: failed to solve: process did not complete
"""
        instruction, location, error = image_engine._extract_failure(log)
        assert instruction == "RUN nope"
        assert location == "Dockerfile line 2"
        assert error is not None
        assert "not found" in error

    def test_a_size_is_parsed(self):
        assert image_engine._parse_size("82.69MB") == int(82.69 * 1024 * 1024)
        assert image_engine._parse_size("0B") == 0
        assert image_engine._parse_size(None) == 0
        assert image_engine._parse_size("nonsense") == 0


class TestValues:
    def test_canonical_is_stable_across_key_order(self):
        assert canonical({"a": 1, "b": 2}) == canonical({"b": 2, "a": 1})

    def test_describe_truncates_a_long_string(self):
        text = describe("x" * 10_000)
        assert "more characters" in text

    def test_json_equal_compares_nested_structures(self):
        assert json_equal({"a": [{"b": 1}]}, {"a": [{"b": 1}]})

    def test_json_equal_notices_a_nested_difference(self):
        assert not json_equal({"a": [{"b": 1}]}, {"a": [{"b": 2}]})


class TestSettingsInheritance:
    def test_a_check_inherits_the_exercise_rules(self):
        base = ComparisonSettings(order_sensitive=False, output_mode="json")
        child = ComparisonSettings.from_dict({}, base)
        assert child.output_mode == "json"
        assert child.order_sensitive is False
