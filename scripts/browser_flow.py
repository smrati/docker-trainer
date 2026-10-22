"""Interactive browser checks.

Static checks catch a missing file; they do not catch a workflow that silently
does nothing. These drive the real UI the way a learner would -- open a
lesson, write a Dockerfile, submit it, read the feedback -- and assert on what
actually ended up on screen.

    uv run python scripts/browser_flow.py [--base URL]

The builds are real, so these checks take a while. They are not part of
``pytest``: they need a running server and a Docker daemon.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from browser_check import CHROME, Chrome, new_tab  # noqa: E402

# The exercise the solve and diff flows use. It has one check, so a wrong
# answer still has the right *shape* and reaches the comparison.
EXERCISE = "f-run-a-container"
CORRECT = "FROM alpine:3.20\n\nCMD [\"echo\", \"hello from docker\"]\n"
WRONG = "FROM alpine:3.20\n\nCMD [\"echo\", \"goodbye from docker\"]\n"
CRASHING = "FROM alpine:3.20\n\nRUN this-command-does-not-exist\n"

# How long to wait for a build. An Alpine build is a second or two; a cold
# pull is longer.
BUILD_WAIT = 25.0


class Checks:
    def __init__(self):
        self.passed = 0
        self.failed = 0

    def ok(self, label):
        self.passed += 1
        print(f"  ok   {label}")

    def bad(self, label, detail=""):
        self.failed += 1
        print(f"  FAIL {label}")
        if detail:
            print(f"       {detail}")


async def wait_for(page, expression, timeout=BUILD_WAIT, interval=0.4):
    """Poll a JS expression until it is truthy, or give up."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if await page.evaluate(expression):
            return True
        await asyncio.sleep(interval)
    return False


async def check_lesson_markdown(page, checks: Checks, base: str):
    """Lesson bodies load lazily on open -- verify the markdown really renders."""
    await page.goto(f"{base}/learn.html", settle=2.0)

    # Before opening, nothing should be rendered yet (lazy by design).
    before = await page.evaluate("document.querySelectorAll('.prose').length")

    await page.evaluate("document.querySelectorAll('details.card')[0].open = true")
    await asyncio.sleep(1.5)

    info = await page.evaluate(
        """(() => {
            const prose = document.querySelector('.prose');
            if (!prose) return {found: false};
            return {
                found: true,
                html: prose.innerHTML.length,
                headings: prose.querySelectorAll('h2, h3').length,
                codeBlocks: prose.querySelectorAll('pre code').length,
                tables: prose.querySelectorAll('table').length,
                unrendered: prose.textContent.includes('```'),
            };
        })()"""
    )

    if not info.get("found"):
        checks.bad("lesson body renders after opening", "no .prose element")
        return
    if info["headings"] < 1:
        checks.bad("lesson markdown produces headings", f"headings={info['headings']}")
    elif info["codeBlocks"] < 1:
        checks.bad("lesson markdown produces code blocks", f"code={info['codeBlocks']}")
    elif info["unrendered"]:
        checks.bad("no raw markdown left visible", "``` still present in the text")
    else:
        checks.ok(
            f"lesson markdown renders ({info['headings']} headings, "
            f"{info['codeBlocks']} code blocks, lazy before={before})"
        )


async def check_solve_flow(page, checks: Checks, base: str):
    """Write a correct Dockerfile, submit, and confirm the feedback is right."""
    await page.goto(f"{base}/exercise.html?id={EXERCISE}", settle=2.0)

    await page.evaluate(
        f"""(() => {{
            const editor = document.querySelector('.code-editor');
            editor.value = {CORRECT!r};
            editor.dispatchEvent(new Event('input', {{bubbles: true}}));
            [...document.querySelectorAll('button')].find(b => b.textContent === 'Submit answer').click();
        }})()"""
    )
    done = await wait_for(page, "!!document.querySelector('.banner.success, .banner.warn')")

    state = await page.evaluate(
        """(() => {
            const banner = document.querySelector('.banner.success');
            const diff = document.querySelector('table.diff');
            const next = [...document.querySelectorAll('a')].find(a => a.textContent.includes('Next exercise'));
            return {
                success: !!banner,
                message: banner ? banner.textContent.trim() : null,
                rows: diff ? diff.querySelectorAll('tbody tr').length : 0,
                headers: diff ? [...diff.querySelectorAll('th')].map(t => t.textContent) : [],
                nextHref: next ? next.getAttribute('href') : null,
                status: document.querySelector('.badge.solved') ? 'solved' : null,
            };
        })()"""
    )

    if not done:
        checks.bad("the submission finished")
    elif not state["success"]:
        checks.bad("correct answer shows a success banner", f"message={state['message']!r}")
    elif state["rows"] < 1:
        checks.bad("every check is listed", f"rows={state['rows']}")
    elif state["headers"] != ["Check", "Result", "Expected", "Yours"]:
        checks.bad("check table headers", f"got {state['headers']}")
    elif not state["nextHref"] or state["nextHref"] == "[object Promise]":
        checks.bad("'Next exercise' link has a real href", f"href={state['nextHref']!r}")
    else:
        checks.ok(
            f"correct answer: banner, {state['rows']} check(s), next -> {state['nextHref']}"
        )


