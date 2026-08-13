"""Which requirements a record is most likely to answer, ordered, never chosen.

The review screen asked a compliance officer to find one rule among 183 in a
dropdown, for every single record. That is technically complete and practically
unusable, and an unusable review step is worse than no review step, because the
person under time pressure starts picking whatever is nearest the top.

So the candidates are ranked. Three things this does not do, each on purpose.

**It does not preselect.** The form still opens on "choose the rule". A default
that is right most of the time is the most dangerous kind of wrong, because it
converts a judgement into a keystroke and the mistakes it makes are invisible.

**It does not filter.** All 183 stay reachable. A ranking that hides the answer
is a ranking that guarantees a wrong mapping, and the excerpt a reviewer is
reading is often the only thing that knows which rule is right.

**It does not call a model.** This is lexical overlap plus two structural
signals, computed in microseconds from the store. Nothing here runs at
rule-evaluation time and nothing here is a claim about meaning. It reorders a
list; a person still decides, and the decision is what gets recorded.

The score is only ever used to sort. It is deliberately not shown as a
percentage, because a number beside a suggestion invites somebody to trust the
number instead of reading the clause.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

__all__ = ["Suggestion", "rank_obligations", "STOPWORDS"]


#: Words too common in regulatory prose to distinguish one duty from another.
#: "Shall" appears in almost every clause in the circular, so matching on it
#: would rank by clause length rather than by relevance.
STOPWORDS = frozenset(
    """
    a an and any are as at be been by for from has have in is it its of on or
    shall should such that the their there this to under upon was were will
    with within would may must not no if then than these those which who whom
    each every all other same both either neither
    broker brokers stock exchange sebi board circular clause provision
    """.split()
)

#: Groups of words that mean the same duty in a firm's document and in the
#: circular. The firm writes "dispatched", the regulation writes "sent".
#: Without this the overlap between a real register and the rule it answers is
#: often zero.
#:
#: Each group collapses to its first member, so one idea contributes one point
#: however many words express it. Expanding a word into its synonyms instead
#: made "dispatched" worth five, which both inflated every score and printed
#: the same five words under every suggestion, so the explanation said nothing.
SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("dispatch", "dispatched", "send", "sent", "issue", "issued", "transmit", "deliver"),
    ("statement", "report", "return", "returns"),
    ("margin", "collateral"),
    ("reconciliation", "reconcile", "reconciled", "settlement"),
    ("acknowledgement", "acknowledge", "acknowledgment", "receipt", "proof"),
    ("register", "record", "records", "log"),
    ("file", "filed", "filing", "submit", "submitted", "furnish", "furnished"),
    ("client", "clients", "customer", "investor", "constituent"),
    ("quarterly", "quarter"),
    ("monthly", "month"),
    ("daily", "day"),
    ("annual", "annually", "year", "yearly"),
)

#: word -> the one token its group is counted as.
CANONICAL: dict[str, str] = {
    word: group[0] for group in SYNONYM_GROUPS for word in group
}

_WORD = re.compile(r"[a-z]{3,}")


def _words(text: str) -> set[str]:
    """Meaningful ideas in a piece of text, one token per idea.

    Synonyms collapse to a single canonical token rather than expanding, so
    "dispatched" in a firm's register and "sent" in a clause are the same one
    point of overlap, and the shared wording shown to a reviewer names the idea
    once instead of listing every spelling of it.
    """
    return {
        CANONICAL.get(w, w)
        for w in _WORD.findall((text or "").lower())
        if w not in STOPWORDS
    }


@dataclass(frozen=True, slots=True)
class Suggestion:
    """One certified rule, and why it was put where it was in the list."""

    obligation: object
    score: float
    #: The words shared between the record and the rule, for the reviewer to
    #: read. A suggestion that cannot say why it is a suggestion is a guess
    #: wearing a ranking's clothes.
    matched: tuple[str, ...]
    #: True when the record's own artifact type matches what the rule requires.
    artifact_match: bool

    @property
    def is_worth_showing(self) -> bool:
        """Whether this rose above noise, rather than merely sorting first.

        With no signal at all every rule scores zero and the "most likely"
        heading would be a lie about an arbitrary order.
        """
        return self.score > 0


def _rule_words(obligation) -> set[str]:
    """The vocabulary of one rule: what it asks for and what proves it."""
    return _words(
        " ".join(
            [
                getattr(obligation.action, "verb", "") or "",
                getattr(obligation.action, "object", "") or "",
                obligation.source.clause_id or "",
                " ".join(e.artifact_type for e in obligation.evidence),
                " ".join(e.description or "" for e in obligation.evidence),
            ]
        )
    )


def rank_obligations(candidate, obligations, *, limit: int = 5) -> list[Suggestion]:
    """Order certified rules by how likely they are to be what this record answers.

    ``candidate`` is anything carrying ``excerpt``, ``artifact_type`` and
    ``source_document``. Returns at most ``limit`` suggestions, best first, and
    only those with a real signal behind them.
    """
    haystack = _words(
        " ".join(
            str(getattr(candidate, field, "") or "")
            for field in ("excerpt", "artifact_type", "source_document", "reference")
        )
    )
    if not haystack:
        return []

    artifact = (getattr(candidate, "artifact_type", "") or "").strip().lower()

    vocabularies = [(o, _rule_words(o)) for o in obligations]

    # How rare each word is across the rulebook.
    #
    # Without this the ranking is worthless in practice. "Dispatch" appears in
    # dozens of clauses and "reconciliation" in two, so matching on the first
    # says almost nothing and matching on the second nearly names the rule. A
    # plain count treats them as equal, which is how a register headed
    # "reconciliation statement" ended up suggesting four unrelated dispatch
    # rules. Standard inverse document frequency, computed from the store, no
    # model involved.
    total = len(vocabularies) or 1
    frequency: dict[str, int] = {}
    for _, words in vocabularies:
        for word in words:
            frequency[word] = frequency.get(word, 0) + 1

    def weight(word: str) -> float:
        return math.log(total / (1 + frequency.get(word, 0))) + 1.0

    scored: list[Suggestion] = []
    for obligation, rule_words in vocabularies:
        shared = haystack & rule_words
        if not shared:
            continue

        # Rare shared words count for more, then divided by the rule's own
        # vocabulary so a long clause does not outrank a short precise one
        # simply by having more words to hit.
        #
        # The denominator has a floor. Without it a three word clause like
        # "issued from time" beat a real reconciliation duty on the strength of
        # one common word, because dividing by the square root of three is a
        # large bonus for saying almost nothing. Six is the point below which a
        # rule's vocabulary is too thin to be evidence of specificity.
        score = sum(weight(w) for w in shared) / (max(len(rule_words), 6) ** 0.5)

        # The record naming the artifact the rule requires is the strongest
        # signal available without understanding either.
        artifact_match = bool(
            artifact
            and any(artifact == e.artifact_type.strip().lower() for e in obligation.evidence)
        )
        if artifact_match:
            score += 2.0

        # The rarest shared words, which are the ones that explain the rank,
        # then alphabetical for a stable display. Slicing alphabetically would
        # show "client" and hide "reconciliation", which is the opposite of
        # useful.
        distinctive = sorted(
            sorted(shared, key=lambda w: -weight(w))[:6]
        )

        scored.append(
            Suggestion(
                obligation=obligation,
                score=score,
                matched=tuple(distinctive),
                artifact_match=artifact_match,
            )
        )

    scored.sort(key=lambda s: (-s.score, s.obligation.source.clause_id))
    return [s for s in scored[:limit] if s.is_worth_showing]
