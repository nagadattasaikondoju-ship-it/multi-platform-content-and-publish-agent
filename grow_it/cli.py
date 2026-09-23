"""Command line entry point: grow-it generate "<topic | draft | URL | file>"."""

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from .ingest import load_source
from .llm import GeminiLLM
from .pipeline import run
from .publish import publish
from .specs import load_specs


def _render_markdown(brief, results) -> str:
    specs = load_specs()
    lines = [f"# {brief.topic}", "", f"**Thesis:** {brief.thesis}", ""]
    for r in results:
        spec = specs[r.post.platform]
        flag = " ⚠️ needs review" if r.needs_review else ""
        lines += [f"## {spec.label}{flag}", ""]
        if r.post.title:
            lines += [f"**Title:** {r.post.title}", ""]
        lines += [r.post.published_text(), ""]
        for field in ("subreddit", "cta_type", "script", "media_prompt"):
            if value := getattr(r.post, field):
                lines += [f"**{field}:** {value}", ""]
        if r.errors:
            lines += ["Open issues: " + "; ".join(r.errors), ""]
    return "\n".join(lines)


def cmd_generate(args) -> int:
    source = load_source(args.source)
    voice = Path(args.voice).read_text() if args.voice else ""
    platforms = args.platforms.split(",") if args.platforms else None

    brief, results = asyncio.run(run(GeminiLLM(), source, platforms=platforms, voice=voice))

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    data = {"brief": brief.model_dump(), "posts": [r.model_dump() for r in results]}
    json_path = out / f"run-{stamp}.json"
    json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    (out / f"run-{stamp}.md").write_text(_render_markdown(brief, results))

    flagged = [r.post.platform for r in results if r.needs_review]
    print(f"Wrote {len(results)} posts to {json_path}")
    if flagged:
        print(f"Needs review: {', '.join(flagged)}")
    return 0


def cmd_publish(args) -> int:
    from .models import GeneratedPost

    data = json.loads(Path(args.run_file).read_text())
    results = [GeneratedPost.model_validate(p) for p in data["posts"]]
    wanted = set(args.platforms.split(",")) if args.platforms else None
    schedule_at = datetime.fromisoformat(args.schedule) if args.schedule else None

    for r in results:
        if wanted and r.post.platform not in wanted:
            continue
        if r.needs_review and not args.include_flagged:
            print(f"skip {r.post.platform}: needs review")
            continue
        result = publish(r.post, schedule_at=schedule_at, dry_run=not args.live)
        print(r.post.platform, json.dumps(result, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="grow-it")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="Create platform posts from a topic, draft, URL or file")
    gen.add_argument("source")
    gen.add_argument("--platforms", help="Comma-separated keys; default is all 13")
    gen.add_argument("--voice", help="Path to a brand voice notes file")
    gen.add_argument("--out", default="runs")
    gen.set_defaults(func=cmd_generate)

    pub = sub.add_parser("publish", help="Send a generated run to Ayrshare")
    pub.add_argument("run_file")
    pub.add_argument("--platforms")
    pub.add_argument("--schedule", help="ISO-8601 time with timezone, e.g. 2026-10-01T09:00:00+05:30")
    pub.add_argument("--include-flagged", action="store_true")
    pub.add_argument("--live", action="store_true", help="Actually post (default is dry-run)")
    pub.set_defaults(func=cmd_publish)

    sub.add_parser("platforms", help="List supported platforms").set_defaults(
        func=lambda _: print("\n".join(f"{k:16} {s.label}" for k, s in load_specs().items())) or 0
    )

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
