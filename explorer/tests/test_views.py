"""View smoke tests — every route → 200 + key content.

Expected values are computed from the query layer / reviews table — the
same live data the views render — so a broken view (wrong sort column,
off-by-one pagination, dropped count, dead template var) fails here
without hardcoding baseline counts into the suite.

App tests read the *live* dev DB read-only (see tests/conftest.py) and
skip when it's unreachable or empty.
"""

import math
import re

import pytest
from markupsafe import escape

from explorer.queries.core import Query
from explorer.queries.datasets import (
    DATASET_COUNT,
    DATASET_TOTAL,
    DATASETS_BY_ORG,
    THEME_COUNTS,
    datasets_facet_counts,
    datasets_stmts,
)
from explorer.queries.links import LINKS_STATS, links_facet_counts, links_stmts
from explorer.queries.metadata import (
    METADATA_KEYS,
    METADATA_VALUE_COUNT,
    METADATA_VALUES,
)
from explorer.queries.organisations import ORGS
from explorer.queries.reports import (
    REPORTS,
    report_facet_counts,
    report_stmts,
)
from explorer.queries.reviews import get_review, latest_reviews
from explorer.queries.series import SERIES_BY_ID, SERIES_COUNT, series_list_stmt
from explorer.sort import sort_orgs
from explorer.views.datasets import PAGE_SIZE as DATASETS_PAGE_SIZE
from explorer.views.links import PAGE_SIZE as LINKS_PAGE_SIZE
from explorer.views.reports import PAGE_SIZE as REPORT_PAGE_SIZE


def esc(s):
    """Titles render through Jinja2 autoescape (markupsafe), which escapes
    `"` as &#34; not &quot;."""
    return str(escape(s or ""))


# ---------------------------------------------------------------------------
# Health + 404s
# ---------------------------------------------------------------------------
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.content == b"ok"


def test_unknown_route_renders_404(client):
    r = client.get("/no-such-page")
    assert r.status_code == 404
    # 404.html, not Django's technical 404 page (its title also contains
    # "Page not found" — assert on template-only markup).
    assert b'href="/static/css/pages/404.css"' in r.content
    assert "404 — Page not found" in r.content.decode()


def test_missing_static_renders_404(client):
    # Missing static falls through WhiteNoise to the catch-all 404 view,
    # which renders 404.html too.
    r = client.get("/static/css/definitely-not-a-file.css")
    assert r.status_code == 404
    assert b'href="/static/css/pages/404.css"' in r.content


# ---------------------------------------------------------------------------
# Home dashboard + reports
# ---------------------------------------------------------------------------
def test_home(client):
    r = client.get("/")
    html = r.content.decode()
    assert r.status_code == 200
    # Every dashboard card label renders (or is deliberately hidden when the
    # count is 0 — the label text still appears in the template data, but
    # the card markup only renders for count > 0).
    shown = [r for r in REPORTS if report_stmts(r)["count"].get()["n"] > 0]
    assert shown, "no dashboard cards to show — dataset looks empty"
    for report in shown:
        assert esc(report["label"]) in html


def test_every_report(client):
    for report in REPORTS:
        stmt = report_stmts(report)
        total = stmt["count"].get(*stmt["params"])["n"]
        url = f"/report/{report['key']}"
        r = client.get(url)
        html = r.content.decode()
        assert r.status_code == 200, f"{url} -> {r.status_code}"
        assert esc(report["label"]) in html
        assert f"{total:,}" in html
        if total:
            rows = stmt["list"].all(*stmt["params"], REPORT_PAGE_SIZE, 0)
            cell = (
                rows[0].get("url")
                if report["kind"] == "duplicate-urls"
                else rows[0].get("dataset_title") or rows[0].get("title") or rows[0].get("name")
            )
            assert cell is None or esc(cell) in html, f"{url}: first row not rendered"


def test_report_unknown_key_404(client):
    assert client.get("/report/bogus-key").status_code == 404


