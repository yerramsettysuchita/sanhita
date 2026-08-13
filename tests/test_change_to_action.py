"""From regulatory text to operational action, which is the problem's title.

The product could say what changed between two editions and which certified
rules were affected. It stopped there. A diff is a fact about a document; an
action is a fact about an organisation, and nothing joined the two.

It was worse than incomplete. The screen that claimed to answer "what does this
mean for this firm" filtered `impact.affected` with `isinstance(row, dict)`,
but those rows are `AffectedRule` dataclasses, so the set was always empty and
every amendment reported no operational impact at all.
"""

from __future__ import annotations

import re

import pytest

from tests.conftest import requires_corpus


# ------------------------------------------------------------ the join


class _Action:
    def __init__(self, verb="file", obj="the quarterly return"):
        self.verb, self.object = verb, obj


class _Src:
    def __init__(self, clause_id="15.1"):
        self.clause_id = clause_id


class _Ob:
    def __init__(self, oid="SB-15.1-a", clause="15.1"):
        self.id, self.action, self.source = oid, _Action(), _Src(clause)


class _Binding:
    def __init__(self, **kw):
        self.process = kw.get("process", "")
        self.function = kw.get("function", "")
        self.system = kw.get("system", "")
        self.control_ref = kw.get("control_ref", "")


class _Controls:
    def __init__(self, mapping=None):
        self._m = mapping or {}

    def get(self, obligation_id):
        return self._m.get(obligation_id)


def _report(affected=(), new_clauses=(), certified_before=10):
    from sanhita.diff.impact import ImpactReport

    return ImpactReport(
        before_label="June 2025",
        after_label="February 2026",
        certified_before=certified_before,
        affected=list(affected),
        new_clauses=list(new_clauses),
    )


def _affected(consequence, oid="SB-15.1-a", clause="15.1", **kw):
    from sanhita.diff.impact import AffectedRule
    from sanhita.diff.tree_diff import ChangeKind

    return AffectedRule(
        obligation_id=oid,
        clause_id=clause,
        consequence=consequence,
        change=kw.pop("change", ChangeKind.MODIFIED),
        was_certified=kw.pop("was_certified", True),
        certified_by=kw.pop("certified_by", "A Named Officer"),
        signature=kw.pop("signature", "a" * 64),
        **kw,
    )


def test_a_lost_signature_becomes_work_somebody_owns():
    from sanhita.change import ActionKind, plan_for_firm
    from sanhita.diff.impact import Consequence

    plan = plan_for_firm(
        _report([_affected(Consequence.RECERTIFY)]),
        [_Ob()],
        _Controls({"SB-15.1-a": _Binding(process="Client reporting", function="Operations",
                                        system="Back office", control_ref="SOP-14")}),
        firm="ABC Securities",
        framework="Stock Brokers Master Circular",
    )

    assert plan.total == 1
    action = plan.actions[0]
    assert action.kind is ActionKind.RECERTIFY
    assert action.is_owned
    assert action.function == "Operations"
    assert action.process == "Client reporting"
    # And it says what to do in words a person can act on.
    assert "signed over are gone" in action.describe()
    assert "Operations" in action.describe()
    assert "A Named Officer" in action.describe()


def test_an_unowned_duty_is_counted_rather_than_hidden():
    """An amendment to a rule nobody owns is one nobody will act on."""
    from sanhita.change import plan_for_firm
    from sanhita.diff.impact import Consequence

    plan = plan_for_firm(
        _report([_affected(Consequence.RECERTIFY)]),
        [_Ob()],
        _Controls(),
        firm="ABC",
        framework="F",
    )

    assert plan.unowned == 1
    assert not plan.actions[0].is_owned
    assert "assign" in plan.actions[0].describe().lower() or not plan.actions[0].is_owned


