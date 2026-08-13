"""The compliance dashboard, and the rule that every figure on it is computed.

The audit's point was that the product was organised around the regulation
while the user arrives asking about their firm. This screen is the answer, and
the thing worth defending is that nothing on it is stored. A dashboard whose
numbers are written down somewhere is a dashboard that will one day disagree
with the engine.
"""

from __future__ import annotations

import re

import pytest

from tests.conftest import requires_corpus, sign_in


@pytest.fixture()
def client(corpus_pdf, tmp_path, monkeypatch):
    import shutil

    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a-test-only-key")
    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)
    client = TestClient(create_app(corpus_pdf, store=store))
    # Compliance actions record who did them, so the journey these
    # tests walk needs an authenticated officer behind it.
    sign_in(client)
    return client


def _plain(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _finish_setup(client, name: str = "ABC Securities"):
    """All three answers, so the compliance lifecycle row is the one rendered.

    Setup and the lifecycle are two modes and only one is on screen at a time,
    so a test about the lifecycle has to get the firm out of setup first.
    """
    client.post(
        "/w/demo/company/save", data={"name": name}, follow_redirects=True
    )
    client.post(
        "/w/demo/company/frameworks", data={"framework": "demo"}, follow_redirects=True
    )
    client.post("/w/demo/setup/complete", follow_redirects=True)


def _stagebar(html: str) -> str:
    """Just the journey row, which is its own element under the masthead."""
    strip = html[html.index('class="stagebar"') :]
    return strip[: strip.index("</nav>")]


@requires_corpus
def test_it_asks_who_the_firm_is_before_answering_anything(client):
    page = _plain(client.get("/w/demo/company").text)

    assert "Whose compliance is this" in page
    assert "Firm name" in page


@requires_corpus
def test_saving_a_profile_makes_the_firm_the_headline(client, tmp_path):
    response = client.post(
        "/w/demo/company/save",
        data={
            "name": "ABC Securities Pvt Ltd",
            "intermediary": "STOCK_BROKER",
            "registration": "INZ000000000",
            "processes": "Daily margin reporting\nClient onboarding",
            "systems": "Margin engine\nCRM",
            "facts": "Offers derivatives\n-Offers portfolio management",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    # Saving lands on setup step two, which names the firm and asks which
    # rulebooks govern it.
    assert "Which SEBI rulebooks apply to ABC Securities Pvt Ltd" in _plain(response.text)

    _declare_a_framework(client)
    page = _plain(client.get("/w/demo/company").text)
    assert "ABC Securities Pvt Ltd" in page
    assert "stock broker" in page
    assert "INZ000000000" in page

    # And it persisted, not just rendered.
    from sanhita.company import Company

    saved = Company.load(tmp_path / "company.json")
    assert saved is not None
    assert saved.processes == ["Daily margin reporting", "Client onboarding"]
    assert saved.business_facts == {
        "Offers derivatives": True,
        "Offers portfolio management": False,
    }


@requires_corpus
def test_a_minus_prefix_records_a_fact_as_no(client, tmp_path):
    """Many SEBI duties are conditional, and no is as load bearing as yes."""
    from sanhita.company import Company

    client.post(
        "/w/demo/company/save",
        data={"name": "X", "facts": "-Holds client funds"},
        follow_redirects=True,
    )

    assert Company.load(tmp_path / "company.json").business_facts == {
        "Holds client funds": False
    }


def _certified_id(tmp_path) -> str:
    from sanhita.cli_compile import _load_registry
    from sanhita.ir.enums import RuleStatus

    for rule in _load_registry(tmp_path / "rules.json").all_current():
        if rule.status is RuleStatus.CERTIFIED:
            return rule.id
    raise AssertionError("no certified rule in the store")


def _declare_a_framework(client):
    """Setup steps two and three, so the overview is reachable.

    The dashboard now waits for all three answers, not two. A firm that has
    named itself and its rulebook is still setting up until it has been asked
    for its records.
    """
    client.post(
        "/w/demo/company/frameworks", data={"framework": "demo"}, follow_redirects=True
    )
    client.post("/w/demo/setup/complete", follow_redirects=True)


def _upload_real_evidence(client, tmp_path) -> str:
    """Give the firm actual records, the way a person would."""
    rule_id = _certified_id(tmp_path)
    csv_text = (
        "obligation_id,entity,occurred_on,filed_on,artifact_type,reference\n"
        f"{rule_id},ABC Securities,2026-01-31,2026-02-01,report,RET-001\n"
    )
    client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "register.csv"},
        content=csv_text.encode(),
    )
    return rule_id


@requires_corpus
def test_no_score_exists_before_the_firm_provides_anything(client):
    """The most misleading thing this product could do, and it used to.

    A percentage beside a firm's name reads as a finding about that firm. Run
    against generated events it is a finding about a random seed.
    """
    client.post(
        "/w/demo/company/save", data={"name": "ABC Securities"}, follow_redirects=True
    )
    _declare_a_framework(client)

    page = _plain(client.get("/w/demo/company").text)

    assert "Assessment not available" in page
    assert not re.search(r"\d+% Of what could be determined", page), "a score appeared with no evidence"
    assert "Upload compliance evidence" in page


@requires_corpus
def test_the_health_figure_matches_the_engine_once_evidence_exists(client, tmp_path):
    """Not stored anywhere. It has to agree with what the engine returns."""
    import datetime as _dt

    from sanhita.execute import WEEKENDS_ONLY, EvidenceStore, RuleEngine
    from sanhita.execute.report import Outcome
    from sanhita.ir.enums import RuleStatus

    client.post(
        "/w/demo/company/save", data={"name": "ABC Securities"}, follow_redirects=True
    )
    _declare_a_framework(client)
    _upload_real_evidence(client, tmp_path)
    client.post("/w/demo/assess", follow_redirects=True)

    page = _plain(client.get("/w/demo/company").text)
    match = re.search(r"(\d+)% Of what could be determined", page)
    assert match, "an assessment should exist once evidence is provided"
    shown = int(match.group(1))

    # Recompute independently, from the same evidence file the app reads.
    from sanhita.cli_compile import _load_registry

    rules = _load_registry(tmp_path / "rules.json").all_current()
    certified = [o for o in rules if o.status is RuleStatus.CERTIFIED]
    report = RuleEngine(WEEKENDS_ONLY).run(
        rules, EvidenceStore.load(tmp_path / "evidence.json"), as_of=_dt.date.today()
    )
    # The same split the dashboard makes. A duty with no record either way is
    # not a failure and is not in the denominator: the figure is over what the
    # records actually settle.
    failing = {
        f.obligation_id
        for f in report.findings
        if f.outcome in (Outcome.MISSING, Outcome.LATE)
    }
    unverified = {
        f.obligation_id for f in report.findings if f.outcome is Outcome.NO_EVIDENCE
    } - failing
    excluded = {u.obligation_id for u in report.undetermined} | {
        u.obligation_id for u in report.unevaluable
    }
    applicable = [o for o in certified if o.id not in excluded]
    met = [o for o in applicable if o.id not in failing and o.id not in unverified]
    determined = len(met) + len(failing)
    expected = round(len(met) / determined * 100)

    assert shown == expected, "the dashboard disagrees with the engine"
    assert determined < len(applicable), "the unknowns are still in the denominator"


@requires_corpus
def test_the_dashboard_names_what_it_computed_from(client, tmp_path):
    client.post("/w/demo/company/save", data={"name": "X"}, follow_redirects=True)
    _declare_a_framework(client)
    _upload_real_evidence(client, tmp_path)
    client.post("/w/demo/assess", follow_redirects=True)

    page = _plain(client.get("/w/demo/company").text)

    assert "What these figures were computed from" in page
    assert "reviewed evidence" in page


@requires_corpus
def test_undetermined_duties_are_excluded_rather_than_counted_as_met(client, tmp_path):
    """Otherwise the score improves the less the system understands."""
    client.post("/w/demo/company/save", data={"name": "X"}, follow_redirects=True)
    _declare_a_framework(client)
    _upload_real_evidence(client, tmp_path)
    client.post("/w/demo/assess", follow_redirects=True)

    page = _plain(client.get("/w/demo/company").text)
    applicable = int(re.search(r"(\d+) Applicable duties", page).group(1))
    needs = int(re.search(r"(\d+) Need a person", page).group(1))
    certified = int(re.search(r"Of (\d+) signed rules", page).group(1))

    assert needs > 0, "the corpus has event driven duties"
    assert applicable + needs <= certified, (
        "a duty cannot be both applicable and undetermined"
    )


@requires_corpus
def test_the_navigation_is_the_lifecycle_in_order(client):
    """Six stages, numbered, in the order the work happens.

    They live in a row of their own beneath the masthead. Sharing that bar with
    the brand, the document switcher, the Advanced menu and the account put ten
    items on one line, and the last two printed through each other.
    """
    _finish_setup(client)
    body = client.get("/w/demo/company").text
    strip = _stagebar(body)

    stages = ["Overview", "Evidence", "Assessment", "Remediation", "Audit"]
    positions = [strip.index(s) for s in stages]
    assert positions == sorted(positions), f"stages are out of order: {stages}"

    # Numbered, so the row reads as a sequence rather than six equal words.
    for step in "12345":
        assert f'class="lifestep-n" aria-hidden="true">{step}<' in strip, step

    # The journey is the only thing in that row.
    assert "Advanced" not in strip, "the Advanced menu is back on the journey row"


@requires_corpus
def test_the_masthead_does_not_carry_the_journey(client):
    """The overflow that printed one label through another must not come back."""
    _finish_setup(client)
    body = client.get("/w/demo/company").text
    masthead = body[body.index('class="masthead"') : body.index("</header>")]

    for stage in ("Overview", "Remediation", "Regulatory changes"):
        assert stage not in masthead, f"{stage} is back in the masthead"
    assert "Advanced" in masthead, "the Advanced menu should stay in the masthead"


@requires_corpus
def test_certification_is_not_a_stage_of_the_firms_journey(client):
    """Somebody signs the rulebook before a firm is ever measured against it.

    A firm reading its own compliance result does not need to know that screen
    exists, so it is reachable but never on the path.
    """
    _finish_setup(client)
    body = client.get("/w/demo/company").text
    strip = _stagebar(body)

    for engine_screen in ("/queue", "/coverage", "/conflicts", "/processes",
                          "/forecast", "/simulate", "/divergence", "/load"):
        assert engine_screen not in strip, f"{engine_screen} is on the firm's path"
        assert engine_screen in body, f"{engine_screen} became unreachable"


@requires_corpus
def test_the_navigation_speaks_the_users_language_not_the_codebase(client):
    """A compliance officer should not need the internal vocabulary.

    The technical names stay in the code, where they are precise and useful.
    They should not be what somebody has to learn to find out whether their
    firm is complying.
    """
    _finish_setup(client)
    body = client.get("/w/demo/company").text
    # Both places somebody navigates from: the journey row and the menu that
    # holds everything else.
    nav = _stagebar(body) + body[body.index('class="masthead"') : body.index("</header>")]

    for jargon in ("Obligation IR", "Clause Tree", "Evidence Store", "Workspace"):
        assert jargon not in nav, f"the navigation exposes {jargon!r}"

    for plain in ("Evidence", "Audit", "Processes and controls"):
        assert plain in nav


@requires_corpus
def test_every_stage_says_where_it_sits_in_the_journey(client):
    """Six numbered words in the masthead mean nothing on their own.

    Each stage screen names its own position, so somebody who arrives on one
    of them by a link, rather than by walking the path, still knows what came
    before it and what comes after.
    """
    # The overview only reaches stage one once the firm is set up; before that
    # it is the setup path, which counts its own steps.
    client.post(
        "/w/demo/company/save", data={"name": "ABC Securities"}, follow_redirects=True
    )
    _declare_a_framework(client)

    stages = {
        "/w/demo/company": "Stage 1 of 5",
        "/w/demo/review": "Stage 2 of 5",
        "/w/demo/gaps": "Stage 3 of 5",
        "/w/demo/remediation": "Stage 4 of 5",
        "/w/demo/audit": "Stage 5 of 5",
    }
    for path, marker in stages.items():
        page = _plain(client.get(path).text)
        assert marker in page, f"{path} does not say where it sits"


@requires_corpus
def test_an_engine_screen_claims_no_stage(client):
    """Certification and the analytical screens are not steps a firm walks.

    If they carried stage numbers the journey would read as fourteen stages
    long, which is the impression the restructure exists to remove.
    """
    for path in ("/w/demo/queue", "/w/demo/coverage", "/w/demo/processes",
                 "/w/demo/forecast", "/w/demo/conflicts"):
        page = _plain(client.get(path).text)
        assert "of 5" not in page, f"{path} presents itself as a stage"
