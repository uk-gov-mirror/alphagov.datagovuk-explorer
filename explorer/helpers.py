"""Shared helpers for date/yearly-chart formatting and theme labels.

The DB-backed yearly counts (yearly_org_counts / yearly_dataset_counts)
live in the query layer (explorer/queries) with their statements; these are
the pure formatting helpers.
"""

from datetime import UTC, datetime
from typing import Any

# data.gov.uk primary theme slugs → display labels
THEME_LABELS = {
    "towns-and-cities": "Towns & Cities",
    "government-spending": "Government Spending",
    "environment": "Environment",
    "government": "Government",
    "mapping": "Mapping",
    "crime-and-justice": "Crime & Justice",
    "transport": "Transport",
    "society": "Society",
    "business-and-economy": "Business & Economy",
    "education": "Education",
    "health": "Health",
    "defence": "Defence",
    "digital-services-performance": "Digital Services Performance",
}


def format_date(iso: str | None) -> str:
    """Format an ISO timestamp as YYYY-MM-DD.

    The DB stores UTC timestamps as naive `timestamp without time zone`;
    a naive parse would treat them as *local* time and shift dates near
    local midnight by a day whenever the server TZ != UTC. We keep naive
    timestamps as-is (the stored UTC value). Timestamps with an explicit
    offset are converted to
    UTC (ISO 8601 with milliseconds and Z, e.g. 2026-08-03T15:04:29.901Z).
    Invalid input is returned
    unchanged; falsy input becomes an em-dash.
    """
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)  # accepts trailing "Z" on 3.11+
    except ValueError:
        return iso
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC)
    # date.isoformat(), NOT dt.strftime(): strftime makes the C library
    # resolve the timezone on every call, and this runs per-row on the
    # big pages. isoformat is pure Python.
    return dt.date().isoformat()


def theme_label(slug: str) -> str:
    """Convert a theme slug into a readable label (fallback: title-case the slug)."""
    if slug in THEME_LABELS:
        return THEME_LABELS[slug]
    return " ".join(w[:1].upper() + w[1:] for w in slug.split("-"))


def _yearly_counts_from_map(counts: dict[str, int]) -> list[dict[str, Any]]:
    """Fill year gaps so the chart has a continuous axis."""
    if not counts:
        return []
    years = sorted(int(y) for y in counts)
    first, last = years[0], years[-1]
    return [{"year": str(y), "label": str(y), "count": counts.get(str(y), 0)} for y in range(first, last + 1)]


def yearly_counts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build a continuous per-year series (YYYY) from [{year, count}] rows."""
    if not rows:
        return []
    return _yearly_counts_from_map({r["year"]: r["count"] for r in rows})
