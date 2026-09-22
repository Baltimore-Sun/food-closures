#!/usr/bin/env python3
"""
scrape_closures.py

Scrapes the "Recent Food Establishment Closures" table from the Baltimore
City Health Department website and writes it to a CSV file.

Source page:
  https://www.baltimorecity.gov/health/our-work/permits-regulations/public-health/recent-food-establishment-closures

The page can contain more than one <table>, and the page's markup/wording
can change over time. To reliably find the correct table, we don't assume
it's the first (or only) table on the page -- instead we look for a
<table> whose <th> headers include (case-insensitively) both:
  - "Reason for Closure"
  - "Date of Closure"

By default, only rows whose "Date of Closure" falls within the last N days
(14 by default) relative to when the script is run are kept. Pass
--mode full to scrape every row in the table instead.

Usage:
    python scrape_closures.py                       # last 14 days -> closures_recent.csv
    python scrape_closures.py --mode full            # entire table -> closures_full.csv
    python scrape_closures.py --days 30 --output out.csv
    python scrape_closures.py --mode full --output all_closures.csv --verbose

Exit codes:
    0  success
    1  could not fetch the page
    2  could not find the target table on the page
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

import requests
from bs4 import BeautifulSoup

SOURCE_URL = (
    "https://www.baltimorecity.gov/health/our-work/permits-regulations/"
    "public-health/recent-food-establishment-closures"
)

# Headers that must all be present (case-insensitive substring match) on a
# <table> for it to be considered "the" closures table.
REQUIRED_HEADER_MARKERS = ("reason for closure", "date of closure")

# The column we filter on for the "recent" mode.
DATE_COLUMN_MARKER = "date of closure"

DEFAULT_WINDOW_DAYS = 14
REQUEST_TIMEOUT_SECONDS = 30
REQUEST_HEADERS = {
    # A plain, honest UA string. Some municipal sites reject requests with
    # no User-Agent at all.
    "User-Agent": (
        "Mozilla/5.0 (compatible; BaltimoreFoodClosuresScraper/1.0; "
        "+https://github.com/)"
    )
}

log = logging.getLogger("scrape_closures")


class ScraperError(RuntimeError):
    """Base class for expected, reportable failures in this script."""


class FetchError(ScraperError):
    pass


class TableNotFoundError(ScraperError):
    pass


@dataclass
class ClosureRow:
    """One row of the closures table, plus a parsed version of its date."""

    values: dict[str, str]  # header -> cell text, in column order
    date_of_closure_raw: str
    date_of_closure: Optional[date]


class _WarningCollector(logging.Handler):
    """
    Collects formatted WARNING+ log records emitted during a run, so they
    can be written out to a companion .txt file alongside the CSV (e.g.
    unparseable dates, ragged rows). Errors that abort the run entirely
    (fetch failure, table not found) are not part of this -- those already
    stop the script with a non-zero exit code and a logged error.
    """

    def __init__(self, level: int = logging.WARNING) -> None:
        super().__init__(level=level)
        self.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
            )
        )
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(self.format(record))


def fetch_html(url: str = SOURCE_URL, timeout: int = REQUEST_TIMEOUT_SECONDS) -> str:
    """Download the page HTML. Raises FetchError on any failure."""
    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise FetchError(f"Failed to fetch {url}: {exc}") from exc

    if not resp.text or "<table" not in resp.text.lower():
        # Not fatal by itself (find_target_table will raise properly), but
        # worth flagging early since it usually means we got a blocked /
        # interstitial / error page instead of the real content.
        log.warning("Fetched page does not appear to contain any <table>.")

    return resp.text


def _clean_header_text(th) -> str:
    # Headers can contain a leading <br> (as on this page: "<br> **Name**"),
    # bold tags, non-breaking spaces, etc. get_text() with a separator
    # handles the <br>, and we collapse/trim whitespace afterwards.
    text = th.get_text(separator=" ", strip=True)
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def find_target_table(soup: BeautifulSoup):
    """
    Return the <table> tag whose headers match REQUIRED_HEADER_MARKERS.

    Raises TableNotFoundError if no such table exists, so that a future
    redesign of the page (headers renamed, table removed, etc.) fails
    loudly instead of silently scraping the wrong data.
    """
    tables = soup.find_all("table")
    log.info("Found %d <table> element(s) on the page.", len(tables))

    for idx, table in enumerate(tables):
        headers = [_clean_header_text(th) for th in table.find_all("th")]
        headers_lower = [h.lower() for h in headers]
        joined = " | ".join(headers_lower)

        if all(marker in joined for marker in REQUIRED_HEADER_MARKERS):
            log.info("Table #%d matches: headers = %s", idx, headers)
            return table
        else:
            log.debug("Table #%d does not match: headers = %s", idx, headers)

    raise TableNotFoundError(
        "No <table> on the page had <th> headers containing both "
        f"{REQUIRED_HEADER_MARKERS!r}. The page structure may have changed."
    )


# Baltimore City publishes dates as M/D/YYYY (sometimes zero-padded,
# sometimes not). We try a small set of plausible formats.
_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y")


def parse_date_cell(raw: str) -> Optional[date]:
    """
    Parse a "Date of Closure" cell into a date object.

    Returns None (rather than raising) if the value is blank or doesn't
    match any known format -- the source data is hand-entered and
    occasionally has typos (e.g. a 3-digit year). Callers should treat
    None as "unparseable" and decide how to handle it; we never guess at
    a corrupted date.
    """
    text = raw.strip()
    if not text:
        return None

    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    log.warning("Could not parse Date of Closure value: %r", raw)
    return None


def parse_table(table) -> tuple[list[str], list[ClosureRow]]:
    """
    Parse the matched <table> into (headers, rows).

    Handles a header row built from <th> cells (whether it's inside
    <thead> or just the first <tr>), and body rows from <tr><td>.
    """
    all_th_rows = table.find_all("tr")
    if not all_th_rows:
        raise TableNotFoundError("Matched table has no <tr> rows at all.")

    # Header row = the first <tr> that contains at least one <th>.
    header_tr = None
    for tr in all_th_rows:
        if tr.find("th") is not None:
            header_tr = tr
            break
    if header_tr is None:
        raise TableNotFoundError("Matched table has no header (<th>) row.")

    headers = [_clean_header_text(th) for th in header_tr.find_all("th")]
    headers_lower = [h.lower() for h in headers]

    try:
        date_col_idx = next(
            i for i, h in enumerate(headers_lower) if DATE_COLUMN_MARKER in h
        )
    except StopIteration:
        raise TableNotFoundError(
            f"Matched table's headers {headers!r} unexpectedly lack a "
            f"'{DATE_COLUMN_MARKER}' column."
        )

    rows: list[ClosureRow] = []
    body_trs = [tr for tr in all_th_rows if tr is not header_tr and tr.find_all("td")]

    for tr in body_trs:
        cells = tr.find_all("td")
        if not cells:
            continue

        cell_texts = [
            re.sub(r"\s+", " ", td.get_text(separator=" ", strip=True)).strip()
            for td in cells
        ]

        # Defensive: pad/truncate to header length in case of a ragged row
        # (merged cells, stray row, etc.) rather than crashing the run.
        if len(cell_texts) < len(headers):
            log.warning(
                "Row has %d cell(s) but table has %d header(s); padding: %r",
                len(cell_texts), len(headers), cell_texts,
            )
            cell_texts = cell_texts + [""] * (len(headers) - len(cell_texts))
        elif len(cell_texts) > len(headers):
            log.warning(
                "Row has %d cell(s) but table has %d header(s); truncating: %r",
                len(cell_texts), len(headers), cell_texts,
            )
            cell_texts = cell_texts[: len(headers)]

        values = dict(zip(headers, cell_texts))
        raw_date = cell_texts[date_col_idx]

        rows.append(
            ClosureRow(
                values=values,
                date_of_closure_raw=raw_date,
                date_of_closure=parse_date_cell(raw_date),
            )
        )

    log.info("Parsed %d data row(s) from the table.", len(rows))
    return headers, rows


def filter_recent(
    rows: list[ClosureRow],
    days: int = DEFAULT_WINDOW_DAYS,
    reference_date: Optional[date] = None,
) -> list[ClosureRow]:
    """
    Keep rows where Date of Closure is within the last `days` days,
    inclusive, relative to reference_date (defaults to today).

    Rows with an unparseable date are excluded and logged, since we can't
    verify their recency. The full table (--mode full) is unaffected by
    this function.
    """
    ref = reference_date or date.today()
    cutoff = ref - timedelta(days=days)

    kept = []
    skipped_unparsed = 0
    for row in rows:
        if row.date_of_closure is None:
            skipped_unparsed += 1
            continue
        if cutoff <= row.date_of_closure <= ref:
            kept.append(row)

    if skipped_unparsed:
        log.warning(
            "Skipped %d row(s) with an unparseable Date of Closure while "
            "filtering for the last %d day(s).",
            skipped_unparsed, days,
        )
    log.info(
        "Kept %d of %d row(s): Date of Closure between %s and %s (inclusive).",
        len(kept), len(rows), cutoff.isoformat(), ref.isoformat(),
    )
    return kept


def write_csv(headers: list[str], rows: list[ClosureRow], output_path: str) -> None:
    parent_dir = os.path.dirname(output_path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.values)
    log.info("Wrote %d row(s) to %s", len(rows), output_path)


def _default_warnings_path(csv_output_path: str) -> str:
    root, _ext = os.path.splitext(csv_output_path)
    return f"{root}_warnings.txt"


def write_warnings_file(
    records: list[str], path: str, mode: str, days: int, source_url: str
) -> None:
    """
    Write a plain-text summary of any WARNING+ messages from the run (e.g.
    unparseable dates, ragged rows) to `path`, overwriting it each time --
    this is a rolling snapshot describing the most recent run, not a log
    that accumulates. Always writes the file, even when there were no
    warnings, so its absence never has to be interpreted as "no warnings
    checked yet" vs. "not run".
    """
    parent_dir = os.path.dirname(path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)

    lines = [
        f"Scrape run: {datetime.now().isoformat(timespec='seconds')}",
        f"Source: {source_url}",
        f"Mode: {mode}" + (f" (days={days})" if mode == "recent" else ""),
        "",
    ]
    if records:
        lines.append(f"{len(records)} warning(s):")
        lines.extend(records)
    else:
        lines.append("No warnings for this run.")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    log.info("Wrote warnings file to %s", path)


def run(
    mode: str = "recent",
    days: int = DEFAULT_WINDOW_DAYS,
    output_path: Optional[str] = None,
    url: str = SOURCE_URL,
    reference_date: Optional[date] = None,
    warnings_output_path: Optional[str] = None,
) -> str:
    """
    Orchestrates fetch -> find table -> parse -> (optionally filter) -> write.
    Returns the output path used. Raises ScraperError subclasses on failure.

    On success, also writes a companion .txt file listing any WARNING-level
    messages logged during the run (unparseable dates, ragged rows, etc.).
    On failure (fetch error / table not found), no CSV or warnings file is
    written -- the run already stops with a logged error and non-zero exit.
    """
    if output_path is None:
        output_path = "closures_full.csv" if mode == "full" else "closures_recent.csv"
    if warnings_output_path is None:
        warnings_output_path = _default_warnings_path(output_path)

    collector = _WarningCollector()
    log.addHandler(collector)
    try:
        html = fetch_html(url)
        soup = BeautifulSoup(html, "html.parser")
        table = find_target_table(soup)
        headers, rows = parse_table(table)

        if mode == "full":
            selected = rows
        else:
            selected = filter_recent(rows, days=days, reference_date=reference_date)

        write_csv(headers, selected, output_path)
    finally:
        log.removeHandler(collector)

    write_warnings_file(
        collector.records, warnings_output_path, mode=mode, days=days, source_url=url
    )
    return output_path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scrape Baltimore City food establishment closures."
    )
    parser.add_argument(
        "--mode",
        choices=("recent", "full"),
        default="recent",
        help="'recent' (default) keeps only rows within --days of today; "
             "'full' keeps the entire table.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_WINDOW_DAYS,
        help=f"Window size in days for --mode recent (default: {DEFAULT_WINDOW_DAYS}).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output CSV path. Defaults to closures_recent.csv or "
             "closures_full.csv depending on --mode.",
    )
    parser.add_argument(
        "--warnings-output",
        default=None,
        help="Path for the warnings .txt file. Defaults to <output>, with "
             "its extension replaced by '_warnings.txt' (e.g. "
             "data/closures.csv -> data/closures_warnings.txt).",
    )
    parser.add_argument(
        "--url",
        default=SOURCE_URL,
        help="Override the source URL (mainly useful for testing).",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        output_path = run(
            mode=args.mode,
            days=args.days,
            output_path=args.output,
            url=args.url,
            warnings_output_path=args.warnings_output,
        )
    except FetchError as exc:
        log.error(str(exc))
        return 1
    except TableNotFoundError as exc:
        log.error(str(exc))
        return 2

    print(output_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
