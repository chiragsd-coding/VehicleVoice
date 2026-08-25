# VehicleVoice — Core Data + Logic Layer

Pure-Python foundation for the VehicleVoice voice-search pipeline. **No external
API keys, no framework dependencies** — only the Python standard library (Faker
and pytest are optional extras used only for the dev test-runner; the catalog
generation itself is pure stdlib and deterministic).

## Layout
```
database/
  generate_catalog.py   builds the ~120-record deterministic SQLite catalog
  db.py                 thin query helper (get_all, get_by_id, count, ...)
services/
  search.py             pure structured filters -> SQL WHERE (no embeddings)
  ranking.py            deterministic weighted score + top_n()
  response.py           template composer (every fact from the record)
tests/
  test_core.py          16 pure-logic tests (stdlib-only)
  run_tests.py          tiny dependency-light runner (no pytest needed)
```

## Run tests — two equivalent ways
```bash
# Dependency-light (stdlib only):
python3 tests/run_tests.py

# Or under pytest (install earlier via .venv):
./.venv/bin/python -m pytest tests -q
```
Both run the same 16 tests and report `16 passed`.

## Generate / inspect the catalog
```bash
python3 database/generate_catalog.py   # writes database/vehicles.db (idempotent)
python3 services/search.py             # demo filter query
python3 services/ranking.py            # demo top-3 under a budget
python3 services/response.py           # demo composed responses
```

## Design notes
- **Search** is pure structured filtering (equality + case-insensitive partial
  on city/body_type). `BODY_ALIASES` maps spoken terms to catalog values; a
  broad "truck" also matches "mini truck" via substring containment. No
  embeddings, no semantic scoring.
- **Ranking** is fully deterministic:
  `score = 0.40*budget_fit + 0.25*year_norm + 0.20*km_norm + 0.15*verified`,
  ties broken by (year, km, id). `top_n(rows, n=3, budget=...)`.
- **Response composer** never invents facts — every number is interpolated from
  the catalog record (prices shown in "lakh", km with thousands separators).
  Handles no-results / 1 / 2–3 matches.
