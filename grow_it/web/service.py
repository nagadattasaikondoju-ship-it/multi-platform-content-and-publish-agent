"""What the web app does, independent of HTTP: run the loop, edit, publish, autopilot."""

import asyncio
import logging
import os
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from .. import zernio
from ..ingest import load_source
from ..llm import LLM
from ..models import ContentBrief, PlatformPost
from ..pipeline import generate_for_platform, make_brief
from ..publish import publish as ayrshare_publish
from ..specs import load_specs
from ..validate import check, measure
from .store import Store, now

log = logging.getLogger("grow_it.web")

EDITABLE_FIELDS = ("title", "body", "hashtags", "media_prompt", "script", "subreddit", "cta_type")
DEFAULT_AUTOPILOT = {
    "enabled": False,
    "every_days": 1,
    "time": "09:00",
    "utc_offset_minutes": 330,
    "platforms": [],
    "mode": "review",
    "last_run_at": None,
}


def provider_name() -> str:
    if os.environ.get("ZERNIO_API_KEY"):
        return "zernio"
    if os.environ.get("AYRSHARE_API_KEY"):
        return "ayrshare"
    return "none"


def measure_post(post: PlatformPost) -> dict:
    spec = load_specs()[post.platform]
    return {
        "length": measure(post.published_text(), spec.count_mode),
        "max": spec.max_chars,
        "soft_max": spec.soft_max_chars,
        "errors": check(post, spec),
    }


