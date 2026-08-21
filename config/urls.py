"""URL configuration for the Explorer.

Routes by page group: health + 404 handler, organisation, metadata,
series, organisations, links, datasets, home (dashboard), dashboard
reports (/report/{key}), dataset detail, reviews, suggestions.

Static: served by whitenoise.middleware.WhiteNoiseMiddleware (see
config/settings.py) — STATIC_URL is "/" so there's no URL pattern here.
Non-file paths fall through to routing below; missing files and unrouted
paths land on the catch-all 404 view at the bottom, rendering 404.html in
both DEBUG modes.
"""

from django.urls import path, re_path

from explorer import views

handler404 = "explorer.views.not_found"

urlpatterns = [
    path("health", views.health),
]

# --- organisation, metadata, series -------------------------------
urlpatterns += [
    path("organisation/<slug:slug>", views.organisation, name="organisation"),
    path("metadata", views.metadata_overview, name="metadata"),
    path(
        "metadata/<str:section>/<str:name>",
        views.metadata_detail,
        name="metadata-detail",
    ),
    path("series", views.series_list, name="series"),
    path("series/<str:series_id>", views.series_detail, name="series-detail"),
]

# --- organisations, links (facet pages) ---------------------------
urlpatterns += [
    path("organisations", views.organisations, name="organisations"),
    path("harvesters", views.harvesters, name="harvesters"),
    path("harvester/<str:source_id>", views.harvester, name="harvester"),
    path("links", views.links, name="links"),
]

# --- datasets (facet page) ----------------------------------------
urlpatterns += [
    path("datasets", views.datasets, name="datasets"),
]

# --- home (dashboard), dashboard reports, dataset detail -----------
urlpatterns += [
    path("", views.dashboard, name="home"),
    path("report/<str:key>", views.report, name="report"),
    path(
        "dataset/<str:org_slug>/<str:dataset_id>",
        views.dataset,
        name="dataset-detail",
    ),
]

# --- reviews + suggestions ----------------------------------------
urlpatterns += [
    path("reviews", views.reviews, name="reviews"),
    path("suggestions", views.suggestions, name="suggestions"),
]

# Catch-all 404 — last pattern, so it only sees paths no route matched
# (missing static files fall through WhiteNoise to here too). Renders
# 404.html in both DEBUG modes; NotFoundMiddleware still converts Http404s
# raised inside views (unknown orgs, series ids, etc.).
urlpatterns += [
    re_path(r"^.*$", views.not_found),
]
