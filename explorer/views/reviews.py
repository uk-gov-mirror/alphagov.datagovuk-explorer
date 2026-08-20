"""GET /reviews — list of LLM-reviewed datasets, read from the reviews
    table (populated by scripts/ingest_reviews.py from the JSONL). Sorted
    worst-first by default so quality problems surface first; every column
    is sortable via ?sort=&dir=, and the sidebar facets filter by each
    score group (?overall=, ?findability=, ?metadata=, ?resources=).

Reads from explorer/queries.latest_reviews (latest per dataset_id wins,
ok:true only; the reviews table is populated by scripts/ingest_reviews.py).

These pages sort in memory, not in SQL — there is no pagination query to
pin ties with a secondary key. Text columns sort with str.lower as a
stand-in for ICU collation (a known, accepted divergence: e.g. "£"
collates differently).
"""

import math

from django.shortcuts import render

from explorer import facets
from explorer.queries.reviews import latest_reviews

from .core import _page_param, _sort_dir

PAGE_SIZE = 50


def _subscore(r: dict, key: str) -> int | None:
    """scores.<key>.score, or None when the subscore is missing."""
    scores = r.get("scores")
    if not isinstance(scores, dict):
        return None
    sub = scores.get(key)
    if not isinstance(sub, dict):
        return None
    return sub.get("score")


# Score dimensions — one facet group per dimension, mirroring the sortable
# columns. overall lives at the top level of a review; the others sit under
# scores.<key>.score. A missing score is represented by the "none" facet.
SCORE_GROUPS = [
    {"key": "overall", "label": "Overall", "get": lambda r: r.get("overall")},
    {
        "key": "findability",
        "label": "Findability",
        "get": lambda r: _subscore(r, "findability"),
    },
    {"key": "metadata", "label": "Metadata", "get": lambda r: _subscore(r, "metadata")},
    {
        "key": "resources",
        "label": "Resources",
        "get": lambda r: _subscore(r, "resources"),
    },
]

# Valid facet values — scores run 0-5; only values actually present in the
# data are rendered as items.
SCORE_VALUES = ["0", "1", "2", "3", "4", "5"]


def _score_or_minus_one(v: int | None) -> int:
    """Sort accessor for a numeric score: missing scores sort below present
    ones."""
    return v if v is not None else -1


# Accessor for each sortable column. Numeric scores sort ascending by
# default (worst first); missing scores sort below present ones.
SORT_COLUMNS = {
    "title": lambda r: r.get("title") or "",
    "org": lambda r: r.get("org_display_name") or "",
    "overall": lambda r: _score_or_minus_one(r.get("overall")),
    "resources": lambda r: _score_or_minus_one(_subscore(r, "resources")),
    "metadata": lambda r: _score_or_minus_one(_subscore(r, "metadata")),
    "findability": lambda r: _score_or_minus_one(_subscore(r, "findability")),
}


def _matches_group(r: dict, filters: dict[str, str], ignore: str | None = None) -> bool:
    """Whether a review matches a group's facet, ignoring that group's own
    selection — used so each group's per-score counts stay meaningful once
    other groups are filtered."""
    for g in SCORE_GROUPS:
        if g["key"] == ignore:
            continue
        want = filters.get(g["key"])
        if not want:
            continue
        have = g["get"](r)
        if want == "none":
            if have is not None:
                return False
        elif str(have) != want:
            return False
    return True


def _sort_reviews(reviews: list[dict], sort: str, dir_: str) -> None:
    """Sort in place, worst-first by default. Ties always break ascending on
    title regardless of dir. Python's stable sort reproduces that exactly:
    sort by title first (ascending), then by the primary key — tied
    primaries keep the title-ascending order. Text columns sort
    case-insensitively via str.lower (a known, accepted divergence from ICU
    collation: e.g. "£" collates differently)."""
    get = SORT_COLUMNS[sort]
    reviews.sort(key=lambda r: (r.get("title") or "").lower())
    if sort in ("title", "org"):
        reviews.sort(key=lambda r: str(get(r)).lower(), reverse=dir_ == "desc")
    else:
        reviews.sort(key=get, reverse=dir_ == "desc")


