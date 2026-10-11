# AI Maintenance Coordinator (Keyhook job demo)

**Live demo:** (coming soon)

A working prototype of an AI agent that takes a tenant maintenance request all the way to a
verified work order, with safety overrides and a human approval gate. Built as a job-application
demo for the AI Product Engineer role at Keyhook (NZ property-maintenance software).

It was built with AI coding assistance (Cursor) under Mohammed Alasaad's direction. It is a
prototype, not a production system and not affiliated with Keyhook. By default it runs in
**Demo mode**: triage is rule-based (no AI model connected), every tool is a mock, and all
tenants, addresses and contractors are **fictional example data**. Nothing is ever sent.

**Stack:** Python FastAPI · SQLite (stdlib `sqlite3`) · single-page HTML/JS/CSS served by FastAPI
(no Node build step) · pydantic · pytest.

## Setup and run

Windows (PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python seed.py                  # optional: the app also auto-seeds an empty DB
.venv\Scripts\python -m uvicorn app.main:app --port 8000
```

macOS / Linux:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m uvicorn app.main:app --port 8000
```

Open http://localhost:8000. Run the tests with `python -m pytest` (inside the venv).
`requirements.txt` holds the runtime dependencies only; `requirements-dev.txt` adds pytest and httpx.

- `python seed.py --reset` wipes all requests and audit logs, then re-creates the 5 sample requests.
  The **Reset demo data** button in the UI does the same thing.
- The database lives at `data/keyhook.db`. Set `KEYHOOK_DB_PATH` to use another file, or
  `KEYHOOK_AUTO_SEED=0` to stop the app seeding an empty database on startup.

### Deploying on Vercel

`index.py` re-exports `app` from `app/main.py`, which Vercel's FastAPI preset picks up with no
extra routing; `vercel.json` only sets the function's `maxDuration`. When the `VERCEL` env var is
set, the SQLite database lives in `/tmp/keyhook.db` (the only writable path) and each fresh
serverless instance re-seeds the 5 fictional sample requests on its first request. That means
the hosted demo **does not keep changes**: anything a visitor adds or approves can disappear when
an instance is recycled, and separate instances do not share data. Use **Reset demo data** to
start clean at any time.

### Optional: LLM mode

By default the app runs in **Demo mode - rule-based, no AI model connected**, and the header
badge says so. To use a real model, set one of these before starting the server:

```powershell
$env:OPENAI_API_KEY = "sk-..."   # optional: $env:OPENAI_MODEL = "gpt-4o-mini"
# or
$env:XAI_API_KEY = "xai-..."     # optional: $env:XAI_MODEL = "grok-3-mini"
```

The model must return JSON matching a strict pydantic schema: known enums, a trade that matches
the category, confidence between 0 and 1, and no extra fields. If the output is invalid, or the
call fails, the request falls back to the rule-based classifier. The triage card and audit log
show that a fallback happened and why. The UI never labels a triage as AI unless a model produced it.

## Workflow

```
received -> triaged -> needs_info | escalated | awaiting_approval
needs_info -> received (tenant replies, re-triage)
escalated -> awaiting_approval (only after a human confirms the emergency is handled)
awaiting_approval -> approved | rejected
approved -> work_order_created -> contractor_notified (DRAFT only) -> verified | verification_failed
verification_failed -> approved (human-triggered retry)
```

The state machine in `app/workflow.py` enforces the transitions. Every transition, tool call
(with its inputs and outputs), safety override, human decision and verification result is written
to the `events` table, so the audit trail survives restarts.

| Step | What happens |
|---|---|
| Intake | Tenant name, address, description, optional photo note, contact preference. Validated by pydantic. |
| Triage | Category, urgency, trade, missing info, confidence, reasoning. Rule-based by default, LLM if configured. |
| Safety rules | Gas smell, sparking/exposed wiring, flooding, or no heat with a vulnerable occupant all force an **emergency escalation**. These rules run after the model and override it. The agent halts, drafts safety instructions for the tenant, and a human must confirm handling before any approval. |
| Needs info | Vague requests get a drafted clarifying question for the tenant (draft only). |
| Approval gate | A property manager must Approve or Reject (a rejection needs a note). `create_work_order` itself refuses to run without a recorded approval. |
| Tools (mock) | `find_contractor(trade, area)` from a seeded contractor table, `estimate_cost`, `create_work_order`, `draft_contractor_message` (stored as "DRAFT - NOT SENT"). |
| Verification | Checks the work order has a contractor, the contractor's trade matches the category, the urgency SLA is set, the contractor message references the right address, the message is a draft, and an approval was recorded first. If any check fails, the UI shows a red "Failed verification" card with the reasons. |
| Fault injection | In the approval panel, pick a fault (wrong trade, missing contractor, wrong address, missing SLA) to see verification catch a bad work order. You can then **Void & retry**. |

## Suggested demo script

1. **Dripping tap** (#1): approve it and watch it go to a verified work order. Open the audit trail
   to see each tool's input and output.
2. **Gas smell** (#3): it is escalated, approval is blocked, and safety instructions are drafted.
   Mark safety handled, then approve.
3. **Something is broken** (#5): it needs info and a clarifying question is drafted. Add a reply
   such as "The oven in the kitchen won't turn on" to re-triage it.
4. **Heat pump** (#2): approve it with the "wrong address" fault. Verification fails with the
   reason shown. Then Void & retry.
5. **Blocked drain** (#4): reject it with a note.

## Project layout

```
app/
  main.py          FastAPI app factory, API routes, error handlers, static UI
  workflow.py      state machine, audit logging, approval/escalation/retry actions
  triage.py        rule-based classifier, safety rules, optional LLM + schema validation
  tools.py         mock tools (contractor lookup, cost estimate, work order, message drafts)
  verification.py  work order verification checks
  models.py        pydantic schemas (API inputs + strict triage output)
  db.py            SQLite schema, connection, seeded contractors
  seed_data.py     the 5 sample tenant requests
  static/          index.html, app.js, styles.css
seed.py            seed / reset the database
tests/             pytest suite (triage, workflow, API)
```

## API overview

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/status` | Triage mode (demo or LLM) |
| GET | `/api/requests?status=&urgency=&category=&q=` | List with filters and status counts |
| POST | `/api/requests` | Create a request (it is triaged immediately) |
| GET | `/api/requests/{id}` | Detail: triage, approvals, work order, drafts, verification, events |
| POST | `/api/requests/{id}/info` | Add the tenant's reply and re-triage (`needs_info` only) |
| POST | `/api/requests/{id}/escalation/handle` | Human confirms the safety handling (`escalated` only) |
| POST | `/api/requests/{id}/approve` | Approve (optional `inject_fault`), then the agent executes |
| POST | `/api/requests/{id}/reject` | Reject with a note |
| POST | `/api/requests/{id}/retry` | Void a failed work order and rebuild it |
| GET | `/api/contractors` | Seeded contractor directory |
| POST | `/api/demo/reset` | Wipe the data and re-seed the samples |

Invalid input returns `422` with field-level errors. Bad filters return `400`. Unknown IDs return
`404`. Actions that are not allowed in the current state return `409`.

## Limitations (honest)

- **Prototype.** It has no authentication, no multi-tenancy, no roles, and it is not hardened for production.
- **Mock tools.** Contractors are a small fictional seeded table, the cost estimates come from a
  made-up rate card, and contractor matching is a simple city lookup plus the highest rating.
- **Demo mode is not AI.** The default classifier uses keyword rules. It is deterministic and
  explainable, but it will misread unusual wording. LLM mode is optional and needs your own API key.
  It has only been exercised with a fake client in tests, not against a live model in this build.
- **No real messaging.** Tenant and contractor messages are drafts stored in SQLite and marked
  "DRAFT - NOT SENT". Nothing is emailed, texted or called.
- **The safety rules are pattern-based.** They err towards escalating, but they can miss phrasing
  they don't recognise. A real system would need a broader policy and a person monitoring
  emergencies. The safety instructions are drafts for a human to review.
- The SLA times (4h, 24h, 120h) and the NZ-specific advice (111, Healthline 0800 611 116) are
  illustrative and not a compliance statement.

---

Built by Mohammed Alasaad with AI coding assistance (Cursor).
