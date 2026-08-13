"""Which part of the firm actually owns each obligation.

Problem Statement 2 asks for something the product stopped just short of:

    "...mapping it to the affected intermediary's operational processes, and
     updating compliance workflows..."

Sanhita maps a clause to an obligation and an obligation to an actor. "Actor"
means stock broker: the category of firm the duty binds. It does not mean the
desk, the system, or the written procedure inside that firm which actually
discharges it. So a gap report could say *rule SB-114.2 produced no evidence*,
which is a finding, when what a compliance officer needs is *Operations, margin
engine, SOP-12 produced no evidence*, which is an action.

**Why this is a sidecar and not a field.**

An obligation's certified bytes are what the signature covers. Adding a field to
:class:`~sanhita.ir.schema.Obligation` changes the signing payload, which
invalidates every signature already in the store. There are 183 of them and they
were made by a named person; silently breaking them to add an operational
convenience would be exactly the kind of thing this product exists to prevent.

A control binding is also not the same *kind* of fact as the rest of the IR.
Everything in an Obligation is a claim about what the regulation says, anchored
to a clause and a hash. A binding is a claim about how one firm has chosen to
organise itself. Two firms reading the same certified rule will bind it to
different teams and both will be right. It belongs beside the rulebook, not
inside it.

So bindings live in their own file, keyed by obligation id, and the signature
over the obligation stays valid forever.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["ControlBinding", "ControlStore"]


@dataclass(frozen=True)
class ControlBinding:
    """Who inside the firm discharges one obligation, and by what procedure.

    The chain the problem statement asks for runs

        clause -> obligation -> process -> function -> system -> control -> evidence

    and the four middle links live here. ``process`` was the missing one. A
    function is who, a system is where, a control is the written procedure, and
    a process is the piece of the business the duty attaches to. Without it the
    binding answered "which team" but not "which part of what this firm
    actually does", which is the question a supervisor asks first.
    """

    obligation_id: str
    #: The team or function that owns it, e.g. "Operations", "Surveillance".
    function: str
    #: The business process the duty attaches to, e.g. "Daily margin
    #: reporting", "Client onboarding". Optional, because a firm can bind a
    #: rule to a team before it has named its processes.
    process: str = ""
    #: The system that holds the evidence, e.g. "margin engine".
    system: str = ""
    #: The firm's own procedure reference, e.g. "SOP-12".
    control_ref: str = ""
    #: Who recorded this binding. A binding is an assertion by a person about
    #: how their firm works, so it carries a name for the same reason a
    #: certification does.
    bound_by: str = ""
    bound_at: _dt.datetime | None = None
    note: str = ""

    def describe(self) -> str:
        """The phrase a gap report puts in front of a finding."""
        parts = [self.function]
        if self.system:
            parts.append(self.system)
        if self.control_ref:
            parts.append(self.control_ref)
        return ", ".join(p for p in parts if p)

    def chain(self) -> list[tuple[str, str]]:
        """The operational chain, as labelled links, skipping what is unset.

        Rendered left to right on screen so a reviewer can read from the clause
        all the way to the artifact without holding anything in their head.
        """
        links = [
            ("process", self.process),
            ("function", self.function),
            ("system", self.system),
            ("control", self.control_ref),
        ]
        return [(label, value) for label, value in links if value]

    @property
    def is_complete(self) -> bool:
        """Every link named. A partial chain still leaves somebody guessing."""
        return all((self.process, self.function, self.system, self.control_ref))

    def to_json(self) -> dict:
        return {
            "obligation_id": self.obligation_id,
            "function": self.function,
            "process": self.process,
            "system": self.system,
            "control_ref": self.control_ref,
            "bound_by": self.bound_by,
            "bound_at": self.bound_at.isoformat() if self.bound_at else None,
            "note": self.note,
        }

    @classmethod
    def from_json(cls, data: dict) -> ControlBinding:
        raw = data.get("bound_at")
        return cls(
            obligation_id=data["obligation_id"],
            function=data.get("function", ""),
            # Absent from bindings written before processes existed. Reading
            # them must not fail, so it defaults rather than raising.
            process=data.get("process", ""),
            system=data.get("system", ""),
            control_ref=data.get("control_ref", ""),
            bound_by=data.get("bound_by", ""),
            bound_at=_dt.datetime.fromisoformat(raw) if raw else None,
            note=data.get("note", ""),
        )


@dataclass
class ControlStore:
    """Every control binding for one workspace.

    Written next to that workspace's rules, atomically, the same way the
    registry is. Losing this file loses operational mapping and nothing else:
    the rulebook, the signatures and the audit chain are untouched by anything
    in here, which is the property that made a sidecar the right shape.
    """

    path: Path
    bindings: dict[str, ControlBinding] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> ControlStore:
        store = cls(path=path)
        if not path.is_file():
            return store
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt sidecar must never take the rulebook down with it.
            return store
        for row in data.get("bindings", []):
            try:
                binding = ControlBinding.from_json(row)
            except (KeyError, ValueError):
                continue
            store.bindings[binding.obligation_id] = binding
        return store

    def save(self) -> None:
        from sanhita.cli_compile import _write_atomically

        payload = {
            "version": 1,
            "bindings": [
                b.to_json()
                for b in sorted(self.bindings.values(), key=lambda x: x.obligation_id)
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomically(
            self.path, json.dumps(payload, indent=2, sort_keys=True) + "\n"
        )

    # ------------------------------------------------------------------ use

    def get(self, obligation_id: str) -> ControlBinding | None:
        return self.bindings.get(obligation_id)

    def bind(
        self,
        obligation_id: str,
        *,
        function: str,
        process: str = "",
        system: str = "",
        control_ref: str = "",
        bound_by: str = "",
        note: str = "",
    ) -> ControlBinding:
        """Record who owns this obligation. Overwrites any previous binding."""
        if not function.strip():
            raise ValueError("A binding needs a function that owns the obligation.")
        binding = ControlBinding(
            obligation_id=obligation_id,
            function=function.strip(),
            process=process.strip(),
            system=system.strip(),
            control_ref=control_ref.strip(),
            bound_by=bound_by.strip(),
            bound_at=_dt.datetime.now(_dt.timezone.utc),
            note=note.strip(),
        )
        self.bindings[obligation_id] = binding
        return binding

    def unbind(self, obligation_id: str) -> bool:
        return self.bindings.pop(obligation_id, None) is not None

    def by_function(self) -> dict[str, list[ControlBinding]]:
        grouped: dict[str, list[ControlBinding]] = {}
        for binding in self.bindings.values():
            grouped.setdefault(binding.function, []).append(binding)
        for entries in grouped.values():
            entries.sort(key=lambda b: b.obligation_id)
        return dict(sorted(grouped.items()))

    def by_process(self) -> dict[str, list[ControlBinding]]:
        """Bindings grouped by the business process they attach to.

        Rules bound to a team but not yet to a named process land under an
        empty key rather than being dropped, so the screen can show that the
        mapping is incomplete instead of quietly looking finished.
        """
        grouped: dict[str, list[ControlBinding]] = {}
        for binding in self.bindings.values():
            grouped.setdefault(binding.process, []).append(binding)
        for entries in grouped.values():
            entries.sort(key=lambda b: b.obligation_id)
        return dict(sorted(grouped.items(), key=lambda kv: (kv[0] == "", kv[0])))

    def systems(self) -> dict[str, int]:
        """Which systems of record carry how many duties."""
        counts: dict[str, int] = {}
        for binding in self.bindings.values():
            if binding.system:
                counts[binding.system] = counts.get(binding.system, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def coverage(self, obligation_ids: list[str]) -> dict:
        """How much of a given set of rules has an owner inside the firm.

        ``bound`` counts rules with any binding at all. ``mapped`` counts the
        ones whose chain is complete from process through to control, which is
        the figure that means something operationally. A rule bound to
        "Operations" and nothing else does not tell anybody what to go and fix.
        """
        total = len(obligation_ids)
        bound = sum(1 for oid in obligation_ids if oid in self.bindings)
        mapped = sum(
            1
            for oid in obligation_ids
            if (binding := self.bindings.get(oid)) is not None and binding.is_complete
        )
        return {
            "total": total,
            "bound": bound,
            "mapped": mapped,
            "unbound": total - bound,
            "ratio": round(bound / total, 4) if total else 0.0,
            "mapped_ratio": round(mapped / total, 4) if total else 0.0,
        }
