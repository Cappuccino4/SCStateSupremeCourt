"""Build the digest email: Markdown for Buttondown, plus a standalone HTML preview.

The email reads top to bottom as: masthead, optional mock banner, one intro sentence,
"Rule changes this week", the Supreme Court opinions, the Supreme Court orders, the
Court of Appeals opinions, items listed without summaries (over the per-run cap), and
the disclaimer footer.

Two safety rules hold everywhere in this module:

- The body starts with Buttondown's editor-mode comment, so Buttondown treats it as
  Markdown. It never starts with ``---`` (Buttondown rejects that as front matter).
- Every string that came from a court, CourtListener, or the model goes through
  ``md_escape`` and every URL through ``md_url``. A stray ``*`` or ``[`` in a case name
  or summary therefore cannot reformat the email, and a Buttondown template tag such as
  ``{{ subscriber.email }}`` inside an opinion is never executed.
"""

from __future__ import annotations

import html
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import SplitResult, quote, urlsplit
from xml.etree import ElementTree

import markdown
from markdown.extensions import Extension
from markdown.treeprocessors import Treeprocessor

from .models import AdvanceSheet, Court, Item, ItemKind, SummaryResult

log = logging.getLogger(__name__)

DIGEST_NAME = "SC Advance Sheet Digest"
EM_DASH = "\u2014"  # em dash, used in the subject line
EDITOR_MODE_COMMENT = "<!-- buttondown-editor-mode: plaintext -->"
MOCK_BANNER = "⚠️ Preview with MOCK summaries — no AI calls were made."
DISCLAIMER = (
    "AI-generated summaries for informational purposes only; not legal advice; always read the full opinion."
)
UNSUBSCRIBE_NOTE = "You can unsubscribe at any time with the link at the bottom of this email."
PREVIEW_NOTE = (
    "Preview of the email body. Buttondown wraps it in your newsletter template and adds the unsubscribe link."
)
NO_RULE_CHANGES = "No opinion or order this week changes or clarifies a rule."
NO_RULE_CHANGES_OPINIONS_ONLY = "No opinion this week changes or clarifies a rule."
NO_NEW_OPINIONS = "No new published opinions this week."
# A supplement covers only what came after this week's earlier issue (a late posting, or a
# summary that was not ready); it must never read as if it described the whole week.
SUPPLEMENT_LABEL = "Supplement"
SUPPLEMENT_REST = "this week's earlier issue covers the rest of the week"
# The court's public orders page, offered to readers when this digest did not look for orders.
ORDERS_PAGE_URL = "https://www.sccourts.org/opinions-orders/court-orders/all-court-orders/"
ORDERS_NOT_COVERED = (
    "Supreme Court orders and rule amendments are not included in this digest; "
    f"check the [Supreme Court's orders page]({ORDERS_PAGE_URL})."
)
ORDERS_INCOMPLETE = (
    "This week's advance sheet could not be checked, so this digest may not include every Supreme Court "
    f"order or rule amendment; check the [Supreme Court's orders page]({ORDERS_PAGE_URL})."
)
UNCLEAR = "unclear from the opinion"  # the model's placeholder for facts it could not find
MAX_NAMES_LISTED = 10  # case names shown in the "not checked for rule changes" note before "and N more"

# Section buckets, in the order the court sections appear.
_SUPREME, _ORDERS, _APPEALS = 0, 1, 2


@dataclass
class DigestInput:
    """Everything one digest is built from.

    ``results`` are the items summarized (or attempted) this run; ``overflow`` are the
    items over the per-run cap, listed without summaries. ``orders_included`` says whether
    this run really checked this week's Supreme Court orders (CourtListener never returns
    them, and with SOURCE=sccourts the week's advance sheet must have been read).
    ``supplement_to`` is the subject of an earlier digest for the same week: this digest
    then only adds to it, and says so. ``cap`` is MAX_OPINIONS_PER_RUN, for the overflow note.
    """

    results: list[SummaryResult]
    overflow: list[Item]
    digest_date: date
    model: str
    courts_included: tuple[Court, ...]
    orders_included: bool
    advance_sheets: list[AdvanceSheet] = field(default_factory=list)
    mock: bool = False
    previous_subjects: frozenset[str] = frozenset()
    source: str = "courtlistener-api"
    supplement_to: str | None = None
    cap: int | None = None


