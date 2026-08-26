"""
test_memory.py -- conversation memory: slot merge, follow-up resolution,
negation (fuel replace-not-AND), selected vehicle, and the session store.
"""
from memory import conversation
from services import nlu


# --- merge_slots: update-only-what's-provided --------------------------------
def test_merge_only_updates_provided():
    old = {"budget": 500_000, "city": "Mumbai", "fuel": None, "body_type": "mini truck"}
    merged = conversation.merge_slots({"fuel": "CNG"}, old)
    assert merged["fuel"] == "CNG"
    assert merged["budget"] == 500_000          # kept
    assert merged["city"] == "Mumbai"           # kept
    assert merged["body_type"] == "mini truck"  # kept


def test_merge_ignores_none_values():
    merged = conversation.merge_slots(
        {"budget": None, "fuel": "CNG", "city": None}, {"budget": 600_000, "city": "Pune"}
    )
    # None-provided keys must NOT clear existing values.
    assert merged["budget"] == 600_000
    assert merged["city"] == "Pune"
    assert merged["fuel"] == "CNG"


def test_merge_replaces_a_value_not_accumulates():
    # Same slot provided twice across turns -> replace (last wins), not AND.
    merged = conversation.merge_slots({"fuel": "CNG"}, {"fuel": "Diesel"})
    assert merged["fuel"] == "CNG"


# --- follow-up resolution ----------------------------------------------------
def test_follow_up_first_variants():
    assert conversation.resolve_follow_up("show the first one") == 0
    assert conversation.resolve_follow_up("first one") == 0
    assert conversation.resolve_follow_up("open the first") == 0
    assert conversation.resolve_follow_up("show me the 1st") == 0


def test_follow_up_second_and_third():
    assert conversation.resolve_follow_up("show the second one") == 1
    assert conversation.resolve_follow_up("open the second") == 1
    assert conversation.resolve_follow_up("tell me about the 2nd") == 1
    assert conversation.resolve_follow_up("the third one please") == 2
    assert conversation.resolve_follow_up("pick three") == 2


def test_follow_up_not_misread():
    # Vague numbers without a pick context must not be treated as a selection.
    assert conversation.resolve_follow_up("show me mini trucks") is None
    assert conversation.resolve_follow_up("I want two trucks") is None
    assert conversation.resolve_follow_up("") is None


# --- negation: fuel is replaced, not AND-ed ----------------------------------
def test_negation_replaces_fuel():
    parsed = nlu.extract_slots("not diesel, CNG")
    assert parsed["slots"]["fuel"] == "CNG"
    assert parsed["negations"] == ["fuel"]
    # And when merged over an existing fuel, the positive fuel wins.
    merged = conversation.merge_slots(parsed["slots"], {"fuel": "Diesel"})
    assert merged["fuel"] == "CNG"


def test_or_only_semantics():
    parsed = nlu.extract_slots("only CNG")
    assert parsed["slots"]["fuel"] == "CNG"
    assert parsed["negations"] == []


# --- apply_parse & state -----------------------------------------------------
def test_apply_parse_sets_selected_vehicle():
    state = conversation.ConversationState()
    parsed = {
        "slots": {"city": "Mumbai"},
        "selected_index": 0,
        "negations": [],
        "is_fallback": True,
    }
    conversation.apply_parse(state, parsed)
    assert state.selected_vehicle == 0
    assert state.slots["city"] == "Mumbai"


def test_conversation_state_default_slots():
    state = conversation.ConversationState()
    assert set(state.slots.keys()) == set(conversation.SLOT_KEYS)
    assert all(v is None for v in state.slots.values())
    assert state.selected_vehicle is None
    assert state.history == []


def test_session_store_get_and_reset():
    store = conversation.SessionStore()
    a = store.get("sess-1")
    a.slots["city"] = "Pune"
    # Same id -> same state object.
    assert store.get("sess-1").slots["city"] == "Pune"
    # Different id -> fresh state.
    assert store.get("sess-2").slots["city"] is None
    store.reset("sess-1")
    assert store.get("sess-1").slots["city"] is None
