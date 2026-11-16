"""Tests for src/sources/sccourts.py against saved copies of the real sccourts.org pages.

No network: listing pages come from tests/fixtures/sccourts/, HTTP is mocked with
``responses``, and advance-sheet PDF text is injected through ``pdf_to_pages`` (the
real pdfplumber page texts of Advance Sheets 41 and 40 are saved as JSON fixtures).
"""

from __future__ import annotations

import logging
from datetime import date

import pytest
import requests
import responses

from src import extract
from src.extract import clean_text
from src.models import AdvanceSheet, Court, ItemKind
from src.net import PoliteSession
from src.sources import LayoutChangedError, SourceError, sccourts
from src.sources.sccourts import (
    ADVANCE_SHEETS_URL,
    ALL_ORDERS_URL,
    BASE_URL,
    LISTING_PATHS,
    AdvanceSheetContents,
    ContentsEntry,
    OrderListing,
    build_order_items,
    fetch,
    listing_url,
    match_order,
    months_between,
    order_date_from_text,
    parse_advance_sheet_contents,
    parse_advance_sheet_list,
    parse_opinion_listing,
    parse_orders_listing,
)
from tests.conftest import fixture_json, fixture_text
from tests.pdfgen import make_pdf

HTML = "text/html; charset=utf-8"
SHEET41_URL = "https://www.sccourts.org/media/opinions/advSheets/no412026.pdf"
SHEET40_URL = "https://www.sccourts.org/media/opinions/advSheets/no402026.pdf"
SHEET41 = AdvanceSheet(number=41, date=date(2026, 11, 12), pdf_url=SHEET41_URL)
SHEET40 = AdvanceSheet(number=40, date=date(2026, 11, 4), pdf_url=SHEET40_URL)
PDF41 = b"%PDF-1.4 stand-in bytes for advance sheet 41"
PDF40 = b"%PDF-1.4 stand-in bytes for advance sheet 40"
SC_NOV = listing_url(Court.SUPREME, 2026, 11)
COA_NOV = listing_url(Court.APPEALS, 2026, 11)
SC_OCT = listing_url(Court.SUPREME, 2026, 10)
COA_OCT = listing_url(Court.APPEALS, 2026, 10)
TODAY = date(2026, 11, 16)
FILED_AFTER = date(2026, 11, 6)  # TODAY minus the default 10-day lookback

FEES_TITLE = "Re: Fees for Copying and Other Related Services"
RULE_TITLE = "Re: Amendments to Rule 1.15, RPC, Rule 407, SCACR"
FEES_KEY = "ORDER:2026-11-04:re-fees-for-copying-and-other-related-services"
RULE_KEY = "ORDER:2026-11-12-01"


# --------------------------------------------------------------------------- helpers


def pages41() -> list[str]:
    return fixture_json("sccourts/advance_sheet_41_pages.json")


def pages40() -> list[str]:
    return fixture_json("sccourts/advance_sheet_40_pages.json")


def page(name: str) -> str:
    return fixture_text(f"sccourts/{name}")


def orders_listing() -> list[OrderListing]:
    return parse_orders_listing(page("all_court_orders_2026.html"))


def fake_pdf_to_pages(data: bytes) -> list[str]:
    """Stands in for extract.pdf_to_pages: returns the saved page texts of the sheet whose bytes it gets."""
    return {PDF41: pages41(), PDF40: pages40()}[data]


def make_session() -> PoliteSession:
    return PoliteSession("SC-Advance-Sheet-Digest/test (pytest)", min_interval=0, max_retries=0)


@pytest.fixture
def rsps():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


def add_html(rsps, url: str, body: str, status: int = 200) -> None:
    rsps.add(responses.GET, url, body=body, status=status, content_type=HTML)


def register_site(
    rsps,
    *,
    sc_nov: str = "sc_published_2026-11.html",
    coa_nov: str = "coa_published_2026-11.html",
    october: bool = False,
) -> None:
    """Mock every sccourts.org URL a normal run in mid-November 2026 requests."""
    add_html(rsps, SC_NOV, page(sc_nov))
    add_html(rsps, COA_NOV, page(coa_nov))
    if october:
        add_html(rsps, SC_OCT, page("sc_published_2026-10.html"))
        add_html(rsps, COA_OCT, page("coa_published_2026-10.html"))
    add_html(rsps, ADVANCE_SHEETS_URL, page("advance_sheets.html"))
    rsps.add(responses.GET, SHEET41_URL, body=PDF41, content_type="application/pdf")
    rsps.add(responses.GET, SHEET40_URL, body=PDF40, content_type="application/pdf")
    add_html(rsps, ALL_ORDERS_URL, page("all_court_orders_2026.html"))


def requested(rsps) -> list[str]:
    return [call.request.url for call in rsps.calls]


def listing_html(body: str) -> str:
    return (
        '<html><body><section class="results-listing"><div class="container">'
        f'<div class="accordion-block my-3">{body}</div></div></section></body></html>'
    )


def heading(text: str) -> str:
    return f'<div class="result-heading teal-bg"><h3 class="small-title">{text}</h3></div>'


def result(
    number: str | None = "28400",
    name: str = "A v. B",
    href: str | None = "/media/opinions/HTMLFiles/SC/28400.pdf",
    aria: str | None = "Download opinion 28400 as PDF",
    link_class: str = "download-link",
    info: str = "<p>Court summary.</p>",
) -> str:
    num = f'<span class="case-number teal-text">{number}</span>' if number is not None else ""
    aria_attr = f' aria-label="{aria}"' if aria is not None else ""
    link = f'<a href="{href}"{aria_attr} class="{link_class}">Download</a>' if href is not None else ""
    return (
        '<div class="accordion-item case-result">'
        f'<button class="result-title">{num}<span class="case-name teal-text">{name}</span></button>'
        f'<div aria-live="polite"><div class="result-info" id="opinion-SCJB.Core.Api.OpinionListDTO" hidden>'
        f"{info}</div></div><p>{link}</p></div>"
    )


def entry_tuples(contents: AdvanceSheetContents) -> list[tuple]:
    return [(e.kind, e.court, e.number, e.title, e.start_page, e.end_page, e.refiled_date) for e in contents.entries]


COVER = (
    "OPINIONS\nOF\nTHE SUPREME COURT\nAND\nCOURT OF APPEALS\nOF\nSOUTH CAROLINA\n"
    "ADVANCE SHEET NO. 7\nFebruary 18, 2027\nPatricia A. Howard, Clerk\nColumbia, South Carolina\nwww.sccourts.org"
)


def synthetic_sheet(contents_pages: list[list[str]], total: int = 40, before_cover: list[str] = ()) -> list[str]:
    """Pages of a made-up advance sheet: optional pre-cover pages, the cover, CONTENTS, then body pages.

    Every page ends with its printed page number, like the real pdfplumber text.
    """
    texts = [*before_cover, COVER, *("\n".join(lines) for lines in contents_pages)]
    while len(texts) < total:
        texts.append(f"Opinion text on page {len(texts) + 1}.")
    return [f"{text}\n{i}" for i, text in enumerate(texts, start=1)]


# --------------------------------------------------------------------------- URLs and months