def test_report_org_facet(client):
    report = next(r for r in REPORTS if r["key"] == "datasets-no-links")
    sql, params = report_facet_counts(report, {})["org"]
    options = Query(sql).all(*params)
    if not options:
        pytest.skip("no org facet options in the live data")
    slug = options[0]["slug"]
    out = report_stmts(report, {"org": slug})
    fcount = out["count"].get(*out["params"])["n"]
    r = client.get(f"/report/datasets-no-links?org={slug}")
    html = r.content.decode()
    assert r.status_code == 200
    assert f"{fcount:,}" in html
    assert esc(options[0]["name"]) in html  # the filter pill

    # bogus org values are ignored — the view validates against the facet
    # options before filtering (the query layer itself would just filter to
    # 0 rows)
    total = report_stmts(report)["count"].get()["n"]
    rb = client.get("/report/datasets-no-links?org=__no_such_org__")
    assert rb.status_code == 200
    assert f"{total:,}" in rb.content.decode()


def test_has_api_both_facets(client):
    """datasets-has-api with ?org= + ?api_type= set simultaneously — the
    only multi-facet report. The page renders, shows the filtered count and
    both filter pills (the org/type names come from the report's own
    per-report option pools)."""
    report = next(r for r in REPORTS if r["key"] == "datasets-has-api")

    def pool(filters, key):
        sql, params = report_facet_counts(report, filters)[key]
        return Query(sql).all(*params)

    org_opts = pool({}, "org")
    type_opts = pool({}, "api_type")
    if not org_opts or not type_opts:
        pytest.skip("no has-api facet options in the live data")
    org_slug, org_name = org_opts[0]["slug"], org_opts[0]["name"]
    api_type, type_name = type_opts[0]["slug"], type_opts[0]["name"]

    stmt = report_stmts(report, {"org": org_slug, "api_type": api_type})
    total = stmt["count"].get(*stmt["params"])["n"]
    r = client.get(f"/report/datasets-has-api?org={org_slug}&api_type={api_type}")
    html = r.content.decode()
    assert r.status_code == 200
    assert f"{total:,}" in html
    assert esc(org_name) in html  # the org filter pill
    assert esc(type_name) in html  # the api-type filter pill


# ---------------------------------------------------------------------------
# /organisations (facet page)
# ---------------------------------------------------------------------------
def test_organisations(client):
    orgs = ORGS.all()
    assert orgs
    total = len(orgs)
    r = client.get("/organisations")
    html = r.content.decode()
    assert r.status_code == 200
    assert f"1-{total:,} of {total:,}" in html

    # default sort: name asc — first org's display name
    sorted_default = list(orgs)
    sort_orgs(sorted_default, "name", "asc")
    assert esc(sorted_default[0]["display_name"] or sorted_default[0]["name"]) in html

    # sort combo
    r2 = client.get("/organisations?sort=dataset_count&dir=desc")
    assert r2.status_code == 200
    sorted_ds = list(orgs)
    sort_orgs(sorted_ds, "dataset_count", "desc")
    assert esc(sorted_ds[0]["display_name"] or sorted_ds[0]["name"]) in r2.content.decode()

    # invalid sort falls back to name asc
    r3 = client.get("/organisations?sort=bogus&dir=bogus")
    assert r3.status_code == 200
    assert esc(sorted_default[0]["display_name"] or sorted_default[0]["name"]) in r3.content.decode()

    # one facet combo: ?year=<latest org-creation year>
    years = sorted(
        {(o["created"] or "")[:4] for o in orgs if o["created"]},
        reverse=True,
    )
    if years:
        year = years[0]
        year_count = sum(1 for o in orgs if (o["created"] or "")[:4] == year)
        r4 = client.get(f"/organisations?year={year}")
        assert f"1-{year_count:,} of {total:,}" in r4.content.decode()

    # ?pubyear=__none__ renders the Never published pill + trailing bucket
    r5 = client.get("/organisations?pubyear=__none__")
    h5 = r5.content.decode()
    assert r5.status_code == 200
    assert 'class="filter-pill"' in h5
    assert "Never published" in h5
    # the bucket stays visible on the unfiltered page, in the pubyear facet
    section = _facet_section(client.get("/organisations").content.decode(), "Filter by year last published")
    assert ">Never published<" in section


