"""GET /organisations — all organisations (server-side sortable, facet
filters).

Facets use the same pattern as /datasets:
  ?year=YYYY     — year created (single-select, years from the chart data)
  ?pubyear=YYYY[,YYYY] — year last published (comma-separated multi-select;
                    __none__ selects orgs that have never published)
  ?datasets=...  — dataset-count bucket (0|1-10|11-50|51-100|101-500|501-1000|1000+)

Sidebar facet counts are self-excluding SQL aggregates from
queries/organisations.py (organisations_facet_counts) — each group counts
over the pool filtered by the other two groups, exactly like /datasets.
The page list/filter/sort itself stays Python-side (the org list is small).

Sort columns are whitelisted in explorer.sort.SORT_COLUMNS; unknown keys
fall back to the default (name asc).
"""

from dataclasses import dataclass

from django.shortcuts import render

from explorer import facets
from explorer.helpers import format_date
from explorer.queries.organisations import (
    DATASET_BUCKET_NAMES,
    DATASET_BUCKET_TESTS,
    DATASET_BUCKETS,
    VALID_DATASET_BUCKETS,
    all_org_rows,
    org_aggregate_rows,
    organisations_facet_counts,
    yearly_org_counts,
)
from explorer.sort import SORT_COLUMNS, sort_orgs

from .core import _sort_dir


def _merge_org_rows(org_rows, agg_rows) -> list[dict]:
    """Merge the org rows with the one-per-org aggregate pass into display
    rows. Owns the format_date / last_published_year / has_data
    computations. Any org_slug in agg_rows has at least one dataset, so the
    slug set doubles as the fetched-slugs set."""
    fetched_slugs = {r["org_slug"] for r in agg_rows}
    resources_by_org = {r["org_slug"]: r["total_resources"] for r in agg_rows}
    views_by_org = {r["org_slug"]: r["total_views"] for r in agg_rows}
    last_published_by_org = {r["org_slug"]: r["last_published"] for r in agg_rows}

    rows = []
    for o in org_rows:
        last_pub = last_published_by_org.get(o["slug"])
        rows.append(
            {
                "slug": o["slug"],
                "name": o["display_name"] or o["title"] or o["name"],
                "dataset_count": o["package_count"] or 0,
                "resource_count": resources_by_org.get(o["slug"]) or 0,
                "views": views_by_org.get(o["slug"]) or 0,
                "type": o["type"],
                "state": o["state"],
                "approval_status": o["approval_status"],
                "created": format_date(o["created"]),
                "last_published": format_date(last_pub),
                "last_published_year": last_pub[:4] if last_pub else None,
                "has_data": o["slug"] in fetched_slugs,
            },
        )
    return rows


@dataclass(frozen=True)
class OrgFilters:
    """The three validated /organisations facet selections (None = not active)."""

    year: str | None
    pub_years: tuple[str, ...] | None
    datasets: str | None


def _parse_filters(request, valid_years, valid_pub_years) -> OrgFilters:
    """Validate the /organisations facet selections from request.GET.

    year is single-select; pubyear is a comma-separated multi-select (every
    value must be a valid year, or the selection must be exactly the
    never-published marker __none__ — never-published orgs are mutually
    exclusive with any real year, so mixed selections are rejected);
    datasets is a bucket key from VALID_DATASET_BUCKETS.
    """
    year = request.GET.get("year")
    current_year = year if year in valid_years else None

    pubyear = request.GET.get("pubyear")
    current_pub_years = None
    if pubyear is not None:
        py = pubyear.split(",")
        if py == ["__none__"]:
            current_pub_years = ("__none__",)
        elif py and all(y in valid_pub_years for y in py):
            current_pub_years = tuple(py)

    datasets = request.GET.get("datasets")
    current_datasets = datasets if datasets in VALID_DATASET_BUCKETS else None

    return OrgFilters(
        year=current_year,
        pub_years=current_pub_years,
        datasets=current_datasets,
    )


def _matches_pub_year(o: dict, pub_years: tuple[str, ...] | None) -> bool:
    """Org matches the pubyear selection: a real last-published year, or the
    never-published bucket (__none__) for orgs with no last_published.
    None (no selection) matches everything."""
    if pub_years is None:
        return True
    if "__none__" in pub_years:
        return o["last_published_year"] is None
    return o["last_published_year"] in pub_years


def _apply_filters(rows, filters: OrgFilters) -> list[dict]:
    """Rows matching all active facet filters (filtering happens before
    sorting)."""
    return [
        o
        for o in rows
        if (filters.year is None or o["created"][:4] == filters.year)
        and _matches_pub_year(o, filters.pub_years)
        and (filters.datasets is None or DATASET_BUCKET_TESTS[filters.datasets](o["dataset_count"]))
    ]


