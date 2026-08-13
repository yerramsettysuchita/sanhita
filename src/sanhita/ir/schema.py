"""The Obligation IR.

A compiled rule is not a checklist row. Flattening this schema into
``{text, deadline, done}`` would reduce Sanhita to a prettier RAG app: it would
lose the distinction between *when a duty wakes up* (Trigger), *how long you
then have* (Deadline), *whether the rule applies to you at all* (Condition),
*when the rule starts biting in the first place* (Commencement), and *what you
must be able to show* (EvidenceReq). Those are five independent axes. Real SEBI
clauses vary along all five, and an engine that cannot represent them
independently cannot execute them deterministically.

Three structural commitments:

1. **Provenance is mandatory.** Every Obligation carries a SourceAnchor with the
   exact clause, page, character span, verbatim text and its SHA-256. A rule
   that cannot point at the words that created it is not compilable.

2. **One clause may yield many obligations.** "The broker shall send a daily
   margin statement to the client and shall retain proof of dispatch" is two
   duties with two evidence requirements. Ids are suffixed ``-a``, ``-b``, ...
   in a deterministic order, so the same clause always yields the same ids.

3. **Certification freezes the artifact.** A CERTIFIED obligation raises on
   mutation and carries an HMAC over its own canonical JSON. Re-certifying is
   not an edit; it is a new version.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Annotated, Any, Iterable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sanhita.ir.canonical import canonical_bytes, canonical_json, sign_hmac, verify_hmac
from sanhita.ir.enums import (
    Actor,
    CommencementKind,
    ConditionKind,
    DayCount,
    DeadlineKind,
    Modality,
    RuleStatus,
    TriggerKind,
)

__all__ = [
    "Action",
    "Certification",
    "CertifiedImmutableError",
    "Commencement",
    "Condition",
    "Deadline",
    "EvidenceReq",
    "KNOWN_ARTIFACT_TYPES",
    "Obligation",
    "ObligationSet",
    "SourceAnchor",
    "Trigger",
    "obligation_id",
    "suffix_for_index",
]


class CertifiedImmutableError(RuntimeError):
    """Raised on any attempt to mutate a CERTIFIED obligation."""


class UnresolvedFieldError(RuntimeError):
    """Raised when certification is attempted with an unresolved ambiguity.

    Not a validation error: the obligation is well-formed. It simply records a
    question the clause does not answer, and a human has not yet answered it.
    """


# Value objects are frozen. You rebuild them, you do not edit them in place.
_VALUE_OBJECT = ConfigDict(frozen=True, extra="forbid", use_enum_values=False)


# --------------------------------------------------------------------------
# Action
# --------------------------------------------------------------------------


class Action(BaseModel):
    """What the actor must actually do, decomposed rather than quoted.

    Free text here would push the semantics back into a string and force the
    rule engine to re-parse English at evaluation time, which is exactly the
    failure mode this product exists to avoid.
    """

    model_config = _VALUE_OBJECT

    verb: str = Field(min_length=1, description="Normalised operative verb, e.g. 'issue', 'retain'.")
    object: str = Field(min_length=1, description="What the verb operates on, e.g. 'margin statement'.")
    qualifiers: list[str] = Field(
        default_factory=list,
        description="Adverbial constraints that narrow the action, e.g. 'in electronic form'.",
    )
    recipient: str | None = Field(
        default=None, description="Party the action is directed at, where the clause names one."
    )
    medium: str | None = Field(
        default=None, description="Channel mandated by the clause, e.g. 'email', 'exchange portal'."
    )

    @field_validator("verb", "object")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return " ".join(value.split()).lower()

    @field_validator("qualifiers")
    @classmethod
    def _order_qualifiers(cls, value: list[str]) -> list[str]:
        # Sorted so two extractors that emit the same qualifiers in different
        # orders produce byte-identical canonical JSON.
        return sorted({" ".join(v.split()) for v in value if v.strip()})


# --------------------------------------------------------------------------
# Trigger
# --------------------------------------------------------------------------


class Trigger(BaseModel):
    """What causes the obligation to become live."""

    model_config = _VALUE_OBJECT

    kind: TriggerKind
    expression: str = Field(
        min_length=1,
        description="Machine-evaluable predicate or event name, e.g. 'trade.executed'.",
    )
    recurrence: str | None = Field(
        default=None,
        description="RRULE-style recurrence, required for SCHEDULE, e.g. 'FREQ=DAILY'.",
    )

    @model_validator(mode="after")
    def _check_recurrence(self) -> "Trigger":
        if self.kind is TriggerKind.SCHEDULE and not self.recurrence:
            raise ValueError("a SCHEDULE trigger requires a recurrence (e.g. 'FREQ=DAILY')")
        if self.kind is not TriggerKind.SCHEDULE and self.recurrence:
            raise ValueError(f"recurrence is meaningless for a {self.kind.value} trigger")
        return self

    @field_validator("recurrence")
    @classmethod
    def _check_rrule(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip().upper()
        if not text.startswith("FREQ="):
            raise ValueError("recurrence must be RRULE-style and begin with 'FREQ='")
        return text


# --------------------------------------------------------------------------
# Commencement and Deadline
# --------------------------------------------------------------------------


class Commencement(BaseModel):
    """When the rule itself starts to apply.

    Distinct from Deadline. A deadline is per-instance ("within one working day
    of the trade"). A commencement is per-rule ("and none of this applies until
    three months after the exchanges issue operational guidelines").

    Amendment circulars phase commencement across paragraph ranges , one range
    keyed to an external event, another to a fixed offset from issuance , so a
    single flat effective-date field cannot represent them.
    """

    model_config = _VALUE_OBJECT

    kind: CommencementKind
    offset_days: int | None = Field(
        default=None, ge=0, description="Days after the anchor, for RELATIVE_TO_ISSUANCE."
    )
    fixed_date: _dt.date | None = Field(
        default=None, description="Calendar date, for FIXED_DATE."
    )
    condition: str | None = Field(
        default=None,
        description="External event gating commencement, e.g. "
        "'exchanges issue operational guidelines'.",
    )

    @model_validator(mode="after")
    def _check_shape(self) -> "Commencement":
        if self.kind is CommencementKind.FIXED_DATE and self.fixed_date is None:
            raise ValueError("FIXED_DATE commencement requires fixed_date")
        if self.kind is CommencementKind.RELATIVE_TO_ISSUANCE and self.offset_days is None:
            raise ValueError("RELATIVE_TO_ISSUANCE commencement requires offset_days")
        if self.kind is CommencementKind.CONDITIONAL_ON_EVENT and not self.condition:
            raise ValueError("CONDITIONAL_ON_EVENT commencement requires a condition")
        return self


#: Module level, not a class attribute: Pydantic treats a leading-underscore
#: class attribute as a private model attribute, which would turn this set into
#: a ModelPrivateAttr and make the membership test below a TypeError.
_PERIODS = frozenset({"DAY", "WEEK", "MONTH", "QUARTER", "HALF_YEAR", "YEAR"})


class Deadline(BaseModel):
    """The compliance horizon for one instance of the obligation.

    Between them, the four kinds and these fields encode every deadline shape
    the master circular actually uses:

      "T+1 working day"        RELATIVE, offset_days=1, business_days=True,
                               anchor_event='trade.date'
      "end of day T"           END_OF_PERIOD, period='DAY', offset_days=0
      "within 7 days of the    RELATIVE, offset_days=7, business_days=False,
       event"                  anchor_event=<the event>
      "quarterly"              END_OF_PERIOD, period='QUARTER'
      "upon demand"            ON_DEMAND
    """

    model_config = _VALUE_OBJECT

    kind: DeadlineKind
    offset_days: int | None = Field(default=None, description="Offset from the anchor or period end.")
    #: The corpus states horizons in hours and months as well as days , "within
    #: six hours of noticing the incident" (62.54), "within two months from the
    #: end of the half year" (13.2.3). Collapsing those onto offset_days would
    #: destroy information the regulation is explicit about, and the technical-
    #: glitch amendment turns precisely on an hours-valued deadline moving from
    #: one hour to two. Exactly one of the three is set on a RELATIVE deadline.
    offset_hours: int | None = Field(default=None, ge=0)
    offset_months: int | None = Field(default=None, ge=0)
    #: Tri-state. UNSPECIFIED when the clause does not state the convention;
    #: that blocks certification until an officer resolves it. See DayCount.
    business_days: DayCount = DayCount.UNSPECIFIED
    anchor_event: str | None = Field(
        default=None, description="Event the offset is measured from, for RELATIVE."
    )
    absolute_date: _dt.date | None = Field(
        default=None, description="The due date itself, for ABSOLUTE."
    )
    period: str | None = Field(
        default=None,
        description="Named period for END_OF_PERIOD: DAY, WEEK, MONTH, QUARTER, HALF_YEAR, YEAR.",
    )
    commencement: Commencement | None = Field(
        default=None, description="When this rule starts applying at all."
    )

    @field_validator("period")
    @classmethod
    def _check_period(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip().upper()
        if text not in _PERIODS:
            raise ValueError(f"period must be one of {sorted(_PERIODS)}, got {value!r}")
        return text

    @property
    def offsets(self) -> tuple[int | None, int | None, int | None]:
        return (self.offset_days, self.offset_hours, self.offset_months)

    @property
    def approx_days(self) -> float | None:
        """A comparable magnitude for triage. Never used to compute a due date."""
        if self.offset_days is not None:
            return float(self.offset_days)
        if self.offset_hours is not None:
            return self.offset_hours / 24
        if self.offset_months is not None:
            return self.offset_months * 30.0
        return None

    @model_validator(mode="after")
    def _check_shape(self) -> "Deadline":
        set_offsets = [value for value in self.offsets if value is not None]

        if self.kind is DeadlineKind.ABSOLUTE:
            if self.absolute_date is None:
                raise ValueError("ABSOLUTE deadline requires absolute_date")
            if set_offsets:
                raise ValueError("ABSOLUTE deadline must not carry an offset")
        elif self.kind is DeadlineKind.RELATIVE:
            if len(set_offsets) != 1:
                raise ValueError(
                    "RELATIVE deadline requires exactly one of "
                    "offset_days, offset_hours or offset_months"
                )
            if not self.anchor_event:
                raise ValueError("RELATIVE deadline requires anchor_event")
        elif self.kind is DeadlineKind.END_OF_PERIOD:
            if not self.period:
                raise ValueError("END_OF_PERIOD deadline requires period")
        elif self.kind is DeadlineKind.ON_DEMAND:
            if set_offsets or self.absolute_date is not None:
                raise ValueError("ON_DEMAND deadline carries no offset or date")

        # Day-counting only means something for a day-valued offset.
        if self.business_days is not DayCount.UNSPECIFIED and self.offset_days is None:
            raise ValueError("business_days is only meaningful alongside offset_days")
        return self

    @property
    def needs_resolution(self) -> bool:
        """True when a human must settle the day-count convention before signing."""
        return self.offset_days is not None and self.business_days is DayCount.UNSPECIFIED


# --------------------------------------------------------------------------
# Condition
# --------------------------------------------------------------------------


class Condition(BaseModel):
    """A gate on applicability: a precondition, a carve-out, or a threshold.

    **This does not currently hold anything a machine can evaluate**, and the
    field below used to claim that it did. What the extractor puts in
    ``expression`` is the clause's own words describing when the obligation
    applies, in prose. Across the stock broker circular that is 931 conditions
    of which about 5% contain so much as a comparator and a number.

    The distinction matters because it bounds what can be built on top. A
    condition in prose is enough to show a reviewer why a rule might not apply
    to them. It is not enough to prove a rulebook consistent, to detect a fact
    pattern no rule covers, or to feed a constraint solver, and any of those
    would need the sentences turned into predicates first.

    Doing that turn with a language model would put a probabilistic step inside
    a result presented as a proof, which is the one thing this product exists to
    refuse. So the intended route is the same one the day-count convention
    takes: the extractor records what the clause says, and a named person
    supplies the formal predicate at certification time, where the decision is
    signed and attributable. Until that exists, this field is prose and says so.
    """

    model_config = _VALUE_OBJECT

    kind: ConditionKind
    expression: str = Field(
        min_length=1,
        description=(
            "When the obligation applies, in the clause's own words. Prose, not "
            "a predicate: see the class docstring."
        ),
    )
    parameters: dict[str, str | int | float] = Field(
        default_factory=dict,
        description=(
            "Named constants recovered from the text, e.g. "
            "{'threshold_inr': 1000000}. Required on a THRESHOLD, because a "
            "threshold with no number is not a threshold."
        ),
    )

    @model_validator(mode="after")
    def _check_threshold(self) -> "Condition":
        if self.kind is ConditionKind.THRESHOLD and not self.parameters:
            raise ValueError("a THRESHOLD condition requires at least one parameter")
        return self


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------

#: Artifact types seen so far. Not a closed set: an extractor that meets a novel
#: artifact should name it rather than force-fit an existing label, and the
#: reviewer decides at certification whether the name is right.
KNOWN_ARTIFACT_TYPES = frozenset(
    {
        "DISPATCH_LOG",
        "CLIENT_ACK",
        "BANK_STATEMENT",
        "REPORT_FILING",
        "REGISTER",
        "AUDIT_REPORT",
        "SYSTEM_LOG",
        "POLICY_DOCUMENT",
        "RECONCILIATION",
    }
)


class EvidenceReq(BaseModel):
    """What the firm must be able to produce to show the duty was discharged.

    This is the join between the compiled rule and the firm's evidence store.
    A MUST with no evidence requirement is unexecutable: there would be nothing
    for the deterministic engine to check.
    """

    model_config = _VALUE_OBJECT

    artifact_type: str = Field(min_length=1)
    retention_period_days: int | None = Field(default=None, ge=0)
    producible_on_demand: bool = False
    description: str | None = None

    @field_validator("artifact_type")
    @classmethod
    def _normalise_type(cls, value: str) -> str:
        text = re.sub(r"[\s\-]+", "_", value.strip()).upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", text):
            raise ValueError(f"artifact_type must be UPPER_SNAKE_CASE, got {value!r}")
        return text

    @property
    def is_known_type(self) -> bool:
        """False for a novel artifact type, which a reviewer should look at."""
        return self.artifact_type in KNOWN_ARTIFACT_TYPES


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------


class SourceAnchor(BaseModel):
    """Exactly where in the regulation this obligation came from.

    ``section`` rather than ``chapter``: the stock-broker master circular is
    section-numbered, and it contains no chapter divisions of its own.

    ``source_circulars`` and ``earliest_source_date`` carry the regulatory
    lineage recovered from the document's own footnotes , the originating
    circulars that the master circular consolidated. That lineage is what lets a
    reviewer see that a 2025 paragraph is really a 2011 rule, unchanged.
    """

    model_config = _VALUE_OBJECT

    circular_id: str = Field(min_length=1)
    section: str = Field(min_length=1, description="Top-level section number, e.g. '114'.")
    clause_id: str = Field(min_length=1, description="Dotted path, e.g. '40.1.8' or '21.1.2(a)'.")
    page: int = Field(ge=1, description="1-based page number in the source PDF.")
    char_span: tuple[int, int] = Field(
        description="[start, end) character offsets into the document's extracted text."
    )
    verbatim_text: str = Field(min_length=1, description="Exact source characters, unnormalised.")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", description="SHA-256 of verbatim_text as UTF-8.")

    source_circulars: list[str] = Field(
        default_factory=list,
        description="Circular references bound to this clause via its footnote markers.",
    )
    earliest_source_date: _dt.date | None = Field(
        default=None, description="Earliest dated circular among source_circulars."
    )

    @field_validator("char_span")
    @classmethod
    def _check_span(cls, value: tuple[int, int]) -> tuple[int, int]:
        start, end = value
        if start < 0 or end < start:
            raise ValueError(f"char_span must satisfy 0 <= start <= end, got {value!r}")
        return value

    @field_validator("source_circulars")
    @classmethod
    def _order_circulars(cls, value: list[str]) -> list[str]:
        return sorted({v.strip() for v in value if v.strip()})

    @model_validator(mode="after")
    def _check_hash(self) -> "SourceAnchor":
        import hashlib

        expected = hashlib.sha256(self.verbatim_text.encode("utf-8")).hexdigest()
        if expected != self.sha256:
            raise ValueError(
                "sha256 does not match verbatim_text; provenance would be unverifiable"
            )
        return self


class ExtractionMeta(BaseModel):
    """Who or what proposed this obligation, and under exactly what settings.

    Recorded on every proposal so a reviewer can reproduce it and an auditor can
    tell a rule-derived field from a model-derived one. `engine` is the load-
    bearing field: a deterministic rule extraction and a language-model
    extraction carry very different warranties, and the certifying officer is
    entitled to know which they are looking at.
    """

    model_config = _VALUE_OBJECT

    engine: str = Field(description="'rules', 'llm' or 'hybrid'.")
    extracted_at: _dt.datetime
    prompt_version: str | None = Field(
        default=None, description="Version of the extraction prompt, for llm/hybrid."
    )
    model_id: str | None = Field(
        default=None, description="Exact model id, e.g. 'claude-opus-5'."
    )
    effort: str | None = Field(default=None, description="Effort level used, if any.")
    ruleset_version: str | None = Field(
        default=None, description="Version of the deterministic ruleset."
    )
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)

    @field_validator("extracted_at")
    @classmethod
    def _tz_aware(cls, value: _dt.datetime) -> _dt.datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=_dt.timezone.utc)
        return value.astimezone(_dt.timezone.utc)


class Certification(BaseModel):
    """A named human's signature over the obligation's canonical bytes."""

    model_config = _VALUE_OBJECT

    certified_by: str = Field(min_length=1, description="Identity of the compliance officer.")
    certified_at: _dt.datetime
    signature: str = Field(
        pattern=r"^[0-9a-f]{64}$", description="HMAC-SHA256 over the obligation's signing payload."
    )
    locked: bool = True
    note: str | None = None

    @field_validator("certified_at")
    @classmethod
    def _tz_aware(cls, value: _dt.datetime) -> _dt.datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=_dt.timezone.utc)
        return value.astimezone(_dt.timezone.utc)


# --------------------------------------------------------------------------
# Obligation ids
# --------------------------------------------------------------------------

#: The clause segment mirrors a parser clause id, which may carry a lettered
#: item "(a)" and the "#2" disambiguator the parser appends when an annexure
#: form repeats a number. Excluding "#" here rejected 22 real clauses.
_ID_RE = re.compile(r"^[A-Z]{2,6}-[0-9A-Za-z().#]+-[a-z]+$")


def suffix_for_index(index: int) -> str:
    """Map 0,1,2,... to a,b,...,z,aa,ab,... deterministically.

    Bijective base-26, so no index ever collides and no suffix is ever skipped.
    """
    if index < 0:
        raise ValueError("index must be non-negative")
    out = ""
    n = index + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("a") + rem) + out
    return out


def obligation_id(prefix: str, clause_id: str, index: int) -> str:
    """Build a stable obligation id, e.g. ``obligation_id('SB', '40.1.8', 0) -> 'SB-40.1.8-a'``.

    Determinism comes from the caller ordering obligations within a clause by
    their position in the clause text, never by extraction order.
    """
    return f"{prefix.upper()}-{clause_id}-{suffix_for_index(index)}"


# --------------------------------------------------------------------------
# Obligation
# --------------------------------------------------------------------------


class Obligation(BaseModel):
    """One typed, machine-executable duty, traceable to one source clause."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        use_enum_values=False,
    )

    id: str = Field(description="Stable id, e.g. 'SB-40.1.8-a'.")
    actor: Actor
    modality: Modality
    action: Action
    trigger: Trigger
    deadline: Deadline | None = None
    conditions: list[Condition] = Field(default_factory=list)
    evidence: list[EvidenceReq] = Field(default_factory=list)
    penalty_ref: str | None = None
    source: SourceAnchor
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        description="Extractor confidence. Never used at evaluation time, only for triage."
    )
    status: RuleStatus = RuleStatus.PROPOSED
    version: str = Field(default="1.0.0", pattern=r"^\d+\.\d+\.\d+$")
    certification: Certification | None = None

    #: Maps a field path ("modality", "deadline.offset_days", "evidence[0]") to
    #: the [start, end) character span of `source.verbatim_text` that justifies
    #: it. This is what stops the extractor inventing content: a field the model
    #: cannot point at in the clause's own words is left None rather than
    #: guessed, and a reviewer can highlight the exact words behind every value.
    field_provenance: dict[str, tuple[int, int]] = Field(default_factory=dict)

    #: How this proposal was produced. Never part of the signature.
    extraction: ExtractionMeta | None = None

    #: Per-field extractor confidence, same keys as `field_provenance`. Low
    #: confidence is information for triage, not a reason to drop a field.
    field_confidence: dict[str, float] = Field(default_factory=dict)

    # ---------------------------------------------------------------- rules

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not _ID_RE.match(value):
            raise ValueError(
                f"obligation id must look like 'SB-40.1.8-a', got {value!r}"
            )
        return value

    @model_validator(mode="after")
    def _check_consistency(self) -> "Obligation":
        # A MUST that demands no evidence cannot be executed against an evidence
        # store, so it is not a compilable rule.
        if self.modality is Modality.MUST and not self.evidence:
            raise ValueError(
                f"{self.id}: a MUST obligation requires at least one EvidenceReq"
            )

        # Certification and status must agree, in both directions.
        if self.status is RuleStatus.CERTIFIED and self.certification is None:
            raise ValueError(f"{self.id}: CERTIFIED status requires a Certification")
        if self.certification is not None and self.status not in (
            RuleStatus.CERTIFIED,
            RuleStatus.SUPERSEDED,
        ):
            raise ValueError(
                f"{self.id}: a Certification is only valid on a CERTIFIED or SUPERSEDED rule"
            )

        # The obligation must be about the clause it claims to come from.
        if not self.id.startswith(("%s-" % self.id.split("-", 1)[0])):  # pragma: no cover
            raise ValueError(f"{self.id}: malformed id")
        clause_part = self.id.split("-", 2)[1]
        if clause_part != self.source.clause_id:
            raise ValueError(
                f"{self.id}: id clause segment {clause_part!r} does not match "
                f"source.clause_id {self.source.clause_id!r}"
            )

        # Every provenance span must actually land inside the clause it claims
        # to quote. A span that runs off the end is a fabricated citation, and
        # is caught here rather than surfacing as a confusing highlight later.
        limit = len(self.source.verbatim_text)
        for field, (start, end) in self.field_provenance.items():
            if not (0 <= start <= end <= limit):
                raise ValueError(
                    f"{self.id}: field_provenance[{field!r}] span {(start, end)} "
                    f"is outside the clause text of length {limit}"
                )

        for field, score in self.field_confidence.items():
            if not 0.0 <= score <= 1.0:
                raise ValueError(
                    f"{self.id}: field_confidence[{field!r}] = {score} is outside 0..1"
                )
        return self

    def quote(self, field: str) -> str | None:
        """The exact words of the clause that justify `field`, or None."""
        span = self.field_provenance.get(field)
        if span is None:
            return None
        return self.source.verbatim_text[span[0] : span[1]]

    def unprovenanced_fields(self) -> list[str]:
        """Populated normative fields carrying no span. For reviewer triage.

        Not an error: `actor` is often implied across a whole section rather
        than restated in the clause. It is surfaced so a reviewer can see
        exactly which values they are being asked to take on trust.
        """
        populated: list[str] = ["actor", "modality", "action", "trigger"]
        if self.deadline is not None:
            populated.append("deadline")
        if self.conditions:
            populated.append("conditions")
        if self.evidence:
            populated.append("evidence")
        if self.penalty_ref:
            populated.append("penalty_ref")
        # A field counts as provenanced if it carries a span itself or any of
        # its parts do , "action" is satisfied by "action.verb"/"action.object".
        keys = set(self.field_provenance)
        return [
            f
            for f in populated
            if f not in keys and not any(k.startswith(f"{f}.") or k.startswith(f"{f}[") for k in keys)
        ]

    # ----------------------------------------------------------- immutability

    def __setattr__(self, name: str, value: Any) -> None:
        # Read status straight out of __dict__: during model construction
        # Pydantic populates __dict__ directly, so this cannot recurse and
        # cannot fire before the object exists.
        if self.__dict__.get("status") is RuleStatus.CERTIFIED:
            raise CertifiedImmutableError(
                f"{self.__dict__.get('id')} is CERTIFIED and version-locked; "
                f"cannot set {name!r}. Supersede it with a new version instead."
            )
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if self.__dict__.get("status") is RuleStatus.CERTIFIED:
            raise CertifiedImmutableError(
                f"{self.__dict__.get('id')} is CERTIFIED and version-locked; "
                f"cannot delete {name!r}."
            )
        super().__delattr__(name)

    # ------------------------------------------------------------ serialising

    def signing_payload(self) -> dict[str, Any]:
        """The canonical dict a signature covers: the rule's normative content.

        Three fields are excluded, each for its own reason:

        ``certification``  a signature cannot cover the bytes containing it.
        ``confidence``     extractor telemetry, not part of what was certified.
        ``extraction``     and ``field_confidence`` and ``field_provenance``:
                           how the proposal was produced and where it was read
                           from. Audit metadata, not normative content , an
                           obligation certified from a rule extraction and the
                           same obligation certified from a model extraction
                           impose identical duties, and must therefore sign
                           identically.
        ``status``         lifecycle, not content. Certifying moves the status
                           from PROPOSED to CERTIFIED, and superseding later
                           moves it again; if status were signed, the act of
                           certifying would invalidate its own signature and
                           superseding a rule would destroy the evidence that
                           it had once been properly certified.

        Everything that determines what the rule *requires* , actor, modality,
        action, trigger, deadline, conditions, evidence, penalty reference,
        version, and the full source anchor including its clause hash , is
        inside the signature.
        """
        return self.model_dump(
            mode="python",
            exclude={
                "certification",
                "confidence",
                "status",
                "extraction",
                "field_confidence",
                "field_provenance",
            },
        )

    def canonical_json(self) -> str:
        """Byte-stable JSON for the whole obligation, certification included."""
        return canonical_json(self.model_dump(mode="python"))

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.model_dump(mode="python"))

    # ---------------------------------------------------------- certification

    def blocking_issues(self) -> list[str]:
        """Everything a human must settle before this rule can be signed.

        The compiler is allowed to say "the clause does not tell me". It is not
        allowed to let that reach a signed artifact unresolved. An ambiguity
        that survives certification becomes an invisible interpretation, which
        is the failure mode the whole product is built against.

        These strings are shown to a compliance officer in the workbench, so
        they are written in plain language rather than as field diagnostics.
        """
        issues: list[str] = []
        if self.deadline is not None and self.deadline.needs_resolution:
            issues.append(
                "The clause does not say whether this deadline counts working "
                "days or calendar days. Somebody has to decide before it can "
                "be signed."
            )
        if self.modality is Modality.MUST and not self.evidence:
            issues.append(
                "This is a MUST, so it needs at least one piece of evidence "
                "that a firm could produce to show it was done."
            )
        if not self.action.verb or not self.action.object:
            issues.append("The action is incomplete. It needs both a verb and an object.")
        return issues

    def certify(
        self,
        *,
        certified_by: str,
        key: bytes | str,
        at: _dt.datetime | None = None,
        note: str | None = None,
    ) -> "Obligation":
        """Return a new, CERTIFIED, version-locked copy. Does not mutate `self`.

        Certification is a transition to a new immutable artifact, not an edit
        of an existing one. The original PROPOSED object is left intact so the
        pre-certification state remains auditable.
        """
        if self.status is RuleStatus.CERTIFIED:
            raise CertifiedImmutableError(f"{self.id} is already certified")
        if self.status is RuleStatus.REJECTED:
            raise ValueError(f"{self.id} was rejected and cannot be certified")

        blocking = self.blocking_issues()
        if blocking:
            raise UnresolvedFieldError(
                f"{self.id} cannot be certified until these are resolved:\n  - "
                + "\n  - ".join(blocking)
            )

        moment = at or _dt.datetime.now(_dt.timezone.utc)
        signature = sign_hmac(self.signing_payload(), key)
        return self.model_copy(
            update={
                "status": RuleStatus.CERTIFIED,
                "certification": Certification(
                    certified_by=certified_by,
                    certified_at=moment,
                    signature=signature,
                    locked=True,
                    note=note,
                ),
            },
            deep=True,
        )

    def verify_signature(self, key: bytes | str) -> bool:
        """True when the certification signature still matches the content.

        This is the tamper check. Field values are frozen by ``__setattr__`` and
        by the frozen value objects, but a list such as ``conditions`` could
        still be mutated in place by a determined caller; that mutation changes
        the signing payload and is caught here.
        """
        if self.certification is None:
            return False
        return verify_hmac(self.signing_payload(), key, self.certification.signature)


