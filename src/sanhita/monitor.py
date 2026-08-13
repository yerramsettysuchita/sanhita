"""Has a newer edition of your rulebook arrived, and has anybody looked at it?

The product could compare two editions beautifully, once a person thought to
open the comparison screen and pick the earlier one from a dropdown. That is
not monitoring. It is a tool that answers a question nobody remembers to ask,
and the whole point of a regulatory change is that it happens on SEBI's
calendar rather than on the compliance officer's.

So this module asks the question on the firm's behalf, every time a screen
loads: of the rulebooks this firm declared, is there an edition on file that
is newer than the one it is being assessed against, and has that edition been
compared yet?

**What this does not do, and the screens must say so.** It does not watch
sebi.gov.in. Nothing here polls a website, subscribes to a feed, or fetches
anything over a network. It watches the documents somebody brought to this
installation, which means an edition SEBI published last week is invisible
until a person uploads it. Calling that "continuous regulatory monitoring"
without the qualification would be the most damaging kind of overclaim: a firm
would believe it was covered, and the silence would mean nothing at all.

What it removes is the other failure, which is real and common: the newer
circular is sitting in the system, somebody uploaded it in March, and no
comparison was ever run because the screen that runs one is three clicks away
and nothing ever said it was worth visiting.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from enum import Enum

__all__ = ["EditionState", "WatchedEdition", "RegulatoryWatch", "watch_for_firm"]


class EditionState(str, Enum):
    """Where one newer edition has got to."""

    #: On file, newer than the one in use, and never compared against it.
    NOT_COMPARED = "NOT_COMPARED"
    #: Compared, and at least one required action was raised as a task.
    IN_HAND = "IN_HAND"
    #: Every task raised from it has closed.
    SETTLED = "SETTLED"

    @property
    def label(self) -> str:
        return {
            EditionState.NOT_COMPARED: "Nobody has looked at this yet",
            EditionState.IN_HAND: "Being worked",
            EditionState.SETTLED: "Done",
        }[self]

    @property
    def needs_attention(self) -> bool:
        return self is not EditionState.SETTLED


@dataclass(frozen=True)
class WatchedEdition:
    """One edition on file that is newer than the one this firm runs on."""

    workspace_id: str
    name: str
    state: EditionState
    #: The date printed on the regulation, where the document gave one. Not the
    #: date it was uploaded, which says nothing about the regulation.
    issued_on: _dt.date | None = None
    #: How long it has been sitting here unexamined.
    uploaded_at: _dt.datetime | None = None
    certified_in_use: int = 0
    tasks_open: int = 0
    tasks_total: int = 0

    @property
    def days_waiting(self) -> int | None:
        if self.uploaded_at is None:
            return None
        now = _dt.datetime.now(_dt.timezone.utc)
        return max(0, (now - self.uploaded_at).days)

    def describe(self) -> str:
        """One sentence, and no verb the product cannot back up."""
        if self.state is EditionState.NOT_COMPARED:
            waited = (
                f" It has been on file for {self.days_waiting} day(s)."
                if self.days_waiting
                else ""
            )
            return (
                f"{self.name} is on file and is a later edition than the one "
                f"this firm is assessed against. Nothing has been compared "
                f"against it, so it is not known which of the "
                f"{self.certified_in_use} certified rules it touches.{waited}"
            )
        if self.state is EditionState.IN_HAND:
            return (
                f"{self.name} has been compared. {self.tasks_open} of "
                f"{self.tasks_total} action(s) raised from it are still open."
            )
        return (
            f"{self.name} has been compared and every one of the "
            f"{self.tasks_total} action(s) raised from it has closed."
        )


@dataclass
class RegulatoryWatch:
    """What has arrived for this firm, and what state each arrival is in."""

    firm: str
    #: The edition the firm is currently assessed against.
    in_use: str = ""
    editions: list[WatchedEdition] = field(default_factory=list)
    #: Rulebooks the firm declared that hold no certified rule, so an
    #: assessment against them would return nothing. Reported because a firm
    #: that ticked a box believes it is covered.
    declared_but_empty: list[str] = field(default_factory=list)
    #: Declared rulebooks whose issue date could not be read off page one, so
    #: nothing can say whether they are earlier or later than the one in use.
    #: Named rather than dropped: an unorderable edition is the one most likely
    #: to be a later circular nobody has noticed.
    undated: list[str] = field(default_factory=list)

    @property
    def waiting(self) -> int:
        return sum(1 for e in self.editions if e.state is EditionState.NOT_COMPARED)

    @property
    def needing_attention(self) -> int:
        return sum(1 for e in self.editions if e.state.needs_attention)

    @property
    def is_quiet(self) -> bool:
        return not self.editions and not self.declared_but_empty and not self.undated

    def headline(self) -> str:
        if self.waiting:
            return (
                f"{self.waiting} later edition(s) of this firm's rulebooks are on "
                "file and have never been compared against the one in use."
            )
        if self.needing_attention:
            return (
                f"{self.needing_attention} regulatory change(s) have been compared "
                "and still have work outstanding."
            )
        if self.declared_but_empty:
            return (
                f"{len(self.declared_but_empty)} declared rulebook(s) carry no "
                "certified rule, so an assessment against them would find nothing."
            )
        if self.undated:
            return (
                f"{len(self.undated)} declared rulebook(s) carry no readable issue "
                "date, so nothing can say whether they are later editions."
            )
        return "No later edition of this firm's rulebooks is on file."


def watch_for_firm(
    *,
    firm: str,
    in_use_id: str,
    in_use_name: str,
    in_use_issued_on: _dt.date | None,
    in_use_fingerprint: str,
    certified_in_use: int,
    candidates,
    tasks_for=None,
) -> RegulatoryWatch:
    """Work out which editions on file are ahead of the one in use.

    ``candidates`` are dicts as the framework picker already builds them: id,
    name, issued_on, certified, declared, and optionally created_at. Only the
    ones the firm declared are watched, because an unrelated rulebook somebody
    uploaded is not this firm's regulatory change.

    ``tasks_for(workspace_id)`` returns the remediation tasks kept beside that
    edition. An amendment task records the fingerprint of the edition it was
    compared *from*, so the tasks are themselves the record of what has been
    looked at, and no new store and no re-parsing are needed to read it.
    """
    watch = RegulatoryWatch(firm=firm, in_use=in_use_name)

    for row in candidates:
        if not row.get("declared") or row.get("id") == in_use_id:
            continue
        if not row.get("certified"):
            watch.declared_but_empty.append(row.get("name", row.get("id", "")))

        issued = row.get("issued_on")
        if issued is None or in_use_issued_on is None:
            # An edition nothing can order. Reported rather than dropped: a
            # rulebook whose date could not be read is exactly the one most
            # likely to be a later edition nobody has noticed, and silently
            # skipping it would make this watch quietest where it matters most.
            watch.undated.append(row.get("name", row.get("id", "")))
            continue
        # Only later editions. An earlier one is the thing you compare
        # against, not something that has arrived.
        if issued <= in_use_issued_on:
            continue

        raised = [
            t
            for t in (tasks_for(row["id"]) if tasks_for else [])
            if getattr(t, "amended_from", "") == in_use_fingerprint
        ]
        open_tasks = [t for t in raised if t.status.is_open]
        if not raised:
            state = EditionState.NOT_COMPARED
        elif open_tasks:
            state = EditionState.IN_HAND
        else:
            state = EditionState.SETTLED

        watch.editions.append(
            WatchedEdition(
                workspace_id=row["id"],
                name=row.get("name", row["id"]),
                state=state,
                issued_on=issued,
                uploaded_at=row.get("created_at"),
                certified_in_use=certified_in_use,
                tasks_open=len(open_tasks),
                tasks_total=len(raised),
            )
        )

    # Unexamined first, settled last, and newest within each.
    watch.editions.sort(
        key=lambda e: (e.state is EditionState.SETTLED, -(e.issued_on or _dt.date.min).toordinal())
    )
    return watch
