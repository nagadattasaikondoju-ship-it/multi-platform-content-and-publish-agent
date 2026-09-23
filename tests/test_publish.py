from datetime import datetime, timezone

import pytest

from grow_it.models import PlatformPost
from grow_it.publish import AYRSHARE_PLATFORM, build_payload, publish
from grow_it.specs import load_specs


def test_every_platform_has_an_ayrshare_mapping():
    assert set(AYRSHARE_PLATFORM) == set(load_specs())


def test_reddit_payload():
    post = PlatformPost(platform="reddit", title="T", body="B", subreddit="startups")
    payload = build_payload(post)
    assert payload["platforms"] == ["reddit"]
    assert payload["redditOptions"] == {"title": "T", "subreddit": "startups"}


def test_schedule_requires_timezone():
    post = PlatformPost(platform="x", body="hi")
    with pytest.raises(ValueError):
        build_payload(post, schedule_at=datetime(2026, 10, 1, 9))
    payload = build_payload(post, schedule_at=datetime(2026, 10, 1, 9, tzinfo=timezone.utc))
    assert payload["scheduleDate"] == "2026-10-01T09:00:00+00:00"


def test_publish_defaults_to_dry_run(monkeypatch):
    monkeypatch.delenv("AYRSHARE_API_KEY", raising=False)
    result = publish(PlatformPost(platform="x", body="hi", hashtags=["ai"]))
    assert result["status"] == "dry_run"
    assert result["payload"]["post"] == "hi\n\n#ai"