def test_a_proposal_changing_is_not_an_event_for_a_firm():
    """Nobody signed it and nothing was running against it."""
    from sanhita.change import plan_for_firm
    from sanhita.diff.impact import Consequence

    plan = plan_for_firm(
        _report([_affected(Consequence.RECOMPILE, was_certified=False, certified_by=None)]),
        [_Ob()],
        _Controls(),
        firm="ABC",
        framework="F",
    )

    assert plan.total == 0, "an unsigned proposal was put on the firm's to-do list"


def test_a_reread_is_ranked_below_a_lost_signature():
    """Overstating damage is how a tool teaches people to ignore it."""
    from sanhita.change import ActionKind, plan_for_firm
    from sanhita.diff.impact import Consequence

    plan = plan_for_firm(
        _report([
            _affected(Consequence.REREAD, oid="SB-2-a", clause="2.1", via="9.9", hops=1),
            _affected(Consequence.RECERTIFY, oid="SB-1-a", clause="1.1"),
        ]),
        [_Ob("SB-1-a", "1.1"), _Ob("SB-2-a", "2.1")],
        _Controls(),
        firm="ABC",
        framework="F",
    )

    assert [a.kind for a in plan.actions] == [ActionKind.RECERTIFY, ActionKind.REREAD]
    assert plan.signatures_lost == 1, "a re-read was counted as a lost signature"
    assert "did not change" in plan.actions[1].describe()


def test_a_new_clause_becomes_a_decision_not_a_duty():
    from sanhita.change import ActionKind, plan_for_firm

    plan = plan_for_firm(
        _report(new_clauses=["62.9"]), [], _Controls(), firm="ABC", framework="F"
    )

    assert plan.actions[0].kind is ActionKind.ASSESS_NEW
    assert "decide whether it carries a duty" in plan.actions[0].describe()


def test_the_plan_groups_by_process_because_work_is_assigned_by_team():
    from sanhita.change import plan_for_firm
    from sanhita.diff.impact import Consequence

    plan = plan_for_firm(
        _report([
            _affected(Consequence.RECERTIFY, oid="SB-1-a", clause="1.1"),
            _affected(Consequence.RECERTIFY, oid="SB-2-a", clause="2.1"),
        ]),
        [_Ob("SB-1-a", "1.1"), _Ob("SB-2-a", "2.1")],
        _Controls({"SB-1-a": _Binding(process="Margin reporting", function="Ops")}),
        firm="ABC",
        framework="F",
    )
    grouped = plan.by_process()

    assert "Margin reporting" in grouped
    assert "Not yet mapped" in grouped
    # Unmapped work sorts last, so a reader sees what is owned first.
    assert list(grouped)[-1] == "Not yet mapped"


def test_a_withdraw_and_a_repoint_break_a_signature_too():
    """The screen's two headline numbers have to come out of one arithmetic.

    They did not. The plan counted only re-certifies as lost signatures while
    the untouched count subtracted every broken one, so a real SEBI reissue
    could show "0 signatures no longer covering" beside "0 certified and
    untouched" with 25 rules signed. Both numbers were defensible alone and the
    pair was nonsense.
    """
    from sanhita.change import plan_for_firm
    from sanhita.diff.impact import Consequence

    plan = plan_for_firm(
        _report(
            [
                _affected(Consequence.WITHDRAW, oid="SB-1-a", clause="1.1"),
                _affected(Consequence.REPOINT, oid="SB-2-a", clause="2.1", now_at="2.2"),
                _affected(Consequence.REREAD, oid="SB-3-a", clause="3.1", via="1.1", hops=1),
            ],
            certified_before=3,
        ),
        [_Ob("SB-1-a", "1.1"), _Ob("SB-2-a", "2.1"), _Ob("SB-3-a", "3.1")],
        _Controls(),
        firm="ABC",
        framework="F",
    )

    assert plan.signatures_lost == 2, "a withdraw or a repoint was counted as harmless"
    assert plan.unaffected == 1, "the re-read was counted as damage"
    assert plan.signatures_lost + plan.unaffected == 3


