"""Sample tenant requests used by ``python seed.py``, the demo reset button and the tests."""

from __future__ import annotations

import sqlite3
from typing import Optional

from .models import RequestCreate
from .triage import LLMClient
from .workflow import create_request

SAMPLE_REQUESTS: list[dict] = [
    {
        "key": "dripping_tap",
        "tenant_name": "Aroha Ngata",
        "property_address": "14 Kowhai Street, Mount Eden, Auckland 1024",
        "description": "The kitchen tap has been dripping constantly for about a week, even when it is fully turned off.",
        "photo_note": "Photo shows water dripping from the kitchen mixer tap spout.",
        "contact_preference": "email",
    },
    {
        "key": "broken_heater",
        "tenant_name": "James Wilson",
        "property_address": "7/22 Cuba Street, Te Aro, Wellington 6011",
        "description": "The heat pump in the lounge stopped working last night. It shows a flashing orange "
                       "light and won't blow warm air.",
        "photo_note": None,
        "contact_preference": "sms",
    },
    {
        "key": "gas_smell",
        "tenant_name": "Priya Sharma",
        "property_address": "3 Riccarton Road, Riccarton, Christchurch 8011",
        "description": "There is a strong smell of gas near the stove in the kitchen. It started about an hour ago.",
        "photo_note": None,
        "contact_preference": "phone",
    },
    {
        "key": "blocked_drain",
        "tenant_name": "Tom Henderson",
        "property_address": "88 Victoria Street, Hamilton Central, Hamilton 3204",
        "description": "The shower drain in the main bathroom is blocked. Water pools up and takes about "
                       "20 minutes to drain away.",
        "photo_note": "Photo of standing water in the shower tray.",
        "contact_preference": "email",
    },
    {
        "key": "vague",
        "tenant_name": "Sam Lee",
        "property_address": "45 George Street, Dunedin Central, Dunedin 9016",
        "description": "Something is broken.",
        "photo_note": None,
        "contact_preference": "email",
    },
]


def sample_payload(key: str) -> dict:
    for s in SAMPLE_REQUESTS:
        if s["key"] == key:
            return {k: v for k, v in s.items() if k != "key"}
    raise KeyError(key)


def seed_requests(conn: sqlite3.Connection, llm: Optional[LLMClient] = None) -> list[int]:
    return [create_request(conn, RequestCreate(**sample_payload(s["key"])), llm) for s in SAMPLE_REQUESTS]