@dataclass
class Digest:
    """A rendered digest, ready to save and to send as a Buttondown draft."""

    subject: str
    markdown: str
    html: str
    counts: dict[str, int]


# --- text helpers -----------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"\s+")
_ENTITY_LIKE_RE = re.compile(r"&(?=#?[A-Za-z0-9]+;)")  # "&copy;" would otherwise render as ©
_MD_SPECIAL_RE = re.compile(r"([\\`*_\[\]>|])")
_TEMPLATE_OPEN_RE = re.compile(r"\{(?=[{%#])")  # Buttondown/Django: {{ var }}, {% tag %}, {# comment #}
_LEADING_MARKER_RE = re.compile(r"^(?:[#+\-]|\d{1,9}[.)](?=\s|$))")
# Braces too, so a URL can never carry a Buttondown template tag ({{ ... }}) into the email.
_URL_UNSAFE = {" ": "%20", "(": "%28", ")": "%29", "<": "%3C", ">": "%3E", "{": "%7B", "}": "%7D"}


def format_date(d: date) -> str:
    """'November 12, 2026' (no leading zero on the day)."""
    return f"{d:%B} {d.day}, {d.year}"


def md_escape(text: str | None) -> str:
    """Make arbitrary text safe to place inside the digest's Markdown.

    Collapses whitespace (including newlines) to single spaces so the text stays on its
    line; backslash-escapes \\ ` * _ [ ] > | and a leading #, +, -, or "1."-style list
    marker; writes < as &lt; (Python-Markdown would show a stray backslash for "\\<");
    keeps "&copy;"-like sequences literal; and breaks Buttondown/Django template syntax
    by turning {{, {%, {# into { {, { %, { #.
    """
    if text is None:
        return ""
    text = _WHITESPACE_RE.sub(" ", str(text)).strip()
    if not text:
        return ""
    text = _ENTITY_LIKE_RE.sub("&amp;", text)
    text = _MD_SPECIAL_RE.sub(r"\\\1", text)
    text = text.replace("<", "&lt;")
    text = _TEMPLATE_OPEN_RE.sub("{ ", text)
    match = _LEADING_MARKER_RE.match(text)
    if match:
        marker = match.group(0)
        text = marker[:-1] + "\\" + marker[-1] + text[len(marker) :]
    return text


def md_url(url: str | None) -> str:
    """Make a URL safe as a Markdown link target: percent-encode spaces, ( ) < > { } and other whitespace.

    Existing %xx escapes are left alone, so encoding twice changes nothing.
    """
    if not url:
        return ""
    out: list[str] = []
    for ch in url.strip():
        if ch in _URL_UNSAFE:
            out.append(_URL_UNSAFE[ch])
        elif ch.isspace():
            out.append(quote(ch, safe=""))
        else:
            out.append(ch)
    return "".join(out)


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _count_phrase(n: int, noun: str) -> str:
    """'no published Court of Appeals opinions' / '1 ... opinion' / '2 ... opinions'."""
    return f"no {noun}s" if n == 0 else _plural(n, noun)


def _join_and(parts: list[str]) -> str:
    if len(parts) <= 2:
        return " and ".join(parts)
    return ", ".join(parts[:-1]) + ", and " + parts[-1]


def _known(value: str | None) -> bool:
    """False for empty values and the model's 'unclear from the opinion' placeholder."""
    if not value or not str(value).strip():
        return False
    return str(value).strip().rstrip(".").casefold() != UNCLEAR


def _tidy_caps(text: str) -> str:
    """'PUBLIC REPRIMAND' -> 'Public reprimand' (header dispositions are all caps). Mixed case is kept."""
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 4 or any(c.islower() for c in letters):
        return text
    lowered = text.strip().lower()
    return lowered[:1].upper() + lowered[1:]


