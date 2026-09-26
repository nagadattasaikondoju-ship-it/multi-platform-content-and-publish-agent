"""What the console should tell someone right now: next best action, alerts, progress.

Pure functions over runs and posts, so the rules are easy to test and to extend.
"""

from datetime import datetime, timedelta, timezone

from ..specs import load_specs

WAITING = ("draft",)
DONE = ("scheduled", "published")


def run_title(run: dict) -> str:
    title = (run.get("meta") or {}).get("title") or ""
    if not title and run.get("brief"):
        title = run["brief"].get("campaign") or run["brief"].get("topic") or ""
    source = " ".join((run.get("source") or "").split())
    return title or (source[:80] + ("…" if len(source) > 80 else ""))


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        value = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _label(platform: str) -> str:
    spec = load_specs().get(platform)
    return spec.label if spec else platform


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def activity_day(post: dict) -> datetime | None:
    """When a post went (or goes) out: its schedule, or when it was published."""
    if post["status"] == "scheduled":
        return _parse(post.get("schedule_at"))
    if post["status"] == "published":
        return _parse(post.get("schedule_at")) or _parse(post.get("updated_at"))
    return None


def consistency(posts: list[dict], now: datetime, days: int = 14) -> dict:
    """Share of the last `days` days with at least one post out."""
    start = (now - timedelta(days=days - 1)).date()
    active = {d.date() for p in posts if (d := activity_day(p)) and start <= d.date() <= now.date()}
    score = round(100 * len(active) / days)
    return {"score": score, "active_days": len(active), "days": days}


def next_action(runs: list[dict], posts: list[dict], *, accounts: int | None, provider: str,
                now: datetime) -> dict:
    """The single most useful thing to do next, most urgent first."""
    by_run = {r["id"]: r for r in runs}
    failed = [p for p in posts if p["status"] == "failed"]
    if failed:
        run = by_run.get(failed[0]["run_id"], {})
        return {"tone": "error", "title": f"{_plural(len(failed), 'post')} failed",
                "body": f"Rewrite or skip them in “{run_title(run)}” so the loop can finish.",
                "url": f"/app/runs/{failed[0]['run_id']}", "cta": "Fix posts"}
    reviewing = [r for r in runs if r["status"] == "review"]
    if reviewing:
        return {"tone": "accent", "title": "Check the source evidence",
                "body": f"“{run_title(reviewing[0])}” is waiting for you to approve its facts and brief "
                        "before any post is written.",
                "url": f"/app/runs/{reviewing[0]['id']}", "cta": "Review brief"}
    waiting = [p for p in posts if p["status"] in WAITING]
    if waiting:
        run_id = waiting[0]["run_id"]
        in_run = sum(1 for p in waiting if p["run_id"] == run_id)
        return {"tone": "accent", "title": f"Finish reviewing {_plural(in_run, 'post')}",
                "body": f"In “{run_title(by_run.get(run_id, {}))}”. Approve what's ready, edit or rewrite the rest.",
                "url": f"/app/runs/{run_id}", "cta": "Review posts"}
    approved = [p for p in posts if p["status"] == "approved"]
    if approved:
        return {"tone": "accent", "title": f"{_plural(len(approved), 'approved post')} ready to go",
                "body": "Schedule or publish them so they don't go stale.",
                "url": f"/app/runs/{approved[0]['run_id']}", "cta": "Schedule"}
    if not runs:
        return {"tone": "accent", "title": "Create your first loop",
                "body": "Paste one idea, draft or link. You'll check the facts and the brief, then get a post "
                        "for every platform you choose.",
                "url": "/app/new", "cta": "Create a loop"}
    if provider == "zernio" and accounts == 0:
        return {"tone": "warn", "title": "Connect a social account",
                "body": "Posts can be written and previewed now; connect an account to publish or schedule them.",
                "url": "/app/integrations", "cta": "Connect"}
    last_out = max((d for p in posts if (d := activity_day(p)) and d <= now), default=None)
    upcoming = [d for p in posts if (d := activity_day(p)) and d > now]
    if not upcoming:
        quiet = (now - last_out).days if last_out else None
        body = (f"Nothing has gone out in {_plural(quiet, 'day')}, and nothing is scheduled."
                if quiet and quiet >= 3 else "Nothing is scheduled yet. Plan the next few days while ideas are fresh.")
        return {"tone": "accent", "title": "Plan your next posts", "body": body, "url": "/app/new", "cta": "Create a loop"}
    return {"tone": "ok", "title": "You're on track",
            "body": f"{_plural(len(upcoming), 'post')} scheduled. Start another loop to keep the calendar full.",
            "url": "/app/new", "cta": "Create a loop"}


