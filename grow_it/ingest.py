"""Turn whatever the user gives us (topic, draft, URL, file) into source text."""

from pathlib import Path

import httpx

MAX_SOURCE_CHARS = 30_000


def load_source(value: str) -> str:
    if value.startswith(("http://", "https://")):
        return _fetch_article(value)
    path = Path(value)
    if len(value) < 4096 and path.is_file():
        return path.read_text()[:MAX_SOURCE_CHARS]
    return value[:MAX_SOURCE_CHARS]


def _fetch_article(url: str) -> str:
    import trafilatura

    html = httpx.get(url, follow_redirects=True, timeout=30).text
    text = trafilatura.extract(html, include_comments=False) or ""
    if not text.strip():
        raise ValueError(f"Could not extract readable text from {url}")
    return f"Source URL: {url}\n\n{text[:MAX_SOURCE_CHARS]}"
