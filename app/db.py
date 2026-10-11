"""SQLite persistence (stdlib sqlite3). Every request, triage, approval, tool call and
state transition is written here so the audit trail survives restarts."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "keyhook.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_name         TEXT NOT NULL,
    property_address    TEXT NOT NULL,
    description         TEXT NOT NULL,
    photo_note          TEXT,
    contact_preference  TEXT NOT NULL,
    status              TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS triages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id       INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    engine           TEXT NOT NULL,
    model            TEXT,
    category         TEXT NOT NULL,
    urgency          TEXT NOT NULL,
    trade            TEXT NOT NULL,
    missing_info     TEXT NOT NULL,
    confidence       REAL NOT NULL,
    reasoning        TEXT NOT NULL,
    safety_flags     TEXT NOT NULL,
    route            TEXT NOT NULL,
    fallback_reason  TEXT,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    decision    TEXT NOT NULL,
    actor       TEXT NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contractors (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL,
    trade   TEXT NOT NULL,
    region  TEXT NOT NULL,
    phone   TEXT NOT NULL,
    email   TEXT NOT NULL,
    rating  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS work_orders (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id        INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    contractor_id     INTEGER REFERENCES contractors(id),
    trade             TEXT NOT NULL,
    category          TEXT NOT NULL,
    urgency           TEXT NOT NULL,
    sla_hours         INTEGER,
    sla_due_at        TEXT,
    property_address  TEXT NOT NULL,
    scope             TEXT NOT NULL,
    cost_low          INTEGER,
    cost_high         INTEGER,
    status            TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id      INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    work_order_id   INTEGER REFERENCES work_orders(id),
    kind            TEXT NOT NULL,
    recipient_type  TEXT NOT NULL,
    recipient       TEXT NOT NULL,
    channel         TEXT NOT NULL,
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL,
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verifications (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id     INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    work_order_id  INTEGER REFERENCES work_orders(id),
    passed         INTEGER NOT NULL,
    checks         TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    kind        TEXT NOT NULL,
    summary     TEXT NOT NULL,
    from_state  TEXT,
    to_state    TEXT,
    data        TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_request ON events(request_id, id);
CREATE INDEX IF NOT EXISTS idx_triages_request ON triages(request_id, id);
CREATE INDEX IF NOT EXISTS idx_work_orders_request ON work_orders(request_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_request ON messages(request_id, id);
"""

