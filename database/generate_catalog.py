"""
generate_catalog.py
-------------------
Builds the ~120-record SQLite vehicle catalog at database/vehicles.db.

Fully deterministic: uses the standard-library `random` with a fixed seed and
curated pools of realistic Indian vehicles. Faker is NOT required (we fall back
to stdlib determinism so the pipeline runs with zero third-party deps). If Faker
happens to be installed it is ignored -- the catalog is always regenerated the
same way from the seed, which makes regeneration idempotent.

Idempotent on run: the DB file is recreated from scratch each invocation, so
re-running produces an identical catalog.
"""
from __future__ import annotations

import os
import random
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vehicles.db")

# ---------------------------------------------------------------------------
# Curated template pool: (make, model, body_type, [fuel options], payload_kg)
# ---------------------------------------------------------------------------
TEMPLATES = [
    # --- mini trucks / light commercial ---
    ("Tata", "Ace", "mini truck", ["Diesel", "CNG"], 750),
    ("Tata", "Ace Gold", "mini truck", ["CNG", "Diesel"], 1000),
    ("Tata", "Ace Mega", "mini truck", ["Diesel", "CNG"], 800),
    ("Mahindra", "Jeeto", "mini truck", ["Diesel", "CNG"], 700),
    ("Mahindra", "Jeeto Plus", "mini truck", ["CNG", "Diesel"], 750),
    ("Ashok Leyland", "Dost", "truck", ["Diesel"], 1500),
    ("Ashok Leyland", "Bada Dost", "pickup", ["Diesel"], 2000),
    ("Ashok Leyland", "Partner", "pickup", ["Diesel"], 1500),
    ("Eicher", "Pro 3015", "truck", ["Diesel"], 3000),
    ("Tata", "407", "truck", ["Diesel"], 3500),
    ("Ashok Leyland", "Ecomet", "truck", ["Diesel"], 5000),
    ("Force", "Trax Cruiser", "truck", ["Diesel"], 4000),
    # --- vans ---
    ("Mahindra", "Supro", "van", ["Diesel", "CNG"], 700),
    ("Maruti Suzuki", "Eeco", "van", ["CNG", "Petrol"], 600),
    ("Force", "Traveller", "van", ["Diesel"], 900),
    ("Tata", "Winger", "van", ["Diesel", "CNG"], 700),
    ("Bajaj", "RE Cargo", "van", ["CNG", "Petrol"], 400),
    # --- hatchbacks ---
    ("Maruti Suzuki", "Alto", "hatchback", ["Petrol", "CNG"], 0),
    ("Maruti Suzuki", "Alto K10", "hatchback", ["Petrol", "CNG"], 0),
    ("Maruti Suzuki", "WagonR", "hatchback", ["Petrol", "CNG"], 0),
    ("Maruti Suzuki", "Swift", "hatchback", ["Petrol", "Diesel"], 0),
    ("Tata", "Tiago", "hatchback", ["Petrol", "CNG"], 0),
    ("Hyundai", "i10", "hatchback", ["Petrol", "CNG"], 0),
    ("Hyundai", "Grand i10", "hatchback", ["Petrol", "CNG"], 0),
    # --- sedans ---
    ("Maruti Suzuki", "Dzire", "sedan", ["Petrol", "CNG"], 0),
    ("Hyundai", "Xcent", "sedan", ["Petrol", "CNG"], 0),
    ("Honda", "Amaze", "sedan", ["Petrol", "Diesel"], 0),
    ("Honda", "City", "sedan", ["Petrol", "Diesel"], 0),
    # --- SUVs ---
    ("Mahindra", "Bolero", "suv", ["Diesel"], 0),
    ("Mahindra", "Scorpio", "suv", ["Diesel"], 0),
    ("Mahindra", "XUV300", "suv", ["Diesel", "Petrol"], 0),
    ("Tata", "Sumo", "suv", ["Diesel"], 0),
    ("Hyundai", "Creta", "suv", ["Diesel", "Petrol"], 0),
    ("Maruti Suzuki", "Ertiga", "suv", ["Petrol", "CNG"], 0),
]

