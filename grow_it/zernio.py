"""Publishing through Zernio (formerly Late): one REST API for all 13 platforms.

API shape taken from the official zernio-sdk: base https://zernio.com/api,
Bearer auth, GET /v1/accounts, POST /v1/posts with camelCase fields.
Every call that would post defaults to dry-run.
"""

import os
import time
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


class ZernioError(RuntimeError):
    """A Zernio API error with the human-readable message Zernio sent back."""

    def __init__(self, message: str, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


def _request(method: str, path: str, **kwargs) -> dict:
    # Reads are safe to repeat, so a dropped connection gets one more try.
    attempts = 2 if method in ("GET", "PATCH") else 1
    for attempt in range(attempts):
        try:
            response = httpx.request(method, f"{base_url()}{path}", headers=_headers(), timeout=30, **kwargs)
            break
        except httpx.TransportError:
            if attempt == attempts - 1:
                raise
    if response.status_code >= 400:
        try:
            body = response.json()
        except ValueError:
            body = {}
        message = body.get("error") or body.get("message") or response.text[:200] or response.reason_phrase
        raise ZernioError(str(message), response.status_code, str(body.get("code", "")))
    return response.json() if response.content else {}


def list_accounts(profile_id: str | None = None) -> list[dict]:
    params = {"profileId": profile_id} if profile_id else None
    return _request("GET", "/v1/accounts", params=params)["accounts"]


# --- multi-user: one Zernio profile per Grow it user ----------------------

def create_profile(name: str, description: str = "") -> str:
    """Create a profile and return its id; an existing profile with the name is reused."""
    try:
        body = _request("POST", "/v1/profiles", json={"name": name, "description": description})
        return body["profile"]["_id"]
    except ZernioError as e:
        if e.status != 409:
            raise
    profiles = _request("GET", "/v1/profiles", params={"name": name})["profiles"]
    if not profiles:
        raise ZernioError(f"Profile {name} exists but could not be found", 409)
    return profiles[0]["_id"]


def connect_url(platform: str, profile_id: str, redirect_url: str) -> str:
    """The URL that sends a user to the platform's own sign-in, then back to redirect_url."""
    # A brand-new profile can take a moment to be visible to every platform's
    # connect endpoint, which answers 403 until then.
    for attempt in range(4):
        try:
            body = _request(
                "GET",
                f"/v1/connect/{ZERNIO_PLATFORM[platform]}",
                params={"profileId": profile_id, "redirect_url": redirect_url},
            )
            return body["authUrl"]
        except ZernioError as e:
            if e.status != 403 or "access to this profile" not in str(e) or attempt == 3:
                raise
            time.sleep(1.5)
    raise AssertionError("unreachable")


def current_zernio_user_id() -> str:
    return _request("GET", "/v1/users")["currentUserId"]


def connect_bluesky(profile_id: str, identifier: str, app_password: str) -> dict:
    """Bluesky signs in with an app password instead of OAuth."""
    state = f"{current_zernio_user_id()}-{profile_id}"
    body = _request("POST", "/v1/connect/bluesky/credentials",
                    json={"identifier": identifier, "appPassword": app_password, "state": state})
    return body.get("account", {})


def telegram_code(profile_id: str) -> dict:
    """An access code the user sends to Zernio's Telegram bot to link a channel or group."""
    return _request("GET", "/v1/connect/telegram", params={"profileId": profile_id})


def telegram_status(code: str) -> dict:
    return _request("PATCH", "/v1/connect/telegram", params={"code": code})


def disconnect_account(account_id: str) -> None:
    _request("DELETE", f"/v1/accounts/{account_id}")


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
    return _request("POST", "/v1/posts", json=payload)
