"""Short-video generation through TopView's text-to-video API.

API shape from the official docs (base https://api.topview.ai/v1), Bearer
auth plus a Topview-Uid header, submit-then-poll for each task. Every call
that would spend credits defaults to dry-run.
"""

import os
import time

import httpx

from .models import PlatformPost

# Vertical video for short-form feeds; platforms not listed here don't need one.
PLATFORM_ASPECT_RATIO = {
    "tiktok": "9:16",
    "youtube": "9:16",
    "instagram": "9:16",
    "snapchat": "9:16",
    "pinterest": "9:16",
}

DEFAULT_MODEL = "Topview Pro"
TERMINAL_STATUSES = ("success", "fail")


def base_url() -> str:
    return os.getenv("TOPVIEW_BASE_URL", "https://api.topview.ai/v1").rstrip("/")


def _headers() -> dict[str, str]:
    api_key = os.environ.get("TOPVIEW_API_KEY")
    uid = os.environ.get("TOPVIEW_UID")
    if not api_key or not uid:
        raise RuntimeError("TOPVIEW_API_KEY and TOPVIEW_UID must both be set")
    return {"Authorization": f"Bearer {api_key}", "Topview-Uid": uid}


class TopViewError(RuntimeError):
    """A TopView API error, or a task that finished with status=fail."""

    def __init__(self, message: str, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


def _request(method: str, path: str, **kwargs) -> dict:
    response = httpx.request(method, f"{base_url()}{path}", headers=_headers(), timeout=30, **kwargs)
    if response.status_code >= 400:
        try:
            body = response.json()
        except ValueError:
            body = {}
        message = body.get("message") or response.text[:200] or response.reason_phrase
        raise TopViewError(str(message), response.status_code, str(body.get("code", "")))
    body = response.json()
    if str(body.get("code", "200")) != "200":
        raise TopViewError(body.get("message", "TopView request failed"), response.status_code, str(body.get("code", "")))
    return body["result"]


def submit_text_to_video(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    aspect_ratio: str | None = "9:16",
    resolution: int | None = None,
    duration: int | None = None,
    sound: str = "on",
    generating_count: int = 1,
) -> str:
    """Submit a text-to-video task and return its taskId."""
    body = {
        "model": model,
        "prompt": prompt,
        "sound": sound,
        "generatingCount": generating_count,
    }
    if aspect_ratio:
        body["aspectRatio"] = aspect_ratio
    if resolution:
        body["resolution"] = resolution
    if duration:
        body["duration"] = duration
    return _request("POST", "/common_task/text2video/task/submit", json=body)["taskId"]


def query_task(task_id: str, *, need_cloud_front_url: bool | None = None) -> dict:
    params = {"taskId": task_id}
    if need_cloud_front_url is not None:
        params["needCloudFrontUrl"] = need_cloud_front_url
    return _request("GET", "/common_task/text2video/task/query", params=params)


def wait_for_video(task_id: str, *, timeout: float = 300, poll_interval: float = 5) -> dict:
    """Poll a task until every video is done, and return the raw result."""
    deadline = time.monotonic() + timeout
    while True:
        result = query_task(task_id)
        if result["status"] in TERMINAL_STATUSES and all(v["status"] in TERMINAL_STATUSES for v in result.get("videos", [])):
            if result["status"] == "fail":
                raise TopViewError(result.get("errorMsg") or "TopView task failed")
            return result
        if time.monotonic() >= deadline:
            raise TopViewError(f"TopView task {task_id} did not finish within {timeout}s")
        time.sleep(poll_interval)


def video_prompt(post: PlatformPost) -> str:
    """The best text we have to describe the video: script, then media prompt, then body."""
    return post.script or post.media_prompt or post.body


def generate_video(
    post: PlatformPost,
    *,
    model: str = DEFAULT_MODEL,
    aspect_ratio: str | None = None,
    resolution: int | None = None,
    duration: int | None = None,
    wait: bool = True,
    timeout: float = 300,
    dry_run: bool = True,
) -> dict:
    """Generate a short video for a platform post via TopView.

    Returns {"status": "dry_run", ...} without spending credits unless
    dry_run=False. With wait=True (the default) it polls until the video is
    ready and the result's "videos" list carries the final filePath URLs.
    """
    prompt = video_prompt(post)
    ratio = aspect_ratio or PLATFORM_ASPECT_RATIO.get(post.platform, "9:16")
    if dry_run:
        return {
            "status": "dry_run",
            "provider": "topview",
            "payload": {
                "model": model,
                "prompt": prompt,
                "aspectRatio": ratio,
                "resolution": resolution,
                "duration": duration,
            },
        }
    task_id = submit_text_to_video(
        prompt, model=model, aspect_ratio=ratio, resolution=resolution, duration=duration
    )
    if not wait:
        return {"status": "running", "provider": "topview", "taskId": task_id}
    result = wait_for_video(task_id, timeout=timeout)
    return {"status": "success", "provider": "topview", **result}
