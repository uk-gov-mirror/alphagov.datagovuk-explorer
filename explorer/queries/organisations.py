"""Organisation statements — the fixed queries against the
organisations/datasets tables the org pages and home dashboard build
on, plus the DB-backed yearly-org-count helper and the self-excluding
SQL facet pools for /organisations' sidebar.

The DB is a build-time snapshot, so the fixed parameterless fetches
(org rows, per-org dataset aggregates, yearly counts) are memoised via
functools.cache — computed once per process, then served from memory.
The /organisations page is the slowest of the facet pages because its
per-org aggregate over datasets (ORG_AGGREGATES) and the pubyear pool's
embedded subquery each scan the whole datasets table (~290ms per
request before memoisation). Restart the process to refresh after a DB
rebuild (same contract as the dashboard's cards() cache).

Only the *fixed* fetches are memoised: filtered facet pools stay live
because their filter key space is unbounded (pubyear is a multi-select).
The raw Query objects (ORGS, ORG_AGGREGATES, ...) stay uncached so any
parameterised use elsewhere is unaffected."""

import functools
from typing import Any

from explorer.helpers import yearly_counts

from .core import Query, cached_unfiltered, facet_where

# Dataset-count facet buckets — 0 plus fixed ranges covering the full
# spread (orgs are heavily skewed small, so the low end is fine-grained).
# The bucket upper edges are listed once; keys, labels and membership
# tests are derived from them so ranges and comparisons can't drift apart.
DATASET_BUCKET_EDGES = (0, 10, 50, 100, 500, 1000)


def _dataset_bucket(lo: int, hi: int) -> tuple[str, str]:
    """(key, label) for the inclusive range [lo, hi]."""
    return (f"{lo}-{hi}", f"{lo:,}-{hi:,}")


def _dataset_buckets(edges: tuple[int, ...]) -> list[tuple[str, str]]:
    """Buckets: 0, then [1, edge1], [edge1+1, edge2], …, open-ended top."""
    buckets: list[tuple[str, str]] = [("0", "0")]
    lo = 1
    for hi in edges[1:]:
        buckets.append(_dataset_bucket(lo, hi))
        lo = hi + 1
    top = edges[-1]
    buckets.append((f"{top}+", f"{top:,}+"))
    return buckets


DATASET_BUCKETS = _dataset_buckets(DATASET_BUCKET_EDGES)
DATASET_BUCKET_NAMES = dict(DATASET_BUCKETS)
VALID_DATASET_BUCKETS = set(DATASET_BUCKET_NAMES)

# Inclusive [lo, hi] per bucket key (hi=None for the open-ended top) — the
# boundary source for both the Python membership tests and the SQL clauses.
DATASET_BUCKET_RANGES: dict[str, tuple[int, int | None]] = {}
for _value, _ in DATASET_BUCKETS:
    if _value == "0":
        DATASET_BUCKET_RANGES[_value] = (0, 0)
    elif _value.endswith("+"):
        DATASET_BUCKET_RANGES[_value] = (int(_value[:-1]) + 1, None)
    else:
        lo, hi = _value.split("-")
        DATASET_BUCKET_RANGES[_value] = (int(lo), int(hi))


# The membership tests the view's Python-side list filtering uses —
# dataset_count is package_count or 0, exactly like the SQL COALESCE below.
def _bucket_test(lo: int, hi: int | None):
    """Membership predicate for one bucket: inclusive [lo, hi], n > lo for
    the open-ended top, and the "0" bucket falls out as [0, 0] ≡ n == 0."""
    if hi is None:
        return lambda n, lo=lo: n > lo
    return lambda n, lo=lo, hi=hi: lo <= n <= hi


DATASET_BUCKET_TESTS = {value: _bucket_test(lo, hi) for value, (lo, hi) in DATASET_BUCKET_RANGES.items()}

# CASE expression mapping COALESCE(package_count, 0) to its bucket key —
# generated from the same ranges so the SQL boundaries can't drift.
_DATASET_BUCKET_CASE = (
    "CASE "
    + " ".join(
        (
            f"WHEN COALESCE(o.package_count, 0) > {lo} THEN '{value}'"
            if hi is None
            else f"WHEN COALESCE(o.package_count, 0) BETWEEN {lo} AND {hi} THEN '{value}'"
        )
        for value, (lo, hi) in DATASET_BUCKET_RANGES.items()
    )
    + f" ELSE '{DATASET_BUCKETS[-1][0]}' END"
)


# All organisations, in display-name order
ORGS = Query(
    """SELECT slug, name, display_name, package_count, type, state,
              approval_status, created, title
         FROM organisations
         ORDER BY LOWER(display_name), slug""",
)

