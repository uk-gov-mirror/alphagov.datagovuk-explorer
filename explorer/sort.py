"""Client-visible sort columns and the in-place sorters that back them.

Every page whitelists the keys it accepts in ?sort= and has a matching
sorter; unknown keys fall back to the page's default.

Text columns are sorted case-insensitively and numeric-aware (locale-aware
collation: "base" sensitivity, numeric ordering).
"""

import re
from typing import Any

# Sortable columns for the org table on the home page
SORT_COLUMNS = [
    "name",
    "dataset_count",
    "resource_count",
    "views",
    "type",
    "state",
    "approval_status",
    "created",
    "last_published",
]

# Sortable columns for the dataset table on the organisation page
DATASET_SORT_COLUMNS = [
    "title",
    "metadata_created",
    "metadata_modified",
    "resources",
    "views",
    "harvested",
]

# Sortable columns for the all-datasets table on the /datasets page — sorting
# happens in PostgreSQL (ORDER BY in the /datasets query builder,
# explorer/queries/datasets.py); this list is just the accepted-keys whitelist.
DATASETS_SORT_COLUMNS = [
    "title",
    "organisation",
    "metadata_created",
    "metadata_modified",
    "resources",
    "views",
    "harvested",
]

# Sortable columns for the resource table on the dataset page
RESOURCE_SORT_COLUMNS = [
    "position",
    "name",
    "format",
    "mimetype",
    "size",
    "created",
    "last_modified",
]


def _natural_key(value: Any) -> tuple[tuple[int, Any], ...]:
    """Case-insensitive, numeric-aware sort key (ICU-style collation):
    - "Dataset 2" sorts before "Dataset 10" (digit runs compare numerically)
    - "3C Shared Services" sorts before "Aberdeen City Council" (digits
      sort before letters, as in ICU collation)
    - case differences are ignored
    """
    parts = re.split(r"(\d+)", str(value).lower())
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in parts if p != "")


def _num_key(row: dict[str, Any], key: str) -> float:
    """Numeric column value, missing → 0 (a missing value sorts as 0)."""
    return row.get(key) or 0


def _text_key(row: dict[str, Any], key: str, fallback: str | None = None) -> tuple:
    value = row.get(key)
    if value is None and fallback is not None:
        value = row.get(fallback)
    return _natural_key(value or "")


def sort_orgs(rows: list[dict[str, Any]], sort: str, dir_: str) -> None:
    """Sort org rows in place by column key and direction (asc|desc)."""
    reverse = dir_ == "desc"
    if sort in ("dataset_count", "resource_count", "views"):
        rows.sort(key=lambda r: _num_key(r, sort), reverse=reverse)
    else:
        rows.sort(key=lambda r: _text_key(r, sort), reverse=reverse)


def sort_datasets(rows: list[dict[str, Any]], sort: str, dir_: str) -> None:
    """Sort dataset rows in place by column key and direction (asc|desc)."""
    reverse = dir_ == "desc"
    if sort in ("resources", "views", "harvested"):
        # 'resources' sorts on resource_count; missing → 0
        column = "resource_count" if sort == "resources" else sort
        rows.sort(key=lambda r: _num_key(r, column), reverse=reverse)
    elif sort == "title":
        rows.sort(key=lambda r: _text_key(r, "title", fallback="name"), reverse=reverse)
    else:
        rows.sort(key=lambda r: _text_key(r, sort), reverse=reverse)


def sort_resources(rows: list[dict[str, Any]], sort: str, dir_: str) -> None:
    """Sort resource rows in place by column key and direction (asc|desc)."""
    reverse = dir_ == "desc"
    if sort == "position":
        # missing position sorts last (after any number)
        rows.sort(
            key=lambda r: r["position"] if isinstance(r.get("position"), int) else float("inf"),
            reverse=reverse,
        )
    elif sort == "size":
        # non-numeric size sorts last (after any number)
        rows.sort(
            key=lambda r: r["size"] if isinstance(r.get("size"), (int, float)) else float("inf"),
            reverse=reverse,
        )
    elif sort == "last_modified":
        # last_modified is often null — fall back to metadata_modified for sorting
        rows.sort(
            key=lambda r: _text_key(r, "last_modified", fallback="metadata_modified"),
            reverse=reverse,
        )
    else:
        rows.sort(key=lambda r: _text_key(r, sort), reverse=reverse)
