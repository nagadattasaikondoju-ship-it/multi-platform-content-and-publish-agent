"""Public, multi-user mode: Google sign-in, per-user data and per-user Zernio profiles."""

import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from grow_it import zernio
from grow_it.web import auth
from grow_it.web.app import create_app
from grow_it.web.store import Store

from .fakes import FakeLLM

PEOPLE = {
    "code-alice": {"sub": "g-alice", "email": "alice@example.com", "name": "Alice", "picture": ""},
    "code-bob": {"sub": "g-bob", "email": "bob@example.com", "name": "Bob", "picture": ""},
}


class FakeZernio:
    """In-memory stand-in for the Zernio API, one account list per profile."""

    def __init__(self):
        self.profiles: dict[str, str] = {}
        self.accounts: dict[str, list[dict]] = {}
        self.published: list[tuple[str, str]] = []
        self.deleted: list[str] = []

    def create_profile(self, name, description=""):
        return self.profiles.setdefault(name, f"prof_{len(self.profiles) + 1}")

    def list_accounts(self, profile_id=None):
        if profile_id is None:
            return [a for accts in self.accounts.values() for a in accts]
        return list(self.accounts.get(profile_id, []))

    def connect_url(self, platform, profile_id, redirect_url, **_):
        return f"https://connect.example/{platform}?profile={profile_id}&back={redirect_url}"

    def connect_bluesky(self, profile_id, identifier, app_password):
        account = {"_id": f"acc_bsky_{profile_id}", "platform": "bluesky", "username": identifier, "isActive": True}
        self.accounts.setdefault(profile_id, []).append(account)
        return account

    def telegram_code(self, profile_id):
        return {"code": "ZRN-TEST", "botUsername": "GrowBot", "instructions": ["Add the bot"]}

    def disconnect_account(self, account_id):
        self.deleted.append(account_id)

    def publish(self, post, account_id, **kwargs):
        if not kwargs.get("dry_run", True):
            self.published.append((post.platform, account_id))
        return {"status": "dry_run" if kwargs.get("dry_run", True) else "ok"}

    def add_account(self, profile_id, platform, account_id):
        self.accounts.setdefault(profile_id, []).append(
            {"_id": account_id, "platform": platform, "username": account_id, "isActive": True}
        )


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("GROW_IT_SECRET", "test-secret")
    monkeypatch.setenv("ZERNIO_API_KEY", "zk")
    monkeypatch.delenv("GROW_IT_PASSWORD", raising=False)
    monkeypatch.delenv("AYRSHARE_API_KEY", raising=False)
    monkeypatch.setattr(auth, "google_user", lambda code, redirect_uri: PEOPLE[code])
    fz = FakeZernio()
    for name in ("create_profile", "list_accounts", "connect_url", "connect_bluesky",
                 "telegram_code", "disconnect_account", "publish"):
        monkeypatch.setattr(zernio, name, getattr(fz, name))
    return fz


@pytest.fixture
def app(fake):
    return create_app(Store(":memory:"), llm_factory=FakeLLM, autopilot=False)


def sign_in(app, code: str) -> TestClient:
    client = TestClient(app)
    start = client.get("/auth/google?next=/app/new", follow_redirects=False)
    assert start.status_code == 303
    query = parse_qs(urlparse(start.headers["location"]).query)
    assert query["client_id"] == ["cid"] and query["scope"] == ["openid email profile"]
    back = client.get(f"/auth/google/callback?code={code}&state={query['state'][0]}", follow_redirects=False)
    assert back.status_code == 303 and back.headers["location"] == "/app/new"
    return client


def wait_ready(client, run_id):
    for _ in range(100):
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] in ("ready", "failed") and not any(p["status"] in ("queued", "generating") for p in run["posts"]):
            return run
        time.sleep(0.03)
    raise AssertionError("run did not finish")


def test_signed_out_visitors_are_sent_to_google_sign_in(app):
    client = TestClient(app)
    assert client.get("/").status_code == 200
    assert client.get("/app", follow_redirects=False).headers["location"].startswith("/login")
    assert client.get("/api/runs").status_code == 401
    assert "Continue with Google" in client.get("/login").text


def test_google_callback_rejects_a_forged_state(app):
    client = TestClient(app)
    client.get("/auth/google", follow_redirects=False)
    bad = client.get("/auth/google/callback?code=code-alice&state=forged", follow_redirects=False)
    assert bad.headers["location"] == "/login?error=signin"
    assert client.get("/api/runs").status_code == 401


def test_session_cookie_cannot_be_forged(app):
    client = TestClient(app)
    client.cookies.set(auth.SESSION_COOKIE, "u_someone.deadbeef")
    assert client.get("/api/runs").status_code == 401


