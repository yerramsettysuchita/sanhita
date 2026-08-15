"""Which firms one person has recorded, and which of them they are looking at.

A visitor could hold one firm. Every sidecar a firm owns is keyed by the
visitor and nothing else, so a compliance officer advising two brokers had to
sign out and use another browser to see the second, and no screen anywhere
listed what they had recorded.

This is the index that makes several possible. It holds ids and names, nothing
more: the firm's own profile, its records, its assessments and its tasks all
stay in the sidecars they were already in, and the id is what tells those
sidecars apart.

**The first company keeps the unscoped name.** Its slot is the empty string, so
its files are exactly the files a single-company deployment already has and
nobody migrates anything. Later companies take ``c2``, ``c3`` and so on:

    company one    company.u4c55baa9.json        <- unchanged, existing data
    company two    company.u4c55baa9.c2.json
    company three  company.u4c55baa9.c3.json

Slots are never reused. Deleting the second company does not free ``c2`` for
the next one, because a stale file left behind by a half-finished delete would
then be adopted by a firm it has nothing to do with. Compliance records under
the wrong firm's name is the worst thing this product could do, and a
monotonic counter costs nothing.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["CompanySlot", "CompanyIndex", "FIRST_SLOT"]

#: The first company's slot. Empty on purpose: its filenames carry no suffix,
#: so a deployment that already holds one firm keeps it untouched.
FIRST_SLOT = ""


@dataclass
class CompanySlot:
    """One firm in the index. The name is a label, not the profile."""

    #: "" for the first, then "c2", "c3". Part of the sidecar filename.
    slot: str
    #: Shown in the list and the switcher. The profile holds the real record.
    name: str = ""
    added_at: _dt.datetime | None = None

    @property
    def id(self) -> str:
        """What a URL calls this slot.

        The empty slot needs a name a path can carry, so it is spelled "first"
        in a URL and "" on disk. Keeping the disk form empty is what avoids the
        migration; keeping the URL form non-empty is what avoids a route that
        ends in a bare slash.
        """
        return self.slot or "first"

    def to_json(self) -> dict:
        return {
            "slot": self.slot,
            "name": self.name,
            "added_at": self.added_at.isoformat() if self.added_at else None,
        }

    @classmethod
    def from_json(cls, raw: dict) -> "CompanySlot":
        stamp = raw.get("added_at")
        return cls(
            slot=raw.get("slot", ""),
            name=raw.get("name", ""),
            added_at=_dt.datetime.fromisoformat(stamp) if stamp else None,
        )


@dataclass
class CompanyIndex:
    """Every firm one visitor has recorded, and which is open."""

    path: Path | None = None
    slots: list[CompanySlot] = field(default_factory=list)
    #: The slot whose data every screen is currently reading.
    active: str = FIRST_SLOT
    #: Highest slot number ever issued. Never decreases, so a deleted
    #: company's files can never be adopted by a later one.
    issued: int = 1

    # ------------------------------------------------------------- reading

    @property
    def is_empty(self) -> bool:
        return not self.slots

    def get(self, slot: str) -> CompanySlot | None:
        for entry in self.slots:
            if entry.slot == slot:
                return entry
        return None

    def by_id(self, identifier: str) -> CompanySlot | None:
        """Resolve what a URL called a slot back to the slot itself."""
        wanted = FIRST_SLOT if identifier in ("first", "") else identifier
        return self.get(wanted)

    def current(self) -> CompanySlot | None:
        return self.get(self.active)

    # ------------------------------------------------------------- writing

    def ensure_first(self, *, name: str = "", at: _dt.datetime | None = None) -> CompanySlot:
        """Record that the unscoped files exist, without moving them.

        Called the first time a visitor with an existing firm reaches a screen
        that needs the index. Their data is already company one; this only
        gives the list something to show.
        """
        existing = self.get(FIRST_SLOT)
        if existing is not None:
            if name and not existing.name:
                existing.name = name
            return existing
        entry = CompanySlot(slot=FIRST_SLOT, name=name, added_at=at)
        self.slots.insert(0, entry)
        return entry

    def add(self, *, name: str = "", at: _dt.datetime | None = None) -> CompanySlot:
        """Issue a slot for a firm nobody has recorded yet."""
        if self.get(FIRST_SLOT) is None:
            return self.ensure_first(name=name, at=at)
        self.issued += 1
        entry = CompanySlot(slot=f"c{self.issued}", name=name, added_at=at)
        self.slots.append(entry)
        return entry

    def rename(self, slot: str, name: str) -> None:
        entry = self.get(slot)
        if entry is not None and name:
            entry.name = name

    def open(self, slot: str) -> bool:
        """Make one of them current. False when it is not one of theirs."""
        if self.get(slot) is None:
            return False
        self.active = slot
        return True

    # -------------------------------------------------------------- on disk

    def to_json(self) -> dict:
        return {
            "slots": [s.to_json() for s in self.slots],
            "active": self.active,
            "issued": self.issued,
        }

    def save(self, path: Path | None = None) -> None:
        target = Path(path or self.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_json(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        self.path = target

    @classmethod
    def load(cls, path: Path) -> "CompanyIndex":
        path = Path(path)
        if not path.is_file():
            return cls(path=path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls(path=path)
        index = cls(
            path=path,
            slots=[CompanySlot.from_json(s) for s in raw.get("slots", [])],
            active=raw.get("active", FIRST_SLOT),
            issued=int(raw.get("issued", 1)),
        )
        # An active slot naming a company that is gone would read another
        # firm's files. Fall back rather than trust it.
        if index.get(index.active) is None:
            index.active = index.slots[0].slot if index.slots else FIRST_SLOT
        return index
