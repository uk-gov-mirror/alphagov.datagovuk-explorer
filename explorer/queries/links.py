"""/links query builder — count/list statements compiled per
(filters, sort, dir), plus the self-excluding SQL sidebar facet pools
(host/format/year) built from the same clause builders."""

import functools

from .core import Query, cached_unfiltered, facet_where

# --- /links query builder ---
#
# filters: { host: None | host | "__none__", format, year }.

# Sortable columns for the links table on the /links page
LINK_SORT_COLUMNS = ["name", "host", "format", "dataset_title", "org_display_name"]

# Column key → SQL ORDER BY expression (text columns, COALESCE'd, qualified
# with the l alias because the query joins against datasets)
LINK_SORT_EXPRS = {
    "name": "COALESCE(l.name, '')",
    "host": "COALESCE(l.host, '')",
    "format": "COALESCE(l.format_norm, '')",
    "dataset_title": "COALESCE(l.dataset_title, '')",
    "org_display_name": "COALESCE(l.org_display_name, '')",
}

# Per-facet clause builders — the same (filters, exclude) →
# ([clause, ...], [param, ...]) shape as datasets.py. One builder dict,
# two consumers: links_stmts ANDs everything for the list/count WHERE, and
# each facet pool omits its own group via core.facet_where.


def _host_clause(filters: dict, exclude: str | None) -> tuple[list, list]:
    """host WHERE fragment + params, or ([], []) when skipped/excluded."""
    if exclude == "host":
        return [], []
    host = filters.get("host")
    if host == "__none__":
        return ["l.host IS NULL"], []
    if host:
        return ["l.host = %s"], [host]
    return [], []


def _format_clause(filters: dict, exclude: str | None) -> tuple[list, list]:
    """format WHERE fragment + params, or ([], []) when skipped/excluded.
    __none__ is the no-format selection (format_norm NULL or '')."""
    if exclude == "format":
        return [], []
    fmt = filters.get("format")
    if fmt == "__none__":
        return ["(l.format_norm IS NULL OR l.format_norm = '')"], []
    if fmt:
        return ["l.format_norm = %s"], [fmt]
    return [], []


def _year_clause(filters: dict, exclude: str | None) -> tuple[list, list]:
    """year WHERE fragment + params, or ([], []) when skipped/excluded."""
    if exclude == "year":
        return [], []
    year = filters.get("year")
    if year:
        return ["l.year_created = %s"], [year]
    return [], []


_LINKS_CLAUSES = {
    "host": _host_clause,
    "format": _format_clause,
    "year": _year_clause,
}


# Compiled statements, keyed by the filter/sort/dir tuple.


def links_stmts(filters: dict, sort: str, dir_: str) -> dict:
    """Return { count, list, params } for one (filters, sort, dir) combo."""
    where, params = facet_where(_LINKS_CLAUSES, filters)
    # All filter columns (host, format_norm, year_created) and every sort
    # column live on links itself, so no join is needed.
    from_ = "FROM links l"

    order_sql = f"LOWER({LINK_SORT_EXPRS[sort]}) {'DESC' if dir_ == 'desc' else 'ASC'}"
    # `, l.id` tiebreak pins tied rows to id order — an unpinned ORDER BY
    # would reshuffle pages, e.g. every NULL-host link under "No URL". It
    # only affects ties; the primary ordering is unchanged.
    order_sql += ", l.id"

    entry = {
        "params": params,
        "count": Query(f"SELECT COUNT(*) AS n {from_} {where}"),
        "list": Query(
            "SELECT l.id, l.resource_id, l.dataset_id, l.org_slug, l.org_display_name,"
            "  l.dataset_title, l.name, l.description, l.url, l.host,"
            "  l.format_norm AS format, l.format AS format_raw, l.position"
            f" {from_} {where}"
            f" ORDER BY {order_sql}"
            " LIMIT %s OFFSET %s",
        ),
    }
    return entry


# --- Sidebar facet counts (self-excluding SQL aggregates) ---
#
# Each group counts over the pool filtered by the *other* groups (excluding
# its own), so a selected host/format/year shrinks the sibling counts
# instead of dead-ending into 0 results. The "No URL" trailing bucket is
# the same pool with host IS NULL.


