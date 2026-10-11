"""Vercel entrypoint: Vercel's FastAPI preset looks for an ``app`` instance in index.py.

Locally, keep using ``python -m uvicorn app.main:app``; this file just re-exports it.
"""

from app.main import app  # noqa: F401
