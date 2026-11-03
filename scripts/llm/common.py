"""Shared infrastructure for the LLM review and suggest scripts.

Digest building, API client, file I/O, worker pool, and helpers used by
both scripts/review.py and scripts/suggest.py.
"""

import json
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path

import httpx
import typer

from scripts.ckan import sleep
from scripts.db import connect, database_url

DATABASE_URL = database_url()
REQUEST_TIMEOUT = 120  # seconds
RETRIES = 2  # attempts are 0..RETRIES (up to 3 tries)
REMOTE_CONCURRENCY = 50
MAX_TOKENS = 2048
TEMPERATURE = 0.2


class LLMError(RuntimeError):
    """LLM/HTTP error carrying an optional HTTP status (for the 429 retry).

    status is set only for HTTP errors; the retry loop checks status ==
    HTTPStatus.TOO_MANY_REQUESTS to decide the backoff.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Timestamp + string helpers
# ---------------------------------------------------------------------------
def iso_now() -> str:
    """Current UTC time, ISO 8601 with milliseconds — e.g. 2026-08-03T15:04:29.901Z."""

    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def truncate(s, n: int):
    """null -> null; string-coerce then slice to n chars + '…' when longer.

    A bool coerces to 'true'/'false' (not 'True'/'False') so the digest
    output stays consistent.
    """

    if s is None:
        return None
    s = ("true" if s else "false") if isinstance(s, bool) else str(s)
    return s[:n] + "…" if len(s) > n else s


def strip_html(s) -> str:
    """Coerce to str, strip tags, decode &amp;/&nbsp;, collapse whitespace."""

    s = str(s if s is not None else "")
    s = re.sub(r"<[^>]*>", " ", s)
    s = s.replace("&amp;", "&")
    s = s.replace("&nbsp;", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


# ---------------------------------------------------------------------------
# API client (shared between remote and local llama.cpp)
# ---------------------------------------------------------------------------
def check_server(client: httpx.Client, base_url: str) -> None:
    """Local-mode health check: {base}/health must return {"status": "ok"}."""

    res = client.get(f"{base_url}/health", timeout=10)
    if not res.is_success:
        raise LLMError(f"health check failed with HTTP {res.status_code}")
    body = res.json()
    if body.get("status") != "ok":
        raise LLMError(f'server reports status "{body.get("status")}"')


def _is_anthropic(base_url: str, model: str = "") -> bool:
    return "api.anthropic.com" in base_url or bool(
        re.match(r"(eu|us|ap)\.anthropic\.", model),
    )


def send_request(
    client: httpx.Client,
    base_url: str,
    api_key: str,
    model: str,
    digest: dict,
    build_prompt: Callable[[dict], list[dict]],
) -> str:
    """One chat completion call. Returns the trimmed reply content.

    Routes to the Anthropic Messages API when base_url contains
    api.anthropic.com; otherwise uses the OpenAI-compatible
    /v1/chat/completions endpoint (remote or local llama).
    """

    if _is_anthropic(base_url, model):
        return _send_anthropic(client, base_url, api_key, model, digest, build_prompt)
    return _send_openai_compat(client, base_url, api_key, model, digest, build_prompt)


def _send_openai_compat(
    client: httpx.Client,
    base_url: str,
    api_key: str,
    model: str,
    digest: dict,
    build_prompt: Callable[[dict], list[dict]],
) -> str:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    body: dict = {"model": model, "messages": build_prompt(digest)}
    if api_key:
        body["thinking"] = {"type": "disabled"}
    body["max_tokens"] = MAX_TOKENS
    if not api_key:
        # Remote models (Haiku 5.5) only accept the default temperature.
        body["temperature"] = TEMPERATURE

    res = client.post(
        f"{base_url}/v1/chat/completions",
        headers=headers,
        content=json.dumps(body, ensure_ascii=False, separators=(",", ":")),
    )
    if not res.is_success:
        raise LLMError(
            f"HTTP {res.status_code}: {truncate(res.text, 200)}",
            status=res.status_code,
        )
    data = res.json()
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        message = None
    content = (message or {}).get("content")
    if not isinstance(content, str) or not content.strip():
        raise LLMError("empty reply content (max_tokens may be too low)")
    return content.strip()


def _send_anthropic(
    client: httpx.Client,
    base_url: str,
    api_key: str,
    model: str,
    digest: dict,
    build_prompt: Callable[[dict], list[dict]],
) -> str:
    messages = build_prompt(digest)
    system_text = next((m["content"] for m in messages if m["role"] == "system"), None)
    user_messages = [m for m in messages if m["role"] != "system"]

    is_oauth = not api_key.startswith("sk-ant-api")
    headers: dict = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    if is_oauth:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["anthropic-beta"] = "oauth-2025-04-20"
    else:
        headers["x-api-key"] = api_key

    body: dict = {"model": model, "max_tokens": MAX_TOKENS, "messages": user_messages}
    if system_text:
        body["system"] = system_text

    res = client.post(
        f"{base_url}/v1/messages",
        headers=headers,
        content=json.dumps(body, ensure_ascii=False, separators=(",", ":")),
    )
    if not res.is_success:
        raise LLMError(
            f"HTTP {res.status_code}: {truncate(res.text, 200)}",
            status=res.status_code,
        )
    data = res.json()
    try:
        content = next(b["text"] for b in data["content"] if b.get("type") == "text")
    except (KeyError, IndexError, StopIteration, TypeError):
        content = None
    if not isinstance(content, str) or not content.strip():
        raise LLMError("empty reply content (max_tokens may be too low)")
    return content.strip()


def extract_json(text: str) -> dict:
    """Strip ```json fences, slice first { to last }, parse as JSON."""

    stripped = re.sub(r"```(?:json)?", "", text, flags=re.IGNORECASE).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object found in reply")
    return json.loads(stripped[start : end + 1])