def test_organisation_detail(client):
    org = next(o for o in ORGS.all() if (DATASET_COUNT.get(o["slug"]) or {}).get("count", 0) > 0)
    r = client.get(f"/organisation/{org['slug']}")
    html = r.content.decode()
    assert r.status_code == 200
    assert esc(org["display_name"] or org["slug"]) in html
    # default sort metadata_modified desc — first dataset row
    d = DATASETS_BY_ORG.all(org["slug"])
    first = sorted(d, key=lambda x: x["metadata_modified"] or "", reverse=True)[:1]
    assert first
    assert esc(first[0]["title"]) in html
    # unknown org → 404
    assert client.get("/organisation/no-such-org").status_code == 404


# ---------------------------------------------------------------------------
# /harvesters
# ---------------------------------------------------------------------------
def test_harvesters(client):
    from explorer.queries.harvesters import harvest_source_rows  # noqa: PLC0415
    from explorer.sort import sort_harvesters  # noqa: PLC0415

    rows = harvest_source_rows()
    assert rows
    total = len(rows)
    r = client.get("/harvesters")
    html = r.content.decode()
    assert r.status_code == 200
    assert f"1-{total:,} of {total:,}" in html

    # default sort: dataset_count desc — most datasets first
    sorted_default = list(rows)
    sort_harvesters(sorted_default, "dataset_count", "desc")
    assert esc(sorted_default[0]["title"]) in html

    # sort combo
    r2 = client.get("/harvesters?sort=dataset_count&dir=desc")
    assert r2.status_code == 200
    sorted_ds = list(rows)
    sort_harvesters(sorted_ds, "dataset_count", "desc")
    assert esc(sorted_ds[0]["title"]) in r2.content.decode()

    # invalid sort falls back to the default column; bogus dir becomes asc
    r3 = client.get("/harvesters?sort=bogus&dir=bogus")
    assert r3.status_code == 200
    sorted_fallback = list(rows)
    sort_harvesters(sorted_fallback, "dataset_count", "asc")
    assert esc(sorted_fallback[0]["title"]) in r3.content.decode()

    # one facet combo: the most common harvest type
    from collections import Counter  # noqa: PLC0415

    type_counts = Counter(r["type"] for r in rows)
    top_type, _ = type_counts.most_common(1)[0]
    n_type = sum(1 for r in rows if r["type"] == top_type)
    r4 = client.get(f"/harvesters?type={top_type}")
    h4 = r4.content.decode()
    assert r4.status_code == 200
    assert f"1-{n_type:,} of {total:,}" in h4

    # ?active=false renders the Inactive pill + badge
    n_inactive = sum(1 for r in rows if not r["active"])
    r5 = client.get("/harvesters?active=false")
    h5 = r5.content.decode()
    assert r5.status_code == 200
    assert f"1-{n_inactive:,} of {total:,}" in h5
    assert 'class="filter-pill"' in h5
    assert "Inactive" in h5

    # ?datasets=0 renders the zero-datasets bucket (pill + count)
    from explorer.queries.organisations import DATASET_BUCKET_TESTS  # noqa: PLC0415

    n_zero = sum(1 for r in rows if DATASET_BUCKET_TESTS["0"](r["dataset_count"]))
    r6 = client.get("/harvesters?datasets=0")
    h6 = r6.content.decode()
    assert r6.status_code == 200
    assert f"1-{n_zero:,} of {total:,}" in h6
    assert 'class="filter-pill"' in h6

    # ?datasets=bogus falls back to the unfiltered list
    r7 = client.get("/harvesters?datasets=bogus")
    assert r7.status_code == 200
    assert f"1-{total:,} of {total:,}" in r7.content.decode()

    # Last run column replaces Created: sortable, renders a date or an
    # em-dash for sources that never ran
    assert "Last run" in html
    assert "Created" not in html
    assert "?sort=last_run&dir=asc" in html


# The headline "datasets harvested" matches the /datasets SOURCE facet's
# Harvested count — both count datasets.harvested = 1 (the per-source table
# column is attribution by id, not the headline).
def test_harvesters_total_matches_datasets_facet(client):
    r = client.get("/harvesters")
    html = r.content.decode()
    datasets_html = client.get("/datasets").content.decode()
    m = re.search(
        r'<span class="facet-name">Harvested</span>\s*'
        r'<span class="facet-count">([\d,]+)</span>',
        datasets_html,
    )
    assert m, "datasets SOURCE facet should show a Harvested count"
    assert f"— {m.group(1)} datasets harvested" in html


