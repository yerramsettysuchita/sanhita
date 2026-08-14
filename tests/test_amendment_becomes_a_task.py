"""An amendment becomes work somebody owns, and only a fact closes it.

The diff screen could say what a new edition cost a firm. Saying it is not
doing it: nobody owned a line of it, nothing was dated, and next quarter there
was no way to tell whether any of it had been acted on.

The interesting half is closure. An evidence task closes when the certified
rule is run again and finds no breach. An amendment task has no records to run
against, so it closes on a different fact, and each kind of amendment work has
its own:

    RECERTIFY    a certification exists over the clause's new characters
    REPOINT      the rule's anchor is the new number, signed over it
    WITHDRAW     the rule is no longer live in the store
    REREAD       a person signed the rule again after the task was raised
    ASSESS_NEW   a rule from the new clause reached certified or rejected

None of them can be asserted. There is still no button that marks a task done.
"""

from __future__ import annotations

import datetime as _dt
import re

import pytest

from tests.conftest import requires_corpus

# ------------------------------------------------------------ the fixtures


class _Action:
    def __init__(self, verb="file", obj="the quarterly return"):
        self.verb, self.object = verb, obj


class _Kind:
    """Just enough of ActionKind for the store to write a task."""

    def __init__(self, value):
        self.value = value

    @property
    def label(self):
        return self.value.title().replace("_", " ")


class _Required:
    """A RequiredAction as far as the remediation store is concerned."""

    def __init__(self, kind, clause_id="15.1", obligation_id="SB-15.1-a", now_at=None):
        self.kind = _Kind(kind)
        self.clause_id = clause_id
        self.obligation_id = obligation_id
        self.now_at = now_at
        self.function = "Operations"

    def describe(self):
        return f"{self.kind.value} clause {self.clause_id}."


def _store(tmp_path):
    from sanhita.remediate import RemediationStore

    return RemediationStore(path=tmp_path / "remediation.json")


def _raise(store, action, **kw):
    from sanhita.remediate import open_for_action

    return open_for_action(
        store,
        action,
        company="ABC Securities",
        by="A Named Officer",
        before_fingerprint=kw.pop("before", "f" * 64),
        after_fingerprint=kw.pop("after", "a" * 64),
        **kw,
    )


# ------------------------------------------------------------ raising it


def test_a_required_action_becomes_a_task_somebody_can_own(tmp_path):
    from sanhita.remediate import TaskStatus

    store = _store(tmp_path)
    task = _raise(store, _Required("RECERTIFY"))

    assert task.status is TaskStatus.OPEN
    assert task.is_from_an_amendment
    assert task.change_kind == "RECERTIFY"
    assert task.clause_id == "15.1"
    assert task.assigned_team == "Operations", "the control binding was dropped"
    assert "signed by a named officer" in task.evidence_required
    assert task.amended_from == "f" * 64


def test_raising_the_same_action_twice_is_one_task(tmp_path):
    """Two tasks for one lost signature means two people re-certifying it."""
    store = _store(tmp_path)
    first = _raise(store, _Required("RECERTIFY"))
    second = _raise(store, _Required("RECERTIFY"))

    assert first.task_id == second.task_id
    assert len(store.tasks) == 1


def test_the_same_clause_in_a_later_edition_is_different_work(tmp_path):
    """A signature lost twice is two pieces of work, not one done twice."""
    store = _store(tmp_path)
    first = _raise(store, _Required("RECERTIFY"), after="a" * 64)
    second = _raise(store, _Required("RECERTIFY"), after="b" * 64)

    assert first.task_id != second.task_id
    assert len(store.tasks) == 2


def test_a_lost_signature_is_raised_ahead_of_a_renumbering(tmp_path):
    from sanhita.remediate import Priority

    store = _store(tmp_path)
    lost = _raise(store, _Required("RECERTIFY", clause_id="1.1"))
    clerical = _raise(store, _Required("REPOINT", clause_id="2.1", now_at="2.2"))

    assert lost.priority is Priority.HIGH
    assert clerical.priority is Priority.MEDIUM


