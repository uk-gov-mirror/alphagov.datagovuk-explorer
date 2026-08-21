"""Harvest source statements — the /harvesters page.

One fetch: all harvest sources with their owning organisation's display
name and their dataset count.

The organisation join is on harvest_sources.org_slug → organisations.slug
(the primary key) — org_slug is denormalised at build time from the
CKAN UUID (scripts/build_db.py), so no json extraction is needed there.
The dataset count joins on datasets.harvest_source_id = h.id — the
datasets→sources key promoted from the dataset's harvest_source_id extra
at build time. (Title joins overcount: source titles aren't unique, e.g.
"UKME WAF" is used by ~50 different orgs.) A LEFT JOIN so sources with
zero datasets still appear. The only json extraction is last_run (the
status block's last_harvest_request), which the /harvesters page sorts on.

HARVESTED_TOTAL is the headline "datasets harvested" figure: the count of
datasets with harvested = 1, the same definition the /datasets SOURCE
facet uses. Per-source counts can't sum to it — some harvested datasets
carry no harvest source id at all (their source record is gone from the
CKAN registry), so they're unlinkable.

The DB is a build-time snapshot, so the fetches are memoised via
functools.cache — computed once per process, then served from memory.
Restart the process to refresh after a DB rebuild (same contract as the
dashboard's cards() cache).
"""

import functools
from typing import Any

from .core import Query

# All harvest sources, with org display name and dataset count. h.id is
# the primary key, so the other h.* columns are functionally dependent
# on it and don't need GROUP BY entries. last_run is the source's last
# harvest request timestamp, read out of the status block in the json
# record (null when never run / missing).
HARVEST_SOURCES = Query(
    """SELECT h.id, h.title, h.url, h.type, h.active, h.frequency,
              h.created,
              NULLIF(h.json::jsonb -> 'status' ->> 'last_harvest_request', 'None') AS last_run,
              COALESCE(o.display_name, o.title, o.name) AS org_name,
              COUNT(d.id) AS dataset_count
       FROM harvest_sources h
       LEFT JOIN organisations o ON o.slug = h.org_slug
       LEFT JOIN datasets d ON d.harvest_source_id = h.id
       GROUP BY h.id, o.display_name, o.title, o.name
       ORDER BY LOWER(h.title), h.id""",
)


# Headline "datasets harvested" count — datasets with the harvested flag
# set (the same definition as the /datasets SOURCE facet). Distinct from
# the sum of the per-source counts, which excludes harvested datasets
# whose source record is no longer in the CKAN registry.
HARVESTED_TOTAL = Query("SELECT COUNT(*) AS n FROM datasets WHERE harvested = 1")


# One harvest source by CKAN id — the /harvester/{id} detail page. The
# full json record rides along so the page can show the fields that
# aren't promoted to columns (description, publisher, harvest status).
HARVEST_SOURCE = Query(
    """SELECT id, title, url, type, active, frequency, org_slug,
              organization_id, created, json
       FROM harvest_sources WHERE id = %s""",
)


@functools.cache
def harvest_source_rows() -> list[dict[str, Any]]:
    """All harvest source rows (HARVEST_SOURCES.all) — memoised:
    build-time snapshot."""
    return HARVEST_SOURCES.all()


@functools.cache
def harvested_total() -> int:
    """Datasets with harvested = 1 (HARVESTED_TOTAL) — memoised:
    build-time snapshot. The /harvesters headline; matches the /datasets
    SOURCE facet's Harvested count."""
    return HARVESTED_TOTAL.get()["n"]
