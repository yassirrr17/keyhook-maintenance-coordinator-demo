"""Explicit state machine for a maintenance request. Every transition, tool call and human
decision is appended to the ``events`` table (the audit trail)."""

from __future__ import annotations

import json
import sqlite3
from typing import Optional

from . import tools
from .db import now_iso, row_to_dict
from .models import CATEGORY_TRADE, SLA_HOURS, RequestCreate
from .triage import LLMClient, triage_request
from .verification import verify_work_order

STATES = (
    "received",
    "triaged",
    "needs_info",
    "escalated",
    "awaiting_approval",
    "approved",
    "rejected",
    "work_order_created",
    "contractor_notified",
    "verified",
    "verification_failed",
)

TRANSITIONS: dict[str, frozenset[str]] = {
    "received": frozenset({"triaged"}),
    "triaged": frozenset({"needs_info", "escalated", "awaiting_approval"}),
    "needs_info": frozenset({"received"}),  # tenant supplied more info -> re-triage
    "escalated": frozenset({"awaiting_approval"}),  # only after a human confirms safety handling
    "awaiting_approval": frozenset({"approved", "rejected"}),
    "approved": frozenset({"work_order_created"}),
    "work_order_created": frozenset({"contractor_notified"}),
    "contractor_notified": frozenset({"verified", "verification_failed"}),
    "verification_failed": frozenset({"approved"}),  # human-triggered retry under the same approval
    "verified": frozenset(),
    "rejected": frozenset(),
}

ALLOWED_ACTIONS = {
    "needs_info": ["provide_info"],
    "escalated": ["handle_escalation"],
    "awaiting_approval": ["approve", "reject"],
    "verification_failed": ["retry"],
}

ESCALATION_BLOCK_MESSAGE = (
    "This request is a safety escalation. A human must handle the emergency and confirm it "
    "before any work order can be approved."
)


class WorkflowError(Exception):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class NotFoundError(WorkflowError):
    def __init__(self, message: str):
        super().__init__(message, 404)


# --------------------------------------------------------------------------------------
# Low-level helpers
# --------------------------------------------------------------------------------------


def log_event(
    conn: sqlite3.Connection,
    request_id: int,
    kind: str,
    summary: str,
    *,
    actor: str = "agent",
    data: Optional[dict] = None,
    from_state: Optional[str] = None,
    to_state: Optional[str] = None,
) -> None:
    conn.execute(
        "INSERT INTO events (request_id, ts, actor, kind, summary, from_state, to_state, data) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (request_id, now_iso(), actor, kind, summary, from_state, to_state,
         json.dumps(data, default=str) if data is not None else None),
    )


def _get_request(conn: sqlite3.Connection, request_id: int) -> dict:
    row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Request {request_id} not found")
    return dict(row)


def _latest_triage(conn: sqlite3.Connection, request_id: int) -> Optional[dict]:
    return row_to_dict(
        conn.execute("SELECT * FROM triages WHERE request_id = ? ORDER BY id DESC LIMIT 1", (request_id,)).fetchone(),
        ("missing_info", "safety_flags"),
    )


def transition(conn: sqlite3.Connection, request_id: int, to_state: str, *, actor: str, reason: str) -> None:
    current = _get_request(conn, request_id)["status"]
    if to_state not in TRANSITIONS.get(current, frozenset()):
        raise WorkflowError(f"Invalid transition: {current} -> {to_state}")
    conn.execute("UPDATE requests SET status = ?, updated_at = ? WHERE id = ?", (to_state, now_iso(), request_id))
    log_event(conn, request_id, "state_transition", f"{current} -> {to_state}: {reason}",
              actor=actor, from_state=current, to_state=to_state)


def _tool(conn: sqlite3.Connection, request_id: int, name: str, inputs: dict, fn) -> dict:
    output = fn()
    log_event(conn, request_id, "tool_call", f"Tool {name}", actor="agent",
              data={"tool": name, "input": inputs, "output": output})
    return output


