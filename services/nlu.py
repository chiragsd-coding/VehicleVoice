"""
nlu.py -- deterministic fallback "understanding" layer.

LATER TASK: the real NLU will be an LLM structured-output extractor. It replaces
the internals of this module only -- the public interface (extract_slots ->
dict) is the contract the pipeline depends on, and it stays unchanged.

FOR NOW this is a rule-based parser tuned to the catalog. It is honest about
being a fallback: every parse is tagged ``is_fallback: True`` and only slots the
catalog can actually filter on are extracted (budget, fuel, body_type, city)
plus a free-text `purpose`. Slots the parser cannot confidently extract are left
as None -- the pipeline treats None as "not specified", so a missed slot never
invents a filter.

Extraction rules
----------------
budget     : "5 lakh"/"5 lac"/"5L" -> INR int; bare 4-7 digit number -> INR int
fuel       : CNG/Diesel/Petrol keywords; negated mentions ("not diesel") are
             dropped and recorded in `negations` so they never become a filter
body_type  : keyword/alias match ("mini truck", "pick up", "suv", "car" ...)
city       : match against the curated known-city list (catalog cities only)
purpose    : free text captured after "for" (e.g. "for delivery services")
selected_index: follow-up pick detection delegated to memory.conversation
"""
from __future__ import annotations

import re
from typing import Optional

from memory import conversation

# Curated city list matching the deterministic catalog (with common aliases).
KNOWN_CITIES = {
    "mumbai": "Mumbai", "delhi": "Delhi", "pune": "Pune",
    "bengaluru": "Bengaluru", "bangalore": "Bengaluru",
    "chennai": "Chennai", "hyderabad": "Hyderabad",
    "kolkata": "Kolkata", "calcutta": "Kolkata",
    "ahmedabad": "Ahmedabad", "jaipur": "Jaipur", "lucknow": "Lucknow",
}

FUEL_TERMS = {
    "cng": "CNG", "gas": "CNG", "diesel": "Diesel", "petrol": "Petrol",
    "electric": "Electric",  # valid filter; catalog has none -> honest 0 results
}

# Longest phrases first so "mini truck" wins over "truck", "pick up" over ...
BODY_PHRASES = {
    "mini truck": "mini truck",
    "pick up": "pickup",
    "hatchback": "hatchback",
    "pickup": "pickup",
    "sedan": "sedan",
    "truck": "truck",
    "van": "van",
    "suv": "suv",
    "hatch": "hatchback",
    "mini": "mini truck",
    "car": "hatchback",
}

_BUDGET_LAKH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:lakh|lac)\b", re.I)
_BUDGET_L_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*l\b", re.I)
_RAW_PRICE_RE = re.compile(r"\b(\d{4,7})\b")  # bare INR-ish number (10k..99,99,999)
_NEGATED_FUEL_RE = re.compile(
    rf"\b(?:not|no)\s+(?:running\s+on\s+)?({'|'.join(FUEL_TERMS)})\b", re.I
)
_FUEL_RE = re.compile(rf"\b({'|'.join(FUEL_TERMS)})\b", re.I)
_PURPOSE_RE = re.compile(r"\bfor\s+(?:my\s+|the\s+)?([a-z][a-z0-9\s-]{1,30}?)(?=[,.;!?]|\Z)", re.I)


def _extract_budget(text: str) -> Optional[int]:
    """Return INR budget as int, or None. Lakh mention wins over bare number."""
    m = _BUDGET_LAKH_RE.search(text)
    if m:
        return int(round(float(m.group(1)) * 100_000))
    m = _BUDGET_L_RE.search(text)
    if m:
        return int(round(float(m.group(1)) * 100_000))
    m = _RAW_PRICE_RE.search(text)
    if m:
        value = int(m.group(1))
        if value >= 10_000:  # ignore small numbers (years, payloads, counts)
            return value
    return None


def _extract_body(text: str) -> Optional[str]:
    lowered = " " + text.lower() + " "
    for phrase, canonical in BODY_PHRASES.items():
        # Optional trailing "s" lets "mini trucks" match "mini truck".
        if re.search(rf"\b{re.escape(phrase)}s?\b", lowered):
            return canonical
    return None


def _extract_city(text: str) -> Optional[str]:
    lowered = " " + text.lower() + " "
    best = None
    for alias, canonical in KNOWN_CITIES.items():
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            # Prefer the longest matched alias (Mumbai over "Pune" etc. not an
            # issue, but keeps Bengaluru > bangalore ordering stable).
            if best is None or len(alias) > len(best[0]):
                best = (alias, canonical)
    return best[1] if best else None


def _extract_fuel_with_negations(text: str) -> tuple[Optional[str], list[str]]:
    """Return (fuel, negated_slot_names).

    "not diesel, CNG" -> ("CNG", ["fuel"]): the negated fuel is removed from the
    text BEFORE extraction so diesel never becomes a filter; the positive fuel
    (CNG) is picked. Replacement rather than accumulation is then guaranteed by
    conversation.merge_slots (last provided value wins).
    """
    t = text
    negated: list[str] = []
    for m in _NEGATED_FUEL_RE.finditer(text):
        if m.group(1).lower() in FUEL_TERMS:
            negated.append("fuel")
            t = t.replace(m.group(0), " ", 1)
    m = _FUEL_RE.search(t)
    fuel = FUEL_TERMS.get(m.group(1).lower()) if m else None
    return fuel, sorted(set(negated))


def _extract_purpose(text: str) -> Optional[str]:
    m = _PURPOSE_RE.search(text)
    if m:
        purpose = re.sub(r"\s+", " ", m.group(1)).strip(" -")
        if purpose:
            return purpose
    return None


def extract_slots(transcript: str) -> dict:
    """Parse a user transcript into a structured slot dict (fallback NLU).

    Returns
    -------
    {
      "slots": {"budget": int|None, "fuel": str|None, "body_type": str|None,
                "city": str|None, "purpose": str|None},
      "selected_index": int|None,   # follow-up pick (0-based)
      "negations": [...],           # slot names the user explicitly rejected
      "is_fallback": True,          # honest marker -- LLM NLU replaces this later
    }
    """
    text = str(transcript or "")

    fuel, negated_fuels = _extract_fuel_with_negations(text)

    body = _extract_body(text)
    city = _extract_city(text)
    budget = _extract_budget(text)
    purpose = _extract_purpose(text)

    # A short purpose like "delivery" that is really a noun phrase ("for city
    # delivery") is kept as free text -- fine for now, it is not filtered on.
    slots = {
        "budget": budget,
        "fuel": fuel,
        "body_type": body,
        "city": city,
        "purpose": purpose,
    }

    return {
        "slots": slots,
        "selected_index": conversation.resolve_follow_up(text),
        "negations": negated_fuels,
        "is_fallback": True,
    }


if __name__ == "__main__":  # pragma: no cover - quick manual demo
    for sample in [
        "mini truck under 5 lakh in Mumbai",
        "only CNG",
        "not diesel, CNG",
        "show the first one",
        "van for delivery services in pune below 4 lakh",
    ]:
        print(f"{sample!r:60} -> {extract_slots(sample)}")