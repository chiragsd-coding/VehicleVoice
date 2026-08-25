"""
test_core.py -- pure-logic tests for catalog, filters, ranking, responses.

These use only the Python standard library and the local project modules, so
they run under pytest (`.venv/bin/python -m pytest`) OR under the tiny stdlib
runner in `tests/run_tests.py` with no third-party deps.
"""
from __future__ import annotations

import os
import tempfile

from database import db as db_mod
from database.generate_catalog import generate_catalog, generate_rows
from services import ranking, response, search

TARGET = 120


def _tmp_db():
    """Generate a fresh catalog in a temp dir; return its path."""
    tmpdir = tempfile.mkdtemp(prefix="vv_test_")
    path = os.path.join(tmpdir, "vehicles.db")
    n = generate_catalog(path)
    assert n == TARGET, f"expected {TARGET} rows, got {n}"
    return path


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
def test_catalog_generates_120_rows():
    db_path = _tmp_db()
    assert db_mod.count(db_path) == TARGET


def test_catalog_has_expected_columns():
    db_path = _tmp_db()
    cols = db_mod.columns(db_path)
    for required in ["make", "model", "year", "price", "fuel",
                     "body_type", "city", "km", "payload_kg", "verified"]:
        assert required in cols, f"missing column {required}"


def test_catalog_generation_is_deterministic():
    rows_a = generate_rows(seed=42, count=TARGET)
    rows_b = generate_rows(seed=42, count=TARGET)
    assert rows_a == rows_b


def test_catalog_values_are_plausible():
    rows = generate_rows(seed=42, count=TARGET)
    for r in rows:
        assert 2008 <= r["year"] <= 2024
        assert r["price"] > 0
        assert r["fuel"] in ("Diesel", "CNG", "Petrol")
        assert r["verified"] in (0, 1)


# ---------------------------------------------------------------------------
# Filters / search
# ---------------------------------------------------------------------------
def test_filter_budget_exact():
    db_path = _tmp_db()
    rows = search.search(db_path, slots={"budget": 600_000})
    assert rows, "expected at least some matches under 6 lakh"
    assert all(r["price"] <= 600_000 for r in rows)


def test_filter_fuel_exact_case_insensitive():
    db_path = _tmp_db()
    rows = search.search(db_path, slots={"fuel": "cng"})
    assert rows, "expected CNG matches"
    assert all(r["fuel"] == "CNG" for r in rows)


def test_filter_city_partial_case_insensitive():
    db_path = _tmp_db()
    rows = search.search(db_path, slots={"city": "beng"})  # matches "Bengaluru"
    assert rows
    assert all("beng" in r["city"].lower() for r in rows)


def test_filter_body_type_near_match():
    db_path = _tmp_db()
    rows = search.search(db_path, slots={"body_type": "truck"})
    assert rows
    # "truck" should also match "mini truck" via partial containment
    bodies = {r["body_type"] for r in rows}
    assert "mini truck" in bodies


def test_combined_filters():
    db_path = _tmp_db()
    rows = search.search(db_path, slots={
        "budget": 600_000, "fuel": "CNG", "body_type": "mini truck",
    })
    for r in rows:
        assert r["price"] <= 600_000
        assert r["fuel"] == "CNG"
        assert r["body_type"] == "mini truck"


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------
def test_ranking_deterministic():
    rows = generate_rows(seed=42, count=TARGET) + generate_rows(seed=7, count=40)
    a = ranking.top_n(rows, n=3, budget=600_000)
    b = ranking.top_n(rows, n=3, budget=600_000)
    assert a == b


