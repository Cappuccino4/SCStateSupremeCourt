"""South Carolina Judicial Branch website (www.sccourts.org), read directly.

OFF BY DEFAULT: sccourts.org's robots.txt disallows automated agents, so the weekly job
uses CourtListener unless ``SOURCE=sccourts``. When enabled, this source reads:

- each court's monthly "published opinions" page (one request per court and month);
- the advance sheets page, and the PDF of every advance sheet in the window. The
  sheet's CONTENTS gives the page range of each published opinion and order, which is
  how Supreme Court orders and rule amendments are found. Some orders are printed on
  the pages BEFORE the cover and are not listed in CONTENTS; those are found too;
- the All Court Orders page, which gives an order its official number
  (``2026-11-12-01``) and its own PDF.

Every parser is tested against saved copies of the real pages (tests/fixtures/sccourts/).
"""

from __future__ import annotations

import html as html_lib
import logging
import re
import warnings
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning
from bs4.element import Tag
from requests.utils import requote_uri

from ..extract import clean_text
from ..models import AdvanceSheet, Court, Item, ItemKind, ScrapeResult, slugify
from ..net import FetchError, PoliteSession
from . import LayoutChangedError, SourceError

log = logging.getLogger(__name__)

SOURCE = "sccourts"
BASE_URL = "https://www.sccourts.org"
LISTING_PATHS: dict[Court, str] = {
    Court.SUPREME: "/opinions-orders/opinions/published-opinions/supreme-court/",
    Court.APPEALS: "/opinions-orders/opinions/published-opinions/court-of-appeals/",
}
ADVANCE_SHEETS_URL = BASE_URL + "/opinions-orders/advance-sheets/"
ALL_ORDERS_URL = BASE_URL + "/opinions-orders/court-orders/all-court-orders/"

ORDER_MATCH_WINDOW_DAYS = 30  # an advance-sheet order only matches a listing dated this close
ORDER_MATCH_MIN_SIMILARITY = 0.8  # token-set Jaccard threshold for order titles
MAX_CONTINUATION_LINES = 2  # a CONTENTS entry may wrap onto at most this many more lines
MAX_CONTENTS_PAGES = 30  # never scan more pages than this looking for the CONTENTS sections

_COURT_ORDER = (Court.SUPREME, Court.APPEALS)


# --------------------------------------------------------------------------- helpers


_MONTHS: dict[str, int] = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip
_DATE_RE = re.compile(
    r"\b(?P<month>January|February|March|April|May|June|July|August|September|October|November|December"
    r"|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)\.?\s+(?P<day>\d{1,2})(?:,\s*|\s+)(?P<year>\d{4})\b",
    re.IGNORECASE,
)
_ENTITY_RE = re.compile(r"&(?:#\d+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);")


def _dates_in(text: str | None) -> list[date]:
    """Every valid 'Month D, YYYY' date in the text, in order (month names are English)."""
    found: list[date] = []
    for m in _DATE_RE.finditer(text or ""):
        month = m.group("month")
        if month[0].islower():  # "may 5, 2026" is a verb, not a date
            continue
        try:
            found.append(date(int(m.group("year")), _MONTHS[month.lower()], int(m.group("day"))))
        except (KeyError, ValueError):
            continue
    return found


def _parse_date(text: str | None) -> date | None:
    dates = _dates_in(text)
    return dates[0] if dates else None


def _month_label(year: int, month: int) -> str:
    return f"{date(year, month, 1):%B} {year}"


def _clean(text: str | None) -> str:
    """Collapse whitespace and unescape entities left over from double escaping."""
    if not text:
        return ""
    if "&" in text and _ENTITY_RE.search(text):
        text = html_lib.unescape(text)
    return " ".join(text.split())


def _text_of(tag: Tag | None, sep: str = "") -> str:
    return _clean(tag.get_text(sep)) if tag is not None else ""


def _soup(markup: str | None) -> BeautifulSoup:
    with warnings.catch_warnings():
        # Short or odd input makes bs4 warn that it "looks like a filename"; it is just bad HTML.
        warnings.simplefilter("ignore", MarkupResemblesLocatorWarning)
        return BeautifulSoup(markup or "", "lxml")


def _absolute_url(href: str | None, base: str | None) -> str | None:
    """Absolute, percent-encoded http(s) URL for an href, else None.

    A space inside the path is kept and encoded as %20 (sccourts.org serves
    ``COA/6170 .pdf`` only under that name; the trimmed ``6170.pdf`` is a 404).
    Leading/trailing whitespace and embedded newlines are dropped, as browsers do.
    """
    if not href:
        return None
    href = re.sub(r"[\t\r\n]", "", str(href)).strip()
    if not href:
        return None
    try:
        url = urljoin(base or BASE_URL + "/", href)
        if urlsplit(url).scheme.lower() not in ("http", "https"):
            return None
    except ValueError:
        return None
    return requote_uri(url)


