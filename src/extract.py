"""PDF text extraction, opinion header parsing, and sizing text for one Claude request.

    extract_text(pdf_bytes)            -> ExtractedText (pdfplumber first, pypdf as the fallback)
    pdf_to_pages(pdf_bytes)            -> cleaned text of each page
    pdf_to_layout_pages(pdf_bytes)     -> cleaned text of each page, page-number lines kept (advance sheets)
    parse_opinion_header(text)         -> OpinionHeader (number, dates, disposition, author, case no.)
    prepare_for_model(text, max_tokens) -> PreparedText whose estimated size fits max_tokens

Nothing here touches the network or the file system.
"""

from __future__ import annotations

import contextlib
import io
import logging
import math
import re
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date

import pdfplumber
import pypdf

from .models import OpinionHeader, PreparedText

log = logging.getLogger(__name__)

MIN_USEFUL_CHARS = 500  # less text than this means "no readable text" (usually a scanned PDF)
CHARS_PER_TOKEN = 3.0  # deliberately low (legal English runs ~3.5-4 chars/token), so estimates err high
QUALITY_THRESHOLD = 0.5  # pdfplumber text scoring below this is retried with pypdf
HEADER_SEARCH_CHARS = 6000  # the opinion header is on the first page

SEPARATE_OPINION_MARKER = "[... remainder of the separate opinion(s) omitted to fit the input limit ...]"
MIDDLE_OMISSION_MARKER = (
    "\n\n[... about {n} characters omitted from the middle of this long opinion to fit the input limit ...]\n\n"
)

# text_quality() calibration: each signal scores 1.0 at or beyond its "good" level.
_GOOD_CHARS_PER_PAGE = 400  # non-space characters on a page of real text (opinion pages have ~2,000)
_GOOD_LETTER_SHARE = 0.6  # share of letters among non-space characters (legal prose is ~0.8)
_MAX_BAD_GLYPH_RATE = 0.1  # 10% or more unmapped glyphs scores zero

_HEAD_SHARE = 0.65  # head/tail split of the character budget when the middle is cut
_CUT_SLACK = 0.2  # a cut may move back up to 20% of its window to land on a line break
_MIN_ROOM = 100  # below this many characters of room, markers do not fit: keep only the beginning

_PDF_LIBRARY_LOGGERS = ("pdfminer", "pdfplumber", "pypdf")

_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


class ExtractionError(Exception):
    """Neither pdfplumber nor pypdf could read the PDF (corrupt, encrypted, or not a PDF)."""


@dataclass
class ExtractedText:
    """Text of one PDF. ``pages`` holds each page's cleaned text; ``text`` joins them with "\\n"."""

    text: str
    pages: list[str]
    method: str  # "pdfplumber" or "pypdf"
    quality: float  # 0..1, see text_quality()
    warnings: list[str] = field(default_factory=list)  # short, plain-English notes for the run log
    raw_pages: list[str] = field(default_factory=list)  # each page as the library returned it
    missing_pages: list[int] = field(default_factory=list)  # 1-based pages no library could read

    @property
    def ok(self) -> bool:
        """True when there is enough text to summarize (a scanned PDF has little or none)."""
        return len(self.text.strip()) >= MIN_USEFUL_CHARS


# --------------------------------------------------------------------------- cleaning

