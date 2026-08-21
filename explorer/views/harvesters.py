"""GET /harvesters — all harvest sources (server-side sortable, facet
filters).

Facets use the same pattern as /organisations:
  ?type=...       — harvest type (ckan, gemini-csw, dcat_json, ...)
  ?active=true|false — active sources only
  ?frequency=...  — harvest frequency (MANUAL, DAILY, WEEKLY, ...)
  ?datasets=...   — dataset-count bucket (0|1-10|11-50|51-100|101-500|501-1000|1000+)
                   — same buckets as /organisations, applied to the
                   per-source dataset_count

The list is small (a few hundred sources), so the filtering, sorting
and sidebar facet counts all stay Python-side — no SQL facet pools like
/datasets needs.

Sort columns are whitelisted in explorer.sort.HARVESTER_SORT_COLUMNS;
unknown keys fall back to the default (dataset_count desc).
"""

import json
from collections import Counter
from dataclasses import dataclass

from django.http import Http404
from django.shortcuts import render

from explorer import facets
from explorer.helpers import format_date
from explorer.queries.datasets import DATASETS_BY_SOURCE
from explorer.queries.harvesters import HARVEST_SOURCE, harvest_source_rows, harvested_total
from explorer.queries.organisations import (
    DATASET_BUCKET_NAMES,
    DATASET_BUCKET_TESTS,
    DATASET_BUCKETS,
    ORG,
    VALID_DATASET_BUCKETS,
)
from explorer.sort import DATASET_SORT_COLUMNS, HARVESTER_SORT_COLUMNS, sort_datasets, sort_harvesters

from .core import _sort_dir

# Fixed value → display-label maps for the type/frequency columns and
# facets. The facet master lists are derived from the data (counts order),
# so a new type/frequency that appears in a rebuild shows up automatically.
TYPE_LABELS = {
    "ckan": "CKAN",
    "dcat_json": "DCAT JSON",
    "dcat_rdf": "DCAT RDF",
    "gemini-csw": "Gemini CSW",
    "gemini-single": "Gemini single",
    "gemini-waf": "Gemini WAF",
    "inventory": "Inventory",
}

FREQUENCY_LABELS = {
    "ALWAYS": "Always",
    "DAILY": "Daily",
    "WEEKLY": "Weekly",
    "MONTHLY": "Monthly",
    "MANUAL": "Manual",
}

ACTIVE_LABELS = {"true": "Active", "false": "Inactive"}


@dataclass(frozen=True)
class HarvesterFilters:
    """The four validated /harvesters facet selections (None = not active)."""

    type: str | None
    active: str | None
    frequency: str | None
    datasets: str | None