def _links_facet_counts(filters: dict) -> dict:
    """Compiled facet-count statements for one (host/format/year) combo —
    the {hosts, no_url, formats, no_format, years} Queries plus
    per-statement params."""
    host_frag, host_params = facet_where(_LINKS_CLAUSES, filters, exclude="host")
    format_frag, format_params = facet_where(_LINKS_CLAUSES, filters, exclude="format")
    year_frag, year_params = facet_where(_LINKS_CLAUSES, filters, exclude="year")

    # The pool guards join the (possibly empty) WHERE fragments. `no_url`
    # reuses the host fragment (the other groups' filters) with host IS
    # NULL instead; `no_format` does the same with format_norm NULL/''.
    host_where = f"{host_frag} AND l.host IS NOT NULL" if host_frag else " WHERE l.host IS NOT NULL"
    no_url_where = f"{host_frag} AND l.host IS NULL" if host_frag else " WHERE l.host IS NULL"
    format_where = (
        f"{format_frag} AND l.format_norm IS NOT NULL AND l.format_norm != ''"
        if format_frag
        else " WHERE l.format_norm IS NOT NULL AND l.format_norm != ''"
    )
    no_format_where = (
        f"{format_frag} AND (l.format_norm IS NULL OR l.format_norm = '')"
        if format_frag
        else " WHERE (l.format_norm IS NULL OR l.format_norm = '')"
    )
    year_where = (
        f"{year_frag} AND l.year_created IS NOT NULL AND l.year_created != ''"
        if year_frag
        else " WHERE l.year_created IS NOT NULL AND l.year_created != ''"
    )

    entry = {
        "params": {
            "hosts": host_params,
            "no_url": host_params,
            "formats": format_params,
            "no_format": format_params,
            "years": year_params,
        },
        "hosts": Query(
            "SELECT host, COUNT(*) AS count"
            " FROM links l"
            f"{host_where}"
            " GROUP BY host ORDER BY count DESC, LOWER(host)"
            " LIMIT 12",
        ),
        "no_url": Query(f"SELECT COUNT(*) AS n FROM links l{no_url_where}"),
        "formats": Query(
            "SELECT format_norm AS fmt, COUNT(*) AS count"
            " FROM links l"
            f"{format_where}"
            " GROUP BY format_norm ORDER BY count DESC, LOWER(format_norm)",
        ),
        "no_format": Query(f"SELECT COUNT(*) AS n FROM links l{no_format_where}"),
        "years": Query(
            "SELECT year_created AS year, COUNT(*) AS count"
            " FROM links l"
            f"{year_where}"
            " GROUP BY year_created ORDER BY year_created DESC",
        ),
    }
    return entry


@cached_unfiltered
def links_facet_counts(filters: dict) -> dict:
    """Sidebar facet counts for /links — each group counts over the pool
    filtered by the other groups (self-excluding). Returns:

      'hosts':     [{'host': ..., 'count': n}, ...] (top 12)
      'no_url':    int — links with no host (the trailing bucket)
      'formats':   [{'fmt': ..., 'count': n}, ...]
      'no_format': int — links with no format (the trailing bucket)
      'years':     [{'year': ..., 'count': n}, ...]

    No-filter calls return the memoised unfiltered pools via
    core.cached_unfiltered — the /links view calls links_facet_counts({})
    on *every* request for its format/year validation whitelists, and the
    no_url/no_format pools alone are ~130ms full scans of links — while
    filtered calls run live (their key space is unbounded, so they can't
    be cached).
    """
    entry = _links_facet_counts(filters)
    p = entry["params"]
    return {
        "hosts": entry["hosts"].all(*p["hosts"]),
        "no_url": (entry["no_url"].get(*p["no_url"]) or {}).get("n", 0),
        "formats": entry["formats"].all(*p["formats"]),
        "no_format": (entry["no_format"].get(*p["no_format"]) or {}).get("n", 0),
        "years": entry["years"].all(*p["years"]),
    }


# --- Fixed statements (the /links report header) ---

# Aggregate link stats for the /links report header
LINKS_STATS = Query(
    """SELECT
         COUNT(*) AS total,
         COUNT(DISTINCT org_slug) AS orgs,
         SUM(CASE WHEN host IS NULL THEN 1 ELSE 0 END) AS no_url,
         SUM(CASE WHEN host = 'data.gov.uk' OR host LIKE '%%.data.gov.uk' THEN 1 ELSE 0 END) AS internal
       FROM links""",
)


@functools.cache
def links_stats() -> dict:
    """LINKS_STATS row (or {}) — memoised: build-time snapshot."""
    return LINKS_STATS.get() or {}
