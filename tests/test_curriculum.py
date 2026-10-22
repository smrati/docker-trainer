"""Tests for the curriculum itself.

The content is data, so it needs the same guarantees as code: an exercise with
no checks, a reference artefact missing its entry file, or a check kind that
the engine cannot run is a broken app -- not something to discover in front
of a learner who has already typed their answer.

The expensive half of these guarantees -- that every reference actually
builds and passes -- lives in ``scripts/smoke.py``, because it needs a
Docker daemon and takes minutes.
"""

import pytest

from app.curriculum.loader import ContentError, load_curriculum
from app.engine.compare import CHECK_KINDS
from app.routers.review import CONCEPT_LABELS


@pytest.fixture(scope="module")
def curriculum():
    return load_curriculum()


class TestStructure:
    def test_tracks_are_ordered_and_levelled(self, curriculum):
        difficulties = [t.difficulty for t in curriculum.tracks]
        assert difficulties == sorted(
            difficulties, key=["beginner", "intermediate", "advanced"].index
        )
        assert len(curriculum.tracks) == 3

    def test_every_lesson_has_exercises(self, curriculum):
        for track in curriculum.tracks:
            for lesson in track.lessons:
                assert lesson.exercises, f"{track.id}/{lesson.id} has no exercises"

    def test_every_exercise_is_reachable(self, curriculum):
        listed = {
            ex.id
            for track in curriculum.tracks
            for lesson in track.lessons
            for ex in lesson.exercises
        }
        assert listed == set(curriculum.exercises)

    def test_ids_are_unique(self, curriculum):
        ids = [ex.id for ex in curriculum.exercises.values()]
        assert len(ids) == len(set(ids))
        lesson_ids = [lesson.id for lesson in curriculum.lessons.values()]
        assert len(lesson_ids) == len(set(lesson_ids))

    def test_there_are_enough_exercises_to_be_a_course(self, curriculum):
        assert len(curriculum.exercises) >= 60

    def test_every_track_has_a_description(self, curriculum):
        for track in curriculum.tracks:
            assert track.description.strip(), f"{track.id} has no description"


class TestExerciseShape:
    def test_every_exercise_has_a_hint(self, curriculum):
        for ex in curriculum.exercises.values():
            assert ex.hints, f"{ex.id} offers no hints"

    def test_every_exercise_has_tags(self, curriculum):
        """Tags drive the spaced-repetition concept view."""
        for ex in curriculum.exercises.values():
            assert ex.tags, f"{ex.id} has no concept tags"

    def test_every_tag_has_a_display_label(self, curriculum):
        """A tag with no label would render as a blank heading."""
        used = {tag for ex in curriculum.exercises.values() for tag in ex.tags}
        missing = used - set(CONCEPT_LABELS)
        assert not missing, f"tags with no display label: {sorted(missing)}"

        for tag in sorted(CONCEPT_LABELS):
            label, description = CONCEPT_LABELS[tag]
            assert label and description, f"{tag} has an empty label or description"

    def test_no_orphan_labels(self, curriculum):
        """A label nothing uses is dead weight, and usually a typo."""
        used = {tag for ex in curriculum.exercises.values() for tag in ex.tags}
        assert not (set(CONCEPT_LABELS) - used), (
            f"labels defined but never used: {sorted(set(CONCEPT_LABELS) - used)}"
        )

    def test_every_exercise_has_teaching_context(self, curriculum):
        """A prompt with no 'why' teaches nothing beyond the answer."""
        for ex in curriculum.exercises.values():
            assert ex.teach.strip(), f"{ex.id} has no explanation of the concept"

    def test_every_exercise_has_a_prompt(self, curriculum):
        for ex in curriculum.exercises.values():
            assert ex.prompt.strip(), f"{ex.id} has no prompt"

    def test_every_exercise_has_a_starter(self, curriculum):
        """A learner should never face an empty editor."""
        for ex in curriculum.exercises.values():
            assert ex.files, f"{ex.id} has no starter files"
            assert ex.entry_file in ex.files, f"{ex.id}'s starter has no entry file"

    def test_every_exercise_has_a_reference(self, curriculum):
        for ex in curriculum.exercises.values():
            assert ex.solution, f"{ex.id} has no reference solution"

    def test_every_exercise_has_at_least_one_check(self, curriculum):
        """Without a check there is nothing to compare, and nothing to learn
        from the diff."""
        for ex in curriculum.exercises.values():
            assert ex.checks, f"{ex.id} has no checks"

    def test_every_check_has_a_name(self, curriculum):
        """The name is what the learner sees in the diff."""
        for ex in curriculum.exercises.values():
            for check in ex.checks:
                assert check.name.strip(), f"{ex.id} has an unnamed check"

    def test_every_case_is_described(self, curriculum):
        for ex in curriculum.exercises.values():
            for case in ex.cases:
                assert case.name.strip(), f"{ex.id} has an unnamed case"

    def test_the_starter_does_not_contain_the_answer(self, curriculum):
        """A starter that already solves the exercise defeats the exercise.

        The whole file set is compared, not just the entry file: an exercise
        about `.dockerignore` has an identical Dockerfile in its starter and
        its reference, and the answer is in another file.
        """
        for ex in curriculum.exercises.values():
            starter = {name: content.strip() for name, content in ex.files.items()}
            solution = {name: content.strip() for name, content in ex.solution.items()}
            assert starter != solution, (
                f"{ex.id}: the starter is identical to the reference solution"
            )


