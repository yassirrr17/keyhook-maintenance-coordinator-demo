# Build spec: AI Maintenance Coordinator (Keyhook job demo)

Build a genuinely working web app from scratch in THIS folder (C:\Users\mohammed\keyhook-demo, Windows, Python 3.14 available).
Purpose: a job-application demo for an "AI Product Engineer" role at Keyhook (NZ property-maintenance software). It must show an AI agent that takes a tenant maintenance request all the way to a verified work order, with safety and human approval.

Stack: Python FastAPI + SQLite (stdlib sqlite3) + a single-page HTML/JS/CSS frontend served by FastAPI (no Node build step). requirements.txt, pytest tests.

Core workflow (explicit state machine, every transition logged):
received -> triaged -> (needs_info | escalated | awaiting_approval) -> approved/rejected -> work_order_created -> contractor_notified (DRAFT only, never really sent) -> verified | verification_failed.

Features:
1. Intake form: tenant name, property address, description, optional photo note, contact preference. Seeded sample requests (dripping tap, broken heater, gas smell, blocked drain, vague "something is broken").
2. Triage: category (plumbing, electrical, gas, heating, appliance, structural, other), urgency (emergency/urgent/routine), trade, missing-info list, confidence, reasoning. Output validated against a strict schema (pydantic). Two modes: (a) LLM mode if OPENAI_API_KEY or XAI_API_KEY env var is set (JSON output, schema-validated, falls back to rules on invalid output); (b) default "Demo mode" deterministic rule-based classifier, with a clear "Demo mode - rule-based, no AI model connected" badge in the UI. Never claim AI is running when it is not.
3. Safety rules that override the model: gas smell, sparking/exposed wiring, flooding, no heat for vulnerable occupant -> emergency escalation, human must handle, show safety instructions draft; agent may not auto-proceed.
4. Missing info: vague requests go to needs_info with a drafted clarifying question to the tenant (draft only).
5. Human approval gate: property manager must Approve/Reject (with note) before any work order. Nothing executes before approval.
6. Tools (mock, deterministic, logged): find_contractor(trade, area) from a seeded contractor table, create_work_order, draft_contractor_message (marked "DRAFT - NOT SENT"), estimate_cost range.
7. Verification step: checks work order has contractor, trade matches category, urgency SLA set, message references the right address; failure shows a red "Failed verification" card with reasons. Include a "fault injection" toggle/button to demonstrate verification catching a bad work order.
8. Execution log / audit trail per request: timestamped steps, tool inputs/outputs, state transitions, who approved. Persist in SQLite; survives restart.
9. Dashboard: list of requests with status chips, filters; detail view with triage card, approval panel, work order, log timeline. Polished, clean, professional UI (dark/neutral modern style), responsive.
10. Error handling: input validation messages, graceful API errors, no crashes on bad input.

Tests (pytest, must all pass): classification of each seed type, gas -> escalated and cannot be approved into a work order without human handling, vague -> needs_info, approval required before work order, rejection path, verification pass and injected-failure path, schema validation rejects bad LLM output and falls back, persistence across a new DB connection, API endpoints (FastAPI TestClient), invalid input returns 422/400. Aim for 20+ tests.

Deliverables: app code, seed script (python seed.py), run instructions, README.md (setup on Windows: python -m venv .venv; .venv\Scripts\pip install -r requirements.txt; .venv\Scripts\python -m uvicorn app.main:app --port 8000), honest Limitations section (mock tools, demo mode, no real messaging, prototype), and a note: "Built by Mohammed Alasaad with AI coding assistance (Cursor)".

Process: create venv, install, implement, run pytest until ALL tests pass, start the server briefly and hit the main endpoints to confirm it serves the UI and API, then stop it. Do not deploy, push, or send anything externally. Do not ask questions; make sensible decisions. At the end print a short summary: files created, test results (count passed/failed), how to run.
