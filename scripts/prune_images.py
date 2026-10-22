"""Remove the images this app built.

The trainer builds a reference image per exercise and keeps it, because
rebuilding it on every submission would double the wait for no benefit. Over
time that adds up: a full curriculum is a few gigabytes of base images and
build layers.

    uv run python scripts/prune_images.py            # remove the trainer's images
    uv run python scripts/prune_images.py --dry-run  # just list them
    uv run python scripts/prune_images.py --all      # also remove dangling layers

Only images under the app's own prefixes are touched, so your own images are
never at risk.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.engine import image as image_engine  # noqa: E402
from app.engine.cli import docker  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list the images that would be removed, and remove nothing",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="also remove dangling build layers and stopped containers",
    )
    args = parser.parse_args()

    usage = image_engine.disk_usage()
    print(
        f"Before: {usage.get('images', 0)} image(s), "
        f"{usage.get('image_bytes', 0) / (1024 * 1024):.1f} MB of images, "
        f"{usage.get('build_cache_bytes', 0) / (1024 * 1024):.1f} MB of build cache"
    )

    removed = image_engine.prune(dry_run=args.dry_run)
    if args.dry_run:
        print(f"{len(removed)} image(s) would be removed:")
        for tag in removed:
            print(f"  {tag}")
    else:
        print(f"Removed {len(removed)} image(s).")

    if args.all:
        for argv in (
            ["image", "prune", "--force"],
            ["container", "prune", "--force"],
            ["network", "prune", "--force"],
            ["builder", "prune", "--force"],
        ):
            result = docker(argv, timeout=120.0)
            print(f"  {' '.join(argv)}: {'ok' if result.ok else 'failed'}")

    usage = image_engine.disk_usage()
    print(
        f"After:  {usage.get('images', 0)} image(s), "
        f"{usage.get('image_bytes', 0) / (1024 * 1024):.1f} MB of images, "
        f"{usage.get('build_cache_bytes', 0) / (1024 * 1024):.1f} MB of build cache"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
