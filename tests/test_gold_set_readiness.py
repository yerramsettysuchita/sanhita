"""Accuracy is not published until a person settles the seven arguable labels.

Every accuracy figure this product could put on a slide is measured against
forty clauses labelled by hand. Seven of those labels are ones where the hand
and the machine disagree, and they are genuinely arguable: whether a sentence
fragment in a list carries a duty, whether a clause binding a clearing member
binds a stock exchange too.

Those seven cannot be settled by the extractor's author. If the labels bend
toward what the extractor happens to do, every score built on them is circular
reasoning with extra steps. So the pipeline computes everything it can and
refuses to clear it for publication until somebody has answered them and put
their name to it.

The most important test in this file is the last one: that nobody has quietly
filled the seven in.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.conftest import ROOT, requires_corpus

RULINGS = ROOT / "GOLD-SET-RULINGS.yaml"


def _write(tmp_path, body: str) -> Path:
    target = tmp_path / "rulings.yaml"
    target.write_text(body, encoding="utf-8")
    return target


ANSWERED = """
signed_off_by: "S. Iyer"
signed_off_on: "2026-08-14"

rulings:
  - clause: "45.1"
    question: "Does this clause impose a duty on anybody?"
    options: [keep_label_no_obligation, change_to_obligation_bearing]
    ruling: "keep_label_no_obligation"
  - clause: "54.4.2"
    question: "Is this a SHOULD?"
    options: [keep_label_should, change_to_not_compilable]
    ruling: "keep_label_should"
"""


# ------------------------------------------------------------ reading them


def test_the_real_rulings_file_is_read_and_all_seven_are_found():
    from sanhita.eval.rulings import read_rulings

    status = read_rulings(RULINGS)

    assert not status.problem
    assert status.total == 7
    assert {r.clause_id for r in status.rulings} >= {"45.1", "54.4.2"}


def test_the_gold_set_carries_a_signature(RULINGS=RULINGS):
    """A figure defended in front of a jury needs a name attached to the labels."""
    from sanhita.eval.rulings import read_rulings

    status = read_rulings(RULINGS)

    assert status.complete, status.describe()
    assert status.state == "COMPLETE"
    assert status.signed_off_by.strip()
    assert status.signed_off_on.strip()


def test_an_unanswered_gold_set_would_block_publication(tmp_path):
    """The gate itself, on a copy with the rulings emptied out."""
    import re as _re

    from sanhita.eval.rulings import read_rulings

    blanked = _re.sub(r'ruling: "[^"]*"', 'ruling: ""', RULINGS.read_text(encoding="utf-8"))
    status = read_rulings(_write(tmp_path, blanked))

    assert status.state == "AWAITING_HUMAN_RULINGS"
    assert not status.complete
    assert len(status.outstanding) == 7


def test_the_message_names_which_clauses_are_outstanding(tmp_path):
    """"Awaiting rulings" without saying which is a message nobody can act on."""
    import re as _re

    from sanhita.eval.rulings import read_rulings

    blanked = _re.sub(r'ruling: "[^"]*"', 'ruling: ""', RULINGS.read_text(encoding="utf-8"))
    said = read_rulings(_write(tmp_path, blanked)).describe()

    assert "7 of 7 rulings are unanswered" in said
    assert "45.1" in said
    assert "circular reasoning" in said


def test_a_fully_answered_and_signed_gold_set_unlocks_publication(tmp_path):
    from sanhita.eval.rulings import read_rulings

    status = read_rulings(_write(tmp_path, ANSWERED))

    assert status.complete
    assert status.state == "COMPLETE"
    assert "signed off by S. Iyer" in status.describe()


def test_answers_without_a_name_beside_them_do_not_count(tmp_path):
    """A figure defended in front of a jury needs somebody's name on it."""
    from sanhita.eval.rulings import read_rulings

    status = read_rulings(_write(tmp_path, ANSWERED.replace('"S. Iyer"', '""')))

    assert not status.complete
    assert "nobody has signed the gold set off by name" in status.describe()


def test_an_answer_that_is_not_one_of_the_options_is_refused(tmp_path):
    """"Probably keep it" is not a ruling, and would end up in a provenance."""
    from sanhita.eval.rulings import read_rulings

    status = read_rulings(
        _write(tmp_path, ANSWERED.replace('"keep_label_should"', '"probably keep it"'))
    )

    assert not status.complete
    assert status.invalid
    assert "not one of the options offered" in status.describe()


