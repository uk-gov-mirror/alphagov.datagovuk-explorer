#!/usr/bin/env python3
"""Build (or rebuild) the PostgreSQL database from the cached JSON on disk.

Reads organisations.json, downloads/harvest_sources.json and every
dataset file under downloads/, then writes everything into the database.
The server can then answer page
requests with fast indexed queries instead of reading and parsing 50k+
JSON files on every request.

The full dataset JSON is stored in the dataset_json table, so nothing is
lost — the files under downloads/ remain the on-disk cache.

Usage: python scripts/build_db.py [--skip-embeddings]
       DATABASE_URL=postgresql://localhost:5432/other python scripts/build_db.py

--skip-embeddings keeps the build fully offline when llama-server is down
(by default the embeddings phase runs and hard-fails if the server is
unreachable).

Phases: wipe, organisations, datasets (batched, parallel file reads),
full-text search, embeddings, views, metadata, stats. Indexes are
migration-owned (0003) — the build populates, never creates.
"""

import csv
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import typer

from scripts.db import connect, database_url
from scripts.embeddings import BATCH, TIMEOUT as EMBED_TIMEOUT

app = typer.Typer(add_completion=False)

# Number of JSON files to read in parallel per batch. Reading many small
# files one-at-a-time is the dominant bottleneck, so we batch them with
# threads to overlap I/O.
READ_BATCH_SIZE = 2000

# Value-distribution table: strings/JSON truncated at this length.
MAX_FIELD_VALUE_LENGTH = 500

# directory-views-year.csv rows are title, url, views.
VIEWS_CSV_COLUMNS = 3

DATA_DIR = Path(__file__).resolve().parent.parent / "downloads"
ORGS_FILE = DATA_DIR / "organisations.json"
HARVEST_SOURCES_FILE = DATA_DIR / "harvest_sources.json"
VIEWS_FILE = Path(__file__).resolve().parent.parent / "data" / "directory-views-year.csv"
DATABASE_URL = database_url()

_WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# CSV parsing (data/directory-views-year.csv)
# ---------------------------------------------------------------------------
def parse_csv_line(line: str) -> list[str]:
    """Parse a single CSV line, handling quoted fields (titles contain
    commas)."""

    return next(csv.reader([line]))


def _parse_int(s: str) -> int | None:
    """Leading-digit semantics for the views CSV: leading whitespace,
    optional sign, then as many digits as possible. None when there are no
    digits (the caller treats that as "no count")."""

    m = re.match(r"\s*[+-]?\d+", s)
    return int(m.group(0)) if m else None


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
    """Reduce normalised temporal from/to values to a JSON array of coverage
    periods, each an [from_year, to_year] pair (either year null). Periods
    are paired up positionally and kept separate so non-contiguous coverage
    (e.g. [1960-1992] and [2000-2016]) can be
    matched as a union instead of collapsing to the first period. Reversed
    pairs (from > to) are swapped. Returns None when neither side yields any
    years."""

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
    return json.dumps(periods, separators=(",", ":")) if periods else None


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
    empty host
    (null) — no slash collapsing. Non-special schemes (mailto:, etc.) and
    scheme-less strings are returned unchanged (the fallback regex rejects
    them).
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
# CKAN's resource `format` field is free text with no controlled vocabulary,
# so grouping on it directly yields 230+ near-duplicate facets. We clean it
# in stages: mechanical fixes first (trim, whitespace collapse, IANA
# media-type URLs → their type, "OGC " prefix strip, dot strip), then a
# documented synonym/bucket map for high-confidence families. Anything not
# in the map keeps its cleaned label — rare-but-real formats (CITYGML, LAZ,
# NETCDF…) are deliberately left alone rather than hidden behind a count
# threshold.
#
# Deliberate judgment calls (all keys below are post-mechanical-cleaning):
#  - The ESRI/ArcGIS REST family is collapsed to ARCGIS REST. The dominant
#    label "ArcGIS GeoServices REST API" is ArcGIS Hub's default export
#    label (ONS alone accounts for 73% of it). Interactive products
#    (storymaps, experiences, online maps) are NOT merged in — genuinely
#    different resource types.
#  - A WEB PAGE bucket collects labels that point at a portal page rather
#    than a data file. API/SPARQL endpoints and DASHBOARD stay separate —
#    for a quality-audit tool "the link isn't a data file" is a finding,
#    not noise.
#  - json1.0/json2.0 are NISRA's JSON-stat API endpoints (JSON-stat/1.0
#    URLs) — a real schema, not a typo of JSON.
#  - Multi-format labels like "CSV / ZIP" are left alone: assigning them to
#    either type is an inference, and splitting them would double-count.

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
# Title normalisation
# ---------------------------------------------------------------------------
# Strip the page-variant suffix from analytics titles and normalise for
# matching. The alternation contains the non-English labels data.gov.uk
# renders (a fixed set of page suffixes, so copy exactly rather than
# approximate).
_TITLE_SUFFIX_RE = re.compile(
    r"\s*[--—]\s*(data\.gov\.uk|national data library|国家数据库|国家数据图书馆|"
    r"国立データライブラリ|biblioteca nacional de datos|bibliothèque nationale de données|"
    r"nationale databibliothek|nationale datenbank|biblioteca nazionale di dati)$",
)


