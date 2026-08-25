"""
ranking.py -- deterministic ranking of candidate vehicles.

Every record gets a deterministic numeric score so all records are directly
comparable. No randomness -- the same input always yields the same ordering.

score = W_BUDGET * budget_fit
      + W_YEAR   * year_norm
      + W_KM     * km_norm
      + W_VERIFIED * verified

  budget_fit : 0..1  -- how comfortably price sits within budget (0 if over)
  year_norm  : 0..1  -- newer year -> higher
  km_norm    : 0..1  -- lower km -> higher
  verified   : 0/1

Weights are explicit and sum to 1.0 so the score is a weighted blend.
"""
from __future__ import annotations

from typing import Optional

# Weighted blend of the four signal components (sums to 1.0).
W_BUDGET = 0.40
W_YEAR = 0.25
W_KM = 0.20
W_VERIFIED = 0.15

YEAR_BASE = 2000
MAX_KM_REF = 300_000  # beyond this, km_norm floors at 0


def budget_fit(price: int, budget: Optional[int]) -> float:
    """1.0 when comfortably within budget, shrinking to 0 as price nears/exceeds it."""
    if budget is None or budget <= 0:
        return 0.5  # neutral when no budget given
    if price > budget:
        return 0.0
    return max(0.0, 1.0 - price / budget)


def year_norm(year: int) -> float:
    return max(0.0, min(1.0, (year - YEAR_BASE) / 24.0))


def km_norm(km: int) -> float:
    return max(0.0, 1.0 - min(km, MAX_KM_REF) / MAX_KM_REF)


def rank_score(row: dict, budget: Optional[int] = None) -> float:
    """Deterministic composite score for a single row."""
    return (
        W_BUDGET * budget_fit(row["price"], budget)
        + W_YEAR * year_norm(row["year"])
        + W_KM * km_norm(row["km"])
        + W_VERIFIED * float(row["verified"])
    )


def top_n(rows: list[dict], n: int = 3, budget: Optional[int] = None) -> list[dict]:
    """
    Return the top `n` rows by deterministic rank score (descending). Ties are
    broken by (year desc, km asc, id asc) so the result is fully deterministic.
    Mutates neither the input nor the caller's ordering.
    """
    scored = [(rank_score(r, budget), r) for r in rows]
    scored.sort(key=lambda pair: (
        pair[0],
        pair[1].get("year", 0),
        -pair[1].get("km", 0),
        -pair[1].get("id", 0),
    ), reverse=True)
    return [r for _, r in scored[:n]]


if __name__ == "__main__":
    from database import db
    db.ensure_catalog()
    rows = db.get_all()
    top = top_n(rows, n=3, budget=600_000)
    print("Top 3 under ₹6 lakh:")
    for v in top:
        print(f"  {v['make']} {v['model']} | ₹{v['price']:,} | {v['year']} | "
              f"{v['km']:,} km | verified={v['verified']} | "
              f"score={rank_score(v, 600_000):.4f}")
