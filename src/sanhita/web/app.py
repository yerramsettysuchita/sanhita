"""The certification workbench.

A local FastAPI app over the same store the CLI writes. No build step, no npm,
no CDN. Every asset is served from disk so the whole thing runs offline in a
room with no network.

Four screens, one spine: the source clause on the left exactly as SEBI published
it, the compiled artifact on the right. The workbench adds the interaction the
product turns on. Hover a compiled field, see the words that justified it light
up in the regulation.

**Anyone can bring their own document.** A workspace is one person's copy of the
whole pipeline: their PDF, their parsed tree, their proposed rules, their
certifications, their ledger. Every screen is scoped to one. The built-in
workspace holds the stock broker master circular as a worked example, so a first
time visitor has something real to look at before uploading anything.

Two rules hold everywhere:

  Nothing on any screen is fabricated. Every figure is read from the store or
  the parsed tree. Where a value does not exist, the screen shows an empty
  state rather than a placeholder. That includes the parse itself: a document
  Sanhita cannot read says so, instead of producing a thin rulebook that looks
  fine until an auditor opens it.

  There is no search box, no question box, and no chat. This is an audit
  surface, not an oracle.
"""

from __future__ import annotations

import contextvars
import datetime as _dt
import logging
import os
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from sanhita import CIRCULAR_ID
from sanhita.auth import AuthError, UserStore
from sanhita.auth import session as _session
from sanhita.certify.lifecycle import CertificationError, RuleRegistry
from sanhita.ir.enums import DayCount, RuleStatus
from sanhita.ir.schema import Deadline, Obligation, UnresolvedFieldError
from sanhita.metrics.coverage import classify_clause, compute_coverage
from sanhita.parse.clause_tree import ClauseTree, parse_clause_tree
from sanhita.parse.quality import ParseQuality, assess
from sanhita.web.highlight import segment_text
from sanhita.web.jobs import JobRunner
from sanhita.workspace import (
    BUILTIN_ID,
    MAX_UPLOAD_BYTES,
    RateLimited,
    RateLimiter,
    UploadRejected,
    Workspace,
    WorkspaceStore,
)

logger = logging.getLogger("sanhita.web")

HERE = Path(__file__).parent
TEMPLATES = HERE / "templates"
STATIC = HERE / "static"


def _find_ui() -> Path:
    """Locate ui/, which holds tokens.css and the fonts.

    Three-deep from this file lands on the repository root in a source
    checkout and on site-packages in an installed one, so the original
    hard-coded relative path found nothing once the package was installed.
    That failed quietly: the mount is guarded by ``is_dir()``, so the app
    started fine and served every page without its typeface or a single design
    token. A silently unstyled deployment is worse than one that refuses to
    boot, because nobody notices until a reviewer opens it.
    """
    candidates = [
        HERE.parent.parent.parent / "ui",   # source checkout
        Path.cwd() / "ui",                  # container WORKDIR
        HERE.parent.parent / "ui",          # installed alongside the package
    ]
    for candidate in candidates:
        if (candidate / "tokens.css").is_file():
            return candidate
    return candidates[0]


UI = _find_ui()

_KEY_ENV = "SANHITA_SIGNING_KEY"

#: How many parsed documents to hold in memory at once. Each is a full clause
#: tree, so this is the difference between a bounded process and one that grows
#: with every document anyone opens.
CACHE_LIMIT = int(os.environ.get("SANHITA_CACHE_LIMIT", "6"))

#: Rules rendered per page of the queue.
PAGE_SIZE = 50

#: The screens that belong to a firm rather than to a regulation.
#
# The product has two audiences. A compliance officer at an intermediary walks
# these five, asking whether their own firm is complying. A regulatory analyst
# works on the rulebook itself, and those screens are genuinely about a
# document. The masthead names whichever of the two the current screen serves.
#
# The chain view renders under `remediation`, so it is covered by that entry.
COMPANY_PAGES = frozenset({"company", "review", "gaps", "remediation", "audit"})

#: Names the opaque handle that keeps one visitor's company data away from the
#: next visitor's on a shared deployment. Holds a random token and nothing else.
VISITOR_COOKIE = "sanhita_visitor"

#: Whose company data the request in flight may read and write. Empty on a
#: single-user install, which is why a laptop and the test suite see plain
#: filenames and nothing about this exists for them. Set once per request by the
#: middleware, read by :func:`_sidecar`. A context variable rather than a
#: parameter because every sidecar read in a request must agree, and threading
#: the scope through forty call sites is forty chances to forget one.
_SCOPE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "sanhita_visitor_scope", default=""
)


@dataclass
class Workbench:
    """One workspace, parsed and loaded.

    Parsing a 399 page PDF is not free, so a workbench is built on first use and
    then held. The registry is re-read from disk whenever it is written, since a
    compile job running on a worker thread writes the same file.
    """

    workspace: Workspace
    tree: ClauseTree
    registry: RuleRegistry
    quality: ParseQuality
    loaded_at: _dt.datetime
    #: Where the firm's own profile lives, above every rulebook rather than
    #: inside one. Carried on the workbench so any code holding a state can
    #: reach the firm without knowing which circular it arrived through.
    company_root: Path | None = None

    @property
    def pdf(self) -> Path:
        return self.workspace.pdf_path

    @property
    def store_path(self) -> Path:
        return self.workspace.store_path

    @property
    def circular_id(self) -> str:
        return CIRCULAR_ID if self.workspace.builtin else self.workspace.id

    def reload_registry(self) -> None:
        from sanhita.cli_compile import _load_registry

        self.registry = _load_registry(self.store_path)

    def save(self) -> None:
        from sanhita.cli_compile import _save_registry

        _save_registry(
            self.registry,
            circular_id=self.circular_id,
            fingerprint=self.tree.fingerprint(),
            path=self.store_path,
        )


