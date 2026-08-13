"""Workspaces: one document, one rulebook, one audit chain.

Until now Sanhita compiled exactly one circular into exactly one store. That is
fine for a command line, and useless for a person who arrives at the site with
their own document.

A workspace is that person's copy of the whole pipeline: their PDF, their parsed
tree, their proposed rules, their certifications, their ledger. Nothing crosses
between workspaces. Deleting one deletes everything it produced.

    .sanhita/
      rules.json                 the built-in worked example, unchanged
      workspaces/
        <id>/
          meta.json
          source.pdf
          rules.json

The id is derived from the document's own bytes, so uploading the same PDF twice
lands in the same workspace instead of silently creating a second copy that
would then disagree with the first.

**On authentication.** A workspace carries an ``owner`` field that is currently
always ``None``, meaning "anyone using this machine". When sign-in is added, the
owner becomes the authenticated user id and ``WorkspaceStore.visible_to`` is the
one place that has to learn about it. No route, template or pipeline stage needs
to change.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "BUILTIN_ID",
    "MAX_PAGES",
    "MAX_TOTAL_BYTES",
    "MAX_UPLOAD_BYTES",
    "RateLimited",
    "RateLimiter",
    "UploadRejected",
    "Workspace",
    "WorkspaceStore",
]

BUILTIN_ID = "demo"

#: A master circular runs to about 5 MB. Anything far past that is not a
#: circular, and we would rather say so than spend a minute finding out.
MAX_UPLOAD_BYTES = 40 * 1024 * 1024

#: Parsing is linear in pages but the whole point is a person waiting on it.
MAX_PAGES = 1200

#: Uploads allowed from one caller inside ``RATE_WINDOW_SECONDS``. Parsing a
#: 400 page circular costs real CPU, so an open upload endpoint is a way to
#: take the machine down or fill its disk. Generous enough that nobody
#: legitimately working through a stack of circulars will notice.
RATE_LIMIT_UPLOADS = 12
RATE_WINDOW_SECONDS = 300

#: Total bytes all uploaded workspaces may occupy before new uploads are
#: refused. Deleting a document frees its share immediately.
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024

_SAFE_NAME = re.compile(r"[^A-Za-z0-9 ._()-]+")


class UploadRejected(ValueError):
    """The uploaded bytes are not something we are willing to parse.

    Carries a sentence meant to be shown to the person who uploaded it, not a
    stack trace.
    """


class RateLimited(UploadRejected):
    """Too many uploads from one caller too quickly."""


class RateLimiter:
    """A fixed window counter, per caller.

    In process and not shared between workers, which is the right size for a
    tool that runs on one machine. If this ever runs behind more than one
    worker, this moves to whatever the deployment already uses for shared
    state; the call site does not change.
    """

    def __init__(
        self,
        limit: int = RATE_LIMIT_UPLOADS,
        window_seconds: int = RATE_WINDOW_SECONDS,
    ) -> None:
        self.limit = limit
        self.window = window_seconds
        self._seen: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, caller: str, *, now: float | None = None) -> None:
        """Raise ``RateLimited`` if this caller has had its allowance."""
        moment = time.monotonic() if now is None else now
        with self._lock:
            recent = [t for t in self._seen.get(caller, ()) if moment - t < self.window]
            if len(recent) >= self.limit:
                wait = int(self.window - (moment - recent[0])) + 1
                recent_count = len(recent)
                self._seen[caller] = recent
                raise RateLimited(
                    f"That is {recent_count} uploads in the last "
                    f"{self.window // 60} minutes, which is the limit. Parsing a "
                    f"long circular is real work, so uploads are paced. Try again "
                    f"in about {wait // 60 + 1} minute(s)."
                )
            recent.append(moment)
            self._seen[caller] = recent


#: Hashing a 399 page PDF is a few milliseconds, but the worked example is
#: rebuilt on every request that resolves a workspace, so it is memoised on the
#: file's path, size and modification time. A file replaced on disk gets a new
#: key and is hashed again.
_DIGESTS: dict[tuple[str, int, int], str] = {}


def _digest_of(path: Path) -> str:
    """The SHA-256 of a file on disk, or empty if it is not there."""
    try:
        stat = path.stat()
    except OSError:
        return ""
    key = (str(path), stat.st_size, int(stat.st_mtime))
    cached = _DIGESTS.get(key)
    if cached is None:
        cached = _DIGESTS[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return cached


@dataclass
class Workspace:
    """One document and everything derived from it."""

    id: str
    name: str
    source_name: str
    doc_sha256: str
    created_at: _dt.datetime
    root: Path
    builtin: bool = False
    #: Reserved for sign-in. ``None`` means "anyone on this machine".
    owner: str | None = None
    #: The date printed on the regulation itself, where it is known.
    #:
    #: Not the same thing as ``created_at``, which is when this machine first
    #: saw the document. The gap between the two is how long the circular sat
    #: before anybody compiled it, and conflating them would let that gap be
    #: reported as though it were a measurement of the pipeline.
    issued_on: _dt.date | None = None
    #: The built-in keeps its historical file locations so an existing checkout
    #: does not lose the work already certified against it.
    pdf_override: Path | None = None
    store_override: Path | None = None

    @property
    def pdf_path(self) -> Path:
        return self.pdf_override or (self.root / "source.pdf")

    @property
    def store_path(self) -> Path:
        return self.store_override or (self.root / "rules.json")

    @property
    def short_hash(self) -> str:
        return self.doc_sha256[:12]

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "source_name": self.source_name,
            "doc_sha256": self.doc_sha256,
            "created_at": self.created_at.isoformat(),
            "issued_on": self.issued_on.isoformat() if self.issued_on else None,
            "owner": self.owner,
        }


def _issue_date(pdf: Path) -> _dt.date | None:
    """The date printed on the circular's own first page, or nothing.

    Recorded at upload because it is the only thing that can order two
    editions of the same rulebook. Without it a later circular sitting in the
    system is indistinguishable from an earlier one, and the regulatory watch
    that tells a firm "a newer edition has arrived and nobody has looked at it"
    has nothing to compare.

    Deliberately not the upload date. When this machine first saw a document
    says nothing about when SEBI issued it, and conflating the two would let a
    2024 circular uploaded today outrank a 2026 one uploaded last week.

    Reads page one only, and swallows every failure: a document whose date
    cannot be read is undated, which the screens report honestly. Failing an
    upload because a date was unparseable would be absurd.
    """
    try:
        import fitz

        from sanhita.parse.footnotes import read_issue_date

        with fitz.open(pdf) as document:
            if not len(document):
                return None
            found = read_issue_date(document[0].get_text())
    except Exception:  # pragma: no cover - a broken PDF is the upload check's job
        return None
    return found[0] if found else None


class WorkspaceStore:
    """The set of workspaces on this machine."""

    def __init__(self, root: Path, *, builtin_pdf: Path, builtin_store: Path) -> None:
        self.root = root
        self._builtin_pdf = builtin_pdf
        self._builtin_store = builtin_store

    # ------------------------------------------------------------------ read

    def builtin(self) -> Workspace:
        """The worked example, backed by the corpus PDF and the original store.

        It is a real workspace in every respect except that it cannot be
        deleted and its files live where they always have, so an existing
        checkout keeps all of its certified work.

        Its hash is read from the file rather than stored, because there is no
        upload moment at which to record one. Every other artifact in this
        product names the SHA-256 of the document behind it, and the worked
        example is the one every visitor sees first, so it cannot be the one
        that says "not recorded".
        """
        return Workspace(
            id=BUILTIN_ID,
            name="Stock Brokers Master Circular",
            source_name=self._builtin_pdf.name,
            doc_sha256=_digest_of(self._builtin_pdf),
            created_at=_dt.datetime(2025, 6, 17, tzinfo=_dt.timezone.utc),
            #: The date on the circular's own masthead, and the date in its
            #: filename. Kept separate from ``created_at`` above, which reads
            #: the same only because the worked example has no meaningful
            #: upload moment to record.
            issued_on=_dt.date(2025, 6, 17),
            root=self._builtin_store.parent,
            builtin=True,
            pdf_override=self._builtin_pdf,
            store_override=self._builtin_store,
        )

    def _read(self, folder: Path) -> Workspace | None:
        meta_file = folder / "meta.json"
        if not meta_file.is_file():
            return None
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        issued = meta.get("issued_on")
        return Workspace(
            id=meta["id"],
            name=meta.get("name") or meta["source_name"],
            source_name=meta["source_name"],
            doc_sha256=meta["doc_sha256"],
            created_at=_dt.datetime.fromisoformat(meta["created_at"]),
            issued_on=_dt.date.fromisoformat(issued) if issued else None,
            root=folder,
            owner=meta.get("owner"),
        )

    def get(self, workspace_id: str) -> Workspace | None:
        if workspace_id == BUILTIN_ID:
            return self.builtin()
        folder = self.root / workspace_id
        # Never let an id escape the workspaces directory.
        if folder.parent != self.root or not folder.is_dir():
            return None
        return self._read(folder)

    def uploaded(self) -> list[Workspace]:
        """Every uploaded workspace, newest first."""
        if not self.root.is_dir():
            return []
        found = [w for w in (self._read(p) for p in self.root.iterdir() if p.is_dir()) if w]
        found.sort(key=lambda w: w.created_at, reverse=True)
        return found

    def may_open(self, workspace: Workspace, owner: str | None) -> bool:
        """Whether this person may open this document.

        The worked example is open to everyone; it is the thing a first-time
        visitor is meant to look at. An uploaded document belongs to whoever
        uploaded it and to nobody else.

        Workspaces carrying no owner were created before sign-in existed. They
        stay readable rather than being orphaned, because silently hiding
        somebody's existing work would be worse than the exposure, and there is
        no owner recorded to restore.
        """
        if workspace.builtin or workspace.owner is None:
            return True
        return workspace.owner == owner

    def visible_to(self, owner: str | None = None) -> list[Workspace]:
        """The workspaces a given person may open, newest first.

        Signed out, that is the worked example and any pre-sign-in documents.
        Signed in, it is those plus their own. One firm's circulars on a shared
        machine are not another's to read.
        """
        items = [w for w in self.uploaded() if self.may_open(w, owner)]
        return [self.builtin(), *items]

    # ----------------------------------------------------------------- write

    def bytes_on_disk(self) -> int:
        """How much space the uploaded workspaces are using."""
        if not self.root.is_dir():
            return 0
        return sum(f.stat().st_size for f in self.root.rglob("*") if f.is_file())

    def create(
        self,
        data: bytes,
        *,
        filename: str,
        name: str = "",
        owner: str | None = None,
    ) -> Workspace:
        """Validate uploaded bytes and lay down a workspace for them."""
        check_pdf(data)

        used = self.bytes_on_disk()
        if used + len(data) > MAX_TOTAL_BYTES:
            gb = MAX_TOTAL_BYTES / (1024**3)
            raise UploadRejected(
                f"Uploaded documents are already using {used / (1024**3):.1f} GB of "
                f"the {gb:.0f} GB allowed. Delete a document you no longer need and "
                "try again."
            )

        digest = hashlib.sha256(data).hexdigest()
        workspace_id = digest[:16]

        existing = self.get(workspace_id)
        if existing is not None:
            # Same bytes, same workspace. Uploading twice must not fork the
            # audit trail into two chains over identical text.
            return existing

        folder = self.root / workspace_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "source.pdf").write_bytes(data)

        clean = _SAFE_NAME.sub("", Path(filename).name).strip() or "circular.pdf"
        workspace = Workspace(
            id=workspace_id,
            name=(name.strip() or Path(clean).stem)[:120],
            source_name=clean,
            doc_sha256=digest,
            created_at=_dt.datetime.now(_dt.timezone.utc),
            root=folder,
            owner=owner,
            issued_on=_issue_date(folder / "source.pdf"),
        )
        (folder / "meta.json").write_text(
            json.dumps(workspace.to_json(), indent=2, sort_keys=True), encoding="utf-8"
        )
        return workspace

    def delete(self, workspace_id: str) -> bool:
        if workspace_id == BUILTIN_ID:
            return False
        folder = self.root / workspace_id
        if folder.parent != self.root or not folder.is_dir():
            return False
        shutil.rmtree(folder)
        return True


def check_pdf(data: bytes) -> None:
    """Reject anything we are not willing to hand to the parser.

    Raises ``UploadRejected`` with a sentence written for the person who
    uploaded the file.
    """
    if not data:
        raise UploadRejected("That file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        size = len(data) / (1024 * 1024)
        limit = MAX_UPLOAD_BYTES / (1024 * 1024)
        raise UploadRejected(
            f"That file is {size:.0f} MB. The limit is {limit:.0f} MB, which is "
            "already several times the size of a master circular."
        )
    if not data.lstrip()[:5].startswith(b"%PDF-"):
        raise UploadRejected(
            "That does not look like a PDF. Sanhita reads the regulator's own "
            "published PDF, because a rule has to trace back to a page and a "
            "byte range in the document that was actually issued."
        )
