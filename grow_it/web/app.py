"""FastAPI app: marketing pages, the console, and a JSON API behind it."""

import asyncio
import hashlib
import hmac
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from ..llm import GeminiLLM
from ..models import PlatformPost
from ..specs import load_specs
from .service import Service, is_serverless, measure_post, normalise_field, provider_name
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


SESSION_COOKIE = "growit_session"
PROTECTED = ("/app", "/api", "/start")
OPEN_API = ("/api/cron/",)


def _session_token(password: str) -> str:
    secret = os.getenv("GROW_IT_SECRET") or password
    return hmac.new(secret.encode(), b"grow-it-console", hashlib.sha256).hexdigest()


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
        return templates.TemplateResponse(request, name, context)

    # --- access control -----------------------------------------------------
    def password() -> str:
        return os.getenv("GROW_IT_PASSWORD", "")

    def lock_required() -> bool:
        # A deployed console must never be open to the internet.
        return bool(password()) or bool(os.getenv("VERCEL"))

    def signed_in(request: Request) -> bool:
        if not lock_required():
            return True
        if not password():
            return False
        return hmac.compare_digest(request.cookies.get(SESSION_COOKIE, ""), _session_token(password()))

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path.startswith(PROTECTED) and not path.startswith(OPEN_API) and not signed_in(request):
            if path.startswith("/api"):
                return JSONResponse({"detail": "Sign in to the console first"}, status_code=401)
            target = path + (f"?{request.url.query}" if request.url.query else "")
            if path == "/start":
                target = "/app/new"
            return RedirectResponse(f"/login?next={target}", status_code=303)
        return await call_next(request)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/app", error: str = ""):
        return page(request, "login.html", nav="", next=next if next.startswith("/") else "/app",
                    error=error, configured=bool(password()))

    @app.post("/login")
    async def login(request: Request):
        form = await request.form()
        target = str(form.get("next") or "/app")
        target = target if target.startswith("/") and not target.startswith("//") else "/app"
        if password() and hmac.compare_digest(str(form.get("password", "")), password()):
            response = RedirectResponse(target, status_code=303)
            response.set_cookie(SESSION_COOKIE, _session_token(password()), httponly=True,
                                secure=bool(os.getenv("VERCEL")), samesite="lax", max_age=60 * 60 * 24 * 30)
            return response
        return RedirectResponse(f"/login?next={target}&error=1", status_code=303)

    @app.get("/logout")
    def logout():
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
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
        runs = store.list_runs()
        totals: dict[str, int] = {}
        for run in runs:
            for status, n in run["counts"].items():
                totals[status] = totals.get(status, 0) + n
        return page(
            request, "app/dashboard.html", tab="dashboard", runs=runs, totals=totals,
            autopilot=service.autopilot_settings(),
        )

    @app.get("/app/new", response_class=HTMLResponse)
    def new_run(request: Request, source: str = ""):
        return page(request, "app/new.html", tab="new", source=source,
                    voice=store.get_setting("voice", {}))

    @app.get("/app/runs/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: str):
        run = store.get_run(run_id)
        if not run:
            raise HTTPException(404, "Run not found")
        return page(request, "app/run.html", tab="dashboard", run=run)

    @app.get("/app/runs/{run_id}/report", response_class=HTMLResponse)
    def report_page(request: Request, run_id: str):
        run = store.get_run(run_id)
        if not run:
            raise HTTPException(404, "Run not found")
        specs = load_specs()
        for post in run["posts"]:
            if post["data"]:
                post["measure"] = measure_post(PlatformPost.model_validate(post["data"]))
            post["label"] = specs[post["platform"]].label
        return page(request, "app/report.html", tab="dashboard", run=run)

    @app.get("/app/calendar", response_class=HTMLResponse)
    def calendar(request: Request):
        return page(request, "app/calendar.html", tab="calendar", posts=store.scheduled_posts())

    @app.get("/app/autopilot", response_class=HTMLResponse)
    def autopilot_page(request: Request):
        settings = service.autopilot_settings()
        return page(
            request, "app/autopilot.html", tab="autopilot", settings=settings,
            topics=store.list_topics(), next_slot=service.next_slot(settings),
        )

    @app.get("/app/connections", response_class=HTMLResponse)
    async def connections_page(request: Request):
        info = await asyncio.to_thread(service.connections)
        return page(request, "app/connections.html", tab="connections", info=info)

    @app.get("/app/voice", response_class=HTMLResponse)
    def voice_page(request: Request):
        return page(request, "app/voice.html", tab="voice", voice=store.get_setting("voice", {}))

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

    @app.get("/api/platforms")
    def api_platforms():
        return platform_list()

    @app.post("/api/runs", status_code=201)
    async def api_create_run(body: RunIn):
        if not body.source.strip():
            raise HTTPException(422, "Give the loop a topic, a draft or a link")
        run_id = service.start_run(body.source, body.platforms)
        return {"id": run_id, "url": f"/app/runs/{run_id}"}

    @app.post("/start")
    async def form_start(request: Request):
        form = await request.form()
        source = str(form.get("source", "")).strip()
        if not source:
            return RedirectResponse("/app/new", status_code=303)
        platforms = form.getlist("platforms") or None
        run_id = service.start_run(source, [str(p) for p in platforms] if platforms else None)
        return RedirectResponse(f"/app/runs/{run_id}", status_code=303)

    @app.get("/api/runs")
    def api_runs():
        return store.list_runs()

    @app.get("/api/runs/{run_id}")
    def api_run(run_id: str):
        run = store.get_run(run_id)
        if not run:
            raise HTTPException(404, "Run not found")
        for post in run["posts"]:
            if post["data"]:
                post["measure"] = measure_post(PlatformPost.model_validate(post["data"]))
        return run

    @app.post("/api/runs/{run_id}/advance")
    async def api_advance(run_id: str):
        try:
            await service.advance(run_id)
        except ValueError as e:
            raise HTTPException(404, str(e))
        return api_run(run_id)

    @app.get("/api/cron/autopilot")
    async def api_cron(request: Request):
        secret = os.getenv("CRON_SECRET", "")
        if not secret or not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {secret}"):
            raise HTTPException(401, "Unauthorized")
        run_id = service.autopilot_tick()
        if run_id and service.serverless:
            await service.run_to_completion(run_id)
        return {"started": run_id}

    @app.delete("/api/runs/{run_id}", status_code=204)
    def api_delete_run(run_id: str):
        store.delete_run(run_id)

    @app.patch("/api/runs/{run_id}/posts/{platform}")
    def api_edit_post(run_id: str, platform: str, body: PostEdit):
        try:
            post = service.edit_post(run_id, platform, body.model_dump(exclude_unset=True))
        except ValueError as e:
            raise HTTPException(409, str(e))
        post["measure"] = measure_post(PlatformPost.model_validate(post["data"]))
        return post

    @app.post("/api/runs/{run_id}/posts/{platform}/status")
    def api_post_status(run_id: str, platform: str, body: StatusIn):
        try:
            return service.set_status(run_id, platform, body.status)
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/api/runs/{run_id}/posts/{platform}/regenerate", status_code=202)
    async def api_regenerate(run_id: str, platform: str):
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
    async def api_publish(run_id: str, body: PublishIn):
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
    async def api_connections():
        return await asyncio.to_thread(service.connections)

    @app.put("/api/voice")
    def api_voice(body: VoiceIn):
        voice = body.model_dump()
        lines = [f"Brand: {voice['brand']}" if voice["brand"] else "",
                 f"Audience: {voice['audience']}" if voice["audience"] else "",
                 voice["notes"]]
        voice["compiled"] = "\n".join(line for line in lines if line.strip())
        store.set_setting("voice", voice)
        return voice

    @app.get("/api/autopilot")
    def api_autopilot():
        settings = service.autopilot_settings()
        return {"settings": settings, "next_slot": service.next_slot(settings).isoformat(),
                "topics": store.list_topics()}

    @app.put("/api/autopilot")
    def api_save_autopilot(body: dict):
        try:
            settings = service.save_autopilot(body)
        except (ValueError, TypeError) as e:
            raise HTTPException(422, f"Invalid autopilot settings: {e}")
        return {"settings": settings, "next_slot": service.next_slot(settings).isoformat()}

    @app.post("/api/autopilot/topics", status_code=201)
    def api_add_topic(body: TopicIn):
        if not body.source.strip():
            raise HTTPException(422, "Topic is empty")
        return {"id": store.add_topic(body.source.strip())}

    @app.delete("/api/autopilot/topics/{topic_id}", status_code=204)
    def api_delete_topic(topic_id: str):
        store.delete_topic(topic_id)

    @app.post("/api/autopilot/run-now", status_code=201)
    async def api_run_now():
        topic = store.next_topic()
        if not topic:
            raise HTTPException(409, "Add a topic to the queue first")
        settings = service.autopilot_settings()
        run_id = service.start_run(topic["source"], settings["platforms"] or None, origin="autopilot")
        store.update_topic(topic["id"], status="used", run_id=run_id)
        return {"id": run_id, "url": f"/app/runs/{run_id}"}

    return app