def create_app(pdf: Path, *, store: Path | None = None) -> FastAPI:
    from sanhita.cli_compile import STORE, _load_registry

    app = FastAPI(title="Sanhita, SEBI Compliance", docs_url=None, redoc_url=None)

    builtin_store = store or STORE
    workspaces = WorkspaceStore(
        root=builtin_store.parent / "workspaces",
        builtin_pdf=pdf,
        builtin_store=builtin_store,
    )
    jobs = JobRunner()
    limiter = RateLimiter()
    #: Sign-in is throttled separately from upload: the two are different kinds
    #: of abuse and a person who mistypes a password should not lose their
    #: ability to upload.
    signin_limiter = RateLimiter(limit=8, window_seconds=300)
    users = UserStore(builtin_store.parent / "users.json")
    #: The firm lives above every rulebook, because it is the root object.
    #
    # It used to be a file inside a workspace, which made a company a property
    # of a circular. That is backwards: a stock broker that also runs a research
    # arm is one firm held to two rulebooks, and the old shape could not say so.
    # The profile now sits beside the store root and each rulebook is something
    # the firm declares applies to it.
    company_root = builtin_store.parent
    _cache: OrderedDict[str, Workbench] = OrderedDict()

    app.state.workspaces = workspaces
    app.state.jobs = jobs
    app.state.limiter = limiter
    app.state.users = users
    app.state.bench_cache = _cache

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    if UI.is_dir():
        # tokens.css and the woff2 files live in ui/, shared with later phases.
        app.mount("/ui", StaticFiles(directory=UI), name="ui")

    templates = Jinja2Templates(directory=str(TEMPLATES))
    templates.env.filters["pct"] = lambda v: f"{v:.1%}"

    from sanhita.analyse.latency import humanise as _humanise

    templates.env.filters["duration"] = _humanise

    #: Gold set scoring, memoised per parse tree.
    #:
    #: ``run_eval`` re-extracts every gold clause and re-scores it. It is
    #: deterministic and depends only on the tree, so running it again on the
    #: same tree can only produce the same answer. Coverage and Facts both call
    #: it on every request, which put half a second of pure recomputation into
    #: two of the most visited pages for no gain at all.
    _eval_cache: dict[str, object] = {}

    def scored(state: Workbench):
        """The gold set result for this document, computed at most once."""
        from sanhita.compile.extract import RuleExtractor
        from sanhita.eval.harness import run_eval

        key = state.tree.fingerprint()
        cached = _eval_cache.get(key)
        if cached is None:
            cached = run_eval(state.tree, RuleExtractor(circular_id=CIRCULAR_ID))
            _eval_cache[key] = cached
        return cached

    def asset_version() -> str:
        """A token that changes whenever a stylesheet or script does.

        Appended to every asset URL so the browser fetches the new file instead
        of a cached one. Without it, a CSS change is invisible until someone
        thinks to hard-refresh, and the natural conclusion is that the change
        did not work.
        """
        newest = 0.0
        for name in ("static/app.css", "static/app.js", "static/landing.css"):
            path = HERE / name
            if path.is_file():
                newest = max(newest, path.stat().st_mtime)
        tokens = UI / "tokens.css"
        if tokens.is_file():
            newest = max(newest, tokens.stat().st_mtime)
        return str(int(newest))

    templates.env.globals["asset_v"] = asset_version()

    # ------------------------------------------------------- workspace access

    def bench(wid: str, request: Request | None = None) -> Workbench:
        """Resolve a workspace to a loaded workbench, parsing it on first use.

        When a request is given, ownership is checked before anything is
        loaded. A document nobody may open answers 404 rather than 403, so the
        response does not confirm that the id exists.
        """
        if request is not None:
            found = workspaces.get(wid)
            signed_in = current_user(request)
            if found is not None and not workspaces.may_open(
                found, signed_in.id if signed_in else None
            ):
                raise HTTPException(404, f"No document {wid!r}.")

        cached = _cache.get(wid)
        if cached is not None:
            _cache.move_to_end(wid)  # most recently used
            return cached

        workspace = workspaces.get(wid)
        if workspace is None:
            raise HTTPException(404, f"No document {wid!r}. It may have been deleted.")
        if not workspace.pdf_path.is_file():
            raise HTTPException(410, "The source PDF for this document is missing.")

        tree = parse_clause_tree(workspace.pdf_path)
        loaded = Workbench(
            workspace=workspace,
            tree=tree,
            registry=_load_registry(workspace.store_path),
            quality=assess(tree),
            loaded_at=_dt.datetime.now(_dt.timezone.utc),
            company_root=company_root,
        )
        _cache[wid] = loaded

        # A parsed tree of a 399 page circular is not small, and without a bound
        # every document anyone ever opens stays in memory for the life of the
        # process. Evict the least recently used, but never the worked example:
        # it is what every visitor lands on, so evicting it guarantees a re-parse
        # on the next request.
        while len(_cache) > CACHE_LIMIT:
            for candidate in list(_cache):
                if candidate != BUILTIN_ID:
                    _cache.pop(candidate, None)
                    break
            else:  # pragma: no cover - only the built-in is cached
                break
        return loaded

    # ------------------------------------------------------------------ auth

    def current_user(request: Request):
        """Whoever this request is signed in as, or None.

        Auth is off until somebody creates an account, so a first run needs no
        sign-in at all. Once accounts exist, setting SANHITA_REQUIRE_AUTH=1
        closes the app to anyone who is not signed in.
        """
        user_id = _session.read(request.cookies.get(_session.COOKIE_NAME))
        return users.get(user_id) if user_id else None

    def auth_required() -> bool:
        return os.environ.get("SANHITA_REQUIRE_AUTH", "").strip() not in ("", "0", "false")

    def shared_deployment() -> bool:
        """Whether strangers can reach this instance.

        The app cannot work this out for itself. A laptop and a public URL look
        identical from inside the process, so it is a deployment setting, and
        the deployment that is public sets it. See ``fly.toml``.
        """
        return os.environ.get("SANHITA_SHARED", "").strip() not in ("", "0", "false")

    def visitor_scope(request: Request) -> str:
        """Whose company data this request is allowed to see.

        The worked example is one workspace that everybody lands on, and the
        journey it demonstrates ends with a firm uploading its own compliance
        records. On a laptop that is fine. On a public URL it means one
        visitor's filing register, with their firm's name on it, is served to
        the next visitor, which would be a privacy breach dressed as a demo.

        So on a shared deployment the firm's own data is kept per visitor.
        Signed in, it is keyed to the account. Not signed in, it is keyed to an
        opaque cookie with nothing in it but a random token, so the jury can
        walk the whole journey without creating an account and still not see
        anybody else's evidence.

        The rulebook is not scoped. That is the regulator's text and is the same
        document for everyone.
        """
        if not shared_deployment():
            return ""
        signed_in = current_user(request)
        if signed_in is not None:
            return f"u{signed_in.id}"
        return request.cookies.get(VISITOR_COOKIE, "")

    @app.middleware("http")
    async def gate(request: Request, call_next):
        """Close the app when auth is required, then harden every response."""
        open_paths = ("/signin", "/signup", "/static", "/ui", "/healthz")
        if auth_required() and not request.url.path.startswith(open_paths):
            if current_user(request) is None:
                return RedirectResponse("/signin", status_code=303)

        # Decide who this is before the route runs, so every sidecar read and
        # write in the request sees the same answer.
        scope = visitor_scope(request)
        minted = ""
        if shared_deployment() and not scope:
            minted = scope = _mint_visitor_token()
        token = _SCOPE.set(scope)
        try:
            response = await call_next(request)
        finally:
            _SCOPE.reset(token)

        if minted:
            response.set_cookie(
                VISITOR_COOKIE,
                minted,
                max_age=60 * 60 * 24 * 30,
                httponly=True,
                samesite="lax",
                secure=request.url.scheme == "https",
            )

        # The certify button is a one-click irreversible action taken by a named
        # person. Framing this app inside another site would let somebody else's
        # page position an invisible copy of it under a cursor, so framing is
        # refused outright rather than restricted to same-origin.
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        # The whole product is served from disk with no CDN and no third party,
        # so the policy can be as tight as 'self'. 'unsafe-inline' covers the
        # small amount of page-local script and the inline style attributes the
        # progress bars use; removing it means moving those to CSS custom
        # properties, which is worth doing but is not a one-line change.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "form-action 'self'; "
            "base-uri 'none'; "
            "frame-ancestors 'none'; "
            "object-src 'none'"
        )
        return response

    def shell(state: Workbench, nav: str, request: Request | None = None, **extra) -> dict:
        """The context every page needs to render the masthead and its links."""
        job = jobs.get(state.workspace.id)
        user = current_user(request) if request is not None else None
        firm = _company(state)
        setup_step = _setup_step(state, firm)
        return {
            "nav": nav,
            "wid": state.workspace.id,
            "base": f"/w/{state.workspace.id}",
            "workspace": state.workspace,
            "quality": state.quality,
            "documents": workspaces.visible_to(user.id if user else None),
            "job": job.to_json() if job else None,
            "user": user,
            "any_users": users.any_users,
            "journey_reached": _journey_reached(state),
            # Who the firm's screens are about, and which screens those are.
            #
            # Decided once, here, rather than by each template, because the
            # thing being corrected is a single question the whole product has
            # to answer the same way: is this page about a firm or about a
            # regulation. The masthead said DOCUMENT everywhere, so a firm's own
            # compliance screen introduced itself as a rulebook.
            "firm": firm,
            "company_context": nav in COMPANY_PAGES,
            # Onboarding and the ongoing lifecycle are different modes and
            # must never be on screen together: "Step 1 of 3" above "Stage 1
            # of 5" leaves somebody unable to say which journey they are on.
            "setup_step": setup_step,
            **extra,
        }

    def _current_assessment(state: Workbench):
        """The recorded run matching the rulebook and records in front of us.

        None when the firm has never been assessed, or when either input has
        changed since it last was. Both cases mean the same thing for anything
        that wants to act on a finding: there is no finding of record yet.
        """
        from sanhita.assess import evidence_fingerprint, rulebook_fingerprint

        evidence, _ = _evidence_for(state)
        if evidence is None:
            return None
        obligations = state.registry.all_current()
        inputs = (rulebook_fingerprint(obligations), evidence_fingerprint(evidence))
        for run in reversed(_assessments(state).runs):
            if run.inputs() == inputs:
                return run
        return None

    def _worklist(state: Workbench):
        """What this firm should do first, for a team that cannot do everything.

        Built from the remediation tasks and the evidence health this firm
        already has, rather than from a store of its own, so it can never
        disagree with the screens it summarises.
        """
        from sanhita.priority import rank_open_work

        return rank_open_work(
            tasks=_remediation(state).open_tasks(),
            health=_evidence_health(state),
            base=f"/w/{state.workspace.id}",
        )

    def _regulatory_watch(state: Workbench, firm, request: Request):
        """Whether a later edition of this firm's rulebooks is sitting unread.

        Asked on the firm's behalf rather than waiting for somebody to open the
        comparison screen, because a regulatory change happens on SEBI's
        calendar and not on the compliance officer's.

        It watches the documents brought to this installation and nothing else.
        Every screen that shows it says so, because a firm that believed this
        polled sebi.gov.in would read silence as cover.
        """
        from sanhita.monitor import watch_for_firm
        from sanhita.remediate import RemediationStore

        if firm is None or not firm.frameworks:
            return None

        def tasks_for(workspace_id: str):
            space = workspaces.get(workspace_id)
            if space is None:
                return []
            # The tasks live beside the edition they were raised on, so they
            # are read from there rather than parsed out of anything.
            scope = _SCOPE.get()
            name = "remediation.json" if not scope else f"remediation.{scope}.json"
            return list(
                RemediationStore.load(space.store_path.with_name(name)).tasks.values()
            )

        certified = sum(
            1 for o in state.registry.all_current() if o.status is RuleStatus.CERTIFIED
        )
        return watch_for_firm(
            firm=firm.name,
            in_use_id=state.workspace.id,
            in_use_name=state.workspace.name,
            in_use_issued_on=state.workspace.issued_on,
            in_use_fingerprint=state.tree.fingerprint(),
            certified_in_use=certified,
            candidates=_framework_rows(state, firm, request),
            tasks_for=tasks_for,
        )

    def _evidence_health(state: Workbench, queue=None):
        """Whether this firm's records are still arriving.

        A recorded assessment is a photograph: it says where the firm stood
        against the records it had that day, and nothing about the four months
        since. This is the other question, and it is the one that goes wrong in
        practice, because a register uploaded once during onboarding looks
        exactly like a register kept up to date.
        """
        from sanhita.health import assess_evidence_health

        evidence, imported = _evidence_for(state)
        if not imported:
            return None
        recorded = _current_assessment(state)
        latest = _assessments(state).latest
        return assess_evidence_health(
            state.registry.all_current(),
            evidence,
            controls=_controls(state),
            awaiting_mapping=len(queue.awaiting()) if queue is not None else 0,
            assessed_on=latest.ran_at if latest is not None else None,
            # Stale means a run exists but its inputs have moved, which is
            # already how the overview decides whether to show a position.
            assessment_is_stale=latest is not None and recorded is None,
        )

    def _setup_step(state: Workbench, firm) -> int:
        """Which onboarding step this firm is on, or 0 when onboarding is done.

        Three answers make a firm ready: who it is, which rulebooks govern it,
        and what records it has. The product used to check only the first two,
        so saving a framework dropped somebody straight onto the dashboard and
        the third step never existed as a screen.

        Onboarding ends when somebody presses the button and not before.
        Uploading a file used to end it, which meant dropping a document on
        step three reloaded the page into the dashboard and the step the user
        was standing on vanished under them mid-action. Records existing is
        evidence that a firm is working, not a decision that it has finished.
        """
        if firm is None:
            return 1
        if not firm.frameworks:
            return 2
        if firm.setup_completed_at is not None:
            return 0
        # Uploading is not finishing.
        #
        # This used to return 0 as soon as any record existed, so dropping a
        # file on step three reloaded the page into the dashboard and the step
        # the user was on vanished under them mid-action. Only the explicit
        # control finishes setting up, which is what makes it a step rather
        # than a side effect.
        return 3

    def _journey_reached(state: Workbench) -> set[str]:
        """Which stages this firm has actually completed.

        Derived from what is on disk rather than from a stored step counter. A
        counter would say a firm had finished stage two after it deleted every
        record it had uploaded, and a progress indicator that lies is worse than
        none. Each stage is complete when the thing that stage produces exists.
        """
        done: set[str] = set()
        firm = _company(state)
        if firm is not None and firm.frameworks:
            done.add("company")
        if _review(state).summary()["mapped"]:
            done.add("review")
        if _assessments(state).latest is not None:
            done.add("gaps")
        remediation = _remediation(state)
        if remediation.all() and not remediation.open_tasks():
            done.add("remediation")
        return done

    def page(path: str):
        """Register a GET at both the workspace-scoped and the legacy path.

        Every link the templates emit is workspace-scoped. The bare paths stay
        registered because they are what the CLI prints, what earlier bookmarks
        point at, and what the test suite asks for. They resolve to the built-in
        workspace.
        """

        def deco(fn):
            app.get("/w/{wid}" + path, response_class=HTMLResponse)(fn)
            app.get(path, response_class=HTMLResponse)(fn)
            return fn

        return deco

    def action(path: str):
        """The same, for POSTs."""

        def deco(fn):
            app.post("/w/{wid}" + path)(fn)
            app.post(path)(fn)
            return fn

        return deco

    # ------------------------------------------------------------- helpers

    def queue_rows(
        state: Workbench,
        *,
        section: str | None = None,
        status: str | None = None,
        unresolved_only: bool = False,
        max_confidence: float | None = None,
    ) -> list[dict]:
        rows: list[dict] = []
        for obligation in state.registry.all_current():
            node = state.tree.get(obligation.source.clause_id)
            issues = obligation.blocking_issues()
            row = {
                "id": obligation.id,
                "clause_id": obligation.source.clause_id,
                "section": obligation.source.section,
                "page": obligation.source.page,
                "status": obligation.status.value,
                "actor": obligation.actor.value,
                "modality": obligation.modality.value,
                "confidence": obligation.confidence,
                "issues": issues,
                "issue_count": len(issues),
                "title": (node.title or node.text[:90]) if node else "",
                "engine": obligation.extraction.engine if obligation.extraction else "none",
            }
            if section and row["section"] != section:
                continue
            if status and row["status"] != status:
                continue
            if unresolved_only and not issues:
                continue
            if max_confidence is not None and row["confidence"] > max_confidence:
                continue
            rows.append(row)
        rows.sort(key=lambda r: (r["section"].zfill(4), r["clause_id"]))
        return rows

    def ordered_queue(state: Workbench) -> list[str]:
        return [r["clause_id"] for r in queue_rows(state)]

    # -------------------------------------------------------------- routes

    @app.get("/", response_class=HTMLResponse)
    def landing(request: Request):
        """The marketing surface.

        Deliberately static and self-contained: it makes claims about the
        product, so it must not depend on whatever happens to be in the store.
        Every figure on it is a fact about the corpus and the test suite, not a
        live count that could read zero on a fresh checkout.
        """
        return templates.TemplateResponse(
            request,
            "landing.html",
            {"nav": "home", "user": current_user(request)},
        )

    @page("/queue")
    def queue(
        request: Request,
        wid: str = BUILTIN_ID,
        section: str | None = None,
        status: str | None = None,
        unresolved: int = 0,
        low_confidence: int = 0,
        limit: int = 0,
        page: int = 1,
    ):
        state = bench(wid, request)
        matched = queue_rows(
            state,
            section=section,
            status=status,
            unresolved_only=bool(unresolved),
            max_confidence=0.75 if low_confidence else None,
        )
        # The counts above the list are always the true totals; only the
        # rendered rows are paged, so the page stays responsive at 1,377 rules
        # without ever showing a number that is not real.
        #
        # Paged rather than capped with a "show all": rendering 1,377 cards into
        # one document worked, but it produced a 193 KB page that a reviewer
        # then had to scroll through with no way back to where they were.
        total_matched = len(matched)
        per_page = PAGE_SIZE if limit <= 0 else max(1, min(limit, 200))
        pages = max(1, -(-total_matched // per_page))  # ceiling division
        page_no = max(1, min(page, pages))
        start = (page_no - 1) * per_page
        rows = matched[start : start + per_page]
        everything = state.registry.all_current()
        counts = {
            "total": len(everything),
            "proposed": sum(1 for o in everything if o.status is RuleStatus.PROPOSED),
            "certified": sum(1 for o in everything if o.status is RuleStatus.CERTIFIED),
            "rejected": sum(1 for o in everything if o.status is RuleStatus.REJECTED),
            "superseded": sum(1 for o in everything if o.status is RuleStatus.SUPERSEDED),
            "blocked": sum(1 for o in everything if o.blocking_issues()),
        }

        # Work lanes. The queue's job is to answer "what should I do next",
        # so the screen leads with the rules that are actually waiting on a
        # human rather than with an undifferentiated list of everything.
        pending = [o for o in everything if o.status is RuleStatus.PROPOSED]
        lanes = {
            "blocked": [o for o in pending if o.blocking_issues()],
            "low_confidence": [
                o for o in pending if not o.blocking_issues() and o.confidence < 0.75
            ],
            "ready": [
                o for o in pending if not o.blocking_issues() and o.confidence >= 0.75
            ],
        }
        lane_counts = {k: len(v) for k, v in lanes.items()}
        first_blocked = lanes["blocked"][0].source.clause_id if lanes["blocked"] else None
        progress = (
            round(counts["certified"] / counts["total"] * 100, 1) if counts["total"] else 0.0
        )
        sections = sorted(
            {o.source.section for o in everything}, key=lambda s: int(s) if s.isdigit() else 9999
        )
        return templates.TemplateResponse(
            request,
            "queue.html",
            shell(
                state,
                "queue",
                request,
                rows=rows,
                counts=counts,
                lane_counts=lane_counts,
                first_blocked=first_blocked,
                progress=progress,
                sections=sections,
                total_matched=total_matched,
                shown=len(rows),
                limit=limit,
                page=page_no,
                pages=pages,
                per_page=per_page,
                first_shown=start + 1 if rows else 0,
                last_shown=start + len(rows),
                filters={
                    "section": section,
                    "status": status,
                    "unresolved": unresolved,
                    "low_confidence": low_confidence,
                },
            ),
        )

    @page("/clause/{clause_id}")
    def workbench(request: Request, clause_id: str, wid: str = BUILTIN_ID):
        state = bench(wid, request)
        node = state.tree.get(clause_id)
        if node is None:
            raise HTTPException(404, f"No clause {clause_id}")

        obligations = [
            o for o in state.registry.all_current() if o.source.clause_id == clause_id
        ]
        obligations.sort(key=lambda o: o.id)

        # The left pane is segmented against the first obligation's spans; the
        # others get their own segmentation so switching between them re-lights
        # the clause correctly.
        controls = _controls(state)
        views = []
        for obligation in obligations:
            views.append(
                {
                    "rule": obligation,
                    "segments": segment_text(node.text, obligation.field_provenance),
                    "issues": obligation.blocking_issues(),
                    "fields": _field_rows(obligation),
                    "history": state.registry.ledger.for_obligation(obligation.id),
                    # Who inside the firm discharges this. Held in a sidecar, so
                    # recording one never touches the signed bytes.
                    "binding": controls.get(obligation.id),
                }
            )

        order = ordered_queue(state)
        try:
            position = order.index(clause_id)
        except ValueError:
            position = -1

        return templates.TemplateResponse(
            request,
            "workbench.html",
            shell(
                state,
                "queue",
                request,
                node=node,
                views=views,
                clause_id=clause_id,
                prev_clause=order[position - 1] if position > 0 else None,
                next_clause=order[position + 1] if 0 <= position < len(order) - 1 else None,
                position=position + 1,
                queue_size=len(order),
                classification=classify_clause(node).value,
                signing_key_present=bool(os.environ.get(_KEY_ENV)),
            ),
        )

    @action("/clause/{clause_id}/bind")
    def bind_control(
        request: Request,
        clause_id: str,
        wid: str = BUILTIN_ID,
        obligation_id: str = Form(...),
        function: str = Form(""),
        process: str = Form(""),
        system: str = Form(""),
        control_ref: str = Form(""),
        by: str = Form(""),
    ):
        """Record which part of the firm owns this obligation.

        Deliberately not a certification. A binding says how one firm has
        chosen to organise itself, which is a different kind of claim from
        what the regulation says, and two firms can bind the same rule to
        different teams and both be right. It is written to a sidecar file and
        never enters the signing payload.
        """
        state = bench(wid, request)
        if state.registry.current(obligation_id) is None:
            raise HTTPException(404, f"No rule {obligation_id} in this document.")

        # A binding is not a certification, but it is a record of how a firm
        # says it is organised, and a gap report cites it by name. Somebody has
        # to stand behind it.
        actor = _acting_officer(request, "Recording who owns a duty")
        controls = _controls(state)
        try:
            if function.strip():
                controls.bind(
                    obligation_id,
                    function=function,
                    process=process,
                    system=system,
                    control_ref=control_ref,
                    bound_by=actor,
                )
            else:
                controls.unbind(obligation_id)
            controls.save()
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return RedirectResponse(f"/w/{wid}/clause/{quote(clause_id)}", status_code=303)

    @action("/clause/{clause_id}/resolve")
    def resolve(
        request: Request,
        clause_id: str,
        wid: str = BUILTIN_ID,
        obligation_id: str = Form(...),
        business_days: str = Form(...),
        by: str = Form(""),
    ):
        """Record a human's answer to a question the clause did not settle."""
        state = bench(wid, request)
        officer = _acting_officer(request)
        current = state.registry.current(obligation_id)
        if current is None or current.deadline is None:
            raise HTTPException(404, "no such obligation, or it carries no deadline")

        try:
            choice = DayCount(business_days)
        except ValueError as exc:
            raise HTTPException(400, f"unknown day-count {business_days!r}") from exc

        updated = current.deadline.model_copy(update={"business_days": choice})
        state.registry.amend(
            obligation_id,
            {"deadline": updated},
            by=officer,
            level="patch",
            note=f"day-count convention resolved to {choice.value} by a human reviewer",
        )
        state.save()
        return RedirectResponse(f"/w/{wid}/clause/{clause_id}", status_code=303)

    def _acting_officer(request, doing: str = "") -> str:
        """Who is doing this, taken from the session rather than the form.

        The whole product rests on a named human standing behind each recorded
        act, and until this function existed that name was a text box: anybody
        could type "S. Iyer, Chief Compliance Officer" into a ledger entry, and
        the entry would carry it forever with nothing behind it. The signature
        never covered the name either, because a signature cannot cover the
        bytes containing it, so no part of the record was tied to a person.

        Applied to **every state-changing compliance act**, not only
        certification. The audit trail is the product's central claim, and a
        trail where the rule was signed by an authenticated officer but the
        assessment beneath it was run by "unattributed" is a trail with a hole
        in exactly the place an inspector would look. Reading is not gated; a
        visitor can walk the whole product and see everything. Writing a record
        somebody may later be asked to answer for is.

        This does not make the signature cryptographically personal. That would
        need a key per officer rather than one key per deployment, which is a
        real change to the trust model and not something to imply we have done.
        What it does is make the recorded identity the account that was signed
        in, so the name in the ledger is one somebody authenticated as.
        """
        signed_in = current_user(request)
        if signed_in is None:
            act = doing or "Certifying, rejecting and amending a rule"
            raise HTTPException(
                401,
                f"{act} is recorded as an official compliance action, so it "
                "needs an account. The record names who did it, and a name "
                "typed into a box is not a record of anybody.",
            )
        return signed_in.name

    @action("/clause/{clause_id}/certify")
    def certify(
        request: Request,
        clause_id: str,
        wid: str = BUILTIN_ID,
        obligation_id: str = Form(...),
        by: str = Form(""),
        note: str = Form(""),
    ):
        state = bench(wid, request)
        key = os.environ.get(_KEY_ENV)
        if not key:
            raise HTTPException(400, f"{_KEY_ENV} is not set; certification signs over canonical bytes")
        # `by` is still accepted and still ignored. The form no longer sends
        # it; dropping the parameter would turn an old bookmark into a 422
        # rather than a certification by the person who is actually signed in.
        officer = _acting_officer(request)
        try:
            state.registry.certify(obligation_id, by=officer, key=key, note=note or None)
        except (UnresolvedFieldError, CertificationError) as exc:
            raise HTTPException(400, str(exc)) from exc
        state.save()
        return RedirectResponse(
            f"/w/{wid}/clause/{clause_id}?certified={obligation_id}", status_code=303
        )

    @action("/clause/{clause_id}/reject")
    def reject(
        request: Request,
        clause_id: str,
        wid: str = BUILTIN_ID,
        obligation_id: str = Form(...),
        by: str = Form(""),
        reason: str = Form(...),
    ):
        state = bench(wid, request)
        if not reason.strip():
            raise HTTPException(400, "a rejection must carry a reason")
        officer = _acting_officer(request)
        try:
            state.registry.reject(obligation_id, by=officer, reason=reason.strip())
        except CertificationError as exc:
            raise HTTPException(400, str(exc)) from exc
        state.save()
        return RedirectResponse(f"/w/{wid}/clause/{clause_id}", status_code=303)

    @action("/clause/{clause_id}/edit")
    def edit(
        request: Request,
        clause_id: str,
        wid: str = BUILTIN_ID,
        obligation_id: str = Form(...),
        by: str = Form(""),
        verb: str = Form(...),
        object: str = Form(...),
        offset_days: str = Form(""),
        note: str = Form(""),
    ):
        """Edit creates a NEW version. It never mutates the existing one."""
        state = bench(wid, request)
        officer = _acting_officer(request)
        current = state.registry.current(obligation_id)
        if current is None:
            raise HTTPException(404, "no such obligation")

        edits: dict = {}
        if verb.strip() != current.action.verb or object.strip() != current.action.object:
            edits["action"] = current.action.model_copy(
                update={"verb": verb.strip(), "object": object.strip()}
            )
        if offset_days.strip() and current.deadline is not None:
            try:
                days = int(offset_days)
            except ValueError as exc:
                raise HTTPException(400, "offset_days must be a whole number") from exc
            if days != current.deadline.offset_days:
                edits["deadline"] = current.deadline.model_copy(update={"offset_days": days})
        if not edits:
            return RedirectResponse(f"/w/{wid}/clause/{clause_id}", status_code=303)

        state.registry.amend(obligation_id, edits, by=officer, note=note or None)
        state.save()
        return RedirectResponse(f"/w/{wid}/clause/{clause_id}", status_code=303)

    @page("/coverage")
    def coverage(
        request: Request,
        wid: str = BUILTIN_ID,
        section: str | None = None,
        as_of: str | None = None,
    ):
        state = bench(wid, request)

        # Point in time. After an incident the question is not what you believe
        # today, it is what you believed then, and the answer here is a replay
        # of a hash-chained ledger rather than a recollection.
        rules = state.registry.all_current()
        as_of_at = None
        if as_of:
            try:
                as_of_at = _dt.datetime.fromisoformat(as_of).replace(
                    tzinfo=_dt.timezone.utc
                )
                rules = state.registry.as_of(as_of_at)
            except ValueError:
                as_of_at = None
        # The gold set was written against the built-in circular, so scoring the
        # classifier on someone else's document would report an accuracy that
        # means nothing. Better to say we have not measured it.
        accuracy = None
        if state.workspace.builtin:
            accuracy = scored(state).classifier_accuracy
        report = compute_coverage(
            state.tree,
            rules,
            classifier_accuracy=accuracy,
        )
        rows = sorted(
            report.by_section.values(),
            key=lambda s: int(s.section) if s.section.isdigit() else 9999,
        )

        # "Where should I start." A page of 98 rows all reading 0.0% tells a
        # reviewer nothing they can act on, so the screen names the one section
        # that would move the number most and links straight into it.
        candidates = [s for s in rows if s.obligation_bearing > s.covered]
        best = max(candidates, key=lambda s: s.obligation_bearing - s.covered, default=None)
        start_here = None
        if best is not None:
            remaining = best.obligation_bearing - best.covered
            start_here = {
                "section": best.section,
                "remaining": remaining,
                "gain": round(remaining / report.obligation_bearing_clauses * 100, 1)
                if report.obligation_bearing_clauses
                else 0.0,
            }

        # Sections worth showing first: biggest, and still incomplete.
        ranked = sorted(
            rows,
            key=lambda s: (s.obligation_bearing - s.covered, s.obligation_bearing),
            reverse=True,
        )

        drill = None
        if section:
            node_ids = {
                n.id
                for n in state.tree.nodes.values()
                if n.section == section and classify_clause(n).in_denominator
            }
            covered = {
                o.source.clause_id
                for o in state.registry.all_current()
                if o.status is RuleStatus.CERTIFIED
            }
            proposed = {
                o.source.clause_id
                for o in state.registry.all_current()
                if o.status is RuleStatus.PROPOSED
            }
            drill = {
                "section": section,
                "uncovered": sorted(
                    (
                        {
                            "clause_id": cid,
                            "state": "proposed" if cid in proposed else "not compiled",
                            "title": (state.tree.get(cid).title or state.tree.get(cid).text[:80]),
                        }
                        for cid in node_ids - covered
                    ),
                    key=lambda r: r["clause_id"],
                ),
            }
        return templates.TemplateResponse(
            request,
            "coverage.html",
            shell(
                state,
                "coverage",
                request,
                report=report,
                rows=rows,
                ranked=ranked,
                start_here=start_here,
                drill=drill,
                as_of=as_of,
                as_of_at=as_of_at,
                ledger_span=(
                    (
                        min(e.at for e in state.registry.ledger),
                        max(e.at for e in state.registry.ledger),
                    )
                    if len(state.registry.ledger)
                    else None
                ),
            ),
        )

    @page("/audit")
    def audit(
        request: Request,
        wid: str = BUILTIN_ID,
        limit: int = 300,
        obligation: str | None = None,
    ):
        state = bench(wid, request)
        everything = list(reversed(list(state.registry.ledger)))
        if obligation:
            everything = [e for e in everything if e.obligation_id == obligation]
        total = len(everything)

        # A raw event stream is unreadable at this scale: 1,377 identical
        # "new to PROPOSED" lines carry no signal. Bulk extractor output is
        # folded into one summary row per batch, and the human decisions, which
        # are the entries anyone actually audits, are listed individually.
        human = [e for e in everything if not e.actor.startswith("extractor:")]
        machine = [e for e in everything if e.actor.startswith("extractor:")]

        batches: dict[tuple, dict] = {}
        for entry in machine:
            key = (entry.at.date(), entry.actor, entry.transition.value)
            batch = batches.setdefault(
                key,
                {
                    "date": entry.at.date(),
                    "actor": entry.actor,
                    "transition": entry.transition.value,
                    "count": 0,
                    "first_seq": entry.sequence,
                    "last_seq": entry.sequence,
                },
            )
            batch["count"] += 1
            batch["first_seq"] = min(batch["first_seq"], entry.sequence)
            batch["last_seq"] = max(batch["last_seq"], entry.sequence)

        entries = human if limit <= 0 else human[:limit]
        return templates.TemplateResponse(
            request,
            "audit.html",
            shell(
                state,
                "audit",
                request,
                entries=entries,
                batches=sorted(batches.values(), key=lambda b: -b["last_seq"]),
                human_total=len(human),
                machine_total=len(machine),
                total_entries=total,
                shown=len(entries),
                obligation=obligation,
                head=state.registry.ledger.head,
                chain_problems=state.registry.ledger.verify_chain(),
                signing_key_present=bool(os.environ.get(_KEY_ENV)),
            ),
        )

    @action("/audit/verify")
    def verify_all(request: Request, wid: str = BUILTIN_ID):
        state = bench(wid, request)
        key = os.environ.get(_KEY_ENV)
        if not key:
            return JSONResponse(
                {"ok": False, "error": f"{_KEY_ENV} is not set"}, status_code=400
            )
        report = state.registry.verify_signatures(key)
        return JSONResponse(
            {
                "ok": report.ok,
                "checked": report.checked,
                "valid": report.valid,
                "tampered": report.tampered,
                "ledger_problems": report.ledger_problems,
            }
        )

    # ═══════════════════════════════════════════════ bring your own document ══

    @app.get("/documents", response_class=HTMLResponse)
    def documents(request: Request, error: str | None = None):
        """The way in: the documents this person may open, and a place to add one.

        Signed out, that is the worked example alone. Signed in, it is the
        worked example plus the documents they brought. One person's circulars
        and the certifications made against them are not another's to read.
        """
        signed_in = current_user(request)
        rows = []
        for ws in workspaces.visible_to(signed_in.id if signed_in else None):
            store_exists = ws.store_path.is_file()
            rows.append(
                {
                    "ws": ws,
                    "compiled": store_exists,
                    "size_kb": (
                        round(ws.pdf_path.stat().st_size / 1024)
                        if ws.pdf_path.is_file()
                        else 0
                    ),
                    "missing": not ws.pdf_path.is_file(),
                    "job": (jobs.get(ws.id).to_json() if jobs.get(ws.id) else None),
                }
            )
        # Split rather than merged. The worked example belongs to nobody and is
        # open to everyone; an uploaded circular belongs to the person who
        # uploaded it and to nobody else. Listing the two together under one
        # heading called "your documents" claimed ownership of something that
        # was never theirs, and hid the fact that a new account genuinely has
        # nothing in it yet.
        return templates.TemplateResponse(
            request,
            "documents.html",
            {
                "nav": "documents",
                "example": [r for r in rows if r["ws"].builtin],
                "mine": [r for r in rows if not r["ws"].builtin],
                "max_mb": round(MAX_UPLOAD_BYTES / (1024 * 1024)),
                "error": error,
                "user": current_user(request),
            },
        )

    @app.post("/documents/upload")
    async def upload(request: Request):
        """Take a PDF as a raw request body.

        Raw bytes rather than a multipart form, so the drop target needs no
        upload library. Two guards before anything touches the disk: a per
        caller rate limit, because parsing a 400 page circular is real CPU, and
        a declared length check, so a huge body is refused before it is read
        into memory rather than after.
        """
        # Upload is the only route that spends real CPU on behalf of a stranger
        # and writes to disk, so it is the one route that always needs a name
        # against it. Reading the worked example still needs no account.
        signed_in = current_user(request)
        if signed_in is None:
            return JSONResponse(
                {
                    "ok": False,
                    "needs_account": True,
                    "error": (
                        "Uploading a circular needs an account. The documents you "
                        "bring and the certifications made against them are "
                        "confidential, so they are kept under a named person."
                    ),
                },
                status_code=401,
            )

        # Rate limited per account rather than per address, which is the right
        # unit: an office behind one address is many people, and one person on
        # a phone and a laptop is still one person.
        try:
            limiter.check(signed_in.id)
        except RateLimited as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=429)

        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
            limit = MAX_UPLOAD_BYTES / (1024 * 1024)
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"That file is larger than the {limit:.0f} MB limit.",
                },
                status_code=413,
            )

        name = request.headers.get("x-sanhita-filename", "circular.pdf")
        data = await request.body()
        try:
            workspace = workspaces.create(data, filename=name, owner=signed_in.id)
        except UploadRejected as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

        # Parse immediately so the redirect lands on a page that already knows
        # whether this document is readable.
        _cache.pop(workspace.id, None)
        try:
            state = bench(workspace.id)
        except HTTPException as exc:
            return JSONResponse({"ok": False, "error": exc.detail}, status_code=400)

        return JSONResponse(
            {
                "ok": True,
                "id": workspace.id,
                "url": f"/w/{workspace.id}",
                "verdict": state.quality.verdict.value,
                "clauses": state.quality.clauses,
            }
        )

    @app.get("/w/{wid}", response_class=HTMLResponse)
    def document(request: Request, wid: str):
        """What Sanhita could and could not read, before anything is compiled."""
        state = bench(wid, request)
        counts = {
            "total": len(state.registry),
            "certified": sum(
                1 for o in state.registry.all_current() if o.status is RuleStatus.CERTIFIED
            ),
            "proposed": sum(
                1 for o in state.registry.all_current() if o.status is RuleStatus.PROPOSED
            ),
        }
        stats = state.tree.stats
        llm_available = _llm_problem() is None

        # How long this document took to become an operating rulebook. Read
        # from timestamps the pipeline was already writing for provenance, so
        # nothing here is instrumented for the sake of the screen.
        from sanhita.analyse import measure_latency

        latency = measure_latency(
            state.registry.all_current(),
            issued_on=state.workspace.issued_on,
        )
        return templates.TemplateResponse(
            request,
            "document.html",
            shell(
                state,
                "document",
                request,
                counts=counts,
                stats=stats,
                fingerprint=state.tree.fingerprint(),
                llm_available=llm_available,
                llm_problem=_llm_problem(),
                latency=latency,
            ),
        )

    @app.post("/w/{wid}/compile")
    def start_compile(request: Request, wid: str, engine: str = Form("rules")):
        state = bench(wid, request)
        if not state.quality.can_compile:
            raise HTTPException(
                400,
                "This document was not read well enough to compile. "
                "Compiling it would produce rules nobody could certify.",
            )
        if engine == "llm" and _llm_problem():
            raise HTTPException(400, _llm_problem())

        def reloaded(_job):
            state.reload_registry()

        try:
            jobs.start(
                workspace_id=wid,
                tree=state.tree,
                circular_id=state.circular_id,
                store_path=state.store_path,
                engine=engine,
                on_done=reloaded,
            )
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return RedirectResponse(f"/w/{wid}", status_code=303)

    @app.get("/w/{wid}/progress")
    def progress(wid: str):
        job = jobs.get(wid)
        if job is None:
            return JSONResponse({"state": "none"})
        return JSONResponse(job.to_json())

    @app.post("/w/{wid}/cancel")
    def cancel_compile(wid: str):
        job = jobs.get(wid)
        if job is not None:
            job.cancel()
        return RedirectResponse(f"/w/{wid}", status_code=303)

    @app.post("/w/{wid}/delete")
    def delete_document(wid: str):
        job = jobs.get(wid)
        if job is not None and job.running:
            raise HTTPException(409, "A compile is still running for this document.")
        _cache.pop(wid, None)
        if not workspaces.delete(wid):
            raise HTTPException(400, "The worked example cannot be deleted.")
        return RedirectResponse("/documents", status_code=303)

    @page("/gaps")
    def gaps(request: Request, wid: str = BUILTIN_ID, limit: int = 60, demo: int = 0):
        """EXECUTE. Certified rules run against evidence, gaps cited back.

        Deliberately the only screen that produces an accusation, and the only
        one whose every line carries a signature. Nothing here runs on a
        proposed rule.
        """
        import datetime as _d

        from sanhita.execute import WEEKENDS_ONLY, RuleEngine

        state = bench(wid, request)
        obligations = state.registry.all_current()
        certified = [o for o in obligations if o.status is RuleStatus.CERTIFIED]

        # No records, no verdict.
        #
        # This screen used to generate events when a firm had uploaded nothing,
        # so a page headed "where you are out of compliance" could show a named
        # company dozens of breaches it had never earned. The caption admitting
        # the events were generated did not undo the impression the findings
        # made. A person now has to ask for the demonstration explicitly.
        report = None
        evidence, imported = _evidence_for(state)
        demo_mode = bool(demo) and not imported
        if certified and demo_mode:
            evidence = _demo_evidence(state)
        if certified and evidence is not None:
            report = RuleEngine(WEEKENDS_ONLY).run(
                obligations, evidence, as_of=_d.date.today()
            )

        # Whether what is on screen corresponds to an assessment somebody ran.
        #
        # The engine is still executed on every view, because a screen that
        # renders stored numbers is a screen that will one day disagree with
        # the engine. But running it to look at it is not the same act as
        # running it as this firm's assessment of record, and only the second
        # is written down. So the page says which of the two you are reading:
        # a recorded run, or a preview of records that have changed since.
        log = _assessments(state)
        current_inputs = None
        recorded = None
        if report is not None and evidence is not None and not demo_mode:
            from sanhita.assess import evidence_fingerprint, rulebook_fingerprint

            current_inputs = (
                rulebook_fingerprint(obligations),
                evidence_fingerprint(evidence),
            )
            for run in reversed(log.runs):
                if run.inputs() == current_inputs:
                    recorded = run
                    break

        # A finding that names a rule is a finding. A finding that names the
        # team, the system and the procedure behind it is an action. The
        # bindings come from the sidecar, so a gap report gains this without
        # anything in the signed rulebook changing.
        controls = _controls(state)
        # Which findings already have somebody working on them. Offering to
        # raise a second task for a gap already owned is how two people end up
        # fixing one breach and neither is sure the other did.
        remediation = _remediation(state)
        tasks_by_gap = {t.gap_id: t for t in remediation.all()}
        return templates.TemplateResponse(
            request,
            "gaps.html",
            shell(
                state,
                "gaps",
                request,
                report=report,
                tasks_by_gap=tasks_by_gap,
                remediation_summary=remediation.summary(),
                findings=report.ranked()[:limit] if report else [],
                certified_count=len(certified),
                total_rules=len(obligations),
                limit=limit,
                imported=imported,
                demo_mode=demo_mode,
                controls=controls,
                control_coverage=controls.coverage([o.id for o in certified]),
                import_error=request.query_params.get("import_error"),
                import_ok=request.query_params.get("imported"),
                # The assessment this view corresponds to, or None when the
                # records have changed since anybody last ran one.
                recorded=recorded,
                last_run=log.latest,
                can_record=current_inputs is not None,
            ),
        )

    @action("/assess")
    def run_assessment(request: Request, wid: str = BUILTIN_ID, by: str = Form("")):
        """Run the certified rulebook against this firm's records, and record it.

        The assessment is an act, not a page view. It used to happen wherever
        somebody happened to look: the gaps screen ran the engine to draw
        itself, and the run was written down only if the person later opened
        the company page. So a firm could read its own breaches and have no
        record that the assessment ever took place, and the history depended on
        the order somebody clicked in.

        Here it is one thing. Press it, the engine runs, the run is written
        with the hash of both inputs, and the result is what you are then
        shown. Nothing else in the product records an assessment.
        """
        import datetime as _d

        from sanhita.assess import rulebook_fingerprint
        from sanhita.execute import WEEKENDS_ONLY, RuleEngine

        state = bench(wid, request)
        obligations = state.registry.all_current()
        certified = [o for o in obligations if o.status is RuleStatus.CERTIFIED]
        if not certified:
            raise HTTPException(
                400,
                "Nothing has been certified in this rulebook, so there is "
                "nothing to assess this firm against.",
            )

        evidence, _ = _evidence_for(state)
        if evidence is None:
            raise HTTPException(
                400,
                "This firm has not provided any compliance records, so there is "
                "nothing to assess. Upload your evidence first.",
            )

        _require_declared(_company(state), state, "record a position against")

        report = RuleEngine(WEEKENDS_ONLY).run(
            obligations, evidence, as_of=_d.date.today()
        )
        log = _assessments(state)
        if log.record(
            report,
            evidence=evidence,
            document=state.workspace.name,
            document_sha256=state.workspace.doc_sha256,
            rulebook_sha256=rulebook_fingerprint(obligations),
            rules_certified=len(certified),
            by=_acting_officer(request, "Running a compliance assessment"),
        ):
            log.save()
        return RedirectResponse(f"/w/{wid}/gaps", status_code=303)

    @app.post("/w/{wid}/evidence")
    async def upload_evidence(request: Request, wid: str):
        """Replace the generated events with a firm's own filing export."""

        from sanhita.execute.ingest import read_any

        state = bench(wid, request)
        body = await request.body()
        known = {o.id for o in state.registry.all_current()}
        filename = request.headers.get("x-sanhita-filename", "upload.csv")

        # CSV, JSON, XLSX and PDF all arrive here. The reader is chosen by
        # extension and each one decides for itself how much it is willing to
        # conclude. A CSV names its rule outright and becomes evidence. A PDF
        # almost never does, so it produces candidates a person confirms.
        result = read_any(body, filename, known_obligations=known)

        # Everything the document yielded goes into the review queue, including
        # what could not be placed.
        #
        # This used to keep only the candidates that already named a rule and
        # throw the rest away, which meant a real margin report could be read
        # perfectly and achieve nothing at all. A person now has something to
        # work through instead of an upload that silently did nothing.
        queue = _review(state)
        queue.add(result.candidates)
        queue.save()

        company = _company(state)
        label = f"{company.name if company else state.workspace.name}, reviewed evidence"
        before = len(queue.mapped())
        evidence = queue.to_evidence(label)
        # How many of this firm's records replaced an earlier statement about
        # the same occasion. Reported rather than left silent: a correction that
        # clears a breach is exactly the kind of change somebody should be told
        # about, even though the earlier assessment keeps its own hash.
        superseded = max(0, before - len(evidence))
        evidence.save(_evidence_write_path(state))

        summary = queue.summary()
        if not result.candidates:
            return JSONResponse(
                {
                    "ok": False,
                    "error": result.summary(),
                    "rows": [{"line": 0, "problem": p} for p in result.problems[:20]],
                },
                status_code=400,
            )

        return JSONResponse(
            {
                "ok": True,
                "accepted": len(result.ready),
                "awaiting": summary["awaiting"],
                "rejected": len(result.problems),
                "superseded": superseded,
                "candidates": len(result.candidates),
                "format": result.fmt,
                "rows": [{"line": 0, "problem": p} for p in result.problems[:20]],
                # A document that named no rule is not an error. It is the
                # normal case for a PDF, and the answer is the review screen.
                "url": (
                    f"/w/{wid}/review" if summary["awaiting"] else f"/w/{wid}/gaps"
                ),
            }
        )

    # ══════════════════════════════════════════════════════════ company ══
    #
    # The product was organised around the regulation, so the journey started
    # with "bring me a document". The person the problem statement is about
    # starts somewhere else entirely: "I am a stockbroker, am I complying".
    # This is that view, and every figure on it is computed from stored data.

    @page("/company")
    def company_screen(request: Request, wid: str = BUILTIN_ID):
        """Company X's compliance position, in one screen."""
        import datetime as _d

        from sanhita.company import IntermediaryType
        from sanhita.execute import WEEKENDS_ONLY, RuleEngine
        from sanhita.execute.report import Outcome

        state = bench(wid, request)
        company = _company(state)
        rules = state.registry.all_current()
        certified = [o for o in rules if o.status is RuleStatus.CERTIFIED]

        queue = _review(state)
        controls = _controls(state)
        remediation = _remediation(state)

        # No assessment until this firm has actually given us something.
        #
        # This screen used to run the engine against generated events and print
        # a compliance percentage, which is the single most misleading thing
        # the product could do. A number like 82% next to a firm's name reads
        # as a finding about that firm. It was a finding about a random number
        # generator seeded from the document id.
        #
        # So the assessment simply does not exist until real evidence is
        # imported, and the screen says so and offers the upload instead. The
        # gaps screen keeps its generated run, because that page is explicitly
        # about exercising the engine and labels itself as such.
        evidence_store, imported = _evidence_for(state)
        evidence_label = evidence_store.label if evidence_store is not None else ""

        # A compliance position, only when one has actually been taken.
        #
        # This screen used to run the engine the moment any evidence existed
        # and print the result as the firm's position. So the overview could
        # say "33% compliant with this framework" while the assessment history
        # said the firm had never been assessed. Both statements came from the
        # same page. The engine being able to produce a number is not the same
        # event as somebody deciding to record one.
        #
        # The gaps screen already drew this distinction. The overview now uses
        # the same test: a run is current only if its two input hashes still
        # match the rulebook and the records in front of us.
        log = _assessments(state)
        recorded = None
        stale = None
        report = None
        if certified and evidence_store is not None:
            from sanhita.assess import evidence_fingerprint, rulebook_fingerprint

            inputs = (rulebook_fingerprint(rules), evidence_fingerprint(evidence_store))
            for run in reversed(log.runs):
                if run.inputs() == inputs:
                    recorded = run
                    break
            if recorded is None:
                # There may be an older run. It is history, not this firm's
                # current position, and saying otherwise is the whole defect.
                stale = log.latest
            else:
                report = RuleEngine(WEEKENDS_ONLY).run(rules, evidence_store)

        # Counted from the run rather than stored anywhere. A dashboard whose
        # numbers are written down somewhere is a dashboard that will one day
        # disagree with the engine.
        # Two sets, kept apart, because they are two different claims.
        #
        # `failing` is what the records prove: an occasion fell due, a record
        # of it exists, and it says the artifact was never filed or was filed
        # late. `unverified` is a duty with no record at all, which is very
        # often one discharged on paper nobody uploaded. Folding them together
        # had this screen reporting 30 failures where 29 were unknowns, while
        # the evidence screen built from the same run said no record is not a
        # breach. One of those had to give and it was not going to be honesty.
        failing = set()
        unverified = set()
        high_risk = 0
        if report:
            for finding in report.findings:
                if finding.outcome is Outcome.SATISFIED:
                    continue
                if finding.outcome is Outcome.NO_EVIDENCE:
                    unverified.add(finding.obligation_id)
                    continue
                failing.add(finding.obligation_id)
                if finding.severity == "high":
                    high_risk += 1
        # A duty can be both, on different occasions. The proven one wins.
        unverified -= failing
        undetermined = {u.obligation_id for u in report.undetermined} if report else set()
        unevaluable = {u.obligation_id for u in report.unevaluable} if report else set()

        applicable = [
            o
            for o in certified
            if o.id not in undetermined and o.id not in unevaluable
        ]
        compliant = [
            o for o in applicable if o.id not in failing and o.id not in unverified
        ]

        # What falls due next. Independent of evidence, so it is useful even
        # before the first assessment.
        upcoming = 0
        if certified:
            from sanhita.analyse import build_forecast

            upcoming = len(
                build_forecast(
                    rules, evidence_store, start=_d.date.today(), horizon_days=30
                ).duties
            )

        # Over what could actually be determined, not over everything owed.
        #
        # The denominator used to be every applicable duty, which put the 29
        # duties nobody has any record of into the same figure as the one the
        # records actually settle. A firm that had uploaded a single register
        # read 33% compliant, and the 67% was mostly "we have not been shown".
        # That is the same contradiction the breach count carried, one level up.
        #
        # `determined` is what the records answer either way: met, or a
        # confirmed gap. The unknowns are reported beside the figure and never
        # inside it.
        determined = len(compliant) + len(failing)
        health = round(len(compliant) / determined * 100) if (report and determined) else None

        return templates.TemplateResponse(
            request,
            "company.html",
            shell(
                state,
                "company",
                request,
                company=company,
                # Asked on the firm's behalf on every load, rather than waiting
                # for somebody to remember the comparison screen exists.
                watch=_regulatory_watch(state, company, request),
                # And, for a team of two facing eighty items, the order to
                # take them in.
                worklist=_worklist(state),
                intermediaries=list(IntermediaryType),
                certified_count=len(certified),
                total_rules=len(rules),
                applicable=len(applicable),
                compliant=len(compliant),
                failing=len(failing),
                unverified=len(unverified),
                determined=determined if report else 0,
                high_risk=high_risk,
                undetermined=len(undetermined),
                unevaluable=len(unevaluable),
                health=health,
                upcoming=upcoming,
                review=queue.summary(),
                control_coverage=controls.coverage([o.id for o in certified]),
                remediation=remediation.summary(),
                open_tasks=remediation.open_tasks()[:5],
                worst=report.ranked()[:5] if report else [],
                evidence_label=evidence_label,
                evidence_imported=imported,
                assessed=recorded is not None,
                # The run this page corresponds to, and the older one it does
                # not. `stale` is set when a firm has been assessed before but
                # its records or its rulebook have changed since.
                recorded=recorded,
                stale=stale,
                # Who this firm is measured against, and when it last was.
                framework=state.workspace.name,
                framework_sha=state.workspace.doc_sha256,
                # Every rulebook on this machine, and whether the firm has said
                # it applies. The firm owns the list; the workspace it is being
                # viewed through is just one entry in it.
                available_frameworks=_framework_rows(state, company, request),
                viewing=state.workspace.id,
                last_run=log.latest,
                run_count=len(log),
                movement=log.movement(),
                history=log.recent(6),
            ),
        )

    @action("/company/save")
    def save_company(
        request: Request,
        wid: str = BUILTIN_ID,
        name: str = Form(...),
        intermediary: str = Form("STOCK_BROKER"),
        registration: str = Form(""),
        processes: str = Form(""),
        systems: str = Form(""),
        facts: str = Form(""),
    ):
        """Record who this firm is and what it does.

        Business facts are a plain list of yes or no answers rather than a
        typed schema, because the conditions that would consume them are prose
        rather than predicates. A compliance officer answering a short list is
        more honest than a system inferring the answers.
        """
        import datetime as _d

        from sanhita.company import Company, IntermediaryType

        state = bench(wid, request)
        try:
            kind = IntermediaryType(intermediary.upper())
        except ValueError:
            kind = IntermediaryType.STOCK_BROKER

        def lines(raw: str) -> list[str]:
            return [line.strip() for line in raw.splitlines() if line.strip()]

        existing = _company(state)
        firm = Company(
            name=name.strip() or "Unnamed firm",
            intermediary=kind,
            registration=registration.strip(),
            processes=lines(processes),
            systems=lines(systems),
            business_facts={
                fact.lstrip("-").strip(): not fact.strip().startswith("-")
                for fact in lines(facts)
            },
            # Which rulebooks apply is declared on its own screen, so saving the
            # profile must not silently drop what was declared there.
            frameworks=list(existing.frameworks) if existing else [],
            created_at=(existing.created_at if existing else None)
            or _d.datetime.now(_d.timezone.utc),
            synthetic=existing.synthetic if existing else False,
        )
        firm.save(_company_write_path(state))
        return RedirectResponse(f"/w/{wid}/company", status_code=303)

    def _framework_rows(state: Workbench, firm, request: Request) -> list[dict]:
        """Every rulebook this person can open, with what it would mean here.

        The certified count matters more than the name: a rulebook nobody has
        signed anything in cannot assess a firm, and saying so beside the tick
        box is more useful than letting somebody select it and find out later.
        """
        signed_in = current_user(request)
        declared = set(firm.frameworks) if firm else set()
        rows = []
        for space in workspaces.visible_to(signed_in.id if signed_in else None):
            try:
                registry = _load_registry(space.store_path)
                certified = sum(
                    1
                    for o in registry.all_current()
                    if o.status is RuleStatus.CERTIFIED
                )
                total = len(registry)
            except (OSError, ValueError):  # pragma: no cover - unreadable store
                certified = total = 0
            rows.append(
                {
                    "id": space.id,
                    "name": space.name,
                    "source": space.source_name,
                    "issued_on": space.issued_on,
                    "certified": certified,
                    "total": total,
                    "declared": space.id in declared,
                    "builtin": space.builtin,
                    # When this machine first saw the document, which is how
                    # long a later edition has been sitting here unexamined.
                    "created_at": space.created_at,
                }
            )
        return rows

    @action("/setup/complete")
    def finish_setup(request: Request, wid: str = BUILTIN_ID):
        """Mark onboarding done, and send the firm to the work.

        Pressed at the end of step three, whether or not anything was uploaded.
        A firm is allowed to finish with no records; it will simply be told
        there is nothing to assess rather than scored on records nobody gave.
        """
        import datetime as _d

        state = bench(wid, request)
        firm = _company(state)
        if firm is None or not firm.frameworks:
            raise HTTPException(
                400,
                "Setting up is not finished. Name the firm and say which SEBI "
                "rulebooks apply to it first.",
            )
        if firm.setup_completed_at is None:
            firm.setup_completed_at = _d.datetime.now(_d.timezone.utc)
            firm.save(_company_write_path(state))
        return RedirectResponse(f"/w/{wid}/review", status_code=303)

    @action("/company/frameworks")
    async def save_frameworks(request: Request, wid: str = BUILTIN_ID):
        """Record which SEBI rulebooks this firm says it is held to.

        A declaration, never an inference. Which framework governs a firm is a
        legal judgement with consequences, and Sanhita deciding it from an
        intermediary category would be exactly the kind of confident guess this
        product refuses to make everywhere else.
        """
        state = bench(wid, request)
        firm = _company(state)
        if firm is None:
            raise HTTPException(
                400, "Name the firm before saying which rulebooks apply to it."
            )

        form = await request.form()
        chosen = [str(v) for v in form.getlist("framework")]
        signed_in = current_user(request)
        known = {
            w.id for w in workspaces.visible_to(signed_in.id if signed_in else None)
        }
        unknown = [f for f in chosen if f not in known]
        if unknown:
            raise HTTPException(404, f"No rulebook {unknown[0]!r}.")

        firm.frameworks = [w for w in known if w in chosen]
        firm.save(_company_write_path(state))
        # Back to the company screen, which now shows step three rather
        # than dropping somebody onto a dashboard mid-setup.
        return RedirectResponse(f"/w/{wid}/company", status_code=303)

    @page("/review")
    def review_screen(request: Request, wid: str = BUILTIN_ID):
        """What the uploads found, and what somebody has to decide about it."""
        from sanhita.suggest import rank_obligations

        state = bench(wid, request)
        queue = _review(state)
        certified = sorted(
            (
                o
                for o in state.registry.all_current()
                if o.status is RuleStatus.CERTIFIED
            ),
            key=lambda o: (o.source.section.zfill(4), o.source.clause_id),
        )
        return templates.TemplateResponse(
            request,
            "review.html",
            shell(
                state,
                "review",
                request,
                evidence_health=_evidence_health(state, queue),
                queue=queue,
                summary=queue.summary(),
                awaiting=queue.awaiting(),
                mapped=queue.mapped(),
                dismissed=queue.dismissed(),
                documents=queue.documents(),
                certified=certified,
                company=_company(state),
                # Ranked, never chosen. The full list of 183 stays in the
                # dropdown beneath, because a ranking that hides the answer
                # guarantees a wrong mapping.
                suggestions={
                    item.item_id: rank_obligations(item.candidate, certified)
                    for item in queue.awaiting()
                },
            ),
        )

    @action("/review/{item_id}/map")
    def map_review_item(
        request: Request,
        item_id: str,
        wid: str = BUILTIN_ID,
        obligation_id: str = Form(...),
        by: str = Form(""),
    ):
        """A person says which duty this document discharges.

        The only way a candidate becomes evidence. Nothing infers this, because
        two texts being similar is not proof that a duty was performed.
        """
        state = bench(wid, request)

        # The dropdown only offers certified rules, and the dropdown is not a
        # security boundary. A crafted post could otherwise map a firm's
        # evidence to any string at all, including a rule that was rejected or
        # never existed, and the engine would then be comparing a real filing
        # against nothing.
        target = state.registry.current(obligation_id.strip())
        if target is None:
            raise HTTPException(
                404, f"No rule {obligation_id!r} exists in this document."
            )
        if target.status is not RuleStatus.CERTIFIED:
            raise HTTPException(
                400,
                f"Rule {obligation_id} is {target.status.value.lower()}, not "
                "certified. Evidence can only be mapped to a requirement a "
                "person has signed, because nothing else is executable.",
            )

        queue = _review(state)
        actor = _acting_officer(request, "Mapping a document to a requirement")
        try:
            queue.map_to(item_id, obligation_id, by=actor)
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        queue.save()

        company = _company(state)
        queue.to_evidence(
            f"{company.name if company else state.workspace.name}, reviewed evidence"
        ).save(_evidence_write_path(state))
        return RedirectResponse(f"/w/{wid}/review", status_code=303)

    @action("/review/{item_id}/dismiss")
    def dismiss_review_item(
        request: Request,
        item_id: str,
        wid: str = BUILTIN_ID,
        reason: str = Form(""),
        by: str = Form(""),
    ):
        state = bench(wid, request)
        queue = _review(state)
        try:
            queue.dismiss(
                item_id,
                by=_acting_officer(request, "Dismissing a candidate record"),
                reason=reason,
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        queue.save()

        company = _company(state)
        queue.to_evidence(
            f"{company.name if company else state.workspace.name}, reviewed evidence"
        ).save(_evidence_write_path(state))
        return RedirectResponse(f"/w/{wid}/review", status_code=303)

    @app.post("/w/{wid}/evidence/clear")
    def clear_evidence(request: Request, wid: str):
        state = bench(wid, request)
        path = _evidence_path(state)
        if path.is_file():
            path.unlink()
        return RedirectResponse(f"/w/{wid}/gaps", status_code=303)

    @app.get("/evidence-template.csv")
    def evidence_template():
        from fastapi.responses import PlainTextResponse

        from sanhita.execute.importer import TEMPLATE_CSV

        return PlainTextResponse(
            TEMPLATE_CSV,
            headers={"content-disposition": 'attachment; filename="sanhita-evidence.csv"'},
        )

    @page("/conflicts")
    def conflicts(request: Request, wid: str = BUILTIN_ID, limit: int = 60):
        """Rules that disagree with each other.

        Only possible because the rules are typed. Finding this by reading
        would mean holding 1,377 clauses in your head at once.
        """
        from sanhita.analyse import ConflictKind, find_conflicts

        state = bench(wid, request)
        report = find_conflicts(state.registry.all_current())
        return templates.TemplateResponse(
            request,
            "conflicts.html",
            shell(
                state,
                "conflicts",
                request,
                report=report,
                findings=report.ranked()[:limit],
                kinds=[k for k in ConflictKind if report.of(k)],
                limit=limit,
            ),
        )

    # ═══════════════════════════════════════════════════════ remediation ══
    #
    # The half of the problem statement the product used to stop short of:
    # "identifying and remediating compliance gaps before they become
    # regulatory findings." Identification was a screen. Remediation is a piece
    # of work somebody owns, and it ends when the certified rule runs again and
    # returns nothing, not when anybody says it is fixed.

    def _evidence_for(state: Workbench):
        """This firm's own records, or nothing.

        This used to fall back to generated events when a firm had uploaded
        nothing, which meant the gaps screen could tell a named company it had
        27 breaches before that company had provided a single document. A
        caption saying "running on generated records" does not undo a page
        headed "where you are out of compliance" full of findings.

        So there is no fallback. No evidence means no assessment, and the
        screens say so. Generated events still exist for demonstrating the
        engine, but a person has to ask for them explicitly and the result is
        labelled as a demonstration rather than as this firm's position.
        """
        from sanhita.execute import EvidenceStore

        if _evidence_path(state).is_file():
            return EvidenceStore.load(_evidence_path(state)), True
        return None, False

    def _demo_evidence(state: Workbench):
        """Generated events, for showing what the engine does. Never a verdict.

        Only reachable when somebody adds ?demo=1, and every screen that uses
        it says plainly that these are not the firm's records.
        """
        import datetime as _d

        from sanhita.execute import WEEKENDS_ONLY
        from sanhita.execute.synthetic import generate

        certified = [
            o for o in state.registry.all_current() if o.status is RuleStatus.CERTIFIED
        ]
        today = _d.date.today()
        return generate(
            certified,
            calendar=WEEKENDS_ONLY,
            start=today - _d.timedelta(days=180),
            end=today - _d.timedelta(days=10),
            seed=f"sanhita-{state.workspace.id}",
        )

    @page("/remediation")
    def remediation_screen(request: Request, wid: str = BUILTIN_ID):
        """Every open piece of work, worst first."""
        state = bench(wid, request)
        store = _remediation(state)
        clauses = {
            o.id: o.source.clause_id for o in state.registry.all_current()
        }

        # What each task could point at, and what it already points at. Keyed by
        # obligation, because the only records that can answer a task are the
        # ones filed against the rule it was raised under.
        evidence, _ = _evidence_for(state)
        by_id = {event.id: event for event in evidence.events} if evidence else {}
        candidates: dict[str, list] = {}
        attached: dict[str, list] = {}
        for task in store.all():
            attached[task.task_id] = [
                by_id[eid] for eid in task.evidence_ids if eid in by_id
            ]
            if evidence is not None:
                already = set(task.evidence_ids)
                candidates[task.task_id] = [
                    event
                    for event in evidence.for_obligation(task.obligation_id)
                    if event.id not in already
                ][:12]

        return templates.TemplateResponse(
            request,
            "remediation.html",
            shell(
                state,
                "remediation",
                request,
                tasks=store.all(),
                summary=store.summary(),
                log=list(reversed(store.log.entries))[:80],
                chain_ok=store.log.verify(),
                clauses=clauses,
                candidates=candidates,
                attached=attached,
                has_evidence=evidence is not None,
            ),
        )

    @action("/remediation/open")
    def open_remediation(
        request: Request,
        wid: str = BUILTIN_ID,
        obligation_id: str = Form(...),
        gap_id: str = Form(...),
        clause_id: str = Form(...),
        title: str = Form(""),
        owner: str = Form(""),
        team: str = Form(""),
        priority: str = Form("MEDIUM"),
        due: str = Form(""),
        action_text: str = Form(""),
        by: str = Form(""),
    ):
        """Raise a task against one finding of an assessment of record.

        A preview is not a finding. The assessment screen runs the engine on
        every view so a person can see what an assessment would say, and it was
        possible to act on that: open a remediation task, own it, give it a
        deadline, and close it, without an assessment ever having been taken.
        The audit chain would then start from something nobody recorded.

        So the gap has to come from a run that exists, against the rulebook and
        the records in front of us now.
        """
        import datetime as _d

        from sanhita.remediate import Priority
        from sanhita.remediate.service import suggested_due_date

        actor = _acting_officer(request, "Raising a remediation task")
        state = bench(wid, request)
        obligation = state.registry.current(obligation_id)
        if obligation is None:
            raise HTTPException(404, f"No rule {obligation_id} in this document.")
        if obligation.status is not RuleStatus.CERTIFIED:
            raise HTTPException(
                400,
                f"Rule {obligation_id} is not certified, so it cannot have "
                "produced a finding to remediate.",
            )

        run = _current_assessment(state)
        if run is None:
            raise HTTPException(
                400,
                "Run the compliance assessment before opening a remediation "
                "task. What is on the gaps screen until then is a preview of "
                "what an assessment would say, not a finding of record.",
            )
        matched = [
            f
            for f in run.findings
            if f.obligation_id == obligation_id and f.event_id == gap_id
        ]
        if not matched:
            raise HTTPException(
                400,
                f"Assessment {run.short_id} reports no finding {gap_id!r} "
                f"against {obligation_id}. A task can only be raised against a "
                "finding that assessment actually made.",
            )

        try:
            level = Priority(priority.upper())
        except ValueError:
            level = Priority.MEDIUM
        try:
            due_date = _d.date.fromisoformat(due) if due else suggested_due_date(level.value)
        except ValueError:
            due_date = suggested_due_date(level.value)

        store = _remediation(state)
        store.open_for_gap(
            gap_id=gap_id,
            obligation_id=obligation_id,
            clause_id=clause_id,
            company=state.workspace.name,
            title=title.strip() or f"Gap on clause {clause_id}",
            by=actor,
            remediation_action=action_text.strip(),
            evidence_required=", ".join(
                e.artifact_type for e in obligation.evidence
            ),
            owner=owner,
            assigned_team=team,
            priority=level,
            due_date=due_date,
            source_rule_version=obligation.version,
        )
        store.save()
        return RedirectResponse(f"/w/{wid}/remediation", status_code=303)

    @action("/remediation/{task_id}/assign")
    def assign_remediation(
        request: Request,
        task_id: str,
        wid: str = BUILTIN_ID,
        owner: str = Form(...),
        team: str = Form(""),
        by: str = Form(""),
    ):
        from sanhita.remediate.tasks import RemediationError

        state = bench(wid, request)
        store = _remediation(state)
        try:
            store.assign(
                task_id,
                owner=owner,
                team=team,
                by=_acting_officer(request, "Assigning a remediation task"),
            )
        except RemediationError as exc:
            raise HTTPException(400, str(exc)) from exc
        store.save()
        return RedirectResponse(f"/w/{wid}/remediation#{task_id}", status_code=303)

    @page("/chain/{task_id}")
    def chain(request: Request, task_id: str, wid: str = BUILTIN_ID):
        """One gap, from the SEBI clause to the moment it closed, on one page.

        Every link in this already existed and every one was on a different
        screen, so proving the story meant a person navigating between four of
        them and holding the ids in their head. An inspector asked to follow a
        closure should not have to do that, and neither should a judge.

            clause -> certified rule -> the record that triggered it
                   -> the assessment that found it -> the task
                   -> the corrective evidence -> the re-check -> closure

        Nothing here is recomputed for display. Each link is read from the store
        that owns it, which is what makes it a trail rather than a diagram.
        """
        state = bench(wid, request)
        store = _remediation(state)
        task = store.get(task_id)
        if task is None:
            raise HTTPException(404, f"No remediation task {task_id!r}.")

        obligation = state.registry.current(task.obligation_id)
        clause = state.tree.nodes.get(task.clause_id)

        evidence, _ = _evidence_for(state)
        by_id = {e.id: e for e in evidence.events} if evidence else {}
        attached = [by_id[eid] for eid in task.evidence_ids if eid in by_id]
        # The record the gap was raised against, where it is still there. On a
        # NO_EVIDENCE finding there was never a record, which the page says.
        trigger = by_id.get(task.gap_id)

        # The assessment that reported this gap: the earliest recorded run whose
        # findings name this obligation. Later runs may no longer mention it,
        # which is what closing it is supposed to look like.
        log = _assessments(state)
        found_in = None
        cleared_in = None
        for run in log.runs:
            names_it = any(f.obligation_id == task.obligation_id for f in run.findings)
            if names_it and found_in is None:
                found_in = run
            if found_in is not None and run.has_findings and not names_it:
                cleared_in = run
                break

        return templates.TemplateResponse(
            request,
            "chain.html",
            shell(
                state,
                "remediation",
                request,
                task=task,
                obligation=obligation,
                clause=clause,
                trigger=trigger,
                attached=attached,
                entries=store.log.for_task(task_id),
                chain_ok=store.log.verify(),
                found_in=found_in,
                cleared_in=cleared_in,
                binding=_controls(state).get(task.obligation_id),
            ),
        )

    @action("/remediation/{task_id}/attach")
    async def attach_remediation_evidence(
        request: Request,
        task_id: str,
        wid: str = BUILTIN_ID,
    ):
        """Name the exact records that answer this task.

        A task that says "corrected evidence was filed" and points at nothing is
        an assertion. A task that names the event ids, and through them the
        document, page and row those events came from, is a record an inspector
        can follow back to a piece of paper.

        Attaching does not close anything. It moves the task to
        READY_FOR_RECHECK, and the re-check still has to agree.
        """
        from sanhita.remediate.tasks import RemediationError

        actor = _acting_officer(request, "Attaching evidence to a task")
        state = bench(wid, request)
        form = await request.form()
        chosen = [str(v).strip() for v in form.getlist("evidence_id") if str(v).strip()]

        evidence, _ = _evidence_for(state)
        if evidence is None:
            raise HTTPException(
                400,
                "This firm has no compliance records, so there is nothing to "
                "attach. Upload the corrected evidence first.",
            )

        # Only ids that exist. A task must never point at an event that was
        # never imported, because the whole value of the attachment is that
        # somebody can follow it back to the document it came from.
        known = {event.id for event in evidence.events}
        unknown = [eid for eid in chosen if eid not in known]
        if unknown:
            raise HTTPException(
                400,
                f"No such evidence in this workspace: {', '.join(unknown[:5])}.",
            )

        store = _remediation(state)
        try:
            store.attach_evidence(task_id, chosen, by=actor)
        except RemediationError as exc:
            raise HTTPException(400, str(exc)) from exc
        store.save()
        return RedirectResponse(f"/w/{wid}/remediation#{task_id}", status_code=303)

    @action("/remediation/{task_id}/recheck")
    def recheck_remediation(
        request: Request,
        task_id: str,
        wid: str = BUILTIN_ID,
        by: str = Form(""),
    ):
        """Run the certified rule again. The only thing that can close a task.

        Deliberately has no "mark as fixed" sibling. The store refuses to set
        VERIFIED or CLOSED by hand, so this route is the sole path to either,
        and what it writes is whatever the deterministic engine returned.
        """
        from sanhita.remediate.service import recheck_task
        from sanhita.remediate.tasks import RemediationError

        state = bench(wid, request)
        store = _remediation(state)
        actor = _acting_officer(request, "Re-checking a remediation task")

        # A task raised from an amendment closes on a different fact. There is
        # no evidence to run a rule against: the question is whether the
        # rulebook itself was put right, which the store and the later document
        # answer between them. Same button, same log, different test.
        task = store.get(task_id)
        if task is not None and task.is_from_an_amendment:
            from sanhita.remediate import recheck_amendment_task

            try:
                recheck_amendment_task(
                    store,
                    task_id,
                    state.registry.all_current(),
                    state.tree,
                    by=actor,
                )
            except RemediationError as exc:
                raise HTTPException(400, str(exc)) from exc
            store.save()
            return RedirectResponse(
                f"/w/{wid}/remediation#{task_id}", status_code=303
            )

        evidence, imported = _evidence_for(state)
        if evidence is None:
            # A re-check against nothing is not a re-check. Closing a task
            # because the engine found no breach in an empty evidence store
            # would be the easiest possible way to fake compliance.
            raise HTTPException(
                400,
                "This firm has no compliance records, so the rule cannot be run "
                "again. Upload the corrected evidence first.",
            )
        # The corrective evidence has to be named, not merely present.
        #
        # Without this a task could close because something somewhere in the
        # firm's records happened to satisfy the rule, and the audit trail
        # would say a gap was fixed without ever saying by which document. The
        # store already refuses to be told a task is fixed; this refuses to let
        # it close without pointing at what fixed it.
        if task is not None and not task.evidence_ids:
            raise HTTPException(
                400,
                "Attach the corrective evidence to this task before re-checking. "
                "A task that closes without naming the record that closed it "
                "leaves an inspector nothing to follow.",
            )

        try:
            recheck_task(
                store,
                task_id,
                state.registry.all_current(),
                evidence,
                by=actor,
            )
        except RemediationError as exc:
            raise HTTPException(400, str(exc)) from exc
        store.save()
        return RedirectResponse(f"/w/{wid}/remediation#{task_id}", status_code=303)

    @page("/processes")
    def processes_screen(request: Request, wid: str = BUILTIN_ID):
        """The chain the problem statement asks for, end to end.

            clause -> obligation -> process -> function -> system -> control
            -> required evidence -> executable check

        Everything on this screen already existed in pieces. The obligation
        knew its clause and its evidence, the binding knew the team and the
        system, and the engine knew whether the check passed. Nothing joined
        them, so a supervisor could see that a rule failed but not what part of
        the firm had failed to do it.
        """
        from sanhita.execute.report import Outcome

        state = bench(wid, request)
        rules = state.registry.all_current()
        certified = [o for o in rules if o.status is RuleStatus.CERTIFIED]
        controls = _controls(state)
        remediation = _remediation(state)

        # The live check result per obligation, so the chain ends in a status
        # rather than trailing off at "required evidence".
        outcome_by_rule: dict[str, str] = {}
        evidence, assessed = _evidence_for(state)
        if certified and evidence is not None:
            from sanhita.execute import WEEKENDS_ONLY, RuleEngine

            report = RuleEngine(WEEKENDS_ONLY).run(rules, evidence)
            for finding in report.findings:
                if finding.outcome is not Outcome.SATISFIED:
                    outcome_by_rule[finding.obligation_id] = finding.outcome.value
            for row in report.undetermined:
                outcome_by_rule.setdefault(row.obligation_id, "UNDETERMINED")

        by_id = {o.id: o for o in certified}
        tasks_by_rule = {t.obligation_id: t for t in remediation.all()}

        chains = []
        for binding in controls.bindings.values():
            obligation = by_id.get(binding.obligation_id)
            if obligation is None:
                continue
            chains.append(
                {
                    "binding": binding,
                    "rule": obligation,
                    "clause_id": obligation.source.clause_id,
                    "evidence": [e.artifact_type for e in obligation.evidence],
                    # Without an assessment there is no result to show, and
                    # defaulting to SATISFIED would mean an unassessed firm
                    # read as a compliant one.
                    "status": outcome_by_rule.get(
                        obligation.id, "SATISFIED" if assessed else "NOT_ASSESSED"
                    ),
                    "task": tasks_by_rule.get(obligation.id),
                }
            )
        chains.sort(key=lambda c: (c["binding"].process or "zz", c["clause_id"]))

        return templates.TemplateResponse(
            request,
            "processes.html",
            shell(
                state,
                "processes",
                request,
                chains=chains,
                by_process=controls.by_process(),
                systems=controls.systems(),
                coverage=controls.coverage([o.id for o in certified]),
                certified_count=len(certified),
            ),
        )

    @page("/load")
    def load_screen(request: Request, wid: str = BUILTIN_ID):
        """What this regulation asks of each kind of firm, per year.

        Nobody has this number today. Getting it by hand means reading 399
        pages and counting, and the answer changes every time the circular is
        reissued. Over a typed rulebook it is arithmetic.
        """
        from sanhita.analyse import assess_fragility, build_graph, measure_burden

        state = bench(wid, request)
        rules = state.registry.all_current()
        burden = measure_burden(rules)
        fragility = assess_fragility(state.tree, build_graph(state.tree), rules)
        return templates.TemplateResponse(
            request,
            "load.html",
            shell(
                state,
                "load",
                request,
                burden=burden,
                fragility=fragility,
            ),
        )

    @page("/divergence")
    def divergence_screen(request: Request, wid: str = BUILTIN_ID, limit: int = 40):
        """Where two firms reading the same clause will land differently.

        The rest of the product removes the cause of divergence by sharing one
        signed rule. This screen answers the question one step earlier, which
        is the one a drafter can still act on.
        """
        from sanhita.analyse import assess_divergence

        state = bench(wid, request)
        report = assess_divergence(
            state.registry.all_current(), ledger=state.registry.ledger
        )
        return templates.TemplateResponse(
            request,
            "divergence.html",
            shell(
                state,
                "divergence",
                request,
                report=report,
                shown=report.top(limit),
                limit=limit,
            ),
        )

    @page("/forecast")
    def forecast_screen(request: Request, wid: str = BUILTIN_ID, horizon: int = 30):
        """What falls due next, and which of it this firm has never managed.

        The gaps screen is retrospective and therefore always slightly too
        late. This one reads the same evidence forwards.
        """
        import datetime as _d

        from sanhita.analyse import build_forecast

        state = bench(wid, request)
        rules = state.registry.all_current()
        certified = [o for o in rules if o.status is RuleStatus.CERTIFIED]

        horizon = max(7, min(180, horizon))
        today = _d.date.today()

        # What falls due is a property of the rulebook and the calendar, so the
        # forecast stands up without any evidence at all. Evidence only adds the
        # note about what has already been filed, and this screen used to invent
        # that note when the firm had filed nothing. A forecast that tells a firm
        # its last four returns went out on time, on the strength of a random
        # number generator, is worse than no forecast.
        evidence, _ = _evidence_for(state)
        imported = evidence is not None

        report = build_forecast(rules, evidence, start=today, horizon_days=horizon)
        return templates.TemplateResponse(
            request,
            "forecast.html",
            shell(
                state,
                "forecast",
                request,
                report=report,
                horizon=horizon,
                imported=imported,
                certified_count=len(certified),
            ),
        )

    @page("/simulate")
    def simulate_screen(
        request: Request,
        wid: str = BUILTIN_ID,
        rule: str | None = None,
        mode: str = "days",
        days: int | None = None,
        period: str | None = None,
        count: str = "BUSINESS",
    ):
        """Regulatory impact assessment. What a draft change would cost.

        Read from the query string rather than posted, so a supervisor can send
        somebody the link to a specific draft and they see the same assessment.
        Nothing is written by this route.
        """
        from sanhita.analyse import Change, ChangeKind, assess_amendment, build_graph
        from sanhita.ir.enums import DayCount

        state = bench(wid, request)
        rules = state.registry.all_current()

        # Only rules a person has signed can be meaningfully amended: an
        # unsigned proposal is not yet part of the rulebook to change.
        certified = sorted(
            (o for o in rules if o.status is RuleStatus.CERTIFIED),
            key=lambda o: (o.source.section.zfill(4), o.source.clause_id, o.id),
        )

        assessment = target = None
        error = None
        if rule:
            target = next((o for o in rules if o.id == rule), None)
            if target is None:
                error = f"No rule with id {rule!r} in this document."
            else:
                try:
                    if mode == "period":
                        if not period:
                            raise ValueError("Choose how often the duty should recur.")
                        change = Change(
                            obligation_id=rule,
                            kind=ChangeKind.DEADLINE_PERIOD,
                            period=period.strip().upper(),
                        )
                    else:
                        if days is None:
                            raise ValueError("Enter a number of days.")
                        if days < 0 or days > 3650:
                            raise ValueError(
                                "A deadline of that length is not a deadline."
                            )
                        change = Change(
                            obligation_id=rule,
                            kind=ChangeKind.DEADLINE_DAYS,
                            days=days,
                            day_count=(
                                DayCount.CALENDAR
                                if count == "CALENDAR"
                                else DayCount.BUSINESS
                            ),
                        )
                    assessment = assess_amendment(
                        rules, [change], references=build_graph(state.tree)
                    )
                except ValueError as exc:
                    error = str(exc)

        return templates.TemplateResponse(
            request,
            "simulate.html",
            shell(
                state,
                "simulate",
                request,
                certified=certified,
                target=target,
                assessment=assessment,
                error=error,
                rule=rule,
                mode=mode,
                days=days,
                period=period,
                count=count,
            ),
        )

    def _comparison(state, request, against: str):
        """Compare this workspace against an earlier edition, for one firm.

        Extracted so the screen and the route that raises work from it compute
        the same plan from the same inputs. A route that trusted the form it
        was posted would let anybody invent an action, own it, and close it,
        which is the amendment version of raising a task from a preview.
        """
        from sanhita.analyse import build_graph
        from sanhita.change import plan_for_firm
        from sanhita.diff import assess_impact, diff_trees

        other_bench = bench(against, request)
        other = other_bench.workspace
        changes = diff_trees(
            other_bench.tree,
            state.tree,
            before_label=other.name,
            after_label=state.workspace.name,
        )
        # Built on the later tree, because the question is what the rules
        # point at now.
        graph = build_graph(state.tree)
        impact = assess_impact(changes, state.registry.all_current(), references=graph)
        firm = _company(state)
        plan = plan_for_firm(
            impact,
            state.registry.all_current(),
            _controls(state),
            firm=firm.name if firm else "this firm",
            framework=state.workspace.name,
        )
        return other, changes, graph, impact, plan

    #: The last SEBI check per workspace. In memory on purpose: what a
    #: regulator's website listed at one moment is not a compliance record, and
    #: writing it beside the rulebook would invite reading it as one.
    _discoveries: dict[str, object] = {}

    def _require_declared(firm, state: Workbench, doing: str) -> None:
        """A firm is only ever assessed against a rulebook it declared.

        Two different ideas share a screen and must not share a meaning. The
        regulatory workbench holds every rulebook on this installation, which
        anybody may open and read. A company's compliance is measured against
        the frameworks that company said govern it, and nothing else.

        Without this check a crafted request could raise a task, approve a
        plan, or record a position for a firm against a circular somebody else
        uploaded this morning, and the firm's audit trail would carry it.
        """
        if firm is None:
            return
        declared = set(getattr(firm, "frameworks", []) or [])
        if declared and state.workspace.id not in declared:
            raise HTTPException(
                400,
                f"{firm.name} has not declared {state.workspace.name!r} as one "
                f"of its frameworks, so it cannot {doing} it. Declare it on the "
                "company screen first, or switch to a framework it did declare.",
            )

    def _plans(state: Workbench):
        """Approvals of amendment action plans, beside the firm's other records."""
        from sanhita.orchestrate import PlanStore

        plans = PlanStore.load(_sidecar(state, "plans.json"))
        plans.path = _writable(state, "plans.json")
        return plans

    @action("/change/approve")
    def approve_action_plan(
        request: Request,
        wid: str = BUILTIN_ID,
        against: str = Form(...),
        decision: str = Form("approve"),
        note: str = Form(""),
    ):
        """The boundary. Everything above it is arithmetic; below it, work exists.

        Approving recomputes the comparison rather than trusting the screen it
        came from, creates one task per recommended action, and records who
        approved it. Declining records that a named person looked and decided
        this firm does not act on it, which is a defensible position and an
        answerable one; silence is neither.
        """
        from sanhita.orchestrate import plan_from_change
        from sanhita.remediate import open_for_action

        actor = _acting_officer(request, "Approving a regulatory action plan")
        state = bench(wid, request)
        firm = _company(state)
        if firm is None or not firm.name:
            raise HTTPException(
                400,
                "An action plan is a decision one firm takes. Record who this "
                "firm is before approving anything on its behalf.",
            )
        _require_declared(firm, state, "approve an action plan against")

        _, changes, _, _, change_plan = _comparison(state, request, against)
        plan = plan_from_change(
            change_plan,
            firm=firm.name,
            framework=state.workspace.name,
            before_fingerprint=changes.before_fingerprint,
            after_fingerprint=changes.after_fingerprint,
        )
        store = _plans(state)
        if decision == "decline":
            store.decline(plan, by=actor, note=note)
            store.save()
            return RedirectResponse(f"/w/{wid}/diff?against={against}", status_code=303)

        tasks = _remediation(state)
        created = []
        for item in change_plan.actions:
            task = open_for_action(
                tasks,
                item,
                company=firm.name,
                by=actor,
                before_fingerprint=changes.before_fingerprint,
                after_fingerprint=changes.after_fingerprint,
            )
            created.append(task.task_id)
        tasks.save()
        store.approve(plan, by=actor, task_ids=created, note=note)
        store.save()
        return RedirectResponse(f"/w/{wid}/diff?against={against}", status_code=303)

    @app.post("/w/{wid}/discover")
    def check_sebi_now(request: Request, wid: str = BUILTIN_ID):
        """Ask SEBI what it is listing, because somebody pressed the button.

        Pressed, not polled. There is no scheduler behind this and the product
        never says there is: the answer is as fresh as the request and no
        fresher. What comes back is a list of titles, dates and links, and
        nothing on it enters the rulebook by being found.
        """
        from sanhita.discover import DiscoveryRefused, discover, fetch_official

        state = bench(wid, request)
        signed_in = current_user(request)
        known = workspaces.visible_to(signed_in.id if signed_in else None)
        try:
            html = fetch_official()
            report = discover(html, known=known)
        except DiscoveryRefused as exc:
            from sanhita.discover import SEBI_CIRCULARS, Discovery

            report = Discovery(
                checked_at=_dt.datetime.now(_dt.timezone.utc),
                source=SEBI_CIRCULARS,
                problem=str(exc),
            )
        _discoveries[state.workspace.id] = report
        return RedirectResponse(f"/w/{wid}/diff", status_code=303)

    @page("/diff")
    def diff_screen(request: Request, wid: str = BUILTIN_ID, against: str | None = None):
        """DIFF. What an amendment costs you in signatures.

        The document being viewed is the later version. The one picked is the
        earlier one, so the comparison reads forward in time the way an
        amendment does.
        """
        from sanhita.diff import Consequence

        state = bench(wid, request)
        signed_in = current_user(request)
        others = [
            w
            for w in workspaces.visible_to(signed_in.id if signed_in else None)
            if w.id != wid
        ]

        changes = impact = other = graph = plan = action_plan = None
        raised = {}
        # What this amendment means for the firm, not just for the rulebook.
        #
        # A count of added and removed clauses is a fact about SEBI. The
        # question a compliance officer has is which of their processes,
        # systems and controls now need attention, and that answer only exists
        # because the control bindings connect a clause to a desk.
        #
        # The block this replaced tested each row of `impact.affected` for
        # being a mapping. Those rows are AffectedRule dataclasses, so the test
        # never passed, the set was always empty, and the operational section
        # never drew a single row. The screen had been reporting no operational
        # impact for every amendment since it was written.
        if against:
            other, changes, graph, impact, plan = _comparison(state, request, against)
            # Which of these are already somebody's work, so the screen offers
            # to raise a task once and shows the task afterwards.
            raised = _raised_actions(state, changes, plan)
            # And the whole amendment as one decision, which is what somebody
            # facing eighty-two actions actually needs.
            firm = _company(state)
            if firm is not None and firm.name:
                from sanhita.orchestrate import plan_from_change

                action_plan = _plans(state).decision_on(
                    plan_from_change(
                        plan,
                        firm=firm.name,
                        framework=state.workspace.name,
                        before_fingerprint=changes.before_fingerprint,
                        after_fingerprint=changes.after_fingerprint,
                    )
                )

        return templates.TemplateResponse(
            request,
            "diff.html",
            shell(
                state,
                "diff",
                request,
                others=others,
                against=against,
                other=other,
                changes=changes,
                impact=impact,
                graph=graph,
                plan=plan,
                raised=raised,
                discovery=_discoveries.get(state.workspace.id),
                action_plan=action_plan,
                company=_company(state),
                consequences=list(Consequence) if impact else [],
            ),
        )

    def _raised_actions(state, changes, plan) -> dict:
        """Tasks that already exist for this amendment.

        Keyed by kind, clause and rule rather than by the derived gap id, so a
        template can look one up without being able to derive hashes.
        """
        from sanhita.remediate import action_gap_id

        if plan is None or changes is None:
            return {}
        by_gap = {t.gap_id: t for t in _remediation(state).tasks.values()}
        found = {}
        for item in plan.actions:
            task = by_gap.get(
                action_gap_id(
                    item,
                    before_fingerprint=changes.before_fingerprint,
                    after_fingerprint=changes.after_fingerprint,
                )
            )
            if task is not None:
                found[f"{item.kind.value}|{item.clause_id}|{item.obligation_id}"] = task
        return found

    @action("/change/open")
    def open_change_task(
        request: Request,
        wid: str = BUILTIN_ID,
        against: str = Form(...),
        kind: str = Form(...),
        clause_id: str = Form(...),
        obligation_id: str = Form(""),
        owner: str = Form(""),
        by: str = Form(""),
    ):
        """Turn one required action into a task somebody owns.

        The action is not taken from the form. The comparison is recomputed and
        the posted action has to appear in it, for the same reason a gap has to
        appear in an assessment of record before it can be remediated: work
        that starts from something nobody computed leaves an inspector a chain
        that begins nowhere.
        """
        from sanhita.remediate import open_for_action

        actor = _acting_officer(request, "Raising a task from a regulatory change")
        state = bench(wid, request)
        firm = _company(state)
        if firm is None or not firm.name:
            raise HTTPException(
                400,
                "A task belongs to a firm. Record who this firm is before "
                "raising work on its behalf.",
            )
        if firm.setup_completed_at is None:
            raise HTTPException(
                400,
                "Finish setting this firm up before raising work against it. A "
                "task raised mid-onboarding belongs to a firm that does not yet "
                "know which rulebooks govern it.",
            )
        _require_declared(firm, state, "raise a task against")

        _, changes, _, _, plan = _comparison(state, request, against)

        wanted = [
            a
            for a in plan.actions
            if a.kind.value == kind.upper()
            and a.clause_id == clause_id
            and (a.obligation_id or "") == obligation_id
        ]
        if not wanted:
            raise HTTPException(
                400,
                f"Comparing these two editions produces no {kind} action on "
                f"clause {clause_id}. A task can only be raised against work "
                "the comparison actually found.",
            )

        firm = _company(state)
        store = _remediation(state)
        task = open_for_action(
            store,
            wanted[0],
            company=firm.name if firm else state.workspace.name,
            by=actor,
            before_fingerprint=changes.before_fingerprint,
            after_fingerprint=changes.after_fingerprint,
        )
        if owner.strip():
            store.assign(task.task_id, owner=owner.strip(), by=actor)
        store.save()
        # Back to the comparison rather than to the queue: a person raising one
        # of these usually raises several, and losing the diff would mean
        # picking the earlier edition again for each one.
        return RedirectResponse(f"/w/{wid}/diff?against={against}", status_code=303)

    @app.get("/w/{wid}/export")
    def export(request: Request, wid: str):
        """The certified rulebook, as canonical JSON, with nothing else in it."""
        state = bench(wid, request)
        certified = [
            o for o in state.registry.all_current() if o.status is RuleStatus.CERTIFIED
        ]
        return JSONResponse(
            {
                "document": state.workspace.name,
                "source_file": state.workspace.source_name,
                "tree_fingerprint": state.tree.fingerprint(),
                "certified_count": len(certified),
                "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                "rules": [o.model_dump(mode="json") for o in certified],
            },
            headers={
                "content-disposition": (
                    f'attachment; filename="sanhita-{state.workspace.id}-certified.json"'
                )
            },
        )

    @app.get("/supervisor", response_class=HTMLResponse)
    def supervisor(request: Request):
        """The view a regulator has and nobody else can build.

        Every other screen answers "how is this firm doing against this
        rulebook". This one answers "how is the market doing against it", and
        it is only possible because one compiled corpus is shared across
        workspaces. A clause that no firm has operationalised is a supervisory
        signal that has never existed before; so is a clause two firms
        certified differently.

        Deliberately read-only and deliberately honest about its own limits: on
        a machine with one document it says so rather than drawing a market.
        """
        from sanhita.demo import synthetic_market
        from sanhita.metrics.coverage import compute_coverage

        signed_in = current_user(request)
        visible = workspaces.visible_to(signed_in.id if signed_in else None)

        # Only documents that have been compiled, and only ones already parsed
        # into the cache.
        #
        # This screen used to call bench() on everything it could see, which
        # parses each PDF. One uploaded mutual fund circular is 748 pages and
        # takes 32 seconds on its own, so a supervisor with three documents
        # waited over a minute, the request timed out, and the CPU it burned
        # made every other page on the site crawl while it ran.
        #
        # Nothing is lost by skipping the rest. A document nobody has compiled
        # has no rules to supervise, and a market view is about what firms have
        # actually operationalised. The ones left out are counted and named on
        # the screen rather than silently dropped.
        compilations = []
        skipped_uncompiled: list[str] = []
        skipped_unparsed: list[str] = []
        for workspace in visible:
            # The worked example is always read, even on a cold start. It is
            # the one document this whole product is a view of, it is compiled
            # and certified, and a supervisory screen that opens showing
            # nothing at all is worse than one that takes a few seconds on its
            # first load. It stays in the cache afterwards, so the cost is paid
            # once per process.
            if not workspace.builtin:
                if not workspace.store_path.is_file():
                    skipped_uncompiled.append(workspace.name)
                    continue
                # An uploaded circular can run to hundreds of pages and tens of
                # seconds. Reading every one of them on demand is what made
                # this page time out, so an uploaded document joins the view
                # once somebody has opened it and it is already in memory.
                if workspace.id not in _cache:
                    skipped_unparsed.append(workspace.name)
                    continue
            try:
                state = bench(workspace.id, request)
            except HTTPException:
                continue
            rules = state.registry.all_current()
            certified = [o for o in rules if o.status is RuleStatus.CERTIFIED]
            report = compute_coverage(state.tree, rules)
            compilations.append(
                {
                    "workspace": workspace,
                    "rules": len(rules),
                    "certified": len(certified),
                    "compiled_coverage": report.compiled_coverage,
                    "certified_coverage": report.clause_coverage,
                    "duty_clauses": report.obligation_bearing_clauses,
                    "certified_clauses": {
                        o.source.clause_id for o in certified
                    },
                    "fingerprint": state.tree.fingerprint(),
                }
            )

        # Which clauses nobody has operationalised. Only meaningful where two
        # or more compilations of the same document exist, so the template says so
        # when they are not.
        by_fingerprint: dict[str, list[dict]] = {}
        for compiled in compilations:
            by_fingerprint.setdefault(compiled["fingerprint"], []).append(compiled)
        cohorts = [
            {
                "fingerprint": fingerprint,
                "compilations": group,
                "covered_by_any": set().union(
                    *(f["certified_clauses"] for f in group)
                )
                if group
                else set(),
                "covered_by_all": set.intersection(
                    *(f["certified_clauses"] for f in group)
                )
                if group and all(f["certified_clauses"] for f in group)
                else set(),
            }
            for fingerprint, group in by_fingerprint.items()
        ]
        cohorts.sort(key=lambda c: -len(c["compilations"]))

        return templates.TemplateResponse(
            request,
            "supervisor.html",
            {
                "nav": "supervisor",
                "user": signed_in,
                "compilations": compilations,
                "cohorts": cohorts,
                "multi_copy": any(len(c["compilations"]) > 1 for c in cohorts),
                "skipped_uncompiled": skipped_uncompiled,
                "skipped_unparsed": skipped_unparsed,
                "visible_total": len(visible),
                # The other half of this screen, and the half its title was
                # already promising. Everything above counts compiled
                # documents, under a variable that used to be called `firms`,
                # so a broker declaring three rulebooks appeared there as three
                # firms. This counts firms.
                "view": _supervisory_view(visible),
                # And, behind an explicit switch, five firms that do not exist,
                # so the question a supervisor could ask across a market can be
                # shown without inventing one. Never written anywhere, never
                # counted in a published figure, labelled on every row.
                "market": synthetic_market() if request.query_params.get("demo") else None,
            },
        )

    def _supervisory_view(visible):
        """One row per firm per framework, from the sidecars beside each store.

        Deliberately reads no PDF and runs no engine. A supervisory screen that
        re-derives a dozen firms' positions on every load is a screen that times
        out, and it was that exact mistake which made this page unusable before.
        Everything below is JSON already on disk.
        """
        import datetime as _d

        from sanhita.assess import AssessmentLog, evidence_fingerprint, rulebook_fingerprint
        from sanhita.company import Company
        from sanhita.execute import EvidenceStore
        from sanhita.remediate import RemediationStore
        from sanhita.supervise import build_view

        scope = _SCOPE.get()

        def beside(space, name: str) -> Path:
            if not scope:
                return space.store_path.with_name(name)
            stem, _, suffix = name.rpartition(".")
            return space.store_path.with_name(f"{stem}.{scope}.{suffix}")

        entries = []
        for space in visible:
            company = Company.load(beside(space, "company.json"))
            if company is None or not company.name:
                entries.append({"company": None})
                continue

            try:
                registry = _load_registry(space.store_path)
            except (OSError, ValueError):  # pragma: no cover - unreadable store
                registry = None
            obligations = registry.all_current() if registry is not None else []
            certified = sum(
                1 for o in obligations if o.status is RuleStatus.CERTIFIED
            )

            evidence_path = beside(space, "evidence.json")
            evidence = EvidenceStore.load(evidence_path) if evidence_path.is_file() else None
            log = AssessmentLog.load(beside(space, "assessments.json"))
            latest = log.latest

            # The recorded run only counts as this firm's position if both its
            # inputs still match, which is the same test the firm's own
            # overview applies. Two screens must not disagree about that.
            recorded = None
            if latest is not None and evidence is not None and obligations:
                inputs = (
                    rulebook_fingerprint(obligations),
                    evidence_fingerprint(evidence),
                )
                for run in reversed(log.runs):
                    if run.inputs() == inputs:
                        recorded = run
                        break

            tasks = RemediationStore.load(beside(space, "remediation.json"))
            window = evidence.window if evidence is not None else None
            entries.append(
                {
                    "company": company,
                    "framework_id": space.id,
                    "framework_name": space.name,
                    "certified": certified,
                    "recorded": recorded,
                    "latest_run": latest,
                    "open_tasks": len(tasks.open_tasks()),
                    "records": len(evidence) if evidence is not None else 0,
                    "days_since_record": (
                        (_d.date.today() - window[1]).days if window else None
                    ),
                }
            )
        return build_view(entries)

    @app.get("/facts", response_class=HTMLResponse)
    def facts(request: Request):
        """Every claim we make, with the number read live and the way to check it.

        A deck goes stale the moment it is exported. This page cannot, because
        nothing on it is typed in: every figure is read from the parse, the
        store or the test suite at the moment you load it. If a slide and this
        page disagree, this page is right.

        **Always about the worked example, whatever else is open.** These are
        the numbers the submission claims, and the submission is about the
        stock broker circular. Reading them off whichever document a visitor
        last uploaded would mean the page said something different for every
        person who opened it, which is the opposite of what it is for. The
        screen names the document so nobody mistakes it for a reading of
        theirs.
        """
        from sanhita.analyse import (
            assess_divergence,
            find_conflicts,
            humanise,
            measure_burden,
            measure_latency,
        )
        from sanhita.parse.footnotes import extract_footnotes

        state = bench(BUILTIN_ID, request)
        stats = state.tree.stats
        rules = state.registry.all_current()
        burden = measure_burden(rules)
        conflicts = find_conflicts(rules)
        divergence = assess_divergence(rules, ledger=state.registry.ledger)
        latency = measure_latency(rules, issued_on=state.workspace.issued_on)
        result = scored(state)
        notes = extract_footnotes(state.tree.document, state.tree.clause_of_line)
        detection = result.metrics.get("obligation detection")
        # Published only because the gold set is signed off. Until it was,
        # these were computed on every run and held back.
        actor = result.metrics.get("actor")
        modality = result.metrics.get("modality")
        deadline = result.metrics.get("deadline kind")
        report = compute_coverage(
            state.tree, rules, classifier_accuracy=result.classifier_accuracy
        )

        certified = [o for o in rules if o.status is RuleStatus.CERTIFIED]
        blocked = [o for o in rules if o.blocking_issues()]

        groups = [
            (
                "The document",
                [
                    ("Pages", f"{stats.page_count}", "sanhita ingest"),
                    ("Sections", f"{stats.sections}", "sanhita ingest"),
                    ("Clauses parsed", f"{stats.total_nodes:,}", "sanhita ingest"),
                    ("Characters extracted", f"{stats.document_chars:,}", "sanhita ingest"),
                    ("Source SHA-256", state.workspace.doc_sha256 or "see ingest", "sanhita ingest"),
                    ("Parse time", f"{stats.parse_seconds:.2f} seconds", "sanhita ingest"),
                ],
            ),
            (
                "Provenance",
                [
                    ("Footnote definitions", f"{notes.definition_count}", "sanhita footnotes"),
                    ("Resolved to a clause", f"{notes.resolved_count}", "sanhita footnotes"),
                    ("Ambiguous", f"{len(notes.ambiguous_markers)}", "sanhita footnotes"),
                    ("Circular references in body", f"{len(notes.body_refs)}", "sanhita footnotes"),
                    ("Tree fingerprint", state.tree.fingerprint(), "sanhita verify"),
                ],
            ),
            (
                "Compiled",
                [
                    ("Rules compiled", f"{len(rules):,}", "sanhita compile"),
                    ("Certified and signed", f"{len(certified)}", "sanhita audit"),
                    ("Blocked on a human decision", f"{len(blocked)}", "the queue"),
                    (
                        "Clauses that carry a duty",
                        f"{report.obligation_bearing_clauses:,}",
                        "sanhita coverage",
                    ),
                    # Three rungs, never one. Quoting the middle alone invites
                    # the reading that the compiler failed on the rest.
                    (
                        "Compiled, of clauses that carry a duty",
                        f"{report.compiled_coverage:.1%} "
                        f"({report.clauses_with_any_rule:,} of "
                        f"{report.obligation_bearing_clauses:,})",
                        "sanhita coverage",
                    ),
                    (
                        "Certified, of clauses that carry a duty",
                        f"{report.clause_coverage:.1%} "
                        f"({report.clauses_with_certified:,} of "
                        f"{report.obligation_bearing_clauses:,}), limited by reviewer hours",
                        "sanhita coverage",
                    ),
                    (
                        "Mapped to evidence, of certified rules",
                        f"{report.evidence_coverage:.1%} "
                        f"({report.certified_with_evidence} of "
                        f"{report.certified_obligations})",
                        "sanhita coverage",
                    ),
                ],
            ),
            (
                "What the regulation costs",
                [
                    (
                        "Heaviest obligation holder",
                        (
                            f"{burden.heaviest.actor.replace('_', ' ').lower()}, "
                            f"{burden.heaviest.duties} duties across "
                            f"{len(burden.heaviest.clauses)} clauses"
                        )
                        if burden.heaviest
                        else "nothing compiled",
                        "the load screen",
                    ),
                    (
                        "Compliance occasions a year, that actor",
                        f"{burden.heaviest.filings_per_year:,}"
                        if burden.heaviest
                        else "0",
                        "the load screen, 250 trading days to a year",
                    ),
                    (
                        "Contradictions inside the circular",
                        f"{len(conflicts.contradictions)} "
                        f"(plus {len(conflicts.duplications)} duplications, "
                        "which are not the same thing)",
                        "the contradictions screen",
                    ),
                    (
                        "Clauses the market is most likely to read two ways",
                        f"{len(divergence.high)} carrying two or more signals, "
                        f"of {divergence.clauses_examined:,} examined",
                        "the divergence screen",
                    ),
                ],
            ),
            (
                "From published text to an operating rule",
                [
                    (
                        "Time to read the whole circular",
                        f"{humanise(latency.compile_window)}, "
                        f"{latency.rules_proposed:,} typed obligations",
                        "the document screen",
                    ),
                    (
                        "Extraction engine",
                        latency.engine_summary,
                        "recorded on every rule",
                    ),
                    (
                        "Time to the first signed rule",
                        humanise(latency.time_to_first_certified),
                        "elapsed, including time nobody was at the desk",
                    ),
                    (
                        "Sat before anyone ran it",
                        humanise(latency.shelf_time)
                        + ", which measures us, not the pipeline",
                        "issued 17 June 2025",
                    ),
                ],
            ),
            (
                "Measured, not asserted",
                [
                    (
                        "Obligation detection F1",
                        f"{detection.f1:.3f}" if detection else "not scored",
                        "sanhita eval, 40-clause gold set"
                        + ("" if result.publishable else ", gold set not yet signed off"),
                    ),
                    (
                        "Denominator classifier accuracy",
                        f"{result.classifier_accuracy:.1%}",
                        "sanhita eval"
                        + ("" if result.publishable else ", gold set not yet signed off"),
                    ),
                    (
                        "Actor accuracy",
                        f"{actor.accuracy:.1%}" if actor else "not scored",
                        "sanhita eval, of the clauses both the gold set and the "
                        "extractor agree carry a duty",
                    ),
                    (
                        "Modality accuracy",
                        f"{modality.accuracy:.1%}" if modality else "not scored",
                        "sanhita eval, same denominator",
                    ),
                    (
                        "Deadline kind accuracy",
                        f"{deadline.accuracy:.1%}" if deadline else "not scored",
                        "sanhita eval, same denominator",
                    ),
                    (
                        "Cost to compile the corpus",
                        "$0.00",
                        "the rules engine makes no API call",
                    ),
                ],
            ),
        ]

        # Stated plainly, on the same page as the good numbers.
        limits = [
            (
                "The parser was built against one circular, and has since been "
                "tested against six more",
                "Seven SEBI master circulars now parse: stock brokers, mutual "
                "funds, depositories, and both the June 2025 and February 2026 "
                "editions of the investment adviser and research analyst "
                "circulars. 7,506 clauses and 3,743 rules in total. Doing that "
                "found a real defect: body text size had been measured once, on "
                "the stock broker circular, and written into the source as 12pt. "
                "The June 2025 research analyst circular is set at 11.3pt, so "
                "every line in it was classified as furniture and a document "
                "with 139 numbered clauses parsed to none. The threshold is now "
                "measured per document. The upload screen still states a verdict "
                "of Readable, With care, or No, and still refuses to compile a "
                "document it could not read.",
            ),
            (
                "One real amendment has been replayed, not twenty",
                "SEBI issued the Master Circular for Investment Advisers in June "
                "2025 and reissued it in February 2026. Both editions are in "
                "corpus/ and the diff between them is real: 57 clauses added, 39 "
                "removed, 5 amended and 376 renumbered. Compiling the June "
                "edition, signing 25 rules on clauses that moved and comparing "
                "produced 82 required actions for one firm. The deck said 20+ "
                "amendments; the true number was zero and is now one.",
            ),
            (
                "The model-assisted extractor is proven but is not used in bulk",
                "It was run live against clause 40.1.8 and returned two "
                "obligations where the rules engine found one, correctly "
                "noticing the clause binds both a stock broker and a clearing "
                "member. It also left the day count unresolved rather than "
                "assuming a convention. But it took 105 seconds for that one "
                "clause, so compiling the whole circular this way would take "
                "roughly forty hours and cost real money. The rules engine does "
                "all 1,377 in about a second for nothing, which is why the "
                "model is reserved for clauses the rules cannot handle.",
            ),
            (
                "No firm has given us their books",
                "A gap report needs a firm's own filing records, and this "
                "installation has whatever has been uploaded to it and nothing "
                "else. There is no longer a fallback to generated events: a "
                "firm with no records is told it has none and offered the "
                "upload, rather than shown a compliance percentage computed "
                "from a random number generator. Generated events still exist "
                "for demonstrating the engine, but somebody has to ask for them "
                "with ?demo=1 and every screen using them says so.",
            ),
            (
                "Regulatory monitoring watches this installation, not sebi.gov.in",
                "The overview says on every load whether a later edition of a "
                "declared rulebook is on file and has never been compared "
                "against the one in use. Nothing polls a website, subscribes to "
                "a feed or fetches anything over a network, so a circular "
                "published this week is invisible until somebody uploads it. "
                "What it removes is the other failure, where the newer circular "
                "has been in the system since March and no comparison was ever "
                "run.",
            ),
            (
                "The gold set is 40 clauses, and every arguable one went against us",
                "Accuracy is measured against 40 clauses labelled by hand. Seven "
                "of those labels were ones where the hand and the machine "
                "disagreed, and they were settled by the project's owner rather "
                "than by the person who wrote the extractor, because a gold set "
                "signed off by its own author cannot measure anything. All seven "
                "were ruled in favour of the human label, which means all seven "
                "went against the extractor: had they gone the other way, "
                "obligation detection would read 1.000 F1 instead of 0.875 and "
                "actor accuracy 100% instead of 95.2%. Forty clauses is a small "
                "gold set and the figures carry that uncertainty.",
            ),
            (
                "The key these 183 signatures were made with has been lost",
                "Each certification carries an HMAC over the rule's canonical "
                "bytes, and the deployment key that produced them is gone. So "
                "they verify against nothing: /audit/verify reports 183 checked, "
                "0 valid. That is stated here rather than quietly left for "
                "somebody to find, and it is worth being precise about what it "
                "does and does not mean. What is lost is the proof that these "
                "183 rules are unaltered since the day they were signed. What "
                "is intact is everything else: each rule still carries the "
                "clause it came from, that clause's own SHA-256, the page, the "
                "character span, the officer's name, the timestamp and the "
                "hash-chained ledger entry for the act of certifying. Nothing "
                "about the rules changed; the witness to their not having "
                "changed did. Recomputing the signatures under a current key "
                "would make the endpoint green and would be a different claim, "
                "so it has not been done silently.",
            ),
            (
                "A signature covers the rule's content, not the officer's identity",
                "One HMAC key belongs to this deployment rather than to each "
                "officer, so the signature proves the rule has not changed since "
                "it was signed and proves nothing cryptographic about who signed "
                "it. What the name now means is the account that was "
                "authenticated: certifying, rejecting and amending need an "
                "account and record it, rather than accepting a name typed into "
                "a box. A per-officer key is a real change to the trust model "
                "and is not claimed here.",
            ),
        ]

        return templates.TemplateResponse(
            request,
            "facts.html",
            {
                "nav": "facts",
                "user": current_user(request),
                "groups": groups,
                "limits": limits,
                "document_name": state.workspace.name,
                "generated_at": _dt.datetime.now(_dt.timezone.utc),
            },
        )

    # ═══════════════════════════════════════════════════════ accounts ══

    #: Where somebody goes after signing in.
    #
    # This used to be /documents, which opens with "Drop a SEBI circular here".
    # That is the regulatory authoring workflow: somebody bringing a rulebook in
    # so it can be compiled and certified. It is not what the primary user
    # arrives to do. A compliance officer at an intermediary signs in to find
    # out whether their own firm is complying, and being asked for a SEBI PDF
    # first tells them they are in the wrong product.
    #
    # The rulebook workflow is unchanged and still reachable, under Advanced.
    HOME = "/w/" + BUILTIN_ID + "/company"


    @app.get("/signin", response_class=HTMLResponse)
    def signin_form(request: Request, next: str = "", error: str | None = None):
        return templates.TemplateResponse(
            request,
            "signin.html",
            {
                "nav": "auth",
                "mode": "signin",
                "next": next or HOME,
                "error": error,
                "any_users": users.any_users,
                "key_present": _session.session_key() is not None,
                "user": current_user(request),
            },
        )

    @app.get("/signup", response_class=HTMLResponse)
    def signup_form(request: Request, error: str | None = None):
        return templates.TemplateResponse(
            request,
            "signin.html",
            {
                "nav": "auth",
                "mode": "signup",
                "next": HOME,
                "error": error,
                "any_users": users.any_users,
                "key_present": _session.session_key() is not None,
                "user": current_user(request),
            },
        )

    def _scope_roots() -> list[Path]:
        """Every directory that holds a scoped sidecar.

        The store root keeps `company.json`; each workspace keeps the rest
        beside its own `rules.json`.
        """
        roots = {builtin_store.parent}
        for space in [workspaces.builtin(), *workspaces.uploaded()]:
            roots.add(space.store_path.parent)
        return sorted(roots)

    def _adopt(request: Request, user) -> str:
        """Carry an anonymous visitor's firm data into the account they just
        made, and say plainly when it could not be carried.

        Without this a visitor could walk the entire journey, sign up at the
        end of it, and watch their firm vanish: the scope moves from the cookie
        token to the account and nothing moves the files. They are still on
        disk, which is exactly why the bug survived, because anybody looking
        afterwards finds the data intact and concludes it worked.
        """
        from sanhita.web.adopt import adopt_visitor_data

        if not shared_deployment():
            return ""
        visitor = request.cookies.get(VISITOR_COOKIE, "")
        result = adopt_visitor_data(
            roots=_scope_roots(),
            visitor_scope=visitor,
            user_scope=f"u{user.id}",
        )
        return result.describe() if result.collided else ""

    def _signed_in(user, destination: str) -> RedirectResponse:
        response = RedirectResponse(destination, status_code=303)
        cookie = _session.issue(user.id)
        if cookie:
            response.set_cookie(
                _session.COOKIE_NAME,
                cookie,
                max_age=_session.MAX_AGE_SECONDS,
                httponly=True,
                samesite="lax",
                path="/",
            )
        return response

    @app.post("/signup")
    def signup(
        request: Request,
        email: str = Form(...),
        name: str = Form(...),
        password: str = Form(...),
    ):
        if _session.session_key() is None:
            raise HTTPException(
                400,
                f"{_KEY_ENV} is not set. Sessions are signed with a key derived "
                "from it, so accounts cannot be used without one.",
            )
        try:
            user = users.create(email=email, name=name, password=password)
        except AuthError as exc:
            return RedirectResponse(f"/signup?error={quote(str(exc))}", status_code=303)
        # Whatever this person did before they signed up is theirs, and the
        # scope is about to change out from under it.
        problem = _adopt(request, user)
        return _signed_in(user, f"{HOME}?carried={quote(problem)}" if problem else HOME)

    @app.post("/signin")
    def signin(
        request: Request,
        email: str = Form(...),
        password: str = Form(...),
        next: str = Form(""),
    ):
        # Throttled per address, because a password guesser has no account yet.
        # scrypt already costs about 100ms an attempt, but that is mitigation by
        # accident; this is the deliberate one.
        caller = request.client.host if request.client else "unknown"
        try:
            signin_limiter.check(f"signin:{caller}")
        except RateLimited:
            return RedirectResponse(
                "/signin?error="
                + quote(
                    "Too many sign-in attempts from this address. Wait a few "
                    "minutes and try again."
                ),
                status_code=303,
            )

        try:
            user = users.authenticate(email, password)
        except AuthError as exc:
            return RedirectResponse(f"/signin?error={quote(str(exc))}", status_code=303)
        destination = (
            next if next.startswith("/") and not next.startswith("//") else HOME
        )
        problem = _adopt(request, user)
        if problem and "?" not in destination:
            destination = f"{destination}?carried={quote(problem)}"
        return _signed_in(user, destination)

    @app.post("/signout")
    def signout():
        response = RedirectResponse("/signin", status_code=303)
        response.delete_cookie(_session.COOKIE_NAME, path="/")
        return response

    # ══════════════════════════════════════════════════════════ errors ══

    #: What each status means in words, rather than in HTTP.
    _HEADLINES = {
        400: "That request could not be used",
        401: "You need an account for that",
        404: "There is nothing at that address",
        409: "Something else is using that right now",
        410: "That is gone",
        413: "That file is too large",
        429: "Too many requests",
        500: "Something broke on our side",
    }

    def _wants_json(request: Request) -> bool:
        """JSON callers get JSON. Browsers get the page."""
        accept = request.headers.get("accept", "")
        return "application/json" in accept and "text/html" not in accept

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        code = exc.status_code
        if _wants_json(request):
            return JSONResponse({"detail": exc.detail}, status_code=code)
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "nav": "error",
                "code": code,
                "headline": _HEADLINES.get(code, "That did not work"),
                "detail": exc.detail,
                "user": current_user(request),
            },
            status_code=code,
        )

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        """Never show a stack trace to a browser, and never pretend it was fine."""
        logger.exception("unhandled error on %s", request.url.path)
        if _wants_json(request):
            return JSONResponse({"detail": "internal error"}, status_code=500)
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "nav": "error",
                "code": 500,
                "headline": _HEADLINES[500],
                "detail": (
                    "The request failed part way through. The details are in the "
                    "server log rather than on this page, because an error "
                    "message is not a place to leak the shape of a system."
                ),
                "user": current_user(request),
            },
            status_code=500,
        )

    @app.get("/healthz")
    def healthz():
        state = bench(BUILTIN_ID)
        return {
            "ok": True,
            "pdf": state.pdf.name,
            "fingerprint": state.tree.fingerprint(),
            "rules": len(state.registry),
            "ledger": len(state.registry.ledger),
            "documents": len(workspaces.visible_to()),
        }

    return app


