"""Shared sidebar-facet machinery for the facet pages
(/organisations, /links, /datasets and the /report/{key} reports).

Two layers live here:

1. Query-string bookkeeping — the ordered ?query base every link on a
   page keeps, and the URL builders derived from it:

   - preserve_params(): the base — sort and dir first, then each active
     facet (key, value) pair in a fixed order, then extras (the
     expanded-lists toggles, ?formats=all / ?years=all)
   - facet_url_for(): the facet_url(key, value) closure built from that
     base — keep the base, set one facet value or clear it
   - facet_qs(): the "&…" fragment appended to ?sort=..&dir=.. by the
     sort_link / pagination macros (optionally without the sort/dir keys
     the macros supply themselves)
   - facet_toggle_url(): the show-more toggle href for a facet list
     (?param=all when expanding, minus it when collapsing)

2. Counts → facet-group assembly — the shared builders every page's view
   calls:

   - facet_counts_group(): single-select — an ordered master list of
     possible values, a {value: count} pool, the selected value, and the
     page's base params; returns the facet_group() dict with items
     (active/count, optional proportion), optional show-more toggle
     wiring (cutoff/toggle_base/toggle_param/expanded/list_id) and
     optional trailing buckets
   - facet_counts_multiselect_group(): multi-select variant (the
     organisations pubyear facet) — each item carries a toggle href that
     adds/removes one value from the selection; optional trailing
     buckets (the "Never published" bucket) render after the items

   The builders are agnostic to how `counts` was computed — every facet
   page computes its pools through the shared core.facet_where helper
   (queries/core.py), each group excluding its own facet, and these
   builders only consume the resulting {value: count} dict. Self-exclusion
   stays the caller's responsibility; the rendering layer never needs to
   know how a count was computed.

- facet_group(): the group dict shape the templates iterate (key, label,
  aria_label, items, plus optional list_id / expanded / more / trailing
  keys for the toggle-able lists)
"""

from urllib.parse import urlencode


def preserve_params(sort, dir_, facets, extras=None):
    """Ordered query-string base: ?sort= & ?dir= first, then each active
    (key, value) facet pair in order, then extras (e.g. {'formats': 'all'})."""
    params = {"sort": sort, "dir": dir_, **{key: value for key, value in facets if value}}
    if extras:
        params.update(extras)
    return params


def facet_url_for(base_params):
    """facet_url(key, value) — keep the base (sort + dir + active facets),
    set one facet value, or clear it (value == '')."""

    def facet_url(key, value):
        params = dict(base_params)
        if value:
            params[key] = value
        else:
            params.pop(key, None)
        return f"?{urlencode(params)}" if params else "?"

    return facet_url


def facet_qs(base_params, *, include_sort=True):
    """The "&…" fragment appended to ?sort=..&dir=.. by the sort_link and
    pagination macros. include_sort=False drops the sort/dir keys (the
    macros supply those themselves). Empty base → "" (no leading &)."""
    params = dict(base_params)
    if not include_sort:
        params.pop("sort", None)
        params.pop("dir", None)
    qs = urlencode(params)
    return f"&{qs}" if qs else ""


def facet_toggle_url(base_params, param, *, expanded):
    """?url for a facet list's show-more toggle — base params plus
    param=all when expanding, minus it when collapsing."""
    params = dict(base_params)
    if expanded:
        params[param] = "all"
    else:
        params.pop(param, None)
    return f"?{urlencode(params)}" if params else "?"


def facet_group(key, label, aria_label, items, **extra):
    """Facet group dict — the shape the report templates iterate. Extra
    kwargs land on the group (list_id / expanded / more / trailing, used
    by the toggle-able lists on /links and /datasets)."""
    return {
        "key": key,
        "label": label,
        "aria_label": aria_label,
        "items": items,
        **extra,
    }


def _master_pairs(master):
    """(value, name) pairs from a master list — either (value, name)
    tuples or dicts with value/name keys."""
    for m in master:
        if isinstance(m, dict):
            yield m["value"], m["name"]
        else:
            yield m[0], m[1]


