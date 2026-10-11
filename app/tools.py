"""Mock, deterministic agent tools. Nothing here contacts the outside world: contractor
lookups read a seeded table and every message is stored as "DRAFT - NOT SENT"."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from .db import now_iso, row_to_dict

DRAFT_MARKER = "DRAFT - NOT SENT"

REGIONS = ("Auckland", "Wellington", "Christchurch", "Hamilton", "Dunedin")
_SUBURB_REGION = {
    "ponsonby": "Auckland", "mount eden": "Auckland", "mt eden": "Auckland", "newmarket": "Auckland",
    "grey lynn": "Auckland", "remuera": "Auckland", "te aro": "Wellington", "kelburn": "Wellington",
    "newtown": "Wellington", "riccarton": "Christchurch", "addington": "Christchurch",
    "frankton": "Hamilton", "mosgiel": "Dunedin",
}

_COST_BASE = {
    "plumbing": (150, 450),
    "electrical": (180, 500),
    "gas": (200, 600),
    "heating": (200, 900),
    "appliance": (150, 600),
    "structural": (300, 2500),
    "other": (100, 400),
}
_URGENCY_MULTIPLIER = {"emergency": 1.5, "urgent": 1.2, "routine": 1.0}


class ApprovalRequiredError(Exception):
    """Raised if anything tries to create a work order without a recorded human approval."""


def extract_area(address: str) -> Optional[str]:
    lowered = address.lower()
    for region in REGIONS:
        if re.search(rf"\b{region.lower()}\b", lowered):
            return region
    for suburb, region in _SUBURB_REGION.items():
        if re.search(rf"\b{suburb}\b", lowered):
            return region
    return None


def find_contractor(conn: sqlite3.Connection, trade: str, area: Optional[str]) -> dict:
    candidates = [dict(r) for r in conn.execute("SELECT * FROM contractors WHERE trade = ?", (trade,))]
    in_area = [c for c in candidates if area and c["region"] == area]
    pool = in_area or candidates
    if not pool:
        return {"found": False, "contractor": None, "out_of_area": False, "candidates_considered": 0}
    best = sorted(pool, key=lambda c: (-c["rating"], c["id"]))[0]
    return {
        "found": True,
        "contractor": best,
        "out_of_area": not in_area,
        "candidates_considered": len(pool),
    }


def estimate_cost(category: str, urgency: str) -> dict:
    low, high = _COST_BASE.get(category, _COST_BASE["other"])
    mult = _URGENCY_MULTIPLIER[urgency]
    return {
        "currency": "NZD",
        "low": int(round(low * mult, -1)),
        "high": int(round(high * mult, -1)),
        "basis": f"Mock rate card for {category}, x{mult} for {urgency} call-out. Indicative only.",
    }


def create_work_order(
    conn: sqlite3.Connection,
    *,
    request_id: int,
    contractor_id: Optional[int],
    trade: str,
    category: str,
    urgency: str,
    sla_hours: Optional[int],
    property_address: str,
    scope: str,
    cost_low: int,
    cost_high: int,
) -> dict:
    status = conn.execute("SELECT status FROM requests WHERE id = ?", (request_id,)).fetchone()
    approved = conn.execute(
        "SELECT 1 FROM approvals WHERE request_id = ? AND decision = 'approved'", (request_id,)
    ).fetchone()
    if status is None or status["status"] != "approved" or approved is None:
        raise ApprovalRequiredError(
            "A property manager must approve this request before a work order can be created."
        )

    created = datetime.now(timezone.utc)
    due = (created + timedelta(hours=sla_hours)).isoformat(timespec="minutes") if sla_hours else None
    cur = conn.execute(
        """INSERT INTO work_orders (request_id, contractor_id, trade, category, urgency, sla_hours, sla_due_at,
               property_address, scope, cost_low, cost_high, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
        (request_id, contractor_id, trade, category, urgency, sla_hours, due, property_address, scope,
         cost_low, cost_high, now_iso()),
    )
    return row_to_dict(conn.execute("SELECT * FROM work_orders WHERE id = ?", (cur.lastrowid,)).fetchone())


