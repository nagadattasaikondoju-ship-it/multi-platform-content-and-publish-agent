"""FastAPI app: marketing pages, the console, and a JSON API behind it."""

import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from ..llm import GeminiLLM
from ..models import PlatformPost
from ..specs import load_specs
from .. import zernio
from . import auth
from .service import LimitReached, Service, measure_post, normalise_field, provider_name
from .store import Store, is_postgres_url

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
_macros = templates.env.get_template("macros.html").module
templates.env.globals.update(
    {name: getattr(_macros, name) for name in ("logo", "logo_mark", "icon", "pbadge", "status")}
)

PLATFORM_COLORS = {
    "x": "#10222A", "linkedin": "#0A66C2", "instagram": "#D62976", "facebook": "#1877F2",
    "threads": "#3A3A3C", "bluesky": "#1185FE", "tiktok": "#EE1D52", "youtube": "#FF0000",
    "pinterest": "#E60023", "reddit": "#FF4500", "telegram": "#229ED9", "snapchat": "#E8C400",
    "google_business": "#34A853",
}
PLATFORM_SHORT = {
    "x": "X", "linkedin": "in", "instagram": "IG", "facebook": "f", "threads": "@", "bluesky": "B",
    "tiktok": "TT", "youtube": "YT", "pinterest": "P", "reddit": "R", "telegram": "TG",
    "snapchat": "SC", "google_business": "G",
}


def platform_list() -> list[dict]:
    return [
        {**spec.model_dump(), "color": PLATFORM_COLORS[key], "short": PLATFORM_SHORT[key]}
        for key, spec in load_specs().items()
    ]


def default_store() -> Store:
    """Postgres when a database URL is configured; else SQLite (temporary on Vercel)."""
    url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or ""
    if is_postgres_url(url):
        return Store(url)
    if os.getenv("VERCEL"):
        store = Store("/tmp/grow_it.db")
        store.kind = "temporary"
        return store
    return Store(os.getenv("GROW_IT_DB", "data/grow_it.db"))


PROTECTED = ("/app", "/api", "/start")
OPEN_API = ("/api/cron/",)
CONNECT_PLATFORMS_OAUTH = {
    "x", "linkedin", "instagram", "facebook", "threads", "tiktok", "youtube",
    "pinterest", "reddit", "google_business",
}


def safe_next(target: str | None) -> str:
    target = target or "/app"
    return target if target.startswith("/") and not target.startswith("//") else "/app"


def base_url(request: Request) -> str:
    """Public origin of this deployment, for OAuth redirect URLs."""
    configured = os.getenv("GROW_IT_BASE_URL", "").rstrip("/")
    if configured:
        return configured
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    return f"{proto}://{host}"


