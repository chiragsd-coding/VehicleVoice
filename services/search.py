"""
search.py -- pure structured filter layer.

Builds a SQL WHERE clause from NLU slots and returns candidate rows. This is
STRICTLY structured filtering: exact/equality filters plus case-insensitive
partial matches on city and body_type. No embeddings, no semantic search, no
full-text scoring.
"""
from __future__ import annotations

from typing import Optional

from database import db

# Body-type near-match keywords: a broad user term should also match specific
# catalog body_type values that contain it (e.g. "truck" -> "mini truck").
# This is a flat string containment match, not semantic.
_BODY_PARTIAL = True

# Aliases so common spoken terms map onto catalog body_type values.
BODY_ALIASES = {
    "truck": "truck",
    "mini truck": "mini truck",
    "mini": "mini truck",
    "van": "van",
    "pickup": "pickup",
    "pick up": "pickup",
    "hatchback": "hatchback",
    "hatch": "hatchback",
    "suv": "suv",
    "sedan": "sedan",
    "car": "hatchback",  # generic "car" -> broadest passenger bucket (best-effort, documented)
}


def normalize_body(value: Optional[str]) -> Optional[str]:
    """Map a spoken body term to the closest catalog body_type alias."""
    if value is None:
        return None
    v = str(value).strip().lower()
    return BODY_ALIASES.get(v, v)


def build_where(slots: dict) -> tuple[list, list]:
    """
    Build (clauses, params) for a SQL WHERE from a slots dict.

    Recognised slot keys:
      budget     -> price <= budget            (int INR)
      fuel       -> fuel = ?                    (case-insensitive exact)
      city       -> city LIKE ?                 (partial, case-insensitive)
      body_type  -> body_type = ? OR LIKE ?     (near/exact)
    """
    clauses: list[str] = []
    params: list = []

    budget = slots.get("budget")
    if budget is not None:
        clauses.append("price <= ?")
        params.append(int(budget))

    fuel = slots.get("fuel")
    if fuel:
        clauses.append("LOWER(fuel) = LOWER(?)")
        params.append(str(fuel))

    city = slots.get("city")
    if city:
        clauses.append("LOWER(city) LIKE LOWER(?)")
        params.append(f"%{city}%")

    body = normalize_body(slots.get("body_type"))
    if body:
        if _BODY_PARTIAL:
            # near/exact: exact value OR substring containment
            clauses.append("(LOWER(body_type) = LOWER(?) OR LOWER(body_type) LIKE LOWER(?))")
            params.extend([body, f"%{body}%"])
        else:
            clauses.append("LOWER(body_type) = LOWER(?)")
            params.append(body)

    return clauses, params


def search(db_path: str = db.DB_PATH, slots: Optional[dict] = None,
           max_rows: Optional[int] = None) -> list[dict]:
    """
    Return candidate rows matching `slots`. Ordered by id for deterministic
    pre-ranking order (ranking happens later in services/ranking.py).
    """
    if slots is None:
        slots = {}
    clauses, params = build_where(slots)
    sql = "SELECT * FROM vehicle"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id"
    if max_rows is not None:
        sql += f" LIMIT {int(max_rows)}"
    return db.query(db_path, sql, tuple(params))


if __name__ == "__main__":
    db.ensure_catalog()
    demo = {"budget": 600_000, "fuel": "CNG", "city": "mumbai", "body_type": "mini truck"}
    rows = search(slots=demo)
    print(f"{len(rows)} matches for {demo}:")
    for v in rows:
        print(f"  {v['make']} {v['model']} | {v['fuel']} | {v['city']} | ₹{v['price']:,}")
