"""What leaves this machine, and what must not.

An audit of the submission archive found it carrying a real personal email
address with its password hash, three throwaway accounts, an assessment
recorded before actor hardening whose actor read `unattributed`, a company
profile overwritten four times during live testing, and 143 files of Python
bytecode.

None of that is wrong as development state. All of it is wrong in a file handed
to a competition, and none of it was anybody's decision: it was there because a
zip of a working directory contains the working directory.

Two things fix it and both are tested here. The demonstration state is
generated rather than curated, so it is the same every time and contains
nothing that belongs to a person. And the archive is built by a script with an
explicit exclusion list, so what is kept out is a decision somebody wrote down
rather than whatever happened to be untracked.
"""

from __future__ import annotations

import datetime as _dt
import json
import shutil

import pytest

from tests.conftest import ROOT, requires_corpus

WHEN = _dt.datetime(2026, 8, 13, 12, 0, tzinfo=_dt.timezone.utc)


@pytest.fixture()
def store(tmp_path, corpus_pdf):
    """A store with the real rulebook and nothing else."""
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", tmp_path / "rules.json")
    return tmp_path


def _seed(store, **kw):
    from sanhita.demo_seed import seed_demo_state

    kw.setdefault("at", WHEN)
    return seed_demo_state(store, **kw)


# ------------------------------------------------------- the demonstration


@requires_corpus
def test_the_demo_state_is_built_from_nothing(store):
    result = _seed(store)

    assert result.certified == 183
    assert result.occasions == 4
    assert result.assessment_id
    for name in ("company.json", "evidence.json", "assessments.json", "users.json"):
        assert (store / name).is_file(), f"{name} was not written"


@requires_corpus
def test_the_same_command_produces_the_same_state(store, tmp_path, corpus_pdf):
    """A demonstration that differs between two runs cannot be rehearsed."""
    second = tmp_path / "second"
    second.mkdir()
    shutil.copy(store / "rules.json", second / "rules.json")

    first = _seed(store)
    again = _seed(second)

    assert first.occasions == again.occasions
    assert first.open_gaps == again.open_gaps
    assert (store / "evidence.json").read_text() == (second / "evidence.json").read_text()


@requires_corpus
def test_the_firm_is_marked_synthetic(store):
    """Nobody watching should have to wonder whether ABC Securities is real."""
    from sanhita.company import Company

    _seed(store)
    company = Company.load(store / "company.json")

    assert company.synthetic is True
    assert company.name == "ABC Securities Pvt Ltd"
    assert company.setup_completed_at is not None, "the demo should open past onboarding"


@requires_corpus
def test_the_only_account_is_obviously_not_a_real_address(store):
    """`.invalid` is reserved by RFC 2606 so an address can never reach anybody."""
    _seed(store)
    users = json.loads((store / "users.json").read_text(encoding="utf-8"))

    assert len(users["users"]) == 1
    email = users["users"][0]["email"]
    assert email.endswith(".invalid"), f"{email} could be a real address"
    assert "gmail" not in email and "@example.com" not in email


@requires_corpus
def test_the_assessment_names_the_officer_who_ran_it(store):
    """The defect this exists for: `ran_by: unattributed` on a jury's screen,
    from the product whose central claim is that every act names somebody."""
    from sanhita.assess import AssessmentLog

    _seed(store)
    run = AssessmentLog.load(store / "assessments.json").latest

    assert run is not None
    assert run.ran_by == "Demo Compliance Officer"
    assert run.ran_by != "unattributed"


@requires_corpus
def test_there_is_one_unfiled_occasion_to_remediate_on_camera(store):
    """A demo with everything closed shows nothing; one with forty open gaps
    shows nothing a viewer can follow."""
    from sanhita.execute import EvidenceStore

    _seed(store)
    events = EvidenceStore.load(store / "evidence.json").events

    unfiled = [e for e in events if e.filed_on is None]
    assert len(unfiled) == 1
    assert len(events) == 4
    # And it is the most recent, so the story reads "they stopped filing".
    assert unfiled[0].occurred_on == max(e.occurred_on for e in events)


@requires_corpus
def test_the_rulebook_is_never_touched(store):
    """Regenerating it would mean re-signing, which changes the provenance of
    every figure this product publishes."""
    before = (store / "rules.json").read_bytes()
    _seed(store)

    assert (store / "rules.json").read_bytes() == before


@requires_corpus
def test_existing_data_is_moved_aside_rather_than_deleted(store):
    """Somebody runs this on their working directory sooner or later."""
    (store / "company.json").write_text('{"name": "Real Firm Ltd"}', encoding="utf-8")
    (store / "evidence.json").write_text('{"events": []}', encoding="utf-8")

    result = _seed(store)

    assert result.backup is not None
    assert result.backup.is_dir()
    assert set(result.moved_aside) >= {"company.json", "evidence.json"}
    kept = json.loads((result.backup / "company.json").read_text(encoding="utf-8"))
    assert kept["name"] == "Real Firm Ltd", "somebody's data was destroyed"


