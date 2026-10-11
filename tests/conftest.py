from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.main import create_app
from app.models import RequestCreate
from app.seed_data import sample_payload
from app.workflow import create_request


class FakeLLM:
    """Stands in for a real model so tests never touch the network."""

    def __init__(self, response=None, error: Exception | None = None, model: str = "fake-model"):
        self.response = response
        self.error = error
        self.model = model
        self.calls = 0

    def complete(self, user_text: str) -> str:
        self.calls += 1
        if self.error:
            raise self.error
        return self.response if isinstance(self.response, str) else json.dumps(self.response)


@pytest.fixture(autouse=True)
def no_llm_keys(monkeypatch):
    for key in ("OPENAI_API_KEY", "XAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def conn(db_path):
    c = connect(db_path)
    init_db(c)
    yield c
    c.close()


@pytest.fixture
def make_request(conn):
    def _make(key: str | None = None, **overrides) -> int:
        payload = sample_payload(key) if key else {}
        payload.update(overrides)
        return create_request(conn, RequestCreate(**payload))

    return _make


@pytest.fixture
def client(db_path):
    app = create_app(db_path=db_path, auto_seed=False, llm_factory=lambda: None)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def sample():
    return sample_payload