# ---------------------------------------------------------------------------
# Store: one JSON file per dataset in <out_dir>/<org>/
# ---------------------------------------------------------------------------
def slugify(title: str) -> str:
    """Lowercase, [^a-z0-9]+ -> '-', trim dashes, [:80]."""
    s = title.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"^-+|-+$", "", s)
    return s[:80]


def load_processed_ids(out_dir: Path) -> tuple[set, set]:
    """(ok, attempted) dataset-id sets from per-dataset JSON files under
    out_dir. Corrupt files are skipped."""

    ok: set[str] = set()
    attempted: set[str] = set()
    if not out_dir.exists():
        return ok, attempted
    for f in out_dir.rglob("*.json"):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        attempted.add(rec.get("dataset_id"))
        if rec.get("ok"):
            ok.add(rec.get("dataset_id"))
    return ok, attempted


def record_path(out_dir: Path, record: dict) -> Path:
    """Canonical file path for a record: out_dir/<org_slug>/<slug>-<id[:8]>.json.

    Shared by write_record and scripts/llm/export_records.py so a DB export
    lands exactly where the review/suggest pipeline looks for processed ids.
    """
    org = record.get("org_slug") or "_unknown"
    title = record.get("title") or record["dataset_id"]
    return out_dir / org / f"{slugify(title)}-{record['dataset_id'][:8]}.json"


