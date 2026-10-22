# Authoring exercises

An exercise is a **task**: a set of files the learner edits, a **reference**
that solves it, and a list of **checks** that turn an artefact into a
comparable value.

The rule that shapes everything else:

> **Grading is by reference solution, not by a lookup table.**

There is no expected output written anywhere. The grader builds the
reference artefact, runs the same checks against it, and compares. The
reference's behaviour *is* the answer. So there is nothing that can drift
out of date, and any correct implementation passes however it was written.

---

## The shape of an exercise

```yaml
- id: f-example                       # unique across every track
  title: A short title
  prompt: |                           # markdown, required
    What the learner must produce.
  kind: image                         # image | compose | cli
  entry_file: Dockerfile              # optional; defaults per kind
  files:                              # the starter the learner edits
    Dockerfile: |
      FROM alpine:3.20
      # ...
  solution:                           # the reference artefact
    Dockerfile: |
      FROM alpine:3.20
      CMD ["echo", "hello"]
  checks:                             # what the grader does
    - name: running the image greets
      kind: default
  cases:                              # optional; one case by default
    - name: the default run
  hints:
    - A first nudge.
    - A stronger nudge.
  teach: |
    Why this works -- shown under "Why this works".
  tags: [dockerfile, commands]
```

### `kind`

| Kind | The learner writes | How it is graded |
| --- | --- | --- |
| `image` | a Dockerfile, plus any files it copies | the image is built and checks run inside it |
| `compose` | a compose project | its canonical configuration is read, and its services are run |
| `cli` | a shell script | the script is run against a prepared Docker environment |

The default entry file is `Dockerfile`, `compose.yaml` and `solve.sh`
respectively. Override it with `entry_file`.

---

## Checks

A check produces **one comparable value**, and that value is produced by
running the check against the reference artefact *and* against the learner's
artefact. The two are then compared.

| Kind | What it does | The value it produces |
| --- | --- | --- |
| `run` | runs a shell command inside the image | stdout + exit code |
| `default` | runs the image with **its own** `ENTRYPOINT`/`CMD` | stdout + exit code |
| `inspect` | reads a field of the built image | that field, as JSON |
| `config` | reads a compose project's canonical configuration | the whole configuration, as JSON |
| `service` | runs a command in one compose service | stdout + exit code |
| `script` | runs the submission's script | stdout + exit code |

### `run` versus `default`

This is the difference that catches people out.

`run` **overrides** the image's entrypoint, so the command you wrote is what
runs. Use it to look *inside* the image: `cat /etc/os-release`, `ls /app`,
`whoami`.

`default` does **not** override anything. It runs the image exactly as
`docker run <image>` would, so the image's own `CMD` decides what happens.
Use it whenever the exercise is about *what the container does*.

An exercise about `CMD` that only has a `run` check is not testing `CMD` at
all.

### `inspect` templates

The template is a full Go template, exactly as `docker image inspect
--format` takes it:

```yaml
template: "{{len .RootFS.Layers}}"        # a number
template: "{{json .Config.ExposedPorts}}" # {"80/tcp":{}}
template: "{{json .Config.User}}"         # "appuser"
template: "{{json .Config.Entrypoint}}"    # ["/usr/bin/app"]
template: "{{json .Config.Cmd}}"          # ["--serve"]
template: "{{json .Config.Env}}"          # ["PATH=...", "APP_VERSION=1.0"]
template: "{{.Size}}"                     # bytes
```

### Per-check settings

```yaml
settings:
  output_mode: exact        # exact | lines | json   (default: lines)
  order_sensitive: true     # does list order matter?  (default: true)
  float_tolerance: 0.001
  relative_tolerance: 0.05  # for measurements, like an image's size
  ignore_paths: ["Config.Labels"]
  scrub: true               # replace ids and timestamps before comparing
```

A check's own settings win over the exercise's.

---

## Cases

A case is the Docker analogue of a test case: the same artefact, exercised
under different conditions.

```yaml
cases:
  - name: the default run
  - name: the production build
    build_args:
      VERSION: "2.0.0"
  - name: with a different environment
    env:
      MODE: "production"
```

`build_args` change the image, so each distinct set of build arguments is a
separate build. `env` changes only the run, so those cases share one image.

Every case runs every check. A failure in any case fails the exercise, and
the diff says which case.

---

## CLI exercises

A CLI exercise has `setup` and `teardown` commands, and the learner writes a
script.

```yaml
kind: cli
files:
  solve.sh: |
    # your commands here
solution:
  solve.sh: |
    docker ps --filter label=trainer=demo --format '{{.Names}}' | sort
setup:
  - "docker run -d --name trainer-demo --label trainer=demo alpine:3.20 sleep 300"
teardown:
  - "docker rm -f trainer-demo"
```

The setup runs, then the script runs, then the teardown runs. The reference
script runs in exactly the same way, in its own setup, so the two see the
same environment.

Two rules:

- **Print names, not ids.** Container ids are random. Use
  `--format '{{.Names}}'`, and set `scrub: true` on the check if you cannot
  avoid them.
- **The setup must be deterministic.** Fixed container names, fixed images.

---

## Source-level rules

Some exercises are about a construct rather than a result, and a result-based
grader cannot see those. They are opt-in:

```yaml
settings:
  forbid: ["latest"]        # must not appear, case-insensitively
  require: ["USER "]        # must appear, case-insensitively
```

---

## Rules for writing good exercises

**The check must be able to fail.** If an empty submission passes, the
exercise is not testing anything. `scripts/smoke.py` checks this.

**The starter must not be the answer.** The smoke test submits an empty
version of each starter file and requires it to fail.

**The reference must pass.** Against itself, obviously -- but also with the
checks you actually wrote, not the ones you meant to write.

**Keep the base image small.** `alpine:3.20` builds in about a second.
`python:3.14-slim` is acceptable. A full `python:3.14` is not: every
submission would spend ten seconds pulling it.

**Do not use `latest`.** Not because the grader forbids it, but because it
is bad practice and the course should not model it.

**Checks must be deterministic.** No timestamps, no random values, no
"whatever order the network answered in". If a check cannot be deterministic,
scrub it or do not write it.

**One idea per exercise.** An exercise that teaches three things teaches
none of them.

**Hints escalate.** The first is a nudge, the last nearly gives it away.

**`teach` explains the idea, not the answer.** It should still make sense to
somebody who solved the exercise a different way.

---

## Checking your work

```bash
uv run python scripts/lint_content.py     # YAML hazards
uv run python scripts/smoke.py            # every reference builds and passes
uv run python scripts/smoke.py f-example  # just one
uv run python scripts/tag_exercises.py    # concept tags
uv run pytest -q                          # the whole suite
```

`scripts/smoke.py` is the important one. It builds every reference, runs
every check against it, and then submits an empty version of every starter
and requires that to fail.
