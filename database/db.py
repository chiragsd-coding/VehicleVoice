"""
db.py -- small query helper for the vehicle catalog.

Provides thin, focused helpers to access the SQLite catalog so other services
don't talk to sqlite directly.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any, Optional

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vehicles.db")

# Columns returned for each row, in order.
VEHICLE_COLS = [
    "id", "make", "model", "year", "price", "fuel",
    "body_type", "city", "km", "payload_kg", "verified",
]


def connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_catalog(db_path: str = DB_PATH) -> None:
    """Generate the catalog if the DB file does not exist yet."""
    if not os.path.exists(db_path):
        from database.generate_catalog import generate_catalog
        generate_catalog(db_path)


def _to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


def query(db_path: str = DB_PATH, sql: str = "", params: tuple = ()) -> list[dict]:
    """Run a raw SELECT and return a list of dict rows."""
    ensure_catalog(db_path)
    conn = connect(db_path)
    try:
        rows = conn.execute(sql, params).fetchall()
        return [_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_all(db_path: str = DB_PATH) -> list[dict]:
    return query(db_path, "SELECT * FROM vehicle")


def get_by_id(vehicle_id: int, db_path: str = DB_PATH) -> Optional[dict]:
    rows = query(db_path, "SELECT * FROM vehicle WHERE id = ?", (vehicle_id,))
    return rows[0] if rows else None


def list_catalog(db_path: str = DB_PATH, limit: int = 120) -> list[dict]:
    return query(db_path, f"SELECT * FROM vehicle LIMIT {int(limit)}")


def count(db_path: str = DB_PATH) -> int:
    return len(query(db_path, "SELECT id FROM vehicle"))


def columns(db_path: str = DB_PATH) -> list[str]:
    conn = connect(db_path)
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(vehicle)").fetchall()]
    finally:
        conn.close()


if __name__ == "__main__":
    print(f"Catalog size: {count()} vehicles.")
    sample = get_all()[:5]
    for v in sample:
        print(v)