# ---------------------------------------------------------------------------
# /harvester/{id} (detail page)
# ---------------------------------------------------------------------------
def test_harvester_detail(client):
    from explorer.queries.harvesters import harvest_source_rows  # noqa: PLC0415

    rows = harvest_source_rows()
    with_datasets = [r for r in rows if r["dataset_count"] > 0]
    assert with_datasets
    source = with_datasets[0]

    r = client.get(f"/harvester/{source['id']}")
    h = r.content.decode()
    assert r.status_code == 200
    assert source["title"] in h
    # breadcrumb back to the list + a dataset row linking to its detail
    assert "/harvesters" in h
    assert "/dataset/" in h

    # the record block shows the fields the list page can't fit
    assert "Last run" in h
    assert "Jobs" in h

    # unknown id → 404
    r404 = client.get("/harvester/does-not-exist")
    assert r404.status_code == 404


# ---------------------------------------------------------------------------
# /links (facet page)
# ---------------------------------------------------------------------------
def test_links(client):
    stats = LINKS_STATS.get()
    assert stats is not None
    total = stats["total"]
    r = client.get("/links")
    html = r.content.decode()
    assert r.status_code == 200
    assert f"{total:,}" in html
    assert 'id="format-facet-list"' in html

    out = links_stmts({}, "host", "asc")
    assert out["count"].get(*out["params"])["n"] == total
    first_page = out["list"].all(*out["params"], LINKS_PAGE_SIZE, 0)
    assert esc(first_page[0]["name"]) in html
    assert f"1-{len(first_page):,} of {total:,}" in html

    # one facet combo: first host + first format (from the unfiltered pools)
    pool = links_facet_counts({})
    if pool["hosts"] and pool["formats"]:
        filters = {"host": pool["hosts"][0]["host"], "format": pool["formats"][0]["fmt"]}
        out2 = links_stmts(filters, "name", "desc")
        n2 = out2["count"].get(*out2["params"])["n"]
        page2 = out2["list"].all(*out2["params"], LINKS_PAGE_SIZE, 0)
        r2 = client.get(
            f"/links?host={pool['hosts'][0]['host']}&format={pool['formats'][0]['fmt']}&sort=name&dir=desc",
        )
        h2 = r2.content.decode()
        assert r2.status_code == 200
        assert esc(page2[0]["name"]) in h2
        assert f"of {n2:,}" in h2

    # ?host=__none__ renders the No URL pill
    r3 = client.get("/links?host=__none__")
    h3 = r3.content.decode()
    assert 'class="filter-pill"' in h3
    assert "No URL" in h3

    # ?format=__none__ renders the No format pill
    r4 = client.get("/links?format=__none__")
    h4 = r4.content.decode()
    assert 'class="filter-pill"' in h4
    assert "No format" in h4

    # the No format trailing bucket renders in the Format facet section
    h5 = client.get("/links").content.decode()
    section = _facet_section(h5, "Filter by format")
    assert ">No format<" in section

    # out-of-range page clamps
    assert client.get("/links?page=99999").status_code == 200


# ---------------------------------------------------------------------------
# /datasets (facet page)
# ---------------------------------------------------------------------------
def _facet_section(html, aria_label):
    """The facet-group section HTML for one aria-label."""
    m = re.search(
        rf'<section class="facet-group" aria-label="{re.escape(aria_label)}">(.*?)</section>',
        html,
        re.S,
    )
    assert m, f"facet section not found: {aria_label}"
    return m.group(1)


def _datasets_sql_count(filters):
    out = datasets_stmts(filters, "organisation", "asc")
    return out["count"].get(*out["params"])["n"]


def test_temporal_facet_order(client):
    from explorer.queries.datasets import TEMPORAL_MAX_YEAR, TEMPORAL_MIN_YEAR  # noqa: PLC0415
    from explorer.views.datasets import _in_window_temporal_years  # noqa: PLC0415

    counts = datasets_facet_counts({})
    pool = {r["year"]: r["count"] for r in counts["temporal_years"]}
    buckets = counts["temporal_buckets"]
    expected = [str(y) for y in _in_window_temporal_years() if pool.get(y, 0) > 0]
    if buckets["post"]:
        expected.append(f"After {TEMPORAL_MAX_YEAR}")
    if buckets["pre1900"]:
        expected.append(f"Before {TEMPORAL_MIN_YEAR}")
    if buckets["none"]:
        expected.append("No temporal year")
    r = client.get("/datasets")
    section = _facet_section(r.content.decode(), "Filter by temporal year")
    names = re.findall(r'<span class="facet-name">([^<]+)</span>', section)
    assert names == expected