def facet_counts_group(
    key,
    label,
    aria_label,
    master,
    counts,
    current,
    *,
    proportions=False,
    cutoff=None,
    toggle_base=None,
    toggle_param=None,
    toggle_label=None,
    expanded=False,
    list_id=None,
    trailing=None,
    always_render=False,
):
    """Single-select facet group: ordered master + pool counts → group dict.

    master is the ordered list of possible values — a list of (value,
    name) tuples or of dicts with value/name keys — and the render order
    is exactly that master order. counts maps value → count for the
    current pool; current is the selected value (or None). The caller
    owns the counting semantics (self-excluding SQL aggregates,
    fixed aggregates or Python-side buckets) — the builder only renders
    the counts it's given. Items whose value is absent from the pool
    (count 0 / missing) are omitted, so the group is None when the pool
    is empty unless always_render is set.

    Options:
    - proportions: each item gains proportion = count / max pool count
      (the --facet-prop CSS bar; opt-in — /links doesn't use it)
    - cutoff + toggle_base/toggle_param/toggle_label/expanded/list_id:
      when the items list is longer than cutoff, the items beyond it get
      the `extra` flag (hidden until expanded) and the group gains
      list_id/expanded plus a `more` toggle dict whose href comes from
      facet_toggle_url(toggle_base, toggle_param, expanded=not expanded)
      and whose label/param (e.g. "years" / "formats") feed the toggle
      text and the JS data attributes — the plural noun differs from the
      group label, so it's passed in rather than derived
    - trailing: item dicts rendered after the items (and after the more
      toggle) — the "No URL" bucket on /links, the temporal After/
      Before/No-year buckets on /datasets
    - always_render: return the group even when the pool is empty (the
      /datasets temporal facet always renders)
    """
    pool_max = max(counts.values(), default=1)
    items = []
    for i, (value, name) in enumerate(_master_pairs(master)):
        count = counts.get(value, 0)
        if count <= 0:
            continue
        item = {
            "value": value,
            "name": name,
            "count": count,
            "active": current == value,
        }
        if proportions:
            item["proportion"] = count / pool_max
        if cutoff is not None:
            item["extra"] = not expanded and i >= cutoff
        items.append(item)

    if not items and not always_render:
        return None

    group = facet_group(key, label, aria_label, items)
    if cutoff is not None and len(items) > cutoff:
        group["list_id"] = list_id
        group["expanded"] = expanded
        group["more"] = {
            "href": facet_toggle_url(toggle_base, toggle_param, expanded=not expanded),
            "expanded": expanded,
            "count": len(items) - cutoff,
            "label": toggle_label,
            "param": toggle_param,
        }
    if trailing:
        group["trailing"] = trailing
    return group


def facet_counts_multiselect_group(
    key,
    label,
    aria_label,
    master,
    counts,
    current,
    *,
    facet_url,
    proportions=False,
    trailing=None,
):
    """Multi-select facet group — the organisations pubyear case.

    Same inputs as facet_counts_group, but current is a collection and
    every item carries a per-item toggle href built from the passed-in
    facet_url(key, value) closure: clicking a selected value removes it
    from the selection (clearing the facet when it was the last one),
    clicking an unselected value adds it. When nothing is selected no
    hrefs are set (the template falls back to the plain facet_url).
    trailing lands on the group verbatim — post-list buckets rendered
    after the items (the "Never published" bucket), each carrying its own
    href (the plain facet_url fallback would replace the selection, not
    clear it when active)."""
    pool_max = max(counts.values(), default=1)
    items = []
    for value, name in _master_pairs(master):
        count = counts.get(value, 0)
        if count <= 0:
            continue
        is_included = bool(current and value in current)
        item = {
            "value": value,
            "name": name,
            "count": count,
            "active": is_included,
        }
        if current:
            rest = [x for x in current if x != value]
            if is_included:
                item["href"] = facet_url(key, ",".join(rest)) if rest else facet_url(key, "")
            else:
                item["href"] = facet_url(key, value)
        if proportions:
            item["proportion"] = count / pool_max
        items.append(item)

    if not items and not trailing:
        return None
    group = facet_group(key, label, aria_label, items)
    if trailing:
        group["trailing"] = trailing
    return group
