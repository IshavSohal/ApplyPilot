"""Local HTTP server for the interactive ApplyPilot dashboard."""

from __future__ import annotations

import copy
import json
import logging
import math
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlunparse

import yaml

from applypilot import config
from applypilot.database import get_connection
from applypilot.view import generate_dashboard

log = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 8192
MAX_SETTINGS_BYTES = 262144
MAX_RESUME_BYTES = 1_000_000
# JSON may expand control-heavy text to six bytes per source byte.
MAX_RESUME_REQUEST_BYTES = 7_000_000
MAX_URL_LENGTH = 2048
_settings_write_lock = threading.Lock()
_DELETE_SETTING = {"__applypilot_delete__": True}


def _is_nonnegative_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return value >= 0 and math.isfinite(value)
    except OverflowError:
        return False


def _deep_merge(existing: dict, incoming: dict) -> dict:
    """Merge editable settings while retaining keys unknown to the UI."""
    merged = copy.deepcopy(existing)
    for key, value in incoming.items():
        if value == _DELETE_SETTING:
            merged.pop(key, None)
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _atomic_write(path: Path, content: str) -> None:
    """Atomically replace a UTF-8 settings file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    """Atomically replace a binary user-data file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_bytes(content)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _tectonic_executable() -> str | None:
    """Locate a usable Tectonic binary, including ~/.local/bin."""
    found = shutil.which("tectonic")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "tectonic"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    return None


def _prepare_tex_for_tectonic(content: str) -> str:
    """Disable pdfTeX-only glyph mapping that breaks Tectonic's XeTeX engine."""
    prepared: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(r"\input{glyphtounicode}") or stripped.startswith(
            r"\pdfgentounicode"
        ):
            prepared.append(f"% {line}  % disabled for Tectonic/XeTeX")
        else:
            prepared.append(line)
    if content.endswith("\n"):
        prepared.append("")
    return "\n".join(prepared)


