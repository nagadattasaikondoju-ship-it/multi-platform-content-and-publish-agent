"""Publishing through Zernio (formerly Late): one REST API for all 13 platforms.

API shape taken from the official zernio-sdk: base https://zernio.com/api,
Bearer auth, GET /v1/accounts, POST /v1/posts with camelCase fields.
Every call that would post defaults to dry-run.
"""

import os
from datetime import datetime, timezone

import httpx

from .models import PlatformPost

# Our platform keys -> Zernio platform names.
ZERNIO_PLATFORM = {
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
    "google_business": "googlebusiness",
}
VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm", ".m4v")


def base_url() -> str:
    return os.getenv("ZERNIO_BASE_URL", "https://zernio.com/api").rstrip("/")


def _headers() -> dict[str, str]:
    api_key = os.environ.get("ZERNIO_API_KEY")
    if not api_key:
        raise RuntimeError("ZERNIO_API_KEY is not set")
    return {"Authorization": f"Bearer {api_key}"}


def list_accounts() -> list[dict]:
    response = httpx.get(f"{base_url()}/v1/accounts", headers=_headers(), timeout=30)
    response.raise_for_status()
    return response.json()["accounts"]


def account_map(accounts: list[dict]) -> dict[str, str]:
    """Our platform key -> the first active connected Zernio account id."""
    by_zernio = {}
    for account in accounts:
        if account.get("isActive", True) and account["platform"] not in by_zernio:
            by_zernio[account["platform"]] = account["_id"]
    return {ours: by_zernio[theirs] for ours, theirs in ZERNIO_PLATFORM.items() if theirs in by_zernio}


def _platform_data(post: PlatformPost, options: dict) -> dict:
    opts = options.get(post.platform, {})
    if post.platform == "youtube":
        return {"title": post.title, **opts}
    if post.platform == "pinterest":
        return {"title": post.title, **opts}  # boardId, link
    if post.platform == "reddit":
        return {"subreddit": post.subreddit, "title": post.title, **opts}
    if post.platform == "google_business":
        # Zernio requires a URL with every call-to-action button.
        if url := opts.get("url"):
            return {"callToAction": {"type": post.cta_type, "url": url}}
        return {}
    if post.platform == "tiktok":
        # Send to the creator's TikTok inbox by default; TikTok requires the
        # creator to confirm consent before direct publishing.
        return {"draft": True, **opts}
    return dict(opts)


def build_payload(
    post: PlatformPost,
    account_id: str,
    *,
    media_urls: list[str] | None = None,
    schedule_at: datetime | None = None,
    options: dict | None = None,
) -> dict:
    """Translate a PlatformPost into a Zernio POST /v1/posts body."""
    target = {"platform": ZERNIO_PLATFORM[post.platform], "accountId": account_id}
    if data := {k: v for k, v in _platform_data(post, options or {}).items() if v is not None}:
        target["platformSpecificData"] = data

    payload: dict = {"content": post.published_text(), "platforms": [target]}
    if post.title and post.platform in ("youtube", "pinterest"):
        payload["title"] = post.title
    if media_urls:
        payload["mediaItems"] = [
            {"type": "video" if url.lower().split("?")[0].endswith(VIDEO_EXTENSIONS) else "image", "url": url}
            for url in media_urls
        ]
    if schedule_at:
        if schedule_at.tzinfo is None:
            raise ValueError("schedule_at must be timezone-aware")
        payload["scheduledFor"] = schedule_at.astimezone(timezone.utc).isoformat()
        payload["timezone"] = "UTC"
    else:
        payload["publishNow"] = True
    return payload


def publish(
    post: PlatformPost,
    account_id: str,
    *,
    media_urls: list[str] | None = None,
    schedule_at: datetime | None = None,
    options: dict | None = None,
    dry_run: bool = True,
) -> dict:
    payload = build_payload(post, account_id, media_urls=media_urls, schedule_at=schedule_at, options=options)
    if dry_run:
        return {"status": "dry_run", "provider": "zernio", "payload": payload}
    response = httpx.post(f"{base_url()}/v1/posts", json=payload, headers=_headers(), timeout=60)
    response.raise_for_status()
    return response.json()
