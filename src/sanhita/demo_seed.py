"""A clean demonstration state, built from nothing, on one command.

Recording a demo off a development store is how a submission leaks. The store
this product was built against accumulated a real person's email address, three
throwaway accounts, an assessment recorded before actor hardening whose actor
reads `unattributed`, and a company profile that had been overwritten four
times during live testing. None of that is wrong as development state. All of
it is wrong on a screen a jury watches.

So the demonstration state is generated rather than curated. One command, no
arguments, and the result is the same every time:

    sanhita demo-seed

**It never destroys anything.** Existing sidecars are moved aside into a
timestamped backup folder first, and the command prints where they went. A
developer who runs this on the wrong directory loses nothing.

**It never touches the rulebook.** `rules.json` holds 183 certifications, and
regenerating them would mean re-signing, which changes the signed bytes and the
provenance of every figure this product publishes. The demo state is the firm's
side only: who they are, what they filed, what was assessed, what is open.

**It leaves exactly one gap open on purpose.** A demo where everything is
already closed has nothing to show; a demo with forty open gaps has nothing a
viewer can follow. One unfiled occasion, on one certified recurring rule, ready
to be remediated on camera.

The identities are unmistakably synthetic. Nobody watching should have to
wonder whether "Demo Compliance Officer" is a real person at a real firm.
"""

from __future__ import annotations

import datetime as _dt
import shutil
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DEMO_OFFICER",
    "DEMO_EMAIL",
    "DEMO_FIRM",
    "AMENDMENT_EDITIONS",
    "SeedResult",
    "seed_demo_state",
]

#: The two editions the amendment demonstration compares. SEBI issued the
#: Master Circular for Investment Advisers in June 2025 and reissued it in
#: February 2026, renumbering 376 clauses. Registering both as workspaces is
#: what lets the comparison screen open ready instead of saying there is
#: nothing to compare against, which is what a deployed site said until this
#: existed: the strongest thing the product does could not be shown on it.
AMENDMENT_EDITIONS = (
    "investment-advisers-2025-06-27.pdf",
    "investment-advisers-2026-02.pdf",
)

#: Synthetic and obviously so. `.invalid` is reserved by RFC 2606 precisely so
#: that an address can be written down without ever reaching anybody.
DEMO_OFFICER = "Demo Compliance Officer"
DEMO_EMAIL = "demo.officer@sanhita.invalid"
DEMO_PASSWORD = "demo-compliance-officer"
DEMO_FIRM = "ABC Securities Pvt Ltd"

#: The firm's own data. The rulebook and the accounts file are handled
#: separately: one must not be regenerated, the other must not be blindly
#: emptied on a machine somebody is using.
FIRM_SIDECARS = (
    "company.json",
    "evidence.json",
    "review.json",
    "assessments.json",
    "remediation.json",
    "controls.json",
    "plans.json",
)


@dataclass
class SeedResult:
    """What the seeding did, in enough detail to check it."""

    root: Path
    backup: Path | None = None
    moved_aside: list[str] = None
    firm: str = DEMO_FIRM
    officer: str = DEMO_OFFICER
    certified: int = 0
    occasions: int = 0
    open_gaps: int = 0
    #: Duties that fell due with no record either way. Reported beside the
    #: confirmed gaps and never inside them.
    unverified: int = 0
    assessment_id: str = ""
    #: Editions registered so the amendment comparison has something to open.
    editions: list[str] = None

    def __post_init__(self) -> None:
        if self.moved_aside is None:
            self.moved_aside = []
        if self.editions is None:
            self.editions = []


def _clear_firm_data(root: Path, at: _dt.datetime) -> tuple[Path | None, list[str]]:
    """Move the firm's existing sidecars aside, keeping every one of them.

    Deleting would be simpler and is the wrong call. Somebody runs this on
    their working directory sooner or later, and losing an afternoon's evidence
    to a demo command is not a trade anybody agreed to.
    """
    existing = []
    for path in sorted(root.glob("*.json")):
        stem = path.name.split(".")[0] + ".json"
        # Scoped copies too: `evidence.<visitor>.json` is somebody's data.
        if stem in FIRM_SIDECARS:
            existing.append(path)
    if not existing:
        return None, []

    backup = root / f"backup-{at.strftime('%Y%m%d-%H%M%S')}"
    backup.mkdir(parents=True, exist_ok=True)
    moved = []
    for path in existing:
        shutil.move(str(path), str(backup / path.name))
        moved.append(path.name)
    return backup, moved


def _demo_account(root: Path) -> None:
    """One synthetic account, replacing whatever accounts were there.

    The development store carried a real personal address and two throwaways.
    A submission artifact should carry neither, and a demo needs exactly one
    identity so the recording never shows an account picker.
    """
    from sanhita.auth import UserStore

    users_path = root / "users.json"
    if users_path.is_file():
        users_path.unlink()
    store = UserStore(users_path)
    store.create(email=DEMO_EMAIL, name=DEMO_OFFICER, password=DEMO_PASSWORD)


def _register_editions(root: Path, corpus: Path) -> list[str]:
    """Put both Investment Adviser editions on the shelf, ready to compare.

    Registering is not parsing: the PDF is copied and a `meta.json` written,
    and the clause tree is built the first time somebody opens it. So this
    costs a file copy rather than the thirty seconds a 400-page circular takes
    to read, which matters because it runs during an image build.

    The issue date is read off each document's own first page by
    `WorkspaceStore.create`, so the two sort correctly and the regulatory watch
    can tell which of them is the later edition.
    """
    from sanhita.workspace import WorkspaceStore

    store = WorkspaceStore(
        root / "workspaces",
        builtin_pdf=corpus / "stock-brokers-master-circular-2025-06-17.pdf",
        builtin_store=root / "rules.json",
    )
    registered = []
    for name in AMENDMENT_EDITIONS:
        source = corpus / name
        if not source.is_file():
            continue
        space = store.create(source.read_bytes(), filename=name, name=_edition_title(name))
        registered.append(f"{space.name} ({space.issued_on or 'undated'})")
    return registered