def test_users_only_see_their_own_loops(app):
    alice, bob = sign_in(app, "code-alice"), sign_in(app, "code-bob")
    with alice, bob:
        run_id = alice.post("/api/runs", json={"source": "Alice idea", "platforms": ["x"]}).json()["id"]
        wait_ready(alice, run_id)
        assert [r["id"] for r in alice.get("/api/runs").json()] == [run_id]
        assert bob.get("/api/runs").json() == []
        for method, path in [("GET", f"/api/runs/{run_id}"), ("POST", f"/api/runs/{run_id}/advance"),
                             ("DELETE", f"/api/runs/{run_id}"), ("GET", f"/app/runs/{run_id}")]:
            assert bob.request(method, path).status_code == 404, path
        assert bob.post(f"/api/runs/{run_id}/publish", json={"platforms": ["x"]}).status_code == 404
        assert bob.patch(f"/api/runs/{run_id}/posts/x", json={"body": "hacked"}).status_code == 404

        alice.post("/api/autopilot/topics", json={"source": "alice topic"})
        assert bob.get("/api/autopilot").json()["topics"] == []
        alice.put("/api/voice", json={"brand": "Alice Co", "audience": "", "notes": ""})
        assert "Alice Co" not in bob.get("/app/voice").text


def test_connect_creates_one_profile_per_user_and_redirects_to_the_platform(app, fake):
    alice = sign_in(app, "code-alice")
    with alice:
        first = alice.get("/app/connect/instagram", follow_redirects=False)
        second = alice.get("/app/connect/linkedin", follow_redirects=False)
        assert first.headers["location"].startswith("https://connect.example/instagram?profile=prof_1")
        assert "back=http://testserver/app/connections" in first.headers["location"]
        assert second.headers["location"].startswith("https://connect.example/linkedin?profile=prof_1")
        assert len(fake.profiles) == 1
    bob = sign_in(app, "code-bob")
    with bob:
        assert "profile=prof_2" in bob.get("/app/connect/x", follow_redirects=False).headers["location"]

        page = bob.get("/app/connections?connected=twitter&username=bobby")
        assert "X (Twitter) connected as bobby" in page.text


def test_live_publishing_only_uses_the_owners_accounts(app, fake):
    alice, bob = sign_in(app, "code-alice"), sign_in(app, "code-bob")
    with alice, bob:
        alice.get("/app/connect/x", follow_redirects=False)  # creates prof_1
        bob.get("/app/connect/x", follow_redirects=False)  # creates prof_2
        fake.add_account("prof_1", "twitter", "acc_alice_x")
        fake.add_account("prof_2", "twitter", "acc_bob_x")

        alice_run = alice.post("/api/runs", json={"source": "idea", "platforms": ["x"]}).json()["id"]
        wait_ready(alice, alice_run)
        result = alice.post(f"/api/runs/{alice_run}/publish", json={"platforms": ["x"], "live": True}).json()
        assert result["outcomes"][0]["ok"]
        assert fake.published == [("x", "acc_alice_x")]

        connections = bob.get("/api/connections").json()
        assert [a["id"] for a in connections["accounts"]] == ["acc_bob_x"]


def test_live_publish_without_a_connected_account_says_so(app, fake):
    carol = sign_in(app, "code-bob")
    with carol:
        run_id = carol.post("/api/runs", json={"source": "idea", "platforms": ["linkedin"]}).json()["id"]
        wait_ready(carol, run_id)
        outcome = carol.post(f"/api/runs/{run_id}/publish", json={"platforms": ["linkedin"], "live": True}).json()["outcomes"][0]
        assert not outcome["ok"] and "Connect this platform" in outcome["message"]
        assert fake.published == []


def test_users_cannot_disconnect_someone_elses_account(app, fake):
    alice, bob = sign_in(app, "code-alice"), sign_in(app, "code-bob")
    with alice, bob:
        alice.get("/app/connect/x", follow_redirects=False)
        fake.add_account("prof_1", "twitter", "acc_alice_x")
        assert bob.delete("/api/connections/acc_alice_x").status_code == 404
        assert alice.delete("/api/connections/acc_alice_x").status_code == 204
        assert fake.deleted == ["acc_alice_x"]


def test_bluesky_and_telegram_connect_into_the_users_profile(app, fake):
    alice = sign_in(app, "code-alice")
    with alice:
        r = alice.post("/api/connections/bluesky", json={"handle": "@alice.bsky.social", "app_password": "abcd"})
        assert r.json() == {"ok": True, "username": "alice.bsky.social"}
        assert fake.accounts["prof_1"][0]["platform"] == "bluesky"
        assert alice.post("/api/connections/telegram").json()["code"] == "ZRN-TEST"


