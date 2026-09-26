"""Brief -> parallel per-platform generation -> validate/repair loop."""

import asyncio

from .llm import LLM, brief_model, post_model
from .models import ContentBrief, GeneratedPost, PlatformPost
from .specs import PlatformSpec, load_specs
from .validate import check, force_fit

BRIEF_SYSTEM = """You are a content strategist. Distill the source into a
platform-neutral brief. Only include facts and numbers that appear in the
source; never invent statistics, quotes, or results."""

POST_SYSTEM = """You are a senior social media editor who writes natively for
each platform. Rewrite the brief for exactly ONE platform, obeying every hard
limit. Do not use facts that are not in the brief. Put hashtags only in the
hashtags field, never in the body. Output only the requested JSON."""

MAX_ATTEMPTS = 3


async def make_brief(llm: LLM, source: str, *, voice: str = "") -> ContentBrief:
    prompt = f"SOURCE:\n{source}"
    if voice:
        prompt += f"\n\nBRAND VOICE NOTES:\n{voice}"
    return await llm.structured(
        prompt=prompt,
        system=BRIEF_SYSTEM,
        schema=ContentBrief,
        model=brief_model(),
        temperature=0.3,
    )


NO_FACTS_RULE = """NO VERIFIED FACTS: the brief has no approved facts. Write an opinion-led,
educational or question-led post. Do not state numbers, results, customer stories,
testimonials or anything presented as fact."""


def prohibited_errors(post: PlatformPost, brief: ContentBrief) -> list[str]:
    """Words or phrases the brief bans, found in the post."""
    banned = getattr(brief, "prohibited", None) or []
    text = " ".join(filter(None, [post.title, post.body, post.script, " ".join(post.hashtags)])).lower()
    return [f'Remove the banned phrase "{w}".' for w in banned if w.strip() and w.strip().lower() in text]


def _post_prompt(brief: ContentBrief, spec: PlatformSpec, voice: str, feedback: list[str]) -> str:
    parts = [spec.prompt_block(), f"BRIEF:\n{brief.model_dump_json(indent=2)}"]
    if not brief.facts:
        parts.append(NO_FACTS_RULE)
    if voice:
        parts.append(f"BRAND VOICE NOTES:\n{voice}")
    if feedback:
        parts.append(
            "YOUR PREVIOUS ATTEMPT WAS REJECTED. Fix all of these:\n- " + "\n- ".join(feedback)
        )
    return "\n\n".join(parts)


async def generate_for_platform(
    llm: LLM, brief: ContentBrief, spec: PlatformSpec, *, voice: str = ""
) -> GeneratedPost:
    feedback: list[str] = []
    post: PlatformPost | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        post = await llm.structured(
            prompt=_post_prompt(brief, spec, voice, feedback),
            system=POST_SYSTEM,
            schema=PlatformPost,
            model=post_model(),
            temperature=0.8 if attempt == 1 else 0.4,
        )
        post.platform = spec.key
        feedback = check(post, spec) + prohibited_errors(post, brief)
        if not feedback:
            return GeneratedPost(post=post, attempts=attempt)

    fitted = force_fit(post, spec)
    errors = check(fitted, spec) + prohibited_errors(fitted, brief)
    return GeneratedPost(post=fitted, attempts=MAX_ATTEMPTS, errors=errors or feedback, needs_review=True)


async def run(
    llm: LLM,
    source: str,
    *,
    platforms: list[str] | None = None,
    voice: str = "",
    concurrency: int = 5,
) -> tuple[ContentBrief, list[GeneratedPost]]:
    specs = load_specs()
    selected = platforms or list(specs)
    unknown = set(selected) - set(specs)
    if unknown:
        raise ValueError(f"Unknown platforms: {sorted(unknown)}. Known: {sorted(specs)}")

    brief = await make_brief(llm, source, voice=voice)
    gate = asyncio.Semaphore(concurrency)

    async def one(key: str) -> GeneratedPost:
        async with gate:
            return await generate_for_platform(llm, brief, specs[key], voice=voice)

    posts = await asyncio.gather(*(one(k) for k in selected))
    return brief, list(posts)
