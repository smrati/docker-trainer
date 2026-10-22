"""Application settings.

The limits below are the engine's ceilings:

* ``BUILD_TIMEOUT_SECONDS``  -- wall clock for one image build.
* ``RUN_TIMEOUT_SECONDS``    -- wall clock for one container run.
* ``SCRIPT_TIMEOUT_SECONDS`` -- wall clock for one shell script (CLI exercises).
* ``CONTAINER_MEMORY_BYTES`` -- address space a container may use.
* ``CONTAINER_CPUS``         -- CPU quota for a container.
* ``OUTPUT_LIMIT_BYTES``     -- how much stdout/stderr may reach the browser.

The wall clock is the *primary* guard, for the same reason the Python trainer
made time rather than a step budget the primary guard: time bounds work, and
"it stopped after 5 seconds" is a message a learner can act on.
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONTENT_DIR = BASE_DIR / "content"
STATIC_DIR = Path(__file__).resolve().parent / "static"
DATABASE_PATH = BASE_DIR / "data" / "docker_trainer.db"

# --------------------------------------------------------------------------
# Timeouts
# --------------------------------------------------------------------------

# Wall clock for one image build. Generous, because a first build has to pull
# a base image over the network; a *learner's* build that has not finished in
# two minutes is a build that is wrong, not a build that is slow.
BUILD_TIMEOUT_SECONDS = 180.0

# Wall clock for one container run. A check is supposed to be a command that
# finishes, so this is short -- a container that has not exited in twenty
# seconds is a container the learner forgot to stop.
RUN_TIMEOUT_SECONDS = 20.0

# Wall clock for a shell script in a CLI exercise. Longer than a container run
# because a script may start several containers before it queries them.
SCRIPT_TIMEOUT_SECONDS = 60.0

# Wall clock for `docker compose build` and for a compose service run.
COMPOSE_BUILD_TIMEOUT_SECONDS = 240.0
COMPOSE_RUN_TIMEOUT_SECONDS = 45.0

# --------------------------------------------------------------------------
# Container limits
# --------------------------------------------------------------------------

# Address space a container may use. A learner's image that allocates a
# gigabyte dies with an OOM instead of taking the machine with it.
CONTAINER_MEMORY_BYTES = 512 * 1024 * 1024

# CPU quota for a container, as a fraction of one core.
CONTAINER_CPUS = 1.0

# Maximum number of processes inside a container. A fork bomb inside a
# container is stopped by the container's own PID limit.
CONTAINER_PIDS_LIMIT = 128

# How much stdout/stderr may be captured from one check. Beyond this the run
# is stopped and the UI says so, rather than the server holding a learner's
# accidental gigabyte of output in memory.
OUTPUT_LIMIT_BYTES = 100_000

# How many failing checks are shown in a grading diff. More than this and the
# shape is off rather than a near miss -- the message says so instead of
# showing an arbitrary slice.
DIFF_CHECK_LIMIT = 8

# Default tolerance for float comparison, used when a check does not set one.
# `0.1 + 0.2 == 0.3` has to hold: a check that teaches floating-point
# paranoia is teaching the wrong lesson.
DEFAULT_FLOAT_TOLERANCE = 1e-9

# Relative tolerance for measurements that are allowed to differ -- an image's
# size, a build's duration. These are compared as a share of the reference.
DEFAULT_RELATIVE_TOLERANCE = 0.05

# --------------------------------------------------------------------------
# Images and projects
# --------------------------------------------------------------------------

# Every image this app builds is tagged under this prefix, so `docker images`
# shows at a glance what the trainer owns and `scripts/prune_images.py` knows
# what it may delete.
IMAGE_TAG_PREFIX = "dockertrainer"

# The compose project name used for every build and every config check. It is
# fixed rather than derived from a directory name, because `docker compose
# config` embeds the project name in its output and a derived name would make
# two identical submissions compare unequal.
COMPOSE_PROJECT_NAME = "dockertrainer"

# The network a container run is attached to. "none" by default: a check has
# no business reaching the internet, and a build context has no business
# being able to exfiltrate one.
DEFAULT_RUN_NETWORK = "none"

# The maximum number of files an exercise may submit. A build context is
# copied into the daemon on every build, so this is a real bound on how much
# work one submission can create.
MAX_SUBMISSION_FILES = 40

# The maximum size of a single submitted file, in bytes.
MAX_FILE_BYTES = 512 * 1024