def _mint_visitor_token() -> str:
    """An opaque handle for an anonymous visitor. Carries no information."""
    import secrets

    return secrets.token_hex(8)


def _sidecar(state: Workbench, name: str) -> Path:
    """Where a workspace keeps one kind of the firm's own data.

    Beside the rules rather than inside them: the firm's data is the firm's, the
    rulebook is the regulator's, and deleting one must never disturb the other.
    Adding any of this to a signed obligation would also invalidate all 183
    existing signatures.

    On a shared deployment the name carries the visitor's scope, so the worked
    example can be walked end to end by two people at once without either of
    them seeing the other's filing register. Off a shared deployment the name is
    plain, which is what a single-user laptop should have.
    """
    scope = _SCOPE.get()
    plain = state.store_path.with_name(name)
    if not scope:
        return plain
    stem, _, suffix = name.rpartition(".")
    scoped = state.store_path.with_name(f"{stem}.{scope}.{suffix}")
    return _seeded_or(scoped, plain)


def _seeded_or(scoped: Path, seed: Path) -> Path:
    """This visitor's own copy, or the seeded demonstration until they write.

    Without this a shared deployment opens on an empty onboarding screen for
    everybody, because the demonstration state seeded by `sanhita demo-seed` is
    unscoped and no visitor's scope matches it. The public site would then show
    none of what the product does until a reviewer typed a firm in themselves.

    So an unscoped file is readable by anyone as a starting point, and the first
    write puts the visitor on their own copy. That is safe in a way that
    sharing generally is not, because the seeded firm is synthetic and marked
    as such: there is nothing in it belonging to a person. A visitor's own
    records never go back into it, and one visitor's changes are never visible
    to another.
    """
    if scoped.exists() or not seed.is_file():
        return scoped
    return seed