def test_constants_and_listing_url():
    assert BASE_URL == "https://www.sccourts.org"
    assert LISTING_PATHS[Court.SUPREME] == "/opinions-orders/opinions/published-opinions/supreme-court/"
    assert LISTING_PATHS[Court.APPEALS] == "/opinions-orders/opinions/published-opinions/court-of-appeals/"
    assert ADVANCE_SHEETS_URL == "https://www.sccourts.org/opinions-orders/advance-sheets/"
    assert ALL_ORDERS_URL == "https://www.sccourts.org/opinions-orders/court-orders/all-court-orders/"
    assert SC_NOV == "https://www.sccourts.org/opinions-orders/opinions/published-opinions/supreme-court/?term=2026-11"
    assert listing_url(Court.APPEALS, 2027, 1) == (
        "https://www.sccourts.org/opinions-orders/opinions/published-opinions/court-of-appeals/?term=2027-01"
    )


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (date(2026, 11, 6), date(2026, 11, 16), [(2026, 11)]),
        (date(2026, 10, 25), date(2026, 11, 16), [(2026, 10), (2026, 11)]),
        (date(2026, 11, 30), date(2027, 2, 1), [(2026, 11), (2026, 12), (2027, 1), (2027, 2)]),
        (date(2026, 11, 16), date(2026, 11, 16), [(2026, 11)]),
        (date(2026, 11, 17), date(2026, 11, 16), []),
    ],
)
def test_months_between(start, end, expected):
    assert months_between(start, end) == expected


# --------------------------------------------------------------------------- opinion listings (real pages)


def test_supreme_court_november_listing():
    items = parse_opinion_listing(page("sc_published_2026-11.html"), Court.SUPREME, SC_NOV)
    assert [item.number for item in items] == ["28354", "28355"]
    nettles, fleming = items
    for item in items:
        assert item.kind is ItemKind.OPINION
        assert item.court is Court.SUPREME
        assert item.filed_date == date(2026, 11, 12)
        assert item.source == "sccourts"
        assert item.link_url == item.pdf_url
        assert item.mirror_url is None and item.text is None and item.advance_sheet is None
    assert nettles.case_name == "In the Matter of Louis David Nettles"
    assert fleming.case_name == "Reid Fleming v. LG Chem Ltd."
    assert nettles.pdf_url == "https://www.sccourts.org/media/opinions/HTMLFiles/SC/28354.pdf"
    assert fleming.pdf_url == "https://www.sccourts.org/media/opinions/HTMLFiles/SC/28355.pdf"
    assert nettles.court_summary == (
        "In this attorney disciplinary matter, Respondent and the Office of Disciplinary Counsel (ODC) have "
        "entered into an Agreement for Discipline by Consent pursuant to Rule 21 of the Rules for Lawyer "
        "Disciplinary Enforcement (RLDE) contained in Rule 413 of the South Carolina Appellate Court Rules (SCACR)."
    )
    assert fleming.court_summary == (
        "Anderson resident Reid Fleming alleges he was burned when the model 18650 lithium-ion battery in his "
        "vape pen exploded. His injury occurred in Charleston."
    )
    assert [item.key for item in items] == ["SC:28354", "SC:28355"]


def test_court_of_appeals_november_listing_keeps_the_space_in_the_pdf_url():
    items = parse_opinion_listing(page("coa_published_2026-11.html"), Court.APPEALS, COA_NOV)
    assert len(items) == 1
    (moore,) = items
    assert moore.number == "6170"
    assert moore.case_name == "Gary Moore v. State"
    assert moore.court is Court.APPEALS
    assert moore.filed_date == date(2026, 11, 12)
    # The href is ".../COA/6170 .pdf"; only the %20-encoded form works (the trimmed URL is a 404).
    assert moore.pdf_url == moore.link_url == "https://www.sccourts.org/media/opinions/HTMLFiles/COA/6170%20.pdf"
    assert moore.key == "COA:6170"
    assert moore.court_summary == (
        "In this post-conviction relief (PCR) action, Gary Moore (Petitioner) appeals the denial of his "
        "PCR application."
    )


def test_court_of_appeals_october_listing_tracks_date_headings():
    items = parse_opinion_listing(page("coa_published_2026-10.html"), Court.APPEALS, COA_OCT)
    assert [(item.number, item.filed_date) for item in items] == [
        ("6167", date(2026, 10, 21)),
        ("6168", date(2026, 10, 21)),
        ("6169", date(2026, 10, 28)),
    ]
    assert [item.case_name for item in items] == [
        "Mastercard International Inc. v. SCDOR",
        "U.S. Bank National Association v. SCDOR",
        "Roy Williams v. Jamma L. W. Bradford",
    ]
    summary = items[0].court_summary
    assert "the ALC's decision upholding the South Carolina Department of Revenue's assessment" in summary
    assert '"final act"' in summary and "(2014 & Supp. 2025)" in summary
    assert "&#x27;" not in summary and "&amp;" not in summary and "&quot;" not in summary
    assert "  " not in summary  # the page has double spaces between sentences
    assert summary.endswith("We affirm.")


def test_supreme_court_october_listing():
    items = parse_opinion_listing(page("sc_published_2026-10.html"), Court.SUPREME, SC_OCT)
    assert [(item.number, item.case_name, item.filed_date) for item in items] == [
        ("28353", "In the Matter of Jason F. Taylor", date(2026, 10, 21))
    ]
    assert items[0].pdf_url == "https://www.sccourts.org/media/opinions/HTMLFiles/SC/28353.pdf"


def test_empty_month_returns_no_items_without_warnings(caplog):
    url = listing_url(Court.SUPREME, 2026, 12)
    with caplog.at_level(logging.WARNING):
        assert parse_opinion_listing(page("sc_published_2026-12_empty.html"), Court.SUPREME, url) == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize(
    "html",
    [
        "",
        "not html at all",
        "<html><body><h1>Down for maintenance</h1></body></html>",
        '<html><body><section class="results-listing"><p>Coming soon</p></section></body></html>',
        '<html><body><div class="accordion-block">outside the results section</div></body></html>',
    ],
)
def test_garbage_listing_raises_layout_changed(html):
    with pytest.raises(LayoutChangedError, match="results block") as info:
        parse_opinion_listing(html, Court.SUPREME, SC_NOV)
    assert isinstance(info.value, SourceError)
    assert SC_NOV in str(info.value)


# --------------------------------------------------------------------------- opinion listings (synthetic edge cases)


def test_listing_skips_items_without_a_date_heading_or_pdf_link(caplog):
    html = listing_html(
        result("28400", "Before Any Heading")
        + heading("November 12, 2026")
        + result("28401", "No Link Here", href=None)
        + result("28402", "Good One", href="/media/opinions/HTMLFiles/SC/28402.pdf")
    )
    with caplog.at_level(logging.WARNING, logger="src.sources.sccourts"):
        items = parse_opinion_listing(html, Court.SUPREME, SC_NOV)
    assert [(item.number, item.case_name) for item in items] == [("28402", "Good One")]
    messages = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "28400" in messages and "not under a date heading" in messages
    assert "28401" in messages and "no PDF link" in messages


