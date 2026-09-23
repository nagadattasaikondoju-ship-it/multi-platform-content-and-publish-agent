from grow_it.models import PlatformPost
from grow_it.pipeline import run

from .fakes import FakeLLM


async def test_generates_all_13_on_first_try():
    brief, results = await run(FakeLLM(), "source text")
    assert brief.topic == "Remote work"
    assert len(results) == 13
    assert all(r.attempts == 1 and not r.needs_review for r in results)


async def test_repair_loop_feeds_errors_back():
    too_long = PlatformPost(platform="x", body="a" * 400)
    llm = FakeLLM({"x": [too_long]})
    _, [result] = await run(llm, "source", platforms=["x"])
    assert result.attempts == 2
    assert not result.needs_review
    retry_prompt = [p for p in llm.prompts if "(key: x)" in p][-1]
    assert "PREVIOUS ATTEMPT WAS REJECTED" in retry_prompt
    assert "Cut at least 120" in retry_prompt


async def test_gives_up_after_three_and_force_fits():
    bad = PlatformPost(platform="bluesky", body="word " * 200)
    llm = FakeLLM({"bluesky": [bad, bad, bad]})
    _, [result] = await run(llm, "source", platforms=["bluesky"])
    assert result.attempts == 3
    assert result.needs_review
    assert len(result.post.body) <= 300


async def test_unknown_platform_rejected():
    import pytest

    with pytest.raises(ValueError, match="Unknown platforms"):
        await run(FakeLLM(), "source", platforms=["myspace"])