def _compile_latex_resume(content: str) -> bytes:
    """Compile LaTeX with Tectonic and return the generated PDF."""
    executable = _tectonic_executable()
    if not executable:
        raise ValueError(
            "Tectonic is not installed. Install it from "
            "https://github.com/tectonic-typesetting/tectonic/releases "
            "or run: snap install tectonic"
        )

    with tempfile.TemporaryDirectory(prefix="applypilot-tex-") as directory:
        work_dir = Path(directory)
        source = work_dir / "resume.tex"
        source.write_text(_prepare_tex_for_tectonic(content), encoding="utf-8")
        command = [
            executable,
            "--outdir",
            str(work_dir),
            str(source),
        ]
        try:
            result = subprocess.run(
                command,
                cwd=work_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("LaTeX compilation timed out after 5 minutes") from exc

        pdf_path = work_dir / "resume.pdf"
        if result.returncode != 0 or not pdf_path.exists():
            lines = []
            for part in (result.stdout, result.stderr):
                if not part:
                    continue
                for line in part.splitlines():
                    stripped = line.strip()
                    if not stripped or stripped.startswith("note: downloading"):
                        continue
                    lines.append(stripped)
            detail = "\n".join(lines)[-4000:] or "Tectonic did not produce a PDF"
            raise ValueError(f"LaTeX compilation failed:\n{detail}")

        pdf = pdf_path.read_bytes()
        if not pdf.startswith(b"%PDF-"):
            raise ValueError("Tectonic produced an invalid PDF")
        return pdf


def load_dashboard_settings() -> dict:
    """Load settings for the browser without exposing the stored password."""
    profile = copy.deepcopy(config.load_profile())
    searches = copy.deepcopy(config.load_search_config())
    if not isinstance(profile, dict) or not isinstance(searches, dict):
        raise ValueError("Profile and search settings must contain objects")

    personal = profile.get("personal")
    password_configured = False
    if isinstance(personal, dict):
        password_configured = bool(personal.pop("password", ""))

    return {
        "profile": profile,
        "searches": searches,
        "password_configured": password_configured,
    }


def load_dashboard_resume(resume_format: str = "txt") -> dict:
    """Return the requested source resume for the Profile view."""
    paths = {
        "txt": config.RESUME_PATH,
        "tex": config.RESUME_TEX_PATH,
    }
    if resume_format not in paths:
        raise ValueError("Resume format must be txt or tex")
    path = paths[resume_format]
    if not path.exists():
        result = {
            "exists": False,
            "filename": path.name,
            "format": resume_format,
            "content": "",
        }
        if resume_format == "tex":
            result["pdf_available"] = False
        return result
    content = path.read_text(encoding="utf-8")
    return {
        "exists": True,
        "filename": path.name,
        "format": resume_format,
        "content": content,
        **(
            {"pdf_available": config.RESUME_PDF_PATH.exists()}
            if resume_format == "tex"
            else {}
        ),
    }


def save_dashboard_resume(filename: object, content: object) -> dict:
    """Validate and atomically save an uploaded text or LaTeX resume.

    LaTeX uploads are compiled with Tectonic first. On compile failure, neither
    resume.tex nor resume.pdf is replaced, so the previous PDF remains.
    """
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError("Resume filename is required")
    clean_name = filename.strip()
    suffix = Path(clean_name).suffix.lower()
    if Path(clean_name).name != clean_name or suffix not in {".txt", ".tex"}:
        raise ValueError("Resume must be a .txt or .tex file")
    if not isinstance(content, str):
        raise ValueError("Resume content must be text")
    if "\x00" in content:
        raise ValueError("Resume contains invalid null characters")
    if not content.strip():
        raise ValueError("Resume cannot be empty")
    if len(content.encode("utf-8")) > MAX_RESUME_BYTES:
        raise ValueError("Resume must be 1 MB or smaller")

    pdf = _compile_latex_resume(content) if suffix == ".tex" else None
    with _settings_write_lock:
        target = config.RESUME_PATH if suffix == ".txt" else config.RESUME_TEX_PATH
        _atomic_write(target, content)
        if pdf is not None:
            _atomic_write_bytes(config.RESUME_PDF_PATH, pdf)
    return load_dashboard_resume(suffix.removeprefix("."))


def _validate_profile(profile: object) -> dict:
    if not isinstance(profile, dict):
        raise ValueError("Profile must be a JSON object")
    for section in (
        "personal",
        "work_authorization",
        "compensation",
        "experience",
        "skills_boundary",
        "resume_facts",
        "eeo_voluntary",
        "availability",
    ):
        if section in profile and not isinstance(profile[section], dict):
            raise ValueError(f"Profile section '{section}' must be an object")

    for section, keys in {
        "skills_boundary": ("programming_languages", "frameworks", "tools"),
        "resume_facts": (
            "preserved_companies",
            "preserved_projects",
            "real_metrics",
        ),
    }.items():
        values = profile.get(section, {})
        for key in keys:
            if key in values and not isinstance(values[key], list):
                raise ValueError(f"Profile field '{section}.{key}' must be a list")
            if key in values and not all(
                isinstance(item, str) for item in values[key]
            ):
                raise ValueError(
                    f"Profile field '{section}.{key}' must contain only text"
                )

    for section, key in (
        ("compensation", "salary_expectation"),
        ("compensation", "salary_range_min"),
        ("compensation", "salary_range_max"),
        ("experience", "years_of_experience_total"),
    ):
        values = profile.get(section, {})
        if key not in values or values[key] == "":
            continue
        value = values[key]
        if isinstance(value, str):
            try:
                number = float(value)
            except ValueError as exc:
                raise ValueError(
                    f"Profile field '{section}.{key}' must be a number"
                ) from exc
            if not math.isfinite(number) or number < 0:
                raise ValueError(
                    f"Profile field '{section}.{key}' must be a non-negative number"
                )
            values[key] = int(number) if number.is_integer() else number
        elif not _is_nonnegative_finite_number(value):
            raise ValueError(
                f"Profile field '{section}.{key}' must be a non-negative number"
            )
    return profile


def _validate_searches(searches: object) -> dict:
    if not isinstance(searches, dict):
        raise ValueError("Search preferences must be a JSON object")

    queries = searches.get("queries", [])
    if not isinstance(queries, list):
        raise ValueError("Search queries must be a list")
    for query in queries:
        if (
            not isinstance(query, dict)
            or not isinstance(query.get("query"), str)
            or not query["query"].strip()
        ):
            raise ValueError("Each search query must include a title")
        tier = query.get("tier", 1)
        if isinstance(tier, str) and tier.isdigit():
            tier = int(tier)
            query["tier"] = tier
        if isinstance(tier, bool) or not isinstance(tier, int) or tier not in (1, 2, 3):
            raise ValueError("Search query tiers must be 1, 2, or 3")

    locations = searches.get("locations", [])
    if not isinstance(locations, list):
        raise ValueError("Search locations must be a list")
    for location in locations:
        if (
            not isinstance(location, dict)
            or not isinstance(location.get("location"), str)
            or not location["location"].strip()
        ):
            raise ValueError("Each search location must include a location")
        if isinstance(location.get("remote"), str):
            remote = location["remote"].strip().lower()
            if remote in {"true", "false"}:
                location["remote"] = remote == "true"
        if "remote" in location and not isinstance(location["remote"], bool):
            raise ValueError("Location remote values must be true or false")

    defaults = searches.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ValueError("Search defaults must be an object")
    for key in ("distance", "hours_old", "results_per_site"):
        if key not in defaults:
            continue
        value = defaults.get(key)
        if value == _DELETE_SETTING:
            continue
        if isinstance(value, str) and value.strip():
            try:
                number = float(value)
            except ValueError as exc:
                raise ValueError(
                    f"Search default '{key}' must be a non-negative number"
                ) from exc
            if not math.isfinite(number) or number < 0:
                raise ValueError(
                    f"Search default '{key}' must be a non-negative number"
                )
            value = int(number) if number.is_integer() else number
            defaults[key] = value
        if not _is_nonnegative_finite_number(value):
            raise ValueError(f"Search default '{key}' must be a non-negative number")

    for key in (
        "accept_remote_anywhere",
        "accept_unknown_locations",
        "greenhouse_location_filter",
        "bigtech_location_filter",
    ):
        if key in searches and not isinstance(searches[key], bool):
            raise ValueError(f"Search preference '{key}' must be true or false")

    for key in (
        "allowed_countries",
        "location_accept",
        "location_reject_non_remote",
        "exclude_titles",
        "priority_titles",
        "boards",
    ):
        if key in searches and not isinstance(searches[key], list):
            raise ValueError(f"Search preference '{key}' must be a list")
        if key in searches and not all(
            isinstance(item, str) for item in searches[key]
        ):
            raise ValueError(
                f"Search preference '{key}' must contain only text"
            )
    return searches


def save_dashboard_profile(profile: object) -> dict:
    """Validate and atomically save profile settings."""
    incoming = _validate_profile(profile)
    with _settings_write_lock:
        existing = config.load_profile()
        merged = _deep_merge(existing, incoming)
        incoming_personal = incoming.get("personal", {})
        existing_personal = existing.get("personal", {})
        if isinstance(incoming_personal, dict):
            password = incoming_personal.get("password")
            merged_personal = merged.setdefault("personal", {})
            if password:
                if not isinstance(password, str):
                    raise ValueError("Password must be text")
                merged_personal["password"] = password
            elif isinstance(existing_personal, dict) and "password" in existing_personal:
                merged_personal["password"] = existing_personal["password"]
            else:
                merged_personal.pop("password", None)
        _atomic_write(
            config.PROFILE_PATH,
            json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
        )
    return load_dashboard_settings()


def save_dashboard_searches(searches: object) -> dict:
    """Validate and atomically save job-search preferences."""
    incoming = _validate_searches(searches)
    with _settings_write_lock:
        existing = config.load_search_config()
        merged = _deep_merge(existing, incoming)
        _atomic_write(
            config.SEARCH_CONFIG_PATH,
            yaml.safe_dump(merged, sort_keys=False, allow_unicode=True),
        )
    return load_dashboard_settings()


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


def delete_job(raw_url: str, conn: sqlite3.Connection | None = None) -> dict:
    """Delete a job posting from the dashboard database."""
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ValueError("Job URL is required")
    url = raw_url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise ValueError("URL is too long")

    conn = conn or get_connection()
    row = conn.execute(
        "SELECT title FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    if not row:
        return {"deleted": False, "url": url, "status": "missing"}

    conn.execute("DELETE FROM jobs WHERE url = ?", (url,))
    conn.commit()
    return {
        "deleted": True,
        "url": url,
        "title": row["title"],
        "status": "deleted",
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

    def _read_json(self, max_bytes: int = MAX_REQUEST_BYTES) -> dict:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0 or content_length > max_bytes:
            raise ValueError("Invalid request size")
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise ValueError("Content-Type must be application/json")
        payload = json.loads(self.rfile.read(content_length))
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object")
        return payload

    def _validate_origin(self) -> None:
        self._validate_local_host()
        origin = self.headers.get("Origin")
        if not origin:
            return
        origin_host = urlparse(origin).netloc.lower()
        request_host = self.headers.get("Host", "").lower()
        if not origin_host or origin_host != request_host:
            raise PermissionError("Cross-origin settings updates are not allowed")

    def _validate_local_host(self) -> None:
        request_host = self.headers.get("Host", "")
        hostname = urlparse(f"//{request_host}").hostname
        bound_host = str(self.server.server_address[0]).lower()
        allowed = {"127.0.0.1", "localhost", "::1", bound_host}
        if not hostname or hostname.lower() not in allowed:
            raise PermissionError("Settings are only available from localhost")

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

        if parsed.path == "/api/settings":
            try:
                self._validate_local_host()
                self._send_json(200, load_dashboard_settings())
            except PermissionError as exc:
                self._send_json(403, {"error": str(exc)})
            except (FileNotFoundError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
                self._send_json(500, {"error": str(exc)})
            return

        if parsed.path == "/api/resume/pdf":
            try:
                self._validate_local_host()
                if (
                    not config.RESUME_TEX_PATH.exists()
                    or not config.RESUME_PDF_PATH.exists()
                ):
                    self._send_json(404, {"error": "Compiled LaTeX resume not found"})
                    return
                self._send_bytes(
                    200,
                    config.RESUME_PDF_PATH.read_bytes(),
                    "application/pdf",
                )
            except PermissionError as exc:
                self._send_json(403, {"error": str(exc)})
            except OSError as exc:
                self._send_json(500, {"error": f"Could not read resume PDF: {exc}"})
            return

        if parsed.path == "/api/resume":
            try:
                self._validate_local_host()
                resume_format = parse_qs(parsed.query).get("format", ["txt"])[0]
                self._send_json(200, load_dashboard_resume(resume_format))
            except PermissionError as exc:
                self._send_json(403, {"error": str(exc)})
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except (OSError, UnicodeError) as exc:
                self._send_json(500, {"error": f"Could not read resume: {exc}"})
            return

        self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in {
            "/api/jobs",
            "/api/jobs/applied",
            "/api/jobs/delete",
            "/api/discovery",
        }:
            self._send_json(404, {"error": "Not found"})
            return

        try:
            payload = self._read_json()
            if path == "/api/discovery":
                result = start_discovery(self.server, payload.get("workers", 4))
            elif path == "/api/jobs/applied":
                result = mark_job_applied(payload.get("url", ""))
            elif path == "/api/jobs/delete":
                result = delete_job(payload.get("url", ""))
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

        if path == "/api/jobs/delete":
            if result["deleted"]:
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

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        if path not in {
            "/api/settings/profile",
            "/api/settings/searches",
            "/api/resume",
        }:
            self._send_json(404, {"error": "Not found"})
            return

        try:
            self._validate_origin()
            max_bytes = (
                MAX_RESUME_REQUEST_BYTES
                if path == "/api/resume"
                else MAX_SETTINGS_BYTES
            )
            payload = self._read_json(max_bytes)
            if path == "/api/settings/profile":
                result = save_dashboard_profile(payload.get("profile"))
            elif path == "/api/resume":
                result = save_dashboard_resume(
                    payload.get("filename"),
                    payload.get("content"),
                )
            else:
                result = save_dashboard_searches(payload.get("searches"))
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
            return
        except (
            FileNotFoundError,
            ValueError,
            json.JSONDecodeError,
            yaml.YAMLError,
        ) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except OSError as exc:
            log.exception("Could not save dashboard settings")
            self._send_json(500, {"error": f"Could not save settings: {exc}"})
            return

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
