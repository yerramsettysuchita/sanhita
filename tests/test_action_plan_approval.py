"""The agentic layer, and the precise limit on what it decides.

The problem statement is called *Agentic Compliance*, and the honest reading is
the hard part. A system that lets a model decide whether a firm complies is not
agentic, it is unaccountable.

So the agency is in the coordination and never in the judgement. Detecting,
comparing, gathering the affected processes and recommending what to do are all
arithmetic over hashes and control bindings. Then a named person approves, and
only then does anything exist.

These tests hold that boundary from both sides: nothing is created without an
approval, and approving creates everything the plan named.
"""

from __future__ import annotations

import datetime as _dt
import re

import pytest

from tests.conftest import requires_corpus, sign_in


# ------------------------------------------------------------ the fixtures


class _Kind:
    def __init__(self, value):
        self.value = value

    @property
    def label(self):
        return self.value.title().replace("_", " ")


class _Action:
    def __init__(self, kind="RECERTIFY", clause="15.1", oid="SB-15.1-a",
                 process="", control_ref="", function=""):
        self.kind = _Kind(kind)
        self.clause_id, self.obligation_id = clause, oid
        self.process, self.control_ref, self.function = process, control_ref, function
        self.now_at = None

    def describe(self):
        return f"{self.kind.value} clause {self.clause_id}."


class _ChangePlan:
    def __init__(self, actions, signatures_lost=0, unowned=0):
        self.actions = actions
        self.before_label, self.after_label = "June 2025", "February 2026"
        self.signatures_lost, self.unowned = signatures_lost, unowned

    @property
    def total(self):
        return len(self.actions)


def _plan(actions=None, **kw):
    from sanhita.orchestrate import plan_from_change

    return plan_from_change(
        _ChangePlan(actions if actions is not None else [_Action()], **kw),
        firm="ABC Securities",
        framework="Stock Brokers Master Circular",
        before_fingerprint="f" * 64,
        after_fingerprint="a" * 64,
    )


# ---------------------------------------------------- what the plan counts


def test_a_plan_counts_the_firms_side_of_the_amendment():
    plan = _plan(
        [
            _Action(clause="1.1", oid="SB-1.1-a", process="Margin reporting",
                    control_ref="SOP-14", function="Operations"),
            _Action(clause="2.1", oid="SB-2.1-a", process="Margin reporting",
                    control_ref="SOP-15", function="Operations"),
            _Action(clause="3.1", oid="SB-3.1-a"),
        ],
        unowned=1,
    )

    assert plan.actions_recommended == 3
    assert plan.obligations_affected == 3
    assert plan.processes_affected == 1, "one process, touched twice, is one process"
    assert plan.controls_affected == 2
    assert plan.unowned == 1


def test_the_same_amendment_for_the_same_firm_is_the_same_plan():
    """Two approvals of one amendment is two sets of tasks and nobody sure
    which is live."""
    assert _plan().id == _plan().id


def test_a_different_amendment_is_a_different_plan():
    from sanhita.orchestrate import plan_from_change

    other = plan_from_change(
        _ChangePlan([_Action()]),
        firm="ABC Securities",
        framework="F",
        before_fingerprint="f" * 64,
        after_fingerprint="b" * 64,
    )
    assert other.id != _plan().id


def test_a_different_firm_is_a_different_plan():
    from sanhita.orchestrate import plan_from_change

    other = plan_from_change(
        _ChangePlan([_Action()]),
        firm="Zeta Broking",
        framework="F",
        before_fingerprint="f" * 64,
        after_fingerprint="a" * 64,
    )
    assert other.id != _plan().id


def test_an_unapproved_plan_says_nothing_has_been_created():
    plan = _plan()

    assert plan.is_open
    assert "Nothing has been created" in plan.headline()


# --------------------------------------------------------- the boundary


def test_approving_records_who_did_it(tmp_path):
    from sanhita.orchestrate import PlanStatus, PlanStore

    store = PlanStore(path=tmp_path / "plans.json")
    store.approve(_plan(), by="R. Nair", task_ids=["REM-1", "REM-2"])

    stored = store.all()[0]
    assert stored.status is PlanStatus.APPROVED
    assert stored.approved_by == "R. Nair"
    assert stored.approved_at is not None
    assert stored.task_ids == ["REM-1", "REM-2"]
    assert "R. Nair approved this" in stored.headline()


