from datetime import datetime, timedelta, timezone

import pytest

from grow_it import zernio
from grow_it.models import PlatformPost
from grow_it.specs import load_specs


def test_every_platform_has_a_zernio_mapping():
    assert set(zernio.ZERNIO_PLATFORM) == set(load_specs())


def test_account_map_uses_first_active_account():
    accounts = [
        {"_id": "a1", "platform": "twitter", "isActive": False},
        {"_id": "a2", "platform": "twitter", "isActive": True},
        {"_id": "a3", "platform": "twitter", "isActive": True},
        {"_id": "b1", "platform": "googlebusiness", "isActive": True},
        {"_id": "c1", "platform": "whatsapp", "isActive": True},
    ]
    assert zernio.account_map(accounts) == {"x": "a2", "google_business": "b1"}


def test_publish_now_payload():
    post = PlatformPost(platform="x", body="hi", hashtags=["ai"])
    payload = zernio.build_payload(post, "acc1")
    assert payload == {
        "content": "hi\n\n#ai",
        "platforms": [{"platform": "twitter", "accountId": "acc1"}],
        "publishNow": True,
    }


def test_scheduled_payload_is_utc():
    post = PlatformPost(platform="linkedin", body="hi")
    ist = timezone(timedelta(hours=5, minutes=30))
    payload = zernio.build_payload(post, "acc1", schedule_at=datetime(2026, 10, 1, 9, tzinfo=ist))
    assert payload["scheduledFor"] == "2026-10-01T03:30:00+00:00"
    assert payload["timezone"] == "UTC"
    assert "publishNow" not in payload
    with pytest.raises(ValueError):
        zernio.build_payload(post, "acc1", schedule_at=datetime(2026, 10, 1, 9))


def test_platform_specific_data():
    reddit = PlatformPost(platform="reddit", title="T", body="B", subreddit="startups")
    target = zernio.build_payload(reddit, "r1")["platforms"][0]
    assert target["platformSpecificData"] == {"subreddit": "startups", "title": "T"}

    yt = PlatformPost(platform="youtube", title="Title", body="Desc", script="s")
    payload = zernio.build_payload(yt, "y1", media_urls=["https://cdn.example.com/v.mp4?sig=1"])
    assert payload["title"] == "Title"
    assert payload["mediaItems"] == [{"type": "video", "url": "https://cdn.example.com/v.mp4?sig=1"}]

    tiktok = PlatformPost(platform="tiktok", body="B", script="s")
    assert zernio.build_payload(tiktok, "t1")["platforms"][0]["platformSpecificData"] == {"draft": True}


def test_google_business_cta_needs_url():
    post = PlatformPost(platform="google_business", body="Open late", cta_type="LEARN_MORE")
    assert "platformSpecificData" not in zernio.build_payload(post, "g1")["platforms"][0]
    payload = zernio.build_payload(post, "g1", options={"google_business": {"url": "https://example.com"}})
    assert payload["platforms"][0]["platformSpecificData"] == {
        "callToAction": {"type": "LEARN_MORE", "url": "https://example.com"}
    }


def test_publish_defaults_to_dry_run(monkeypatch):
    monkeypatch.delenv("ZERNIO_API_KEY", raising=False)
    result = zernio.publish(PlatformPost(platform="bluesky", body="hi"), "b1")
    assert result["status"] == "dry_run"
    assert result["provider"] == "zernio"