def week_progress(posts: list[dict], now: datetime) -> dict:
    start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=7)
    this_week = [p for p in posts if (d := _parse(p.get("updated_at"))) and d >= start]
    out = [p for p in posts if (d := activity_day(p)) and start <= d < end]
    return {
        "written": sum(1 for p in this_week if p["status"] not in ("queued", "generating", "failed")),
        "approved": sum(1 for p in this_week if p["status"] in ("approved", *DONE)),
        "scheduled": sum(1 for p in out if p["status"] == "scheduled"),
        "published": sum(1 for p in out if p["status"] == "published"),
    }


def notifications(runs: list[dict], posts: list[dict], now: datetime) -> list[dict]:
    by_run = {r["id"]: r for r in runs}
    items = []
    failed_runs: dict[str, int] = {}
    for p in posts:
        if p["status"] == "failed":
            failed_runs[p["run_id"]] = failed_runs.get(p["run_id"], 0) + 1
    for run_id, n in failed_runs.items():
        items.append({"tone": "error", "title": f"{_plural(n, 'post')} failed",
                      "body": run_title(by_run.get(run_id, {})), "url": f"/app/runs/{run_id}"})
    for run in runs:
        if run["status"] == "review":
            items.append({"tone": "accent", "title": "Brief ready for review", "body": run_title(run),
                          "url": f"/app/runs/{run['id']}"})
        elif run["status"] == "failed" and run.get("error"):
            items.append({"tone": "error", "title": "Loop stopped", "body": run["error"][:120],
                          "url": f"/app/runs/{run['id']}"})
    waiting: dict[str, int] = {}
    for p in posts:
        if p["status"] in WAITING:
            waiting[p["run_id"]] = waiting.get(p["run_id"], 0) + 1
    for run_id, n in waiting.items():
        items.append({"tone": "warn", "title": f"{_plural(n, 'post')} to approve",
                      "body": run_title(by_run.get(run_id, {})), "url": f"/app/runs/{run_id}"})
    soon = now + timedelta(hours=24)
    for p in posts:
        at = _parse(p.get("schedule_at"))
        if p["status"] == "scheduled" and at and now <= at <= soon:
            items.append({"tone": "ok", "title": f"{_label(p['platform'])} post goes out soon",
                          "body": run_title(by_run.get(p["run_id"], {})), "url": f"/app/runs/{p['run_id']}"})
    return items[:12]


def overview(runs: list[dict], posts: list[dict], *, accounts: int | None, provider: str,
             now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    by_run = {r["id"]: r for r in runs}
    decorate = lambda p: {**p, "title": run_title(by_run.get(p["run_id"], p)), "label": _label(p["platform"])}  # noqa: E731
    upcoming = sorted((p for p in posts if p["status"] == "scheduled" and (_parse(p.get("schedule_at")) or now) >= now),
                      key=lambda p: p.get("schedule_at") or "")
    return {
        "next": next_action(runs, posts, accounts=accounts, provider=provider, now=now),
        "week": week_progress(posts, now),
        "consistency": consistency(posts, now),
        "pending": [decorate(p) for p in posts if p["status"] in WAITING][:6],
        "upcoming": [decorate(p) for p in upcoming][:6],
        "failed": [decorate(p) for p in posts if p["status"] == "failed"][:6],
        "reviewing": [r for r in runs if r["status"] == "review"][:4],
        "recent": runs[:5],
        "has_published": any(p["status"] == "published" for p in posts),
    }