def normalize_title(raw):
    """Strip the page-variant suffix from analytics titles and normalise for
    matching."""

    s = (raw or "").lower()
    s = _WS_RE.sub(" ", s).strip()
    s = _TITLE_SUFFIX_RE.sub("", s)
    return s.strip()


# ---------------------------------------------------------------------------
# Metadata field usage
# ---------------------------------------------------------------------------
def field_value_str(v):
    """Convert a field value to a string for the value-distribution table.
    Long strings are truncated at 500 chars to keep
    the index size reasonable; objects/arrays become JSON (also truncated).
    None, empty string, empty array and empty object are all collapsed to a
    single "(empty)" bucket so they don't clutter the value table as
    separate rows."""

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
# Dataset views (data/directory-views-year.csv — tracked in git, not in downloads/)
# ---------------------------------------------------------------------------
# data.gov.uk exports directory page views as a CSV whose rows look like:
#   Page title,Page location,Views
#   some dataset title,https://www.data.gov.uk/dataset/<uuid>/<slug>,1234
# The export also redacts parts of some dataset UUIDs, replacing chunks of
# the id with the literal text "[date]". We resolve those rows below.

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_VIEW_URL_RE = re.compile(r"data\.gov\.uk/dataset/([^/?#]+)")


@dataclass
class _Pattern:
    """Aggregated views for one [date]-redacted id pattern."""

    views: int = 0
    titles: set = field(default_factory=set)


@dataclass
class ViewsCsv:
    """Parsed views CSV: clean-UUID rows and [date]-redacted patterns."""

    views: dict = field(default_factory=dict)  # id -> total views
    patterns: dict = field(default_factory=dict)  # key -> _Pattern


def load_views_csv() -> ViewsCsv:
    """Read directory-views-year.csv (if present) and split it into clean
    UUID rows and [date]-redacted patterns."""

    result = ViewsCsv()
    if not VIEWS_FILE.exists():
        return result

    for line in VIEWS_FILE.read_text(encoding="utf-8").split("\n"):
        trimmed = line.strip()
        if not trimmed or trimmed.startswith(("#", "---")):
            continue

        cols = parse_csv_line(line)
        if len(cols) < VIEWS_CSV_COLUMNS:
            continue  # e.g. the "Grand total" row

        url = cols[1]
        views = _parse_int(cols[2])
        if not url or views is None or views <= 0:
            continue

        m = _VIEW_URL_RE.search(url)
        if not m:
            continue  # not a dataset page (404s, etc.)

        id_part = m.group(1)
        if "[date]" in id_part:
            key = id_part.replace("[date]", "XX")
            p = result.patterns.get(key)
            if p is None:
                p = _Pattern()
                result.patterns[key] = p
            p.views += views
            p.titles.add(cols[0])
        elif _UUID_RE.match(id_part):
            result.views[id_part] = result.views.get(id_part, 0) + views
    return result


# "[date]" can replace a variable-length chunk of a UUID (sometimes several
# dash-separated segments), so the wildcard matches one or more hex groups.
DATE_WILDCARD = r"(?:[0-9a-f]+(?:-[0-9a-f]+)*)"


def _title_fallback(data, title_ids) -> str | None:
    """Page-title fallback for a [date]-redacted views row — accept only
    when exactly one dataset matches the normalized title."""
    for raw_title in data.titles:
        base = normalize_title(raw_title)
        if not base:
            continue
        ids = title_ids.get(base)
        if ids and len(set(ids)) == 1:
            return ids[0]
    return None


