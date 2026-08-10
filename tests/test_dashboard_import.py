"""Tests for external dashboard job imports."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest
import yaml

import applypilot.dashboard_server as dashboard_server
from applypilot.apply.prompt import _build_salary_section
from applypilot.config import location_filter_is_mandatory, location_is_allowed
from applypilot.dashboard_server import (
    DashboardHTTPServer,
    DashboardRequestHandler,
    delete_job,
    import_external_job,
    job_import_status,
    load_dashboard_resume,
    load_dashboard_settings,
    mark_job_applied,
    normalize_job_url,
    save_dashboard_profile,
    save_dashboard_resume,
    save_dashboard_searches,
    start_tailoring,
    tailoring_status,
)
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


def test_delete_job(db) -> None:
    imported = import_external_job("https://example.com/jobs/delete-me", db)

    result = delete_job(imported["url"], db)
    missing = delete_job(imported["url"], db)

    assert result["deleted"] is True
    assert result["status"] == "deleted"
    assert missing["deleted"] is False
    assert missing["status"] == "missing"
    assert (
        db.execute(
            "SELECT COUNT(*) FROM jobs WHERE url = ?",
            (imported["url"],),
        ).fetchone()[0]
        == 0
    )


def test_delete_job_requires_url(db) -> None:
    with pytest.raises(ValueError, match="Job URL is required"):
        delete_job("", db)


@pytest.fixture
def settings_files(tmp_path, monkeypatch):
    profile_path = tmp_path / "profile.json"
    search_path = tmp_path / "searches.yaml"
    resume_path = tmp_path / "resume.txt"
    resume_tex_path = tmp_path / "resume.tex"
    resume_pdf_path = tmp_path / "resume.pdf"
    profile_path.write_text(
        json.dumps(
            {
                "personal": {
                    "full_name": "Example User",
                    "email": "user@example.com",
                    "password": "stored-secret",
                },
                "experience": {"target_role": "Software Engineer"},
                "skills_boundary": {
                    "programming_languages": ["Python", "Custom Language"],
                    "frameworks": ["FastAPI"],
                    "tools": ["Custom Platform"],
                },
                "resume_facts": {
                    "preserved_companies": ["Example Corp"],
                    "preserved_projects": ["ApplyPilot"],
                    "preserved_school": "Example University",
                    "real_metrics": ["50% faster"],
                },
                "custom_section": {"keep": True},
            }
        ),
        encoding="utf-8",
    )
    search_path.write_text(
        yaml.safe_dump(
            {
                "defaults": {
                    "location": "Canada",
                    "distance": 25,
                    "hours_old": 72,
                    "results_per_site": 50,
                },
                "queries": [{"query": "Software Engineer", "tier": 1}],
                "locations": [{"location": "Canada", "remote": True}],
                "allowed_countries": ["Canada", "United States"],
                "location_accept": ["Toronto"],
                "location_reject_non_remote": ["India"],
                "priority_titles": ["Backend Engineer", "Custom Title"],
                "exclude_titles": ["Senior Director"],
                "custom_search_key": "keep",
                "custom_null": None,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    resume_path.write_text("Existing resume\nExperience\n", encoding="utf-8")
    resume_tex_path.write_text(
        "\\documentclass{article}\n\\begin{document}\nResume\n\\end{document}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboard_server.config, "PROFILE_PATH", profile_path)
    monkeypatch.setattr(dashboard_server.config, "SEARCH_CONFIG_PATH", search_path)
    monkeypatch.setattr(dashboard_server.config, "RESUME_PATH", resume_path)
    monkeypatch.setattr(
        dashboard_server.config,
        "RESUME_TEX_PATH",
        resume_tex_path,
    )
    monkeypatch.setattr(dashboard_server.config, "RESUME_PDF_PATH", resume_pdf_path)
    monkeypatch.setattr(
        dashboard_server,
        "_compile_latex_resume",
        lambda content: b"%PDF-1.4\n% fake tectonic output\n",
    )
    return profile_path, search_path


def test_dashboard_settings_redact_and_preserve_password(settings_files) -> None:
    profile_path, _ = settings_files
    settings = load_dashboard_settings()

    assert settings["password_configured"] is True
    assert "password" not in settings["profile"]["personal"]

    profile = settings["profile"]
    profile["experience"]["target_role"] = "Backend Engineer"
    profile["experience"]["years_of_experience_total"] = "2.5"
    profile["compensation"] = {"salary_expectation": "100000"}
    saved = save_dashboard_profile(profile)

    stored = json.loads(profile_path.read_text(encoding="utf-8"))
    assert stored["personal"]["password"] == "stored-secret"
    assert stored["experience"]["target_role"] == "Backend Engineer"
    assert stored["experience"]["years_of_experience_total"] == 2.5
    assert stored["compensation"]["salary_expectation"] == 100000
    assert stored["custom_section"] == {"keep": True}
    assert "password" not in saved["profile"]["personal"]


def test_dashboard_search_settings_save_yaml(settings_files) -> None:
    _, search_path = settings_files
    searches = load_dashboard_settings()["searches"]
    searches["queries"].append({"query": "AI Engineer", "tier": "2"})
    searches["defaults"]["distance"] = "30"
    searches["locations"][0]["remote"] = "false"

    save_dashboard_searches(searches)

    stored = yaml.safe_load(search_path.read_text(encoding="utf-8"))
    assert stored["queries"][-1] == {"query": "AI Engineer", "tier": 2}
    assert stored["defaults"]["distance"] == 30
    assert stored["locations"][0]["remote"] is False
    assert stored["custom_search_key"] == "keep"


def test_dashboard_search_settings_reject_invalid_tier(settings_files) -> None:
    searches = load_dashboard_settings()["searches"]
    searches["queries"][0]["tier"] = 9
    with pytest.raises(ValueError, match="tiers"):
        save_dashboard_searches(searches)


def test_dashboard_search_settings_reject_non_text_values(settings_files) -> None:
    searches = load_dashboard_settings()["searches"]
    searches["queries"][0]["query"] = None
    with pytest.raises(ValueError, match="title"):
        save_dashboard_searches(searches)


def test_dashboard_tag_editor_lists_round_trip_custom_values(settings_files) -> None:
    profile_path, search_path = settings_files
    settings = load_dashboard_settings()
    settings["profile"]["skills_boundary"]["tools"].append("In-house Tool")
    settings["profile"]["resume_facts"]["real_metrics"].append("99.9% uptime")
    settings["searches"]["allowed_countries"].append("Mexico")
    settings["searches"]["priority_titles"].append("Developer Advocate (AI)")

    save_dashboard_profile(settings["profile"])
    save_dashboard_searches(settings["searches"])

    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    searches = yaml.safe_load(search_path.read_text(encoding="utf-8"))
    assert profile["skills_boundary"]["programming_languages"] == [
        "Python",
        "Custom Language",
    ]
    assert profile["skills_boundary"]["tools"][-1] == "In-house Tool"
    assert profile["resume_facts"]["real_metrics"][-1] == "99.9% uptime"
    assert searches["allowed_countries"][-1] == "Mexico"
    assert searches["priority_titles"][-1] == "Developer Advocate (AI)"
    assert searches["exclude_titles"] == ["Senior Director"]


def test_dashboard_settings_reject_non_text_list_items(settings_files) -> None:
    settings = load_dashboard_settings()
    settings["profile"]["skills_boundary"] = {"tools": [123]}
    with pytest.raises(ValueError, match="only text"):
        save_dashboard_profile(settings["profile"])

    settings["searches"]["allowed_countries"] = ["Canada", 123]
    with pytest.raises(ValueError, match="only text"):
        save_dashboard_searches(settings["searches"])


def test_dashboard_settings_reject_invalid_numbers(settings_files) -> None:
    settings = load_dashboard_settings()
    settings["profile"]["compensation"] = {"salary_expectation": "-1"}
    with pytest.raises(ValueError, match="non-negative"):
        save_dashboard_profile(settings["profile"])

    settings["searches"]["defaults"]["distance"] = "nan"
    with pytest.raises(ValueError, match="non-negative"):
        save_dashboard_searches(settings["searches"])


def test_salary_prompt_accepts_numeric_profile_values() -> None:
    section = _build_salary_section(
        {
            "compensation": {
                "salary_expectation": 100000,
                "salary_currency": "CAD",
            }
        }
    )
    assert "120000" in section


def test_dashboard_search_settings_can_clear_numeric_default(settings_files) -> None:
    _, search_path = settings_files
    searches = load_dashboard_settings()["searches"]
    searches["defaults"]["distance"] = {"__applypilot_delete__": True}

    save_dashboard_searches(searches)

    stored = yaml.safe_load(search_path.read_text(encoding="utf-8"))
    assert "distance" not in stored["defaults"]
    assert "custom_null" in stored and stored["custom_null"] is None


def test_dashboard_resume_load_and_replace(settings_files) -> None:
    assert (
        dashboard_server.MAX_RESUME_REQUEST_BYTES
        >= dashboard_server.MAX_RESUME_BYTES * 6
    )
    result = load_dashboard_resume()
    assert result == {
        "exists": True,
        "filename": "resume.txt",
        "format": "txt",
        "content": "Existing resume\nExperience\n",
    }

    updated = save_dashboard_resume(
        "updated-resume.txt",
        "Updated resume\nProjects\n",
    )

    assert updated["content"] == "Updated resume\nProjects\n"
    assert (
        dashboard_server.config.RESUME_PATH.read_text(encoding="utf-8")
        == "Updated resume\nProjects\n"
    )

    latex = save_dashboard_resume(
        "updated-resume.tex",
        "\\documentclass{article}\n\\begin{document}\nUpdated\n\\end{document}\n",
    )
    assert latex["format"] == "tex"
    assert latex["pdf_available"] is True
    assert load_dashboard_resume("tex")["content"] == latex["content"]
    assert (
        dashboard_server.config.RESUME_PDF_PATH.read_bytes().startswith(b"%PDF-")
    )


@pytest.mark.parametrize("filename", ["resume.pdf", "../resume.txt", ""])
def test_dashboard_resume_rejects_invalid_filename(
    settings_files,
    filename,
) -> None:
    with pytest.raises(ValueError):
        save_dashboard_resume(filename, "Resume")


def test_dashboard_resume_rejects_empty_content(settings_files) -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        save_dashboard_resume("resume.txt", " \n\t")


def test_prepare_tex_for_tectonic_disables_pdftex_glyph_map() -> None:
    source = (
        "\\documentclass{article}\n"
        "\\input{glyphtounicode}\n"
        "\\pdfgentounicode=1\n"
        "\\begin{document}Hi\\end{document}\n"
    )
    prepared = dashboard_server._prepare_tex_for_tectonic(source)
    assert r"\input{glyphtounicode}" not in prepared.splitlines()
    assert "% \\input{glyphtounicode}" in prepared
    assert "% \\pdfgentounicode=1" in prepared
    assert "\\begin{document}Hi\\end{document}" in prepared


def test_dashboard_latex_compile_failure_keeps_previous_pdf(
    settings_files,
    monkeypatch,
) -> None:
    pdf_path = dashboard_server.config.RESUME_PDF_PATH
    tex_path = dashboard_server.config.RESUME_TEX_PATH
    original_tex = tex_path.read_text(encoding="utf-8")
    pdf_path.write_bytes(b"%PDF-1.4\n% previous\n")

    def fail_compile(_content: str) -> bytes:
        raise ValueError("LaTeX compilation failed:\nmissing package")

    monkeypatch.setattr(dashboard_server, "_compile_latex_resume", fail_compile)

    with pytest.raises(ValueError, match="compilation failed"):
        save_dashboard_resume(
            "broken.tex",
            "\\documentclass{article}\\begin{document}x\\end{document}",
        )

    assert tex_path.read_text(encoding="utf-8") == original_tex
    assert pdf_path.read_bytes() == b"%PDF-1.4\n% previous\n"


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
    [
        "China - Remote",
        "Remote - India",
        "London, United Kingdom",
        "Tbilisi, Georgia",
        "Remote",
    ],
)
def test_country_location_policy_rejects_other_or_unknown_regions(location: str) -> None:
    config = {
        "allowed_countries": ["Canada", "United States"],
        "accept_remote_anywhere": False,
        "accept_unknown_locations": False,
    }
    assert not location_is_allowed(location, config)


def test_country_allowlist_makes_discovery_filter_mandatory() -> None:
    config = {
        "allowed_countries": ["Canada", "United States"],
        "greenhouse_location_filter": False,
        "bigtech_location_filter": False,
    }

    assert location_filter_is_mandatory(config)


def test_dashboard_api_imports_job(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "api.db"
    init_db(db_path)
    monkeypatch.setattr(
        dashboard_server,
        "get_connection",
        lambda: get_connection(db_path),
    )
    monkeypatch.setattr(dashboard_server, "enrich_external_job", lambda _url: None)

    def complete_discovery(server, workers):
        with server.discovery_lock:
            server.discovery_state = {
                **server.discovery_state,
                "status": "complete",
                "result": {"new": 3, "existing": 7},
                "finished_at": "2026-07-29T15:00:00+00:00",
            }

    monkeypatch.setattr(
        dashboard_server,
        "_execute_discovery",
        complete_discovery,
    )

    def complete_tailoring(request):
        return "complete", {"approved": 2, "failed": 1, "errors": 0}, None

    monkeypatch.setattr(
        dashboard_server,
        "_run_tailoring_request",
        complete_tailoring,
    )

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

        connection = get_connection(db_path)
        connection.execute(
            "UPDATE jobs SET full_description = ?, fit_score = 4 WHERE url = ?",
            ("A complete job description suitable for tailoring", result["url"]),
        )
        connection.commit()

        individual_tailoring_request = urllib.request.Request(
            f"{base_url}/api/tailoring/job",
            data=json.dumps({"url": result["url"], "validation_mode": "normal"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(individual_tailoring_request) as response:
            individual = json.load(response)
            assert response.status == 202
            assert individual["target_url"] == result["url"]

        for _ in range(20):
            with urllib.request.urlopen(f"{base_url}/api/tailoring/status") as response:
                individual = json.load(response)
            if individual["status"] == "idle" and individual["recent"]:
                break
            time.sleep(0.01)
        assert individual["recent"][0]["target_url"] == result["url"]
        assert individual["recent"][0]["status"] == "complete"

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

        delete_request = urllib.request.Request(
            f"{base_url}/api/jobs/delete",
            data=json.dumps({"url": result["url"]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(delete_request) as response:
            deleted = json.load(response)
            assert response.status == 200
            assert deleted["deleted"] is True

        assert (
            get_connection(db_path).execute(
                "SELECT COUNT(*) FROM jobs WHERE url = ?",
                (result["url"],),
            ).fetchone()[0]
            == 0
        )

        discovery_request = urllib.request.Request(
            f"{base_url}/api/discovery",
            data=json.dumps({"workers": 2}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(discovery_request) as response:
            discovery = json.load(response)
            assert response.status == 202
            assert discovery["status"] == "running"

        for _ in range(20):
            with urllib.request.urlopen(
                f"{base_url}/api/discovery/status"
            ) as response:
                discovery = json.load(response)
            if discovery["status"] == "complete":
                break
            time.sleep(0.01)
        assert discovery["result"] == {"new": 3, "existing": 7}

        tailoring_request = urllib.request.Request(
            f"{base_url}/api/tailoring",
            data=json.dumps(
                {"min_score": 7, "limit": 20, "validation_mode": "normal"}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(tailoring_request) as response:
            tailoring = json.load(response)
            assert response.status == 202
            assert tailoring["status"] == "queued"
            assert tailoring["kind"] == "batch"

        for _ in range(20):
            with urllib.request.urlopen(
                f"{base_url}/api/tailoring/status"
            ) as response:
                tailoring = json.load(response)
            if (
                tailoring["status"] == "idle"
                and tailoring["recent"]
                and tailoring["recent"][0]["kind"] == "batch"
            ):
                break
            time.sleep(0.01)
        assert tailoring["recent"][0]["result"] == {
            "approved": 2,
            "failed": 1,
            "errors": 0,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_tailoring_queue_runs_fifo_deduplicates_and_continues_after_error(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "tailoring-queue.db"
    conn = init_db(db_path)
    urls = [f"https://example.com/jobs/{index}" for index in range(3)]
    for url in urls:
        conn.execute(
            "INSERT INTO jobs (url, title, full_description, fit_score) "
            "VALUES (?, 'Engineer', 'Complete description', 8)",
            (url,),
        )
    conn.commit()
    monkeypatch.setattr(
        dashboard_server,
        "get_connection",
        lambda: get_connection(db_path),
    )

    first_started = threading.Event()
    release_first = threading.Event()
    calls: list[str] = []
    active = 0
    max_active = 0
    calls_lock = threading.Lock()

    def execute(request):
        nonlocal active, max_active
        with calls_lock:
            active += 1
            max_active = max(max_active, active)
            calls.append(request["target_url"])
        if request["target_url"] == urls[0]:
            first_started.set()
            assert release_first.wait(timeout=2)
        with calls_lock:
            active -= 1
        if request["target_url"] == urls[1]:
            return "error", None, "simulated failure"
        return "complete", {"approved": 1, "failed": 0, "errors": 0}, None

    monkeypatch.setattr(dashboard_server, "_run_tailoring_request", execute)
    server = DashboardHTTPServer(("127.0.0.1", 0), DashboardRequestHandler)
    try:
        first = start_tailoring(server, min_score=1, limit=1, target_url=urls[0])
        assert first["status"] == "queued"
        assert first_started.wait(timeout=2)

        second = start_tailoring(server, min_score=1, limit=1, target_url=urls[1])
        third = start_tailoring(server, min_score=1, limit=1, target_url=urls[2])
        duplicate = start_tailoring(server, min_score=1, limit=1, target_url=urls[1])

        assert second["queue_position"] == 1
        assert third["queue_position"] == 2
        assert duplicate["id"] == second["id"]
        assert duplicate["deduplicated"] is True
        state = tailoring_status(server)
        assert state["current"]["target_url"] == urls[0]
        assert [item["target_url"] for item in state["queued"]] == urls[1:]

        release_first.set()
        for _ in range(100):
            state = tailoring_status(server)
            if state["status"] == "idle":
                break
            time.sleep(0.01)

        assert state["status"] == "idle"
        assert calls == urls
        assert max_active == 1
        statuses = {item["target_url"]: item["status"] for item in state["recent"]}
        assert statuses == {
            urls[0]: "complete",
            urls[1]: "error",
            urls[2]: "complete",
        }
    finally:
        release_first.set()
        server.server_close()


def test_tailoring_queue_skips_job_that_becomes_ineligible(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "tailoring-stale.db"
    conn = init_db(db_path)
    urls = ["https://example.com/jobs/active", "https://example.com/jobs/stale"]
    for url in urls:
        conn.execute(
            "INSERT INTO jobs (url, title, full_description, fit_score) "
            "VALUES (?, 'Engineer', 'Complete description', 8)",
            (url,),
        )
    conn.commit()
    monkeypatch.setattr(
        dashboard_server,
        "get_connection",
        lambda: get_connection(db_path),
    )

    first_started = threading.Event()
    release_first = threading.Event()
    original_execute = dashboard_server._run_tailoring_request

    def execute(request):
        if request["target_url"] == urls[0]:
            first_started.set()
            assert release_first.wait(timeout=2)
            return "complete", {"approved": 1, "failed": 0, "errors": 0}, None
        return original_execute(request)

    monkeypatch.setattr(dashboard_server, "_run_tailoring_request", execute)
    server = DashboardHTTPServer(("127.0.0.1", 0), DashboardRequestHandler)
    try:
        start_tailoring(server, min_score=1, limit=1, target_url=urls[0])
        assert first_started.wait(timeout=2)
        start_tailoring(server, min_score=1, limit=1, target_url=urls[1])
        conn.execute(
            "UPDATE jobs SET applied_at = '2026-08-03T12:00:00+00:00' WHERE url = ?",
            (urls[1],),
        )
        conn.commit()
        release_first.set()

        for _ in range(100):
            state = tailoring_status(server)
            if state["status"] == "idle":
                break
            time.sleep(0.01)

        stale = next(item for item in state["recent"] if item["target_url"] == urls[1])
        assert stale["status"] == "skipped"
        assert stale["result"]["reason"] == "This job is already marked as applied"
    finally:
        release_first.set()
        server.server_close()


def test_tailoring_queue_allows_only_one_outstanding_batch(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()

    def execute(_request):
        started.set()
        assert release.wait(timeout=2)
        return "complete", {"approved": 0, "failed": 0, "errors": 0}, None

    monkeypatch.setattr(dashboard_server, "_run_tailoring_request", execute)
    server = DashboardHTTPServer(("127.0.0.1", 0), DashboardRequestHandler)
    try:
        start_tailoring(server)
        assert started.wait(timeout=2)
        with pytest.raises(RuntimeError, match="bulk tailoring request"):
            start_tailoring(server)
    finally:
        release.set()
        server.server_close()


def test_dashboard_settings_api(settings_files) -> None:
    server = DashboardHTTPServer(
        ("127.0.0.1", 0),
        DashboardRequestHandler,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        with urllib.request.urlopen(f"{base_url}/api/settings") as response:
            settings = json.load(response)
        assert settings["password_configured"] is True
        assert "password" not in settings["profile"]["personal"]

        with urllib.request.urlopen(f"{base_url}/api/resume") as response:
            resume = json.load(response)
        assert resume["content"] == "Existing resume\nExperience\n"
        with urllib.request.urlopen(
            f"{base_url}/api/resume?format=tex"
        ) as response:
            latex_resume = json.load(response)
        assert latex_resume["format"] == "tex"

        latex_request = urllib.request.Request(
            f"{base_url}/api/resume",
            data=json.dumps(
                {
                    "filename": "replacement.tex",
                    "content": (
                        "\\documentclass{article}\n"
                        "\\begin{document}\nHello\n\\end{document}\n"
                    ),
                }
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "Origin": base_url,
            },
            method="PUT",
        )
        with urllib.request.urlopen(latex_request) as response:
            latex_uploaded = json.load(response)
        assert latex_uploaded["pdf_available"] is True

        with urllib.request.urlopen(f"{base_url}/api/resume/pdf") as response:
            assert response.status == 200
            assert response.headers.get_content_type() == "application/pdf"
            assert response.read().startswith(b"%PDF-")

        resume_request = urllib.request.Request(
            f"{base_url}/api/resume",
            data=json.dumps(
                {
                    "filename": "replacement.txt",
                    "content": "Replacement resume",
                }
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "Origin": base_url,
            },
            method="PUT",
        )
        with urllib.request.urlopen(resume_request) as response:
            uploaded = json.load(response)
        assert uploaded["content"] == "Replacement resume"

        rebound = urllib.request.Request(
            f"{base_url}/api/settings",
            headers={"Host": "attacker.example"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(rebound)
        assert exc_info.value.code == 403

        settings["profile"]["personal"]["full_name"] = "Updated User"
        request = urllib.request.Request(
            f"{base_url}/api/settings/profile",
            data=json.dumps({"profile": settings["profile"]}).encode(),
            headers={
                "Content-Type": "application/json",
                "Origin": base_url,
            },
            method="PUT",
        )
        with urllib.request.urlopen(request) as response:
            updated = json.load(response)
        assert updated["profile"]["personal"]["full_name"] == "Updated User"

        invalid = urllib.request.Request(
            f"{base_url}/api/settings/searches",
            data=json.dumps(
                {"searches": {"queries": [{"query": "Test", "tier": 9}]}}
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "Origin": base_url,
            },
            method="PUT",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(invalid)
        assert exc_info.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_dashboard_has_active_and_applied_tabs(tmp_path, monkeypatch) -> None:
    connection = init_db(tmp_path / "dashboard.db")
    import_external_job("https://example.com/jobs/active", connection)
    connection.execute(
        "UPDATE jobs SET full_description = ? WHERE url = ?",
        ("Complete job description", "https://example.com/jobs/active"),
    )
    applied = import_external_job("https://example.com/jobs/done", connection)
    mark_job_applied(applied["url"], connection)
    connection.execute(
        "UPDATE jobs SET tailored_resume_path = ? WHERE url = ?",
        (
            str(tmp_path / "Example_Engineer_Tailored_Resume.tex"),
            applied["url"],
        ),
    )
    connection.commit()

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
    assert "delete-job-btn" in html
    assert "/api/jobs/delete" in html
    assert "All Sources" in html
    assert 'data-site="example.com"' in html
    assert "filterSource(this.value)" in html
    assert "Run Discovery" in html
    assert "/api/discovery/status" in html
    assert "Run Tailoring" in html
    assert "/api/tailoring/status" in html
    assert "Queued (#" in html
    assert "applypilotTailoringPending" in html
    assert "Tailored resume" in html
    assert "Resume PDF" in html
    assert "kind=tex" in html
    assert "kind=report" in html
    assert 'class="tailor-job-btn"' in html
    assert "/api/tailoring/job" in html
    assert 'data-view="dashboard"' in html
    assert 'data-view="profile"' in html
    assert "Profile and Preferences" in html
    assert "fetch('/api/settings/' + type" in html
    assert 'data-settings-tab="resume"' in html
    assert 'id="resume-preview"' in html
    assert 'accept=".txt,text/plain"' in html
    assert 'accept=".tex,text/x-tex,application/x-tex"' in html
    assert 'id="latex-resume-preview"' in html
    assert "pdfjs-dist@4.10.38" in html
    assert "/api/resume/pdf" in html
    assert "compiled with Tectonic" in html
    assert "fetchResume('/api/resume?format=tex'" in html
    assert "fetch('/api/resume'" in html
    assert "new TextDecoder('utf-8', {fatal: true})" in html
    assert "switchSettingsTab(currentSettingsTab)" in html
    assert "latex.js@" not in html
    assert 'data-profile-number="compensation.salary_expectation"' in html
    assert 'data-profile-number="experience.years_of_experience_total"' in html
    assert 'type="number" min="0" step="any"' in html
    assert '<select data-profile-path="work_authorization.work_permit_type">' in html
    assert '<select data-profile-path="experience.education_level">' in html
    assert '<select data-profile-path="eeo_voluntary.gender">' in html
    tag_paths = (
        "skills_boundary.programming_languages",
        "skills_boundary.frameworks",
        "skills_boundary.tools",
        "resume_facts.preserved_companies",
        "resume_facts.preserved_projects",
        "resume_facts.real_metrics",
        "allowed_countries",
        "location_accept",
        "location_reject_non_remote",
        "include_titles",
        "priority_titles",
        "exclude_titles",
    )
    assert html.count('<div class="tag-editor" data-tag-editor') == len(tag_paths)
    for path in tag_paths:
        assert f'data-tag-path="{path}"' in html
    assert 'textarea data-profile-list="skills_boundary.' not in html
    assert 'textarea data-profile-list="resume_facts.' not in html
    assert 'textarea data-search-list="priority_titles"' not in html
    assert 'textarea data-search-list="exclude_titles"' not in html
    assert 'textarea data-search-list="allowed_countries"' not in html
    assert 'textarea data-search-list="location_accept"' not in html
    assert 'textarea data-search-list="location_reject_non_remote"' not in html
    assert html.count('data-tag-add type="button" aria-label="Add ') == len(tag_paths)
    assert html.count('data-tag-list aria-live="polite"') == len(tag_paths)
    assert "remove.setAttribute('aria-label', 'Remove ' + value)" in html
    assert "const value = input.value.trim();" in html
    assert "if (!value || values.includes(value)) return;" in html
    assert "if (event.key !== 'Enter') return;" in html