class TestKinds:
    def test_every_kind_is_one_the_engine_supports(self, curriculum):
        for ex in curriculum.exercises.values():
            assert ex.kind in ("image", "compose", "cli"), f"{ex.id}: {ex.kind}"

    def test_every_check_kind_is_one_the_engine_supports(self, curriculum):
        for ex in curriculum.exercises.values():
            for check in ex.checks:
                assert check.kind in CHECK_KINDS, f"{ex.id}: {check.kind}"

    def test_an_image_exercise_only_uses_image_checks(self, curriculum):
        """A compose check cannot run against an image, and the loader
        refuses the exercise -- so this must not happen."""
        allowed = {"run", "default", "inspect"}
        for ex in curriculum.exercises.values():
            if ex.kind != "image":
                continue
            for check in ex.checks:
                assert check.kind in allowed, f"{ex.id}: {check.kind} on an image"

    def test_a_compose_exercise_only_uses_compose_checks(self, curriculum):
        allowed = {"config", "service"}
        for ex in curriculum.exercises.values():
            if ex.kind != "compose":
                continue
            for check in ex.checks:
                assert check.kind in allowed, f"{ex.id}: {check.kind} on a compose project"

    def test_a_cli_exercise_has_setup_commands(self, curriculum):
        """Without setup there is nothing for the script to look at."""
        for ex in curriculum.exercises.values():
            if ex.kind == "cli":
                assert ex.setup, f"{ex.id} has no setup commands"

    def test_a_cli_exercise_has_teardown_commands(self, curriculum):
        """Without teardown the containers pile up."""
        for ex in curriculum.exercises.values():
            if ex.kind == "cli":
                assert ex.teardown, f"{ex.id} has no teardown commands"

    def test_a_compose_exercise_has_a_compose_file(self, curriculum):
        from app.engine.compose import COMPOSE_FILENAMES

        for ex in curriculum.exercises.values():
            if ex.kind != "compose":
                continue
            names = {name.rsplit("/", 1)[-1] for name in ex.solution}
            assert names & set(COMPOSE_FILENAMES), (
                f"{ex.id}: the reference has no compose file"
            )

    def test_an_image_exercise_has_a_dockerfile(self, curriculum):
        for ex in curriculum.exercises.values():
            if ex.kind != "image":
                continue
            assert any(
                name.rsplit("/", 1)[-1].lower().startswith("dockerfile")
                for name in ex.solution
            ), f"{ex.id}: the reference has no Dockerfile"


class TestSourceRules:
    def test_a_forbidden_string_is_not_in_the_reference(self, curriculum):
        """An exercise that forbids `latest` must not have a reference that
        uses it."""
        offenders = []
        for ex in curriculum.exercises.values():
            haystack = "\n".join(ex.solution.values()).lower()
            for needle in ex.settings.forbid:
                if needle.lower() in haystack:
                    offenders.append(f"{ex.id} uses {needle!r}")
        assert not offenders, f"references break their own rules: {offenders}"

    def test_a_required_string_is_in_the_reference(self, curriculum):
        offenders = []
        for ex in curriculum.exercises.values():
            haystack = "\n".join(ex.solution.values()).lower()
            for needle in ex.settings.require:
                if needle.lower() not in haystack:
                    offenders.append(f"{ex.id} is missing {needle!r}")
        assert not offenders, f"references break their own rules: {offenders}"


