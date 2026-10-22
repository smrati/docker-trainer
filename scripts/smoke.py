"""A smoke test of the whole engine: build, run, compare.

Run directly:  ``uv run python scripts/smoke.py [exercise-id]``
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.curriculum.loader import load_curriculum  # noqa: E402
from app.engine.runner import MODE_SUBMIT, evaluate  # noqa: E402


def main() -> int:
    wanted = sys.argv[1] if len(sys.argv) > 1 else None
    curriculum = load_curriculum()

    exercises = [
        ex
        for ex in curriculum.exercises.values()
        if wanted is None or wanted in ex.id
    ]
    if not exercises:
        print(f"No exercise matching {wanted!r}")
        return 1

    failures = 0
    for exercise in exercises:
        print(f"--- {exercise.id}: {exercise.title}")

        # The reference must pass against itself.
        outcome = evaluate(curriculum, exercise, exercise.solution, mode=MODE_SUBMIT)
        if not outcome.ok:
            print(f"    reference failed to build: {outcome.error}")
            print(outcome.build_log[-2000:])
            failures += 1
            continue
        if not outcome.comparison or not outcome.comparison.passed:
            print("    the reference did not match itself:")
            for check in (outcome.comparison.checks if outcome.comparison else []):
                if not check.passed:
                    print(f"      {check.name}: {check.detail}")
                    print(f"        expected: {check.expected[:400]}")
                    print(f"        actual:   {check.actual[:400]}")
            failures += 1
            continue

        print(f"    reference matches itself ({outcome.duration_ms:.0f} ms)")

        # An empty submission must not pass.
        empty = evaluate(
            curriculum,
            exercise,
            {name: "" for name in exercise.files},
            mode=MODE_SUBMIT,
        )
        if empty.ok and empty.comparison and empty.comparison.passed:
            print("    an empty submission passed -- the checks are too weak")
            failures += 1
        else:
            print("    an empty submission fails, as it should")

    print()
    if failures:
        print(f"{failures} problem(s) found.")
        return 1
    print(f"All {len(exercises)} exercise(s) behaved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
