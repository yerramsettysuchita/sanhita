"""Five firms that do not exist, and the labelling that makes that honest.

A supervisory view over one firm demonstrates the plumbing and not the point.
The point is what a regulator could see that nobody can see today: the same
clause unmet at four firms out of five, or two firms reading one paragraph
differently and both filing accordingly.

Showing that needs several firms. This installation has one, and inventing real
ones would be the exact fabrication the product is built against. So these are
synthetic, and what these tests hold is everything that keeps that honest: they
are behind a switch, labelled before anything else, never written to disk,
never counted in a published figure, and named so nobody could mistake them for
registered intermediaries.
"""

from __future__ import annotations

import re

import pytest

from tests.conftest import requires_corpus


def _market():
    from sanhita.demo import synthetic_market

    return synthetic_market()


# ------------------------------------------------------- what it contains


def test_there_are_enough_firms_for_a_market_question_to_exist():
    market = _market()

    assert len(market.firms) == 5
    assert market.failing >= 2
    assert len(market.firms) - market.assessed >= 1, (
        "a firm nobody has assessed is the most interesting supervisory row"
    )


def test_the_firms_are_named_so_nobody_could_mistake_them_for_real_ones():
    """"Meridian Securities" would eventually be screenshotted without the caveat."""
    market = _market()

    for firm in market.firms:
        assert re.fullmatch(r"Firm [A-Z]", firm.name), (
            f"{firm.name!r} could be read as a real intermediary"
        )


def test_a_firm_never_assessed_has_no_position_rather_than_a_zero():
    market = _market()
    unassessed = [f for f in market.firms if f.state == "NEVER_ASSESSED"]

    assert unassessed
    assert all(f.position is None for f in unassessed)


def test_a_duty_unmet_at_most_firms_is_the_signal_that_does_not_exist_today():
    market = _market()

    assert market.shared_gaps
    worst = market.shared_gaps[0]
    assert worst.share >= 50
    assert "usually the clause" in worst.describe()


def test_the_shared_gaps_are_worst_first():
    market = _market()
    shares = [len(g.firms) for g in market.shared_gaps]

    assert shares == sorted(shares, reverse=True)


def test_observed_divergence_names_the_readings_and_who_holds_them():
    market = _market()

    assert market.divergences
    first = market.divergences[0]
    assert first.camps >= 2
    assert len(first.readings) >= 3
    assert "all of them believe they comply" in first.describe()


def test_the_demonstration_is_the_same_every_time():
    """A demonstration that differs between two runs is one nobody can check."""
    first, second = _market(), _market()

    assert [f.name for f in first.firms] == [f.name for f in second.firms]
    assert [f.position for f in first.firms] == [f.position for f in second.firms]
    assert [g.clause_id for g in first.shared_gaps] == [
        g.clause_id for g in second.shared_gaps
    ]


def test_the_caveat_travels_with_the_data():
    market = _market()

    assert "not real firms" in market.label
    assert "None of these firms exist" in market.caveat()
    assert "not a finding about the market" in market.caveat()


def test_nothing_here_is_written_anywhere():
    """The property that keeps the synthetic firms from becoming records."""
    import inspect

    from sanhita import demo

    source = inspect.getsource(demo)
    for writer in ("open(", ".save(", "write_text", "json.dump", "Path("):
        assert writer not in source, (
            f"the demonstration touches the filesystem via {writer!r}"
        )


# ------------------------------------------------------------ through the UI


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
def test_the_synthetic_firms_are_off_by_default(client):
    """Nobody should meet Firm A without having asked for it."""
    page = _plain(client.get("/supervisor").text)

    assert "Firm A" not in page
    assert "Show the synthetic demonstration" in page


@requires_corpus
def test_asking_for_it_labels_it_before_it_shows_anything(client):
    body = client.get("/supervisor?demo=1").text
    page = _plain(body)

    assert "Synthetic demonstration data, not real firms and not market data" in page
    assert "Firm A" in page
    # The label comes before the first synthetic figure on the page.
    assert page.index("Synthetic demonstration data") < page.index("Firm A")


@requires_corpus
def test_the_synthetic_firms_are_not_counted_in_the_real_rows(client):
    """The two must never be added together."""
    client.post(
        "/w/demo/company/save",
        data={"name": "ABC Securities Pvt Ltd"},
        follow_redirects=True,
    )
    page = _plain(client.get("/supervisor?demo=1").text)

    real = page[: page.index("Synthetic demonstration data")]
    assert "ABC Securities Pvt Ltd" in real
    assert "Firm A" not in real, "synthetic firms leaked into the real supervisory rows"


@requires_corpus
def test_predicted_and_observed_divergence_are_kept_apart(client):
    """One is computed from the real circular. The other is invented."""
    page = _plain(client.get("/supervisor?demo=1").text)

    assert "Observed divergence, as against predicted" in page
    assert "Predicted divergence is computed from the real circular" in page
    assert "Observed divergence below is synthetic" in page


@requires_corpus
def test_the_demonstration_can_be_put_away_again(client):
    body = client.get("/supervisor?demo=1").text

    assert 'href="/supervisor"' in body


@requires_corpus
def test_the_synthetic_run_writes_nothing_to_the_store(client, tmp_path):
    before = sorted(p.name for p in tmp_path.iterdir())
    client.get("/supervisor?demo=1")
    after = sorted(p.name for p in tmp_path.iterdir())

    assert before == after, "the demonstration left files behind"
