"""Unit tests for scripts/check_links.py (offline — no DB, no network).

Covers is_checkable_url and the check_url HEAD→GET→Playwright chain;
fetch_fn and pw_fn are injected AsyncMocks so no real HTTP or browser
is needed.
"""

import asyncio
import os
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://localhost:5432/test-db")

import scripts.check_links as cl

# ---------------------------------------------------------------------------
# is_checkable_url
# ---------------------------------------------------------------------------


def test_is_checkable_url_valid_http():
    assert cl.is_checkable_url("http://example.com/file.csv") is True


def test_is_checkable_url_valid_https():
    assert cl.is_checkable_url("https://data.gov.uk/dataset/abc") is True


def test_is_checkable_url_blank():
    assert cl.is_checkable_url("") is False
    assert cl.is_checkable_url(None) is False
    assert cl.is_checkable_url("   ") is False


def test_is_checkable_url_ftp():
    assert cl.is_checkable_url("ftp://example.com/file") is False


def test_is_checkable_url_mailto():
    assert cl.is_checkable_url("mailto:user@example.com") is False


def test_is_checkable_url_no_host():
    assert cl.is_checkable_url("http://") is False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resp(status: int, url: str = "http://example.com/resource") -> MagicMock:
    """Build a fake httpx.Response-like object."""
    r = MagicMock()
    r.status_code = status
    r.url = url
    return r


def _run(coro):
    return asyncio.run(coro)


URL = "http://example.com/resource"


# ---------------------------------------------------------------------------
# check_url — HEAD branch
# ---------------------------------------------------------------------------


def test_head_200_ok():
    fetch = AsyncMock(return_value=_resp(200))
    result = _run(cl.check_url(URL, fetch_fn=fetch, pw_fn=None))
    assert result["ok"] is True
    assert result["method"] == "HEAD"
    assert result["http_status"] == 200
    assert fetch.call_count == 1
    fetch.assert_called_with("HEAD", URL)


def test_head_301_ok():
    fetch = AsyncMock(return_value=_resp(301, "http://example.com/other"))
    result = _run(cl.check_url(URL, fetch_fn=fetch, pw_fn=None))
    assert result["ok"] is True
    assert result["method"] == "HEAD"


def test_head_404_broken_no_get():
    fetch = AsyncMock(return_value=_resp(404))
    pw = AsyncMock()
    result = _run(cl.check_url(URL, fetch_fn=fetch, pw_fn=pw))
    assert result["ok"] is False
    assert result["method"] == "HEAD"
    assert result["http_status"] == 404
    # GET and Playwright must not be called
    assert fetch.call_count == 1
    pw.assert_not_called()


def test_head_410_broken_no_get():
    fetch = AsyncMock(return_value=_resp(410))
    pw = AsyncMock()
    result = _run(cl.check_url(URL, fetch_fn=fetch, pw_fn=pw))
    assert result["ok"] is False
    assert result["http_status"] == 410
    assert fetch.call_count == 1
    pw.assert_not_called()


def test_head_429_no_fallthrough():
    fetch = AsyncMock(return_value=_resp(429))
    pw = AsyncMock()
    result = _run(cl.check_url(URL, fetch_fn=fetch, pw_fn=pw))
    assert result["ok"] is False
    assert result["http_status"] == 429
    # Playwright must not be called for a 429
    pw.assert_not_called()


def test_head_timeout_skips_get_tries_playwright():
    fetch = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    pw = AsyncMock(return_value={"ok": True, "status": 200, "final_url": URL})
    result = _run(cl.check_url(URL, fetch_fn=fetch, pw_fn=pw))
    # Playwright was tried, GET was not
    assert result["method"] == "PLAYWRIGHT"
    assert result["ok"] is True
    assert fetch.call_count == 1  # only HEAD, no GET
    pw.assert_called_once_with(URL)


def test_head_500_tries_get():
    fetch = AsyncMock(side_effect=[_resp(500), _resp(200)])
    result = _run(cl.check_url(URL, fetch_fn=fetch, pw_fn=None))
    assert result["ok"] is True
    assert result["method"] == "GET"
    assert fetch.call_count == 2