def test_an_approval_without_a_name_is_refused(tmp_path):
    from sanhita.orchestrate import PlanStore

    store = PlanStore(path=tmp_path / "plans.json")
    with pytest.raises(ValueError, match="named person"):
        store.approve(_plan(), by="   ")


def test_declining_is_recorded_rather_than_left_as_silence(tmp_path):
    """"We looked and it does not touch us" is a defensible position and an
    answerable one. Silence is neither."""
    from sanhita.orchestrate import PlanStatus, PlanStore

    store = PlanStore(path=tmp_path / "plans.json")
    store.decline(_plan(), by="R. Nair", note="no adviser business")

    stored = store.all()[0]
    assert stored.status is PlanStatus.DECLINED
    assert "takes no action on it" in stored.headline()
    assert stored.note == "no adviser business"


def test_a_decided_plan_is_not_decided_twice(tmp_path):
    from sanhita.orchestrate import PlanStatus, PlanStore

    store = PlanStore(path=tmp_path / "plans.json")
    store.approve(_plan(), by="R. Nair", task_ids=["REM-1"])
    store.approve(_plan(), by="Somebody Else", task_ids=["REM-2", "REM-3"])

    stored = store.all()[0]
    assert stored.approved_by == "R. Nair"
    assert stored.task_ids == ["REM-1"]
    assert stored.status is PlanStatus.APPROVED


def test_the_decision_survives_a_round_trip(tmp_path):
    from sanhita.orchestrate import PlanStatus, PlanStore

    store = PlanStore(path=tmp_path / "plans.json")
    store.approve(_plan(), by="R. Nair", task_ids=["REM-1"], at=_dt.datetime(
        2026, 8, 13, tzinfo=_dt.timezone.utc
    ))
    store.save()

    reloaded = PlanStore.load(tmp_path / "plans.json").all()[0]
    assert reloaded.status is PlanStatus.APPROVED
    assert reloaded.approved_by == "R. Nair"
    assert reloaded.after_fingerprint == "a" * 64


def test_an_undecided_plan_reads_back_as_the_proposal(tmp_path):
    """Contents are recomputed from the diff; only the decision is stored."""
    from sanhita.orchestrate import PlanStore

    store = PlanStore(path=tmp_path / "plans.json")
    fresh = _plan()

    assert store.decision_on(fresh) is fresh
    assert fresh.is_open


def test_nothing_in_this_module_creates_work_by_itself():
    """The property the whole design turns on, asserted at the source."""
    import inspect

    from sanhita import orchestrate

    source = inspect.getsource(orchestrate)
    assert "open_for_action" not in source, (
        "the orchestrator creates tasks directly, bypassing the approval"
    )
    assert "def approve" in source
    assert "def decline" in source


# ------------------------------------------------------------ through the UI


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


@pytest.fixture()
def client(corpus_pdf, tmp_path, monkeypatch):
    import shutil

    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a" * 64)
    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)
    client = TestClient(create_app(corpus_pdf, store=store))
    sign_in(client, name="R. Nair")
    client.post(
        "/w/demo/company/save",
        data={"name": "ABC Securities Pvt Ltd", "intermediary": "STOCK_BROKER"},
        follow_redirects=True,
    )
    client.post(
        "/w/demo/company/frameworks", data={"framework": "demo"}, follow_redirects=True
    )
    client.post("/w/demo/setup/complete", follow_redirects=True)
    return client


def _plain(html: str) -> str:
    body = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


@requires_corpus
def test_the_screen_asks_the_firms_question_before_offering_the_button(client, corpus_pdf):
    uploaded = client.post(
        "/documents/upload",
        content=corpus_pdf.read_bytes() + b"\n% a later edition",
        headers={"x-sanhita-filename": "later.pdf"},
    )
    assert uploaded.status_code == 200, uploaded.text

    page = _plain(client.get(f"/w/demo/diff?against={uploaded.json()['id']}").text)

    # Identical trees produce no plan, and the screen says so rather than
    # offering an approval of nothing.
    assert "Nothing of this firm's is touched" in page