def _is_pdf_href(href: str | None) -> bool:
    if not href:
        return False
    try:
        path = urlsplit(str(href).strip()).path
    except ValueError:
        return False
    return path.lower().endswith(".pdf")


def _first_pdf_link(el: Tag | None) -> Tag | None:
    if el is None:
        return None
    for a in el.select("a[href]"):
        if _is_pdf_href(a.get("href")):
            return a
    return None


def _reason(exc: BaseException) -> str:
    """An exception as one log-friendly clause (no newlines, no trailing period)."""
    return " ".join(str(exc).split()).rstrip(".") or exc.__class__.__name__


def _slice_pages(pages: list[str], start: int, end: int) -> str:
    """Pages start..end (1-based, inclusive, clamped to the document) joined with newlines.

    The pages keep their number-only lines (the CONTENTS parser needs them); the text
    of an order or opinion drops them, exactly as extract.clean_text does for any PDF.
    """
    start, end = max(1, start), min(len(pages), end)
    if start > end:
        return ""
    return "\n".join(clean_text(p or "") for p in pages[start - 1 : end])


# --------------------------------------------------------------------------- opinion listings


def listing_url(court: Court, year: int, month: int) -> str:
    """Monthly published-opinions page, e.g. ``.../supreme-court/?term=2026-11``."""
    return f"{BASE_URL}{LISTING_PATHS[court]}?term={year:04d}-{month:02d}"


def months_between(start: date, end: date) -> list[tuple[int, int]]:
    """Every (year, month) from start's month through end's month, inclusive ([] if start > end)."""
    if start > end:
        return []
    months: list[tuple[int, int]] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def parse_opinion_listing(html: str, court: Court, page_url: str) -> list[Item]:
    """Opinions on one monthly published-opinions page, in page order.

    The page groups ``div.accordion-item.case-result`` items under date headings
    (``div.result-heading h3``: "November 12, 2026"). Every element id on the page is the
    same, so the parser relies only on classes and document order. Raises
    LayoutChangedError if the results block is missing; an empty month returns [].
    """
    soup = _soup(html)
    blocks = [b for section in soup.select("section.results-listing") for b in section.select("div.accordion-block")]
    if not blocks:
        raise LayoutChangedError(
            f"The {court.display_name} published-opinions page ({page_url}) has no results block "
            "(section.results-listing > div.accordion-block). sccourts.org may have been redesigned; "
            "src/sources/sccourts.py needs updating. Until then, use SOURCE=courtlistener."
        )
    items: list[Item] = []
    results_seen = 0
    for block in blocks:
        current: date | None = None
        for el in block.select(".result-heading, .case-result"):
            if "result-heading" in (el.get("class") or []):
                heading = _text_of(el.find(["h2", "h3", "h4"]) or el, " ")
                current = _parse_date(heading)
                if current is None:
                    log.warning(
                        "Unrecognized date heading %r on %s; the opinions under it are skipped.", heading, page_url
                    )
                continue
            results_seen += 1
            item = _listing_item(el, court, page_url, current)
            if item is not None:
                items.append(item)
    if not results_seen and not any("no published opinions" in b.get_text(" ").lower() for b in blocks):
        log.warning(
            "%s found no opinions and no 'no published opinions' notice; the page layout may have changed.", page_url
        )
    return items


def _listing_item(el: Tag, court: Court, page_url: str, filed: date | None) -> Item | None:
    link = el.select_one("a.download-link[href]") or _first_pdf_link(el)
    pdf_url = _absolute_url(link.get("href"), page_url) if link is not None else None
    number = _text_of(el.select_one(".case-number")) or (_number_from_link(link) if link is not None else None)
    case_name = _text_of(el.select_one(".case-name"))
    label = f"{court.value} {number or '(no number)'} {case_name or '(no case name)'}"
    if filed is None:
        log.warning("Skipping %s on %s: it is not under a date heading.", label, page_url)
        return None
    if not pdf_url:
        log.warning("Skipping %s on %s: it has no PDF link.", label, page_url)
        return None
    if not case_name:
        case_name = f"Opinion No. {number}" if number else "Untitled opinion"
        log.warning("%s on %s has no case name; using %r.", label, page_url, case_name)
    return Item(
        kind=ItemKind.OPINION,
        court=court,
        case_name=case_name,
        filed_date=filed,
        number=number,
        pdf_url=pdf_url,
        link_url=pdf_url,
        source=SOURCE,
        court_summary=_text_of(el.select_one(".result-info"), " "),
    )


