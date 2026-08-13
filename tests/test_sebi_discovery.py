"""Ask SEBI what it has published, when somebody presses the button.

Two properties matter more than the fetching, and these tests exist mostly to
hold them.

**Only sebi.gov.in.** A compliance product that will follow a link to any host,
and then present what it finds as regulation, is a product that can be told what
the law is by whoever controls that host.

**A layout change fails loudly.** SEBI's listing is HTML written for people and
it will change. An empty result reads as "SEBI has published nothing", which is
the one wrong answer available, so an unreadable page is a stated failure.

No test here touches the network. The listing is a fixture.
"""

from __future__ import annotations

import datetime as _dt

import pytest


# A SEBI listing page, in the shape the real one has: document links wrapped in
# table markup, dates beside them, and a good deal of navigation furniture that
# must not end up in the result.
LISTING = """
<html><body>
<div class="nav"><a href="/">Home</a> <a href="#top">Top</a>
  <a href="/sebiweb/other/OtherAction.do?doPkiFaq=yes">FAQ</a></div>
<table class="table">
  <tr>
    <td><a href="/legal/circulars/feb-2026/master-circular-for-investment-advisers_92345.html">Master Circular for Investment Advisers</a></td>
    <td>Feb 06, 2026</td>
  </tr>
  <tr>
    <td><a href="/legal/circulars/jun-2025/master-circular-for-stock-brokers_84512.html">Master Circular for Stock Brokers</a></td>
    <td>Jun 17, 2025</td>
  </tr>
  <tr>
    <td><a href="/legal/circulars/aug-2026/timelines-for-disclosure_99001.html">Timelines for disclosure of material events</a></td>
    <td>Aug 04, 2026</td>
  </tr>
</table>
<div class="footer"><a href="/contact.html">Contact</a><a href="/legal/">PDF</a></div>
</body></html>
"""


class _Known:
    def __init__(self, name, issued_on=None):
        self.name, self.issued_on = name, issued_on


def _discover(html=LISTING, known=()):
    from sanhita.discover import discover

    return discover(html, known=list(known))


# ----------------------------------------------------- reading the listing


def test_the_publications_sebi_lists_are_read_off_the_page():
    report = _discover()

    assert report.ok
    titles = [p.title for p in report.publications]
    assert "Master Circular for Investment Advisers" in titles
    assert "Master Circular for Stock Brokers" in titles
    assert len(report.publications) == 3


def test_navigation_furniture_is_not_reported_as_regulation():
    """A discovery list padded with "Contact Us" is one nobody reads twice."""
    report = _discover()
    titles = [p.title for p in report.publications]

    for junk in ("Home", "Top", "Contact", "PDF", "FAQ"):
        assert junk not in titles


def test_the_issue_date_beside_each_row_is_captured():
    report = _discover()
    by_title = {p.title: p for p in report.publications}

    assert by_title["Master Circular for Investment Advisers"].issued_on == _dt.date(
        2026, 2, 6
    )
    assert by_title["Master Circular for Stock Brokers"].issued_on == _dt.date(2025, 6, 17)


def test_the_newest_publication_is_first():
    """A regulator's listing is read from the top."""
    report = _discover()

    assert report.publications[0].issued_on == _dt.date(2026, 8, 4)


def test_each_publication_has_a_stable_identity_from_its_own_url():
    report = _discover()
    again = _discover()

    assert report.publications[0].identity == again.publications[0].identity
    assert len({p.identity for p in report.publications}) == 3


def test_a_row_is_hashed_so_two_checks_can_be_compared():
    report = _discover()

    assert all(len(p.row_sha256) == 64 for p in report.publications)


# ------------------------------------------- what this installation knows


def test_a_publication_already_on_file_is_not_reported_as_new():
    from sanhita.discover import PublicationState

    report = _discover(known=[_Known("Stock Brokers Master Circular", _dt.date(2025, 6, 17))])
    by_title = {p.title: p for p in report.publications}

    known = by_title["Master Circular for Stock Brokers"]
    assert known.state is PublicationState.KNOWN
    assert known.known_as == "Stock Brokers Master Circular"
    assert "already on file" in known.describe()


def test_a_reissue_under_the_same_title_is_flagged_as_changed():
    """SEBI renames and reissues its own circulars. The date is the stable half."""
    from sanhita.discover import PublicationState

    report = _discover(
        known=[_Known("Master Circular for Investment Advisers", _dt.date(2025, 6, 27))]
    )
    by_title = {p.title: p for p in report.publications}

    assert by_title["Master Circular for Investment Advisers"].state is (
        PublicationState.UPDATED
    )


