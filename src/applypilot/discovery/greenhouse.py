"""Greenhouse Boards API discovery: fetches jobs from companies hosted on Greenhouse.

Greenhouse exposes a free, unauthenticated JSON board API per company:

    GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true

When `content=true`, every posting is returned with its full HTML description,
location, departments, offices, and an `absolute_url` that doubles as the apply
URL -- so no enrichment call (and no LLM tokens) are required.

Companies are loaded from `config/greenhouse_companies.yaml`. Title and location
filters use the same `searches.yaml` keys as the other discovery scrapers, so a
single user config drives the whole pipeline.
"""

import html as html_module
import json
import logging
import sqlite3
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import yaml

from applypilot import config
from applypilot.config import CONFIG_DIR
from applypilot.database import get_connection, init_db
from applypilot.discovery.workday import strip_html

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
API_BASE = "https://boards-api.greenhouse.io/v1/boards"


# -- Company registry from YAML ---------------------------------------------

def load_companies() -> dict:
    """Load Greenhouse company registry from config/greenhouse_companies.yaml."""
    path = CONFIG_DIR / "greenhouse_companies.yaml"
    if not path.exists():
        log.warning("greenhouse_companies.yaml not found at %s", path)
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data.get("companies", {})


# -- Filtering helpers -------------------------------------------------------

def _load_location_filter(search_cfg: dict | None = None) -> tuple[list[str], list[str]]:
    """Load location accept/reject lists from search config."""
    if search_cfg is None:
        search_cfg = config.load_search_config()
    accept = search_cfg.get("location_accept", [])
    reject = search_cfg.get("location_reject_non_remote", [])
    return accept, reject


def _location_ok(location: str | None, accept: list[str], reject: list[str]) -> bool:
    """Check if a job location passes the user's location filter."""
    if not location:
        return True
    loc = location.lower()
    if any(r in loc for r in ("remote", "anywhere", "work from home", "wfh", "distributed")):
        return True
    for a in accept:
        if a.lower() in loc:
            return True
    # for r in reject:
    #     if r.lower() in loc:
    #         return False
    return False


def _load_query_terms(search_cfg: dict | None = None) -> list[str]:
    """Pull each `query` from `searches.yaml` and lowercase for substring matching.

    Returns the list of raw query strings (e.g. "software engineer", "backend
    developer"). Used for case-insensitive substring matching against job titles.
    """
    if search_cfg is None:
        search_cfg = config.load_search_config()
    queries = search_cfg.get("queries", [])
    terms = []
    for q in queries:
        if isinstance(q, dict) and q.get("query"):
            terms.append(str(q["query"]).lower().strip())
        elif isinstance(q, str):
            terms.append(q.lower().strip())
    return [t for t in terms if t]


def _load_excluded_titles(search_cfg: dict | None = None) -> list[str]:
    """Load the `exclude_titles` list from search config."""
    if search_cfg is None:
        search_cfg = config.load_search_config()
    excludes = search_cfg.get("exclude_titles", [])
    return [str(e).lower().strip() for e in excludes if e]


def _title_matches(title: str | None, terms: list[str], excludes: list[str]) -> bool:
    """Case-insensitive substring match: title must contain any query term and
    must not contain any excluded phrase.
    """
    if not title:
        return False
    title_lower = title.lower()

    for ex in excludes:
        if ex and ex in title_lower:
            return False

    if not terms:
        return True

    return any(term in title_lower for term in terms)


# -- HTTP fetch --------------------------------------------------------------

def _http_get_json(url: str, max_retries: int = 3, backoff: float = 2.0) -> dict:
    """GET a URL with retries on 429 / transient failures. Returns parsed JSON."""
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(url)
            req.add_header("Accept", "application/json")
            req.add_header("User-Agent", UA)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 404:
                raise
            if e.code == 429 and attempt < max_retries:
                wait = backoff * (attempt + 1) * 2
                log.warning("429 from %s, retry %d/%d in %.0fs", url, attempt + 1, max_retries, wait)
                time.sleep(wait)
                continue
            if attempt < max_retries:
                wait = backoff * (attempt + 1)
                log.warning("HTTP %s from %s, retry %d/%d in %.0fs",
                            e.code, url, attempt + 1, max_retries, wait)
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
            if attempt < max_retries:
                wait = backoff * (attempt + 1)
                log.warning("Transient error on %s: %s -- retry %d/%d in %.0fs",
                            url, e, attempt + 1, max_retries, wait)
                time.sleep(wait)
                continue
            raise
    if last_err:
        raise last_err
    return {}


def fetch_company_jobs(board_token: str) -> list[dict]:
    """Fetch all jobs for a Greenhouse board, with descriptions inlined.

    Returns the raw `jobs` list from the API (each item has keys like `id`,
    `title`, `absolute_url`, `location`, `content`, `updated_at`, `departments`,
    `offices`, `metadata`).
    """
    url = f"{API_BASE}/{board_token}/jobs?content=true"
    data = _http_get_json(url)
    return data.get("jobs", []) or []


# -- Description normalization ----------------------------------------------

def _normalize_description(content: str | None) -> str:
    """Greenhouse returns `content` as HTML-escaped HTML (entities like
    `&lt;p&gt;`). Unescape once so the HTML stripper can do its job, then
    convert tags to plain text.
    """
    if not content:
        return ""
    decoded = html_module.unescape(content)
    return strip_html(decoded)


# -- Per-company processing --------------------------------------------------

