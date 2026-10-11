import pytest
from fastapi.testclient import TestClient

from app.main import DEMO_LABEL, create_app
from app.seed_data import sample_payload


def create(client, key):
    res = client.post("/api/requests", json=sample_payload(key))
    assert res.status_code == 201, res.text
    return res.json()


def test_serves_ui_and_static_assets(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "Maintenance Coordinator" in res.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200


def test_status_reports_demo_mode_without_keys(client):
    body = client.get("/api/status").json()
    assert body == {"mode": "demo", "provider": None, "model": None, "label": DEMO_LABEL}


def test_status_reports_llm_mode_when_key_set(client, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "test-key")
    body = client.get("/api/status").json()
    assert body["mode"] == "llm" and body["provider"] == "xai"


def test_create_and_get_request(client):
    body = create(client, "dripping_tap")
    assert body["request"]["status"] == "awaiting_approval"
    assert body["triage"]["category"] == "plumbing"
    assert body["allowed_actions"] == ["approve", "reject"]
    rid = body["request"]["id"]
    again = client.get(f"/api/requests/{rid}").json()
    assert again["request"]["id"] == rid
    assert len(again["events"]) >= 3


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"tenant_name": "A", "property_address": "1 Main St, Auckland", "description": "Tap drips"},
        {"tenant_name": "   ", "property_address": "1 Main St, Auckland", "description": "Tap drips"},
        {"tenant_name": "Ana", "property_address": "1", "description": "Tap drips"},
        {"tenant_name": "Ana", "property_address": "1 Main St, Auckland", "description": ""},
        {"tenant_name": "Ana", "property_address": "1 Main St, Auckland", "description": "x" * 2001},
        {"tenant_name": "Ana", "property_address": "1 Main St, Auckland", "description": "Tap drips",
         "contact_preference": "carrier pigeon"},
        {"tenant_name": "Ana", "property_address": "1 Main St, Auckland", "description": "Tap drips", "admin": True},
    ],
)
def test_invalid_intake_returns_422(client, payload):
    res = client.post("/api/requests", json=payload)
    assert res.status_code == 422
    body = res.json()
    assert body["detail"] == "Validation failed" and body["errors"]


def test_malformed_json_returns_422(client):
    res = client.post("/api/requests", content=b"{not json", headers={"Content-Type": "application/json"})
    assert res.status_code == 422


def test_unknown_request_and_bad_id(client):
    assert client.get("/api/requests/9999").status_code == 404
    assert client.get("/api/requests/not-a-number").status_code == 422
    assert client.post("/api/requests/9999/approve", json={"approver": "Pat Manager"}).status_code == 404


def test_bad_filter_returns_400(client):
    assert client.get("/api/requests?status=bogus").status_code == 400
    assert client.get("/api/requests?urgency=soonish").status_code == 400


def test_approval_endpoint_validation_and_state_errors(client):
    rid = create(client, "vague")["request"]["id"]
    assert client.post(f"/api/requests/{rid}/approve", json={}).status_code == 422
    assert client.post(f"/api/requests/{rid}/approve",
                       json={"approver": "Pat Manager", "inject_fault": "meteor"}).status_code == 422
    res = client.post(f"/api/requests/{rid}/approve", json={"approver": "Pat Manager"})
    assert res.status_code == 409
    assert "needs_info" in res.json()["detail"]


def test_reject_requires_note(client):
    rid = create(client, "blocked_drain")["request"]["id"]
    assert client.post(f"/api/requests/{rid}/reject", json={"approver": "Pat Manager"}).status_code == 422
    res = client.post(f"/api/requests/{rid}/reject", json={"approver": "Pat Manager", "note": "Duplicate job"})
    assert res.status_code == 200
    assert res.json()["request"]["status"] == "rejected"
    assert res.json()["work_order"] is None


def test_gas_escalation_flow_via_api(client):
    body = create(client, "gas_smell")
    rid = body["request"]["id"]
    assert body["request"]["status"] == "escalated"
    assert body["allowed_actions"] == ["handle_escalation"]

    blocked = client.post(f"/api/requests/{rid}/approve", json={"approver": "Pat Manager"})
    assert blocked.status_code == 409
    assert "safety escalation" in blocked.json()["detail"]

    unconfirmed = client.post(f"/api/requests/{rid}/escalation/handle",
                              json={"handler": "Pat Manager", "note": "Called", "safety_confirmed": False})
    assert unconfirmed.status_code == 400

    handled = client.post(f"/api/requests/{rid}/escalation/handle",
                          json={"handler": "Pat Manager", "note": "Tenant safe, supplier isolated gas",
                                "safety_confirmed": True})
    assert handled.json()["request"]["status"] == "awaiting_approval"
    done = client.post(f"/api/requests/{rid}/approve", json={"approver": "Pat Manager"}).json()
    assert done["request"]["status"] == "verified"


def test_full_flow_with_fault_and_retry_via_api(client):
    rid = create(client, "dripping_tap")["request"]["id"]
    res = client.post(f"/api/requests/{rid}/approve",
                      json={"approver": "Pat Manager", "inject_fault": "wrong_address"}).json()
    assert res["request"]["status"] == "verification_failed"
    assert res["verification"]["passed"] is False
    assert res["allowed_actions"] == ["retry"]
    fixed = client.post(f"/api/requests/{rid}/retry", json={"actor": "Pat Manager"}).json()
    assert fixed["request"]["status"] == "verified"


def test_needs_info_flow_via_api(client):
    rid = create(client, "vague")["request"]["id"]
    assert client.post(f"/api/requests/{rid}/info", json={"additional_info": ""}).status_code == 422
    res = client.post(f"/api/requests/{rid}/info",
                      json={"additional_info": "The kitchen tap is leaking under the sink."}).json()
    assert res["request"]["status"] == "awaiting_approval"
    assert res["triage"]["category"] == "plumbing"


def test_list_filters_and_counts(client):
    for key in ("dripping_tap", "gas_smell", "vague"):
        create(client, key)
    all_items = client.get("/api/requests").json()
    assert all_items["total"] == 3
    assert all_items["counts"] == {"awaiting_approval": 1, "escalated": 1, "needs_info": 1}
    escalated = client.get("/api/requests?status=escalated").json()["items"]
    assert [i["category"] for i in escalated] == ["gas"]
    assert len(client.get("/api/requests?urgency=emergency").json()["items"]) == 1
    assert len(client.get("/api/requests?q=Kowhai").json()["items"]) == 1


def test_demo_reset_seeds_five_samples(client):
    res = client.post("/api/demo/reset")
    assert res.status_code == 200 and res.json()["seeded"] == 5
    counts = client.get("/api/requests").json()["counts"]
    assert counts == {"awaiting_approval": 3, "escalated": 1, "needs_info": 1}


def test_data_survives_app_restart(db_path):
    app1 = create_app(db_path=db_path, auto_seed=False, llm_factory=lambda: None)
    with TestClient(app1) as c1:
        rid = create(c1, "broken_heater")["request"]["id"]
        c1.post(f"/api/requests/{rid}/approve", json={"approver": "Pat Manager"})

    app2 = create_app(db_path=db_path, auto_seed=False, llm_factory=lambda: None)
    with TestClient(app2) as c2:
        body = c2.get(f"/api/requests/{rid}").json()
        assert body["request"]["status"] == "verified"
        assert any(e["kind"] == "tool_call" for e in body["events"])