def test_raising_one_is_on_the_hash_chained_log(tmp_path):
    store = _store(tmp_path)
    task = _raise(store, _Required("WITHDRAW"))

    intact, problem = store.log.verify()
    assert intact, problem
    created = store.log.for_task(task.task_id)
    assert created and created[0].actor == "A Named Officer"


def test_the_amendment_survives_a_round_trip_to_disk(tmp_path):
    """A re-check months later still has to name which two editions this was."""
    from sanhita.remediate import RemediationStore

    store = _store(tmp_path)
    task = _raise(store, _Required("REPOINT", now_at="15.2"))
    store.save()

    reloaded = RemediationStore.load(tmp_path / "remediation.json").get(task.task_id)
    assert reloaded.change_kind == "REPOINT"
    assert reloaded.change_now_at == "15.2"
    assert reloaded.amended_from == "f" * 64
    assert reloaded.amended_to == "a" * 64


# ------------------------------------------------------------- closing it


class _Cert:
    def __init__(self, by="A Named Officer", at=None):
        self.certified_by = by
        self.certified_at = at or _dt.datetime(2026, 8, 1, tzinfo=_dt.timezone.utc)


class _Anchor:
    def __init__(self, clause_id, sha):
        self.clause_id, self.sha256 = clause_id, sha


class _Rule:
    def __init__(self, oid, clause_id, sha, status, certification=None):
        from sanhita.ir.enums import RuleStatus

        self.id = oid
        self.source = _Anchor(clause_id, sha)
        self.status = RuleStatus(status)
        self.certification = certification


class _Node:
    def __init__(self, sha):
        self.sha256 = sha


class _Tree:
    def __init__(self, mapping):
        self._m = mapping

    def get(self, clause_id):
        return self._m.get(clause_id)


NEW_TEXT = "n" * 64
OLD_TEXT = "o" * 64


def _recheck(store, task_id, rules, tree, by="A Named Officer", at=None):
    from sanhita.remediate import recheck_amendment_task

    return recheck_amendment_task(store, task_id, rules, tree, by=by, at=at)


def test_a_recertify_stays_open_while_the_signature_covers_the_old_text(tmp_path):
    """The defect this whole module exists to prevent: closing on a claim."""
    from sanhita.remediate import TaskStatus

    store = _store(tmp_path)
    task = _raise(store, _Required("RECERTIFY"))
    rules = [_Rule("SB-15.1-a", "15.1", OLD_TEXT, "CERTIFIED", _Cert())]

    verdict = _recheck(store, task.task_id, rules, _Tree({"15.1": _Node(NEW_TEXT)}))

    assert not verdict.closes
    assert "still covers the earlier text" in verdict.detail
    assert store.get(task.task_id).status is not TaskStatus.CLOSED


def test_a_recertify_closes_when_a_signature_covers_the_new_text(tmp_path):
    from sanhita.remediate import TaskStatus

    store = _store(tmp_path)
    task = _raise(store, _Required("RECERTIFY"))
    rules = [_Rule("SB-15.1-a", "15.1", NEW_TEXT, "CERTIFIED", _Cert())]

    verdict = _recheck(store, task.task_id, rules, _Tree({"15.1": _Node(NEW_TEXT)}))

    assert verdict.closes
    assert "A Named Officer" in verdict.detail
    closed = store.get(task.task_id)
    assert closed.status is TaskStatus.CLOSED, closed.last_recheck_result
    assert closed.verified_at is not None


def test_an_uncertified_rule_does_not_close_a_recertify(tmp_path):
    """Recompiling over the new text is not somebody signing it."""
    store = _store(tmp_path)
    task = _raise(store, _Required("RECERTIFY"))
    rules = [_Rule("SB-15.1-a", "15.1", NEW_TEXT, "PROPOSED")]

    verdict = _recheck(store, task.task_id, rules, _Tree({"15.1": _Node(NEW_TEXT)}))

    assert not verdict.closes
    assert "not certified" in verdict.detail