# ---------------------------------------------------------------------------
# check_url — GET branch
# ---------------------------------------------------------------------------


def test_get_200_ok():
    fetch = AsyncMock(side_effect=[_resp(500), _resp(200)])
    result = _run(cl.check_url(URL, fetch_fn=fetch, pw_fn=None))
    assert result["ok"] is True
    assert result["method"] == "GET"
    assert result["http_status"] == 200


def test_get_404_broken_no_playwright():
    fetch = AsyncMock(side_effect=[_resp(500), _resp(404)])
    pw = AsyncMock()
    result = _run(cl.check_url(URL, fetch_fn=fetch, pw_fn=pw))
    assert result["ok"] is False
    assert result["http_status"] == 404
    pw.assert_not_called()


def test_get_timeout_broken_no_playwright():
    fetch = AsyncMock(side_effect=[_resp(500), httpx.TimeoutException("timed out")])
    pw = AsyncMock()
    result = _run(cl.check_url(URL, fetch_fn=fetch, pw_fn=pw))
    assert result["ok"] is False
    assert "timeout" in result["error"]
    pw.assert_not_called()


def test_get_503_tries_playwright():
    fetch = AsyncMock(side_effect=[_resp(500), _resp(503)])
    pw = AsyncMock(return_value={"ok": True, "status": 200, "final_url": URL})
    result = _run(cl.check_url(URL, fetch_fn=fetch, pw_fn=pw))
    assert result["method"] == "PLAYWRIGHT"
    assert result["ok"] is True
    pw.assert_called_once_with(URL)


# ---------------------------------------------------------------------------
# check_url — Playwright branch
# ---------------------------------------------------------------------------


def test_playwright_ok():
    fetch = AsyncMock(side_effect=httpx.ConnectError("refused"))
    pw = AsyncMock(return_value={"ok": True, "status": 200, "final_url": URL})
    result = _run(cl.check_url(URL, fetch_fn=fetch, pw_fn=pw))
    assert result["method"] == "PLAYWRIGHT"
    assert result["ok"] is True


def test_playwright_download_ok():
    fetch = AsyncMock(side_effect=[_resp(500), _resp(503)])
    pw = AsyncMock(return_value={"ok": True, "status": None, "final_url": URL})
    result = _run(cl.check_url(URL, fetch_fn=fetch, pw_fn=pw))
    assert result["method"] == "PLAYWRIGHT"
    assert result["ok"] is True
    assert result["http_status"] is None


def test_playwright_error_broken():
    fetch = AsyncMock(side_effect=[_resp(500), _resp(503)])
    pw = AsyncMock(return_value={"ok": False, "status": None, "error": "playwright:net::ERR_NAME_NOT_RESOLVED"})
    result = _run(cl.check_url(URL, fetch_fn=fetch, pw_fn=pw))
    assert result["method"] == "PLAYWRIGHT"
    assert result["ok"] is False
    assert "playwright:" in result["error"]


# ---------------------------------------------------------------------------
# HostGate
# ---------------------------------------------------------------------------


def test_host_gate_accelerates():
    # start=1000ms, floor=250ms, step=250ms → levels: 1000, 750, 500, 250
    gate = cl.HostGate(start_ms=1000, floor_ms=250)
    host = "example.com"
    for _ in range(5):
        gate.record(host, status=200, error=None)
    assert gate._cur_interval(host) == pytest.approx(0.750)
    for _ in range(5):
        gate.record(host, status=200, error=None)
    assert gate._cur_interval(host) == pytest.approx(0.500)
    for _ in range(5):
        gate.record(host, status=200, error=None)
    assert gate._cur_interval(host) == pytest.approx(0.250)
    # Already at floor — more successes don't go lower
    for _ in range(5):
        gate.record(host, status=200, error=None)
    assert gate._cur_interval(host) == pytest.approx(0.250)


