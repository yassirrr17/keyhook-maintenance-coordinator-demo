"""Seed the SQLite database with contractors and the sample tenant requests.

Usage:
    .venv\\Scripts\\python seed.py           # seed only if there are no requests yet
    .venv\\Scripts\\python seed.py --reset   # wipe all requests/logs and re-seed
"""

from __future__ import annotations

import argparse

from app.db import clear_request_data, connect, init_db, resolve_db_path
from app.seed_data import seed_requests
from app.triage import llm_client_from_env


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="delete existing requests and audit logs first")
    parser.add_argument("--db", default=None, help="database path (default: data/keyhook.db or KEYHOOK_DB_PATH)")
    args = parser.parse_args()

    conn = connect(args.db)
    try:
        init_db(conn)
        existing = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        if existing and not args.reset:
            print(f"Database already has {existing} request(s); nothing seeded. Use --reset to start fresh.")
            return
        if args.reset:
            clear_request_data(conn)
        ids = seed_requests(conn, llm_client_from_env())
        print(f"Seeded {len(ids)} sample requests into {resolve_db_path(args.db)}:")
        for row in conn.execute(
            "SELECT r.id, r.status, t.category, t.urgency, r.description FROM requests r "
            "JOIN triages t ON t.request_id = r.id ORDER BY r.id"
        ):
            print(f"  #{row['id']:<3} {row['status']:<18} {row['category']:<11} {row['urgency']:<9} "
                  f"{row['description'][:50]}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