def _store_message(conn, *, request_id, work_order_id, kind, recipient_type, recipient, channel, subject, body) -> dict:
    cur = conn.execute(
        """INSERT INTO messages (request_id, work_order_id, kind, recipient_type, recipient, channel, subject, body,
               status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (request_id, work_order_id, kind, recipient_type, recipient, channel, subject, body, DRAFT_MARKER, now_iso()),
    )
    return row_to_dict(conn.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone())


def draft_contractor_message(
    conn: sqlite3.Connection,
    *,
    request: dict,
    work_order: dict,
    contractor: Optional[dict],
    address: str,
) -> dict:
    if contractor:
        to_line = f"{contractor['name']} <{contractor['email']}> ({contractor['phone']})"
        greeting = f"Hi {contractor['name']} team,"
        recipient = contractor["name"]
    else:
        to_line = "(no contractor assigned)"
        greeting = "Hi,"
        recipient = "(no contractor assigned)"
    sla = (
        f"attend within {work_order['sla_hours']} hours (by {work_order['sla_due_at']} UTC)"
        if work_order["sla_hours"]
        else "attendance time not set"
    )
    urgency = work_order["urgency"].capitalize()
    subject = f"Work order #{work_order['id']} - {urgency} {work_order['category']} job at {address}"
    body = "\n".join(
        [
            DRAFT_MARKER,
            f"To: {to_line}",
            f"Subject: {subject}",
            "",
            greeting,
            "",
            "Please could you attend the following job:",
            "",
            f"Address: {address}",
            f"Work order: #{work_order['id']}",
            f"Trade required: {work_order['trade'].replace('_', ' ')}",
            f"Urgency: {urgency} - {sla}",
            f"Issue reported by tenant: \"{request['description']}\"",
            f"Tenant contact preference: {request['contact_preference']} (details shared once you accept)",
            f"Indicative budget: NZD ${work_order['cost_low']}-${work_order['cost_high']} "
            "(please call before exceeding)",
            "",
            "Please reply to accept or decline this job.",
            "",
            "Thanks,",
            "Property Management Team",
        ]
    )
    return _store_message(
        conn,
        request_id=request["id"],
        work_order_id=work_order["id"],
        kind="contractor_work_order",
        recipient_type="contractor",
        recipient=recipient,
        channel="email",
        subject=subject,
        body=body,
    )


def draft_tenant_clarification(conn: sqlite3.Connection, *, request: dict, missing_info: list[str]) -> dict:
    first = request["tenant_name"].split()[0]
    questions = missing_info or ["Could you describe the problem in a bit more detail?"]
    subject = "A few quick questions about your maintenance request"
    body = "\n".join(
        [
            DRAFT_MARKER,
            f"To: {request['tenant_name']} (via {request['contact_preference']})",
            f"Subject: {subject}",
            "",
            f"Hi {first},",
            "",
            f"Thanks for letting us know about the issue at {request['property_address']}. "
            "So we can send the right tradesperson, could you tell us:",
            "",
            *[f"- {q}" for q in questions],
            "",
            "A photo would also help if you can take one.",
            "If you smell gas, see sparks or exposed wiring, or water is flooding, call 111 and contact us immediately.",
            "",
            "Thanks,",
            "Property Management Team",
        ]
    )
    return _store_message(
        conn,
        request_id=request["id"],
        work_order_id=None,
        kind="tenant_clarification",
        recipient_type="tenant",
        recipient=request["tenant_name"],
        channel=request["contact_preference"],
        subject=subject,
        body=body,
    )


def draft_safety_instructions(conn: sqlite3.Connection, *, request: dict, flags: list[dict]) -> dict:
    first = request["tenant_name"].split()[0]
    labels = ", ".join(f["label"] for f in flags)
    steps: list[str] = []
    for f in flags:
        for step in f["instructions"]:
            if step not in steps:
                steps.append(step)
    subject = f"URGENT safety steps - {labels}"
    body = "\n".join(
        [
            DRAFT_MARKER,
            "For property manager review before sending. A human must handle this emergency.",
            f"To: {request['tenant_name']} (via {request['contact_preference']}; phone call recommended)",
            f"Subject: {subject}",
            "",
            f"Hi {first},",
            "",
            f"We've received your report about {request['property_address']} and treated it as a possible "
            f"emergency ({labels}). Please follow these steps now:",
            "",
            *[f"{i}. {s}" for i, s in enumerate(steps, 1)],
            "",
            "Property Management Team",
        ]
    )
    return _store_message(
        conn,
        request_id=request["id"],
        work_order_id=None,
        kind="tenant_safety_instructions",
        recipient_type="tenant",
        recipient=request["tenant_name"],
        channel=request["contact_preference"],
        subject=subject,
        body=body,
    )