def _heading_text(text: str) -> str:
    escaped = md_escape(text) or "Untitled"
    # A run of "#" at the end of an ATX heading is read as a closing sequence; keep it visible.
    return re.sub(r"#+$", lambda m: "\\#" * len(m.group(0)), escaped)


def _percent(fraction: float) -> int:
    """Kept share of a truncated text as a whole percent, never shown as 0% or 100%."""
    return max(1, min(99, round(float(fraction) * 100)))


# --- links ------------------------------------------------------------------------


def _url_parts(url: str | None) -> SplitResult | None:
    try:
        return urlsplit((url or "").strip())
    except ValueError:  # e.g. an unbalanced "[" in the host; rendering must never crash on one bad URL
        return None


def _is_web_url(url: str | None) -> bool:
    return bool(url) and url.strip().lower().startswith(("https://", "http://"))


def _is_pdf(url: str | None) -> bool:
    parts = _url_parts(url)
    return parts is not None and parts.path.lower().rstrip().endswith(".pdf")


def _link(label: str, url: str | None) -> str | None:
    """Markdown link, or None when there is no usable http(s) URL."""
    if not _is_web_url(url):
        return None
    return f"[{label}]({md_url(url)})"


def _main_link_label(item: Item) -> str:
    label = "Read the order" if item.kind == ItemKind.ORDER else "Read the full opinion"
    return f"{label} (PDF)" if _is_pdf(item.link_url) else label


def _mirror_label(url: str) -> str:
    """Say what the second link is, so readers know why there are two."""
    parts = _url_parts(url)
    if parts is None:
        return "Alternate copy"
    host = (parts.hostname or "").lower()
    if host == "storage.courtlistener.com":
        return "CourtListener copy"
    if host == "courtlistener.com" or host.endswith(".courtlistener.com"):
        return "View on CourtListener"
    if "/advsheets/" in parts.path.lower():
        return "Advance sheet copy"
    return "Alternate copy"


def _links_line(item: Item) -> str:
    parts = [_link(_main_link_label(item), item.link_url)]
    if item.mirror_url and item.mirror_url.strip() != (item.link_url or "").strip():
        parts.append(_link(_mirror_label(item.mirror_url), item.mirror_url))
    return " · ".join(p for p in parts if p)


# --- grouping and counting --------------------------------------------------------


def _bucket(item: Item) -> int:
    if item.kind == ItemKind.ORDER:
        return _ORDERS
    return _SUPREME if item.court == Court.SUPREME else _APPEALS


def _item_sort_key(item: Item) -> tuple:
    number = (item.number or "").strip()
    numeric = number.isdigit()
    name = (item.case_name or "").casefold()
    return (item.filed_date, 0 if numeric else 1, int(number) if numeric else 0, number, name)


def _buckets(results: list[SummaryResult]) -> dict[int, list[SummaryResult]]:
    grouped: dict[int, list[SummaryResult]] = {_SUPREME: [], _ORDERS: [], _APPEALS: []}
    for result in results:
        grouped[_bucket(result.item)].append(result)
    for entries in grouped.values():
        entries.sort(key=lambda r: _item_sort_key(r.item))
    return grouped


def _all_items(inp: DigestInput) -> list[Item]:
    return [r.item for r in inp.results] + list(inp.overflow)


def _kind_counts(inp: DigestInput) -> tuple[int, int]:
    """(opinions, orders) across summarized, failed, and overflow items."""
    items = _all_items(inp)
    opinions = sum(1 for it in items if it.kind == ItemKind.OPINION)
    orders = sum(1 for it in items if it.kind == ItemKind.ORDER)
    return opinions, orders


def _has_orders(inp: DigestInput) -> bool:
    return any(it.kind == ItemKind.ORDER for it in _all_items(inp))


def _orders_covered(inp: DigestInput) -> bool:
    """True when this digest really checked this week's Supreme Court orders (CourtListener has none).

    Order items alone do not prove it: they can come from an older advance sheet while
    this week's sheet could not be read (``orders_included`` is then False).
    """
    if not inp.orders_included:
        return False
    if _has_orders(inp):
        return True
    return not (inp.source or "").startswith("courtlistener")


