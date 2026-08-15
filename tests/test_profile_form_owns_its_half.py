"""A form with six boxes must not be able to reset a ten field object.

The profile form asks for the firm's name, its intermediary type, its
registration number, its processes, its systems and its business facts. The
``Company`` it saves into holds four more: which rulebooks the firm declared,
when onboarding finished, when the profile was created, and whether it is the
worked example rather than a real firm.

The save route used to rebuild the whole object from the form and hand-list the
other four to copy across. That shape failed twice, both times silently.

**A field added later was not on the list.** ``setup_completed_at`` arrived with
onboarding and was never added, so correcting a registration number a month
afterwards sent the firm back to step three of setting up, and every route that
requires a set-up firm then refused it.

**The object copied from was not always the visitor's own.** Reads fall through
to the demonstration state, so on a shared deployment a visitor with no profile
yet was shown the seeded firm, and saving inherited its history. Including
``synthetic=True``, which printed "demonstration data" across a real firm's real
profile. A product whose whole argument is that the screen can be trusted does
not get to mislabel real data as fake.

Both are now impossible by construction: the route updates in place, and
``Company.FORM_FIELDS`` names the half the form is allowed to touch. The first
test below is the one that matters most, because it fails the moment somebody
adds a field without deciding which half it belongs to.
"""

from __future__ import annotations

import datetime as _dt
import re
import shutil

import pytest

from tests.conftest import requires_corpus

# The four the form must never reach, and who owns each instead.
#
# A name, not just an exclusion. "Not on the form" is not a reason, and a set
# of bare strings invites the next person to add one to make a failing test
# pass. Naming the owner forces the real question: if nothing owns this field,
# what writes it, and why is it on Company at all?
#
# Kept here as a literal rather than derived from the model, so this file
# disagrees with the model when the model changes. A test that reads its
# expectations out of the thing under test cannot fail.
OWNED_BY = {
    "frameworks": "the frameworks screen, /company/frameworks. Which rulebook "
                  "governs a firm is a legal declaration, not a profile detail",
    "setup_completed_at": "onboarding, /setup/complete. Stamped once, when the "
                          "third answer is given",
    "created_at": "the first save. Set when the profile does not exist yet and "
                  "never rewritten",
    "synthetic": "the demonstration seeder. True only on the worked example, "
                 "and printed wherever the profile is shown",
}
OWNED_ELSEWHERE = frozenset(OWNED_BY)


# ------------------------------------------------------- the structural rule


def test_every_field_belongs_to_exactly_one_half():
    """Add a field to Company and this fails until you place it.

    Not a style check. A field nobody placed is a field the save route will
    treat by accident, and the two defects this module exists to prevent were
    both exactly that.
    """
    import dataclasses

    from sanhita.company import Company

    # `dataclasses.fields` rather than `__dataclass_fields__`: the latter also
    # carries ClassVars, so it would report FORM_FIELDS itself as unplaced.
    declared = {f.name for f in dataclasses.fields(Company)}
    placed = set(Company.FORM_FIELDS) | set(OWNED_ELSEWHERE)

    unplaced = declared - placed
    assert not unplaced, (
        f"{sorted(unplaced)} is on Company but in neither half. Decide whether "
        "the profile form owns it (add it to Company.FORM_FIELDS and to the "
        "form) or something else does (add it to OWNED_ELSEWHERE here)."
    )
    stale = placed - declared
    assert not stale, f"{sorted(stale)} is claimed by a half but is not on Company"


def test_no_field_is_claimed_by_both_halves():
    """Ambiguous ownership is worse than none, because it reads as settled.

    A field in both halves means the form may write it *and* something else
    owns it, which is the original defect wearing a label that says it was
    considered. The failure names the field rather than printing both sets.
    """
    from sanhita.company import Company

    both = sorted(set(Company.FORM_FIELDS) & set(OWNED_ELSEWHERE))
    assert not both, (
        f"{both} is claimed by the profile form and by "
        f"{'; '.join(OWNED_BY[f] for f in both)}. One of the two is wrong: "
        "either the form owns it and it is not owned elsewhere, or it is not "
        "on the form and must come out of Company.FORM_FIELDS."
    )


def test_every_preserved_field_names_who_owns_it():
    """'Not on the form' is an exclusion. This asks for the reason."""
    for name, owner in OWNED_BY.items():
        assert owner and len(owner) > 20, (
            f"{name!r} is preserved but its owner is not described. Say what "
            "writes it and when, or reconsider whether it belongs on Company."
        )


def test_applying_the_form_leaves_the_other_half_alone():
    """At the model, before any route is involved."""
    from sanhita.company import Company, IntermediaryType

    stamp = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
    firm = Company(
        name="Original",
        frameworks=["w1", "w2"],
        setup_completed_at=stamp,
        created_at=stamp,
        synthetic=True,
    )

    firm.apply_profile_form(
        name="Renamed",
        intermediary=IntermediaryType.STOCK_BROKER,
        registration="INZ000123456",
        processes=["Daily margin reporting"],
        systems=["Margin engine"],
        business_facts={"Offers derivatives": True},
    )

    assert firm.name == "Renamed"
    assert firm.registration == "INZ000123456"
    # And the half the form does not own, untouched.
    assert firm.frameworks == ["w1", "w2"]
    assert firm.setup_completed_at == stamp
    assert firm.created_at == stamp
    assert firm.synthetic is True


# --------------------------------------------------------- through the route


@pytest.fixture()
def client(corpus_pdf, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a-test-only-key")
    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)
    client = TestClient(create_app(corpus_pdf, store=store))
    client.post(
        "/signup",
        data={
            "name": "A Named Officer",
            "email": "officer@example.com",
            "password": "a-long-enough-password",
        },
        follow_redirects=True,
    )
    return client


def _plain(html: str) -> str:
    body = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


@requires_corpus
def test_a_profile_edit_keeps_everything_the_form_does_not_ask_about(client, tmp_path):
    """The whole other half, through the real route, in one assertion."""
    from sanhita.company import Company

    client.post(
        "/w/demo/company/save",
        data={"name": "ABC Securities", "intermediary": "STOCK_BROKER"},
        follow_redirects=True,
    )
    client.post(
        "/w/demo/company/frameworks", data={"framework": "demo"}, follow_redirects=True
    )
    client.post("/w/demo/setup/complete", follow_redirects=True)

    before = Company.load(tmp_path / "company.json")
    assert before is not None and before.setup_completed_at is not None

    # A month later, somebody corrects the registration number.
    client.post(
        "/w/demo/company/save",
        data={
            "name": "ABC Securities",
            "intermediary": "STOCK_BROKER",
            "registration": "INZ000123456",
        },
        follow_redirects=True,
    )
    after = Company.load(tmp_path / "company.json")

    assert after.registration == "INZ000123456", "the edit did not take"
    for attribute in sorted(OWNED_ELSEWHERE):
        assert getattr(after, attribute) == getattr(before, attribute), (
            f"saving the profile changed {attribute!r}, which the form does not ask about"
        )


@requires_corpus
def test_a_brand_new_profile_starts_from_defaults(client, tmp_path):
    """Nothing is inherited when there is nothing of the visitor's to inherit."""
    from sanhita.company import Company

    client.post(
        "/w/demo/company/save",
        data={"name": "Zeta Broking", "intermediary": "STOCK_BROKER"},
        follow_redirects=True,
    )
    firm = Company.load(tmp_path / "company.json")

    assert firm.name == "Zeta Broking"
    assert firm.frameworks == []
    assert firm.setup_completed_at is None
    assert firm.synthetic is False
    assert firm.created_at is not None, "a profile should know when it was made"