def _number_from_link(link: Tag) -> str | None:
    """Opinion number from 'Download opinion 6170 as PDF' or from the PDF file name."""
    m = re.search(r"\bopinion\s+(\S+)\s+as\s+pdf\b", str(link.get("aria-label") or ""), re.IGNORECASE)
    if m:
        return m.group(1)
    try:
        name = unquote(urlsplit(str(link.get("href") or "").strip()).path.rsplit("/", 1)[-1])
    except ValueError:
        return None
    stem = re.sub(r"\.pdf$", "", name, flags=re.IGNORECASE).strip()
    return stem if re.fullmatch(r"\d{3,6}", stem) else None


# --------------------------------------------------------------------------- advance sheets list


_SHEET_NUMBER_RE = re.compile(r"\bsheet\s*(?:no\.?\s*)?(\d{1,3})\b", re.IGNORECASE)
_SHEET_FILE_RE = re.compile(r"no(\d{1,3})(?:19|20)\d\d\.pdf$", re.IGNORECASE)


def parse_advance_sheet_list(html: str) -> list[AdvanceSheet]:
    """Advance sheets listed on the advance sheets page (number, date, absolute PDF URL), in page order."""
    soup = _soup(html)
    results = soup.select(".advance-sheet-result")
    if not results:
        if soup.select_one("section.results-listing") is None:
            raise LayoutChangedError(
                f"The advance sheets page ({ADVANCE_SHEETS_URL}) has no advance-sheet entries "
                "(div.advance-sheet-result). sccourts.org may have been redesigned."
            )
        log.warning("The advance sheets page (%s) lists no advance sheets.", ADVANCE_SHEETS_URL)
        return []
    sheets: list[AdvanceSheet] = []
    for el in results:
        link = el.select_one("h2 a[href]") or el.select_one("a[href]")
        label = _text_of(link, " ")
        url = _absolute_url(link.get("href"), ADVANCE_SHEETS_URL) if link is not None else None
        number = _sheet_number(label, url)
        when = _parse_date(_text_of(el.select_one(".subtitle"), " ")) or _parse_date(_text_of(el, " "))
        if url is None or number is None or when is None:
            missing = ", ".join(
                name for name, value in (("PDF link", url), ("number", number), ("date", when)) if value is None
            )
            log.warning("Skipping advance sheet entry %r: no %s.", label or _text_of(el, " "), missing)
            continue
        sheets.append(AdvanceSheet(number=number, date=when, pdf_url=url))
    return sheets


def _sheet_number(label: str, url: str | None) -> int | None:
    m = _SHEET_NUMBER_RE.search(label) or re.search(r"\b(\d{1,3})\b", label)
    if m:
        return int(m.group(1))
    m = _SHEET_FILE_RE.search(urlsplit(url).path) if url else None  # .../advSheets/no412026.pdf
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------- advance sheet CONTENTS


@dataclass
class ContentsEntry:
    """One published opinion or order in an advance sheet, with its (1-based) page range."""

    kind: ItemKind
    court: Court
    number: str | None  # "28354", "6170"; None for orders (their number comes from the orders listing)
    title: str  # case name, or "Re: ..." for an order
    start_page: int
    end_page: int
    refiled_date: date | None = None  # "(Withdrawn, Substituted, and Refiled November 12, 2026)"


@dataclass
class AdvanceSheetContents:
    entries: list[ContentsEntry]  # sorted by start page
    cover_page: int | None  # page with "ADVANCE SHEET NO."; None if not found
    total_pages: int


_COVER_RE = re.compile(r"ADVANCE\s+SHEET\s+NO\b")
_DASH = r"[-\u2010-\u2015\u2212\ufffd]"  # hyphen, en/em dashes, minus sign (and a mangled dash)
_ENTRY_RE = re.compile(rf"^(?P<num>\d{{3,6}}|order)\s*{_DASH}\s*(?P<rest>\S.*)$", re.IGNORECASE)
_UNPUBLISHED_NUM_RE = re.compile(rf"^\d{{4}}{_DASH}[A-Z]{{2,4}}{_DASH}\d+", re.IGNORECASE)  # 2026-UP-421
_TRAILING_PAGE_RE = re.compile(r"^(?P<body>.*\S)\s+(?P<page>\d{1,4})$")
_NONE_RE = re.compile(r"^none\.?$", re.IGNORECASE)

_SC_HEADINGS = frozenset({"THE SUPREME COURT OF SOUTH CAROLINA"})
_COA_HEADINGS = frozenset({"THE SOUTH CAROLINA COURT OF APPEALS", "THE COURT OF APPEALS OF SOUTH CAROLINA"})
_PUBLISHED_HEADINGS = frozenset(
    {"PUBLISHED OPINIONS AND ORDERS", "PUBLISHED ORDERS AND OPINIONS", "PUBLISHED OPINIONS"}
)
_UNPUBLISHED_HEADING = "UNPUBLISHED"  # "UNPUBLISHED OPINIONS" ends a published section


