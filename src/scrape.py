"""Find this run's opinions (and, with SOURCE=sccourts, orders) from the configured source.

    find_items(settings, session, today) -> ScrapeResult

The lookback window is ``[today - LOOKBACK_DAYS, today]``. The source does the reading
(see src/sources/); this module picks the source, drops anything dated after ``today``,
removes duplicates, and logs what was found per court.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from . import extract
from .config import ConfigError, Settings
from .models import Court, Item, ItemKind, ScrapeResult
from .net import PoliteSession
from .sources import courtlistener, sccourts

log = logging.getLogger(__name__)

ORDERS_NEED_SCCOURTS = (
    "INCLUDE_ORDERS is on, but Supreme Court orders and rule amendments are only available with "
    "SOURCE=sccourts (CourtListener lists opinions only), so this run covers opinions."
)
NO_COURTS = (
    "INCLUDE_SUPREME_COURT and INCLUDE_COURT_OF_APPEALS are both off, so there are no opinions to look for."
)
SCCOURTS_NEEDS_CONTACT = (
    "SOURCE=sccourts needs CONTACT_EMAIL: every request to sccourts.org must carry an email address its "
    "operators can use to reach you. Set the CONTACT_EMAIL Secret (or Variable); see 'Optional: "
    "SOURCE=sccourts' in OPERATING.md."
)


def enabled_courts(settings: Settings) -> list[Court]:
    """The courts this digest covers, Supreme Court first (INCLUDE_SUPREME_COURT / INCLUDE_COURT_OF_APPEALS)."""
    courts: list[Court] = []
    if settings.include_supreme_court:
        courts.append(Court.SUPREME)
    if settings.include_court_of_appeals:
        courts.append(Court.APPEALS)
    return courts


def find_items(settings: Settings, session: PoliteSession, today: date) -> ScrapeResult:
    """Everything the configured source lists for the lookback window, before state filtering.

    Raises SourceError (or LayoutChangedError) when the source cannot be read, and
    ConfigError for an unknown SOURCE or for SOURCE=sccourts without CONTACT_EMAIL (checked
    here, after command-line overrides, and before any request).
    """
    filed_after = today - timedelta(days=settings.lookback_days)
    courts = enabled_courts(settings)
    source = (settings.source or "").lower()
    log.info(
        "Looking for items filed from %s through %s (LOOKBACK_DAYS=%d) using SOURCE=%s.",
        _fmt(filed_after),
        _fmt(today),
        settings.lookback_days,
        source,
    )

    if source == "sccourts":
        if not courts and not settings.include_orders:
            log.warning("%s", NO_COURTS)
            return ScrapeResult(items=[], source=sccourts.SOURCE)
        if not settings.contact_email:
            raise ConfigError(SCCOURTS_NEEDS_CONTACT)
        result = sccourts.fetch(
            session,
            courts,
            filed_after,
            settings.include_orders,
            pdf_to_pages=extract.pdf_to_layout_pages,  # keeps page numbers that wrapped onto their own line
            today=today,
        )
        # The sccourts source returns its warnings without logging them (CourtListener logs its own).
        for warning in result.warnings:
            log.warning("%s", warning)
    elif source == "courtlistener":
        if settings.include_orders:
            log.info("%s", ORDERS_NEED_SCCOURTS)
        if not courts:
            log.warning("%s", NO_COURTS)
            token = (settings.courtlistener_api_token or "").strip()
            return ScrapeResult(items=[], source=courtlistener.SOURCE_API if token else courtlistener.SOURCE_FEED)
        result = courtlistener.fetch(session, courts, filed_after, settings.courtlistener_api_token)
    else:
        raise ConfigError(f"SOURCE must be courtlistener or sccourts (got {settings.source!r})")

    items: list[Item] = []
    seen: set[str] = set()
    for item in result.items:
        if item.filed_date > today:
            log.info("Leaving out %s: it is dated %s, after %s.", item.label, _fmt(item.filed_date), _fmt(today))
            continue
        key = item.key
        if key in seen:
            log.debug("Leaving out a duplicate of %s.", item.label)
            continue
        seen.add(key)
        items.append(item)

    _log_found(items, courts, source == "sccourts" and settings.include_orders)
    return ScrapeResult(
        items=items,
        source=result.source,
        advance_sheets=list(result.advance_sheets),
        warnings=list(result.warnings),
        checked_advance_sheets=list(result.checked_advance_sheets),
    )


def _log_found(items: list[Item], courts: list[Court], orders_checked: bool) -> None:
    parts = [
        f"{sum(1 for i in items if i.kind is ItemKind.OPINION and i.court is court)} {court.display_name} opinion(s)"
        for court in courts
    ]
    orders = sum(1 for i in items if i.kind is ItemKind.ORDER)
    if orders or orders_checked:
        parts.append(f"{orders} Supreme Court order(s)")
    log.info("Found %d item(s): %s.", len(items), ", ".join(parts) or "none")
    for item in items:
        log.info("  %s (filed %s)", item.label, _fmt(item.filed_date))


def _fmt(d: date) -> str:
    return f"{d:%B} {d.day}, {d.year}"
