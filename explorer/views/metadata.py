"""GET /metadata — field-adoption overview across the catalogue (top-level
fields and extras keys, sorted by how many datasets use them).
GET /metadata/{section}/{name} — value distribution for one field, paginated.
"""

import math

from django.http import Http404
from django.shortcuts import render

from explorer.queries.datasets import DATASET_TOTAL
from explorer.queries.metadata import METADATA_KEYS, METADATA_VALUE_COUNT, METADATA_VALUES

from .core import _page_param

# 100 values per page on the drill-down view
METADATA_PAGE_SIZE = 100


def metadata_overview(request):
    """GET /metadata — list of field keys with dataset counts."""
    keys = METADATA_KEYS.all()
    total_datasets = DATASET_TOTAL.get()["n"]

    top_fields: list[dict] = []
    extras_fields: list[dict] = []
    for k in keys:
        entry = {
            "key": k["key"],
            "count": k["non_empty"],
            "distinct": k["distinct_values"],
            "pct": (k["non_empty"] / total_datasets) * 100,
        }
        if k["section"] == "extras":
            entry["label"] = k["key"][7:]  # strip "extras:"
            extras_fields.append(entry)
        else:
            entry["label"] = k["key"][4:]  # strip "top:"
            top_fields.append(entry)

    return render(
        request,
        "metadata.html",
        {
            "title": "Metadata — data.gov.uk Explorer",
            "section": "metadata",
            "top_fields": top_fields,
            "extras_fields": extras_fields,
            "total_datasets": total_datasets,
        },
    )


def metadata_detail(request, section, name):
    """GET /metadata/{section}/{name} — value distribution for one field."""
    # Only top and extras sections exist
    if section not in {"top", "extras"}:
        raise Http404

    full_key = f"{section}:{name}"

    total_values = METADATA_VALUE_COUNT.get(full_key)
    if not total_values or total_values["n"] == 0:
        raise Http404

    # Total datasets that have this field — look up from metadata_keys
    keys = METADATA_KEYS.all()
    key_row = next((k for k in keys if k["key"] == full_key), None)
    dataset_count = key_row["count"] if key_row else 0
    non_empty_count = key_row["non_empty"] if key_row else 0

    total = total_values["n"]
    total_pages = max(1, math.ceil(total / METADATA_PAGE_SIZE))
    current_page = min(_page_param(request), total_pages)
    offset = (current_page - 1) * METADATA_PAGE_SIZE

    rows = METADATA_VALUES.all(full_key, METADATA_PAGE_SIZE, offset)

    display_label = name

    return render(
        request,
        "metadata_values.html",
        {
            "title": f"{display_label} — Metadata — data.gov.uk Explorer",
            "section": "metadata",
            "field_key": full_key,
            "field_label": display_label,
            "dataset_count": dataset_count,
            "non_empty_count": non_empty_count,
            "rows": rows,
            "page": current_page,
            "total_pages": total_pages,
            "start_index": offset + 1,
            "end_index": offset + len(rows),
        },
    )
