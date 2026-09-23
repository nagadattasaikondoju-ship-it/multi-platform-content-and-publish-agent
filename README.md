# Grow it — Multi-Platform Content & Publish Agent

**One idea. One loop. Thirteen platforms.**

An AI agent built on Google Gemini that turns one topic, draft or article into 13 platform-native posts. Each post is checked for its platform's character limits, hashtag rules and required fields, then published or scheduled through one API (Zernio or Ayrshare).

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
5. Publish          Zernio (or Ayrshare): post now or schedule (dry-run by default)
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
cp .env.example .env   # add GEMINI_API_KEY (and ZERNIO_API_KEY to publish)
```

## Website and console

```bash
grow-it serve            # http://127.0.0.1:8000
```

The site has four public pages (Home, How it works, Platforms, Pricing) and a console:

| Page | What it does |
|---|---|
| `/app` — Loops | Every loop you have run and where each post stands |
| `/app/new` — New loop | Paste a topic, draft or link; pick platforms; start the loop |
| `/app/runs/<id>` | Posts arrive live; edit with a live character meter, approve, skip, rewrite one platform, then publish now, schedule, or preview |
| `/app/runs/<id>/report` | What it read, the brief it chose, what it made, what it sent — printable as a PDF |
| `/app/autopilot` | Idea queue plus a schedule (every N days at a time). Review mode waits for you; publish mode sends posts that pass every check |
| `/app/calendar` | Scheduled and published posts by month |
| `/app/voice` | Brand, audience and voice notes added to every brief |
| `/app/connections` | Which keys are set and which Zernio accounts are connected |

Loops, posts and settings are stored in SQLite at `data/grow_it.db` (override with `GROW_IT_DB`). Background work runs in the server process; if it restarts mid-loop, the loop is marked interrupted and single posts can be rewritten.

The design follows the makerzz.space design system: Archivo and Space Grotesk (self-hosted, SIL OFL), a teal-led palette with a warm yellow accent, pill actions and soft borders.

## Command line

```bash
# All 13 platforms from a topic, a draft file, or an article URL
grow-it generate "Why async-first teams ship faster"
grow-it generate drafts/launch.md --voice brand/voice.md
grow-it generate https://example.com/blog/post --platforms x,linkedin,threads

# Output: runs/run-<timestamp>.json and a readable runs/run-<timestamp>.md

# See which social accounts are connected in Zernio
grow-it accounts

# Preview the payloads (dry-run is the default)
grow-it publish runs/run-20261001-090000.json

# Actually schedule them
grow-it publish runs/run-20261001-090000.json --schedule 2026-10-01T09:00:00+05:30 --live
```

Posts flagged `needs_review` are skipped at publish time unless you pass `--include-flagged`.

`publish` uses Zernio when `ZERNIO_API_KEY` is set, otherwise Ayrshare; force one with `--provider zernio|ayrshare`. With Zernio, each post goes to the first active account connected for that platform, and platforms with no connected account are skipped. TikTok posts go to the creator's TikTok inbox (draft) by default, because TikTok requires the creator to confirm before direct publishing. A Google Business call-to-action button is only added when a URL is supplied.

## Environment Variables

The CLI reads a `.env` file in the current directory automatically (it never overrides variables already set).

| Variable | Needed for |
|---|---|
| `GEMINI_API_KEY` | generation ([get one](https://aistudio.google.com/apikey)) |
| `GROW_IT_BRIEF_MODEL` / `GROW_IT_POST_MODEL` | optional model overrides (default `gemini-flash-latest`; Pro models have no free-tier quota) |
| `GROW_IT_FALLBACK_MODELS` | comma-separated models tried when the main one is overloaded or rate-limited |
| `ZERNIO_API_KEY` | `publish --live` and `accounts` via [Zernio](https://zernio.com) |
| `AYRSHARE_API_KEY` | `publish --live --provider ayrshare` |

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
├── zernio.py               # Zernio accounts + payloads, dry-run by default
├── cli.py                  # `grow-it` command (generate, publish, accounts, serve)
└── web/
    ├── app.py              # FastAPI routes: pages + JSON API
    ├── service.py          # run the loop, edit, publish, autopilot
    ├── store.py            # SQLite persistence
    ├── templates/          # Jinja pages (site + console)
    └── static/             # CSS design system, JS, fonts
tests/                      # run with `pytest` (no API keys needed)
```

## Roadmap

- [x] Content input schema and brand-voice notes
- [x] Rules for all 13 platforms
- [x] Gemini generation with validate-and-repair loop
- [x] Publishing and scheduling through Zernio or Ayrshare (dry-run by default)
- [ ] Media generation: images (Gemini image), carousels, short videos (Remotion)
- [ ] Niche research: scrape top-performing posts to inform the brief
- [x] Website and review console (FastAPI + Jinja)
- [x] Autopilot: idea queue on a schedule, review or publish mode
- [x] Printable run report
- [ ] Best-time scheduling per platform
- [ ] Analytics pulled back into each report
- [ ] Multi-user profiles and billing

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
