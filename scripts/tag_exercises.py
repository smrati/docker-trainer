"""Check the concept tags on every exercise in the curriculum.

Tags drive the concept-level view in spaced repetition, so they come from
one controlled vocabulary rather than being invented per exercise. The
vocabulary lives in ``app/routers/review.py`` next to the labels the review
page shows, so a tag and its label can never drift apart.

    uv run python scripts/tag_exercises.py          # check
    uv run python scripts/tag_exercises.py --list   # show the vocabulary

The script is a *check*, not a writer. The content is reviewed and diffed
like code, and a tool that silently rewrites it would be a tool nobody could
trust. If an exercise has no tags, this says so and names it; adding them is
a one-line edit in the content file.
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.curriculum.loader import load_curriculum  # noqa: E402
from app.routers.review import CONCEPT_LABELS  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list",
        action="store_true",
        help="list the concept vocabulary and how many exercises use each",
    )
    args = parser.parse_args()

    if args.list:
        for tag, (label, description) in sorted(CONCEPT_LABELS.items()):
            print(f"{tag:16} {label} -- {description}")
        return 0

    curriculum = load_curriculum()

    problems: list[str] = []
    untagged: list[str] = []
    used: Counter = Counter()

    for exercise_id, exercise in sorted(curriculum.exercises.items()):
        if not exercise.tags:
            untagged.append(exercise_id)
            continue
        for tag in exercise.tags:
            if tag not in CONCEPT_LABELS:
                problems.append(f"{exercise_id}: unknown tag '{tag}'")
            used[tag] += 1

    # A tag nothing uses is dead weight, and usually a typo.
    for tag in sorted(set(CONCEPT_LABELS) - set(used)):
        problems.append(f"concept '{tag}' has a label but no exercise uses it")

    # A label with no description would render as a blank card.
    for tag, (label, description) in sorted(CONCEPT_LABELS.items()):
        if not label or not description:
            problems.append(f"concept '{tag}' has an empty label or description")

    if untagged:
        problems.append(
            f"{len(untagged)} exercise(s) have no concept tags: {', '.join(untagged)}"
        )

    if problems:
        print("ERROR: the curriculum's tags do not match the vocabulary:")
        for problem in problems:
            print(f"  {problem}")
        return 1

    print(
        f"{len(curriculum.exercises)} exercises checked, "
        f"{len(used)} distinct concepts in use"
    )
    for tag, count in used.most_common():
        print(f"  {tag:16} {count:3} exercise(s)  {CONCEPT_LABELS[tag][0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