def create_app(store: Store | None = None, llm_factory=GeminiLLM, autopilot: bool = True,
               serverless: bool | None = None) -> FastAPI:
    store = store or default_store()
    service = Service(store, llm_factory, serverless=serverless)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if not service.serverless:
            # In-process work only exists on a long-running server.
            store.recover_interrupted()
            if autopilot:
                task = asyncio.create_task(service.autopilot_forever())
        yield
        if task:
            task.cancel()

    app = FastAPI(title="Grow it", lifespan=lifespan)
    app.state.service = service
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    def page(request: Request, name: str, **context) -> HTMLResponse:
        context.setdefault("platforms", platform_list())
        context.setdefault("provider", provider_name())
        context.setdefault("serverless", service.serverless)
        context.setdefault("storage", store.kind)
        context.setdefault("user", getattr(request.state, "user", None))
        context.setdefault("auth_mode", auth.mode())
        context.setdefault("is_admin", service.is_admin(context["user"]))
        return templates.TemplateResponse(request, name, context)

    # --- who is signed in ---------------------------------------------------
    def current_user(request: Request) -> dict | None:
        mode = auth.mode()
        if mode == "local":
            return store.ensure_user(auth.LOCAL_USER, "You")
        if mode == "locked":
            return None
        user_id = auth.verify(request.cookies.get(auth.SESSION_COOKIE, ""))
        if not user_id:
            return None
        if mode == "password" and user_id != auth.OWNER_USER:
            return None
        return store.get_user(user_id)

    def session_response(target: str, user_id: str) -> RedirectResponse:
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(auth.SESSION_COOKIE, auth.sign(user_id), httponly=True,
                            secure=bool(os.getenv("VERCEL")), samesite="lax", max_age=60 * 60 * 24 * 30)
        return response

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        request.state.user = await asyncio.to_thread(current_user, request)
        if path.startswith(PROTECTED) and not path.startswith(OPEN_API) and not request.state.user:
            if path.startswith("/api"):
                return JSONResponse({"detail": "Sign in first"}, status_code=401)
            target = "/app/new" if path == "/start" else path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/login?next={quote(target)}", status_code=303)
        return await call_next(request)

    def uid(request: Request) -> str:
        return request.state.user["id"]

    def own_run(request: Request, run_id: str) -> dict:
        run = store.get_run(run_id)
        if not run or run["user_id"] != uid(request):
            raise HTTPException(404, "Run not found")
        return run

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/app", error: str = ""):
        if request.state.user:
            return RedirectResponse(safe_next(next), status_code=303)
        return page(request, "login.html", nav="", next=safe_next(next), error=error,
                    configured=bool(auth.password()))

    @app.post("/login")
    async def login(request: Request):
        form = await request.form()
        target = safe_next(str(form.get("next") or "/app"))
        if auth.mode() == "password" and hmac.compare_digest(str(form.get("password", "")), auth.password()):
            store.ensure_user(auth.OWNER_USER, "Owner")
            return session_response(target, auth.OWNER_USER)
        return RedirectResponse(f"/login?next={quote(target)}&error=1", status_code=303)

    @app.get("/auth/google")
    def google_start(request: Request, next: str = "/app"):
        if not auth.google_enabled():
            return RedirectResponse("/login", status_code=303)
        state = auth.new_state()
        response = RedirectResponse(
            auth.google_login_url(f"{base_url(request)}/auth/google/callback", state), status_code=303
        )
        response.set_cookie(auth.STATE_COOKIE, f"{state}|{safe_next(next)}", httponly=True, max_age=600,
                            secure=bool(os.getenv("VERCEL")), samesite="lax")
        return response

    @app.get("/auth/google/callback")
    async def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        expected, _, target = request.cookies.get(auth.STATE_COOKIE, "").partition("|")
        if error or not code or not expected or not hmac.compare_digest(state, expected):
            return RedirectResponse("/login?error=google", status_code=303)
        try:
            info = await asyncio.to_thread(
                auth.google_user, code, f"{base_url(request)}/auth/google/callback"
            )
        except Exception:
            return RedirectResponse("/login?error=google", status_code=303)
        user = store.upsert_google_user(info["sub"], info.get("email", ""), info.get("name", ""),
                                        info.get("picture", ""))
        response = session_response(safe_next(target), user["id"])
        response.delete_cookie(auth.STATE_COOKIE)
        return response

    @app.get("/logout")
    def logout():
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(auth.SESSION_COOKIE)
        return response

    # --- marketing ----------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        return page(request, "home.html", nav="home")

    @app.get("/how-it-works", response_class=HTMLResponse)
    def how(request: Request):
        return page(request, "how.html", nav="how")

    @app.get("/platforms", response_class=HTMLResponse)
    def platforms_page(request: Request):
        return page(request, "platforms.html", nav="platforms")

    @app.get("/pricing", response_class=HTMLResponse)
    def pricing(request: Request):
        return page(request, "pricing.html", nav="pricing")

    # --- console ------------------------------------------------------------
    @app.get("/app", response_class=HTMLResponse)
    def dashboard(request: Request):
        runs = store.list_runs(uid(request))
        totals: dict[str, int] = {}
        for run in runs:
            for status, n in run["counts"].items():
                totals[status] = totals.get(status, 0) + n
        return page(
            request, "app/dashboard.html", tab="dashboard", runs=runs, totals=totals,
            autopilot=service.autopilot_settings(uid(request)),
        )

    @app.get("/app/new", response_class=HTMLResponse)
    def new_run(request: Request, source: str = ""):
        return page(request, "app/new.html", tab="new", source=source,
                    voice=store.get_setting(f"voice:{uid(request)}", {}))

    @app.get("/app/runs/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: str):
        return page(request, "app/run.html", tab="dashboard", run=own_run(request, run_id))

    @app.get("/app/runs/{run_id}/report", response_class=HTMLResponse)
    def report_page(request: Request, run_id: str):
        run = own_run(request, run_id)
        specs = load_specs()
        for post in run["posts"]:
            if post["data"]:
                post["measure"] = measure_post(PlatformPost.model_validate(post["data"]))
            post["label"] = specs[post["platform"]].label
        return page(request, "app/report.html", tab="dashboard", run=run)

    @app.get("/app/calendar", response_class=HTMLResponse)
    def calendar(request: Request):
        return page(request, "app/calendar.html", tab="calendar", posts=store.scheduled_posts(uid(request)))

    @app.get("/app/autopilot", response_class=HTMLResponse)
    def autopilot_page(request: Request):
        settings = service.autopilot_settings(uid(request))
        return page(
            request, "app/autopilot.html", tab="autopilot", settings=settings,
            topics=store.list_topics(uid(request)), next_slot=service.next_slot(settings),
        )

    @app.get("/app/connections", response_class=HTMLResponse)
    async def connections_page(request: Request, connected: str = "", error: str = "",
                               username: str = "", platform: str = ""):
        info = await asyncio.to_thread(service.connections, request.state.user)
        by_platform = {}
        for account in info["accounts"]:
            by_platform.setdefault(account["platform"], account)
        flash = None
        if connected:
            ours = {v: k for k, v in zernio.ZERNIO_PLATFORM.items()}.get(connected, connected)
            label = load_specs()[ours].label if ours in load_specs() else connected
            flash = ("ok", f"{label} connected{f' as {username}' if username else ''}.")
        elif error:
            flash = ("error", f"Connecting {platform or 'that account'} didn't finish: {error.replace('_', ' ')}")
        return page(request, "app/connections.html", tab="connections", info=info,
                    by_platform=by_platform, flash=flash, oauth_platforms=CONNECT_PLATFORMS_OAUTH)

    @app.get("/app/connect/{platform}")
    async def connect_platform(request: Request, platform: str):
        if platform not in CONNECT_PLATFORMS_OAUTH:
            raise HTTPException(404, "This platform connects differently")
        if provider_name() != "zernio":
            return RedirectResponse("/app/connections?error=publishing_not_configured", status_code=303)
        try:
            url = await asyncio.to_thread(
                service.connect_link, request.state.user, platform, f"{base_url(request)}/app/connections"
            )
        except Exception as e:
            return RedirectResponse(
                f"/app/connections?error={quote(str(e)[:200])}&platform={quote(load_specs()[platform].label)}",
                status_code=303,
            )
        return RedirectResponse(url, status_code=303)

    @app.get("/app/voice", response_class=HTMLResponse)
    def voice_page(request: Request):
        return page(request, "app/voice.html", tab="voice", voice=store.get_setting(f"voice:{uid(request)}", {}))

    # --- API ----------------------------------------------------------------
    class RunIn(BaseModel):
        source: str
        platforms: list[str] | None = None

    class PostEdit(BaseModel):
        title: str | None = None
        body: str | None = None
        hashtags: list[str] | str | None = None
        media_prompt: str | None = None
        script: str | None = None
        subreddit: str | None = None
        cta_type: str | None = None

    class StatusIn(BaseModel):
        status: str

    class PublishIn(BaseModel):
        platforms: list[str]
        schedule_at: datetime | None = None
        live: bool = False
        options: dict | None = None

    class PreviewIn(BaseModel):
        platform: str
        post: PostEdit

    class VoiceIn(BaseModel):
        brand: str = ""
        audience: str = ""
        notes: str = ""

    class TopicIn(BaseModel):
        source: str

    class BlueskyIn(BaseModel):
        handle: str
        app_password: str

    def start(request: Request, source: str, platforms: list[str] | None, origin: str = "manual") -> str:
        try:
            return service.start_run(uid(request), source, platforms, origin=origin)
        except LimitReached as e:
            raise HTTPException(429, str(e))

    @app.get("/api/platforms")
    def api_platforms():
        return platform_list()

    @app.post("/api/runs", status_code=201)
    async def api_create_run(request: Request, body: RunIn):
        if not body.source.strip():
            raise HTTPException(422, "Give the loop a topic, a draft or a link")
        run_id = start(request, body.source, body.platforms)
        return {"id": run_id, "url": f"/app/runs/{run_id}"}

    @app.post("/start")
    async def form_start(request: Request):
        form = await request.form()
        source = str(form.get("source", "")).strip()
        if not source:
            return RedirectResponse("/app/new", status_code=303)
        platforms = form.getlist("platforms") or None
        try:
            run_id = start(request, source, [str(p) for p in platforms] if platforms else None)
        except HTTPException:
            return RedirectResponse(f"/app/new?source={quote(source)}", status_code=303)
        return RedirectResponse(f"/app/runs/{run_id}", status_code=303)

    @app.get("/api/runs")
    def api_runs(request: Request):
        return store.list_runs(uid(request))

    @app.get("/api/runs/{run_id}")
    def api_run(request: Request, run_id: str):
        run = own_run(request, run_id)
        for post in run["posts"]:
            if post["data"]:
                post["measure"] = measure_post(PlatformPost.model_validate(post["data"]))
        return run

    @app.post("/api/runs/{run_id}/advance")
    async def api_advance(request: Request, run_id: str):
        own_run(request, run_id)
        await service.advance(run_id)
        return api_run(request, run_id)

    @app.get("/api/cron/autopilot")
    async def api_cron(request: Request):
        secret = os.getenv("CRON_SECRET", "")
        if not secret or not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {secret}"):
            raise HTTPException(401, "Unauthorized")
        started = service.autopilot_tick_all()
        if started and service.serverless:
            await asyncio.gather(*(service.run_to_completion(r) for r in started), return_exceptions=True)
        return {"started": started}

    @app.delete("/api/runs/{run_id}", status_code=204)
    def api_delete_run(request: Request, run_id: str):
        own_run(request, run_id)
        store.delete_run(run_id)

    @app.patch("/api/runs/{run_id}/posts/{platform}")
    def api_edit_post(request: Request, run_id: str, platform: str, body: PostEdit):
        own_run(request, run_id)
        try:
            post = service.edit_post(run_id, platform, body.model_dump(exclude_unset=True))
        except ValueError as e:
            raise HTTPException(409, str(e))
        post["measure"] = measure_post(PlatformPost.model_validate(post["data"]))
        return post

    @app.post("/api/runs/{run_id}/posts/{platform}/status")
    def api_post_status(request: Request, run_id: str, platform: str, body: StatusIn):
        own_run(request, run_id)
        try:
            return service.set_status(run_id, platform, body.status)
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/api/runs/{run_id}/posts/{platform}/regenerate", status_code=202)
    async def api_regenerate(request: Request, run_id: str, platform: str):
        own_run(request, run_id)
        try:
            service.regenerate(run_id, platform)
        except ValueError as e:
            raise HTTPException(409, str(e))
        return {"ok": True}

    @app.post("/api/preview")
    def api_preview(body: PreviewIn):
        if body.platform not in load_specs():
            raise HTTPException(404, "Unknown platform")
        data = {"platform": body.platform, "body": ""}
        for key, value in body.post.model_dump(exclude_unset=True).items():
            data[key] = normalise_field(key, value)
        return measure_post(PlatformPost.model_validate(data))

    @app.post("/api/runs/{run_id}/publish")
    async def api_publish(request: Request, run_id: str, body: PublishIn):
        own_run(request, run_id)
        if body.schedule_at and body.schedule_at.tzinfo is None:
            raise HTTPException(422, "schedule_at needs a timezone")
        try:
            outcomes = await asyncio.to_thread(
                service.publish, run_id, body.platforms,
                schedule_at=body.schedule_at, live=body.live, options=body.options,
            )
        except RuntimeError as e:
            raise HTTPException(502, str(e))
        return {"provider": provider_name(), "live": body.live, "outcomes": outcomes}

    @app.get("/api/connections")
    async def api_connections(request: Request):
        return await asyncio.to_thread(service.connections, request.state.user)

    @app.post("/api/connections/bluesky")
    async def api_connect_bluesky(request: Request, body: BlueskyIn):
        try:
            account = await asyncio.to_thread(
                service.connect_bluesky, request.state.user, body.handle, body.app_password
            )
        except Exception as e:
            raise HTTPException(400, f"Bluesky didn't accept that: {e}")
        return {"ok": True, "username": account.get("username", body.handle)}

    @app.post("/api/connections/telegram")
    async def api_telegram_code(request: Request):
        try:
            return await asyncio.to_thread(service.telegram_code, request.state.user)
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.get("/api/connections/telegram/{code}")
    async def api_telegram_status(code: str):
        try:
            return await asyncio.to_thread(zernio.telegram_status, code)
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/connections/{account_id}", status_code=204)
    async def api_disconnect(request: Request, account_id: str):
        try:
            await asyncio.to_thread(service.disconnect, request.state.user, account_id)
        except ValueError as e:
            raise HTTPException(404, str(e))

    @app.put("/api/voice")
    def api_voice(request: Request, body: VoiceIn):
        voice = body.model_dump()
        lines = [f"Brand: {voice['brand']}" if voice["brand"] else "",
                 f"Audience: {voice['audience']}" if voice["audience"] else "",
                 voice["notes"]]
        voice["compiled"] = "\n".join(line for line in lines if line.strip())
        store.set_setting(f"voice:{uid(request)}", voice)
        return voice

    @app.get("/api/autopilot")
    def api_autopilot(request: Request):
        settings = service.autopilot_settings(uid(request))
        return {"settings": settings, "next_slot": service.next_slot(settings).isoformat(),
                "topics": store.list_topics(uid(request))}

    @app.put("/api/autopilot")
    def api_save_autopilot(request: Request, body: dict):
        try:
            settings = service.save_autopilot(uid(request), body)
        except (ValueError, TypeError) as e:
            raise HTTPException(422, f"Invalid autopilot settings: {e}")
        return {"settings": settings, "next_slot": service.next_slot(settings).isoformat()}

    @app.post("/api/autopilot/topics", status_code=201)
    def api_add_topic(request: Request, body: TopicIn):
        if not body.source.strip():
            raise HTTPException(422, "Topic is empty")
        return {"id": store.add_topic(body.source.strip(), uid(request))}

    @app.delete("/api/autopilot/topics/{topic_id}", status_code=204)
    def api_delete_topic(request: Request, topic_id: str):
        store.delete_topic(topic_id, uid(request))

    @app.post("/api/autopilot/run-now", status_code=201)
    async def api_run_now(request: Request):
        topic = store.next_topic(uid(request))
        if not topic:
            raise HTTPException(409, "Add a topic to the queue first")
        settings = service.autopilot_settings(uid(request))
        run_id = start(request, topic["source"], settings["platforms"] or None, origin="autopilot")
        store.update_topic(topic["id"], status="used", run_id=run_id)
        return {"id": run_id, "url": f"/app/runs/{run_id}"}

    return app