@needs_a_real_amendment
def test_a_real_amendment_becomes_a_plan_a_person_approves(tmp_path, monkeypatch):
    """The whole agentic loop, on a document SEBI published."""
    import shutil

    from fastapi.testclient import TestClient

    from sanhita.certify import certify
    from sanhita.cli_compile import _load_registry, _save_registry
    from sanhita.compile.extract import ExtractionStatus, RuleExtractor
    from sanhita.diff import diff_trees
    from sanhita.orchestrate import PlanStatus, PlanStore
    from sanhita.parse.clause_tree import parse_clause_tree
    from sanhita.remediate import RemediationStore
    from sanhita.web.app import create_app

    key = "0" * 64
    monkeypatch.setenv("SANHITA_SIGNING_KEY", key)
    old_pdf, new_pdf = _both_editions()
    old_copy, new_copy = tmp_path / OLD_EDITION, tmp_path / NEW_EDITION
    shutil.copy(old_pdf, old_copy)
    shutil.copy(new_pdf, new_copy)

    before, after = parse_clause_tree(old_copy), parse_clause_tree(new_copy)
    changes = diff_trees(before, after, before_label="June", after_label="February")
    registry = _load_registry(tmp_path / "rules.json")
    extractor = RuleExtractor(circular_id="ia-2025-06")
    for node in before.nodes.values():
        outcome = extractor.extract(node)
        if outcome.status is ExtractionStatus.PROPOSED:
            for obligation in outcome.obligations:
                registry.propose(obligation, by="extractor:rules")
    moved = {c.clause_id for c in changes.changes if c.is_change}
    signed = 0
    for rule in list(registry.all_current()):
        if rule.source.clause_id in moved and signed < 10:
            certify(registry, rule.id, by="A Named Officer", key=key)
            signed += 1
    _save_registry(
        registry,
        circular_id="ia-2025-06",
        fingerprint=before.fingerprint(),
        path=tmp_path / "rules.json",
    )

    client = TestClient(create_app(new_copy, store=tmp_path / "rules.json"))
    sign_in(client, name="R. Nair")
    client.post(
        "/w/demo/company/save",
        data={"name": "Meridian Advisers LLP", "intermediary": "INVESTMENT_ADVISER"},
        follow_redirects=True,
    )
    client.post(
        "/w/demo/company/frameworks", data={"framework": "demo"}, follow_redirects=True
    )
    client.post("/w/demo/setup/complete", follow_redirects=True)
    uploaded = client.post(
        "/documents/upload",
        content=old_copy.read_bytes(),
        headers={"x-sanhita-filename": OLD_EDITION},
    )
    assert uploaded.status_code == 200, uploaded.text
    against = uploaded.json()["id"]

    page = _plain(client.get(f"/w/demo/diff?against={against}").text)
    assert "What does this change mean for Meridian Advisers LLP?" in page
    assert "Nothing has been created yet" in page
    assert "Approve and create tasks" in page

    # Nothing exists until somebody approves.
    assert not RemediationStore.load(tmp_path / "remediation.json").tasks

    approved = client.post(
        "/w/demo/change/approve",
        data={"against": against, "decision": "approve", "note": "reviewed"},
        follow_redirects=False,
    )
    assert approved.status_code == 303, approved.text

    tasks = RemediationStore.load(tmp_path / "remediation.json")
    assert tasks.tasks, "approving created nothing"
    assert all(t.is_from_an_amendment for t in tasks.tasks.values())

    stored = PlanStore.load(tmp_path / "plans.json").all()[0]
    assert stored.status is PlanStatus.APPROVED
    assert stored.approved_by == "R. Nair"
    assert len(stored.task_ids) == len(tasks.tasks)

    page = _plain(client.get(f"/w/demo/diff?against={against}").text)
    assert "Decision of record" in page
    assert "R. Nair approved this" in page


@requires_corpus
def test_an_anonymous_visitor_cannot_approve_a_plan(client, corpus_pdf, tmp_path):
    """Reading the plan is open. Turning it into work is not."""
    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    stranger = TestClient(create_app(corpus_pdf, store=tmp_path / "rules.json"))
    response = stranger.post(
        "/w/demo/change/approve",
        data={"against": "demo", "decision": "approve"},
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert "official compliance action" in response.text


@requires_corpus
def test_a_plan_cannot_be_approved_for_an_undeclared_framework(client, corpus_pdf):
    """A firm is only ever measured against a rulebook it declared."""
    uploaded = client.post(
        "/documents/upload",
        content=corpus_pdf.read_bytes() + b"\n% another circular",
        headers={"x-sanhita-filename": "unrelated.pdf"},
    )
    assert uploaded.status_code == 200, uploaded.text
    other = uploaded.json()["id"]

    response = client.post(
        f"/w/{other}/change/approve",
        data={"against": "demo", "decision": "approve"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "has not declared" in response.text