def _score_facet_group(g: dict, all_reviews: list[dict], filters: dict[str, str]) -> dict | None:
    """One score facet group. Counts are computed over the reviews left by
    the *other* groups' filters, so the per-score rows always sum to the
    selectable pool. Items cover the score values present in the data, plus
    "No score" when any review is missing that score. None when no scores
    are present."""
    pool = [r for r in all_reviews if _matches_group(r, filters, ignore=g["key"])]
    counts: dict[str, int] = {}
    no_score = 0
    for r in pool:
        have = g["get"](r)
        if have is None:
            no_score += 1
        else:
            counts[str(have)] = counts.get(str(have), 0) + 1
    max_count = max(counts.values()) if counts else 0
    max_count = max(max_count, no_score, 1)
    items = [
        {
            "value": v,
            "name": f"{v}/5",
            "count": counts[v],
            "active": filters.get(g["key"]) == v,
            "proportion": counts[v] / max_count,
        }
        for v in SCORE_VALUES
        if v in counts
    ]
    if no_score:
        items.append(
            {
                "value": "none",
                "name": "No score",
                "count": no_score,
                "active": filters.get(g["key"]) == "none",
                "proportion": no_score / max_count,
            },
        )
    if not items:
        return None
    return facets.facet_group(g["key"], g["label"], f"Filter by {g['label'].lower()} score", items)


def reviews(request):
    """GET /reviews — the LLM review table with per-score facets."""
    all_reviews = latest_reviews()

    # Current facet selections — single-select per group, combinable across
    # groups. A value must be a valid score or "none" to be accepted.
    filters: dict[str, str] = {}
    for g in SCORE_GROUPS:
        v = request.GET.get(g["key"])
        if v == "none" or v in SCORE_VALUES:
            filters[g["key"]] = v
    has_filters = any(g["key"] in filters for g in SCORE_GROUPS)

    # Reviews matching a group's facet, ignoring that group's own selection —
    # used so each group's per-score counts stay meaningful once other
    # groups are filtered (see _matches_group).
    filtered = [r for r in all_reviews if _matches_group(r, filters)] if has_filters else all_reviews

    sort, dir_ = _sort_dir(request, SORT_COLUMNS, "overall")
    _sort_reviews(filtered, sort, dir_)

    total_pages = max(1, math.ceil(len(filtered) / PAGE_SIZE))
    page = min(_page_param(request), total_pages)
    start = (page - 1) * PAGE_SIZE

    # Shared query-string machinery from explorer/facets.py: the base keeps
    # sort/dir then the active facets in SCORE_GROUPS order; facet_qs drops
    # sort/dir for the sort/pagination links; facet_url sets or clears one
    # facet value (empty value clears it, back to the pills).
    base_params = facets.preserve_params(sort, dir_, list(filters.items()))
    facet_url = facets.facet_url_for(base_params)
    facet_qs = facets.facet_qs(base_params, include_sort=False)

    # Facet groups for the sidebar — one per score dimension.
    facet_groups = [
        group for group in (_score_facet_group(g, all_reviews, filters) for g in SCORE_GROUPS) if group is not None
    ]

    return render(
        request,
        "reviews.html",
        {
            "title": f"Dataset reviews ({len(filtered)})",
            "section": "reviews",
            "reviews": filtered[start : start + PAGE_SIZE],
            "total": len(all_reviews),
            "shown": len(filtered),
            "page": page,
            "total_pages": total_pages,
            "start_index": start + 1,
            "end_index": min(start + PAGE_SIZE, len(filtered)),
            "sort": sort,
            "dir": dir_,
            "facet_groups": facet_groups,
            "filters": filters,
            "facet_qs": facet_qs,
            "facet_url": facet_url,
        },
    )