CITIES = [
    "Mumbai", "Delhi", "Pune", "Bengaluru", "Chennai",
    "Hyderabad", "Kolkata", "Ahmedabad", "Jaipur", "Lucknow",
]

YEAR_MIN, YEAR_MAX = 2008, 2024
TARGET_ROWS = 120
SEED = 42

# Base price (INR) by age bucket for a new-ish vehicle, then scaled down with age/km.
# new_price maps roughly to model segment; we vary per template by body_type.
BODY_NEW_PRICE = {
    "mini truck": 450_000,
    "truck": 1_600_000,
    "pickup": 1_000_000,
    "van": 600_000,
    "hatchback": 500_000,
    "sedan": 750_000,
    "suv": 900_000,
}


def _make_row(rng: random.Random, make, model, body, fuels, payload) -> dict:
    """Build one vehicle dict with realistic, internally-consistent fields."""
    year = rng.randint(YEAR_MIN, YEAR_MAX)
    age = 2025 - year
    # km grows with age (roughly 8-18k km/year)
    km = max(0, int(age * rng.randint(8000, 18000) + rng.randint(0, 5000)))

    new_price = BODY_NEW_PRICE[body]
    # Depreciation ~ 10-14% per year on the used market, plus extra for high km.
    deprec = (1 - 0.12) ** age
    km_penalty = 1 - min(km, 250_000) / 500_000
    base = new_price * deprec * km_penalty
    # Commercial bodies (truck/pickup/van/mini truck) trade a bit higher per INR.
    multiplier = {"truck": 1.0, "pickup": 0.95, "van": 0.9,
                  "mini truck": 0.9, "hatchback": 1.15, "sedan": 1.1, "suv": 1.0}
    price = int(round(base * multiplier[body] * rng.uniform(0.85, 1.15)))
    price = max(120_000, price - (price % 5000))

    return {
        "make": make,
        "model": model,
        "year": year,
        "price": price,
        "fuel": rng.choice(fuels),
        "body_type": body,
        "city": rng.choice(CITIES),
        "km": km,
        "payload_kg": payload,
        "verified": rng.randint(0, 1),
    }


def generate_rows(seed: int = SEED, count: int = TARGET_ROWS) -> list[dict]:
    """Return `count` deterministic vehicle dicts."""
    rng = random.Random(seed)
    rows = []
    while len(rows) < count:
        tpl = rng.choice(TEMPLATES)
        rows.append(_make_row(rng, *tpl))
    return rows


def build_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vehicle (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            make       TEXT NOT NULL,
            model      TEXT NOT NULL,
            year       INTEGER NOT NULL,
            price      INTEGER NOT NULL,          -- INR
            fuel       TEXT NOT NULL,             -- Diesel / CNG / Petrol
            body_type  TEXT NOT NULL,             -- mini truck / truck / van / ...
            city       TEXT NOT NULL,
            km         INTEGER NOT NULL,
            payload_kg INTEGER NOT NULL,
            verified   INTEGER NOT NULL DEFAULT 0  -- 0 / 1
        );
        """
    )


def generate_catalog(db_path: str = DB_PATH, seed: int = SEED, count: int = TARGET_ROWS) -> int:
    """(Re)generate the catalog DB idempotently. Returns number of rows written."""
    if os.path.exists(db_path):
        os.remove(db_path)  # regenerate from scratch each run
    conn = sqlite3.connect(db_path)
    build_schema(conn)
    rows = generate_rows(seed=seed, count=count)
    conn.executemany(
        """
        INSERT INTO vehicle
            (make, model, year, price, fuel, body_type, city, km, payload_kg, verified)
        VALUES (:make, :model, :year, :price, :fuel, :body_type, :city, :km, :payload_kg, :verified)
        """,
        rows,
    )
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM vehicle").fetchone()[0]
    conn.close()
    return n


if __name__ == "__main__":
    n = generate_catalog()
    print(f"Generated catalog at {DB_PATH} with {n} vehicle records.")
