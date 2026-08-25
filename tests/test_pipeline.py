"""
test_pipeline.py -- API-level pipeline tests against a fresh FastAPI TestClient.

Covers the mandated smoke scenario (turn 1 "mini truck under 5 lakh in Mumbai",
turn 2 "only CNG" keeps budget/city and sets fuel), slot-merge across turns,
follow-up selection, the 0-result robustness path, and no-hallucination: every
number in the spoken answer must trace back to a returned catalog record.
"""
import re

from fastapi.testclient import TestClient

from backend.main import app
from services.response import format_km, format_price_inr

client = TestClient(app)


def _voice(session_id: str, transcript: str) -> dict:
    resp = client.post("/api/voice", json={"session_id": session_id, "transcript": transcript})
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- mandated smoke scenario ------------------------------------------------
def test_smoke_two_turn_slot_merge():
    sid = "smoke-sess"
    t1 = _voice(sid, "mini truck under 5 lakh in Mumbai")
    assert t1["slots"]["budget"] == 500_000
    assert t1["slots"]["city"] == "Mumbai"
    assert t1["slots"]["body_type"] == "mini truck"
    assert len(t1["results"]) <= 3
    assert t1["matched_count"] >= 1
    assert "latency_ms" in t1 and t1["latency_ms"]["total"] >= 0

    t2 = _voice(sid, "only CNG")
    # Budget & city carried over from turn 1; fuel set; body kept.
    assert t2["slots"]["budget"] == 500_000
    assert t2["slots"]["city"] == "Mumbai"
    assert t2["slots"]["body_type"] == "mini truck"
    assert t2["slots"]["fuel"] == "CNG"
    assert len(t2["results"]) <= 3


# --- multi-turn merge + follow-up + no-hallucination ------------------------
def test_pipeline_merge_followup_and_no_hallucination():
    sid = "mh-sess"
    t1 = _voice(sid, "CNG mini trucks")
    # Plenty of CNG mini trucks in the catalog -> full top-3.
    assert len(t1["results"]) == 3
    spoken = t1["spoken"]
    for idx, v in enumerate(t1["results"], start=1):
        assert f"Option {idx}" in spoken

    # (a) Every ₹price token in the spoken text matches a returned record.
    record_prices = {format_price_inr(v["price"]) for v in t1["results"]}
    price_tokens = re.findall(r"₹[\d.,]+\s*lakh", spoken)
    assert price_tokens, "expected at least one price token in spoken answer"
    for tok in price_tokens:
        assert tok in record_prices, f"spoken price {tok!r} not in returned records"

    # (b) Every km figure matches a returned record.
    record_kms = {format_km(v["km"]) for v in t1["results"]}
    for km in re.findall(r"([\d,]+) km", spoken):
        assert km in record_kms, f"spoken km {km!r} not in returned records"

    # (c) No raw 5+ digit integers leak into the text (e.g. "450000").
    assert not re.search(r"\b\d{5,}\b", spoken.replace("1st", "").replace(
        "2nd", "").replace("3rd", "")), "raw large integer leaked into spoken text"

    # Turn 2: folow-up pick keeps the same slots/search and records the selection.
    t2 = _voice(sid, "show the first one")
    assert t2["selected_vehicle"] == 0
    assert t2["slots"]["budget"] is None  # nothing new provided
    assert len(t2["results"]) == 3


# --- 0-result robustness -----------------------------------------------------
def test_pipeline_zero_results_helpful():
    sid = "zero-sess"
    out = _voice(sid, "an electric mini truck in mumbai")
    assert out["results"] == []
    assert "no matching" in out["spoken"]
    assert out["matched_count"] == 0


# --- health ------------------------------------------------------------------
def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body


# --- latency log is written --------------------------------------------------
def test_latency_log_written(tmp_path, monkeypatch):
    from backend import main as main_mod
    target = tmp_path / "latency.log"
    monkeypatch.setattr(main_mod, "LATENCY_LOG", str(target))
    main_mod.log_latency("tlog", "mini truck", {"nlu": 1.1, "merge": 0.2,
                                                "search": 3.3, "rank": 0.4,
                                                "compose": 0.5, "stt": 0.0})
    lines = target.read_text().splitlines()
    assert len(lines) == 1
    assert "nlu=1.1ms" in lines[0] and "total=5.5ms" in lines[0]
    assert "tlog" in lines[0] and "mini truck" in lines[0]
