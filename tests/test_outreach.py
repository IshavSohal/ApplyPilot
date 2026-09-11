import json

import httpx
import pytest

from applypilot.database import init_db
from applypilot.outreach.apollo import ApolloClient, ApolloError
from applypilot.outreach.service import (
    approve_batch,
    cancel_batch,
    clear_cancelled_batch,
    enqueue_for_job,
    prepare_batch,
    rank_people,
    _resolve_organization,
)


class FakeApollo:
    def __init__(self):
        self.sent = []

    def search_organizations(self, name):
        return [{"id": "org-1", "name": name, "primary_domain": "example.com"}]

    def email_accounts(self):
        return [{"id": "mailbox-1", "email": "me@example.net"}]

    def search_people(self, **_kwargs):
        return [
            {"id": "manager", "name": "Morgan Manager", "first_name": "Morgan", "last_name": "Manager", "title": "Engineering Manager"},
            {"id": "leader", "name": "Lee Leader", "first_name": "Lee", "last_name": "Leader", "title": "Director of Engineering"},
            {"id": "recruiter", "name": "Rae Recruiter", "first_name": "Rae", "last_name": "Recruiter", "title": "Technical Recruiter"},
            {"id": "peer", "name": "Sam Senior", "first_name": "Sam", "last_name": "Senior", "title": "Senior Backend Engineer"},
            {"id": "peer-2", "name": "Pat Platform", "first_name": "Pat", "last_name": "Platform", "title": "Staff Platform Engineer"},
        ]

    def enrich_person(self, person_id):
        return {"email": f"{person_id}@example.com", "email_status": "verified"}

    def create_contact(self, recipient):
        return {"id": f"contact-{recipient['apollo_person_id']}"}

    def create_email_draft(self, **kwargs):
        return {"id": f"message-{kwargs['contact_id']}"}

    def send_email(self, message_id):
        self.sent.append(message_id)
        return {"id": message_id, "status": "scheduled"}

    def email_status(self, message_id):
        return {"id": message_id, "status": "completed", "completed_at": "2026-09-09T12:00:00+00:00"}


class FakeLLM:
    def ask(self, prompt, **_kwargs):
        recipients = json.loads(prompt.split("RECIPIENTS: ", 1)[1].split("\nWRITING SAMPLES:", 1)[0])
        return json.dumps([
            {
                "person_id": item["person_id"],
                "subject": "Backend Engineer application",
                "body_text": "Hi there,\n\nI applied for the Backend Engineer role and appreciated the focus on reliable systems. My background includes production Python services, and I would value your perspective on the engineering team.\n\nBest,\nTest User",
                "used_facts": ["The role focuses on reliable systems"],
            }
            for item in recipients
        ])


@pytest.fixture
def outreach_db(tmp_path):
    conn = init_db(tmp_path / "outreach.db")
    conn.execute(
        "INSERT INTO jobs (url, title, company, full_description, applied_at, apply_status) "
        "VALUES (?, ?, ?, ?, ?, 'applied')",
        (
            "https://jobs.example.com/backend",
            "Backend Engineer",
            "Example",
            "Build reliable Python services for a growing platform engineering team.",
            "2026-09-09T10:00:00+00:00",
        ),
    )
    conn.commit()
    return conn


def test_enqueue_is_disabled_by_default(outreach_db, monkeypatch):
    monkeypatch.delenv("OUTREACH_ENABLED", raising=False)
    assert enqueue_for_job("https://jobs.example.com/backend", outreach_db) is None


def test_prepare_review_and_send_are_idempotent(outreach_db, monkeypatch):
    monkeypatch.setenv("OUTREACH_ENABLED", "true")
    monkeypatch.setenv("APOLLO_EMAIL_ACCOUNT_ID", "mailbox-1")
    monkeypatch.setattr("applypilot.outreach.service.fetch_official_pages", lambda _domain: [])
    monkeypatch.setattr(
        "applypilot.outreach.service.config.load_profile",
        lambda: {
            "personal": {"full_name": "Test User"},
            "resume_facts": {"real_metrics": ["Built production Python services"]},
            "outreach": {"signature": "Test User", "writing_samples": ["One", "Two", "Three"]},
        },
    )
    monkeypatch.setattr("applypilot.llm.get_client", lambda: FakeLLM())
    apollo = FakeApollo()

    first = enqueue_for_job("https://jobs.example.com/backend", outreach_db)
    second = enqueue_for_job("https://jobs.example.com/backend", outreach_db)
    assert first["id"] == second["id"]

    batch = prepare_batch(first["id"], conn=outreach_db, apollo=apollo)
    assert batch["status"] == "ready_for_review"
    assert len(batch["recipients"]) == 5

    with pytest.raises(ValueError, match="confirmation"):
        approve_batch(first["id"], batch["recipients"], confirmed=False, conn=outreach_db, apollo=apollo)
    assert apollo.sent == []

    selected = [
        {"id": item["id"], "subject": item["subject"], "body_text": item["body_text"]}
        for item in batch["recipients"][:2]
    ]
    sent = approve_batch(first["id"], selected, confirmed=True, conn=outreach_db, apollo=apollo)
    assert sent["status"] == "completed"
    assert len(apollo.sent) == 2
    assert [item["status"] for item in sent["recipients"]].count("sent") == 2
    assert [item["status"] for item in sent["recipients"]].count("excluded") == 3