# One organisation
ORG = Query(
    """SELECT slug, name, display_name, package_count, type, state,
              approval_status, created, title
         FROM organisations WHERE slug = %s""",
)

# Per-org aggregates over datasets — one pass over the table. Any
# org_slug present here has at least one dataset, so it doubles as the
# fetched-slugs set.
ORG_AGGREGATES = Query(
    """SELECT org_slug,
              SUM(resource_count) AS total_resources,
              SUM(views) AS total_views,
              MAX(metadata_created) AS last_published
       FROM datasets GROUP BY org_slug""",
)

# Total resources per org
RESOURCE_COUNTS = Query(
    """SELECT org_slug, SUM(resource_count) AS total
       FROM datasets GROUP BY org_slug""",
)

# Total views per org
VIEWS_BY_ORG = Query(
    """SELECT org_slug, SUM(views) AS total
       FROM datasets GROUP BY org_slug""",
)

# Most recent dataset publication timestamp per org
LAST_PUBLISHED_BY_ORG = Query(
    """SELECT org_slug, MAX(metadata_created) AS last_published
       FROM datasets WHERE metadata_created IS NOT NULL
       GROUP BY org_slug""",
)


# Orgs created per year (YYYY) — created is always ISO, so
# substr(created, 1, 4) is the year; the regex skips anything non-ISO.
YEARLY_ORGS = Query(
    r"""SELECT substr(created, 1, 4) AS year, COUNT(*) AS count
       FROM organisations WHERE created ~ '^\d{4}'
       GROUP BY substr(created, 1, 4)""",
)


@functools.cache
def all_org_rows() -> list[dict[str, Any]]:
    """All organisations (ORGS.all) — memoised: build-time snapshot."""
    return ORGS.all()


@functools.cache
def org_aggregate_rows() -> list[dict[str, Any]]:
    """One-pass per-org aggregates over datasets (ORG_AGGREGATES.all) —
    memoised: build-time snapshot. The dominant cost on /organisations."""
    return ORG_AGGREGATES.all()


@functools.cache
def yearly_org_counts() -> list[dict[str, Any]]:
    """Count organisations created per year (YYYY), continuous from first
    to last year — memoised: build-time snapshot."""
    return yearly_counts(YEARLY_ORGS.all())


# --- /organisations sidebar facet pools (self-excluding SQL aggregates) ---
#
# Each group counts over the pool filtered by the other two groups via the
# shared core.facet_where helper — the same one-sentence algorithm as
# /datasets, expressed over organisations joined to a per-org
# last-published aggregation of datasets. The list/count filtering stays
# Python-side in the view (the org list is small); only the sidebar pools
# move to SQL.

# Per-org last-published aggregate — the LEFT JOIN base all three pools
# share (the pubyear clause/pool reads a.last_published from it).
_ORG_AGG = (
    "LEFT JOIN ("
    "  SELECT org_slug, MAX(metadata_created) AS last_published"
    "  FROM datasets GROUP BY org_slug"
    ") a ON a.org_slug = o.slug"
)

# Pool guards: the \d{4} created-year skip (created is always ISO) and
# the last-published IS NOT NULL skip (orgs with no datasets land in no
# pubyear bucket).
_YEAR_CREATED_GUARD = r"substr(o.created, 1, 4) ~ '^\d{4}'"
_PUB_YEAR_GUARD = "a.last_published IS NOT NULL"


def _year_created_clause(filters: dict, exclude: str | None) -> tuple[list, list]:
    """year (created) WHERE fragment + params, or ([], []) when skipped/excluded."""
    if exclude == "year":
        return [], []
    year = filters.get("year")
    if year:
        return ["substr(o.created, 1, 4) = %s"], [year]
    return [], []


def _pub_year_clause(filters: dict, exclude: str | None) -> tuple[list, list]:
    """pubyear (last published year, multi-select) WHERE fragment + params,
    or ([], []) when skipped/excluded. Matches orgs whose MAX(metadata_created)
    year is one of the selected years — the view's
    o["last_published_year"] in filters.pub_years check. __none__ is the
    never-published selection (orgs with no datasets → a.last_published NULL)."""
    if exclude == "pubyear":
        return [], []
    pub_years = filters.get("pubyear")
    if pub_years:
        if "__none__" in pub_years:
            return ["a.last_published IS NULL"], []
        return ["substr(a.last_published, 1, 4) = ANY(%s)"], [list(pub_years)]
    return [], []


