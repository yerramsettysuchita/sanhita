"""Ranking the 183 requirements without ever choosing one.

The review screen asked a compliance officer to find one rule among 183, in a
dropdown, for every record. An unusable review step is worse than none, because
the person under time pressure starts picking whatever is near the top.

The ranking exists to make the right answer easy to find. The properties that
keep it honest are the ones asserted here: nothing is preselected, nothing is
hidden, every suggestion says why it is one, and no model is involved.
"""

from __future__ import annotations

import re

import pytest

from tests.conftest import requires_corpus


def _candidate(excerpt: str, artifact: str = "", reference: str = ""):
    from sanhita.execute.ingest import Candidate, Confidence

    return Candidate(
        source_document="ABC_register.pdf",
        page=1,
        row=None,
        excerpt=excerpt,
        occurred_on=None,
        filed_on=None,
        reference=reference,
        artifact_type=artifact,
        entity="ABC Securities",
        obligation_id=None,
        confidence=Confidence.UNRESOLVED,
        why="",
    )


# ------------------------------------------------------------- the ranking


def test_a_record_with_no_words_suggests_nothing():
    """Silence is the correct answer to no signal, not an arbitrary order."""
    from sanhita.suggest import rank_obligations

    assert rank_obligations(_candidate("   "), []) == []


def test_common_regulatory_words_do_not_drive_the_ranking():
    """"Shall" is in almost every clause, so matching it ranks by length."""
    from sanhita.suggest import STOPWORDS

    for word in ("shall", "the", "broker", "sebi", "exchange"):
        assert word in STOPWORDS


@requires_corpus
def test_the_right_rule_rises_for_a_realistic_record(corpus_pdf, tmp_path):
    """The point of the whole thing, measured against the real store."""
    import shutil

    from sanhita.cli_compile import _load_registry
    from sanhita.ir.enums import RuleStatus
    from sanhita.suggest import rank_obligations

    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)
    certified = [
        o for o in _load_registry(store).all_current() if o.status is RuleStatus.CERTIFIED
    ]

    # Pick a real certified rule and write the kind of line a firm's own
    # register would carry about it.
    target = next(o for o in certified if o.evidence and o.action.object)
    excerpt = f"{target.action.object[:60]} 2026-01-31 REF-001 dispatched"
    ranked = rank_obligations(
        _candidate(excerpt, artifact=target.evidence[0].artifact_type), certified
    )

    assert ranked, "a record quoting a rule's own words suggested nothing"
    assert len(ranked) <= 5, "the shortlist is not short"
    assert any(s.obligation.id == target.id for s in ranked), (
        "the rule the record was written from did not make the shortlist"
    )


@requires_corpus
def test_a_matching_artifact_type_outranks_wording_alone(corpus_pdf, tmp_path):
    """The strongest signal available without understanding either document."""
    import shutil

    from sanhita.cli_compile import _load_registry
    from sanhita.ir.enums import RuleStatus
    from sanhita.suggest import rank_obligations

    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)
    certified = [
        o for o in _load_registry(store).all_current() if o.status is RuleStatus.CERTIFIED
    ]
    target = next(o for o in certified if o.evidence and o.action.object)

    with_artifact = rank_obligations(
        _candidate(target.action.object[:60], artifact=target.evidence[0].artifact_type),
        certified,
    )
    hit = next((s for s in with_artifact if s.obligation.id == target.id), None)
    assert hit is not None and hit.artifact_match
    assert hit.score >= 2.0, "an artifact match added no weight"


def test_every_suggestion_can_say_why_it_is_one():
    """A suggestion that cannot explain itself is a guess wearing a ranking."""
    from sanhita.suggest import rank_obligations

    class _Ev:
        artifact_type = "RECONCILIATION"
        description = "monthly reconciliation statement"

    class _Action:
        verb = "prepare"
        object = "monthly reconciliation statement of client funds"

    class _Src:
        clause_id = "15.10.1"
        section = "15"

    class _Ob:
        id = "SB-15.10.1-a"
        action = _Action()
        source = _Src()
        evidence = [_Ev()]

    ranked = rank_obligations(
        _candidate("Monthly reconciliation statement of client funds, dispatched"),
        [_Ob()],
    )
    assert ranked
    assert ranked[0].matched, "no shared words were recorded"
    assert "reconciliation" in ranked[0].matched


# ---------------------------------------------------------- through the UI


@pytest.fixture()
def client(corpus_pdf, tmp_path, monkeypatch):
    import shutil

    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a-test-only-key")
    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)
    return TestClient(create_app(corpus_pdf, store=store))


def _margin_report() -> bytes:
    import fitz

    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (60, 80),
        "ABC SECURITIES\nMonthly reconciliation statement register\n"
        "2026-01-31   REC-001   dispatched\n"
        "2026-02-28   REC-002   dispatched",
        fontsize=11,
    )
    data = document.tobytes()
    document.close()
    return data


@requires_corpus
def test_nothing_is_preselected_in_the_form(client):
    """A default that is right most of the time is the dangerous kind of wrong."""
    client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "ABC_register.pdf"},
        content=_margin_report(),
    )
    body = client.get("/w/demo/review").text

    # The word appears inside clause text too, so match the attribute rather
    # than the string.
    assert not re.search(r"<option[^>]*selected", body), (
        "an option is preselected in the dropdown"
    )
    assert '<option value="">choose the rule</option>' in body