def test_listing_number_fallbacks_relative_links_and_text_cleanup():
    html = listing_html(
        heading("October 28, 2026")
        # no case-number span: number from the aria-label
        + result(None, "Aria &amp; Label v. State", href="/media/opinions/HTMLFiles/COA/6201.pdf",
                 aria="Download opinion 6201 as PDF")
        # empty span and no aria-label: number from the file name (with its stray space)
        + result("", "File\u00a0Name   v.\n  State", aria=None,
                 href="https://www.sccourts.org/media/opinions/HTMLFiles/COA/6202 .pdf")
        # link without the download-link class: any .pdf link is used; a double-escaped entity is decoded
        + result("6203", "O&amp;#x27;Neal v. Smith", href="../../HTMLFiles/COA/6203.PDF", link_class="other",
                 info="<p>First   part.</p><p>Second part.</p>")
    )  # fmt: skip
    items = parse_opinion_listing(html, Court.APPEALS, "https://www.sccourts.org/media/opinions/x/y/?term=2026-10")
    assert [item.number for item in items] == ["6201", "6202", "6203"]
    first, second, third = items
    assert first.case_name == "Aria & Label v. State"
    assert first.pdf_url == "https://www.sccourts.org/media/opinions/HTMLFiles/COA/6201.pdf"
    assert second.case_name == "File Name v. State"
    assert second.pdf_url == "https://www.sccourts.org/media/opinions/HTMLFiles/COA/6202%20.pdf"
    assert third.case_name == "O'Neal v. Smith"
    assert third.pdf_url == "https://www.sccourts.org/media/opinions/HTMLFiles/COA/6203.PDF"
    assert third.court_summary == "First part. Second part."
    assert all(item.filed_date == date(2026, 10, 28) for item in items)


def test_listing_unrecognized_heading_skips_its_items_and_a_new_heading_recovers(caplog):
    html = listing_html(
        heading("Sometime soon")
        + result("28400", "Skipped")
        + heading("November 5, 2026")
        + result("28401", "Kept", href="/media/opinions/HTMLFiles/SC/28401.pdf")
    )
    with caplog.at_level(logging.WARNING, logger="src.sources.sccourts"):
        items = parse_opinion_listing(html, Court.SUPREME, SC_NOV)
    assert [(item.number, item.filed_date) for item in items] == [("28401", date(2026, 11, 5))]
    assert any("Unrecognized date heading" in r.getMessage() for r in caplog.records)


