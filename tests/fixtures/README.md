# Test fixtures

Saved copies of real pages so the parsers are tested against what the sites
actually serve. The tests never touch the network.

All files were captured on **2026-11-16**. The "most recent week" in these files
is the set of opinions filed **November 12, 2026** (Advance Sheet No. 41).

Several test modules depend on the exact entries of these files (the "Used by"
column). When a source changes its format, save fresh captures under **new** file
names and add tests for them, rather than overwriting these: replacing one means
updating every module that uses it.

## `sccourts/` (South Carolina Judicial Branch, www.sccourts.org)

Captured during a one-time manual survey. sccourts.org's `robots.txt` disallows
automated agents (`User-agent: *` / `Disallow: /`), so the weekly job defaults to
CourtListener and only uses these pages when `SOURCE=sccourts`.

| File | Source URL | Used by |
|---|---|---|
| `sc_published_2026-11.html` | `/opinions-orders/opinions/published-opinions/supreme-court/` (current month view: Nov 2026, two opinions filed Nov 12) | test_sccourts, test_main |
| `coa_published_2026-11.html` | `/opinions-orders/opinions/published-opinions/court-of-appeals/` (Nov 2026: opinion 6170, whose PDF href contains a stray space: `COA/6170 .pdf`) | test_sccourts, test_main |
| `sc_published_2026-10.html` | `.../supreme-court/?term=2026-10` (one date heading) | test_sccourts |
| `coa_published_2026-10.html` | `.../court-of-appeals/?term=2026-10` (two date headings: Oct 21 and Oct 28) | test_sccourts |
| `sc_published_2026-12_empty.html` | `.../supreme-court/?term=2026-12` (empty month: "There are no published opinions for this month available at this time.") | test_sccourts |
| `advance_sheets.html` | `/opinions-orders/advance-sheets/` (Advance Sheets 41 and 40) | test_sccourts, test_main |
| `all_court_orders_2026.html` | `/opinions-orders/court-orders/all-court-orders/` (2026 orders incl. `2026-11-12-01`, the Rule 1.15 RPC amendment) | test_sccourts, test_main |
| `advance_sheet_41_pages.json` | pdfplumber page texts of `/media/opinions/advSheets/no412026.pdf` (43 pages; pages 1-3 are an order printed *before* the cover; CONTENTS on pages 5-10) | test_sccourts, test_main, test_extract, test_summarize |
| `advance_sheet_40_pages.json` | pdfplumber page texts of `/media/opinions/advSheets/no402026.pdf` (7 pages; no published opinions or orders) | test_sccourts, test_extract |

## `courtlistener/` (Free Law Project, www.courtlistener.com)

| File | Notes | Used by |
|---|---|---|
| `feed_sc.xml` | Live capture of the public Atom feed `https://www.courtlistener.com/feed/court/sc/` (20 newest entries; `<category term="Published">`; PDF `enclosure` links on storage.courtlistener.com) | test_courtlistener, test_main, test_scrape |
| `feed_scctapp.xml` | Live capture of `https://www.courtlistener.com/feed/court/scctapp/` (20 entries, 19 unpublished) | test_courtlistener, test_main, test_scrape |
| `search_v4_sample.json` | **Constructed, not captured.** The v4 Search API requires a token, so this file was built from the documented response fields (wiki.free.law v4 search docs) using the real values of this week's opinions. Re-check it against a live response after the first CourtListener API run. Includes one `Unpublished` result to test filtering, and the real `COA/6170 .pdf` space quirk in `download_url`. | test_courtlistener, test_main, test_scrape |

## `pdfs/` and `opinion_texts/`

- `pdfs/sc_28354.pdf`: the real PDF of Op. No. 28354, *In the Matter of Louis David Nettles* (4 pages). Used by test_extract.
- `opinion_texts/*.txt`: pdfplumber text of Op. Nos. 28354, 28355, and 6170 (UTF-8; real en dashes and section signs). Used by test_extract, test_summarize, test_courtlistener, and test_main (which builds its opinion PDFs from them).

CourtListener's stored PDF for 28355 is byte-different from the court's current file:
the court later corrected one counsel's surname. The text is otherwise identical. Digest links
therefore prefer the court's own URL (`download_url`) and give CourtListener's copy as a mirror.
