"""Who is signed in: Auth0, Google or Neon Auth sign-in for the public product, a password
or a local user for single-owner setups. Sessions are HMAC-signed cookies."""

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


def auth0_enabled() -> bool:
    return bool(os.getenv("AUTH0_DOMAIN") and os.getenv("AUTH0_CLIENT_ID") and os.getenv("AUTH0_CLIENT_SECRET"))


def neon_enabled() -> bool:
    return bool(os.getenv("NEON_AUTH_BASE_URL"))


def provider() -> str | None:
    """Which hosted sign-in is configured: Auth0, then Google, then Neon Auth."""
    if auth0_enabled():
        return "auth0"
    if google_enabled():
        return "google"
    if neon_enabled():
        return "neon"
    return None


def callback_path() -> str:
    return "/auth/google/callback" if provider() == "google" else "/auth/callback"


def _auth0_base() -> str:
    domain = os.environ["AUTH0_DOMAIN"].strip().removeprefix("https://").rstrip("/")
    return f"https://{domain}"


def password() -> str:
    return os.getenv("GROW_IT_PASSWORD", "")


def mode() -> str:
    """oauth | password | local | locked."""
    if provider():
        return "oauth"
    if password():
        return "password"
    if os.getenv("VERCEL"):
        return "locked"  # a deployment is never left open to the internet
    return "local"


def _secret() -> bytes:
    value = (
        os.getenv("GROW_IT_SECRET")
        or os.getenv("AUTH0_CLIENT_SECRET")
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


def auth0_login_url(redirect_uri: str, state: str) -> str:
    return _auth0_base() + "/authorize?" + urlencode({
        "client_id": os.environ["AUTH0_CLIENT_ID"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
    })


def auth0_user(code: str, redirect_uri: str) -> dict:
    token = httpx.post(_auth0_base() + "/oauth/token", data={
        "grant_type": "authorization_code",
        "client_id": os.environ["AUTH0_CLIENT_ID"],
        "client_secret": os.environ["AUTH0_CLIENT_SECRET"],
        "code": code,
        "redirect_uri": redirect_uri,
    }, timeout=20)
    token.raise_for_status()
    info = httpx.get(_auth0_base() + "/userinfo", timeout=20,
                     headers={"Authorization": f"Bearer {token.json()['access_token']}"})
    info.raise_for_status()
    return info.json()


def auth0_logout_url(return_to: str) -> str:
    return _auth0_base() + "/v2/logout?" + urlencode(
        {"client_id": os.environ["AUTH0_CLIENT_ID"], "returnTo": return_to}
    )


# --- Neon Auth (managed Better Auth, added with the Neon database on Vercel) ---------
# The browser never talks to Neon Auth directly: this server asks it for the Google
# sign-in link, keeps the "challenge" cookie it returns in our own cookie, and after
# Google sends the person back with a one-time verifier, swaps both for the user.

NEON_VERIFIER_PARAM = "neon_auth_session_verifier"
NEON_CHALLENGE_COOKIE = "growit_neon_challenge"
NEON_COOKIE_PREFIX = "__Secure-neon-auth"


def _neon_base() -> str:
    return os.environ["NEON_AUTH_BASE_URL"].rstrip("/")


def _neon_headers(origin: str, cookie: str = "") -> dict:
    headers = {"Origin": origin, "x-neon-auth-middleware": "true"}
    if cookie:
        headers["Cookie"] = cookie
    return headers


def neon_start(callback_url: str, origin: str) -> tuple[str, str]:
    """Ask Neon Auth to start a Google sign-in. Returns (Google URL, challenge cookies)."""
    response = httpx.post(_neon_base() + "/sign-in/social", timeout=20,
                          json={"provider": "google", "callbackURL": callback_url},
                          headers=_neon_headers(origin))
    if response.status_code >= 400:
        raise ValueError(f"Neon Auth refused the sign-in ({response.status_code}): {response.text[:200]}")
    challenge = "; ".join(
        f"{c.name}={c.value}" for c in response.cookies.jar if c.name.startswith(NEON_COOKIE_PREFIX)
    )
    url = response.json().get("url") or response.headers.get("location", "")
    if not url or not challenge:
        raise ValueError("Neon Auth did not return a sign-in link")
    return url, challenge


def neon_user(verifier: str, challenge: str, origin: str) -> dict:
    """Swap the verifier (from the callback URL) and our stored challenge for the user."""
    response = httpx.get(_neon_base() + "/get-session", timeout=20,
                         params={NEON_VERIFIER_PARAM: verifier},
                         headers=_neon_headers(origin, challenge))
    response.raise_for_status()
    user = (response.json() or {}).get("user") or {}
    if not user.get("id"):
        raise ValueError("Neon Auth returned no user")
    return {"sub": f"neon|{user['id']}", "email": user.get("email", ""),
            "name": user.get("name", ""), "picture": user.get("image") or ""}


def login_url(redirect_uri: str, state: str) -> str:
    if provider() == "auth0":
        return auth0_login_url(redirect_uri, state)
    return google_login_url(redirect_uri, state)


def fetch_user(code: str, redirect_uri: str) -> dict:
    """The signed-in person's identity: at least sub, and usually email, name, picture."""
    if provider() == "auth0":
        return auth0_user(code, redirect_uri)
    return google_user(code, redirect_uri)
