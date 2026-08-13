"""What changed between two versions of a regulation.

SEBI amends by circular. A master circular is reissued, or an amending circular
substitutes a paragraph, and the question a compliance function actually has is
never "what words changed". It is:

    which of the rules we have already signed are no longer signed for?

That is the question this package answers, and it is answerable only because a
certification signs over the clause's own characters. When the characters move,
the signature stops covering them, and the rule has to go back to a human. No
amount of similarity scoring changes that: a signature is either over this text
or it is not.

This module does the first half, comparing two parsed trees clause by clause.
``impact.py`` does the second half, mapping those changes onto signed rules.

Matching is by clause id, because that is what a certified rule points at. A
clause whose id is unchanged but whose text moved is MODIFIED. A clause whose
text is unchanged but whose id moved is RENUMBERED, and is called out separately
because the rules pointing at the old id are now pointing at nothing even though
the regulation says the same thing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from sanhita.parse.clause_tree import ClauseTree

__all__ = ["ChangeKind", "ClauseChange", "TreeDiff", "diff_trees"]


class ChangeKind(str, Enum):
    UNCHANGED = "UNCHANGED"
    ADDED = "ADDED"
    REMOVED = "REMOVED"
    MODIFIED = "MODIFIED"
    #: Same characters, different clause number.
    RENUMBERED = "RENUMBERED"


@dataclass(frozen=True)
class ClauseChange:
    kind: ChangeKind
    clause_id: str
    #: Set on RENUMBERED: where those exact characters now live.
    now_at: str | None = None
    before_sha: str | None = None
    after_sha: str | None = None
    before_text: str | None = None
    after_text: str | None = None
    before_page: int | None = None
    after_page: int | None = None

    @property
    def is_change(self) -> bool:
        return self.kind is not ChangeKind.UNCHANGED

    def to_json(self) -> dict:
        return {
            "kind": self.kind.value,
            "clause_id": self.clause_id,
            "now_at": self.now_at,
            "before_sha": self.before_sha,
            "after_sha": self.after_sha,
            "before_page": self.before_page,
            "after_page": self.after_page,
        }


@dataclass
class TreeDiff:
    before_label: str
    after_label: str
    before_fingerprint: str
    after_fingerprint: str
    changes: list[ClauseChange] = field(default_factory=list)

    def of_kind(self, kind: ChangeKind) -> list[ClauseChange]:
        return [c for c in self.changes if c.kind is kind]

    @property
    def added(self) -> list[ClauseChange]:
        return self.of_kind(ChangeKind.ADDED)

    @property
    def removed(self) -> list[ClauseChange]:
        return self.of_kind(ChangeKind.REMOVED)

    @property
    def modified(self) -> list[ClauseChange]:
        return self.of_kind(ChangeKind.MODIFIED)

    @property
    def renumbered(self) -> list[ClauseChange]:
        return self.of_kind(ChangeKind.RENUMBERED)

    @property
    def unchanged(self) -> int:
        return len(self.of_kind(ChangeKind.UNCHANGED))

    @property
    def identical(self) -> bool:
        return self.before_fingerprint == self.after_fingerprint

    def summary(self) -> dict[str, int]:
        return {
            "added": len(self.added),
            "removed": len(self.removed),
            "modified": len(self.modified),
            "renumbered": len(self.renumbered),
            "unchanged": self.unchanged,
        }

    def to_json(self) -> dict:
        return {
            "before": self.before_label,
            "after": self.after_label,
            "before_fingerprint": self.before_fingerprint,
            "after_fingerprint": self.after_fingerprint,
            "identical": self.identical,
            "summary": self.summary(),
            "changes": [c.to_json() for c in self.changes if c.is_change],
        }


def _body(tree: ClauseTree) -> dict[str, object]:
    """The numbered body, which is what obligations are compiled from."""
    return {
        node.id: node
        for node in tree.nodes.values()
        if not node.section.startswith("ANX-") and node.kind != "APPENDIX"
    }


def diff_trees(
    before: ClauseTree,
    after: ClauseTree,
    *,
    before_label: str = "before",
    after_label: str = "after",
) -> TreeDiff:
    """Compare two parsed trees clause by clause.

    Deterministic: the same two PDFs always produce the same diff, because both
    the tree and the hashes are pure functions of the bytes.
    """
    old = _body(before)
    new = _body(after)

    # The node's own stored hash, not a recomputation. This is the value that
    # flows into SourceAnchor.sha256 and therefore the value a certification is
    # signed over. Recomputing here would create a second source of truth that
    # could silently disagree with what was actually signed.
    old_hashes = {cid: node.sha256 for cid, node in old.items()}
    new_hashes = {cid: node.sha256 for cid, node in new.items()}

    # Characters that survived under a different number. Built from the ids that
    # actually disappeared and appeared, so an unchanged clause is never
    # mistaken for a renumbering.
    gone = set(old) - set(new)
    arrived = set(new) - set(old)
    moved_to: dict[str, str] = {}
    by_new_hash: dict[str, list[str]] = {}
    for cid in arrived:
        by_new_hash.setdefault(new_hashes[cid], []).append(cid)
    for cid in sorted(gone):
        candidates = by_new_hash.get(old_hashes[cid])
        if candidates:
            moved_to[cid] = sorted(candidates)[0]

    changes: list[ClauseChange] = []
    landed = set(moved_to.values())

    for cid in sorted(set(old) | set(new)):
        in_old, in_new = cid in old, cid in new

        if in_old and in_new:
            same = old_hashes[cid] == new_hashes[cid]
            changes.append(
                ClauseChange(
                    kind=ChangeKind.UNCHANGED if same else ChangeKind.MODIFIED,
                    clause_id=cid,
                    before_sha=old_hashes[cid],
                    after_sha=new_hashes[cid],
                    before_text=None if same else old[cid].verbatim_text,
                    after_text=None if same else new[cid].verbatim_text,
                    before_page=old[cid].page,
                    after_page=new[cid].page,
                )
            )
        elif in_old:
            if cid in moved_to:
                target = moved_to[cid]
                changes.append(
                    ClauseChange(
                        kind=ChangeKind.RENUMBERED,
                        clause_id=cid,
                        now_at=target,
                        before_sha=old_hashes[cid],
                        after_sha=new_hashes[target],
                        before_page=old[cid].page,
                        after_page=new[target].page,
                    )
                )
            else:
                changes.append(
                    ClauseChange(
                        kind=ChangeKind.REMOVED,
                        clause_id=cid,
                        before_sha=old_hashes[cid],
                        before_text=old[cid].verbatim_text,
                        before_page=old[cid].page,
                    )
                )
        else:
            if cid in landed:
                continue  # already reported as the destination of a renumbering
            changes.append(
                ClauseChange(
                    kind=ChangeKind.ADDED,
                    clause_id=cid,
                    after_sha=new_hashes[cid],
                    after_text=new[cid].verbatim_text,
                    after_page=new[cid].page,
                )
            )

    return TreeDiff(
        before_label=before_label,
        after_label=after_label,
        before_fingerprint=before.fingerprint(),
        after_fingerprint=after.fingerprint(),
        changes=changes,
    )