def test_rank_people_balances_hiring_circle():
    people = FakeApollo().search_people()
    ranked = rank_people(people, "Backend Engineer", "Python backend platform")
    kinds = [item["candidate_kind"] for item in ranked[:4]]
    assert kinds == ["manager", "leader", "recruiter", "peer"]


def test_clear_cancelled_batch_removes_batch_and_recipients(outreach_db, monkeypatch):
    monkeypatch.setenv("OUTREACH_ENABLED", "true")
    batch = enqueue_for_job("https://jobs.example.com/backend", outreach_db)
    outreach_db.execute(
        "INSERT INTO outreach_recipients "
        "(id, batch_id, apollo_person_id, status, created_at, updated_at) "
        "VALUES ('recipient-1', ?, 'person-1', 'ready', ?, ?)",
        (batch["id"], "2026-09-10T10:00:00+00:00", "2026-09-10T10:00:00+00:00"),
    )
    outreach_db.commit()

    cancel_batch(batch["id"], outreach_db)
    result = clear_cancelled_batch(batch["id"], outreach_db)

    assert result["status"] == "cleared"
    assert outreach_db.execute(
        "SELECT 1 FROM outreach_batches WHERE id = ?", (batch["id"],)
    ).fetchone() is None
    assert outreach_db.execute(
        "SELECT 1 FROM outreach_recipients WHERE batch_id = ?", (batch["id"],)
    ).fetchone() is None


def test_clear_cancelled_batch_rejects_active_batch(outreach_db, monkeypatch):
    monkeypatch.setenv("OUTREACH_ENABLED", "true")
    batch = enqueue_for_job("https://jobs.example.com/backend", outreach_db)

    with pytest.raises(ValueError, match="Only a cancelled"):
        clear_cancelled_batch(batch["id"], outreach_db)


def test_rank_people_prefers_same_job_location_within_candidate_kind():
    people = [
        {
            "id": "remote-manager",
            "name": "Very Relevant Remote Manager",
            "title": "Backend Platform Engineering Manager",
            "city": "Vancouver",
            "state": "British Columbia",
            "country": "Canada",
        },
        {
            "id": "local-manager",
            "name": "Local Manager",
            "title": "Engineering Manager",
            "city": "Toronto",
            "state": "Ontario",
            "country": "Canada",
        },
    ]

    ranked = rank_people(
        people,
        "Backend Engineer",
        "Build a Python backend platform",
        "Toronto, Ontario, Canada",
    )

    assert ranked[0]["person_id"] == "local-manager"
    assert "Same city" in ranked[0]["relevance_reason"]


def test_rank_people_does_not_bias_location_for_remote_only_job():
    people = [
        {
            "id": "best-title",
            "name": "Best Title",
            "title": "Backend Platform Engineering Manager",
            "city": "Vancouver",
        },
        {
            "id": "other-title",
            "name": "Other Title",
            "title": "Engineering Manager",
            "city": "Toronto",
        },
    ]

    ranked = rank_people(people, "Backend Engineer", "Python backend platform", "Remote")

    assert ranked[0]["person_id"] == "best-title"


def test_apollo_people_search_accepts_location_filters():
    captured = {}

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"people": []})

    client = ApolloClient(
        "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=1,
    )
    client.search_people(
        organization_id="org-1",
        domain="example.com",
        locations=["Toronto, Ontario, Canada"],
    )

    assert captured["person_locations"] == ["Toronto, Ontario, Canada"]


def test_organization_resolution_uses_exact_workday_tenant_fallback():
    class OrganizationsApollo:
        def __init__(self):
            self.queries = []

        def search_organizations(self, name):
            self.queries.append(name)
            if name.lower() == "td":
                return [{"id": "td", "name": "TD", "primary_domain": "td.com"}]
            return [{"id": "wrong", "name": "TD Canada Trust Bank", "primary_domain": None}]

    apollo = OrganizationsApollo()
    organization, domain = _resolve_organization(
        {
            "company": "TD Bank",
            "url": "https://td.wd3.myworkdayjobs.com/TD_Bank_Careers/job/role",
        },
        apollo,
    )

    assert apollo.queries == ["TD Bank", "td"]
    assert organization["id"] == "td"
    assert domain == "td.com"


def test_apollo_client_redacts_key_and_labels_auth_error():
    def handler(request):
        assert request.headers["x-api-key"] == "secret"
        return httpx.Response(401, json={"error": "bad key"})

    client = ApolloClient(
        "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=1,
    )
    with pytest.raises(ApolloError, match="authentication failed") as exc:
        client.health()
    assert "secret" not in str(exc.value)


def test_apollo_email_draft_uses_current_flat_payload():
    captured = {}

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"emailer_message": {"id": "message-1"}})

    client = ApolloClient(
        "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=1,
    )
    draft = client.create_email_draft(
        contact_id="contact-1",
        subject="Hello",
        body_html="<p>Hello</p>",
        email_account_id="mailbox-1",
    )
    assert draft["id"] == "message-1"
    assert captured == {
        "contact_id": "contact-1",
        "subject": "Hello",
        "body_html": "<p>Hello</p>",
    }


def test_outreach_tables_are_available(outreach_db):
    names = {row[0] for row in outreach_db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"outreach_batches", "outreach_recipients", "company_research", "outreach_suppressions"} <= names
