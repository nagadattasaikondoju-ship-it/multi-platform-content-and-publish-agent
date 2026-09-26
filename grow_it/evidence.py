"""Source analysis and the evidence trail behind every post.

The model proposes claims, each with the exact excerpt it came from. We never take
its word for it: a claim only counts as supported when its excerpt really appears
in the source text. The person then approves, edits or drops each claim, and only
approved claims reach the posts.
"""

import re
import uuid

from .llm import LLM, brief_model
from .models import ContentBrief, SourceAnalysis
from .specs import load_specs

ANALYSIS_SYSTEM = """You are a meticulous content strategist and fact checker.
Read the SOURCE and extract what it actually says. Rules:
- Only use the SOURCE. Never add outside knowledge, statistics, customers, results or quotes.
- For every claim, copy the supporting excerpt from the SOURCE exactly, character for character.
  If the source does not literally say it, leave the excerpt empty.
- A short idea with no facts is fine: return few or no claims rather than inventing any.
- Flag superlatives, guarantees, comparisons and health, financial or legal claims as risky."""

CONTENT_STYLES = (
    "Educational", "Contrarian", "Founder story", "Case-study style", "How-to", "Listicle",
    "Thought leadership", "Promotional", "Community conversation", "Customer pain-point focused",
    "Product update", "Local business update",
)

OBJECTIVES = (
    "Build personal brand", "Generate B2B leads", "Promote a service", "Launch a product",
    "Drive website traffic", "Grow audience", "Local business visibility",
    "Build thought leadership", "Keep content consistency",
)

INPUT_TYPES = {
    "idea": "Quick idea",
    "draft": "Draft",
    "article": "Article or blog post",
    "url": "Web page",
    "document": "Uploaded document",
    "reuse": "Past loop",
}


class LoopBrief(ContentBrief):
    """The editable brief a person signs off before any post is written."""

    campaign: str = ""
    pain_point: str = ""
    desired_outcome: str = ""
    angle: str = ""
    secondary_cta: str = ""
    tone: str = ""
    style: str = ""
    pillars: list[str] = []
    prohibited: list[str] = []
    keywords: list[str] = []
    hashtag_guidance: str = ""
    approval_required: bool = False


def _normalise(text: str) -> str:
    text = text.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip()


def in_source(snippet: str, source: str) -> bool:
    """Whether the excerpt really appears in the source (ignoring case, spacing and curly quotes)."""
    snippet = _normalise(snippet).strip(" .\"'")
    return len(snippet) >= 8 and snippet in _normalise(source)


def context_block(meta: dict) -> str:
    fields = [
        ("Title", meta.get("title")), ("Source type", INPUT_TYPES.get(meta.get("input_type", ""), "")),
        ("Source URL", meta.get("url")), ("Author", meta.get("author")), ("Source date", meta.get("source_date")),
        ("Objective", meta.get("objective")), ("Audience", meta.get("audience")), ("Desired CTA", meta.get("cta")),
    ]
    lines = [f"{k}: {v}" for k, v in fields if v]
    return "CONTEXT FROM THE USER:\n" + "\n".join(lines) if lines else ""


async def analyze_source(llm: LLM, source: str, meta: dict | None = None, voice: str = "") -> SourceAnalysis:
    parts = [f"SOURCE:\n{source}"]
    if block := context_block(meta or {}):
        parts.append(block)
    if voice:
        parts.append(f"BRAND VOICE NOTES:\n{voice}")
    return await llm.structured(
        prompt="\n\n".join(parts), system=ANALYSIS_SYSTEM, schema=SourceAnalysis,
        model=brief_model(), temperature=0.2,
    )


def evidence_from(analysis: SourceAnalysis, source: str) -> list[dict]:
    """Claims with a verdict. Supported claims start approved; unsupported ones never do."""
    evidence = []
    for claim in analysis.claims:
        supported = bool(claim.snippet) and in_source(claim.snippet, source)
        evidence.append({
            "id": uuid.uuid4().hex[:8],
            "text": claim.text.strip(),
            "kind": claim.kind if claim.kind in ("fact", "metric", "quote", "opinion") else "fact",
            "snippet": claim.snippet.strip() if supported else "",
            "supported": supported,
            "approved": supported,
            "edited": False,
        })
    return evidence


def draft_brief(analysis: SourceAnalysis, meta: dict, evidence: list[dict], platforms: list[str],
                voice: dict | None = None) -> dict:
    voice = voice or {}
    specs = load_specs()
    return LoopBrief(
        topic=analysis.title,
        thesis=analysis.thesis,
        audience=meta.get("audience") or analysis.audience,
        key_points=analysis.key_insights,
        hooks=analysis.hooks,
        facts=[e["text"] for e in evidence if e["approved"]],
        cta=meta.get("cta") or voice.get("default_cta") or (analysis.cta_options[0] if analysis.cta_options else ""),
        campaign=meta.get("campaign", ""),
        pain_point=analysis.pain_points[0] if analysis.pain_points else "",
        desired_outcome=meta.get("objective", ""),
        secondary_cta=analysis.cta_options[1] if len(analysis.cta_options) > 1 else "",
        tone=voice.get("tone", ""),
        style=meta.get("style") or "Educational",
        pillars=analysis.topics[:4],
        prohibited=[w.strip() for w in (voice.get("avoid") or "").split(",") if w.strip()],
        keywords=analysis.topics[:5],
        hashtag_guidance=f"Brand hashtags: {voice['hashtags']}" if voice.get("hashtags") else "",
    ).model_dump() | {"platforms": [p for p in platforms if p in specs]}


def final_brief(brief: dict, evidence: list[dict]) -> LoopBrief:
    """The brief the writers get: approved claims only, whatever the brief's fact list said."""
    fields = {k: v for k, v in brief.items() if k in LoopBrief.model_fields}
    fields["facts"] = [e["text"].strip() for e in evidence if e.get("approved") and e.get("text", "").strip()]
    return LoopBrief.model_validate(fields)
