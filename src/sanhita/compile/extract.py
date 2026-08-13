"""Clause to proposed Obligations.

The contract of this module is narrow and strict:

**Zero is a normal answer.** Most clauses in a master circular carry no
obligation at all — headings, definitions, recitals, cross-references, tables of
contents for annexures. An extractor that finds a duty in every clause is
broken, and the statistics `sanhita compile` prints are designed to make that
failure obvious rather than flattering.

**Nothing is invented.** Every populated field records the character span of the
clause's own `verbatim_text` that justifies it. A field the extractor cannot
point at is left `None`. The one deliberate exception is `EvidenceReq`, which is
*derived* from the action verb rather than quoted — SEBI rarely names the
artifact explicitly — so it carries no span, a low per-field confidence, and
shows up in `unprovenanced_fields()` for the reviewer.

**One clause, many duties.** Each deontic verb in a clause opens a candidate
obligation, so "shall report ... and shall maintain ..." yields two, suffixed
`-a` and `-b` in source-text order.

Two engines implement this. `RuleExtractor` is deterministic and cannot
hallucinate by construction. `LLMExtractor` asks Claude for JSON conforming to
the IR, validates it with Pydantic, retries once with the validation error, and
then gives up and marks the clause EXTRACTION_FAILED rather than dropping it.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from enum import Enum

from sanhita.compile.temporal import RULESET_VERSION, parse_temporal
from sanhita.ir.enums import (
    Actor,
    ConditionKind,
    DeadlineKind,
    Modality,
    TriggerKind,
)
from sanhita.ir.schema import (
    Action,
    Condition,
    Deadline,
    EvidenceReq,
    ExtractionMeta,
    Obligation,
    SourceAnchor,
    Trigger,
    obligation_id,
)
from sanhita.parse.clause_tree import ClauseNode

__all__ = [
    "ClauseOutcome",
    "ExtractionStats",
    "ExtractionStatus",
    "RuleExtractor",
    "extract_clause",
]


class ExtractionStatus(str, Enum):
    """What happened to one clause. Every clause gets exactly one of these."""

    PROPOSED = "PROPOSED"
    NO_OBLIGATION = "NO_OBLIGATION"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"


@dataclass(slots=True)
class ClauseOutcome:
    """The result of compiling one clause. Never silently empty."""

    clause_id: str
    status: ExtractionStatus
    obligations: list[Obligation] = field(default_factory=list)
    reason: str = ""
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def mean_confidence(self) -> float:
        if not self.obligations:
            return 0.0
        return round(sum(o.confidence for o in self.obligations) / len(self.obligations), 3)


@dataclass(slots=True)
class ExtractionStats:
    """What a compile run did. Printed by `sanhita compile`."""

    clauses_processed: int = 0
    obligations_proposed: int = 0
    zero_obligation_clauses: int = 0
    extraction_failures: int = 0
    confidences: list[float] = field(default_factory=list)
    wall_seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    engine: str = "rules"
    model_id: str | None = None
    reasons: dict[str, int] = field(default_factory=dict)

    @property
    def mean_confidence(self) -> float:
        if not self.confidences:
            return 0.0
        return round(sum(self.confidences) / len(self.confidences), 3)

    @property
    def cost_usd(self) -> float:
        """Cost at Claude Opus 5 list rates: $5/MTok in, $25/MTok out.

        Zero for the rules engine, which makes no API calls at all.
        """
        return round(
            self.input_tokens / 1_000_000 * 5.0 + self.output_tokens / 1_000_000 * 25.0, 4
        )


# --------------------------------------------------------------------------
# Vocabulary, all observed in the corpus
# --------------------------------------------------------------------------

#: Order matters: "shall not" must be tested before "shall".
_DEONTIC: list[tuple[str, Modality]] = [
    (r"shall\s+not\s+be\s+(?:required|obliged|liable)\s+to", Modality.MAY),
    (r"shall\s+not", Modality.MUST_NOT),
    (r"should\s+not", Modality.SHOULD),
    (r"may\s+not", Modality.MUST_NOT),
    (r"must\s+not", Modality.MUST_NOT),
    (r"shall\s+also\s+be\s+required\s+to", Modality.MUST),
    (r"shall\s+be\s+required\s+to", Modality.MUST),
    (r"(?:are|is)\s+required\s+to", Modality.MUST),
    (r"(?:are|is)\s+mandated\s+to", Modality.MUST),
    # "will have to hold at least 40% ... for a period of three years" (4.3.2)
    # is as binding as "shall", and was invisible until this was added.
    (r"(?:will|would)\s+have\s+to", Modality.MUST),
    (r"(?:are|is)\s+(?:to\s+be|obliged\s+to)", Modality.MUST),
    (r"(?:are|is)\s+advised\s+to", Modality.SHOULD),
    (r"shall", Modality.MUST),
    (r"must", Modality.MUST),
    (r"should", Modality.SHOULD),
    (r"may", Modality.MAY),
]
_DEONTIC_RE = re.compile(
    "|".join(f"(?P<d{i}>\\b{pat}\\b)" for i, (pat, _) in enumerate(_DEONTIC)), re.I
)

#: Phrases where a deontic verb is grammatical furniture, not a duty. These are
#: the single biggest source of false positives: "shall mean" defines a term,
#: "shall be deemed" creates a legal fiction, neither imposes an action.
_NON_DEONTIC_CONTEXT = re.compile(
    r"\b(?:shall|should|may|must|will|would)\s+"
    r"(?:"
    # Definitions and legal fictions — these create meaning, not duties.
    r"mean|have\s+the\s+(?:same\s+)?meaning|be\s+deemed|be\s+construed|be\s+read|"
    r"stand\s+rescinded|come\s+into\s+force|be\s+applicable\s+from|"
    r"apply\s+(?:mutatis|from)|include\s+the\s+following\s*:"
    # Consequences — "failure ... shall result in penalty" describes what
    # happens to you, and is not something a firm can be asked to *do*.
    r"|result\s+in|attract|amount\s+to|be\s+liable|lead\s+to|not\s+arise|"
    r"be\s+treated\s+as|be\s+considered\s+as|be\s+subject\s+to\s+(?:penal|disciplinary)"
    r")\b",
    re.I,
)

_ACTORS: list[tuple[str, Actor]] = [
    (r"depository\s+participants?|DPs?\b", Actor.DEPOSITORY_PARTICIPANT),
    (r"clearing\s+corporations?|CCs?\b", Actor.CLEARING_CORPORATION),
    (r"clearing\s+members?", Actor.CLEARING_MEMBER),
    (r"stock\s+exchanges?|exchanges?\b|recognized\s+stock\s+exchanges?", Actor.STOCK_EXCHANGE),
    (r"depositor(?:y|ies)", Actor.DEPOSITORY),
    (r"market\s+infrastructure\s+institutions?|MIIs?\b", Actor.MII),
    (r"asset\s+management\s+compan(?:y|ies)|AMCs?\b", Actor.AMC),
    (r"registrars?\s+to\s+an?\s+issue|RTAs?\b", Actor.RTA),
    (r"investment\s+advisers?", Actor.INVESTMENT_ADVISER),
    (
        r"stock\s+brokers?|member\s+brokers?|trading\s+members?|TMs?\s*/\s*CMs?"
        r"|TMs?\b|qualified\s+stock\s+brokers?|QSBs?\b|SBs?\s*/\s*TMs?"
        # The circular also names the duty-bearer by role rather than by type.
        # In a stock-broker master circular these all denote the broker.
        r"|intermediar(?:y|ies)|registered\s+intermediar(?:y|ies)"
        r"|brokers?\b|members?\b",
        Actor.STOCK_BROKER,
    ),
]
_ACTOR_RE = re.compile(
    "|".join(f"(?P<a{i}>\\b(?:{pat}))" for i, (pat, _) in enumerate(_ACTORS)), re.I
)

#: "... shall be dated and time stamped by the brokers" — the duty-bearer sits
#: after the verb in a passive construction.
_PASSIVE_ACTOR = re.compile(
    r"\bby\s+(?P<actor>(?:the\s+|all\s+|such\s+|respective\s+){0,2}[A-Za-z][\w\s/'’-]{0,40})",
    re.I,
)

#: A preposition immediately governing the following noun, which therefore
#: cannot be the subject bearing the duty.
_PREPOSITION_TAIL = re.compile(
    r"\b(?:to|from|with|by|of|in|at|for|into|onto|upon|through|against|"
    r"between|among|towards?|under|over|before|after|per)\s+"
    r"(?:the\s+|a\s+|an\s+|any\s+|all\s+|such\s+|other\s+|its\s+|their\s+|"
    r"respective\s+|concerned\s+){0,3}$",
    re.I,
)

#: Action verb -> the artifact a firm would have to show. Derived, not quoted:
#: SEBI names the duty, not the audit trail, so this mapping is our contribution
#: and is marked as such on every EvidenceReq it produces.
_EVIDENCE_BY_VERB: dict[str, str] = {
    "report": "REPORT_FILING", "submit": "REPORT_FILING", "furnish": "REPORT_FILING",
    "file": "REPORT_FILING", "upload": "REPORT_FILING", "disclose": "REPORT_FILING",
    "intimate": "DISPATCH_LOG", "inform": "DISPATCH_LOG", "send": "DISPATCH_LOG",
    "issue": "DISPATCH_LOG", "dispatch": "DISPATCH_LOG", "communicate": "DISPATCH_LOG",
    "notify": "DISPATCH_LOG", "provide": "DISPATCH_LOG", "share": "DISPATCH_LOG",
    "maintain": "REGISTER", "preserve": "REGISTER", "retain": "REGISTER",
    "keep": "REGISTER", "record": "REGISTER", "document": "REGISTER",
    "obtain": "CLIENT_ACK", "collect": "CLIENT_ACK", "seek": "CLIENT_ACK",
    "audit": "AUDIT_REPORT", "inspect": "AUDIT_REPORT", "review": "AUDIT_REPORT",
    "reconcile": "RECONCILIATION", "settle": "RECONCILIATION",
    "pay": "BANK_STATEMENT", "transfer": "BANK_STATEMENT", "deposit": "BANK_STATEMENT",
    "monitor": "SYSTEM_LOG", "log": "SYSTEM_LOG", "block": "SYSTEM_LOG",
    "frame": "POLICY_DOCUMENT", "formulate": "POLICY_DOCUMENT",
    "establish": "POLICY_DOCUMENT", "put": "POLICY_DOCUMENT",
}
_DEFAULT_ARTIFACT = "REGISTER"

#: Adverbs and auxiliaries between the modal and the real verb. Deliberately
#: short: an earlier version also skipped "have" and "ensure that the <noun>",
#: which swallowed the verb itself and silently lost every "shall have in place"
#: and "should ensure that" duty in the corpus.
_VERB_AFTER_MODAL = re.compile(
    r"\s*(?:not\s+|also\s+|further\s+|thereafter\s+|immediately\s+|forthwith\s+|"
    r"promptly\s+|then\s+|duly\s+|continue\s+to\s+|be\s+)*"
    r"(?P<verb>[a-z]+(?:ed|ing)?)\b",
    re.I,
)

#: Words that are never the head verb of a duty.
_NOT_A_VERB = {
    "and", "or", "the", "a", "an", "to", "in", "of", "for", "that", "which",
    "such", "any", "all", "his", "her", "its", "their", "this", "these", "as",
    "at", "on", "by", "with", "from", "not", "no",
}

#: A prepositional phrase the verb governs, consumed before reading the object.
#: Bounded to a few words so it cannot swallow the object itself.
_LEADING_PREP = re.compile(
    r"\s*(?:to|for|with|on|by|at|in|from|through|upon|into|onto|towards)\s+"
    r"(?:the\s+|their\s+|its\s+|his\s+|all\s+|such\s+|a\s+|an\s+)?"
    r"(?:[A-Za-z][\w\-'’/()+]*)(?:\s+[A-Za-z][\w\-'’/()+]*){0,3}"
    r"(?=\s+(?:the|such|all|its|their|a|an|to|for|with|on|by|at|in|from)\s|\s*[.,;:]|$)",
)

_RECIPIENT_RE = re.compile(
    r"\bto\s+(?:the\s+|their\s+|its\s+|all\s+)?(?P<recipient>"
    r"stock\s+exchanges?|exchanges?|SEBI|the\s+Board|depositor(?:y|ies)|"
    r"clearing\s+corporations?|clients?|investors?|members?|board\s+of\s+directors)\b",
    re.I,
)
_MEDIUM_RE = re.compile(
    r"\b(?:through|by\s+way\s+of|via|by\s+means\s+of|using)\s+"
    r"(?P<medium>(?:SMS|e-?mail|electronic\s+\w+|the\s+\w+\s+portal|"
    r"physical\s+\w+|post|courier)(?:\s+and\s+e-?mail)?)\b",
    re.I,
)

_CONDITION_RE = re.compile(
    r"\b(?P<kind>unless|except\s+(?:where|in\s+case|that)?|provided\s+that|"
    r"if|in\s+case\s+(?:of|where)?|where\s+the|subject\s+to)\b\s*(?P<body>[^.;]{5,160})",
    re.I,
)
_THRESHOLD_RE = re.compile(
    r"(?:exceed(?:s|ing)?|more\s+than|greater\s+than|at\s+least|minimum\s+of|"
    r"not\s+less\s+than|above)\s+"
    r"(?:Rs\.?\s*|INR\s*|₹\s*)?(?P<value>[\d,]+(?:\.\d+)?)\s*"
    r"(?P<unit>crores?|lakhs?|per\s*cent|percent|%)?",
    re.I,
)

_EVENT_TRIGGER_RE = re.compile(
    r"\b(?:on|upon|in\s+the\s+event\s+of|whenever|as\s+soon\s+as|when|after)\s+"
    r"(?P<event>[a-z][a-z\s\-']{4,70}?)"
    r"(?=[.,;]|\s+(?:shall|the|and|or)\b|$)",
    re.I,
)


# --------------------------------------------------------------------------
# Rule extractor
# --------------------------------------------------------------------------


_OBJECT_BOUNDARY = re.compile(
    r"[.,;:]|\s+(?:to|for|with|within|on|by|through|in\s+the|as\s+per|and\s+shall|"
    r"which|that\s+the|whether|unless|provided)\b",
    re.I,
)
_MAX_OBJECT_WORDS = 14


def _read_object(text: str, start: int) -> tuple[str, int, int] | None:
    """Read the object of the verb, truncating rather than failing.

    An earlier version required a clause boundary within 13 words and returned
    None otherwise, which silently discarded every long-object duty in the
    corpus — "shall conduct appropriate due diligence in selecting the third
    party and in monitoring of its performance" has fifteen. Truncating records
    a slightly short object; failing records no obligation at all, which is far
    worse.
    """
    window = text[start : start + 240]
    lead = re.match(r"\s*", window)
    offset = lead.end() if lead else 0
    body = window[offset:]
    if not body.strip():
        return None

    boundary = _OBJECT_BOUNDARY.search(body)
    candidate = body[: boundary.start()] if boundary else body

    words = candidate.split()
    if not words:
        return None
    if len(words) > _MAX_OBJECT_WORDS:
        candidate = " ".join(words[:_MAX_OBJECT_WORDS])

    consumed = len(candidate.rstrip())
    obj = " ".join(candidate.split())[:90]
    if not obj:
        return None
    begin = start + offset
    return obj, begin, begin + consumed


class RuleExtractor:
    """Deterministic extraction. Cannot invent text, by construction."""

    engine = "rules"
    ruleset_version = f"rules-1.0.0+{RULESET_VERSION}"

    def __init__(self, *, circular_id: str, prefix: str = "SB") -> None:
        self.circular_id = circular_id
        self.prefix = prefix

    # ------------------------------------------------------------- helpers

    def _modality_at(self, text: str, match: re.Match[str]) -> Modality | None:
        for index, (_pattern, modality) in enumerate(_DEONTIC):
            if match.group(f"d{index}"):
                return modality
        return None

    def _actor_before(self, text: str, at: int) -> tuple[Actor, tuple[int, int]] | None:
        """The actor that is the *subject* of the deontic verb.

        Taking the nearest match to the left is wrong, because the nearest noun
        is usually the object of a preposition rather than the party under a
        duty: in "for operating in any other Clearing Corporation or any Stock
        Exchange, the entity shall follow ...", the last actor-shaped phrase
        before "shall" is "Stock Exchange", which is not who must act.

        So preposition-governed mentions are skipped, and of what remains the
        *earliest* member of the subject cluster nearest the verb is taken —
        "Stock Exchanges and/or Depositories ... shall ensure" is a joint duty
        led by the exchanges.
        """
        offset = max(0, at - 220)
        window = text[offset:at]

        candidates: list[tuple[int, Actor, tuple[int, int]]] = []
        for match in _ACTOR_RE.finditer(window):
            for index, (_pattern, actor) in enumerate(_ACTORS):
                if not match.group(f"a{index}"):
                    continue
                preceding = window[max(0, match.start() - 24) : match.start()]
                if _PREPOSITION_TAIL.search(preceding):
                    break  # governed by a preposition; not the subject
                candidates.append(
                    (match.start(), actor, (offset + match.start(), offset + match.end()))
                )
                break

        if not candidates:
            # Passive voice names the actor after the verb: "requests should be
            # dated and time stamped by the brokers".
            passive = _PASSIVE_ACTOR.search(text, at, at + 200)
            if passive:
                inner = _ACTOR_RE.search(passive.group("actor"))
                if inner:
                    for index, (_pattern, actor) in enumerate(_ACTORS):
                        if inner.group(f"a{index}"):
                            base = passive.start("actor")
                            return actor, (base + inner.start(), base + inner.end())

            # Last resort: allow a preposition-governed mention. "In case of a
            # broker who ..., he shall be required to disclose" puts the real
            # subject behind "of", and refusing it loses the duty entirely.
            for match in _ACTOR_RE.finditer(window):
                for index, (_pattern, actor) in enumerate(_ACTORS):
                    if match.group(f"a{index}"):
                        return actor, (offset + match.start(), offset + match.end())
            return None

        # Keep the cluster closest to the verb, then read it left to right.
        last_start = candidates[-1][0]
        cluster = [c for c in candidates if last_start - c[0] <= 60]
        _start, actor, span = cluster[0]
        return actor, span

    def _action_after(
        self, text: str, at: int
    ) -> tuple[Action, dict[str, tuple[int, int]]] | None:
        tail = text[at : at + 320]
        verb_match = _VERB_AFTER_MODAL.match(tail)
        if not verb_match:
            return None
        verb = verb_match.group("verb").lower()
        if verb in _NOT_A_VERB:
            return None

        verb_start = at + verb_match.start("verb")
        verb_end = at + verb_match.end("verb")

        # Skip any prepositional phrases the verb governs before the object.
        # "report to the Stock Exchange on T+5 day the actual short-collection"
        # must yield "the actual short-collection", not "to the Stock Exchange":
        # the recipient and the horizon have their own fields, and reading the
        # first phrase as the object records the wrong duty entirely.
        cursor = verb_end
        for _ in range(4):
            skip = _LEADING_PREP.match(text, cursor)
            if not skip:
                break
            cursor = skip.end()

        found = _read_object(text, cursor)
        if found is None and cursor != verb_end:
            # Skipping the prepositional phrases consumed the object itself, as
            # in "should be accessible from both systems". Retry unskipped.
            cursor = verb_end
            found = _read_object(text, cursor)
        if found is None:
            return None
        obj, obj_start, obj_end = found

        spans = {"action.verb": (verb_start, verb_end), "action.object": (obj_start, obj_end)}

        recipient = medium = None
        scope = text[at : at + 320]
        recipient_match = _RECIPIENT_RE.search(scope)
        if recipient_match:
            recipient = " ".join(recipient_match.group("recipient").split())
            spans["action.recipient"] = (
                at + recipient_match.start("recipient"),
                at + recipient_match.end("recipient"),
            )
        medium_match = _MEDIUM_RE.search(scope)
        if medium_match:
            medium = " ".join(medium_match.group("medium").split())
            spans["action.medium"] = (
                at + medium_match.start("medium"),
                at + medium_match.end("medium"),
            )

        return (
            Action(verb=verb, object=obj, recipient=recipient, medium=medium),
            spans,
        )

    def _conditions(self, text: str) -> tuple[list[Condition], dict[str, tuple[int, int]]]:
        conditions: list[Condition] = []
        spans: dict[str, tuple[int, int]] = {}

        for index, match in enumerate(_CONDITION_RE.finditer(text)):
            head = match.group("kind").lower()
            body = " ".join(match.group("body").split()).strip(" ,;.")
            if len(body) < 5:
                continue
            kind = (
                ConditionKind.EXEMPTION
                if head.startswith(("unless", "except"))
                else ConditionKind.PRECONDITION
            )
            conditions.append(Condition(kind=kind, expression=body[:160]))
            spans[f"conditions[{index}]"] = (match.start(), match.end())
            if len(conditions) >= 4:
                break

        threshold = _THRESHOLD_RE.search(text)
        if threshold:
            raw = threshold.group("value").replace(",", "")
            try:
                value = float(raw)
            except ValueError:
                value = None
            if value is not None:
                unit = (threshold.group("unit") or "").lower()
                key = "threshold_pct" if "cent" in unit or "%" in unit else "threshold_value"
                multiplier = {"crore": 1e7, "crores": 1e7, "lakh": 1e5, "lakhs": 1e5}.get(unit, 1)
                conditions.append(
                    Condition(
                        kind=ConditionKind.THRESHOLD,
                        expression=" ".join(threshold.group(0).split()),
                        parameters={key: value * multiplier, "unit": unit or "count"},
                    )
                )
                spans[f"conditions[{len(conditions) - 1}]"] = (
                    threshold.start(),
                    threshold.end(),
                )
        return conditions, spans

    def _evidence(self, action: Action) -> tuple[list[EvidenceReq], float]:
        """Derive the artifact a firm must be able to show. Never quoted."""
        artifact = _EVIDENCE_BY_VERB.get(action.verb)
        confidence = 0.6 if artifact else 0.25
        return (
            [
                EvidenceReq(
                    artifact_type=artifact or _DEFAULT_ARTIFACT,
                    producible_on_demand=True,
                    description=(
                        f"Derived from the action '{action.verb} {action.object}'. "
                        "Not stated in the clause; confirm at certification."
                    ),
                )
            ],
            confidence,
        )

    def _trigger(
        self, text: str, recurrence: str | None
    ) -> tuple[Trigger, dict[str, tuple[int, int]]]:
        if recurrence:
            return (
                Trigger(
                    kind=TriggerKind.SCHEDULE,
                    expression="schedule.due",
                    recurrence=recurrence,
                ),
                {},
            )
        event = _EVENT_TRIGGER_RE.search(text)
        if event:
            phrase = " ".join(event.group("event").split())
            if len(phrase.split()) >= 2:
                return (
                    Trigger(kind=TriggerKind.EVENT, expression=phrase[:90]),
                    {"trigger": (event.start(), event.end())},
                )
        return Trigger(kind=TriggerKind.CONTINUOUS, expression="always"), {}

    # -------------------------------------------------------------- public

    def extract(self, node: ClauseNode) -> ClauseOutcome:
        """Compile one clause node. Returns exactly one outcome."""
        text = node.text

        if node.kind in ("SECTION", "ANNEXURE", "APPENDIX"):
            return ClauseOutcome(node.id, ExtractionStatus.NO_OBLIGATION, reason="heading")
        if len(text.split()) < 6:
            return ClauseOutcome(node.id, ExtractionStatus.NO_OBLIGATION, reason="too-short")

        drafts: list[dict] = []
        provenances: list[dict[str, tuple[int, int]]] = []
        confidences: list[dict[str, float]] = []

        temporal = parse_temporal(text)

        for match in _DEONTIC_RE.finditer(text):
            modality = self._modality_at(text, match)
            if modality is None:
                continue

            # "shall mean" / "shall be deemed" are grammar, not duty.
            following = text[match.start() : match.start() + 60]
            if _NON_DEONTIC_CONTEXT.match(following):
                continue

            actor_hit = self._actor_before(text, match.start())
            if actor_hit is None:
                continue
            actor, actor_span = actor_hit

            action_hit = self._action_after(text, match.end())
            if action_hit is None:
                continue
            action, action_spans = action_hit

            conditions, condition_spans = self._conditions(text)
            evidence, evidence_confidence = self._evidence(action)
            trigger, trigger_spans = self._trigger(text, temporal.recurrence)

            # Only a positive duty produces something a firm can be asked to
            # show. A prohibition has no artifact proving a thing was *not*
            # done, and a permission ("may appoint an authorised person")
            # obliges nobody to file anything. Attaching evidence to either
            # would invent a filing requirement the regulation never imposed.
            if modality in (Modality.MUST_NOT, Modality.MAY):
                evidence = []
                per_field_evidence = None
            else:
                per_field_evidence = evidence_confidence

            deadline = temporal.deadline
            if trigger.kind is TriggerKind.SCHEDULE and deadline is None:
                deadline = Deadline(kind=DeadlineKind.END_OF_PERIOD, period="DAY")

            provenance: dict[str, tuple[int, int]] = {
                "modality": (match.start(), match.end()),
                "actor": actor_span,
                **action_spans,
                **condition_spans,
                **trigger_spans,
            }
            if deadline is not None and "deadline" in temporal.spans:
                provenance["deadline"] = temporal.spans["deadline"]
            if temporal.recurrence and "trigger.recurrence" in temporal.spans:
                provenance["trigger.recurrence"] = temporal.spans["trigger.recurrence"]

            per_field: dict[str, float] = {
                "modality": 0.97,
                "actor": 0.85,
                "action.verb": 0.80,
                "action.object": 0.62,
            }
            if per_field_evidence is not None:
                per_field["evidence[0]"] = per_field_evidence
            if deadline is not None:
                per_field["deadline"] = temporal.confidence or 0.5

            drafts.append(
                dict(
                    actor=actor,
                    modality=modality,
                    action=action,
                    trigger=trigger,
                    deadline=deadline,
                    conditions=conditions,
                    evidence=evidence,
                    confidence=round(
                        sum(per_field.values()) / len(per_field), 3
                    ),
                )
            )
            provenances.append(provenance)
            confidences.append(per_field)

        if not drafts:
            return ClauseOutcome(
                node.id,
                ExtractionStatus.NO_OBLIGATION,
                reason="no-deontic-duty",
            )

        anchor = SourceAnchor(
            circular_id=self.circular_id,
            section=node.section or node.id.split(".")[0],
            clause_id=node.id,
            page=node.page,
            char_span=node.char_span,
            verbatim_text=text,
            sha256=node.sha256,
        )
        meta = ExtractionMeta(
            engine=self.engine,
            extracted_at=_dt.datetime.now(_dt.timezone.utc),
            ruleset_version=self.ruleset_version,
        )

        obligations: list[Obligation] = []
        for index, draft in enumerate(drafts):
            try:
                obligations.append(
                    Obligation(
                        id=obligation_id(self.prefix, node.id, index),
                        source=anchor,
                        extraction=meta,
                        field_provenance=provenances[index],
                        field_confidence=confidences[index],
                        **draft,
                    )
                )
            except Exception as exc:  # validation is the contract; report, never drop
                return ClauseOutcome(
                    node.id,
                    ExtractionStatus.EXTRACTION_FAILED,
                    reason="ir-validation",
                    error=f"{type(exc).__name__}: {exc}",
                )

        return ClauseOutcome(
            node.id, ExtractionStatus.PROPOSED, obligations=obligations, reason="deontic"
        )


def extract_clause(node: ClauseNode, *, circular_id: str, prefix: str = "SB") -> ClauseOutcome:
    """Convenience wrapper around a fresh `RuleExtractor`."""
    return RuleExtractor(circular_id=circular_id, prefix=prefix).extract(node)
