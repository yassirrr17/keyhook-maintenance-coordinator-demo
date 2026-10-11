"""FastAPI app: JSON API under /api and the single-page UI at /."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Optional

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import workflow
from .db import clear_request_data, connect, init_db, resolve_db_path
from .models import (
    CATEGORIES,
    FAULT_TYPES,
    URGENCIES,
    ApproveBody,
    EscalationHandleBody,
    ProvideInfoBody,
    RejectBody,
    RequestCreate,
    RetryBody,
)
from .seed_data import SAMPLE_REQUESTS, seed_requests
from .triage import LLMClient, llm_client_from_env, llm_config_from_env

logger = logging.getLogger("keyhook")
STATIC_DIR = Path(__file__).resolve().parent / "static"

DEMO_LABEL = "Demo mode - rule-based, no AI model connected"


def create_app(
    db_path: Optional[str] = None,
    auto_seed: Optional[bool] = None,
    llm_factory: Callable[[], Optional[LLMClient]] = llm_client_from_env,
) -> FastAPI:
    path = resolve_db_path(db_path)
    if auto_seed is None:
        auto_seed = os.environ.get("KEYHOOK_AUTO_SEED", "1") == "1"
    state = {"initialized": False}

    def ensure_initialized() -> None:
        if state["initialized"]:
            return
        conn = connect(path)
        try:
            init_db(conn)
            if auto_seed and conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0:
                seed_requests(conn, llm_factory())
        finally:
            conn.close()
        state["initialized"] = True

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        ensure_initialized()
        yield

    app = FastAPI(title="AI Maintenance Coordinator (Keyhook demo)", version="1.0.0", lifespan=lifespan)
    app.state.db_path = path

    def get_conn():
        ensure_initialized()
        conn = connect(path)
        try:
            yield conn
        finally:
            conn.close()

    # ---------------------------------------------------------------- error handling

    @app.exception_handler(workflow.WorkflowError)
    async def workflow_error_handler(_request: Request, exc: workflow.WorkflowError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, exc: RequestValidationError):
        errors = []
        for err in exc.errors():
            loc = [str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path")]
            errors.append({"field": ".".join(loc) or "body", "message": err.get("msg", "Invalid value")})
        return JSONResponse(status_code=422, content={"detail": "Validation failed", "errors": errors})

    @app.exception_handler(Exception)
    async def unhandled_error_handler(_request: Request, exc: Exception):
        logger.exception("Unhandled error", exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    # ---------------------------------------------------------------- UI

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # ---------------------------------------------------------------- API

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/status")
    def status():
        cfg = llm_config_from_env()
        if cfg:
            return {
                "mode": "llm",
                "provider": cfg["provider"],
                "model": cfg["model"],
                "label": f"LLM mode - {cfg['provider']} {cfg['model']} (schema-validated, rule fallback)",
            }
        return {"mode": "demo", "provider": None, "model": None, "label": DEMO_LABEL}

    @app.get("/api/meta")
    def meta():
        return {
            "states": list(workflow.STATES),
            "transitions": {k: sorted(v) for k, v in workflow.TRANSITIONS.items()},
            "categories": list(CATEGORIES),
            "urgencies": list(URGENCIES),
            "fault_types": list(FAULT_TYPES),
            "samples": SAMPLE_REQUESTS,
        }

    @app.get("/api/requests")
    def list_requests(
        status: Optional[str] = Query(None, max_length=40),
        urgency: Optional[str] = Query(None, max_length=40),
        category: Optional[str] = Query(None, max_length=40),
        q: Optional[str] = Query(None, max_length=200),
        conn=Depends(get_conn),
    ):
        if status and status not in workflow.STATES:
            raise workflow.WorkflowError(f"Unknown status filter '{status}'", 400)
        if urgency and urgency not in URGENCIES:
            raise workflow.WorkflowError(f"Unknown urgency filter '{urgency}'", 400)
        if category and category not in CATEGORIES:
            raise workflow.WorkflowError(f"Unknown category filter '{category}'", 400)
        return workflow.list_requests(conn, status=status, urgency=urgency, category=category, q=q or None)

    @app.post("/api/requests", status_code=201)
    def create_request(body: RequestCreate, conn=Depends(get_conn)):
        rid = workflow.create_request(conn, body, llm_factory())
        return workflow.get_request_detail(conn, rid)

    @app.get("/api/requests/{request_id}")
    def get_request(request_id: int, conn=Depends(get_conn)):
        return workflow.get_request_detail(conn, request_id)

    @app.post("/api/requests/{request_id}/info")
    def provide_info(request_id: int, body: ProvideInfoBody, conn=Depends(get_conn)):
        workflow.provide_info(conn, request_id, body.additional_info, body.actor, llm_factory())
        return workflow.get_request_detail(conn, request_id)

    @app.post("/api/requests/{request_id}/escalation/handle")
    def handle_escalation(request_id: int, body: EscalationHandleBody, conn=Depends(get_conn)):
        workflow.handle_escalation(conn, request_id, body.handler, body.note, body.safety_confirmed)
        return workflow.get_request_detail(conn, request_id)

    @app.post("/api/requests/{request_id}/approve")
    def approve(request_id: int, body: ApproveBody, conn=Depends(get_conn)):
        workflow.approve(conn, request_id, body.approver, body.note, body.inject_fault)
        return workflow.get_request_detail(conn, request_id)

    @app.post("/api/requests/{request_id}/reject")
    def reject(request_id: int, body: RejectBody, conn=Depends(get_conn)):
        workflow.reject(conn, request_id, body.approver, body.note)
        return workflow.get_request_detail(conn, request_id)

    @app.post("/api/requests/{request_id}/retry")
    def retry(request_id: int, body: RetryBody, conn=Depends(get_conn)):
        workflow.retry(conn, request_id, body.actor)
        return workflow.get_request_detail(conn, request_id)

    @app.get("/api/contractors")
    def contractors(conn=Depends(get_conn)):
        return [dict(r) for r in conn.execute("SELECT * FROM contractors ORDER BY region, trade, id")]

    @app.post("/api/demo/reset")
    def reset_demo(conn=Depends(get_conn)):
        clear_request_data(conn)
        ids = seed_requests(conn, llm_factory())
        return {"seeded": len(ids), "ids": ids}

    return app


app = create_app()
