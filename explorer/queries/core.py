"""Row fetching and the Query class — the plumbing every other
queries module builds on.

Placeholder rule: ALL raw SQL through django.db.connection uses psycopg3's
native `%s` placeholders. Params are always passed to the cursor (even
empty tuples), so a literal `%` in a statement must be written doubled
(%%…%%) — psycopg3's client-side binding converts %% back to % on every
path. This is the standard psycopg3 rule; the doubled-percent LIKE
patterns in links.py/metadata.py are the easiest thing to corrupt when
moving statements, so keep them verbatim.
"""

from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from functools import cache, wraps
from typing import Any

from django.db import connection


def _fetch_all(sql: str, params: tuple) -> list[dict]:
    """Run a SELECT and return rows as dicts keyed by column name.

    Params are always passed, even when empty, so psycopg3's client-side
    binding runs on every path: `%%` → `%` consistently, and a stray
    literal `%` in a no-param statement raises instead of reaching
    Postgres (a bug catcher, not a workaround).
    """
    with connection.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


class Query:
    """A compiled SQL query with get/all methods.

    Statements are built per (filters, sort, dir) by the builders that
    create them; the SQL itself is cheap to rebuild, so no caching.
    """

    def __init__(self, sql: str):
        self._sql = sql

    def get(self, *params: Any) -> dict | None:
        """First row or None."""
        rows = _fetch_all(self._sql, params)
        return rows[0] if rows else None

    def all(self, *params: Any) -> list[dict]:
        """All rows."""
        return _fetch_all(self._sql, params)


# ── Shared self-excluding-facet-counts helper ─────────────────────────────
# Every facet page's query module (/datasets, /links, /organisations,
# /report/{key}) builds its sidebar facet counts from this one primitive:
# each facet group counts over the pool filtered by every *other* active
# facet, omitting its own ("if I clicked this option, given my other
# filters"). The per-page clause builders are dicts keyed by facet key;
# facet_where ANDs every clause except the excluded one.


def facet_where(
    clause_builders: Mapping[str, Callable[[dict, str | None], tuple[list, list]]],
    filters: dict,
    exclude: str | None = None,
) -> tuple[str, list]:
    """WHERE fragment + params from a dict of per-facet clause builders,
    omitting `exclude` (the facet group being counted).

    Each builder takes (filters, exclude) and returns ([clause, ...],
    [param, ...]) — or ([], []) when its filter isn't active (or when
    `exclude` names it). Clauses are AND-ed in dict order; the fragment
    is " WHERE a AND b" or "".

    Self-exclusion is per-facet, not global: only the builder whose key
    matches `exclude` is skipped — the other builders still apply their
    filters, so a facet's own count pool reflects every other active
    filter (and the list/count queries call with exclude=None to apply
    everything).
    """
    clauses: list = []
    params: list = []
    for builder in clause_builders.values():
        c, p = builder(filters, exclude)
        clauses.extend(c)
        params.extend(p)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


# ── Unfiltered-result memoisation ────────────────────────────────────────
# The DB is a build-time snapshot, so a facet page's no-filter pools are
# constant between rebuilds — the common page view can be computed once per
# process instead of per request. Filtered calls stay live: their key space
# (facet values x sort x page) is unbounded, so caching them would hold dead
# entries forever. This is the one caching rule the facet pages share (the
# dashboard's cards() is memoised wholesale for the same reason).


def cached_unfiltered(compute: Callable[[dict], Any]) -> Callable[[dict], Any]:
    """Memoise a filters-keyed function's no-filter result.

    Wraps a function taking a filters dict whose values are None/''/() when
    a facet isn't selected (the clause builders treat falsy as inactive).
    The first call with no active filter values computes compute({}) and
    memoises it; every later no-filter call returns that instead of hitting
    the DB. Calls with any active value run compute(filters) live.

    Restart the process to refresh after a DB rebuild (same contract as the
    dashboard's cards() cache).
    """

    @cache
    def unfiltered() -> Any:
        return compute({})

    @wraps(compute)
    def wrapper(filters: dict) -> Any:
        if not any(filters.values()):
            return unfiltered()
        return compute(filters)

    return wrapper


# ── Parallel fetch ───────────────────────────────────────────────────────
# Several pages run several independent SELECTs per request (dashboard counts,
# facet tables, ...); a sync Django view runs them sequentially on one
# connection. This helper overlaps the round-trips: each fetch runs on a
# pool thread with its own thread-local connection.
#
# Prefer a single merged GROUP BY over this helper where the queries can
# share a table scan (see ORG_AGGREGATES in queries/organisations.py) —
# it needs no threads at all. Use this helper for pages whose queries
# can't be merged (e.g. the home page's per-item counts, /datasets).
#
# Connections are closed after every call (finally), so the reused threads
# never leave idle connections behind between requests — the failure mode
# this pattern is known for. Statements that must share a transaction must
# NOT go through here (only single independent SELECTs).

_pool = ThreadPoolExecutor(max_workers=5, thread_name_prefix="dgfetch")


def _run_closed(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    finally:
        # Django connections are thread-local: this closes the worker
        # thread's own connection, not the request thread's.
        connection.close()


def fetch_parallel(fns: list[Callable[[], Any]]) -> list[Any]:
    """Run zero-arg fetch callables concurrently; return results in order.

    Each callable runs on a pool thread with its own DB connection, which
    is closed when the call finishes. The request thread's connection (if
    any) is untouched.
    """
    futures = [_pool.submit(_run_closed, fn) for fn in fns]
    return [f.result() for f in futures]