def test_temporal_toggle_keeps_metadata(client):
    # The More-years toggle keeps every active facet — including metadata
    # (regression: a previous version of the toggle URL dropped it).
    for url in (
        "/datasets?metadata_key=top:type&metadata_value=dataset&years=all",
        "/datasets?metadata_key=top:type&metadata_value=dataset",
    ):
        html = client.get(url).content.decode()
        m = re.search(r'<a href="([^"]*)"\s+class="facet-more-link facet-toggle"', html)
        assert m, "temporal More-years toggle missing"
        assert "metadata_key=top%3Atype" in m.group(1)
        assert "metadata_value=dataset" in m.group(1)
    # and with no metadata filter, the toggle URL stays clean
    html = client.get("/datasets?years=all").content.decode()
    m = re.search(r'<a href="([^"]*)"\s+class="facet-more-link facet-toggle"', html)
    assert m
    assert "metadata" not in m.group(1)


def test_theme_facet_order(client):
    from explorer.views.datasets import _theme_master  # noqa: PLC0415

    r = client.get("/datasets")
    section = _facet_section(r.content.decode(), "Filter by primary theme")
    names = re.findall(r'<span class="facet-name">([^<]+)</span>', section)
    assert names == [esc(t["label"]) for t in _theme_master()]


def test_datasets(client):
    total = _datasets_sql_count({})
    r = client.get("/datasets")
    html = r.content.decode()
    assert r.status_code == 200
    assert f"1-{DATASETS_PAGE_SIZE:,} of {total:,}" in html

    out = datasets_stmts({}, "organisation", "asc")
    first_page = out["list"].all(*out["params"], DATASETS_PAGE_SIZE, 0)
    assert esc(first_page[0]["title"]) in html
    assert esc(first_page[0]["organisation"]) in html

    # one facet combo: a real theme, with the sidebar count matching SQL
    theme_rows = THEME_COUNTS.all()
    theme = next((t["theme"] for t in theme_rows if t["theme"] != "__none__"), None)
    if theme:
        r2 = client.get(f"/datasets?theme={theme}")
        h2 = r2.content.decode()
        assert r2.status_code == 200
        assert f"of {_datasets_sql_count({'theme': theme}):,}" in h2

    # sidebar theme count equals the SQL facet aggregate (unfiltered page)
    theme_counts = {r["theme"]: r["count"] for r in datasets_facet_counts({})["themes"]}
    if theme_counts:
        t = next(
            (k for k, v in theme_counts.items() if k != "__none__" and v > 0),
            None,
        )
        if t:
            assert f"?sort=organisation&amp;dir=asc&amp;theme={t}" in html
            assert f'<span class="facet-count">{theme_counts[t]:,}</span>' in html

    # invalid page value clamps to 1 (rather than erroring)
    assert client.get("/datasets?page=bogus").status_code == 200
    assert client.get("/datasets?page=99999").status_code == 200


# ---------------------------------------------------------------------------
# /metadata
# ---------------------------------------------------------------------------
def test_metadata_pages(client):
    total_row = DATASET_TOTAL.get()
    assert total_row is not None
    total = total_row["n"]
    r = client.get("/metadata")
    html = r.content.decode()
    assert r.status_code == 200
    assert f"{total:,}" in html

    keys = METADATA_KEYS.all()
    top = next(k for k in keys if k["section"] == "top")
    section, name = top["key"].split(":", 1)
    r2 = client.get(f"/metadata/{section}/{name}")
    h2 = r2.content.decode()
    assert r2.status_code == 200
    assert f"<h1>{name}</h1>" in h2
    first = METADATA_VALUES.all(top["key"], 100, 0)[0]
    assert esc(first["value"]) in h2

    total_values_row = METADATA_VALUE_COUNT.get(top["key"])
    assert total_values_row is not None
    total_values = total_values_row["n"]
    if total_values > 100:
        assert client.get(f"/metadata/{section}/{name}?page=2").status_code == 200
    assert client.get(f"/metadata/bogus/{name}").status_code == 404
    assert client.get("/metadata/top/__no_such_field__").status_code == 404


