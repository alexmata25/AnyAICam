"""AAC Voice Call visitor-intent classification, Phase 1.

Mirrors aaco.py's own DeterministicLanguageAdapter shape deliberately:
a frozen result dataclass, a Protocol interface, and one concrete,
regex/keyword-based implementation -- "a deterministic intent-
classification layer is acceptable if AACO is not ready yet, but keep
it behind a clean interface" (the product spec's own words). A future
AACO/NLP-backed classifier is a drop-in replacement for
DeterministicVisitorIntentClassifier; no caller (aac_voice_call.py)
depends on anything beyond the VisitorIntentClassifier Protocol.

Deliberately NOT exact-phrase-only matching: KEYWORD_INTENTS below maps
several phrasings/synonyms per canonical intent, and classify() does
substring matching against a normalized (lowercased, whitespace-
collapsed) transcript, not a fixed set of exact sentences -- "I'm here
for a delivery", "got a package for you", and "delivery" all resolve to
the same "delivery" intent. Confidence is a simple, honest heuristic
(1.0 for a recognized phrase, 0.0 for no match at all) -- deliberately
not dressed up as a real ML probability, since it isn't one; the field
exists so a real classifier can report a real one later without any
caller's contract changing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

# Canonical intents this Phase 1 slice recognizes. "unknown" is a real,
# always-possible outcome -- never a caller error -- for a transcript
# that doesn't match anything recognized; homeowner notification still
# happens (a person triggered the entrance camera and something was
# said), just without a confident, human-readable intent label.
GREETING = "greeting"
PRESENCE_CHECK = "presence_check"
DELIVERY = "delivery"
MAINTENANCE = "maintenance"
VISITOR = "visitor"
UNKNOWN = "unknown"

# Ordered: more specific intents are checked before the generic
# "visitor" catch-all phrase, so "maintenance visitor" resolves to
# maintenance, not visitor. Each canonical intent maps to several
# phrasings/keywords a real front-door conversation might actually
# produce, not one exact sentence.
KEYWORD_INTENTS: dict[str, tuple[str, ...]] = {
    DELIVERY: ("delivery", "package", "dropping off", "drop off", "courier", "mail carrier", "amazon", "ups", "fedex"),
    MAINTENANCE: ("maintenance", "technician", "repair", "here to fix", "service call", "meter reading"),
    PRESENCE_CHECK: ("is anybody home", "anyone home", "anybody there", "is someone home", "is anyone there"),
    GREETING: ("hello", "hi there", "hey there", "i'm here", "im here", "knock knock"),
    VISITOR: ("visitor", "here to see", "here for", "guest"),
}


@dataclass(frozen=True)
class VisitorIntent:
    """Result of classifying one visitor utterance. `matched_phrase` is
    the specific keyword/phrase that triggered the match (None for
    UNKNOWN), kept for audit/debugging -- never shown to the homeowner
    as if it were the visitor's exact words; `transcript_text` (stored
    separately by the caller) is the real transcript."""

    intent: str
    confidence: float
    matched_phrase: str | None = None


class VisitorIntentClassifier(Protocol):
    """The one interface aac_voice_call.py depends on. A future AACO/
    NLP-backed implementation satisfies this same Protocol -- no other
    change required anywhere else in the Voice Call feature."""

    def classify(self, transcript_text: str) -> VisitorIntent: ...


class DeterministicVisitorIntentClassifier:
    """Phase 1's real implementation: normalized substring/keyword
    matching against KEYWORD_INTENTS, first match wins in dict-
    iteration order (Python dicts preserve insertion order, so the
    more-specific-first ordering above is meaningful, not accidental)."""

    def classify(self, transcript_text: str) -> VisitorIntent:
        normalized = " ".join((transcript_text or "").lower().split())
        if not normalized:
            return VisitorIntent(intent=UNKNOWN, confidence=0.0, matched_phrase=None)
        for intent, phrases in KEYWORD_INTENTS.items():
            for phrase in phrases:
                if phrase in normalized:
                    return VisitorIntent(intent=intent, confidence=1.0, matched_phrase=phrase)
        return VisitorIntent(intent=UNKNOWN, confidence=0.0, matched_phrase=None)
