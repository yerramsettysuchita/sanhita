"""LLM extraction: Claude proposes, Pydantic validates, a human certifies.

The model never sees the IR's runtime types. It answers a narrow wire schema
(`ClauseExtraction` below), and that answer is then checked against the clause's
own characters before anything becomes an Obligation:

  * every span must lie inside the clause text
  * every quoted span must be *consistent with* the field it justifies
  * the whole thing must survive Pydantic validation of the real IR

On a validation failure the clause is retried exactly once, with the error text
handed back to the model. A second failure marks the clause EXTRACTION_FAILED
and moves on. A clause is never silently dropped.

Determinism note. The brief asked for `temperature=0`. Sampling parameters were
removed on Claude Opus 5 and now return a 400, so temperature is not available
and — importantly — was never a real determinism guarantee even when it was.
Reproducibility here comes from the parts that actually provide it: a pinned
model id, a versioned prompt, a constrained output schema, span verification
against the source text, and the fact that nothing the model says is trusted
until a human signs it. All of that is recorded on `ExtractionMeta`.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sanhita.compile.extract import ClauseOutcome, ExtractionStatus
from sanhita.compile.prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt
from sanhita.ir.enums import (
    Actor,
    ConditionKind,
    DayCount,
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
    "DEFAULT_MODEL",
    "ClauseExtraction",
    "LLMExtractor",
    "LLMUnavailable",
    "ProposedObligation",
    "Span",
]

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT = "high"

#: List rates for claude-opus-5, USD per million tokens.
PRICE_IN_PER_MTOK = 5.0
PRICE_OUT_PER_MTOK = 25.0


class LLMUnavailable(RuntimeError):
    """Raised when no Anthropic credentials or SDK are available."""


# --------------------------------------------------------------------------
# Wire schema — what the model is allowed to say
# --------------------------------------------------------------------------


class Span(BaseModel):
    """A [start, end) character range into the clause text."""

    model_config = ConfigDict(extra="forbid")

    start: int = Field(ge=0)
    end: int = Field(ge=0)
    quote: str = Field(description="The exact substring, echoed back for checking.")


class ProposedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verb: str
    object: str
    qualifiers: list[str] = Field(default_factory=list)
    recipient: str | None = None
    medium: str | None = None


class ProposedCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ConditionKind
    expression: str
    span: Span | None = None


class ArtifactType(str, Enum):
    """The closed artifact vocabulary, enforced in the wire schema.

    Left as a free string, the model describes the artifact in prose — the first
    real run returned "report of actual short-collection/non-collection of
    margins from clients submitted to the Stock Exchange" as an artifact_type,
    which is a sentence, not a type. Constraining it here means structured
    output enforces the vocabulary rather than the prompt merely requesting it.
    """

    DISPATCH_LOG = "DISPATCH_LOG"
    CLIENT_ACK = "CLIENT_ACK"
    BANK_STATEMENT = "BANK_STATEMENT"
    REPORT_FILING = "REPORT_FILING"
    REGISTER = "REGISTER"
    AUDIT_REPORT = "AUDIT_REPORT"
    SYSTEM_LOG = "SYSTEM_LOG"
    POLICY_DOCUMENT = "POLICY_DOCUMENT"
    RECONCILIATION = "RECONCILIATION"


class ProposedEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_type: ArtifactType
    retention_period_days: int | None = None
    producible_on_demand: bool = False
    span: Span | None = None


class Period(str, Enum):
    """The closed period vocabulary. Constrained for the same reason as
    ArtifactType: left free, the model returns descriptions like
    "settlement day" that the IR cannot accept."""

    DAY = "DAY"
    WEEK = "WEEK"
    MONTH = "MONTH"
    QUARTER = "QUARTER"
    HALF_YEAR = "HALF_YEAR"
    YEAR = "YEAR"


class ProposedDeadline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: DeadlineKind
    offset_days: int | None = None
    offset_hours: int | None = None
    offset_months: int | None = None
    #: UNSPECIFIED unless the clause states the convention in words. The model
    #: must not apply market convention on the regulation's behalf.
    business_days: DayCount = DayCount.UNSPECIFIED
    anchor_event: str | None = None
    period: Period | None = None
    span: Span | None = None


class ProposedObligation(BaseModel):
    """One duty as the model reports it, before any IR validation."""

    model_config = ConfigDict(extra="forbid")

    actor: Actor | None
    modality: Modality
    action: ProposedAction
    trigger_kind: TriggerKind
    trigger_expression: str
    recurrence: str | None = None
    deadline: ProposedDeadline | None = None
    conditions: list[ProposedCondition] = Field(default_factory=list)
    evidence: list[ProposedEvidence] = Field(default_factory=list)
    penalty_ref: str | None = None

    modality_span: Span
    actor_span: Span | None = None
    action_verb_span: Span
    action_object_span: Span | None = None

    confidence: float = Field(ge=0.0, le=1.0)
    field_confidence: dict[str, float] = Field(default_factory=dict)


class ClauseExtraction(BaseModel):
    """The model's complete answer for one clause."""

    model_config = ConfigDict(extra="forbid")

    carries_obligation: bool = Field(
        description="False for definitions, recitals, headings and cross-references."
    )
    reason: str = Field(description="One short phrase, especially when the list is empty.")
    obligations: list[ProposedObligation] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Extractor