def digest_counts(inp: DigestInput) -> dict[str, int]:
    """Counts for logs, digest.json, and the run summary."""
    opinions, orders = _kind_counts(inp)
    summarized = sum(1 for r in inp.results if r.ok)
    return {
        "opinions": opinions,
        "orders": orders,
        "summarized": summarized,
        "failed": len(inp.results) - summarized,
        "overflow": len(inp.overflow),
        "rule_changes": sum(1 for r in inp.results if r.ok and r.summary.is_substantive),
    }


# --- subject ----------------------------------------------------------------------


def build_subject(inp: DigestInput) -> str:
    """'SC Advance Sheet Digest — November 12, 2026 — 3 opinions + 1 order', unique among previous subjects."""
    opinions, orders = _kind_counts(inp)
    if opinions == 0 and orders > 0:
        tally = _plural(orders, "order")
    else:
        tally = _plural(opinions, "opinion")
        if orders:
            tally += f" + {_plural(orders, 'order')}"
    base = f"{DIGEST_NAME} {EM_DASH} {format_date(inp.digest_date)} {EM_DASH} {tally}"
    previous = inp.previous_subjects or frozenset()
    if inp.supplement_to:
        # A second email for the same week: label it, so it never reads as a replacement.
        candidate, n = f"{base} (supplement)", 2
        while candidate in previous:
            candidate = f"{base} (supplement {n})"
            n += 1
        return candidate
    if base not in previous:
        return base
    candidate, n = f"{base} (update)", 2
    while candidate in previous:
        candidate = f"{base} (update {n})"
        n += 1
    return candidate


# --- Markdown ---------------------------------------------------------------------


def _advance_sheet_text(inp: DigestInput) -> str | None:
    """'Advance Sheet No. 41' (linked) for the sheet(s) this digest's items appear in."""
    by_number = {s.number: s for s in inp.advance_sheets}
    numbers = sorted({it.advance_sheet for it in _all_items(inp) if it.advance_sheet})
    if not numbers:
        numbers = sorted(s.number for s in inp.advance_sheets if s.date == inp.digest_date)
    if not numbers:
        return None

    def label(n: int, text: str) -> str:
        sheet = by_number.get(n)
        return (_link(text, sheet.pdf_url) if sheet else None) or text

    if len(numbers) == 1:
        return label(numbers[0], f"Advance Sheet No. {numbers[0]}")
    return "Advance Sheet Nos. " + _join_and([label(n, str(n)) for n in numbers])


def _masthead(inp: DigestInput) -> str:
    parts = [f"**{DIGEST_NAME}**", format_date(inp.digest_date)]
    sheet = _advance_sheet_text(inp)
    if sheet:
        parts.append(sheet)
    if inp.supplement_to:
        parts.append(SUPPLEMENT_LABEL)
    return " · ".join(parts)


def _intro(inp: DigestInput) -> str:
    counts = Counter(_bucket(it) for it in _all_items(inp))
    supplement = bool(inp.supplement_to)
    parts: list[str] = []
    # A supplement names only what it adds: "no Supreme Court opinions" would deny the earlier issue.
    if counts[_SUPREME] or (Court.SUPREME in inp.courts_included and not supplement):
        parts.append(_count_phrase(counts[_SUPREME], "published Supreme Court opinion"))
    if counts[_ORDERS]:
        parts.append(_count_phrase(counts[_ORDERS], "Supreme Court order"))
    if counts[_APPEALS] or (Court.APPEALS in inp.courts_included and not supplement):
        parts.append(_count_phrase(counts[_APPEALS], "published Court of Appeals opinion"))
    if not parts:
        return NO_NEW_OPINIONS
    if supplement:
        earlier = md_escape(inp.supplement_to)
        sentence = f"Supplement to this week's earlier issue (“{earlier}”): " + _join_and(parts)
    else:
        sentence = "New this week: " + _join_and(parts)
    n = len(inp.overflow)
    if n:
        verb, what = ("is", "a summary") if n == 1 else ("are", "summaries")
        sentence += f" ({n} {verb} listed at the end without {what})"
    return sentence + "."


