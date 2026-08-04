"""Tests for pipeline stage resolution."""

from __future__ import annotations

from applypilot.database import get_jobs_by_stage, get_stats, init_db
from applypilot.pipeline import _PENDING_SQL, _resolve_stages


def test_discover_includes_enrich_and_score_when_llm_available(monkeypatch) -> None:
    monkeypatch.setattr("applypilot.config.get_tier", lambda: 2)
    assert _resolve_stages(["discover"]) == ["discover", "enrich", "score"]


def test_discover_stays_alone_without_llm(monkeypatch) -> None:
    monkeypatch.setattr("applypilot.config.get_tier", lambda: 1)
    assert _resolve_stages(["discover"]) == ["discover"]


def test_discover_with_later_stages_keeps_order(monkeypatch) -> None:
    monkeypatch.setattr("applypilot.config.get_tier", lambda: 2)
    assert _resolve_stages(["discover", "tailor"]) == [
        "discover",
        "enrich",
        "score",
        "tailor",
    ]


def test_explicit_score_only_unchanged(monkeypatch) -> None:
    monkeypatch.setattr("applypilot.config.get_tier", lambda: 2)
    assert _resolve_stages(["score"]) == ["score"]


def test_pending_tailoring_excludes_jobs_already_applied_to(tmp_path) -> None:
    connection = init_db(tmp_path / "jobs.db")
    for url, applied_at in (
        ("https://example.com/not-applied", None),
        ("https://example.com/already-applied", "2026-08-02T12:00:00+00:00"),
    ):
        connection.execute(
            "INSERT INTO jobs (url, title, full_description, fit_score, applied_at) "
            "VALUES (?, 'Engineer', 'Complete job description', 8, ?)",
            (url, applied_at),
        )
    connection.commit()

    pending = get_jobs_by_stage(
        connection,
        stage="pending_tailor",
        min_score=7,
    )
    assert [job["url"] for job in pending] == ["https://example.com/not-applied"]
    assert get_stats(connection)["untailored_eligible"] == 1

    pipeline_count = connection.execute(_PENDING_SQL["tailor"], (7,)).fetchone()[0]
    assert pipeline_count == 1