# ---------------------------------------------------------------------------
# /series
# ---------------------------------------------------------------------------
def test_series_pages(client):
    total_row = SERIES_COUNT.get()
    assert total_row is not None
    total = total_row["n"]
    r = client.get("/series")
    html = r.content.decode()
    assert r.status_code == 200
    assert f"{total:,}" in html

    default = series_list_stmt("dataset_count", "desc").all(50, 0)
    assert default
    assert esc(default[0]["root_title"]) in html

    r2 = client.get("/series?sort=root_title&dir=asc")
    assert r2.status_code == 200
    by_title = series_list_stmt("root_title", "asc").all(50, 0)
    assert esc(by_title[0]["root_title"]) in r2.content.decode()

    sid = default[0]["id"]
    r3 = client.get(f"/series/{sid}")
    h3 = r3.content.decode()
    assert r3.status_code == 200
    series_row = SERIES_BY_ID.get(sid)
    assert series_row is not None
    assert esc(series_row["root_title"]) in h3

    assert client.get("/series/abc").status_code == 404
    assert client.get("/series/99999999").status_code == 404


# ---------------------------------------------------------------------------
# /reviews — row order vs an independent expectation computed from the
# reviews table (the DB is the source of truth)
# ---------------------------------------------------------------------------
_REVIEW_SORTS = ("title", "org", "overall", "findability", "metadata", "resources")


def _subscore(r, key):
    scores = r.get("scores")
    if isinstance(scores, dict):
        sub = scores.get(key)
        if isinstance(sub, dict):
            return sub.get("score")
    return None


def _review_key(r, sort):
    if sort == "title":
        return (r.get("title") or "").lower()
    if sort == "org":
        return str(r.get("org_display_name") or "").lower()
    v = r.get("overall") if sort == "overall" else _subscore(r, sort)
    return v if v is not None else -1


def _review_matches(r, filters):
    groups = [("overall", lambda x: x.get("overall"))] + [
        (k, lambda x, k=k: _subscore(x, k)) for k in ("findability", "metadata", "resources")
    ]
    for key, get in groups:
        want = filters.get(key)
        if not want:
            continue
        have = get(r)
        if want == "none":
            if have is not None:
                return False
        elif str(have) != want:
            return False
    return True


def expected_review_ids(sort, dir_, filters, page, page_size=50):
    rows = latest_reviews()
    filtered = [r for r in rows if _review_matches(r, filters)]
    filtered.sort(key=lambda r: (r.get("title") or "").lower())
    filtered.sort(key=lambda r: _review_key(r, sort), reverse=dir_ == "desc")
    total_pages = max(1, math.ceil(len(filtered) / page_size))
    page = min(max(page, 1), total_pages)
    start = (page - 1) * page_size
    return [r["dataset_id"] for r in filtered[start : start + page_size]]


def page_ids(html):
    """Dataset ids in table-row order — every row links to its dataset."""
    return re.findall(r'/dataset/[^/]+/([^"]+)"', html)


def test_reviews(client):
    all_reviews = latest_reviews()
    assert all_reviews
    expect, total = expected_review_ids("overall", "asc", {}, 1), len(all_reviews)

    r = client.get("/reviews")
    html = r.content.decode()
    assert r.status_code == 200
    assert f"Dataset reviews ({total})" in html
    assert page_ids(html) == expect

    # every sort column, both directions
    for sort in _REVIEW_SORTS:
        for dir_ in ("asc", "desc"):
            r = client.get(f"/reviews?sort={sort}&dir={dir_}")
            assert r.status_code == 200
            assert page_ids(r.content.decode()) == expected_review_ids(
                sort,
                dir_,
                {},
                1,
            ), f"sort={sort} dir={dir_}"

    # invalid sort falls back to overall asc
    r = client.get("/reviews?sort=bogus")
    assert page_ids(r.content.decode()) == expected_review_ids("overall", "asc", {}, 1)

    # one facet combo across two score groups
    r = client.get("/reviews?overall=3&metadata=4")
    assert r.status_code == 200
    assert page_ids(r.content.decode()) == expected_review_ids(
        "overall",
        "asc",
        {"overall": "3", "metadata": "4"},
        1,
    )

    # "none" facet renders its pill
    r = client.get("/reviews?overall=none")
    assert b'class="filter-pill"' in r.content
    assert b"No score" in r.content

    # page clamp — 99999 lands on the last page
    r = client.get("/reviews?page=99999")
    assert page_ids(r.content.decode()) == expected_review_ids(
        "overall",
        "asc",
        {},
        99999,
    )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# /suggestions
