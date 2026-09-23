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


def test_load_dotenv_does_not_override(tmp_path, monkeypatch):
    from grow_it.cli import load_dotenv

    env = tmp_path / ".env"
    env.write_text("# comment\nGROW_IT_A='from-file'\nGROW_IT_B=from-file\n")
    monkeypatch.delenv("GROW_IT_A", raising=False)
    monkeypatch.setenv("GROW_IT_B", "from-env")
    load_dotenv(str(env))
    import os

    assert os.environ["GROW_IT_A"] == "from-file"
    assert os.environ["GROW_IT_B"] == "from-env"
    monkeypatch.delenv("GROW_IT_A")
