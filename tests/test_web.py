import time
from urllib.parse import unquote
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
     "/app/calendar", "/app/autopilot", "/app/voice", "/app/connections",
     "/app/library", "/app/approvals", "/app/analytics", "/app/brand", "/app/assets",
     "/app/integrations", "/app/team", "/app/settings"],
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
        svc.save_autopilot("u1", {"enabled": True, "time": "09:00", "utc_offset_minutes": 330, **settings})
        return svc

    def test_first_slot_is_today_if_not_passed(self):
        svc = self.service()
        now = datetime(2026, 10, 1, 2, 0, tzinfo=timezone.utc)  # 07:30 IST
        assert svc.next_slot(svc.autopilot_settings("u1"), now) == datetime(2026, 10, 1, 3, 30, tzinfo=timezone.utc)

    def test_first_slot_moves_to_tomorrow_when_passed(self):
        svc = self.service()
        now = datetime(2026, 10, 1, 6, 0, tzinfo=timezone.utc)  # 11:30 IST
        assert svc.next_slot(svc.autopilot_settings("u1"), now) == datetime(2026, 10, 2, 3, 30, tzinfo=timezone.utc)

    def test_every_n_days_after_a_run(self):
        svc = self.service(every_days=3)
        settings = svc.autopilot_settings("u1")
        settings["last_run_at"] = datetime(2026, 10, 1, 3, 30, tzinfo=timezone.utc).isoformat()
        now = datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc)
        assert svc.next_slot(settings, now) == datetime(2026, 10, 4, 3, 30, tzinfo=timezone.utc)

    def test_tick_needs_enabled_due_and_a_topic(self):
        svc = self.service()
        due = datetime(2026, 10, 1, 3, 31, tzinfo=timezone.utc)
        assert svc.autopilot_tick("u1", due) is None  # empty queue
        svc.save_autopilot("u1", {"enabled": False})
        svc.store.add_topic("idea", "u1")
        assert svc.autopilot_tick("u1", due) is None  # disabled
        assert svc.autopilot_tick("u1", due - timedelta(hours=2)) is None


def test_interrupted_runs_are_marked_failed_on_startup():
    store = Store(":memory:")
    run_id = store.create_run("idea", ["x", "bluesky"])
    store.update_post(run_id, "bluesky", status="draft", data={"platform": "bluesky", "body": "hi"})
    assert store.recover_interrupted() == 1
    run = store.get_run(run_id)
    assert run["status"] == "failed" and "Interrupted" in run["error"]
    statuses = {p["platform"]: p["status"] for p in run["posts"]}
    assert statuses == {"x": "failed", "bluesky": "draft"}


# --- deployment behaviour (Vercel) ---------------------------------------

