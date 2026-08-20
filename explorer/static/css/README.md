# CSS structure

Plain CSS, split into files. No build step — files are loaded straight from
`explorer/static/` with multiple `<link>` tags.

## Files

| File | What it styles | Loaded |
|---|---|---|
| `base.css` | Fonts, design tokens (`:root`), reset, container, subtitle, count | every page (layout) |
| `layout.css` | Site header, nav, footer | every page (layout) |
| `components/table.css` | The shared `.data-table` component — text wraps by default; columns are typed with `.col-num` / `.col-date` | every page (layout) |
| `components/badges.css` | Status badges, format badges, score badges | every page (layout) |
| `components/pagination.css` | `.pagination`, `.page-link`, `.page-info` | every page (layout) |
| `components/chart.css` | Yearly bar chart | `/`, `/datasets`, `/organisation/:slug` |
| `components/pills.css` | Active-filter pills | `/datasets`, `/links`, `/report/:key`, `/reviews` |
| `components/facets.css` | Facet sidebar + `.links-layout` grid | `/datasets`, `/links`, `/report/:key`, `/reviews` |
| `pages/dashboard.css` | Dashboard summary cards (home) | `/` |
| `pages/report.css` | Report pages — description + fixed-width tables | `/report/:key` |
| `pages/dataset.css` | Dataset detail page (incl. LLM review card) | `/dataset/:orgSlug/:datasetId` |
| `pages/404.css` | Not-found page | 404 |

## Rules

- **Shared** CSS (base, layout, table, badges, pagination) is linked once in
  `views/_layout.njk` and applies to every page.
- **Feature/page** CSS is linked via `{% block styles %}` at the top of the
  template that uses it — see the table above for which template needs which
  file. Keep the mapping in sync if you add or move styles.
- Order doesn't matter between files (selectors don't overlap), but keep
  `base.css` first so tokens/reset are in place.
- Design tokens (colours, radii, shadows) live in `base.css` — prefer
  `var(--token)` over hardcoded values.
- If styles are used by more than one page, they're a *component*; if they
  belong to exactly one page, they go in `pages/`. Move a class when its
  usage changes — don't duplicate it.