def test_host_gate_429_locks_and_steps_back():
    gate = cl.HostGate(start_ms=1000, floor_ms=250)
    host = "example.com"
    # Accelerate to floor
    for _ in range(15):
        gate.record(host, status=200, error=None)
    assert gate._cur_interval(host) == pytest.approx(0.250)
    assert gate._accelerating.get(host, True) is True

    # 429: lock acceleration, step up by 250ms
    gate.record(host, status=429, error=None)
    assert gate._accelerating[host] is False
    assert gate._cur_interval(host) == pytest.approx(0.500)

    # Successes after lock: no speed-up
    for _ in range(15):
        gate.record(host, status=200, error=None)
    assert gate._cur_interval(host) == pytest.approx(0.500)

    # Further 429s keep stepping back, capped at start
    gate.record(host, status=429, error=None)
    assert gate._cur_interval(host) == pytest.approx(0.750)
    gate.record(host, status=429, error=None)
    assert gate._cur_interval(host) == pytest.approx(1.000)
    gate.record(host, status=429, error=None)
    assert gate._cur_interval(host) == pytest.approx(1.000)


def test_host_gate_errors_dont_affect_rate():
    gate = cl.HostGate(start_ms=1000, floor_ms=250)
    host = "example.com"
    for _ in range(15):
        gate.record(host, status=200, error=None)
    assert gate._cur_interval(host) == pytest.approx(0.250)

    # Errors don't change rate or lock acceleration
    gate.record(host, status=None, error="timeout:15s")
    gate.record(host, status=503, error=None)
    gate.record(host, status=None, error="connect:refused")
    assert gate._cur_interval(host) == pytest.approx(0.250)
    assert gate._accelerating.get(host, True) is True


def test_host_gate_dead_host_on_timeouts():
    gate = cl.HostGate(start_ms=1000, floor_ms=250)
    host = "dead.example.com"
    for _ in range(9):
        assert gate.record(host, status=None, error="timeout:15s") is False
    assert not gate.is_dead(host)
    assert gate.record(host, status=None, error="timeout:15s") is True
    assert gate.is_dead(host)
    # Only fires once
    assert gate.record(host, status=None, error="timeout:15s") is False


def test_host_gate_dead_host_on_5xx():
    gate = cl.HostGate(start_ms=1000, floor_ms=250)
    host = "example.com"
    for _ in range(9):
        gate.record(host, status=503, error=None)
    assert not gate.is_dead(host)
    assert gate.record(host, status=503, error=None) is True
    assert gate.is_dead(host)


def test_host_gate_dead_host_on_connect_error():
    gate = cl.HostGate(start_ms=1000, floor_ms=250)
    host = "example.com"
    for _ in range(10):
        gate.record(host, status=None, error="connect:refused")
    assert gate.is_dead(host)


def test_host_gate_dead_host_mixed_signals():
    gate = cl.HostGate(start_ms=1000, floor_ms=250)
    host = "example.com"
    # Mix of dead signals: 3 timeouts + 4 5xx + 3 connect errors = 10
    for _ in range(3):
        gate.record(host, status=None, error="timeout:15s")
    for _ in range(4):
        gate.record(host, status=503, error=None)
    for _ in range(2):
        gate.record(host, status=None, error="connect:refused")
    assert not gate.is_dead(host)
    assert gate.record(host, status=None, error="dns:getaddrinfo failed") is True
    assert gate.is_dead(host)


# ---------------------------------------------------------------------------
# load_urls / write_result — mock Db
# ---------------------------------------------------------------------------


def _mock_db(rows: list[dict]):
    """Return a mock Db whose .prepare().all() returns rows."""
    query = MagicMock()
    query.all.return_value = rows
    query.run.return_value = None
    db = MagicMock()
    db.prepare.return_value = query
    return db


def test_load_urls_filters_non_http():
    db = _mock_db(
        [
            {"url": "http://example.com/a"},
            {"url": "ftp://example.com/b"},
            {"url": ""},
            {"url": "https://gov.uk/c"},
        ],
    )
    urls = cl.load_urls(db)
    assert urls == ["http://example.com/a", "https://gov.uk/c"]