@dataclass
class _Pending:
    """A CONTENTS entry whose page number has not been seen yet (its title wraps)."""

    kind: ItemKind
    court: Court
    number: str | None
    parts: list[str]
    continuations: int = 0
    refiled_date: date | None = None


def parse_advance_sheet_contents(pages: list[str]) -> AdvanceSheetContents:
    """Published opinions and orders of an advance sheet, from its per-page texts.

    ``pages[0]`` is page 1; printed page numbers equal PDF page numbers. Reads the
    "PUBLISHED OPINIONS AND ORDERS" (Supreme Court) and "PUBLISHED OPINIONS" (Court of
    Appeals) sections of CONTENTS, and adds orders printed before the cover page. An
    entry ends where the next one starts (the last one at the final page). Never raises:
    odd text yields fewer entries and a warning in the log.
    """
    texts = [p if isinstance(p, str) else "" for p in (pages or [])]
    total = len(texts)
    cover: int | None = None
    entries: list[ContentsEntry] = []
    try:
        cover = _find_cover(texts)
        found = _parse_contents_sections(texts, _contents_start(texts, cover), total)
        _set_end_pages(found, total)
        listed_orders = {_normalize_title(e.title) for e in found if e.kind is ItemKind.ORDER}
        for order in _pre_cover_orders(texts, cover):
            if _normalize_title(order.title) not in listed_orders:
                found.append(order)
        entries = sorted(found, key=lambda e: e.start_page)
    except Exception:  # defensive: an odd advance sheet must never stop the run
        log.warning("Could not parse the advance sheet's CONTENTS; its orders are skipped.", exc_info=True)
    return AdvanceSheetContents(entries=entries, cover_page=cover, total_pages=total)


def _find_cover(texts: list[str]) -> int | None:
    for i, text in enumerate(texts):
        if _COVER_RE.search(text):
            return i + 1
    return None


def _contents_start(texts: list[str], cover: int | None) -> int:
    """0-based index of the first CONTENTS page (falls back to the cover, or page 1)."""
    first = cover - 1 if cover else 0
    for i in range(first, len(texts)):
        head = [" ".join(line.split()).upper() for line in texts[i].splitlines() if line.strip()][:5]
        if "CONTENTS" in head:
            return i
    return first


def _page_lines(text: str, page_no: int) -> list[str]:
    """A page's lines without its printed page number (the last line, when it equals the page number)."""
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip() == str(page_no):
        lines.pop()
    return lines


def _looks_like_heading(line: str) -> bool:
    """An all-capitals section heading such as 'PETITIONS FOR REHEARING'."""
    letters = [c for c in line if c.isalpha()]
    if len(letters) < 4 or any(c.islower() for c in letters) or len(line.split()) < 2:
        return False
    if line.startswith("(") or _ENTRY_RE.match(line) or _UNPUBLISHED_NUM_RE.match(line):
        return False
    return not _TRAILING_PAGE_RE.match(line)


def _clean_title(text: str) -> str:
    title = " ".join(text.split())
    title = re.sub(r"\s*\.{2,}[\s.]*$", "", title)  # dot leaders before the page number
    return title.strip(" -\u2013\u2014")


