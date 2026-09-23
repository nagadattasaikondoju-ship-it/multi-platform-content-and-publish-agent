"""Who is signed in: Google accounts for the public product, a password or a
local user for single-owner setups. Sessions are HMAC-signed cookies."""

import hashlib
import hmac
import os
import secrets
from urllib.parse import urlencode

import httpx

SESSION_COOKIE = "growit_session"
STATE_COOKIE = "growit_oauth_state"
LOCAL_USER = "local"
OWNER_USER = "owner"

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def google_enabled() -> bool:
    return bool(os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET"))


def password() -> str:
    return os.getenv("GROW_IT_PASSWORD", "")


def mode() -> str:
    """google | password | local | locked."""
    if google_enabled():
        return "google"
    if password():
        return "password"
    if os.getenv("VERCEL"):
        return "locked"  # a deployment is never left open to the internet
    return "local"


def _secret() -> bytes:
    value = (
        os.getenv("GROW_IT_SECRET")
        or os.getenv("GOOGLE_CLIENT_SECRET")
        or password()
        or "grow-it-local-development"
    )
    return value.encode()


def sign(user_id: str) -> str:
    mac = hmac.new(_secret(), f"session:{user_id}".encode(), hashlib.sha256).hexdigest()
    return f"{user_id}.{mac}"


def verify(token: str) -> str | None:
    user_id, _, mac = (token or "").rpartition(".")
    if not user_id:
        return None
    return user_id if hmac.compare_digest(sign(user_id), f"{user_id}.{mac}") else None


def new_state() -> str:
    return secrets.token_urlsafe(24)


def google_login_url(redirect_uri: str, state: str) -> str:
    return GOOGLE_AUTH_URL + "?" + urlencode({
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    })


def google_user(code: str, redirect_uri: str) -> dict:
    """Exchange the callback code for the user's Google identity."""
    token = httpx.post(GOOGLE_TOKEN_URL, data={
        "code": code,
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }, timeout=20)
    token.raise_for_status()
    info = httpx.get(GOOGLE_USERINFO_URL, timeout=20,
                     headers={"Authorization": f"Bearer {token.json()['access_token']}"})
    info.raise_for_status()
    data = info.json()
    if not data.get("email_verified", True):
        raise ValueError("Google says this email address is not verified")
    return data
