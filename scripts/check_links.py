"""Link checker — HEAD → GET → Playwright fallback.

Reads all distinct unchecked URLs from the `links` table, checks each
one, and upserts results into `link_check_results`. Resumable: already-
checked URLs are skipped unless --force is given. Safe to interrupt and
rerun.

Usage:
    python -m scripts.check_links [options]
    just check-links [options]
"""

import asyncio
import json
import re
import sys
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from itertools import chain, zip_longest
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import typer

try:
    from playwright.async_api import async_playwright as _async_playwright

    _HAS_PLAYWRIGHT = True
except ImportError:
    _HAS_PLAYWRIGHT = False

from scripts.db import Db, connect, database_url

BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
BROWSER_HEADERS = {"User-Agent": BROWSER_UA, "Accept": "*/*"}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mK]")
_CALL_LOG_RE = re.compile(r"\n?=+\s*logs\s*=+.*$", re.DOTALL | re.IGNORECASE)

app = typer.Typer(add_completion=False)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def is_checkable_url(url: str | None) -> bool:
    """Return True for http/https URLs that look valid enough to attempt."""
    if not url or not url.strip():
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _strip_pw_error(msg: str) -> str:
    """Strip ANSI codes and Playwright call-log appendix from an error message."""
    msg = _ANSI_RE.sub("", msg)
    msg = _CALL_LOG_RE.sub("", msg)
    return msg.strip()


def _classify_connect_error(exc: httpx.ConnectError) -> str:
    msg = str(exc).lower()
    if "ssl" in msg or "certificate" in msg or "handshake" in msg:
        return f"ssl:{exc}"
    if "getaddrinfo" in msg or "name or service not known" in msg or "nodename nor servname" in msg:
        return f"dns:{exc}"
    return f"connect:{exc}"


# ---------------------------------------------------------------------------
# Per-host rate gate (asyncio)
# ---------------------------------------------------------------------------


class HostGate:
    """Per-host rate limiter with dead-host detection.

    Rate logic:
      - success (any 2xx-4xx except 429): count streak; every SPEED_UP_AFTER
        consecutive successes steps the interval down by floor_ms toward floor_ms.
      - 429: permanently lock acceleration; step interval up by floor_ms toward start_ms.
      - 5xx / timeout / connect error: count toward DEAD_THRESHOLD; no rate change.

    Dead-host logic:
      - Once a host accumulates DEAD_THRESHOLD dead signals, is_dead() returns True
        and record() returns True exactly once (the turn it crosses the threshold).

    Host groups:
      - Hosts in the same group share a single rate-limit gate. A 429 (or any signal)
        on any member slows the whole group equally. Pass groups as a list of sets.
    """

    SPEED_UP_AFTER = 5

    def __init__(
        self,
        start_ms: int = 1000,
        floor_ms: int = 250,
        dead_threshold: int = 10,
        groups: dict[str, set[str]] | None = None,
        host_config: dict[str, dict] | None = None,
    ) -> None:
        self._dead_threshold = dead_threshold
        self._start = start_ms / 1000.0
        self._floor = floor_ms / 1000.0
        # Map each host to its group name.
        self._host_to_group: dict[str, str] = {}
        for name, hosts in (groups or {}).items():
            for h in hosts:
                self._host_to_group[h] = name
        # Per-host overrides, keyed by resolved group key.
        self._host_cfg: dict[str, dict] = {}
        for host, cfg in (host_config or {}).items():
            self._host_cfg[self._resolve(host)] = cfg
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}
        self._intervals: dict[str, float] = {}
        self._streak: dict[str, int] = {}
        self._accelerating: dict[str, bool] = {}
        self._dead_counts: dict[str, int] = {}
        self._dead: set[str] = set()

    def _resolve(self, host: str) -> str:
        return self._host_to_group.get(host, host)

    def _start_for(self, key: str) -> float:
        ms = self._host_cfg.get(key, {}).get("start_ms")
        return ms / 1000.0 if ms is not None else self._start

    def _floor_for(self, key: str) -> float:
        ms = self._host_cfg.get(key, {}).get("floor_ms")
        return ms / 1000.0 if ms is not None else self._floor

    def _cur_interval(self, key: str) -> float:
        return self._intervals.get(key, self._start_for(key))

    async def acquire(self, host: str) -> None:
        key = self._resolve(host)
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
            self._last[key] = 0.0
        async with self._locks[key]:
            now = time.monotonic()
            wait = self._last[key] + self._cur_interval(key) - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[key] = time.monotonic()

    def record(self, host: str, *, status: int | None, error: str | None) -> bool:
        """Update per-host state. Returns True if the host just crossed the dead threshold."""
        key = self._resolve(host)
        err = error or ""
        is_success = status is not None and status != 429 and status < 500
        is_429 = status == 429
        is_dead_signal = (
            (status is not None and status >= 500)
            or err.startswith(
                ("timeout:", "connect:", "dns:", "ssl:"),
            )
            or (err.startswith("playwright:") and err != "playwright:unavailable")
        )

        if is_success:
            if self._accelerating.get(key, True):
                streak = self._streak.get(key, 0) + 1
                self._streak[key] = streak
                if streak >= self.SPEED_UP_AFTER:
                    floor = self._floor_for(key)
                    self._intervals[key] = max(self._cur_interval(key) - floor, floor)
                    self._streak[key] = 0
        elif is_429:
            self._accelerating[key] = False
            self._streak[key] = 0
            self._intervals[key] = min(self._cur_interval(key) + self._floor_for(key), self._start_for(key))
        elif is_dead_signal:
            self._streak[key] = 0
            count = self._dead_counts.get(key, 0) + 1
            self._dead_counts[key] = count
            if self._dead_threshold and count >= self._dead_threshold and key not in self._dead:
                self._dead.add(key)
                return True

        return False

    def is_dead(self, host: str) -> bool:
        return self._resolve(host) in self._dead