def test_what_is_new_is_what_the_headline_leads_with():
    report = _discover(known=[_Known("Stock Brokers", _dt.date(2025, 6, 17))])

    assert len(report.new) == 2
    assert "2 of the 3 publications" in report.headline()


def test_a_duplicate_link_is_reported_once():
    doubled = LISTING.replace("</table>", """
      <tr><td><a href="/legal/circulars/feb-2026/master-circular-for-investment-advisers_92345.html">Master Circular for Investment Advisers</a></td><td>Feb 06, 2026</td></tr>
    </table>""")
    report = _discover(doubled)

    urls = [p.url for p in report.publications]
    assert len(urls) == len(set(urls))


# ------------------------------------------------- only the official source


def test_a_source_that_is_not_sebi_is_refused():
    """The property this whole module turns on."""
    from sanhita.discover import discover

    report = discover(LISTING, known=[], source="https://example.com/circulars")

    assert not report.ok
    assert "not an official SEBI host" in report.problem
    assert not report.publications


def test_the_fetcher_refuses_a_non_sebi_url_before_asking_for_it():
    from sanhita.discover import DiscoveryRefused, fetch_official

    with pytest.raises(DiscoveryRefused, match="not an official SEBI host"):
        fetch_official("https://sebi.gov.in.example.com/listing")


def test_the_fetcher_refuses_plain_http():
    from sanhita.discover import DiscoveryRefused, fetch_official

    with pytest.raises(DiscoveryRefused, match="https only"):
        fetch_official("http://www.sebi.gov.in/listing")


def test_a_link_off_sebi_inside_the_listing_is_ignored():
    """Even on SEBI's own page, an outbound link is not SEBI's regulation."""
    poisoned = LISTING.replace(
        "</table>",
        '<tr><td><a href="https://evil.example.com/legal/circulars/fake_1.html">'
        "A convincing looking circular</a></td><td>Aug 05, 2026</td></tr></table>",
    )
    report = _discover(poisoned)

    assert all("sebi.gov.in" in p.url for p in report.publications)
    assert "A convincing looking circular" not in [p.title for p in report.publications]


def test_the_default_source_is_sebis_own_listing():
    from sanhita.discover import OFFICIAL_HOSTS, SEBI_CIRCULARS
    from urllib.parse import urlparse

    assert urlparse(SEBI_CIRCULARS).hostname in OFFICIAL_HOSTS
    assert SEBI_CIRCULARS.startswith("https://")


# ----------------------------------------------------- failing out loud


def test_typographic_quotes_survive_the_decoding():
    """SEBI serves the listing as Windows-1252 without saying so.

    Decoded as UTF-8 alone, a circular titled "Green-Channel" becomes one
    titled "?Green-Channel?", and that mangled title is then what is shown and
    what is hashed into the row identity.
    """
    from sanhita.discover import _decode

    raw = "“Green-Channel” rollout".encode("cp1252")
    assert _decode(raw) == "“Green-Channel” rollout"
    assert _decode("Master Circular".encode("utf-8")) == "Master Circular"


def test_a_page_that_cannot_be_read_is_a_stated_failure_not_an_empty_list():
    """"Nothing found" and "could not look" are different answers."""
    report = _discover("<html><body><p>Service unavailable</p></body></html>")

    assert not report.ok
    assert "no publication rows were found" in report.problem
    assert not report.publications


def test_an_empty_body_is_not_reported_as_a_clean_check():
    report = _discover("")

    assert not report.publications
    assert "more likely to mean the page has changed shape" in report.headline()


# ------------------------------------------------------------ through the UI


def test_the_screen_offers_the_check_and_never_calls_it_monitoring(tmp_path, corpus_pdf, monkeypatch):
    import re
    import shutil

    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a" * 64)
    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)
    client = TestClient(create_app(corpus_pdf, store=store))

    body = client.get("/w/demo/diff").text
    page = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))

    assert "Check SEBI now" in page
    assert "Nothing found here is downloaded, parsed or certified by being found" in page
    for overclaim in ("real-time monitoring", "continuously monitors", "automatically monitors"):
        assert overclaim not in page.lower()


def test_a_failed_check_is_shown_as_a_failure(tmp_path, corpus_pdf, monkeypatch):
    """The network is never reached in tests, so this is the path that runs."""
    import re
    import shutil

    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a" * 64)
    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)
    client = TestClient(create_app(corpus_pdf, store=store))

    import sanhita.discover as discovery

    def _refuse(*_args, **_kwargs):
        raise discovery.DiscoveryRefused("SEBI could not be reached: offline")

    monkeypatch.setattr(discovery, "fetch_official", _refuse)

    client.post("/w/demo/discover", follow_redirects=True)
    body = client.get("/w/demo/diff").text
    page = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))

    assert "SEBI's listing could not be read" in page
    assert "offline" in page
