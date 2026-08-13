"""Deterministic temporal parsing: clause English to Deadline, Trigger and Commencement.

This is the part of extraction that must not be probabilistic. A deadline is
the field a compliance engine actually computes against, so "within seven
working days of the following month" has to become the same typed structure on
every run, on every machine, for ever. A language model that is 97% reliable
here is not good enough: the 3% is a missed filing.

So the rules run first and the model only sees what they decline. Every reading
carries the character span that produced it, the name of the rule that fired,
and a confidence — and a phrase no rule recognises returns nothing at all rather
than a guess.

Shapes recovered, all drawn from the corpus rather than invented:

    "on T+5 day"                      RELATIVE  offset_days=5  business=False
    "within two working days"         RELATIVE  offset_days=2  business=True
    "within seven calendar days"      RELATIVE  offset_days=7  business=False
    "within 24 hours of pay-out"      RELATIVE  offset_hours=24
    "within six months from ..."      RELATIVE  offset_months=6
    "by the end of trading day"       END_OF_PERIOD  period=DAY
    "at the end of the T day"         END_OF_PERIOD  period=DAY  anchor=T
    "end of the half year"            END_OF_PERIOD  period=HALF_YEAR
    "on a daily basis"                SCHEDULE  FREQ=DAILY
    "on a half yearly basis"          SCHEDULE  FREQ=MONTHLY;INTERVAL=6
    "as and when called for"          ON_DEMAND
    "immediately" / "forthwith"       RELATIVE  offset_hours=0
    "not later than one hour"         RELATIVE  offset_hours=1
    "with effect from April 01, 2019" Commencement FIXED_DATE
    "three months after the exchanges
     issue operational guidelines"    Commencement CONDITIONAL_ON_EVENT
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Iterable

from sanhita.ir.enums import CommencementKind, DayCount, DeadlineKind, TriggerKind
from sanhita.ir.schema import Commencement, Deadline

__all__ = [
    "RULESET_VERSION",
    "TemporalReading",
    "parse_commencement",
    "parse_recurrence",
    "parse_temporal",
    "word_to_int",
]

#: Bumped whenever a rule changes meaning. Recorded on every proposal, so a
#: reviewer can tell which ruleset produced a reading they are certifying.
RULESET_VERSION = "temporal-1.0.0"


# --------------------------------------------------------------------------
# Numbers
# --------------------------------------------------------------------------

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}

#: "one", "twenty-four", "45", and the corpus's "12 (twelve)" / "one (1)" forms.
_NUMBER_RE = re.compile(
    r"(?P<digits>\d{1,4})\s*(?:\(\s*[a-z\-]+\s*\))?"
    r"|(?P<words>(?:"
    + "|".join(sorted(_TENS, key=len, reverse=True))
    + r")(?:[\s\-](?:"
    + "|".join(sorted(_UNITS, key=len, reverse=True))
    + r"))?|"
    + "|".join(sorted(_UNITS, key=len, reverse=True))
    + r")\s*(?:\(\s*\d{1,4}\s*\))?",
    re.I,
)


def word_to_int(text: str) -> int | None:
    """Parse '7', 'seven', 'twenty-four', '12 (twelve)' or 'one (1)'."""
    cleaned = re.sub(r"\(.*?\)", " ", text).strip().lower()
    if not cleaned:
        return None
    if cleaned.isdigit():
        return int(cleaned)
    total = 0
    matched = False
    for part in re.split(r"[\s\-]+", cleaned):
        if part in _TENS:
            total += _TENS[part]
            matched = True
        elif part in _UNITS:
            total += _UNITS[part]
            matched = True
        elif part == "and":
            continue
        else:
            return None
    return total if matched else None


_NUM = (
    r"(?:\d{1,4}\s*(?:\(\s*[a-z\-]+\s*\))?"
    r"|(?:" + "|".join(sorted(_TENS, key=len, reverse=True)) + r")(?:[\s\-]"
    r"(?:" + "|".join(sorted(_UNITS, key=len, reverse=True)) + r"))?"
    r"|" + "|".join(sorted(_UNITS, key=len, reverse=True)) + r")"
    r"(?:\s*\(\s*\d{1,4}\s*\))?"
)

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


@dataclass(slots=True)
class TemporalReading:
    """What the rules recovered from one clause, with the words behind it."""

    deadline: Deadline | None = None
    recurrence: str | None = None
    trigger_kind: TriggerKind | None = None
    commencement: Commencement | None = None

    spans: dict[str, tuple[int, int]] = field(default_factory=dict)
    rules_fired: list[str] = field(default_factory=list)
    confidence: float = 0.0
    #: Phrases that look temporal but that no rule could type. Reported, never
    #: guessed at — these are exactly what the LLM pass is asked to look at.
    unparsed: list[tuple[str, tuple[int, int]]] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return (
            self.deadline is None
            and self.recurrence is None
            and self.commencement is None
        )

    def quote(self, key: str, text: str) -> str | None:
        span = self.spans.get(key)
        return text[span[0] : span[1]] if span else None


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------

_DAY_KIND = r"(?P<kind>working|trading|business|calendar|clear)?\s*"

#: "on T+5 day", "T+1 day", "by T+2 working days"
_T_PLUS = re.compile(r"\bT\s*\+\s*(?P<n>\d{1,3})\s*" + _DAY_KIND + r"days?\b", re.I)

_WITHIN_DAYS = re.compile(
    r"\b(?:with[ei]n|not\s+later\s+than|no\s+later\s+than|latest\s+by)\s+"
    r"(?:a\s+period\s+of\s+)?(?P<n>" + _NUM + r")\s*" + _DAY_KIND + r"days?\b",
    re.I,
)
_WITHIN_HOURS = re.compile(
    r"\b(?:with[ei]n|not\s+later\s+than|no\s+later\s+than)\s+"
    r"(?P<n>" + _NUM + r")\s*hours?\b",
    re.I,
)
_WITHIN_MONTHS = re.compile(
    r"\b(?:with[ei]n|not\s+later\s+than|no\s+later\s+than)\s+"
    r"(?:a\s+period\s+of\s+)?(?P<n>" + _NUM + r")\s*months?\b",
    re.I,
)
_WITHIN_WEEKS = re.compile(
    r"\b(?:with[ei]n|not\s+later\s+than)\s+(?P<n>" + _NUM + r")\s*weeks?\b", re.I
)

#: "immediately", "forthwith", "without delay" — a zero-hour horizon, which is
#: materially different from having no deadline at all.
_IMMEDIATE = re.compile(
    r"\b(?:immediately|forthwith|without\s+(?:any\s+)?delay|"
    r"on\s+an?\s+immediate\s+basis)\b",
    re.I,
)

_ON_DEMAND = re.compile(
    r"\b(?:up)?on\s+demand\b"
    r"|\bas\s+and\s+when\s+(?:called(?:\s+for)?|required|sought|demanded|requested)\b"
    r"|\bwhenever\s+(?:required|demanded|called\s+for)\b"
    r"|\bas\s+may\s+be\s+(?:required|called\s+for|sought)\b",
    re.I,
)

_PERIOD_WORDS = {
    "day": "DAY",
    "trading day": "DAY",
    "working day": "DAY",
    "business day": "DAY",
    "t day": "DAY",
    "week": "WEEK",
    "month": "MONTH",
    "quarter": "QUARTER",
    "half year": "HALF_YEAR",
    "half-year": "HALF_YEAR",
    "year": "YEAR",
    "financial year": "YEAR",
}

_END_OF = re.compile(
    r"\b(?:by|at|before|as\s+on|as\s+at)?\s*the\s+"
    r"(?P<eod>end|close)\s+of\s+(?:the\s+)?"
    r"(?P<anchor>T\s*(?:[+-]\s*\d)?\s*)?"
    r"(?P<period>trading\s+day|working\s+day|business\s+day|day|week|month|"
    r"quarter|half[\s\-]?year|financial\s+year|year)\b",
    re.I,
)
#: The corpus also writes it without an article: "by the end of trading day",
#: "end of day fund balance", "End of the Day (EOD) obligation".
_END_OF_BARE = re.compile(
    r"\b(?:by|at|before)?\s*(?P<eod>end)\s+of\s+"
    r"(?P<period>trading\s+day|working\s+day|business\s+day|day)\b",
    re.I,
)

_RECURRENCE_WORDS: list[tuple[str, str]] = [
    # Longest first so "half yearly" is not eaten by "yearly".
    (r"half[\s\-]?yearly|semi[\s\-]?annually|on\s+a\s+half[\s\-]?yearly\s+basis",
     "FREQ=MONTHLY;INTERVAL=6"),
    (r"fortnightly|on\s+a\s+fortnightly\s+basis", "FREQ=WEEKLY;INTERVAL=2"),
    # RFC 5545 spells this FREQ=MONTHLY;INTERVAL=3; FREQ=QUARTERLY is emitted
    # because it is what the compiled-rule contract specifies, and it round-trips
    # through the Trigger validator unchanged.
    (r"quarterly|on\s+a\s+quarterly\s+basis|every\s+quarter", "FREQ=QUARTERLY"),
    (r"monthly|on\s+a\s+monthly\s+basis|every\s+month", "FREQ=MONTHLY"),
    (r"weekly|on\s+a\s+weekly\s+basis|every\s+week", "FREQ=WEEKLY"),
    (r"daily|on\s+a\s+daily\s+basis|every\s+day|each\s+day", "FREQ=DAILY"),
    (r"annually|yearly|on\s+an?\s+annual\s+basis|every\s+year", "FREQ=YEARLY"),
]
_RECURRENCE_RE = re.compile(
    "|".join(f"(?P<r{i}>{pat})" for i, (pat, _) in enumerate(_RECURRENCE_WORDS)), re.I
)

_WEF_DATE = re.compile(
    r"\bwith\s+effect\s+from\s+(?P<month>" + "|".join(_MONTHS) + r")\s+"
    r"(?P<day>\d{1,2})\s*,?\s*(?P<year>(?:19|20)\d{2})\b",
    re.I,
)
_EFFECTIVE_DATE = re.compile(
    r"\beffective\s+date\s+(?:for\s+[\w\s()]+\s+)?is\s+(?P<month>"
    + "|".join(_MONTHS)
    + r")\s+(?P<day>\d{1,2})\s*,?\s*(?P<year>(?:19|20)\d{2})\b",
    re.I,
)

#: "three months after the exchanges issue operational guidelines" — an offset
#: hung off an external event rather than a date. This is the shape the phased
#: amendment circulars use, and the reason Commencement exists at all.
_CONDITIONAL_COMMENCEMENT = re.compile(
    r"\b(?P<n>" + _NUM + r")\s*(?P<unit>day|week|month|year)s?\s+"
    r"(?:from|after|of)\s+(?:the\s+)?"
    r"(?P<condition>(?!issuance\s+of\s+this\s+circular)"
    r"(?:date\s+of\s+)?[a-z][a-z\s,'\-]{6,90}?)"
    r"(?=[.,;]|\s+(?:shall|will|and|or)\b|$)",
    re.I,
)
_RELATIVE_TO_ISSUANCE = re.compile(
    r"\b(?P<n>" + _NUM + r")\s*(?P<unit>day|week|month|year)s?\s+"
    r"(?:from|after|of)\s+(?:the\s+)?"
    r"(?:date\s+of\s+)?(?:issuance|issue|publication)\s+of\s+"
    r"(?:this|the)\s+(?:circular|framework|master\s+circular|notification)\b",
    re.I,
)

_UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}

#: Words that name the event a relative deadline hangs off. Extracted so the
#: anchor is the regulation's own event, not a placeholder.
_ANCHOR_AFTER = re.compile(
    r"\b(?:of|from|after|following)\s+(?:the\s+|such\s+|its\s+)?"
    r"(?P<anchor>[a-z][a-z\s\-/']{2,60}?)"
    r"(?=[.,;)]|\s+(?:shall|and|or|to|in|by|for|with|unless|provided)\b|$)",
    re.I,
)


def _anchor_from(text: str, after: int) -> tuple[str, tuple[int, int]] | None:
    """Read the event a relative deadline is measured from, if the clause names it."""
    window = text[after : after + 140]
    match = _ANCHOR_AFTER.search(window)
    if not match:
        return None
    anchor = " ".join(match.group("anchor").split()).strip(" ,;.")
    if len(anchor) < 3:
        return None
    start = after + match.start("anchor")
    return anchor, (start, start + len(match.group("anchor")))


def _day_count(kind: str | None) -> DayCount:
    """Read the day-counting convention the clause states, or admit it doesn't.

    Returning UNSPECIFIED for a bare "within 30 days" is the whole point: the
    two readings produce different due dates, and the parser is not entitled to
    pick one on the regulation's behalf.
    """
    if not kind:
        return DayCount.UNSPECIFIED
    lowered = kind.lower()
    if lowered in {"working", "trading", "business", "clear"}:
        return DayCount.BUSINESS
    if lowered == "calendar":
        return DayCount.CALENDAR
    return DayCount.UNSPECIFIED


# --------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------


def parse_recurrence(text: str) -> tuple[str, tuple[int, int]] | None:
    """Find a scheduling cadence and return its RRULE plus the span behind it."""
    match = _RECURRENCE_RE.search(text)
    if not match:
        return None
    for index, (_pattern, rrule) in enumerate(_RECURRENCE_WORDS):
        if match.group(f"r{index}"):
            return rrule, (match.start(), match.end())
    return None


def parse_commencement(text: str) -> tuple[Commencement, tuple[int, int]] | None:
    """Find when the rule itself starts to apply, if the clause says so."""
    for pattern in (_WEF_DATE, _EFFECTIVE_DATE):
        match = pattern.search(text)
        if match:
            try:
                when = _dt.date(
                    int(match.group("year")),
                    _MONTHS[match.group("month").lower()],
                    int(match.group("day")),
                )
            except ValueError:
                continue
            return (
                Commencement(kind=CommencementKind.FIXED_DATE, fixed_date=when),
                (match.start(), match.end()),
            )

    match = _RELATIVE_TO_ISSUANCE.search(text)
    if match:
        count = word_to_int(match.group("n"))
        if count is not None:
            days = count * _UNIT_DAYS[match.group("unit").lower()]
            return (
                Commencement(kind=CommencementKind.RELATIVE_TO_ISSUANCE, offset_days=days),
                (match.start(), match.end()),
            )

    match = _CONDITIONAL_COMMENCEMENT.search(text)
    if match:
        count = word_to_int(match.group("n"))
        condition = " ".join(match.group("condition").split()).strip(" ,;.")
        # Only a clause-shaped condition counts. A bare noun such as "the payout"
        # is a deadline anchor, not a commencement trigger, and reading it as one
        # would silently defer a live obligation.
        if count is not None and len(condition.split()) >= 3:
            return (
                Commencement(
                    kind=CommencementKind.CONDITIONAL_ON_EVENT,
                    offset_days=count * _UNIT_DAYS[match.group("unit").lower()],
                    condition=condition,
                ),
                (match.start(), match.end()),
            )
    return None


def _first(text: str, patterns: Iterable[re.Pattern[str]]) -> re.Match[str] | None:
    best: re.Match[str] | None = None
    for pattern in patterns:
        match = pattern.search(text)
        if match and (best is None or match.start() < best.start()):
            best = match
    return best


def parse_temporal(text: str) -> TemporalReading:
    """Read every temporal commitment in one clause. Pure function of `text`."""
    reading = TemporalReading()
    flat = text

    # ---------------------------------------------------------- commencement
    commencement = parse_commencement(flat)
    if commencement is not None:
        reading.commencement, reading.spans["commencement"] = commencement
        reading.rules_fired.append("commencement")

    # ------------------------------------------------------------ recurrence
    recurrence = parse_recurrence(flat)
    if recurrence is not None:
        reading.recurrence, reading.spans["trigger.recurrence"] = recurrence
        reading.trigger_kind = TriggerKind.SCHEDULE
        reading.rules_fired.append("recurrence")

    # -------------------------------------------------------------- deadline
    deadline, rule, spans = _parse_deadline(flat)
    if deadline is not None:
        reading.deadline = deadline
        reading.spans.update(spans)
        reading.rules_fired.append(rule)

    # A recurring duty with no explicit horizon is due within its own period:
    # "shall share the UCC data on a daily basis" means by the end of that day.
    if reading.deadline is None and reading.recurrence:
        period = {
            "FREQ=DAILY": "DAY",
            "FREQ=WEEKLY": "WEEK",
            "FREQ=MONTHLY": "MONTH",
            "FREQ=QUARTERLY": "QUARTER",
            "FREQ=YEARLY": "YEAR",
        }.get(reading.recurrence)
        if period:
            reading.deadline = Deadline(kind=DeadlineKind.END_OF_PERIOD, period=period)
            reading.spans["deadline"] = reading.spans["trigger.recurrence"]
            reading.rules_fired.append("period-from-recurrence")

    # ---------------------------------------------------- unrecognised phrases
    for match in re.finditer(
        r"\b(?:with[ei]n|not\s+later\s+than|by\s+the\s+end\s+of|upon|before)\b[^.;]{0,60}",
        flat,
        re.I,
    ):
        span = (match.start(), match.end())
        if any(_overlaps(span, s) for s in reading.spans.values()):
            continue
        reading.unparsed.append((match.group(0).strip(), span))

    reading.confidence = _score(reading)
    return reading


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _parse_deadline(
    text: str,
) -> tuple[Deadline | None, str, dict[str, tuple[int, int]]]:
    """Type the compliance horizon. Returns (deadline, rule name, spans)."""

    # T+n is the most specific shape and must be tried before "within n days".
    match = _T_PLUS.search(text)
    if match:
        # T is the trade date by definition, so the anchor is fixed by the
        # notation itself. Scanning forward for an "of ..." phrase would pick up
        # whatever noun happened to follow — on clause 40.1.8 that yielded the
        # anchor "all margins from clients", which is the object, not the event.
        # T+n almost certainly counts settlement days in Indian markets — but
        # "on T+5 day" does not say so, and applying that convention here would
        # bake an unstated interpretation into a signed artifact. The parser
        # reports what the text states and leaves the convention UNSPECIFIED,
        # which blocks certification until an officer resolves it.
        return (
            Deadline(
                kind=DeadlineKind.RELATIVE,
                offset_days=int(match.group("n")),
                business_days=_day_count(match.group("kind")),
                anchor_event="trade.date",
            ),
            "t-plus",
            {"deadline": (match.start(), match.end())},
        )

    match = _WITHIN_DAYS.search(text)
    if match:
        count = word_to_int(match.group("n"))
        if count is not None:
            anchor = _anchor_from(text, match.end()) or ("event", None)
            spans = {"deadline": (match.start(), match.end())}
            if anchor[1]:
                spans["deadline.anchor_event"] = anchor[1]
            return (
                Deadline(
                    kind=DeadlineKind.RELATIVE,
                    offset_days=count,
                    business_days=_day_count(match.group("kind")),
                    anchor_event=anchor[0],
                ),
                "within-days",
                spans,
            )

    match = _WITHIN_HOURS.search(text)
    if match:
        count = word_to_int(match.group("n"))
        if count is not None:
            anchor = _anchor_from(text, match.end()) or ("event", None)
            spans = {"deadline": (match.start(), match.end())}
            if anchor[1]:
                spans["deadline.anchor_event"] = anchor[1]
            return (
                Deadline(
                    kind=DeadlineKind.RELATIVE,
                    offset_hours=count,
                    anchor_event=anchor[0],
                ),
                "within-hours",
                spans,
            )

    match = _WITHIN_WEEKS.search(text)
    if match:
        count = word_to_int(match.group("n"))
        if count is not None:
            anchor = _anchor_from(text, match.end()) or ("event", None)
            return (
                Deadline(
                    kind=DeadlineKind.RELATIVE,
                    offset_days=count * 7,
                    anchor_event=anchor[0],
                ),
                "within-weeks",
                {"deadline": (match.start(), match.end())},
            )

    match = _WITHIN_MONTHS.search(text)
    if match:
        count = word_to_int(match.group("n"))
        if count is not None:
            anchor = _anchor_from(text, match.end()) or ("event", None)
            spans = {"deadline": (match.start(), match.end())}
            if anchor[1]:
                spans["deadline.anchor_event"] = anchor[1]
            return (
                Deadline(
                    kind=DeadlineKind.RELATIVE,
                    offset_months=count,
                    anchor_event=anchor[0],
                ),
                "within-months",
                spans,
            )

    match = _first(text, (_END_OF, _END_OF_BARE))
    if match:
        raw_period = " ".join(match.group("period").split()).lower().replace("-", " ")
        period = _PERIOD_WORDS.get(raw_period, "DAY")
        anchor_group = match.groupdict().get("anchor")
        anchor = " ".join(anchor_group.split()) if anchor_group else None
        return (
            Deadline(
                kind=DeadlineKind.END_OF_PERIOD,
                period=period,
                anchor_event=anchor or None,
            ),
            "end-of-period",
            {"deadline": (match.start(), match.end())},
        )

    match = _ON_DEMAND.search(text)
    if match:
        return (
            Deadline(kind=DeadlineKind.ON_DEMAND),
            "on-demand",
            {"deadline": (match.start(), match.end())},
        )

    match = _IMMEDIATE.search(text)
    if match:
        anchor = _anchor_from(text, match.end()) or ("event", None)
        return (
            Deadline(
                kind=DeadlineKind.RELATIVE, offset_hours=0, anchor_event=anchor[0]
            ),
            "immediate",
            {"deadline": (match.start(), match.end())},
        )

    return None, "", {}


def _score(reading: TemporalReading) -> float:
    """Confidence in the reading. Deliberately not a model output.

    Specific, unambiguous shapes score high; readings inferred rather than read
    score lower; anything left unparsed pulls the whole reading down so it
    surfaces in review.
    """
    if reading.is_empty:
        return 0.0
    base = {
        "t-plus": 0.98,
        "within-hours": 0.95,
        "within-days": 0.95,
        "within-months": 0.93,
        "within-weeks": 0.90,
        "on-demand": 0.92,
        "end-of-period": 0.88,
        "immediate": 0.85,
        "period-from-recurrence": 0.70,
    }
    scores = [base[rule] for rule in reading.rules_fired if rule in base]
    if reading.recurrence:
        scores.append(0.94)
    if reading.commencement:
        scores.append(0.86)
    if not scores:
        return 0.0
    score = min(scores)
    if reading.unparsed:
        score -= 0.10 * min(len(reading.unparsed), 3)
    return round(max(0.0, min(1.0, score)), 3)
