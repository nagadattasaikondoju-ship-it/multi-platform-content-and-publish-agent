"""Publishing through Ayrshare, which covers all 13 platforms with one API.

Every function defaults to dry-run so nothing reaches a real account unless
the caller opts in explicitly.
"""

import os
from datetime import datetime

import httpx

from .models import PlatformPost

AYRSHARE_URL = "https://api.ayrshare.com/api/post"

# Our platform keys -> Ayrshare platform names.
AYRSHARE_PLATFORM = {
    "x": "twitter",
    "linkedin": "linkedin",
    "instagram": "instagram",
    "facebook": "facebook",
    "threads": "threads",
    "bluesky": "bluesky",
    "tiktok": "tiktok",
    "youtube": "youtube",
    "pinterest": "pinterest",
    "reddit": "reddit",
    "telegram": "telegram",
    "snapchat": "snapchat",
    "google_business": "gmb",
}


def build_payload(
    post: PlatformPost,
    *,
    media_urls: list[str] | None = None,
    schedule_at: datetime | None = None,
    options: dict | None = None,
) -> dict:
    """Translate a PlatformPost into an Ayrshare /post request body."""
    options = options or {}
    payload: dict = {
        "post": post.published_text(),
        "platforms": [AYRSHARE_PLATFORM[post.platform]],
    }
    if media_urls:
        payload["mediaUrls"] = media_urls
    if schedule_at:
        if schedule_at.tzinfo is None:
            raise ValueError("schedule_at must be timezone-aware")
        payload["scheduleDate"] = schedule_at.isoformat()

    if post.platform == "youtube":
        payload["youTubeOptions"] = {"title": post.title, "shorts": True}
    elif post.platform == "reddit":
        payload["redditOptions"] = {"title": post.title, "subreddit": post.subreddit}
    elif post.platform == "pinterest":
        payload["pinterestOptions"] = {"title": post.title, **options.get("pinterest", {})}
    elif post.platform == "google_business":
        cta = {"actionType": post.cta_type}
        if url := options.get("google_business", {}).get("url"):
            cta["url"] = url
        payload["gmbOptions"] = {"callToAction": cta}
    return payload


def publish(
    post: PlatformPost,
    *,
    media_urls: list[str] | None = None,
    schedule_at: datetime | None = None,
    options: dict | None = None,
    profile_key: str | None = None,
    dry_run: bool = True,
) -> dict:
    payload = build_payload(post, media_urls=media_urls, schedule_at=schedule_at, options=options)
    if dry_run:
        return {"status": "dry_run", "payload": payload}

    api_key = os.environ.get("AYRSHARE_API_KEY")
    if not api_key:
        raise RuntimeError("AYRSHARE_API_KEY is not set")
    headers = {"Authorization": f"Bearer {api_key}"}
    if profile_key:
        headers["Profile-Key"] = profile_key
    response = httpx.post(AYRSHARE_URL, json=payload, headers=headers, timeout=60)
    response.raise_for_status()
    return response.json()
