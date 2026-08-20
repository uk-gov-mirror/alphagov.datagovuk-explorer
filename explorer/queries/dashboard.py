"""Home-dashboard card data for GET / — the summary cards grouped by kind.

The dashboard is a pure aggregation page: totals from the domain tables
(datasets / links / organisations) plus one count per data-quality report
(REPORTS from queries/reports). All of that assembly lives here, so the
view (views/dashboard.py) is a thin render of dashboard.html.

Every fetch is an independent single-SELECT aggregate over the build-time
snapshot. cards() is memoised (functools.cache): the first request per
process computes everything — all ~17 queries run concurrently via
core.fetch_parallel instead of sequentially on the request connection
(sequential execution is what made the dashboard slow — the reports alone
scan the datasets/links tables ~12 times) — and every later request
serves the result from memory. Each pool thread has its own connection,
closed after the call.

Memoisation contract: the DB is a build-time snapshot, so the dashboard
only changes when the DB is rebuilt — restart the process to refresh
after a rebuild (same contract the active-org card's cache used to have).

`_active_card` is a pure function of the org rows fetched here; it used to
carry its own functools.cache because it did its own two fetches, but the
rows are already in hand for cards()' compute, so memoising the
Python-side computation over ~1.5k rows buys nothing.
"""

import functools
import re

from explorer.queries.core import fetch_parallel
from explorer.queries.datasets import DATASET_TOTAL, THEME_COUNTS
from explorer.queries.links import LINKS_STATS
from explorer.queries.organisations import LAST_PUBLISHED_BY_ORG, ORGS
from explorer.queries.reports import REPORTS, report_stmts

# Active-org card — how many most-recent publication years count as "active".
ACTIVE_YEAR_COUNT = 2


def _active_card(org_rows: list, last_pub_rows: list) -> dict:
    """'Organisations have published since …' card — pure, no DB.

    org_rows: ORGS.all() rows; last_pub_rows: LAST_PUBLISHED_BY_ORG.all()
    rows. Callers fetch both once (see cards()).
    """
    last_pub = {r["org_slug"]: r["last_published"] for r in last_pub_rows}
    years = sorted(
        {d[:4] for d in last_pub.values() if re.fullmatch(r"\d{4}", d[:4])},
        reverse=True,
    )
    active_years = years[:ACTIVE_YEAR_COUNT]
    active_set = set(active_years)
    since = str(int(active_years[1]) - 1) if len(active_years) >= ACTIVE_YEAR_COUNT else None
    count = sum(1 for o in org_rows if (last_pub.get(o["slug"]) or "")[:4] in active_set)

    return {
        "key": "orgs-active",
        "label": (f"Organisations have published since {since}" if since else "Organisations have published recently"),
        "count": count,
        "link": (f"/organisations?pubyear={','.join(active_years)}" if active_years else "/organisations"),
    }


@functools.cache
def cards() -> dict:
    """Everything dashboard.html needs: totals, cards (keyed by report key /
    card key), per-kind has-items flags, and the grand total.

    Cards come in two shapes:
    - one per report, counting that report's rows via queries/reports'
      report_stmts (the count/list SQL builders);
    - hand-built cards (orgs-active, orgs-no-datasets, datasets-no-theme)
      from the totals queries.

    All ~17 fetches — the three totals, the org rows for the hand-built
    cards, and one count per report — are independent single-SELECT
    aggregates, so they run concurrently via core.fetch_parallel and the
    results come back in the same order as the task list.

    Memoised: computed once per process on first request; later requests
    serve the cached result (rebuild/restart contract in the module docstring).
    """
    report_count_fns: list = []
    for report in REPORTS:
        stmt = report_stmts(report)
        # Default-arg binding: `stmt` is rebound each iteration, so capture
        # it per-iteration or every lambda would run the last report's count.
        report_count_fns.append(lambda stmt=stmt: stmt["count"].get(*stmt["params"])["n"])

    org_rows, last_pub_rows, total_datasets_row, links_stats, theme_count_rows, *report_counts = fetch_parallel(
        [
            ORGS.all,
            LAST_PUBLISHED_BY_ORG.all,
            DATASET_TOTAL.get,
            LINKS_STATS.get,
            THEME_COUNTS.all,
            *report_count_fns,
        ],
    )

    total_datasets = total_datasets_row["n"]
    total_orgs = len(org_rows)
    total_links = links_stats["total"]
    totals = {"orgs": total_orgs, "datasets": total_datasets, "links": total_links}

    cards = {}
    for report, count in zip(REPORTS, report_counts, strict=True):
        cards[report["key"]] = {
            "label": report["label"],
            "count": count,
            # duplicate-urls has no totals bucket → percent is None
            "percent": ((count / totals[report["kind"]] * 100) if totals.get(report["kind"]) else None),
            "href": f"/report/{report['key']}",
        }

    # orgs-active card
    active = _active_card(org_rows, last_pub_rows)
    cards[active["key"]] = {
        "label": active["label"],
        "count": active["count"],
        "percent": (active["count"] / totals["orgs"] * 100) if totals["orgs"] else None,
        "href": active["link"],
    }

    # orgs-no-datasets card
    no_datasets_count = sum(1 for o in org_rows if (o["package_count"] or 0) == 0)
    cards["orgs-no-datasets"] = {
        "label": "Organisations with no datasets",
        "count": no_datasets_count,
        "percent": ((no_datasets_count / totals["orgs"] * 100) if totals["orgs"] else None),
        "href": "/organisations?datasets=0",
    }

    # datasets-no-theme card
    no_theme_count = next(
        (r["count"] for r in theme_count_rows if r["theme"] == "__none__"),
        0,
    )
    cards["datasets-no-theme"] = {
        "label": "Datasets with no theme",
        "count": no_theme_count,
        "percent": ((no_theme_count / totals["datasets"] * 100) if totals["datasets"] else None),
        "href": "/datasets?theme=none",
    }

    # Per-kind card ordering for the has-items flags (dashboard.html renders
    # its own hard-coded card order per group).
    group_keys: dict[str, list[str]] = {}
    for report in REPORTS:
        group_keys.setdefault(report["kind"], []).append(report["key"])
    group_keys.setdefault("orgs", []).extend([active["key"], "orgs-no-datasets"])
    group_keys.setdefault("datasets", []).append("datasets-no-theme")
    group_has_items = {kind: any(cards[key]["count"] > 0 for key in keys) for kind, keys in group_keys.items()}

    return {
        "totals": totals,
        "cards": cards,
        "group_has_items": group_has_items,
        "dashboard_total": sum(c["count"] for c in cards.values()),
    }
