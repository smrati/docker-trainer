"""Tests for the tooling in scripts/.

These tools exist because authoring YAML by hand is a minefield, and a minefield
that only explodes for the person who mined it is a minefield that gets
avoided. Each script must catch its own failure mode and must not damage
content that is already correct.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _load(name: str):
    """Import a script by path, so the tests can call its functions."""
    spec = importlib.util.spec_from_file_location(f"scripts_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lint_content = _load("lint_content")
fix_quoting = _load("fix_quoting")
tag_exercises = _load("tag_exercises")


class TestLintContent:
    def test_a_valid_file_passes(self, tmp_path):
        path = tmp_path / "track.yaml"
        path.write_text("id: t\ntitle: T\nlessons:\n  - id: l\n    title: L\n")
        assert lint_content.lint_file(path) == []

    def test_a_backtick_at_the_start_of_a_list_item_is_caught(self, tmp_path):
        path = tmp_path / "track.yaml"
        path.write_text("id: t\nhints:\n  - `FROM` is a thing\n")
        issues = lint_content.lint_file(path)
        assert any("reserved" in issue for issue in issues)

    def test_a_colon_inside_a_plain_scalar_is_caught(self, tmp_path):
        """YAML reads `title: The catch: it truncates` as a nested mapping
        and refuses it, so the file must be reported either way."""
        path = tmp_path / "track.yaml"
        path.write_text("id: t\ntitle: The catch: it truncates\n")
        issues = lint_content.lint_file(path)
        assert issues, "a plain scalar containing ': ' was not reported"
        assert any("line" in issue for issue in issues)

    def test_a_parse_error_is_reported_with_a_line(self, tmp_path):
        path = tmp_path / "track.yaml"
        path.write_text("id: t\n  bad: [\n")
        issues = lint_content.lint_file(path)
        assert any("PARSE ERROR" in issue for issue in issues)
        assert any("line" in issue for issue in issues)

    def test_a_flow_mapping_is_not_flagged(self, tmp_path):
        """`tags: [dockerfile, layers]` is valid YAML and must not be
        second-guessed."""
        path = tmp_path / "track.yaml"
        path.write_text("id: t\ntags: [dockerfile, layers]\n")
        assert lint_content.lint_file(path) == []

    def test_a_quoted_scalar_is_not_flagged(self, tmp_path):
        path = tmp_path / "track.yaml"
        path.write_text("id: t\ntitle: \"`FROM` is a thing\"\n")
        assert lint_content.lint_file(path) == []

    def test_the_shipped_content_passes(self):
        """The content in the repository must lint clean."""
        for path in sorted((ROOT / "content").rglob("*.yaml")):
            assert lint_content.lint_file(path) == [], f"{path} does not lint"


class TestFixQuoting:
    def test_an_offending_line_is_quoted(self, tmp_path):
        path = tmp_path / "track.yaml"
        path.write_text("id: t\nhints:\n  - `FROM` is a thing\n")
        fix_quoting.fix_file(path, True)
        assert '"`FROM` is a thing"' in path.read_text()

    def test_a_valid_file_is_left_alone(self, tmp_path):
        path = tmp_path / "track.yaml"
        original = "id: t\nhints:\n  - a plain hint\n"
        path.write_text(original)
        fix_quoting.fix_file(path, True)
        assert path.read_text() == original

    def test_the_result_lints_clean(self, tmp_path):
        path = tmp_path / "track.yaml"
        path.write_text("id: t\nhints:\n  - `FROM` is a thing\n")
        fix_quoting.fix_file(path, True)
        assert lint_content.lint_file(path) == []

    def test_a_block_scalar_is_not_touched(self, tmp_path):
        """A Dockerfile inside a `|` block must not be rewritten."""
        path = tmp_path / "track.yaml"
        original = (
            "id: t\nfiles:\n  Dockerfile: |\n"
            "    FROM alpine:3.20\n"
            "    CMD [\"echo\", \"hi\"]\n"
        )
        path.write_text(original)
        fix_quoting.fix_file(path, True)
        assert path.read_text() == original


class TestTagExercises:
    def test_the_vocabulary_covers_every_tag_in_use(self):
        """A tag with no label would render as a blank heading, and a label
        nothing uses is dead weight."""
        from app.curriculum.loader import load_curriculum
        from app.routers.review import CONCEPT_LABELS

        curriculum = load_curriculum()
        used = {tag for ex in curriculum.exercises.values() for tag in ex.tags}
        assert used <= set(CONCEPT_LABELS), f"tags with no label: {used - set(CONCEPT_LABELS)}"
        assert not (set(CONCEPT_LABELS) - used), (
            f"labels defined but never used: {set(CONCEPT_LABELS) - used}"
        )

    def test_every_exercise_has_tags(self):
        from app.curriculum.loader import load_curriculum

        curriculum = load_curriculum()
        untagged = [ex.id for ex in curriculum.exercises.values() if not ex.tags]
        assert not untagged, f"exercises with no tags: {untagged}"


class TestPruneScript:
    def test_it_exists_and_exposes_a_main(self):
        prune = _load("prune_images")
        assert callable(prune.main)

    def test_it_only_touches_the_trainers_own_images(self):
        """Your own images must never be at risk."""
        from app.config import IMAGE_TAG_PREFIX
        from app.engine.image import IMAGE_TAG_PREFIX as engine_prefix

        assert IMAGE_TAG_PREFIX == engine_prefix
        assert IMAGE_TAG_PREFIX == "dockertrainer"

    def test_the_prefixes_are_namespaced(self):
        """Every image the trainer builds must be identifiable as such."""
        from app.engine import image as image_engine

        assert callable(image_engine.prune)
        assert callable(image_engine.disk_usage)


@pytest.mark.parametrize(
    "name",
    ["lint_content.py", "fix_quoting.py", "fix_indentation.py", "tag_exercises.py",
     "prune_images.py", "smoke.py", "browser_check.py", "browser_flow.py"],
)
def test_every_script_has_a_docstring(name):
    """A script nobody can read is a script nobody will trust."""
    source = (SCRIPTS / name).read_text()
    assert source.lstrip().startswith('"""'), f"{name} has no module docstring"
