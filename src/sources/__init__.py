"""Opinion sources. Each module exposes a ``fetch(...) -> ScrapeResult`` function.

- ``courtlistener``: the default. Free Law Project's CourtListener API (with a free
  token) or its public Atom feeds (no token). Never contacts sccourts.org.
- ``sccourts``: the South Carolina Judicial Branch website directly, including the
  weekly advance sheet's orders and rule amendments. Off unless ``SOURCE=sccourts``,
  because sccourts.org's robots.txt disallows automated agents.
"""


class SourceError(RuntimeError):
    """A source could not be read (network failure, bad token, or changed page layout)."""


class LayoutChangedError(SourceError):
    """The page no longer has the structure the parser expects (the site was redesigned)."""
