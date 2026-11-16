"""Data types shared by every pipeline stage.

The stages (scrape -> extract -> summarize -> digest -> send) pass these objects to
one another, so this module is the contract between them. Keep it free of I/O and
third-party imports.
"""

from __future__ import annotations

import enum
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date


class Court(str, enum.Enum):
    """The two South Carolina appellate courts. The value is used in state keys."""

    SUPREME = "SC"
    APPEALS = "COA"

    @property
    def display_name(self) -> str:
        return "Supreme Court" if self is Court.SUPREME else "Court of Appeals"

    @property
    def full_name(self) -> str:
        if self is Court.SUPREME:
            return "Supreme Court of South Carolina"
        return "South Carolina Court of Appeals"


class ItemKind(str, enum.Enum):
    OPINION = "opinion"
    ORDER = "order"


def slugify(text: str, max_len: int = 60) -> str:
    """Lowercase ASCII slug: 'Re: Amendments to Rule 1.15' -> 're-amendments-to-rule-1-15'."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "untitled"


@dataclass
class Item:
    """One published opinion or court order found by a source.

    ``pdf_url`` is what the pipeline downloads (in CourtListener mode, CourtListener's
    stored copy, so the job never touches sccourts.org). ``link_url`` is what readers
    click in the email (the court's own PDF when known).
    """

    kind: ItemKind
    court: Court
    case_name: str
    filed_date: date
    number: str | None  # "28355", "6170", or an order number like "2026-11-12-01"; None until known
    pdf_url: str | None
    link_url: str
    mirror_url: str | None = None  # optional second link, e.g. CourtListener's copy
    source: str = ""  # "courtlistener-api" | "courtlistener-feed" | "sccourts"
    source_id: str | None = None  # e.g. CourtListener cluster id "11060100"
    court_summary: str = ""  # court's own one-line blurb (sccourts listing); shown nowhere, never sent to Claude
    author: str | None = None  # filled from the PDF header (extract.parse_opinion_header)
    disposition_hint: str | None = None  # e.g. "REVERSED", from the PDF header
    advance_sheet: int | None = None  # advance sheet number, when known
    text: str | None = None  # pre-extracted text (orders sliced from an advance sheet); None -> download pdf_url
    refiled: bool = False  # withdrawn, substituted, and refiled opinion
    # Other identities of the same item, e.g. an order's date-and-title key, which stays the
    # same whether or not the order was matched to the All Court Orders page.
    extra_keys: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        """Stable, source-independent identity used in data/processed.json."""
        if self.kind is ItemKind.ORDER:
            if self.number:
                return f"ORDER:{self.number}"
            return f"ORDER:{self.filed_date.isoformat()}:{slugify(self.case_name)}"
        if self.number:
            return f"{self.court.value}:{self.number}"
        if self.source.startswith("courtlistener") and self.source_id:
            return f"CL:{self.source_id}"
        return f"{self.court.value}:{self.filed_date.isoformat()}:{slugify(self.case_name)}"

    @property
    def alias_keys(self) -> list[str]:
        """Other keys this item may already be recorded under (its CourtListener id, its extra keys)."""
        key = self.key
        aliases: list[str] = []
        if self.source.startswith("courtlistener") and self.source_id:
            alias = f"CL:{self.source_id}"
            if alias != key:
                aliases.append(alias)
        for extra in self.extra_keys:
            if extra and extra != key and extra not in aliases:
                aliases.append(extra)
        return aliases

    @property
    def label(self) -> str:
        """Short human label for logs: 'SC 28355 Reid Fleming v. LG Chem Ltd.'."""
        prefix = "Order" if self.kind is ItemKind.ORDER else self.court.value
        num = f" {self.number}" if self.number else ""
        return f"{prefix}{num} {self.case_name}"


# Allowed values of Summary.change_type. "changes" and "clarifies" are substantive and
# appear in the digest's "Rule changes this week" section.
CHANGE_TYPES: tuple[str, ...] = ("changes", "clarifies", "reaffirms", "none", "unclear")
SUBSTANTIVE_CHANGE_TYPES: tuple[str, ...] = ("changes", "clarifies")

# Field order of the JSON object Claude returns (also the digest.json export order).
SUMMARY_FIELDS: tuple[str, ...] = (
    "case_name",
    "opinion_number",
    "court",
    "date",
    "author",
    "one_line",
    "holding",
    "what_changed",
    "change_type",
    "practice_areas",
    "practice_tip",
    "disposition",
)


@dataclass
class Summary:
    """The structured summary of one opinion or order (validated Claude output)."""

    case_name: str
    opinion_number: str
    court: str
    date: str
    author: str
    one_line: str
    holding: str
    what_changed: str
    change_type: str  # one of CHANGE_TYPES
    practice_areas: list[str]
    practice_tip: str
    disposition: str

    @property
    def is_substantive(self) -> bool:
        return self.change_type in SUBSTANTIVE_CHANGE_TYPES

    def to_dict(self) -> dict:
        return {name: getattr(self, name) for name in SUMMARY_FIELDS}


@dataclass
class OpinionHeader:
    """Facts parsed from the first page of an opinion PDF (see extract.parse_opinion_header).

    Every field is None when it cannot be found; parsing never raises.
    """

    opinion_number: str | None = None  # "28355"
    filed_date: date | None = None  # from "... Filed November 12, 2026"
    heard_or_submitted: str | None = None  # "Heard September 16, 2026" / "Submitted October 16, 2026"
    disposition: str | None = None  # the all-caps line after "Filed", e.g. "REVERSED", "PUBLIC REPRIMAND"
    author: str | None = None  # "Justice Hill", "Chief Justice Kittredge", "Per Curiam", "Judge Konduros"
    appellate_case_no: str | None = None  # "2025-002086"


@dataclass
class PreparedText:
    """Opinion text sized for one Claude request (see extract.prepare_for_model)."""

    text: str
    truncated: bool
    kept_fraction: float  # 1.0 when nothing was cut
    estimated_tokens: int  # rough estimate of the text's input tokens
    note: str | None = None  # reader-facing note when truncated, e.g. "based on about 70% of the text"


class ResultStatus(str, enum.Enum):
    SUMMARIZED = "summarized"
    FAILED = "failed"


@dataclass
class Usage:
    """Token usage and estimated cost of the API calls made for one item (or a whole run)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    requests: int = 0
    cost_usd: float | None = 0.0  # None if any call used a model with no price in config

    def __add__(self, other: Usage) -> Usage:
        cost = None if self.cost_usd is None or other.cost_usd is None else self.cost_usd + other.cost_usd
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens + other.cache_creation_input_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
            requests=self.requests + other.requests,
            cost_usd=cost,
        )