def _court_label(item: Item) -> str:
    return "Order" if item.kind == ItemKind.ORDER else item.court.display_name


def _rule_change_line(result: SummaryResult) -> str:
    item, summary = result.item, result.summary
    label = "Changes:" if summary.change_type == "changes" else "Clarifies:"
    # Strip only the label that matches change_type, so a disagreeing label is never relabeled.
    own_label = re.compile(rf"^\s*{label[:-1]}\s*:\s*", re.IGNORECASE)
    text = own_label.sub("", summary.what_changed or "", count=1).strip() or summary.one_line
    case = md_escape(item.case_name) or "Untitled"
    line = f"- **{case}** ({_court_label(item)}) — **{label}** {md_escape(text)}"
    link = _link("PDF", item.link_url)
    return f"{line} {link}" if link else line


def _case_names(items: list[Item]) -> str:
    """'A v. B; C v. D; and 3 more' (case names contain commas, so they are joined with semicolons)."""
    names = [md_escape(it.case_name) or "Untitled" for it in items[:MAX_NAMES_LISTED]]
    more = len(items) - len(names)
    return "; ".join(names) + (f"; and {more} more" if more else "")


def _unchecked_sentence(items: list[Item], *, more: bool) -> str:
    """'2 opinions were not summarized, so they were not checked for rule changes: A v. B; C v. D.'"""
    n = len(items)
    noun = "item" if any(it.kind == ItemKind.ORDER for it in items) else "opinion"
    count = f"{n} more" if more else str(n)
    if n == 1:
        return f"{count} {noun} was not summarized, so it was not checked for rule changes: {_case_names(items)}."
    return f"{count} {noun}s were not summarized, so they were not checked for rule changes: {_case_names(items)}."


def _rule_changes(inp: DigestInput, buckets: dict[int, list[SummaryResult]]) -> str:
    """The top section: rule changes, and an honest account of what could not be checked."""
    orders_covered = _orders_covered(inp)
    with_orders = orders_covered or _has_orders(inp)
    supplement = bool(inp.supplement_to)
    blocks = ["## Rule changes this week"]
    substantive = [
        r for b in (_ORDERS, _SUPREME, _APPEALS) for r in buckets[b] if r.ok and r.summary.is_substantive
    ]
    # Opinions nobody classified (failed summaries, items over the cap) may change a rule too.
    unchecked = sorted(
        [r.item for r in inp.results if not r.ok] + list(inp.overflow), key=lambda it: (_bucket(it), _item_sort_key(it))
    )
    if substantive:
        blocks.append("\n".join(_rule_change_line(r) for r in substantive))
        notes = [_unchecked_sentence(unchecked, more=True)] if unchecked else []
        if supplement:
            notes.append(f"Only this supplement's items are listed here; {SUPPLEMENT_REST}.")
        if notes:
            blocks.append(f"_{' '.join(notes)}_")
    elif unchecked:
        sentences = []
        if any(r.ok for r in inp.results):
            scope = "in this supplement" if supplement else "this week"
            what = "opinions or orders" if with_orders else "opinions"
            sentences.append(f"None of the summarized {what} {scope} changes or clarifies a rule.")
        sentences.append(_unchecked_sentence(unchecked, more=False))
        sentences.append(f"Read {'it' if len(unchecked) == 1 else 'them'} directly (links below).")
        if supplement:
            sentences.append(SUPPLEMENT_REST[:1].upper() + SUPPLEMENT_REST[1:] + ".")
        blocks.append(f"_{' '.join(sentences)}_")
    elif supplement:
        noun = "opinion or order" if with_orders else "opinion"
        blocks.append(f"_No {noun} in this supplement changes or clarifies a rule; {SUPPLEMENT_REST}._")
    else:
        blocks.append(f"_{NO_RULE_CHANGES if with_orders else NO_RULE_CHANGES_OPINIONS_ONLY}_")
    if not orders_covered:
        blocks.append(f"_{ORDERS_INCOMPLETE if _has_orders(inp) else ORDERS_NOT_COVERED}_")
    return "\n\n".join(blocks)