def _parse_contents_sections(texts: list[str], start_index: int, total: int) -> list[ContentsEntry]:
    """Entries of the published sections of CONTENTS (end pages not set yet)."""
    entries: list[ContentsEntry] = []
    court: Court | None = None
    in_published = False
    closed: set[Court] = set()
    pending: _Pending | None = None
    last: ContentsEntry | None = None

    def finish(p: _Pending, page: int) -> ContentsEntry:
        entry = ContentsEntry(p.kind, p.court, p.number, _clean_title(" ".join(p.parts)), page, page, p.refiled_date)
        entries.append(entry)
        return entry

    def drop(p: _Pending | None) -> None:
        if p is not None:
            log.warning(
                "Advance sheet CONTENTS: no page number found for %r; it was skipped.", _clean_title(" ".join(p.parts))
            )

    def split_page(text: str, page_no: int) -> tuple[str, int | None]:
        m = _TRAILING_PAGE_RE.match(text)
        if m and page_no < int(m.group("page")) <= total:  # opinions start after the CONTENTS page
            return m.group("body"), int(m.group("page"))
        return text, None

    for page_no in range(start_index + 1, min(total, start_index + MAX_CONTENTS_PAGES) + 1):
        if closed >= set(_COURT_ORDER) or (entries and page_no >= min(e.start_page for e in entries)):
            break  # both sections read, or the first opinion has been reached
        for raw in _page_lines(texts[page_no - 1], page_no):
            line = " ".join(raw.split())
            if not line:
                continue
            upper = line.upper().rstrip(":").strip()

            if upper in _SC_HEADINGS or upper in _COA_HEADINGS:
                heading_court = Court.SUPREME if upper in _SC_HEADINGS else Court.APPEALS
                if heading_court is court and in_published:
                    continue  # the heading repeated on a continuation page
                drop(pending)
                pending, last = None, None
                if in_published and court is not None:
                    closed.add(court)
                court, in_published = heading_court, False
                continue
            if upper in _PUBLISHED_HEADINGS:
                drop(pending)
                pending, last = None, None
                if court is None and "ORDERS" in upper:
                    court = Court.SUPREME
                in_published = court is not None and court not in closed
                continue
            if upper.startswith("CONTENTS"):
                continue
            if upper.startswith(_UNPUBLISHED_HEADING) or (pending is None and _looks_like_heading(line)):
                drop(pending)
                pending, last = None, None
                if in_published and court is not None:
                    closed.add(court)
                in_published = False
                continue
            if not in_published or court is None:
                continue

            if _NONE_RE.match(line) or _UNPUBLISHED_NUM_RE.match(line):
                drop(pending)
                pending, last = None, None
                continue
            m = _ENTRY_RE.match(line)
            if m:
                drop(pending)
                is_order = m.group("num").lower() == "order"
                pending = _Pending(
                    kind=ItemKind.ORDER if is_order else ItemKind.OPINION,
                    court=court,
                    number=None if is_order else m.group("num"),
                    parts=[],
                )
                text = m.group("rest")
            elif line.startswith("("):  # "(Withdrawn, Substituted, and Refiled November 12, 2026)"
                refiled = _parse_date(line) if "refiled" in line.lower() else None
                if pending is not None:
                    pending.refiled_date = refiled or pending.refiled_date
                    _, page = split_page(line, page_no)
                    if page is not None:
                        last, pending = finish(pending, page), None
                    else:
                        pending.continuations += 1
                        if pending.continuations >= MAX_CONTINUATION_LINES:
                            drop(pending)
                            pending = None
                elif last is not None and refiled is not None:
                    last.refiled_date = refiled
                continue
            elif re.fullmatch(r"\d{1,4}", line):  # a page number wrapped onto its own line
                if pending is not None and page_no < int(line) <= total:
                    last, pending = finish(pending, int(line)), None
                continue
            elif pending is not None:
                pending.continuations += 1
                text = line
            else:
                continue  # stray text between entries

            body, page = split_page(text, page_no)
            pending.parts.append(body)
            if page is not None:
                last, pending = finish(pending, page), None
            elif pending.continuations >= MAX_CONTINUATION_LINES:
                drop(pending)
                pending = None
    drop(pending)
    return entries


def _set_end_pages(entries: list[ContentsEntry], total: int) -> None:
    """Each entry ends the page before the next entry starts; the last one at the final page."""
    starts = sorted({e.start_page for e in entries})
    for e in entries:
        later = [s for s in starts if s > e.start_page]
        end = later[0] - 1 if later else total
        e.end_page = max(e.start_page, min(end, total))


def _pre_cover_orders(texts: list[str], cover: int | None) -> list[ContentsEntry]:
    """Orders printed before the cover page (not listed in CONTENTS). The last one ends at cover - 1."""
    if not cover or cover <= 1:
        return []
    starts = [(page_no, title) for page_no in range(1, cover) if (title := _order_heading_title(texts[page_no - 1]))]
    orders: list[ContentsEntry] = []
    for i, (page_no, title) in enumerate(starts):
        end = starts[i + 1][0] - 1 if i + 1 < len(starts) else cover - 1
        orders.append(ContentsEntry(ItemKind.ORDER, Court.SUPREME, None, title, page_no, max(page_no, end)))
    if orders:
        log.info("Found %d order(s) printed before the advance sheet's cover page.", len(orders))
    return orders


def _order_heading_title(text: str) -> str | None:
    """'Re: ...' title when the page starts an order (an 'ORDER' line and a 'Re:' line near the top)."""
    lines = [" ".join(line.split()) for line in text.splitlines()]
    head = [line for line in lines if line][:15]
    if not any(line.upper() == "ORDER" for line in head):
        return None
    for i, line in enumerate(head):
        if re.match(r"re\s*:", line, re.IGNORECASE):
            parts = [line]
            for nxt in head[i + 1 : i + 1 + MAX_CONTINUATION_LINES]:
                if nxt.upper() == "ORDER" or re.match(r"appellate\s+case\s+no", nxt, re.IGNORECASE):
                    break
                parts.append(nxt)
            return _clean(" ".join(parts))
    return None


# --------------------------------------------------------------------------- orders listing


@dataclass
class OrderListing:
    """One order on the All Court Orders page."""

    number: str  # "2026-11-12-01"
    title: str  # "Amendments to Rule 1.15, RPC, Rule 407, SCACR"
    pdf_url: str
    date: date | None  # from the number