@requires_corpus
def test_the_accounts_can_be_left_alone_on_purpose(store):
    _seed(store)
    before = (store / "users.json").read_text(encoding="utf-8")
    (store / "users.json").write_text(before.replace("Demo", "Kept"), encoding="utf-8")

    _seed(store, include_account=False)

    assert "Kept" in (store / "users.json").read_text(encoding="utf-8")


# ------------------------------------------------------------ the archive


def test_the_exclusion_list_says_why_for_every_line():
    """A bare exclusion list rots, because nobody remembers why a line is there."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("mk", ROOT / "make-submission.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert len(module.EXCLUDE) >= 15
    for pattern, reason in module.EXCLUDE:
        assert pattern and reason, f"{pattern!r} has no stated reason"
        assert len(reason) > 8, f"{reason!r} does not explain anything"


def test_every_kind_of_personal_data_is_on_the_exclusion_list():
    """The four that actually matter, asserted by name."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("mk", ROOT / "make-submission.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    patterns = [p for p, _ in module.EXCLUDE]

    for must_drop in (
        ".sanhita/users.json",
        ".sanhita/workspaces/**",
        ".sanhita/company.json",
        ".sanhita/evidence.json",
        ".sanhita/assessments.json",
    ):
        assert must_drop in patterns, f"{must_drop} would be shipped"


def test_the_rulebook_and_the_circular_are_kept_on_purpose():
    """A clone that boots to an empty screen demonstrates nothing."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("mk", ROOT / "make-submission.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    kept = [k for k, _ in module.KEEP_ANYWAY]

    assert ".sanhita/rules.json" in kept
    assert any("stock-brokers" in k for k in kept)


def test_a_firms_own_data_is_excluded_even_though_it_sits_beside_the_rulebook():
    """`.sanhita/` holds both the regulator's compiled text and one firm's
    private records, and only one of them may leave."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("mk", ROOT / "make-submission.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module._excluded(".sanhita/users.json")
    assert module._excluded(".sanhita/company.json")
    assert module._excluded(".sanhita/workspaces/abc123/meta.json")
    assert module._excluded(".sanhita/evidence.u7.json"), "a scoped copy leaked"
    assert module._excluded("src/sanhita/__pycache__/app.cpython-314.pyc")
    # And the two that must survive it.
    assert not module._excluded(".sanhita/rules.json")
    assert not module._excluded("src/sanhita/discover.py")


# --------------------------------------------- what the deployed image needs


@requires_corpus
def test_the_amendment_editions_are_registered_ready_to_compare(store):
    """A deployed site held one document, so the comparison screen said there
    was nothing to compare against and the strongest thing this product does
    could not be shown on it at all."""
    from sanhita.workspace import WorkspaceStore

    result = _seed(store, amendment=True, corpus=ROOT / "corpus", backup=False)

    assert len(result.editions) == 2, result.editions
    spaces = WorkspaceStore(
        store / "workspaces",
        builtin_pdf=ROOT / "corpus" / "stock-brokers-master-circular-2025-06-17.pdf",
        builtin_store=store / "rules.json",
    ).uploaded()
    assert len(spaces) == 2
    issued = sorted(s.issued_on for s in spaces if s.issued_on)
    assert issued == [_dt.date(2025, 6, 27), _dt.date(2026, 2, 6)], (
        "the editions must carry their own issue dates or nothing can order them"
    )


@requires_corpus
def test_registering_an_edition_does_not_parse_it(store):
    """This runs during an image build. Reading a 400-page circular there would
    add half a minute per edition for nothing: the tree is built the first time
    somebody opens it."""
    import time

    started = time.perf_counter()
    _seed(store, amendment=True, corpus=ROOT / "corpus", backup=False)
    elapsed = time.perf_counter() - started

    assert elapsed < 20, f"seeding took {elapsed:.1f}s, so something is parsing"


@requires_corpus
def test_the_image_builds_the_demo_account_rather_than_copying_it(store):
    """`.dockerignore` keeps users.json out of the build context on purpose.
    The account has to be created inside the build or the deployed site is one
    nobody can sign in to, which is what it was."""
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert ".sanhita/users.json" in dockerignore, "password hashes would be copied in"
    assert ".sanhita/workspaces/" in dockerignore, "somebody's uploads would be copied in"
    assert "sanhita demo-seed --amendment" in dockerfile, (
        "the image no longer builds its own demonstration state"
    )


@requires_corpus
def test_the_image_carries_the_editions_the_comparison_needs():
    """They were excluded to save 20 MB. Two of them are worth 2.3 MB."""
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert "corpus/investment-advisers-*.pdf" not in dockerignore, (
        "the amendment demonstration has nothing to compare against again"
    )
    for name in ("depositories", "research-analysts", "mutual-funds"):
        assert f"corpus/{name}-*.pdf" in dockerignore, (
            f"{name} is being shipped for no reason"
        )
