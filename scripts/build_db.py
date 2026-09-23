#!/usr/bin/env python3
"""Build (or rebuild) the PostgreSQL database from the cached JSON on disk.

Reads organisations.json, downloads/harvest_sources.json and every
dataset file under downloads/, then writes everything into the database.
The server can then answer page requests with fast indexed queries instead
of reading and parsing 50k+ JSON files on every request.

The full dataset JSON is stored in the dataset_json table, so nothing is
lost — the files under downloads/ remain the on-disk cache.

Usage: python scripts/build_db.py
       DATABASE_URL=postgresql://localhost:5432/other python scripts/build_db.py

Phases: wipe, organisations, datasets (batched, parallel file reads),
full-text search, views, metadata, dataset_api, dataset_content_hash.
Indexes are migration-owned (0003) — the build populates, never creates.

Embeddings are a separate step: run scripts/build_embeddings.py after building.
"""

import csv
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

import typer

from scripts.db import connect, database_url

app = typer.Typer(add_completion=False)

# Number of JSON files to read in parallel per batch. Reading many small
# files one-at-a-time is the dominant bottleneck, so we batch them with
# threads to overlap I/O.
READ_BATCH_SIZE = 2000

# Value-distribution table: strings/JSON truncated at this length.
MAX_FIELD_VALUE_LENGTH = 500

DATA_DIR = Path(__file__).resolve().parent.parent / "downloads"
ORGS_FILE = DATA_DIR / "organisations.json"
HARVEST_SOURCES_FILE = DATA_DIR / "harvest_sources.json"
VIEWS_FILE = Path(__file__).resolve().parent.parent / "data" / "datagovuk-pages-2.csv"
DATABASE_URL = database_url()

_WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Temporal coverage normalisation
# ---------------------------------------------------------------------------
def _stringify(v) -> str:
    """Stringify a scalar the way it appears in JSON: booleans lowercase
    ('true'/'false'), integral floats rendered without the trailing '.0',
    everything else str()."""

    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def temporal_val(v):
    """Normalise a temporal coverage value for storage.

    CKAN stores these as either a plain string ("2012-06-09", "point") or
    an array of dates (multiple coverage periods). Blank/empty values →
    None; arrays are joined so the column stays readable and queryable.
    """

    if v is None or v == "":
        return None
    if isinstance(v, list):
        return ", ".join(_stringify(x) for x in v) if v else None
    return _stringify(v)


_YEAR_RE = re.compile(r"\b(1[5-9]\d\d|20\d\d)\b", re.ASCII)


def temporal_year(v):
    """Extract the coverage year from a normalised temporal value. Values are
    messy (ISO dates "2010-04-01", UK dates "31/07/2015", junk like
    "present"/"-"/"19"/"Months") — pull the first 4-digit year in the
    1500-2099 range, else None. re.ASCII keeps \\d and \\b ASCII-only."""

    if not v:
        return None
    m = _YEAR_RE.search(_stringify(v))
    return m.group(1) if m else None


def temporal_periods(from_val, to_val):
    """Reduce normalised temporal from/to values to a list of coverage
    periods, each an [from_year, to_year] pair (either year null). Periods
    are paired up positionally and kept separate so non-contiguous coverage
    (e.g. [1960-1992] and [2000-2016]) can be matched as a union instead
    of collapsing to the first period. Reversed pairs (from > to) are
    swapped. Returns None when neither side yields any years."""

    from_years = [temporal_year(y) for y in _stringify(from_val).split(", ")] if from_val else []
    to_years = [temporal_year(y) for y in _stringify(to_val).split(", ")] if to_val else []
    n = max(len(from_years), len(to_years))
    if not n:
        return None
    periods = []
    for i in range(n):
        # A missing year parses as falsy — guard the length before indexing.
        f = int(from_years[i]) if i < len(from_years) and from_years[i] else None
        t = int(to_years[i]) if i < len(to_years) and to_years[i] else None
        if f is None and t is None:
            continue
        periods.append([t, f] if f is not None and t is not None and f > t else [f, t])
    return periods or None


# Explicit year-range separators: "1838 - 1862", "2019-20", "2009 to 2010",
# en/em-dash variants (\u2013/\u2014 — escaped so the source stays ASCII).
# re.ASCII keeps \d and \b ASCII-only like _YEAR_RE. The end boundary is
# (?!\d) rather than \b so filenames like "2021 - 2023_0300_S3.pdf"
# (reference-number suffixes glued to the year) still parse as a range — \b
# would reject the underscore after "2023".
_RANGE_SEP = r"(?:\s*[-\u2013\u2014]\s*|\s+to\s+)"
_RANGE_RE = re.compile(
    rf"\b(1[5-9]\d\d|20\d\d){_RANGE_SEP}(\d\d|\d{{4}})(?!\d)",
    re.ASCII,
)
# Standalone-year pass for the text outside ranges: the same relaxed end
# boundary, so a year with a suffix glued on ("2023_0300" with no range,
# "2023data") still yields a period. The leading \b stays: "data_2023"
# (underscore before the year) is not a year start.
_STANDALONE_YEAR_RE = re.compile(r"\b(1[5-9]\d\d|20\d\d)(?!\d)", re.ASCII)
# 2-digit end years expand via their century (2019-20 -> 2020); the result
# must land in the same window _YEAR_RE accepts (1500-2099), and 4-digit
# ends like "0300" fail the floor check.
_CENTURY = 100
_YEAR_MIN = 1000
_YEAR_MAX = 2099