@dataclass
class SummaryResult:
    """Outcome of summarizing one item."""

    item: Item
    status: ResultStatus
    summary: Summary | None = None
    error: str | None = None  # short reader-safe reason when status is FAILED
    usage: Usage = field(default_factory=Usage)
    model: str | None = None  # model that produced the summary (may differ after a server-side fallback)
    truncated: bool = False  # input was shortened to fit MAX_INPUT_TOKENS_PER_OPINION
    kept_fraction: float = 1.0  # share of the text sent when truncated
    from_cache: bool = False  # reused from the summary cache (no API call)
    mock: bool = False  # produced by the offline mock summarizer (no API call)

    @property
    def ok(self) -> bool:
        return self.status is ResultStatus.SUMMARIZED and self.summary is not None


@dataclass
class AdvanceSheet:
    number: int
    date: date
    pdf_url: str


@dataclass
class ScrapeResult:
    """Everything a source found for the requested window, before state filtering."""

    items: list[Item]
    source: str
    advance_sheets: list[AdvanceSheet] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Advance sheets whose PDF was read and parsed, so their orders were really checked
    # (SOURCE=sccourts with INCLUDE_ORDERS). ``advance_sheets`` also lists sheets that failed.
    checked_advance_sheets: list[AdvanceSheet] = field(default_factory=list)
