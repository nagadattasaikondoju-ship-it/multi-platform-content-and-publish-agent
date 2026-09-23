"""Command line entry point: grow-it generate "<topic | draft | URL | file>"."""

import argparse
import asyncio
import json
import os
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


def _provider(requested: str) -> str:
    if requested != "auto":
        return requested
    if os.environ.get("ZERNIO_API_KEY"):
        return "zernio"
    return "ayrshare"


def cmd_publish(args) -> int:
    from . import zernio
    from .models import GeneratedPost

    data = json.loads(Path(args.run_file).read_text())
    results = [GeneratedPost.model_validate(p) for p in data["posts"]]
    wanted = set(args.platforms.split(",")) if args.platforms else None
    schedule_at = datetime.fromisoformat(args.schedule) if args.schedule else None
    provider = _provider(args.provider)

    accounts: dict[str, str] = {}
    if provider == "zernio":
        try:
            accounts = zernio.account_map(zernio.list_accounts())
            print(f"Zernio accounts connected for: {', '.join(sorted(accounts)) or 'none'}")
        except Exception as e:
            if args.live:
                raise
            # A dry-run can still show the payloads without reaching Zernio.
            print(f"Could not list Zernio accounts ({e}); previewing with placeholder ids")
            accounts = {key: f"<{key}-account-id>" for key in zernio.ZERNIO_PLATFORM}

    for r in results:
        key = r.post.platform
        if wanted and key not in wanted:
            continue
        if r.needs_review and not args.include_flagged:
            print(f"skip {key}: needs review")
            continue
        if provider == "zernio":
            if key not in accounts:
                print(f"skip {key}: no connected Zernio account")
                continue
            result = zernio.publish(r.post, accounts[key], schedule_at=schedule_at, dry_run=not args.live)
        else:
            result = publish(r.post, schedule_at=schedule_at, dry_run=not args.live)
        print(key, json.dumps(result, ensure_ascii=False))
    return 0


def cmd_accounts(args) -> int:
    from . import zernio

    for account in zernio.list_accounts():
        name = account.get("username") or account.get("displayName") or ""
        print(f"{account['platform']:16} {account['_id']}  {name}")
    return 0


def load_dotenv(path: str = ".env") -> None:
    """Read KEY=VALUE lines from .env without overriding the real environment."""
    env = Path(path)
    if not env.is_file():
        return
    for line in env.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="grow-it")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="Create platform posts from a topic, draft, URL or file")
    gen.add_argument("source")
    gen.add_argument("--platforms", help="Comma-separated keys; default is all 13")
    gen.add_argument("--voice", help="Path to a brand voice notes file")
    gen.add_argument("--out", default="runs")
    gen.set_defaults(func=cmd_generate)

    pub = sub.add_parser("publish", help="Send a generated run to Zernio or Ayrshare")
    pub.add_argument("run_file")
    pub.add_argument("--platforms")
    pub.add_argument("--schedule", help="ISO-8601 time with timezone, e.g. 2026-10-01T09:00:00+05:30")
    pub.add_argument("--include-flagged", action="store_true")
    pub.add_argument(
        "--provider",
        choices=["auto", "zernio", "ayrshare"],
        default="auto",
        help="auto uses Zernio when ZERNIO_API_KEY is set, otherwise Ayrshare",
    )
    pub.add_argument("--live", action="store_true", help="Actually post (default is dry-run)")
    pub.set_defaults(func=cmd_publish)

    sub.add_parser("accounts", help="List social accounts connected in Zernio").set_defaults(
        func=cmd_accounts
    )

    sub.add_parser("platforms", help="List supported platforms").set_defaults(
        func=lambda _: print("\n".join(f"{k:16} {s.label}" for k, s in load_specs().items())) or 0
    )

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
