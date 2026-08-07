"""ApplyPilot HTML Dashboard Generator.

Generates a self-contained HTML dashboard with:
  - Summary stats (total, enriched, scored, high-fit)
  - Score distribution bar chart
  - Jobs-by-source breakdown
  - Filterable job cards grouped by score
  - Client-side search and score filtering
"""

from __future__ import annotations

import re
import webbrowser
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import quote

from rich.console import Console

from applypilot.config import APP_DIR, load_search_config
from applypilot.database import get_connection

console = Console()


def format_posted_at(value: str | None) -> str:
    """Format an available source posting date for a dashboard badge."""
    if not value:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if text.lower().startswith("posted"):
        return text
    if "ago" in text.lower():
        return f"Posted {text}"

    parsed = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for pattern in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
    if parsed:
        label = parsed.strftime("%b %d, %Y").replace(" 0", " ")
        return f"Posted {label}"
    return f"Posted {text}"


def format_applied_at(value: str | None) -> str:
    """Format an application timestamp for a dashboard badge."""
    posted_label = format_posted_at(value)
    return posted_label.replace("Posted", "Applied", 1) if posted_label else ""


def generate_dashboard(output_path: str | None = None) -> str:
    """Generate an HTML dashboard of all jobs with fit scores.

    Args:
        output_path: Where to write the HTML file. Defaults to ~/.applypilot/dashboard.html.

    Returns:
        Absolute path to the generated HTML file.
    """
    out = Path(output_path) if output_path else APP_DIR / "dashboard.html"

    conn = get_connection()

    # Stats
    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    ready = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE full_description IS NOT NULL AND application_url IS NOT NULL"
    ).fetchone()[0]
    scored = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL"
    ).fetchone()[0]
    high_fit = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score >= 7"
    ).fetchone()[0]
    pending_tailoring = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score >= 7 "
        "AND full_description IS NOT NULL AND applied_at IS NULL "
        "AND tailored_resume_path IS NULL "
        "AND COALESCE(tailor_attempts, 0) < 5"
    ).fetchone()[0]
    applied_count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL"
    ).fetchone()[0]
    active_count = total - applied_count

    # Score distribution
    score_dist: dict[int, int] = {}
    if scored:
        rows = conn.execute(
            "SELECT fit_score, COUNT(*) FROM jobs "
            "WHERE fit_score IS NOT NULL "
            "GROUP BY fit_score ORDER BY fit_score DESC"
        ).fetchall()
        for r in rows:
            score_dist[r[0]] = r[1]

    # Site stats
    site_stats = conn.execute("""
        SELECT site,
               COUNT(*) as total,
               SUM(CASE WHEN fit_score >= 7 THEN 1 ELSE 0 END) as high_fit,
               SUM(CASE WHEN fit_score BETWEEN 5 AND 6 THEN 1 ELSE 0 END) as mid_fit,
               SUM(CASE WHEN fit_score < 5 AND fit_score IS NOT NULL THEN 1 ELSE 0 END) as low_fit,
               SUM(CASE WHEN fit_score IS NULL THEN 1 ELSE 0 END) as unscored,
               ROUND(AVG(fit_score), 1) as avg_score
        FROM jobs GROUP BY site ORDER BY high_fit DESC, total DESC
    """).fetchall()

    # All jobs, with scored jobs first and unscored discovery results last
    jobs = conn.execute(
        """
        SELECT url, title, salary, description, location, site, strategy,
               full_description, application_url, detail_error, posted_at,
               fit_score, score_reasoning, applied_at, tailored_resume_path,
               COALESCE(tailor_attempts, 0) AS tailor_attempts
        FROM jobs
        """
    ).fetchall()

    priority_terms = [
        str(term).lower().strip()
        for term in load_search_config().get("priority_titles", [])
        if term
    ]

    def is_priority_job(job) -> bool:
        title = (job["title"] or "").lower()
        return any(
            re.search(rf"\b{re.escape(term)}\b", title)
            for term in priority_terms
        )

    jobs = sorted(
        jobs,
        key=lambda job: (
            job["fit_score"] is None,
            -(job["fit_score"] or 0),
            not is_priority_job(job),
            job["site"] or "",
            job["title"] or "",
        ),
    )
    priority_count = sum(is_priority_job(job) for job in jobs)

    # Color map per site
    colors = {
        "RemoteOK": "#10b981", "WelcomeToTheJungle": "#f59e0b",
        "Job Bank Canada": "#3b82f6", "CareerJet Canada": "#8b5cf6",
        "Hacker News Jobs": "#ff6600", "BuiltIn Remote": "#ec4899",
        "TD Bank": "#00a651", "CIBC": "#c41f3e", "RBC": "#003168",
        "indeed": "#2164f3", "linkedin": "#0a66c2",
        "Dice": "#eb1c26", "Glassdoor": "#0caa41",
    }

    # Score distribution bar chart
    score_bars = ""
    max_count = max(score_dist.values()) if score_dist else 1
    for s in range(10, 0, -1):
        count = score_dist.get(s, 0)
        pct = (count / max_count * 100) if max_count else 0
        score_color = "#10b981" if s >= 7 else ("#f59e0b" if s >= 5 else "#ef4444")
        score_bars += f"""
        <div class="score-row">
          <span class="score-label">{s}</span>
          <div class="score-bar-track">
            <div class="score-bar-fill" style="width:{pct}%;background:{score_color}"></div>
          </div>
          <span class="score-count">{count}</span>
        </div>"""

    # Site stats rows
    site_rows = ""
    for s in site_stats:
        site = s["site"] or "?"
        color = colors.get(site, "#6b7280")
        avg = s["avg_score"] or 0
        site_rows += f"""
        <div class="site-row">
          <div class="site-name" style="color:{color}">{escape(site)}</div>
          <div class="site-nums">{s['total']} jobs &middot; {s['high_fit']} strong fit &middot; avg score {avg}</div>
          <div class="bar-track">
            <div class="bar-fill" style="width:{s['high_fit']/max(s['total'],1)*100}%;background:{color}"></div>
            <div class="bar-fill" style="width:{s['mid_fit']/max(s['total'],1)*100}%;background:{color}66"></div>
          </div>
        </div>"""
    source_options = "".join(
        f'<option value="{escape(s["site"] or "__unknown__")}">'
        f'{escape(s["site"] or "Unknown")}</option>'
        for s in site_stats
    )

    # Job cards grouped by score
    job_sections = ""
    current_score = None
    for j in jobs:
        score = j["fit_score"] or 0
        priority = is_priority_job(j)
        if score != current_score:
            if current_score is not None:
                job_sections += "</div>"
            score_color = "#10b981" if score >= 7 else ("#f59e0b" if score >= 5 else "#64748b")
            score_label = {
                10: "Perfect Match", 9: "Excellent Fit", 8: "Strong Fit",
                7: "Good Fit", 6: "Moderate+", 5: "Moderate", 0: "Unscored",
            }.get(score, f"Score {score}")
            count_at_score = total - scored if score == 0 else score_dist.get(score, 0)
            score_badge = "&mdash;" if score == 0 else str(score)
            job_sections += f"""
            <h2 class="score-header" data-score-group="{score}" style="border-color:{score_color}">
              <span class="score-badge" style="background:{score_color}">{score_badge}</span>
              {score_label} <span class="score-group-count">({count_at_score} jobs)</span>
            </h2>
            <div class="job-grid">"""
            current_score = score

        title = escape(j["title"] or "Untitled")
        url = escape(j["url"] or "")
        salary = escape(j["salary"] or "")
        location = escape(j["location"] or "")
        site = escape(j["site"] or "Unknown")
        site_value = escape(j["site"] or "__unknown__")
        site_color = colors.get(j["site"] or "", "#6b7280")
        apply_url = escape(j["application_url"] or "")

        # Parse keywords and reasoning from score_reasoning
        reasoning_raw = j["score_reasoning"] or ""
        reasoning_lines = reasoning_raw.split("\n")
        keywords = reasoning_lines[0][:120] if reasoning_lines else ""
        reasoning = reasoning_lines[1][:200] if len(reasoning_lines) > 1 else ""

        desc_preview = escape(j["full_description"] or "")[:300]
        full_desc_html = escape(j["full_description"] or "").replace("\n", "<br>")
        desc_len = len(j["full_description"] or "")
        detail_error = escape(j["detail_error"] or "")
        posted_label = escape(format_posted_at(j["posted_at"]))
        applied_label = escape(format_applied_at(j["applied_at"]))
        is_applied = bool(j["applied_at"])

        meta_parts = []
        if priority:
            meta_parts.append('<span class="meta-tag priority">Priority: early career</span>')
        meta_parts.append(
            f'<span class="meta-tag site-tag" style="background:{site_color}33;color:{site_color}">{site}</span>'
        )
        if salary:
            meta_parts.append(f'<span class="meta-tag salary">{salary}</span>')
        if location:
            meta_parts.append(f'<span class="meta-tag location">{location[:40]}</span>')
        if posted_label:
            meta_parts.append(f'<span class="meta-tag posted">{posted_label}</span>')
        if applied_label:
            meta_parts.append(f'<span class="meta-tag applied">{applied_label}</span>')
        meta_html = " ".join(meta_parts)

        apply_html = ""
        if apply_url:
            apply_html = f'<a href="{apply_url}" class="apply-link" target="_blank">Apply</a>'
        mark_applied_html = ""
        if not is_applied:
            mark_applied_html = (
                f'<button class="mark-applied-btn" data-job-url="{url}" '
                'type="button">Mark as applied</button>'
            )
        tailor_html = ""
        if (
            not is_applied
            and j["full_description"]
            and not j["tailored_resume_path"]
            and j["tailor_attempts"] < 5
        ):
            tailor_html = (
                f'<button class="tailor-job-btn" data-job-url="{url}" '
                'type="button">Tailor</button>'
            )
        artifact_html = ""
        if j["tailored_resume_path"]:
            encoded_url = quote(j["url"] or "", safe="")
            artifact_kinds = [("pdf", "Resume PDF")]
            if Path(j["tailored_resume_path"]).suffix.lower() == ".tex":
                artifact_kinds.append(("tex", "LaTeX"))
            artifact_kinds.append(("report", "Report"))
            artifact_links = "".join(
                f'<a href="/api/jobs/artifact?url={encoded_url}&kind={kind}" '
                f'class="artifact-download" download>{label}</a>'
                for kind, label in artifact_kinds
            )
            artifact_html = (
                '<div class="tailored-artifacts">'
                '<div><div class="tailored-artifacts-title">Tailored resume</div>'
                '<div class="tailored-artifacts-copy">Generated specifically for this job posting.</div></div>'
                f'<div class="tailored-artifact-actions">{artifact_links}</div>'
                '</div>'
            )

        job_sections += f"""
        <div class="job-card" data-score="{score}" data-applied="{str(is_applied).lower()}"
             data-site="{site_value}" data-location="{location.lower()}">
          <div class="card-header">
            <span class="score-pill" style="background:{'#10b981' if score >= 7 else ('#f59e0b' if score >= 5 else '#64748b')}">{"&mdash;" if score == 0 else score}</span>
            <a href="{url}" class="job-title" target="_blank">{title}</a>
            <button class="delete-job-btn" data-job-url="{url}" type="button"
                    title="Delete job" aria-label="Delete job">
              <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" focusable="false">
                <path fill="currentColor" d="M9 3h6l1 2h4v2H4V5h4l1-2zm1 6h2v9h-2V9zm4 0h2v9h-2V9zM7 9h2v9H7V9zm-1 12h12a1 1 0 0 0 1-1V8H5v12a1 1 0 0 0 1 1z"/>
              </svg>
            </button>
          </div>
          <div class="meta-row">{meta_html}</div>
          {f'<div class="keywords-row">{escape(keywords)}</div>' if keywords else ''}
          {f'<div class="reasoning-row">{escape(reasoning)}</div>' if reasoning else ''}
          {f'<div class="import-error">Enrichment failed: {detail_error}</div>' if detail_error else ''}
          <p class="desc-preview">{desc_preview}...</p>
          {"<details class='full-desc-details'><summary class='expand-btn'>Full Description (" + f'{desc_len:,}' + " chars)</summary><div class='full-desc'>" + full_desc_html + "</div></details>" if j["full_description"] else ""}
          {artifact_html}
          <div class="card-footer">{tailor_html}{apply_html}{mark_applied_html}</div>
        </div>"""

    if current_score is not None:
        job_sections += "</div>"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ApplyPilot Dashboard</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; background: #0f172a; color: #e2e8f0; }}
  .app-shell {{ min-height: 100vh; display: flex; }}
  .sidebar {{ position: sticky; top: 0; width: 220px; height: 100vh; flex-shrink: 0; background: #111827; border-right: 1px solid #334155; padding: 1.5rem 1rem; }}
  .sidebar-brand {{ font-size: 1.2rem; font-weight: 750; margin: 0 0.5rem 1.5rem; color: #f8fafc; }}
  .sidebar-nav {{ display: flex; flex-direction: column; gap: 0.4rem; }}
  .nav-btn {{ width: 100%; border: 0; border-radius: 8px; background: transparent; color: #94a3b8; padding: 0.7rem 0.8rem; text-align: left; font-size: 0.9rem; font-weight: 600; cursor: pointer; }}
  .nav-btn:hover {{ color: #e2e8f0; background: #1e293b; }}
  .nav-btn.active {{ color: #bfdbfe; background: #1e3a5f; }}
  .main-content {{ width: calc(100% - 220px); padding: 2rem; }}
  .app-view {{ display: none; }}
  .app-view.active {{ display: block; }}

  h1 {{ font-size: 1.8rem; font-weight: 700; margin-bottom: 0.5rem; }}
  .subtitle {{ color: #94a3b8; margin-bottom: 2rem; }}

  /* External job import */
  .import-panel {{ background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 1.25rem; margin-bottom: 1.5rem; }}
  .import-panel h2 {{ font-size: 1rem; margin-bottom: 0.35rem; }}
  .import-panel p {{ color: #94a3b8; font-size: 0.82rem; margin-bottom: 0.8rem; }}
  .import-form {{ display: flex; gap: 0.65rem; }}
  .import-input {{ flex: 1; min-width: 220px; background: #0f172a; border: 1px solid #475569; color: #e2e8f0; padding: 0.65rem 0.8rem; border-radius: 7px; }}
  .import-button {{ border: 0; border-radius: 7px; padding: 0.65rem 1rem; background: #3b82f6; color: white; font-weight: 600; cursor: pointer; }}
  .import-button:disabled {{ opacity: 0.55; cursor: wait; }}
  .import-status {{ min-height: 1.25rem; margin-top: 0.65rem; font-size: 0.82rem; color: #93c5fd; }}
  .import-status.error {{ color: #fca5a5; }}
  .import-error {{ color: #fca5a5; background: #450a0a55; border-radius: 5px; padding: 0.45rem; margin-bottom: 0.5rem; font-size: 0.75rem; }}
  .discovery-panel {{ display: flex; align-items: center; justify-content: space-between; gap: 1rem; background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 1.25rem; margin-bottom: 1.5rem; }}
  .discovery-panel h2 {{ font-size: 1rem; margin-bottom: 0.35rem; }}
  .discovery-panel p {{ color: #94a3b8; font-size: 0.82rem; }}
  .discovery-actions {{ display: flex; flex-direction: column; align-items: flex-end; min-width: 180px; }}
  .discovery-button {{ border: 0; border-radius: 7px; padding: 0.65rem 1rem; background: #10b981; color: #052e16; font-weight: 700; cursor: pointer; }}
  .discovery-button:disabled {{ opacity: 0.55; cursor: wait; }}
  .tailoring-button {{ border: 0; border-radius: 7px; padding: 0.65rem 1rem; background: #60a5fa; color: #172554; font-weight: 700; cursor: pointer; }}
  .tailoring-button:disabled {{ opacity: 0.55; cursor: wait; }}
  .discovery-status {{ min-height: 1.25rem; margin-top: 0.5rem; font-size: 0.78rem; color: #6ee7b7; text-align: right; }}
  .discovery-status.error {{ color: #fca5a5; }}

  /* Profile and search settings */
  .settings-header {{ margin-bottom: 1.5rem; }}
  .settings-header p {{ color: #94a3b8; margin-top: 0.4rem; }}
  .settings-tabs {{ display: flex; gap: 0.5rem; margin-bottom: 1.25rem; border-bottom: 1px solid #334155; }}
  .settings-tab {{ border: 0; border-bottom: 3px solid transparent; background: transparent; color: #94a3b8; padding: 0.7rem 0.9rem; font-weight: 600; cursor: pointer; }}
  .settings-tab.active {{ color: #60a5fa; border-bottom-color: #60a5fa; }}
  .settings-form {{ display: none; }}
  .settings-form.active {{ display: block; }}
  .settings-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1rem; }}
  .settings-card {{ background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 1.25rem; }}
  .settings-card.wide {{ grid-column: 1 / -1; }}
  .settings-card h2 {{ font-size: 1rem; margin-bottom: 1rem; }}
  .field-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.85rem; }}
  .field {{ display: flex; flex-direction: column; gap: 0.3rem; }}
  .field.wide {{ grid-column: 1 / -1; }}
  .field label {{ color: #cbd5e1; font-size: 0.78rem; font-weight: 600; }}
  .field input, .field textarea, .field select {{ width: 100%; background: #0f172a; border: 1px solid #475569; color: #e2e8f0; padding: 0.55rem 0.65rem; border-radius: 6px; font: inherit; font-size: 0.85rem; }}
  .field textarea {{ min-height: 90px; resize: vertical; }}
  .field small {{ color: #64748b; font-size: 0.72rem; }}
  .check-field {{ display: flex; align-items: center; gap: 0.55rem; color: #cbd5e1; font-size: 0.82rem; margin-bottom: 0.65rem; }}
  .check-field input {{ accent-color: #3b82f6; }}
  .settings-actions {{ position: sticky; bottom: 0; display: flex; align-items: center; justify-content: flex-end; gap: 1rem; background: #0f172ae6; padding: 1rem 0; margin-top: 1rem; backdrop-filter: blur(6px); }}
  .save-settings-btn, .add-row-btn {{ border: 0; border-radius: 7px; background: #3b82f6; color: white; padding: 0.6rem 1rem; font-weight: 650; cursor: pointer; }}
  .save-settings-btn:disabled {{ opacity: 0.55; cursor: wait; }}
  .add-row-btn {{ background: #334155; color: #cbd5e1; font-size: 0.78rem; padding: 0.4rem 0.65rem; }}
  .settings-status {{ color: #6ee7b7; font-size: 0.82rem; }}
  .settings-status.error {{ color: #fca5a5; }}
  .editable-list {{ display: flex; flex-direction: column; gap: 0.6rem; margin-bottom: 0.75rem; }}
  .editable-row {{ display: grid; grid-template-columns: 1fr 110px auto; gap: 0.55rem; align-items: center; }}
  .editable-row.location-row {{ grid-template-columns: 1fr 100px auto; }}
  .editable-row input, .editable-row select {{ background: #0f172a; border: 1px solid #475569; color: #e2e8f0; padding: 0.5rem 0.6rem; border-radius: 6px; }}
  .remove-row-btn {{ border: 0; background: transparent; color: #fca5a5; padding: 0.4rem; cursor: pointer; }}
  .tag-editor {{ display: flex; flex-direction: column; gap: 0.55rem; }}
  .tag-list {{ display: flex; flex-wrap: wrap; gap: 0.4rem; min-height: 1.8rem; }}
  .tag-pill {{ display: inline-flex; align-items: center; gap: 0.4rem; max-width: 100%; background: #1e3a5f; color: #bfdbfe; border: 1px solid #3b82f655; border-radius: 999px; padding: 0.25rem 0.35rem 0.25rem 0.6rem; font-size: 0.78rem; }}
  .tag-pill-text {{ overflow-wrap: anywhere; }}
  .tag-remove-btn {{ display: inline-flex; align-items: center; justify-content: center; width: 1.25rem; height: 1.25rem; flex: 0 0 auto; border: 0; border-radius: 50%; background: transparent; color: #bfdbfe; cursor: pointer; font: inherit; font-size: 1rem; line-height: 1; }}
  .tag-remove-btn:hover, .tag-remove-btn:focus-visible {{ background: #3b82f633; color: white; }}
  .tag-editor-controls {{ display: flex; gap: 0.5rem; }}
  .tag-editor-controls input {{ min-width: 0; flex: 1; }}
  .tag-add-btn {{ border: 0; border-radius: 6px; background: #334155; color: #e2e8f0; padding: 0.5rem 0.8rem; font-weight: 600; cursor: pointer; }}
  .tag-add-btn:hover {{ background: #475569; }}
  .resume-preview {{ min-height: 320px; max-height: 65vh; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; background: #0f172a; border: 1px solid #334155; border-radius: 8px; padding: 1rem; color: #cbd5e1; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.8rem; line-height: 1.55; }}
  .resume-upload-row {{ display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }}
  .resume-file-input {{ flex: 1; min-width: 220px; background: #0f172a; border: 1px solid #475569; color: #cbd5e1; padding: 0.55rem; border-radius: 7px; }}
  .resume-file-input::file-selector-button {{ border: 0; border-radius: 5px; background: #334155; color: #e2e8f0; padding: 0.4rem 0.65rem; margin-right: 0.7rem; cursor: pointer; }}
  .resume-meta {{ color: #94a3b8; font-size: 0.78rem; margin-bottom: 0.75rem; }}
  .resume-render-frame {{ width: 100%; min-height: 720px; border: 1px solid #334155; border-radius: 8px; background: #525659; }}
  .resume-pdf-viewer {{ width: 100%; min-height: 720px; max-height: 80vh; overflow: auto; border: 1px solid #334155; border-radius: 8px; background: #525659; padding: 1rem; display: flex; flex-direction: column; align-items: center; gap: 1rem; }}
  .resume-pdf-page {{ max-width: 100%; background: white; box-shadow: 0 2px 10px #00000066; }}
  .resume-render-error {{ color: #fca5a5; background: #450a0a55; border-radius: 7px; padding: 0.75rem; margin-bottom: 0.75rem; white-space: pre-wrap; }}
  .resume-source-details {{ margin-top: 0.8rem; }}
  .resume-source-details summary {{ color: #93c5fd; cursor: pointer; font-size: 0.82rem; }}
  .settings-loading {{ color: #94a3b8; padding: 2rem 0; }}

  /* Summary cards */
  .summary {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 1rem; margin-bottom: 2.5rem; }}
  .stat-card {{ background: #1e293b; border-radius: 12px; padding: 1.25rem; }}
  .stat-num {{ font-size: 2rem; font-weight: 700; }}
  .stat-label {{ color: #94a3b8; font-size: 0.85rem; margin-top: 0.25rem; }}
  .stat-ok .stat-num {{ color: #10b981; }}
  .stat-scored .stat-num {{ color: #60a5fa; }}
  .stat-high .stat-num {{ color: #f59e0b; }}
  .stat-total .stat-num {{ color: #e2e8f0; }}

  /* Filters */
  .filters {{ background: #1e293b; border-radius: 12px; padding: 1.25rem; margin-bottom: 2rem; display: flex; gap: 1rem; flex-wrap: wrap; align-items: center; }}
  .filter-label {{ color: #94a3b8; font-size: 0.85rem; font-weight: 600; }}
  .filter-btn {{ background: #334155; border: none; color: #94a3b8; padding: 0.4rem 0.8rem; border-radius: 6px; cursor: pointer; font-size: 0.8rem; transition: all 0.15s; }}
  .filter-btn:hover {{ background: #475569; color: #e2e8f0; }}
  .filter-btn.active {{ background: #60a5fa; color: #0f172a; font-weight: 600; }}
  .search-input {{ background: #334155; border: 1px solid #475569; color: #e2e8f0; padding: 0.4rem 0.8rem; border-radius: 6px; font-size: 0.8rem; width: 200px; }}
  .search-input::placeholder {{ color: #64748b; }}
  .source-select {{ background: #334155; border: 1px solid #475569; color: #e2e8f0; padding: 0.4rem 0.8rem; border-radius: 6px; font-size: 0.8rem; max-width: 220px; }}

  /* Score distribution */
  .score-section {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-bottom: 2.5rem; }}
  .score-dist {{ background: #1e293b; border-radius: 12px; padding: 1.5rem; }}
  .score-dist h3 {{ font-size: 1rem; margin-bottom: 1rem; color: #94a3b8; }}
  .score-row {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.4rem; }}
  .score-label {{ width: 1.5rem; text-align: right; font-size: 0.85rem; font-weight: 600; }}
  .score-bar-track {{ flex: 1; height: 14px; background: #334155; border-radius: 4px; overflow: hidden; }}
  .score-bar-fill {{ height: 100%; border-radius: 4px; transition: width 0.3s; }}
  .score-count {{ width: 2.5rem; font-size: 0.8rem; color: #94a3b8; }}

  /* Site bars */
  .sites-section {{ background: #1e293b; border-radius: 12px; padding: 1.5rem; }}
  .sites-section h3 {{ font-size: 1rem; margin-bottom: 1rem; color: #94a3b8; }}
  .site-row {{ margin-bottom: 0.8rem; }}
  .site-name {{ font-weight: 600; font-size: 0.9rem; }}
  .site-nums {{ color: #94a3b8; font-size: 0.75rem; margin: 0.15rem 0; }}
  .bar-track {{ height: 8px; background: #334155; border-radius: 4px; display: flex; overflow: hidden; }}
  .bar-fill {{ height: 100%; transition: width 0.3s; }}

  /* Score group headers */
  .score-header {{ font-size: 1.2rem; font-weight: 600; margin: 2.5rem 0 1rem; padding-bottom: 0.5rem; border-bottom: 3px solid; display: flex; align-items: center; gap: 0.75rem; }}
  .score-badge {{ display: inline-flex; align-items: center; justify-content: center; width: 2rem; height: 2rem; border-radius: 8px; color: #0f172a; font-weight: 700; font-size: 1rem; }}

  /* Job grid */
  .job-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 1rem; }}

  .job-card {{ background: #1e293b; border-radius: 10px; padding: 1rem; border-left: 3px solid #334155; transition: all 0.15s; }}
  .job-card:hover {{ transform: translateY(-2px); box-shadow: 0 4px 12px #00000044; }}
  .job-card[data-score="9"], .job-card[data-score="10"] {{ border-left-color: #10b981; }}
  .job-card[data-score="8"] {{ border-left-color: #34d399; }}
  .job-card[data-score="7"] {{ border-left-color: #60a5fa; }}
  .job-card[data-score="6"] {{ border-left-color: #f59e0b; }}
  .job-card[data-score="5"] {{ border-left-color: #f59e0b88; }}

  .card-header {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.5rem; }}
  .score-pill {{ display: inline-flex; align-items: center; justify-content: center; min-width: 1.6rem; height: 1.6rem; border-radius: 6px; color: #0f172a; font-weight: 700; font-size: 0.8rem; flex-shrink: 0; }}

  .job-title {{ color: #e2e8f0; text-decoration: none; font-weight: 600; font-size: 0.95rem; min-width: 0; }}
  .job-title:hover {{ color: #60a5fa; }}
  .delete-job-btn {{ margin-left: auto; flex-shrink: 0; display: inline-flex; align-items: center; justify-content: center; width: 1.75rem; height: 1.75rem; border: 0; border-radius: 6px; background: transparent; color: #64748b; cursor: pointer; }}
  .delete-job-btn:hover {{ color: #fca5a5; background: #450a0a55; }}
  .delete-job-btn:disabled {{ opacity: 0.55; cursor: wait; }}

  .meta-row {{ display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 0.4rem; }}
  .meta-tag {{ font-size: 0.72rem; padding: 0.15rem 0.5rem; border-radius: 4px; background: #334155; color: #94a3b8; }}
  .meta-tag.priority {{ background: #3b0764; color: #d8b4fe; font-weight: 600; }}
  .meta-tag.salary {{ background: #064e3b; color: #6ee7b7; }}
  .meta-tag.location {{ background: #1e3a5f; color: #93c5fd; }}
  .meta-tag.posted {{ background: #422006; color: #fde68a; }}
  .meta-tag.applied {{ background: #064e3b; color: #6ee7b7; font-weight: 600; }}

  .keywords-row {{ font-size: 0.75rem; color: #10b981; margin-bottom: 0.3rem; line-height: 1.4; }}
  .reasoning-row {{ font-size: 0.75rem; color: #94a3b8; margin-bottom: 0.5rem; font-style: italic; line-height: 1.4; }}

  .desc-preview {{ font-size: 0.8rem; color: #64748b; line-height: 1.5; margin-bottom: 0.75rem; max-height: 3.6em; overflow: hidden; }}

  .card-footer {{ display: flex; justify-content: flex-end; gap: 0.5rem; }}
  .tailored-artifacts {{ display: flex; align-items: center; justify-content: space-between; gap: 0.8rem; margin: 0.8rem 0; padding: 0.75rem; border: 1px solid #2563eb55; border-radius: 8px; background: #17255466; }}
  .tailored-artifacts-title {{ color: #bfdbfe; font-size: 0.82rem; font-weight: 700; }}
  .tailored-artifacts-copy {{ color: #94a3b8; font-size: 0.72rem; margin-top: 0.15rem; }}
  .tailored-artifact-actions {{ display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 0.4rem; }}
  .artifact-download {{ color: #bfdbfe; font-size: 0.76rem; font-weight: 600; text-decoration: none; padding: 0.35rem 0.6rem; border: 1px solid #60a5fa66; border-radius: 6px; background: #1e3a8a44; }}
  .artifact-download:hover {{ background: #1d4ed866; }}
  .apply-link {{ font-size: 0.8rem; color: #60a5fa; text-decoration: none; padding: 0.3rem 0.8rem; border: 1px solid #60a5fa33; border-radius: 6px; font-weight: 500; }}
  .apply-link:hover {{ background: #60a5fa22; }}
  .mark-applied-btn {{ font-size: 0.8rem; color: #6ee7b7; background: transparent; padding: 0.3rem 0.8rem; border: 1px solid #10b98155; border-radius: 6px; font-weight: 500; cursor: pointer; }}
  .mark-applied-btn:hover {{ background: #10b98122; }}
  .mark-applied-btn:disabled {{ opacity: 0.55; cursor: wait; }}
  .tailor-job-btn {{ font-size: 0.8rem; color: #bfdbfe; background: #1d4ed833; padding: 0.3rem 0.8rem; border: 1px solid #60a5fa66; border-radius: 6px; font-weight: 650; cursor: pointer; }}
  .tailor-job-btn:hover {{ background: #1d4ed866; }}
  .tailor-job-btn:disabled {{ opacity: 0.55; cursor: wait; }}

  /* Active/applied tabs */
  .tabs {{ display: flex; gap: 0.5rem; margin-bottom: 1.5rem; border-bottom: 1px solid #334155; }}
  .tab-btn {{ border: 0; border-bottom: 3px solid transparent; background: transparent; color: #94a3b8; padding: 0.75rem 1rem; font-size: 0.95rem; font-weight: 600; cursor: pointer; }}
  .tab-btn:hover {{ color: #e2e8f0; }}
  .tab-btn.active {{ color: #60a5fa; border-bottom-color: #60a5fa; }}

  /* Expandable full description */
  .full-desc-details {{ margin-bottom: 0.75rem; }}
  .expand-btn {{ font-size: 0.8rem; color: #60a5fa; cursor: pointer; list-style: none; padding: 0.3rem 0; }}
  .expand-btn::-webkit-details-marker {{ display: none; }}
  .expand-btn:hover {{ color: #93c5fd; }}
  .full-desc {{ font-size: 0.8rem; color: #cbd5e1; line-height: 1.6; margin-top: 0.5rem; padding: 0.75rem; background: #0f172a; border-radius: 8px; max-height: 400px; overflow-y: auto; white-space: pre-wrap; word-break: break-word; }}

  .hidden {{ display: none !important; }}
  .job-count {{ color: #94a3b8; font-size: 0.85rem; margin-bottom: 1rem; }}

  @media (max-width: 768px) {{
    .app-shell {{ display: block; }}
    .sidebar {{ position: sticky; z-index: 10; width: 100%; height: auto; padding: 0.75rem 1rem; border-right: 0; border-bottom: 1px solid #334155; }}
    .sidebar-brand {{ margin: 0 0 0.65rem; }}
    .sidebar-nav {{ flex-direction: row; }}
    .nav-btn {{ width: auto; }}
    .main-content {{ width: 100%; padding: 1rem; }}
    .summary {{ grid-template-columns: repeat(2, 1fr); }}
    .score-section {{ grid-template-columns: 1fr; }}
    .job-grid {{ grid-template-columns: 1fr; }}
    .import-form {{ flex-direction: column; }}
    .discovery-panel {{ align-items: stretch; flex-direction: column; }}
    .discovery-actions {{ align-items: stretch; }}
    .discovery-status {{ text-align: left; }}
    .settings-grid, .field-grid {{ grid-template-columns: 1fr; }}
    .settings-card.wide, .field.wide {{ grid-column: auto; }}
    .editable-row, .editable-row.location-row {{ grid-template-columns: 1fr; }}
    .tag-editor-controls {{ align-items: stretch; }}
  }}
</style>
</head>
<body>

<div class="app-shell">
<aside class="sidebar">
  <div class="sidebar-brand">ApplyPilot</div>
  <nav class="sidebar-nav" aria-label="Application">
    <button class="nav-btn active" data-view="dashboard" onclick="switchView('dashboard')">Dashboard</button>
    <button class="nav-btn" data-view="profile" onclick="switchView('profile')">Profile</button>
  </nav>
</aside>
<main class="main-content">
<section id="dashboard-view" class="app-view active">

<h1>ApplyPilot Dashboard</h1>
<p class="subtitle">{active_count} active jobs &middot; {applied_count} applied &middot; {priority_count} early-career priorities &middot; {high_fit} strong matches (7+)</p>

<section class="import-panel">
  <h2>Add a job from the web</h2>
  <p>Paste a public job-posting URL. It will appear immediately while ApplyPilot fetches its details.</p>
  <form id="job-import-form" class="import-form">
    <input id="job-url" class="import-input" type="url" name="url" required
           placeholder="https://company.com/careers/job..." autocomplete="url">
    <button id="job-import-button" class="import-button" type="submit">Add Job</button>
  </form>
  <div id="import-status" class="import-status" role="status"></div>
</section>

<section class="discovery-panel">
  <div>
    <h2>Discover new jobs</h2>
    <p>Crawl configured career sites, then enrich and score new jobs with your LLM.</p>
  </div>
  <div class="discovery-actions">
    <button id="discovery-button" class="discovery-button" type="button">
      Run Discovery
    </button>
    <div id="discovery-status" class="discovery-status" role="status"></div>
  </div>
</section>

<section class="discovery-panel">
  <div>
    <h2>Tailor resumes</h2>
    <p>Generate a job-specific LaTeX resume and PDF for up to 20 eligible jobs. {pending_tailoring} currently eligible.</p>
  </div>
  <div class="discovery-actions">
    <button id="tailoring-button" class="tailoring-button" type="button">
      Run Tailoring
    </button>
    <div id="tailoring-status" class="discovery-status" role="status"></div>
  </div>
</section>

<div class="summary">
  <div class="stat-card stat-total"><div class="stat-num">{total}</div><div class="stat-label">Total Jobs</div></div>
  <div class="stat-card stat-ok"><div class="stat-num">{ready}</div><div class="stat-label">Ready (desc + URL)</div></div>
  <div class="stat-card stat-scored"><div class="stat-num">{scored}</div><div class="stat-label">Scored by LLM</div></div>
  <div class="stat-card stat-high"><div class="stat-num">{high_fit}</div><div class="stat-label">Strong Fit (7+)</div></div>
</div>

<div class="filters">
  <span class="filter-label">Score:</span>
  <button class="filter-btn active" onclick="filterScore(0, this)">All Jobs</button>
  <button class="filter-btn" onclick="filterScore(7, this)">7+ Strong</button>
  <button class="filter-btn" onclick="filterScore(8, this)">8+ Excellent</button>
  <button class="filter-btn" onclick="filterScore(9, this)">9+ Perfect</button>
  <span class="filter-label" style="margin-left:1rem">Source:</span>
  <select class="source-select" onchange="filterSource(this.value)">
    <option value="">All Sources</option>
    {source_options}
  </select>
  <span class="filter-label" style="margin-left:1rem">Search:</span>
  <input type="text" class="search-input" placeholder="Filter by title, site..." oninput="filterText(this.value)">
</div>

<div class="score-section">
  <div class="score-dist">
    <h3>Score Distribution</h3>
    {score_bars}
  </div>
  <div class="sites-section">
    <h3>By Source</h3>
    {site_rows}
  </div>
</div>

<nav class="tabs" aria-label="Job status">
  <button class="tab-btn active" data-tab="active" onclick="switchTab('active')">
    Active postings ({active_count})
  </button>
  <button class="tab-btn" data-tab="applied" onclick="switchTab('applied')">
    Applied ({applied_count})
  </button>
</nav>

<div id="job-count" class="job-count"></div>

{job_sections}

</section>

<section id="profile-view" class="app-view">
  <div class="settings-header">
    <h1>Profile and Preferences</h1>
    <p>Manage the information ApplyPilot uses for matching, tailoring, and discovery.</p>
  </div>
  <div class="settings-tabs">
    <button class="settings-tab active" data-settings-tab="profile"
            onclick="switchSettingsTab('profile')">Personal Profile</button>
    <button class="settings-tab" data-settings-tab="searches"
            onclick="switchSettingsTab('searches')">Job Preferences</button>
    <button class="settings-tab" data-settings-tab="resume"
            onclick="switchSettingsTab('resume')">Resume</button>
  </div>
  <div id="settings-loading" class="settings-loading">Loading settings...</div>

  <form id="profile-settings-form" class="settings-form">
    <div class="settings-grid">
      <section class="settings-card wide">
        <h2>Personal Information</h2>
        <div class="field-grid">
          <div class="field"><label>Full name</label><input data-profile-path="personal.full_name"></div>
          <div class="field"><label>Preferred name</label><input data-profile-path="personal.preferred_name"></div>
          <div class="field"><label>Email</label><input type="email" data-profile-path="personal.email"></div>
          <div class="field"><label>Phone</label><input data-profile-path="personal.phone"></div>
          <div class="field"><label>City</label><input data-profile-path="personal.city"></div>
          <div class="field"><label>Province or state</label><input data-profile-path="personal.province_state"></div>
          <div class="field"><label>Country</label><input data-profile-path="personal.country"></div>
          <div class="field"><label>Postal or ZIP code</label><input data-profile-path="personal.postal_code"></div>
          <div class="field wide"><label>Street address</label><input data-profile-path="personal.address"></div>
          <div class="field"><label>LinkedIn URL</label><input type="url" data-profile-path="personal.linkedin_url"></div>
          <div class="field"><label>GitHub URL</label><input type="url" data-profile-path="personal.github_url"></div>
          <div class="field"><label>Portfolio URL</label><input type="url" data-profile-path="personal.portfolio_url"></div>
          <div class="field"><label>Website URL</label><input type="url" data-profile-path="personal.website_url"></div>
          <div class="field wide">
            <label>Job-site password</label>
            <input type="password" autocomplete="new-password" data-profile-password>
            <small id="password-help">Leave blank to keep the existing password.</small>
          </div>
        </div>
      </section>

      <section class="settings-card">
        <h2>Work Authorization</h2>
        <label class="check-field"><input type="checkbox" data-profile-path="work_authorization.legally_authorized_to_work"> Legally authorized to work</label>
        <label class="check-field"><input type="checkbox" data-profile-path="work_authorization.require_sponsorship"> Requires sponsorship</label>
        <div class="field"><label>Work permit type</label>
          <select data-profile-path="work_authorization.work_permit_type">
            <option value="">Not applicable</option>
            <option>Citizen</option>
            <option>Permanent Resident</option>
            <option>Open Work Permit</option>
            <option>Employer-Specific Work Permit</option>
            <option>Other</option>
          </select>
        </div>
      </section>

      <section class="settings-card">
        <h2>Compensation</h2>
        <div class="field-grid">
          <div class="field"><label>Salary expectation</label><input type="number" min="0" step="any" data-profile-number="compensation.salary_expectation"></div>
          <div class="field"><label>Currency</label>
            <select data-profile-path="compensation.salary_currency">
              <option>CAD</option>
              <option>USD</option>
              <option>EUR</option>
              <option>GBP</option>
              <option>AUD</option>
            </select>
          </div>
          <div class="field"><label>Range minimum</label><input type="number" min="0" step="any" data-profile-number="compensation.salary_range_min"></div>
          <div class="field"><label>Range maximum</label><input type="number" min="0" step="any" data-profile-number="compensation.salary_range_max"></div>
        </div>
      </section>

      <section class="settings-card">
        <h2>Experience</h2>
        <div class="field-grid">
          <div class="field"><label>Years of experience</label><input type="number" min="0" step="any" data-profile-number="experience.years_of_experience_total"></div>
          <div class="field"><label>Education level</label>
            <select data-profile-path="experience.education_level">
              <option value="">Not specified</option>
              <option>High School</option>
              <option>Associate Degree</option>
              <option>Bachelor's</option>
              <option>Master's</option>
              <option>PhD</option>
              <option>Self-taught</option>
              <option>Other</option>
            </select>
          </div>
          <div class="field"><label>Current title</label><input data-profile-path="experience.current_title"></div>
          <div class="field"><label>Target role</label><input data-profile-path="experience.target_role"></div>
        </div>
      </section>

      <section class="settings-card">
        <h2>Availability</h2>
        <div class="field"><label>Earliest start date</label><input data-profile-path="availability.earliest_start_date"></div>
      </section>

      <section class="settings-card wide">
        <h2>Skills</h2>
        <div class="field-grid">
          <div class="field">
            <label for="programming-languages-input">Programming languages</label>
            <div class="tag-editor" data-tag-editor data-tag-scope="profile" data-tag-path="skills_boundary.programming_languages">
              <div class="tag-list" data-tag-list aria-live="polite"></div>
              <div class="tag-editor-controls">
                <input id="programming-languages-input" data-tag-input autocomplete="off">
                <button class="tag-add-btn" data-tag-add type="button" aria-label="Add programming language">Add</button>
              </div>
            </div>
          </div>
          <div class="field">
            <label for="frameworks-input">Frameworks and libraries</label>
            <div class="tag-editor" data-tag-editor data-tag-scope="profile" data-tag-path="skills_boundary.frameworks">
              <div class="tag-list" data-tag-list aria-live="polite"></div>
              <div class="tag-editor-controls">
                <input id="frameworks-input" data-tag-input autocomplete="off">
                <button class="tag-add-btn" data-tag-add type="button" aria-label="Add framework or library">Add</button>
              </div>
            </div>
          </div>
          <div class="field wide">
            <label for="tools-input">Tools and platforms</label>
            <div class="tag-editor" data-tag-editor data-tag-scope="profile" data-tag-path="skills_boundary.tools">
              <div class="tag-list" data-tag-list aria-live="polite"></div>
              <div class="tag-editor-controls">
                <input id="tools-input" data-tag-input autocomplete="off">
                <button class="tag-add-btn" data-tag-add type="button" aria-label="Add tool or platform">Add</button>
              </div>
            </div>
          </div>
        </div>
      </section>

      <section class="settings-card">
        <h2>Resume Facts</h2>
        <div class="field">
          <label for="preserved-companies-input">Companies to preserve</label>
          <div class="tag-editor" data-tag-editor data-tag-scope="profile" data-tag-path="resume_facts.preserved_companies">
            <div class="tag-list" data-tag-list aria-live="polite"></div>
            <div class="tag-editor-controls">
              <input id="preserved-companies-input" data-tag-input autocomplete="off">
              <button class="tag-add-btn" data-tag-add type="button" aria-label="Add company to preserve">Add</button>
            </div>
          </div>
        </div>
        <div class="field">
          <label for="preserved-projects-input">Projects to preserve</label>
          <div class="tag-editor" data-tag-editor data-tag-scope="profile" data-tag-path="resume_facts.preserved_projects">
            <div class="tag-list" data-tag-list aria-live="polite"></div>
            <div class="tag-editor-controls">
              <input id="preserved-projects-input" data-tag-input autocomplete="off">
              <button class="tag-add-btn" data-tag-add type="button" aria-label="Add project to preserve">Add</button>
            </div>
          </div>
        </div>
        <div class="field"><label>School to preserve</label><input data-profile-path="resume_facts.preserved_school"></div>
        <div class="field">
          <label for="real-metrics-input">Real metrics</label>
          <div class="tag-editor" data-tag-editor data-tag-scope="profile" data-tag-path="resume_facts.real_metrics">
            <div class="tag-list" data-tag-list aria-live="polite"></div>
            <div class="tag-editor-controls">
              <input id="real-metrics-input" data-tag-input autocomplete="off">
              <button class="tag-add-btn" data-tag-add type="button" aria-label="Add real metric">Add</button>
            </div>
          </div>
        </div>
      </section>

      <section class="settings-card">
        <h2>Voluntary EEO Information</h2>
        <div class="field"><label>Gender</label>
          <select data-profile-path="eeo_voluntary.gender">
            <option>Decline to self-identify</option>
            <option>Woman</option>
            <option>Man</option>
            <option>Non-binary</option>
            <option>Other</option>
          </select>
        </div>
        <div class="field"><label>Race or ethnicity</label>
          <select data-profile-path="eeo_voluntary.race_ethnicity">
            <option>Decline to self-identify</option>
            <option>American Indian or Alaska Native</option>
            <option>Asian</option>
            <option>Black or African American</option>
            <option>Hispanic or Latino</option>
            <option>Native Hawaiian or Other Pacific Islander</option>
            <option>White</option>
            <option>Two or More Races</option>
          </select>
        </div>
        <div class="field"><label>Veteran status</label>
          <select data-profile-path="eeo_voluntary.veteran_status">
            <option>Decline to self-identify</option>
            <option>I am a protected veteran</option>
            <option>I am not a protected veteran</option>
          </select>
        </div>
        <div class="field"><label>Disability status</label>
          <select data-profile-path="eeo_voluntary.disability_status">
            <option>Decline to self-identify</option>
            <option>Yes, I have a disability</option>
            <option>No, I do not have a disability</option>
          </select>
        </div>
      </section>
    </div>
    <div class="settings-actions">
      <span id="profile-settings-status" class="settings-status" role="status"></span>
      <button class="save-settings-btn" type="submit">Save Profile</button>
    </div>
  </form>

  <form id="search-settings-form" class="settings-form">
    <div class="settings-grid">
      <section class="settings-card wide">
        <h2>Search Defaults</h2>
        <div class="field-grid">
          <div class="field"><label>Default location</label><input data-search-path="defaults.location"></div>
          <div class="field"><label>Distance</label><input type="number" min="0" step="any" data-search-number="defaults.distance"></div>
          <div class="field"><label>Maximum posting age (hours)</label><input type="number" min="0" step="any" data-search-number="defaults.hours_old"></div>
          <div class="field"><label>Results per site</label><input type="number" min="0" step="any" data-search-number="defaults.results_per_site"></div>
        </div>
      </section>

      <section class="settings-card">
        <h2>Discovery Filters</h2>
        <label class="check-field"><input type="checkbox" data-search-check="greenhouse_location_filter"> Filter Greenhouse locations</label>
        <label class="check-field"><input type="checkbox" data-search-check="bigtech_location_filter"> Filter big-tech locations</label>
        <label class="check-field"><input type="checkbox" data-search-check="accept_remote_anywhere"> Accept remote jobs anywhere</label>
        <label class="check-field"><input type="checkbox" data-search-check="accept_unknown_locations"> Accept unknown locations</label>
      </section>

      <section class="settings-card">
        <h2>Allowed Countries</h2>
        <div class="field">
          <label for="allowed-countries-input">Countries</label>
          <div class="tag-editor" data-tag-editor data-tag-scope="searches" data-tag-path="allowed_countries">
            <div class="tag-list" data-tag-list aria-live="polite"></div>
            <div class="tag-editor-controls">
              <input id="allowed-countries-input" data-tag-input autocomplete="off">
              <button class="tag-add-btn" data-tag-add type="button" aria-label="Add allowed country">Add</button>
            </div>
          </div>
        </div>
        <div class="field">
          <label for="accepted-locations-input">Accepted location terms</label>
          <div class="tag-editor" data-tag-editor data-tag-scope="searches" data-tag-path="location_accept">
            <div class="tag-list" data-tag-list aria-live="polite"></div>
            <div class="tag-editor-controls">
              <input id="accepted-locations-input" data-tag-input autocomplete="off">
              <button class="tag-add-btn" data-tag-add type="button" aria-label="Add accepted location term">Add</button>
            </div>
          </div>
        </div>
        <div class="field">
          <label for="rejected-locations-input">Rejected location terms</label>
          <div class="tag-editor" data-tag-editor data-tag-scope="searches" data-tag-path="location_reject_non_remote">
            <div class="tag-list" data-tag-list aria-live="polite"></div>
            <div class="tag-editor-controls">
              <input id="rejected-locations-input" data-tag-input autocomplete="off">
              <button class="tag-add-btn" data-tag-add type="button" aria-label="Add rejected location term">Add</button>
            </div>
          </div>
        </div>
      </section>

      <section class="settings-card wide">
        <h2>Search Queries</h2>
        <div id="query-list" class="editable-list"></div>
        <button id="add-query-button" class="add-row-btn" type="button">Add Query</button>
      </section>

      <section class="settings-card wide">
        <h2>Search Locations</h2>
        <div id="location-list" class="editable-list"></div>
        <button id="add-location-button" class="add-row-btn" type="button">Add Location</button>
      </section>

      <section class="settings-card">
        <h2>Title Preferences</h2>
        <div class="field">
          <label for="priority-titles-input">Priority titles</label>
          <div class="tag-editor" data-tag-editor data-tag-scope="searches" data-tag-path="priority_titles">
            <div class="tag-list" data-tag-list aria-live="polite"></div>
            <div class="tag-editor-controls">
              <input id="priority-titles-input" data-tag-input autocomplete="off">
              <button class="tag-add-btn" data-tag-add type="button" aria-label="Add priority title">Add</button>
            </div>
          </div>
        </div>
        <div class="field">
          <label for="exclude-titles-input">Excluded titles</label>
          <div class="tag-editor" data-tag-editor data-tag-scope="searches" data-tag-path="exclude_titles">
            <div class="tag-list" data-tag-list aria-live="polite"></div>
            <div class="tag-editor-controls">
              <input id="exclude-titles-input" data-tag-input autocomplete="off">
              <button class="tag-add-btn" data-tag-add type="button" aria-label="Add excluded title">Add</button>
            </div>
          </div>
        </div>
      </section>

      <section class="settings-card">
        <h2>Job Boards</h2>
        <div class="field"><label>Boards</label><textarea data-search-list="boards"></textarea><small>One item per line; leave blank when using direct-employer discovery only.</small></div>
        <div class="field"><label>Country code or name</label><input data-search-path="country"></div>
      </section>
    </div>
    <div class="settings-actions">
      <span id="search-settings-status" class="settings-status" role="status"></span>
      <button class="save-settings-btn" type="submit">Save Preferences</button>
    </div>
  </form>

  <div id="resume-settings-form" class="settings-form">
    <div class="settings-grid">
      <section class="settings-card wide">
        <h2>Plain-Text Resume</h2>
        <div id="resume-meta" class="resume-meta">Loading resume...</div>
        <pre id="resume-preview" class="resume-preview">Loading resume...</pre>
      </section>
      <section class="settings-card wide">
        <h2>Upload a New Resume</h2>
        <p class="resume-meta">Choose a UTF-8 plain-text file. The new file replaces the current resume.txt.</p>
        <form id="text-resume-upload-form" class="resume-upload-row">
          <input id="resume-file" class="resume-file-input" type="file"
                 accept=".txt,text/plain" required>
          <button class="save-settings-btn" type="submit">Upload Text Resume</button>
        </form>
        <div id="resume-settings-status" class="settings-status" role="status"></div>
      </section>

      <section class="settings-card wide">
        <h2>LaTeX Resume</h2>
        <div id="latex-resume-meta" class="resume-meta">Loading LaTeX resume...</div>
        <div id="latex-render-error" class="resume-render-error hidden"></div>
        <div id="latex-resume-preview" class="resume-pdf-viewer"
             aria-label="Compiled LaTeX resume preview"></div>
        <details class="resume-source-details">
          <summary>View LaTeX source</summary>
          <pre id="latex-resume-source" class="resume-preview"></pre>
        </details>
      </section>

      <section class="settings-card wide">
        <h2>Upload a New LaTeX Resume</h2>
        <p class="resume-meta">Choose a UTF-8 .tex file. ApplyPilot compiles it with Tectonic and previews the PDF with PDF.js. Failed compiles keep your previous PDF.</p>
        <form id="latex-resume-upload-form" class="resume-upload-row">
          <input id="latex-resume-file" class="resume-file-input" type="file"
                 accept=".tex,text/x-tex,application/x-tex" required>
          <button class="save-settings-btn" type="submit">Upload LaTeX Resume</button>
        </form>
        <div id="latex-resume-status" class="settings-status" role="status"></div>
      </section>
    </div>
  </div>
</section>

</main>
</div>

<script>
let minScore = 0;
let searchText = '';
let selectedSource = '';
let currentTab = window.location.hash === '#applied' ? 'applied' : 'active';
const importForm = document.getElementById('job-import-form');
const importInput = document.getElementById('job-url');
const importButton = document.getElementById('job-import-button');
const importStatus = document.getElementById('import-status');
const discoveryButton = document.getElementById('discovery-button');
const discoveryStatus = document.getElementById('discovery-status');
const tailoringButton = document.getElementById('tailoring-button');
const tailoringStatus = document.getElementById('tailoring-status');
let tailoringPollTimer = null;

function setImportStatus(message, isError = false) {{
  importStatus.textContent = message;
  importStatus.classList.toggle('error', isError);
}}

importForm.addEventListener('submit', async event => {{
  event.preventDefault();
  importButton.disabled = true;
  setImportStatus('Adding job...');
  try {{
    const response = await fetch('/api/jobs', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{url: importInput.value}})
    }});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Could not add job');
    if (result.created || result.status === 'pending') {{
      sessionStorage.setItem('applypilotPendingImport', result.url);
      window.location.reload();
      return;
    }}
    setImportStatus(result.message || 'This job is already in the dashboard.');
  }} catch (error) {{
    const hint = window.location.protocol === 'file:'
      ? ' Run `applypilot dashboard` and use the localhost page.'
      : '';
    setImportStatus(error.message + hint, true);
  }} finally {{
    importButton.disabled = false;
  }}
}});

async function pollPendingImport() {{
  const url = sessionStorage.getItem('applypilotPendingImport');
  if (!url) return;
  setImportStatus('Job added. Fetching title, company, location, and description...');
  try {{
    const response = await fetch('/api/jobs/status?url=' + encodeURIComponent(url));
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Could not check import');
    if (result.status === 'pending') {{
      window.setTimeout(pollPendingImport, 1500);
      return;
    }}
    sessionStorage.removeItem('applypilotPendingImport');
    if (result.status === 'error') {{
      setImportStatus('Job added, but enrichment failed: ' + (result.error || 'unknown error'), true);
      window.setTimeout(() => window.location.reload(), 2500);
      return;
    }}
    setImportStatus('Job details fetched. Refreshing...');
    window.setTimeout(() => window.location.reload(), 500);
  }} catch (error) {{
    sessionStorage.removeItem('applypilotPendingImport');
    setImportStatus(error.message, true);
  }}
}}

pollPendingImport();

function renderDiscoveryStatus(state) {{
  discoveryStatus.classList.toggle('error', state.status === 'error');
  if (state.status === 'running') {{
    discoveryButton.disabled = true;
    discoveryButton.textContent = 'Discovery running...';
    discoveryStatus.textContent = 'Searching career sites, then enriching and scoring. You can keep using the dashboard.';
    window.setTimeout(refreshDiscoveryStatus, 2000);
    return;
  }}

  discoveryButton.disabled = false;
  discoveryButton.textContent = 'Run Discovery';
  if (state.status === 'complete') {{
    const result = state.result || {{}};
    const scoredNote = result.scored ? ' Jobs were scored.' : '';
    discoveryStatus.textContent =
      `Finished: ${{result.new || 0}} new, ${{result.existing || 0}} existing jobs.${{scoredNote}}`;
    if (sessionStorage.getItem('applypilotDiscoveryRunning')) {{
      sessionStorage.removeItem('applypilotDiscoveryRunning');
      window.setTimeout(() => window.location.reload(), 1000);
    }}
  }} else if (state.status === 'error') {{
    sessionStorage.removeItem('applypilotDiscoveryRunning');
    discoveryStatus.textContent = 'Discovery failed: ' + (state.error || 'unknown error');
  }} else {{
    discoveryStatus.textContent = '';
  }}
}}

async function refreshDiscoveryStatus() {{
  try {{
    const response = await fetch('/api/discovery/status');
    const state = await response.json();
    if (!response.ok) throw new Error(state.error || 'Could not check discovery');
    renderDiscoveryStatus(state);
  }} catch (error) {{
    discoveryButton.disabled = false;
    discoveryStatus.classList.add('error');
    discoveryStatus.textContent = error.message;
  }}
}}

discoveryButton.addEventListener('click', async () => {{
  discoveryButton.disabled = true;
  discoveryStatus.textContent = 'Starting discovery...';
  try {{
    const response = await fetch('/api/discovery', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{workers: 4}})
    }});
    const state = await response.json();
    if (!response.ok) throw new Error(state.error || 'Could not start discovery');
    sessionStorage.setItem('applypilotDiscoveryRunning', 'true');
    renderDiscoveryStatus(state);
  }} catch (error) {{
    discoveryButton.disabled = false;
    discoveryStatus.classList.add('error');
    discoveryStatus.textContent = error.message;
  }}
}});

refreshDiscoveryStatus();

function scheduleTailoringRefresh() {{
  if (tailoringPollTimer !== null) window.clearTimeout(tailoringPollTimer);
  tailoringPollTimer = window.setTimeout(() => {{
    tailoringPollTimer = null;
    refreshTailoringStatus();
  }}, 2000);
}}

function renderTailoringStatus(state) {{
  const current = state.current || null;
  const queued = Array.isArray(state.queued) ? state.queued : [];
  const recent = Array.isArray(state.recent) ? state.recent : [];
  const bulkOutstanding =
    (current && current.kind === 'batch') || queued.some(request => request.kind === 'batch');
  const jobButtons = document.querySelectorAll('.tailor-job-btn');
  if (!current && !queued.length && tailoringPollTimer !== null) {{
    window.clearTimeout(tailoringPollTimer);
    tailoringPollTimer = null;
  }}
  tailoringStatus.classList.remove('error');

  tailoringButton.disabled = Boolean(bulkOutstanding);
  tailoringButton.textContent = bulkOutstanding ? 'Bulk tailoring queued...' : 'Run Tailoring';
  jobButtons.forEach(button => {{
    const active = current && current.target_url === button.dataset.jobUrl;
    const pending = queued.find(request => request.target_url === button.dataset.jobUrl);
    if (active) {{
      button.disabled = true;
      button.textContent = 'Tailoring...';
    }} else if (pending) {{
      button.disabled = true;
      button.textContent = `Queued (#${{pending.queue_position}})`;
    }} else {{
      button.disabled = false;
      button.textContent = 'Tailor';
    }}
  }});

  if (current || queued.length) {{
    const waiting = queued.length;
    if (current && current.kind === 'job') {{
      tailoringStatus.textContent =
        `Tailoring the selected job; ${{waiting}} request${{waiting === 1 ? '' : 's'}} queued.`;
    }} else if (current) {{
      tailoringStatus.textContent =
        `Running bulk tailoring; ${{waiting}} request${{waiting === 1 ? '' : 's'}} queued.`;
    }} else {{
      tailoringStatus.textContent =
        `${{waiting}} tailoring request${{waiting === 1 ? '' : 's'}} queued.`;
    }}
    scheduleTailoringRefresh();
    return;
  }}

  const latest = recent[0] || null;
  if (latest && latest.status === 'complete') {{
    const result = latest.result || {{}};
    tailoringStatus.textContent =
      `Finished: ${{result.approved || 0}} tailored, ${{result.failed || 0}} failed, ${{result.errors || 0}} errors.`;
  }} else if (latest && latest.status === 'skipped') {{
    tailoringStatus.textContent =
      'Skipped queued tailoring: ' + ((latest.result || {{}}).reason || 'job is no longer eligible');
  }} else if (latest && latest.status === 'error') {{
    tailoringStatus.classList.add('error');
    tailoringStatus.textContent = 'Tailoring failed: ' + (latest.error || 'unknown error');
  }} else {{
    tailoringStatus.textContent = '';
  }}

  if (sessionStorage.getItem('applypilotTailoringPending')) {{
    sessionStorage.removeItem('applypilotTailoringPending');
    window.setTimeout(() => window.location.reload(), 1000);
  }}
}}

async function refreshTailoringStatus() {{
  try {{
    const response = await fetch('/api/tailoring/status');
    const state = await response.json();
    if (!response.ok) throw new Error(state.error || 'Could not check tailoring');
    renderTailoringStatus(state);
  }} catch (error) {{
    tailoringStatus.classList.add('error');
    tailoringStatus.textContent = error.message;
    scheduleTailoringRefresh();
  }}
}}

tailoringButton.addEventListener('click', async () => {{
  tailoringButton.disabled = true;
  tailoringStatus.textContent = 'Starting tailoring...';
  try {{
    const response = await fetch('/api/tailoring', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{min_score: 7, limit: 20, validation_mode: 'normal'}})
    }});
    const request = await response.json();
    if (!response.ok) throw new Error(request.error || 'Could not start tailoring');
    sessionStorage.setItem('applypilotTailoringPending', 'true');
    await refreshTailoringStatus();
  }} catch (error) {{
    tailoringButton.disabled = false;
    tailoringStatus.classList.add('error');
    tailoringStatus.textContent = error.message;
  }}
}});

document.querySelectorAll('.tailor-job-btn').forEach(button => {{
  button.addEventListener('click', async () => {{
    button.disabled = true;
    button.textContent = 'Starting...';
    tailoringStatus.textContent = 'Starting tailoring for the selected job...';
    try {{
      const response = await fetch('/api/tailoring/job', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{url: button.dataset.jobUrl, validation_mode: 'normal'}})
      }});
      const request = await response.json();
      if (!response.ok) throw new Error(request.error || 'Could not tailor this job');
      sessionStorage.setItem('applypilotTailoringPending', 'true');
      await refreshTailoringStatus();
    }} catch (error) {{
      button.disabled = false;
      button.textContent = 'Tailor';
      tailoringStatus.classList.add('error');
      tailoringStatus.textContent = error.message;
    }}
  }});
}});

refreshTailoringStatus();

function switchTab(tab) {{
  currentTab = tab;
  document.querySelectorAll('.tab-btn').forEach(button => {{
    button.classList.toggle('active', button.dataset.tab === tab);
  }});
  history.replaceState(null, '', tab === 'applied' ? '#applied' : window.location.pathname);
  applyFilters();
}}

function filterScore(min, button) {{
  minScore = min;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  button.classList.add('active');
  applyFilters();
}}

function filterText(text) {{
  searchText = text.toLowerCase();
  applyFilters();
}}

function filterSource(source) {{
  selectedSource = source.toLowerCase();
  applyFilters();
}}

function applyFilters() {{
  let shown = 0;
  let total = 0;
  document.querySelectorAll('.job-card').forEach(card => {{
    total++;
    const score = parseInt(card.dataset.score) || 0;
    const text = card.textContent.toLowerCase();
    const scoreMatch = score >= minScore;
    const textMatch = !searchText || text.includes(searchText);
    const sourceMatch = !selectedSource || card.dataset.site.toLowerCase() === selectedSource;
    const tabMatch = card.dataset.applied === String(currentTab === 'applied');
    if (scoreMatch && textMatch && sourceMatch && tabMatch) {{
      card.classList.remove('hidden');
      shown++;
    }} else {{
      card.classList.add('hidden');
    }}
  }});
  document.getElementById('job-count').textContent = `Showing ${{shown}} of ${{total}} jobs`;

  // Hide empty score groups
  document.querySelectorAll('.score-header').forEach(header => {{
    const grid = header.nextElementSibling;
    if (grid && grid.classList.contains('job-grid')) {{
      const visible = grid.querySelectorAll('.job-card:not(.hidden)').length;
      header.style.display = visible ? '' : 'none';
      grid.style.display = visible ? '' : 'none';
      const count = header.querySelector('.score-group-count');
      if (count) count.textContent = `(${{visible}} job${{visible === 1 ? '' : 's'}})`;
    }}
  }});
}}

document.addEventListener('click', async event => {{
  const deleteButton = event.target.closest('.delete-job-btn');
  if (deleteButton) {{
    const card = deleteButton.closest('.job-card');
    const title = card?.querySelector('.job-title')?.textContent?.trim() || 'this job';
    if (!window.confirm(`Delete "${{title}}" from the dashboard?`)) return;
    deleteButton.disabled = true;
    try {{
      const response = await fetch('/api/jobs/delete', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{url: deleteButton.dataset.jobUrl}})
      }});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Could not delete job');
      card?.remove();
      applyFilters();
    }} catch (error) {{
      deleteButton.disabled = false;
      window.alert(error.message);
    }}
    return;
  }}

  const button = event.target.closest('.mark-applied-btn');
  if (!button) return;
  const card = button.closest('.job-card');
  button.disabled = true;
  button.textContent = 'Saving...';
  try {{
    const response = await fetch('/api/jobs/applied', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{url: button.dataset.jobUrl}})
    }});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Could not update job');
    if (card) card.dataset.applied = 'true';
    button.remove();
    applyFilters();
  }} catch (error) {{
    button.disabled = false;
    button.textContent = 'Mark as applied';
    window.alert(error.message);
  }}
}});

let currentView = window.location.hash === '#profile' ? 'profile' : 'dashboard';
let settingsState = null;
let resumeLoaded = false;
let currentSettingsTab = 'profile';
let resumeRequestGeneration = 0;
let resumeUploadInProgress = false;

function switchView(view, updateHash = true) {{
  currentView = view;
  document.querySelectorAll('.app-view').forEach(element => {{
    element.classList.toggle('active', element.id === view + '-view');
  }});
  document.querySelectorAll('.nav-btn').forEach(button => {{
    button.classList.toggle('active', button.dataset.view === view);
  }});
  if (updateHash) {{
    const target = view === 'profile'
      ? '#profile'
      : (currentTab === 'applied' ? '#applied' : window.location.pathname);
    history.replaceState(null, '', target);
  }}
}}

function switchSettingsTab(tab) {{
  currentSettingsTab = tab;
  document.querySelectorAll('.settings-tab').forEach(button => {{
    button.classList.toggle('active', button.dataset.settingsTab === tab);
  }});
  const formIds = {{
    profile: 'profile-settings-form',
    searches: 'search-settings-form',
    resume: 'resume-settings-form'
  }};
  document.querySelectorAll('.settings-form').forEach(form => {{
    form.classList.toggle('active', form.id === formIds[tab]);
  }});
  if (tab === 'resume' && !resumeLoaded) loadResume();
}}

function getPath(object, path) {{
  return path.split('.').reduce((value, key) => value == null ? undefined : value[key], object);
}}

function setPath(object, path, value) {{
  const keys = path.split('.');
  let target = object;
  keys.slice(0, -1).forEach(key => {{
    if (!target[key] || typeof target[key] !== 'object') target[key] = {{}};
    target = target[key];
  }});
  target[keys[keys.length - 1]] = value;
}}

function listFromText(value) {{
  return value.split('\\n').map(item => item.trim()).filter(Boolean);
}}

function asBoolean(value) {{
  if (value === true) return true;
  if (typeof value !== 'string') return false;
  return ['true', 'yes', 'y', '1'].includes(value.trim().toLowerCase());
}}

function setConstrainedValue(input, value) {{
  const text = value == null ? '' : String(value);
  if (input.tagName === 'SELECT') {{
    input.querySelectorAll('[data-custom-option]').forEach(option => option.remove());
    const exists = Array.from(input.options).some(option => option.value === text);
    if (text && !exists) {{
      const option = document.createElement('option');
      option.value = text;
      option.textContent = text + ' (custom)';
      option.dataset.customOption = 'true';
      input.appendChild(option);
    }}
  }}
  input.value = text;
}}

function tagEditorValues(editor) {{
  const root = settingsState[editor.dataset.tagScope] || {{}};
  settingsState[editor.dataset.tagScope] = root;
  let values = getPath(root, editor.dataset.tagPath);
  if (!Array.isArray(values)) {{
    values = [];
    setPath(root, editor.dataset.tagPath, values);
  }}
  return values;
}}

function renderTagEditor(editor) {{
  const list = editor.querySelector('[data-tag-list]');
  list.replaceChildren();
  tagEditorValues(editor).forEach((value, index) => {{
    const pill = document.createElement('span');
    pill.className = 'tag-pill';

    const text = document.createElement('span');
    text.className = 'tag-pill-text';
    text.textContent = value;

    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'tag-remove-btn';
    remove.textContent = '\u00d7';
    remove.setAttribute('aria-label', 'Remove ' + value);
    remove.addEventListener('click', () => {{
      tagEditorValues(editor).splice(index, 1);
      renderTagEditor(editor);
    }});

    pill.append(text, remove);
    list.appendChild(pill);
  }});
}}

function renderTagEditors() {{
  document.querySelectorAll('[data-tag-editor]').forEach(renderTagEditor);
}}

function addTagEditorValue(editor) {{
  const input = editor.querySelector('[data-tag-input]');
  const value = input.value.trim();
  const values = tagEditorValues(editor);
  if (!value || values.includes(value)) return;
  values.push(value);
  input.value = '';
  renderTagEditor(editor);
  input.focus();
}}

document.querySelectorAll('[data-tag-editor]').forEach(editor => {{
  editor.querySelector('[data-tag-add]').addEventListener('click', () => {{
    addTagEditorValue(editor);
  }});
  editor.querySelector('[data-tag-input]').addEventListener('keydown', event => {{
    if (event.key !== 'Enter') return;
    event.preventDefault();
    addTagEditorValue(editor);
  }});
}});

function populateProfileForm() {{
  const profile = settingsState.profile || {{}};
  document.querySelectorAll('[data-profile-path]').forEach(input => {{
    const value = getPath(profile, input.dataset.profilePath);
    if (input.type === 'checkbox') input.checked = asBoolean(value);
    else setConstrainedValue(input, value);
  }});
  document.querySelectorAll('[data-profile-number]').forEach(input => {{
    const value = getPath(profile, input.dataset.profileNumber);
    input.value = value == null ? '' : value;
  }});
  document.querySelectorAll('[data-profile-list]').forEach(input => {{
    const value = getPath(profile, input.dataset.profileList);
    input.value = Array.isArray(value) ? value.join('\\n') : '';
  }});
  renderTagEditors();
  const passwordInput = document.querySelector('[data-profile-password]');
  passwordInput.value = '';
  document.getElementById('password-help').textContent = settingsState.password_configured
    ? 'A password is configured. Leave blank to keep it unchanged.'
    : 'No password is configured. Leave blank if one is not needed.';
}}

function collectProfileForm() {{
  const profile = JSON.parse(JSON.stringify(settingsState.profile || {{}}));
  document.querySelectorAll('[data-profile-path]').forEach(input => {{
    setPath(
      profile,
      input.dataset.profilePath,
      input.type === 'checkbox' ? input.checked : input.value.trim()
    );
  }});
  document.querySelectorAll('[data-profile-list]').forEach(input => {{
    setPath(profile, input.dataset.profileList, listFromText(input.value));
  }});
  document.querySelectorAll('[data-profile-number]').forEach(input => {{
    const value = input.value === '' ? '' : Number(input.value);
    setPath(profile, input.dataset.profileNumber, value);
  }});
  const password = document.querySelector('[data-profile-password]').value;
  if (password) setPath(profile, 'personal.password', password);
  return profile;
}}

function readQueryRows() {{
  return Array.from(document.querySelectorAll('[data-query-index]')).map(input => {{
    const index = input.dataset.queryIndex;
    const tier = document.querySelector(`[data-query-tier-index="${{index}}"]`);
    return {{query: input.value.trim(), tier: Number(tier.value)}};
  }});
}}

function readLocationRows() {{
  return Array.from(document.querySelectorAll('[data-location-index]')).map(input => {{
    const index = input.dataset.locationIndex;
    const remote = document.querySelector(`[data-location-remote-index="${{index}}"]`);
    return {{location: input.value.trim(), remote: remote.checked}};
  }});
}}

function syncEditableRows() {{
  settingsState.searches.queries = readQueryRows();
  settingsState.searches.locations = readLocationRows();
}}

function renderQueries() {{
  const container = document.getElementById('query-list');
  container.replaceChildren();
  const queries = settingsState.searches.queries || [];
  queries.forEach((query, index) => {{
    const row = document.createElement('div');
    row.className = 'editable-row';
    const input = document.createElement('input');
    input.value = query.query || '';
    input.placeholder = 'Job title';
    input.dataset.queryIndex = index;
    const tier = document.createElement('select');
    tier.dataset.queryTierIndex = index;
    [1, 2, 3].forEach(number => {{
      const option = document.createElement('option');
      option.value = number;
      option.textContent = 'Tier ' + number;
      option.selected = Number(query.tier || 1) === number;
      tier.appendChild(option);
    }});
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'remove-row-btn';
    remove.textContent = 'Remove';
    remove.addEventListener('click', () => {{
      syncEditableRows();
      settingsState.searches.queries.splice(index, 1);
      renderQueries();
    }});
    row.append(input, tier, remove);
    container.appendChild(row);
  }});
}}

function renderLocations() {{
  const container = document.getElementById('location-list');
  container.replaceChildren();
  const locations = settingsState.searches.locations || [];
  locations.forEach((location, index) => {{
    const row = document.createElement('div');
    row.className = 'editable-row location-row';
    const input = document.createElement('input');
    input.value = location.location || '';
    input.placeholder = 'City, region, or country';
    input.dataset.locationIndex = index;
    const remoteLabel = document.createElement('label');
    remoteLabel.className = 'check-field';
    const remote = document.createElement('input');
    remote.type = 'checkbox';
    remote.checked = Boolean(location.remote);
    remote.dataset.locationRemoteIndex = index;
    remoteLabel.append(remote, document.createTextNode(' Remote'));
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'remove-row-btn';
    remove.textContent = 'Remove';
    remove.addEventListener('click', () => {{
      syncEditableRows();
      settingsState.searches.locations.splice(index, 1);
      renderLocations();
    }});
    row.append(input, remoteLabel, remove);
    container.appendChild(row);
  }});
}}

function populateSearchForm() {{
  const searches = settingsState.searches || {{}};
  document.querySelectorAll('[data-search-path]').forEach(input => {{
    const value = getPath(searches, input.dataset.searchPath);
    input.value = value == null ? '' : value;
  }});
  document.querySelectorAll('[data-search-number]').forEach(input => {{
    const value = getPath(searches, input.dataset.searchNumber);
    input.value = value == null ? '' : value;
  }});
  document.querySelectorAll('[data-search-check]').forEach(input => {{
    let value = getPath(searches, input.dataset.searchCheck);
    if (value === undefined && input.dataset.searchCheck === 'bigtech_location_filter') {{
      value = searches.greenhouse_location_filter;
    }}
    input.checked = Boolean(value);
  }});
  document.querySelectorAll('[data-search-list]').forEach(input => {{
    const value = getPath(searches, input.dataset.searchList);
    input.value = Array.isArray(value) ? value.join('\\n') : '';
  }});
  renderTagEditors();
  if (!Array.isArray(searches.queries)) searches.queries = [];
  if (!Array.isArray(searches.locations)) searches.locations = [];
  renderQueries();
  renderLocations();
}}

function collectSearchForm() {{
  const searches = JSON.parse(JSON.stringify(settingsState.searches || {{}}));
  document.querySelectorAll('[data-search-path]').forEach(input => {{
    setPath(searches, input.dataset.searchPath, input.value.trim());
  }});
  document.querySelectorAll('[data-search-number]').forEach(input => {{
    if (input.value === '') {{
      setPath(searches, input.dataset.searchNumber, {{__applypilot_delete__: true}});
    }}
    else setPath(searches, input.dataset.searchNumber, Number(input.value));
  }});
  document.querySelectorAll('[data-search-check]').forEach(input => {{
    setPath(searches, input.dataset.searchCheck, input.checked);
  }});
  document.querySelectorAll('[data-search-list]').forEach(input => {{
    setPath(searches, input.dataset.searchList, listFromText(input.value));
  }});
  searches.queries = readQueryRows();
  searches.locations = readLocationRows();
  return searches;
}}

function setSettingsStatus(type, message, isError = false) {{
  const prefix = type === 'searches' ? 'search' : type;
  const status = document.getElementById(prefix + '-settings-status');
  status.textContent = message;
  status.classList.toggle('error', isError);
}}

async function loadSettings() {{
  try {{
    const response = await fetch('/api/settings');
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Could not load settings');
    settingsState = result;
    populateProfileForm();
    populateSearchForm();
    document.getElementById('settings-loading').classList.add('hidden');
    switchSettingsTab(currentSettingsTab);
  }} catch (error) {{
    const loading = document.getElementById('settings-loading');
    loading.textContent = error.message;
    loading.classList.add('settings-status', 'error');
  }}
}}

function displayResume(result) {{
  const content = result.content || '';
  document.getElementById('resume-preview').textContent =
    content || 'No plain-text resume has been uploaded yet.';
  document.getElementById('resume-meta').textContent = result.exists
    ? `${{result.filename}} · ${{content.length.toLocaleString()}} characters`
    : 'No resume.txt found';
}}

async function displayLatexResume(result) {{
  const content = result.content || '';
  const source = document.getElementById('latex-resume-source');
  const viewer = document.getElementById('latex-resume-preview');
  const errorBox = document.getElementById('latex-render-error');
  const meta = document.getElementById('latex-resume-meta');
  source.textContent = content || 'No LaTeX resume has been uploaded yet.';
  errorBox.classList.add('hidden');
  errorBox.textContent = '';
  viewer.replaceChildren();

  if (!result.exists) {{
    meta.textContent = 'No resume.tex found';
    return;
  }}
  if (!result.pdf_available) {{
    meta.textContent = `${{result.filename}} · PDF not compiled`;
    errorBox.textContent =
      'No compiled PDF is available. Upload the .tex file again to compile it with Tectonic.';
    errorBox.classList.remove('hidden');
    return;
  }}

  meta.textContent = `${{result.filename}} · compiled with Tectonic`;
  try {{
    const pdfjs = await import(
      'https://cdn.jsdelivr.net/npm/pdfjs-dist@4.10.38/build/pdf.min.mjs'
    );
    pdfjs.GlobalWorkerOptions.workerSrc =
      'https://cdn.jsdelivr.net/npm/pdfjs-dist@4.10.38/build/pdf.worker.min.mjs';
    const pdf = await pdfjs.getDocument({{
      url: '/api/resume/pdf?t=' + Date.now(),
      withCredentials: false
    }}).promise;
    for (let pageNumber = 1; pageNumber <= pdf.numPages; pageNumber++) {{
      const page = await pdf.getPage(pageNumber);
      const viewport = page.getViewport({{scale: 1.35}});
      const canvas = document.createElement('canvas');
      canvas.className = 'resume-pdf-page';
      canvas.width = viewport.width;
      canvas.height = viewport.height;
      canvas.setAttribute('aria-label', 'Resume page ' + pageNumber);
      viewer.appendChild(canvas);
      await page.render({{
        canvasContext: canvas.getContext('2d'),
        viewport
      }}).promise;
    }}
  }} catch (error) {{
    const frame = document.createElement('iframe');
    frame.className = 'resume-render-frame';
    frame.title = 'Compiled LaTeX resume PDF';
    frame.src = '/api/resume/pdf?t=' + Date.now();
    viewer.appendChild(frame);
    errorBox.textContent =
      'PDF.js preview failed (' + error.message + '). Showing the browser PDF viewer instead.';
    errorBox.classList.remove('hidden');
  }}
}}

async function loadResume(preserveStatus = false) {{
  if (resumeUploadInProgress) return;
  const textStatus = document.getElementById('resume-settings-status');
  const latexStatus = document.getElementById('latex-resume-status');
  const generation = ++resumeRequestGeneration;
  async function fetchResume(url, fallbackError) {{
    const response = await fetch(url);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || fallbackError);
    return result;
  }}
  const [textOutcome, latexOutcome] = await Promise.allSettled([
    fetchResume('/api/resume', 'Could not load text resume'),
    fetchResume('/api/resume?format=tex', 'Could not load LaTeX resume')
  ]);
  if (generation !== resumeRequestGeneration) return;

  if (textOutcome.status === 'fulfilled') {{
    displayResume(textOutcome.value);
    if (!preserveStatus) {{
      textStatus.textContent = '';
      textStatus.classList.remove('error');
    }}
  }} else {{
    textStatus.textContent = textOutcome.reason.message;
    textStatus.classList.add('error');
    document.getElementById('resume-meta').textContent = 'Text resume unavailable';
  }}
  if (latexOutcome.status === 'fulfilled') {{
    displayLatexResume(latexOutcome.value);
    if (!preserveStatus) {{
      latexStatus.textContent = '';
      latexStatus.classList.remove('error');
    }}
  }} else {{
    latexStatus.textContent = latexOutcome.reason.message;
    latexStatus.classList.add('error');
    document.getElementById('latex-resume-meta').textContent =
      'LaTeX resume unavailable';
  }}
  resumeLoaded = true;
}}

async function saveSettings(type, value, button) {{
  button.disabled = true;
  setSettingsStatus(type, 'Saving...');
  try {{
    const response = await fetch('/api/settings/' + type, {{
      method: 'PUT',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{[type]: value}})
    }});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Could not save settings');
    if (type === 'profile') {{
      settingsState.profile = result.profile;
      settingsState.password_configured = result.password_configured;
      populateProfileForm();
    }} else {{
      settingsState.searches = result.searches;
      populateSearchForm();
    }}
    setSettingsStatus(type, type === 'profile' ? 'Profile saved.' : 'Preferences saved.');
  }} catch (error) {{
    setSettingsStatus(type, error.message, true);
  }} finally {{
    button.disabled = false;
  }}
}}

document.getElementById('profile-settings-form').addEventListener('submit', event => {{
  event.preventDefault();
  saveSettings('profile', collectProfileForm(), event.submitter);
}});

document.getElementById('search-settings-form').addEventListener('submit', event => {{
  event.preventDefault();
  saveSettings('searches', collectSearchForm(), event.submitter);
}});

async function uploadResume(event, options) {{
  event.preventDefault();
  const fileInput = document.getElementById(options.inputId);
  const file = fileInput.files[0];
  const button = event.submitter;
  const status = document.getElementById(options.statusId);
  if (!file || !file.name.toLowerCase().endsWith(options.extension)) {{
    status.textContent = `Choose a ${{options.extension}} resume file.`;
    status.classList.add('error');
    return;
  }}
  if (file.size > 1000000) {{
    status.textContent = 'Resume must be 1 MB or smaller.';
    status.classList.add('error');
    return;
  }}
  if (resumeUploadInProgress) {{
    status.textContent = 'Another resume upload is already in progress.';
    status.classList.add('error');
    return;
  }}

  resumeRequestGeneration += 1;
  resumeUploadInProgress = true;
  button.disabled = true;
  status.textContent = options.extension === '.tex'
    ? 'Uploading and compiling with Tectonic...'
    : 'Uploading...';
  status.classList.remove('error');
  try {{
    let content;
    try {{
      const bytes = await file.arrayBuffer();
      content = new TextDecoder('utf-8', {{fatal: true}}).decode(bytes);
    }} catch (error) {{
      throw new Error('Resume must be a valid UTF-8 text file.');
    }}
    if (!content.trim()) throw new Error('Resume cannot be empty.');
    const response = await fetch('/api/resume', {{
      method: 'PUT',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{filename: file.name, content}})
    }});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Could not upload resume');
    if (result.format === 'tex') await displayLatexResume(result);
    else displayResume(result);
    fileInput.value = '';
    status.textContent = options.successMessage;
  }} catch (error) {{
    status.textContent = error.message;
    status.classList.add('error');
  }} finally {{
    resumeUploadInProgress = false;
    resumeLoaded = false;
    await loadResume(true);
    button.disabled = false;
  }}
}}

document.getElementById('text-resume-upload-form').addEventListener(
  'submit',
  event => uploadResume(event, {{
    inputId: 'resume-file',
    statusId: 'resume-settings-status',
    extension: '.txt',
    successMessage: 'Text resume uploaded.'
  }})
);

document.getElementById('latex-resume-upload-form').addEventListener(
  'submit',
  event => uploadResume(event, {{
    inputId: 'latex-resume-file',
    statusId: 'latex-resume-status',
    extension: '.tex',
    successMessage: 'LaTeX resume compiled and saved.'
  }})
);

document.getElementById('add-query-button').addEventListener('click', () => {{
  syncEditableRows();
  settingsState.searches.queries.push({{query: '', tier: 1}});
  renderQueries();
}});

document.getElementById('add-location-button').addEventListener('click', () => {{
  syncEditableRows();
  settingsState.searches.locations.push({{location: '', remote: false}});
  renderLocations();
}});

loadSettings();
switchView(currentView, false);
if (currentView === 'dashboard') switchTab(currentTab);
</script>

</body>
</html>"""

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    abs_path = str(out.resolve())
    console.print(f"[green]Dashboard written to {abs_path}[/green]")
    return abs_path


def open_dashboard(output_path: str | None = None) -> None:
    """Generate the dashboard and open it in the default browser.

    Args:
        output_path: Where to write the HTML file. Defaults to ~/.applypilot/dashboard.html.
    """
    path = generate_dashboard(output_path)
    console.print("[dim]Opening in browser...[/dim]")
    webbrowser.open(f"file:///{path}")
