import pytest

from app import tools, workflow
from app.db import connect


def status_of(conn, rid):
    return conn.execute("SELECT status FROM requests WHERE id = ?", (rid,)).fetchone()["status"]


def transitions_of(conn, rid):
    return [
        (r["from_state"], r["to_state"])
        for r in conn.execute(
            "SELECT from_state, to_state FROM events WHERE request_id = ? AND kind = 'state_transition' ORDER BY id",
            (rid,),
        )
    ]


def work_order_count(conn, rid):
    return conn.execute("SELECT COUNT(*) FROM work_orders WHERE request_id = ?", (rid,)).fetchone()[0]


# ----------------------------------------------------------------- state machine


def test_invalid_transition_is_rejected(conn, make_request):
    rid = make_request("dripping_tap")
    with pytest.raises(workflow.WorkflowError, match="Invalid transition"):
        with conn:
            workflow.transition(conn, rid, "verified", actor="test", reason="skip ahead")
    assert status_of(conn, rid) == "awaiting_approval"


def test_every_transition_is_logged_in_order(conn, make_request):
    rid = make_request("dripping_tap")
    workflow.approve(conn, rid, "Pat Manager", "Go ahead")
    assert transitions_of(conn, rid) == [
        ("received", "triaged"),
        ("triaged", "awaiting_approval"),
        ("awaiting_approval", "approved"),
        ("approved", "work_order_created"),
        ("work_order_created", "contractor_notified"),
        ("contractor_notified", "verified"),
    ]
    actors = [r["actor"] for r in conn.execute(
        "SELECT actor FROM events WHERE request_id = ? AND to_state = 'approved'", (rid,))]
    assert actors == ["Pat Manager"]
    tools_called = [r["summary"] for r in conn.execute(
        "SELECT summary FROM events WHERE request_id = ? AND kind = 'tool_call' ORDER BY id", (rid,))]
    assert tools_called == ["Tool find_contractor", "Tool estimate_cost", "Tool create_work_order",
                            "Tool draft_contractor_message"]


# ----------------------------------------------------------------- safety escalation


def test_gas_is_escalated_and_cannot_be_approved_without_human_handling(conn, make_request):
    rid = make_request("gas_smell")
    assert status_of(conn, rid) == "escalated"
    draft = conn.execute("SELECT * FROM messages WHERE request_id = ?", (rid,)).fetchone()
    assert draft["kind"] == "tenant_safety_instructions"
    assert draft["body"].startswith("DRAFT - NOT SENT")
    assert "111" in draft["body"]

    with pytest.raises(workflow.WorkflowError, match="safety escalation"):
        workflow.approve(conn, rid, "Pat Manager")
    with pytest.raises(workflow.WorkflowError):
        workflow.reject(conn, rid, "Pat Manager", "not now")
    assert status_of(conn, rid) == "escalated"
    assert work_order_count(conn, rid) == 0

    with pytest.raises(workflow.WorkflowError) as exc:
        workflow.handle_escalation(conn, rid, "Pat Manager", "Called tenant", safety_confirmed=False)
    assert exc.value.status_code == 400
    assert status_of(conn, rid) == "escalated"


def test_gas_after_human_handling_can_be_approved(conn, make_request):
    rid = make_request("gas_smell")
    workflow.handle_escalation(conn, rid, "Pat Manager", "Tenant evacuated; gas supplier attended", True)
    assert status_of(conn, rid) == "awaiting_approval"
    workflow.approve(conn, rid, "Pat Manager")
    assert status_of(conn, rid) == "verified"
    wo = conn.execute(
        "SELECT w.*, c.trade AS ctrade FROM work_orders w JOIN contractors c ON c.id = w.contractor_id "
        "WHERE request_id = ?", (rid,)).fetchone()
    assert wo["ctrade"] == "gasfitter" and wo["sla_hours"] == 4


# ----------------------------------------------------------------- needs info


def test_vague_goes_to_needs_info_with_draft_question(conn, make_request):
    rid = make_request("vague")
    assert status_of(conn, rid) == "needs_info"
    msg = conn.execute("SELECT * FROM messages WHERE request_id = ?", (rid,)).fetchone()
    assert msg["kind"] == "tenant_clarification"
    assert msg["status"] == "DRAFT - NOT SENT"
    assert "?" in msg["body"]
    with pytest.raises(workflow.WorkflowError):
        workflow.approve(conn, rid, "Pat Manager")