def organisations(request):
    """GET /organisations — all orgs, server-side sortable, with facets."""
    # Three fetches, all memoised in queries/organisations.py (build-time
    # snapshot): org rows, the merged per-org aggregate pass, and the
    # yearly-created chart counts. The per-org aggregate is the page's
    # dominant cost (~145ms), so it's cached after the first request.
    org_rows = all_org_rows()
    agg_rows = org_aggregate_rows()
    yearly = yearly_org_counts()

    rows = _merge_org_rows(org_rows, agg_rows)

    sort, dir_ = _sort_dir(request, SORT_COLUMNS, "name")

    # Facet master lists — the validation whitelists and the facet builders
    # consume these (computed once, not per consumer). Year comes from the
    # chart data; pubyear years come from the merged rows.
    years = [y["year"] for y in yearly][::-1]
    pub_years = sorted(
        {o["last_published_year"] for o in rows if o["last_published_year"]},
        reverse=True,
    )

    filters = _parse_filters(request, set(years), set(pub_years))
    shown_rows = _apply_filters(rows, filters)

    # Sort happens after filtering
    sort_orgs(shown_rows, sort, dir_)

    pubyear_param = ",".join(filters.pub_years) if filters.pub_years else None

    # Shared query-string base: sort, dir, then the active facets (year,
    # pubyear, datasets) in a fixed order.
    base_params = facets.preserve_params(
        sort,
        dir_,
        [
            ("year", filters.year),
            ("pubyear", pubyear_param),
            ("datasets", filters.datasets),
        ],
    )
    facet_url = facets.facet_url_for(base_params)

    # Fragment for sort/pagination links — the sort_link/pagination macros
    # append it to ?sort=..&dir=.., so sort/dir come off here.
    facet_qs = facets.facet_qs(base_params, include_sort=False)

    # Sidebar facet groups (pool counts + current selection -> group).
    # Counts are self-excluding SQL aggregates (each group applies the other
    # two filters, omitting its own) via the shared core.facet_where helper;
    # the shared builders only assemble them into items.
    facet_counts = organisations_facet_counts(
        {
            "year": filters.year,
            "pubyear": filters.pub_years,
            "datasets": filters.datasets,
        },
    )
    bucket_counts = {r["bucket"]: r["count"] for r in facet_counts["datasets"]}
    year_pool_counts = {r["year"]: r["count"] for r in facet_counts["year"]}
    pub_year_pool_counts = {r["year"]: r["count"] for r in facet_counts["pubyear"]}

    # The pubyear facet's trailing bucket — orgs that have never published
    # (no datasets at all). Its href toggles __none__ in the selection:
    # clicking it replaces any selected years (an org either has a last-
    # published year or it doesn't), clicking it again clears the facet.
    pubyear_trailing = []
    if facet_counts["no_pubyear"]:
        pubyear_trailing.append(
            {
                "value": "__none__",
                "name": "Never published",
                "count": facet_counts["no_pubyear"],
                "active": filters.pub_years == ("__none__",),
                "href": (
                    facet_url("pubyear", "") if filters.pub_years == ("__none__",) else facet_url("pubyear", "__none__")
                ),
            },
        )

    facet_groups = [
        group
        for group in (
            facets.facet_counts_group(
                "datasets",
                "Datasets",
                "Filter by number of datasets",
                [(value, name) for value, name in DATASET_BUCKETS],
                bucket_counts,
                filters.datasets,
                proportions=True,
            ),
            facets.facet_counts_group(
                "year",
                "Year created",
                "Filter by year created",
                [(y, y) for y in years],
                year_pool_counts,
                filters.year,
                proportions=True,
            ),
            facets.facet_counts_multiselect_group(
                "pubyear",
                "Year last published",
                "Filter by year last published",
                [(y, y) for y in pub_years],
                pub_year_pool_counts,
                filters.pub_years,
                facet_url=facet_url,
                proportions=True,
                trailing=pubyear_trailing or None,
            ),
        )
        if group is not None
    ]

    return render(
        request,
        "organisations.html",
        {
            "title": "data.gov.uk — Explorer",
            "section": "orgs",
            "orgs": shown_rows,
            "total_orgs": len(rows),
            "shown_orgs": len(shown_rows),
            "total_datasets": sum(o["dataset_count"] for o in rows),
            "sort": sort,
            "dir": dir_,
            "yearly": yearly,
            "year": filters.year,
            "pubyear": (
                "Never published"
                if filters.pub_years == ("__none__",)
                else ", ".join(filters.pub_years)
                if filters.pub_years
                else None
            ),
            "datasets": filters.datasets,
            "datasets_label": (DATASET_BUCKET_NAMES[filters.datasets] if filters.datasets else None),
            "facet_groups": facet_groups,
            "facet_qs": facet_qs,
            "facet_url": facet_url,
        },
    )