def _dedupe_periods(periods: list) -> list:
    """Order-preserving dedupe of [from, to] pairs."""
    seen: set = set()
    out = []
    for p in periods:
        key = tuple(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _text_periods(text) -> list:
    """Extract coverage periods from free text (a dataset title or a
    resource name). Explicit year ranges first — "1838 - 1862", "2019-20"
    (2-digit tail expanded via its century, so 2019-20 → 2019-2020),
    "2009 to 2010" — then standalone years as closed single-year periods
    [y, y]. Reversed ranges are swapped. Deduped, capped at 10 periods."""
    if not text:
        return []
    periods = []
    for m in _RANGE_RE.finditer(text):
        a = int(m.group(1))
        b = int(m.group(2))
        if b < _CENTURY:
            b += (a // _CENTURY) * _CENTURY  # century expansion: 2019-20 -> 2020
        if not _YEAR_MIN <= b <= _YEAR_MAX:
            continue
        periods.append([min(a, b), max(a, b)])
    # Standalone years in the text outside the matched ranges.
    periods.extend([int(m.group(1))] * 2 for m in _STANDALONE_YEAR_RE.finditer(_RANGE_RE.sub(" ", text)))
    return _dedupe_periods(periods)[:10]


def _suggested_periods(ds: dict) -> tuple[list | None, str | None]:
    """Infer coverage periods when the publisher declared none: the dataset
    title first (high confidence), resource names as fallback (noisier,
    lower value — filenames can carry reference numbers). Returns
    (periods, source) with source 'title' or 'resource', or (None, None)
    when nothing is found."""
    periods = _text_periods(ds.get("title"))
    if periods:
        return periods, "title"
    all_periods: list = []
    for r in ds.get("resources") or []:
        all_periods.extend(_text_periods(r.get("name")))
    periods = _dedupe_periods(all_periods)[:10]
    if periods:
        return periods, "resource"
    return None, None


# ---------------------------------------------------------------------------
# Host extraction
# ---------------------------------------------------------------------------
# Loose scheme://host fallback for URLs that defeat urlsplit (unencoded
# spaces, angle brackets, ...): /^[a-z][a-z0-9+.-]*:\/\/([^/?#]+)/i
_SCHEME_HOST_RE = re.compile(r"^[a-z][a-z0-9+.-]*://([^/?#]+)", re.IGNORECASE)
# A real hostname can only contain word chars (letters/digits/underscore),
# dots and hyphens — re.ASCII makes \w ASCII-only.
_BAD_HOST_CHARS = re.compile(r"[^\w.-]", re.ASCII)

# Schemes where WHATWG treats backslashes as slashes and auto-parses the
# authority (http:/host, http:host, http:///host all put host in authority).
_SPECIAL_SCHEMES = {"http", "https", "ws", "wss", "ftp", "file"}
_SCHEME_RE = re.compile(r"^([a-z][a-z0-9+.-]*):", re.IGNORECASE)


def _whatwg_normalize(url: str) -> str:
    """Approximate WHATWG URL host parsing enough for extract_host.

    For special schemes, new URL() treats backslashes as forward slashes and
    enters the authority state after any number of slashes (zero included)
    following the scheme colon — so `http:/host`, `http:host` and
    `http:///host` all yield host `host`. urlsplit requires exactly `//`, so
    normalize those shapes first. The `file` scheme is the exception:
    WHATWG only parses a host after exactly `file://host`, so Windows
    paths like `file:///C:/...` (and `file://` + backslash forms) have an
    empty host (null) — no slash collapsing. Non-special schemes (mailto:,
    etc.) and scheme-less strings are returned unchanged (the fallback
    regex rejects them).
    """

    m = _SCHEME_RE.match(url)
    if not m or m.group(1).lower() not in _SPECIAL_SCHEMES:
        return url
    scheme = m.group(1).lower()
    rest = url[m.end() :]
    if "\\" in rest:
        rest = rest.replace("\\", "/")
    # http/https/ws/wss/ftp: the authority follows any number of slashes
    # (http:/host, http:host, http:///host all put host in authority).
    # file: only parses a host after exactly `file://host` — file:///C:/…
    # and file://\\J:\… have an EMPTY host (the drive letter is part of the
    # path), so don't collapse slashes for it.
    if scheme != "file":
        rest = re.sub(r"^/*", "//", rest)
    return f"{scheme}:{rest}"


def _idna_host(host: str) -> str:
    """WHATWG applies IDNA ToASCII to special-scheme hosts, so unicode hosts
    come out punycoded (e.g. 'Spend-over-£25k…' → 'xn--spend-over-25k…').
    Python's urlsplit leaves the unicode in place; the 'idna' codec produces
    the same form. Raises on invalid IDNA."""

    return host.encode("idna").decode("ascii")


def extract_host(url):
    """Extract a normalised hostname from a resource URL. Strips the leading
    "www." and lowercases so the links report can group by host. Returns
    None for blank/unparseable URLs.

    Uses the WHATWG URL algorithm: try urlsplit (with a pre-normalisation
    for special schemes — backslashes-as-slashes, lenient authority slashes
    — and IDNA punycode for unicode hosts), reject malformed ports, fall
    back to the loose scheme://host regex, then lowercase + strip www. and
    reject hosts containing non-[\\w.-] chars. IPv6 bracket hosts are
    rejected by the bad-char check."""

    if not url or not isinstance(url, str):
        return None
    host = None
    try:
        parts = urlsplit(_whatwg_normalize(url))
        # new URL() throws on non-numeric or out-of-range ports; urlsplit's
        # .port property raises ValueError for the same inputs. hostname
        # alone would silently accept them, so probe the port when present.
        if parts.hostname and ":" in parts.netloc:
            _ = parts.port  # raises ValueError on a malformed port
        host = parts.hostname or None
        # new URL() punycodes unicode hosts (IDNA ToASCII); urlsplit leaves
        # them in place — encode to the same xn-- form.
        if host and not host.isascii():
            host = _idna_host(host)
    except (ValueError, UnicodeError):
        host = None
    if host is None:
        m = _SCHEME_HOST_RE.match(url)
        host = m.group(1) if m else None
    if not host:
        return None
    host = host.lower().removeprefix("www.")
    if _BAD_HOST_CHARS.search(host):
        return None
    return host


# ---------------------------------------------------------------------------
# Format normalisation
# ---------------------------------------------------------------------------
# CKAN's `format` field is free text, so values get mechanical cleaning
# (uppercase, trim, IANA media-type URL → its type, dot strip), then map
# through MIME_TO_NAME / FORMAT_ALIASES. Anything not in the maps keeps its
# cleaned label — rare-but-real formats are left alone rather than hidden.
# Judgment calls behind the aliases:
#  - ArcGIS GeoServices REST API and ESRI variants collapse to ARCGIS REST
#    (ArcGIS Hub's default export label); interactive products (storymaps,
#    experiences) stay separate.
#  - Portal-page labels (websites, webpages) → WEB PAGE; API/SPARQL and
#    DASHBOARD stay separate — "the link isn't a data file" is a finding.
#  - json1.0/json2.0 are NISRA's JSON-stat endpoints, not typos.
#  - Multi-format labels like "CSV / ZIP" are left alone — assigning them
#    to either type is an inference and splitting double-counts.

# MIME types (raw or from IANA media-type URLs) → common names
MIME_TO_NAME = {
    "TEXT/CSV": "CSV",
    "APPLICATION/JSON": "JSON",
    "APPLICATION/LD+JSON": "JSON-LD",
    "APPLICATION/PARQUET": "PARQUET",
    "APPLICATION/ZIP": "ZIP",
    "TEXT/PLAIN": "TXT",
    "TXT/PLAIN": "TXT",
    "APPLICATION/RDF+XML": "RDF",
    "TEXT/N3": "N3",
    "TEXT/TURTLE": "TTL",
    "APPLICATION/GPX+XML": "GPX",
    "APPLICATION/VNDGOOGLE-EARTHKML+XML": "KML",
    "APPLICATION/VNDOPENXMLFORMATS-OFFICEDOCUMENTSPREADSHEETMLSHEET": "XLSX",
    "APPLICATION/OCTET-STREAM": "OCTET-STREAM",
    "APPLICATION/XHTML+XML": "HTML",
    "TEXT/HTML; CHARSET=UTF-8": "HTML",
    "TEXT/RTF": "RTF",
    "APPLICATION/GML+XML": "GML",
    "APPLICATION/GEOPACKAGE+SQLITE3": "GPKG",
    "APPLICATION/X-MSDOS-PROGRAM": "EXE",
    "APPLICATION/X-NETCDF": "NETCDF",
    "APPLICATION/MSACCESS": "MDB",
}

# High-confidence synonyms and deliberate buckets (keys are post-cleaning)
FORMAT_ALIASES = {
    # typos
    "CVS": "CSV",
    "CVC": "CSV",
    "CSV FILE": "CSV",
    "CSV / CSV": "CSV",
    "XLXS": "XLSX",
    "XLX": "XLSX",
    "XSLX": "XLSX",
    "HMTL": "HTML",
    "HML": "HTML",
    "TIF": "TIFF",
    "KMX": "KMZ",
    "EXEL": "XLS",
    "EXCELL": "XLS",
    "GEOPACKAGE": "GPKG",
    "GEOPACKAGES": "GPKG",
    "GEODATABASE": "GDB",
    "SHAPE": "SHP",
    # JSON-stat API endpoints (NISRA) — raw values "json1.0"/"json2.0"
    "JSON10": "JSON-STAT",
    "JSON20": "JSON-STAT",
    # same format, different labels
    "PDF / PDF": "PDF",
    "ZIP / ZIP": "ZIP",
    "WEBMAP": "WEB MAP",
    "POWERBI": "POWER BI",
    "OD / ODS": "ODS",
    "WORD": "DOC",
    "WORD DOC": "DOC",
    "MS WORD": "DOC",
    "POWERPOINT": "PPT",
    "RDFA": "RDF",
    "HTML+RDFA": "RDF",
    "SKOS RDF": "RDF",
    # ArcGIS Hub default export label + synonyms → one REST bucket
    "ARCGIS GEOSERVICES REST API": "ARCGIS REST",
    "ESRI REST": "ARCGIS REST",
    "ESRI GEOSERVICE": "ARCGIS REST",
    "ESRI REST API": "ARCGIS REST",
    "ESRI REST SERVICE": "ARCGIS REST",
    "FEATURE SERVER": "ARCGIS REST",
    "FEATURE SERVICE": "ARCGIS REST",
    "MAP SERVICE": "ARCGIS REST",
    # Crown Commercial Service's standard download label (a ZIP bundle; 100%
    # of this value is one org) → ZIP
    "APPLICATION/ZIP, APPLICATION/OCTET-STREAM, APPLICATION/X-ZIP-COMPRESSED, MULTIPART/X-ZIP": "ZIP",
    # Links to a portal page rather than a data file
    "WEBPAGE": "WEB PAGE",
    "WEBSITE": "WEB PAGE",
    "WEB": "WEB PAGE",
    "WEBLINK": "WEB PAGE",
    "URL": "WEB PAGE",
    "OPEN DATA SITE": "WEB PAGE",
    "OPEN DATA WEBSITE": "WEB PAGE",
    "OPEN DATE SITE": "WEB PAGE",
    "ESRI OPEN DATA SITE": "WEB PAGE",
    "SCHOOL LOCALITIES ON OPEN DATA SITE": "WEB PAGE",
    "WEP PAGE": "WEB PAGE",
    "WEBSITE CONTAINING DATA FILES": "WEB PAGE",
    "HTTP": "WEB PAGE",
    "HTTPS": "WEB PAGE",
    "INFORMATION AND DOWNLOAD": "WEB PAGE",
    "DATA DOWNLOAD": "WEB PAGE",
}

# IANA media-type URL, e.g. https://www.iana.org/assignments/media-types/text/csv
IANA_RE = re.compile(
    r"^https?://www\.iana\.org/assignments/media-types/(.+)$",
    re.IGNORECASE,
)


def normalise_format(raw):
    """Normalise a resource format string for the links facet.
    Returns None for blank."""

    if not raw or not isinstance(raw, str):
        return None
    f = raw.strip()
    if f == "":
        return None

    # IANA media-type URLs carry the MIME type in the URL path — pull it out
    # before the dot-strip mangles the hostname.
    m = IANA_RE.match(f)
    if m:
        f = m.group(1)

    # Mechanical cleaning: uppercase, strip dots, collapse runs of
    # whitespace (so trailing "CSV " folds into "CSV"), and drop the "OGC "
    # prefix from WFS/WMS/WMTS so they share one facet with their bare forms.
    f = f.upper().replace(".", "")
    f = _WS_RE.sub(" ", f).strip()
    if f == "":
        return None
    m = re.match(r"^OGC (WFS|WMS|WMTS)$", f)
    if m:
        f = m.group(1)

    # MIME types → common names (text/csv → CSV, application/zip → ZIP…)
    if f in MIME_TO_NAME:
        return MIME_TO_NAME[f]

    # High-confidence synonyms and deliberate buckets
    if f in FORMAT_ALIASES:
        return FORMAT_ALIASES[f]

    return f


# ---------------------------------------------------------------------------
# Metadata field usage
# ---------------------------------------------------------------------------
def field_value_str(v):
    """Convert a field value to a string for the value-distribution table.
    Long strings/JSON are truncated at 500 chars to keep the index
    reasonable; None, empty string, empty array and empty object all
    collapse to a single "(empty)" bucket so they don't clutter the value
    table as separate rows."""

    if v is None:
        return "(empty)"
    if isinstance(v, str):
        if v == "":
            return "(empty)"
        return v[:MAX_FIELD_VALUE_LENGTH] + "..." if len(v) > MAX_FIELD_VALUE_LENGTH else v
    if isinstance(v, (bool, int, float)):
        return _stringify(v)
    if isinstance(v, list):
        if not v:
            return "(empty)"
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))[:MAX_FIELD_VALUE_LENGTH]
    if isinstance(v, dict):
        s = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
        if s == "{}":
            return "(empty)"
        return s[:MAX_FIELD_VALUE_LENGTH]
    return _stringify(v)


