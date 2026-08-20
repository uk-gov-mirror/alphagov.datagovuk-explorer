"""Jinja2 backend for Django — the app's template filters.

The templates — macros, blocks, the five custom filters — are written for
Jinja2, so the app uses Jinja2 as Django's template backend instead of
Django's own template language. The filter functions below are the app's
own, registered on the environment in __init__.
"""

import json
import math

from django.template.backends.jinja2 import Jinja2 as DjangoJinja2
from django.templatetags.static import static

from .helpers import format_date


def _dump(value, indent: int = 2) -> str:
    """Pretty JSON — insertion-order keys, raw unicode.

    Jinja2's builtin `tojson` sorts keys and escapes non-ASCII
    (ensure_ascii=True); the dataset page's relationships blocks want
    insertion-order, raw-unicode output. `indent` mirrors the
    template's `| dump(2)` call.
    """
    return json.dumps(value, indent=indent, ensure_ascii=False)


def _num(value) -> str:
    """Format numbers with thousands separators, e.g. 12345 → "12,345"."""
    if value is None or value == "":
        return "—"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    if n.is_integer():
        return f"{int(n):,}"
    # preserve fractional precision, add thousands grouping (e.g. 12,345.25)
    return f"{n:,.12g}"


def _percent(value) -> str:
    """Percentage with one decimal, e.g. 21.3%.

    Tiny but non-zero values show as "< 0.1%" rather than a misleading "0%".
    Rounds half-up (Python's round() is banker's rounding), no trailing .0.
    """
    if value is None or value == "":
        return "—"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    # floor(x + 0.5) gives half-up for x >= 0
    rounded = math.floor(n * 10 + 0.5) / 10
    if rounded == 0 and n > 0:
        return "< 0.1%"
    if rounded.is_integer():
        return f"{int(rounded)}%"
    return f"{rounded}%"


def _round1(value) -> str:
    """Round to 1 decimal place, as a bare number string.

    The metadata pages use `| round1` rather than the `percent` filter, so
    tiny values round to "0%" exactly (no "< 0.1%" fallback) and whole
    results drop the trailing ".0" (50 renders as "50", not "50.0").
    Callers append the "%" themselves, matching the template text.
    """
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "NaN"  # unreachable with real data
    rounded = math.floor(n * 10 + 0.5) / 10
    if rounded.is_integer():
        return str(int(rounded))
    return str(rounded)


def _prop(value) -> int | float | str:
    """Render a 0-1 proportion: whole values drop the trailing .0, and
    fractions use Python's shortest-round-trip form (e.g. "0.5", or
    "8.26e-05" for tiny values — valid in the CSS contexts it feeds)."""
    if value is None:
        return 0
    try:
        n = float(value)
    except (TypeError, ValueError):
        return value
    if n.is_integer():
        return int(n)
    return str(n)


class Jinja2(DjangoJinja2):
    """Django template backend backed by Jinja2 with the app's filters."""

    def __init__(self, params):
        super().__init__(params)
        # The app's five custom filters. `urlencode` is Jinja2's
        # builtin (standard urllib quote), so it needs no re-registration.
        self.env.filters["num"] = _num
        self.env.filters["percent"] = _percent
        self.env.filters["round1"] = _round1
        self.env.filters["prop"] = _prop
        self.env.filters["date_short"] = format_date
        self.env.filters["dump"] = _dump
        # `static` — same code path as DTL's {% static %} tag
        # (django.templatetags.static → staticfiles_storage.url), so asset
        # references stay idiomatic and follow STATIC_URL. Django 6.1's
        # Jinja2 backend injects no globals, so this registration is required.
        self.env.globals["static"] = static
