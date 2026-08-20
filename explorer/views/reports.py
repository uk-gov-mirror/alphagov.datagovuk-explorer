"""GET /report/{key} — one data-quality finding per page.

GET /report/{key}        — one paginated report per finding, with optional
                            single-select org facet (?org=)
GET /report/{key}?url=... — duplicate-URL detail mode (links-duplicate-urls)

The home dashboard (GET /) is views/dashboard.py; its card data is
assembled in queries/dashboard.py.
"""

import json
import math

from django.http import Http404
from django.shortcuts import render

from explorer import facets
from explorer.queries.core import Query
from explorer.queries.reports import (
    REPORTS,
    report_facet_counts,
    report_stmts,
    report_unfiltered_count,
    report_unfiltered_options,
)

from .core import _page_param

# Pagination — 100 rows per page on report pages
PAGE_SIZE = 100


def _duplicate_url_report(request, report, url):
    """Duplicate-URLs detail mode (?url=<encoded-url>) — every dataset that
    links to one shared URL."""
    count_stmt = Query(report["detail_count_sql"])
    list_stmt = Query(report["detail_sql"])
    total = count_stmt.get(url)["n"]

    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(_page_param(request), total_pages)
    offset = (page - 1) * PAGE_SIZE

    rows = list_stmt.all(url, PAGE_SIZE, offset)

    return render(
        request,
        "report.html",
        {
            "title": "Duplicate URL — data.gov.uk Explorer",
            "section": "dashboard",
            "current": {
                "key": report["key"],
                "label": report["label"],
                "description": report["description"],
                "kind": "links",
                "count": total,
                "hidden_cols": {"url"},
            },
            "detail_url": url,
            "has_facets": False,
            "facet_qs": "",
            "rows": rows,
            "page": page,
            "total_pages": total_pages,
            "start_index": offset + 1,
            "end_index": offset + len(rows),
        },
    )


def _report_facets(report, query_params) -> tuple[list, dict, list]:
    """Compile the report's single-select facet groups + active selections,
    validated against the report's own unfiltered facet options.

    Each facet's displayed counts are self-excluding (queries/reports.py's
    report_facet_counts): they apply every other active facet's filter,
    omitting their own — a no-op for the single-facet reports, and the real
    case for datasets-has-api (?org= + ?api_type=, where org counts shrink
    under an api_type selection and vice versa)."""
    facet_groups = []
    active_filters: dict[str, str] = {}
    active_facets: list[dict] = []

    # Pass 1 — validate each requested value against the report's own
    # unfiltered option pool (self-exclusion affects counts, not validity).
    # The unfiltered pools are memoised per report key (build-time
    # snapshot); the view runs them on every request even when a facet is
    # selected, so this is what keeps the heavy has-api pools cached.
    unfiltered_options = report_unfiltered_options(report["key"])
    for facet in report.get("facets", []):
        options = unfiltered_options[facet["key"]]
        wanted = query_params.get(facet["key"])
        if wanted is not None:
            match = next((o for o in options if o["slug"] == wanted), None)
            if match:
                active_filters[facet["key"]] = match["slug"]
                active_facets.append(
                    {
                        "key": facet["key"],
                        "label": facet["label"],
                        "current_name": match["name"],
                    },
                )

    # Pass 2 — self-excluding counts with the complete active-filter set.
    # No active filters → the pools are the memoised unfiltered ones; with
    # filters the counts are computed live (self-excluding, per-combo SQL).
    counts_stmt = report_facet_counts(report, active_filters)
    for facet in report.get("facets", []):
        if active_filters:
            sql, params = counts_stmt[facet["key"]]
            options = Query(sql).all(*params)
        else:
            options = unfiltered_options[facet["key"]]
        current = active_filters.get(facet["key"])
        facet_groups.append(
            facets.facet_counts_group(
                facet["key"],
                facet["label"],
                f"Filter by {facet['label'].lower()}",
                [(o["slug"], o["name"]) for o in options],
                {o["slug"]: o["count"] for o in options},
                current,
                proportions=True,
            ),
        )
    return facet_groups, active_filters, active_facets


def report(request, key):
    """GET /report/{key} — one data-quality finding, paginated via ?page=.

    Duplicate-URL detail mode (?url=) lists every dataset that links to one
    shared URL. Otherwise the report's own single-select facets narrow the
    report: ?org=<slug> (all faceted reports) and ?api_type=<slug> (the
    "Datasets with an API" report's API-type facet).
    """
    report = next((r for r in REPORTS if r["key"] == key), None)
    if report is None:
        raise Http404

    url = request.GET.get("url")

    # Duplicate URLs detail mode: ?url=<encoded-url>
    if report["kind"] == "duplicate-urls" and url is not None:
        return _duplicate_url_report(request, report, url)

    # Optional single-select facets (?org=<slug>, ?api_type=<slug>...). Each
    # report defines its own `facets` list; the selected values are validated
    # against the compiled facet options before being used as filters.
    facet_groups, active_filters, active_facets = _report_facets(
        report,
        {
            "org": request.GET.get("org"),
            "api_type": request.GET.get("api_type"),
        },
    )

    # Query-string machinery — same pattern as the other facet pages: a
    # preserve_params base (sort/dir are the pagination macro's defaults;
    # report pages have no sort UI), facet_url_for for the facet links
    # and pills, facet_qs for the pagination links.
    base_params = facets.preserve_params(
        "name",
        "asc",
        [(key, value) for key, value in active_filters.items()],
    )
    facet_url = facets.facet_url_for(base_params)
    facet_qs = facets.facet_qs(base_params, include_sort=False)

    stmt = report_stmts(report, active_filters)
    # No-filter count is memoised per report key (build-time snapshot);
    # filtered counts run live (per-combo SQL).
    total = report_unfiltered_count(report["key"]) if not active_filters else stmt["count"].get(*stmt["params"])["n"]

    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(_page_param(request), total_pages)
    offset = (page - 1) * PAGE_SIZE

    rows = stmt["list"].all(*stmt["params"], PAGE_SIZE, offset)

    # datasets-has-api: api_links arrives as a jsonb string (the query
    # layer's psycopg str loader — jsonb comes back as a JSON string here)
    # — parse it into a list of {name, format, url} dicts so the template
    # can render the matched resources.
    if report.get("show_api_links"):
        rows = [dict(r) for r in rows]
        for row in rows:
            row["api_links"] = json.loads(row["api_links"]) if row.get("api_links") else []

    return render(
        request,
        "report.html",
        {
            "title": f"{report['label']} — data.gov.uk Explorer",
            "section": "dashboard",
            "current": {
                "key": report["key"],
                "label": report["label"],
                "description": report["description"],
                "kind": report["kind"],
                "count": total,
                "hidden_cols": set(report.get("hidden_cols", [])),
                "show_api_links": report.get("show_api_links", False),
            },
            "facet_groups": facet_groups,
            "active_facets": active_facets,
            "has_facets": bool(facet_groups),
            "facet_url": facet_url,
            "facet_qs": facet_qs,
            "rows": rows,
            "page": page,
            "total_pages": total_pages,
            "start_index": offset + 1,
            "end_index": offset + len(rows),
        },
    )