# ---------------------------------------------------------------------------
# Dataset views (data/datagovuk-pages-2.csv — tracked in git)
# ---------------------------------------------------------------------------
# Search Console clicks per page. The CSV has two columns: path (relative,
# e.g. /dataset/<uuid>/<slug>) and clicks. We extract the UUID and sum.

_VIEWS_PATH_RE = re.compile(r"^/dataset/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def load_views_csv() -> dict[str, int]:
    """Read the views CSV and return {dataset_uuid: total_clicks}."""

    if not VIEWS_FILE.exists():
        return {}

    result: dict[str, int] = {}
    with VIEWS_FILE.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            m = _VIEWS_PATH_RE.match(row["path"])
            if not m:
                continue
            clicks = int(row["clicks"])
            if clicks <= 0:
                continue
            uid = m.group(1)
            result[uid] = result.get(uid, 0) + clicks
    return result


# ---------------------------------------------------------------------------
# Wipe — schema is owned by Django migrations; the build only truncates
# the 9 tables it populates (series tables excluded).
# ---------------------------------------------------------------------------

TRUNCATE_SQL = (
    "TRUNCATE TABLE dataset_api, dataset_content_hash, embedding_map, dataset_embeddings, "
    "metadata_values, metadata_keys, links, temporal_periods, dataset_json, datasets, "
    "organisations, harvest_sources CASCADE"
)


# ---------------------------------------------------------------------------
# Dataset load
# ---------------------------------------------------------------------------

# Prepared-statement SQL for the per-batch insert — static strings, so they
# live at module level and _process_batch reads as data-flow, not SQL-plus-
# mapping. The datasets/json statements upsert (the pipeline re-runs are
# idempotent); the links statement is bulk-loaded per batch.
INSERT_DATASET_SQL = """
INSERT INTO datasets
    (id, org_slug, org_display_name, title, name, notes, metadata_created,
     metadata_modified, resource_count, theme_primary,
     harvested, harvest_source_title, harvest_source_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (id) DO UPDATE SET
    org_slug = EXCLUDED.org_slug, org_display_name = EXCLUDED.org_display_name,
    title = EXCLUDED.title, name = EXCLUDED.name, notes = EXCLUDED.notes,
    metadata_created = EXCLUDED.metadata_created,
    metadata_modified = EXCLUDED.metadata_modified,
    resource_count = EXCLUDED.resource_count,
    theme_primary = EXCLUDED.theme_primary,
    harvested = EXCLUDED.harvested,
    harvest_source_title = EXCLUDED.harvest_source_title,
    harvest_source_id = EXCLUDED.harvest_source_id
"""

# One row per coverage period — bulk-loaded per batch like links.
# Upsert: a dataset can appear under multiple orgs in the download set.
INSERT_PERIOD_SQL = """
INSERT INTO temporal_periods
    (dataset_id, position, from_year, to_year, source)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT (dataset_id, position) DO UPDATE SET
    from_year = EXCLUDED.from_year,
    to_year   = EXCLUDED.to_year,
    source    = EXCLUDED.source
"""

INSERT_JSON_SQL = """
INSERT INTO dataset_json (id, json) VALUES (?, ?)
ON CONFLICT (id) DO UPDATE SET json = EXCLUDED.json
"""

INSERT_LINK_SQL = """
INSERT INTO links
    (resource_id, dataset_id, org_slug, org_display_name, dataset_title,
     name, description, url, host, format, format_norm, year_created, created, position)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class _BuildState:
    """Mutable build counters + accumulators shared across batches."""

    def __init__(self) -> None:
        self.count = 0
        self.skipped = 0
        self.fts_rows: list = []
        self.field_counts: dict = {}
        self.value_counts: dict = {}
        self.seen_meta_ids: set = set()


def _read_parse(item: dict) -> dict:
    """Read + parse one dataset file off the main thread. Read or parse
    failure → {'skipped': True}."""

    try:
        raw = item["filepath"].read_text(encoding="utf-8")
    except OSError:
        return {"skipped": True}
    try:
        ds = json.loads(raw)
    except ValueError:
        return {"skipped": True}
    return {"ds": ds, "raw": raw, "orgSlug": item["orgSlug"], "skipped": False}


def _extras(ds: dict) -> dict:
    """The dataset's extras keyed by key (harvest bookkeeping)."""
    return {e["key"]: e["value"] for e in ds.get("extras") or []}


def _dataset_row(ds: dict, extras: dict, org_name, org_display) -> tuple:
    """The 13 VALUES for insert_ds, derived from one dataset dict."""
    return (
        ds.get("id"),
        org_name,
        org_display,
        ds.get("title"),
        ds.get("name"),
        ds.get("notes") or None,
        ds.get("metadata_created"),
        ds.get("metadata_modified"),
        len(ds.get("resources") or []),
        ds.get("theme-primary") or None,
        1 if extras.get("harvest_object_id") else 0,
        extras.get("harvest_source_title") or None,
        extras.get("harvest_source_id") or None,
    )


def _dataset_period_rows(ds: dict) -> list[tuple]:
    """The insert_period rows for one dataset: (dataset_id, position,
    from_year, to_year, source). Declared periods from the publisher's
    temporal_coverage-from/to (source='declared') when they yield any;
    otherwise suggested periods inferred from the title or a resource name
    (source='title'/'resource'). Inference fills gaps only — never
    alongside declared coverage. Empty when neither source yields a year."""
    periods = temporal_periods(
        temporal_val(ds.get("temporal_coverage-from")),
        temporal_val(ds.get("temporal_coverage-to")),
    )
    source: str
    if periods:
        source = "declared"
    else:
        periods, suggested_source = _suggested_periods(ds)
        if not periods:
            return []
        # _suggested_periods returns a source only together with periods
        assert suggested_source is not None
        source = suggested_source
    return [(ds.get("id"), i, p[0], p[1], source) for i, p in enumerate(periods)]


def _link_rows(ds: dict, org_name, org_display, year_created) -> list[tuple]:
    """The insert_link rows, one per resource."""
    rows = []
    for r in ds.get("resources") or []:
        raw_format = r.get("format") or None
        position = r.get("position")
        rows.append(
            (
                r.get("id") or None,
                ds.get("id"),
                org_name,
                org_display,
                ds.get("title"),
                r.get("name") or None,
                r.get("description") or None,
                r.get("url") or None,
                extract_host(r.get("url")),
                raw_format,
                normalise_format(raw_format),
                year_created,
                r.get("created") or None,
                position if isinstance(position, (int, float)) else None,
            ),
        )
    return rows


def _fts_row(ds: dict) -> dict:
    """The fts-row dict for the tsvector column (tags space-joined)."""
    return {
        "id": ds.get("id"),
        "title": _WS_RE.sub(" ", (ds.get("title") or "")).strip(),
        "notes": _WS_RE.sub(" ", (ds.get("notes") or "")).strip(),
        "tags": " ".join(
            t
            for t in (
                _WS_RE.sub(
                    " ",
                    (t.get("display_name") or t.get("name") or ""),
                ).strip()
                for t in ds.get("tags") or []
            )
            if t
        ),
    }


def _meta_counts(ds: dict, st: _BuildState) -> None:
    """Metadata field usage — count each top-level field and extras key so
    the /metadata report can show field adoption across the catalogue.

    Only the first occurrence of each dataset id is counted (duplicate
    files are skipped) so the counts match the deduplicated datasets table
    — that invariant is why this can't fold into _dataset_row.
    """
    if ds.get("id") in st.seen_meta_ids:
        return
    st.seen_meta_ids.add(ds.get("id"))
    for key, value in ds.items():
        if key.startswith("_") or key in {"resources", "extras"}:
            continue
        fk = f"top:{key}"
        fc = st.field_counts.setdefault(fk, {"total": 0, "nonEmpty": 0})
        fc["total"] += 1
        vm = st.value_counts.setdefault(fk, {})
        vs = field_value_str(value)
        vm[vs] = vm.get(vs, 0) + 1
        if vs != "(empty)":
            fc["nonEmpty"] += 1
    seen_extras: set = set()
    for e in ds.get("extras") or []:
        if e["key"] in seen_extras:
            continue
        seen_extras.add(e["key"])
        fk = f"extras:{e['key']}"
        fc = st.field_counts.setdefault(fk, {"total": 0, "nonEmpty": 0})
        fc["total"] += 1
        vm = st.value_counts.setdefault(fk, {})
        vs = field_value_str(e.get("value"))
        vm[vs] = vm.get(vs, 0) + 1
        if vs != "(empty)":
            fc["nonEmpty"] += 1


def _process_batch(db, batch: list[dict], st: _BuildState) -> None:
    """Process files in batches: read each batch in parallel (overlapping
    I/O via threads), parse, then insert in a single transaction."""

    # Phase 1: read & parse the whole batch concurrently. ThreadPoolExecutor
    # preserves input order, so insertion order — and therefore row ids —
    # stay deterministic.
    with ThreadPoolExecutor() as pool:
        parsed = list(pool.map(_read_parse, batch))

    # Phase 2: insert within a pg transaction (single client, BEGIN/COMMIT)
    def _tx(tx) -> None:
        insert_ds = tx.prepare(INSERT_DATASET_SQL)
        insert_json = tx.prepare(INSERT_JSON_SQL)
        insert_link = tx.prepare(INSERT_LINK_SQL)
        insert_period = tx.prepare(INSERT_PERIOD_SQL)

        for item in parsed:
            if item["skipped"]:
                st.skipped += 1
                continue
            ds, raw, org_slug = item["ds"], item["raw"], item["orgSlug"]

            # Shared per-dataset derivations — extras, org-name fallbacks
            # and the created year feed both the dataset row and the links.
            extras = _extras(ds)
            org = ds.get("_organisation") or {}
            org_name = org.get("name") or org_slug
            org_display = org.get("display_name") or org_slug
            year_created = (ds.get("metadata_created") or "")[:4] or None

            insert_ds.run(*_dataset_row(ds, extras, org_name, org_display))
            insert_json.run(ds.get("id"), raw)
            for row in _link_rows(ds, org_name, org_display, year_created):
                insert_link.run(*row)
            # Period rows after the dataset row — the FK needs the parent
            # to exist. Resources are already in the parsed ds, so no
            # reordering was needed to have them here.
            for row in _dataset_period_rows(ds):
                insert_period.run(*row)

            st.fts_rows.append(_fts_row(ds))
            _meta_counts(ds, st)
            st.count += 1

    db.transaction(_tx)


def _load_orgs() -> list:
    """Read downloads/organisations.json — the friendly CLI errors on
    failure are the interface (get-organisations regenerates the file)."""
    print("Reading organisations.json...", file=sys.stderr)
    try:
        return json.loads(ORGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        print(f"Could not read {ORGS_FILE}: {err}", file=sys.stderr)
        print(
            "Run `just get-organisations` first (regenerates it from the CKAN API).",
            file=sys.stderr,
        )
        raise typer.Exit(1) from None


def _load_harvest_sources() -> list:
    """Read downloads/harvest_sources.json — the friendly CLI errors on
    failure are the interface (get-harvest-sources regenerates the file)."""
    print("Reading harvest_sources.json...", file=sys.stderr)
    try:
        return json.loads(HARVEST_SOURCES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        print(f"Could not read {HARVEST_SOURCES_FILE}: {err}", file=sys.stderr)
        print(
            "Run `just get-harvest-sources` first (regenerates it from the CKAN API).",
            file=sys.stderr,
        )
        raise typer.Exit(1) from None


def _collect_files() -> list[dict[str, str | Path]]:
    """All .json dataset files with their org slug. Sorted on both levels so
    the build is deterministic regardless of filesystem directory order (and
    matches the insertion order a table diff expects)."""
    if not DATA_DIR.is_dir():
        print(
            f"No {DATA_DIR}/ directory found — run get_datasets.py first.",
            file=sys.stderr,
        )
        raise typer.Exit(1)

    all_files: list[dict[str, str | Path]] = []
    for org_dir in sorted(DATA_DIR.iterdir(), key=lambda p: p.name):
        if not org_dir.is_dir():
            continue
        all_files.extend(
            {"filepath": f, "orgSlug": org_dir.name}
            for f in sorted(org_dir.iterdir(), key=lambda p: p.name)
            if f.name.endswith(".json")
        )
    return all_files


def _load_organisations_tx(tx, orgs) -> None:
    """Insert/upsert the organisations rows."""
    insert_org = tx.prepare(
        """
        INSERT INTO organisations
            (slug, name, display_name, package_count, type, state, approval_status, created, title, json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (slug) DO UPDATE SET
            name = EXCLUDED.name, display_name = EXCLUDED.display_name,
            package_count = EXCLUDED.package_count, type = EXCLUDED.type,
            state = EXCLUDED.state, approval_status = EXCLUDED.approval_status,
            created = EXCLUDED.created, title = EXCLUDED.title, json = EXCLUDED.json
        """,
    )
    for o in orgs:
        insert_org.run(
            o["name"],
            o["name"],
            o.get("display_name"),
            o.get("package_count"),
            o.get("type"),
            o.get("state"),
            o.get("approval_status"),
            o.get("created"),
            o.get("title"),
            # JSON.stringify(o) — no spaces, raw unicode.
            json.dumps(o, ensure_ascii=False, separators=(",", ":")),
        )


def _load_harvest_sources_tx(tx, sources, org_slug_by_uuid) -> None:
    """Insert/upsert the harvest_sources rows. org_slug_by_uuid maps the
    CKAN organisation UUID (organization_id) to its slug (the
    organisations PK) so the join key is denormalised at build time
    instead of read out of the org record's json column on every query."""
    insert_source = tx.prepare(
        """
        INSERT INTO harvest_sources
            (id, title, url, type, active, frequency, organization_id,
             org_slug, created, json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            title = EXCLUDED.title, url = EXCLUDED.url, type = EXCLUDED.type,
            active = EXCLUDED.active, frequency = EXCLUDED.frequency,
            organization_id = EXCLUDED.organization_id,
            org_slug = EXCLUDED.org_slug, created = EXCLUDED.created,
            json = EXCLUDED.json
        """,
    )
    for s in sources:
        insert_source.run(
            s.get("id"),
            s.get("title") or None,
            s.get("url") or None,
            s.get("type") or None,
            s.get("active"),
            s.get("frequency") or None,
            s.get("organization_id") or None,
            org_slug_by_uuid.get(s.get("organization_id")),
            s.get("created") or None,
            # JSON.stringify(s) — no spaces, raw unicode.
            json.dumps(s, ensure_ascii=False, separators=(",", ":")),
        )


def _populate_fts_tx(tx, fts_rows) -> None:
    """Populate the tags + fts (tsvector) columns. Tags are stored as a
    space-joined string for the suggestions route; fts is a tsvector for the
    "more like this" query (lexical, via tsquery @@)."""
    update_fts = tx.prepare(
        """
        UPDATE datasets
        SET tags = ?, fts = to_tsvector(
            'english',
            coalesce(?, '') || ' ' || coalesce(?, '') || ' ' || coalesce(?, '')
        )
        WHERE id = ?
        """,
    )
    for r in fts_rows:
        update_fts.run(r["tags"], r["title"], r["notes"], r["tags"], r["id"])


def _write_views_tx(tx, views_by_id) -> None:
    """Write the per-dataset view counts."""
    update_views = tx.prepare("UPDATE datasets SET views = ? WHERE id = ?")
    for id_, v in views_by_id.items():
        update_views.run(v, id_)


def _write_meta_tx(tx, field_counts, value_counts) -> int:
    """Write the metadata field/value counters into metadata_keys /
    metadata_values for the /metadata report; returns the distinct-value row
    count."""
    insert_meta_key = tx.prepare(
        "INSERT INTO metadata_keys (key, section, count, non_empty, distinct_values) VALUES (?, ?, ?, ?, ?)",
    )
    insert_meta_val = tx.prepare(
        "INSERT INTO metadata_values (key, value, count) VALUES (?, ?, ?)",
    )
    val_rows = 0
    for fk, fc in field_counts.items():
        section = "extras" if fk.startswith("extras:") else "top"
        vm = value_counts.get(fk)
        distinct = len(vm) if vm else 0
        insert_meta_key.run(fk, section, fc["total"], fc["nonEmpty"], distinct)
        if vm:
            for val, vc in vm.items():
                insert_meta_val.run(fk, val, vc)
                val_rows += 1
    return val_rows


# ---------------------------------------------------------------------------
# dataset_api summary table
# ---------------------------------------------------------------------------
# API detection SQL for the build-time snapshot — single % for ILIKE (no
# psycopg3 params; db.exec skips placeholder parsing). Keep in sync with
# explorer/queries/reports.py (_API_SIGNAL_SQL / _API_TYPE_CASE).

_BUILD_API_SIGNAL = (
    "l.format_norm ILIKE '%arcgis rest%'"
    " OR l.format_norm ILIKE '%wms%'"
    " OR l.format_norm ILIKE '%wfs%'"
    " OR l.format_norm ILIKE '%ogc api%'"
    " OR (l.format_norm ILIKE '%api%' AND l.format_norm NOT ILIKE '%mapinfo%')"
    " OR l.format_norm ILIKE '%sparql%'"
    " OR l.format_norm = 'CSW'"
    " OR l.format_norm ILIKE '%georss%'"
    r" OR l.url ~ '/rest/services/'"
    r" OR l.url ~* '\?service='"
    r" OR l.url ~* '\?request='"
    r" OR l.url ~* '\?f=json'"
    r" OR l.url ~ 'ogcapi'"
    r" OR l.url ~* '/wms(\?|$)'"
    r" OR l.url ~* '/wfs(\?|$)'"
    r" OR l.url ~ '/ogc/features'"
    r" OR l.name ~* '\mapi\M'"
    r" OR l.name ~* '\msparql\M'"
    r" OR l.name ~* '\mwfs\M'"
    r" OR l.name ~* '\mwms\M'"
    r" OR l.description ~* '\mapi\M'"
    r" OR l.description ~* '\msparql\M'"
)

_BUILD_API_TYPE_CASE = (
    "CASE"
    " WHEN l.format_norm ILIKE '%arcgis rest%' THEN 'arcgis-rest'"
    " WHEN l.format_norm ILIKE '%ogc api%' THEN 'ogc-api'"
    " WHEN l.format_norm ILIKE '%sparql%' THEN 'sparql'"
    " WHEN l.format_norm ILIKE '%wms%' THEN 'wms'"
    " WHEN l.format_norm ILIKE '%wfs%' THEN 'wfs'"
    " WHEN l.format_norm ILIKE '%api%' AND l.format_norm NOT ILIKE '%mapinfo%' THEN 'api'"
    " WHEN l.format_norm = 'CSW' THEN 'csw'"
    " WHEN l.format_norm ILIKE '%georss%' THEN 'georss'"
    r" WHEN l.url ~ '/rest/services/' THEN 'arcgis-rest'"
    r" WHEN l.url ~* '\?service=WMS' THEN 'wms'"
    r" WHEN l.url ~* '\?service=WFS' THEN 'wfs'"
    r" WHEN l.url ~* '\?service=' OR l.url ~* '\?request=' OR l.url ~* '\?f=json' THEN 'ogc-api'"
    r" WHEN l.url ~* '/wms(\?|$)' THEN 'wms'"
    r" WHEN l.url ~* '/wfs(\?|$)' THEN 'wfs'"
    r" WHEN l.url ~ '/ogc/features' OR l.url ~ 'ogcapi' THEN 'ogc-api'"
    r" WHEN l.name ~* 'ogc api' THEN 'ogc-api'"
    r" WHEN l.name ~* '\msparql\M' OR l.description ~* '\msparql\M' THEN 'sparql'"
    r" WHEN l.name ~* '\mwfs\M' OR l.description ~* '\mwfs\M' THEN 'wfs'"
    r" WHEN l.name ~* '\mwms\M' OR l.description ~* '\mwms\M' THEN 'wms'"
    r" WHEN l.name ~* '\mapi\M' OR l.description ~* '\mapi\M' THEN 'unknown'"
    " ELSE 'unknown' END"
)

_BUILD_MAP_LAYER_TYPES = "('arcgis-rest', 'wms', 'wfs', 'ogc-api', 'csw', 'georss')"

INSERT_DATASET_API_SQL = f"""
INSERT INTO dataset_api (dataset_id, api_category)
SELECT
    datasets.id,
    CASE WHEN EXISTS (
        SELECT 1 FROM links l
        WHERE l.dataset_id = datasets.id
          AND ({_BUILD_API_SIGNAL})
          AND ({_BUILD_API_TYPE_CASE}) IN {_BUILD_MAP_LAYER_TYPES}
    ) THEN 'map-layers' ELSE 'data-apis' END AS api_category
FROM datasets
WHERE EXISTS (
    SELECT 1 FROM links l
    WHERE l.dataset_id = datasets.id
      AND ({_BUILD_API_SIGNAL})
) OR (datasets.title ~* '\\mapi\\M' OR datasets.notes ~* '\\mapi\\M')
"""


def _populate_dataset_api(db) -> int:
    """Populate the dataset_api summary table from links + dataset signals.
    Returns the row count inserted."""
    db.exec(INSERT_DATASET_API_SQL)
    row = db.prepare("SELECT COUNT(*) AS n FROM dataset_api").get()
    return row["n"]


# ---------------------------------------------------------------------------
# dataset_content_hash summary table
# ---------------------------------------------------------------------------
# Tier-1 exact-duplicate detection (docs/ideas.md "Duplicate dataset
# detection"): one md5 hash per dataset over its normalised title, notes and
# the sorted, deduped set of its resource URLs. Two datasets with the same
# hash are byte-for-byte content duplicates (the harvest-flooding pattern —
# see docs/harvest-flooding-report.md); GROUP BY content_hash HAVING
# COUNT(*) > 1 finds them in one indexed pass, no self-join.
#
# Computed entirely in SQL (like dataset_api above) rather than a Python
# loop, both for speed and so the normalisation lives in one place. URL
# normalisation strips the query string/fragment and a trailing slash —
# tracking params shouldn't defeat a match.
INSERT_DATASET_CONTENT_HASH_SQL = r"""
INSERT INTO dataset_content_hash (dataset_id, content_hash)
SELECT
    d.id,
    md5(
        trim(regexp_replace(lower(coalesce(d.title, '')), '\s+', ' ', 'g')) || E'\x1f' ||
        trim(regexp_replace(lower(coalesce(d.notes, '')), '\s+', ' ', 'g')) || E'\x1f' ||
        coalesce(u.urls, '')
    )
FROM datasets d
LEFT JOIN (
    SELECT dataset_id, string_agg(DISTINCT norm_url, E'\x1f' ORDER BY norm_url) AS urls
    FROM (
        SELECT dataset_id, rtrim(regexp_replace(lower(trim(url)), '[?#].*$', ''), '/') AS norm_url
        FROM links
        WHERE url IS NOT NULL AND url != ''
    ) norm
    GROUP BY dataset_id
) u ON u.dataset_id = d.id
"""


def _populate_dataset_content_hash(db) -> int:
    """Populate the dataset_content_hash summary table. Returns the row
    count inserted (one per dataset)."""
    db.exec(INSERT_DATASET_CONTENT_HASH_SQL)
    row = db.prepare("SELECT COUNT(*) AS n FROM dataset_content_hash").get()
    return row["n"]


# ---------------------------------------------------------------------------
# Main build
# ---------------------------------------------------------------------------
def build() -> None:
    """Rebuild the database from downloads/ + organisations.json +
    harvest_sources.json + CSVs."""

    orgs = _load_orgs()
    # CKAN org UUID → slug (the organisations PK) — the harvest sources
    # loader uses it to denormalise org_slug so queries join on the PK.
    org_slug_by_uuid = {o.get("id"): o.get("name") for o in orgs}
    harvest_sources = _load_harvest_sources()
    all_files = _collect_files()

    print(f"Building on {DATABASE_URL}...", file=sys.stderr)
    print(f"  {len(orgs)} organisations", file=sys.stderr)
    print(f"  {len(harvest_sources)} harvest sources", file=sys.stderr)

    db = connect(DATABASE_URL)
    try:
        print(f"  Found {len(all_files)} dataset files to process", file=sys.stderr)

        st = _BuildState()

        # Phase 1: wipe — drop old rows, keep the migrated tables
        db.exec(TRUNCATE_SQL)

        # Phase 2: load organisations
        db.transaction(partial(_load_organisations_tx, orgs=orgs))
        print(f"  {len(orgs)} organisations", file=sys.stderr)

        # Phase 3: load harvest sources (downloads/harvest_sources.json)
        db.transaction(
            partial(
                _load_harvest_sources_tx,
                sources=harvest_sources,
                org_slug_by_uuid=org_slug_by_uuid,
            ),
        )
        print(f"  {len(harvest_sources)} harvest sources", file=sys.stderr)

        # Phase 4: load datasets (batched)
        for i in range(0, len(all_files), READ_BATCH_SIZE):
            _process_batch(db, all_files[i : i + READ_BATCH_SIZE], st)
            if st.count % 10000 == 0 or st.count == len(all_files):
                print(f"  {st.count} datasets...", file=sys.stderr)

        # Indexes are migration-owned (0003) — the build populates, it never
        # creates. On the baseline DB they pre-exist.
        print("  indexes: migration-owned (0003)", file=sys.stderr)

        # Phase 5: full-text search — tags + fts (tsvector) columns.
        # idx_datasets_fts (GIN) is migration-owned (0003) — the populated
        # fts rows are indexed by the migration-created index.
        db.transaction(partial(_populate_fts_tx, fts_rows=st.fts_rows))
        print(f"  tsvector populated: {len(st.fts_rows)} datasets", file=sys.stderr)

        # Phase 6: views (search clicks per dataset)
        views_by_id = load_views_csv()
        if views_by_id:
            db.transaction(partial(_write_views_tx, views_by_id=views_by_id))
            print(f"  {len(views_by_id)} datasets have views data.", file=sys.stderr)

        # Phase 7: metadata field usage — write the counters collected during
        # the dataset load into metadata_keys / metadata_values.
        val_rows = db.transaction(
            partial(_write_meta_tx, field_counts=st.field_counts, value_counts=st.value_counts),
        )
        print(
            f"  metadata: {len(st.field_counts)} fields, {val_rows} distinct values",
            file=sys.stderr,
        )

        # Phase 8: dataset_api — snapshot which datasets have an API and
        # their matched links; used by the report and datasets-page facet.
        api_count = _populate_dataset_api(db)
        print(f"  dataset_api: {api_count} datasets", file=sys.stderr)

        # Phase 9: dataset_content_hash — exact-duplicate detection
        # (Tier 1, see docs/ideas.md); needs links loaded first.
        hash_count = _populate_dataset_content_hash(db)
        print(f"  dataset_content_hash: {hash_count} datasets", file=sys.stderr)

        link_row = db.prepare("SELECT COUNT(*) AS n FROM links").get()
        link_count = link_row["n"]
        print(
            f"Done: {st.count} datasets ({st.skipped} files skipped), {link_count} resource links.",
        )
        print(f"Index written to {DATABASE_URL}")
    finally:
        db.close()


@app.command()
def main() -> None:
    """Rebuild the database from downloads/ + organisations.json +
    harvest_sources.json + CSVs."""

    try:
        build()
    except typer.Exit:
        raise  # exit codes raised inside build() (e.g. missing inputs)
    except (RuntimeError, ValueError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(1) from None


@app.command()
def dataset_api() -> None:
    """Rebuild just the dataset_api table (TRUNCATE + INSERT).

    Runs in seconds against the existing datasets/links data — use this
    when tweaking the API detection algorithm without a full rebuild."""

    db = connect(DATABASE_URL)
    try:
        db.exec("TRUNCATE TABLE dataset_api")
        n = _populate_dataset_api(db)
        by_cat = db.prepare(
            "SELECT api_category, COUNT(*) AS n FROM dataset_api GROUP BY api_category ORDER BY n DESC",
        ).all()
        print(f"dataset_api: {n} datasets")
        for row in by_cat:
            print(f"  {row['api_category']}: {row['n']}")
    finally:
        db.close()


@app.command()
def views() -> None:
    """Reload dataset view counts from the views CSV.

    Resets all views to 0, then loads the CSV — runs in seconds against
    the existing datasets table, no full rebuild needed."""

    db = connect(DATABASE_URL)
    try:
        db.exec("UPDATE datasets SET views = 0")
        views_by_id = load_views_csv()
        if views_by_id:
            db.transaction(partial(_write_views_tx, views_by_id=views_by_id))
        print(f"views: {len(views_by_id)} datasets updated")
    finally:
        db.close()


@app.command()
def dataset_content_hash() -> None:
    """Rebuild just the dataset_content_hash table (TRUNCATE + INSERT).

    Runs in seconds against the existing datasets/links data — use this
    when tweaking the hash normalisation without a full rebuild."""

    db = connect(DATABASE_URL)
    try:
        db.exec("TRUNCATE TABLE dataset_content_hash")
        n = _populate_dataset_content_hash(db)
        dupes = db.prepare(
            "SELECT COUNT(*) AS n FROM ("
            "  SELECT content_hash FROM dataset_content_hash"
            "  GROUP BY content_hash HAVING COUNT(*) > 1"
            ") sub",
        ).get()["n"]
        print(f"dataset_content_hash: {n} datasets, {dupes} duplicate hash groups")
    finally:
        db.close()


if __name__ == "__main__":
    app()
