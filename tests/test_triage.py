import json

import pytest

from app.seed_data import sample_payload
from app.triage import LLMOutputError, detect_safety, parse_llm_output, triage_request

from .conftest import FakeLLM


def _triage(key, llm=None):
    s = sample_payload(key)
    return triage_request(s["description"], s["photo_note"], llm)


# ----------------------------------------------------------------- seed classification


def test_dripping_tap_is_routine_plumbing():
    r = _triage("dripping_tap")
    assert (r.output.category, r.output.urgency, r.output.trade) == ("plumbing", "routine", "plumber")
    assert r.route == "awaiting_approval"
    assert r.output.confidence >= 0.8
    assert r.engine == "rules" and not r.safety_flags


def test_broken_heater_is_urgent_heating():
    r = _triage("broken_heater")
    assert (r.output.category, r.output.urgency, r.output.trade) == ("heating", "urgent", "heating_technician")
    assert r.route == "awaiting_approval"
    assert any("vulnerable" in m for m in r.output.missing_info)


def test_gas_smell_is_emergency_escalation():
    r = _triage("gas_smell")
    assert (r.output.category, r.output.urgency, r.output.trade) == ("gas", "emergency", "gasfitter")
    assert r.route == "escalated"
    assert [f.code for f in r.safety_flags] == ["gas_smell"]


def test_blocked_drain_is_plumbing():
    r = _triage("blocked_drain")
    assert (r.output.category, r.output.trade) == ("plumbing", "plumber")
    assert r.route == "awaiting_approval"


def test_vague_request_needs_info():
    r = _triage("vague")
    assert r.output.category == "other"
    assert r.output.confidence < 0.5
    assert r.route == "needs_info"
    assert len(r.output.missing_info) >= 2


# ----------------------------------------------------------------- safety rules


@pytest.mark.parametrize(
    "text, code, category",
    [
        ("The power point in the bedroom is sparking when I plug things in.", "electrical_hazard", "electrical"),
        ("There are exposed wires hanging from the hallway light fitting.", "electrical_hazard", "electrical"),
        ("A pipe burst under the kitchen sink and the floor is flooding.", "flooding", "plumbing"),
        ("Our heat pump isn't working and we have a newborn baby at home.", "no_heat_vulnerable", "heating"),
    ],
)
def test_safety_rules_force_emergency_escalation(text, code, category):
    r = triage_request(text)
    assert code in [f.code for f in r.safety_flags]
    assert r.output.urgency == "emergency"
    assert r.output.category == category
    assert r.route == "escalated"


def test_no_heat_without_vulnerable_occupant_is_not_emergency():
    r = triage_request("The heater in the bedroom is not working.")
    assert r.output.urgency == "urgent"
    assert not detect_safety("The heater in the bedroom is not working.")


def test_safety_rules_override_the_model():
    llm = FakeLLM({
        "category": "appliance", "urgency": "routine", "trade": "appliance_technician",
        "missing_info": [], "confidence": 0.9, "reasoning": "Stove issue.",
    })
    r = _triage("gas_smell", llm)
    assert r.engine == "llm"
    assert (r.output.category, r.output.urgency, r.output.trade) == ("gas", "emergency", "gasfitter")
    assert r.route == "escalated"
    assert len(r.overrides) == 2


# ----------------------------------------------------------------- LLM schema validation


VALID_LLM = {
    "category": "plumbing", "urgency": "routine", "trade": "plumber",
    "missing_info": [], "confidence": 0.88, "reasoning": "Dripping kitchen tap.",
}


def test_valid_llm_output_is_used():
    llm = FakeLLM(VALID_LLM)
    r = _triage("dripping_tap", llm)
    assert r.engine == "llm" and r.model == "fake-model"
    assert r.fallback_reason is None
    assert r.output.confidence == 0.88


@pytest.mark.parametrize(
    "bad",
    [
        "this is not json",
        "",
        json.dumps({**VALID_LLM, "category": "wizardry"}),
        json.dumps({**VALID_LLM, "urgency": "whenever"}),
        json.dumps({**VALID_LLM, "confidence": 1.7}),
        json.dumps({**VALID_LLM, "confidence": "high"}),
        json.dumps({**VALID_LLM, "trade": "electrician"}),
        json.dumps({**VALID_LLM, "extra_field": True}),
        json.dumps({k: v for k, v in VALID_LLM.items() if k != "reasoning"}),
        json.dumps([VALID_LLM]),
    ],
)
def test_schema_rejects_bad_llm_output(bad):
    with pytest.raises(LLMOutputError):
        parse_llm_output(bad)


def test_bad_llm_output_falls_back_to_rules():
    llm = FakeLLM(json.dumps({**VALID_LLM, "category": "wizardry"}))
    r = _triage("dripping_tap", llm)
    assert llm.calls == 1
    assert r.engine == "rules" and r.model is None
    assert "schema validation" in r.fallback_reason
    assert r.output.category == "plumbing"


def test_llm_exception_falls_back_to_rules():
    r = _triage("broken_heater", FakeLLM(error=TimeoutError("model timed out")))
    assert r.engine == "rules"
    assert "LLM call failed" in r.fallback_reason
    assert r.output.category == "heating"


def test_fenced_json_is_accepted():
    out = parse_llm_output("```json\n" + json.dumps(VALID_LLM) + "\n```")
    assert out.category == "plumbing"