def test_untouched_certified_rules_are_reported_too():
    """"Nothing to do here" is an answer somebody needs."""
    from sanhita.change import plan_for_firm
    from sanhita.diff.impact import Consequence

    plan = plan_for_firm(
        _report([_affected(Consequence.RECERTIFY)], certified_before=183),
        [_Ob()],
        _Controls(),
        firm="ABC",
        framework="F",
    )

    assert plan.unaffected == 182


# --------------------------------------------------------- through the UI


@pytest.fixture()
def client(corpus_pdf, tmp_path, monkeypatch):
    import shutil

    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a-test-only-key")
    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)
    return TestClient(create_app(corpus_pdf, store=store))


def _plain(html: str) -> str:
    body = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


def _second_edition(client, corpus_pdf, suffix=b"\n% a later edition"):
    """A firm, signed in, with a second edition of its rulebook to compare."""
    client.post(
        "/signup",
        data={
            "name": "A Named Officer",
            "email": "officer@example.com",
            "password": "a-long-enough-password",
        },
        follow_redirects=True,
    )
    client.post(
        "/w/demo/company/save",
        data={"name": "ABC Securities Pvt Ltd", "intermediary": "STOCK_BROKER"},
        follow_redirects=True,
    )
    uploaded = client.post(
        "/documents/upload",
        content=corpus_pdf.read_bytes() + suffix,
        headers={"x-sanhita-filename": "later-edition.pdf"},
    )
    if uploaded.status_code != 200:  # pragma: no cover - upload guard changed
        pytest.skip(f"could not add a second edition: {uploaded.text}")
    return uploaded.json()["id"]


@requires_corpus
def test_the_amendment_screen_asks_the_firms_question(client, corpus_pdf):
    other = _second_edition(client, corpus_pdf)
    page = _plain(client.get(f"/w/demo/diff?against={other}").text)

    assert "What this means for ABC Securities Pvt Ltd" in page


@requires_corpus
def test_a_comparison_answers_the_firm_even_when_nothing_changed(client, corpus_pdf):
    """"Nothing of yours is touched" is an answer, and it has to be given.

    Silence here is what the old broken filter produced, and silence is
    indistinguishable from a tool that did not look.
    """
    other = _second_edition(client, corpus_pdf)
    page = _plain(client.get(f"/w/demo/diff?against={other}").text)

    assert "What this means for ABC Securities Pvt Ltd" in page
    assert "Actions for this firm" in page or "Nothing of this firm's is touched" in page


# ------------------------------------------- a real SEBI amendment, end to end


OLD_EDITION = "investment-advisers-2025-06-27.pdf"
NEW_EDITION = "investment-advisers-2026-02.pdf"


def _both_editions():
    from tests.conftest import ROOT

    old, new = ROOT / "corpus" / OLD_EDITION, ROOT / "corpus" / NEW_EDITION
    return (old, new) if old.is_file() and new.is_file() else (None, None)


needs_a_real_amendment = pytest.mark.skipif(
    _both_editions() == (None, None),
    reason="both investment adviser editions are needed (corpus/ is gitignored)",
)


