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


# --- source analysis: what the source actually says, with evidence ---------------

CLAIM_KINDS = ("fact", "metric", "quote", "opinion")


class SourceClaim(BaseModel):
    """One statement found in the source, with the exact words that support it."""

    text: str = Field(description="The claim, stated plainly in one sentence.")
    kind: str = Field(description="One of: fact, metric, quote, opinion.")
    snippet: str = Field(
        description="The shortest exact excerpt from the SOURCE that supports the claim, copied "
        "character for character. Empty if the source does not literally support it."
    )


class SourceAnalysis(BaseModel):
    """Everything worth knowing about the source before any post is written."""

    title: str = Field(description="A short working title for this content.")
    thesis: str = Field(description="The single central idea, one sentence.")
    key_insights: list[str] = Field(description="3-6 supporting insights.")
    claims: list[SourceClaim] = Field(
        description="Factual claims, metrics, figures and quotable lines found in the source. "
        "Only what the source says; never add outside knowledge."
    )
    pain_points: list[str] = Field(default_factory=list, description="Audience problems the source addresses.")
    benefits: list[str] = Field(default_factory=list, description="Benefits of the product, service or idea.")
    hooks: list[str] = Field(description="3-5 opening lines grounded in the source.")
    topics: list[str] = Field(default_factory=list, description="2-5 topic clusters.")
    audience: str = Field(description="Who this is most useful for.")
    cta_options: list[str] = Field(default_factory=list, description="2-3 possible calls to action.")
    risky_claims: list[str] = Field(
        default_factory=list,
        description="Statements that would need proof before publishing (superlatives, guarantees, "
        "health/financial/legal claims, comparisons with competitors).",
    )
