# Docker Trainer

A full-stack web app for learning, practising and getting assessed on Docker.
FastAPI backend, vanilla JavaScript frontend, SQLite for progress storage.
No build step, no bundler, no framework.

Built for working software professionals at any level: three tracks, eighteen
lessons and seventy-three graded exercises, from your first `FROM` to
multi-stage builds, BuildKit cache mounts and reproducible images.

---

## Quick start

```bash
uv sync
uv run uvicorn app.main:app --reload
```

Then open <http://127.0.0.1:8000>.

**Docker has to be installed and running.** The exercises are really built
and really run, so there is no daemon, no exercises. The lessons and the
exercise text still work without one, and the home page says so plainly.

Run the tests:

```bash
uv run pytest -q
```

Lint the Python and the curriculum:

```bash
uv run ruff check app tests scripts
uv run python scripts/lint_content.py
```

Check the curriculum's concept tags, and verify that every reference
solution builds and passes its own checks:

```bash
uv run python scripts/tag_exercises.py
uv run python scripts/smoke.py
```

`smoke.py` is the important one. It builds every reference in the
curriculum, runs every check against it, and then submits an empty version of
every starter and requires that to fail. It needs a Docker daemon and takes
a few minutes.

Free up the disk the trainer's images use:

```bash
uv run python scripts/prune_images.py --dry-run
uv run python scripts/prune_images.py
```

### Browser checks

There is no bundler to complain that a module failed to load, so the frontend
is verified by driving a real headless Chrome over the DevTools Protocol:

```bash
uv run uvicorn app.main:app --port 8100 &     # the checks drive a live server

uv run python scripts/browser_check.py --base http://127.0.0.1:8100
uv run python scripts/browser_flow.py         # drives port 8101
```

`browser_check` loads every page and fails on any console error, uncaught
exception or failed request. `browser_flow` then *uses* the app: opens a
lesson and checks the markdown rendered, writes a Dockerfile and submits it,
inspects the failure diff, triggers a build failure, reads a traceback,
reveals a solution, runs the playground and filters the review queue.

This is not redundant with the pytest suite. The playground built the
*context directory* as if it were a Dockerfile, because the throwaway
exercise it builds had no entry file: the page rendered, every asset loaded,
every HTTP check passed, and the playground simply never built anything.
`test_every_api_method_used_exists` catches that class of mistake
statically; the flow checks catch what static analysis cannot.

---

## How it works

### Grading is by reference solution, not by a lookup table

Every exercise stores a **reference solution** -- a set of files: a
Dockerfile, a compose project, a shell script -- and a list of **checks**.

When you submit, the app builds and runs *your* files exactly as it builds
and runs the reference's, and compares. The reference's behaviour **is** the
expected answer:

- for a check that runs a command, the output and the exit code;
- for a check that inspects the image, the field it read;
- for a check that reads a compose project, the whole canonical
  configuration.

There is no hand-written expected output anywhere in this app, so there is
nothing that can drift out of date, and any correct implementation passes
however you wrote it. `CMD ["echo", "hi"]` and `CMD echo "hi"` are the same
answer.

The reference is built once per exercise and cached.

### Three kinds of exercise

| Kind | What you write | How it is graded |
| --- | --- | --- |
| `image` | a Dockerfile, plus the files it copies | the image is built; checks run inside it |
| `compose` | a compose project | its canonical configuration is read; its services are run |
| `cli` | a shell script | the script runs against a prepared Docker environment |

A submission is always a **set of files**, whatever the kind. The editor
has a tab per file, because a Dockerfile is rarely the only thing you write.

### Checks

A check produces one comparable value, and that value is produced by running
the check against the reference *and* against your artefact.

| Check | What it does |
| --- | --- |
| `run` | runs a shell command **inside** the image, overriding its entrypoint |
| `default` | runs the image with **its own** `ENTRYPOINT`/`CMD`, as `docker run` would |
| `inspect` | reads a field of the built image: `{{json .Config.ExposedPorts}}` |
| `config` | reads a compose project's canonical configuration |
| `service` | runs a command inside one compose service |
| `script` | runs the submission's script |

`run` and `default` are different on purpose. `run` overrides the
entrypoint, so it looks *inside* the image. `default` does not, so it tells
you what the image actually *does*. An exercise about `CMD` graded only by
`run` checks is not testing `CMD` at all.

### Cases

A case is the Docker analogue of a test case: the same artefact, exercised
under different conditions.

```yaml
cases:
  - name: the default run
  - name: the production build
    build_args:
      VERSION: "2.0.0"
```

A build argument changes the image, so each distinct set of build arguments
is a separate build. An environment variable changes only the run. Every
case runs every check; a failure in any case fails the exercise, and the
diff says which one.

### Comparison is semantic, not textual

| Rule | Default | Why |
| --- | --- | --- |
| Object key order | **ignored** | The order of a compose file's keys is not the skill. |
| `true` vs `1` | **different** | JSON's `true` is not `1`, and a boolean field must not accept one. |
| `null` | matches only `null` | The same rule the SQL trainer uses for `NULL`. |
| Numbers | compared numerically | `1` matches `1.0`; a measurement gets a tolerance. |
| List order | **sensitive** | In a compose file, the order of `ports` decides which is published first. |