def _process_company(
    key: str,
    company: dict,
    terms: list[str],
    excludes: list[str],
    accept_locs: list[str],
    reject_locs: list[str],
    location_filter: bool,
) -> dict:
    """Fetch + filter + store jobs for one Greenhouse company."""
    name = company.get("name", key)
    token = company.get("board_token", key)
    result = {"company": name, "found": 0, "kept": 0, "new": 0, "existing": 0, "error": None}

    try:
        raw_jobs = fetch_company_jobs(token)
    except Exception as e:
        log.error("%s: API error: %s", name, e)
        result["error"] = str(e)
        return result

    result["found"] = len(raw_jobs)
    log.info("%s: %d total postings", name, len(raw_jobs))

    if not raw_jobs:
        return result

    now = datetime.now(timezone.utc).isoformat()
    rows: list[tuple] = []

    for job in raw_jobs:
        title = job.get("title") or ""
        loc_obj = job.get("location") or {}
        location = loc_obj.get("name") if isinstance(loc_obj, dict) else None

        if not _title_matches(title, terms, excludes):
            continue
        if location_filter and not _location_ok(location, accept_locs, reject_locs):
            continue

        url = job.get("absolute_url") or ""
        if not url:
            continue

        full_description = _normalize_description(job.get("content"))
        short_desc = full_description[:500] if full_description else None

        detail_scraped_at = now if full_description and len(full_description) > 200 else None
        full_for_db = full_description if detail_scraped_at else None

        rows.append((
            url,
            title or None,
            None,  # salary -- Greenhouse doesn't expose this on the boards API
            short_desc,
            location,
            name,
            "greenhouse_api",
            now,
            full_for_db,
            url,
            detail_scraped_at,
        ))

    result["kept"] = len(rows)

    conn = get_connection()
    new = 0
    existing = 0
    for row in rows:
        try:
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, "
                "strategy, discovered_at, full_description, application_url, "
                "detail_scraped_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1
    conn.commit()

    result["new"] = new
    result["existing"] = existing
    log.info("%s: %d kept after filter -> %d new, %d dupes", name, len(rows), new, existing)
    return result


# -- Public entry point ------------------------------------------------------

def run_greenhouse_discovery(
    companies: dict | None = None,
    workers: int = 1,
) -> dict:
    """Main entry point for Greenhouse-based discovery.

    Loads the company registry from `config/greenhouse_companies.yaml` (or uses
    the provided dict), then loads search queries + location filters from the
    user's `searches.yaml` and pulls every matching posting in a single API
    call per company.

    Args:
        companies: Override the company registry. If None, loads from YAML.
        workers: Number of parallel threads for company scraping. Default 1.

    Returns:
        Dict with stats: found, kept, new, existing, errors, companies.
    """
    if companies is None:
        companies = load_companies()

    if not companies:
        log.warning("No Greenhouse companies configured. Create config/greenhouse_companies.yaml.")
        return {"found": 0, "kept": 0, "new": 0, "existing": 0, "errors": 0, "companies": 0}

    init_db()

    search_cfg = config.load_search_config()
    terms = _load_query_terms(search_cfg)
    excludes = _load_excluded_titles(search_cfg)
    accept_locs, reject_locs = _load_location_filter(search_cfg)
    location_filter = search_cfg.get("greenhouse_location_filter", True)

    log.info("Greenhouse crawl: %d companies | %d query terms | %d excludes | workers=%d",
             len(companies), len(terms), len(excludes), workers)

    keys = list(companies.keys())
    grand: dict = {"found": 0, "kept": 0, "new": 0, "existing": 0, "errors": 0,
                   "companies": len(keys)}
    t0 = time.time()

    if workers > 1 and len(keys) > 1:
        completed = 0
        with ThreadPoolExecutor(max_workers=min(workers, len(keys))) as pool:
            futures = {
                pool.submit(
                    _process_company, key, companies[key],
                    terms, excludes, accept_locs, reject_locs, location_filter,
                ): key
                for key in keys
            }
            for fut in as_completed(futures):
                r = fut.result()
                completed += 1
                grand["found"] += r["found"]
                grand["kept"] += r["kept"]
                grand["new"] += r["new"]
                grand["existing"] += r["existing"]
                if r["error"]:
                    grand["errors"] += 1
                if completed % 5 == 0 or completed == len(keys):
                    elapsed = time.time() - t0
                    log.info("Greenhouse progress: %d/%d (%d new, %d dupes, %d errors) [%.0fs]",
                             completed, len(keys), grand["new"], grand["existing"],
                             grand["errors"], elapsed)
    else:
        for i, key in enumerate(keys, 1):
            r = _process_company(
                key, companies[key],
                terms, excludes, accept_locs, reject_locs, location_filter,
            )
            grand["found"] += r["found"]
            grand["kept"] += r["kept"]
            grand["new"] += r["new"]
            grand["existing"] += r["existing"]
            if r["error"]:
                grand["errors"] += 1
            if i % 5 == 0 or i == len(keys):
                elapsed = time.time() - t0
                log.info("Greenhouse progress: %d/%d (%d new, %d dupes, %d errors) [%.0fs]",
                         i, len(keys), grand["new"], grand["existing"],
                         grand["errors"], elapsed)

    elapsed = time.time() - t0
    log.info("Greenhouse crawl done in %.0fs: %d found, %d kept, %d new, %d dupes, %d errors",
             elapsed, grand["found"], grand["kept"], grand["new"], grand["existing"],
             grand["errors"])
    return grand