async def check_diff_flow(page, checks: Checks, base: str):
    """A wrong answer must explain itself, not just say 'wrong'.

    The answer keeps the right shape and gets the output wrong, so grading
    reaches the comparison and produces a diff rather than a crash.
    """
    await page.goto(f"{base}/exercise.html?id={EXERCISE}", settle=2.0)

    await page.evaluate(
        f"""(() => {{
            const editor = document.querySelector('.code-editor');
            editor.value = {WRONG!r};
            editor.dispatchEvent(new Event('input', {{bubbles: true}}));
            [...document.querySelectorAll('button')].find(b => b.textContent === 'Submit answer').click();
        }})()"""
    )
    done = await wait_for(page, "!!document.querySelector('.banner.success, .banner.warn, .banner.error')")

    state = await page.evaluate(
        """(() => {
            const warn = document.querySelector('.banner.warn');
            const diff = document.querySelector('table.diff');
            const detail = document.querySelector('.diff-detail');
            return {
                warned: !!warn,
                message: warn ? warn.textContent.trim() : null,
                diffRows: diff ? diff.querySelectorAll('tbody tr').length : 0,
                headers: diff ? [...diff.querySelectorAll('th')].map(t => t.textContent) : [],
                detail: detail ? detail.textContent.trim() : null,
                expected: diff ? diff.querySelector('tbody .was')?.textContent.trim() : null,
                actual: diff ? diff.querySelector('tbody .now')?.textContent.trim() : null,
            };
        })()"""
    )

    if not done:
        checks.bad("the submission finished")
    elif not state["warned"]:
        checks.bad("wrong answer shows a warning banner", f"message={state['message']!r}")
    elif state["diffRows"] < 1:
        checks.bad("wrong answer shows a per-check diff", f"diffRows={state['diffRows']}")
    elif state["headers"] != ["Check", "Result", "Expected", "Yours"]:
        checks.bad("diff table headers", f"got {state['headers']}")
    elif not state["detail"]:
        checks.bad("wrong answer says what differs", "no .diff-detail element")
    else:
        checks.ok(
            f"wrong answer: banner + {state['diffRows']} diff row(s); "
            f"expected {state['expected']!r} got {state['actual']!r}"
        )


async def check_build_failure(page, checks: Checks, base: str):
    """A Dockerfile that does not build must say so, and show the output."""
    await page.goto(f"{base}/exercise.html?id={EXERCISE}", settle=2.0)

    await page.evaluate(
        f"""(() => {{
            const editor = document.querySelector('.code-editor');
            editor.value = {CRASHING!r};
            editor.dispatchEvent(new Event('input', {{bubbles: true}}));
            [...document.querySelectorAll('button')].find(b => b.textContent === 'Submit answer').click();
        }})()"""
    )
    done = await wait_for(
        page, "!!document.querySelector('.banner.error, .banner.warn, .banner.success')"
    )

    state = await page.evaluate(
        """(() => {
            const banner = document.querySelector('.banner.error');
            const log = document.querySelector('pre.output');
            return {
                errored: !!banner,
                message: banner ? banner.textContent.trim() : null,
                log: log ? log.textContent : '',
            };
        })()"""
    )

    if not done:
        checks.bad("the failing build finished")
    elif not state["errored"]:
        checks.bad("a failed build shows an error banner", f"message={state['message']!r}")
    elif "not found" not in state["log"] and "ERROR" not in state["log"]:
        checks.bad("the build output is shown", "no error line in the log")
    else:
        checks.ok("a failed build shows an error and its output")

async def check_hints_and_files(page, checks: Checks, base: str):
    """Hints are progressive, and the editor shows every file."""
    await page.goto(f"{base}/exercise.html?id=f-copy-a-directory", settle=2.0)

    await page.evaluate(
        """[...document.querySelectorAll('button')].find(b => b.textContent.startsWith('Hint')).click()"""
    )
    await asyncio.sleep(1.2)
    first = await page.evaluate(
        """({text: document.querySelector('.hint')?.textContent.trim() || null,
            label: document.querySelector('.hint-index')?.textContent || null})"""
    )
    await page.evaluate(
        """[...document.querySelectorAll('button')].find(b => b.textContent.startsWith('Hint')).click()"""
    )
    await asyncio.sleep(1.2)
    second = await page.evaluate("document.querySelectorAll('.hint').length")

    if not first["text"]:
        checks.bad("hint reveals text")
    elif not first["label"] or not first["label"].startswith("Hint 1 of"):
        checks.bad("hint is numbered", f"label={first['label']!r}")
    elif second != 2:
        checks.bad("hints accumulate", f"count={second}")
    else:
        checks.ok(f"hints progressive: 1 -> {second}")

    # The file tabs list the exercise's files. Assert on rendered size, not
    # DOM presence: a container with display:none still has all its child
    # nodes, so a querySelectorAll count passes on content nobody can see.
    tabs = await page.evaluate(
        """(() => {
            const tab = document.querySelector('.file-tab');
            const host = tab ? tab.parentElement : null;
            return {
                tabs: document.querySelectorAll('.file-tab').length,
                height: tab ? tab.getBoundingClientRect().height : 0,
                hostDisplay: host ? getComputedStyle(host).display : 'none',
            };
        })()"""
    )
    if tabs["tabs"] < 1:
        checks.bad("the editor shows a tab per file", f"tabs={tabs['tabs']}")
    elif tabs["hostDisplay"] == "none":
        checks.bad("the file tabs are actually visible", "display is 'none'")
    elif tabs["height"] <= 0:
        checks.bad("the file tabs render with height", f"height={tabs['height']}")
    else:
        checks.ok(
            f"file tabs: {tabs['tabs']} file(s), rendered {round(tabs['height'])}px tall"
        )


