"""Independent verification of a drafted work order before it is considered done."""

from __future__ import annotations

import re
import sqlite3

from .models import CATEGORY_TRADE, SLA_HOURS
from .tools import DRAFT_MARKER


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def verify_work_order(conn: sqlite3.Connection, request_id: int, work_order_id: int) -> dict:
    req = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    wo = conn.execute("SELECT * FROM work_orders WHERE id = ?", (work_order_id,)).fetchone()
    triage = conn.execute(
        "SELECT * FROM triages WHERE request_id = ? ORDER BY id DESC LIMIT 1", (request_id,)
    ).fetchone()
    contractor = (
        conn.execute("SELECT * FROM contractors WHERE id = ?", (wo["contractor_id"],)).fetchone()
        if wo and wo["contractor_id"]
        else None
    )
    message = conn.execute(
        "SELECT * FROM messages WHERE work_order_id = ? AND kind = 'contractor_work_order' ORDER BY id DESC LIMIT 1",
        (work_order_id,),
    ).fetchone()
    approval = conn.execute(
        "SELECT * FROM approvals WHERE request_id = ? AND decision = 'approved' ORDER BY id DESC LIMIT 1",
        (request_id,),
    ).fetchone()

    checks: list[dict] = []

    def check(name: str, label: str, passed: bool, ok: str, fail: str) -> None:
        checks.append({"name": name, "label": label, "passed": bool(passed), "detail": ok if passed else fail})

    check(
        "contractor_assigned",
        "Work order has a contractor",
        contractor is not None,
        f"Assigned to {contractor['name']}" if contractor else "",
        "No contractor is assigned to the work order.",
    )

    expected_trade = CATEGORY_TRADE[triage["category"]]
    actual = contractor["trade"] if contractor else None
    check(
        "trade_matches_category",
        "Contractor trade matches category",
        wo["trade"] == expected_trade and actual == expected_trade,
        f"{triage['category']} -> {expected_trade}",
        f"Category '{triage['category']}' needs a {expected_trade}, but the work order trade is "
        f"'{wo['trade']}' and the contractor's trade is '{actual or 'none'}'.",
    )

    expected_sla = SLA_HOURS[triage["urgency"]]
    check(
        "sla_set",
        "Urgency SLA is set",
        wo["urgency"] == triage["urgency"] and wo["sla_hours"] == expected_sla and bool(wo["sla_due_at"]),
        f"{wo['urgency']} -> {expected_sla}h, due {wo['sla_due_at']}",
        f"Urgency '{triage['urgency']}' requires a {expected_sla}h SLA, but the work order has "
        f"sla_hours={wo['sla_hours']} and due={wo['sla_due_at']}.",
    )

    check(
        "message_references_address",
        "Contractor message references the right address",
        message is not None and _norm(req["property_address"]) in _norm(message["body"]),
        f"Message includes '{req['property_address']}'",
        f"The contractor message does not mention the property address '{req['property_address']}'.",
    )

    check(
        "message_is_draft",
        "Contractor message is a draft (not sent)",
        message is not None and message["body"].startswith(DRAFT_MARKER) and message["status"] == DRAFT_MARKER,
        "Marked DRAFT - NOT SENT",
        "The contractor message is missing or not marked as a draft.",
    )

    check(
        "approval_recorded",
        "Human approval recorded before work order",
        approval is not None and approval["created_at"] <= wo["created_at"],
        f"Approved by {approval['actor']}" if approval else "",
        "No human approval was recorded before the work order was created.",
    )

    return {
        "passed": all(c["passed"] for c in checks),
        "checks": checks,
        "reasons": [c["detail"] for c in checks if not c["passed"]],
    }
