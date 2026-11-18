"""Tests for src/sources/courtlistener.py.

Everything runs against the saved fixtures in tests/fixtures/courtlistener/ and the
`responses` library: no test touches the network or needs a real CourtListener token.
Any request to a URL that is not registered (sccourts.org included) fails the test.
"""

from __future__ import annotations

import copy
import logging
from datetime import date
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
import responses
from responses import matchers

from src.models import Court, ItemKind, ScrapeResult
from src.net import PoliteSession
from src.sources import LayoutChangedError, SourceError
from src.sources import courtlistener as cl
from tests.conftest import fixture_bytes, fixture_json, fixture_text

LOGGER = "src.sources.courtlistener"
TOKEN = "cl-test-token-7f3a9c"
SAMPLE = "courtlistener/search_v4_sample.json"
SC_FEED_FILE = "courtlistener/feed_sc.xml"
COA_FEED_FILE = "courtlistener/feed_scctapp.xml"
SC_FEED_URL = "https://www.courtlistener.com/feed/court/sc/"
COA_FEED_URL = "https://www.courtlistener.com/feed/court/scctapp/"
ATOM_TYPE = "application/atom+xml; charset=utf-8"
BOTH = [Court.SUPREME, Court.APPEALS]
FILED_AFTER = date(2026, 11, 2)  # a run on Nov 12 with LOOKBACK_DAYS=10
WEEK_START = date(2026, 11, 6)  # a run on Nov 16 with LOOKBACK_DAYS=10
CURSOR = "cz0yMDI2LTExLTEy"
NEXT_URL = (
    "https://www.courtlistener.com/api/rest/v4/search/?court=sc+scctapp&cursor=" + CURSOR
    + "&filed_after=2026-11-02&order_by=dateFiled+desc&stat_Published=on&type=o"
)
STORAGE = "https://storage.courtlistener.com/pdf/2026/11/12/"

SPEC_NO_TOKEN_WARNING = (
    "No COURTLISTENER_API_TOKEN is set, so opinions come from CourtListener's public feeds, which list "
    "only the 20 newest decisions per court (published and unpublished). A busy week can push opinions out "
    "of a feed; add a free CourtListener API token for complete results."
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _session() -> PoliteSession:
    # No throttling and no urllib3 retries: call counts are exact and tests stay fast.
    return PoliteSession("SC-Advance-Sheet-Digest/test (pytest)", min_interval=0, max_retries=0)


def _sample_result(index: int) -> dict:
    """A copy of one sample result: 0 Nettles, 1 Fleming, 2 Moore, 3 unpublished Smith."""
    return copy.deepcopy(fixture_json(SAMPLE)["results"][index])


def _page(*results, next_url: str | None = None) -> dict:
    return {"count": len(results), "next": next_url, "previous": None, "results": list(results)}


def _result(cluster: int, case_name: str, docket: str, filed: str, court_id: str = "sc") -> dict:
    """A minimal, valid search result in the documented v4 shape."""
    slug = case_name.lower().replace(" ", "-").replace(".", "")
    folder = "SC" if court_id == "sc" else "COA"
    return {
        "absolute_url": f"/opinion/{cluster}/{slug}/",
        "caseName": case_name,
        "cluster_id": cluster,
        "court_id": court_id,
        "dateFiled": filed,
        "docketNumber": docket,
        "status": "Published",
        "opinions": [
            {
                "download_url": f"https://www.sccourts.org/media/opinions/HTMLFiles/{folder}/{docket}.pdf",
                "id": cluster + 500000,
                "local_path": f"pdf/{filed.replace('-', '/')}/{slug.replace('-', '_')}.pdf",
                "snippet": "",
            }
        ],
    }


def _parse_one(result: dict):
    items, _ = cl.parse_search_results(_page(result))
    assert len(items) == 1, items
    return items[0]


def _add_api_page(rsps, payload, *, cursor: str | None = None, courts=BOTH, filed_after=FILED_AFTER) -> None:
    params = cl.search_params(courts, filed_after)
    if cursor:
        params = {**params, "cursor": cursor}
    rsps.get(cl.API_SEARCH_URL, json=payload, match=[matchers.query_param_matcher(params)])


def _add_feeds(rsps, *, sc: bool = True, coa: bool = True) -> None:
    if sc:
        rsps.get(SC_FEED_URL, body=fixture_bytes(SC_FEED_FILE), content_type=ATOM_TYPE)
    if coa:
        rsps.get(COA_FEED_URL, body=fixture_bytes(COA_FEED_FILE), content_type=ATOM_TYPE)


def _atom(*entries: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<feed xml:lang="en-us" xmlns="http://www.w3.org/2005/Atom">'
        "<title>CourtListener.com: test feed</title>" + "".join(entries) + "</feed>"
    )


def _entry(
    cluster: str = "11070001",
    *,
    title: str = "State v. Example",
    published: str | None = "2026-11-12T00:00:00-08:00",
    updated: str | None = None,
    category: str | None = "Published",
    summary: str = "&lt;p&gt;THE STATE OF SOUTH CAROLINA In The Supreme Court Opinion No. 28360&lt;/p&gt;",
    enclosure: str | None = STORAGE + "state_v._example.pdf",
    alternate: str | None = "default",
    entry_id: str | None = "default",
) -> str:
    page = f"https://www.courtlistener.com/opinion/{cluster}/state-v-example/" if alternate == "default" else alternate
    ident = page if entry_id == "default" else entry_id
    parts = [f"<entry><title>{title}</title>"]
    if page:
        parts.append(f'<link href="{page}" rel="alternate"/>')
    if published:
        parts.append(f"<published>{published}</published>")
    if updated:
        parts.append(f"<updated>{updated}</updated>")
    if ident:
        parts.append(f"<id>{ident}</id>")
    parts.append(f'<summary type="html">{summary}</summary>')
    if enclosure:
        parts.append(f'<link href="{enclosure}" length="0" rel="enclosure" type="application/pdf"/>')
    if category:
        parts.append(f'<category term="{category}"/>')
    parts.append("</entry>")
    return "".join(parts)


def _warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------


def test_constants():
    assert cl.API_SEARCH_URL == "https://www.courtlistener.com/api/rest/v4/search/"
    assert cl.FEED_URL == "https://www.courtlistener.com/feed/court/{court_id}/"
    assert cl.FEED_URL.format(court_id="scctapp") == COA_FEED_URL
    assert cl.STORAGE_BASE == "https://storage.courtlistener.com/"
    assert cl.SITE_BASE == "https://www.courtlistener.com"
    assert cl.COURT_IDS == {Court.SUPREME: "sc", Court.APPEALS: "scctapp"}
    assert cl.MAX_PAGES == 5
    assert cl.NO_TOKEN_WARNING == SPEC_NO_TOKEN_WARNING


# ---------------------------------------------------------------------------
# normalize_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://www.sccourts.org/media/opinions/HTMLFiles/COA/6170 .pdf",
            "https://www.sccourts.org/media/opinions/HTMLFiles/COA/6170%20.pdf",
        ),
        (  # already encoded: no double encoding (%2520)
            "https://www.sccourts.org/media/opinions/HTMLFiles/COA/6170%20.pdf",
            "https://www.sccourts.org/media/opinions/HTMLFiles/COA/6170%20.pdf",
        ),
        ("  https://storage.courtlistener.com/pdf/x.pdf \n", "https://storage.courtlistener.com/pdf/x.pdf"),
        ("http://example.org/a b.pdf", "http://example.org/a%20b.pdf"),
        ("HTTPS://Example.org/x.pdf", "HTTPS://Example.org/x.pdf"),
        ("https://example.org/café.pdf", "https://example.org/caf%C3%A9.pdf"),
        ("https://example.org/search?q=a b&page=2", "https://example.org/search?q=a%20b&page=2"),
    ],
)
def test_normalize_url_encodes_unsafe_characters_once(raw, expected):
    assert cl.normalize_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "ftp://example.org/a.pdf",
        "javascript:alert(1)",
        "mailto:clerk@example.org",
        "/media/opinions/HTMLFiles/SC/28355.pdf",
        "www.sccourts.org/media/opinions/HTMLFiles/SC/28355.pdf",
        "https://",
        "https:///path-only",
        "https://[::1/broken-ipv6",
        12345,
    ],
)
def test_normalize_url_rejects_empty_and_non_http_values(raw):
    assert cl.normalize_url(raw) is None