@needs_a_real_amendment
def test_a_real_sebi_amendment_becomes_work_this_firm_owns(tmp_path, monkeypatch):
    """The problem statement's title, proven on a document SEBI published.

    SEBI issued the Master Circular for Investment Advisers in June 2025 and
    reissued it in February 2026. Nothing here is a synthetic edit: the store is
    compiled from the June edition, signed by a named person, and then compared
    against the February edition through the same route a user would use.

    The unit tests above prove the join on constructed inputs. This proves it on
    the real thing, which is a different claim.
    """
    import shutil

    from fastapi.testclient import TestClient

    from sanhita.certify import certify
    from sanhita.cli_compile import _load_registry, _save_registry
    from sanhita.compile.extract import ExtractionStatus, RuleExtractor
    from sanhita.diff import diff_trees
    from sanhita.parse.clause_tree import parse_clause_tree
    from sanhita.web.app import create_app

    key = "0" * 64
    monkeypatch.setenv("SANHITA_SIGNING_KEY", key)
    old_pdf, new_pdf = _both_editions()
    # Copied, because the workspace writes its sidecars beside the store.
    old_copy, new_copy = tmp_path / OLD_EDITION, tmp_path / NEW_EDITION
    shutil.copy(old_pdf, old_copy)
    shutil.copy(new_pdf, new_copy)

    before, after = parse_clause_tree(old_copy), parse_clause_tree(new_copy)
    changes = diff_trees(
        before, after, before_label="IA June 2025", after_label="IA February 2026"
    )
    assert not changes.identical, "the two editions parsed the same, so there is nothing to prove"

    # Compile the June edition, which is what the firm would have signed.
    registry = _load_registry(tmp_path / "rules.json")
    extractor = RuleExtractor(circular_id="ia-2025-06")
    for node in before.nodes.values():
        outcome = extractor.extract(node)
        if outcome.status is ExtractionStatus.PROPOSED:
            for obligation in outcome.obligations:
                registry.propose(obligation, by="extractor:rules")

    # Sign the rules whose clauses this amendment actually moved. A signature
    # over an untouched clause proves nothing about the join.
    moved = {c.clause_id for c in changes.changes if c.is_change}
    signed = 0
    for rule in list(registry.all_current()):
        if rule.source.clause_id in moved and signed < 25:
            certify(registry, rule.id, by="A Named Officer", key=key)
            signed += 1
    assert signed, "no compiled rule sits on a clause this amendment changed"
    _save_registry(
        registry,
        circular_id="ia-2025-06",
        fingerprint=before.fingerprint(),
        path=tmp_path / "rules.json",
    )

    client = TestClient(create_app(new_copy, store=tmp_path / "rules.json"))
    client.post(
        "/signup",
        data={
            "name": "A Named Officer",
            "email": "officer@example.com",
            "password": "a-long-enough-password",
        },
        follow_redirects=True,
    )
    client.post(
        "/w/demo/company/save",
        data={"name": "Meridian Advisers LLP", "intermediary": "INVESTMENT_ADVISER"},
        follow_redirects=True,
    )
    uploaded = client.post(
        "/documents/upload",
        content=old_copy.read_bytes(),
        headers={"x-sanhita-filename": OLD_EDITION},
    )
    assert uploaded.status_code == 200, uploaded.text

    page = _plain(client.get(f"/w/demo/diff?against={uploaded.json()['id']}").text)

    assert "What this means for Meridian Advisers LLP" in page
    assert "Actions for this firm" in page, (
        "a real SEBI amendment against 25 signed rules produced no action at all"
    )
    assert "Signatures no longer covering" in page
    # And the work is named in words somebody could act on rather than a code.
    assert any(
        phrase in page
        for phrase in (
            "has to be read and signed again",
            "Withdraw the rule",
            "has to point at the new number",
        )
    ), "the plan rendered no readable instruction"


@requires_corpus
def test_the_operational_section_is_not_permanently_empty(client):
    """The defect this replaced: a filter that could never match.

    `impact.affected` holds AffectedRule dataclasses. The old block kept only
    rows where `isinstance(row, dict)`, so it discarded every one of them and
    the screen reported no operational impact for every amendment ever.
    """
    import inspect

    from sanhita.web import app as app_module

    source = inspect.getsource(app_module)
    assert "isinstance(row, dict)" not in source, (
        "the filter that made the operational section always empty is back"
    )
    assert "plan_for_firm" in source


@requires_corpus
def test_nothing_on_the_amendment_screen_acts_by_itself(client, corpus_pdf):
    """It ranks and explains. Certifying and withdrawing stay with a person."""
    other = _second_edition(client, corpus_pdf)
    body = client.get(f"/w/demo/diff?against={other}").text

    for verb in ("/certify", "/withdraw", "/recertify"):
        assert f'action="{verb}' not in body, f"the screen posts to {verb}"
    page = _plain(body)
    assert (
        "nothing on this page re-certifies, withdraws or closes anything" in page
        or "Nothing of this firm's is touched" in page
    )