def _writable(state: Workbench, name: str) -> Path:
    """Where this visitor's own copy belongs, seeded or not.

    Reads may fall through to the demonstration state; writes never do. A
    visitor editing the seed would change what the next visitor opens on.
    """
    scope = _SCOPE.get()
    if not scope:
        return state.store_path.with_name(name)
    stem, _, suffix = name.rpartition(".")
    return state.store_path.with_name(f"{stem}.{scope}.{suffix}")


def _evidence_path(state: Workbench) -> Path:
    """This firm's imported filing records, or the seeded ones until it has any."""
    return _sidecar(state, "evidence.json")


def _evidence_write_path(state: Workbench) -> Path:
    """Where this visitor's own records are saved. Never the seeded ones."""
    return _writable(state, "evidence.json")


def _assessments(state: Workbench):
    """The history of assessments run in this workspace.

    Another sidecar beside the rest, for the same reason as the others: adding
    a field to a signed obligation would invalidate all 183 signatures, and a
    firm's assessment history is not part of the regulator's rulebook anyway.
    """
    from sanhita.assess import AssessmentLog

    log = AssessmentLog.load(_sidecar(state, "assessments.json"))
    log.path = _writable(state, "assessments.json")
    return log


def _company(state: Workbench):
    """The firm, or None until somebody has said who they are.

    Read from above every rulebook rather than from inside one. The same firm
    is the same firm whichever circular you happened to arrive through, and a
    profile that changed when you switched documents was the clearest symptom
    of the company being a property of the regulation instead of the reverse.
    """
    from sanhita.company import Company

    return Company.load(_company_path(state))


