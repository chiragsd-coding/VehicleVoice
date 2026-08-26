"""
conversation.py -- in-memory conversation memory for the voice pipeline.

Holds per-session state (search slots, selected vehicle, transcript history) and
the multi-turn rules that let a follow-up like "only CNG" or "show the first
one" make sense:

* merge_slots -- update-only-what's-provided semantics. A turn that only
  mentions CNG keeps the earlier budget/city and just sets fuel.
* resolve_follow_up -- "show the first one" / "open the second" resolve to a
  zero-based index into the last result list (stored on selected_vehicle).
* negation -- "no" / "not diesel, CNG" replaces fuel rather than AND-ing it.
  Replacing is exactly what merge_slots already does (last provided value wins);
  NLU is responsible for not emitting a negated fuel as a wanted one.

Sessions live in a thread-safe in-memory store keyed by session_id. No
persistence yet -- it is a dict guarded by a lock.
"""
from __future__ import annotations

import re
import threading
from typing import Optional

# Slot keys the conversation can carry across turns.
SLOT_KEYS = ["budget", "fuel", "body_type", "city", "purpose"]

# Ordinal words -> zero-based index into the last result list.
ORDINALS = {
    "first": 0, "1st": 0, "one": 0,
    "second": 1, "2nd": 1, "two": 1,
    "third": 2, "3rd": 2, "three": 2,
}

# Explicit ordinals are always a pick ("first", "the 3rd", "second one").
_ORDINAL_RE = re.compile(r"\b(the\s+)?(first|1st|second|2nd|third|3rd)(\s+one)?\b", re.I)
# Vague numbers ("one/two/three/1/2/3") only count as a pick in a pick context.
_PICK_CONTEXT_RE = re.compile(
    r"\b(number|no\.?|option|pick|select|choose|show|open|take|get)\s+([123]|one|two|three)\b",
    re.I,
)


class ConversationState:
    """Per-session conversational state."""

    def __init__(self, slots: Optional[dict] = None):
        self.slots: dict = dict(slots) if slots is not None else {k: None for k in SLOT_KEYS}
        self.selected_vehicle: Optional[int] = None
        self.history: list = []

    def to_dict(self) -> dict:
        return {
            "slots": dict(self.slots),
            "selected_vehicle": self.selected_vehicle,
            "history": list(self.history),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ConversationState({self.slots!r}, selected={self.selected_vehicle!r})"


def merge_slots(new: dict, old: Optional[dict]) -> dict:
    """Update-only-what's-provided slot merge.

    Returns a new dict: every key in `new` whose value is not None overrides the
    corresponding key in `old`; all other keys carry over unchanged.
    "Only CNG" arrives as {"fuel": "CNG"} and therefore keeps budget/city intact.
    """
    merged = dict(old or {})
    for key, value in (new or {}).items():
        if value is not None:
            merged[key] = value
    return merged


def resolve_follow_up(transcript: str) -> Optional[int]:
    """Return the zero-based index of the vehicle the user is picking, else None.

    Matches "show the first one", "open the second", "first one", "the 3rd" and
    "pick two". Bare "two"/"three" without a pick context is NOT treated as a
    selection, so "two trucks" is not misread as picking.

    >>> resolve_follow_up("show the first one")
    0
    >>> resolve_follow_up("open the second")
    1
    >>> resolve_follow_up("just show me mini trucks")
    None
    """
    if not transcript:
        return None
    t = re.sub(r"[^a-z0-9\s]", " ", transcript.lower())

    m = _ORDINAL_RE.search(t)
    if m:
        return ORDINALS[m.group(2).lower()]

    m = _PICK_CONTEXT_RE.search(t)
    if m:
        return ORDINALS[m.group(2).lower()]
    return None


class SessionStore:
    """Thread-safe in-memory store of ConversationState keyed by session_id."""

    def __init__(self):
        self._sessions: dict = {}
        self._lock = threading.RLock()

    def get(self, session_id: str) -> ConversationState:
        """Return the (fresh if new) state for a session."""
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                state = ConversationState()
                self._sessions[session_id] = state
            return state

    def reset(self, session_id: str) -> None:
        """Drop a session's state (used by tests / long-idle cleanup)."""
        with self._lock:
            self._sessions.pop(session_id, None)


def apply_parse(state: ConversationState, parsed: dict) -> ConversationState:
    """Apply an NLU parse dict onto a conversation state (in place).

    parsed expected keys:
      slots           -> dict of detected slots (merged with existing)
      selected_index  -> int|None follow-up pick, recorded on selected_vehicle
    Returns the (mutated) state.
    """
    slots = parsed.get("slots")
    if slots:
        state.slots = merge_slots(slots, state.slots)
    idx = parsed.get("selected_index")
    if idx is not None:
        state.selected_vehicle = int(idx)
    return state