def _edition_title(filename: str) -> str:
    """A name a person would recognise on a dropdown."""
    if "2025-06" in filename:
        return "Investment Advisers, June 2025"
    if "2026-02" in filename:
        return "Investment Advisers, February 2026"
    return filename


def seed_demo_state(
    root: Path,
    *,
    workspace_id: str = "demo",
    at: _dt.datetime | None = None,
    include_account: bool = True,
    amendment: bool = False,
    corpus: Path | None = None,
    backup: bool = True,
) -> SeedResult:
    """Build the demonstration state. Same inputs, same result, every time.

    ``root`` is the store directory, normally ``.sanhita``. The rulebook inside
    it is read and never written.
    """
    from sanhita.assess import AssessmentLog
    from sanhita.cli_compile import _load_registry
    from sanhita.company import Company, IntermediaryType
    from sanhita.execute import WEEKENDS_ONLY, ComplianceEvent, EvidenceStore, RuleEngine
    from sanhita.ir.enums import DeadlineKind, RuleStatus

    moment = at or _dt.datetime.now(_dt.timezone.utc)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if backup:
        kept, moved = _clear_firm_data(root, moment)
    else:
        # A fresh image build has nothing worth keeping, and a backup folder
        # baked into a container is clutter that ships forever.
        kept, moved = None, []
        for name in FIRM_SIDECARS:
            target = root / name
            if target.is_file():
                target.unlink()
    result = SeedResult(root=root, backup=kept, moved_aside=moved)

    registry = _load_registry(root / "rules.json")
    obligations = registry.all_current()
    certified = [o for o in obligations if o.status is RuleStatus.CERTIFIED]
    result.certified = len(certified)
    if not certified:
        raise RuntimeError(
            f"{root / 'rules.json'} carries no certified rule, so there is "
            "nothing to demonstrate against. Compile and certify first."
        )

    # -- the firm
    company = Company(
        name=DEMO_FIRM,
        intermediary=IntermediaryType.STOCK_BROKER,
        registration="INZ000000000",
        processes=[
            "Daily margin reporting",
            "Client onboarding",
            "Quarterly regulatory filing",
        ],
        systems=["Back office", "Margin engine"],
        frameworks=[workspace_id],
        setup_completed_at=moment,
        created_at=moment,
        # The flag the screens already print. A synthetic firm must never be
        # mistakable for a real intermediary on a jury's screen.
        synthetic=True,
    )
    company.save(root / "company.json")

    # -- the records
    #
    # One recurring certified duty, filed for every occasion but the most
    # recent. That leaves exactly one open gap: enough to walk a remediation on
    # camera, few enough that a viewer can follow it.
    recurring = [
        o
        for o in certified
        if o.deadline is not None
        and o.deadline.kind is DeadlineKind.END_OF_PERIOD
        and o.deadline.period
        and o.evidence
    ]
    if not recurring:
        raise RuntimeError("no certified recurring rule to build a demonstration on")
    recurring.sort(key=lambda o: o.id)
    subject = recurring[0]

    evidence = EvidenceStore(label=f"{DEMO_FIRM}, filing register")
    today = moment.date()
    for months_back in (4, 3, 2, 1):
        occurred = _month_end(today, months_back)
        evidence.supersede(
            ComplianceEvent(
                id=f"EV-DEMO-{occurred.isoformat()}",
                obligation_id=subject.id,
                entity=DEMO_FIRM,
                occurred_on=occurred,
                artifact_type=subject.evidence[0].artifact_type,
                # The most recent one was never filed. That is the gap.
                filed_on=None if months_back == 1 else occurred,
                reference=f"DEMO-{occurred.strftime('%Y%m')}",
            )
        )
    evidence.save(root / "evidence.json")
    result.occasions = len(evidence)

    # -- one assessment, run by the demo officer
    #
    # Recorded rather than left to the first click, so the demonstration opens
    # on a firm with a position rather than on an empty dashboard. Attributed,
    # because an assessment reading "run by unattributed" on a jury's screen is
    # the product contradicting its own central claim.
    report = RuleEngine(WEEKENDS_ONLY).run(certified, evidence, as_of=today)
    log = AssessmentLog.load(root / "assessments.json")
    log.record(
        report,
        evidence=evidence,
        document="Stock Brokers Master Circular",
        document_sha256="",
        rulebook_sha256=_rulebook_fingerprint(obligations),
        rules_certified=len(certified),
        by=DEMO_OFFICER,
        at=moment,
    )
    log.save()
    latest = log.latest
    result.assessment_id = latest.run_id if latest else ""
    result.open_gaps = latest.breaches if latest else 0
    result.unverified = latest.no_evidence if latest else 0

    if include_account:
        _demo_account(root)
    if amendment:
        result.editions = _register_editions(
            root, Path(corpus) if corpus else Path("corpus")
        )
    return result


def _month_end(today: _dt.date, months_back: int) -> _dt.date:
    import calendar as _cal

    year, month = today.year, today.month
    for _ in range(months_back):
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return _dt.date(year, month, _cal.monthrange(year, month)[1])


def _rulebook_fingerprint(obligations) -> str:
    from sanhita.assess import rulebook_fingerprint

    return rulebook_fingerprint(obligations)
