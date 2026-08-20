"""GET /links — every resource URL across all datasets.

Server-side sortable via ?sort= & ?dir=, filterable by host/domain, format
and dataset year via single-select facet links, paginated (100/page).

Facet sidebar counts are self-excluding SQL aggregates from
explorer/queries/links.py (links_facet_counts) over the same clause
builders the page list/count use (links_stmts) — each group applies the
other active filters, so a selected host/format/year shrinks the sibling
counts (the same behaviour as /datasets). The report-header totals stay
fixed whole-table aggregates (LINKS_STATS).
"""

import math

from django.shortcuts import render

from explorer import facets
from explorer.queries.links import (
    LINK_SORT_COLUMNS,
    links_facet_counts,
    links_stats,
    links_stmts,
)

from .core import _page_param, _sort_dir

# Pagination — 100 links per page, via ?page=
PAGE_SIZE = 100

# Facet sidebar: formats beyond this cutoff are hidden behind the
# "More formats" toggle (all other facet lists are always shown).
FORMAT_FACET_CUTOFF = 10

# Hostnames — RFC 1035/2181 caps a fully-qualified name at 253 chars.
MAX_HOST_LENGTH = 253


def links(request):
    """GET /links — the all-links report with sidebar facets."""
    stats = links_stats()
    no_url_links = stats.get("no_url") or 0

    # Sidebar facet pools — self-excluding SQL aggregates. The unfiltered
    # pools drive the format/year validation whitelists (filter-independent);
    # the filtered pools drive the sidebar counts once the selections are
    # validated.
    base_pool = links_facet_counts({})
    valid_formats = {f["fmt"] for f in base_pool["formats"]}
    valid_years = {y["year"] for y in base_pool["years"]}

    # Format facet — all formats are rendered to the page; the "More formats"
    # toggle expands/collapses the list client-side (with a ?formats=all
    # fallback when JS is off). formats_expanded only sets the initial state.
    formats_expanded = request.GET.get("formats") == "all"

    # Validate against the full list so any format can be filtered even when
    # it's beyond the top 10 shown by default.
    # Facet values — bound as WHERE parameters, never interpolated.
    host = request.GET.get("host")
    current_host = None
    if host == "__none__":
        current_host = "__none__"
    elif host and len(host) <= MAX_HOST_LENGTH:
        current_host = host

    format_ = request.GET.get("format")
    current_format = "__none__" if format_ == "__none__" else format_ if format_ in valid_formats else None
    current_year = request.GET.get("year")
    current_year = current_year if current_year in valid_years else None

    sort, dir_ = _sort_dir(request, LINK_SORT_COLUMNS, "host")

    filters = {"host": current_host, "format": current_format, "year": current_year}
    pool = links_facet_counts(filters)
    stmts_out = links_stmts(filters, sort, dir_)

    total = stmts_out["count"].get(*stmts_out["params"])["n"]
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    current_page = min(_page_param(request), total_pages)
    offset = (current_page - 1) * PAGE_SIZE
    link_rows = stmts_out["list"].all(*stmts_out["params"], PAGE_SIZE, offset)

    # Query-string fragments shared by sort links / facet links / pills.
    # Dicts preserve insertion order, so urlencode emits the fixed
    # parameter order: sort, dir, then host, format, year, formats.
    base_params = facets.preserve_params(
        sort,
        dir_,
        [
            ("host", current_host),
            ("format", current_format),
            ("year", current_year),
        ],
        {"formats": "all"} if formats_expanded else None,
    )
    facet_url = facets.facet_url_for(base_params)

    # Extra query string preserving the active facets (for sort/pagination links)
    facet_qs = facets.facet_qs(base_params, include_sort=False)

    # Facet groups for the sidebar (pool counts + current selection -> group)
    facet_groups = [
        group
        for group in (
            facets.facet_counts_group(
                "host",
                "Domain",
                "Filter by domain",
                [(h["host"], h["host"]) for h in pool["hosts"]],
                {h["host"]: h["count"] for h in pool["hosts"]},
                current_host,
                proportions=True,
                trailing=(
                    [
                        {
                            "value": "__none__",
                            "name": "No URL",
                            "count": pool["no_url"],
                            "active": current_host == "__none__",
                        },
                    ]
                    if pool["no_url"]
                    else None
                ),
            ),
            facets.facet_counts_group(
                "format",
                "Format",
                "Filter by format",
                [(f["fmt"], f["fmt"]) for f in pool["formats"]],
                {f["fmt"]: f["count"] for f in pool["formats"]},
                current_format,
                proportions=True,
                cutoff=FORMAT_FACET_CUTOFF,
                toggle_base=base_params,
                toggle_param="formats",
                toggle_label="formats",
                expanded=formats_expanded,
                list_id="format-facet-list",
                trailing=(
                    [
                        {
                            "value": "__none__",
                            "name": "No format",
                            "count": pool["no_format"],
                            "active": current_format == "__none__",
                        },
                    ]
                    if pool["no_format"]
                    else None
                ),
            ),
            facets.facet_counts_group(
                "year",
                "Year created",
                "Filter by year created",
                [(y["year"], y["year"]) for y in pool["years"]],
                {y["year"]: y["count"] for y in pool["years"]},
                current_year,
                proportions=True,
            ),
        )
        if group is not None
    ]

    return render(
        request,
        "links.html",
        {
            "title": "Links — data.gov.uk Explorer",
            "section": "links",
            "links": link_rows,
            "facet_groups": facet_groups,
            "current_host": current_host,
            "current_format": current_format,
            "current_year": current_year,
            "facet_qs": facet_qs,
            "facet_url": facet_url,
            "total_links": stats.get("total") or 0,
            "filtered_links": total,
            "no_url_links": no_url_links,
            "internal_links": stats.get("internal") or 0,
            "external_links": (stats.get("total") or 0) - (stats.get("internal") or 0) - no_url_links,
            "total_orgs": stats.get("orgs") or 0,
            "page": current_page,
            "total_pages": total_pages,
            "page_size": PAGE_SIZE,
            "start_index": offset + 1,
            "end_index": offset + len(link_rows),
            "sort": sort,
            "dir": dir_,
        },
    )