def _opinion_number(result: SummaryResult) -> str | None:
    item = result.item
    if item.number and item.number.strip():
        return item.number.strip()
    # The model copies "Opinion No. 28355" from the text; use it only if it looks like one.
    if item.kind == ItemKind.OPINION and result.ok:
        value = (result.summary.opinion_number or "").strip()
        if re.fullmatch(r"\d{3,6}", value):
            return value
    return None


def _meta_line(result: SummaryResult) -> str:
    """'**Op. No. 28355** · Filed November 12, 2026 · Justice Hill · **Reversed**'."""
    item = result.item
    summary = result.summary if result.ok else None
    parts: list[str] = []
    number = _opinion_number(result)
    if number:
        prefix = "Order No." if item.kind == ItemKind.ORDER else "Op. No."
        parts.append(f"**{prefix} {md_escape(number)}**")
    filed = f"Filed {format_date(item.filed_date)}"
    if item.refiled:
        filed += " (withdrawn, substituted, and refiled)"
    parts.append(filed)
    author = summary.author if summary else item.author
    if _known(author):
        parts.append(md_escape(author))
    disposition = summary.disposition if summary else item.disposition_hint
    if _known(disposition):
        parts.append(f"**{md_escape(_tidy_caps(disposition))}**")
    return " · ".join(parts)


def _entry(result: SummaryResult) -> str:
    """One opinion or order: heading, meta line, summary (or the failure note), links."""
    item = result.item
    blocks = [f"### {_heading_text(item.case_name)}\n{_meta_line(result)}"]
    if result.ok:
        summary = result.summary
        blocks.append(f"**Takeaway:** {md_escape(summary.one_line)}")
        blocks.append(f"**Holding:** {md_escape(summary.holding)}")
        blocks.append(f"**What changed:** {md_escape(summary.what_changed)}")
        blocks.append(f"**Why it matters:** {md_escape(summary.practice_tip)}")
        raw_areas = summary.practice_areas or []
        if isinstance(raw_areas, str):
            raw_areas = [raw_areas]
        areas = [md_escape(a) for a in raw_areas if a and str(a).strip()]
        if areas:
            blocks.append(f"_Practice areas: {', '.join(areas)}_")
        if result.truncated:
            pct = _percent(result.kept_fraction)
            blocks.append(f"_Long opinion: this summary is based on about {pct}% of its text._")
    else:
        reason = md_escape(result.error).rstrip(" .") or "the summary could not be generated"
        noun = "order" if item.kind == ItemKind.ORDER else "opinion"
        blocks.append(f"_The AI summary isn't available this week ({reason}). Please read the full {noun}._")
    links = _links_line(item)
    if links:
        blocks.append(links)
    return "\n\n".join(blocks)


def _more_note(bucket: int, n_overflow: int, has_entries: bool) -> str:
    """Point readers at the overflow list from a section whose items did not all fit."""
    noun = "order" if bucket == _ORDERS else "opinion"
    where = "" if bucket == _ORDERS else " from this court"
    if not has_entries:
        whose = "This week's orders are" if bucket == _ORDERS else "This court's new opinions are"
        return f"_{whose} listed at the end without summaries._"
    if n_overflow == 1:
        return f"_1 more {noun}{where} is listed at the end without a summary._"
    return f"_{n_overflow} more {noun}s{where} are listed at the end without summaries._"


def _section(title: str, bucket: int, results: list[SummaryResult], n_overflow: int, show_empty: bool) -> str | None:
    if not results and not n_overflow and not show_empty:
        return None
    blocks = [f"## {title}"]
    if results:
        blocks.append("\n\n---\n\n".join(_entry(r) for r in results))
    if n_overflow:
        blocks.append(_more_note(bucket, n_overflow, bool(results)))
    elif not results:
        blocks.append(f"_{NO_NEW_OPINIONS}_")
    return "\n\n".join(blocks)


