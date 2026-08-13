"""Ask SEBI what it has published, when somebody presses the button.

The regulatory watch could tell a firm that a later edition was sitting in this
installation unexamined. It could not tell them a circular existed at all,
because nothing here had ever looked at sebi.gov.in. So the loop began with a
person remembering to check a website, which is the step that fails.

This closes that, and the shape of the closure matters more than the fetching.

**It is pressed, not polled.** There is no background scheduler, no daemon and
no cron. A person opens the regulatory screen and asks; the answer is as fresh
as that request and no fresher. Calling this "real-time monitoring" would be a
lie a firm could rely on, so the product calls it what it is: checking SEBI now.

**Discovery is not ingestion.** Nothing found here enters the certified
rulebook. A discovered circular is a title, a date, a URL and a hash, shown to
a person who decides whether to bring it in. The chain stays:

    discover -> review -> ingest -> parse -> diff -> impact -> a human approves

**Only sebi.gov.in.** The host is checked before a request is made and again
after redirects, and anything else is refused by name. A compliance product
that would follow a redirect to an arbitrary host, and then present what it
found as regulation, is a product that can be told what the law is by whoever
controls that redirect.

**A layout change fails loudly.** SEBI's listing is HTML written for people,
and it will change. When the page cannot be read this returns a stated failure
rather than an empty list, because an empty list is indistinguishable from
"SEBI has published nothing" and that is the one wrong answer available.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urljoin, urlparse

__all__ = [
    "OFFICIAL_HOSTS",
    "SEBI_CIRCULARS",
    "PublicationState",
    "Publication",
    "Discovery",
    "DiscoveryRefused",
    "discover",
    "fetch_official",
]

#: The only hosts this will read regulation from.
OFFICIAL_HOSTS = frozenset({"sebi.gov.in", "www.sebi.gov.in"})

#: SEBI's own listing of circulars. A constant rather than a setting, because a
#: configurable "where is the regulator" is a configurable answer to what the
#: law is.
SEBI_CIRCULARS = (
    "https://www.sebi.gov.in/sebiweb/home/HomeAction.do"
    "?doListing=yes&sid=1&ssid=7&smid=0"
)

#: How long to wait. Short: this runs inside a page load a person is watching.
TIMEOUT_SECONDS = 12
#: Enough for a listing page. A regulator's index that runs to megabytes is not
#: a listing, and reading it into memory unbounded is how a fetch becomes a
#: denial of service against yourself.
MAX_BYTES = 4 * 1024 * 1024


class DiscoveryRefused(RuntimeError):
    """The source was not SEBI, or the listing could not be read."""


class PublicationState(str, Enum):
    """What this installation already knows about one publication."""

    #: Never seen here. The interesting one.
    NEW = "NEW"
    #: Seen, but SEBI is now showing a different date or link for it.
    UPDATED = "UPDATED"
    #: Already on file.
    KNOWN = "KNOWN"

    @property
    def label(self) -> str:
        return {
            PublicationState.NEW: "New to this installation",
            PublicationState.UPDATED: "Changed since it was brought in",
            PublicationState.KNOWN: "Already on file",
        }[self]


@dataclass(frozen=True)
class Publication:
    """One circular SEBI is listing, as SEBI describes it."""

    title: str
    url: str
    issued_on: _dt.date | None = None
    state: PublicationState = PublicationState.NEW
    #: SHA-256 of the listing row this was read from, so two runs can be
    #: compared without keeping the whole page.
    row_sha256: str = ""
    #: Which local workspace it matched, when it matched one.
    known_as: str = ""

    @property
    def identity(self) -> str:
        """A stable handle for one publication, derived from its own URL.

        SEBI's URLs carry a document id, which is the closest thing to an
        identifier the listing offers. Titles repeat and dates repeat; the
        path does not.
        """
        path = urlparse(self.url).path
        return hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]

    def describe(self) -> str:
        when = f" issued {self.issued_on.isoformat()}" if self.issued_on else ""
        if self.state is PublicationState.NEW:
            return (
                f"SEBI is listing {self.title!r}{when}, and nothing on this "
                "installation matches it. Bringing it in is a decision for a "
                "person: it is downloaded, parsed and compared, and nothing is "
                "certified by finding it."
            )
        if self.state is PublicationState.UPDATED:
            return (
                f"{self.title!r}{when} is on file here as {self.known_as!r}, but "
                "SEBI is now listing it differently. Worth comparing."
            )
        return f"{self.title!r}{when} is already on file here as {self.known_as!r}."


@dataclass
class Discovery:
    """What SEBI was listing at the moment somebody asked."""

    checked_at: _dt.datetime
    source: str
    publications: list[Publication] = field(default_factory=list)
    #: Set when the listing could not be read. The screens must show this
    #: rather than an empty result, because "nothing found" and "could not
    #: look" are different answers and only one of them is reassuring.
    problem: str = ""

    @property
    def ok(self) -> bool:
        return not self.problem

    def of(self, state: PublicationState) -> list[Publication]:
        return [p for p in self.publications if p.state is state]

    @property
    def new(self) -> list[Publication]:
        return self.of(PublicationState.NEW)

    def headline(self) -> str:
        if self.problem:
            return f"SEBI's listing could not be read: {self.problem}"
        if not self.publications:
            return (
                "SEBI's listing was read and no publications were found on it. "
                "That is more likely to mean the page has changed shape than "
                "that SEBI has published nothing."
            )
        fresh = len(self.new)
        if fresh:
            return (
                f"{fresh} of the {len(self.publications)} publications SEBI is "
                "listing are not on this installation."
            )
        return (
            f"All {len(self.publications)} publications SEBI is listing are "
            "already on file here."
        )


# --------------------------------------------------------------- fetching


def _check_host(url: str) -> None:
    host = (urlparse(url).hostname or "").lower()
    if host not in OFFICIAL_HOSTS:
        raise DiscoveryRefused(
            f"{host or url!r} is not an official SEBI host. Sanhita reads "
            "regulation from sebi.gov.in and from nowhere else, because a "
            "product that will follow a link to any host can be told what the "
            "law is by whoever controls that host."
        )
    if urlparse(url).scheme != "https":
        raise DiscoveryRefused("Regulation is read over https only.")


def fetch_official(url: str = SEBI_CIRCULARS, *, timeout: int = TIMEOUT_SECONDS) -> str:
    """GET one page from SEBI. Refuses anything that is not SEBI.

    Kept separate from :func:`discover` so the parsing can be tested without a
    network, which is how it is tested: no test in this repository reaches the
    internet.
    """
    import urllib.error
    import urllib.request

    _check_host(url)
    request = urllib.request.Request(
        url,
        headers={
            # Named honestly. A regulator's logs should show who is reading.
            "User-Agent": "Sanhita/1.0 (SEBI TechSprint prototype; regulation reader)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            # Checked again: the first check was of the URL we asked for, and
            # this is the host we actually reached.
            _check_host(response.geturl())
            raw = response.read(MAX_BYTES)
    except DiscoveryRefused:
        raise
    except urllib.error.HTTPError as exc:
        raise DiscoveryRefused(f"SEBI answered {exc.code} for that listing.") from exc
    except Exception as exc:  # noqa: BLE001 - urllib raises a wide family
        raise DiscoveryRefused(f"SEBI could not be reached: {exc}") from exc
    return _decode(raw)


def _decode(raw: bytes) -> str:
    """Text out of SEBI's bytes, without turning quotation marks into rubble.

    The listing is served without a reliable charset and contains typographic
    quotes and dashes. Decoding it as UTF-8 alone renders a circular titled
    "Green-Channel" as one titled "�Green-Channel�", which is then the
    title shown on screen and hashed into the row identity. Windows-1252 is
    tried second because that is what the page actually is.
    """
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- reading


#: An anchor pointing at something on SEBI that looks like a document.
_ROW = re.compile(
    r'<a\b[^>]*href=["\'](?P<href>[^"\']+)["\'][^>]*>(?P<title>.*?)</a>',
    re.I | re.S,
)
_TAGS = re.compile(r"<[^>]+>")
_DATE = re.compile(
    r"\b(?P<month>[A-Z][a-z]{2,8})\.?\s+(?P<day>\d{1,2})\s*,?\s*(?P<year>(?:19|20)\d{2})\b"
)
#: SEBI's document paths. Narrow on purpose: the listing page is full of
#: navigation links, and a discovery list padded with "Contact Us" is one
#: nobody will read twice.
_DOCUMENT_PATH = re.compile(
    r"/(legal|sebi_data|sebiweb/home/detail)/", re.I
)


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", _TAGS.sub(" ", fragment)).strip()


def _read_listing(html: str, *, base: str) -> list[tuple[str, str, _dt.date | None, str]]:
    """Pull (title, absolute url, issue date, row hash) out of SEBI's listing.

    Tolerant of the markup around the rows and strict about what counts as a
    document, because the listing page is mostly navigation.
    """
    from sanhita.parse.footnotes import parse_circular_date

    found: list[tuple[str, str, _dt.date | None, str]] = []
    seen: set[str] = set()
    for match in _ROW.finditer(html):
        href = match.group("href").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        url = urljoin(base, href)
        if (urlparse(url).hostname or "").lower() not in OFFICIAL_HOSTS:
            continue
        if not _DOCUMENT_PATH.search(urlparse(url).path):
            continue
        title = _text(match.group("title"))
        if len(title) < 12:
            # Navigation furniture: "More", "PDF", a stray icon's alt text.
            continue
        if url in seen:
            continue
        seen.add(url)

        # The date usually sits just after the anchor, in the same row.
        tail = html[match.end() : match.end() + 400]
        dated = _DATE.search(tail) or _DATE.search(title)
        issued = parse_circular_date(dated.group(0)) if dated else None
        row_hash = hashlib.sha256(
            f"{title}\x1f{url}\x1f{issued or ''}".encode("utf-8")
        ).hexdigest()
        found.append((title, url, issued, row_hash))
    return found


def discover(
    html: str,
    *,
    known,
    source: str = SEBI_CIRCULARS,
    at: _dt.datetime | None = None,
) -> Discovery:
    """Read SEBI's listing and say which entries this installation lacks.

    ``known`` is an iterable of what is already here: objects carrying ``name``
    and ``issued_on``, which is exactly the shape a workspace has. Matching is
    on the issue date first and the title second, because SEBI renames its own
    circulars between editions and the date is the stable half.
    """
    report = Discovery(
        checked_at=at or _dt.datetime.now(_dt.timezone.utc), source=source
    )
    try:
        _check_host(source)
    except DiscoveryRefused as exc:
        report.problem = str(exc)
        return report

    rows = _read_listing(html, base=source)
    if not rows and html.strip():
        report.problem = (
            "no publication rows were found on the page. SEBI's listing is "
            "written for people and its markup changes; this is reported "
            "rather than returned as an empty result, because an empty result "
            "reads as 'SEBI has published nothing'."
        )
        return report

    by_date: dict[_dt.date, str] = {}
    titles: set[str] = set()
    for item in known:
        name = (getattr(item, "name", "") or "").strip()
        if name:
            titles.add(name.lower())
        issued = getattr(item, "issued_on", None)
        if issued is not None:
            by_date[issued] = name

    for title, url, issued, row_hash in rows:
        state = PublicationState.NEW
        known_as = ""
        if issued is not None and issued in by_date:
            state = PublicationState.KNOWN
            known_as = by_date[issued]
        elif title.lower() in titles:
            # Same title, different date: SEBI has reissued it.
            state = PublicationState.UPDATED
            known_as = title
        report.publications.append(
            Publication(
                title=title,
                url=url,
                issued_on=issued,
                state=state,
                row_sha256=row_hash,
                known_as=known_as,
            )
        )

    # Newest first, undated last. A regulator's listing is read from the top.
    report.publications.sort(
        key=lambda p: (p.issued_on is None, -(p.issued_on or _dt.date.min).toordinal())
    )
    return report
