"""Company X, as a first-class thing rather than a field on something else.

The product was organised around the regulation. A workspace was a rulebook,
"company" was derived from the workspace name, and the user journey started
with *bring me a regulatory document*. That is the right shape for a compiler
and the wrong shape for the person the problem statement is about, whose
journey starts with *I am a stockbroker, am I complying*.

This module is the other end of that. It holds two things the rest of the
system needed and did not have.

**A company.** Its name, what kind of intermediary it is, and the facts about
its business that decide which duties reach it at all.

**A review queue.** Uploading a margin report produces candidates, because a
company PDF does not contain a Sanhita rule id and guessing which duty it
discharges is the one thing this product must never do. Somebody has to say
*this satisfies SB-40.1.8-a*. Until that flow existed the candidates were
computed and thrown away, so a real document could be read perfectly and still
never reach the engine.

    upload -> candidates -> A PERSON MAPS ONE -> evidence -> the rule runs

The middle step is the whole point and it is what lives here.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import ClassVar

from sanhita.execute.evidence import EvidenceStore
from sanhita.execute.ingest import Candidate, Confidence

__all__ = [
    "Company",
    "IntermediaryType",
    "ReviewQueue",
    "ReviewItem",
]


class IntermediaryType(str, Enum):
    """The SEBI categories. Stock broker is the one with a compiled corpus.

    The others are here because the problem statement names them and the IR
    extends to them without redesign, not because a rulebook exists for each.
    """

    STOCK_BROKER = "STOCK_BROKER"
    INVESTMENT_ADVISER = "INVESTMENT_ADVISER"
    RESEARCH_ANALYST = "RESEARCH_ANALYST"
    DEPOSITORY_PARTICIPANT = "DEPOSITORY_PARTICIPANT"
    ASSET_MANAGEMENT_COMPANY = "ASSET_MANAGEMENT_COMPANY"
    REGISTRAR_AND_TRANSFER_AGENT = "REGISTRAR_AND_TRANSFER_AGENT"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").lower()


@dataclass
class Company:
    """One regulated firm, and the facts that decide what applies to it.

    ``business_facts`` is deliberately a plain mapping of question to yes or
    no rather than a typed schema. Many SEBI duties are conditional on facts
    like whether a firm offers derivatives or holds client funds, and the
    Condition objects that carry those tests are prose rather than predicates.
    Until they are formal, a compliance officer answering a short list of
    questions is more honest than a system pretending to infer the answers.
    """

    name: str
    intermediary: IntermediaryType = IntermediaryType.STOCK_BROKER
    #: SEBI registration, where the firm has one. Shown, never validated.
    registration: str = ""

    #: Question to answer. "Offers derivatives" -> True.
    business_facts: dict[str, bool] = field(default_factory=dict)
    #: Named business processes, e.g. "Daily margin reporting".
    processes: list[str] = field(default_factory=list)
    #: Systems of record, e.g. "Margin engine".
    systems: list[str] = field(default_factory=list)

    #: Which rulebooks this firm has said apply to it, by workspace id.
    #
    # The firm is the root object and the regulation is a property of it, not
    # the other way round. A stock broker that also runs a research arm is held
    # to two circulars, and until this existed the product could only express
    # "one workspace, which happens to contain a company", so the second
    # rulebook had no way to belong to the same firm.
    #
    # It is a declaration rather than an inference. Sanhita will not decide for
    # a firm which SEBI framework governs it; that is a legal judgement with
    # consequences, and a compliance officer ticking a box is the honest way to
    # record it. Empty means nobody has said yet, which the screens report as
    # such rather than assuming.
    frameworks: list[str] = field(default_factory=list)

    #: When onboarding was finished, or None while it is still running.
    #
    # Setting up is three answers: who the firm is, which rulebooks govern it,
    # and what records it has. The first two are visible in the profile, the
    # third is not, so without this the product could not tell "has not reached
    # step three yet" from "finished setting up and has uploaded nothing". It
    # jumped from step two straight to the dashboard and step three never
    # existed as a screen.
    setup_completed_at: _dt.datetime | None = None

    created_at: _dt.datetime | None = None
    #: True where this profile is the worked example rather than a real firm.
    #: Printed wherever the profile is shown, because a synthetic company must
    #: never be mistakable for a real one.
    synthetic: bool = False

    #: The fields the profile form is allowed to set. Everything else on this
    #: class is owned elsewhere and must survive a save untouched.
    #
    # This exists because the save route used to rebuild the whole object from
    # a form that carries six of these ten fields, and hand-list the other four
    # to copy across. Two things went wrong with that, both silently:
    #
    #   A field added later was absent from the hand-list, so the next profile
    #   save reset it. `setup_completed_at` was added for onboarding and left
    #   out of the list, so correcting a registration number sent a firm back
    #   to step three of setting up.
    #
    #   The object copied from was whatever `_company` returned, and on a
    #   shared deployment reads fall through to the seeded demonstration firm.
    #   A visitor with no profile of their own inherited that firm's history,
    #   including `synthetic=True`, which printed "demonstration data" across a
    #   real firm's real profile.
    #
    # Naming the form's half here, and updating in place rather than
    # reconstructing, makes both impossible: a new field is preserved by
    # default, because the form cannot reach it.
    FORM_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"name", "intermediary", "registration", "business_facts", "processes", "systems"}
    )

    def apply_profile_form(
        self,
        *,
        name: str,
        intermediary: IntermediaryType,
        registration: str,
        processes: list[str],
        systems: list[str],
        business_facts: dict[str, bool],
    ) -> "Company":
        """Update what the profile form owns, and nothing else.

        In place on purpose. A method that returned a new object would put the
        old problem back: something would have to decide what to carry across,
        and that decision is what kept going wrong.
        """
        self.name = name.strip() or "Unnamed firm"
        self.intermediary = intermediary
        self.registration = registration.strip()
        self.processes = list(processes)
        self.systems = list(systems)
        self.business_facts = dict(business_facts)
        return self

    @property
    def is_configured(self) -> bool:
        """Enough to be useful. A name alone is not a profile."""
        return bool(self.name and (self.processes or self.business_facts))

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "intermediary": self.intermediary.value,
            "registration": self.registration,
            "business_facts": dict(self.business_facts),
            "processes": list(self.processes),
            "systems": list(self.systems),
            "frameworks": list(self.frameworks),
            "setup_completed_at": (
                self.setup_completed_at.isoformat()
                if self.setup_completed_at else None
            ),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "synthetic": self.synthetic,
        }

    @classmethod
    def from_json(cls, raw: dict) -> Company:
        created = raw.get("created_at")
        return cls(
            name=raw.get("name", ""),
            intermediary=IntermediaryType(
                raw.get("intermediary", IntermediaryType.STOCK_BROKER.value)
            ),
            registration=raw.get("registration", ""),
            business_facts=dict(raw.get("business_facts", {})),
            processes=list(raw.get("processes", [])),
            systems=list(raw.get("systems", [])),
            # Absent from profiles written before a firm could hold more than
            # one rulebook. Reading one must not fail.
            frameworks=list(raw.get("frameworks", [])),
            setup_completed_at=(
                _dt.datetime.fromisoformat(raw["setup_completed_at"])
                if raw.get("setup_completed_at") else None
            ),
            created_at=_dt.datetime.fromisoformat(created) if created else None,
            synthetic=raw.get("synthetic", False),
        )

    @classmethod
    def load(cls, path: Path) -> Company | None:
        if not path.is_file():
            return None
        try:
            return cls.from_json(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError, KeyError, ValueError):
            return None

    def save(self, path: Path) -> None:
        from sanhita.cli_compile import _write_atomically

        path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomically(
            path, json.dumps(self.to_json(), indent=2, sort_keys=True) + "\n"
        )


@dataclass
class ReviewItem:
    """One candidate awaiting somebody's judgement, or already given it."""

    item_id: str
    candidate: Candidate
    #: Set when a person maps it. Until then this is not evidence.
    mapped_obligation: str | None = None
    mapped_by: str = ""
    mapped_at: _dt.datetime | None = None
    #: Set when a person says this is not evidence of anything.
    dismissed: bool = False
    dismissed_reason: str = ""

    @property
    def state(self) -> str:
        if self.dismissed:
            return "DISMISSED"
        if self.mapped_obligation:
            return "MAPPED"
        return "AWAITING_REVIEW"

    @property
    def is_open(self) -> bool:
        return not self.dismissed and not self.mapped_obligation

    def to_json(self) -> dict:
        return {
            "item_id": self.item_id,
            "candidate": self.candidate.to_json(),
            "mapped_obligation": self.mapped_obligation,
            "mapped_by": self.mapped_by,
            "mapped_at": self.mapped_at.isoformat() if self.mapped_at else None,
            "dismissed": self.dismissed,
            "dismissed_reason": self.dismissed_reason,
        }

    @classmethod
    def from_json(cls, raw: dict) -> ReviewItem:
        c = raw["candidate"]
        mapped = raw.get("mapped_at")

        def date(key: str):
            value = c.get(key)
            return _dt.date.fromisoformat(value) if value else None

        return cls(
            item_id=raw["item_id"],
            candidate=Candidate(
                source_document=c.get("source_document", ""),
                page=c.get("page"),
                row=c.get("row"),
                excerpt=c.get("excerpt", ""),
                occurred_on=date("occurred_on"),
                filed_on=date("filed_on"),
                reference=c.get("reference"),
                artifact_type=c.get("artifact_type", ""),
                entity=c.get("entity", ""),
                obligation_id=c.get("obligation_id"),
                confidence=Confidence(c.get("confidence", "UNRESOLVED")),
                why=c.get("why", ""),
            ),
            mapped_obligation=raw.get("mapped_obligation"),
            mapped_by=raw.get("mapped_by", ""),
            mapped_at=_dt.datetime.fromisoformat(mapped) if mapped else None,
            dismissed=raw.get("dismissed", False),
            dismissed_reason=raw.get("dismissed_reason", ""),
        )


