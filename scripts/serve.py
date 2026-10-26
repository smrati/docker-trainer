"""Start the app on the first free port.

    uv run python scripts/serve.py               # 8000, or the next free one
    uv run python scripts/serve.py --port 9000   # start at 9000 instead
    uv run python scripts/serve.py --host 0.0.0.0
    uv run python scripts/serve.py --dry-run     # print the port and stop

The usual complaint about ``uvicorn app.main:app`` is that port 8000 is
sometimes taken -- by something else, or by a previous run that has not
been stopped. This script asks the operating system which ports are free
and uses the first one, so the same command works every time.

The port is found by *binding* it, not by reading a list of listening
sockets. Binding is the only question that matters: a port can appear in
no list and still be unusable, and a port that is free in a list can be
taken by the time the server starts. There is still a small race between
the check and the server binding -- another process can take the port in
that window -- but it is a window of milliseconds and it only happens if
something else is starting on the same port at exactly the same moment.
"""

import argparse
import errno
import os
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The port to start looking from.
DEFAULT_PORT = 8000

# How many ports to try before giving up. Twenty covers a laptop where
# something has grabbed a range of them.
MAX_ATTEMPTS = 20

# The highest port number there is.
MAX_PORT = 65535


def is_free(port: int, host: str = "127.0.0.1") -> bool:
    """Is this port free to bind?

    The socket is bound *and* listened on, because a port can be reserved
    without being in the list of listening sockets, and only a real bind
    finds out.
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            sock.listen(1)
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, errno.EACCES, errno.EADDRNOTAVAIL):
            return False
        raise
    return True


def find_port(preferred: int, host: str = "127.0.0.1", attempts: int = MAX_ATTEMPTS):
    """The first free port at or after ``preferred``.

    Returns ``(port, taken)``, where ``taken`` is the list of ports that
    were busy, so the caller can say why it moved.
    """
    taken = []
    for offset in range(attempts):
        candidate = preferred + offset
        if candidate > MAX_PORT:
            break
        if is_free(candidate, host):
            return candidate, taken
        taken.append(candidate)
    return None, taken


def _display_host(host: str) -> str:
    """The host to show in the URL.

    ``0.0.0.0`` is the address to bind, not one to visit, so the URL
    shows the loopback address instead.
    """
    return "127.0.0.1" if host in ("0.0.0.0", "::") else host


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", DEFAULT_PORT)),
        help="the port to start looking from (default: %(default)s)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "127.0.0.1"),
        help="the address to bind (default: %(default)s)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="restart the server when the code changes",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the port that would be used and stop",
    )
    args = parser.parse_args()

    port, taken = find_port(args.port, args.host)
    if port is None:
        print(
            f"No free port in {args.port}-{args.port + MAX_ATTEMPTS - 1}.",
            file=sys.stderr,
        )
        return 1

    if taken:
        print(f"Port {args.port} is busy, so using {port} instead.")
        sys.stdout.flush()

    url = f"http://{_display_host(args.host)}:{port}"
    if args.dry_run:
        print(port)
        return 0

    argv = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        args.host,
        "--port",
        str(port),
    ]
    if args.reload:
        argv.append("--reload")

    print()
    print("  Docker Trainer")
    print(f"  {url}")
    print()
    print("  Ctrl+C to stop.")
    print()
    # Flushed explicitly: the banner is the first thing worth reading, and
    # it must not wait for the child's output to arrive.
    sys.stdout.flush()

    try:
        return subprocess.call(argv, cwd=ROOT)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