**A failed grade always shows a diff** — every check, what was expected, and
what you produced, and the *path* to the difference when the answer is a
structure:

```
services.web.ports[0].target: expected 80, got 8080
```

Being told "incorrect" without being shown which check differed is the
worst experience this app could deliver.

### Two rules that look at your source

A result-based grader cannot tell *how* you wrote something. Some exercises
are about the construct rather than the result, so they may ask for
something extra:

```yaml
settings:
  forbid: ["latest"]        # the image may not be tagged `latest`
  require: ["USER "]        # the Dockerfile must declare a user
```

These are opt-in per exercise. An exercise that does not ask for them is
graded on behaviour alone.

### What the containers get, and why

Every check runs in a container with:

- **no network** — a check has no business reaching the internet, and a
  build context has no business sending anything out;
- **a 512 MB memory cap, a CPU quota and a PID limit** — a runaway image
  dies with an OOM or with the container's own PID limit rather than taking
  the machine with it;
- **its own `USER` and `WORKDIR`** — because an exercise about `USER` is
  testing exactly that;
- **a wall clock** — a container that has not exited in twenty seconds is
  stopped.

### The trust model, stated plainly

This is a local, single-user teaching application, and the CLI exercise
kind hands the learner the `docker` command line. That is a real thing and
it is not hidden: the person running the app already has a shell and
already has Docker.

What the app does anyway:

- **a minimal environment** for every subprocess, so no credential in your
  shell leaks into a learner's build;
- **a denylist** for the flags that hand a container the host's privileges
  (`--privileged`, `--pid=host`, `--net=host`, `--cap-add`, …). This is a
  denylist rather than an allow-list for the same reason the SQL trainer
  used one: an allow-list has to enumerate every flag Docker will ever
  grow, and silently blocking a legitimate command is a bad trade. It
  catches accidents; it is not a sandbox;
- **a wall clock** on every build, run and script;
- **output caps**, so an accidental gigabyte of printed output is truncated
  rather than held in memory;
- **build contexts in a throwaway directory**, with paths validated: an
  absolute path, a `..`, or a backslash is refused.

If you ever deploy this for untrusted users, disable the CLI exercise kind.
Everything else is contained by the container itself.

### `run` and `submit` are separate endpoints

`/run` builds and runs your files and shows you what they did. It never
grades and never records progress, so experimenting cannot be mistaken for
completing an exercise.

### Solved and revealed are counted separately

Opening a solution is free, but it is recorded. There are three states:

- `unsolved` — not completed
- `revealed` — solution opened, explicitly **not** a solve
- `solved` — passed with no prior reveal

Passing an exercise *after* revealing it leaves it `revealed`. The dashboard
reports solved and revealed side by side and never combines them into one
completion figure — a percentage that silently includes copied answers is
worse than no percentage.

---

### Spaced repetition

Solving an exercise puts it into a review schedule with a widening interval:

```
first pass          ->  1 day
second pass         ->  2.2 days
third               ->  4.8 days
...                 ->  capped at 180
```

A **failure is not a longer interval — it is due immediately.** "Review"
therefore means "redo what you got wrong", which is the same feedback loop
the grader already runs. A failure resets stability to zero, so the next
pass earns a full day rather than topping up a 113-day interval.

Three things are deliberately *not* scheduled:

- **Revealed exercises.** Opening the answer and reproducing it proves you
  can copy, not that you retained anything. Revealing also removes the
  exercise from the schedule outright.
- **Failures of exercises you have never solved.** Failing something you
  are currently learning is not forgetting — it is learning.
- **`run`, as opposed to `submit`.** Exploring is not recall.

Because a working developer thinks in topics rather than exercise ids,
exercises also carry concept tags (`layers`, `multi-stage`, `buildkit`, …)
and the review page aggregates per-concept strength. Concepts are ranked by
actionable work first — how many are due — then by pass rate. Deliberately
not a weighted composite score: a hidden formula is impossible to explain to
a learner and impossible to tune without real usage data.

## Layout

```
app/
  main.py              FastAPI app, static mount, SPA fallback
  config.py            limits and paths
  db.py                app database + migrations
  progress.py          solved/revealed state machine, review persistence
  srs.py               spaced-repetition scheduling (pure functions)
  grading.py           run -> compare -> record -> schedule
  engine/
    cli.py             the one place that shells out to docker
    context.py         a submission -> a validated build context
    image.py           building, caching, inspecting, pruning
    probe.py           running a command inside a built image
    compose.py         compose config, compose build, compose run
    script.py          running a shell script, and the flag denylist
    values.py          values, rendered for a human
    compare.py         semantic comparison of values and checks
    runner.py          the parent: build, run, compare, clean up
  curriculum/loader.py YAML loading and validation
  routers/             content.py (read), practice.py (run/submit/reveal),
                       review.py (due queue + concepts)
  static/              7 pages, 6 ES modules, one stylesheet
content/
  AUTHORING.md         how to write an exercise
  */track.yaml         tracks -> lessons -> exercises
scripts/               lint_content.py, fix_quoting.py, fix_indentation.py,
                       tag_exercises.py, smoke.py, prune_images.py,
                       browser_check.py, browser_flow.py
tests/
```

