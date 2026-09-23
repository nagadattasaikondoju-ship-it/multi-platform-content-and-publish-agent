from functools import cache
from importlib.resources import files

import yaml
from pydantic import BaseModel


class PlatformSpec(BaseModel):
    key: str
    label: str
    max_chars: int
    soft_max_chars: int | None = None
    count_mode: str = "chars"
    hashtags: tuple[int, int] = (0, 30)
    title_max: int | None = None
    tone: str
    format: str
    needs_media: bool = False
    extra_required: list[str] = []
    banned_phrases: list[str] = []
    allowed_cta_types: list[str] = []

    def prompt_block(self) -> str:
        lo, hi = self.hashtags
        lines = [
            f"Platform: {self.label} (key: {self.key})",
            f"HARD LIMIT: body + hashtags <= {self.max_chars} characters.",
            f"Hashtags: between {lo} and {hi}.",
            f"Tone: {self.tone}",
            f"Format: {self.format}",
        ]
        if self.soft_max_chars:
            lines.append(f"Aim for about {self.soft_max_chars} characters or fewer.")
        if self.title_max:
            lines.append(f"Title is REQUIRED, <= {self.title_max} characters.")
        if self.extra_required:
            lines.append(f"Required fields: {', '.join(self.extra_required)}.")
        if self.needs_media:
            lines.append("This platform needs media: fill media_prompt.")
        if self.banned_phrases:
            lines.append(f"Never use: {', '.join(self.banned_phrases)}.")
        return "\n".join(lines)


@cache
def load_specs() -> dict[str, PlatformSpec]:
    raw = yaml.safe_load(files("grow_it.config").joinpath("platforms.yaml").read_text())
    return {key: PlatformSpec(key=key, **cfg) for key, cfg in raw.items()}