def write_record(out_dir: Path, record: dict) -> None:
    """Write one JSON file per dataset, overwriting any existing file."""
    path = record_path(out_dir, record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(record, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Worker infrastructure
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LLMConfig:
    """The call-wide values every worker needs — client, endpoint,
    auth, model and output directory. Built once in run() and passed down."""

    client: httpx.Client
    base_url: str
    api_key: str
    model: str
    out_dir: Path
    show_prompt: bool = False


def _summary_guard(summary_lock: threading.Lock | None):
    """The summary lock when running in a thread pool; a no-op context for
    direct (single-threaded) calls."""

    return summary_lock if summary_lock is not None else nullcontext()


def record_base(row, model: str) -> dict:
    """The fixed record fields shared by ok and error records."""
    return {
        "dataset_id": row["ckan_id"],
        "title": row["title"],
        "org_slug": row["org_slug"],
        "org_display_name": row["org_display_name"],
        "model": model,
    }


def fetch_record(
    config: LLMConfig,
    base: dict,
    digest: dict,
    build_prompt: Callable[[dict], list[dict]],
    validate: Callable[[dict], None],
) -> dict:
    """One dataset's record: send -> parse -> validate -> ok:true.

    The retry loop allows up to RETRIES+1 attempts; any error retries, but
    only HTTP 429 backs off (sleep 2000*(attempt+1)ms). The returned record
    is ok:true with the parsed model output, or ok:false with the last
    error message.

    validate(parsed) should raise LLMError if the parsed response is invalid.
    """
    record: dict = {**base, "ok": False, "error": "unknown"}
    for attempt in range(RETRIES + 1):
        try:
            content = send_request(config.client, config.base_url, config.api_key, config.model, digest, build_prompt)
            parsed = extract_json(content)
            validate(parsed)
            record = {**base, "ok": True, **parsed}
            break
        except (httpx.HTTPError, LLMError, ValueError) as err:
            record["error"] = str(err)
            if getattr(err, "status", None) == HTTPStatus.TOO_MANY_REQUESTS and attempt < RETRIES:
                sleep(2000 * (attempt + 1))
    return record


def record_summary(record: dict, summary: dict, summary_lock: threading.Lock | None) -> None:
    """Accumulate ok/failed counts into the shared summary dict
    (lock-guarded when running in a thread pool)."""
    with _summary_guard(summary_lock):
        if record["ok"]:
            summary["ok"] += 1
        else:
            summary["failed"] += 1


def run_workers(
    config: LLMConfig,
    rows: list,
    concurrency: int,
    *,
    show_progress: bool,
    summary: dict,
    process_one: Callable,
) -> None:
    """Run workers: min(concurrency, rows) threads share an index + summary.

    process_one(config, row, i, total, show_progress, summary, summary_lock)
    is the per-row function provided by each script.
    """

    next_i = 0
    index_lock = threading.Lock()
    summary_lock = threading.Lock()
    workers = min(concurrency, len(rows))

    def worker() -> None:
        nonlocal next_i
        while True:
            with index_lock:
                if next_i >= len(rows):
                    return
                i = next_i
                next_i += 1
            process_one(
                config,
                rows[i],
                i,
                len(rows),
                show_progress=show_progress,
                summary=summary,
                summary_lock=summary_lock,
            )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker) for _ in range(workers)]
        for f in futures:
            f.result()