_ORDER_NUMBER_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-\d{1,3}$")


def _date_from_order_number(number: str) -> date | None:
    m = _ORDER_NUMBER_RE.match(number)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def parse_orders_listing(html: str) -> list[OrderListing]:
    """Orders on the All Court Orders page (``article[data-ordernumber]``), in page order."""
    soup = _soup(html)
    articles = soup.select("article[data-ordernumber]")
    if not articles and soup.select_one("section.results-listing") is None:
        raise LayoutChangedError(
            f"The All Court Orders page ({ALL_ORDERS_URL}) has no order entries (article[data-ordernumber]). "
            "sccourts.org may have been redesigned."
        )
    listings: list[OrderListing] = []
    for art in articles:
        number = _clean(str(art.get("data-ordernumber") or "")) or _text_of(art.select_one(".case-number"))
        link = _first_pdf_link(art.select_one(".order-link")) or _first_pdf_link(art)  # not a related order's PDF
        pdf_url = _absolute_url(link.get("href"), ALL_ORDERS_URL) if link is not None else None
        if not number or not pdf_url:
            missing = "PDF link" if number else "number"
            log.warning("Skipping order %r on the All Court Orders page: no %s.", number, missing)
            continue
        listings.append(
            OrderListing(
                number=number,
                title=_text_of(art.select_one(".case-name"), " "),
                pdf_url=pdf_url,
                date=_date_from_order_number(number),
            )
        )
    return listings


def _normalize_title(title: str | None) -> str:
    """Lowercase, drop a leading 're:', punctuation -> space, collapse spaces."""
    text = _clean(title).lower()
    text = re.sub(r"^re\s*:", " ", text)
    text = re.sub(r"[^\w\s]|_", " ", text)
    return " ".join(text.split())


def _title_similarity(a: str, b: str) -> float:
    """1.0 for equal normalized titles, at least 0.9 when one contains the other, else token-set Jaccard."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    tokens_a, tokens_b = set(a.split()), set(b.split())
    score = len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
    shorter, longer = sorted((a, b), key=len)
    # Containment counts at word boundaries, and only for titles of two or more words
    # (a one-word title such as "probate" would otherwise match far too much).
    if len(shorter.split()) >= 2 and f" {shorter} " in f" {longer} ":
        score = max(score, 0.9)
    return score


def _numbers(normalized: str) -> set[str]:
    return {token for token in normalized.split() if token.isdigit()}


def match_order(title: str, listings: Iterable[OrderListing], near: date | None) -> OrderListing | None:
    """The listing for an advance-sheet order: similar title, dated within 30 days of ``near``, closest date first.

    Titles match when one contains the other or their token-set Jaccard index is at
    least 0.8, and their numbers agree (one title's numbers are a subset of the other's),
    so "Amendments to Rule 509" never matches a "Rule 510" order.
    """
    target = _normalize_title(title)
    if not target:
        return None
    target_numbers = _numbers(target)
    best: OrderListing | None = None
    best_rank: tuple[int, float, str] | None = None
    for listing in listings:
        if near is not None:
            if listing.date is None:
                continue
            days = abs((listing.date - near).days)
            if days > ORDER_MATCH_WINDOW_DAYS:
                continue
        else:
            days = 0
        candidate = _normalize_title(listing.title)
        numbers = _numbers(candidate)
        if target_numbers and numbers and not (target_numbers <= numbers or numbers <= target_numbers):
            continue
        score = _title_similarity(target, candidate)
        if score < ORDER_MATCH_MIN_SIMILARITY:
            continue
        rank = (days, -score, listing.number)
        if best_rank is None or rank < best_rank:
            best, best_rank = listing, rank
    return best


def order_date_from_text(text: str | None) -> date | None:
    """The LAST 'Month D, YYYY' date in an order's text (the date under the signatures)."""
    dates = _dates_in(text)
    return dates[-1] if dates else None


