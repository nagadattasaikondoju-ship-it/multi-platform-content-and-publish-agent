import pytest

from grow_it import video
from grow_it.models import PlatformPost


def test_video_prompt_prefers_script_then_media_prompt_then_body():
    assert video.video_prompt(PlatformPost(platform="tiktok", body="b", script="s")) == "s"
    assert video.video_prompt(PlatformPost(platform="tiktok", body="b", media_prompt="m")) == "m"
    assert video.video_prompt(PlatformPost(platform="tiktok", body="b")) == "b"


def test_generate_video_defaults_to_dry_run(monkeypatch):
    monkeypatch.delenv("TOPVIEW_API_KEY", raising=False)
    monkeypatch.delenv("TOPVIEW_UID", raising=False)
    post = PlatformPost(platform="tiktok", body="b", script="a 30 second script")
    result = video.generate_video(post)
    assert result["status"] == "dry_run"
    assert result["provider"] == "topview"
    assert result["payload"]["prompt"] == "a 30 second script"
    assert result["payload"]["aspectRatio"] == "9:16"


def test_generate_video_dry_run_respects_aspect_ratio_override():
    post = PlatformPost(platform="youtube", body="b", script="s")
    result = video.generate_video(post, aspect_ratio="16:9")
    assert result["payload"]["aspectRatio"] == "16:9"


def test_submit_requires_credentials(monkeypatch):
    monkeypatch.delenv("TOPVIEW_API_KEY", raising=False)
    monkeypatch.delenv("TOPVIEW_UID", raising=False)
    with pytest.raises(RuntimeError, match="TOPVIEW_API_KEY"):
        video.submit_text_to_video("a prompt")


def test_submit_text_to_video_returns_task_id(monkeypatch):
    seen = {}

    def fake_request(method, path, **kwargs):
        seen["method"], seen["path"], seen["json"] = method, path, kwargs["json"]
        return {"taskId": "t1", "status": "init"}

    monkeypatch.setattr(video, "_request", fake_request)
    task_id = video.submit_text_to_video("a prompt", aspect_ratio="9:16", resolution=1080)
    assert task_id == "t1"
    assert seen["method"] == "POST"
    assert seen["path"] == "/common_task/text2video/task/submit"
    assert seen["json"]["aspectRatio"] == "9:16"
    assert seen["json"]["resolution"] == 1080


def test_wait_for_video_polls_until_terminal(monkeypatch):
    calls = {"n": 0}

    def fake_query(task_id, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            return {"status": "running", "videos": []}
        return {"status": "success", "videos": [{"status": "success", "filePath": "https://cdn/v.mp4"}]}

    monkeypatch.setattr(video, "query_task", fake_query)
    monkeypatch.setattr(video.time, "sleep", lambda s: None)
    result = video.wait_for_video("t1", timeout=10, poll_interval=0)
    assert result["videos"][0]["filePath"] == "https://cdn/v.mp4"
    assert calls["n"] == 3


def test_wait_for_video_raises_on_failure(monkeypatch):
    monkeypatch.setattr(video, "query_task", lambda task_id, **k: {"status": "fail", "errorMsg": "boom", "videos": []})
    with pytest.raises(video.TopViewError, match="boom"):
        video.wait_for_video("t1", timeout=10, poll_interval=0)


def test_wait_for_video_times_out(monkeypatch):
    monkeypatch.setattr(video, "query_task", lambda task_id, **k: {"status": "running", "videos": []})
    monkeypatch.setattr(video.time, "monotonic", lambda: 100.0)
    with pytest.raises(video.TopViewError, match="did not finish"):
        video.wait_for_video("t1", timeout=0, poll_interval=0)