def _datasets_bucket_clause(filters: dict, exclude: str | None) -> tuple[list, list]:
    """datasets (dataset-count bucket) WHERE fragment + params, or ([], [])
    when skipped/excluded. Boundaries come from DATASET_BUCKET_RANGES — the
    same edges as the Python tests, with package_count COALESCE'd to 0 to
    mirror the view's `or 0`."""
    if exclude == "datasets":
        return [], []
    bucket = filters.get("datasets")
    if bucket:
        lo, hi = DATASET_BUCKET_RANGES[bucket]
        col = "COALESCE(o.package_count, 0)"
        if hi is None:
            return [f"{col} > %s"], [lo]
        return [f"{col} BETWEEN %s AND %s"], [lo, hi]
    return [], []


# The three clause builders keyed by facet — the dict core.facet_where
# ANDs together (minus the excluded facet) for the pools.
_ORG_FACET_CLAUSES = {
    "year": _year_created_clause,
    "pubyear": _pub_year_clause,
    "datasets": _datasets_bucket_clause,
}


def _org_facet_counts(filters: dict) -> dict:
    """Compiled facet-count statements for one (year/pubyear/datasets) combo
    — the {year, pubyear, no_pubyear, datasets} Queries plus per-statement
    params."""
    year_where, year_params = facet_where(_ORG_FACET_CLAUSES, filters, exclude="year")
    pubyear_frag, pubyear_params = facet_where(_ORG_FACET_CLAUSES, filters, exclude="pubyear")
    datasets_where, datasets_params = facet_where(_ORG_FACET_CLAUSES, filters, exclude="datasets")

    # The pool guards join the (possibly empty) WHERE fragments. `no_pubyear`
    # reuses the pubyear fragment (the other groups' filters) with the guard
    # flipped — last_published IS NULL (orgs with no datasets) instead of the
    # year-list guard's IS NOT NULL.
    year_where = f"{year_where} AND {_YEAR_CREATED_GUARD}" if year_where else f" WHERE {_YEAR_CREATED_GUARD}"
    pubyear_where = f"{pubyear_frag} AND {_PUB_YEAR_GUARD}" if pubyear_frag else f" WHERE {_PUB_YEAR_GUARD}"
    no_pubyear_where = (
        f"{pubyear_frag} AND a.last_published IS NULL" if pubyear_frag else " WHERE a.last_published IS NULL"
    )

    entry = {
        "params": {
            "year": year_params,
            "pubyear": pubyear_params,
            "no_pubyear": pubyear_params,
            "datasets": datasets_params,
        },
        "year": Query(
            "SELECT substr(o.created, 1, 4) AS year, COUNT(*) AS count"
            f" FROM organisations o {_ORG_AGG}{year_where}"
            " GROUP BY substr(o.created, 1, 4)",
        ),
        "pubyear": Query(
            "SELECT substr(a.last_published, 1, 4) AS year, COUNT(*) AS count"
            f" FROM organisations o {_ORG_AGG}{pubyear_where}"
            " GROUP BY substr(a.last_published, 1, 4)",
        ),
        "no_pubyear": Query(f"SELECT COUNT(*) AS n FROM organisations o {_ORG_AGG}{no_pubyear_where}"),
        # One pass over the (1:1-joined) org rows; every org lands in exactly
        # one bucket via the ELSE top.
        "datasets": Query(
            f"SELECT {_DATASET_BUCKET_CASE} AS bucket, COUNT(*) AS count"
            f" FROM organisations o {_ORG_AGG}{datasets_where}"
            " GROUP BY 1",
        ),
    }
    return entry


def _run_facet_counts(filters: dict) -> dict:
    """Compile + fetch the three self-excluding sidebar pools for one
    (year/pubyear/datasets) filter combo."""
    entry = _org_facet_counts(filters)
    p = entry["params"]
    return {
        "year": entry["year"].all(*p["year"]),
        "pubyear": entry["pubyear"].all(*p["pubyear"]),
        "no_pubyear": (entry["no_pubyear"].get(*p["no_pubyear"]) or {}).get("n", 0),
        "datasets": entry["datasets"].all(*p["datasets"]),
    }


@cached_unfiltered
def organisations_facet_counts(filters: dict) -> dict:
    """Sidebar facet counts for /organisations — each group counts over the
    pool filtered by the other two groups (self-excluding). Returns:

      'year':        [{'year': 'YYYY', 'count': n}, ...]
      'pubyear':     [{'year': 'YYYY', 'count': n}, ...]
      'no_pubyear':  int — orgs with no last-published year (the
                     "Never published" trailing bucket)
      'datasets':    [{'bucket': '0'|'1-10'|..., 'count': n}, ...]

    No-filter calls (the common view) return the memoised unfiltered pools
    via core.cached_unfiltered; only calls with an active filter run the SQL
    live (their key space is unbounded, so they can't be cached).
    """
    return _run_facet_counts(filters)
