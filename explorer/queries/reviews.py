"""Reviews / suggestions helpers — DB-backed, read from the `reviews`
table (populated by scripts/ingest_reviews.py from the JSONL)."""

import json

from .core import Query

# ---------------------------------------------------------------------------
# Reviews / suggestions — DB-backed, read from the `reviews` table
# ---------------------------------------------------------------------------
#
# The reviews table stores one row per JSONL record with the full record in
# `json`. Only ok:true records count, and only the latest one per
# dataset_id — latest = later row in the file = higher `id` (ingest inserts
# in file order). `json` is TEXT, so it comes back as a plain string and
# the views keep their json.loads + dict access.

# All ok reviews, one (latest) per dataset — DISTINCT ON keeps the
# highest-id (latest) record per dataset_id.
_LATEST_REVIEWS = Query(
    """SELECT json FROM (
      SELECT DISTINCT ON (dataset_id) dataset_id, id, json
      FROM reviews WHERE ok = true
      ORDER BY dataset_id, id DESC
    ) latest ORDER BY id""",
)

# Latest ok review for one dataset id.
_REVIEW_FOR = Query(
    "SELECT json FROM reviews WHERE ok = true AND dataset_id = %s ORDER BY id DESC LIMIT 1",
)


def latest_reviews() -> list[dict]:
    """All ok reviews, one (latest) per dataset."""
    return [json.loads(row["json"]) for row in _LATEST_REVIEWS.all()]


def _review_for(dataset_id: str) -> dict | None:
    """Latest ok review for one dataset id, or None."""
    rows = _REVIEW_FOR.all(dataset_id)
    if not rows:
        return None
    return json.loads(rows[0]["json"])


# Alias — get_classification is the same query as get_review.
get_review = _review_for
get_classification = _review_for
