"""Pydantic schemas: API inputs and the strict triage output contract."""

from __future__ import annotations

from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Category = Literal["plumbing", "electrical", "gas", "heating", "appliance", "structural", "other"]
Urgency = Literal["emergency", "urgent", "routine"]
Trade = Literal[
    "plumber",
    "electrician",
    "gasfitter",
    "heating_technician",
    "appliance_technician",
    "builder",
    "handyman",
]
ContactPreference = Literal["email", "phone", "sms"]
FaultType = Literal["wrong_trade", "missing_contractor", "wrong_address", "missing_sla"]

CATEGORIES: tuple[str, ...] = Category.__args__
URGENCIES: tuple[str, ...] = Urgency.__args__
FAULT_TYPES: tuple[str, ...] = FaultType.__args__

CATEGORY_TRADE: dict[str, str] = {
    "plumbing": "plumber",
    "electrical": "electrician",
    "gas": "gasfitter",
    "heating": "heating_technician",
    "appliance": "appliance_technician",
    "structural": "builder",
    "other": "handyman",
}

SLA_HOURS: dict[str, int] = {"emergency": 4, "urgent": 24, "routine": 120}

PersonName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=100)]
ShortNote = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)]


class TriageOutput(BaseModel):
    """The only shape a triage (rule-based or LLM) is allowed to produce."""

    model_config = ConfigDict(extra="forbid", strict=True)

    category: Category
    urgency: Urgency
    trade: Trade
    missing_info: list[Annotated[str, StringConstraints(min_length=1, max_length=300)]] = Field(max_length=10)
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def trade_must_match_category(self) -> "TriageOutput":
        expected = CATEGORY_TRADE[self.category]
        if self.trade != expected:
            raise ValueError(f"trade '{self.trade}' does not match category '{self.category}' (expected '{expected}')")
        return self


class RequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_name: PersonName
    property_address: Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=200)]
    description: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=2000)]
    photo_note: Optional[Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]] = None
    contact_preference: ContactPreference = "email"


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approver: PersonName
    note: Optional[ShortNote] = None
    inject_fault: Optional[FaultType] = None


class RejectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approver: PersonName
    note: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=1000)]


class EscalationHandleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    handler: PersonName
    note: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=1000)]
    safety_confirmed: bool


class ProvideInfoBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    additional_info: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=2000)]
    actor: Optional[PersonName] = None


class RetryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: PersonName
