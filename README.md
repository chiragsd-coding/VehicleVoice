# VehicleVoice — Voice Vehicle Search Assistant

VehicleVoice is a push-to-talk vehicle search MVP. A transcript such as
“mini truck under 5 lakh in Mumbai” is parsed into structured slots, applied to
a deterministic SQLite catalog, ranked, and rendered as a written response.
The response is also the text that a future TTS adapter can speak. The current
keyless path accepts typed transcripts through the same API contract; microphone
STT and audio TTS adapters are intentionally not wired yet.

## Architecture (current implementation)

This is **one FastAPI app with separated service modules**, not a collection of
network services:

```text
transcript (POST /api/voice)
    │
    ├─ services/nlu.py             deterministic fallback slot extraction
    ├─ memory/conversation.py      per-session slot merge + ordinal selection
    ├─ services/search.py          structured SQLite WHERE filters only
    ├─ services/ranking.py         deterministic weighted top-3 ranking
    └─ services/response.py        fixed templates, catalog-backed facts only
             │
             └─ JSON: slots, results, spoken text, matched_count, latency_ms

backend/main.py                    FastAPI wiring + latency logger
frontend/                          React + Vite push-to-talk UI
 database/generate_catalog.py       reproducible catalog generator
 database/vehicles.db               current generated SQLite catalog
 evaluation/tests.json              keyless scenario fixture
 evaluation/evaluate.py             per-case + aggregate evaluation harness
```

`services/nlu.py` currently exposes a rule-based fallback with the interface a
future structured-output NLU can replace. `services/stt.py` and
`services/tts.py` are not present in this checkout yet: the frontend's audio
upload and playback hooks are reserved for that later milestone. This README
describes the implementation that exists, rather than claiming those adapters
are complete.

### Search and response guarantees

- **Structured search only:** `services/search.py` translates budget, fuel, city,
  and body-type slots into SQL equality/LIKE predicates. There are no embeddings,
  vector indexes, or semantic retrieval fallbacks.
- **Deterministic ranking:** `services/ranking.py` uses the explicit blend
  `0.40 budget_fit + 0.25 year_norm + 0.20 km_norm + 0.15 verified`, with stable
  tie-breaking. The API returns at most three ranked rows.
- **No hallucinations:** `services/response.py` is template-built. Vehicle name,
  year, price, fuel, and kilometres are interpolated from the returned catalog
  dictionaries only. Query budget/city may be repeated as user-provided context;
  no LLM writes the vehicle facts or the final answer.
- **Conversation memory:** a session carries forward only slots provided on
  earlier turns. “Only CNG” therefore updates fuel without erasing a prior
  budget or city; “show the first one” records a zero-based selection index.
  Memory is process-local and in-memory, so it is not a production persistence
  layer.

## Deterministic catalog and data assumptions

`database/generate_catalog.py` uses Python `random.Random(seed=42)` and curated
Indian vehicle templates/cities to write exactly 120 rows by default. It does
not require Faker, and regeneration recreates the SQLite file from scratch:

```bash
python3 database/generate_catalog.py
python3 database/db.py
```

The evaluation fixture records the assumptions (`seed=42`, `rows=120`, and
`ranking_top_n=3`) and includes exact IDs for scenarios whose ranking is meant
to be regression-tested. If the generator seed, row count, templates, or ranking
formula changes, review/update `evaluation/tests.json` deliberately rather than
silently treating changed IDs as model accuracy.

## Local setup and run

The backend requires Python 3.10+ and the packages in `requirements.txt`:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Run the API from the repository root:

```bash
.venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 3000
```

Useful endpoints:

- `GET /health` — health and app version.
- `POST /api/voice` — JSON body `{"session_id": "...", "transcript": "..."}`.
  The response contains merged slots, up to three result rows, `spoken` text,
  `matched_count`, and per-stage `latency_ms`.
- `GET /` — serves `frontend/dist` when it exists, otherwise a small backend
  placeholder page.

Example request:

```bash
curl -s http://127.0.0.1:3000/api/voice \
  -H 'content-type: application/json' \
  -d '{"session_id":"demo","transcript":"CNG mini trucks"}'
```

### Frontend

The React + Vite UI supports typed transcript fallback, result cards, slot
inspection, and the spoken-response text panel. Build it from `frontend/`:

```bash
cd frontend
npm install
npm run build
cd ..
.venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 3000
```

For frontend development, run `npm run dev` in `frontend/` and the backend on
port 8000; Vite proxies API calls to that backend. The current `/api/voice/audio`
hook is intentionally not implemented, so microphone capture falls back to
text input until STT is added.

## Tests and evaluation

Run the full Python test suite (pure logic, memory, API pipeline, and harness):

```bash
.venv/bin/python -m pytest tests -q
```

The original dependency-light runner covers the pure core module only:

```bash
python3 tests/run_tests.py
```

Run the keyless evaluation harness directly against the current pipeline:

```bash
.venv/bin/python evaluation/evaluate.py
```

It executes `evaluation/tests.json` through `backend.main.run_pipeline`, so it
needs no running server, API key, LLM, Whisper, microphone, or TTS provider. It
prints each case and turn, matched count/top IDs, and aggregate case/turn,
slot, result, selection, response, and grounding metrics. `--json` emits a
machine-readable report for CI or other tooling:

```bash
.venv/bin/python evaluation/evaluate.py --json
```

The command exits `0` only when all fixture cases pass, `1` when an evaluation
assertion fails, and `2` for a malformed fixture or setup error.

## Resource and keyless decisions

The MVP deliberately keeps the critical path small and auditable: one Python
process, SQLite, deterministic filtering/ranking, and a rule-based NLU fallback.
That makes local development and CI possible without paid APIs, GPU memory, or
network-dependent model downloads. STT, an LLM NLU adapter, and TTS can be
added behind replaceable module interfaces later; they must not become sources
of vehicle facts.

A locally hosted `qwen2.5:0.5b` model is an optional future experiment, not a
requirement of this implementation or the evaluation harness. At that size it
may be useful for lightweight structured slot extraction on constrained
hardware, but it can omit slots, produce malformed structured output, or
misread multi-turn/negated language. It is therefore not used as the response
composer, search engine, ranking function, or source of catalog truth. Any
future adapter must validate its structured output and preserve the same
keyless fallback and template/no-hallucination guarantees.

## Current scope and limitations

- The API currently takes text transcripts; audio upload/STT and generated audio
  playback are follow-on work.
- Session memory is in-process and volatile; there is no authentication or
  multi-worker shared state.
- The catalog is synthetic and deterministic, not live inventory.
- The evaluation harness measures this fixed fallback pipeline against explicit
  fixture expectations. It is a regression signal for the current catalog and
  rules, not a claim about real-world voice recognition or vehicle-market
  coverage.