def _company_path(state: Workbench) -> Path:
    """Beside the store root, not beside a workspace's rules.

    Still scoped per visitor on a shared deployment, for the same reason the
    evidence is: a firm's name and its business facts are the firm's.
    """
    root = state.company_root or state.store_path.parent
    scope = _SCOPE.get()
    if not scope:
        return root / "company.json"
    return _seeded_or(root / f"company.{scope}.json", root / "company.json")


def _company_write_path(state: Workbench) -> Path:
    """Where a visitor's own profile is saved. Never the seeded one."""
    root = state.company_root or state.store_path.parent
    scope = _SCOPE.get()
    return root / (f"company.{scope}.json" if scope else "company.json")


def _review(state: Workbench):
    """The queue of uploaded evidence awaiting somebody's judgement.

    A fifth file beside the others. Candidates are neither the regulator's text
    nor confirmed evidence; they are things a document appeared to say, held
    until a person rules on them.
    """
    from sanhita.company import ReviewQueue

    queue = ReviewQueue.load(_sidecar(state, "review.json"))
    queue.path = _writable(state, "review.json")
    return queue


def _remediation(state: Workbench):
    """This workspace's remediation tasks and their hash-chained log.

    A fourth file beside the rulebook, the evidence and the control bindings.
    Remediation is what the firm did about a finding, which is neither the
    regulator's text nor the firm's filing history, and it has its own chain so
    the certification ledger stays exactly what it was.
    """
    from sanhita.remediate import RemediationStore

    store = RemediationStore.load(_sidecar(state, "remediation.json"))
    store.path = _writable(state, "remediation.json")
    return store