# Fictional, seeded contractor directory used by the mock find_contractor tool.
CONTRACTORS = [
    # name, trade, region, phone, email, rating
    ("Harbour City Plumbing", "plumber", "Auckland", "09 555 0101", "jobs@harbourplumbing.example.co.nz", 4.8),
    ("Mt Eden Pipes & Drains", "plumber", "Auckland", "09 555 0102", "office@mtedenpipes.example.co.nz", 4.5),
    ("Sparkright Electrical", "electrician", "Auckland", "09 555 0103", "admin@sparkright.example.co.nz", 4.7),
    ("Auckland Gas Services", "gasfitter", "Auckland", "09 555 0104", "dispatch@aklgas.example.co.nz", 4.6),
    ("WarmHome Heat Pumps", "heating_technician", "Auckland", "09 555 0105", "service@warmhome.example.co.nz", 4.6),
    ("FixIt Appliance Repairs", "appliance_technician", "Auckland", "09 555 0106", "bookings@fixitappliance.example.co.nz", 4.4),
    ("Kauri Building Co", "builder", "Auckland", "09 555 0107", "build@kauribuilding.example.co.nz", 4.5),
    ("Handy Andy Auckland", "handyman", "Auckland", "09 555 0108", "andy@handyandy.example.co.nz", 4.3),
    ("Capital Plumbing", "plumber", "Wellington", "04 555 0201", "jobs@capitalplumbing.example.co.nz", 4.6),
    ("Windy City Electrical", "electrician", "Wellington", "04 555 0202", "office@windycityelec.example.co.nz", 4.5),
    ("Wellington Gasfitters", "gasfitter", "Wellington", "04 555 0203", "jobs@wgtngas.example.co.nz", 4.7),
    ("Te Aro Heating Solutions", "heating_technician", "Wellington", "04 555 0204", "hello@tearoheating.example.co.nz", 4.8),
    ("Capital Appliance Care", "appliance_technician", "Wellington", "04 555 0205", "care@capitalappliance.example.co.nz", 4.2),
    ("Harbour Handyman Services", "handyman", "Wellington", "04 555 0206", "help@harbourhandyman.example.co.nz", 4.4),
    ("Garden City Plumbers", "plumber", "Christchurch", "03 555 0301", "jobs@gardencityplumbers.example.co.nz", 4.5),
    ("Canterbury Sparks", "electrician", "Christchurch", "03 555 0302", "office@cantysparks.example.co.nz", 4.6),
    ("Riccarton Gas & Heating", "gasfitter", "Christchurch", "03 555 0303", "emergency@riccartongas.example.co.nz", 4.9),
    ("Southern Heat Pumps", "heating_technician", "Christchurch", "03 555 0304", "service@southernheat.example.co.nz", 4.4),
    ("Avon Builders", "builder", "Christchurch", "03 555 0305", "build@avonbuilders.example.co.nz", 4.3),
    ("Waikato Plumbing & Drainage", "plumber", "Hamilton", "07 555 0401", "jobs@waikatoplumbing.example.co.nz", 4.7),
    ("Hamilton Electrical", "electrician", "Hamilton", "07 555 0402", "admin@hamiltonelectrical.example.co.nz", 4.4),
    ("Waikato Gas Co", "gasfitter", "Hamilton", "07 555 0403", "jobs@waikatogas.example.co.nz", 4.5),
    ("Octagon Plumbing", "plumber", "Dunedin", "03 555 0501", "jobs@octagonplumbing.example.co.nz", 4.6),
    ("Otago Electrical", "electrician", "Dunedin", "03 555 0502", "office@otagoelectrical.example.co.nz", 4.5),
    ("Dunedin Gasfitting", "gasfitter", "Dunedin", "03 555 0503", "jobs@dunedingas.example.co.nz", 4.4),
    ("Southern Handyman Dunedin", "handyman", "Dunedin", "03 555 0504", "jobs@southernhandyman.example.co.nz", 4.2),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# On Vercel (serverless) the project directory is read-only and nothing persists between
# instances, so the database lives in /tmp and is re-seeded with the fictional sample data
# whenever a fresh instance starts (see ``ensure_initialized`` in app/main.py).
SERVERLESS_DB_PATH = Path("/tmp") / "keyhook.db"


def is_serverless() -> bool:
    return bool(os.environ.get("VERCEL"))


def resolve_db_path(path: str | os.PathLike | None = None) -> str:
    if path or os.environ.get("KEYHOOK_DB_PATH"):
        return str(path or os.environ.get("KEYHOOK_DB_PATH"))
    return str(SERVERLESS_DB_PATH if is_serverless() else DEFAULT_DB_PATH)


def connect(path: str | os.PathLike | None = None) -> sqlite3.Connection:
    db_path = resolve_db_path(path)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    (count,) = conn.execute("SELECT COUNT(*) FROM contractors").fetchone()
    if count == 0:
        conn.executemany(
            "INSERT INTO contractors (name, trade, region, phone, email, rating) VALUES (?, ?, ?, ?, ?, ?)",
            CONTRACTORS,
        )
    conn.commit()


def clear_request_data(conn: sqlite3.Connection) -> None:
    with conn:
        for table in ("events", "verifications", "messages", "work_orders", "approvals", "triages", "requests"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute(
            "DELETE FROM sqlite_sequence WHERE name IN "
            "('events','verifications','messages','work_orders','approvals','triages','requests')"
        )


def row_to_dict(row: sqlite3.Row | None, json_fields: tuple[str, ...] = ()) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for f in json_fields:
        if d.get(f) is not None:
            d[f] = json.loads(d[f])
    return d