# ---------------------------------------------------------------------------
# opinion_number_from_text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Opinion No. 28355", "28355"),
        ("Appellate Case No. 2025-002086 Opinion No. 28355 Heard September 16, 2026", "28355"),
        ("Opinion No.28355", "28355"),
        ("Opinion No . 28355", "28355"),
        ("Opinion No 28355", "28355"),
        ("Opinion  No.   6170", "6170"),
        ("Opinion\nNo.\n6170\nHeard October 7, 2026", "6170"),
        ("Opinion\xa0No.\xa028354", "28354"),
        ("OPINION NO. 28354", "28354"),
        ("Opinion No. 28354 ... as in Opinion No. 27000", "28354"),  # the header's number comes first
        ("Unpublished Opinion No. 2026-UP-421", None),
        ("THIS OPINION HAS NO PRECEDENTIAL VALUE.", None),
        ("Appellate Case No. 2024-000459", None),
        ("Opinion No. 12", None),
        ("Opinion No. 1234567", None),
        ("Opinion No. ２８３５５", None),  # fullwidth digits are not an opinion number

        ("", None),
        (None, None),
    ],
)
def test_opinion_number_from_text(text, expected):
    assert cl.opinion_number_from_text(text) == expected


@pytest.mark.parametrize(
    ("relpath", "expected"),
    [
        ("opinion_texts/sc_28354.txt", "28354"),
        ("opinion_texts/sc_28355.txt", "28355"),
        ("opinion_texts/coa_6170.txt", "6170"),
    ],
)
def test_opinion_number_from_real_opinion_text(relpath, expected):
    assert cl.opinion_number_from_text(fixture_text(relpath)) == expected


# ---------------------------------------------------------------------------
# search_params
# ---------------------------------------------------------------------------


def test_search_params_both_courts():
    assert cl.search_params(BOTH, FILED_AFTER) == {
        "type": "o",
        "court": "sc scctapp",
        "filed_after": "2026-11-02",
        "order_by": "dateFiled desc",
        "stat_Published": "on",
    }


def test_search_params_single_court_order_and_duplicates():
    assert cl.search_params([Court.APPEALS], date(2026, 1, 5))["court"] == "scctapp"
    assert cl.search_params([Court.SUPREME], date(2026, 1, 5))["court"] == "sc"
    assert cl.search_params([Court.APPEALS, Court.SUPREME, Court.APPEALS], date(2026, 1, 5))["court"] == "sc scctapp"
    assert cl.search_params([Court.APPEALS], date(2026, 1, 5))["filed_after"] == "2026-01-05"


def test_search_params_refuses_an_empty_court_list():
    # An empty court parameter would search every court in the country.
    with pytest.raises(ValueError):
        cl.search_params([], FILED_AFTER)


# ---------------------------------------------------------------------------
# parse_search_results: the sample response
# ---------------------------------------------------------------------------


def test_parse_search_results_sample_keeps_published_only():
    items, next_url = cl.parse_search_results(fixture_json(SAMPLE))
    assert next_url is None
    assert [item.case_name for item in items] == [
        "In the Matter of Louis David Nettles",
        "Reid Fleming v. LG Chem Ltd.",
        "Gary Moore v. State",
    ]  # "Jane and John Smith v. SCDSS" is Unpublished
    for item in items:
        assert item.kind is ItemKind.OPINION
        assert item.source == "courtlistener-api"
        assert item.filed_date == date(2026, 11, 12)
        assert urlsplit(item.pdf_url).hostname == "storage.courtlistener.com"
        assert item.court_summary == ""
        assert item.text is None and item.author is None and item.advance_sheet is None


def test_parse_search_results_sample_numbers():
    nettles, fleming, moore = cl.parse_search_results(fixture_json(SAMPLE))[0]
    assert nettles.number == "28354"  # docketNumber 2026-000394 is a case number -> from the snippet
    assert fleming.number == "28355"  # docketNumber
    assert moore.number is None  # docketNumber 2024-000459 and an empty snippet


def test_parse_search_results_sample_links():
    nettles, fleming, moore = cl.parse_search_results(fixture_json(SAMPLE))[0]
    assert nettles.pdf_url == STORAGE + "in_the_matter_of_louis_david_nettles.pdf"
    assert nettles.link_url == "https://www.sccourts.org/media/opinions/HTMLFiles/SC/28354.pdf"
    assert nettles.mirror_url == nettles.pdf_url
    assert fleming.pdf_url == STORAGE + "reid_fleming_v._lg_chem_ltd..pdf"
    assert fleming.link_url == "https://www.sccourts.org/media/opinions/HTMLFiles/SC/28355.pdf"
    assert fleming.mirror_url == fleming.pdf_url
    assert moore.pdf_url == STORAGE + "gary_moore_v._state.pdf"
    # The court's URL has a raw space ("COA/6170 .pdf"); only the encoded form works.
    assert moore.link_url == "https://www.sccourts.org/media/opinions/HTMLFiles/COA/6170%20.pdf"
    assert moore.link_url.endswith("6170%20.pdf") and " " not in moore.link_url
    assert moore.mirror_url == moore.pdf_url


def test_parse_search_results_sample_ids_and_keys():
    nettles, fleming, moore = cl.parse_search_results(fixture_json(SAMPLE))[0]
    assert (nettles.court, fleming.court, moore.court) == (Court.SUPREME, Court.SUPREME, Court.APPEALS)
    assert (nettles.source_id, fleming.source_id, moore.source_id) == ("11060101", "11060100", "11060103")
    assert nettles.key == "SC:28354" and nettles.alias_keys == ["CL:11060101"]
    assert fleming.key == "SC:28355" and fleming.alias_keys == ["CL:11060100"]
    assert moore.key == "CL:11060103" and moore.alias_keys == []


def test_parse_search_results_returns_next_url():
    payload = fixture_json(SAMPLE)
    payload["next"] = NEXT_URL
    assert cl.parse_search_results(payload)[1] == NEXT_URL
    for empty in ("", "   ", None, 42):
        payload["next"] = empty
        assert cl.parse_search_results(payload)[1] is None


def test_parse_search_results_empty_page():
    assert cl.parse_search_results(_page()) == ([], None)


@pytest.mark.parametrize(
    "payload",
    [None, [], "oops", {}, {"results": None}, {"results": {"0": {}}}, {"detail": "Invalid token."}],
)
def test_parse_search_results_rejects_a_response_that_is_not_a_search_page(payload):
    with pytest.raises(LayoutChangedError):
        cl.parse_search_results(payload)


# ---------------------------------------------------------------------------
# parse_search_results: tolerance (the sample was built from the documented schema)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("index", [0, 1, 2])
@pytest.mark.parametrize("how", ["missing", "null"])
def test_parse_search_results_survives_any_missing_or_null_field(index, how):
    base = _sample_result(index)
    neighbor = _sample_result(1 if index != 1 else 0)
    variants: list[tuple[str, dict]] = []
    for key in base:
        variant = copy.deepcopy(base)
        if how == "missing":
            del variant[key]
        else:
            variant[key] = None
        variants.append((key, variant))
    for key in base["opinions"][0]:
        variant = copy.deepcopy(base)
        if how == "missing":
            del variant["opinions"][0][key]
        else:
            variant["opinions"][0][key] = None
        variants.append((f"opinions[0].{key}", variant))
    for name, variant in variants:
        items, _ = cl.parse_search_results(_page(variant, neighbor))  # must never raise
        assert neighbor["caseName"] in [item.case_name for item in items], name
        for item in items:
            assert item.link_url and item.link_url.startswith("https://"), name
            assert item.pdf_url is None or urlsplit(item.pdf_url).hostname == "storage.courtlistener.com", name