def test_ranking_orders_known_case():
    # Vehicle A: cheap, new, low km, verified -> best.
    # Vehicle B: same price/year but high km, unverified -> worst.
    rows = [
        {"id": 1, "make": "Tata", "model": "A", "year": 2023, "price": 300_000,
         "fuel": "CNG", "body_type": "mini truck", "city": "Mumbai",
         "km": 10000, "payload_kg": 700, "verified": 1},
        {"id": 2, "make": "Tata", "model": "B", "year": 2023, "price": 300_000,
         "fuel": "CNG", "body_type": "mini truck", "city": "Mumbai",
         "km": 90000, "payload_kg": 700, "verified": 0},
        {"id": 3, "make": "Tata", "model": "C", "year": 2023, "price": 300_000,
         "fuel": "CNG", "body_type": "mini truck", "city": "Mumbai",
         "km": 90000, "payload_kg": 700, "verified": 1},
    ]
    top = ranking.top_n(rows, n=3, budget=350_000)
    assert top[0]["id"] == 1  # best on every signal


def test_ranking_prefers_within_budget():
    # Identical on every other signal -> the one within budget must win.
    within = {"id": 1, "make": "T", "model": "M", "year": 2020, "price": 400_000,
              "fuel": "CNG", "body_type": "van", "city": "X", "km": 50000,
              "payload_kg": 600, "verified": 1}
    over = {"id": 2, "make": "T", "model": "M2", "year": 2020, "price": 900_000,
            "fuel": "CNG", "body_type": "van", "city": "X", "km": 50000,
            "payload_kg": 600, "verified": 1}
    top = ranking.top_n([within, over], n=1, budget=500_000)
    assert top[0]["id"] == 1


# ---------------------------------------------------------------------------
# Response composer
# ---------------------------------------------------------------------------
def _vehicle(**kw):
    base = {"make": "Tata", "model": "Ace Gold", "year": 2019, "price": 480_000,
            "fuel": "CNG", "body_type": "mini truck", "city": "Mumbai",
            "km": 42000, "payload_kg": 1000, "verified": 1}
    base.update(kw)
    return base


def test_response_no_results():
    out = response.compose_response([], {"budget": 200_000})
    assert "no matching" in out
    assert "200_000" not in out  # never leak raw ints
    assert "₹2 lakh" in out  # budget phrased naturally


def test_response_single_result_contains_record_facts():
    v = _vehicle()
    out = response.compose_response([v], {"budget": 600_000})
    assert "480_000" not in out        # raw ints must never appear
    assert "4.8 lakh" in out           # formatted price from record
    assert "42,000" in out             # formatted km from record
    assert "2019" in out               # year from record
    assert "Ace Gold" in out
    assert "CNG" in out
    assert "Verified papers" in out


def test_response_multi_result_lists_every_record_fact():
    v1 = _vehicle(id=1)
    v2 = _vehicle(id=2, make="Mahindra", model="Jeeto", price=350_000,
                  km=61000, fuel="Diesel", verified=0)
    out = response.compose_response([v1, v2], {"budget": 600_000})
    for fact in ["Option 1", "Option 2", "480_000", "4.8 lakh", "42,000",
                 "350_000", "3.5 lakh", "61,000", "Diesel", "not yet verified"]:
        if fact in ("480_000", "350_000"):
            assert fact not in out  # raw integers must not appear
        else:
            assert fact in out, f"missing expected fact {fact!r}"


def test_response_two_three_handling():
    # 2 results -> multi-list template; 3 results -> same.
    two = [_vehicle(id=1), _vehicle(id=2, make="Mahindra", model="Jeeto")]
    assert compose_has_option(two)
    three = two + [_vehicle(id=3, make="Ashok Leyland", model="Dost")]
    assert "Option 3" in response.compose_response(three, {})


def compose_has_option(vehicles):
    out = response.compose_response(vehicles, {"budget": 600_000})
    return "Option 1" in out and "Option 2" in out


# Expose a list of all tests for the tiny runner.
ALL_TESTS = [v for k, v in list(globals().items())
             if k.startswith("test_") and callable(v)]


if __name__ == "__main__":
    from tests import run_tests
    run_tests.main(module_name="tests.test_core")
