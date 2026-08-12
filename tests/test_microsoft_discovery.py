import json
import urllib.parse

from applypilot.discovery import greenhouse


def test_fetch_microsoft_jobs_normalizes_and_paginates(monkeypatch):
    calls = []

    def fake_request(url, **_kwargs):
        calls.append(url)
        start = int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["start"][0])
        positions = []
        if start == 0:
            positions = [
                {
                    "id": 123,
                    "name": "Software Engineer",
                    "locations": ["Canada, Ontario, Toronto"],
                    "postedTs": 1_722_470_400,
                    "positionUrl": "/careers/job/123",
                }
            ] * 10
        elif start == 10:
            positions = [
                {
                    "id": 456,
                    "name": "Senior Software Engineer",
                    "standardizedLocations": ["Vancouver, BC, CA"],
                    "positionUrl": "/careers/job/456",
                }
            ]
        return json.dumps({"data": {"count": 11, "positions": positions}}).encode()

    monkeypatch.setattr(greenhouse, "_http_request", fake_request)

    jobs = greenhouse._fetch_microsoft_jobs({"max_pages": 5}, ["software engineer"])

    assert len(calls) == 2
    assert len(jobs) == 2
    assert jobs[0] == {
        "title": "Software Engineer",
        "location": "Canada, Ontario, Toronto",
        "url": "https://apply.careers.microsoft.com/careers/job/123",
        "content": "",
        "content_is_full": False,
        "posted_at": "2024-08-01",
    }
    assert jobs[1]["location"] == "Vancouver, BC, CA"


def test_bigtech_config_includes_supported_microsoft_provider():
    microsoft = greenhouse.load_bigtech_companies()["microsoft"]

    assert microsoft["name"] == "Microsoft"
    assert microsoft["provider"] in greenhouse.BIGTECH_FETCHERS
