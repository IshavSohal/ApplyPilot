"""Durable preparation, review, and sending for post-application outreach."""

from __future__ import annotations

import html
import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from applypilot import config
from applypilot.database import get_connection
from applypilot.outreach.apollo import ApolloClient, ApolloError
from applypilot.outreach.research import fetch_official_pages

log = logging.getLogger(__name__)

DESIRED_RECIPIENTS = 5
MAX_ENRICHMENTS = 10
SAME_COMPANY_COOLDOWN_DAYS = 30
TERMINAL_BATCH_STATES = {"completed", "cancelled"}
RECRUITER_WORDS = ("recruit", "talent", "people partner", "sourcer")
LEADER_WORDS = ("chief", "vice president", "vp ", "head of", "director")
MANAGER_WORDS = ("manager", "lead")
REMOTE_ONLY_WORDS = ("remote", "anywhere", "work from home", "wfh", "distributed")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def enabled() -> bool:
    return os.environ.get("OUTREACH_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def enqueue_for_job(job_url: str, conn: sqlite3.Connection | None = None) -> dict | None:
    """Idempotently create a batch for an applied job when outreach is enabled."""
    if not enabled():
        return None
    conn = conn or get_connection()
    row = conn.execute("SELECT applied_at FROM jobs WHERE url = ?", (job_url,)).fetchone()
    if not row or not row["applied_at"]:
        return None
    now = _now()
    batch_id = str(uuid.uuid4())
    conn.execute(
        "INSERT OR IGNORE INTO outreach_batches "
        "(id, job_url, status, created_at, updated_at) VALUES (?, ?, 'queued', ?, ?)",
        (batch_id, job_url, now, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM outreach_batches WHERE job_url = ?", (job_url,)).fetchone()
    return dict(row) if row else None


def cancel_for_job(job_url: str, conn: sqlite3.Connection | None = None) -> None:
    conn = conn or get_connection()
    now = _now()
    conn.execute(
        "UPDATE outreach_batches SET status = 'cancelled', updated_at = ? "
        "WHERE job_url = ? AND status IN ('queued', 'preparing', 'ready_for_review', 'failed') "
        "AND NOT EXISTS (SELECT 1 FROM outreach_recipients r "
        "WHERE r.batch_id = outreach_batches.id AND r.status IN ('sending', 'sent'))",
        (now, job_url),
    )
    conn.commit()


def _batch_row(identifier: str, conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM outreach_batches WHERE id = ? OR job_url = ? LIMIT 1",
        (identifier, identifier),
    ).fetchone()


def get_batch(identifier: str, conn: sqlite3.Connection | None = None) -> dict | None:
    conn = conn or get_connection()
    row = _batch_row(identifier, conn)
    if not row:
        return None
    batch = dict(row)
    batch["company_research"] = _loads(batch.pop("company_research_json", None), {})
    recipients = conn.execute(
        "SELECT * FROM outreach_recipients WHERE batch_id = ? "
        "ORDER BY relevance_score DESC, created_at",
        (batch["id"],),
    ).fetchall()
    batch["recipients"] = []
    for recipient in recipients:
        item = dict(recipient)
        item["source_facts"] = _loads(item.pop("source_facts_json", None), [])
        batch["recipients"].append(item)
    return batch


def _loads(value: str | None, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


def _domain_from_organization(organization: dict) -> str:
    domain = str(
        organization.get("primary_domain")
        or organization.get("website_url")
        or organization.get("website")
        or ""
    ).strip()
    if "://" not in domain:
        domain = f"https://{domain}"
    return (urlparse(domain).hostname or "").lower().removeprefix("www.")


def _choose_organization(company: str, organizations: list[dict]) -> dict | None:
    normalized = re.sub(r"[^a-z0-9]", "", company.lower())
    for organization in organizations:
        name = re.sub(r"[^a-z0-9]", "", str(organization.get("name") or "").lower())
        if name == normalized:
            return organization
    return organizations[0] if organizations else None


def _workday_tenant_alias(*urls: str | None) -> str | None:
    """Extract a company-owned Workday tenant as a conservative search alias."""
    for url in urls:
        host = (urlparse(str(url or "")).hostname or "").lower()
        match = re.fullmatch(r"([a-z0-9_-]+)\.wd\d+\.myworkdayjobs\.com", host)
        if match:
            return re.sub(r"[-_]+", " ", match.group(1)).strip()
    return None


def _resolve_organization(job: dict, apollo: ApolloClient) -> tuple[dict | None, str]:
    """Resolve an Apollo organization, retrying an exact Workday tenant alias."""
    company = str(job.get("company") or "").strip()
    organizations = apollo.search_organizations(company)
    organization = _choose_organization(company, organizations)
    domain = _domain_from_organization(organization or {})
    if domain:
        return organization, domain

    alias = _workday_tenant_alias(job.get("url"), job.get("application_url"))
    if alias and re.sub(r"[^a-z0-9]", "", alias.lower()) != re.sub(
        r"[^a-z0-9]", "", company.lower()
    ):
        alias_organization = _choose_organization(alias, apollo.search_organizations(alias))
        alias_domain = _domain_from_organization(alias_organization or {})
        if alias_domain:
            return alias_organization, alias_domain
    return organization, ""


def _candidate_kind(title: str) -> str:
    lowered = title.lower()
    if any(word in lowered for word in RECRUITER_WORDS):
        return "recruiter"
    if any(word in lowered for word in LEADER_WORDS):
        return "leader"
    if any(word in lowered for word in MANAGER_WORDS):
        return "manager"
    return "peer"


def _role_terms(job_title: str, description: str) -> set[str]:
    ignored = {"senior", "junior", "staff", "lead", "manager", "engineer", "developer", "the", "and", "with"}
    words = re.findall(r"[a-z][a-z+#.-]{2,}", f"{job_title} {description[:3000]}".lower())
    return {word.strip(".-") for word in words if word not in ignored}


def _location_queries(location: str | None) -> list[str]:
    """Return useful Apollo location filters, omitting remote-only fragments."""
    queries: list[str] = []
    for raw_part in re.split(r"\s*;\s*", str(location or "")):
        part = raw_part.strip()
        if not part:
            continue
        for word in REMOTE_ONLY_WORDS:
            part = re.sub(rf"\b{re.escape(word)}\b", " ", part, flags=re.IGNORECASE)
        part = re.sub(r"\s*[-–—()/]\s*", " ", part)
        part = re.sub(r"(?:^\s*,\s*|\s*,\s*$)", "", part)
        part = re.sub(r"\s+", " ", part).strip(" ,-–—")
        if part and part.lower() not in {item.lower() for item in queries}:
            queries.append(part)
    return queries[:5]


def _normalized_location(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _location_contains(job_location: str, value: object) -> bool:
    needle = _normalized_location(value)
    haystack = f" {_normalized_location(job_location)} "
    return bool(needle and f" {needle} " in haystack)


def _location_preference(person: dict, job_location: str | None) -> tuple[int, str | None]:
    """Score explicit Apollo location fields without excluding unknown locations."""
    if not job_location or not _location_queries(job_location):
        return 0, None
    if person.get("_location_search_match"):
        return 50, f"Located in or near the role's location ({job_location})"

    city = person.get("city")
    state = person.get("state")
    country = person.get("country")
    if _location_contains(job_location, city):
        return 50, f"Same city as the role ({city})"
    if _location_contains(job_location, state):
        return 30, f"Same region as the role ({state})"
    if _location_contains(job_location, country):
        return 12, f"Same country as the role ({country})"
    return 0, None


def rank_people(
    people: list[dict],
    job_title: str,
    description: str,
    job_location: str | None = None,
) -> list[dict]:
    """Rank current employees and retain a balanced hiring-circle ordering."""
    terms = _role_terms(job_title, description)
    ranked: list[dict] = []
    for person in people:
        person_id = person.get("id") or person.get("person_id")
        title = str(person.get("title") or "")
        if not person_id or not title:
            continue
        if person.get("employment_history") and not any(
            bool(item.get("current")) for item in person.get("employment_history", [])
        ):
            continue
        title_terms = set(re.findall(r"[a-z][a-z+#.-]{2,}", title.lower()))
        kind = _candidate_kind(title)
        score = min(60, len(terms & title_terms) * 15)
        score += {"manager": 30, "leader": 24, "recruiter": 20, "peer": 15}[kind]
        location_score, location_reason = _location_preference(person, job_location)
        score += location_score
        item = dict(person)
        item["person_id"] = str(person_id)
        item["candidate_kind"] = kind
        item["relevance_score"] = score
        role_reason = {
            "manager": "Likely manager for the role's function",
            "leader": "Leader in a function related to the role",
            "recruiter": "Recruiting or talent contact",
            "peer": "Senior employee close to the role's team",
        }[kind]
        item["relevance_reason"] = (
            f"{role_reason}; {location_reason}" if location_reason else role_reason
        )
        ranked.append(item)
    ranked.sort(key=lambda item: (-item["relevance_score"], item.get("name") or ""))
    balanced: list[dict] = []
    for kind in ("manager", "leader", "recruiter", "peer"):
        match = next((item for item in ranked if item["candidate_kind"] == kind and item not in balanced), None)
        if match:
            balanced.append(match)
    balanced.extend(item for item in ranked if item not in balanced)
    return balanced


def _already_contacted(email: str, domain: str, conn: sqlite3.Connection) -> bool:
    cutoff = (datetime.now(UTC) - timedelta(days=SAME_COMPANY_COOLDOWN_DAYS)).isoformat()
    return bool(conn.execute(
        "SELECT 1 FROM outreach_recipients r JOIN outreach_batches b ON b.id = r.batch_id "
        "WHERE lower(r.email) = lower(?) AND b.company_domain = ? AND r.status = 'sent' "
        "AND r.sent_at >= ? LIMIT 1",
        (email, domain, cutoff),
    ).fetchone())


def _is_suppressed(person_id: str, email: str, conn: sqlite3.Connection) -> bool:
    keys = [f"person:{person_id}", f"email:{email.lower()}"]
    return bool(conn.execute(
        "SELECT 1 FROM outreach_suppressions WHERE key IN (?, ?) LIMIT 1", keys
    ).fetchone())


def _extract_json(text: str):
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(
            r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE | re.DOTALL
        )
    start = min((index for index in (cleaned.find("["), cleaned.find("{")) if index >= 0), default=-1)
    if start < 0:
        raise ValueError("The LLM did not return JSON")
    return json.loads(cleaned[start:])


def _generate_messages(job: dict, recipients: list[dict], research: dict, profile: dict) -> list[dict]:
    from applypilot.llm import get_client

    samples = profile.get("outreach", {}).get("writing_samples", [])
    if len([item for item in samples if str(item).strip()]) < 3:
        raise ValueError("Add at least three outreach writing samples to your profile")
    signature = str(profile.get("outreach", {}).get("signature") or profile.get("personal", {}).get("full_name") or "")
    resume_facts = profile.get("resume_facts", {})
    safe_research = {
        "apollo": research.get("apollo", {}),
        "official_pages": [
            {"url": page["url"], "text": page["text"][:2500]}
            for page in research.get("official_pages", [])
        ],
    }
    prompt = f"""Write one concise networking email per recipient after a job application.
Return ONLY a JSON array with objects: person_id, subject, body_text, used_facts (array of short source-backed facts).

Rules:
- 100-160 words, plain text, natural and specific, no invented familiarity.
- Mention the exact role and one grounded company or role detail.
- Explain why contacting this person's function makes sense; do not claim they own the opening.
- Connect only to candidate facts supplied below. Never invent experience or company facts.
- Match the writing samples' tone and phrasing. End with the supplied signature.
- Do not include citations, tracking links, attachments, or an unsubscribe paragraph.

JOB: {json.dumps({'title': job.get('title'), 'company': job.get('company'), 'description': (job.get('full_description') or '')[:8000]})}
CANDIDATE FACTS: {json.dumps(resume_facts)}
OFFICIAL COMPANY RESEARCH: {json.dumps(safe_research)}
RECIPIENTS: {json.dumps([{'person_id': r['person_id'], 'name': r.get('name'), 'title': r.get('title'), 'reason': r.get('relevance_reason')} for r in recipients])}
WRITING SAMPLES: {json.dumps(samples)}
SIGNATURE: {signature}
"""
    result = _extract_json(get_client().ask(prompt, temperature=0.3, max_tokens=3500))
    if not isinstance(result, list):
        raise ValueError("The LLM returned an invalid email batch")  # noqa: TRY004
    by_id = {str(item.get("person_id")): item for item in result if isinstance(item, dict)}
    output = []
    for recipient in recipients:
        item = by_id.get(recipient["person_id"])
        if not item:
            continue
        subject = str(item.get("subject") or "").strip()
        body = str(item.get("body_text") or "").strip()
        if not subject or not body or len(subject) > 200 or len(body) > 4000:
            continue
        output.append({**recipient, "subject": subject, "body_text": body, "used_facts": item.get("used_facts") or []})
    return output


def prepare_batch(
    identifier: str,
    *,
    conn: sqlite3.Connection | None = None,
    apollo: ApolloClient | None = None,
) -> dict:
    conn = conn or get_connection()
    batch = _batch_row(identifier, conn)
    if not batch:
        raise ValueError("Outreach batch not found")
    if batch["status"] in TERMINAL_BATCH_STATES:
        return get_batch(batch["id"], conn) or {}
    now = _now()
    claimed = conn.execute(
        "UPDATE outreach_batches SET status = 'preparing', error = NULL, updated_at = ? "
        "WHERE id = ? AND status IN ('queued', 'failed')",
        (now, batch["id"]),
    ).rowcount
    conn.commit()
    if not claimed:
        return get_batch(batch["id"], conn) or {}

    try:
        job_row = conn.execute("SELECT * FROM jobs WHERE url = ?", (batch["job_url"],)).fetchone()
        if not job_row or not job_row["applied_at"]:
            raise ValueError("The job is not marked as applied")
        job = dict(job_row)
        profile = config.load_profile()
        apollo = apollo or ApolloClient()
        organization, domain = _resolve_organization(job, apollo)
        if not organization:
            raise ValueError("Apollo could not resolve the employer")
        if not domain:
            raise ValueError("Apollo did not provide the employer's official domain")

        cached = conn.execute(
            "SELECT facts_json, sources_json, researched_at FROM company_research WHERE domain = ?",
            (domain,),
        ).fetchone()
        fresh_after = datetime.now(UTC) - timedelta(days=7)
        use_cache = False
        if cached:
            try:
                use_cache = datetime.fromisoformat(cached["researched_at"]) >= fresh_after
            except (TypeError, ValueError):
                use_cache = False
        if use_cache:
            pages = _loads(cached["sources_json"], [])
        else:
            pages = fetch_official_pages(domain)
            conn.execute(
                "INSERT OR REPLACE INTO company_research (domain, facts_json, sources_json, researched_at) "
                "VALUES (?, ?, ?, ?)",
                (domain, json.dumps(organization), json.dumps(pages), _now()),
            )
        research = {"apollo": organization, "official_pages": pages}
        people_by_id: dict[str, dict] = {}
        location_queries = _location_queries(job.get("location"))
        if location_queries:
            try:
                local_people = apollo.search_people(
                    organization_id=str(organization.get("id") or organization.get("organization_id") or "") or None,
                    domain=domain,
                    locations=location_queries,
                    per_page=100,
                )
                for person in local_people:
                    person_id = str(person.get("id") or person.get("person_id") or "")
                    if person_id:
                        people_by_id[person_id] = {**person, "_location_search_match": True}
            except ApolloError as exc:
                log.warning("Apollo location-filtered people search failed; using company-wide results: %s", exc)

        company_people = apollo.search_people(
            organization_id=str(organization.get("id") or organization.get("organization_id") or "") or None,
            domain=domain,
            per_page=100,
        )
        for person in company_people:
            person_id = str(person.get("id") or person.get("person_id") or "")
            if person_id and person_id not in people_by_id:
                people_by_id[person_id] = person
        ranked = rank_people(
            list(people_by_id.values()),
            job.get("title") or "",
            job.get("full_description") or "",
            job.get("location"),
        )
        eligible: list[dict] = []
        attempted = 0
        for candidate in ranked:
            if attempted >= MAX_ENRICHMENTS or len(eligible) >= DESIRED_RECIPIENTS:
                break
            attempted += 1
            person = apollo.enrich_person(candidate["person_id"])
            if not person:
                continue
            email = str(person.get("email") or "").strip()
            email_status = str(person.get("email_status") or "").lower()
            if not email or email_status != "verified":
                continue
            if _is_suppressed(candidate["person_id"], email, conn) or _already_contacted(email, domain, conn):
                continue
            eligible.append({
                **candidate,
                "first_name": person.get("first_name") or candidate.get("first_name"),
                "last_name": person.get("last_name") or candidate.get("last_name"),
                "name": person.get("name") or candidate.get("name"),
                "title": person.get("title") or candidate.get("title"),
                "linkedin_url": person.get("linkedin_url") or candidate.get("linkedin_url"),
                "email": email,
                "email_status": email_status,
            })
        messages = _generate_messages(job, eligible, research, profile) if eligible else []
        if not messages:
            raise ValueError("No relevant employees with verified work emails were available")

        conn.execute("DELETE FROM outreach_recipients WHERE batch_id = ? AND status != 'sent'", (batch["id"],))
        created = _now()
        for item in messages:
            conn.execute(
                "INSERT INTO outreach_recipients "
                "(id, batch_id, apollo_person_id, first_name, last_name, title, linkedin_url, "
                "email, email_status, relevance_score, relevance_reason, subject, body_text, "
                "source_facts_json, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?)",
                (
                    str(uuid.uuid4()), batch["id"], item["person_id"], item.get("first_name"),
                    item.get("last_name"), item.get("title"), item.get("linkedin_url"),
                    item.get("email"), item.get("email_status"), item.get("relevance_score"),
                    item.get("relevance_reason"), item.get("subject"), item.get("body_text"),
                    json.dumps(item.get("used_facts") or []), created, created,
                ),
            )
        conn.execute(
            "UPDATE outreach_batches SET status = 'ready_for_review', company_domain = ?, "
            "company_research_json = ?, error = NULL, updated_at = ? WHERE id = ?",
            (domain, json.dumps(research), _now(), batch["id"]),
        )
        conn.commit()
    except Exception as exc:
        conn.execute(
            "UPDATE outreach_batches SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
            (str(exc)[:1000], _now(), batch["id"]),
        )
        conn.commit()
        raise
    return get_batch(batch["id"], conn) or {}


def _body_html(body: str) -> str:
    return "".join(f"<p>{html.escape(paragraph).replace(chr(10), '<br>')}</p>" for paragraph in body.split("\n\n") if paragraph.strip())


def refresh_delivery_statuses(
    identifier: str,
    *,
    conn: sqlite3.Connection | None = None,
    apollo: ApolloClient | None = None,
) -> dict:
    conn = conn or get_connection()
    batch = _batch_row(identifier, conn)
    if not batch:
        raise ValueError("Outreach batch not found")
    rows = conn.execute(
        "SELECT id, apollo_message_id FROM outreach_recipients "
        "WHERE batch_id = ? AND status = 'sending' AND apollo_message_id IS NOT NULL",
        (batch["id"],),
    ).fetchall()
    if rows:
        apollo = apollo or ApolloClient()
    for row in rows:
        try:
            result = apollo.email_status(row["apollo_message_id"])
            status = str(result.get("status") or "").lower()
            if status == "completed":
                conn.execute(
                    "UPDATE outreach_recipients SET status = 'sent', sent_at = ?, error = NULL, updated_at = ? WHERE id = ?",
                    (result.get("completed_at") or _now(), _now(), row["id"]),
                )
            elif status == "failed":
                error = result.get("failure_reason") or result.get("not_sent_reason") or result.get("message")
                conn.execute(
                    "UPDATE outreach_recipients SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                    (str(error or "Apollo could not send the email")[:1000], _now(), row["id"]),
                )
            elif status == "drafted":
                # Approval was already persisted before the draft was created.
                # Resuming this send is safe after a process crash.
                apollo.send_email(row["apollo_message_id"])
        except ApolloError as exc:
            log.warning("Could not refresh Apollo email %s: %s", row["apollo_message_id"], exc)
    _update_batch_after_send(batch["id"], conn)
    conn.commit()
    return get_batch(batch["id"], conn) or {}


def _update_batch_after_send(batch_id: str, conn: sqlite3.Connection) -> None:
    statuses = [row[0] for row in conn.execute(
        "SELECT status FROM outreach_recipients WHERE batch_id = ? AND status != 'excluded'", (batch_id,)
    ).fetchall()]
    if not statuses:
        status = "cancelled"
    elif any(item == "sending" for item in statuses):
        status = "sending"
    elif any(item == "failed" for item in statuses):
        status = "partial_failed" if any(item == "sent" for item in statuses) else "failed"
    elif all(item in {"sent", "suppressed"} for item in statuses):
        status = "completed"
    else:
        status = "ready_for_review"
    completed_at = _now() if status == "completed" else None
    conn.execute(
        "UPDATE outreach_batches SET status = ?, completed_at = COALESCE(completed_at, ?), updated_at = ? WHERE id = ?",
        (status, completed_at, _now(), batch_id),
    )


def approve_batch(
    batch_id: str,
    recipients: list[dict],
    *,
    confirmed: bool,
    conn: sqlite3.Connection | None = None,
    apollo: ApolloClient | None = None,
) -> dict:
    """Persist reviewed edits and send only explicitly selected recipients."""
    if confirmed is not True:
        raise ValueError("Explicit send confirmation is required")
    if not isinstance(recipients, list) or not recipients:
        raise ValueError("Select at least one recipient")
    email_account_id = os.environ.get("APOLLO_EMAIL_ACCOUNT_ID", "").strip()
    if not email_account_id:
        raise ApolloError("APOLLO_EMAIL_ACCOUNT_ID is not configured")
    apollo = apollo or ApolloClient()
    accounts = apollo.email_accounts()
    if not any(str(account.get("id")) == email_account_id for account in accounts):
        raise ApolloError("The configured Apollo sending mailbox is not linked to this API user")
    conn = conn or get_connection()
    batch = _batch_row(batch_id, conn)
    if not batch or batch["status"] not in {"ready_for_review", "failed", "partial_failed"}:
        raise ValueError("This outreach batch is not ready to send")
    selected_ids: list[str] = []
    for edit in recipients:
        if not isinstance(edit, dict) or not edit.get("id"):
            raise ValueError("Each selected recipient must include an ID")
        subject = str(edit.get("subject") or "").strip()
        body = str(edit.get("body_text") or "").strip()
        if not subject or not body or len(subject) > 200 or len(body) > 4000:
            raise ValueError("Every selected email needs a valid subject and body")
        updated = conn.execute(
            "UPDATE outreach_recipients SET subject = ?, body_text = ?, updated_at = ? "
            "WHERE id = ? AND batch_id = ? AND status IN ('ready', 'failed')",
            (subject, body, _now(), str(edit["id"]), batch["id"]),
        ).rowcount
        if not updated:
            raise ValueError("A selected recipient is no longer eligible to send")
        selected_ids.append(str(edit["id"]))
    placeholders = ",".join("?" for _ in selected_ids)
    conn.execute(
        f"UPDATE outreach_recipients SET status = 'excluded', updated_at = ? "
        f"WHERE batch_id = ? AND status = 'ready' AND id NOT IN ({placeholders})",
        [_now(), batch["id"], *selected_ids],
    )
    conn.execute(
        "UPDATE outreach_batches SET status = 'sending', approved_at = COALESCE(approved_at, ?), updated_at = ? WHERE id = ?",
        (_now(), _now(), batch["id"]),
    )
    conn.commit()

    for recipient_id in selected_ids:
        row = conn.execute("SELECT * FROM outreach_recipients WHERE id = ?", (recipient_id,)).fetchone()
        if not row or row["status"] not in {"ready", "failed"}:
            continue
        item = dict(row)
        if _is_suppressed(item["apollo_person_id"], item["email"], conn):
            conn.execute(
                "UPDATE outreach_recipients SET status = 'suppressed', updated_at = ? WHERE id = ?",
                (_now(), recipient_id),
            )
            continue
        try:
            contact_id = item.get("apollo_contact_id")
            if not contact_id:
                contact_id = apollo.create_contact(item)["id"]
            message_id = item.get("apollo_message_id")
            if not message_id:
                draft = apollo.create_email_draft(
                    contact_id=contact_id,
                    subject=item["subject"],
                    body_html=_body_html(item["body_text"]),
                    email_account_id=email_account_id,
                )
                message_id = draft["id"]
            conn.execute(
                "UPDATE outreach_recipients SET status = 'sending', apollo_contact_id = ?, "
                "apollo_message_id = ?, error = NULL, updated_at = ? WHERE id = ?",
                (contact_id, message_id, _now(), recipient_id),
            )
            conn.commit()
            apollo.send_email(message_id)
        except Exception as exc:  # noqa: BLE001 - isolate each recipient's send failure
            conn.execute(
                "UPDATE outreach_recipients SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                (str(exc)[:1000], _now(), recipient_id),
            )
            conn.commit()
    _update_batch_after_send(batch["id"], conn)
    conn.commit()
    return refresh_delivery_statuses(batch["id"], conn=conn, apollo=apollo)


def retry_batch(identifier: str, *, conn: sqlite3.Connection | None = None, apollo: ApolloClient | None = None) -> dict:
    conn = conn or get_connection()
    batch = _batch_row(identifier, conn)
    if not batch:
        raise ValueError("Outreach batch not found")
    if batch["status"] == "failed" and not conn.execute(
        "SELECT 1 FROM outreach_recipients WHERE batch_id = ?", (batch["id"],)
    ).fetchone():
        conn.execute("UPDATE outreach_batches SET status = 'queued', updated_at = ? WHERE id = ?", (_now(), batch["id"]))
        conn.commit()
        return prepare_batch(batch["id"], conn=conn, apollo=apollo)
    failed = [dict(row) for row in conn.execute(
        "SELECT id, subject, body_text FROM outreach_recipients WHERE batch_id = ? AND status = 'failed'",
        (batch["id"],),
    ).fetchall()]
    if not failed:
        return refresh_delivery_statuses(batch["id"], conn=conn, apollo=apollo)
    return approve_batch(batch["id"], failed, confirmed=True, conn=conn, apollo=apollo)


def cancel_batch(identifier: str, conn: sqlite3.Connection | None = None) -> dict:
    conn = conn or get_connection()
    batch = _batch_row(identifier, conn)
    if not batch:
        raise ValueError("Outreach batch not found")
    sent = conn.execute(
        "SELECT 1 FROM outreach_recipients WHERE batch_id = ? AND status IN ('sending', 'sent') LIMIT 1",
        (batch["id"],),
    ).fetchone()
    if sent:
        raise ValueError("A batch cannot be cancelled after sending has started")
    conn.execute(
        "UPDATE outreach_batches SET status = 'cancelled', updated_at = ? WHERE id = ?",
        (_now(), batch["id"]),
    )
    conn.commit()
    return get_batch(batch["id"], conn) or {}


def clear_cancelled_batch(identifier: str, conn: sqlite3.Connection | None = None) -> dict:
    """Permanently remove a cancelled batch and its unsent local recipients."""
    conn = conn or get_connection()
    batch = _batch_row(identifier, conn)
    if not batch:
        raise ValueError("Outreach batch not found")
    if batch["status"] != "cancelled":
        raise ValueError("Only a cancelled outreach batch can be cleared")
    started = conn.execute(
        "SELECT 1 FROM outreach_recipients WHERE batch_id = ? "
        "AND status IN ('sending', 'sent') LIMIT 1",
        (batch["id"],),
    ).fetchone()
    if started:
        raise ValueError("Outreach with sent or in-flight emails cannot be cleared")
    result = {"id": batch["id"], "job_url": batch["job_url"], "status": "cleared"}
    conn.execute("DELETE FROM outreach_recipients WHERE batch_id = ?", (batch["id"],))
    conn.execute("DELETE FROM outreach_batches WHERE id = ?", (batch["id"],))
    conn.commit()
    return result


def suppress_recipient(recipient_id: str, reason: str = "user", conn: sqlite3.Connection | None = None) -> dict:
    conn = conn or get_connection()
    recipient = conn.execute("SELECT * FROM outreach_recipients WHERE id = ?", (recipient_id,)).fetchone()
    if not recipient:
        raise ValueError("Outreach recipient not found")
    if recipient["status"] in {"sending", "sent"}:
        raise ValueError("A recipient cannot be suppressed after sending has started")
    now = _now()
    keys = [f"person:{recipient['apollo_person_id']}"]
    if recipient["email"]:
        keys.append(f"email:{recipient['email'].lower()}")
    conn.executemany(
        "INSERT OR REPLACE INTO outreach_suppressions (key, reason, created_at) VALUES (?, ?, ?)",
        [(key, reason[:300], now) for key in keys],
    )
    conn.execute(
        "UPDATE outreach_recipients SET status = 'suppressed', updated_at = ? WHERE id = ?",
        (now, recipient_id),
    )
    _update_batch_after_send(recipient["batch_id"], conn)
    conn.commit()
    return get_batch(recipient["batch_id"], conn) or {}