def _overflow_line(item: Item) -> str:
    """'- **Case** · Court of Appeals · Op. No. 6171 · Filed November 12, 2026 · [PDF](...)'."""
    is_order = item.kind == ItemKind.ORDER
    parts = [
        f"**{md_escape(item.case_name) or 'Untitled'}**",
        "Supreme Court order" if is_order else item.court.display_name,
    ]
    if item.number and item.number.strip():
        parts.append(f"{'Order No.' if is_order else 'Op. No.'} {md_escape(item.number)}")
    filed = f"Filed {format_date(item.filed_date)}"
    if item.refiled:
        filed += " (withdrawn, substituted, and refiled)"
    parts.append(filed)
    link = _link("PDF" if _is_pdf(item.link_url) else "Link", item.link_url)
    if link:
        parts.append(link)
    return "- " + " · ".join(parts)


def _overflow_section(inp: DigestInput) -> str:
    cap = inp.cap if inp.cap else len(inp.results)
    if cap:
        sentence = f"This week had more items than the {cap}-summary limit; these are listed without summaries."
    else:
        sentence = "This week had more items than this run could summarize; these are listed without summaries."
    items = sorted(inp.overflow, key=lambda it: (_bucket(it), _item_sort_key(it)))
    return "\n\n".join(
        ["## Also filed this week (not summarized)", sentence, "\n".join(_overflow_line(it) for it in items)]
    )


def _model_text(inp: DigestInput) -> str:
    """The configured model, plus any other model that actually served a summary (server-side fallback)."""
    model = (inp.model or "").strip()
    others = sorted(
        {
            r.model.strip()
            for r in inp.results
            if r.ok and not r.mock and r.model and r.model.strip() and r.model.strip() != model
        }
    )
    if others and model:
        return f"{model}; some summaries by {', '.join(others)}"
    return model or ", ".join(others)


def _sources_line(source: str) -> str:
    source = source or ""
    if source.startswith("courtlistener"):
        return "Opinions: South Carolina Judicial Branch, retrieved via CourtListener (Free Law Project)."
    if source == "sccourts":
        return "Opinions and orders: South Carolina Judicial Branch (sccourts.org)."
    return "Opinions: South Carolina Judicial Branch."


def _footer(inp: DigestInput) -> str:
    model = _model_text(inp)
    by = f"an AI model ({md_escape(model)})" if model else "an AI model"
    return "\n\n".join(
        [
            "---",
            f"**{DISCLAIMER}**",
            f"Summaries are written by {by} from the text of each opinion and may contain errors or omissions. "
            "Opinions can be revised, withdrawn, or reheard.",
            _sources_line(inp.source),
            UNSUBSCRIBE_NOTE,
        ]
    )


def render_markdown(inp: DigestInput, subject: str) -> str:
    """The email body Buttondown receives (Markdown; starts with the editor-mode comment)."""
    log.debug("Rendering the digest body for %r", subject)
    buckets = _buckets(inp.results)
    overflow_counts = Counter(_bucket(it) for it in inp.overflow)
    blocks = [EDITOR_MODE_COMMENT, _masthead(inp)]
    if inp.mock or any(r.mock for r in inp.results):
        blocks.append(f"**{MOCK_BANNER}**")
    blocks.append(_intro(inp))
    blocks.append(_rule_changes(inp, buckets))
    # A supplement leaves out empty court sections: "No new published opinions" would deny the earlier issue.
    show_empty = not inp.supplement_to
    sections = (
        ("Supreme Court", _SUPREME, show_empty and Court.SUPREME in inp.courts_included),
        ("Supreme Court orders", _ORDERS, False),
        ("Court of Appeals", _APPEALS, show_empty and Court.APPEALS in inp.courts_included),
    )
    for title, bucket, show_empty in sections:
        section = _section(title, bucket, buckets[bucket], overflow_counts[bucket], show_empty)
        if section:
            blocks.append(section)
    if inp.overflow:
        blocks.append(_overflow_section(inp))
    blocks.append(_footer(inp))
    return "\n\n".join(blocks) + "\n"


# --- HTML preview -----------------------------------------------------------------


