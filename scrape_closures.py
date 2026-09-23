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

Geocoding: each row's Address is geocoded via the ArcGIS World Geocoding
Service, and the resulting coordinates are spatially joined against
Baltimore's official Neighborhood Statistical Areas layer to populate a
Neighborhood column. This requires an ArcGIS API key with the Geocoding
privilege enabled -- pass --arcgis-api-key or set the ARCGIS_API_KEY
environment variable. Pass --no-geocode to skip this entirely.

Exit codes:
    0  success
    1  could not fetch the page
    2  could not find the target table on the page
    3  geocoding was requested but no ArcGIS API key was available
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

import requests
from bs4 import BeautifulSoup

SOURCE_URL = (
    "https://www.baltimorecity.gov/health/our-work/permits-regulations/"
    "public-health/recent-food-hment-closures"
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

# All scraped addresses are within Baltimore City -- appended to each
# address before geocoding, since the source table only gives street +
# zip (e.g. "2334 N. Charles St., 21218").
GEOCODE_LOCALITY_SUFFIX = "Baltimore, Maryland, USA"

# Current documented ArcGIS World Geocoding Service endpoint. Requests
# are sent as POST so the API key/token stays out of the request URL
# (and therefore out of any URL that might get logged).
ARCGIS_GEOCODE_URL = (
    "https://geocode-api.arcgis.com/arcgis/rest/services/World/GeocodeServer/"
    "findAddressCandidates"
)

# Baltimore City's official Neighborhood Statistical Areas layer (public,
# no API key required). Field "Name" holds the neighborhood name.
NEIGHBORHOOD_LAYER_QUERY_URL = (
    "https://services1.arcgis.com/mVFRs7NF4iFitgbY/ArcGIS/rest/services/"
    "DataPoints/FeatureServer/38/query"
)

# Small pause between rows' geocoding requests, out of courtesy to both
# services -- at the volumes this script deals with (well under 20
# addresses per run) this adds a few seconds at most.
GEOCODE_REQUEST_DELAY_SECONDS = 0.2

log = logging.getLogger("scrape_closures")


class ScraperError(RuntimeError):
    """Base class for expected, reportable failures in this script."""


class FetchError(ScraperError):
    pass


class TableNotFoundError(ScraperError):
    pass


class GeocodingConfigError(ScraperError):
    pass


class _SecretRedactingFilter(logging.Filter):
    """
    Defense-in-depth: strips a known secret value out of any log message
    before it's emitted, in case something downstream (an exception's
    string form, a library's own debug logging, etc.) ever includes it.
    We also avoid ever putting the API key in a request URL in the first
    place (see geocode_address), so this should normally have nothing to
    redact.
    """

    def __init__(self, secret: Optional[str]) -> None:
        super().__init__()
        self._secret = secret

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secret:
            return True
        if self._secret in str(record.msg):
            record.msg = str(record.msg).replace(self._secret, "***REDACTED***")
        if record.args:
            record.args = tuple(
                arg.replace(self._secret, "***REDACTED***")
                if isinstance(arg, str) and self._secret in arg
                else arg
                for arg in record.args
            )
        return True


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
    Parse a "Date of Closure" cell into a date object using known formats
    only -- no inference from other rows. Returns None if the value is
    blank or doesn't match any known format.

    This intentionally does NOT log a warning on failure: the caller
    (parse_table) first tries to recover the date from surrounding rows
    before deciding whether it's truly unparseable and warning about it.
    """
    text = raw.strip()
    if not text:
        return None

    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    return None


def _extract_day_component(raw: str) -> Optional[int]:
    """
    Try to read just the day-of-month token from a raw M/D/Y-shaped date
    string, without validating the month or year (those may be the
    corrupted part). Returns None if the string isn't in a recognizable
    M/D/Y shape, or if the day token itself isn't a legible 1-31 number --
    per the "don't guess an unreadable day" rule, that case is a hard
    failure for the caller, not something context can fix.
    """
    parts = raw.strip().split("/")
    if len(parts) != 3:
        return None

    day_str = parts[1].strip()
    if not day_str.isdigit():
        return None

    day = int(day_str)
    if not (1 <= day <= 31):
        return None

    return day


def _infer_date_from_neighbors(
    raw: str, prev_date: Optional[date], next_date: Optional[date]
) -> Optional[date]:
    """
    Recover a date that failed normal parsing, using the fact that the
    source table is in chronological order by Date of Closure.

    Only applies when:
      - the day-of-month is legibly readable from `raw` (see
        _extract_day_component -- if not, we never guess), AND
      - both the immediately preceding and following rows have a known
        date, AND those two dates share the same month and year (i.e.
        this row sits inside a same-month-and-year run).

    In that case we assume this row's month and year match its
    neighbors', and combine that with the legible day. Returns None
    (meaning: still unparseable) if any of that doesn't hold, or if the
    resulting month/day/year combination isn't a real calendar date.
    """
    day = _extract_day_component(raw)
    if day is None:
        return None

    if prev_date is None or next_date is None:
        return None
    if prev_date.year != next_date.year or prev_date.month != next_date.month:
        return None

    try:
        return date(year=next_date.year, month=next_date.month, day=day)
    except ValueError:
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

    # First pass: collect raw cell text per row, and a "strict" parse of
    # the Date of Closure (no cross-row inference yet -- we need every
    # row's strict result available before we can look at neighbors).
    parsed_rows: list[tuple[dict[str, str], str, Optional[date]]] = []
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
        parsed_rows.append((values, raw_date, parse_date_cell(raw_date)))

    # Second pass: for any row whose date didn't parse normally, try to
    # recover it from its immediate neighbors (the table is chronological
    # by Date of Closure -- see _infer_date_from_neighbors). Only now do
    # we log, since only now do we know whether it was truly unparseable
    # or successfully recovered from context.
    rows: list[ClosureRow] = []
    n = len(parsed_rows)
    for i, (values, raw_date, strict_date) in enumerate(parsed_rows):
        final_date = strict_date

        if final_date is None:
            prev_date = parsed_rows[i - 1][2] if i > 0 else None
            next_date = parsed_rows[i + 1][2] if i < n - 1 else None
            inferred = _infer_date_from_neighbors(raw_date, prev_date, next_date)

            if inferred is not None:
                log.warning(
                    "Date of Closure %r did not parse; inferred %s from "
                    "surrounding rows (prev=%s, next=%s), which share the "
                    "same month and year.",
                    raw_date, inferred.isoformat(),
                    prev_date.isoformat() if prev_date else None,
                    next_date.isoformat() if next_date else None,
                )
                final_date = inferred
            else:
                log.warning("Could not parse Date of Closure value: %r", raw_date)

        rows.append(
            ClosureRow(
                values=values,
                date_of_closure_raw=raw_date,
                date_of_closure=final_date,
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


# Display-name renames applied to the final CSV only, after every other
# step (table matching, date-column detection, geocoding's "Address"
# column lookup) has already used the original scraped header text --
# none of that logic needs to know about these renames.
COLUMN_RENAMES: dict[str, str] = {
    "Name": "Establishment",
    "Reason for Closure": "Closure reason",
    "Date of Closure": "Closure date",
    "Date Approved to Re-open": "Date approved to re-open",
}


def apply_column_renames(headers: list[str], rows: list[ClosureRow]) -> list[str]:
    """
    Rename output columns per COLUMN_RENAMES, in place on `rows` and
    returned as a new header list. A no-op for any header not listed
    (e.g. "Address", "Neighborhood", "Latitude", etc. are left as-is).
    """
    new_headers = [COLUMN_RENAMES.get(h, h) for h in headers]
    if new_headers == headers:
        return headers  # nothing matched; skip rewriting every row's dict

    for row in rows:
        for old_name, new_name in COLUMN_RENAMES.items():
            if old_name in row.values:
                row.values[new_name] = row.values.pop(old_name)

    return new_headers


def _find_column(headers: list[str], marker: str) -> Optional[int]:
    """Return the index of the first header containing `marker` (case-insensitive)."""
    marker_lower = marker.lower()
    for i, h in enumerate(headers):
        if marker_lower in h.lower():
            return i
    return None


def geocode_address(
    address: str, api_key: str, timeout: int = REQUEST_TIMEOUT_SECONDS
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Geocode a single address via the ArcGIS World Geocoding Service.
    Returns (latitude, longitude, score) -- score is 0-100, ArcGIS's own
    match-confidence value. Returns (None, None, None) on any failure or
    no-match, with a warning logged (never raises, so one bad address
    doesn't abort the whole run).

    Sent as POST with the key in the body, not the URL, so it never ends
    up in a logged request line.
    """
    payload = {
        "f": "json",
        "singleLine": address,
        "outFields": "Score",
        "maxLocations": 1,
        "token": api_key,
    }
    try:
        resp = requests.post(ARCGIS_GEOCODE_URL, data=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        log.warning("Geocoding request failed for address %r: %s", address, exc)
        return None, None, None

    if isinstance(data, dict) and "error" in data:
        err = data["error"]
        log.warning(
            "ArcGIS geocoding API error for address %r: %s",
            address, err.get("message", err) if isinstance(err, dict) else err,
        )
        return None, None, None

    candidates = data.get("candidates") or []
    if not candidates:
        log.warning("No geocoding match found for address: %r", address)
        return None, None, None

    best = candidates[0]
    location = best.get("location") or {}
    lat = location.get("y")
    lon = location.get("x")
    score = best.get("score")

    if lat is None or lon is None:
        log.warning("Geocoding candidate for %r had no usable location: %r", address, best)
        return None, None, None

    return float(lat), float(lon), (float(score) if score is not None else None)


def lookup_neighborhood(
    lat: float, lon: float, timeout: int = REQUEST_TIMEOUT_SECONDS
) -> Optional[str]:
    """
    Spatially join a (lat, lon) point against Baltimore's official
    Neighborhood Statistical Areas layer and return the neighborhood
    Name, or None if the lookup fails or the point falls outside every
    polygon (e.g. an address just outside city limits). No API key
    needed -- this is a public feature service.
    """
    params = {
        "f": "json",
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "Name",
        "returnGeometry": "false",
    }
    try:
        resp = requests.get(NEIGHBORHOOD_LAYER_QUERY_URL, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        log.warning("Neighborhood lookup failed for (%s, %s): %s", lat, lon, exc)
        return None

    if isinstance(data, dict) and "error" in data:
        err = data["error"]
        log.warning(
            "Neighborhood layer API error for (%s, %s): %s",
            lat, lon, err.get("message", err) if isinstance(err, dict) else err,
        )
        return None

    features = data.get("features") or []
    if not features:
        log.warning("No neighborhood polygon contains point (%s, %s).", lat, lon)
        return None

    return features[0].get("attributes", {}).get("Name") or None


def enrich_with_geocoding(
    headers: list[str],
    rows: list[ClosureRow],
    api_key: str,
    request_delay_seconds: float = GEOCODE_REQUEST_DELAY_SECONDS,
) -> list[str]:
    """
    For each row, geocode its Address and spatially join the result
    against Baltimore's neighborhood boundaries. Adds/populates, on each
    row's `values` dict, in place:
      - "Neighborhood" (from the boundary layer's Name field)
      - "Latitude", "Longitude" (from the geocoder)
      - "Geocode Confidence" (ArcGIS's 0-100 match score)

    Returns the updated header list: Neighborhood is inserted immediately
    after the Address column; Latitude, Longitude, and Geocode
    Confidence are appended at the end. If no Address column can be
    found at all, logs a warning and returns `headers` unchanged (no
    columns added) rather than failing the whole run.
    """
    address_idx = _find_column(headers, "address")
    if address_idx is None:
        log.warning(
            "Could not find an 'Address' column in headers %r; skipping "
            "geocoding and neighborhood lookup.", headers,
        )
        return headers

    address_col = headers[address_idx]
    new_headers = (
        headers[: address_idx + 1]
        + ["Neighborhood"]
        + headers[address_idx + 1 :]
        + ["Latitude", "Longitude", "Geocode Confidence"]
    )

    for i, row in enumerate(rows):
        row.values.setdefault("Neighborhood", "")
        row.values.setdefault("Latitude", "")
        row.values.setdefault("Longitude", "")
        row.values.setdefault("Geocode Confidence", "")

        address = (row.values.get(address_col) or "").strip()
        if not address:
            log.warning("Row %d has a blank Address; skipping geocoding for it.", i)
            continue

        full_address = f"{address}, {GEOCODE_LOCALITY_SUFFIX}"
        lat, lon, score = geocode_address(full_address, api_key)

        if lat is not None and lon is not None:
            row.values["Latitude"] = f"{lat:.6f}"
            row.values["Longitude"] = f"{lon:.6f}"
            row.values["Geocode Confidence"] = "" if score is None else str(score)

            neighborhood = lookup_neighborhood(lat, lon)
            if neighborhood:
                row.values["Neighborhood"] = neighborhood

        if request_delay_seconds and i < len(rows) - 1:
            time.sleep(request_delay_seconds)

    return new_headers


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
    geocode: bool = True,
    api_key: Optional[str] = None,
) -> str:
    """
    Orchestrates fetch -> find table -> parse -> (optionally filter) ->
    (optionally geocode + neighborhood lookup) -> write.
    Returns the output path used. Raises ScraperError subclasses on failure.

    On success, also writes a companion .txt file listing any WARNING-level
    messages logged during the run (unparseable dates, ragged rows, failed
    geocodes, etc.). On failure (fetch error / table not found / geocoding
    misconfigured), no CSV or warnings file is written -- the run already
    stops with a logged error and non-zero exit.
    """
    if output_path is None:
        output_path = "closures_full.csv" if mode == "full" else "closures_recent.csv"
    if warnings_output_path is None:
        warnings_output_path = _default_warnings_path(output_path)

    if geocode and not api_key:
        raise GeocodingConfigError(
            "Geocoding is enabled but no ArcGIS API key was provided. Pass "
            "--arcgis-api-key, set the ARCGIS_API_KEY environment variable, "
            "or pass --no-geocode to skip geocoding and neighborhood lookup."
        )

    collector = _WarningCollector()
    collector.addFilter(_SecretRedactingFilter(api_key))
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

        if geocode:
            # Only geocode the rows we're actually keeping -- with the
            # "recent" default that's usually well under 20 addresses.
            headers = enrich_with_geocoding(headers, selected, api_key)

        headers = apply_column_renames(headers, selected)

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
        "--arcgis-api-key",
        default=None,
        help="ArcGIS API key (with the Geocoding privilege) for geocoding "
             "and neighborhood lookup. Defaults to the ARCGIS_API_KEY "
             "environment variable.",
    )
    parser.add_argument(
        "--no-geocode",
        action="store_true",
        help="Skip geocoding and neighborhood lookup entirely -- the "
             "Neighborhood/Latitude/Longitude/Geocode Confidence columns "
             "will not be added.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    api_key = args.arcgis_api_key or os.environ.get("ARCGIS_API_KEY")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Defense-in-depth: never let the API key value itself reach a log
    # line, regardless of where in the call stack it might turn up.
    # NOTE: filters must be attached to the *handler*, not a Logger
    # object, to be consulted for records propagating up from child
    # loggers (e.g. "scrape_closures", or urllib3's own debug logging
    # under --verbose) -- a filter on the root Logger itself is only
    # ever consulted for records that originate at the root logger.
    redactor = _SecretRedactingFilter(api_key)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)

    try:
        output_path = run(
            mode=args.mode,
            days=args.days,
            output_path=args.output,
            url=args.url,
            warnings_output_path=args.warnings_output,
            geocode=not args.no_geocode,
            api_key=api_key,
        )
    except FetchError as exc:
        log.error(str(exc))
        return 1
    except TableNotFoundError as exc:
        log.error(str(exc))
        return 2
    except GeocodingConfigError as exc:
        log.error(str(exc))
        return 3

    print(output_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
