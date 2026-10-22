"""FastAPI application entry point.

Serves the API and the static frontend from one process, so the whole app is
`uv run uvicorn app.main:app --reload` with no build step and no separate
web server.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import STATIC_DIR
from app.curriculum.loader import ContentError, load_curriculum
from app.db import init_db
from app.routers import content, practice, review


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load and validate the curriculum before serving anything.

    Failing here is deliberate. A broken exercise is an authoring bug, and
    discovering it at startup is far better than surfacing a reference
    solution that does not build to a learner who has already typed their
    answer.
    """
    init_db()
    try:
        app.state.curriculum = load_curriculum()
    except ContentError as exc:
        raise RuntimeError(f"Could not load the curriculum: {exc}") from exc
    yield


app = FastAPI(
    title="Docker Trainer",
    description="Learn, practise and get assessed on Docker.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(content.router)
app.include_router(practice.router)
app.include_router(review.router)


@app.get("/api/health")
def health(request: Request):
    """Status, content counts, and whether the docker daemon is reachable.

    The daemon check is reported rather than enforced. The app is perfectly
    usable for reading lessons without a daemon; it simply cannot build
    anything, and saying so plainly is better than failing on the first
    submission.
    """
    from app.engine.cli import compose_available, daemon_version

    curriculum = getattr(request.app.state, "curriculum", None)
    if curriculum is None:
        return {"status": "starting"}

    version = daemon_version()
    return {
        "status": "ok",
        "tracks": len(curriculum.tracks),
        "lessons": len(curriculum.lessons),
        "exercises": len(curriculum.exercises),
        "checks": sum(len(ex.checks) for ex in curriculum.exercises.values()),
        "docker": {
            "available": version is not None,
            "version": version,
            "compose": compose_available(),
        },
    }


# The frontend is mounted last so its catch-all never shadows an /api route.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


@app.exception_handler(404)
async def not_found(request: Request, exc):
    """Unknown API paths get JSON; unknown page paths get the SPA shell.

    The raised exception's own detail is preserved -- a 404 that says only
    "Not found" throws away the useful part, like the list of valid exercises.
    """
    detail = getattr(exc, "detail", None) or "Not found"
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": detail}, status_code=404)
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return JSONResponse({"detail": detail}, status_code=404)