def build_order_items(
    sheet: AdvanceSheet,
    contents: AdvanceSheetContents,
    pages: list[str],
    listings: list[OrderListing],
) -> list[Item]:
    """One ORDER item per order in the advance sheet, with its text sliced from the sheet.

    When the order is on the All Court Orders page, the item gets that number and PDF
    (and the advance-sheet page as a mirror); otherwise it links to its page in the sheet.
    Either way it also carries its date-and-title key in ``extra_keys``, so a run that
    matched it and a run that could not (the orders page failed or lagged) agree that it
    is the same order.
    """
    items: list[Item] = []
    for entry in contents.entries:
        if entry.kind is not ItemKind.ORDER:
            continue
        text = _slice_pages(pages, entry.start_page, entry.end_page)
        if not text.strip():
            log.warning(
                "Skipping order %r from Advance Sheet No. %d: pages %d-%d have no text.",
                entry.title, sheet.number, entry.start_page, entry.end_page,
            )  # fmt: skip
            continue
        signed = order_date_from_text(text)
        listing = match_order(entry.title, listings, signed or sheet.date)
        page_link = f"{sheet.pdf_url}#page={entry.start_page}"
        if listing is not None:
            log.info("Order %r matched All Court Orders entry %s.", entry.title, listing.number)
        else:
            log.info(
                "Order %r is not on the All Court Orders page; linking to page %d of Advance Sheet No. %d.",
                entry.title, entry.start_page, sheet.number,
            )  # fmt: skip
        items.append(
            Item(
                kind=ItemKind.ORDER,
                court=Court.SUPREME,
                case_name=entry.title,
                filed_date=(listing.date if listing is not None else None) or signed or sheet.date,
                number=listing.number if listing is not None else None,
                pdf_url=None,
                link_url=listing.pdf_url if listing is not None else page_link,
                mirror_url=page_link if listing is not None else None,
                source=SOURCE,
                advance_sheet=sheet.number,
                text=text,
                extra_keys=(f"ORDER:{(signed or sheet.date).isoformat()}:{slugify(entry.title)}",),
            )
        )
    return items


# --------------------------------------------------------------------------- fetch


def fetch(
    session: PoliteSession,
    courts: list[Court],
    filed_after: date,
    include_orders: bool,
    pdf_to_pages: Callable[[bytes], list[str]],
    today: date,
) -> ScrapeResult:
    """Published opinions filed on or after ``filed_after`` (plus, optionally, the advance sheets' orders).

    Reads each requested court's listing page for every month in [filed_after, today];
    a failure there raises SourceError. With ``include_orders``, also reads every advance
    sheet dated in [filed_after, today]: its orders become items (whatever ``courts`` is),
    listing opinions that appear in it get ``advance_sheet`` set, and an opinion in it that
    is missing from the listings becomes an item built from the sheet (with a warning).
    Failures on the advance-sheet side become warnings; the opinions are still returned.
    """
    requested = set(courts)
    wanted = [c for c in _COURT_ORDER if c in requested]
    notes: list[str] = []
    log.info(
        "Reading www.sccourts.org directly (SOURCE=sccourts). Its robots.txt asks automated agents not to "
        "crawl the site, so keep requests few; the default source is CourtListener."
    )

    listed: list[Item] = []
    for court in wanted:
        for year, month in months_between(filed_after, today):
            url = listing_url(court, year, month)
            try:
                page = session.get_text(url)
            except FetchError as exc:
                hint = (
                    " sccourts.org may be blocking automated requests; SOURCE=courtlistener (the default) avoids it."
                    if exc.status in (401, 403, 429)
                    else " The site may be down; try again later, or use SOURCE=courtlistener (the default)."
                )
                raise SourceError(
                    f"Could not read the {court.display_name} published-opinions page for "
                    f"{_month_label(year, month)}: {_reason(exc)}.{hint}"
                ) from exc
            found = parse_opinion_listing(page, court, url)
            month_label = _month_label(year, month)
            log.info("%s, %s: %d published opinion(s) listed.", court.display_name, month_label, len(found))
            listed.extend(found)

    items = [item for item in listed if item.filed_date >= filed_after]
    sheets: list[AdvanceSheet] = []
    checked: list[AdvanceSheet] = []
    if include_orders:
        sheets, checked, extra = _advance_sheet_items(
            session, wanted, listed, filed_after, today, pdf_to_pages, notes
        )
        items.extend(extra)

    result = ScrapeResult(
        items=_dedupe_and_sort(items),
        source=SOURCE,
        advance_sheets=sheets,
        warnings=notes,
        checked_advance_sheets=checked,
    )
    log.info(
        "sccourts.org: %d item(s) filed on or after %s (%s).",
        len(result.items),
        filed_after.isoformat(),
        ", ".join(f"{n} {label}" for label, n in _counts(result.items)) or "none",
    )
    return result