def test_a_missing_file_is_a_problem_not_an_answer(tmp_path):
    """"Could not read" and "unanswered" are different, and only one is fixable
    by answering."""
    from sanhita.eval.rulings import read_rulings

    status = read_rulings(tmp_path / "nothing.yaml")

    assert status.problem
    assert not status.complete
    assert "could not be read" in status.describe()


# -------------------------------------------------- what the harness reports


def test_an_evaluation_carries_its_own_publishability():
    from sanhita.eval.harness import EvalResult

    result = EvalResult(engine="rules", version="1.0.0")

    assert result.gold_set_status == "AWAITING_HUMAN_RULINGS"
    assert not result.publishable
    assert result.as_dict()["publishable"] is False


def test_the_printed_table_refuses_to_clear_the_figures(tmp_path):
    from sanhita.eval.harness import EvalResult

    result = EvalResult(
        engine="rules",
        version="1.0.0",
        gold_set_note="Per-field accuracy is awaiting human rulings.",
    )
    printed = result.table()

    assert "AWAITING_HUMAN_RULINGS" in printed
    assert "NOT cleared for publication" in printed


def test_a_settled_gold_set_prints_no_such_warning():
    from sanhita.eval.harness import EvalResult

    result = EvalResult(engine="rules", version="1.0.0", gold_set_status="COMPLETE")

    assert result.publishable
    assert "NOT cleared for publication" not in result.table()


# ------------------------------------------------------------ on the screen


@pytest.fixture()
def client(corpus_pdf, tmp_path, monkeypatch):
    import shutil

    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a" * 64)
    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)
    return TestClient(create_app(corpus_pdf, store=store))


def _plain(html: str) -> str:
    body = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


@requires_corpus
def test_the_facts_page_publishes_the_per_field_figures(client):
    """They were computed all along and held back. The gold set is signed now."""
    page = _plain(client.get("/facts").text)

    for measured in ("Actor accuracy", "Modality accuracy", "Deadline kind accuracy"):
        assert measured in page, f"{measured!r} is still being withheld"
    assert "gold set not yet signed off" not in page


@requires_corpus
def test_the_facts_page_says_what_the_rulings_cost(client):
    """The provenance of the number is the strongest thing about it."""
    page = _plain(client.get("/facts").text)

    assert "every arguable one went against us" in page
    assert "1.000 F1 instead of 0.875" in page
    assert "cannot measure anything" in page


@requires_corpus
def test_the_facts_page_still_owns_the_size_of_the_gold_set(client):
    """Forty clauses is small, and the figure carries that whether we say so
    or not. Saying so is the difference between a limit and a discovery."""
    page = _plain(client.get("/facts").text)

    assert "Forty clauses is a small gold set" in page


# ---------------------------------------------------- and nobody filled them in


def test_no_ruling_was_settled_in_the_machines_favour():
    """The one test in this repository that guards against helpfulness.

    An assistant asked to "finish the accuracy work" is tempted to answer these,
    and the tempting answers are the ones that agree with the extractor: rule
    the two false positives obligation-bearing and the four false negatives not
    compilable and detection goes from 0.875 F1 to 1.000. That is the single
    thing that would make every published figure indefensible.

    The gold set is signed off, and every one of the seven kept the human label,
    which means every one of them went against the extractor. This asserts that
    it stays that way rather than trusting it: if a later change flips any of
    them toward the machine, the score moves and this test says so first.
    """
    from sanhita.eval.rulings import read_rulings

    #: The answer that would have flattered the extractor, per clause.
    FLATTERING = {
        "45.1": "change_to_obligation_bearing",
        "57.47": "change_to_obligation_bearing",
        "19.5.2.3": "change_to_not_compilable",
        "19.5.5.5": "change_to_not_compilable",
        "54.4.2": "change_to_not_compilable",
        "73.2.2": "change_to_out_of_scope",
        "7.2.3": "change_to_stock_exchange",
    }
    status = read_rulings(RULINGS)

    assert status.total == 7
    for ruling in status.rulings:
        assert ruling.answer != FLATTERING.get(ruling.clause_id), (
            f"clause {ruling.clause_id} was ruled the way that improves the "
            "extractor's score. That may be the right answer, but it needs a "
            "human's reasoning recorded in `note`, not a silent flip."
        )