_CID_RE = re.compile(r"\(cid:\d+\)")
_LINE_BREAK_RE = re.compile(r"\r\n?|[\f\v\x85\u2028\u2029]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # every control character except \t and \n
_PAGE_NUMBER_LINE_RE = re.compile(r"\s*[0-9]{1,3}\s*")
_EXTRA_BLANK_LINES_RE = re.compile(r"\n{4,}")
_WHITESPACE_RE = re.compile(r"\s+")


def clean_text(text: str, drop_page_numbers: bool = True) -> str:
    """Tidy raw PDF text without changing its words.

    Removes "(cid:NN)" artifacts and control characters (tabs and no-break spaces become
    spaces), strips trailing spaces, drops lines that are only a page number (1-3 digits)
    unless ``drop_page_numbers`` is False, and collapses 3+ blank lines to 2. Typographic
    characters such as §, – and “ ” are kept.
    """
    if not text:
        return ""
    text = _CID_RE.sub("", text)
    text = _LINE_BREAK_RE.sub("\n", text)
    text = text.replace("\t", " ").replace("\u00a0", " ")
    text = _CONTROL_RE.sub("", text)
    lines = (line.rstrip() for line in text.split("\n"))
    if drop_page_numbers:
        lines = (line for line in lines if not _PAGE_NUMBER_LINE_RE.fullmatch(line))
    text = "\n".join(lines)
    return _EXTRA_BLANK_LINES_RE.sub("\n\n\n", text).strip("\n")


def text_quality(text: str, n_pages: int) -> float:
    """Score extracted text from 0 (unusable) to 1 (clean).

    Multiplies three signals: non-space characters per page (a scanned page has none), the
    share of letters among non-space characters (a broken font encoding yields symbols), and
    the rate of unmapped glyphs ("(cid:NN)" artifacts and U+FFFD replacement characters).
    """
    return _quality(text, n_pages)[0]


def _quality(text: str, n_pages: int) -> tuple[float, float, float, float]:
    """(score, non-space characters per page, letter share, unmapped-glyph rate)."""
    text = text or ""
    bad = len(_CID_RE.findall(text)) + text.count("\ufffd")
    visible = _WHITESPACE_RE.sub("", _CID_RE.sub("", text).replace("\ufffd", ""))
    glyphs = len(visible) + bad
    if glyphs == 0:
        return 0.0, 0.0, 0.0, 0.0
    per_page = glyphs / max(1, n_pages)
    letter_share = sum(ch.isalpha() for ch in visible) / len(visible) if visible else 0.0
    bad_rate = bad / glyphs
    score = (
        min(1.0, per_page / _GOOD_CHARS_PER_PAGE)
        * min(1.0, letter_share / _GOOD_LETTER_SHARE)
        * max(0.0, 1.0 - bad_rate / _MAX_BAD_GLYPH_RATE)
    )
    return round(score, 3), per_page, letter_share, bad_rate


# --------------------------------------------------------------------------- extraction

# Reads every page's raw text: (page texts, [(page number, error) for pages that failed]).
PageReader = Callable[[bytes], tuple[list[str], list[tuple[int, str]]]]


def extract_text(pdf_bytes: bytes) -> ExtractedText:
    """Extract a PDF's text with pdfplumber, falling back to pypdf.

    pypdf is tried when pdfplumber raises or its text quality is below QUALITY_THRESHOLD, and
    the better of the two results is kept. A page pdfplumber cannot read is filled from
    pypdf; pages neither library can read are listed in ``missing_pages``. Raises
    ExtractionError when neither library can read the PDF. A readable PDF with little or
    no text (a scanned image) is returned with ``ok`` False rather than raising.
    """
    if not pdf_bytes:
        raise ExtractionError("The PDF is empty (0 bytes).")
    data = bytes(pdf_bytes)

    primary, primary_error = _extract_with("pdfplumber", _pdfplumber_pages, data)
    if primary is not None and primary.quality >= QUALITY_THRESHOLD:
        if primary.missing_pages:
            _fill_missing_pages(primary, data)
        _log_extracted(primary)
        return primary

    if primary is None:
        reason = f"pdfplumber could not read the PDF ({primary_error})"
    else:
        reason = f"pdfplumber's text quality was low ({primary.quality:.2f})"
    log.info("%s; trying pypdf.", reason)
    fallback, fallback_error = _extract_with("pypdf", _pypdf_pages, data)

    if fallback is not None and (primary is None or fallback.quality > primary.quality):
        chosen, other = fallback, primary
        chosen.warnings.insert(0, f"Used pypdf because {reason}.")
    elif primary is not None:
        chosen, other = primary, fallback
        if fallback is None:
            chosen.warnings.insert(0, f"Kept pdfplumber's text; pypdf could not read the PDF ({fallback_error}).")
        else:
            chosen.warnings.insert(0, f"Kept pdfplumber's text; pypdf's was no better (quality {fallback.quality:.2f}).")
    else:
        hint = "" if b"%PDF" in data[:1024] else " The data does not look like a PDF (it may be an error page)."
        raise ExtractionError(
            f"Could not read the PDF with pdfplumber ({primary_error}) or pypdf ({fallback_error}).{hint}"
        )

    if chosen.missing_pages and other is not None:
        _fill_from(chosen, other)
    if chosen.quality < QUALITY_THRESHOLD:
        if not chosen.text.strip():
            chosen.warnings.append("The PDF has no extractable text (it may be a scanned image).")
        else:
            chosen.warnings.append(
                f"The extracted text looks poor (quality {chosen.quality:.2f}); "
                "the PDF may be scanned or use unusual fonts."
            )
    _log_extracted(chosen, other)
    return chosen


def pdf_to_pages(pdf_bytes: bytes) -> list[str]:
    """Cleaned text of each page (index 0 = page 1). Raises ExtractionError if the PDF is unreadable."""
    return extract_text(pdf_bytes).pages


def pdf_to_layout_pages(pdf_bytes: bytes) -> list[str]:
    """Like pdf_to_pages, but lines that hold only a number are kept (index 0 = page 1).

    An advance sheet's CONTENTS can put an entry's page number on a line of its own; the
    sccourts parser needs that line, and drops each page's own printed number itself.
    Raises ExtractionError if the PDF is unreadable.
    """
    return [clean_text(page, drop_page_numbers=False) for page in extract_text(pdf_bytes).raw_pages]


def _numbers_text(numbers: list[int]) -> str:
    return ", ".join(str(number) for number in numbers)


def _extract_with(method: str, reader: PageReader, data: bytes) -> tuple[ExtractedText | None, str | None]:
    """Run one extractor: (result, None) on success, (None, short reason) on failure."""
    try:
        with _quiet_pdf_libraries():
            raw_pages, failures = reader(data)
    except Exception as exc:  # the PDF libraries raise many exception types on malformed files
        return None, _describe_error(exc)
    if not raw_pages:
        return None, "the PDF has no pages"
    if len(failures) == len(raw_pages):
        return None, f"no page could be read ({failures[0][1]})"
    quality = text_quality("\n".join(raw_pages), len(raw_pages))
    pages = [clean_text(page) for page in raw_pages]
    missing = [number for number, _ in failures]
    notes = []
    if missing:
        notes.append(f"{method} could not read page(s) {_numbers_text(missing)}; their text is missing.")
    result = ExtractedText(
        clean_text("\n".join(pages)), pages, method, quality, notes, raw_pages=list(raw_pages), missing_pages=missing
    )
    return result, None


def _fill_missing_pages(result: ExtractedText, data: bytes) -> None:
    """Fill the pages pdfplumber could not read from pypdf, in place (the rest stays pdfplumber's)."""
    fallback, error = _extract_with("pypdf", _pypdf_pages, data)
    if fallback is None:
        log.info("pypdf could not fill the unreadable page(s) %s (%s).", _numbers_text(result.missing_pages), error)
        return
    _fill_from(result, fallback)


def _fill_from(result: ExtractedText, other: ExtractedText) -> None:
    """Copy into ``result`` each of its missing pages that ``other`` read (same page count only)."""
    missing = list(result.missing_pages)
    if not missing or len(other.raw_pages) != len(result.raw_pages):
        return
    filled = [n for n in missing if n not in other.missing_pages and other.pages[n - 1].strip()]
    if not filled:
        return
    for number in filled:
        result.raw_pages[number - 1] = other.raw_pages[number - 1]
        result.pages[number - 1] = other.pages[number - 1]
    result.missing_pages = [n for n in missing if n not in filled]
    result.text = clean_text("\n".join(result.pages))
    result.quality = text_quality("\n".join(result.raw_pages), len(result.raw_pages))
    first_note = f"{result.method} could not read page(s) {_numbers_text(missing)}; their text is missing."
    note = f"{result.method} could not read page(s) {_numbers_text(missing)}; filled page(s) {_numbers_text(filled)}"
    note += f" from {other.method}"
    if result.missing_pages:
        note += f", and page(s) {_numbers_text(result.missing_pages)} are still missing"
    result.warnings = [note + "." if w == first_note else w for w in result.warnings]


def _pdfplumber_pages(data: bytes) -> tuple[list[str], list[tuple[int, str]]]:
    pages: list[str] = []
    failures: list[tuple[int, str]] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            try:
                pages.append(page.extract_text() or "")
            except Exception as exc:  # one damaged page should not lose the others
                pages.append("")
                failures.append((number, _describe_error(exc)))
            finally:
                with contextlib.suppress(Exception):
                    page.close()  # frees pdfplumber's per-page caches (long advance sheets)
    return pages, failures


def _pypdf_pages(data: bytes) -> tuple[list[str], list[tuple[int, str]]]:
    reader = pypdf.PdfReader(io.BytesIO(data))  # tries the empty password on encrypted files
    pages: list[str] = []
    failures: list[tuple[int, str]] = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            pages.append(page.extract_text() or "")
        except Exception as exc:  # one damaged page should not lose the others
            pages.append("")
            failures.append((number, _describe_error(exc)))
    return pages, failures


@contextlib.contextmanager
def _quiet_pdf_libraries() -> Iterator[None]:
    """Hide the PDF libraries' warnings about harmless quirks of real-world PDFs.

    pdfminer and pypdf log a WARNING for every recoverable oddity, and in GitHub Actions each
    warning becomes an annotation. Failures still surface as exceptions. Nothing is hidden
    when this module's logger is at DEBUG level (``-v``).
    """
    if log.isEnabledFor(logging.DEBUG):
        yield
        return
    saved = [(logger, logger.level) for logger in map(logging.getLogger, _PDF_LIBRARY_LOGGERS)]
    for logger, _ in saved:
        logger.setLevel(logging.ERROR)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            yield
    finally:
        for logger, level in saved:
            logger.setLevel(level)


def _log_extracted(result: ExtractedText, other: ExtractedText | None = None) -> None:
    compare = f"; {other.method}'s was {other.quality:.2f}" if other is not None else ""
    log.info(
        "Extracted %d page(s) with %s: %s characters, quality %.2f%s.",
        len(result.pages), result.method, f"{len(result.text):,}", result.quality, compare,
    )
    if result.ok and result.quality < QUALITY_THRESHOLD:
        log.warning(
            "The extracted text looks garbled (quality %.2f), so its summary may be unreliable.", result.quality
        )


def _describe_error(exc: BaseException) -> str:
    message = _WHITESPACE_RE.sub(" ", str(exc)).strip()
    text = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    return text if len(text) <= 160 else text[:157] + "..."


# --------------------------------------------------------------------------- header

_DATE_TEXT = r"[A-Z][a-z]{2,8}\.?\s*\d{1,2}\s*,\s*\d{4}"
_DATE_PARTS_RE = re.compile(r"([A-Za-z]+)\.?\s*(\d{1,2})\s*,\s*(\d{4})")
_OPINION_NO_RE = re.compile(r"\bOpinion\s*No\.?\s*(\d{3,6})(?![\d-])")
_FILED_RE = re.compile(rf"\bFiled\s*({_DATE_TEXT})")
_HEARD_RE = re.compile(rf"\b(Heard|Submitted|Argued|Reheard)\s+({_DATE_TEXT})")
_CASE_NO_RE = re.compile(r"\bAppellate\s+Case\s+No\.?\s*(\d{4}-\d{3,7})\b")
_COURT_RE = re.compile(r"\bIn\s+the\s+(Supreme\s+Court|Court\s+of\s+Appeals)\b", re.IGNORECASE)

# An all-caps name: "HILL", "JOHN DOE", "MCDONALD", "O'NEAL", "JAMES E. MOORE".
_NAME = r"[A-Z][A-Z.'\u2019\-]*(?:[ \t]+[A-Z][A-Z.'\u2019\-]*)*"
_NAME_SUFFIX = r"(?:[ \t]*,[ \t]*(?:JR|SR|II|III|IV)\.?)?"  # "G. THOMAS COOPER, JR."
_RANK = r"(?:A\.[ \t]*J|C\.[ \t]*J|JJ|J)\."  # COA style: "J." judge, "C.J." chief, "A.J." acting
_PER_CURIAM_RE = re.compile(r"^[ \t]*PER[ \t]+CURIAM[ \t]*:", re.MULTILINE | re.IGNORECASE)
_JUSTICE_LINE_RE = re.compile(
    rf"^[ \t]*(ACTING[ \t]+)?(CHIEF[ \t]+)?JUSTICE[ \t]+({_NAME}{_NAME_SUFFIX})[ \t]*:", re.MULTILINE
)
_JUDGE_LINE_RE = re.compile(rf"^[ \t]*({_NAME})[ \t]*,[ \t]*(A\.[ \t]*J|C\.[ \t]*J|J)\.[ \t]*:", re.MULTILINE)

_DISPOSITION_LINE_RE = re.compile(r"[A-Z][A-Z ,;&'\u2019\-]*[A-Z,;]\.?")
_DISPOSITION_MAX_CHARS = 90
_DISPOSITION_SCAN_LINES = 2  # the disposition sits right under the "Filed" line
_ROMAN_SUFFIXES = frozenset({"II", "III", "IV"})


def parse_opinion_header(text: str) -> OpinionHeader:
    """Parse the facts printed at the top of an opinion. Fields not found are None; never raises.

    Only the first HEADER_SEARCH_CHARS characters are searched, so citations in the body
    cannot be mistaken for header lines.
    """
    header = OpinionHeader()
    if not text:
        return header
    try:
        _fill_header(header, str(text)[:HEADER_SEARCH_CHARS])
    except Exception as exc:  # defensive: header hints must never stop the pipeline
        log.warning("Could not fully parse the opinion header (%s).", _describe_error(exc))
    return header


def _fill_header(header: OpinionHeader, head: str) -> None:
    number = _OPINION_NO_RE.search(head)
    if number:
        header.opinion_number = number.group(1)
    case_no = _CASE_NO_RE.search(head)
    if case_no:
        header.appellate_case_no = case_no.group(1)

    filed = _FILED_RE.search(head)
    if filed:
        header.filed_date = _parse_date(filed.group(1))
        before = head[max(0, filed.start() - 300) : filed.start()]
        heard = list(_HEARD_RE.finditer(before))
        header.heard_or_submitted = _format_heard(heard[-1]) if heard else None
        region_start = filed.end()
    elif number:
        heard_match = _HEARD_RE.search(head, number.end(), number.end() + 300)
        header.heard_or_submitted = _format_heard(heard_match) if heard_match else None
        region_start = number.end()
    else:
        region_start = 0

    region = head[region_start:]
    court = _COURT_RE.search(head[:1500])
    supreme = court is not None and court.group(1).lower().startswith("supreme")
    author, author_at = _find_author(region, supreme)
    header.author = author
    if filed:
        header.disposition = _find_disposition(region[:author_at] if author_at is not None else region)


def _parse_date(text: str) -> date | None:
    """'November 12, 2026' (or 'Nov. 12, 2026') -> date; None if it is not a real date."""
    match = _DATE_PARTS_RE.fullmatch(_WHITESPACE_RE.sub(" ", text.strip()))
    if not match:
        return None
    word = match.group(1).lower()
    months = [i for i, name in enumerate(_MONTHS, start=1) if len(word) >= 3 and name.lower().startswith(word)]
    if len(months) != 1:
        return None
    try:
        return date(int(match.group(3)), months[0], int(match.group(2)))
    except ValueError:
        return None


def _format_date(d: date) -> str:
    return f"{_MONTHS[d.month - 1]} {d.day}, {d.year}"


def _format_heard(match: re.Match[str]) -> str:
    parsed = _parse_date(match.group(2))
    if parsed is None:
        return _WHITESPACE_RE.sub(" ", match.group(0)).strip()
    return f"{match.group(1)} {_format_date(parsed)}"


def _find_author(region: str, supreme: bool) -> tuple[str | None, int | None]:
    """The majority opinion's author from its opening line, and where that line starts."""
    found: list[tuple[int, str]] = []
    per_curiam = _PER_CURIAM_RE.search(region)
    if per_curiam:
        found.append((per_curiam.start(), "Per Curiam"))
    justice = _JUSTICE_LINE_RE.search(region)
    if justice:
        title = ("Acting " if justice.group(1) else "") + ("Chief " if justice.group(2) else "") + "Justice"
        found.append((justice.start(), f"{title} {_title_name(justice.group(3))}"))
    judge = _JUDGE_LINE_RE.search(region)
    if judge:
        noun = "Justice" if supreme else "Judge"  # "KONDUROS, J.:" is the Court of Appeals style
        rank = _WHITESPACE_RE.sub("", judge.group(2))
        title = {"J": noun, "C.J": f"Chief {noun}", "A.J": f"Acting {noun}"}[rank]
        found.append((judge.start(), f"{title} {_title_name(judge.group(1))}"))
    if not found:
        return None, None
    start, author = min(found)
    return author, start


def _title_name(name: str) -> str:
    """'MCDONALD' -> 'McDonald', "O'NEAL" -> "O'Neal", 'JAMES E. MOORE, III' -> 'James E. Moore, III'."""
    words = []
    for word in name.split():
        if word.strip(".,") in _ROMAN_SUFFIXES:
            words.append(word)
            continue
        chars = []
        upper_next = True
        for ch in word.lower():
            chars.append(ch.upper() if upper_next else ch)
            upper_next = ch in "'\u2019-"
        titled = "".join(chars)
        if len(titled) > 2 and titled.startswith("Mc"):
            titled = "Mc" + titled[2].upper() + titled[3:]
        words.append(titled)
    return " ".join(words)


def _find_disposition(region: str) -> str | None:
    """First short all-caps line right after the "Filed" line ('REVERSED', 'PUBLIC REPRIMAND').

    Parenthetical notes such as "(Withdrawn, Substituted, and Refiled ...)" are skipped. Only
    the first _DISPOSITION_SCAN_LINES other lines are considered, so an all-caps heading in
    the body ("BACKGROUND") is never taken for the disposition.
    """
    lines: list[str] = []
    in_parenthetical = False
    for raw in region.split("\n")[1:]:  # [1:] skips the rest of the "Filed" line
        line = raw.strip()
        if not line:
            continue
        if in_parenthetical or line.startswith("("):
            in_parenthetical = ")" not in line
            continue
        lines.append(line)
    for i, line in enumerate(lines[:_DISPOSITION_SCAN_LINES]):
        if not _is_disposition_line(line):
            continue
        parts = [line]
        # "AFFIRMED IN PART, REVERSED IN PART, AND" / "REMANDED" wrapped onto two lines
        while (
            len(parts) < 3
            and parts[-1].endswith((",", ";", " AND", " OR"))
            and i + len(parts) < len(lines)
            and _is_disposition_line(lines[i + len(parts)])
        ):
            parts.append(lines[i + len(parts)])
        return _WHITESPACE_RE.sub(" ", " ".join(parts)).strip(" .,;")
    return None


def _is_disposition_line(line: str) -> bool:
    return (
        len(line) <= _DISPOSITION_MAX_CHARS
        and bool(_DISPOSITION_LINE_RE.fullmatch(line))
        and sum(ch.isalpha() for ch in line) >= 4
    )


# --------------------------------------------------------------------------- sizing for the model

# A concurring or dissenting opinion's opening line: "JUSTICE FEW, dissenting:",
# "THOMAS, J., concurring in part and dissenting in part:", "WILLIAMS, C.J., and THOMAS, J., dissenting:".
_SEPARATE_OPINION_RE = re.compile(
    rf"^[ \t]*{_NAME}(?:[ \t]*,[ \t]*{_RANK})?(?:[ \t]*,?[ \t]+and[ \t]+{_NAME}(?:[ \t]*,[ \t]*{_RANK})?)*"
    r"[ \t]*,[ \t]*(?:concurring|dissenting)\b[^:\n]{0,120}:",
    re.MULTILINE,
)
# An author line in the court's own style: "JUSTICE HILL:", "CHIEF JUSTICE KITTREDGE:",
# "PER CURIAM:", "KONDUROS, J.:". The first one opens the majority. The Supreme Court opens
# a separate opinion the same way ("FEW, J., dissenting in a separate opinion." at the end
# of the majority, then "JUSTICE FEW: I respectfully dissent."), so a later one whose
# preceding text mentions a concurrence, a dissent, or a separate opinion starts one.
_AUTHOR_OPENER_RE = re.compile(
    rf"^[ \t]*(?:(?:ACTING[ \t]+)?(?:CHIEF[ \t]+)?JUSTICE[ \t]+{_NAME}{_NAME_SUFFIX}|PER[ \t]+CURIAM"
    rf"|{_NAME}[ \t]*,[ \t]*{_RANK})[ \t]*:",
    re.MULTILINE,
)
_SEPARATE_OPINION_CUE_RE = re.compile(r"concur|dissent|separate opinion", re.IGNORECASE)
_SEPARATE_OPINION_CUE_CHARS = 1500  # how far before a later author line to look for that cue


def estimate_tokens(text: str) -> int:
    """Rough input-token count: ceil(characters / CHARS_PER_TOKEN)."""
    return math.ceil(len(text or "") / CHARS_PER_TOKEN)


def prepare_for_model(text: str, max_tokens: int) -> PreparedText:
    """Fit ``text`` into ``max_tokens`` estimated tokens for one Claude request.

    1. Text that fits is returned unchanged.
    2. If a separate opinion (concurrence/dissent) follows the majority and the majority
       fits, the whole majority is kept plus as much of the separate opinion(s) as fits. A
       separate opinion starts at a line like "JUSTICE FEW, dissenting:" or, after the
       majority's own author line, at a later author line like "JUSTICE FEW: I respectfully
       dissent." that follows a mention of a concurrence, dissent, or separate opinion.
    3. Otherwise the beginning (65% of the budget) and the end (35%) are kept, with a marker
       saying how much was cut from the middle. When a separate opinion follows the majority,
       the beginning and end are taken from the majority, so the end of a long dissent is
       never presented as the end of the court's opinion.
    Cuts land on line breaks. The result always satisfies
    ``estimate_tokens(result.text) <= max_tokens``.
    """
    if max_tokens < 0:
        raise ValueError(f"max_tokens must be zero or more (got {max_tokens})")
    text = text or ""
    tokens = estimate_tokens(text)
    if tokens <= max_tokens:
        return PreparedText(text=text, truncated=False, kept_fraction=1.0, estimated_tokens=tokens)
    stripped = text.strip()
    if estimate_tokens(stripped) <= max_tokens:  # only surrounding whitespace was over the limit
        return PreparedText(
            text=stripped, truncated=False, kept_fraction=1.0, estimated_tokens=estimate_tokens(stripped)
        )

    budget = _char_budget(max_tokens)
    separate_at = _first_separate_opinion(stripped)
    shortened = _trim_separate_opinions(stripped, separate_at, budget) if separate_at is not None else None
    out, kept_chars, how = shortened or _keep_head_and_tail(stripped, budget, separate_at)
    if estimate_tokens(out) > max_tokens:  # defensive: the steps above already respect the budget
        log.warning("Shortened opinion text was still over the limit; cutting it to %s characters.", f"{budget:,}")
        out = out[:budget]
        kept_chars = min(kept_chars, len(out))

    fraction = min(round(kept_chars / len(stripped), 2), 0.99)  # never "100%" when something was cut
    percent = round(fraction * 100)
    log.info(
        "Opinion text is about %s tokens, over the %s-token limit: %s (about %d%% of the text kept).",
        f"{tokens:,}", f"{max_tokens:,}", how, percent,
    )
    return PreparedText(
        text=out,
        truncated=True,
        kept_fraction=fraction,
        estimated_tokens=estimate_tokens(out),
        note=f"Long opinion: this summary is based on about {percent}% of its text.",
    )


def slice_pages(pages: list[str], start: int, end: int) -> str:
    """Pages ``start``..``end`` (1-based, inclusive), clamped to the document, joined with "\\n"."""
    first = max(1, start)
    last = min(len(pages), end)
    if first > last:
        return ""
    return "\n".join(pages[first - 1 : last])


def _char_budget(max_tokens: int) -> int:
    """The most characters whose estimate_tokens() is still <= max_tokens."""
    budget = math.floor(max_tokens * CHARS_PER_TOKEN)
    while budget > 0 and math.ceil(budget / CHARS_PER_TOKEN) > max_tokens:
        budget -= 1
    return max(0, budget)


def _first_separate_opinion(text: str) -> int | None:
    """Where the first concurring/dissenting opinion starts (a line start), if one follows the majority.

    Two forms are recognized: "JUSTICE FEW, dissenting:" and, after the majority's own
    author line, a later author line such as "JUSTICE FEW: I respectfully dissent." that
    follows a mention of a concurrence, dissent, or separate opinion.
    """
    starts: list[int] = []
    match = _SEPARATE_OPINION_RE.search(text, MIN_USEFUL_CHARS)
    if match:
        starts.append(match.start())
    majority_author = _AUTHOR_OPENER_RE.search(text)
    if majority_author:
        for later in _AUTHOR_OPENER_RE.finditer(text, max(majority_author.end(), MIN_USEFUL_CHARS)):
            before = text[max(0, later.start() - _SEPARATE_OPINION_CUE_CHARS) : later.start()]
            if _SEPARATE_OPINION_CUE_RE.search(before):
                starts.append(later.start())
                break
    return min(starts) if starts else None


def _trim_separate_opinions(text: str, start: int, budget: int) -> tuple[str, int, str] | None:
    """Whole majority + as much separate opinion as fits + marker; None if the majority alone is too long.

    Returns (shortened text, characters kept from ``text``, what was done for the log).
    """
    marker = "\n\n" + SEPARATE_OPINION_MARKER
    limit = budget - len(marker)
    majority = text[:start].rstrip()
    if limit <= 0 or len(majority) > limit:
        return None
    cut = text.rfind("\n", start, limit + 1)  # the last line break that still fits
    kept = text[:cut].rstrip() if cut > start else majority
    return kept + marker, len(kept), "kept the whole majority opinion and trimmed the separate opinion(s)"


def _keep_head_and_tail(text: str, budget: int, majority_end: int | None) -> tuple[str, int, str]:
    """Beginning + middle-omission marker + end, within ``budget`` characters.

    With ``majority_end``, the beginning and end come from the majority opinion and the
    separate opinions are replaced by their marker. Returns (text, characters kept, log note).
    """
    if majority_end is not None:
        source, suffix = text[:majority_end].rstrip(), "\n\n" + SEPARATE_OPINION_MARKER
        how = "kept the beginning and end of the majority opinion and left out the separate opinion(s)"
    else:
        source, suffix = text, ""
        how = "kept the beginning and end of the opinion"
    # The marker can only shrink once the real omitted count is known (it is <= len(source)).
    reserve = len(MIDDLE_OMISSION_MARKER.format(n=f"{len(source):,}")) + len(suffix)
    room = budget - reserve
    if room < _MIN_ROOM:  # a tiny limit: the markers would not fit, so keep only the beginning
        head = _cut_head(text, budget)
        return head, len(head), "kept only the beginning (the limit is too small for omission markers)"
    if len(source) <= room:  # defensive: callers only get here when source is too long
        return source + suffix, len(source), how
    head = _cut_head(source, int(room * _HEAD_SHARE))
    tail = _cut_tail(source, room - int(room * _HEAD_SHARE))
    omitted = len(source) - len(head) - len(tail)
    return head + MIDDLE_OMISSION_MARKER.format(n=f"{omitted:,}") + tail + suffix, len(head) + len(tail), how


def _cut_head(text: str, limit: int) -> str:
    """At most ``limit`` characters from the start, ending at a line break when one is close."""
    if len(text) <= limit:
        return text
    if limit <= 0:
        return ""
    floor = int(limit * (1 - _CUT_SLACK))
    for sep in ("\n", " "):
        cut = text.rfind(sep, 0, limit + 1)
        if cut > 0 and cut >= floor:
            return text[:cut].rstrip()
    return text[:limit].rstrip()


def _cut_tail(text: str, limit: int) -> str:
    """At most ``limit`` characters from the end, starting at a line start when one is close."""
    if len(text) <= limit:
        return text
    if limit <= 0:
        return ""
    start = len(text) - limit
    if text[start - 1] == "\n":
        return text[start:].lstrip()
    ceiling = start + int(limit * _CUT_SLACK)
    for sep in ("\n", " "):
        cut = text.find(sep, start, ceiling + 1)
        if cut != -1:
            return text[cut + 1 :].lstrip()
    return text[start:].lstrip()