def resolve_date_pattern_views(patterns: dict, all_ids: list, title_ids: dict) -> dict:
    """Resolve rows whose dataset id was redacted as "[date]" in the export:

      1. Treat "[date]" as a wildcard and find a dataset whose id fits the
         remaining fragments (unique match only).
      2. Fall back to the page title (stripped of its " - data.gov.uk"-style
         suffix), again only when it matches exactly one dataset.

    Rows we can't resolve confidently (page-not-found junk, re-published
    datasets with stale ids) are skipped. Returns {id: views}."""

    # Index ids by their first 8 hex chars so wildcard matching stays fast
    ids_by_first_seg: dict = {}
    for id_ in all_ids:
        seg = id_[:8]
        if seg not in ids_by_first_seg:
            ids_by_first_seg[seg] = []
        ids_by_first_seg[seg].append(id_)

    resolved: dict = {}

    for key, data in patterns.items():
        re_pattern = re.compile(rf"^{key.replace('XX', DATE_WILDCARD)}$")

        # Narrow candidates using the fixed fragments around the wildcards
        parts = key.split("XX")
        prefix = parts[0]
        suffix = parts[-1]
        if prefix:
            first_seg = prefix.split("-")[0]
            bucket = ids_by_first_seg.get(first_seg) if re.fullmatch(r"[0-9a-f]{8}", first_seg) else None
            candidates = [id_ for id_ in bucket if id_.startswith(prefix)] if bucket else all_ids
        elif suffix:
            candidates = [id_ for id_ in all_ids if id_.endswith(suffix)]
        else:
            continue  # no fixed fragment at all — nothing to anchor on

        hit_ids = list(
            dict.fromkeys(id_ for id_ in candidates if re_pattern.fullmatch(id_)),
        )
        if len(hit_ids) == 1:
            resolved[hit_ids[0]] = resolved.get(hit_ids[0], 0) + data.views
            continue

        # Title fallback — only accept when exactly one dataset matches
        title_hit = _title_fallback(data, title_ids)
        if title_hit:
            resolved[title_hit] = resolved.get(title_hit, 0) + data.views
    return resolved


# ---------------------------------------------------------------------------
# Wipe — schema is owned by Django migrations; the build only truncates
# the 9 tables it populates (series tables excluded).
# ---------------------------------------------------------------------------

TRUNCATE_SQL = (
    "TRUNCATE TABLE embedding_map, dataset_embeddings, metadata_values, "
    "metadata_keys, links, dataset_json, datasets, organisations, "
    "harvest_sources CASCADE"
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
     temporal_coverage_from, temporal_coverage_to, temporal_granularity,
     temporal_periods,
     harvested, harvest_source_title, harvest_source_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (id) DO UPDATE SET
    org_slug = EXCLUDED.org_slug, org_display_name = EXCLUDED.org_display_name,
    title = EXCLUDED.title, name = EXCLUDED.name, notes = EXCLUDED.notes,
    metadata_created = EXCLUDED.metadata_created,
    metadata_modified = EXCLUDED.metadata_modified,
    resource_count = EXCLUDED.resource_count,
    theme_primary = EXCLUDED.theme_primary,
    temporal_coverage_from = EXCLUDED.temporal_coverage_from,
    temporal_coverage_to = EXCLUDED.temporal_coverage_to,
    temporal_granularity = EXCLUDED.temporal_granularity,
    temporal_periods = EXCLUDED.temporal_periods,
    harvested = EXCLUDED.harvested,
    harvest_source_title = EXCLUDED.harvest_source_title,
    harvest_source_id = EXCLUDED.harvest_source_id
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
        self.all_ids: list = []
        self.fts_rows: list = []
        self.title_ids: dict = {}
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
    """The 17 VALUES for insert_ds, derived from one dataset dict."""
    from_val = temporal_val(ds.get("temporal_coverage-from"))
    to_val = temporal_val(ds.get("temporal_coverage-to"))
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
        from_val,
        to_val,
        temporal_val(ds.get("temporal_granularity")),
        temporal_periods(from_val, to_val),
        1 if extras.get("harvest_object_id") else 0,
        extras.get("harvest_source_title") or None,
        extras.get("harvest_source_id") or None,
    )


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


def _title_index(ds: dict, st: _BuildState) -> None:
    """Index the dataset title for the views-pattern resolution."""
    norm_title = normalize_title(ds.get("title"))
    if norm_title:
        st.title_ids.setdefault(norm_title, []).append(ds.get("id"))


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

            st.all_ids.append(ds.get("id"))
            st.fts_rows.append(_fts_row(ds))
            _meta_counts(ds, st)
            _title_index(ds, st)
            st.count += 1

    db.transaction(_tx)