class Service:
    def __init__(self, store: Store, llm_factory: Callable[[], LLM]):
        self.store = store
        self._llm_factory = llm_factory
        self._llm: LLM | None = None
        self._tasks: set[asyncio.Task] = set()

    @property
    def llm(self) -> LLM:
        if self._llm is None:
            self._llm = self._llm_factory()
        return self._llm

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def wait_idle(self) -> None:
        """Wait for background work to finish (used by tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # --- the loop ---------------------------------------------------------
    def start_run(self, source: str, platforms: list[str] | None = None, origin: str = "manual") -> str:
        specs = load_specs()
        selected = [p for p in (platforms or list(specs)) if p in specs] or list(specs)
        run_id = self.store.create_run(source.strip(), selected, origin)
        self._spawn(self._generate(run_id))
        return run_id

    async def _generate(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        voice = self.store.get_setting("voice", {}).get("compiled", "")
        try:
            source = await asyncio.to_thread(load_source, run["source"])
            brief = await make_brief(self.llm, source, voice=voice)
        except Exception as e:
            log.exception("brief failed for %s", run_id)
            self.store.update_run(run_id, status="failed", error=_short(e))
            for p in run["platforms"]:
                self.store.update_post(run_id, p, status="failed", errors=["The brief could not be written."])
            return

        self.store.update_run(run_id, status="writing", brief=brief.model_dump())
        gate = asyncio.Semaphore(5)
        await asyncio.gather(*(self._write_one(run_id, brief, p, voice, gate) for p in run["platforms"]))

        final = self.store.get_run(run_id)
        failed = all(p["status"] == "failed" for p in final["posts"])
        self.store.update_run(run_id, status="failed" if failed else "ready")
        if final["origin"] == "autopilot" and not failed:
            await self._autopilot_after_run(run_id)

    async def _write_one(self, run_id, brief: ContentBrief, platform: str, voice: str, gate) -> None:
        async with gate:
            self.store.update_post(run_id, platform, status="generating")
            try:
                result = await generate_for_platform(self.llm, brief, load_specs()[platform], voice=voice)
            except Exception as e:
                log.exception("generation failed for %s/%s", run_id, platform)
                self.store.update_post(run_id, platform, status="failed", errors=[_short(e)])
                return
            self.store.update_post(
                run_id,
                platform,
                status="draft",
                data=result.post.model_dump(),
                attempts=result.attempts,
                errors=result.errors,
                needs_review=result.needs_review,
            )

    def regenerate(self, run_id: str, platform: str) -> None:
        run = self.store.get_run(run_id)
        if not run or not run["brief"]:
            raise ValueError("This run has no brief yet")
        brief = ContentBrief.model_validate(run["brief"])
        voice = self.store.get_setting("voice", {}).get("compiled", "")
        self.store.update_post(run_id, platform, status="queued", errors=[])
        self._spawn(self._write_one(run_id, brief, platform, voice, asyncio.Semaphore(1)))

    # --- review -----------------------------------------------------------
    def edit_post(self, run_id: str, platform: str, changes: dict) -> dict:
        current = self.store.get_post(run_id, platform)
        if not current or not current["data"]:
            raise ValueError("Nothing to edit yet")
        data = dict(current["data"])
        for key in EDITABLE_FIELDS:
            if key in changes:
                data[key] = normalise_field(key, changes[key])
        post = PlatformPost.model_validate(data)
        errors = check(post, load_specs()[platform])
        status = current["status"] if current["status"] in ("draft", "approved", "skipped") else "draft"
        if errors and status == "approved":
            status = "draft"
        self.store.update_post(
            run_id, platform, data=post.model_dump(), errors=errors, needs_review=bool(errors), status=status
        )
        return self.store.get_post(run_id, platform)

    def set_status(self, run_id: str, platform: str, status: str) -> dict:
        if status not in ("draft", "approved", "skipped"):
            raise ValueError("Status must be draft, approved or skipped")
        post = self.store.get_post(run_id, platform)
        if not post or not post["data"]:
            raise ValueError("This post has not been written yet")
        if status == "approved" and post["errors"]:
            raise ValueError("Fix the flagged problems before approving")
        self.store.update_post(run_id, platform, status=status)
        return self.store.get_post(run_id, platform)

    # --- publish ----------------------------------------------------------
    def publish(
        self,
        run_id: str,
        platforms: list[str],
        *,
        schedule_at: datetime | None = None,
        live: bool = False,
        options: dict | None = None,
    ) -> list[dict]:
        provider = provider_name()
        accounts: dict[str, str] = {}
        if provider == "zernio":
            try:
                accounts = zernio.account_map(zernio.list_accounts())
            except Exception as e:
                if live:
                    raise RuntimeError(f"Could not reach Zernio: {_short(e)}") from e
                accounts = {k: f"<{k}-account-id>" for k in zernio.ZERNIO_PLATFORM}

        outcomes = []
        for platform in platforms:
            post = self.store.get_post(run_id, platform)
            if not post or not post["data"]:
                continue
            if post["errors"]:
                outcomes.append({"platform": platform, "ok": False, "message": "Has unresolved problems"})
                continue
            model = PlatformPost.model_validate(post["data"])
            try:
                if provider == "zernio":
                    if platform not in accounts:
                        raise RuntimeError("No connected Zernio account for this platform")
                    result = zernio.publish(
                        model, accounts[platform], schedule_at=schedule_at, options=options, dry_run=not live
                    )
                else:
                    result = ayrshare_publish(model, schedule_at=schedule_at, options=options, dry_run=not live)
            except Exception as e:
                self.store.update_post(run_id, platform, status="failed", result={"error": _short(e)})
                outcomes.append({"platform": platform, "ok": False, "message": _short(e)})
                continue

            if not live:
                status = "previewed"
            elif schedule_at:
                status = "scheduled"
            else:
                status = "published"
            self.store.update_post(
                run_id,
                platform,
                status=status,
                result={"provider": provider if provider != "none" else "ayrshare", "live": live, **result},
                schedule_at=schedule_at.isoformat() if schedule_at else (now() if live else None),
            )
            outcomes.append({"platform": platform, "ok": True, "status": status})
        return outcomes

    # --- connections ------------------------------------------------------
    def connections(self) -> dict:
        info = {
            "gemini": bool(os.environ.get("GEMINI_API_KEY")),
            "zernio": bool(os.environ.get("ZERNIO_API_KEY")),
            "ayrshare": bool(os.environ.get("AYRSHARE_API_KEY")),
            "provider": provider_name(),
            "accounts": [],
            "accounts_error": None,
        }
        if info["zernio"]:
            try:
                info["accounts"] = [
                    {
                        "platform": a.get("platform"),
                        "name": a.get("username") or a.get("displayName") or "",
                        "active": a.get("isActive", True),
                    }
                    for a in zernio.list_accounts()
                ]
            except Exception as e:
                info["accounts_error"] = _short(e)
        return info

    # --- autopilot --------------------------------------------------------
    def autopilot_settings(self) -> dict:
        return {**DEFAULT_AUTOPILOT, **self.store.get_setting("autopilot", {})}

    def save_autopilot(self, changes: dict) -> dict:
        settings = self.autopilot_settings()
        for key in ("enabled", "every_days", "time", "utc_offset_minutes", "platforms", "mode"):
            if key in changes:
                settings[key] = changes[key]
        settings["every_days"] = max(1, min(30, int(settings["every_days"])))
        if settings["mode"] not in ("review", "publish"):
            settings["mode"] = "review"
        datetime.strptime(settings["time"], "%H:%M")  # validates format
        self.store.set_setting("autopilot", settings)
        return settings

    def next_slot(self, settings: dict, current: datetime | None = None) -> datetime:
        """The next time the loop should fire, in UTC."""
        current = current or datetime.now(timezone.utc)
        tz = timezone(timedelta(minutes=int(settings["utc_offset_minutes"])))
        hour, minute = map(int, settings["time"].split(":"))
        local_now = current.astimezone(tz)
        slot = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        last = settings.get("last_run_at")
        if last:
            last_local = datetime.fromisoformat(last).astimezone(tz)
            earliest = last_local.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(
                days=int(settings["every_days"])
            )
            slot = max(slot, earliest)
            if slot <= last_local:
                slot += timedelta(days=int(settings["every_days"]))
        elif slot < local_now - timedelta(minutes=5):
            slot += timedelta(days=1)
        return slot.astimezone(timezone.utc)

    def autopilot_tick(self, current: datetime | None = None) -> str | None:
        """Start a run if a slot is due and a topic is waiting. Returns the run id."""
        current = current or datetime.now(timezone.utc)
        settings = self.autopilot_settings()
        if not settings["enabled"] or self.next_slot(settings, current) > current:
            return None
        topic = self.store.next_topic()
        if not topic:
            return None
        run_id = self.start_run(topic["source"], settings["platforms"] or None, origin="autopilot")
        self.store.update_topic(topic["id"], status="used", run_id=run_id)
        settings["last_run_at"] = current.isoformat()
        self.store.set_setting("autopilot", settings)
        return run_id

    async def _autopilot_after_run(self, run_id: str) -> None:
        if self.autopilot_settings()["mode"] != "publish":
            return
        run = self.store.get_run(run_id)
        clean = [p["platform"] for p in run["posts"] if p["status"] == "draft" and not p["errors"]]
        for platform in clean:
            self.store.update_post(run_id, platform, status="approved")
        await asyncio.to_thread(self.publish, run_id, clean, live=True)

    async def autopilot_forever(self, interval: float = 60) -> None:
        while True:
            try:
                self.autopilot_tick()
            except Exception:
                log.exception("autopilot tick failed")
            await asyncio.sleep(interval)


def normalise_field(key: str, value):
    if key == "hashtags":
        if isinstance(value, str):
            value = value.replace(",", " ").split()
        return [t.lstrip("#").strip() for t in value if t.strip().lstrip("#")]
    if isinstance(value, str):
        value = value.strip()
    if key == "body":
        return value or ""
    return value or None


def _short(error: Exception) -> str:
    text = str(error) or error.__class__.__name__
    return text if len(text) <= 300 else text[:297] + "…"
