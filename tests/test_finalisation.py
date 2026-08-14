"""The properties the product is being frozen on.

Not new behaviour. These assert the guarantees that the whole design rests on,
in the strongest form available: by driving the real routes and looking at what
actually changed on disk, rather than by reading the source and believing it.

* Reading never writes. An assessment is an act with a record, so no page view
  may create one, and no page view may quietly change a firm's data.
* One source of truth. What a screen shows and what the recorded run says are
  the same numbers, because they come from the same engine on the same inputs.
* No number is presented as more than it covers.
"""

from __future__ import annotations

import re

import pytest

from tests.conftest import complete_setup, requires_corpus, sign_in


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
    # And a firm that has finished onboarding. The routes now hold the same
    # order the screens walk a visitor through, so a POST to /assess before
    # setup is complete is refused rather than silently accepted.
    complete_setup(client)
    return client


def _plain(html: str) -> str:
    body = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


#: Every screen a person can reach by following links, plus the two global ones.
GET_ROUTES = [
    "/",
    "/documents",
    "/signin",
    "/w/demo",
    "/w/demo/company",
    "/w/demo/review",
    "/w/demo/gaps",
    "/w/demo/remediation",
    "/w/demo/diff",
    "/w/demo/audit",
    "/w/demo/queue",
    "/w/demo/coverage",
    "/w/demo/processes",
    "/w/demo/forecast",
    "/w/demo/simulate",
    "/w/demo/conflicts",
    "/w/demo/divergence",
    "/w/demo/load",
    "/supervisor",
    "/facts",
]


def _set_up(client, tmp_path):
    from sanhita.cli_compile import _load_registry
    from sanhita.ir.enums import RuleStatus

    client.post(
        "/w/demo/company/save",
        data={"name": "ABC Securities", "intermediary": "STOCK_BROKER"},
        follow_redirects=True,
    )
    client.post(
        "/w/demo/company/frameworks", data={"framework": "demo"}, follow_redirects=True
    )
    rule = next(
        r
        for r in _load_registry(tmp_path / "rules.json").all_current()
        if r.status is RuleStatus.CERTIFIED
    )
    client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "register.csv"},
        content=(
            "obligation_id,entity,occurred_on,filed_on,artifact_type,reference\n"
            f"{rule.id},ABC Securities,2026-03-31,2026-04-02,report,RET-001\n"
        ).encode(),
    )


def _snapshot(tmp_path) -> dict[str, bytes]:
    """Every file the firm's data lives in, as bytes."""
    return {
        p.name: p.read_bytes()
        for p in sorted(tmp_path.glob("*.json"))
        if p.name != "rules.json"
    }


# ------------------------------------------------------- reading never writes


@requires_corpus
def test_no_page_view_changes_anything(client, tmp_path):
    """Asserted against the bytes on disk, not against a reading of the code.

    A GET that writes is how an audit trail acquires entries nobody made, and
    it is how the assessment history used to depend on the order somebody
    happened to click in.
    """
    _set_up(client, tmp_path)
    client.post("/w/demo/assess", follow_redirects=True)

    before = _snapshot(tmp_path)
    assert before, "the fixture wrote nothing, so this test proves nothing"

    for path in GET_ROUTES:
        assert client.get(path).status_code == 200, path

    after = _snapshot(tmp_path)
    assert after == before, (
        "a page view changed the firm's data: "
        f"{sorted(k for k in after if after.get(k) != before.get(k))}"
    )


@requires_corpus
def test_no_page_view_creates_an_assessment(client, tmp_path):
    """The one that matters most, so it is asserted on its own."""
    from sanhita.assess import AssessmentLog

    _set_up(client, tmp_path)
    for path in GET_ROUTES:
        client.get(path)

    assert not (tmp_path / "assessments.json").is_file()
    assert len(AssessmentLog.load(tmp_path / "assessments.json")) == 0


@requires_corpus
def test_every_reachable_screen_answers(client, tmp_path):
    """Including once the firm has walked the whole journey, not only empty."""
    _set_up(client, tmp_path)
    client.post("/w/demo/assess", follow_redirects=True)

    for path in GET_ROUTES:
        response = client.get(path)
        assert response.status_code == 200, f"{path} -> {response.status_code}"
        assert "masthead" in response.text or path == "/", path


# ------------------------------------------------------ one source of truth


@requires_corpus
def test_the_screen_and_the_record_say_the_same_thing(client, tmp_path):
    """They must agree, because they are the same engine on the same inputs.

    The screen recomputes rather than rendering stored numbers, on purpose: a
    screen that trusts a stored figure is a screen that will one day disagree
    with the engine. The guarantee is therefore not "the screen reads the
    record" but "they cannot differ", and that is what this asserts.
    """
    from sanhita.assess import AssessmentLog

    _set_up(client, tmp_path)
    client.post("/w/demo/assess", follow_redirects=True)
    run = AssessmentLog.load(tmp_path / "assessments.json").latest

    page = _plain(client.get("/w/demo/gaps").text)
    shown_breaches = int(re.search(r"(\d+) Confirmed gaps", page).group(1))
    shown_unverified = int(re.search(r"(\d+) Not verifiable", page).group(1))
    shown_met = int(re.search(r"(\d+) Met on time", page).group(1))

    assert shown_breaches == run.breaches
    assert shown_met == run.satisfied
    # And the two are kept apart on the screen exactly as they are in the
    # record, which is the whole reason the distinction exists.
    assert shown_unverified == run.no_evidence
    assert run.short_id in page, "the screen does not name the run it corresponds to"