# --------------------------------------------------------------------------
# ObligationSet
# --------------------------------------------------------------------------


class ObligationSet(BaseModel):
    """The obligations compiled from a single clause.

    Exists because "one clause, one rule" is false often enough to matter. Its
    job is to make the one-to-many relationship explicit and to hand out the
    ``-a``, ``-b`` suffixes from a single deterministic place, so two runs over
    the same clause cannot disagree about which duty is ``-a``.
    """

    model_config = ConfigDict(extra="forbid")

    clause_id: str
    obligations: list[Obligation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_members(self) -> "ObligationSet":
        for ob in self.obligations:
            if ob.source.clause_id != self.clause_id:
                raise ValueError(
                    f"{ob.id} anchors to clause {ob.source.clause_id!r}, "
                    f"not to this set's clause {self.clause_id!r}"
                )
        ids = [ob.id for ob in self.obligations]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate obligation ids in clause {self.clause_id}: {ids}")
        return self

    @classmethod
    def build(
        cls,
        clause_id: str,
        drafts: Iterable[dict[str, Any]],
        *,
        prefix: str = "SB",
    ) -> "ObligationSet":
        """Assign deterministic ids to draft obligations, in the order given.

        The caller is responsible for supplying drafts in source-text order.
        That is the only ordering that is stable across runs; extraction order
        is not.
        """
        obligations = [
            Obligation(**{**draft, "id": obligation_id(prefix, clause_id, i)})
            for i, draft in enumerate(drafts)
        ]
        return cls(clause_id=clause_id, obligations=obligations)

    def canonical_json(self) -> str:
        return canonical_json(self.model_dump(mode="python"))
