"""The review stage: source evidence and an editable brief before any post is written."""

import time

import pytest
from fastapi.testclient import TestClient

from grow_it.evidence import in_source
from grow_it.web.app import create_app
from grow_it.web.store import Store

from .fakes import FakeLLM

SOURCE = (
    "Last quarter we moved the whole team to async updates. In the first month we cut meetings "
    "by 40% and shipped two releases early. Nobody misses the Monday stand-up."
)


@pytest.fixture
def client(monkeypatch):
    for var in ("ZERNIO_API_KEY", "AYRSHARE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    llm = FakeLLM()
    app = create_app(Store(":memory:"), llm_factory=lambda: llm, autopilot=False)
    with TestClient(app) as c:
        c.llm = llm
        yield c


def wait_status(client, run_id, statuses, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] in statuses and not any(p["status"] in ("queued", "generating") for p in run["posts"]
                                                 if run["status"] not in ("review",)):
            return run
        time.sleep(0.03)
    raise AssertionError(f"run never reached {statuses}")


def start(client, **extra):
    body = {"source": SOURCE, "platforms": ["x", "linkedin"], "review": True,
            "meta": {"title": "Async", "input_type": "draft", "audience": "Founders", "cta": "Book a call"}} | extra
    created = client.post("/api/runs", json=body)
    assert created.status_code == 201
    return wait_status(client, created.json()["id"], ("review", "failed"))


def test_in_source_ignores_case_spacing_and_curly_quotes():
    assert in_source("We cut  meetings\nby 40%", SOURCE)
    assert in_source("Nobody misses the Monday stand‑up".replace("‑", "-"), SOURCE)
    assert not in_source("3x more productive", SOURCE)
    assert not in_source("the", SOURCE)  # too short to count as evidence


def test_review_stops_before_writing_and_checks_every_claim(client):
    run = start(client)
    assert run["status"] == "review"
    assert all(p["status"] == "queued" for p in run["posts"])  # nothing written yet
    assert not any("PLATFORM" in p for p in client.llm.prompts)
    supported, invented = run["evidence"]
    assert supported["supported"] and supported["approved"] and supported["snippet"]
    assert not invented["supported"] and not invented["approved"] and invented["snippet"] == ""
    assert run["brief"]["facts"] == ["Teams cut meetings by 40%."]
    assert run["brief"]["audience"] == "Founders" and run["brief"]["cta"] == "Book a call"
    assert run["analysis"]["risky_claims"] == ["3x more productive"]


def test_posts_only_use_approved_evidence_and_the_edited_brief(client):
    run = start(client)
    supported, invented = run["evidence"]
    supported["text"] = "We cut meetings by 40% in a month."
    invented["approved"] = False
    extra = {"text": "Made-up claim", "snippet": "not in the source at all", "approved": True}
    body = {"brief": {"thesis": "Write it down instead.", "prohibited": ["synergy"], "tone": "dry"},
            "evidence": [supported, invented, extra], "platforms": ["linkedin"]}
    done = client.post(f"/api/runs/{run['id']}/generate", json=body)
    assert done.status_code == 200
    final = wait_status(client, run["id"], ("ready", "failed"))
    assert final["status"] == "ready" and [p["platform"] for p in final["posts"]] == ["linkedin"]
    assert final["brief"]["facts"] == ["We cut meetings by 40% in a month.", "Made-up claim"]
    added = final["evidence"][2]
    assert added["added"] and not added["supported"]  # the person approved it, flagged as unsupported
    prompt = next(p for p in client.llm.prompts if "(key: linkedin)" in p)
    assert "Write it down instead." in prompt and "synergy" in prompt


def test_a_source_without_facts_asks_for_opinion_led_posts(client):
    run = start(client)
    for claim in run["evidence"]:
        claim["approved"] = False
    client.post(f"/api/runs/{run['id']}/generate", json={"brief": {}, "evidence": run["evidence"], "platforms": ["x"]})
    wait_status(client, run["id"], ("ready", "failed"))
    prompt = next(p for p in client.llm.prompts if "(key: x)" in p)
    assert "NO VERIFIED FACTS" in prompt


def test_banned_phrases_are_flagged_when_editing(client):
    run = start(client)
    client.post(f"/api/runs/{run['id']}/generate",
                json={"brief": {"prohibited": ["game-changer"]}, "evidence": run["evidence"], "platforms": ["linkedin"]})
    wait_status(client, run["id"], ("ready",))
    edited = client.patch(f"/api/runs/{run['id']}/posts/linkedin", json={"body": "A total game-changer for teams."})
    assert any("game-changer" in e for e in edited.json()["errors"])


def test_review_is_validated_and_only_happens_once(client):
    run = start(client)
    assert client.put(f"/api/runs/{run['id']}/review",
                      json={"brief": {}, "evidence": [], "platforms": ["nope"]}).status_code == 400
    saved = client.put(f"/api/runs/{run['id']}/review",
                       json={"brief": {"campaign": "Q3"}, "evidence": run["evidence"], "platforms": ["x"]})
    assert saved.status_code == 200 and saved.json()["brief"]["campaign"] == "Q3"
    client.post(f"/api/runs/{run['id']}/generate", json={"brief": {}, "evidence": run["evidence"], "platforms": ["x"]})
    again = client.post(f"/api/runs/{run['id']}/generate", json={"brief": {}, "evidence": [], "platforms": ["x"]})
    assert again.status_code == 400


def test_the_review_page_renders(client):
    run = start(client)
    page = client.get(f"/app/runs/{run['id']}")
    assert page.status_code == 200 and "Source evidence" in page.text


def test_serverless_review_is_driven_by_the_browser(monkeypatch):
    for var in ("ZERNIO_API_KEY", "AYRSHARE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(Store(":memory:"), llm_factory=FakeLLM, autopilot=False, serverless=True)
    with TestClient(app) as c:
        run_id = c.post("/api/runs", json={"source": SOURCE, "platforms": ["x"], "review": True}).json()["id"]
        assert c.get(f"/api/runs/{run_id}").json()["status"] == "analyzing"
        run = c.post(f"/api/runs/{run_id}/advance").json()
        assert run["status"] == "review" and run["evidence"][0]["supported"]
        c.post(f"/api/runs/{run_id}/generate", json={"brief": {}, "evidence": run["evidence"], "platforms": ["x"]})
        for _ in range(5):
            run = c.post(f"/api/runs/{run_id}/advance").json()
        assert run["status"] == "ready" and run["posts"][0]["status"] == "draft"