def test_unreadable_results_are_skipped_with_a_warning(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    no_name = _sample_result(0)
    no_name["caseName"] = None
    bad_date = _sample_result(2)
    bad_date["dateFiled"] = "not a date"
    items, _ = cl.parse_search_results(_page(no_name, _sample_result(1), bad_date, "garbage", None, 42))
    assert [item.case_name for item in items] == ["Reid Fleming v. LG Chem Ltd."]
    assert len(_warnings(caplog)) == 5
    assert "Gary Moore v. State" in caplog.text and "dateFiled" in caplog.text


def test_case_name_full_is_used_when_case_name_is_missing():
    result = _sample_result(1)
    result["caseName"] = ""
    result["caseNameFull"] = "Reid Fleming,  Petitioner,\n v. LG Chem, Ltd."
    assert _parse_one(result).case_name == "Reid Fleming, Petitioner, v. LG Chem, Ltd."


def test_case_name_whitespace_is_collapsed():
    result = _sample_result(1)
    result["caseName"] = "  Reid   Fleming\n v. LG Chem Ltd. "
    assert _parse_one(result).case_name == "Reid Fleming v. LG Chem Ltd."


def test_missing_status_counts_as_published_other_statuses_are_dropped(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    no_status = _sample_result(0)
    del no_status["status"]  # the request asked for published opinions only
    lowercase = _sample_result(1)
    lowercase["status"] = "published"
    errata = _sample_result(2)
    errata["status"] = "Errata"
    items, _ = cl.parse_search_results(_page(no_status, lowercase, errata, _sample_result(3)))
    assert [item.number for item in items] == ["28354", "28355"]
    assert _warnings(caplog) == []  # filtering is expected, not a problem


def test_other_courts_are_ignored_and_a_missing_court_id_falls_back_to_the_court_name(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    other_court = _sample_result(0)
    other_court["court_id"] = "nc"
    no_court_id = _sample_result(2)  # "court": "Court of Appeals of South Carolina"
    no_court_id["court_id"] = None
    no_court_at_all = _sample_result(1)
    no_court_at_all["court_id"] = None
    no_court_at_all["court"] = None
    items, _ = cl.parse_search_results(_page(other_court, no_court_id, no_court_at_all))
    assert [(item.case_name, item.court) for item in items] == [("Gary Moore v. State", Court.APPEALS)]
    assert len(_warnings(caplog)) == 1  # only the result with no court at all is a problem


def test_supreme_court_inferred_from_court_name():
    result = _sample_result(1)
    del result["court_id"]  # "court": "Supreme Court of South Carolina"
    assert _parse_one(result).court is Court.SUPREME


def test_court_id_is_case_insensitive():
    result = _sample_result(2)
    result["court_id"] = "SCCTAPP"
    assert _parse_one(result).court is Court.APPEALS


def test_cluster_id_falls_back_to_absolute_url():
    result = _sample_result(1)
    result["cluster_id"] = None
    item = _parse_one(result)
    assert item.source_id == "11060100"
    assert item.key == "SC:28355" and item.alias_keys == ["CL:11060100"]


def test_no_cluster_id_anywhere_gives_a_name_based_key():
    result = _sample_result(2)
    result["cluster_id"] = None
    result["absolute_url"] = None
    item = _parse_one(result)
    assert item.source_id is None
    assert item.key == "COA:2026-11-12:gary-moore-v-state"
    assert item.mirror_url == item.pdf_url  # still has CourtListener's stored PDF as the mirror


@pytest.mark.parametrize(
    ("docket", "snippet", "expected"),
    [
        ("28355", "", "28355"),
        (" 28355 ", "", "28355"),
        (28355, "", "28355"),  # a number instead of a string
        ("6170", None, "6170"),
        ("2025-002086", "Appellate Case No. 2025-002086 Opinion No. 28355 Heard", "28355"),
        ("2026-UP-421", "", None),
        ("12", "", None),
        ("1234567", "", None),
        (None, "Opinion No. <mark>28355</mark> Heard", "28355"),  # highlight markup
        (None, None, None),
        ([], {"a": 1}, None),  # wrong types are ignored, not fatal
    ],
)
def test_number_rules(docket, snippet, expected):
    result = _sample_result(1)
    result["docketNumber"] = docket
    result["opinions"][0]["snippet"] = snippet
    assert _parse_one(result).number == expected


def test_number_from_a_later_opinion_snippet():
    result = _sample_result(2)  # Moore: docket 2024-000459, empty snippet
    extra = copy.deepcopy(result["opinions"][0])
    extra.update(id=1, local_path="", snippet="Gary Moore v. State Opinion No. 6170 Heard October 7, 2026")
    result["opinions"].append(extra)
    assert _parse_one(result).number == "6170"


def test_the_first_opinion_with_a_local_path_is_used():
    result = _sample_result(1)
    first = copy.deepcopy(result["opinions"][0])
    first.update(local_path=None, download_url="https://www.sccourts.org/media/opinions/HTMLFiles/SC/28355-old.pdf")
    second = copy.deepcopy(result["opinions"][0])
    second.update(local_path="pdf/2026/11/12/reid_fleming_v._lg_chem_ltd._2.pdf")
    result["opinions"] = [first, second]
    item = _parse_one(result)
    assert item.pdf_url == STORAGE + "reid_fleming_v._lg_chem_ltd._2.pdf"
    assert item.link_url == "https://www.sccourts.org/media/opinions/HTMLFiles/SC/28355.pdf"
    assert item.mirror_url == item.pdf_url


def test_no_stored_pdf_never_falls_back_to_downloading_from_sccourts(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    result = _sample_result(1)
    result["opinions"][0]["local_path"] = None
    item = _parse_one(result)
    # The pipeline downloads pdf_url, and this source must never send it to sccourts.org.
    assert item.pdf_url is None
    assert item.link_url == "https://www.sccourts.org/media/opinions/HTMLFiles/SC/28355.pdf"
    assert item.mirror_url == "https://www.courtlistener.com/opinion/11060100/reid-fleming-v-lg-chem-ltd/"
    assert len(_warnings(caplog)) == 1 and "no stored PDF" in caplog.text


def test_no_stored_pdf_falls_back_to_a_download_url_on_another_host():
    result = _sample_result(1)
    result["opinions"][0]["local_path"] = ""
    result["opinions"][0]["download_url"] = "https://opinions.example.org/SC/28355 final.pdf"
    item = _parse_one(result)
    assert item.pdf_url == "https://opinions.example.org/SC/28355%20final.pdf"
    assert item.link_url == item.pdf_url
    assert item.mirror_url == "https://www.courtlistener.com/opinion/11060100/reid-fleming-v-lg-chem-ltd/"


def test_http_download_url_is_not_used_as_the_reader_link():
    result = _sample_result(1)
    result["opinions"][0]["download_url"] = "http://www.sccourts.org/media/opinions/HTMLFiles/SC/28355.pdf"
    item = _parse_one(result)
    assert item.link_url == item.pdf_url == STORAGE + "reid_fleming_v._lg_chem_ltd..pdf"
    assert item.mirror_url == "https://www.courtlistener.com/opinion/11060100/reid-fleming-v-lg-chem-ltd/"


def test_no_download_url_links_to_the_stored_copy():
    result = _sample_result(1)
    result["opinions"][0]["download_url"] = None
    item = _parse_one(result)
    assert item.link_url == item.pdf_url == STORAGE + "reid_fleming_v._lg_chem_ltd..pdf"
    assert item.mirror_url == "https://www.courtlistener.com/opinion/11060100/reid-fleming-v-lg-chem-ltd/"


def test_no_opinions_links_to_the_courtlistener_page():
    result = _sample_result(1)
    result["opinions"] = None
    item = _parse_one(result)
    assert item.pdf_url is None
    assert item.link_url == "https://www.courtlistener.com/opinion/11060100/reid-fleming-v-lg-chem-ltd/"
    assert item.mirror_url is None  # it would only repeat link_url
    assert item.number == "28355"


def test_result_with_no_link_at_all_is_skipped(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    result = _sample_result(1)
    result["opinions"] = []
    result["absolute_url"] = None
    assert cl.parse_search_results(_page(result)) == ([], None)
    assert len(_warnings(caplog)) == 1


@pytest.mark.parametrize(
    ("local_path", "expected"),
    [
        ("/pdf/2026/11/12/a b.pdf", "https://storage.courtlistener.com/pdf/2026/11/12/a%20b.pdf"),
        ("pdf/2026/11/12/john_doe_#1.pdf", "https://storage.courtlistener.com/pdf/2026/11/12/john_doe_%231.pdf"),
        ("https://storage.courtlistener.com/pdf/x.pdf", "https://storage.courtlistener.com/pdf/x.pdf"),
    ],
)
def test_local_path_variants(local_path, expected):
    result = _sample_result(1)
    result["opinions"][0]["local_path"] = local_path
    assert _parse_one(result).pdf_url == expected


def test_relative_absolute_url_and_datetime_date_filed():
    result = _sample_result(1)
    result["absolute_url"] = "opinion/11060100/reid-fleming-v-lg-chem-ltd/"
    result["dateFiled"] = "2026-11-12T00:00:00-08:00"
    result["opinions"] = None
    item = _parse_one(result)
    assert item.filed_date == date(2026, 11, 12)
    assert item.link_url == "https://www.courtlistener.com/opinion/11060100/reid-fleming-v-lg-chem-ltd/"


# ---------------------------------------------------------------------------
# parse_feed: the real feeds
# ---------------------------------------------------------------------------


def test_parse_feed_supreme_court_fixture():
    items = cl.parse_feed(fixture_text(SC_FEED_FILE), Court.SUPREME)
    assert len(items) == 16  # of 20 entries; 4 are unpublished
    names = [item.case_name for item in items]
    for unpublished in (
        "Lonny S. Wright v. Brandy D. Duncan",
        "Roman v. PNC Financial",
        "Vannata v. Town of Sullivan's Island",
        "In the Matter of Robert B. Sharp",
    ):
        assert unpublished not in names
    for item in items:
        assert item.kind is ItemKind.OPINION
        assert item.court is Court.SUPREME
        assert item.source == "courtlistener-feed"
        assert item.pdf_url.startswith("https://storage.courtlistener.com/pdf/")
        assert item.link_url == item.pdf_url  # the court's own URL is unknown in feed mode
        assert item.mirror_url.startswith(f"https://www.courtlistener.com/opinion/{item.source_id}/")


def test_parse_feed_supreme_court_this_week():
    by_name = {item.case_name: item for item in cl.parse_feed(fixture_text(SC_FEED_FILE), Court.SUPREME)}
    nettles = by_name["In the Matter of Louis David Nettles"]
    assert nettles.filed_date == date(2026, 11, 12)
    assert nettles.number == "28354"
    assert nettles.source_id == "11060101"
    assert nettles.key == "SC:28354" and nettles.alias_keys == ["CL:11060101"]
    assert nettles.pdf_url == STORAGE + "in_the_matter_of_louis_david_nettles.pdf"
    assert nettles.mirror_url == "https://www.courtlistener.com/opinion/11060101/in-the-matter-of-louis-david-nettles/"

    fleming = by_name["Reid Fleming v. LG Chem Ltd."]
    assert fleming.filed_date == date(2026, 11, 12)
    assert fleming.source_id == "11060100"
    assert fleming.pdf_url == STORAGE + "reid_fleming_v._lg_chem_ltd..pdf"
    # This feed entry's summary stops before the header line, so the feed cannot tell
    # the number (28355); the pipeline reads it from the PDF header later.
    assert "28355" not in fixture_text(SC_FEED_FILE)
    assert fleming.number is None
    assert fleming.key == "CL:11060100"


def test_parse_feed_supreme_court_numbers_and_dates():
    items = cl.parse_feed(fixture_text(SC_FEED_FILE), Court.SUPREME)
    assert {item.case_name: item.number for item in items if item.number} == {
        "In the Matter of Louis David Nettles": "28354",
        "In the Matter of Jason F. Taylor": "28353",
        "In the Matter of Sara Elizabeth Smoot": "28351",
        "In the Matter of Paul W. Owen": "28348",
        "In the Matter of Courtney N. Gilchrist": "28346",
        "In the Matter of John D. Compton, III.": "28347",
        "In the Matter of Michael D. Brown": "28349",
        "SCDSS v. Gerardo Pimienta": "28345",
        "SC Board of Financial Instituions v. CDM Corp, Inc.": "28342",
        "Town of Hilton Head v. Beaufort County": "28341",
        "In the Matter of Shawn T. Daily": "28340",
    }
    by_name = {item.case_name: item for item in items}
    # The date part as written; "-07:00" (summer) stamps are not shifted.
    assert by_name["In the Matter of Jason F. Taylor"].filed_date == date(2026, 10, 21)
    assert by_name["In the Matter of Shawn T. Daily"].filed_date == date(2026, 6, 17)


def test_parse_feed_court_of_appeals_fixture():
    items = cl.parse_feed(fixture_text(COA_FEED_FILE), Court.APPEALS)
    assert len(items) == 1  # 19 of its 20 entries are unpublished
    moore = items[0]
    assert moore.case_name == "Gary Moore v. State"
    assert moore.filed_date == date(2026, 11, 12)
    assert moore.court is Court.APPEALS
    # Unlike Fleming's, this entry's summary includes "Opinion No. 6170".
    assert "Opinion No. 6170" in fixture_text(COA_FEED_FILE)
    assert moore.number == "6170"
    assert moore.source_id == "11060103"
    assert moore.key == "COA:6170" and moore.alias_keys == ["CL:11060103"]
    assert moore.pdf_url == moore.link_url == STORAGE + "gary_moore_v._state.pdf"
    assert moore.mirror_url == "https://www.courtlistener.com/opinion/11060103/gary-moore-v-state/"
    assert moore.source == "courtlistener-feed"


def test_parse_feed_accepts_bytes_and_a_leading_bom():
    assert [i.key for i in cl.parse_feed(fixture_bytes(COA_FEED_FILE), Court.APPEALS)] == ["COA:6170"]
    assert len(cl.parse_feed("\ufeff\n" + fixture_text(COA_FEED_FILE), Court.APPEALS)) == 1
    assert len(cl.parse_feed(b"\xef\xbb\xbf" + fixture_bytes(COA_FEED_FILE), Court.APPEALS)) == 1


def test_no_item_from_the_fixtures_downloads_from_sccourts():
    items = (
        cl.parse_search_results(fixture_json(SAMPLE))[0]
        + cl.parse_feed(fixture_text(SC_FEED_FILE), Court.SUPREME)
        + cl.parse_feed(fixture_text(COA_FEED_FILE), Court.APPEALS)
    )
    assert len(items) == 20
    for item in items:
        assert urlsplit(item.pdf_url).hostname == "storage.courtlistener.com", item.label


# ---------------------------------------------------------------------------
# parse_feed: synthetic entries
# ---------------------------------------------------------------------------


def test_parse_feed_only_published_entries():
    xml = _atom(
        _entry("1", category="Unpublished"),
        _entry("2", category=None),
        _entry("3", category="published"),
        _entry("4"),
    )
    assert [item.source_id for item in cl.parse_feed(xml, Court.SUPREME)] == ["3", "4"]


def test_parse_feed_title_is_unescaped_and_collapsed():
    xml = _atom(
        _entry("1", title="Paul R. Vannatta v. Town of Sullivan&amp;#x27;s Island"),
        _entry("2", title="  AT&amp;T  Corp.\n v. Smith &amp;notary "),
    )
    names = [item.case_name for item in cl.parse_feed(xml, Court.SUPREME)]
    # Real entities are unescaped; a literal "&notary" is not mistaken for "&not;".
    assert names == ["Paul R. Vannatta v. Town of Sullivan's Island", "AT&T Corp. v. Smith &notary"]


def test_parse_feed_number_comes_from_the_tag_stripped_summary():
    xml = _atom(
        _entry("1", summary="&lt;p&gt;Supreme Court Opinion&lt;br&gt;No.&lt;b&gt;28361&lt;/b&gt; Heard&lt;/p&gt;"),
        _entry("2", summary="&lt;p&gt;Unpublished Opinion No. 2026-UP-100&lt;/p&gt;"),
        _entry("3", summary=""),
    )
    assert [item.number for item in cl.parse_feed(xml, Court.SUPREME)] == ["28361", None, None]


def test_parse_feed_published_date_is_taken_as_written():
    xml = _atom(
        _entry("1", published="2026-11-12T00:00:00-08:00"),
        _entry("2", published="2026-11-12T23:30:00+14:00"),
        _entry("3", published=None, updated="2026-11-05T00:00:00-08:00"),  # falls back to <updated>
    )
    dates = [item.filed_date for item in cl.parse_feed(xml, Court.APPEALS)]
    assert dates == [date(2026, 11, 12), date(2026, 11, 12), date(2026, 11, 5)]


def test_parse_feed_entry_without_enclosure_links_to_the_opinion_page():
    item = cl.parse_feed(_atom(_entry("11070002", enclosure=None)), Court.SUPREME)[0]
    assert item.pdf_url is None
    assert item.link_url == "https://www.courtlistener.com/opinion/11070002/state-v-example/"
    assert item.mirror_url is None


def test_parse_feed_page_from_entry_id_when_there_is_no_alternate_link():
    page = "https://www.courtlistener.com/opinion/11070009/state-v-example/"
    item = cl.parse_feed(_atom(_entry("11070009", alternate=None, entry_id=page)), Court.SUPREME)[0]
    assert item.source_id == "11070009"
    assert item.mirror_url == page


def test_parse_feed_enclosure_space_is_encoded():
    item = cl.parse_feed(_atom(_entry("5", enclosure=STORAGE + "state v. example.pdf")), Court.SUPREME)[0]
    assert item.pdf_url == STORAGE + "state%20v.%20example.pdf"


def test_parse_feed_skips_unreadable_entries_with_a_warning(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    xml = _atom(
        _entry("1", published=None),  # no date at all
        _entry("2", title="   "),  # no title
        _entry("3", enclosure=None, alternate=None, entry_id="tag:courtlistener.com,2026:3"),  # no link
        _entry("4"),
    )
    assert [item.source_id for item in cl.parse_feed(xml, Court.SUPREME)] == ["4"]
    assert len(_warnings(caplog)) == 3


def test_parse_feed_with_no_entries_is_empty():
    assert cl.parse_feed(_atom(), Court.SUPREME) == []


@pytest.mark.parametrize(
    "document",
    [
        "",
        "   ",
        b"",
        "not xml at all",
        "<html><body>Service Unavailable</body></html>",
        "<html><body><p>unclosed</body></html>",
        '<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>',
        "<feed><title>no Atom namespace</title></feed>",
        '{"results": []}',
    ],
)
def test_parse_feed_rejects_documents_that_are_not_atom_feeds(document):
    with pytest.raises(LayoutChangedError):
        cl.parse_feed(document, Court.SUPREME)


# ---------------------------------------------------------------------------
# fetch: API mode
# ---------------------------------------------------------------------------


def test_fetch_api_mode_single_page():
    with responses.RequestsMock() as rsps:
        rsps.get(
            cl.API_SEARCH_URL,
            json=fixture_json(SAMPLE),
            match=[
                matchers.query_param_matcher(cl.search_params(BOTH, FILED_AFTER)),
                matchers.header_matcher({"Authorization": f"Token {TOKEN}", "Accept": "application/json"}),
            ],
        )
        result = cl.fetch(_session(), BOTH, FILED_AFTER, TOKEN)
        calls = list(rsps.calls)
    assert isinstance(result, ScrapeResult)
    assert result.source == "courtlistener-api"
    assert result.warnings == []
    assert result.advance_sheets == []
    assert [item.key for item in result.items] == ["SC:28354", "SC:28355", "CL:11060103"]
    assert len(calls) == 1
    headers = calls[0].request.headers
    assert headers["Authorization"] == f"Token {TOKEN}"
    assert headers["Accept"] == "application/json"
    assert headers["User-Agent"].startswith("SC-Advance-Sheet-Digest/")


def test_fetch_api_mode_follows_the_next_chain():
    page1 = fixture_json(SAMPLE)
    page1["next"] = NEXT_URL
    page2 = _page(
        _result(11055000, "Jane Doe v. Richard Roe", "28352", "2026-11-05"),
        _sample_result(1),  # Fleming again on the next page -> deduplicated
        _result(11040000, "Old Case v. Older Case", "28339", "2026-10-30"),  # before filed_after -> dropped
    )
    with responses.RequestsMock() as rsps:
        _add_api_page(rsps, page1)
        _add_api_page(rsps, page2, cursor=CURSOR)
        result = cl.fetch(_session(), BOTH, FILED_AFTER, TOKEN)
        calls = list(rsps.calls)
    assert len(calls) == 2
    assert all(call.request.headers["Authorization"] == f"Token {TOKEN}" for call in calls)
    assert parse_qs(urlsplit(calls[1].request.url).query)["cursor"] == [CURSOR]
    # Supreme Court first, then filing date, then number.
    assert [item.key for item in result.items] == ["SC:28352", "SC:28354", "SC:28355", "CL:11060103"]
    assert result.warnings == []


def test_fetch_api_mode_follows_a_relative_next_link():
    page1 = _page(_sample_result(1), next_url="/api/rest/v4/search/?cursor=abc")
    with responses.RequestsMock() as rsps:
        _add_api_page(rsps, page1)
        rsps.get(
            cl.API_SEARCH_URL, json=_page(_sample_result(0)), match=[matchers.query_param_matcher({"cursor": "abc"})]
        )
        result = cl.fetch(_session(), BOTH, FILED_AFTER, TOKEN)
    assert [item.key for item in result.items] == ["SC:28354", "SC:28355"]


def test_fetch_api_mode_reads_at_most_max_pages():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        for n in range(1, cl.MAX_PAGES + 2):  # one page more than MAX_PAGES exists
            payload = _page(
                _result(11050000 + n, f"Case {n} v. State", str(28300 + n), "2026-11-12"),
                next_url=f"{cl.API_SEARCH_URL}?cursor=page{n + 1}",
            )
            params = cl.search_params(BOTH, FILED_AFTER) if n == 1 else {"cursor": f"page{n}"}
            rsps.get(cl.API_SEARCH_URL, json=payload, match=[matchers.query_param_matcher(params)])
        result = cl.fetch(_session(), BOTH, FILED_AFTER, TOKEN)
        assert len(rsps.calls) == cl.MAX_PAGES
    assert [item.number for item in result.items] == ["28301", "28302", "28303", "28304", "28305"]
    assert len(result.warnings) == 1
    assert "more than 5 pages" in result.warnings[0]


def test_fetch_api_mode_rejected_token_401():
    with responses.RequestsMock() as rsps:
        rsps.get(cl.API_SEARCH_URL, status=401, json={"detail": "Invalid token."})
        with pytest.raises(SourceError) as excinfo:
            cl.fetch(_session(), BOTH, FILED_AFTER, TOKEN)
    assert str(excinfo.value) == (
        "CourtListener rejected the API token (HTTP 401). Check the COURTLISTENER_API_TOKEN secret."
    )
    assert TOKEN not in str(excinfo.value)
    assert not isinstance(excinfo.value, LayoutChangedError)


def test_fetch_api_mode_rejected_token_403():
    with responses.RequestsMock() as rsps:
        rsps.get(cl.API_SEARCH_URL, status=403, json={"detail": "You do not have permission."})
        with pytest.raises(SourceError, match=r"rejected the API token \(HTTP 403\).*COURTLISTENER_API_TOKEN"):
            cl.fetch(_session(), BOTH, FILED_AFTER, TOKEN)


def test_fetch_api_mode_token_rejected_on_a_later_page():
    with responses.RequestsMock() as rsps:
        _add_api_page(rsps, _page(_sample_result(1), next_url=NEXT_URL))
        page2_params = {**cl.search_params(BOTH, FILED_AFTER), "cursor": CURSOR}
        rsps.get(cl.API_SEARCH_URL, status=401, match=[matchers.query_param_matcher(page2_params)])
        with pytest.raises(SourceError, match="HTTP 401"):
            cl.fetch(_session(), BOTH, FILED_AFTER, TOKEN)


@pytest.mark.parametrize("status", [500, 503])
def test_fetch_api_mode_server_errors_say_try_again_later(status):
    with responses.RequestsMock() as rsps:
        rsps.get(cl.API_SEARCH_URL, status=status, json={"detail": "nope"})
        with pytest.raises(SourceError) as excinfo:
            cl.fetch(_session(), BOTH, FILED_AFTER, TOKEN)
        assert len(rsps.calls) == 1
    message = str(excinfo.value)
    assert "Could not search CourtListener's API" in message and f"HTTP {status}" in message
    assert "(nope)" in message and "temporarily unavailable" in message
    assert "COURTLISTENER_API_TOKEN" not in message  # not a token problem
    assert TOKEN not in message


@pytest.mark.parametrize("status", [400, 404, 410])
def test_fetch_api_mode_permanent_errors_point_to_an_api_change(status):
    # A rejected parameter or a retired endpoint fails every run: "try again later" would mislead.
    with responses.RequestsMock() as rsps:
        rsps.get(cl.API_SEARCH_URL, status=status, json={"detail": "Invalid parameter: filed_after"})
        with pytest.raises(SourceError) as excinfo:
            cl.fetch(_session(), BOTH, FILED_AFTER, TOKEN)
        assert len(rsps.calls) == 1
    message = str(excinfo.value)
    assert message == (
        f"CourtListener's API refused the search (HTTP {status}: Invalid parameter: filed_after). Its API may have "
        "changed; see 'If a source changes its format' in OPERATING.md."
    )
    assert "temporarily" not in message and TOKEN not in message


def test_fetch_api_mode_refused_next_page_link_suggests_a_rerun():
    with responses.RequestsMock() as rsps:
        _add_api_page(rsps, _page(_sample_result(1), next_url=NEXT_URL))
        page2_params = {**cl.search_params(BOTH, FILED_AFTER), "cursor": CURSOR}
        rsps.get(cl.API_SEARCH_URL, status=400, json={"detail": "Invalid cursor"},
                 match=[matchers.query_param_matcher(page2_params)])  # fmt: skip
        with pytest.raises(SourceError) as excinfo:
            cl.fetch(_session(), BOTH, FILED_AFTER, TOKEN)
    message = str(excinfo.value)
    assert message.startswith("CourtListener refused the link to the next page of search results (HTTP 400: Invalid cursor)")
    assert "Run the workflow again" in message


def test_error_detail_never_contains_the_token():
    with responses.RequestsMock() as rsps:
        rsps.get(cl.API_SEARCH_URL, status=400, json={"detail": f"Bad request from token {TOKEN}"})
        with pytest.raises(SourceError) as excinfo:
            cl.fetch(_session(), BOTH, FILED_AFTER, TOKEN)
    assert TOKEN not in str(excinfo.value) and "[redacted]" in str(excinfo.value)


def test_fetch_api_mode_rate_limited():
    with responses.RequestsMock() as rsps:
        rsps.get(cl.API_SEARCH_URL, status=429)
        with pytest.raises(SourceError, match=r"HTTP 429"):
            cl.fetch(_session(), BOTH, FILED_AFTER, TOKEN)


def test_fetch_api_mode_network_error():
    with responses.RequestsMock() as rsps:
        rsps.get(cl.API_SEARCH_URL, body=requests.ConnectionError("network is unreachable"))
        with pytest.raises(SourceError, match="Could not search CourtListener's API"):
            cl.fetch(_session(), BOTH, FILED_AFTER, TOKEN)


def test_fetch_api_mode_response_that_is_not_json():
    with responses.RequestsMock() as rsps:
        rsps.get(cl.API_SEARCH_URL, body="<html>Down for maintenance</html>", content_type="text/html")
        with pytest.raises(SourceError, match="did not return JSON"):
            cl.fetch(_session(), BOTH, FILED_AFTER, TOKEN)


def test_fetch_api_mode_json_that_is_not_a_search_page():
    with responses.RequestsMock() as rsps:
        rsps.get(cl.API_SEARCH_URL, json={"detail": "Something else entirely."})
        with pytest.raises(LayoutChangedError):
            cl.fetch(_session(), BOTH, FILED_AFTER, TOKEN)


@pytest.mark.parametrize(
    "next_url",
    [
        "https://api.example.com/v4/search/?cursor=abc",
        "http://www.courtlistener.com/api/rest/v4/search/?cursor=abc",  # not https
        "https://www.courtlistener.com.example.net/api/rest/v4/search/?cursor=abc",
    ],
)
def test_fetch_api_mode_never_sends_the_token_anywhere_else(next_url):
    with responses.RequestsMock() as rsps:  # an unregistered request would fail the test
        _add_api_page(rsps, _page(_sample_result(0), _sample_result(1), next_url=next_url))
        result = cl.fetch(_session(), BOTH, FILED_AFTER, TOKEN)
        assert len(rsps.calls) == 1
    assert [item.key for item in result.items] == ["SC:28354", "SC:28355"]
    assert len(result.warnings) == 1 and "was not followed" in result.warnings[0]


def test_fetch_api_mode_reports_unreadable_results(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    payload = fixture_json(SAMPLE)
    payload["results"] += [{"caseName": None}, "junk"]
    with responses.RequestsMock() as rsps:
        _add_api_page(rsps, payload)
        result = cl.fetch(_session(), BOTH, FILED_AFTER, TOKEN)
    assert len(result.items) == 3
    assert result.warnings == [
        "2 CourtListener search result(s) could not be read and were skipped (details are in the log above)."
    ]


def test_fetch_api_mode_fails_loudly_when_no_result_has_a_known_status():
    # If CourtListener relabels "Published" (say, to its display label "Precedential"), every result
    # would be filtered out and the run would look like a quiet week. It must fail instead.
    payload = fixture_json(SAMPLE)
    for result in payload["results"]:
        result["status"] = "Precedential" if result["status"] == "Published" else "Non-Precedential"
    with responses.RequestsMock() as rsps:
        _add_api_page(rsps, payload)
        with pytest.raises(LayoutChangedError) as excinfo:
            cl.fetch(_session(), BOTH, FILED_AFTER, TOKEN)
    message = str(excinfo.value)
    assert "returned 4 result(s), but none could be read as a published South Carolina opinion" in message
    assert "4 not marked Published (statuses seen: 'Non-Precedential', 'Precedential')" in message
    assert "may have changed its API" in message


def test_fetch_api_mode_fails_loudly_when_every_result_is_unreadable():
    payload = fixture_json(SAMPLE)
    for result in payload["results"]:
        result["case_name"] = result.pop("caseName")  # a renamed field
    with responses.RequestsMock() as rsps:
        _add_api_page(rsps, payload)
        with pytest.raises(LayoutChangedError, match=r"3 unreadable"):
            cl.fetch(_session(), BOTH, FILED_AFTER, TOKEN)


def test_fetch_api_mode_results_from_other_courts_only_fail_loudly():
    page = _page(_result(11055001, "Out Of State v. Case", "28350", "2026-11-12", court_id="nc"))
    with responses.RequestsMock() as rsps:
        _add_api_page(rsps, page)
        with pytest.raises(LayoutChangedError, match="1 from other courts"):
            cl.fetch(_session(), BOTH, FILED_AFTER, TOKEN)


def test_fetch_api_mode_empty_results_are_a_quiet_week_not_an_error():
    with responses.RequestsMock() as rsps:
        _add_api_page(rsps, _page())
        result = cl.fetch(_session(), BOTH, FILED_AFTER, TOKEN)
    assert result.items == [] and result.warnings == []


def test_fetch_feed_mode_fails_loudly_when_no_entry_has_a_known_category():
    xml = _atom(_entry("1", category="Precedential"), _entry("2", category=None))
    with responses.RequestsMock() as rsps:
        rsps.get(SC_FEED_URL, body=xml, content_type=ATOM_TYPE)
        with pytest.raises(LayoutChangedError) as excinfo:
            cl.fetch(_session(), [Court.SUPREME], WEEK_START, None)
    message = str(excinfo.value)
    assert "Supreme Court feed lists 2 decision(s), but none is marked Published or Unpublished" in message
    assert "categories seen: 'Precedential'" in message


def test_fetch_feed_mode_warns_about_some_uncategorized_entries():
    xml = _atom(_entry("1"), _entry("2", category="Precedential"))
    with responses.RequestsMock() as rsps:
        rsps.get(SC_FEED_URL, body=xml, content_type=ATOM_TYPE)
        result = cl.fetch(_session(), [Court.SUPREME], WEEK_START, None)
    assert [item.source_id for item in result.items] == ["1"]
    assert any("1 of the 2 entries" in w and "neither Published nor Unpublished" in w for w in result.warnings)


def test_fetch_feed_mode_court_of_appeals_feed_that_is_mostly_unpublished_is_fine():
    # The real Court of Appeals feed is 19 of 20 Unpublished: not a format change.
    with responses.RequestsMock() as rsps:
        _add_feeds(rsps, sc=False)
        result = cl.fetch(_session(), [Court.APPEALS], WEEK_START, None)
    assert [item.key for item in result.items] == ["COA:6170"]
    assert result.warnings == [SPEC_NO_TOKEN_WARNING]


def test_fetch_api_mode_never_logs_the_token(caplog):
    caplog.set_level(logging.DEBUG)
    payload = fixture_json(SAMPLE)
    payload["next"] = NEXT_URL
    with responses.RequestsMock() as rsps:
        _add_api_page(rsps, payload)
        _add_api_page(rsps, _page(), cursor=CURSOR)
        cl.fetch(_session(), BOTH, FILED_AFTER, TOKEN)
    assert caplog.text  # something was logged
    assert TOKEN not in caplog.text


def test_fetch_api_mode_single_court_and_token_whitespace():
    with responses.RequestsMock() as rsps:
        _add_api_page(rsps, _page(_sample_result(2)), courts=[Court.APPEALS])
        result = cl.fetch(_session(), [Court.APPEALS], FILED_AFTER, f"  {TOKEN}\n")
        assert rsps.calls[0].request.headers["Authorization"] == f"Token {TOKEN}"
    assert [item.case_name for item in result.items] == ["Gary Moore v. State"]


@pytest.mark.parametrize("pasted", [f"Token {TOKEN}", f"token {TOKEN}", f"  TOKEN  {TOKEN} "])
def test_fetch_api_mode_tolerates_a_pasted_token_prefix(pasted):
    # People often copy the whole header value ("Token abc...") into the secret.
    with responses.RequestsMock() as rsps:
        _add_api_page(rsps, _page(_sample_result(2)), courts=[Court.APPEALS])
        cl.fetch(_session(), [Court.APPEALS], FILED_AFTER, pasted)
        assert rsps.calls[0].request.headers["Authorization"] == f"Token {TOKEN}"


def test_fetch_api_mode_keeps_only_the_requested_courts():
    # Even if the API ignored the court filter, a disabled court's opinions never come back.
    with responses.RequestsMock() as rsps:
        _add_api_page(rsps, fixture_json(SAMPLE), courts=[Court.APPEALS])
        result = cl.fetch(_session(), [Court.APPEALS], FILED_AFTER, TOKEN)
    assert [item.case_name for item in result.items] == ["Gary Moore v. State"]


def test_fetch_api_mode_filed_after_is_inclusive():
    # The server is asked for filed_after too; the local filter is a safety net with the same floor.
    page = _page(
        _result(11055001, "On The Floor v. State", "28350", "2026-11-02"),
        _result(11055002, "Day Before v. State", "28349", "2026-11-01"),
    )
    with responses.RequestsMock() as rsps:
        _add_api_page(rsps, page)
        result = cl.fetch(_session(), BOTH, FILED_AFTER, TOKEN)
    assert [item.case_name for item in result.items] == ["On The Floor v. State"]


@pytest.mark.parametrize("token", [TOKEN, None])
def test_fetch_with_no_courts_makes_no_requests(token):
    with responses.RequestsMock() as rsps:  # nothing registered: any request would fail
        result = cl.fetch(_session(), [], FILED_AFTER, token, check_feeds=True)
        assert len(rsps.calls) == 0
    assert result.items == []


# ---------------------------------------------------------------------------
# fetch: API mode with check_feeds (opinions the search has not indexed yet)
# ---------------------------------------------------------------------------

NEW_DAY = "2026-11-18T00:00:00-08:00"


def _header(court: str, number: str) -> str:
    """A feed summary (escaped HTML) whose text gives the opinion number."""
    return f"&lt;p&gt;THE STATE OF SOUTH CAROLINA In The {court} Opinion No. {number} Filed November 18, 2026&lt;/p&gt;"


def test_check_feeds_adds_published_opinions_the_search_has_not_indexed(caplog):
    # November 18, 2026: the feeds listed the day's opinions an hour before the search returned them,
    # and CourtListener had briefly stored some of them more than once.
    caplog.set_level(logging.INFO, logger=LOGGER)
    sc_feed = _atom(
        _entry("11065476", title="Tonya Winbush v. City of Anderson", published=NEW_DAY,
               summary=_header("Supreme Court", "28356"), enclosure=STORAGE + "winbush.pdf"),  # new: added
        _entry("11065473", title="Paul David Hess v. Morphis Pediatric Group", published=NEW_DAY,
               summary="&lt;p&gt;THE STATE OF SOUTH CAROLINA In The Supreme Court Paul David Hess&lt;/p&gt;",
               enclosure=STORAGE + "hess.pdf"),  # new, no number in the excerpt: added (its PDF gives the number)
        _entry("11060100", title="Reid Fleming v. LG Chem Ltd.",
               summary="&lt;p&gt;no number in this excerpt&lt;/p&gt;"),  # a search result's cluster: not added
        _entry("11065477", title="In the Matter of Louis David Nettles",
               summary=_header("Supreme Court", "28354")),  # a search result's number, another cluster: not added
        _entry("11065490", title="Unpublished v. Case", published=NEW_DAY, category="Unpublished"),  # not added
        _entry("11040000", title="Old v. Case", published="2026-10-28T00:00:00-08:00",
               summary=_header("Supreme Court", "28339")),  # filed before the window: not added
    )  # fmt: skip
    coa_feed = _atom(
        _entry("11060103", title="Gary Moore v. State",
               summary=_header("Court of Appeals", "6170")),  # the unnumbered search result CL:11060103: not added
        _entry("11065478", title="State v. Phillip Ronald Miller", published=NEW_DAY,
               summary=_header("Court of Appeals", "6171"), enclosure=STORAGE + "miller.pdf"),  # new: added
        _entry("11065479", title="State v. Phillip Ronald Miller", published=NEW_DAY,
               summary=_header("Court of Appeals", "6171"), enclosure=STORAGE + "miller_1.pdf"),  # a 2nd copy: once
    )  # fmt: skip
    with responses.RequestsMock() as rsps:
        _add_api_page(rsps, fixture_json(SAMPLE))
        rsps.get(SC_FEED_URL, body=sc_feed, content_type=ATOM_TYPE)
        rsps.get(COA_FEED_URL, body=coa_feed, content_type=ATOM_TYPE)
        result = cl.fetch(_session(), BOTH, FILED_AFTER, TOKEN, check_feeds=True)
        calls = list(rsps.calls)
    assert [item.key for item in result.items] == [
        "SC:28354", "SC:28355", "SC:28356", "CL:11065473", "CL:11060103", "COA:6171",
    ]  # fmt: skip
    assert result.source == "courtlistener-api" and result.warnings == []
    added = [item for item in result.items if item.source == cl.SOURCE_FEED]
    assert [item.case_name for item in added] == [
        "Tonya Winbush v. City of Anderson", "Paul David Hess v. Morphis Pediatric Group", "State v. Phillip Ronald Miller",
    ]  # fmt: skip
    assert [item.pdf_url for item in added] == [STORAGE + "winbush.pdf", STORAGE + "hess.pdf", STORAGE + "miller.pdf"]
    # Feed-only items are marked, so the pipeline uses them only if their PDF confirms what they are.
    assert [item.ahead_of_search for item in result.items] == [False, False, True, True, False, True]
    # The search first, then each feed; the token goes only to the search.
    assert [urlsplit(c.request.url).path for c in calls] == ["/api/rest/v4/search/", "/feed/court/sc/", "/feed/court/scctapp/"]
    assert calls[0].request.headers["Authorization"] == f"Token {TOKEN}"
    assert not [c for c in calls[1:] if "Authorization" in c.request.headers]
    messages = [r.getMessage() for r in caplog.records if r.name == LOGGER]
    assert any(m.startswith("3 published opinion(s) are in CourtListener's public feeds but not yet") for m in messages)
    assert any("API search, plus 3 from the public feeds" in m for m in messages)
    assert _warnings(caplog) == []


def test_check_feeds_adds_nothing_when_the_search_is_current(caplog):
    # The saved feeds list the same published opinions as the saved search page.
    caplog.set_level(logging.INFO, logger=LOGGER)
    with responses.RequestsMock() as rsps:
        _add_api_page(rsps, fixture_json(SAMPLE))
        _add_feeds(rsps)
        result = cl.fetch(_session(), BOTH, FILED_AFTER, TOKEN, check_feeds=True)
        assert len(rsps.calls) == 3
    assert [item.key for item in result.items] == ["SC:28354", "SC:28355", "CL:11060103"]
    assert result.warnings == [] and _warnings(caplog) == []
    assert not any("not yet in its search results" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("problem", ["server error", "connection error", "not a feed"])
def test_check_feeds_problems_never_stop_the_run(problem, caplog):
    with responses.RequestsMock() as rsps:
        _add_api_page(rsps, fixture_json(SAMPLE))
        if problem == "server error":
            rsps.get(SC_FEED_URL, status=503)
        elif problem == "connection error":
            rsps.get(SC_FEED_URL, body=requests.ConnectionError("connection reset"))
        else:
            rsps.get(SC_FEED_URL, body="<html><body>Service Unavailable</body></html>", content_type="text/html")
        _add_feeds(rsps, sc=False)
        result = cl.fetch(_session(), BOTH, FILED_AFTER, TOKEN, check_feeds=True)
    # The search results are used as they are, and the run is not marked as having a problem.
    assert [item.key for item in result.items] == ["SC:28354", "SC:28355", "CL:11060103"]
    assert result.warnings == []
    logged = [r.getMessage() for r in _warnings(caplog)]
    assert len(logged) == 1
    assert logged[0].startswith("Could not check CourtListener's public Supreme Court feed")
    assert "uses the search results alone" in logged[0] and TOKEN not in logged[0]


def test_check_feeds_reads_only_the_enabled_courts_feed():
    with responses.RequestsMock() as rsps:  # no Court of Appeals feed registered: reading it would fail
        _add_api_page(rsps, _page(_sample_result(0)), courts=[Court.SUPREME])
        rsps.get(SC_FEED_URL, body=fixture_bytes(SC_FEED_FILE), content_type=ATOM_TYPE)
        result = cl.fetch(_session(), [Court.SUPREME], FILED_AFTER, TOKEN, check_feeds=True)
        assert [urlsplit(c.request.url).path for c in rsps.calls] == ["/api/rest/v4/search/", "/feed/court/sc/"]
    # This search page has only Nettles; the feed also lists Fleming, so the feed supplies it.
    assert [item.case_name for item in result.items] == ["In the Matter of Louis David Nettles", "Reid Fleming v. LG Chem Ltd."]
    assert [item.source for item in result.items] == [cl.SOURCE_API, cl.SOURCE_FEED]


def test_check_feeds_changes_nothing_without_a_token():
    with responses.RequestsMock() as rsps:
        _add_feeds(rsps)
        result = cl.fetch(_session(), BOTH, FILED_AFTER, None, check_feeds=True)
        assert len(rsps.calls) == 2  # each feed once
    assert result.source == "courtlistener-feed" and result.warnings[0] == SPEC_NO_TOKEN_WARNING


# ---------------------------------------------------------------------------
# fetch: feed mode (no token)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("token", [None, "", "   "])
def test_fetch_feed_mode_this_week(token):
    with responses.RequestsMock() as rsps:
        _add_feeds(rsps)
        result = cl.fetch(_session(), BOTH, WEEK_START, token)
        calls = list(rsps.calls)
    assert result.source == "courtlistener-feed"
    assert [(item.court, item.case_name, item.number) for item in result.items] == [
        (Court.SUPREME, "In the Matter of Louis David Nettles", "28354"),
        (Court.SUPREME, "Reid Fleming v. LG Chem Ltd.", None),
        (Court.APPEALS, "Gary Moore v. State", "6170"),
    ]
    assert all(item.filed_date == date(2026, 11, 12) for item in result.items)
    assert all(item.source == "courtlistener-feed" for item in result.items)
    # Both feeds reach back past Nov 6 (SC: June 17; COA: Nov 4), so only the general warning.
    assert result.warnings == [SPEC_NO_TOKEN_WARNING]
    assert len(calls) == 2
    assert all("Authorization" not in call.request.headers for call in calls)
    assert {urlsplit(call.request.url).hostname for call in calls} == {"www.courtlistener.com"}


def test_fetch_feed_mode_warns_when_a_feed_does_not_reach_back_far_enough():
    with responses.RequestsMock() as rsps:
        _add_feeds(rsps)
        result = cl.fetch(_session(), BOTH, FILED_AFTER, None)  # Nov 2; the COA feed starts Nov 4
    assert result.warnings[0] == SPEC_NO_TOKEN_WARNING
    assert len(result.warnings) == 2
    reach = result.warnings[1]
    assert "Court of Appeals" in reach and "November 4, 2026" in reach and "November 2, 2026" in reach
    assert "Supreme Court" not in reach  # the Supreme Court feed reaches back to June 17, 2026
    assert [item.key for item in result.items] == ["SC:28354", "CL:11060100", "COA:6170"]


@pytest.mark.parametrize(
    ("filed_after", "warned"),
    [(date(2026, 11, 3), True), (date(2026, 11, 4), False), (date(2026, 11, 5), False)],
)
def test_fetch_feed_mode_reach_back_boundary(filed_after, warned):
    # The COA feed's oldest entry is Nov 4: only a window starting before that can have
    # lost opinions off the end of the feed.
    with responses.RequestsMock() as rsps:
        _add_feeds(rsps, sc=False)
        result = cl.fetch(_session(), [Court.APPEALS], filed_after, None)
    assert (len(result.warnings) == 2) is warned


def test_fetch_feed_mode_reads_only_the_requested_courts():
    with responses.RequestsMock() as rsps:  # the SC feed is not registered
        _add_feeds(rsps, sc=False)
        result = cl.fetch(_session(), [Court.APPEALS], WEEK_START, None)
        assert len(rsps.calls) == 1
    assert [item.key for item in result.items] == ["COA:6170"]


def test_fetch_feed_mode_date_filter_and_sort_order():
    with responses.RequestsMock() as rsps:
        _add_feeds(rsps)
        result = cl.fetch(_session(), BOTH, date(2026, 6, 1), None)
    assert [item.case_name for item in result.items] == [
        "In the Matter of Shawn T. Daily",  # 2026-06-17
        "Town of Hilton Head v. Beaufort County",  # 06-24
        "SC Board of Financial Instituions v. CDM Corp, Inc.",  # 07-15
        "Calvin Henson v. SCDC",  # 07-22, no number: by name
        "Heatley v. State",  # 07-22
        "SCDSS v. Gerardo Pimienta",  # 07-31
        "In the Matter of Courtney N. Gilchrist",  # 08-12: 28346
        "In the Matter of John D. Compton, III.",  # 28347
        "In the Matter of Paul W. Owen",  # 28348
        "In the Matter of Michael D. Brown",  # 28349
        "In the Matter of Sara Elizabeth Smoot",  # 09-02: 28351, then the unnumbered one
        "ACLU v. Wilson",
        "SC Public Interest Foundation v. Oconee County",  # 09-09
        "In the Matter of Jason F. Taylor",  # 10-21
        "In the Matter of Louis David Nettles",  # 11-12: 28354
        "Reid Fleming v. LG Chem Ltd.",
        "Gary Moore v. State",  # Court of Appeals last
    ]
    # A window starting June 1 is older than both feeds reach (June 17 and November 4).
    assert result.warnings[0] == SPEC_NO_TOKEN_WARNING
    assert len(result.warnings) == 3
    assert "Supreme Court feed reaches back only to June 17, 2026" in result.warnings[1]
    assert "Court of Appeals feed reaches back only to November 4, 2026" in result.warnings[2]


def test_fetch_feed_mode_filed_after_is_inclusive():
    with responses.RequestsMock() as rsps:
        _add_feeds(rsps)
        result = cl.fetch(_session(), BOTH, date(2026, 10, 21), None)
    assert [item.number for item in result.items] == ["28353", "28354", None, "6170"]


def test_fetch_feed_mode_deduplicates_by_key():
    xml = _atom(_entry("11070001"), _entry("11070001"), _entry("11070003", summary=""))
    with responses.RequestsMock() as rsps:
        rsps.get(SC_FEED_URL, body=xml, content_type=ATOM_TYPE)
        result = cl.fetch(_session(), [Court.SUPREME], WEEK_START, None)
    assert [item.key for item in result.items] == ["SC:28360", "CL:11070003"]


def test_fetch_feed_mode_http_error():
    with responses.RequestsMock() as rsps:
        rsps.get(SC_FEED_URL, status=404)
        with pytest.raises(SourceError, match="Supreme Court feed"):
            cl.fetch(_session(), BOTH, WEEK_START, None)


def test_fetch_feed_mode_server_error_is_a_source_error():
    with responses.RequestsMock() as rsps:
        _add_feeds(rsps, coa=False)
        rsps.get(COA_FEED_URL, status=503)
        with pytest.raises(SourceError, match="Court of Appeals feed"):
            cl.fetch(_session(), BOTH, WEEK_START, None)


def test_fetch_feed_mode_html_instead_of_a_feed():
    with responses.RequestsMock() as rsps:
        rsps.get(SC_FEED_URL, body="<html><body>Down for maintenance</body></html>", content_type="text/html")
        with pytest.raises(LayoutChangedError, match="not an Atom feed"):
            cl.fetch(_session(), [Court.SUPREME], WEEK_START, None)


def test_fetch_feed_mode_empty_feed_warns():
    with responses.RequestsMock() as rsps:
        rsps.get(SC_FEED_URL, body=_atom(), content_type=ATOM_TYPE)
        result = cl.fetch(_session(), [Court.SUPREME], WEEK_START, None)
    assert result.items == []
    assert result.warnings[0] == SPEC_NO_TOKEN_WARNING
    assert len(result.warnings) == 2 and "listed no decisions" in result.warnings[1]


def test_fetch_feed_mode_reports_unreadable_entries(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    xml = _atom(
        _entry("1", published=None),
        _entry("2"),
        _entry("3", category="Unpublished", published="2026-10-01T00:00:00-07:00"),
    )
    with responses.RequestsMock() as rsps:
        rsps.get(SC_FEED_URL, body=xml, content_type=ATOM_TYPE)
        # The feed reaches back to Oct 1 (its unpublished entry), so no reach-back warning.
        result = cl.fetch(_session(), [Court.SUPREME], date(2026, 10, 1), None)
    assert [item.source_id for item in result.items] == ["2"]
    assert result.warnings == [
        SPEC_NO_TOKEN_WARNING,
        "1 entry(ies) of CourtListener's Supreme Court feed could not be read and were skipped "
        "(details are in the log above).",
    ]


def test_fetch_logs_every_warning_it_returns(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    with responses.RequestsMock() as rsps:
        _add_feeds(rsps)
        result = cl.fetch(_session(), BOTH, FILED_AFTER, None)
    logged = [record.getMessage() for record in _warnings(caplog)]
    assert logged == result.warnings
