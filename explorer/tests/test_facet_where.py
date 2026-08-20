"""Unit tests for queries.core.facet_where — the shared self-excluding
WHERE builder every facet page's query module uses.

Hand-built clause builders only, no live DB (unlike test_queries.py, which
needs the populated dev DB): these pin the exclusion semantics themselves —
normal exclusion, exclude=None (all clauses), skipped filters, and multiple
simultaneously-active filters.
"""

from explorer.queries.core import facet_where


def _theme(filters, exclude):
    if exclude == "theme":
        return [], []
    theme = filters.get("theme")
    if theme:
        return ["theme = %s"], [theme]
    return [], []


def _year(filters, exclude):
    if exclude == "year":
        return [], []
    year = filters.get("year")
    if year:
        return ["substr(created, 1, 4) = %s"], [year]
    return [], []


def _source(filters, exclude):
    if exclude == "source":
        return [], []
    if filters.get("source") == "manual":
        return ["harvested = 0"], []
    return [], []


BUILDERS = {"theme": _theme, "year": _year, "source": _source}


def test_exclude_omits_one_builder():
    """exclude names the facet whose own filter is dropped — the others
    still apply (self-exclusion is per-facet, not global)."""
    where, params = facet_where(BUILDERS, {"theme": "x", "year": "2020"}, exclude="theme")
    assert where == " WHERE substr(created, 1, 4) = %s"
    assert params == ["2020"]


def test_no_exclusion_applies_all():
    """exclude=None (the list/count WHERE) applies every active filter."""
    where, params = facet_where(BUILDERS, {"theme": "x", "year": "2020"}, exclude=None)
    assert where == " WHERE theme = %s AND substr(created, 1, 4) = %s"
    assert params == ["x", "2020"]


def test_no_active_filters_produces_empty_where():
    where, params = facet_where(BUILDERS, {}, exclude=None)
    assert where == ""
    assert params == []


def test_builder_returning_empty_is_skipped():
    """A builder whose filter isn't active returns ([], []) — no clause,
    no param (e.g. the source facet's manual/no-param clause)."""
    where, params = facet_where(BUILDERS, {"source": "manual"}, exclude="year")
    assert where == " WHERE harvested = 0"
    assert params == []


def test_exclude_matching_inactive_facet_is_noop():
    """exclude naming an inactive facet is a no-op — the active ones stay."""
    where, params = facet_where(BUILDERS, {"theme": "x"}, exclude="source")
    assert where == " WHERE theme = %s"
    assert params == ["x"]


def test_multiple_active_filters_keep_dict_order():
    where, params = facet_where(BUILDERS, {"theme": "x", "source": "manual", "year": "2020"})
    assert where == (" WHERE theme = %s AND substr(created, 1, 4) = %s AND harvested = 0")
    assert params == ["x", "2020"]