def test_listing_with_unknown_items_and_no_empty_notice_warns(caplog):
    html = listing_html('<div class="accordion-item new-style-result"><p>28400 Something</p></div>')
    with caplog.at_level(logging.WARNING, logger="src.sources.sccourts"):
        assert parse_opinion_listing(html, Court.SUPREME, SC_NOV) == []
    assert any("layout may have changed" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- advance sheets list


def test_advance_sheet_list():
    assert parse_advance_sheet_list(page("advance_sheets.html")) == [SHEET41, SHEET40]


def test_advance_sheet_list_synthetic_variants(caplog):
    html = (
        '<section class="results-listing"><div class="result-table">'
        '<div class="result-title advance-sheet-result"><h2><a href="/media/opinions/advSheets/no072027.pdf">'
        'Advance Sheet No. 7</a></h2><p class="subtitle">February 18, 2027</p></div>'
        '<div class="result-title advance-sheet-result"><h2><a href="/media/opinions/advSheets/no062027.pdf">'
        "Download</a></h2><p class=\"subtitle\">February 11, 2027</p></div>"
        '<div class="result-title advance-sheet-result"><h2><a href="/media/opinions/advSheets/no052027.pdf">'
        'Advance Sheet 5</a></h2><p class="subtitle">Date to be announced</p></div>'
        "</div></section>"
    )
    with caplog.at_level(logging.WARNING, logger="src.sources.sccourts"):
        sheets = parse_advance_sheet_list(html)
    assert sheets == [
        AdvanceSheet(7, date(2027, 2, 18), "https://www.sccourts.org/media/opinions/advSheets/no072027.pdf"),
        AdvanceSheet(6, date(2027, 2, 11), "https://www.sccourts.org/media/opinions/advSheets/no062027.pdf"),
    ]
    assert any("Advance Sheet 5" in r.getMessage() and "date" in r.getMessage() for r in caplog.records)


def test_advance_sheet_list_layout_changes():
    with pytest.raises(LayoutChangedError):
        parse_advance_sheet_list("<html><body><p>Page not found</p></body></html>")
    # The results section with no entries is an (odd) empty list, not a layout change.
    assert parse_advance_sheet_list('<section class="results-listing"><div class="result-table"></div></section>') == []


# --------------------------------------------------------------------------- advance sheet CONTENTS (real sheets)


def test_advance_sheet_41_contents():
    contents = parse_advance_sheet_contents(pages41())
    assert contents.cover_page == 4
    assert contents.total_pages == 43
    assert entry_tuples(contents) == [
        (ItemKind.ORDER, Court.SUPREME, None, FEES_TITLE, 1, 3, None),
        (ItemKind.OPINION, Court.SUPREME, "28354", "In the Matter of Louis David Nettles", 11, 14, None),
        (ItemKind.OPINION, Court.SUPREME, "28355", "Reid Fleming v. LG Chem, Ltd.", 15, 25, None),
        (ItemKind.ORDER, Court.SUPREME, None, RULE_TITLE, 26, 27, None),
        (ItemKind.OPINION, Court.APPEALS, "6170", "Gary Moore v. State", 28, 43, None),
    ]


def test_advance_sheet_41_page_ranges_match_the_page_texts():
    texts = pages41()
    contents = parse_advance_sheet_contents(texts)
    for entry in contents.entries:
        first, last = texts[entry.start_page - 1], texts[entry.end_page - 1]
        if entry.kind is ItemKind.OPINION:
            assert f"Opinion No. {entry.number}" in first
            assert f"Opinion No. {entry.number}" not in texts[entry.start_page - 2]
            assert "concur." in last  # every opinion here ends with the concurrence line
        else:
            assert entry.title in first and "ORDER" in first.splitlines()
            assert "Columbia, South Carolina" in last  # the signature block
    assert "ADVANCE SHEET NO. 41" in texts[contents.cover_page - 1]


def test_advance_sheet_41_contents_from_cleaned_pages():
    # Parsing must not depend on each page's own printed number.
    cleaned = ["\n".join(text.splitlines()[:-1]) for text in pages41()]
    assert entry_tuples(parse_advance_sheet_contents(cleaned)) == entry_tuples(parse_advance_sheet_contents(pages41()))


def _production_pages(texts: list[str]) -> list[str]:
    """The page texts the weekly job gives the parser: a PDF of the pages, read by the production reader."""
    return extract.pdf_to_layout_pages(make_pdf([text.splitlines() or [""] for text in texts]))


def test_advance_sheet_41_contents_through_the_production_reader():
    assert entry_tuples(parse_advance_sheet_contents(_production_pages(pages41()))) == entry_tuples(
        parse_advance_sheet_contents(pages41())
    )


def test_wrapped_page_number_survives_the_production_reader():
    # A CONTENTS page number on a line of its own: the reader used to drop 1-3 digit lines, so the
    # entry lost its page and was skipped (and an order beside it could swallow the next opinion's pages).
    texts = synthetic_sheet(
        [
            [
                "CONTENTS",
                "THE SUPREME COURT OF SOUTH CAROLINA",
                "PUBLISHED OPINIONS AND ORDERS",
                "28400 – The State v. Someone Whose Name",
                "12",
                "Order – Re: Amendments to Rule 1.15, RPC, Rule 407, SCACR 16",
                "UNPUBLISHED OPINIONS",
                "THE SOUTH CAROLINA COURT OF APPEALS",
                "PUBLISHED OPINIONS",
                "6200 – Ann Lee v. SCDOT 18",
                "UNPUBLISHED OPINIONS",
            ]
        ],
        total=20,
    )
    entries = parse_advance_sheet_contents(_production_pages(texts)).entries
    assert [(e.number, e.title, e.start_page, e.end_page) for e in entries] == [
        ("28400", "The State v. Someone Whose Name", 12, 15),
        (None, "Re: Amendments to Rule 1.15, RPC, Rule 407, SCACR", 16, 17),
        ("6200", "Ann Lee v. SCDOT", 18, 20),
    ]
    assert "12" not in extract.pdf_to_pages(make_pdf([texts[1].splitlines()]))[0].splitlines()


def test_order_text_from_the_production_reader_is_unchanged():
    # The text sent to Claude (and so the summary cache key) is exactly what the old reader gave.
    pdf = make_pdf([text.splitlines() or [""] for text in pages41()])
    layout, cleaned = extract.pdf_to_layout_pages(pdf), extract.pdf_to_pages(pdf)
    new = build_order_items(SHEET41, parse_advance_sheet_contents(layout), layout, orders_listing())
    old = build_order_items(SHEET41, parse_advance_sheet_contents(pages41()), cleaned, orders_listing())
    assert [item.text for item in new] == [item.text for item in old]
    assert all(item.text for item in new)


def test_advance_sheet_40_has_no_published_opinions_or_orders():
    contents = parse_advance_sheet_contents(pages40())
    assert contents.entries == []
    assert contents.cover_page == 1
    assert contents.total_pages == 7


# --------------------------------------------------------------------------- advance sheet CONTENTS (synthetic)


def test_contents_dashes_wrapped_titles_and_refiled_lines():
    texts = synthetic_sheet(
        [
            [
                "CONTENTS",
                "THE SUPREME COURT OF SOUTH CAROLINA",
                "PUBLISHED OPINIONS AND ORDERS",
                "28400 \u2014 The State v. John Doe 10",
                "28401 - Jane Roe v. Richard Roe, as Personal Representative of the",
                "Estate of Rex Roe 14",
                "28402 \u2013 Alpha Corp. v. Beta LLC, Gamma Inc., Delta",
                "Holdings, Epsilon Partners, and",
                "Zeta Ventures 20",
                "(Withdrawn, Substituted, and Refiled February 17, 2027)",
                "Order \u2013 Re: Amendments to Rule 410",
                "and Rule 413, SCACR 24",
                "UNPUBLISHED OPINIONS",
                "2027-MO-001 \u2013 Somebody v. Someone 25",
                "(Withdrawn, Substituted, and Refiled February 17, 2027)",
                "PETITIONS FOR REHEARING",
                "28399 \u2013 Pending Case v. Other Party Pending",
            ],
            [
                "THE SOUTH CAROLINA COURT OF APPEALS",
                "PUBLISHED OPINIONS",
                "6200 \u2013The State v. Sam Smith 26",
                "6201 \u2013 Ann Lee v. SCDOT",
                "(Withdrawn, Substituted, and Refiled February 16, 2027) 30",
                "UNPUBLISHED OPINIONS",
                "2027-UP-010 \u2013 X v. Y",
            ],
        ]
    )
    contents = parse_advance_sheet_contents(texts)
    assert contents.cover_page == 1 and contents.total_pages == 40
    assert entry_tuples(contents) == [
        (ItemKind.OPINION, Court.SUPREME, "28400", "The State v. John Doe", 10, 13, None),
        (
            ItemKind.OPINION, Court.SUPREME, "28401",
            "Jane Roe v. Richard Roe, as Personal Representative of the Estate of Rex Roe", 14, 19, None,
        ),
        (
            ItemKind.OPINION, Court.SUPREME, "28402",
            "Alpha Corp. v. Beta LLC, Gamma Inc., Delta Holdings, Epsilon Partners, and Zeta Ventures", 20, 23,
            date(2027, 2, 17),
        ),
        (ItemKind.ORDER, Court.SUPREME, None, "Re: Amendments to Rule 410 and Rule 413, SCACR", 24, 25, None),
        (ItemKind.OPINION, Court.APPEALS, "6200", "The State v. Sam Smith", 26, 29, None),
        (ItemKind.OPINION, Court.APPEALS, "6201", "Ann Lee v. SCDOT", 30, 40, date(2027, 2, 16)),
    ]  # fmt: skip


def test_contents_entry_with_too_many_continuation_lines_is_dropped(caplog):
    texts = synthetic_sheet(
        [
            [
                "CONTENTS",
                "THE SUPREME COURT OF SOUTH CAROLINA",
                "PUBLISHED OPINIONS AND ORDERS",
                "28400 \u2013 A very long",
                "caption that keeps",
                "going and going",
                "and only then ends 11",  # a third continuation line: past the limit, so not joined
                "28401 \u2013 Next Case 12",
                "28402 \u2013 Never Gets",
                "A Page Number",
                "UNPUBLISHED OPINIONS",
            ]
        ],
        total=20,
    )
    with caplog.at_level(logging.WARNING, logger="src.sources.sccourts"):
        contents = parse_advance_sheet_contents(texts)
    assert [(e.number, e.title, e.start_page, e.end_page) for e in contents.entries] == [("28401", "Next Case", 12, 20)]
    dropped = " ".join(r.getMessage() for r in caplog.records)
    assert "A very long caption that keeps going and going" in dropped
    assert "Never Gets A Page Number" in dropped


def test_contents_wrapped_page_number_entry_across_pages_and_repeated_heading():
    texts = synthetic_sheet(
        [
            [
                "CONTENTS",
                "THE SUPREME COURT OF SOUTH CAROLINA",
                "PUBLISHED OPINIONS AND ORDERS",
                "28400 \u2013 The State v. Someone Whose Name",
                "12",  # the page number landed on its own line
                "28401 \u2013 Carolina Widgets, LLC v. Palmetto",
            ],
            [
                "THE SUPREME COURT OF SOUTH CAROLINA",  # heading repeated on the next CONTENTS page
                "Gadgets, Inc. 15",
                "UNPUBLISHED OPINIONS",
                "None",
                "THE SOUTH CAROLINA COURT OF APPEALS",
                "PUBLISHED OPINIONS",
                "None",
                "UNPUBLISHED OPINIONS",
            ],
        ],
        total=18,
    )
    assert [(e.number, e.title, e.start_page, e.end_page) for e in parse_advance_sheet_contents(texts).entries] == [
        ("28400", "The State v. Someone Whose Name", 12, 14),
        ("28401", "Carolina Widgets, LLC v. Palmetto Gadgets, Inc.", 15, 18),
    ]


def test_contents_page_numbers_must_point_past_the_contents_and_inside_the_sheet():
    texts = synthetic_sheet(
        [
            [
                "CONTENTS",
                "THE SUPREME COURT OF SOUTH CAROLINA",
                "PUBLISHED OPINIONS AND ORDERS",
                "28400 \u2013 In re the 2026",  # 2026 is past the last page: part of the title
                "General Election 11",
                "28401 \u2013 Smith v. Rule 1",  # 1 is before the CONTENTS page: part of the title
                "Corp. 13",
                "Order \u2013 Re: Something ........ 15",  # dot leaders are trimmed
                "UNPUBLISHED OPINIONS",
            ]
        ],
        total=16,
    )
    assert [(e.number, e.title, e.start_page, e.end_page) for e in parse_advance_sheet_contents(texts).entries] == [
        ("28400", "In re the 2026 General Election", 11, 12),
        ("28401", "Smith v. Rule 1 Corp.", 13, 14),
        (None, "Re: Something", 15, 16),
    ]


def test_contents_end_pages_follow_page_order_even_if_listed_out_of_order():
    texts = synthetic_sheet(
        [
            [
                "THE SUPREME COURT OF SOUTH CAROLINA",
                "PUBLISHED OPINIONS AND ORDERS",
                "28401 \u2013 Second v. Case 20",
                "28400 \u2013 First v. Case 10",
                "UNPUBLISHED OPINIONS",
            ]
        ],
        total=30,
    )
    assert [(e.number, e.start_page, e.end_page) for e in parse_advance_sheet_contents(texts).entries] == [
        ("28400", 10, 19),
        ("28401", 20, 30),
    ]


def test_orders_printed_before_the_cover():
    before = [
        "The Supreme Court of South Carolina\nRe: Order About Something\nAppellate Case No. 2027-000001\nORDER\n"
        "The Court amends the rule.",
        "IT IS SO ORDERED.\nColumbia, South Carolina\nFebruary 10, 2027",
        "The Supreme Court of South Carolina\nRe: A Second Order Whose Title\nWraps Onto Another Line\nORDER\nText.",
    ]
    contents_page = ["CONTENTS", "THE SUPREME COURT OF SOUTH CAROLINA", "PUBLISHED OPINIONS AND ORDERS", "None"]
    contents = parse_advance_sheet_contents(synthetic_sheet([contents_page], total=8, before_cover=before))
    assert contents.cover_page == 4
    assert entry_tuples(contents) == [
        (ItemKind.ORDER, Court.SUPREME, None, "Re: Order About Something", 1, 2, None),
        (ItemKind.ORDER, Court.SUPREME, None, "Re: A Second Order Whose Title Wraps Onto Another Line", 3, 3, None),
    ]


def test_pre_cover_page_without_an_order_heading_is_ignored():
    before = ["NOTICE\nThe Clerk's Office will be closed on Monday, February 15, 2027."]
    contents_page = ["CONTENTS", "THE SUPREME COURT OF SOUTH CAROLINA", "PUBLISHED OPINIONS AND ORDERS", "None"]
    contents = parse_advance_sheet_contents(synthetic_sheet([contents_page], total=5, before_cover=before))
    assert contents.cover_page == 2
    assert contents.entries == []


@pytest.mark.parametrize(
    "texts",
    [
        [],
        [""],
        ["", None, 5],
        ["random garbage " * 200],
        ["CONTENTS\nTHE SUPREME COURT OF SOUTH CAROLINA\nPUBLISHED OPINIONS AND ORDERS\n28400 \u2013"],
        ["PUBLISHED OPINIONS AND ORDERS\n(Refiled\n99999 \u2013 x 5\nOrder - \n\u2013\n12\n" * 30],
        ["ADVANCE SHEET NO. 1", "ORDER\nRe:\n", "THE SOUTH CAROLINA COURT OF APPEALS\nPUBLISHED OPINIONS\n6200 - 3"],
    ],
)
def test_contents_never_raises_on_odd_text(texts):
    contents = parse_advance_sheet_contents(texts)
    assert isinstance(contents, AdvanceSheetContents)
    assert contents.total_pages == len(texts)
    for entry in contents.entries:
        assert 1 <= entry.start_page <= entry.end_page <= contents.total_pages


def test_contents_unexpected_internal_error_is_logged_not_raised(monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(sccourts, "_parse_contents_sections", broken)
    with caplog.at_level(logging.WARNING, logger="src.sources.sccourts"):
        contents = parse_advance_sheet_contents(pages41())
    assert contents.entries == [] and contents.total_pages == 43
    assert any("CONTENTS" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- orders listing and matching


def test_orders_listing():
    listings = orders_listing()
    assert len(listings) >= 90
    assert len(listings) == 94
    by_number = {listing.number: listing for listing in listings}
    rule = by_number["2026-11-12-01"]
    assert rule.title == "Amendments to Rule 1.15, RPC, Rule 407, SCACR"
    assert rule.pdf_url == "https://www.sccourts.org/media/courtOrders/PDFs/2026-11-12-01.pdf"
    assert rule.date == date(2026, 11, 12)
    for listing in listings:  # every order's own PDF (never a "related order" link)
        assert listing.pdf_url == f"https://www.sccourts.org/media/courtOrders/PDFs/{listing.number}.pdf"
        assert listing.date is not None and listing.title
    assert by_number["2026-02-13-04"].title == "Local Rules for Richland County Court of Common Pleas"
    assert by_number["2026-03-04-01"].title == (
        "Local Rule for Ninth Judicial Circuit \u2013 Standing Discovery Protective Order"
    )
    assert not any("copying" in listing.title.lower() for listing in listings)


def test_orders_listing_synthetic_and_layout_change(caplog):
    html = (
        '<section class="results-listing"><div class="result-table">'
        '<article class="result" data-ordernumber="2027-02-10-01"><div class="order-link">'
        '<a href="/media/courtOrders/PDFs/2027-02-10-01.pdf">2027-02-10-01</a></div>'
        '<p class="case-name">Re:  Something   New</p></article>'
        '<article class="result" data-ordernumber="2027-02-11-01"><p class="case-name">No PDF</p></article>'
        '<article class="result" data-ordernumber="Special-1"><div class="order-link">'
        '<a href="https://www.sccourts.org/media/courtOrders/PDFs/special.pdf">x</a></div>'
        '<p class="case-name">Odd number</p></article>'
        "</div></section>"
    )
    with caplog.at_level(logging.WARNING, logger="src.sources.sccourts"):
        listings = parse_orders_listing(html)
    assert listings == [
        OrderListing(
            "2027-02-10-01", "Re: Something New", "https://www.sccourts.org/media/courtOrders/PDFs/2027-02-10-01.pdf",
            date(2027, 2, 10),
        ),
        OrderListing("Special-1", "Odd number", "https://www.sccourts.org/media/courtOrders/PDFs/special.pdf", None),
    ]  # fmt: skip
    assert any("2027-02-11-01" in r.getMessage() for r in caplog.records)
    with pytest.raises(LayoutChangedError):
        parse_orders_listing("<html><body>Nothing here</body></html>")


def test_match_order_rule_1_15_matches_the_listing():
    listings = orders_listing()
    match = match_order(RULE_TITLE, listings, date(2026, 11, 12))
    assert match is not None and match.number == "2026-11-12-01"
    assert match_order(RULE_TITLE, listings, date(2026, 12, 12)).number == "2026-11-12-01"  # 30 days: still in
    assert match_order(RULE_TITLE, listings, date(2026, 12, 13)) is None  # 31 days: out of the window


@pytest.mark.parametrize("near", [date(2026, 11, 4), date(2026, 11, 12)])
def test_match_order_copying_fees_matches_nothing(near):
    assert match_order(FEES_TITLE, orders_listing(), near) is None


def test_match_order_prefers_the_closest_date():
    title = "Committee to Perform Duties of Disciplinary Counsel, Rule 29, RJDE, Rule 502, SCACR"
    listings = orders_listing()
    assert match_order(title, listings, date(2026, 1, 20)).number == "2026-01-09-01"
    assert match_order(title, listings, date(2026, 4, 1)).number == "2026-04-08-02"
    early = OrderListing("2027-03-01-01", "Amendments to Rule 410, SCACR", "https://x.test/a.pdf", date(2027, 3, 1))
    late = OrderListing("2027-03-20-01", "Amendments to Rule 410, SCACR", "https://x.test/b.pdf", date(2027, 3, 20))
    assert match_order("Re: Amendments to Rule 410, SCACR", [early, late], date(2027, 3, 18)) is late
    assert match_order("Re: Amendments to Rule 410, SCACR", [early, late], date(2027, 3, 5)) is early


def test_match_order_normalization_containment_and_jaccard():
    listing = OrderListing(
        "2026-05-19-02", "Amendments to Rule 509, South Carolina Appellate Court Rules", "https://x.test/509.pdf",
        date(2026, 5, 19),
    )  # fmt: skip
    near = date(2026, 5, 21)
    # case, a leading "RE:", and punctuation do not matter
    assert match_order("RE: AMENDMENTS TO RULE 509 -- SOUTH CAROLINA APPELLATE COURT RULES.", [listing], near)
    # one title containing the other (at word boundaries)
    assert match_order("Re: Amendments to Rule 509", [listing], near)
    # token-set Jaccard of exactly 0.8 ("Amendment" vs "Amendments")
    assert match_order("Re: Amendment to Rule 509, South Carolina Appellate Court Rules", [listing], near)
    # different rule numbers never match, even with a high Jaccard index
    assert match_order("Re: Amendments to Rule 510, South Carolina Appellate Court Rules", [listing], near) is None
    # unrelated titles, one-word containment, empty titles, and undated listings do not match
    assert match_order("Re: Interest Rate on Money Decrees and Judgments", [listing], near) is None
    assert match_order("Re: Amendments", [listing], near) is None
    assert match_order("Re:", [listing], near) is None
    undated = OrderListing("X-1", listing.title, listing.pdf_url, None)
    assert match_order(listing.title, [undated], near) is None


def test_order_date_from_text():
    texts = pages41()
    assert order_date_from_text("\n".join(texts[0:3])) == date(2026, 11, 4)
    assert order_date_from_text("\n".join(texts[25:27])) == date(2026, 11, 12)
    assert order_date_from_text("Effective January 1, 2027.\nColumbia, South Carolina\nDecember 3, 2026") == date(
        2026, 12, 3
    )
    assert order_date_from_text("Signed Nov. 4, 2026") == date(2026, 11, 4)
    assert order_date_from_text("Sept. 30, 2026 and then February 30, 2026") == date(2026, 9, 30)
    assert order_date_from_text("November 4, 2026; the lawyer may 5, 2026 pay") == date(2026, 11, 4)
    assert order_date_from_text("No date at all (2007 & Supp. 2025).") is None
    assert order_date_from_text("") is None and order_date_from_text(None) is None


# --------------------------------------------------------------------------- order items


def test_build_order_items_for_sheet_41():
    texts = pages41()
    items = build_order_items(SHEET41, parse_advance_sheet_contents(texts), texts, orders_listing())
    assert [item.key for item in items] == [FEES_KEY, RULE_KEY]
    fees, rule = items
    for item in items:
        assert item.kind is ItemKind.ORDER
        assert item.court is Court.SUPREME
        assert item.pdf_url is None
        assert item.advance_sheet == 41
        assert item.source == "sccourts"

    assert rule.case_name == RULE_TITLE
    assert rule.number == "2026-11-12-01"
    assert rule.filed_date == date(2026, 11, 12)
    assert rule.link_url == "https://www.sccourts.org/media/courtOrders/PDFs/2026-11-12-01.pdf"
    assert rule.mirror_url == SHEET41_URL + "#page=26"
    assert rule.text == "\n".join(clean_text(text) for text in texts[25:27])  # page-number lines dropped
    assert "Rule 1.15" in rule.text and "charging lien" in rule.text
    assert "Gary Moore" not in rule.text and "Reid Fleming" not in rule.text

    assert fees.case_name == FEES_TITLE
    assert fees.number is None
    assert fees.filed_date == date(2026, 11, 4)  # the signature date (not on the orders listing)
    assert fees.link_url == SHEET41_URL + "#page=1"
    assert fees.mirror_url is None
    assert fees.text == "\n".join(clean_text(text) for text in texts[0:3])
    assert "Fees for Copying" in fees.text and "IT IS SO ORDERED." in fees.text
    assert "ADVANCE SHEET NO." not in fees.text


def test_build_order_items_without_the_orders_listing_links_to_the_sheet():
    texts = pages41()
    fees, rule = build_order_items(SHEET41, parse_advance_sheet_contents(texts), texts, [])
    assert rule.number is None
    assert rule.key == "ORDER:2026-11-12:re-amendments-to-rule-1-15-rpc-rule-407-scacr"
    assert rule.filed_date == date(2026, 11, 12)
    assert rule.link_url == SHEET41_URL + "#page=26"
    assert rule.mirror_url is None
    assert fees.key == FEES_KEY


def test_an_order_has_the_same_identity_whether_or_not_the_orders_page_matched_it():
    # One run may match the order to the All Court Orders page (ORDER:2026-11-12-01) and another may
    # not (the page failed or lagged): the date-and-title key links the two, so it is emailed once.
    texts = pages41()
    contents = parse_advance_sheet_contents(texts)
    matched = build_order_items(SHEET41, contents, texts, orders_listing())[1]
    unmatched = build_order_items(SHEET41, contents, texts, [])[1]
    assert matched.key == RULE_KEY
    assert matched.alias_keys == [unmatched.key]
    assert unmatched.alias_keys == []  # its date-and-title key is its own key


def test_build_order_items_dates_and_blank_pages(caplog):
    sheet = AdvanceSheet(7, date(2027, 2, 18), "https://www.sccourts.org/media/opinions/advSheets/no072027.pdf")
    texts = ["Re: Undated order text", "   ", "Opinion text"]
    contents = AdvanceSheetContents(
        entries=[
            ContentsEntry(ItemKind.ORDER, Court.SUPREME, None, "Re: Undated Order", 1, 1),
            ContentsEntry(ItemKind.ORDER, Court.SUPREME, None, "Re: Blank Order", 2, 2),
            ContentsEntry(ItemKind.OPINION, Court.SUPREME, "28400", "A v. B", 3, 3),
        ],
        cover_page=None,
        total_pages=3,
    )
    with caplog.at_level(logging.WARNING, logger="src.sources.sccourts"):
        items = build_order_items(sheet, contents, texts, [])
    assert [(item.case_name, item.filed_date, item.link_url) for item in items] == [
        ("Re: Undated Order", date(2027, 2, 18), sheet.pdf_url + "#page=1")
    ]
    assert any("Re: Blank Order" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- fetch (end to end with mocked HTTP)


def test_fetch_end_to_end(rsps):
    register_site(rsps)
    result = fetch(make_session(), [Court.SUPREME, Court.APPEALS], FILED_AFTER, True,
                   pdf_to_pages=fake_pdf_to_pages, today=TODAY)  # fmt: skip
    assert result.source == "sccourts"
    assert result.warnings == []
    assert result.advance_sheets == [SHEET41]  # Sheet 40 (Nov 4) is older than filed_after
    assert [item.key for item in result.items] == ["SC:28354", "SC:28355", FEES_KEY, RULE_KEY, "COA:6170"]
    by_key = {item.key: item for item in result.items}
    for key in ("SC:28354", "SC:28355", "COA:6170"):
        assert by_key[key].advance_sheet == 41  # set because the opinion is in the sheet's CONTENTS
        assert by_key[key].text is None  # listing opinions are downloaded from their own PDF later
        assert by_key[key].refiled is False
    assert by_key["COA:6170"].link_url == "https://www.sccourts.org/media/opinions/HTMLFiles/COA/6170%20.pdf"
    assert by_key[RULE_KEY].link_url.endswith("/courtOrders/PDFs/2026-11-12-01.pdf")
    assert "Rule 1.15" in by_key[RULE_KEY].text
    assert "Fees for Copying" in by_key[FEES_KEY].text
    urls = requested(rsps)
    assert sorted(urls) == sorted([SC_NOV, COA_NOV, ADVANCE_SHEETS_URL, SHEET41_URL, ALL_ORDERS_URL])
    assert SHEET40_URL not in urls
    assert result.checked_advance_sheets == [SHEET41]  # read and parsed: its orders were really checked


def test_fetch_filters_listing_opinions_by_filed_date(rsps):
    register_site(rsps, october=True)
    result = fetch(make_session(), [Court.SUPREME, Court.APPEALS], date(2026, 10, 25), True,
                   pdf_to_pages=fake_pdf_to_pages, today=TODAY)  # fmt: skip
    keys = [item.key for item in result.items]
    # 28353, 6167 and 6168 were filed October 21, before filed_after; 6169 (October 28) is kept.
    assert keys == ["SC:28354", "SC:28355", FEES_KEY, RULE_KEY, "COA:6169", "COA:6170"]
    assert [sheet.number for sheet in result.advance_sheets] == [40, 41]
    assert result.warnings == []
    assert {item.key: item.advance_sheet for item in result.items}["COA:6169"] is None
    urls = requested(rsps)
    assert {SC_OCT, COA_OCT, SC_NOV, COA_NOV, SHEET40_URL, SHEET41_URL} <= set(urls)
    assert urls.count(ALL_ORDERS_URL) == 1


def test_fetch_adds_an_opinion_that_is_in_the_sheet_but_not_on_the_listing(rsps):
    # The Court of Appeals page comes back empty, so 6170 is known only from the advance sheet.
    register_site(rsps, coa_nov="sc_published_2026-12_empty.html")
    result = fetch(make_session(), [Court.SUPREME, Court.APPEALS], FILED_AFTER, True,
                   pdf_to_pages=fake_pdf_to_pages, today=TODAY)  # fmt: skip
    by_key = {item.key: item for item in result.items}
    moore = by_key["COA:6170"]
    assert moore.kind is ItemKind.OPINION and moore.court is Court.APPEALS
    assert moore.case_name == "Gary Moore v. State"
    assert moore.number == "6170"
    assert moore.filed_date == date(2026, 11, 12)
    assert moore.pdf_url is None
    assert moore.link_url == SHEET41_URL + "#page=28"
    assert moore.text == "\n".join(clean_text(text) for text in pages41()[27:43])
    assert "KONDUROS, J.:" in moore.text
    assert moore.advance_sheet == 41 and moore.source == "sccourts"
    assert len(result.warnings) == 1
    warning = result.warnings[0]
    assert "6170" in warning and "Gary Moore v. State" in warning and "Advance Sheet No. 41" in warning
    assert "pages 28-43" in warning


def test_fetch_does_not_re_add_a_listed_opinion_filed_before_the_window(rsps):
    # Here the sheet is dated a day after its opinions, and the window starts on the sheet's
    # date: the listing opinions are too old to keep, and being in the sheet must not bring
    # them back as sheet-only items.
    add_html(rsps, SC_NOV, page("sc_published_2026-11.html"))
    add_html(rsps, COA_NOV, page("coa_published_2026-11.html"))
    add_html(
        rsps,
        ADVANCE_SHEETS_URL,
        '<section class="results-listing"><div class="result-title advance-sheet-result">'
        f'<h2><a href="{SHEET41_URL}">Advance Sheet 41</a></h2>'
        '<p class="subtitle">November 13, 2026</p></div></section>',
    )
    rsps.add(responses.GET, SHEET41_URL, body=PDF41, content_type="application/pdf")
    add_html(rsps, ALL_ORDERS_URL, page("all_court_orders_2026.html"))
    result = fetch(make_session(), [Court.SUPREME, Court.APPEALS], date(2026, 11, 13), True,
                   pdf_to_pages=fake_pdf_to_pages, today=TODAY)  # fmt: skip
    assert [item.key for item in result.items] == [FEES_KEY, RULE_KEY]
    assert result.warnings == []


def test_fetch_only_requested_courts(rsps):
    register_site(rsps)
    result = fetch(make_session(), [Court.APPEALS], FILED_AFTER, True, pdf_to_pages=fake_pdf_to_pages, today=TODAY)
    # Orders follow INCLUDE_ORDERS, not the court list; Supreme Court opinions are not added from the sheet.
    assert [item.key for item in result.items] == [FEES_KEY, RULE_KEY, "COA:6170"]
    assert result.warnings == []
    assert SC_NOV not in requested(rsps)


def test_fetch_without_orders_never_touches_the_advance_sheets(rsps):
    register_site(rsps)
    result = fetch(make_session(), [Court.SUPREME, Court.APPEALS], FILED_AFTER, False,
                   pdf_to_pages=fake_pdf_to_pages, today=TODAY)  # fmt: skip
    assert [item.key for item in result.items] == ["SC:28354", "SC:28355", "COA:6170"]
    assert result.advance_sheets == [] and result.warnings == []
    assert all(item.advance_sheet is None for item in result.items)
    assert sorted(requested(rsps)) == sorted([SC_NOV, COA_NOV])


def test_fetch_reads_every_month_in_the_window_across_a_year_boundary(rsps):
    empty = page("sc_published_2026-12_empty.html")
    urls = [listing_url(court, y, m) for court in (Court.SUPREME, Court.APPEALS) for y, m in ((2026, 12), (2027, 1))]
    for url in urls:
        add_html(rsps, url, empty)
    result = fetch(make_session(), [Court.APPEALS, Court.SUPREME], date(2026, 12, 28), False,
                   pdf_to_pages=fake_pdf_to_pages, today=date(2027, 1, 3))  # fmt: skip
    assert result.items == []
    assert requested(rsps) == urls  # Supreme Court first, months in order


def test_fetch_skips_sheets_dated_after_today_and_fetches_orders_only_when_needed(rsps):
    register_site(rsps)
    result = fetch(make_session(), [Court.SUPREME], date(2026, 11, 1), True,
                   pdf_to_pages=fake_pdf_to_pages, today=date(2026, 11, 10))  # fmt: skip
    assert result.advance_sheets == [SHEET40]  # Sheet 41 (November 12) is after "today"
    urls = requested(rsps)
    assert SHEET40_URL in urls and SHEET41_URL not in urls
    assert ALL_ORDERS_URL not in urls  # Sheet 40 has no orders, so the orders page is not needed
    assert not any(item.kind is ItemKind.ORDER for item in result.items)


def test_fetch_reads_the_orders_page_once_for_several_sheets(rsps):
    register_site(rsps, october=True)
    result = fetch(make_session(), [Court.SUPREME, Court.APPEALS], date(2026, 10, 25), True,
                   pdf_to_pages=lambda data: pages41(), today=TODAY)  # fmt: skip
    assert requested(rsps).count(ALL_ORDERS_URL) == 1
    assert [item.key for item in result.items if item.kind is ItemKind.ORDER] == [FEES_KEY, RULE_KEY]  # deduped


def test_fetch_marks_refiled_listing_opinions(rsps):
    register_site(rsps)
    texts = pages41()
    texts[4] = texts[4].replace(
        "28354 \u2013 In the Matter of Louis David Nettles 11\n",
        "28354 \u2013 In the Matter of Louis David Nettles 11\n"
        "(Withdrawn, Substituted, and Refiled November 12, 2026)\n",
    )
    result = fetch(make_session(), [Court.SUPREME, Court.APPEALS], FILED_AFTER, True,
                   pdf_to_pages=lambda data: texts, today=TODAY)  # fmt: skip
    by_key = {item.key: item for item in result.items}
    assert by_key["SC:28354"].refiled is True
    assert by_key["SC:28355"].refiled is False


@pytest.mark.parametrize("status", [404, 503])
def test_fetch_listing_failure_raises_source_error(rsps, status):
    add_html(rsps, SC_NOV, "error", status=status)
    with pytest.raises(SourceError, match="Supreme Court published-opinions page for November 2026") as info:
        fetch(make_session(), [Court.SUPREME, Court.APPEALS], FILED_AFTER, True,
              pdf_to_pages=fake_pdf_to_pages, today=TODAY)  # fmt: skip
    assert f"HTTP {status}" in str(info.value)
    assert "SOURCE=courtlistener" in str(info.value)


def test_fetch_listing_forbidden_explains_blocking(rsps):
    add_html(rsps, SC_NOV, "Forbidden", status=403)
    with pytest.raises(SourceError, match="blocking automated requests"):
        fetch(make_session(), [Court.SUPREME], FILED_AFTER, False, pdf_to_pages=fake_pdf_to_pages, today=TODAY)


def test_fetch_listing_connection_error_raises_source_error(rsps):
    rsps.add(responses.GET, SC_NOV, body=requests.ConnectionError("connection reset"))
    with pytest.raises(SourceError, match="connection reset"):
        fetch(make_session(), [Court.SUPREME], FILED_AFTER, False, pdf_to_pages=fake_pdf_to_pages, today=TODAY)


def test_fetch_listing_layout_change_propagates(rsps):
    add_html(rsps, SC_NOV, "<html><body><p>New website!</p></body></html>")
    with pytest.raises(LayoutChangedError):
        fetch(make_session(), [Court.SUPREME], FILED_AFTER, True, pdf_to_pages=fake_pdf_to_pages, today=TODAY)


def test_fetch_advance_sheets_page_failure_is_a_warning(rsps):
    add_html(rsps, SC_NOV, page("sc_published_2026-11.html"))
    add_html(rsps, COA_NOV, page("coa_published_2026-11.html"))
    add_html(rsps, ADVANCE_SHEETS_URL, "oops", status=500)
    result = fetch(make_session(), [Court.SUPREME, Court.APPEALS], FILED_AFTER, True,
                   pdf_to_pages=fake_pdf_to_pages, today=TODAY)  # fmt: skip
    assert [item.key for item in result.items] == ["SC:28354", "SC:28355", "COA:6170"]
    assert result.advance_sheets == []
    assert result.checked_advance_sheets == []  # no orders were checked
    assert len(result.warnings) == 1
    assert "advance sheets page" in result.warnings[0] and "HTTP 500" in result.warnings[0]
    assert "\n" not in result.warnings[0]


def test_fetch_advance_sheets_page_layout_change_is_a_warning(rsps):
    add_html(rsps, SC_NOV, page("sc_published_2026-11.html"))
    add_html(rsps, ADVANCE_SHEETS_URL, "<html><body>Redesigned</body></html>")
    result = fetch(make_session(), [Court.SUPREME], FILED_AFTER, True, pdf_to_pages=fake_pdf_to_pages, today=TODAY)
    assert [item.key for item in result.items] == ["SC:28354", "SC:28355"]
    assert len(result.warnings) == 1 and "advance sheets page" in result.warnings[0]


def test_fetch_advance_sheet_pdf_failure_is_a_warning(rsps):
    add_html(rsps, SC_NOV, page("sc_published_2026-11.html"))
    add_html(rsps, COA_NOV, page("coa_published_2026-11.html"))
    add_html(rsps, ADVANCE_SHEETS_URL, page("advance_sheets.html"))
    rsps.add(responses.GET, SHEET41_URL, status=404)
    result = fetch(make_session(), [Court.SUPREME, Court.APPEALS], FILED_AFTER, True,
                   pdf_to_pages=fake_pdf_to_pages, today=TODAY)  # fmt: skip
    assert [item.key for item in result.items] == ["SC:28354", "SC:28355", "COA:6170"]
    assert result.advance_sheets == [SHEET41]  # the sheet exists, even though its PDF could not be read
    assert result.checked_advance_sheets == []  # but its orders were not checked
    assert len(result.warnings) == 1
    assert "Advance Sheet No. 41" in result.warnings[0] and "HTTP 404" in result.warnings[0]
    assert ALL_ORDERS_URL not in requested(rsps)


def test_fetch_pdf_text_extraction_failure_is_a_warning(rsps):
    register_site(rsps)

    def unreadable(data: bytes) -> list[str]:
        raise ValueError("no text layer in this PDF")

    result = fetch(make_session(), [Court.SUPREME, Court.APPEALS], FILED_AFTER, True,
                   pdf_to_pages=unreadable, today=TODAY)  # fmt: skip
    assert [item.key for item in result.items] == ["SC:28354", "SC:28355", "COA:6170"]
    assert len(result.warnings) == 1 and "no text layer in this PDF" in result.warnings[0]


def test_fetch_orders_page_failure_links_orders_to_the_sheet(rsps):
    add_html(rsps, SC_NOV, page("sc_published_2026-11.html"))
    add_html(rsps, COA_NOV, page("coa_published_2026-11.html"))
    add_html(rsps, ADVANCE_SHEETS_URL, page("advance_sheets.html"))
    rsps.add(responses.GET, SHEET41_URL, body=PDF41, content_type="application/pdf")
    add_html(rsps, ALL_ORDERS_URL, "gone", status=404)
    result = fetch(make_session(), [Court.SUPREME, Court.APPEALS], FILED_AFTER, True,
                   pdf_to_pages=fake_pdf_to_pages, today=TODAY)  # fmt: skip
    orders = [item for item in result.items if item.kind is ItemKind.ORDER]
    assert [(item.number, item.link_url) for item in orders] == [
        (None, SHEET41_URL + "#page=1"),
        (None, SHEET41_URL + "#page=26"),
    ]
    assert len(result.warnings) == 1 and "All Court Orders page" in result.warnings[0]