def _occasion_id(obligation_id: str, entity: str, occurred_on: _dt.date) -> str:
    """A stable name for one occasion, derived from what makes it that occasion.

    The same duty, the same entity, the same date it fell due, gives the same
    id every time, on any machine, in any order, however many times the register
    is re-uploaded. That is what lets a remediation task attach a record and
    still be pointing at it a month later.

    Twelve hex characters. Not a signature and not a secret, so it is short
    enough to read out over a phone call to whoever filed the thing.
    """
    import hashlib

    seed = f"{obligation_id}|{entity}|{occurred_on.isoformat()}".encode("utf-8")
    return f"EV-{hashlib.sha256(seed).hexdigest()[:12]}"


@dataclass
class ReviewQueue:
    """Everything a company uploaded that nobody has ruled on yet.

    This is the bridge the product was missing. Ingestion produced candidates
    and then discarded any that did not name a rule, so a real margin report
    could be read correctly and still never reach the engine. Holding them
    here means a document arrives once and a person works through it, rather
    than the upload silently achieving nothing.
    """

    path: Path
    items: dict[str, ReviewItem] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> ReviewQueue:
        queue = cls(path=path)
        if not path.is_file():
            return queue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return queue
        for row in data.get("items", []):
            try:
                item = ReviewItem.from_json(row)
            except (KeyError, ValueError):
                continue
            queue.items[item.item_id] = item
        return queue

    def save(self) -> None:
        from sanhita.cli_compile import _write_atomically

        payload = {
            "version": 1,
            "items": [
                i.to_json() for i in sorted(self.items.values(), key=lambda x: x.item_id)
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomically(
            self.path, json.dumps(payload, indent=2, sort_keys=True) + "\n"
        )

    # ------------------------------------------------------------- writing

    def add(self, candidates: list[Candidate]) -> list[ReviewItem]:
        """Take everything an upload found, including what it could not place.

        Candidates that already name a rule are recorded as mapped by the
        document itself, so a CSV that states its obligation does not make a
        person confirm what the file already said.
        """
        added: list[ReviewItem] = []
        start = len(self.items)
        for offset, candidate in enumerate(candidates, start=1):
            item = ReviewItem(
                item_id=f"RV-{start + offset:05d}",
                candidate=candidate,
                mapped_obligation=(
                    candidate.obligation_id
                    if candidate.confidence is Confidence.STATED
                    else None
                ),
                mapped_by="the document itself"
                if candidate.confidence is Confidence.STATED
                else "",
            )
            self.items[item.item_id] = item
            added.append(item)
        return added

    def map_to(
        self, item_id: str, obligation_id: str, *, by: str
    ) -> ReviewItem:
        """A person says which duty this document discharges."""
        item = self.items.get(item_id)
        if item is None:
            raise KeyError(f"No review item {item_id!r}.")
        if not obligation_id.strip():
            raise ValueError("Naming no obligation is not mapping it to one.")
        if item.candidate.occurred_on is None:
            raise ValueError(
                "This candidate carries no date, so there is no occasion for a "
                "rule to be checked against. It cannot become evidence."
            )
        item.mapped_obligation = obligation_id.strip()
        item.mapped_by = by.strip() or "unattributed"
        item.mapped_at = _dt.datetime.now(_dt.timezone.utc)
        item.dismissed = False
        item.dismissed_reason = ""
        return item

    def dismiss(self, item_id: str, *, by: str, reason: str = "") -> ReviewItem:
        """A person says this is not evidence of anything.

        Kept rather than deleted. A reviewer who dismissed a page of a margin
        report should be answerable for that later, and a queue that forgets
        its rejections cannot show one.
        """
        item = self.items.get(item_id)
        if item is None:
            raise KeyError(f"No review item {item_id!r}.")
        item.dismissed = True
        item.dismissed_reason = reason.strip() or "not evidence of an obligation"
        item.mapped_obligation = None
        item.mapped_by = by.strip() or "unattributed"
        item.mapped_at = _dt.datetime.now(_dt.timezone.utc)
        return item

    # ------------------------------------------------------------- reading

    def awaiting(self) -> list[ReviewItem]:
        return sorted(
            (i for i in self.items.values() if i.is_open),
            key=lambda i: (i.candidate.confidence is not Confidence.PROBABLE, i.item_id),
        )

    def mapped(self) -> list[ReviewItem]:
        return sorted(
            (i for i in self.items.values() if i.mapped_obligation),
            key=lambda i: i.item_id,
        )

    def dismissed(self) -> list[ReviewItem]:
        return sorted((i for i in self.items.values() if i.dismissed), key=lambda i: i.item_id)

    def documents(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.items.values():
            name = item.candidate.source_document or "unnamed"
            counts[name] = counts.get(name, 0) + 1
        return dict(sorted(counts.items()))

    def summary(self) -> dict:
        return {
            "total": len(self.items),
            "awaiting": len(self.awaiting()),
            "mapped": len(self.mapped()),
            "dismissed": len(self.dismissed()),
            "documents": len(self.documents()),
        }

    def to_evidence(self, label: str) -> EvidenceStore:
        """Only what a person mapped, or what the document itself stated.

        This is the boundary. Nothing awaiting review and nothing dismissed
        reaches the engine, so an unreviewed upload cannot quietly become a
        compliance conclusion.

        Later records supersede earlier ones about the same occasion. A firm
        that files late and then uploads its corrected register is making a
        second statement about one occasion, not reporting a second occasion.
        Keeping both left the engine reading one duty as never filed and also
        filed, so the breach could never clear no matter what the firm did, and
        the remediation loop had no way to reach CLOSED through the UI.

        Nothing is lost. Every assessment records the hash of the records it
        ran against, so the earlier position stays on the record with its own
        hash and can be reproduced.

        Ids name the occasion, not the row it arrived on. They used to be the
        position in the queue, so re-uploading a register renumbered everything,
        and a remediation task that had attached ``EV-00001`` was left pointing
        at a record that no longer existed. An id that identifies the duty and
        the date it fell due survives every re-upload, which is what makes the
        attachment worth anything.
        """
        store = EvidenceStore(label=label)
        for item in self.mapped():
            candidate = item.candidate
            if candidate.occurred_on is None:
                continue
            resolved = Candidate(
                source_document=candidate.source_document,
                page=candidate.page,
                row=candidate.row,
                excerpt=candidate.excerpt,
                occurred_on=candidate.occurred_on,
                filed_on=candidate.filed_on,
                reference=candidate.reference,
                artifact_type=candidate.artifact_type,
                entity=candidate.entity,
                obligation_id=item.mapped_obligation,
                confidence=candidate.confidence,
                why=candidate.why,
            )
            store.supersede(
                resolved.to_event(
                    _occasion_id(
                        item.mapped_obligation, candidate.entity, candidate.occurred_on
                    ),
                    mapped_by=item.mapped_by,
                    at=item.mapped_at,
                )
            )
        return store