# ---------------------------------------------------------------------------
# Shared run() + CLI helpers
# ---------------------------------------------------------------------------
def run(
    *,
    limit: int | None,
    org: str | None,
    dataset: list[str] | None,
    model: str,
    base_url: str,
    api_key: str,
    concurrency: int,
    out_dir: Path,
    include_reviewed: bool,
    show_progress: bool,
    show_prompt: bool = False,
    process_one: Callable,
) -> None:
    """Fetch + process + write per-dataset records for the selected rows."""

    is_remote = bool(api_key)
    org_filter = org or None
    id_filter = dataset or []

    with httpx.Client(follow_redirects=True, timeout=REQUEST_TIMEOUT) as client:
        if not is_remote:
            try:
                check_server(client, base_url)
            except (httpx.HTTPError, LLMError, ValueError) as err:
                print(
                    f"Cannot reach the model server at {base_url}: {err}",
                    file=sys.stderr,
                )
                print(
                    "Start the server configured by LOCAL_BASE_URL (e.g. llama-server) "
                    "or pass an API key for remote mode.",
                    file=sys.stderr,
                )
                raise typer.Exit(1) from None

        processed, attempted = load_processed_ids(out_dir)
        if len(processed) > 0 and not include_reviewed:
            print(
                f"Skipping {len(processed)} already-processed dataset(s) (--include-reviewed to force)",
            )

        db = connect(DATABASE_URL)
        try:
            select_sql = """SELECT d.ckan_id, d.title, d.org_slug, d.org_display_name, j.json
             FROM datasets d
             JOIN dataset_json j ON j.dataset_id = d.id
             WHERE (?::text IS NULL OR d.org_slug = ?)
               AND d.resource_count > 0"""

            if id_filter:
                placeholders = ",".join("?" for _ in id_filter)
                rows = db.prepare(
                    select_sql + f"\n AND d.ckan_id IN ({placeholders})",
                ).all(org_filter, org_filter, *id_filter)
            elif include_reviewed:
                ids = list(attempted)
                pick = limit if limit is not None else len(ids)
                rows = []
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    rows = db.prepare(
                        select_sql + f"\n AND d.ckan_id IN ({placeholders})\n LIMIT ?",
                    ).all(org_filter, org_filter, *ids, pick)
            elif limit is not None:
                pick = limit
                candidates = db.prepare(
                    select_sql + "\n ORDER BY RANDOM()\n LIMIT ?",
                ).all(org_filter, org_filter, pick * 4)
                rows = [r for r in candidates if r["ckan_id"] not in processed][:pick]
            else:
                candidates = db.prepare(
                    select_sql + "\n ORDER BY d.org_slug, d.title",
                ).all(org_filter, org_filter)
                rows = [r for r in candidates if r["ckan_id"] not in processed]
        finally:
            db.close()

        if not rows:
            print("No datasets to process.")
            return
        print(
            f"Processing {len(rows)} dataset(s) with {model} via {base_url} (concurrency {concurrency})",
        )

        summary = {"ok": 0, "failed": 0}
        t0 = time.monotonic()
        run_workers(
            LLMConfig(
                client=client,
                base_url=base_url,
                api_key=api_key,
                model=model,
                out_dir=out_dir,
                show_prompt=show_prompt,
            ),
            rows,
            concurrency,
            show_progress=show_progress,
            summary=summary,
            process_one=process_one,
        )

        elapsed = time.monotonic() - t0
        print(
            f"Done in {elapsed:.1f}s — {summary['ok']} processed, {summary['failed']} failed. "
            f"Results written to {out_dir}",
        )


def cli_resolve_config(
    *,
    api_key: str | None,
    base_url: str | None,
    model: str | None,
    concurrency: int | None,
) -> tuple[str, str, str, int]:
    """Resolve API key, base URL, model and concurrency from CLI args + env.

    Returns (key, base, model, concurrency). Exits with code 1 on error.
    """
    key = (
        api_key
        or os.environ.get("LLM")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or os.environ.get("ANTHROPIC_API_KEY")
        or ""
    ).strip()
    is_remote = bool(key)
    base = base_url or (os.environ.get("LLM_BASE_URL") if is_remote else os.environ.get("LOCAL_BASE_URL"))
    mdl = model or (os.environ.get("LLM_MODEL") if is_remote else os.environ.get("LOCAL_MODEL"))
    if not base or not mdl:
        vars_msg = "LLM_MODEL and LLM_BASE_URL" if is_remote else "LOCAL_MODEL and LOCAL_BASE_URL"
        print(
            f"Need a model and API base URL — set {vars_msg} in .env (or pass --model / --base-url).",
            file=sys.stderr,
        )
        raise typer.Exit(1)

    if is_remote:
        conc = concurrency if concurrency is not None else REMOTE_CONCURRENCY
        if conc < 1:
            print("--concurrency must be >= 1", file=sys.stderr)
            raise typer.Exit(1)
    else:
        conc = 1

    return key, base, mdl, conc