def test_daily_loop_limit(app, monkeypatch):
    monkeypatch.setenv("GROW_IT_DAILY_LOOP_LIMIT", "2")
    alice = sign_in(app, "code-alice")
    with alice:
        for _ in range(2):
            assert alice.post("/api/runs", json={"source": "idea", "platforms": ["x"]}).status_code == 201
        blocked = alice.post("/api/runs", json={"source": "idea", "platforms": ["x"]})
        assert blocked.status_code == 429 and "today's 2 loops" in blocked.json()["detail"]


def test_sign_out_ends_the_session(app):
    alice = sign_in(app, "code-alice")
    with alice:
        assert alice.get("/api/runs").status_code == 200
        alice.get("/logout")
        assert alice.get("/api/runs").status_code == 401


def test_admin_emails_skip_the_limit_and_see_site_setup(app, monkeypatch):
    monkeypatch.setenv("GROW_IT_DAILY_LOOP_LIMIT", "1")
    monkeypatch.setenv("GROW_IT_ADMIN_EMAILS", "Alice@Example.com")
    alice, bob = sign_in(app, "code-alice"), sign_in(app, "code-bob")
    with alice, bob:
        for _ in range(3):
            assert alice.post("/api/runs", json={"source": "idea", "platforms": ["x"]}).status_code == 201
        assert "Site setup" in alice.get("/app/connections").text
        assert "Site setup" not in bob.get("/app/connections").text


@pytest.fixture
def auth0_app(fake, monkeypatch):
    monkeypatch.setenv("AUTH0_DOMAIN", "growit.eu.auth0.com")
    monkeypatch.setenv("AUTH0_CLIENT_ID", "a0id")
    monkeypatch.setenv("AUTH0_CLIENT_SECRET", "a0secret")
    people = {"a0-code": {"sub": "google-oauth2|123", "email": "dana@example.com", "name": "Dana"}}
    monkeypatch.setattr(auth, "auth0_user", lambda code, redirect_uri: people[code])
    return create_app(Store(":memory:"), llm_factory=FakeLLM, autopilot=False)


def test_auth0_sign_in_round_trip(auth0_app):
    client = TestClient(auth0_app)
    assert "Continue with Google or email" in client.get("/login").text
    start = client.get("/auth/login?next=/app/voice", follow_redirects=False)
    url = urlparse(start.headers["location"])
    query = parse_qs(url.query)
    assert url.netloc == "growit.eu.auth0.com" and url.path == "/authorize"
    assert query["client_id"] == ["a0id"]
    assert query["redirect_uri"] == ["http://testserver/auth/callback"]
    back = client.get(f"/auth/callback?code=a0-code&state={query['state'][0]}", follow_redirects=False)
    assert back.headers["location"] == "/app/voice"
    with client:
        assert "Dana" in client.get("/app").text


def test_auth0_logout_ends_the_auth0_session_too(auth0_app):
    client = TestClient(auth0_app)
    out = client.get("/logout", follow_redirects=False)
    url = urlparse(out.headers["location"])
    assert url.netloc == "growit.eu.auth0.com" and url.path == "/v2/logout"
    assert parse_qs(url.query)["returnTo"] == ["http://testserver/"]


def _legacy_rows(store: Store) -> str:
    """Rows written before sign-in existed: no owner, global voice and autopilot settings."""
    run_id = store.create_run("old idea", ["x"])
    store.add_topic("old topic", None)
    store.set_setting("voice", {"brand": "Old Co", "compiled": "Brand: Old Co"})
    store.set_setting("autopilot", {"enabled": True})
    return run_id


def test_an_admin_adopts_loops_made_before_sign_in(fake, monkeypatch):
    monkeypatch.setenv("GROW_IT_ADMIN_EMAILS", "alice@example.com")
    store = Store(":memory:")
    run_id = _legacy_rows(store)
    app = create_app(store, llm_factory=FakeLLM, autopilot=False)
    bob = sign_in(app, "code-bob")
    with bob:
        assert bob.get("/api/runs").json() == []  # not an admin: adopts nothing
    alice = sign_in(app, "code-alice")
    with alice:
        assert [r["id"] for r in alice.get("/api/runs").json()] == [run_id]
        assert [t["source"] for t in alice.get("/api/autopilot").json()["topics"]] == ["old topic"]
        assert "Old Co" in alice.get("/app/voice").text
    assert store.get_setting("voice") is None and store.get_setting("autopilot") is None


def test_the_password_owner_adopts_loops_made_before_sign_in(monkeypatch):
    for name in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "AUTH0_DOMAIN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GROW_IT_PASSWORD", "pw")
    store = Store(":memory:")
    run_id = _legacy_rows(store)
    with TestClient(create_app(store, llm_factory=FakeLLM, autopilot=False)) as client:
        client.post("/login", data={"password": "pw", "next": "/app"})
        assert [r["id"] for r in client.get("/api/runs").json()] == [run_id]
