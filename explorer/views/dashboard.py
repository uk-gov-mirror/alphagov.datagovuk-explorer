"""GET / — the home dashboard (summary cards grouped by kind).

Card data is assembled in queries/dashboard.py (totals + one count per
report from queries/reports); this view only renders it.
"""

from django.shortcuts import render

from explorer.queries.dashboard import cards


def dashboard(request):
    """GET / — the dashboard with summary cards grouped by kind."""
    data = cards()
    return render(
        request,
        "dashboard.html",
        {
            "title": "Dashboard — data.gov.uk Explorer",
            "section": "dashboard",
            "cards": data["cards"],
            "group_has_items": data["group_has_items"],
            "dashboard_total": data["dashboard_total"],
            "total_orgs": data["totals"]["orgs"],
            "total_datasets": data["totals"]["datasets"],
            "total_links": data["totals"]["links"],
        },
    )
