"""Drive headless Chrome over the DevTools Protocol.

Exists because the frontend has no build step and therefore no bundler to tell
it that a module failed to load. A page can satisfy every static check --
syntax valid, assets present, imports resolve -- and still be broken at
runtime because a property is undefined on `null`.

Reports, per page:
  * uncaught exceptions and console errors
  * failed network requests
  * a screenshot

Usage:
    uv run python scripts/browser_check.py [--base URL] [--shots DIR]
"""

import argparse
import asyncio
import base64
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import websockets
except ImportError:  # pragma: no cover
    print("needs websockets: uv add --dev websockets", file=sys.stderr)
    raise SystemExit(2) from None

CHROME = (
    shutil.which("google-chrome")
    or shutil.which("chromium")
    or shutil.which("chromium-browser")
)

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Chrome:
    def __init__(self, width=1280, height=1200):
        if not CHROME:
            raise RuntimeError("no Chrome/Chromium binary found")
        self.port = free_port()
        self.profile = tempfile.mkdtemp(prefix="chrome-profile-")
        self.process = subprocess.Popen(
            [
                CHROME,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--no-first-run",
                "--disable-dev-shm-usage",
                "--hide-scrollbars",
                f"--remote-debugging-port={self.port}",
                f"--user-data-dir={self.profile}",
                f"--window-size={width},{height}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.base = f"http://127.0.0.1:{self.port}"

    def wait_ready(self, timeout=25.0) -> str:
        import urllib.request

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{self.base}/json/version", timeout=1) as response:
                    return json.load(response)["webSocketDebuggerUrl"]
            except Exception:
                time.sleep(0.2)
        raise TimeoutError("Chrome did not expose a debugging endpoint")

    def close(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            self.process.kill()
        shutil.rmtree(self.profile, ignore_errors=True)


class Page:
    """A single tab, with the error collection attached."""

    def __init__(self, ws):
        self.ws = ws
        self.counter = 0
        self.errors: list[str] = []
        self.failed_requests: list[str] = []

    async def send(self, method, **params):
        self.counter += 1
        message_id = self.counter
        await self.ws.send(json.dumps({"id": message_id, "method": method, "params": params}))
        while True:
            raw = json.loads(await self.ws.recv())
            self._note(raw)
            if raw.get("id") == message_id:
                if "error" in raw:
                    raise RuntimeError(f"{method} failed: {raw['error']}")
                return raw.get("result", {})

    def _note(self, message):
        method = message.get("method")
        params = message.get("params", {})

        if method == "Runtime.exceptionThrown":
            detail = params.get("exceptionDetails", {})
            text = detail.get("exception", {}).get("description") or detail.get("text")
            self.errors.append(f"uncaught: {text}")
        elif method == "Runtime.consoleAPICalled" and params.get("type") in {"error", "warning"}:
            args = " ".join(str(a.get("value", a.get("description", ""))) for a in params.get("args", []))
            self.errors.append(f"console.{params['type']}: {args}")
        elif method == "Log.entryAdded":
            entry = params.get("entry", {})
            if entry.get("level") == "error":
                self.errors.append(f"log: {entry.get('text')}")
        elif method == "Network.loadingFailed":
            self.failed_requests.append(params.get("errorText", "unknown"))

    async def evaluate(self, expression):
        result = await self.send(
            "Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True
        )
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            raise RuntimeError(detail.get("exception", {}).get("description") or detail.get("text"))
        return result.get("result", {}).get("value")

    async def goto(self, url, settle=1.5):
        await self.send("Page.navigate", url=url)
        await asyncio.sleep(settle)

    async def screenshot(self, path: Path):
        data = await self.send("Page.captureScreenshot", format="png")
        path.write_bytes(base64.b64decode(data["data"]))


async def new_tab(browser: Chrome) -> Page:
    import urllib.request

    # Newer Chrome requires PUT here; GET is rejected with 405.
    request = urllib.request.Request(
        f"{browser.base}/json/new?about:blank", method="PUT"
    )
    with urllib.request.urlopen(request) as response:
        target = json.load(response)
    ws_url = target["webSocketDebuggerUrl"]
    ws = await websockets.connect(ws_url, max_size=64 * 1024 * 1024)
    page = Page(ws)
    await page.send("Page.enable")
    await page.send("Runtime.enable")
    await page.send("Log.enable")
    await page.send("Network.enable")
    return page


PAGES = [
    ("home", "/"),
    ("learn", "/learn.html"),
    ("practice", "/practice.html"),
    ("exercise", "/exercise.html?id=f-run-a-container"),
    ("review", "/review.html"),
    ("playground", "/playground.html"),
    ("progress", "/progress.html"),
]


async def run(base: str, shot_dir: Path | None, only: str | None):
    browser = Chrome()
    failures = 0
    try:
        browser.wait_ready()
        for name, path in PAGES:
            if only and only != name:
                continue
            page = await new_tab(browser)
            await page.goto(f"{base}{path}")
            await asyncio.sleep(1.0)

            if shot_dir:
                await page.screenshot(shot_dir / f"{name}.png")

            if page.errors or page.failed_requests:
                failures += 1
                print(f"FAIL {name} ({path})")
                for error in page.errors:
                    print(f"     {error}")
                for request in page.failed_requests:
                    print(f"     request failed: {request}")
            else:
                print(f"ok   {name} ({path})")
            await page.ws.close()
    finally:
        browser.close()
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8100")
    parser.add_argument("--shots", default=None)
    parser.add_argument("--only", default=None)
    args = parser.parse_args()

    if not CHROME:
        print("no Chrome/Chromium found; skipping browser checks")
        return 0

    shot_dir = Path(args.shots).resolve() if args.shots else None
    if shot_dir:
        shot_dir.mkdir(parents=True, exist_ok=True)

    failures = asyncio.run(run(args.base, shot_dir, args.only))
    print()
    if failures:
        print(f"{failures} page(s) reported browser errors")
        return 1
    print("no console errors, exceptions or failed requests on any page")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())