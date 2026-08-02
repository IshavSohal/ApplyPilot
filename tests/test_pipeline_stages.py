"""Tests for pipeline stage resolution."""

from __future__ import annotations

from applypilot.pipeline import _resolve_stages


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
