import pytest

from grow_it.models import PlatformPost
from grow_it.specs import load_specs
from grow_it.validate import check, force_fit, measure, x_weighted_length

SPECS = load_specs()


def test_all_13_platforms_load():
    assert len(SPECS) == 13


def test_x_counts_urls_as_23():
    assert x_weighted_length("hi https://example.com/a/very/long/path/indeed") == 3 + 23


def test_x_counts_cjk_and_emoji_as_2():
    assert x_weighted_length("日本") == 4
    assert x_weighted_length("👍🏽") == 2
    assert x_weighted_length("👨‍👩‍👧") == 2


def test_bluesky_counts_graphemes():
    assert measure("👨‍👩‍👧 hi", "graphemes") == 4


def test_over_limit_is_reported_with_amount():
    post = PlatformPost(platform="x", body="a" * 300)
    errors = check(post, SPECS["x"])
    assert any("Cut at least 20" in e for e in errors)


def test_hashtags_count_toward_limit():
    post = PlatformPost(platform="threads", body="a" * 495, hashtags=["growth"])
    assert check(post, SPECS["threads"])


def test_reddit_rules():
    post = PlatformPost(platform="reddit", body="Link in bio for more #growth", hashtags=["x"])
    errors = " ".join(check(post, SPECS["reddit"]))
    assert "title is required" in errors.lower()
    assert "subreddit" in errors
    assert "link in bio" in errors
    assert "hashtags in the body" in errors
    assert "between 0 and 0" in errors


def test_required_hashtag_range():
    post = PlatformPost(platform="linkedin", body="Hello", hashtags=["one"])
    assert any("between 3 and 5" in e for e in check(post, SPECS["linkedin"]))


def test_hashtag_with_space_rejected():
    post = PlatformPost(platform="x", body="Hello", hashtags=["two words"])
    assert check(post, SPECS["x"])


@pytest.mark.parametrize("key", ["x", "bluesky", "threads", "snapchat"])
def test_force_fit_always_fits(key):
    spec = SPECS[key]
    post = PlatformPost(platform=key, body="word " * 400, hashtags=["a", "b", "c", "d", "e"])
    fitted = force_fit(post, spec)
    assert measure(fitted.published_text(), spec.count_mode) <= spec.max_chars
    assert len(fitted.hashtags) <= spec.hashtags[1]
