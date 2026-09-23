# Multi-Platform Content & Publish Agent (grow-it)

An AI agent built on Google Gemini that turns one topic, draft or article into 13 platform-native posts. Each post is checked for its platform's character limits, hashtag rules and required fields, then published or scheduled through one API.

## How It Works

```
topic | draft | URL | file
        │
        ▼
1. Ingest           URL → readable article text (trafilatura)
2. Brief            Gemini Flash → thesis, key points, hooks, facts, CTA
3. Fan-out          Gemini Flash, one call per platform, in parallel,
                    structured JSON output (Pydantic schema)
4. Validate/repair  Deterministic checks. On failure the exact errors
                    are fed back to Gemini (up to 3 attempts), then
                    force-fit and flagged for human review
5. Publish          Ayrshare API: post now or schedule (dry-run by default)
```

LLMs can't count characters reliably, so the model writes and `grow_it/validate.py` does the counting:

- X uses weighted length (URLs count 23, CJK characters and emoji count 2).
- Bluesky counts graphemes.
- Every other platform counts plain characters.

## Supported Platforms

All rules live in [`grow_it/config/platforms.yaml`](grow_it/config/platforms.yaml). The prompt and the validator both read from this file.

| Key | Platform | Limit | Hashtags | Extra required |
|---|---|---|---|---|
| `x` | X (Twitter) | 280 weighted | 0–2 | |
| `linkedin` | LinkedIn | 3,000 | 3–5 | |
| `instagram` | Instagram | 2,200 | 3–5 | media |
| `facebook` | Facebook | 63,206 (aim ~400) | 0–3 | |
| `threads` | Threads | 500 | 0–1 | |
| `bluesky` | Bluesky | 300 graphemes | 0–2 | |
| `tiktok` | TikTok | 4,000 | 3–5 | script, media |
| `youtube` | YouTube Shorts | 5,000 + title 100 | 3–5 | title, script, media |
| `pinterest` | Pinterest | 500 + title 100 | 0–3 | title, media |
| `reddit` | Reddit | 40,000 + title 300 | none | title, subreddit |
| `telegram` | Telegram | 4,096 | 0–3 | |
| `snapchat` | Snapchat | 160 | 0–3 | media |
| `google_business` | Google Business Profile | 1,500 | none | cta_type |

Platforms change their limits often, so check them again before relying on them.

## Setup

```bash
git clone https://github.com/nagadattasaikondoju-ship-it/multi-platform-content-and-publish-agent.git
cd multi-platform-content-and-publish-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # add GEMINI_API_KEY (and AYRSHARE_API_KEY to publish)
export $(grep -v '^#' .env | xargs)
```

## Usage

```bash
# All 13 platforms from a topic, a draft file, or an article URL
grow-it generate "Why async-first teams ship faster"
grow-it generate drafts/launch.md --voice brand/voice.md
grow-it generate https://example.com/blog/post --platforms x,linkedin,threads

# Output: runs/run-<timestamp>.json and a readable runs/run-<timestamp>.md

# Preview the Ayrshare payloads (dry-run is the default)
grow-it publish runs/run-20261001-090000.json

# Actually schedule them
grow-it publish runs/run-20261001-090000.json --schedule 2026-10-01T09:00:00+05:30 --live
```

Posts flagged `needs_review` are skipped at publish time unless you pass `--include-flagged`.

## Environment Variables

The CLI reads a `.env` file in the current directory automatically (it never overrides variables already set).

| Variable | Needed for |
|---|---|
| `GEMINI_API_KEY` | generation ([get one](https://aistudio.google.com/apikey)) |
| `GROW_IT_BRIEF_MODEL` / `GROW_IT_POST_MODEL` | optional model overrides (default `gemini-flash-latest`; Pro models have no free-tier quota) |
| `GROW_IT_FALLBACK_MODELS` | comma-separated models tried when the main one is overloaded or rate-limited |
| `AYRSHARE_API_KEY` | `publish --live` |

Never commit API keys, access tokens, or credentials to the repository.

## Project Structure

```text
grow_it/
├── config/platforms.yaml   # per-platform rules (single source of truth)
├── models.py               # ContentBrief, PlatformPost, GeneratedPost
├── specs.py                # loads platforms.yaml, renders prompt rules
├── ingest.py               # topic / file / URL → source text
├── llm.py                  # Gemini client behind a small protocol (fakeable)
├── pipeline.py             # brief → parallel fan-out → validate/repair
├── validate.py             # deterministic compliance checks + force_fit
├── publish.py              # Ayrshare payloads, dry-run by default
└── cli.py                  # `grow-it` command
tests/                      # run with `pytest` (no API keys needed)
```

## Roadmap

- [x] Content input schema and brand-voice notes
- [x] Rules for all 13 platforms
- [x] Gemini generation with validate-and-repair loop
- [x] Publishing and scheduling through Ayrshare (dry-run by default)
- [ ] Media generation: images (Gemini image), carousels, short videos (Remotion)
- [ ] Niche research: scrape top-performing posts to inform the brief
- [ ] Review and approval dashboard (FastAPI + web UI or Telegram bot)
- [ ] Posting cadence and best-time scheduling with a job queue
- [ ] Analytics pull and PDF run report
- [ ] Multi-user profiles (Ayrshare `Profile-Key`) and billing

## Contributing

Contributions, workflow ideas, prompt improvements, and integration suggestions are welcome.

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Open a pull request with a clear explanation

## License

Add a license before using this project publicly or commercially. The MIT License is a common option for open-source projects.

## Author

Created by [Naga Datta](https://github.com/nagadattasaikondoju-ship-it).
