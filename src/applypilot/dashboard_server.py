"""Local HTTP server for the interactive ApplyPilot dashboard."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlunparse

from applypilot.database import get_connection
from applypilot.view import generate_dashboard

log = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 8192
MAX_URL_LENGTH = 2048


def normalize_job_url(raw_url: str) -> str:
    """Validate and normalize an externally supplied job URL."""
    if not isinstance(raw_url, str):
        raise ValueError("URL must be a string")
    raw_url = raw_url.strip()
    if not raw_url:
        raise ValueError("Enter a job URL")
    if len(raw_url) > MAX_URL_LENGTH:
        raise ValueError("URL is too long")

    parsed = urlparse(raw_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("URL must start with http:// or https://")
    if not parsed.hostname:
        raise ValueError("URL must include a valid hostname")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing credentials are not allowed")

    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path or "/",
            parsed.params,
            parsed.query,
            "",
        )
    )


def import_external_job(raw_url: str, conn: sqlite3.Connection | None = None) -> dict:
    """Insert an external job URL and return its import state."""
    url = normalize_job_url(raw_url)
    conn = conn or get_connection()
    existing = conn.execute(
        "SELECT url, title, detail_scraped_at, detail_error FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    if existing:
        return {
            "created": False,
            "url": url,
            "title": existing["title"],
            "status": job_import_status(url, conn)["status"],
        }

    hostname = urlparse(url).hostname or "external"
    title = f"Imported job from {hostname}"
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO jobs (url, title, site, strategy, discovered_at, application_url) "
        "VALUES (?, ?, ?, 'external_upload', ?, ?)",
        (url, title, hostname, now, url),
    )
    conn.commit()
    return {"created": True, "url": url, "title": title, "status": "pending"}


def job_import_status(raw_url: str, conn: sqlite3.Connection | None = None) -> dict:
    """Return the current enrichment status for an imported URL."""
    url = normalize_job_url(raw_url)
    conn = conn or get_connection()
    row = conn.execute(
        "SELECT url, title, site, full_description, detail_scraped_at, detail_error "
        "FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    if not row:
        return {"url": url, "status": "missing"}
    if row["detail_error"]:
        status = "error"
    elif row["detail_scraped_at"] and row["full_description"]:
        status = "complete"
    elif row["detail_scraped_at"]:
        status = "partial"
    else:
        status = "pending"
    return {
        "url": url,
        "status": status,
        "title": row["title"],
        "site": row["site"],
        "error": row["detail_error"],
    }


def mark_job_applied(raw_url: str, conn: sqlite3.Connection | None = None) -> dict:
    """Mark an existing dashboard job as manually applied."""
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ValueError("Job URL is required")
    url = raw_url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise ValueError("URL is too long")

    conn = conn or get_connection()
    row = conn.execute(
        "SELECT title, applied_at FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    if not row:
        return {"updated": False, "url": url, "status": "missing"}

    applied_at = row["applied_at"] or datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE jobs SET applied_at = ?, "
        "apply_status = COALESCE(apply_status, 'manually_applied') WHERE url = ?",
        (applied_at, url),
    )
    conn.commit()
    return {
        "updated": True,
        "url": url,
        "title": row["title"],
        "status": "applied",
        "applied_at": applied_at,
    }


def enrich_external_job(url: str) -> None:
    """Enrich one imported URL in an isolated background worker."""
    from applypilot.enrichment.detail import scrape_site_batch

    conn = get_connection()
    row = conn.execute("SELECT title, site FROM jobs WHERE url = ?", (url,)).fetchone()
    if not row:
        return

    try:
        scrape_site_batch(conn, row["site"] or "external", [(url, row["title"])], delay=0)
    except Exception as exc:
        log.exception("External job enrichment failed for %s", url)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE jobs SET detail_error = ?, detail_scraped_at = ? WHERE url = ?",
            (str(exc)[:500], now, url),
        )
        conn.commit()


def _execute_discovery(server: DashboardHTTPServer, workers: int) -> None:
    """Run discovery and publish its result to the dashboard server."""
    from applypilot.pipeline import _run_discover

    try:
        result = _run_discover(workers=workers)
    except Exception as exc:
        log.exception("Dashboard discovery failed")
        with server.discovery_lock:
            server.discovery_state = {
                **server.discovery_state,
                "status": "error",
                "error": str(exc)[:500],
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
        return

    with server.discovery_lock:
        server.discovery_state = {
            **server.discovery_state,
            "status": "complete",
            "result": result,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }


def start_discovery(server: DashboardHTTPServer, workers: int = 4) -> dict:
    """Start one background discovery run, rejecting overlapping runs."""
    if not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= 8:
        raise ValueError("Workers must be an integer between 1 and 8")

    with server.discovery_lock:
        if server.discovery_state["status"] == "running":
            return dict(server.discovery_state)
        server.discovery_state = {
            "status": "running",
            "workers": workers,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "result": None,
            "error": None,
        }
        state = dict(server.discovery_state)

    server.discovery_pool.submit(_execute_discovery, server, workers)
    return state


class DashboardHTTPServer(ThreadingHTTPServer):
    """Threaded localhost server with a bounded enrichment pool."""

    daemon_threads = True

    def __init__(self, server_address, handler_class):
        super().__init__(server_address, handler_class)
        self.enrichment_pool = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="applypilot-enrich",
        )
        self.discovery_pool = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="applypilot-discovery",
        )
        self.discovery_lock = threading.Lock()
        self.discovery_state = {
            "status": "idle",
            "workers": None,
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
        }
        self.render_lock = threading.Lock()

    def server_close(self) -> None:
        self.enrichment_pool.shutdown(wait=False, cancel_futures=True)
        self.discovery_pool.shutdown(wait=False, cancel_futures=True)
        super().server_close()


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """Serve the dashboard and its external-job API."""

    server: DashboardHTTPServer

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            with self.server.render_lock:
                output = Path(generate_dashboard())
                body = output.read_bytes()
            self._send_bytes(200, body, "text/html; charset=utf-8")
            return

        if parsed.path == "/api/jobs/status":
            raw_url = parse_qs(parsed.query).get("url", [""])[0]
            try:
                self._send_json(200, job_import_status(raw_url))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            return

        if parsed.path == "/api/discovery/status":
            with self.server.discovery_lock:
                state = dict(self.server.discovery_state)
            self._send_json(200, state)
            return

        self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in {"/api/jobs", "/api/jobs/applied", "/api/discovery"}:
            self._send_json(404, {"error": "Not found"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
                raise ValueError("Invalid request size")
            if "application/json" not in self.headers.get("Content-Type", ""):
                raise ValueError("Content-Type must be application/json")
            payload = json.loads(self.rfile.read(content_length))
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object")
            if path == "/api/discovery":
                result = start_discovery(self.server, payload.get("workers", 4))
            elif path == "/api/jobs/applied":
                result = mark_job_applied(payload.get("url", ""))
            else:
                result = import_external_job(payload.get("url", ""))
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except sqlite3.Error as exc:
            log.exception("Could not import external job")
            self._send_json(500, {"error": f"Database error: {exc}"})
            return

        if path == "/api/discovery":
            self._send_json(202, result)
            return

        if path == "/api/jobs/applied":
            if result["updated"]:
                self._send_json(200, result)
            else:
                self._send_json(404, {"error": "Job not found"})
            return

        if result["created"]:
            self.server.enrichment_pool.submit(enrich_external_job, result["url"])
            self._send_json(201, result)
        else:
            result["message"] = "This job is already in the dashboard"
            self._send_json(200, result)

    def log_message(self, format: str, *args) -> None:
        log.debug("Dashboard: " + format, *args)


def serve_dashboard(
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    """Run the interactive dashboard until interrupted."""
    server = DashboardHTTPServer((host, port), DashboardRequestHandler)
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}/"
    print(f"ApplyPilot dashboard: {url}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        server.server_close()
