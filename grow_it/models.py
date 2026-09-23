from pydantic import BaseModel, Field


class ContentBrief(BaseModel):
    """Platform-neutral distillation of the source material."""

    topic: str
    thesis: str = Field(description="The single core idea, one sentence.")
    audience: str
    key_points: list[str] = Field(description="3-7 supporting points.")
    hooks: list[str] = Field(description="3-5 attention-grabbing opening lines.")
    facts: list[str] = Field(
        default_factory=list,
        description="Numbers or claims taken verbatim from the source. Never invent.",
    )
    cta: str = Field(description="What the reader should do next.")


class PlatformPost(BaseModel):
    """One post, shaped for one platform. Hashtags are kept separate from body."""

    platform: str
    title: str | None = None
    body: str
    hashtags: list[str] = Field(default_factory=list, description="Without the # sign.")
    media_prompt: str | None = Field(
        default=None, description="Prompt for an image/video generator, if media helps."
    )
    script: str | None = None
    subreddit: str | None = None
    cta_type: str | None = None

    def published_text(self) -> str:
        """Body plus hashtags, exactly as it will be posted."""
        if not self.hashtags:
            return self.body
        tags = " ".join(f"#{t.lstrip('#')}" for t in self.hashtags)
        return f"{self.body}\n\n{tags}"


class GeneratedPost(BaseModel):
    """A post after validation, with the record of how it got there."""

    post: PlatformPost
    attempts: int
    errors: list[str] = Field(default_factory=list)
    needs_review: bool = False