def _controls(state: Workbench):
    """This workspace's control bindings.

    A third file beside the other two, for the same reason: a binding is a
    claim about how one firm is organised, which is neither the regulator's
    text nor the firm's filing history. Read fresh on every request rather than
    cached, because it is small and a stale ownership map is worse than a
    slightly slower page.
    """
    from sanhita.controls import ControlStore

    controls = ControlStore.load(_sidecar(state, "controls.json"))
    controls.path = _writable(state, "controls.json")
    return controls


def _llm_problem() -> str | None:
    """Why the LLM extractor cannot run right now, or None if it can."""
    from sanhita.compile.llm import LLMExtractor

    return LLMExtractor.credential_error()


def _field_rows(obligation: Obligation) -> list[dict]:
    """The right-hand pane: one row per compiled field, with its provenance."""
    rows: list[dict] = []

    def add(name: str, label: str, value: object, *, mono: bool = False) -> None:
        if value in (None, "", [], {}):
            return
        rows.append(
            {
                "name": name,
                "label": label,
                "value": value,
                "quote": obligation.quote(name),
                "confidence": obligation.field_confidence.get(name),
                "mono": mono,
            }
        )

    add("actor", "Actor", obligation.actor.value, mono=True)
    add("modality", "Modality", obligation.modality.value, mono=True)
    add("action.verb", "Action verb", obligation.action.verb)
    add("action.object", "Action object", obligation.action.object)
    add("action.recipient", "Recipient", obligation.action.recipient)
    add("action.medium", "Medium", obligation.action.medium)

    trigger = obligation.trigger
    add("trigger", "Trigger", f"{trigger.kind.value} · {trigger.expression}")
    add("trigger.recurrence", "Recurrence", trigger.recurrence, mono=True)

    deadline = obligation.deadline
    if deadline is not None:
        add("deadline", "Deadline", _deadline_label(deadline), mono=True)

    for index, condition in enumerate(obligation.conditions):
        add(f"conditions[{index}]", f"Condition {index + 1}", f"{condition.kind.value} · {condition.expression}")
    for index, evidence in enumerate(obligation.evidence):
        add(f"evidence[{index}]", f"Evidence {index + 1}", evidence.artifact_type, mono=True)

    add("penalty_ref", "Penalty reference", obligation.penalty_ref)
    return rows


def _deadline_label(deadline: Deadline) -> str:
    bits = [deadline.kind.value]
    if deadline.offset_days is not None:
        bits.append(f"{deadline.offset_days} days")
    if deadline.offset_hours is not None:
        bits.append(f"{deadline.offset_hours} hours")
    if deadline.offset_months is not None:
        bits.append(f"{deadline.offset_months} months")
    if deadline.period:
        bits.append(deadline.period)
    if deadline.offset_days is not None:
        bits.append(
            {
                DayCount.BUSINESS: "working days",
                DayCount.CALENDAR: "calendar days",
                DayCount.UNSPECIFIED: "convention UNSPECIFIED",
            }[deadline.business_days]
        )
    if deadline.anchor_event:
        bits.append(f"from {deadline.anchor_event}")
    return " · ".join(bits)