# --------------------------------------------------------------------------


_CONVENTION_WORDS = re.compile(r"\b(working|trading|business|calendar|clear)\s+days?\b", re.I)


def _verified_day_count(claimed: DayCount, text: str, span: tuple[int, int] | None) -> DayCount:
    """Accept a day-count claim only if the clause actually states it.

    On the first real run the model was told, explicitly and in capitals, not to
    apply market convention — and applied it anyway, returning BUSINESS for
    "on T+5 day". Instructions are a request; this is the enforcement. If the
    words the model cited do not contain a convention term, the claim is
    downgraded to UNSPECIFIED and a human resolves it.

    The check widens slightly around the cited span, because the convention is
    sometimes stated a few words away ("within two working days after imposing
    penalty" cited as "within two working days").
    """
    if claimed is DayCount.UNSPECIFIED:
        return claimed
    if span is None:
        return DayCount.UNSPECIFIED
    window = text[max(0, span[0] - 40) : min(len(text), span[1] + 40)]
    if not _CONVENTION_WORDS.search(window):
        return DayCount.UNSPECIFIED
    return claimed


@dataclass(slots=True)
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0


class LLMExtractor:
    """Claude-backed extraction, constrained to the IR and checked against the text."""

    engine = "llm"

    def __init__(
        self,
        *,
        circular_id: str,
        prefix: str = "SB",
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        client=None,
        max_tokens: int = 8000,
    ) -> None:
        self.circular_id = circular_id
        self.prefix = prefix
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self._client = client

    # ------------------------------------------------------------- client

    #: Checked in order. SANHITA_ANTHROPIC_API_KEY first so a project-scoped key
    #: can be set without disturbing whatever ANTHROPIC_API_KEY is already in the
    #: shell for other work.
    KEY_ENV_VARS = ("SANHITA_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")

    @classmethod
    def resolve_key(cls) -> str | None:
        for name in cls.KEY_ENV_VARS:
            value = os.environ.get(name)
            if value and value.strip():
                return value.strip()
        return None

    @classmethod
    def credential_error(cls) -> str | None:
        """A precise, actionable message, or None when the path is usable.

        Returns the *reason* rather than a bare boolean so the CLI can tell a
        missing package apart from a missing key apart from a malformed key.
        """
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return (
                "the `anthropic` package is not installed.\n"
                "  Fix:  pip install anthropic\n"
                "  Or:   run with --engine rules (deterministic, no API calls)"
            )

        key = cls.resolve_key()
        if key:
            if not key.startswith("sk-ant-"):
                return (
                    "the API key found does not look like an Anthropic key "
                    "(expected it to start with 'sk-ant-').\n"
                    f"  Checked: {', '.join(cls.KEY_ENV_VARS)}"
                )
            return None

        if os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            return None
        from pathlib import Path

        config = Path(
            os.environ.get("ANTHROPIC_CONFIG_DIR", Path.home() / ".config" / "anthropic")
        )
        if (config / "credentials").is_dir():
            return None

        return (
            "no Anthropic credentials found.\n"
            f"  Fix:  set SANHITA_ANTHROPIC_API_KEY (or ANTHROPIC_API_KEY)\n"
            "  Or:   run `ant auth login`\n"
            "  Or:   run with --engine rules (deterministic, no API calls, $0)"
        )

    @classmethod
    def credentials_available(cls) -> bool:
        return cls.credential_error() is None

    @property
    def client(self):
        if self._client is None:
            problem = self.credential_error()
            if problem:
                raise LLMUnavailable(problem)
            import anthropic

            key = self.resolve_key()
            self._client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        return self._client

    # -------------------------------------------------------------- calls

    def _call(self, node: ClauseNode, correction: str | None = None) -> tuple[ClauseExtraction, _Usage]:
        user = build_user_prompt(
            clause_id=node.id, page=node.page, clause_text=node.text
        )
        if correction:
            user += (
                "\n\nYour previous answer failed schema validation with this error. "
                "Correct it and return the whole answer again.\n"
                f"<error>\n{correction}\n</error>"
            )

        response = self.client.messages.parse(
            model=self.model,
            max_tokens=self.max_tokens,
            # The system prompt is byte-stable, so it caches across every clause
            # in the run and only the short user turn is billed at full rate.
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            output_config={"effort": self.effort},
            messages=[{"role": "user", "content": user}],
            output_format=ClauseExtraction,
        )

        usage = _Usage(
            input_tokens=getattr(response.usage, "input_tokens", 0) or 0,
            output_tokens=getattr(response.usage, "output_tokens", 0) or 0,
        )

        # Claude Opus 5 can decline a request outright; that arrives as a normal
        # 200 with stop_reason "refusal" and empty content, so it must be
        # checked before reading anything off the response.
        if getattr(response, "stop_reason", None) == "refusal":
            detail = getattr(response, "stop_details", None)
            category = getattr(detail, "category", None) if detail else None
            raise RuntimeError(f"model declined the request (category={category})")

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise ValueError("model returned no parseable output")
        return parsed, usage

    # ------------------------------------------------------------ mapping

    def _verify_span(self, span: Span | None, text: str) -> tuple[int, int] | None:
        """Accept a span only if it is real and the model quoted it honestly."""
        if span is None:
            return None
        if not (0 <= span.start <= span.end <= len(text)):
            return None
        actual = text[span.start : span.end]
        if span.quote and actual.strip() != span.quote.strip():
            # The model cited a range whose contents it misreported. Try to
            # rescue it by locating the quote it actually meant; if that fails,
            # drop the span rather than record a false citation.
            found = text.find(span.quote.strip())
            if found < 0:
                return None
            return (found, found + len(span.quote.strip()))
        return (span.start, span.end)

    def _to_obligation(
        self,
        node: ClauseNode,
        proposal: ProposedObligation,
        index: int,
        anchor: SourceAnchor,
        meta: ExtractionMeta,
    ) -> Obligation:
        text = node.text
        provenance: dict[str, tuple[int, int]] = {}

        modality_span = self._verify_span(proposal.modality_span, text)
        if modality_span:
            provenance["modality"] = modality_span
        actor_span = self._verify_span(proposal.actor_span, text)
        if actor_span:
            provenance["actor"] = actor_span
        verb_span = self._verify_span(proposal.action_verb_span, text)
        if verb_span:
            provenance["action.verb"] = verb_span
        object_span = self._verify_span(proposal.action_object_span, text)
        if object_span:
            provenance["action.object"] = object_span

        deadline = None
        if proposal.deadline is not None:
            spec = proposal.deadline
            span = self._verify_span(spec.span, text)
            deadline = Deadline(
                kind=spec.kind,
                offset_days=spec.offset_days,
                offset_hours=spec.offset_hours,
                offset_months=spec.offset_months,
                business_days=_verified_day_count(spec.business_days, text, span),
                anchor_event=spec.anchor_event,
                period=spec.period.value if spec.period else None,
            )
            if span:
                provenance["deadline"] = span

        conditions: list[Condition] = []
        for position, spec in enumerate(proposal.conditions):
            conditions.append(
                Condition(
                    kind=spec.kind,
                    expression=spec.expression,
                    parameters={} if spec.kind is not ConditionKind.THRESHOLD else {"stated": 1},
                )
            )
            span = self._verify_span(spec.span, text)
            if span:
                provenance[f"conditions[{position}]"] = span

        evidence: list[EvidenceReq] = []
        for position, spec in enumerate(proposal.evidence):
            evidence.append(
                EvidenceReq(
                    artifact_type=spec.artifact_type.value,
                    retention_period_days=spec.retention_period_days,
                    producible_on_demand=spec.producible_on_demand,
                )
            )
            span = self._verify_span(spec.span, text)
            if span:
                provenance[f"evidence[{position}]"] = span

        trigger = Trigger(
            kind=proposal.trigger_kind,
            expression=proposal.trigger_expression,
            recurrence=proposal.recurrence
            if proposal.trigger_kind is TriggerKind.SCHEDULE
            else None,
        )

        return Obligation(
            id=obligation_id(self.prefix, node.id, index),
            actor=proposal.actor or Actor.STOCK_BROKER,
            modality=proposal.modality,
            action=Action(
                verb=proposal.action.verb,
                object=proposal.action.object,
                qualifiers=proposal.action.qualifiers,
                recipient=proposal.action.recipient,
                medium=proposal.action.medium,
            ),
            trigger=trigger,
            deadline=deadline,
            conditions=conditions,
            evidence=evidence,
            penalty_ref=proposal.penalty_ref,
            source=anchor,
            confidence=proposal.confidence,
            extraction=meta,
            field_provenance=provenance,
            field_confidence=proposal.field_confidence,
        )

    # -------------------------------------------------------------- public

    def extract(self, node: ClauseNode) -> ClauseOutcome:
        """Compile one clause. Retries once on validation failure, then reports."""
        usage = _Usage()
        correction: str | None = None

        for attempt in (1, 2):
            try:
                parsed, call_usage = self._call(node, correction)
                usage.input_tokens += call_usage.input_tokens
                usage.output_tokens += call_usage.output_tokens
            except Exception as exc:
                if attempt == 2:
                    return ClauseOutcome(
                        node.id,
                        ExtractionStatus.EXTRACTION_FAILED,
                        reason="api-error",
                        error=f"{type(exc).__name__}: {exc}",
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                    )
                correction = f"{type(exc).__name__}: {exc}"
                continue

            if not parsed.carries_obligation or not parsed.obligations:
                return ClauseOutcome(
                    node.id,
                    ExtractionStatus.NO_OBLIGATION,
                    reason=parsed.reason or "model-found-none",
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                )

            anchor = SourceAnchor(
                circular_id=self.circular_id,
                section=node.section or node.id.split(".")[0],
                clause_id=node.id,
                page=node.page,
                char_span=node.char_span,
                verbatim_text=node.text,
                sha256=node.sha256,
            )
            meta = ExtractionMeta(
                engine=self.engine,
                extracted_at=_dt.datetime.now(_dt.timezone.utc),
                prompt_version=PROMPT_VERSION,
                model_id=self.model,
                effort=self.effort,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )

            try:
                obligations = [
                    self._to_obligation(node, proposal, index, anchor, meta)
                    for index, proposal in enumerate(parsed.obligations)
                ]
            except ValidationError as exc:
                if attempt == 2:
                    return ClauseOutcome(
                        node.id,
                        ExtractionStatus.EXTRACTION_FAILED,
                        reason="ir-validation",
                        error=str(exc),
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                    )
                correction = str(exc)
                continue

            return ClauseOutcome(
                node.id,
                ExtractionStatus.PROPOSED,
                obligations=obligations,
                reason=parsed.reason or "model-extracted",
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )

        # Unreachable: both attempts return inside the loop.
        raise AssertionError("extraction loop fell through")  # pragma: no cover
