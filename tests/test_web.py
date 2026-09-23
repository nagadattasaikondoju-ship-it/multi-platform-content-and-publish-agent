import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from grow_it.models import PlatformPost
from grow_it.web.app import create_app
from grow_it.web.service import Service
from grow_it.web.store import Store

from .fakes import FakeLLM


@pytest.fixture
def client(monkeypatch):
    for var in ("ZERNIO_API_KEY", "AYRSHARE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    llm = FakeLLM({"x": [PlatformPost(platform="x", body="a" * 400)] * 3})
    app = create_app(Store(":memory:"), llm_factory=lambda: llm, autopilot=False)
    with TestClient(app) as c:
        c.llm = llm
        yield c


def wait_for(client, run_id, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] in ("ready", "failed") and not any(
            p["status"] in ("queued", "generating") for p in run["posts"]
        ):
            return run
        time.sleep(0.05)
    raise AssertionError("run did not finish")


@pytest.mark.parametrize(
    "path",
    ["/", "/how-it-works", "/platforms", "/pricing", "/app", "/app/new",
     "/app/calendar", "/app/autopilot", "/app/voice", "/app/connections"],
)
def test_pages_render(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "grow it" in response.text


def test_full_loop_generate_edit_approve_preview(client):
    run_id = client.post("/api/runs", json={"source": "Remote work", "platforms": ["x", "bluesky"]}).json()["id"]
    run = wait_for(client, run_id)
    assert run["status"] == "ready"
    posts = {p["platform"]: p for p in run["posts"]}
    assert posts["bluesky"]["status"] == "draft" and not posts["bluesky"]["errors"]
    # X failed three times, was force-fitted and flagged.
    assert posts["x"]["needs_review"] and posts["x"]["attempts"] == 3
    assert posts["x"]["measure"]["length"] <= 280

    # Editing re-checks; an over-long edit is flagged and can't be approved.
    edited = client.patch(f"/api/runs/{run_id}/posts/bluesky", json={"body": "b" * 320}).json()
    assert edited["errors"] and edited["measure"]["length"] > 300
    refused = client.post(f"/api/runs/{run_id}/posts/bluesky/status", json={"status": "approved"})
    assert refused.status_code == 409

    fixed = client.patch(f"/api/runs/{run_id}/posts/bluesky", json={"body": "Short and clear.", "hashtags": "#ai remote"}).json()
    assert fixed["errors"] == [] and fixed["data"]["hashtags"] == ["ai", "remote"]
    assert client.post(f"/api/runs/{run_id}/posts/bluesky/status", json={"status": "approved"}).status_code == 200

    result = client.post(f"/api/runs/{run_id}/publish", json={"platforms": ["bluesky"], "live": False}).json()
    assert result["outcomes"] == [{"platform": "bluesky", "ok": True, "status": "previewed"}]
    post = client.get(f"/api/runs/{run_id}").json()["posts"]
    bluesky = next(p for p in post if p["platform"] == "bluesky")
    assert bluesky["status"] == "previewed"
    assert bluesky["result"]["payload"]["post"] == "Short and clear.\n\n#ai #remote"

    assert client.get(f"/app/runs/{run_id}").status_code == 200
    report = client.get(f"/app/runs/{run_id}/report")
    assert report.status_code == 200 and "What it read" in report.text


def test_publish_refuses_posts_with_problems(client):
    run_id = client.post("/api/runs", json={"source": "t", "platforms": ["bluesky"]}).json()["id"]
    wait_for(client, run_id)
    client.patch(f"/api/runs/{run_id}/posts/bluesky", json={"body": "x" * 400})
    outcome = client.post(f"/api/runs/{run_id}/publish", json={"platforms": ["bluesky"]}).json()["outcomes"][0]
    assert outcome == {"platform": "bluesky", "ok": False, "message": "Has unresolved problems"}


def test_live_publish_needs_timezone(client):
    run_id = client.post("/api/runs", json={"source": "t", "platforms": ["bluesky"]}).json()["id"]
    wait_for(client, run_id)
    response = client.post(
        f"/api/runs/{run_id}/publish",
        json={"platforms": ["bluesky"], "schedule_at": "2026-10-01T09:00:00"},
    )
    assert response.status_code == 422


def test_preview_endpoint_counts_like_the_validator(client):
    m = client.post("/api/preview", json={"platform": "x", "post": {"body": "hi https://example.com/very/long"}}).json()
    assert m["length"] == 3 + 23 and m["max"] == 280 and m["errors"] == []


def test_brand_voice_reaches_the_prompt(client):
    client.put("/api/voice", json={"brand": "Grow it", "audience": "founders", "notes": "No exclamation marks."})
    run_id = client.post("/api/runs", json={"source": "t", "platforms": ["threads"]}).json()["id"]
    wait_for(client, run_id)
    assert any("Brand: Grow it" in p and "No exclamation marks." in p for p in client.llm.prompts)


def test_form_start_redirects_to_run(client):
    response = client.post("/start", data={"source": "An idea"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/app/runs/")


def test_empty_source_rejected(client):
    assert client.post("/api/runs", json={"source": "   "}).status_code == 422


def test_autopilot_queue_and_run_now(client):
    assert client.post("/api/autopilot/run-now").status_code == 409
    client.post("/api/autopilot/topics", json={"source": "Queued idea"})
    run = client.post("/api/autopilot/run-now").json()
    finished = wait_for(client, run["id"])
    assert finished["origin"] == "autopilot"
    assert client.get("/api/autopilot").json()["topics"][0]["status"] == "used"


def test_autopilot_rejects_bad_time(client):
    assert client.put("/api/autopilot", json={"time": "9am"}).status_code == 422


class TestSchedule:
    def service(self, **settings):
        store = Store(":memory:")
        svc = Service(store, FakeLLM)
        svc.save_autopilot({"enabled": True, "time": "09:00", "utc_offset_minutes": 330, **settings})
        return svc

    def test_first_slot_is_today_if_not_passed(self):
        svc = self.service()
        now = datetime(2026, 10, 1, 2, 0, tzinfo=timezone.utc)  # 07:30 IST
        assert svc.next_slot(svc.autopilot_settings(), now) == datetime(2026, 10, 1, 3, 30, tzinfo=timezone.utc)

    def test_first_slot_moves_to_tomorrow_when_passed(self):
        svc = self.service()
        now = datetime(2026, 10, 1, 6, 0, tzinfo=timezone.utc)  # 11:30 IST
        assert svc.next_slot(svc.autopilot_settings(), now) == datetime(2026, 10, 2, 3, 30, tzinfo=timezone.utc)

    def test_every_n_days_after_a_run(self):
        svc = self.service(every_days=3)
        settings = svc.autopilot_settings()
        settings["last_run_at"] = datetime(2026, 10, 1, 3, 30, tzinfo=timezone.utc).isoformat()
        now = datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc)
        assert svc.next_slot(settings, now) == datetime(2026, 10, 4, 3, 30, tzinfo=timezone.utc)

    def test_tick_needs_enabled_due_and_a_topic(self):
        svc = self.service()
        due = datetime(2026, 10, 1, 3, 31, tzinfo=timezone.utc)
        assert svc.autopilot_tick(due) is None  # empty queue
        svc.save_autopilot({"enabled": False})
        svc.store.add_topic("idea")
        assert svc.autopilot_tick(due) is None  # disabled
        assert svc.autopilot_tick(due - timedelta(hours=2)) is None


def test_interrupted_runs_are_marked_failed_on_startup():
    store = Store(":memory:")
    run_id = store.create_run("idea", ["x", "bluesky"])
    store.update_post(run_id, "bluesky", status="draft", data={"platform": "bluesky", "body": "hi"})
    assert store.recover_interrupted() == 1
    run = store.get_run(run_id)
    assert run["status"] == "failed" and "Interrupted" in run["error"]
    statuses = {p["platform"]: p["status"] for p in run["posts"]}
    assert statuses == {"x": "failed", "bluesky": "draft"}