async def check_reveal_marks_revealed(page, checks: Checks, base: str):
    """Revealing must change the recorded state, not just show the answer."""
    await page.goto(f"{base}/exercise.html?id=f-one-layer", settle=2.0)
    await page.evaluate(
        """[...document.querySelectorAll('button')].find(b => b.textContent.includes('Show solution')).click()"""
    )
    await asyncio.sleep(1.5)

    state = await page.evaluate(
        """(() => {
            const badges = [...document.querySelectorAll('.badge')].map(b => b.textContent.trim());
            return { badges, hasSolution: !!document.querySelector('pre.output.value') };
        })()"""
    )
    if not state["hasSolution"]:
        checks.bad("revealing shows the solution")
    elif "Revealed" not in state["badges"]:
        checks.bad("revealing updates the status badge", f"badges={state['badges']}")
    else:
        checks.ok(f"reveal shows solution and marks revealed: {state['badges']}")


async def check_playground(page, checks: Checks, base: str):
    await page.goto(f"{base}/playground.html", settle=2.0)

    await page.evaluate(
        """(() => {
            document.querySelector('#editor').value = "FROM alpine:3.20\\nCMD [\\"echo\\", \\"playground works\\"]\\n";
            document.querySelector('#run').click();
        })()"""
    )
    done = await wait_for(page, "!!document.querySelector('.check-card pre.output')", timeout=60.0)

    output = await page.evaluate(
        """(() => {
            // The check's own output, not the build log: the log is long and
            // comes first in the DOM.
            const pre = document.querySelector('.check-card pre.output');
            return pre ? pre.textContent.trim() : null;
        })()"""
    )
    if not done:
        checks.bad("the playground build finished")
    elif "playground works" not in (output or ""):
        checks.bad("playground runs the image and shows its output", f"got {output!r}")
    else:
        checks.ok("playground builds an image and shows its output")


async def check_review_flow(page, checks: Checks, base: str):
    """Concept filtering must actually filter the queue."""
    await page.goto(f"{base}/review.html", settle=2.0)
    before = await page.evaluate("document.querySelectorAll('#queue a.ex-row').length")
    await page.evaluate(
        """document.querySelectorAll('#concepts .card')[0].click()"""
    )
    await asyncio.sleep(0.6)
    after = await page.evaluate("document.querySelectorAll('#queue a.ex-row').length")
    chips = await page.evaluate(
        """document.querySelector('#queue-sub')?.textContent.trim() || ''"""
    )
    if "Filtering by" not in chips:
        checks.bad("concept click shows a filter chip", f"sub={chips!r}")
    elif after > before:
        checks.bad("concept filter narrows the queue", f"{before} -> {after}")
    else:
        checks.ok(f"concept filter works ({before} -> {after} rows)")


FLOWS = [
    ("lesson markdown", check_lesson_markdown),
    ("solve flow", check_solve_flow),
    ("diff flow", check_diff_flow),
    ("build failure", check_build_failure),
    ("hints and file tabs", check_hints_and_files),
    ("reveal marks revealed", check_reveal_marks_revealed),
    ("playground", check_playground),
    ("review filtering", check_review_flow),
]


async def run(base: str):
    browser = Chrome()
    checks = Checks()
    try:
        browser.wait_ready()
        for label, flow in FLOWS:
            page = await new_tab(browser)
            try:
                await flow(page, checks, base)
            except Exception as exc:
                checks.failed += 1
                print(f"  FAIL {label} (raised {type(exc).__name__}: {exc})")
            finally:
                await page.ws.close()
    finally:
        browser.close()
    return checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base",
        default="http://127.0.0.1:8101",
        help="the base URL of the running app (default: %(default)s)",
    )
    args = parser.parse_args()

    if not CHROME:
        print("no Chrome/Chromium found; skipping")
        return 0
    checks = asyncio.run(run(args.base.rstrip('/')))
    print()
    print(f"{checks.passed} passed, {checks.failed} failed")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
