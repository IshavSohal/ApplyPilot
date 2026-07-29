"""Tests for external dashboard job imports."""

from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request

import pytest

import applypilot.dashboard_server as dashboard_server
from applypilot.dashboard_server import (
    DashboardHTTPServer,
    DashboardRequestHandler,
    import_external_job,
    job_import_status,
    mark_job_applied,
    normalize_job_url,
)
from applypilot.config import location_is_allowed
from applypilot.database import get_connection, init_db
from applypilot.enrichment.detail import extract_job_metadata
from applypilot.view import format_applied_at, format_posted_at, generate_dashboard


@pytest.fixture
def db(tmp_path):
    connection = init_db(tmp_path / "applypilot.db")
    yield connection
    connection.close()


def test_normalize_job_url() -> None:
    assert (
        normalize_job_url(" HTTPS://Example.COM/jobs/123?source=test#apply ")
        == "https://example.com/jobs/123?source=test"
    )


@pytest.mark.parametrize(
    "url",
    ["", "example.com/job", "file:///tmp/job", "https://user:pass@example.com/job"],
)
def test_normalize_job_url_rejects_invalid_values(url: str) -> None:
    with pytest.raises(ValueError):
        normalize_job_url(url)


def test_import_external_job_and_duplicate(db) -> None:
    first = import_external_job("https://example.com/jobs/123", db)
    second = import_external_job("https://example.com/jobs/123#apply", db)

    assert first["created"] is True
    assert first["status"] == "pending"
    assert second["created"] is False
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1

    row = db.execute(
        "SELECT strategy, site, application_url FROM jobs"
    ).fetchone()
    assert row["strategy"] == "external_upload"
    assert row["site"] == "example.com"
    assert row["application_url"] == "https://example.com/jobs/123"
    assert job_import_status(first["url"], db)["status"] == "pending"


def test_mark_job_applied(db) -> None:
    imported = import_external_job("https://example.com/jobs/applied", db)

    result = mark_job_applied(imported["url"], db)
    repeated = mark_job_applied(imported["url"], db)

    assert result["updated"] is True
    assert result["status"] == "applied"
    assert repeated["applied_at"] == result["applied_at"]
    row = db.execute(
        "SELECT applied_at, apply_status FROM jobs WHERE url = ?",
        (imported["url"],),
    ).fetchone()
    assert row["applied_at"] == result["applied_at"]
    assert row["apply_status"] == "manually_applied"


def test_mark_job_applied_reports_missing_job(db) -> None:
    result = mark_job_applied("https://example.com/jobs/missing", db)
    assert result["updated"] is False
    assert result["status"] == "missing"


def test_extract_job_metadata_from_json_ld() -> None:
    metadata = extract_job_metadata(
        {
            "page_title": "Fallback title | Example",
            "json_ld": [
                {
                    "@type": "JobPosting",
                    "title": "Junior Software Engineer",
                    "datePosted": "2026-07-20",
                    "hiringOrganization": {"name": "Example Corp"},
                    "jobLocation": {
                        "@type": "Place",
                        "address": {
                            "addressLocality": "Toronto",
                            "addressRegion": "ON",
                            "addressCountry": "Canada",
                        },
                    },
                }
            ],
        }
    )

    assert metadata == {
        "title": "Junior Software Engineer",
        "company": "Example Corp",
        "location": "Toronto, ON, Canada",
        "posted_at": "2026-07-20",
    }


def test_extract_job_metadata_uses_page_title_fallback() -> None:
    metadata = extract_job_metadata(
        {"page_title": "Backend Engineer | Example Careers", "json_ld": []}
    )
    assert metadata["title"] == "Backend Engineer"
    assert metadata["company"] is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-07-20", "Posted Jul 20, 2026"),
        ("2026-07-20T14:30:00Z", "Posted Jul 20, 2026"),
        ("April  9, 2026", "Posted Apr 9, 2026"),
        ("Posted 2 Days Ago", "Posted 2 Days Ago"),
        (None, ""),
    ],
)
def test_format_posted_at(value, expected) -> None:
    assert format_posted_at(value) == expected


def test_format_applied_at() -> None:
    assert format_applied_at("2026-07-29T15:30:00Z") == "Applied Jul 29, 2026"


@pytest.mark.parametrize(
    "location",
    [
        "Toronto, ON, Canada",
        "Vancouver, BC",
        "Seattle, WA",
        "Remote, US",
        "Austin, Texas, USA",
    ],
)
def test_country_location_policy_accepts_canada_and_us(location: str) -> None:
    config = {
        "allowed_countries": ["Canada", "United States"],
        "accept_remote_anywhere": False,
        "accept_unknown_locations": False,
    }
    assert location_is_allowed(location, config)


@pytest.mark.parametrize(
    "location",
    ["China - Remote", "Remote - India", "London, United Kingdom", "Remote"],
)
def test_country_location_policy_rejects_other_or_unknown_regions(location: str) -> None:
    config = {
        "allowed_countries": ["Canada", "United States"],
        "accept_remote_anywhere": False,
        "accept_unknown_locations": False,
    }
    assert not location_is_allowed(location, config)


def test_dashboard_api_imports_job(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "api.db"
    init_db(db_path)
    monkeypatch.setattr(
        dashboard_server,
        "get_connection",
        lambda: get_connection(db_path),
    )
    monkeypatch.setattr(dashboard_server, "enrich_external_job", lambda _url: None)

    server = DashboardHTTPServer(
        ("127.0.0.1", 0),
        DashboardRequestHandler,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        request = urllib.request.Request(
            f"{base_url}/api/jobs",
            data=json.dumps({"url": "https://example.com/jobs/api-test"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            result = json.load(response)
            assert response.status == 201
            assert result["created"] is True

        query = urllib.parse.urlencode({"url": result["url"]})
        with urllib.request.urlopen(f"{base_url}/api/jobs/status?{query}") as response:
            status = json.load(response)
            assert status["status"] == "pending"

        applied_request = urllib.request.Request(
            f"{base_url}/api/jobs/applied",
            data=json.dumps({"url": result["url"]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(applied_request) as response:
            applied = json.load(response)
            assert response.status == 200
            assert applied["status"] == "applied"

        row = get_connection(db_path).execute(
            "SELECT applied_at FROM jobs WHERE url = ?",
            (result["url"],),
        ).fetchone()
        assert row["applied_at"] is not None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_dashboard_has_active_and_applied_tabs(tmp_path, monkeypatch) -> None:
    connection = init_db(tmp_path / "dashboard.db")
    import_external_job("https://example.com/jobs/active", connection)
    applied = import_external_job("https://example.com/jobs/done", connection)
    mark_job_applied(applied["url"], connection)

    import applypilot.view as view

    monkeypatch.setattr(view, "get_connection", lambda: connection)
    output = tmp_path / "dashboard.html"
    generate_dashboard(str(output))
    html = output.read_text(encoding="utf-8")

    assert "Active postings (1)" in html
    assert "Applied (1)" in html
    assert 'data-applied="false"' in html
    assert 'data-applied="true"' in html
    assert "Mark as applied" in html