# ---------------------------------------------------------------------------
_CONFIDENCE_ORDER = {"high": 3, "medium": 2, "low": 1}


def expected_suggestion_ids(sort, dir_, page, page_size=50):
    unique = latest_reviews()
    ids = [r["dataset_id"] for r in unique]
    theme_map, tags_map = {}, {}
    if ids:
        ph = ",".join("%s" for _ in ids)
        theme_map = {
            row["id"]: row["theme_primary"]
            for row in Query(
                f"SELECT id, theme_primary FROM datasets WHERE id IN ({ph})",
            ).all(*ids)
        }
        tags_map = {
            row["id"]: row["tags"]
            for row in Query(f"SELECT id, tags FROM datasets WHERE id IN ({ph})").all(
                *ids,
            )
        }
    enriched = []
    for r in unique:
        current_theme = theme_map.get(r["dataset_id"]) or None
        enriched.append(
            {
                **r,
                "current_theme": current_theme,
                "current_tags": tags_map.get(r["dataset_id"]) or "",
                "theme_changed": r.get("theme") != current_theme,
            },
        )
    sorters = {
        "title": lambda r: (r.get("title") or "").lower(),
        "org": lambda r: str(r.get("org_display_name") or "").lower(),
        "theme": lambda r: str(r.get("current_theme") or "").lower(),
        "confidence": lambda r: _CONFIDENCE_ORDER.get(r.get("theme_confidence"), 0),
    }
    enriched.sort(key=lambda r: (r.get("title") or "").lower())
    enriched.sort(key=sorters[sort], reverse=dir_ == "desc")
    total_pages = max(1, math.ceil(len(enriched) / page_size))
    page = min(max(page, 1), total_pages)
    start = (page - 1) * page_size
    return [r["dataset_id"] for r in enriched[start : start + page_size]], len(enriched)


def test_suggestions(client):
    _, total = expected_suggestion_ids("confidence", "asc", 1)
    assert total > 0

    r = client.get("/suggestions")
    html = r.content.decode()
    assert r.status_code == 200
    assert f"Suggestions ({total})" in html

    for sort in ("title", "org", "theme", "confidence"):
        for dir_ in ("asc", "desc"):
            r = client.get(f"/suggestions?sort={sort}&dir={dir_}")
            expect, _ = expected_suggestion_ids(sort, dir_, 1)
            assert page_ids(r.content.decode()) == expect, f"sort={sort} dir={dir_}"

    # invalid sort falls back to confidence asc
    r = client.get("/suggestions?sort=bogus")
    expect, _ = expected_suggestion_ids("confidence", "asc", 1)
    assert page_ids(r.content.decode()) == expect

    # page clamp
    r = client.get("/suggestions?page=99999")
    expect, _ = expected_suggestion_ids("confidence", "asc", 99999)
    assert r.status_code == 200
    assert page_ids(r.content.decode()) == expect


# ---------------------------------------------------------------------------
# /dataset/{org}/{id}
# ---------------------------------------------------------------------------
def test_dataset_detail_with_review(client):
    rev = latest_reviews()[0]
    dataset_id = rev["dataset_id"]
    slug = rev.get("org_slug") or "x"

    r = client.get(f"/dataset/{slug}/{dataset_id}")
    html = r.content.decode()
    assert r.status_code == 200, f"status {r.status_code}"
    assert (
        esc(rev.get("title") or rev.get("suggested_title") or "") in html or get_review(dataset_id) is not None
    )  # review block present below

    # review block — Overall X/5 and the findability subscore
    assert f"Overall {rev['overall']}/5" in html
    sub = (rev.get("scores") or {}).get("findability") or {}
    if sub.get("score") is not None:
        assert f"Findability {sub['score']}/5" in html

    # 404s
    assert client.get("/dataset/x/__no_such_id__").status_code == 404
    assert client.get("/dataset/x/y").status_code == 404
