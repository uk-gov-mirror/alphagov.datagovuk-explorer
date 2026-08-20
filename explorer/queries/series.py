"""/series list builder — list statements built per (sort, dir) call, plus
the fixed series statements and the series_built() helper."""

from .core import Query

# --- /series list builder ---
#
# The ORDER BY is dynamic (whitelisted columns only) but the column set is
# fixed.

# Sortable columns whitelist
SERIES_SORT_COLUMNS = ["root_title", "type", "dataset_count", "org_count"]


def series_list_stmt(sort: str, dir_: str) -> Query:
    order_dir = "ASC" if dir_ == "asc" else "DESC"
    order_expr = f"{sort} {order_dir}" if sort in ("dataset_count", "org_count") else f"LOWER({sort}) {order_dir}"
    # `, id` tiebreak — the series table has large tie groups, so an
    # unpinned ORDER BY would shuffle the row set between requests. Same
    # treatment as the datasets/links list queries.
    return Query(
        f"SELECT id, root_title, type, dataset_count, org_count "
        f"FROM series ORDER BY {order_expr}, id LIMIT %s OFFSET %s",
    )


# --- Fixed series statements (the /series list + detail pages) ---

# Series that a dataset belongs to
DATASET_SERIES = Query(
    """SELECT s.id, s.root_title, s.type, s.dataset_count
       FROM series_datasets sd
       JOIN series s ON s.id = sd.series_id
       WHERE sd.dataset_id = %s""",
)

# Other datasets in the same series as the current one
SERIES_DATASETS_EXCEPT = Query(
    """SELECT d.id, d.title, d.org_slug, d.org_display_name, d.theme_primary,
              d.resource_count, d.metadata_created, sd.date_suffix
       FROM series_datasets sd
       JOIN datasets d ON d.id = sd.dataset_id
       WHERE sd.series_id = %s AND sd.dataset_id != %s
       ORDER BY d.metadata_created DESC, LOWER(sd.dataset_title)
       LIMIT 10""",
)

SERIES_COUNT = Query("SELECT COUNT(*) AS n FROM series")

SERIES_BY_ID = Query(
    "SELECT id, root_title, type, dataset_count, org_count FROM series WHERE id = %s",
)

SERIES_DATASETS = Query(
    """SELECT sd.dataset_id, sd.dataset_title, sd.date_suffix, sd.org_slug, sd.org_display_name,
              d.theme_primary, d.resource_count, d.metadata_created
       FROM series_datasets sd
       JOIN datasets d ON d.id = sd.dataset_id
       WHERE sd.series_id = %s
       ORDER BY d.metadata_created DESC, LOWER(sd.dataset_title)""",
)


# True when the series table holds any rows (see series_built)
_SERIES_EXISTS = Query("SELECT EXISTS (SELECT 1 FROM series) AS exists")


def series_built() -> bool:
    """True when the series tables hold data (the /series views' "not built"
    guard — the table always exists since migrations own the schema, so
    "not built" means empty)."""
    row = _SERIES_EXISTS.get()
    return bool(row and row["exists"])