# ---------------------------------------------------------------------------
# check_url — HEAD → GET → Playwright chain
# ---------------------------------------------------------------------------


def _make_result(
    url: str,
    *,
    method: str,
    ok: bool,
    status: int | None = None,
    final_url: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "url": url,
        "checked_at": datetime.now(tz=UTC),
        "method": method,
        "ok": ok,
        "http_status": status,
        "final_url": final_url,
        "error": error,
    }


async def check_url(
    url: str,
    *,
    fetch_fn: Callable,
    pw_fn: Callable | None = None,
    gate: HostGate | None = None,
    pw_sem: asyncio.Semaphore | None = None,
    timeout_ms: int = 15_000,
    bypass_hosts: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Check one URL using the HEAD → GET → Playwright chain.

    fetch_fn(method, url) and pw_fn(url) are injected so tests can mock them.
    gate and pw_sem are optional; when absent their acquisition is skipped.
    """
    host = urlparse(url).hostname or ""

    if host in bypass_hosts:
        return _make_result(url, method="SKIPPED", ok=False, error="connect:bypass host")

    timeout_s = timeout_ms / 1000.0

    async def _fetch(method: str) -> httpx.Response:
        if gate:
            await gate.acquire(host)
        return await fetch_fn(method, url)

    # --- HEAD ---
    head_status: int | None = None
    head_final: str | None = None
    need_get = False
    need_pw = False

    for _attempt in range(2):
        try:
            resp = await _fetch("HEAD")
            head_status = resp.status_code
            head_final = str(resp.url)
            break
        except httpx.TimeoutException:
            # timeout: skip GET, go straight to Playwright
            need_pw = True
            break
        except httpx.ConnectError:
            if _attempt == 0:
                await asyncio.sleep(1)
                continue
            # connect error: fall through to GET
            need_get = True
            break
        except httpx.HTTPStatusError:
            break
        except httpx.HTTPError:
            if _attempt == 0:
                await asyncio.sleep(1)
                continue
            # other HTTP error: fall through to GET
            need_get = True
            break

    if head_status is not None and not need_pw:
        if 200 <= head_status < 400:
            return _make_result(url, method="HEAD", ok=True, status=head_status, final_url=head_final)
        if head_status == 429:
            return _make_result(url, method="HEAD", ok=False, status=429)
        if head_status in (404, 410):
            return _make_result(url, method="HEAD", ok=False, status=head_status)
        need_get = True

    # --- GET ---
    if need_get or (head_status is None and not need_pw):
        get_status: int | None = None
        get_final: str | None = None

        for _attempt in range(2):
            try:
                resp = await _fetch("GET")
                get_status = resp.status_code
                get_final = str(resp.url)
                break
            except httpx.TimeoutException:
                # timeout on GET: broken, skip Playwright
                return _make_result(url, method="GET", ok=False, error=f"timeout:{timeout_s}s")
            except httpx.ConnectError:
                if _attempt == 0:
                    await asyncio.sleep(1)
                    continue
                # connect error on GET: fall through to Playwright (get_status stays None)
                break
            except httpx.HTTPError:
                if _attempt == 0:
                    await asyncio.sleep(1)
                    continue
                # other HTTP error on GET: fall through to Playwright
                break

        if get_status is not None:
            if 200 <= get_status < 400:
                return _make_result(url, method="GET", ok=True, status=get_status, final_url=get_final)
            if get_status == 429:
                return _make_result(url, method="GET", ok=False, status=429)
            if get_status in (404, 410):
                return _make_result(url, method="GET", ok=False, status=get_status)
            need_pw = True
        else:
            need_pw = True

    # --- Playwright ---
    if not need_pw or pw_fn is None:
        status = head_status if head_status is not None else None
        return _make_result(url, method="ERROR", ok=False, status=status, error="playwright:unavailable")

    if pw_sem:
        async with pw_sem:
            if gate:
                await gate.acquire(host)
            pw_result = await pw_fn(url)
    else:
        if gate:
            await gate.acquire(host)
        pw_result = await pw_fn(url)

    if pw_result.get("ok"):
        return _make_result(
            url,
            method="PLAYWRIGHT",
            ok=True,
            status=pw_result.get("status"),
            final_url=pw_result.get("final_url"),
        )
    return _make_result(
        url,
        method="PLAYWRIGHT",
        ok=False,
        status=pw_result.get("status"),
        error=pw_result.get("error"),
    )


# ---------------------------------------------------------------------------
# Playwright helper (constructed in main, not imported at module level)
# ---------------------------------------------------------------------------


async def make_pw_fn(browser: Any, timeout_ms: int) -> Callable:
    """Return a coroutine that navigates to url using a fresh browser context."""

    async def pw_check(url: str) -> dict[str, Any]:
        ctx = await browser.new_context()
        try:
            page = await ctx.new_page()
            try:
                response = await page.goto(url, timeout=timeout_ms, wait_until="load")
                final = page.url
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if "download" in msg.lower():
                    return {"ok": True, "status": None, "final_url": url}
                clean = _strip_pw_error(msg)[:300]
                return {"ok": False, "status": None, "error": f"playwright:{clean}"}
            else:
                if response is None:
                    return {"ok": True, "status": None, "final_url": final}
                status = response.status
                return {"ok": status < 400, "status": status, "final_url": final}
            finally:
                await page.close()
        finally:
            await ctx.close()

    return pw_check


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

_UPSERT_RESULT_SQL = """
INSERT INTO link_check_results
    (url, checked_at, method, ok, http_status, final_url, error)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (url) DO UPDATE SET
    checked_at  = EXCLUDED.checked_at,
    method      = EXCLUDED.method,
    ok          = EXCLUDED.ok,
    http_status = EXCLUDED.http_status,
    final_url   = EXCLUDED.final_url,
    error       = EXCLUDED.error
"""

_LOAD_SQL = """
SELECT DISTINCT l.url
FROM links l
WHERE l.url IS NOT NULL
  AND l.url LIKE 'http%%'
  AND l.url ~* '^https{{0,1}}://[^/]'
  AND NOT EXISTS (
      SELECT 1 FROM link_check_results lcr
      WHERE lcr.url = l.url AND lcr.checked_at IS NOT NULL
  )
  {extra}
ORDER BY l.url
"""

_LOAD_FORCE_SQL = """
SELECT DISTINCT l.url
FROM links l
WHERE l.url IS NOT NULL
  AND l.url LIKE 'http%%'
  AND l.url ~* '^https{{0,1}}://[^/]'
  {extra}
ORDER BY l.url
"""

_LOAD_ERRORS_SQL = """
SELECT DISTINCT l.url
FROM links l
JOIN link_check_results lcr ON lcr.url = l.url
WHERE l.url IS NOT NULL
  AND l.url LIKE 'http%%'
  AND l.url ~* '^https{{0,1}}://[^/]'
  AND lcr.ok = false
  {extra}
ORDER BY l.url
"""


def _interleave_by_host(urls: list[str]) -> list[str]:
    """Round-robin interleave URLs across hosts, shuffled within each host group."""
    by_host: dict[str, list[str]] = defaultdict(list)
    for url in urls:
        by_host[urlparse(url).hostname or ""].append(url)
    groups = list(by_host.values())
    return [u for u in chain.from_iterable(zip_longest(*groups)) if u is not None]


def load_urls(
    db: Db,
    *,
    force: bool = False,
    errors_only: bool = False,
    limit: int | None = None,
    domain: str | None = None,
    publisher: str | None = None,
) -> list[str]:
    """Return unchecked (or all, with --force; or error results only, with --errors-only) http URLs from links."""
    clauses = []
    params: list[Any] = []

    if domain:
        clauses.append("AND (split_part(l.url, '/', 3) = ? OR split_part(l.url, '/', 3) LIKE ?)")
        params.extend([domain, f"%.{domain}"])
    if publisher:
        clauses.append("AND l.org_slug = ?")
        params.append(publisher)

    extra = "\n  ".join(clauses)
    if errors_only:
        template = _LOAD_ERRORS_SQL
    elif force:
        template = _LOAD_FORCE_SQL
    else:
        template = _LOAD_SQL
    sql = template.format(extra=extra)

    rows = db.prepare(sql).all(*params)
    urls = [r["url"] for r in rows if is_checkable_url(r["url"])]
    urls = _interleave_by_host(urls)
    if limit:
        urls = urls[:limit]
    return urls


def write_result(db: Db, result: dict[str, Any]) -> None:
    stmt = db.prepare(_UPSERT_RESULT_SQL)
    stmt.run(
        result["url"],
        result["checked_at"],
        result["method"],
        result["ok"],
        result["http_status"],
        result["final_url"],
        result["error"],
    )


_MARK_MALFORMED_SQL = """
INSERT INTO link_check_results (url, checked_at, method, ok, http_status, final_url, error)
SELECT DISTINCT l.url, %s, 'SKIPPED', false, NULL::integer, NULL, 'url:malformed'
FROM links l
WHERE l.host IS NULL AND l.url IS NOT NULL AND l.url != ''
ON CONFLICT (url) DO NOTHING
"""

# Catches anything that slips past the malformed mark but can't be HTTP-checked:
# typo schemes (htts://, hhttps://, ttp://), non-HTTP schemes (ftp://, file://),
# wrong case (Https://), and http-prefixed-but-malformed (http:/foo, https:///foo).
_MARK_UNCHECKABLE_SQL = """
INSERT INTO link_check_results (url, checked_at, method, ok, http_status, final_url, error)
SELECT DISTINCT l.url, %s, 'SKIPPED', false, NULL::integer, NULL, 'url:malformed'
FROM links l
WHERE l.url IS NOT NULL AND l.url != ''
  AND NOT (l.url LIKE 'http%%' AND l.url ~* '^https?://[^/]')
ON CONFLICT (url) DO NOTHING
"""


def _mark_malformed_urls(db: Db) -> int:
    """Mark links with unparseable URLs (host IS NULL) as url:malformed. Returns count marked."""
    now = datetime.now(tz=UTC)
    with db.conn.cursor() as cur:
        cur.execute(_MARK_MALFORMED_SQL, (now,))
        return cur.rowcount


def _mark_uncheckable_urls(db: Db) -> int:
    """Mark non-HTTP/non-checkable URLs (typo schemes, ftp://, etc.) as url:malformed."""
    now = datetime.now(tz=UTC)
    with db.conn.cursor() as cur:
        cur.execute(_MARK_UNCHECKABLE_SQL, (now,))
        return cur.rowcount


_DEAD_HOST_SQL = """
UPDATE link_check_results
SET checked_at  = %s,
    method      = 'SKIPPED',
    ok          = false,
    http_status = NULL,
    final_url   = NULL,
    error       = 'timeout:dead host'
WHERE url IN (SELECT DISTINCT url FROM links WHERE host = %s)
  AND checked_at IS NULL
"""

_REFRESH_LINK_HEALTH_SQL = """
INSERT INTO org_link_health (org_slug, link_health)
SELECT l.org_slug,
       COUNT(*) FILTER (WHERE lcr.ok) * 100.0 / NULLIF(COUNT(*), 0) AS link_health
FROM links l
LEFT JOIN link_check_results lcr ON l.url = lcr.url
GROUP BY l.org_slug
ON CONFLICT (org_slug) DO UPDATE SET link_health = EXCLUDED.link_health
"""


def _refresh_org_link_health(db: Db) -> None:
    with db.conn.cursor() as cur:
        cur.execute(_REFRESH_LINK_HEALTH_SQL)


def _refresh_link_status(db: Db) -> None:
    """Rebuild the /links/status matview from the fresh check results.

    CONCURRENTLY so a live site keeps reading the previous snapshot while
    the view rebuilds; the unique index on link_id (migrations/0003) is what
    makes that possible. db.exec autocommits, so this is not in a transaction
    block — a requirement of CONCURRENTLY.
    """
    db.exec("REFRESH MATERIALIZED VIEW CONCURRENTLY mv_link_status")


def _bulk_mark_dead_host(db: Db, host: str) -> int:
    """Mark all remaining unchecked URLs for host as timed-out. Returns count marked."""
    now = datetime.now(tz=UTC)
    with db.conn.cursor() as cur:
        cur.execute(_DEAD_HOST_SQL, (now, host))
        return cur.rowcount


# ---------------------------------------------------------------------------
# Writer coroutine (single consumer, no lock contention on DB)
# ---------------------------------------------------------------------------


async def writer(queue: asyncio.Queue, db: Db) -> None:
    while True:
        item = await queue.get()
        if item is None:
            queue.task_done()
            break
        write_result(db, item)
        queue.task_done()


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


async def _process_url(
    url: str,
    *,
    fetch_fn: Callable,
    pw_fn: Callable | None,
    gate: HostGate,
    pw_sem: asyncio.Semaphore,
    worker_sem: asyncio.Semaphore,
    timeout_ms: int,
    queue: asyncio.Queue,
    counter: list[int],
    db: Db,
) -> None:
    host = urlparse(url).hostname or ""

    async with worker_sem:
        if gate.is_dead(host):
            counter[0] += 1
            return

        try:
            result = await check_url(
                url,
                fetch_fn=fetch_fn,
                pw_fn=pw_fn,
                gate=gate,
                pw_sem=pw_sem,
                timeout_ms=timeout_ms,
            )
            newly_dead = gate.record(host, status=result.get("http_status"), error=result.get("error"))
            if newly_dead:
                n = _bulk_mark_dead_host(db, host)
                print(f"  Dead host {host}: marked {n} remaining URL(s) as timed-out", flush=True)
        except Exception as exc:  # noqa: BLE001
            result = _make_result(url, method="ERROR", ok=False, error=f"connect:{exc}")
        await queue.put(result)
        counter[0] += 1


# ---------------------------------------------------------------------------
# Progress logger
# ---------------------------------------------------------------------------


async def _log_progress(
    counter: list[int],
    total: int,
    worker_sem: asyncio.Semaphore,
    pw_sem: asyncio.Semaphore,
    workers: int,
    pw_pages: int,
    queue: asyncio.Queue,
    interval: int = 30,
) -> None:
    last_n = 0
    while True:
        await asyncio.sleep(interval)
        n = counter[0]
        rate = (n - last_n) / interval
        last_n = n
        pct = n * 100 // total if total else 0
        w_used = workers - worker_sem._value  # noqa: SLF001
        pw_used = pw_pages - pw_sem._value  # noqa: SLF001
        q_depth = queue.qsize()
        print(
            f"  {n}/{total} ({pct}%) | {rate:.1f}/s"
            f" | workers {w_used}/{workers} | playwright {pw_used}/{pw_pages}"
            f" | queue {q_depth}",
            flush=True,
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


@app.command()
def main(
    limit: int = typer.Option(0, help="Only check the first N unchecked URLs (0 = all)"),
    domain: str = typer.Option("", help="Restrict to URLs on this domain or its subdomains"),
    publisher: str = typer.Option("", help="Restrict to URLs from this publisher (organisation slug)"),
    workers: int = typer.Option(200, help="URLs in flight at once"),
    pw_pages: int = typer.Option(70, help="Max concurrent Playwright pages"),
    timeout_ms: int = typer.Option(15_000, help="HTTP timeout per request (ms)"),
    start_ms: int = typer.Option(1_000, help="Starting gap per host in ms (1 req/s); speeds up toward --floor-ms"),
    floor_ms: int = typer.Option(250, help="Minimum gap per host in ms (4 req/s)"),
    dead_threshold: int = typer.Option(10, help="Mark host dead after N consecutive failures (0 = disable)"),
    force: bool = typer.Option(False, help="Recheck URLs already in link_check_results"),
    errors_only: bool = typer.Option(False, help="Recheck only URLs whose last result was an error (ok=false)"),
    group_hosts: list[str] = typer.Option(  # noqa: B008
        [],
        help="Comma-separated hosts that share one rate-limit gate (repeat for multiple groups)",
    ),
    config: str = typer.Option(
        "check_links_config.json",
        help="Path to JSON config file for host groups and per-host settings",
    ),
) -> None:
    groups = [set(g.split(",")) for g in group_hosts if g]
    asyncio.run(
        _main(
            limit=limit or None,
            domain=domain or None,
            publisher=publisher or None,
            workers=workers,
            pw_pages=pw_pages,
            timeout_ms=timeout_ms,
            start_ms=start_ms,
            floor_ms=floor_ms,
            dead_threshold=dead_threshold,
            force=force,
            errors_only=errors_only,
            groups=groups,
            config_file=config,
        ),
    )


def _load_config(config_file: str) -> dict:
    path = Path(config_file)
    if not path.exists():
        return {}
    with path.open() as f:
        return json.load(f)


async def _main(
    *,
    limit: int | None,
    domain: str | None,
    publisher: str | None,
    workers: int,
    pw_pages: int,
    timeout_ms: int,
    start_ms: int,
    floor_ms: int,
    dead_threshold: int,
    force: bool,
    errors_only: bool = False,
    groups: list[set[str]] | None = None,
    config_file: str = "check_links_config.json",
) -> None:
    cfg = _load_config(config_file)
    # Named groups from config file; unnamed CLI groups use min(hosts) as the name.
    all_groups: dict[str, set[str]] = {name: set(hosts) for name, hosts in cfg.get("groups", {}).items()}
    for g in groups or []:
        all_groups.setdefault(min(g), g)
    host_config: dict[str, dict] = cfg.get("hosts", {})

    db = connect(database_url())
    try:
        n_malformed = _mark_malformed_urls(db)
        if n_malformed:
            print(f"  Marked {n_malformed} malformed URL(s) as url:malformed", flush=True)

        n_uncheckable = _mark_uncheckable_urls(db)
        if n_uncheckable:
            print(f"  Marked {n_uncheckable} uncheckable URL(s) as url:malformed", flush=True)

        print("Loading URLs…", flush=True)
        urls = load_urls(db, force=force, errors_only=errors_only, limit=limit, domain=domain, publisher=publisher)
        total = len(urls)
        print(f"  {total} URL(s) to check", flush=True)
        if not total:
            return

        if not _HAS_PLAYWRIGHT:
            print("  playwright not installed — Playwright fallback disabled", flush=True)

        gate = HostGate(start_ms, floor_ms, dead_threshold, groups=all_groups, host_config=host_config)
        worker_sem = asyncio.Semaphore(workers)
        pw_sem = asyncio.Semaphore(pw_pages)
        queue: asyncio.Queue[dict | None] = asyncio.Queue()
        counter: list[int] = [0]

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout_ms / 1000.0,
            headers=BROWSER_HEADERS,
            verify=True,
        ) as client:

            async def fetch_fn(method: str, url: str) -> httpx.Response:
                if method == "GET":
                    # Stream GET requests: read headers only, skip the body.
                    # Prevents memory bloat on large file downloads.
                    resp = await client.send(
                        client.build_request("GET", url),
                        stream=True,
                    )
                    await resp.aclose()
                    return resp
                return await client.request(method, url)

            if _HAS_PLAYWRIGHT:
                try:
                    async with _async_playwright() as pw:
                        browser = await pw.chromium.launch(headless=True)
                        pw_fn = await make_pw_fn(browser, timeout_ms)
                        await _run_workers(
                            urls,
                            fetch_fn=fetch_fn,
                            pw_fn=pw_fn,
                            gate=gate,
                            pw_sem=pw_sem,
                            worker_sem=worker_sem,
                            timeout_ms=timeout_ms,
                            queue=queue,
                            db=db,
                            counter=counter,
                            total=total,
                            workers=workers,
                            pw_pages=pw_pages,
                        )
                        await browser.close()
                except Exception as exc:  # noqa: BLE001
                    print(f"  Playwright browser unavailable ({exc}) — falling back to HTTP only", flush=True)
                    await _run_workers(
                        urls,
                        fetch_fn=fetch_fn,
                        pw_fn=None,
                        gate=gate,
                        pw_sem=pw_sem,
                        worker_sem=worker_sem,
                        timeout_ms=timeout_ms,
                        queue=queue,
                        db=db,
                        counter=counter,
                        total=total,
                        workers=workers,
                        pw_pages=pw_pages,
                    )
            else:
                await _run_workers(
                    urls,
                    fetch_fn=fetch_fn,
                    pw_fn=None,
                    gate=gate,
                    pw_sem=pw_sem,
                    worker_sem=worker_sem,
                    timeout_ms=timeout_ms,
                    queue=queue,
                    db=db,
                    counter=counter,
                    total=total,
                    workers=workers,
                    pw_pages=pw_pages,
                )

        print(f"Done: {counter[0]}/{total} URL(s) checked.", flush=True)
        print("Refreshing org link health…", flush=True)
        _refresh_org_link_health(db)
        print("Refreshing link status report…", flush=True)
        _refresh_link_status(db)
    finally:
        db.close()


async def _run_workers(
    urls: list[str],
    *,
    fetch_fn: Callable,
    pw_fn: Callable | None,
    gate: HostGate,
    pw_sem: asyncio.Semaphore,
    worker_sem: asyncio.Semaphore,
    timeout_ms: int,
    queue: asyncio.Queue,
    db: Db,
    counter: list[int],
    total: int,
    workers: int,
    pw_pages: int,
) -> None:
    writer_task = asyncio.create_task(writer(queue, db))
    logger_task = asyncio.create_task(_log_progress(counter, total, worker_sem, pw_sem, workers, pw_pages, queue))

    tasks = [
        asyncio.create_task(
            _process_url(
                url,
                fetch_fn=fetch_fn,
                pw_fn=pw_fn,
                gate=gate,
                pw_sem=pw_sem,
                worker_sem=worker_sem,
                timeout_ms=timeout_ms,
                queue=queue,
                counter=counter,
                db=db,
            ),
        )
        for url in urls
    ]

    await asyncio.gather(*tasks)
    await queue.put(None)
    await writer_task
    logger_task.cancel()


if __name__ == "__main__":
    try:
        app()
    except (RuntimeError, OSError, KeyError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
