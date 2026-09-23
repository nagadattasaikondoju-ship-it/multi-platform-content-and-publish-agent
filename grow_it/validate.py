"""Deterministic compliance checks. The LLM writes; this module counts."""

import re

import regex

from .models import PlatformPost
from .specs import PlatformSpec

URL_RE = re.compile(r"https?://\S+")
X_URL_WEIGHT = 23
# twitter-text v3: code points in these ranges weigh 1, everything else 2.
X_LIGHT_RANGES = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))
HASHTAG_IN_BODY_RE = re.compile(r"(?<!\w)#\w+")
VALID_TAG_RE = re.compile(r"^\w+$")


def _x_weight(cluster: str) -> int:
    if len(cluster) > 1 or ord(cluster) > 0xFFFF:
        # Emoji sequences and astral symbols count as one heavy character.
        if any(ord(c) > 0xFFFF or c in "‍️" for c in cluster):
            return 2
    return sum(1 if any(lo <= ord(c) <= hi for lo, hi in X_LIGHT_RANGES) else 2 for c in cluster)


def x_weighted_length(text: str) -> int:
    urls = URL_RE.findall(text)
    stripped = URL_RE.sub("", text)
    return len(urls) * X_URL_WEIGHT + sum(_x_weight(g) for g in regex.findall(r"\X", stripped))


def measure(text: str, count_mode: str) -> int:
    if count_mode == "x_weighted":
        return x_weighted_length(text)
    if count_mode == "graphemes":
        return len(regex.findall(r"\X", text))
    return len(text)


def check(post: PlatformPost, spec: PlatformSpec) -> list[str]:
    """Return human-readable errors, phrased so they can be fed back to the model."""
    errors: list[str] = []

    if not post.body.strip():
        errors.append("Body is empty.")

    length = measure(post.published_text(), spec.count_mode)
    if length > spec.max_chars:
        errors.append(
            f"Body plus hashtags is {length} characters; the limit is {spec.max_chars}. "
            f"Cut at least {length - spec.max_chars} characters."
        )

    lo, hi = spec.hashtags
    if not lo <= len(post.hashtags) <= hi:
        errors.append(f"Use between {lo} and {hi} hashtags (got {len(post.hashtags)}).")
    bad_tags = [t for t in post.hashtags if not VALID_TAG_RE.match(t.lstrip("#"))]
    if bad_tags:
        errors.append(f"Hashtags must be single words without spaces or symbols: {bad_tags}.")
    if HASHTAG_IN_BODY_RE.search(post.body):
        errors.append("Do not put hashtags in the body; use the hashtags field only.")

    if spec.title_max:
        if not post.title or not post.title.strip():
            errors.append("A title is required.")
        elif len(post.title) > spec.title_max:
            errors.append(f"Title is {len(post.title)} characters; the limit is {spec.title_max}.")

    for field in spec.extra_required:
        if not (getattr(post, field, None) or "").strip():
            errors.append(f"The field '{field}' is required for {spec.label}.")

    if spec.allowed_cta_types and post.cta_type and post.cta_type not in spec.allowed_cta_types:
        errors.append(f"cta_type must be one of {spec.allowed_cta_types} (got '{post.cta_type}').")

    lowered = post.body.lower()
    for phrase in spec.banned_phrases:
        if phrase.lower() in lowered:
            errors.append(f"Remove the phrase '{phrase}'.")

    return errors


def force_fit(post: PlatformPost, spec: PlatformSpec) -> PlatformPost:
    """Last resort after repair attempts fail: trim hashtags and body to fit."""
    lo, hi = spec.hashtags
    fixed = post.model_copy(
        update={
            "hashtags": [t.lstrip("#") for t in post.hashtags if VALID_TAG_RE.match(t.lstrip("#"))][:hi],
            "body": HASHTAG_IN_BODY_RE.sub("", post.body).strip(),
        }
    )
    if spec.title_max and fixed.title and len(fixed.title) > spec.title_max:
        fixed.title = fixed.title[: spec.title_max - 1].rstrip() + "…"

    words = fixed.body.split(" ")
    while words and measure(fixed.published_text(), spec.count_mode) > spec.max_chars:
        words.pop()
        fixed.body = " ".join(words).rstrip(",.;: ") + "…"
    return fixed
