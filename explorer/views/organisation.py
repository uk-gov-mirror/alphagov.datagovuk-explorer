"""GET /organisation/{slug} — datasets for one organisation.

Sortable via ?sort= (DATASET_SORT_COLUMNS whitelist) and ?dir=asc|desc.
Default: metadata_modified desc (most recently updated first). A prior
app's comment stated this intent but its code
(`dir === 'desc' ? 'desc' : 'asc'`) actually defaulted to asc (oldest
first); we follow the stated intent.
"""

from django.http import Http404
from django.shortcuts import render

from explorer.helpers import yearly_counts
from explorer.queries.datasets import DATASET_COUNT, DATASETS_BY_ORG, YEARLY_BY_ORG
from explorer.queries.organisations import ORG
from explorer.sort import DATASET_SORT_COLUMNS, sort_datasets

from .core import _sort_dir


def organisation(request, slug):
    """GET /organisation/{slug} — one org's datasets, sorted by ?sort/?dir."""
    org_row = ORG.get(slug)
    if org_row is None:
        raise Http404

    datasets = DATASETS_BY_ORG.all(slug)
    dataset_count_row = DATASET_COUNT.get(slug)

    harvested_count = sum(1 for d in datasets if d["harvested"])

    org = {
        "slug": org_row["slug"],
        "display_name": org_row["display_name"] or org_row["slug"],
        "dataset_count": dataset_count_row["count"],
        "harvested_count": harvested_count,
        "datasets": datasets,
    }

    sort, dir_ = _sort_dir(request, DATASET_SORT_COLUMNS, "metadata_modified", "desc")
    sort_datasets(org["datasets"], sort, dir_)

    # Datasets created per year for this org's chart
    yearly = yearly_counts(YEARLY_BY_ORG.all(slug))
    max_yearly = max((x["count"] for x in yearly), default=0)

    return render(
        request,
        "organisation.html",
        {
            "title": f"{org['display_name']} — Datasets",
            "section": "orgs",
            "org": org,
            "sort": sort,
            "dir": dir_,
            "yearly": yearly,
            "max_yearly": max_yearly,
        },
    )