@requires_corpus
def test_the_recorded_run_is_reproducible_from_its_own_hashes(client, tmp_path):
    """Feed the same two inputs to the same engine and get the same counts."""
    import datetime as _dt

    from sanhita.assess import (
        AssessmentLog,
        evidence_fingerprint,
        rulebook_fingerprint,
    )
    from sanhita.cli_compile import _load_registry
    from sanhita.execute import WEEKENDS_ONLY, EvidenceStore, RuleEngine

    _set_up(client, tmp_path)
    client.post("/w/demo/assess", follow_redirects=True)
    run = AssessmentLog.load(tmp_path / "assessments.json").latest

    rules = _load_registry(tmp_path / "rules.json").all_current()
    evidence = EvidenceStore.load(tmp_path / "evidence.json")

    assert rulebook_fingerprint(rules) == run.rulebook_sha256
    assert evidence_fingerprint(evidence) == run.evidence_sha256

    again = RuleEngine(WEEKENDS_ONLY).run(rules, evidence, as_of=_dt.date.today())
    assert again.breaches == run.breaches
    assert again.satisfied == run.satisfied
    assert again.rules_evaluated == run.rules_evaluated


# ------------------------------------------------- no number oversells itself


@requires_corpus
def test_no_figure_is_called_overall_company_compliance(client, tmp_path):
    """A firm may hold several rulebooks. One rulebook's figure is not the firm."""
    _set_up(client, tmp_path)
    client.post("/w/demo/setup/complete", follow_redirects=True)
    client.post("/w/demo/assess", follow_redirects=True)
    page = _plain(client.get("/w/demo/company").text)

    for overstated in (
        "Overall compliance",
        "Overall company compliance",
        "Total compliance",
    ):
        assert overstated.lower() not in page.lower(), f"the page claims {overstated!r}"

    assert "Of what could be determined" in page
    assert "declared, 1 assessed here" in page


@requires_corpus
def test_the_product_names_the_person_who_arrives(client):
    """The masthead used to name the analyst's job, not the visitor's."""
    body = client.get("/w/demo/company").text
    masthead = body[body.index('class="masthead"') : body.index("</header>")]

    assert "SEBI Compliance" in masthead
    assert "Certification Workbench" not in masthead
    # And the work itself is still reachable, under Advanced, where the
    # regulatory analyst rather than the firm goes. The group is now called
    # "Regulation"; the screen inside it is still the certification queue.
    assert "Requirements to approve" in body


# ------------------------------------------------- the seven rulings, staged


def test_the_gold_set_was_settled_by_somebody_who_is_not_the_extractor():
    """The provenance of every accuracy figure this product publishes.

    This used to assert the seven rulings were still blank, which was the right
    guard while they were open. They are settled now, so what has to hold is
    the property rather than the state: all seven clauses are still there, all
    seven are answered, and a name is attached. Whose name it is cannot be
    checked by a test; that it is not empty can.

    ``tests/test_gold_set_readiness.py`` holds the harder half, which is that
    no ruling quietly moves in the extractor's favour.
    """
    import pathlib

    from sanhita.eval.rulings import read_rulings

    path = pathlib.Path(__file__).resolve().parent.parent / "GOLD-SET-RULINGS.yaml"
    assert path.is_file(), "the rulings file is missing"
    status = read_rulings(path)

    assert status.total == 7, "there should be exactly seven rulings"
    for clause in ("45.1", "57.47", "19.5.2.3", "19.5.5.5", "54.4.2", "73.2.2", "7.2.3"):
        assert any(r.clause_id == clause for r in status.rulings), f"{clause} is gone"
    assert status.complete, status.describe()
    assert status.signed_off_by.strip()


# ------------------------------------------------------------- deployment


def test_the_image_keeps_a_copy_the_host_disk_cannot_shadow():
    """A persisted store must not hide the rulebook it exists to persist.

    `render.yaml` mounts a disk at `/app/.sanhita`, which is exactly where the
    compiled rulebook is baked. A fresh disk is empty, so the mount covered the
    183 certifications and the deployed site reported `"rules": 0` while its
    health check still passed. Verified by running the image with an empty
    mount there before this was fixed.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = root / "docker-entrypoint.sh"

    # The property, not one spelling of it. This asserted a literal
    # `COPY .sanhita/ ./.sanhita-dist/` until the pristine copy started being
    # taken after the demonstration state is built rather than before, so that
    # a restored store is a working demonstration instead of a bare rulebook.
    assert ".sanhita-dist" in dockerfile, (
        "the image keeps no copy outside the mount point"
    )
    assert "/app/.sanhita-dist/" in dockerfile
    # And it is taken after the seeding, or restoring would undo it.
    assert dockerfile.index("demo-seed") < dockerfile.index(
        "cp -R /app/.sanhita/. /app/.sanhita-dist/"
    ), "the pristine copy is taken before the demonstration is built into it"
    assert entrypoint.is_file()
    assert 'CMD ["/app/docker-entrypoint.sh"]' in dockerfile

    script = entrypoint.read_text(encoding="utf-8")
    # Seeds only when the live store has no rulebook, so an existing store with
    # real certifications and a firm's evidence is never overwritten.
    assert "! -f \"$LIVE/rules.json\"" in script
    assert "exec sanhita serve" in script


def test_the_deployment_configs_agree_on_what_is_persisted():
    """Two hosts, one product. A difference here is a difference in behaviour."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    render = (root / "render.yaml").read_text(encoding="utf-8")
    fly = (root / "fly.toml").read_text(encoding="utf-8")

    # Both must scope a firm's data per visitor, because both are public URLs.
    assert "SANHITA_SHARED" in render
    assert "SANHITA_SHARED" in fly
    # And both must set a signing key rather than inheriting the image default.
    assert "SANHITA_SIGNING_KEY" in render
    assert "change-me-in-the-host-secrets" not in render