@requires_corpus
def test_the_full_list_is_still_reachable(client, tmp_path):
    """A ranking that hides the answer guarantees a wrong mapping."""
    from sanhita.cli_compile import _load_registry
    from sanhita.ir.enums import RuleStatus

    client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "ABC_register.pdf"},
        content=_margin_report(),
    )
    body = client.get("/w/demo/review").text

    certified = sum(
        1
        for o in _load_registry(tmp_path / "rules.json").all_current()
        if o.status is RuleStatus.CERTIFIED
    )
    # One <option> per certified rule per awaiting item, plus the empty prompt.
    assert body.count('<option value="SB-') >= certified, (
        "the ranking removed rules from the list instead of reordering it"
    )


@requires_corpus
def test_the_shortlist_says_it_is_an_ordering_not_a_reading(client):
    client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "ABC_register.pdf"},
        content=_margin_report(),
    )
    page = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", client.get("/w/demo/review").text))

    assert "Most likely, by wording. You decide" in page
    assert "not a reading of either document" in page
    assert "Check the clause before you agree with it" in page


@requires_corpus
def test_no_score_is_shown_as_a_number(client):
    """A percentage beside a suggestion invites trusting it over the clause."""
    client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "ABC_register.pdf"},
        content=_margin_report(),
    )
    page = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", client.get("/w/demo/review").text))

    section = page[page.find("Most likely, by wording") :][:1200]
    assert not re.search(r"\d+(\.\d+)?%\s*(match|confiden|likely)", section, re.I)


def test_one_idea_counts_once_however_many_words_express_it():
    """Synonym expansion used to make a single word worth five.

    "dispatched" became send, sent, issue, transmit and deliver, which inflated
    every score and printed the same five words under every suggestion, so the
    explanation told a reviewer nothing about why this rule rather than that
    one.
    """
    from sanhita.suggest import CANONICAL, _words

    assert _words("dispatched") == _words("sent") == _words("transmit")
    assert len(_words("dispatched sent transmit deliver")) == 1

    # Every group collapses to exactly one token.
    for word, canon in CANONICAL.items():
        assert _words(word) == {canon}, word


def test_the_shared_wording_names_the_idea_not_every_spelling():
    from sanhita.suggest import rank_obligations

    class _Ev:
        artifact_type = "STATEMENT"
        description = "monthly statement sent to clients"

    class _Action:
        verb = "send"
        object = "monthly statement to each client"

    class _Src:
        clause_id = "1.1"
        section = "1"

    class _Ob:
        id = "SB-1.1-a"
        action = _Action()
        source = _Src()
        evidence = [_Ev()]

    ranked = rank_obligations(
        _candidate("Monthly statement dispatched to every client"), [_Ob()]
    )

    assert ranked
    matched = ranked[0].matched
    assert len(matched) == len(set(matched)), "a word was counted twice"
    assert "dispatch" in matched, "the idea is not named"
    for spelling in ("sent", "send", "transmit", "deliver", "issue"):
        assert spelling not in matched, f"{spelling} is a spelling, not an idea"


@requires_corpus
def test_a_rare_word_outranks_a_common_one(corpus_pdf, tmp_path):
    """The fix that made the shortlist worth reading.

    "Dispatch" appears across dozens of clauses and "reconciliation" in a
    handful, so matching the first says almost nothing. Counting them equally
    put four unrelated dispatch rules above the reconciliation duty a
    reconciliation register plainly answers.
    """
    import shutil

    from sanhita.cli_compile import _load_registry
    from sanhita.ir.enums import RuleStatus
    from sanhita.suggest import rank_obligations

    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)
    certified = [
        o for o in _load_registry(store).all_current() if o.status is RuleStatus.CERTIFIED
    ]

    ranked = rank_obligations(
        _candidate("2026-01-31 REC-001", artifact="RECONCILIATION"), certified
    )

    assert ranked, "a record naming its artifact type suggested nothing"
    assert "reconciliation" in ranked[0].matched, (
        f"a common word outranked the rare one: {ranked[0].matched}"
    )
    assert ranked[0].artifact_match
    assert ranked[0].score > ranked[-1].score * 1.5, (
        "the shortlist is flat, so it is not really a ranking"
    )


@requires_corpus
def test_a_thin_clause_does_not_win_on_one_common_word(corpus_pdf, tmp_path):
    """Dividing by the square root of three was a large bonus for saying little."""
    import shutil

    from sanhita.cli_compile import _load_registry
    from sanhita.ir.enums import RuleStatus
    from sanhita.suggest import rank_obligations

    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)
    certified = [
        o for o in _load_registry(store).all_current() if o.status is RuleStatus.CERTIFIED
    ]

    ranked = rank_obligations(
        _candidate("2026-01-31 REC-001 dispatched", artifact=""), certified
    )
    # Every suggestion resting on a single common word should score close
    # together, rather than one thin clause running away with it.
    single_word = [s for s in ranked if len(s.matched) == 1]
    if len(single_word) > 1:
        assert single_word[0].score / single_word[-1].score < 2.0, (
            "a short clause is still winning on vocabulary length alone"
        )
