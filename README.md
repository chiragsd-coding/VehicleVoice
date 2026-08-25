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

---

# VehicleVoice — Backend (FastAPI pipeline)

One FastAPI app wiring the full search pipeline
`nlu → conversation memory → search → ranking → response composer` with
per-stage latency logging. STT/TTS and the React frontend are later tasks.

## Layout (new since milestone 1)
```
backend/
  main.py              FastAPI app: POST /api/voice, GET /health, static mount
memory/
  conversation.py      ConversationState, merge_slots, follow-up resolution,
                       thread-safe in-memory SessionStore
services/
  nlu.py               deterministic fallback slot extractor (LLM lands later,
                       same public interface: extract_slots() -> dict)
logs/latency.log       appended per request (tab-delimited, per-stage + total)
```

## Run
```bash
.venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 3000
```
Imports work from the repo root (the app also self-inserts the repo root onto
`sys.path` so `uvicorn backend.main:app` works from any cwd).

## API
- `POST /api/voice` — body `{"session_id": "...", "transcript": "..."}`
  returns `{session_id, transcript, slots (merged), selected_vehicle, results
  (top 3), spoken, matched_count, latency_ms {nlu, merge, search, rank, compose,
  stt, total}}`. Guaranteed to always speak a template-built answer, even with 0
  results.
- `GET /health` — `{"status": "ok", "version": "..."}`
- `GET /` — serves `frontend/dist` when the React build exists, otherwise a
  minimal inline placeholder page so the port-3000 site is never blank.

## Multi-turn semantics (memory/conversation.py)
- `merge_slots` — update-only-what's-provided: "only CNG" keeps earlier
  budget/city and sets fuel.
- `resolve_follow_up` — "show the first/second/third one" -> index into the last
  result list, recorded on `selected_vehicle`.
- Negation — "not diesel, CNG" replaces fuel (CNG wins), never AND-accumulates.

## Tests
```bash
./.venv/bin/python -m pytest tests -q    # 32 tests: core (16) + memory + pipeline
```
`tests/test_pipeline.py` runs the API through FastAPI's TestClient and asserts
the no-hallucination invariant: every number in the spoken answer traces back
to a returned catalog record.