def _require_status(request: dict, expected: str, action: str) -> None:
    if request["status"] == "escalated" and expected != "escalated":
        raise WorkflowError(ESCALATION_BLOCK_MESSAGE)
    if request["status"] != expected:
        raise WorkflowError(
            f"Cannot {action}: request is '{request['status']}', expected '{expected}'."
        )


def _run_atomically(conn: sqlite3.Connection, request_id: Optional[int], action: str, fn):
    """Run ``fn`` in one transaction. On unexpected failure roll back and log the error."""
    try:
        with conn:
            return fn()
    except WorkflowError:
        raise
    except tools.ApprovalRequiredError as exc:
        raise WorkflowError(str(exc)) from exc
    except Exception as exc:
        if request_id is not None:
            with conn:
                log_event(conn, request_id, "error", f"{action} failed and was rolled back: {type(exc).__name__}",
                          actor="system", data={"error": str(exc)[:500]})
        raise


# --------------------------------------------------------------------------------------
# Workflow steps
# --------------------------------------------------------------------------------------


def _triage_step(conn: sqlite3.Connection, request_id: int, llm: Optional[LLMClient]) -> None:
    req = _get_request(conn, request_id)
    result = triage_request(req["description"], req["photo_note"], llm)
    out = result.output
    flags = [f.to_dict() for f in result.safety_flags]

    if result.fallback_reason:
        log_event(conn, request_id, "llm_fallback", result.fallback_reason, actor="system",
                  data={"raw_output": result.raw_output})
    engine_label = f"LLM ({result.model})" if result.engine == "llm" else "rule-based classifier"
    log_event(
        conn, request_id, "triage",
        f"Triage by {engine_label}: {out.category} / {out.urgency} / {out.trade} (confidence {out.confidence:.2f})",
        actor="agent",
        data={"engine": result.engine, "model": result.model, "output": out.model_dump(), "route": result.route},
    )
    for override in result.overrides:
        log_event(conn, request_id, "safety_override", f"Safety rule overrode classifier: {override}", actor="safety-rules")

    conn.execute(
        """INSERT INTO triages (request_id, engine, model, category, urgency, trade, missing_info, confidence,
               reasoning, safety_flags, route, fallback_reason, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (request_id, result.engine, result.model, out.category, out.urgency, out.trade, json.dumps(out.missing_info),
         out.confidence, out.reasoning, json.dumps(flags), result.route, result.fallback_reason, now_iso()),
    )
    transition(conn, request_id, "triaged", actor="agent",
               reason=f"Classified as {out.category}/{out.urgency} by {engine_label}")

    if result.route == "escalated":
        _tool(conn, request_id, "draft_safety_instructions", {"flags": [f["code"] for f in flags]},
              lambda: tools.draft_safety_instructions(conn, request=req, flags=flags))
        transition(conn, request_id, "escalated", actor="safety-rules",
                   reason=f"Safety rule triggered ({', '.join(f['label'] for f in flags)}). "
                          "Agent halted; a human must handle this emergency.")
    elif result.route == "needs_info":
        _tool(conn, request_id, "draft_tenant_clarification", {"missing_info": out.missing_info},
              lambda: tools.draft_tenant_clarification(conn, request=req, missing_info=out.missing_info))
        transition(conn, request_id, "needs_info", actor="agent",
                   reason="Request too vague to dispatch; clarifying question drafted for the tenant (not sent).")
    else:
        transition(conn, request_id, "awaiting_approval", actor="agent",
                   reason="Triage complete; waiting for property manager approval.")


def _execute(conn: sqlite3.Connection, request_id: int, fault: Optional[str]) -> None:
    """Agent execution after approval: contractor -> estimate -> work order -> draft -> verify."""
    req = _get_request(conn, request_id)
    if req["status"] != "approved":
        raise tools.ApprovalRequiredError("Execution requires an approved request.")
    triage = _latest_triage(conn, request_id)
    category, urgency = triage["category"], triage["urgency"]
    trade = CATEGORY_TRADE[category]
    area = tools.extract_area(req["property_address"])

    found = _tool(conn, request_id, "find_contractor", {"trade": trade, "area": area},
                  lambda: tools.find_contractor(conn, trade, area))
    estimate = _tool(conn, request_id, "estimate_cost", {"category": category, "urgency": urgency},
                     lambda: tools.estimate_cost(category, urgency))

    contractor = found["contractor"]
    sla_hours: Optional[int] = SLA_HOURS[urgency]
    message_address = req["property_address"]

    if fault:
        if fault == "wrong_trade":
            row = conn.execute("SELECT * FROM contractors WHERE trade != ? ORDER BY id LIMIT 1", (trade,)).fetchone()
            contractor = dict(row)
            detail = f"Swapped contractor for {contractor['name']} ({contractor['trade']})"
        elif fault == "missing_contractor":
            contractor = None
            detail = "Removed the contractor from the work order"
        elif fault == "wrong_address":
            message_address = "1 Wrong Street, Nowhere 0000"
            detail = f"Contractor message will use the wrong address '{message_address}'"
        elif fault == "missing_sla":
            sla_hours = None
            detail = "Removed the urgency SLA from the work order"
        else:
            raise WorkflowError(f"Unknown fault type '{fault}'", 400)
        log_event(conn, request_id, "fault_injection", f"DEMO FAULT INJECTED ({fault}): {detail}",
                  actor="demo-fault-injector", data={"fault": fault})

    wo_inputs = {
        "request_id": request_id,
        "contractor_id": contractor["id"] if contractor else None,
        "trade": trade,
        "category": category,
        "urgency": urgency,
        "sla_hours": sla_hours,
        "property_address": req["property_address"],
        "scope": f"{category.capitalize()} repair: {req['description'][:300]}",
        "cost_low": estimate["low"],
        "cost_high": estimate["high"],
    }
    work_order = _tool(conn, request_id, "create_work_order", wo_inputs,
                       lambda: tools.create_work_order(conn, **wo_inputs))
    transition(conn, request_id, "work_order_created", actor="agent",
               reason=f"Work order #{work_order['id']} created")

    message = _tool(
        conn, request_id, "draft_contractor_message",
        {"work_order_id": work_order["id"], "contractor_id": wo_inputs["contractor_id"], "address": message_address},
        lambda: tools.draft_contractor_message(conn, request=req, work_order=work_order,
                                               contractor=contractor, address=message_address),
    )
    transition(conn, request_id, "contractor_notified", actor="agent",
               reason=f"Contractor message #{message['id']} drafted (DRAFT - NOT SENT; nothing was sent)")

    result = verify_work_order(conn, request_id, work_order["id"])
    conn.execute(
        "INSERT INTO verifications (request_id, work_order_id, passed, checks, created_at) VALUES (?, ?, ?, ?, ?)",
        (request_id, work_order["id"], int(result["passed"]), json.dumps(result["checks"]), now_iso()),
    )
    log_event(conn, request_id, "verification",
              "Verification passed" if result["passed"] else f"Verification FAILED: {'; '.join(result['reasons'])}",
              actor="verifier", data=result)
    if result["passed"]:
        transition(conn, request_id, "verified", actor="verifier", reason="All verification checks passed")
    else:
        conn.execute("UPDATE work_orders SET status = 'failed_verification' WHERE id = ?", (work_order["id"],))
        transition(conn, request_id, "verification_failed", actor="verifier",
                   reason=f"{len(result['reasons'])} verification check(s) failed")


# --------------------------------------------------------------------------------------
# Public API (each call is one transaction)
# --------------------------------------------------------------------------------------


def create_request(conn: sqlite3.Connection, data: RequestCreate, llm: Optional[LLMClient] = None) -> int:
    def run() -> int:
        cur = conn.execute(
            """INSERT INTO requests (tenant_name, property_address, description, photo_note, contact_preference,
                   status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'received', ?, ?)""",
            (data.tenant_name, data.property_address, data.description, data.photo_note or None,
             data.contact_preference, now_iso(), now_iso()),
        )
        rid = cur.lastrowid
        log_event(conn, rid, "intake", "Maintenance request received", actor=f"tenant: {data.tenant_name}",
                  data=data.model_dump(), to_state="received")
        _triage_step(conn, rid, llm)
        return rid

    return _run_atomically(conn, None, "create_request", run)


def provide_info(conn: sqlite3.Connection, request_id: int, additional_info: str,
                 actor: Optional[str] = None, llm: Optional[LLMClient] = None) -> None:
    req = _get_request(conn, request_id)
    _require_status(req, "needs_info", "add information")
    who = actor or "Property manager (tenant reply)"

    def run() -> None:
        description = f"{req['description']}\nAdditional info: {additional_info}"
        conn.execute("UPDATE requests SET description = ?, updated_at = ? WHERE id = ?",
                     (description, now_iso(), request_id))
        log_event(conn, request_id, "tenant_reply", "Additional information added", actor=who,
                  data={"additional_info": additional_info})
        transition(conn, request_id, "received", actor=who, reason="More information received; re-triaging")
        _triage_step(conn, request_id, llm)

    _run_atomically(conn, request_id, "provide_info", run)


def handle_escalation(conn: sqlite3.Connection, request_id: int, handler: str, note: str,
                      safety_confirmed: bool) -> None:
    req = _get_request(conn, request_id)
    _require_status(req, "escalated", "handle escalation")
    if not safety_confirmed:
        raise WorkflowError("You must confirm the emergency has been handled by a human (safety_confirmed=true).", 400)

    def run() -> None:
        conn.execute(
            "INSERT INTO approvals (request_id, decision, actor, note, created_at) VALUES (?, 'safety_handled', ?, ?, ?)",
            (request_id, handler, note, now_iso()),
        )
        log_event(conn, request_id, "human_action", f"Safety escalation handled by {handler}", actor=handler,
                  data={"note": note, "safety_confirmed": True})
        transition(conn, request_id, "awaiting_approval", actor=handler,
                   reason="Human confirmed the emergency is handled; repair now needs approval")

    _run_atomically(conn, request_id, "handle_escalation", run)


def approve(conn: sqlite3.Connection, request_id: int, approver: str, note: Optional[str] = None,
            inject_fault: Optional[str] = None) -> None:
    req = _get_request(conn, request_id)
    _require_status(req, "awaiting_approval", "approve")

    def run() -> None:
        conn.execute(
            "INSERT INTO approvals (request_id, decision, actor, note, created_at) VALUES (?, 'approved', ?, ?, ?)",
            (request_id, approver, note, now_iso()),
        )
        log_event(conn, request_id, "human_action", f"Approved by {approver}", actor=approver,
                  data={"note": note, "inject_fault": inject_fault})
        transition(conn, request_id, "approved", actor=approver, reason=f"Approved by {approver}")
        _execute(conn, request_id, inject_fault)

    _run_atomically(conn, request_id, "approve", run)


def reject(conn: sqlite3.Connection, request_id: int, approver: str, note: str) -> None:
    req = _get_request(conn, request_id)
    _require_status(req, "awaiting_approval", "reject")

    def run() -> None:
        conn.execute(
            "INSERT INTO approvals (request_id, decision, actor, note, created_at) VALUES (?, 'rejected', ?, ?, ?)",
            (request_id, approver, note, now_iso()),
        )
        log_event(conn, request_id, "human_action", f"Rejected by {approver}", actor=approver, data={"note": note})
        transition(conn, request_id, "rejected", actor=approver, reason=f"Rejected by {approver}: {note}")

    _run_atomically(conn, request_id, "reject", run)


def retry(conn: sqlite3.Connection, request_id: int, actor: str) -> None:
    req = _get_request(conn, request_id)
    _require_status(req, "verification_failed", "retry")

    def run() -> None:
        conn.execute(
            "UPDATE work_orders SET status = 'void' WHERE request_id = ? AND status = 'failed_verification'",
            (request_id,),
        )
        log_event(conn, request_id, "human_action", f"Retry requested by {actor}; failed work order voided",
                  actor=actor)
        transition(conn, request_id, "approved", actor=actor,
                   reason="Re-running execution under the existing approval (no fault injection)")
        _execute(conn, request_id, None)

    _run_atomically(conn, request_id, "retry", run)


# --------------------------------------------------------------------------------------
# Read models
# --------------------------------------------------------------------------------------


def get_request_detail(conn: sqlite3.Connection, request_id: int) -> dict:
    req = _get_request(conn, request_id)
    triage = _latest_triage(conn, request_id)
    work_orders = []
    for row in conn.execute(
        """SELECT w.*, c.name AS contractor_name, c.trade AS contractor_trade, c.region AS contractor_region,
                  c.phone AS contractor_phone, c.email AS contractor_email, c.rating AS contractor_rating
           FROM work_orders w LEFT JOIN contractors c ON c.id = w.contractor_id
           WHERE w.request_id = ? ORDER BY w.id DESC""",
        (request_id,),
    ):
        work_orders.append(dict(row))
    verification = row_to_dict(
        conn.execute("SELECT * FROM verifications WHERE request_id = ? ORDER BY id DESC LIMIT 1",
                     (request_id,)).fetchone(),
        ("checks",),
    )
    if verification:
        verification["passed"] = bool(verification["passed"])
    return {
        "request": req,
        "triage": triage,
        "approvals": [dict(r) for r in conn.execute(
            "SELECT * FROM approvals WHERE request_id = ? ORDER BY id", (request_id,))],
        "work_order": work_orders[0] if work_orders else None,
        "work_orders": work_orders,
        "messages": [dict(r) for r in conn.execute(
            "SELECT * FROM messages WHERE request_id = ? ORDER BY id DESC", (request_id,))],
        "verification": verification,
        "events": [row_to_dict(r, ("data",)) for r in conn.execute(
            "SELECT * FROM events WHERE request_id = ? ORDER BY id", (request_id,))],
        "allowed_actions": ALLOWED_ACTIONS.get(req["status"], []),
    }


def list_requests(conn: sqlite3.Connection, *, status: Optional[str] = None, urgency: Optional[str] = None,
                  category: Optional[str] = None, q: Optional[str] = None) -> dict:
    sql = """SELECT r.id, r.tenant_name, r.property_address, r.description, r.status, r.created_at, r.updated_at,
                    t.category, t.urgency, t.trade, t.confidence, t.engine
             FROM requests r
             LEFT JOIN triages t ON t.id = (SELECT MAX(id) FROM triages WHERE request_id = r.id)
             WHERE 1 = 1"""
    params: list = []
    if status:
        sql += " AND r.status = ?"
        params.append(status)
    if urgency:
        sql += " AND t.urgency = ?"
        params.append(urgency)
    if category:
        sql += " AND t.category = ?"
        params.append(category)
    if q:
        sql += " AND (r.tenant_name LIKE ? OR r.property_address LIKE ? OR r.description LIKE ?)"
        like = f"%{q}%"
        params += [like, like, like]
    sql += " ORDER BY r.id DESC"
    items = [dict(r) for r in conn.execute(sql, params)]
    counts = {row["status"]: row["n"] for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM requests GROUP BY status")}
    return {"items": items, "counts": counts, "total": sum(counts.values())}
