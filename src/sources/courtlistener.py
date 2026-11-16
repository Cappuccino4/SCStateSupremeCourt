"""CourtListener source (the default): published South Carolina opinions via Free Law Project.

CourtListener (https://www.courtlistener.com) collects the opinions that both South
Carolina appellate courts publish. With a free API token this module asks the v4 Search
API for every published opinion filed in the lookback window. Without a token it reads
CourtListener's public Atom feeds instead, which list only the 20 newest decisions per
court (published and unpublished), so a busy week can push opinions out of a feed.

This source never contacts sccourts.org. ``Item.pdf_url`` (the file the pipeline
downloads) is CourtListener's stored copy of the PDF; the court's own URL is used only
as ``Item.link_url``, the link readers click in the email.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

from requests.utils import requote_uri

from ..models import Court, Item, ItemKind, ScrapeResult
from ..net import FetchError, PoliteSession
from . import LayoutChangedError, SourceError

log = logging.getLogger(__name__)

API_SEARCH_URL = "https://www.courtlistener.com/api/rest/v4/search/"
FEED_URL = "https://www.courtlistener.com/feed/court/{court_id}/"
STORAGE_BASE = "https://storage.courtlistener.com/"
SITE_BASE = "https://www.courtlistener.com"
COURT_IDS: dict[Court, str] = {Court.SUPREME: "sc", Court.APPEALS: "scctapp"}
MAX_PAGES = 5  # search-result pages read per run; a normal week fits on the first page
API_PAGE_INTERVAL_SECONDS = 13.0  # free API limit: 5 requests/minute, 50/hour, 125/day

SOURCE_API = "courtlistener-api"
SOURCE_FEED = "courtlistener-feed"

NO_TOKEN_WARNING = (
    "No COURTLISTENER_API_TOKEN is set, so opinions come from CourtListener's public feeds, "
    "which list only the 20 newest decisions per court (published and unpublished). A busy week "
    "can push opinions out of a feed; add a free CourtListener API token for complete results."
)

ATOM_NS = "http://www.w3.org/2005/Atom"
_NS = {"atom": ATOM_NS}
_COURT_BY_ID: dict[str, Court] = {court_id: court for court, court_id in COURT_IDS.items()}

# The pipeline downloads Item.pdf_url, so pdf_url never points at these hosts:
# sccourts.org's robots.txt disallows automated agents, and CourtListener mode must not
# touch it. (Readers may still be sent there by link_url.)
_NO_DOWNLOAD_HOSTS = ("sccourts.org",)

# "Opinion No. 28355", tolerating "No.28355", "No . 28355", and line breaks. Not the
# "2026" of an unpublished "Opinion No. 2026-UP-421".
_OPINION_NO_RE = re.compile(r"\bOpinion\s+No\s*\.?\s*([0-9]{3,6})(?![0-9])(?!-\w)", re.IGNORECASE)
_PLAIN_NUMBER_RE = re.compile(r"[0-9]{3,6}")  # a docketNumber that is the opinion number
_ID_RE = re.compile(r"[0-9]+")
_CLUSTER_ID_RE = re.compile(r"/opinion/([0-9]+)(?:/|$)")
_DATE_PREFIX_RE = re.compile(r"\s*([0-9]{4}-[0-9]{2}-[0-9]{2})")
_TAG_RE = re.compile(r"<[^>]*>")
# Only complete entities ("&#x27;", "&amp;"), so a literal "AT&T" or "&notary" survives.
_ENTITY_RE = re.compile(r"&(?:#[0-9]{1,7}|#[xX][0-9A-Fa-f]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")
_ABSOLUTE_URL_RE = re.compile(r"https?://", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Small helpers (public ones are used by tests and by other modules)
# ---------------------------------------------------------------------------


def normalize_url(url: str | None) -> str | None:
    """Clean a URL for links and downloads: strip it and percent-encode unsafe characters.

    A raw space becomes ``%20`` (the court's ``.../COA/6170 .pdf`` only works encoded);
    existing ``%xx`` escapes are kept as they are. Returns None for empty or
    non-http(s) values.
    """
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url:
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:  # e.g. a malformed IPv6 host
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    return requote_uri(url)


def opinion_number_from_text(text: str | None) -> str | None:
    """The opinion number in an opinion's header text: 'Opinion No. 28355' -> '28355'."""
    if not isinstance(text, str) or not text:
        return None
    match = _OPINION_NO_RE.search(text)
    return match.group(1) if match else None


def search_params(courts: list[Court], filed_after: date) -> dict[str, str]:
    """Query parameters of the v4 Search API request for published opinions of ``courts``.

    ``filed_after`` is an inclusive floor. Raises ValueError for an empty court list
    (an empty ``court`` parameter would search every court in the country).
    """
    court_ids = [COURT_IDS[court] for court in _unique_courts(courts)]
    if not court_ids:
        raise ValueError("search_params needs at least one court")
    return {
        "type": "o",
        "court": " ".join(court_ids),
        "filed_after": f"{filed_after:%Y-%m-%d}",
        "order_by": "dateFiled desc",
        "stat_Published": "on",
    }


def _fmt_date(d: date) -> str:
    return f"{d:%B} {d.day}, {d.year}"


def _unique_courts(courts: Iterable[Court] | None) -> list[Court]:
    """The requested courts in a fixed order (Supreme Court first), without duplicates."""
    wanted = {Court(c) for c in courts or ()}
    return [court for court in COURT_IDS if court in wanted]


def _court_names(courts: list[Court]) -> str:
    return " and ".join(court.display_name for court in courts) or "no"


def _text(value: Any) -> str:
    """A JSON scalar as stripped text ('' for null, booleans, lists, and objects)."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return ""


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _unescape_entities(text: str) -> str:
    return _ENTITY_RE.sub(lambda m: html.unescape(m.group(0)), text)


def _html_to_text(fragment: str) -> str:
    """Plain text of an HTML fragment: tags removed, entities unescaped, spaces collapsed."""
    return _collapse(html.unescape(_TAG_RE.sub(" ", fragment)))


def _parse_date(value: Any) -> date | None:
    """The date part of '2026-11-12' or '2026-11-12T00:00:00-08:00', as written.

    No time-zone conversion: CourtListener stamps opinions at local midnight, and the
    date part is the filing date.
    """
    if not isinstance(value, str):
        return None
    match = _DATE_PREFIX_RE.match(value)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _downloadable(url: str) -> bool:
    """False for hosts this job must never download from (see _NO_DOWNLOAD_HOSTS)."""
    host = (urlsplit(url).hostname or "").lower()
    return not any(host == h or host.endswith("." + h) for h in _NO_DOWNLOAD_HOSTS)


def _storage_url(local_path: str) -> str | None:
    """CourtListener's stored copy of a file, from an opinion's ``local_path``."""
    if not local_path:
        return None
    if _ABSOLUTE_URL_RE.match(local_path):
        return normalize_url(local_path)
    # local_path is a storage key (a file path), so encode it as a path.
    return normalize_url(STORAGE_BASE + quote(local_path.lstrip("/"), safe="/"))


def _site_url(path: str) -> str | None:
    """Absolute CourtListener URL for a path such as '/opinion/11060100/slug/'."""
    if not path:
        return None
    if _ABSOLUTE_URL_RE.match(path):
        return normalize_url(path)
    return normalize_url(SITE_BASE + "/" + path.lstrip("/"))


def _cluster_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    match = _CLUSTER_ID_RE.search(urlsplit(url).path)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# v4 Search API
# ---------------------------------------------------------------------------


class _NotWanted(Exception):
    """A well-formed result this digest does not cover (unpublished, or another court)."""


class _NotPublished(_NotWanted):
    """A result whose precedential status is not "Published"."""


@dataclass
class _SearchPage:
    items: list[Item]
    next_url: str | None
    filtered: int = 0  # unpublished or other courts (expected; not a problem)
    malformed: int = 0  # results that could not be read (logged as warnings)
    read: int = 0  # results on the page
    not_published: int = 0  # the part of ``filtered`` dropped for their status
    statuses: set[str] = field(default_factory=set)  # every status value seen


def parse_search_results(payload: dict) -> tuple[list[Item], str | None]:
    """Turn one page of v4 Search API results into Items. Returns ``(items, next_url)``.

    Keeps only published opinions of the two South Carolina courts, one Item per result
    (opinion cluster). A malformed result is skipped with a warning and never stops the
    run. Raises LayoutChangedError if the payload is not a search response at all.
    """
    page = _parse_search_page(payload)
    return page.items, page.next_url


def _parse_search_page(payload: Any) -> _SearchPage:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise LayoutChangedError(
            "CourtListener's search API returned an unexpected response (no list of results). "
            "The API may have changed; see https://www.courtlistener.com/help/api/rest/search/."
        )
    page = _SearchPage(items=[], next_url=None, read=len(payload["results"]))
    for index, result in enumerate(payload["results"], start=1):
        if isinstance(result, dict) and _text(result.get("status")):
            page.statuses.add(_text(result.get("status")))
        try:
            item = _item_from_result(result)
        except _NotWanted as reason:
            page.filtered += 1
            if isinstance(reason, _NotPublished):
                page.not_published += 1
            log.debug("Ignoring CourtListener search result %d (%s): %s", index, _describe(result), reason)
            continue
        except Exception as exc:  # one bad result must never stop the run
            page.malformed += 1
            log.warning(
                "Skipped CourtListener search result %d (%s) because it could not be read: %s",
                index,
                _describe(result),
                exc,
            )
            continue
        if item.pdf_url is None:
            log.warning(
                "CourtListener has no stored PDF for %s yet, and this job never downloads from "
                "the court's website, so it cannot be summarized this run (it is still listed).",
                item.label,
            )
        page.items.append(item)
    next_url = payload.get("next")
    page.next_url = next_url.strip() if isinstance(next_url, str) and next_url.strip() else None
    return page


def _describe(result: Any) -> str:
    """Short label of a raw search result for log lines. Never raises."""
    if not isinstance(result, dict):
        return f"not an object: {type(result).__name__}"
    name = _collapse(_text(result.get("caseName"))) or "no case name"
    cluster = _text(result.get("cluster_id"))
    return f"{name}, cluster {cluster}" if cluster else name


def _item_from_result(result: Any) -> Item:
    """One search result (an opinion cluster) -> Item. Raises _NotWanted or ValueError."""
    if not isinstance(result, dict):
        raise ValueError(f"expected an object, got {type(result).__name__}")
    # The request asks for published opinions only, so a missing status counts as published.
    status = _text(result.get("status"))
    if status and status.lower() != "published":
        raise _NotPublished(f"status is {status!r}")
    court = _court_of(result)
    case_name = _collapse(_text(result.get("caseName")) or _text(result.get("caseNameFull")))
    if not case_name:
        raise ValueError("it has no caseName")
    filed = _parse_date(result.get("dateFiled"))
    if filed is None:
        raise ValueError(f"dateFiled {result.get('dateFiled')!r} is not a date")

    raw_opinions = result.get("opinions")
    opinions = [op for op in raw_opinions if isinstance(op, dict)] if isinstance(raw_opinions, list) else []
    # The opinion CourtListener stored a PDF for, else the first one.
    chosen = next((op for op in opinions if _text(op.get("local_path"))), opinions[0] if opinions else {})
    ordered = [chosen, *(op for op in opinions if op is not chosen)]

    number = _number_from_result(result, ordered)
    absolute_url = _text(result.get("absolute_url"))
    opinion_page = _site_url(absolute_url)
    cluster_id = _text(result.get("cluster_id"))
    if not _ID_RE.fullmatch(cluster_id):
        cluster_id = _cluster_id_from_url(opinion_page) or ""

    storage_pdf = _storage_url(_text(chosen.get("local_path")))
    download_url = next(
        (url for url in (normalize_url(_text(op.get("download_url"))) for op in ordered) if url), None
    )
    if storage_pdf:
        pdf_url = storage_pdf
    elif download_url and _downloadable(download_url):
        pdf_url = download_url
    else:
        pdf_url = None

    # Readers get the court's own PDF when known: CourtListener's stored copy can be an
    # earlier version of the file. CourtListener's copy then becomes the mirror link.
    if download_url and download_url.lower().startswith("https://"):
        link_url = download_url
    else:
        link_url = pdf_url or opinion_page
    if not link_url:
        raise ValueError("it has no PDF or opinion link")
    mirror_url = storage_pdf if storage_pdf and storage_pdf != link_url else opinion_page
    if mirror_url == link_url:
        mirror_url = None

    return Item(
        kind=ItemKind.OPINION,
        court=court,
        case_name=case_name,
        filed_date=filed,
        number=number,
        pdf_url=pdf_url,
        link_url=link_url,
        mirror_url=mirror_url,
        source=SOURCE_API,
        source_id=cluster_id or None,
    )


def _court_of(result: dict) -> Court:
    court_id = _text(result.get("court_id")).lower()
    if court_id:
        court = _COURT_BY_ID.get(court_id)
        if court is None:
            raise _NotWanted(f"court_id {court_id!r} is not a South Carolina appellate court")
        return court
    # No court_id: fall back to the court's name, e.g. "Court of Appeals of South Carolina".
    name = _text(result.get("court")).lower()
    if "south carolina" in name:
        if "appeals" in name:
            return Court.APPEALS
        if "supreme" in name:
            return Court.SUPREME
    raise ValueError("it has no court_id")


def _number_from_result(result: dict, opinions: list[dict]) -> str | None:
    """Opinion number: a 3-6 digit docketNumber, else 'Opinion No. N' in an opinion snippet."""
    docket = _text(result.get("docketNumber"))
    if _PLAIN_NUMBER_RE.fullmatch(docket):
        return docket
    for opinion in opinions:
        number = opinion_number_from_text(_html_to_text(_text(opinion.get("snippet"))))
        if number:
            return number
    return None


# ---------------------------------------------------------------------------
# Public Atom feeds
# ---------------------------------------------------------------------------


@dataclass
class _FeedPage:
    items: list[Item]  # published entries only
    entry_dates: list[date] = field(default_factory=list)  # every entry, published or not
    malformed: int = 0
    entries: int = 0  # every <entry>
    uncategorized: int = 0  # entries marked neither Published nor Unpublished
    categories: set[str] = field(default_factory=set)  # every category term seen


def parse_feed(xml_text: str | bytes, court: Court) -> list[Item]:
    """Published opinions listed in a CourtListener court feed (Atom XML).

    Accepts the document as text or as raw bytes (then the XML declaration's encoding
    applies). Malformed entries are skipped with a warning. Raises LayoutChangedError if
    the document is not an Atom feed.
    """
    return _read_feed(xml_text, Court(court)).items


def _read_feed(xml_text: str | bytes, court: Court) -> _FeedPage:
    root = _parse_atom(xml_text, court)
    page = _FeedPage(items=[])
    for index, entry in enumerate(root.findall("atom:entry", _NS), start=1):
        page.entries += 1
        filed = _parse_date(entry.findtext("atom:published", default="", namespaces=_NS))
        if filed is None:  # <updated> is required in Atom; <published> is optional
            filed = _parse_date(entry.findtext("atom:updated", default="", namespaces=_NS))
        if filed is not None:
            page.entry_dates.append(filed)
        raw_terms = [(cat.get("term") or "").strip() for cat in entry.findall("atom:category", _NS)]
        page.categories.update(term for term in raw_terms if term)
        terms = {term.lower() for term in raw_terms}
        if not terms & {"published", "unpublished"}:
            page.uncategorized += 1
        if "published" not in terms:
            continue
        try:
            page.items.append(_item_from_entry(entry, court, filed))
        except Exception as exc:  # one bad entry must never stop the run
            page.malformed += 1
            title = _collapse(entry.findtext("atom:title", default="", namespaces=_NS) or "") or "no title"
            log.warning(
                "Skipped entry %d (%s) of CourtListener's %s feed because it could not be read: %s",
                index,
                title,
                court.display_name,
                exc,
            )
    return page


def _parse_atom(xml_text: str | bytes, court: Court) -> ET.Element:
    what = f"CourtListener's {court.display_name} feed"
    if isinstance(xml_text, bytes):
        xml_text = xml_text.removeprefix(b"\xef\xbb\xbf").lstrip()
    elif isinstance(xml_text, str):
        xml_text = xml_text.lstrip("\ufeff \t\r\n")
    else:
        raise LayoutChangedError(f"{what} could not be read (got {type(xml_text).__name__}, not XML).")
    if not xml_text:
        raise LayoutChangedError(f"{what} was empty.")
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, ValueError) as exc:
        raise LayoutChangedError(f"{what} is not valid XML ({exc}). CourtListener may have changed it.") from exc
    if root.tag != f"{{{ATOM_NS}}}feed":
        local_name = root.tag.rsplit("}", 1)[-1]
        raise LayoutChangedError(
            f"{what} is not an Atom feed (its top element is <{local_name}>). CourtListener may have changed it."
        )
    return root


def _item_from_entry(entry: ET.Element, court: Court, filed: date | None) -> Item:
    """One published feed entry -> Item. Raises ValueError if it lacks what an Item needs."""
    if filed is None:
        raise ValueError("it has no valid <published> date")
    title = _collapse(_unescape_entities(entry.findtext("atom:title", default="", namespaces=_NS) or ""))
    if not title:
        raise ValueError("it has no title")
    page, enclosure = _entry_links(entry)
    pdf_url = enclosure if enclosure and _downloadable(enclosure) else None
    # The court's own PDF URL is unknown in feed mode, so readers get CourtListener's copy.
    link_url = enclosure or page
    if not link_url:
        raise ValueError("it has no PDF or opinion link")
    summary = entry.findtext("atom:summary", default="", namespaces=_NS) or ""
    return Item(
        kind=ItemKind.OPINION,
        court=court,
        case_name=title,
        filed_date=filed,
        number=opinion_number_from_text(_html_to_text(summary)),
        pdf_url=pdf_url,
        link_url=link_url,
        mirror_url=page if page and page != link_url else None,
        source=SOURCE_FEED,
        source_id=_cluster_id_from_url(page),
    )


def _entry_links(entry: ET.Element) -> tuple[str | None, str | None]:
    """(CourtListener opinion page, PDF enclosure) of a feed entry; None when missing."""
    page: str | None = None
    enclosure: str | None = None
    for link in entry.findall("atom:link", _NS):
        rel = (link.get("rel") or "alternate").strip().lower()  # Atom's default rel is "alternate"
        href = (link.get("href") or "").strip()
        if not href:
            continue
        if rel == "alternate" and page is None:
            page = _site_url(href)
        elif rel == "enclosure" and enclosure is None:
            enclosure = normalize_url(urljoin(SITE_BASE + "/", href))
    if page is None:  # CourtListener's <id> is the same opinion-page URL
        entry_id = (entry.findtext("atom:id", default="", namespaces=_NS) or "").strip()
        if _cluster_id_from_url(entry_id):
            page = _site_url(entry_id)
    return page, enclosure


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


def fetch(session: PoliteSession, courts: list[Court], filed_after: date, token: str | None) -> ScrapeResult:
    """Published opinions of ``courts`` filed on or after ``filed_after``.

    With ``token``: CourtListener's v4 Search API (complete results). Without one: the
    public Atom feeds (20 newest decisions per court; ``warnings`` explains the limits).
    Items are deduplicated by key and sorted Supreme Court first, then by filing date and
    opinion number. Every warning in the result is also logged here. Raises SourceError
    (or its subclass LayoutChangedError) when CourtListener cannot be read.
    """
    wanted = _unique_courts(courts)
    token = (token or "").strip()
    if token[:6].lower() == "token ":  # tolerate a secret saved with the header's "Token " prefix
        token = token[6:].strip()
    token = token or None
    if token:
        source = SOURCE_API
        found, warnings = _fetch_api(session, wanted, filed_after, token) if wanted else ([], [])
    else:
        source = SOURCE_FEED
        found, warnings = _fetch_feeds(session, wanted, filed_after)

    found = [item for item in found if item.court in wanted]  # even if the API ignored the court filter
    in_window = [item for item in found if item.filed_date >= filed_after]
    if len(in_window) < len(found):
        log.info(
            "Left out %d CourtListener opinion(s) filed before %s.", len(found) - len(in_window), _fmt_date(filed_after)
        )
    items = sorted(_dedupe(in_window), key=_sort_key)
    per_court = ", ".join(
        f"{sum(1 for item in items if item.court is court)} {court.display_name}" for court in wanted
    )
    log.info(
        "CourtListener (%s): %d published opinion(s) filed on or after %s%s.",
        "API search" if source == SOURCE_API else "public feeds",
        len(items),
        _fmt_date(filed_after),
        f" ({per_court})" if per_court else "",
    )
    return ScrapeResult(items=items, source=source, warnings=warnings)


def _fetch_api(
    session: PoliteSession, courts: list[Court], filed_after: date, token: str
) -> tuple[list[Item], list[str]]:
    # The token goes only in this header, and only to CourtListener's API host.
    headers = {"Authorization": f"Token {token}", "Accept": "application/json"}
    url = API_SEARCH_URL
    params: dict[str, str] | None = search_params(courts, filed_after)
    items: list[Item] = []
    warnings: list[str] = []
    malformed = 0
    read = not_published = other = 0
    statuses: set[str] = set()
    log.info(
        "Searching CourtListener for published %s opinions filed on or after %s.",
        _court_names(courts),
        _fmt_date(filed_after),
    )
    for page_number in range(1, MAX_PAGES + 1):
        payload = _get_search_page(session, url, params, headers)
        page = _parse_search_page(payload)
        total = payload.get("count")
        log.info(
            "CourtListener search page %d: %d published opinion(s)%s%s.",
            page_number,
            len(page.items),
            f" ({page.filtered} other result(s) ignored)" if page.filtered else "",
            f"; {total} result(s) in all" if page_number == 1 and isinstance(total, int) else "",
        )
        items.extend(page.items)
        malformed += page.malformed
        read += page.read
        not_published += page.not_published
        other += page.filtered - page.not_published
        statuses |= page.statuses
        if not page.next_url:
            break
        next_url = urljoin(API_SEARCH_URL, page.next_url)
        if not _is_api_url(next_url):
            warnings.append(
                _warn(
                    "CourtListener's link to the next page of search results points somewhere other than "
                    f"{urlsplit(API_SEARCH_URL).hostname} ({urlsplit(next_url).hostname or 'no host'}), so it "
                    "was not followed and later results were not read."
                )
            )
            break
        url, params = next_url, None
        if session.min_interval > 0:
            # CourtListener's free API allows 5 requests a minute (checked 2026-11-16); space
            # follow-up pages out. (Tests use min_interval=0 and skip the pause.)
            time.sleep(max(0.0, API_PAGE_INTERVAL_SECONDS - session.min_interval))
    else:
        warnings.append(
            _warn(
                f"CourtListener had more than {MAX_PAGES} pages of search results; only the first {MAX_PAGES} "
                "were read, so some older opinions in the lookback window may be missing. Lower LOOKBACK_DAYS "
                "or run the workflow more often."
            )
        )
    if read and not items:
        # The request asks for published South Carolina opinions only, so results that all fail
        # to qualify mean the API changed (a renamed status or field), not a quiet week.
        reasons = []
        if not_published:
            seen = ", ".join(repr(s) for s in sorted(statuses)) or "none"
            reasons.append(f"{not_published} not marked Published (statuses seen: {seen})")
        if other:
            reasons.append(f"{other} from other courts")
        if malformed:
            reasons.append(f"{malformed} unreadable")
        raise LayoutChangedError(
            f"CourtListener's search for published opinions returned {read} result(s), but none could be read as "
            f"a published South Carolina opinion ({'; '.join(reasons)}). CourtListener may have changed its API; "
            "see 'If a source changes its format' in the README."
        )
    if malformed:
        warnings.append(
            _warn(
                f"{malformed} CourtListener search result(s) could not be read and were skipped "
                "(details are in the log above)."
            )
        )
    return items, warnings


def _error_detail(body: bytes | None, secret: str = "") -> str | None:
    """The 'detail' of a CourtListener error response, shortened (and never containing the token)."""
    if not body:
        return None
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    detail = data.get("detail") if isinstance(data, dict) else None
    if detail is None:
        return None
    text = _collapse(detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False))
    if secret:
        text = text.replace(secret, "[redacted]")
    return text if len(text) <= 300 else text[:297] + "..."


def _get_search_page(
    session: PoliteSession, url: str, params: dict[str, str] | None, headers: dict[str, str]
) -> Any:
    try:
        return session.get_json(url, params=params, headers=headers)
    except FetchError as exc:
        if exc.status in (401, 403):
            raise SourceError(
                f"CourtListener rejected the API token (HTTP {exc.status}). "
                "Check the COURTLISTENER_API_TOKEN secret."
            ) from exc
        if exc.status == 429:
            raise SourceError(
                "CourtListener is rate-limiting this API token (HTTP 429). Try the run again later."
            ) from exc
        token = headers.get("Authorization", "").removeprefix("Token ").strip()
        detail = _error_detail(getattr(exc, "body", None), token)
        if exc.status is not None and 400 <= exc.status < 500:
            said = f": {detail}" if detail else ""
            if params is not None:  # the first request, built from search_params()
                raise SourceError(
                    f"CourtListener's API refused the search (HTTP {exc.status}{said}). Its API may have changed; "
                    "see 'If a source changes its format' in the README."
                ) from exc
            raise SourceError(  # a "next page" link: its cursor may simply have expired
                f"CourtListener refused the link to the next page of search results (HTTP {exc.status}{said}). "
                "Run the workflow again; if it keeps failing, CourtListener may have changed its API."
            ) from exc
        said = f" ({detail})" if detail else ""
        raise SourceError(
            f"Could not search CourtListener's API: {exc}{said}. CourtListener may be temporarily unavailable; "
            "try the run again later."
        ) from exc


def _is_api_url(url: str) -> bool:
    """True only for https URLs on CourtListener's API host (the token is sent there)."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    return parts.scheme.lower() == "https" and host == urlsplit(API_SEARCH_URL).hostname


def _fetch_feeds(session: PoliteSession, courts: list[Court], filed_after: date) -> tuple[list[Item], list[str]]:
    warnings = [_warn(NO_TOKEN_WARNING)]
    items: list[Item] = []
    for court in courts:
        url = FEED_URL.format(court_id=COURT_IDS[court])
        log.info("Reading CourtListener's public %s feed.", court.display_name)
        try:
            body = session.get_bytes(url, headers={"Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.5"})
        except FetchError as exc:
            raise SourceError(f"Could not read CourtListener's public {court.display_name} feed: {exc}") from exc
        page = _read_feed(body, court)
        if page.entries and page.uncategorized == page.entries:
            seen = ", ".join(repr(c) for c in sorted(page.categories)) or "none"
            raise LayoutChangedError(
                f"CourtListener's {court.display_name} feed lists {page.entries} decision(s), but none is marked "
                f"Published or Unpublished (categories seen: {seen}). CourtListener may have changed it; see "
                "'If a source changes its format' in the README."
            )
        if page.uncategorized:
            warnings.append(
                _warn(
                    f"{page.uncategorized} of the {page.entries} entries in CourtListener's {court.display_name} "
                    "feed are marked neither Published nor Unpublished, so they were skipped. CourtListener may "
                    "have changed its feed."
                )
            )
        items.extend(page.items)
        if not page.entry_dates:
            warnings.append(
                _warn(
                    f"CourtListener's public {court.display_name} feed listed no decisions, so no "
                    f"{court.display_name} opinions could be found this run."
                )
            )
        else:
            oldest, newest = min(page.entry_dates), max(page.entry_dates)
            log.info(
                "%s feed: %d decision(s), %d published, filed %s to %s.",
                court.display_name,
                len(page.entry_dates),
                len(page.items),
                _fmt_date(oldest),
                _fmt_date(newest),
            )
            if oldest > filed_after:
                warnings.append(
                    _warn(
                        f"CourtListener's public {court.display_name} feed reaches back only to "
                        f"{_fmt_date(oldest)} (its {len(page.entry_dates)} newest decisions), but this run covers "
                        f"opinions filed on or after {_fmt_date(filed_after)}, so some {court.display_name} "
                        "opinions may be missing. Add a free CourtListener API token for complete results."
                    )
                )
        if page.malformed:
            warnings.append(
                _warn(
                    f"{page.malformed} entry(ies) of CourtListener's {court.display_name} feed could not be read "
                    "and were skipped (details are in the log above)."
                )
            )
    return items, warnings


def _warn(message: str) -> str:
    """Log a run-level warning and return it for ScrapeResult.warnings."""
    log.warning("%s", message)
    return message


def _dedupe(items: Iterable[Item]) -> list[Item]:
    unique: dict[str, Item] = {}
    for item in items:
        key = item.key
        if key in unique:
            log.debug("Ignoring a duplicate CourtListener result for %s.", item.label)
            continue
        unique[key] = item
    return list(unique.values())


def _sort_key(item: Item) -> tuple:
    """Supreme Court first, then filing date, then opinion number (unnumbered last), then name."""
    number = item.number or ""
    if _ID_RE.fullmatch(number):
        number_key: tuple[int, int, str] = (0, int(number), number)
    elif number:
        number_key = (1, 0, number)
    else:
        number_key = (2, 0, "")
    return (0 if item.court is Court.SUPREME else 1, item.filed_date, number_key, item.case_name.lower())