def test_load_urls_respects_limit():
    rows = [{"url": f"http://example.com/{i}"} for i in range(10)]
    db = _mock_db(rows)
    urls = cl.load_urls(db, limit=3)
    assert len(urls) == 3


def test_load_urls_force_omits_checked_filter():
    db = _mock_db([{"url": "http://example.com/a"}])
    cl.load_urls(db, force=True)
    sql_arg = db.prepare.call_args[0][0]
    assert "NOT EXISTS" not in sql_arg
    assert "link_check_results" not in sql_arg


def test_load_urls_errors_only_filters_to_error_results():
    db = _mock_db([{"url": "http://example.com/a"}])
    cl.load_urls(db, errors_only=True)
    sql_arg = db.prepare.call_args[0][0]
    assert "lcr.ok = false" in sql_arg
    assert "JOIN link_check_results" in sql_arg


def test_load_urls_domain():
    db = _mock_db([{"url": "http://data.gov.uk/a"}])
    cl.load_urls(db, domain="data.gov.uk")
    sql_arg = db.prepare.call_args[0][0]
    assert "split_part" in sql_arg
    params = db.prepare.return_value.all.call_args[0]
    assert "data.gov.uk" in params
    assert "%.data.gov.uk" in params


def test_load_urls_publisher():
    db = _mock_db([{"url": "http://example.com/a"}])
    cl.load_urls(db, publisher="some-org")
    sql_arg = db.prepare.call_args[0][0]
    assert "l.org_slug = ?" in sql_arg
    params = db.prepare.return_value.all.call_args[0]
    assert params == ("some-org",)


def test_write_result_calls_upsert():
    db = _mock_db([])
    result = {
        "url": "http://example.com/a",
        "checked_at": "2026-09-20T12:00:00+00:00",
        "method": "HEAD",
        "ok": True,
        "http_status": 200,
        "final_url": "http://example.com/a",
        "error": None,
    }
    cl.write_result(db, result)
    db.prepare.return_value.run.assert_called_once()
    call_args = db.prepare.return_value.run.call_args[0]
    assert call_args[0] == "http://example.com/a"
    assert call_args[3] is True  # ok
    assert call_args[4] == 200  # http_status


# ---------------------------------------------------------------------------
# _strip_pw_error
# ---------------------------------------------------------------------------


def test_strip_pw_error_removes_ansi():
    msg = "\x1b[31mError\x1b[0m: something"
    assert cl._strip_pw_error(msg) == "Error: something"


def test_strip_pw_error_removes_call_log():
    msg = "Error: net::ERR_NAME_NOT_RESOLVED\n=== logs ===\nnavigating to ...\n============"
    result = cl._strip_pw_error(msg)
    assert "logs" not in result
    assert "Error: net::ERR_NAME_NOT_RESOLVED" in result


# ---------------------------------------------------------------------------
# checked_at is TIMESTAMPTZ — the skip/dead-host markers must pass a datetime
# (an isoformat() string raises DatatypeMismatch).
# ---------------------------------------------------------------------------


def _db_with_cursor() -> tuple[MagicMock, MagicMock]:
    """A Db whose cursor() context manager yields the same mock cursor."""
    cursor = MagicMock()
    cursor.rowcount = 0
    cursor.__enter__.return_value = cursor
    db = MagicMock()
    db.conn.cursor.return_value = cursor
    return db, cursor


def test_mark_malformed_urls_passes_datetime():
    db, cursor = _db_with_cursor()
    cl._mark_malformed_urls(db)
    params = cursor.execute.call_args[0][1]
    assert isinstance(params[0], datetime)


def test_mark_uncheckable_urls_passes_datetime():
    db, cursor = _db_with_cursor()
    cl._mark_uncheckable_urls(db)
    params = cursor.execute.call_args[0][1]
    assert isinstance(params[0], datetime)


def test_bulk_mark_dead_host_passes_datetime():
    db, cursor = _db_with_cursor()
    cl._bulk_mark_dead_host(db, "example.com")
    params = cursor.execute.call_args[0][1]
    assert isinstance(params[0], datetime)
    assert params[1] == "example.com"