def _load_orgs() -> list:
    """Read downloads/organisations.json — the friendly CLI errors on
    failure are the interface (fetch-organisations regenerates the file)."""
    print("Reading organisations.json...", file=sys.stderr)
    try:
        return json.loads(ORGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        print(f"Could not read {ORGS_FILE}: {err}", file=sys.stderr)
        print(
            "Run `just fetch-organisations` first (regenerates it from the CKAN API).",
            file=sys.stderr,
        )
        raise typer.Exit(1) from None


def _load_harvest_sources() -> list:
    """Read downloads/harvest_sources.json — the friendly CLI errors on
    failure are the interface (fetch-harvest-sources regenerates the file)."""
    print("Reading harvest_sources.json...", file=sys.stderr)
    try:
        return json.loads(HARVEST_SOURCES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        print(f"Could not read {HARVEST_SOURCES_FILE}: {err}", file=sys.stderr)
        print(
            "Run `just fetch-harvest-sources` first (regenerates it from the CKAN API).",
            file=sys.stderr,
        )
        raise typer.Exit(1) from None


def _collect_files() -> list[dict[str, str | Path]]:
    """All .json dataset files with their org slug. Sorted on both levels so
    the build is deterministic regardless of filesystem directory order (and
    matches the insertion order a table diff expects)."""
    if not DATA_DIR.is_dir():
        print(
            f"No {DATA_DIR}/ directory found — run download_datasets.py first.",
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


def _embed_datasets(db, fts_rows) -> None:
    """Embed the fts rows via llama-server (bge-base-en-v1.5, 768-dim
    normalised) and write them to the db. Lazy imports — only needed when
    embeddings run; --skip-embeddings keeps the build offline."""
    from scripts.embed_only import (  # noqa: PLC0415 — lazy: only needed when embeddings run
        build_texts,
        embed_batch,
    )

    print(
        "Computing embeddings (bge-base-en-v1.5 via llama-server)...",
        file=sys.stderr,
    )

    texts = build_texts(fts_rows)

    with httpx.Client(follow_redirects=True, timeout=EMBED_TIMEOUT) as client:
        for batch_start in range(0, len(texts), BATCH):
            batch_end = min(batch_start + BATCH, len(texts))
            embed_batch(client, db, texts, fts_rows, batch_start, batch_end)
            if batch_end % 5000 == 0 or batch_end >= len(texts):
                print(
                    f"  {batch_end}/{len(texts)} embeddings...",
                    file=sys.stderr,
                )
    print(f"  embeddings: {len(texts)} datasets", file=sys.stderr)


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
# Main build
# ---------------------------------------------------------------------------
def build(*, skip_embeddings: bool = False) -> None:
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

        # Phase 6: embeddings — semantic vectors for "more like this" via
        # pgvector (bge-base-en-v1.5 via llama-server). --skip-embeddings
        # keeps the build offline.
        if not skip_embeddings:
            _embed_datasets(db, st.fts_rows)

        # Phase 7: views — merge the [date]-redacted rows in, then write
        views_csv = load_views_csv()
        views_by_id = views_csv.views
        if views_csv.patterns:
            date_views = resolve_date_pattern_views(
                views_csv.patterns,
                st.all_ids,
                st.title_ids,
            )
            print(
                f"  views: {len(views_csv.views)} ids from clean URLs, "
                f"{len(date_views)} from [date]-redacted URLs "
                f"({len(views_csv.patterns) - len(date_views)} unmatched)",
                file=sys.stderr,
            )
            for id_, v in date_views.items():
                views_by_id[id_] = views_by_id.get(id_, 0) + v

        if views_by_id:
            db.transaction(partial(_write_views_tx, views_by_id=views_by_id))
            print(f"  {len(views_by_id)} datasets have views data.", file=sys.stderr)

        # Phase 8: metadata field usage — write the counters collected during
        # the dataset load into metadata_keys / metadata_values.
        val_rows = db.transaction(
            partial(_write_meta_tx, field_counts=st.field_counts, value_counts=st.value_counts),
        )
        print(
            f"  metadata: {len(st.field_counts)} fields, {val_rows} distinct values",
            file=sys.stderr,
        )

        link_row = db.prepare("SELECT COUNT(*) AS n FROM links").get()
        link_count = link_row["n"]
        print(
            f"Done: {st.count} datasets ({st.skipped} files skipped), {link_count} resource links.",
        )
        print(f"Index written to {DATABASE_URL}")
    finally:
        db.close()


@app.command()
def main(
    *,
    skip_embeddings: bool = typer.Option(
        False,  # noqa: FBT003 — typer.Option's default is the first positional
        "--skip-embeddings",
        help="skip the llama-server embeddings phase (offline build)",
    ),
) -> None:
    """Rebuild the database from downloads/ + organisations.json +
    harvest_sources.json + CSVs."""

    try:
        build(skip_embeddings=skip_embeddings)
    except typer.Exit:
        raise  # exit codes raised inside build() (e.g. missing inputs)
    except (httpx.HTTPError, RuntimeError, ValueError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(1) from None


if __name__ == "__main__":
    app()
