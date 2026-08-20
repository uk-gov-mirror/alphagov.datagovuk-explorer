"""GET /suggestions — list of LLM-classified datasets with theme/tag/title
suggestions, read from the reviews table (populated by
scripts/ingest_reviews.py). Sorted by confidence so low-confidence
(ambiguous) datasets surface first. Only the latest classification per
dataset is shown.

The batch enrichment (theme + tags lookup per dataset) uses the sync
query layer's Query class — the dynamic IN placeholders are native `%s`,
like the rest of the SQL.
"""

import math

from django.shortcuts import render

from explorer.queries.core import Query
from explorer.queries.reviews import latest_reviews

from .core import _page_param, _sort_dir

PAGE_SIZE = 50

# Confidence order for the numeric sort — high/medium/low map to 3/2/1.
_CONFIDENCE_ORDER = {"high": 3, "medium": 2, "low": 1}

# Accessor for each sortable column.
SORT_COLUMNS = {
    "title": lambda r: r.get("title") or "",
    "org": lambda r: r.get("org_display_name") or "",
    "theme": lambda r: r.get("current_theme") or "",
    "confidence": lambda r: _CONFIDENCE_ORDER.get(r.get("theme_confidence"), 0),
}


def suggestions(request):
    """GET /suggestions — the LLM classification table with suggested themes."""
    # latest_reviews() already dedups to one (latest) record per dataset.
    unique = latest_reviews()
    ids = [r["dataset_id"] for r in unique]

    # Batch-load current themes and tags — one query each instead of N.
    theme_map: dict = {}
    tags_map: dict = {}
    if ids:
        placeholders = ",".join("%s" for _ in ids)
        theme_rows = Query(
            f"SELECT id, theme_primary FROM datasets WHERE id IN ({placeholders})",
        ).all(*ids)
        theme_map = {row["id"]: row["theme_primary"] for row in theme_rows}
        tag_rows = Query(
            f"SELECT id, tags FROM datasets WHERE id IN ({placeholders})",
        ).all(*ids)
        tags_map = {row["id"]: row["tags"] for row in tag_rows}

    # Enrich (a falsy DB value reads as missing).
    enriched = []
    for r in unique:
        current_theme = theme_map.get(r["dataset_id"]) or None
        enriched.append(
            {
                **r,
                "current_theme": current_theme,
                "current_tags": tags_map.get(r["dataset_id"]) or "",
                "theme_changed": r.get("theme") != current_theme,
            },
        )

    sort, dir_param = _sort_dir(request, SORT_COLUMNS, "confidence")

    # Same two-pass sort as explorer/views/reviews.py: stable sort by title
    # first, then by the primary key — tied primaries keep the
    # title-ascending order.
    get = SORT_COLUMNS[sort]
    enriched.sort(key=lambda r: (r.get("title") or "").lower())
    if sort in ("title", "org", "theme"):
        enriched.sort(key=lambda r: str(get(r)).lower(), reverse=dir_param == "desc")
    else:
        enriched.sort(key=get, reverse=dir_param == "desc")

    total = len(enriched)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(_page_param(request), total_pages)
    start = (page - 1) * PAGE_SIZE

    return render(
        request,
        "suggestions.html",
        {
            "title": f"Suggestions ({total})",
            "section": "suggestions",
            "suggestions": enriched[start : start + PAGE_SIZE],
            "total": total,
            "shown": total,
            "page": page,
            "total_pages": total_pages,
            "start_index": start + 1,
            "end_index": min(start + PAGE_SIZE, total),
            "sort": sort,
            "dir": dir_param,
        },
    )