def test_a_repoint_needs_the_rule_to_point_at_the_new_number(tmp_path):
    store = _store(tmp_path)
    task = _raise(store, _Required("REPOINT", clause_id="15.1", now_at="16.4"))
    tree = _Tree({"16.4": _Node(NEW_TEXT)})

    still = _recheck(
        store,
        task.task_id,
        [_Rule("SB-15.1-a", "15.1", NEW_TEXT, "CERTIFIED", _Cert())],
        tree,
    )
    assert not still.closes
    assert "still points at clause 15.1" in still.detail

    done = _recheck(
        store,
        task.task_id,
        [_Rule("SB-15.1-a", "16.4", NEW_TEXT, "CERTIFIED", _Cert())],
        tree,
    )
    assert done.closes


def test_a_withdraw_closes_only_when_the_rule_is_off_the_books(tmp_path):
    store = _store(tmp_path)
    task = _raise(store, _Required("WITHDRAW"))

    still = _recheck(
        store,
        task.task_id,
        [_Rule("SB-15.1-a", "15.1", OLD_TEXT, "CERTIFIED", _Cert())],
        _Tree({}),
    )
    assert not still.closes
    assert "still certified" in still.detail

    done = _recheck(
        store, task.task_id, [_Rule("SB-15.1-a", "15.1", OLD_TEXT, "REJECTED")], _Tree({})
    )
    assert done.closes


def test_a_reread_closes_on_a_signature_dated_after_it_was_raised(tmp_path):
    """No hash can prove somebody read a clause that did not change.

    What can be proved is that they signed it again knowing the amendment had
    landed, so that is what closure requires.
    """
    store = _store(tmp_path)
    raised_at = _dt.datetime(2026, 8, 10, tzinfo=_dt.timezone.utc)
    task = _raise(store, _Required("REREAD"), at=raised_at)
    tree = _Tree({"15.1": _Node(OLD_TEXT)})

    stale = _recheck(
        store,
        task.task_id,
        [
            _Rule(
                "SB-15.1-a",
                "15.1",
                OLD_TEXT,
                "CERTIFIED",
                _Cert(at=_dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)),
            )
        ],
        tree,
    )
    assert not stale.closes
    assert "predates this amendment" in stale.detail

    fresh = _recheck(
        store,
        task.task_id,
        [
            _Rule(
                "SB-15.1-a",
                "15.1",
                OLD_TEXT,
                "CERTIFIED",
                _Cert(at=_dt.datetime(2026, 8, 12, tzinfo=_dt.timezone.utc)),
            )
        ],
        tree,
    )
    assert fresh.closes
    assert "after this was raised" in fresh.detail


def test_a_new_clause_closes_on_a_decision_either_way(tmp_path):
    """Rejecting a clause is as much a decision as certifying one."""
    store = _store(tmp_path)
    task = _raise(store, _Required("ASSESS_NEW", clause_id="62.9", obligation_id=""))

    undecided = _recheck(
        store, task.task_id, [_Rule("SB-62.9-a", "62.9", NEW_TEXT, "PROPOSED")], _Tree({})
    )
    assert not undecided.closes
    assert "certified or rejected yet" in undecided.detail

    decided = _recheck(
        store, task.task_id, [_Rule("SB-62.9-a", "62.9", NEW_TEXT, "REJECTED")], _Tree({})
    )
    assert decided.closes


def test_a_clause_missing_from_the_later_document_is_not_a_pass(tmp_path):
    """"Could not check" and "checked and passed" are different answers."""
    from sanhita.remediate import TaskStatus

    store = _store(tmp_path)
    task = _raise(store, _Required("RECERTIFY"))

    verdict = _recheck(
        store,
        task.task_id,
        [_Rule("SB-15.1-a", "15.1", NEW_TEXT, "CERTIFIED", _Cert())],
        _Tree({}),
    )

    assert not verdict.evaluated
    assert not verdict.closes
    assert store.get(task.task_id).status is TaskStatus.OPEN


def test_every_recheck_is_on_the_log_whatever_it_found(tmp_path):
    store = _store(tmp_path)
    task = _raise(store, _Required("RECERTIFY"))
    _recheck(
        store,
        task.task_id,
        [_Rule("SB-15.1-a", "15.1", OLD_TEXT, "CERTIFIED", _Cert())],
        _Tree({"15.1": _Node(NEW_TEXT)}),
    )
    _recheck(
        store,
        task.task_id,
        [_Rule("SB-15.1-a", "15.1", NEW_TEXT, "CERTIFIED", _Cert())],
        _Tree({"15.1": _Node(NEW_TEXT)}),
    )

    intact, problem = store.log.verify()
    assert intact, problem
    kinds = [e.transition.value for e in store.log.for_task(task.task_id)]
    assert kinds.count("RECHECKED") == 2
    assert "CLOSED" in kinds
    assert store.get(task.task_id).recheck_count == 2


