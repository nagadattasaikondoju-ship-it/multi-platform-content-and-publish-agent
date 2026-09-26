"""Home dashboard rules: the next best action and the numbers around it."""

from datetime import datetime, timedelta, timezone

from grow_it.web import insights

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)  # a Thursday


def run(id, status="ready", title="Launch", **extra):
    return {"id": id, "status": status, "source": "src", "meta": {"title": title}, "brief": None,
            "created_at": NOW.isoformat(), **extra}


def post(run_id, status, platform="x", schedule_at=None, updated_at=None, errors=()):
    return {"run_id": run_id, "platform": platform, "status": status, "schedule_at": schedule_at,
            "updated_at": updated_at or NOW.isoformat(), "errors": list(errors)}


def action(runs, posts, **kw):
    return insights.next_action(runs, posts, accounts=kw.get("accounts"), provider=kw.get("provider", "zernio"), now=NOW)


def test_new_user_is_told_to_create_a_loop():
    assert action([], [])["url"] == "/app/new" and "first loop" in action([], [])["title"]


def test_actions_are_ranked_by_urgency():
    runs = [run("a"), run("b", status="review", title="Q3 plan")]
    posts = [post("a", "draft"), post("a", "failed", "linkedin")]
    assert action(runs, posts)["title"] == "1 post failed"
    posts = [post("a", "draft")]
    assert action(runs, posts)["url"] == "/app/runs/b" and "evidence" in action(runs, posts)["title"]
    assert action([run("a")], [post("a", "draft"), post("a", "draft", "linkedin")])["title"] == "Finish reviewing 2 posts"
    assert "ready to go" in action([run("a")], [post("a", "approved")])["title"]


def test_disconnected_publishing_and_quiet_periods():
    assert action([run("a")], [post("a", "skipped")], accounts=0)["url"] == "/app/integrations"
    old = (NOW - timedelta(days=10)).isoformat()
    quiet = action([run("a")], [post("a", "published", schedule_at=old)], accounts=2)
    assert "10 days" in quiet["body"]
    soon = (NOW + timedelta(days=1)).isoformat()
    assert action([run("a")], [post("a", "scheduled", schedule_at=soon)], accounts=2)["title"] == "You're on track"


def test_consistency_counts_distinct_days_with_posts_out():
    days = [(NOW - timedelta(days=d)).isoformat() for d in (0, 0, 1, 5, 20)]
    posts = [post("a", "published", schedule_at=d) for d in days]
    c = insights.consistency(posts, NOW)
    assert c == {"score": round(100 * 3 / 14), "active_days": 3, "days": 14}


def test_week_progress_and_notifications():
    tomorrow = (NOW + timedelta(hours=5)).isoformat()
    posts = [post("a", "draft"), post("a", "approved", "linkedin"), post("a", "scheduled", "threads", schedule_at=tomorrow),
             post("a", "failed", "reddit", errors=["boom"])]
    week = insights.week_progress(posts, NOW)
    assert week == {"written": 3, "approved": 2, "scheduled": 1, "published": 0}
    titles = [n["title"] for n in insights.notifications([run("a"), run("b", status="review")], posts, NOW)]
    assert titles[0] == "1 post failed" and "Brief ready for review" in titles
    assert "1 post to approve" in titles and "Threads post goes out soon" in titles


def test_run_title_prefers_meta_then_brief_then_source():
    assert insights.run_title({"meta": {"title": "T"}, "source": "s"}) == "T"
    assert insights.run_title({"meta": {}, "brief": {"topic": "Topic"}, "source": "s"}) == "Topic"
    assert insights.run_title({"meta": {}, "brief": None, "source": "x" * 100}).endswith("…")
