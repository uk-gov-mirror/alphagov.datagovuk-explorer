"""Health check, 404 handler and shared view helpers."""

from django.http import HttpResponse
from django.shortcuts import render


def health(request):
    """Health check — Railway health checks must return 2xx."""
    return HttpResponse("ok")


def not_found(request, exception=None):
    """Custom 404 template.

    Doubles as the catch-all URL view: unrouted paths and missing static
    files hit it directly, so 404.html renders in both DEBUG modes.
    """
    return render(request, "404.html", {"title": "Page not found"}, status=404)


def _page_param(request, default: int = 1) -> int:
    """?page= as an int, clamped to >= 1. Non-ints and 0 clamp to 1;
    out-of-range pages are clamped to total_pages by the callers."""
    try:
        page = int(request.GET.get("page", str(default)))
    except (TypeError, ValueError):
        return default
    return max(page, 1)


def _sort_dir(request, valid_columns, default_sort: str, default_dir: str = "asc") -> tuple[str, str]:
    """?sort=/?dir= parsed and validated against the view's column set.

    Unknown sort keys fall back to default_sort; a dir value other than
    "desc" becomes "asc". (series.py used to fall back to "desc" for
    garbage input — this unifies every page on the same "asc" fallback.)
    """
    sort = request.GET.get("sort", default_sort)
    sort = sort if sort in valid_columns else default_sort
    dir_ = request.GET.get("dir", default_dir)
    dir_ = "desc" if dir_ == "desc" else "asc"
    return sort, dir_