class TestValidation:
    """The loader must refuse a broken exercise rather than serve it."""

    def _track(self, tmp_path, body: str):
        (tmp_path / "workspaces").mkdir()
        (tmp_path / "t").mkdir()
        (tmp_path / "t" / "track.yaml").write_text(
            "id: t\ntitle: T\ndifficulty: beginner\nlessons:\n"
            "  - id: l\n    title: L\n    exercises:\n" + body
        )
        return tmp_path

    def test_missing_directory_is_reported(self, tmp_path):
        with pytest.raises(ContentError, match="content directory not found"):
            load_curriculum(tmp_path / "nope")

    def test_a_missing_reference_is_reported(self, tmp_path):
        body = (
            "      - id: e\n        title: E\n        prompt: p\n        kind: image\n"
            "        entry_file: Dockerfile\n        files: {Dockerfile: 'x'}\n"
            "        checks:\n"
            "          - {name: c, kind: run, command: 'true'}\n"
        )
        with pytest.raises(ContentError, match="has no reference solution"):
            load_curriculum(self._track(tmp_path, body))

    def test_missing_required_field_names_the_field(self, tmp_path):
        body = (
            "      - id: e\n        prompt: p\n        kind: image\n"
            "        entry_file: Dockerfile\n        files: {Dockerfile: 'x'}\n"
            "        solution: {Dockerfile: 'x'}\n        checks:\n"
            "          - {name: c, kind: run, command: 'true'}\n"
        )
        with pytest.raises(ContentError, match="missing required field 'title'"):
            load_curriculum(self._track(tmp_path, body))

    def test_an_exercise_without_checks_is_rejected(self, tmp_path):
        body = (
            "      - id: e\n        title: E\n        prompt: p\n        kind: image\n"
            "        entry_file: Dockerfile\n        files: {Dockerfile: 'x'}\n"
            "        solution: {Dockerfile: 'x'}\n"
        )
        with pytest.raises(ContentError, match="has no checks"):
            load_curriculum(self._track(tmp_path, body))

    def test_a_reference_without_the_entry_file_is_rejected(self, tmp_path):
        body = (
            "      - id: e\n        title: E\n        prompt: p\n        kind: image\n"
            "        entry_file: Dockerfile\n        files: {Dockerfile: 'x'}\n"
            "        solution: {other.txt: 'x'}\n        checks:\n"
            "          - {name: c, kind: run, command: 'true'}\n"
        )
        with pytest.raises(ContentError, match="has no 'Dockerfile'"):
            load_curriculum(self._track(tmp_path, body))

    def test_a_starter_without_the_entry_file_is_rejected(self, tmp_path):
        body = (
            "      - id: e\n        title: E\n        prompt: p\n        kind: image\n"
            "        entry_file: Dockerfile\n        files: {other.txt: 'x'}\n"
            "        solution: {Dockerfile: 'x'}\n        checks:\n"
            "          - {name: c, kind: run, command: 'true'}\n"
        )
        with pytest.raises(ContentError, match="the starter has no 'Dockerfile'"):
            load_curriculum(self._track(tmp_path, body))

    def test_an_unknown_check_kind_is_rejected(self, tmp_path):
        body = (
            "      - id: e\n        title: E\n        prompt: p\n        kind: image\n"
            "        entry_file: Dockerfile\n        files: {Dockerfile: 'x'}\n"
            "        solution: {Dockerfile: 'x'}\n        checks:\n"
            "          - {name: c, kind: telepathy}\n"
        )
        with pytest.raises(ContentError, match="'kind' must be one of"):
            load_curriculum(self._track(tmp_path, body))

    def test_an_unknown_exercise_kind_is_rejected(self, tmp_path):
        body = (
            "      - id: e\n        title: E\n        prompt: p\n        kind: kubernetes\n"
            "        entry_file: Dockerfile\n        files: {Dockerfile: 'x'}\n"
            "        solution: {Dockerfile: 'x'}\n        checks:\n"
            "          - {name: c, kind: run, command: 'true'}\n"
        )
        with pytest.raises(ContentError, match="'kind' must be one of"):
            load_curriculum(self._track(tmp_path, body))

    def test_a_duplicate_exercise_id_across_tracks_is_rejected(self, tmp_path):
        (tmp_path / "workspaces").mkdir()

        def lesson(name: str) -> str:
            return (
                f"  - id: l_{name}\n    title: L\n    exercises:\n"
                "      - id: dup\n        title: E\n        prompt: p\n        kind: image\n"
                "        entry_file: Dockerfile\n        files: {Dockerfile: 'x'}\n"
                "        solution: {Dockerfile: 'x'}\n        checks:\n"
                "          - {name: c, kind: run, command: 'true'}\n"
            )

        for name in ("one", "two"):
            (tmp_path / name).mkdir()
            (tmp_path / name / "track.yaml").write_text(
                f"id: {name}\ntitle: T\ndifficulty: beginner\nlessons:\n" + lesson(name)
            )
        with pytest.raises(ContentError, match="duplicate exercise id 'dup'"):
            load_curriculum(tmp_path)

    def test_a_cli_exercise_without_setup_is_rejected(self, tmp_path):
        body = (
            "      - id: e\n        title: E\n        prompt: p\n        kind: cli\n"
            "        entry_file: solve.sh\n        files: {solve.sh: 'x'}\n"
            "        solution: {solve.sh: 'x'}\n"
        )
        with pytest.raises(ContentError, match="needs 'setup' commands"):
            load_curriculum(self._track(tmp_path, body))
