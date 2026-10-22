"""Shared FastAPI dependencies."""

from fastapi import HTTPException, Request

from app.curriculum.loader import Curriculum


def get_curriculum(request: Request) -> Curriculum:
    """The curriculum loaded once at startup and shared by all requests."""
    curriculum = getattr(request.app.state, "curriculum", None)
    if curriculum is None:  # pragma: no cover - only if lifespan was skipped
        raise HTTPException(status_code=503, detail="Curriculum is not loaded yet.")
    return curriculum
