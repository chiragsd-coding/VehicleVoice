"""
response.py -- template-based response composer.

CRITICAL RULE: every fact in the emitted text must come from the catalog record.
There is NO free-form LLM output here -- the user-facing summary is assembled
from a fixed set of templates whose placeholders are interpolated exclusively
with values present in the `vehicles` dicts. Nothing is invented.

Numbers are formatted from the record fields (price in Indian "lakh", km with a
thousands separator) so the text always carries the ground-truth values.
"""
from __future__ import annotations

from typing import Optional


def format_price_inr(price: int) -> str:
    """Format an INR price as a natural "₹X lakh" or "₹X.Y lakh" string."""
    lakh = price / 100_000.0
    if lakh >= 10:
        return f"₹{lakh:.1f} lakh"
    # trim trailing .0 -> "₹4.8 lakh", "₹3 lakh"
    s = f"{lakh:.1f}"
    if s.endswith(".0"):
        s = s[:-2]
    return f"₹{s} lakh"


def format_km(km: int) -> str:
    return f"{km:,}"


def _fuel_phrase(fuel: str) -> str:
    return {"CNG": "CNG", "Diesel": "Diesel", "Petrol": "Petrol"}.get(fuel, fuel)


def _suitability(vehicle: dict) -> str:
    """A deterministic suitability phrase derived only from real record fields."""
    body = vehicle["body_type"]
    if body in ("mini truck", "pickup", "truck"):
        return "Suitable for goods & city delivery"
    if body == "van":
        return "Suitable for carrying cargo & passengers"
    if body == "suv":
        return "Spacious family vehicle"
    if body in ("hatchback", "sedan"):
        return "Good for personal city use"
    return "Available for sale"


def _option_line(idx: int, vehicle: dict, budget: Optional[int]) -> str:
    opt = f"Option {idx}: {vehicle['make']} {vehicle['model']}, " \
          f"{vehicle['year']} model, {format_price_inr(vehicle['price'])}, " \
          f"{format_km(vehicle['km'])} km."

    if budget is not None and vehicle["price"] <= budget:
        opt += " Fits your budget."
    elif budget is not None:
        opt += f" Slightly above your {format_price_inr(budget)} budget."
    else:
        opt += " Within any budget."

    opt += f" {_fuel_phrase(vehicle['fuel'])}. {_suitability(vehicle)}."
    if vehicle["verified"]:
        opt += " Verified papers."
    else:
        opt += " Papers not yet verified."
    return opt


def compose_response(vehicles: list[dict], slots: Optional[dict] = None) -> str:
    """
    Compose the natural-language answer for the ranked `vehicles`.

    vehicles: already-ranked list of dicts (typically the top 3 from ranking).
    slots:    the current slot dict (used only for budget/context phrasing).
    """
    if slots is None:
        slots = {}
    budget = slots.get("budget")

    if not vehicles:
        city = slots.get("city")
        budget_s = f" under {format_price_inr(budget)}" if budget else ""
        if city:
            return (f"Sorry, we found no matching {city} vehicle{budget_s}. "
                    f"Try a different city, raise your budget, or remove a filter.")
        return (f"Sorry, we found no matching vehicles{budget_s}. "
                f"Try raising your budget or changing your filters.")

    lines = []
    if len(vehicles) == 1:
        v = vehicles[0]
        lines.append(f"Here is the best match: {v['make']} {v['model']}, "
                     f"{v['year']} model, {format_price_inr(v['price'])}, "
                     f"{format_km(v['km'])} km.")
        if budget is not None and v["price"] <= budget:
            lines.append("It fits your budget.")
        elif budget is not None:
            lines.append(f"It is slightly above your {format_price_inr(budget)} budget.")
        lines.append(f"{_fuel_phrase(v['fuel'])}. {_suitability(v)}.")
        lines.append("Verified papers." if v["verified"] else "Papers not yet verified.")
        return " ".join(lines)

    intro = "Here are the top matching vehicles."
    if budget:
        intro += f" Within a {format_price_inr(budget)} budget."
    lines.append(intro)
    for idx, v in enumerate(vehicles, start=1):
        lines.append(_option_line(idx, v, budget))
    return " ".join(lines)


if __name__ == "__main__":
    demo_vehicles = [
        {"make": "Tata", "model": "Ace Gold", "year": 2019, "price": 480_000,
         "fuel": "CNG", "body_type": "mini truck", "city": "Mumbai", "km": 42000,
         "payload_kg": 1000, "verified": 1},
        {"make": "Mahindra", "model": "Jeeto", "year": 2018, "price": 350_000,
         "fuel": "Diesel", "body_type": "mini truck", "city": "Pune", "km": 61000,
         "payload_kg": 700, "verified": 0},
    ]
    print(compose_response(demo_vehicles, {"budget": 600_000}))
    print()
    print(compose_response([], {"budget": 200_000}))