def test_providing_info_re_triages(conn, make_request):
    rid = make_request("vague")
    workflow.provide_info(conn, rid, "The oven in the kitchen won't turn on at all since yesterday.")
    assert status_of(conn, rid) == "awaiting_approval"
    triages = conn.execute("SELECT category FROM triages WHERE request_id = ? ORDER BY id", (rid,)).fetchall()
    assert [t["category"] for t in triages] == ["other", "appliance"]
    assert ("needs_info", "received") in transitions_of(conn, rid)


# ----------------------------------------------------------------- approval gate


def test_work_order_requires_approval(conn, make_request):
    rid = make_request("dripping_tap")
    assert status_of(conn, rid) == "awaiting_approval"
    assert work_order_count(conn, rid) == 0
    with pytest.raises(tools.ApprovalRequiredError):
        tools.create_work_order(
            conn, request_id=rid, contractor_id=1, trade="plumber", category="plumbing", urgency="routine",
            sla_hours=120, property_address="x", scope="x", cost_low=1, cost_high=2,
        )
    assert work_order_count(conn, rid) == 0


def test_rejection_path(conn, make_request):
    rid = make_request("blocked_drain")
    workflow.reject(conn, rid, "Pat Manager", "Tenant-caused damage, tenant to arrange")
    assert status_of(conn, rid) == "rejected"
    assert work_order_count(conn, rid) == 0
    approval = conn.execute("SELECT * FROM approvals WHERE request_id = ?", (rid,)).fetchone()
    assert (approval["decision"], approval["actor"]) == ("rejected", "Pat Manager")
    with pytest.raises(workflow.WorkflowError):
        workflow.approve(conn, rid, "Pat Manager")


# ----------------------------------------------------------------- verification


def test_verification_passes_for_good_work_order(conn, make_request):
    rid = make_request("broken_heater")
    workflow.approve(conn, rid, "Pat Manager")
    d = workflow.get_request_detail(conn, rid)
    assert d["request"]["status"] == "verified"
    assert d["verification"]["passed"] is True
    assert all(c["passed"] for c in d["verification"]["checks"])
    assert d["work_order"]["contractor_trade"] == "heating_technician"
    assert d["work_order"]["contractor_region"] == "Wellington"
    assert d["work_order"]["sla_hours"] == 24
    contractor_msg = next(m for m in d["messages"] if m["kind"] == "contractor_work_order")
    assert contractor_msg["body"].startswith("DRAFT - NOT SENT")
    assert "7/22 Cuba Street, Te Aro, Wellington 6011" in contractor_msg["body"]


@pytest.mark.parametrize(
    "fault, failing_check",
    [
        ("wrong_trade", "trade_matches_category"),
        ("missing_contractor", "contractor_assigned"),
        ("wrong_address", "message_references_address"),
        ("missing_sla", "sla_set"),
    ],
)
def test_injected_fault_fails_verification(conn, make_request, fault, failing_check):
    rid = make_request("dripping_tap")
    workflow.approve(conn, rid, "Pat Manager", inject_fault=fault)
    d = workflow.get_request_detail(conn, rid)
    assert d["request"]["status"] == "verification_failed"
    failed = [c["name"] for c in d["verification"]["checks"] if not c["passed"]]
    assert failing_check in failed
    assert any(e["kind"] == "fault_injection" for e in d["events"])


def test_retry_after_failed_verification(conn, make_request):
    rid = make_request("dripping_tap")
    workflow.approve(conn, rid, "Pat Manager", inject_fault="wrong_trade")
    workflow.retry(conn, rid, "Pat Manager")
    d = workflow.get_request_detail(conn, rid)
    assert d["request"]["status"] == "verified"
    assert [w["status"] for w in d["work_orders"]] == ["open", "void"]


# ----------------------------------------------------------------- persistence


def test_state_and_audit_trail_persist_across_connections(db_path, conn, make_request):
    rid = make_request("dripping_tap")
    workflow.approve(conn, rid, "Pat Manager", "Approved for persistence test")
    events_before = conn.execute("SELECT COUNT(*) FROM events WHERE request_id = ?", (rid,)).fetchone()[0]
    conn.close()

    fresh = connect(db_path)
    try:
        d = workflow.get_request_detail(fresh, rid)
        assert d["request"]["status"] == "verified"
        assert len(d["events"]) == events_before > 5
        assert d["approvals"][0]["actor"] == "Pat Manager"
        assert d["work_order"] is not None and d["verification"]["passed"] is True
    finally:
        fresh.close()