class _NewTabLinks(Treeprocessor):
    """Give every external link target="_blank" (runs after Python-Markdown builds the links)."""

    def run(self, root: ElementTree.Element) -> None:
        for link in root.iter("a"):
            href = link.get("href") or ""
            if href and not href.startswith("#"):
                link.set("target", "_blank")
                link.set("rel", "noopener noreferrer")


class _NewTabLinksExtension(Extension):
    """Python-Markdown extension: external links open in a new tab."""

    def extendMarkdown(self, md: markdown.Markdown) -> None:
        md.treeprocessors.register(_NewTabLinks(md), "digest_new_tab_links", 5)


_PREVIEW_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; padding: 0 16px 48px; background: #eceef1; color: #1f2933;
  font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
.preview-note { max-width: 680px; margin: 0 auto; padding: 18px 2px 12px; font-size: 13px; line-height: 1.5;
  color: #52606d; }
.preview-note p { margin: 0 0 4px; }
.preview-subject span { font-weight: 600; color: #1f2933; }
.email { max-width: 680px; margin: 0 auto; background: #ffffff; border: 1px solid #d9dde3; border-radius: 8px;
  padding: 32px 44px 28px; overflow-wrap: break-word; }
.email h1, .email h2, .email h3 { font-family: Georgia, "Times New Roman", Times, serif; color: #102a43; }
.email h2 { font-size: 22px; line-height: 1.3; margin: 40px 0 16px; padding-bottom: 8px;
  border-bottom: 2px solid #102a43; }
.email h3 { font-size: 19px; line-height: 1.35; margin: 28px 0 4px; }
.email h3 + p { margin-top: 0; font-size: 14.5px; color: #486581; }
.email p { margin: 0 0 14px; }
.email ul { margin: 0 0 16px; padding-left: 22px; }
.email li { margin: 0 0 10px; }
.email a { color: #0b5cad; text-underline-offset: 2px; }
.email hr { border: 0; border-top: 1px solid #e4e7eb; margin: 30px 0; }
.email em { color: #52606d; }
.email > p:first-of-type { font-size: 14px; color: #52606d; padding-bottom: 14px; margin-bottom: 18px;
  border-bottom: 1px solid #e4e7eb; }
.email > p:first-of-type strong { font-family: Georgia, "Times New Roman", Times, serif; font-size: 17px;
  color: #102a43; }
@media (max-width: 600px) {
  body { padding: 0 0 32px; }
  .preview-note { padding: 14px 16px 10px; }
  .email { border-radius: 0; border-left: 0; border-right: 0; padding: 24px 16px; }
}
""".strip()


def render_html(markdown_body: str, subject: str) -> str:
    """A complete HTML5 page showing the email body, for local and artifact previews."""
    content = markdown.markdown(markdown_body, extensions=["extra", "sane_lists", _NewTabLinksExtension()])
    title = html.escape(subject)
    return "\n".join(
        [
            "<!DOCTYPE html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            '<meta name="color-scheme" content="light">',
            f"<title>{title}</title>",
            f"<style>\n{_PREVIEW_CSS}\n</style>",
            "</head>",
            "<body>",
            '<header class="preview-note">',
            f"<p>{html.escape(PREVIEW_NOTE)}</p>",
            f'<p class="preview-subject"><span>Subject:</span> {title}</p>',
            "</header>",
            '<main class="email">',
            content,
            "</main>",
            "</body>",
            "</html>",
            "",
        ]
    )


def build_digest(inp: DigestInput) -> Digest:
    """Subject, Markdown body, HTML preview, and counts for one run."""
    subject = build_subject(inp)
    body = render_markdown(inp, subject)
    counts = digest_counts(inp)
    log.info(
        "Digest %r: %d opinion(s), %d order(s); %d summarized, %d failed, %d listed without a summary; "
        "%d rule change(s)",
        subject,
        counts["opinions"],
        counts["orders"],
        counts["summarized"],
        counts["failed"],
        counts["overflow"],
        counts["rule_changes"],
    )
    return Digest(subject=subject, markdown=body, html=render_html(body, subject), counts=counts)