def _facet_master(counts: Counter, labels: dict[str, str]) -> list[tuple[str, str]]:
    """(value, label) pairs ordered by count desc (ties alphabetical) —
    the facet-group master list and the row-label lookups both consume it."""
    return [
        (value, labels.get(value, value.title())) for value, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def _dataset_bucket(count: int) -> str:
    """Bucket key for a dataset count — the orgs page's DATASET_BUCKET_TESTS
    reversed: first bucket whose range contains the count. Falls back to the
    open-ended top bucket (covers counts that drift above the defined edges)."""
    for value, test in DATASET_BUCKET_TESTS.items():
        if test(count):
            return value
    return DATASET_BUCKETS[-1][0]


def _parse_filters(request, valid_types: set, valid_frequencies: set) -> HarvesterFilters:
    """Validate the /harvesters facet selections from request.GET. Each
    facet is single-select; unknown values are ignored (falls back to no
    selection)."""
    type_ = request.GET.get("type")
    current_type = type_ if type_ in valid_types else None

    active = request.GET.get("active")
    current_active = active if active in ("true", "false") else None

    frequency = request.GET.get("frequency")
    current_frequency = frequency if frequency in valid_frequencies else None

    datasets = request.GET.get("datasets")
    current_datasets = datasets if datasets in VALID_DATASET_BUCKETS else None

    return HarvesterFilters(
        type=current_type,
        active=current_active,
        frequency=current_frequency,
        datasets=current_datasets,
    )


def _matches(r: dict, filters: HarvesterFilters, exclude: str | None = None) -> bool:
    """Row matches every active facet except `exclude` (the group being
    counted — self-excluding pools, the same rule as /organisations)."""
    return (
        (exclude == "type" or filters.type is None or r["type"] == filters.type)
        and (exclude == "active" or filters.active is None or r["active_key"] == filters.active)
        and (exclude == "frequency" or filters.frequency is None or r["frequency"] == filters.frequency)
        and (
            exclude == "datasets"
            or filters.datasets is None
            or DATASET_BUCKET_TESTS[filters.datasets](r["dataset_count"])
        )
    )


def harvesters(request):
    """GET /harvesters — all harvest sources, server-side sortable, with
    type/status/frequency facets."""
    rows = harvest_source_rows()

    # Decorate: display labels (type/frequency/status) + formatted dates.
    # facet masters double as the label maps, so rows and facets can't drift.
    all_types = Counter(r["type"] for r in rows)
    all_frequencies = Counter(r["frequency"] for r in rows)
    type_master = _facet_master(all_types, TYPE_LABELS)
    frequency_master = _facet_master(all_frequencies, FREQUENCY_LABELS)
    type_labels = dict(type_master)
    frequency_labels = dict(frequency_master)

    for r in rows:
        r["type_label"] = type_labels.get(r["type"], r["type"])
        r["frequency_label"] = frequency_labels.get(r["frequency"], r["frequency"])
        r["active_key"] = "true" if r["active"] else "false"
        r["active_label"] = ACTIVE_LABELS[r["active_key"]]
        r["last_run"] = format_date(r["last_run"])

    sort, dir_ = _sort_dir(request, HARVESTER_SORT_COLUMNS, "dataset_count", "desc")

    filters = _parse_filters(
        request,
        set(all_types),
        set(all_frequencies),
    )
    shown_rows = [r for r in rows if _matches(r, filters)]
    sort_harvesters(shown_rows, sort, dir_)

    # Shared query-string base: sort, dir, then the active facets in a
    # fixed order.
    base_params = facets.preserve_params(
        sort,
        dir_,
        [
            ("type", filters.type),
            ("active", filters.active),
            ("frequency", filters.frequency),
            ("datasets", filters.datasets),
        ],
    )
    facet_url = facets.facet_url_for(base_params)
    facet_qs = facets.facet_qs(base_params, include_sort=False)

    # Sidebar facet groups — Python-side self-excluding pools: each group
    # counts over the rows filtered by the other two facets (excluding its
    # own), via the shared core.facet_where-style _matches exclude rule.
    type_counts = Counter(r["type"] for r in rows if _matches(r, filters, exclude="type"))
    active_counts = Counter(r["active_key"] for r in rows if _matches(r, filters, exclude="active"))
    frequency_counts = Counter(r["frequency"] for r in rows if _matches(r, filters, exclude="frequency"))
    dataset_counts = Counter(
        _dataset_bucket(r["dataset_count"]) for r in rows if _matches(r, filters, exclude="datasets")
    )

    facet_groups = [
        group
        for group in (
            facets.facet_counts_group(
                "type",
                "Type",
                "Filter by harvest type",
                type_master,
                type_counts,
                filters.type,
                proportions=True,
            ),
            facets.facet_counts_group(
                "active",
                "Status",
                "Filter by source status",
                [(v, ACTIVE_LABELS[v]) for v in ("true", "false")],
                active_counts,
                filters.active,
                proportions=True,
            ),
            facets.facet_counts_group(
                "frequency",
                "Frequency",
                "Filter by harvest frequency",
                frequency_master,
                frequency_counts,
                filters.frequency,
                proportions=True,
            ),
            facets.facet_counts_group(
                "datasets",
                "Datasets",
                "Filter by number of datasets",
                DATASET_BUCKETS,
                dataset_counts,
                filters.datasets,
                proportions=True,
            ),
        )
        if group is not None
    ]

    return render(
        request,
        "harvesters.html",
        {
            "title": "Harvesters — data.gov.uk Explorer",
            "section": "harvesters",
            "sources": shown_rows,
            "total": len(rows),
            "shown": len(shown_rows),
            # Headline: harvested datasets by the dataset's own harvested
            # flag — the same definition the /datasets SOURCE facet counts.
            # The per-source dataset_count column is attribution: it only
            # covers datasets whose harvest source record is in the registry,
            # so its sum (linked_datasets) can be less.
            "total_datasets": harvested_total(),
            "linked_datasets": sum(r["dataset_count"] for r in rows),
            "sort": sort,
            "dir": dir_,
            "type": filters.type,
            "type_label": type_labels.get(filters.type) if filters.type else None,
            "active": filters.active,
            "active_label": ACTIVE_LABELS[filters.active] if filters.active else None,
            "frequency": filters.frequency,
            "frequency_label": frequency_labels.get(filters.frequency) if filters.frequency else None,
            "datasets": filters.datasets,
            "datasets_label": (DATASET_BUCKET_NAMES[filters.datasets] if filters.datasets else None),
            "facet_groups": facet_groups,
            "facet_qs": facet_qs,
            "facet_url": facet_url,
        },
    )


def harvester(request, source_id):
    """GET /harvester/{id} — one harvest source's record and datasets.

    Follows the organisation detail pattern: the source's promoted columns
    plus the fields only the full json record carries (description,
    publisher, harvest status/jobs), then the datasets harvested by it
    (the harvest_source_id join, same as the list page).
    """
    row = HARVEST_SOURCE.get(source_id)
    if row is None:
        raise Http404

    record = json.loads(row["json"]) if row["json"] else {}
    status = record.get("status") or {}

    org_name = None
    if row["org_slug"]:
        org_row = ORG.get(row["org_slug"])
        if org_row is not None:
            org_name = org_row["display_name"] or org_row["title"] or org_row["name"]

    datasets = DATASETS_BY_SOURCE.all(row["id"]) if row["id"] else []
    sort, dir_ = _sort_dir(request, DATASET_SORT_COLUMNS, "metadata_modified", "desc")
    sort_datasets(datasets, sort, dir_)

    active = bool(row["active"])
    # The API writes the literal string "None" (not null) for missing
    # timestamps — normalise it so format_date renders an em-dash.
    last_run = status.get("last_harvest_request")
    next_run = record.get("next_run")
    source = {
        "id": row["id"],
        "title": row["title"],
        "url": row["url"],
        "type_label": TYPE_LABELS.get(row["type"], (row["type"] or "").title()),
        "active": active,
        "active_label": ACTIVE_LABELS["true" if active else "false"],
        "frequency_label": FREQUENCY_LABELS.get(row["frequency"], (row["frequency"] or "").title()),
        "org_slug": row["org_slug"],
        "org_name": org_name,
        "created": format_date(row["created"]),
        "last_run": format_date(None if last_run == "None" else last_run),
        "next_run": format_date(None if next_run == "None" else next_run),
        "job_count": status.get("job_count"),
        "publisher": record.get("publisher_title") or record.get("publisher_id"),
        "description": record.get("description"),
        "dataset_count": len(datasets),
        "datasets": datasets,
    }

    return render(
        request,
        "harvester.html",
        {
            "title": f"{source['title'] or source['id']} — Harvesters",
            "section": "harvesters",
            "source": source,
            "sort": sort,
            "dir": dir_,
        },
    )
