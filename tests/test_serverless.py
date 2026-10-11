"""The Vercel (serverless) path: DB in /tmp, fresh sample data on a cold start."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import db
from app.main import create_app
from app.seed_data import SAMPLE_REQUESTS


def test_vercel_env_puts_db_in_tmp(monkeypatch):
    monkeypatch.delenv("KEYHOOK_DB_PATH", raising=False)
    monkeypatch.setenv("VERCEL", "1")
    assert db.resolve_db_path() == str(db.SERVERLESS_DB_PATH)


def test_local_default_unchanged(monkeypatch):
    monkeypatch.delenv("KEYHOOK_DB_PATH", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    assert db.resolve_db_path() == str(db.DEFAULT_DB_PATH)


def test_cold_start_seeds_fresh_samples_without_lifespan(monkeypatch, tmp_path):
    # Simulate a fresh serverless instance: empty /tmp DB, first request may arrive without
    # the lifespan having run, so the first API call must still see the seeded samples.
    monkeypatch.delenv("KEYHOOK_DB_PATH", raising=False)
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setattr(db, "SERVERLESS_DB_PATH", tmp_path / "keyhook.db")
    app = create_app(llm_factory=lambda: None)
    client = TestClient(app)  # not used as a context manager -> lifespan does not run
    data = client.get("/api/requests").json()
    items = data["items"] if isinstance(data, dict) and "items" in data else data
    assert len(items) == len(SAMPLE_REQUESTS)
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