def _advance_sheet_items(
    session: PoliteSession,
    wanted: list[Court],
    listed: list[Item],
    filed_after: date,
    today: date,
    pdf_to_pages: Callable[[bytes], list[str]],
    notes: list[str],
) -> tuple[list[AdvanceSheet], list[AdvanceSheet], list[Item]]:
    """(advance sheets in the window, the ones read and parsed, extra items: orders and sheet-only opinions)."""
    try:
        all_sheets = parse_advance_sheet_list(session.get_text(ADVANCE_SHEETS_URL))
    except (FetchError, SourceError) as exc:
        notes.append(
            f"Could not read the sccourts.org advance sheets page: {_reason(exc)}. "
            "Supreme Court orders and rule amendments were not checked this run."
        )
        return [], [], []
    by_number = {s.number: s for s in all_sheets if filed_after <= s.date <= today}
    sheets = sorted(by_number.values(), key=lambda s: (s.date, s.number))
    if not sheets:
        log.info("No advance sheet is dated between %s and %s; no orders to add.", filed_after, today)
        return [], [], []

    listed_by_number = {(item.court, item.number): item for item in listed if item.number}
    listings: list[OrderListing] | None = None  # fetched at most once, and only if a sheet has orders
    extra: list[Item] = []
    checked: list[AdvanceSheet] = []
    for sheet in sheets:
        try:
            pages = list(pdf_to_pages(session.get_bytes(sheet.pdf_url)))
        except Exception as exc:  # download or text extraction failed (any kind of error)
            notes.append(
                f"Could not read Advance Sheet No. {sheet.number} ({sheet.pdf_url}): {_reason(exc)}. "
                "Its orders and rule amendments are not included."
            )
            continue
        contents = parse_advance_sheet_contents(pages)
        orders = [e for e in contents.entries if e.kind is ItemKind.ORDER]
        opinions = [e for e in contents.entries if e.kind is ItemKind.OPINION]
        log.info(
            "Advance Sheet No. %d (%s, %d pages): %d published opinion(s) and %d order(s).",
            sheet.number, sheet.date.isoformat(), contents.total_pages, len(opinions), len(orders),
        )  # fmt: skip
        if contents.cover_page is None:
            notes.append(
                f"Advance Sheet No. {sheet.number} ({sheet.pdf_url}) has no recognizable cover page; "
                "its orders may be incomplete."
            )
        else:
            checked.append(sheet)
        if orders and listings is None:
            listings = _fetch_order_listings(session, notes)
        extra.extend(build_order_items(sheet, contents, pages, listings or []))

        for entry in opinions:
            match = listed_by_number.get((entry.court, entry.number))
            if match is not None:
                match.advance_sheet = sheet.number
                match.refiled = match.refiled or entry.refiled_date is not None
                continue
            if entry.court not in wanted:
                continue
            text = _slice_pages(pages, entry.start_page, entry.end_page)
            if not text.strip():
                notes.append(
                    f"{entry.court.display_name} Op. No. {entry.number} ({entry.title}) is listed in Advance Sheet "
                    f"No. {sheet.number}, but its pages have no text; it was skipped."
                )
                continue
            extra.append(
                Item(
                    kind=ItemKind.OPINION,
                    court=entry.court,
                    case_name=entry.title,
                    filed_date=entry.refiled_date or sheet.date,
                    number=entry.number,
                    pdf_url=None,
                    link_url=f"{sheet.pdf_url}#page={entry.start_page}",
                    source=SOURCE,
                    advance_sheet=sheet.number,
                    text=text,
                    refiled=entry.refiled_date is not None,
                )
            )
            notes.append(
                f"{entry.court.display_name} Op. No. {entry.number} ({entry.title}) is in Advance Sheet No. "
                f"{sheet.number} but not on the court's published-opinions page; it was added from the advance "
                f"sheet (pages {entry.start_page}-{entry.end_page})."
            )
    return sheets, checked, extra


def _fetch_order_listings(session: PoliteSession, notes: list[str]) -> list[OrderListing]:
    try:
        listings = parse_orders_listing(session.get_text(ALL_ORDERS_URL))
    except (FetchError, SourceError) as exc:
        notes.append(
            f"Could not read the sccourts.org All Court Orders page: {_reason(exc)}. "
            "Orders link to their page in the advance sheet instead of their own PDF."
        )
        return []
    log.info("All Court Orders page: %d order(s) listed.", len(listings))
    return listings


def _sort_key(item: Item) -> tuple:
    """Supreme Court opinions, then orders, then Court of Appeals opinions; each by date and number."""
    if item.kind is ItemKind.ORDER:
        group = 1
    else:
        group = 0 if item.court is Court.SUPREME else 2
    number = item.number or ""
    number_key = (0, int(number), "") if number.isdigit() else (1, 0, number)
    return (group, item.filed_date, number_key, item.case_name.lower())


def _dedupe_and_sort(items: list[Item]) -> list[Item]:
    unique: dict[str, Item] = {}
    for item in items:
        unique.setdefault(item.key, item)  # the first one wins (listing items come first)
    return sorted(unique.values(), key=_sort_key)


def _counts(items: list[Item]) -> list[tuple[str, int]]:
    labels = (
        ("Supreme Court opinion(s)", lambda i: i.kind is ItemKind.OPINION and i.court is Court.SUPREME),
        ("Court of Appeals opinion(s)", lambda i: i.kind is ItemKind.OPINION and i.court is Court.APPEALS),
        ("order(s)", lambda i: i.kind is ItemKind.ORDER),
    )
    return [(label, n) for label, test in labels if (n := sum(1 for i in items if test(i)))]