Two separate databases, deliberately: the app database holds progress, while
every build a learner submits gets its own throwaway directory. Nothing you
type can reach your progress.

Schema changes go through `MIGRATIONS` in `db.py` and are applied against
`PRAGMA user_version`; `tests/test_db.py` migrates a real v1 file and checks
the existing progress survives.

## Adding content

Read `content/AUTHORING.md` — it is the full specification of an exercise.
The short version:

```yaml
- id: my-exercise
  title: Short title
  prompt: What the learner must produce.
  kind: image                 # or "compose" or "cli"
  entry_file: Dockerfile      # optional; defaults per kind
  files:                      # the starter the learner edits
    Dockerfile: |
      FROM alpine:3.20
      # ...
  solution:                   # the reference
    Dockerfile: |
      FROM alpine:3.20
      CMD ["echo", "hello"]
  checks:                     # what the grader does
    - name: running the image greets
      kind: default
  cases:
    - name: the default run
  hints:
    - First nudge.
  teach: >
    Why this works -- shown under "Why this works".
  tags: [dockerfile, commands]
```

Everything is validated at startup: duplicate ids, a reference with no
entry file, an exercise with no checks, and a check kind the engine cannot
run all fail loudly rather than surprising a learner mid-exercise.
`tests/test_curriculum.py` enforces this, plus that every exercise has
hints, an explanation, tags and a starter that does not contain the answer.

### A note on authoring YAML

Plain scalars in YAML have sharp edges, and the errors point at the wrong
line. A scalar cannot *begin* with `` ` `` or `*` (both common in inline
code and bold), cannot contain `": "`, and inside a `|` block any line
indented less than the first content line silently terminates the block.

`scripts/lint_content.py` catches all of these and names the offending line.
`scripts/fix_quoting.py` repairs the quoting case and
`scripts/fix_indentation.py` repairs the mis-indented-continuation case.

The repair tools are deliberately narrow: they fix the one thing they
understand and preserve each line's depth *relative to its block* so nested
Dockerfiles keep their shape. Structural mistakes — a mis-indented key, a
broken list — are left for a human, because a tool that rewrites what it
does not fully understand will quietly corrupt content. Run them with
`--check` first.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | status, content counts, docker daemon state |
| `GET` | `/api/curriculum` | full tree with per-exercise status |
| `GET` | `/api/lessons/{id}` | lesson text (never solutions) |
| `GET` | `/api/exercises/{id}` | prompt, starter files, check names, status |
| `GET` | `/api/exercises/{id}/hints/{n}` | one hint at a time |
| `POST` | `/api/exercises/{id}/run` | build and run, no grading |
| `POST` | `/api/exercises/{id}/submit` | grade and record |
| `POST` | `/api/exercises/{id}/solution` | reveal, and record the reveal |
| `POST` | `/api/playground/run` | build any Dockerfile, ungraded |
| `GET` | `/api/review` | due queue, counts, per-concept strength |
| `GET`/`DELETE` | `/api/progress` | dashboard data / reset |

## Known limits

- **A Docker daemon is required.** The app reads lessons without one, but
  it cannot build anything.
- **Single local user.** No accounts, no sync — progress lives in one
  SQLite file.
- **Builds are slow enough to notice.** A cold base-image pull is tens of
  seconds; a warm one is a second or two. The reference image is cached so
  only your build is repeated.
- **The CLI exercise kind grants Docker access.** It is a local, single-user
  tool. See the trust model above.
- **Side effects are not always observable.** The grader compares what your
  artefact does, so an exercise cannot detect that you mutated a file in a
  way it does not then report.
- **Service-to-service DNS is not tested behaviourally.** `docker compose
  run --no-deps` does not start dependencies, so a network exercise is
  graded on the canonical configuration and on the container's own network
  settings.
- **The schedule is uniform.** Every concept uses the same interval ladder,
  and exercise difficulty does not yet affect how fast an interval grows. A
  hard exercise would probably deserve a slower growth rate than an easy
  one.
- **Concepts are evenly weighted.** Two exercises sharing a tag contribute
  the same weight regardless of how central that tag is to the exercise.
- **No import or export.** Progress lives in one local file.
- **Browser checks need Chrome/Chromium on PATH and a running server**;
  they are not part of `pytest`, so they must be run deliberately.
- **`uv build` fails**: setuptools cannot auto-discover packages in a flat
  layout. This is an application rather than a distributable library, so
  there is nothing to install — but if you ever want a PyPI package, it
  needs an explicit `[tool.hatch.build]` or a `src/` layout.

## Licence

[Apache-2.0](LICENSE) — copyright 2026 smrati.

Apache-2.0 rather than MIT because it grants contributors an explicit
patent licence, and terminates that grant if they bring patent litigation
against the project. For most solo projects the practical difference is
nil; it matters if this ever gets used inside a company.
