from grow_it.models import ContentBrief, PlatformPost

BRIEF = ContentBrief(
    topic="Remote work",
    thesis="Async-first teams ship faster.",
    audience="Startup founders",
    key_points=["Fewer meetings", "Written decisions", "Deep work blocks"],
    hooks=["Your calendar is the bottleneck."],
    facts=[],
    cta="Try one meeting-free day this week.",
)


class FakeLLM:
    """Returns queued posts per platform; records every prompt it receives."""

    def __init__(self, posts_by_platform: dict[str, list[PlatformPost]] | None = None):
        self.queue = {k: list(v) for k, v in (posts_by_platform or {}).items()}
        self.prompts: list[str] = []

    async def structured(self, *, prompt, system, schema, model, temperature):
        self.prompts.append(prompt)
        if schema is ContentBrief:
            return BRIEF
        key = prompt.split("(key: ", 1)[1].split(")", 1)[0]
        queued = self.queue.get(key)
        if queued:
            return queued.pop(0)
        return good_post(key)


def good_post(key: str) -> PlatformPost:
    return PlatformPost(
        platform=key,
        title="Async-first teams ship faster",
        body="Your calendar is the bottleneck. Cut one meeting a day and write decisions down.",
        hashtags={
            "linkedin": ["remotework", "startups", "productivity"],
            "instagram": ["remotework", "startups", "productivity"],
            "tiktok": ["remotework", "startups", "productivity"],
            "youtube": ["remotework", "startups", "productivity"],
        }.get(key, []),
        media_prompt="Minimal illustration of an empty calendar",
        script="Hook: your calendar is the bottleneck...",
        subreddit="startups",
        cta_type="LEARN_MORE",
    )