def make_client(monkeypatch, **kwargs):
    for var in ("ZERNIO_API_KEY", "AYRSHARE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(Store(":memory:"), llm_factory=FakeLLM, autopilot=False, **kwargs)
    return TestClient(app)


def test_serverless_run_is_driven_by_advance(monkeypatch):
    with make_client(monkeypatch, serverless=True) as c:
        run_id = c.post("/api/runs", json={"source": "idea", "platforms": ["x", "bluesky", "threads", "telegram"]}).json()["id"]
        assert c.get(f"/api/runs/{run_id}").json()["status"] == "briefing"  # nothing runs by itself
        statuses = []
        for _ in range(6):
            run = c.post(f"/api/runs/{run_id}/advance").json()
            statuses.append(run["status"])
            if run["status"] == "ready":
                break
        assert statuses[0] == "writing" and statuses[-1] == "ready"
        assert all(p["status"] == "draft" for p in run["posts"])

        # Rewriting one platform queues it; the next advance writes it.
        c.post(f"/api/runs/{run_id}/posts/x/regenerate")
        assert c.post(f"/api/runs/{run_id}/advance").json()["status"] == "ready"
        assert next(p for p in c.get(f"/api/runs/{run_id}").json()["posts"] if p["platform"] == "x")["status"] == "draft"


def test_password_locks_console_and_api(monkeypatch):
    monkeypatch.setenv("GROW_IT_PASSWORD", "s3cret")
    with make_client(monkeypatch) as c:
        assert c.get("/").status_code == 200  # marketing stays public
        r = c.get("/app/new?source=hi", follow_redirects=False)
        assert r.status_code == 303 and unquote(r.headers["location"]) == "/login?next=/app/new?source=hi"
        assert c.get("/api/runs").status_code == 401
        assert c.post("/start", data={"source": "x"}, follow_redirects=False).headers["location"].startswith("/login")

        bad = c.post("/login", data={"password": "nope", "next": "/app"}, follow_redirects=False)
        assert "error=1" in bad.headers["location"]
        good = c.post("/login", data={"password": "s3cret", "next": "//evil.com"}, follow_redirects=False)
        assert good.headers["location"] == "/app"
        assert c.get("/app").status_code == 200 and c.get("/api/runs").status_code == 200


def test_vercel_without_password_stays_locked(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("GROW_IT_PASSWORD", raising=False)
    with make_client(monkeypatch) as c:
        assert c.get("/api/runs").status_code == 401
        page = c.get("/login")
        assert "GROW_IT_PASSWORD" in page.text


def test_cron_requires_secret(monkeypatch):
    monkeypatch.delenv("CRON_SECRET", raising=False)
    with make_client(monkeypatch, serverless=True) as c:
        assert c.get("/api/cron/autopilot").status_code == 401
        monkeypatch.setenv("CRON_SECRET", "abc")
        assert c.get("/api/cron/autopilot", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.get("/api/cron/autopilot", headers={"Authorization": "Bearer abc"}).json() == {"started": []}


def test_cron_runs_a_due_autopilot_loop_to_completion(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "abc")
    with make_client(monkeypatch, serverless=True) as c:
        service = c.app.state.service
        service.save_autopilot("local", {"enabled": True, "time": "00:00", "utc_offset_minutes": 0,
                                         "platforms": ["bluesky"]})
        settings = service.autopilot_settings("local")
        settings["last_run_at"] = "2020-01-01T00:00:00+00:00"
        service.store.set_setting("autopilot:local", settings)
        service.store.add_topic("Queued idea", "local")
        [started] = c.get("/api/cron/autopilot", headers={"Authorization": "Bearer abc"}).json()["started"]
        run = c.get(f"/api/runs/{started}").json()
        assert run["status"] == "ready" and run["origin"] == "autopilot"


def test_claim_posts_reclaims_stale_work():
    store = Store(":memory:")
    run_id = store.create_run("idea", ["x", "bluesky"])
    assert store.claim_posts(run_id, limit=1) == ["x"]
    assert store.claim_posts(run_id, limit=5) == ["bluesky"]
    assert store.claim_posts(run_id, limit=5) == []  # both in flight
    assert store.claim_posts(run_id, limit=5, stale_after_seconds=-1) == ["x", "bluesky"]


def test_postgres_placeholders_are_translated():
    from grow_it.web.store import _DB

    class Conn:
        def execute(self, sql, params):
            return sql

    assert _DB(Conn(), postgres=True).execute("SELECT * FROM t WHERE a = ? AND b = ?") == "SELECT * FROM t WHERE a = %s AND b = %s"


def test_public_pages_are_readable_by_crawlers_and_link_previews(client):
    for path in ("/", "/how-it-works", "/platforms", "/pricing"):
        head = client.head(path)
        assert head.status_code == 200 and head.content == b"", path
        assert head.headers["content-type"].startswith("text/html")
        html = client.get(path).text
        assert 'content="index, follow"' in html
        assert '<meta property="og:title"' in html and "/static/img/og.png" in html
        assert f'<link rel="canonical" href="http://testserver{path}">' in html
    assert client.head("/static/img/og.png").status_code == 200

    robots = client.get("/robots.txt").text
    assert "Disallow: /app" in robots and "Sitemap: http://testserver/sitemap.xml" in robots
    sitemap = client.get("/sitemap.xml")
    assert sitemap.headers["content-type"].startswith("application/xml")
    assert "<loc>http://testserver/pricing</loc>" in sitemap.text
    assert 'content="noindex, nofollow"' in client.get("/app").text