def test_an_evidence_task_is_refused_by_the_amendment_recheck(tmp_path):
    """The two kinds close on different facts, so they must not be confused."""
    from sanhita.remediate import RemediationError

    store = _store(tmp_path)
    task = store.open_for_gap(
        gap_id="EV-1",
        obligation_id="SB-15.1-a",
        clause_id="15.1",
        company="ABC",
        title="Missing filing",
        by="A Named Officer",
    )

    with pytest.raises(RemediationError, match="evidence finding"):
        _recheck(store, task.task_id, [], _Tree({}))


def test_there_is_still_no_way_to_assert_a_task_is_done(tmp_path):
    """The property the whole design turns on, asserted on the new path."""
    import inspect

    from sanhita.remediate import amendment

    source = inspect.getsource(amendment)
    assert "TaskStatus.CLOSED" in source
    # Closure is reachable only through the verdict, which is a pure function
    # of the store and the tree.
    body = source[source.index("def recheck_amendment_task") :]
    assert "if verdict.closes:" in body
    assert "def _verdict" in source


# ------------------------------------------------------------ through the UI


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


@requires_corpus
def test_the_route_refuses_an_action_the_comparison_never_found(client, corpus_pdf):
    """The amendment version of raising a task from a preview."""
    from sanhita.remediate import RemediationStore

    client.post(
        "/signup",
        data={
            "name": "A Named Officer",
            "email": "officer@example.com",
            "password": "a-long-enough-password",
        },
        follow_redirects=True,
    )
    # A firm that clears every earlier gate, so what this test refuses is the
    # invented action and nothing else.
    client.post(
        "/w/demo/company/save",
        data={"name": "ABC Securities", "intermediary": "STOCK_BROKER"},
        follow_redirects=True,
    )
    client.post(
        "/w/demo/company/frameworks", data={"framework": "demo"}, follow_redirects=True
    )
    client.post("/w/demo/setup/complete", follow_redirects=True)
    uploaded = client.post(
        "/documents/upload",
        content=corpus_pdf.read_bytes() + b"\n% a later edition",
        headers={"x-sanhita-filename": "later-edition.pdf"},
    )
    assert uploaded.status_code == 200, uploaded.text

    response = client.post(
        "/w/demo/change/open",
        data={
            "against": uploaded.json()["id"],
            "kind": "RECERTIFY",
            "clause_id": "999.9",
            "obligation_id": "SB-999.9-a",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "no RECERTIFY action on clause 999.9" in response.text
    assert not RemediationStore.load(
        corpus_pdf.parent.parent / ".sanhita" / "remediation.json"
    ).tasks


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
def test_a_real_amendment_becomes_a_task_and_the_store_closes_it(tmp_path, monkeypatch):
    """The whole loop, on a document SEBI actually published.

    Compare two editions, raise one of the required actions as a task through
    the route a person would use, check that it will not close while the
    rulebook is still wrong, put the rulebook right, and check again.

    Nothing here asserts that the work was done. The store and the later
    document are asked, and they answer.
    """
    import shutil

    from fastapi.testclient import TestClient

    from sanhita.certify import certify
    from sanhita.cli_compile import _load_registry, _save_registry
    from sanhita.compile.extract import ExtractionStatus, RuleExtractor
    from sanhita.diff import diff_trees
    from sanhita.parse.clause_tree import parse_clause_tree
    from sanhita.remediate import RemediationStore, TaskStatus
    from sanhita.web.app import create_app

    key = "0" * 64
    monkeypatch.setenv("SANHITA_SIGNING_KEY", key)
    old_pdf, new_pdf = _both_editions()
    old_copy, new_copy = tmp_path / OLD_EDITION, tmp_path / NEW_EDITION
    shutil.copy(old_pdf, old_copy)
    shutil.copy(new_pdf, new_copy)

    before, after = parse_clause_tree(old_copy), parse_clause_tree(new_copy)
    changes = diff_trees(before, after, before_label="June 2025", after_label="February 2026")

    # A store as it would stand before the amendment: compiled from the June
    # edition and signed by a named officer.
    registry = _load_registry(tmp_path / "rules.json")
    extractor = RuleExtractor(circular_id="ia-2025-06")
    for node in before.nodes.values():
        outcome = extractor.extract(node)
        if outcome.status is ExtractionStatus.PROPOSED:
            for obligation in outcome.obligations:
                registry.propose(obligation, by="extractor:rules")

    removed = {c.clause_id for c in changes.removed}
    withdrawable = next(
        (r for r in registry.all_current() if r.source.clause_id in removed), None
    )
    assert withdrawable is not None, "no compiled rule sits on a clause this edition removed"
    certify(registry, withdrawable.id, by="A Named Officer", key=key)
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

    # The comparison offers this as work, and raising it is a form on the page.
    page = client.get(f"/w/demo/diff?against={against}").text
    assert "Make this somebody's task" in page

    raised = client.post(
        "/w/demo/change/open",
        data={
            "against": against,
            "kind": "WITHDRAW",
            "clause_id": withdrawable.source.clause_id,
            "obligation_id": withdrawable.id,
            "owner": "R. Nair",
        },
        follow_redirects=False,
    )
    assert raised.status_code == 303, raised.text

    store = RemediationStore.load(tmp_path / "remediation.json")
    task = next(t for t in store.tasks.values() if t.is_from_an_amendment)
    assert task.owner == "R. Nair"
    assert task.change_kind == "WITHDRAW"
    assert task.amended_to == after.fingerprint()

    # It will not close while the rule is still certified in the store.
    client.post(f"/w/demo/remediation/{task.task_id}/recheck", follow_redirects=True)
    reloaded = RemediationStore.load(tmp_path / "remediation.json").get(task.task_id)
    assert reloaded.status is not TaskStatus.CLOSED
    assert "still certified" in reloaded.last_recheck_result

    # Put the rulebook right, through the same screens a person would use. A
    # certified rule cannot simply be rejected: it is superseded by a new
    # version, which is then rejected, so the signed version keeps its
    # signature and a historical evaluation can still be replayed.
    clause = withdrawable.source.clause_id
    amended = client.post(
        f"/w/demo/clause/{clause}/edit",
        data={
            "obligation_id": withdrawable.id,
            "verb": withdrawable.action.verb,
            "object": withdrawable.action.object + " (clause removed)",
            "by": "A Named Officer",
            "note": "the February 2026 edition removed this clause",
        },
        follow_redirects=False,
    )
    assert amended.status_code == 303, amended.text
    rejected = client.post(
        f"/w/demo/clause/{clause}/reject",
        data={
            "obligation_id": withdrawable.id,
            "by": "A Named Officer",
            "reason": "the clause was removed by the February 2026 edition",
        },
        follow_redirects=False,
    )
    assert rejected.status_code == 303, rejected.text

    client.post(f"/w/demo/remediation/{task.task_id}/recheck", follow_redirects=True)
    closed = RemediationStore.load(tmp_path / "remediation.json").get(task.task_id)

    assert closed.status is TaskStatus.CLOSED
    assert closed.verified_at is not None
    intact, problem = RemediationStore.load(tmp_path / "remediation.json").log.verify()
    assert intact, problem


@requires_corpus
def test_an_amendment_task_shows_its_own_way_of_closing(client, tmp_path):
    """It has no evidence to attach, so it must not ask for any."""
    from sanhita.remediate import RemediationStore, open_for_action

    store = RemediationStore(path=tmp_path / "remediation.json")
    open_for_action(
        store,
        _Required("RECERTIFY", clause_id="15.1", obligation_id="SB-15.1-a"),
        company="ABC Securities",
        by="A Named Officer",
        before_fingerprint="f" * 64,
        after_fingerprint="a" * 64,
    )
    store.save()

    page = _plain(client.get("/w/demo/remediation").text)

    assert "Check whether this is done" in page
    assert "Raised from a regulatory change rather than a missing record" in page
    assert "There is no button that marks it fixed" in page
