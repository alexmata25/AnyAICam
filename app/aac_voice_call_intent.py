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

import re
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
    more-specific-first ordering above is meaningful, not accidental).

    Left exactly as Phase 1 shipped it -- trigger_visitor_event()'s own
    default classifier for the customer-triggered simulate-trigger
    route, which already has a real transcript available at call time
    and whose existing tests pin this exact behavior. The proactive
    listening-window flow (2026-09-23, aac_voice_call.py's
    record_visitor_utterance()) uses NaturalLanguageVisitorIntentClassifier
    below instead -- see that class's own docstring for why."""

    def classify(self, transcript_text: str) -> VisitorIntent:
        normalized = " ".join((transcript_text or "").lower().split())
        if not normalized:
            return VisitorIntent(intent=UNKNOWN, confidence=0.0, matched_phrase=None)
        for intent, phrases in KEYWORD_INTENTS.items():
            for phrase in phrases:
                if phrase in normalized:
                    return VisitorIntent(intent=intent, confidence=1.0, matched_phrase=phrase)
        return VisitorIntent(intent=UNKNOWN, confidence=0.0, matched_phrase=None)


# Broader signal coverage for the proactive listening-window flow
# (2026-09-23) -- many more real phrasings per canonical intent than
# KEYWORD_INTENTS above, and confidence now reflects how many distinct
# signals matched rather than a flat 1.0/0.0, so "I've got a package
# for you, it needs a signature" (two delivery signals) is reported
# more confidently than a single bare "package". Still deterministic,
# not a real ML/embedding model -- exactly the same honesty posture
# VisitorIntent.confidence's own docstring already commits to, and the
# same caliber of "genuine understanding through normalization + broad
# pattern coverage, not a bigger fixed-phrase table" aaco.py's own
# DeterministicLanguageAdapter/_normalize() already established as
# this codebase's real bar for "not a fixed phrase list" -- see that
# module's docstring for the identical design philosophy applied to a
# different (VMS command, not visitor-intent) grammar.
_SIGNAL_INTENTS: dict[str, tuple[str, ...]] = {
    DELIVERY: (
        "delivery", "deliveries", "package", "packages", "parcel", "dropping off", "drop off", "dropped off",
        "courier", "mail carrier", "mailman", "postal", "amazon", "ups", "fedex", "dhl", "usps",
        "leave it at the door", "leave this here", "needs a signature", "sign for", "got a box",
    ),
    MAINTENANCE: (
        "maintenance", "technician", "repair", "here to fix", "fixing", "service call", "servicing",
        "meter reading", "reading the meter", "inspector", "inspection", "exterminator", "plumber",
        "electrician", "here to install", "scheduled appointment", "work order",
    ),
    PRESENCE_CHECK: (
        "is anybody home", "anyone home", "anybody there", "is someone home", "is anyone there",
        "anyone here", "is anybody here", "hello is anyone", "somebody home",
    ),
    GREETING: (
        "hello", "hi there", "hey there", "i'm here", "im here", "knock knock", "good morning",
        "good afternoon", "good evening", "just saying hi", "just stopping by to say hi",
    ),
    VISITOR: (
        "visitor", "here to see", "here for", "guest", "friend of", "i'm a friend", "stopping by",
        "visiting", "here to visit", "family member", "relative",
    ),
}

# Explicit, narrow urgency/distress signals -- deliberately separate
# from the intent-keyword tables above (an urgent delivery is still a
# DELIVERY intent; urgency is an orthogonal signal used only to decide
# escalate-vs-continue in aac_voice_call.py's record_visitor_utterance(),
# never to change what intent is reported, and NEVER to influence door
# authorization -- see that function's own docstring for why).
URGENT_SIGNALS: tuple[str, ...] = (
    "emergency", "urgent", "help me", "please help", "it's an emergency", "call 911",
    "right now", "immediately", "hurry",
)


class NaturalLanguageVisitorIntentClassifier:
    """The proactive listening-window flow's real classifier (2026-09-23):
    normalization (case/punctuation/whitespace-insensitive, matching
    aaco.py's own _normalize() posture) plus broad, multi-signal,
    word-boundary phrase matching against _SIGNAL_INTENTS -- many
    natural phrasings per canonical intent, not one fixed sentence per
    intent, and NOT simple substring containment (word-boundary regex
    avoids a false match inside an unrelated word, e.g. "ups" never
    matching inside "backups"). confidence scales with how many
    distinct signals for the winning intent were found (capped at
    1.0), so a visitor who says more clearly what they want is reported
    more confidently, not just "matched or didn't"."""

    def classify(self, transcript_text: str) -> VisitorIntent:
        normalized = " ".join((transcript_text or "").lower().split())
        normalized = re.sub(r"[.,!?;:]+", " ", normalized)
        normalized = " ".join(normalized.split())
        if not normalized:
            return VisitorIntent(intent=UNKNOWN, confidence=0.0, matched_phrase=None)

        best_intent = UNKNOWN
        best_matches: list[str] = []
        for intent, phrases in _SIGNAL_INTENTS.items():
            matches = [phrase for phrase in phrases if re.search(rf"\b{re.escape(phrase)}\b", normalized)]
            if len(matches) > len(best_matches):
                best_intent, best_matches = intent, matches
        if not best_matches:
            return VisitorIntent(intent=UNKNOWN, confidence=0.0, matched_phrase=None)
        confidence = min(1.0, 0.6 + 0.2 * len(best_matches))
        return VisitorIntent(intent=best_intent, confidence=confidence, matched_phrase=best_matches[0])

    @staticmethod
    def has_urgent_signal(transcript_text: str) -> bool:
        normalized = " ".join((transcript_text or "").lower().split())
        return any(re.search(rf"\b{re.escape(signal)}\b", normalized) for signal in URGENT_SIGNALS)
