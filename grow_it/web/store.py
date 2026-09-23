"""SQLite persistence for runs, posts, settings and the autopilot queue."""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    platforms TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    brief TEXT,
    origin TEXT NOT NULL DEFAULT 'manual'
);
CREATE TABLE IF NOT EXISTS posts (
    run_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    status TEXT NOT NULL,
    data TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    errors TEXT NOT NULL DEFAULT '[]',
    needs_review INTEGER NOT NULL DEFAULT 0,
    schedule_at TEXT,
    result TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, platform)
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS topics (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    run_id TEXT
);
"""

# Post lifecycle: queued -> generating -> draft -> approved -> previewed | scheduled | published
# with skipped and failed as side exits.
POST_STATUSES = (
    "queued", "generating", "draft", "approved", "skipped",
    "previewed", "scheduled", "published", "failed",
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._memory = sqlite3.connect(":memory:", check_same_thread=False) if self.path == ":memory:" else None
        with self._conn() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        db = self._memory or sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            if not self._memory:
                db.close()

    # --- runs -------------------------------------------------------------
    def create_run(self, source: str, platforms: list[str], origin: str = "manual") -> str:
        run_id = uuid.uuid4().hex[:10]
        stamp = now()
        with self._conn() as db:
            db.execute(
                "INSERT INTO runs (id, created_at, source, platforms, status, origin) VALUES (?,?,?,?,?,?)",
                (run_id, stamp, source, json.dumps(platforms), "briefing", origin),
            )
            db.executemany(
                "INSERT INTO posts (run_id, platform, status, updated_at) VALUES (?,?,?,?)",
                [(run_id, p, "queued", stamp) for p in platforms],
            )
        return run_id

    def update_run(self, run_id: str, **fields) -> None:
        if "brief" in fields and fields["brief"] is not None:
            fields["brief"] = json.dumps(fields["brief"])
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as db:
            db.execute(f"UPDATE runs SET {sets} WHERE id = ?", (*fields.values(), run_id))

    def get_run(self, run_id: str) -> dict | None:
        with self._conn() as db:
            row = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if not row:
                return None
            posts = db.execute(
                "SELECT * FROM posts WHERE run_id = ? ORDER BY rowid", (run_id,)
            ).fetchall()
        return self._run_dict(row, [self._post_dict(p) for p in posts])

    def list_runs(self, limit: int = 50) -> list[dict]:
        with self._conn() as db:
            rows = db.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            counts = db.execute(
                "SELECT run_id, status, COUNT(*) AS n FROM posts GROUP BY run_id, status"
            ).fetchall()
        tally: dict[str, dict[str, int]] = {}
        for c in counts:
            tally.setdefault(c["run_id"], {})[c["status"]] = c["n"]
        return [self._run_dict(r, None, tally.get(r["id"], {})) for r in rows]

    def delete_run(self, run_id: str) -> None:
        with self._conn() as db:
            db.execute("DELETE FROM posts WHERE run_id = ?", (run_id,))
            db.execute("DELETE FROM runs WHERE id = ?", (run_id,))

    def recover_interrupted(self) -> int:
        """Background work dies with the process; mark anything mid-flight as failed."""
        message = "Interrupted when the server restarted. Start the loop again or rewrite single posts."
        with self._conn() as db:
            count = db.execute(
                "UPDATE runs SET status = 'failed', error = ? WHERE status IN ('briefing', 'writing')",
                (message,),
            ).rowcount
            db.execute(
                "UPDATE posts SET status = 'failed', errors = ?, updated_at = ? "
                "WHERE status IN ('queued', 'generating')",
                (json.dumps(["Interrupted before it was written."]), now()),
            )
        return count

    # --- posts ------------------------------------------------------------
    def update_post(self, run_id: str, platform: str, **fields) -> None:
        for key in ("data", "errors", "result"):
            if key in fields and fields[key] is not None and not isinstance(fields[key], str):
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
        if "needs_review" in fields:
            fields["needs_review"] = int(bool(fields["needs_review"]))
        fields["updated_at"] = now()
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as db:
            db.execute(
                f"UPDATE posts SET {sets} WHERE run_id = ? AND platform = ?",
                (*fields.values(), run_id, platform),
            )

    def get_post(self, run_id: str, platform: str) -> dict | None:
        with self._conn() as db:
            row = db.execute(
                "SELECT * FROM posts WHERE run_id = ? AND platform = ?", (run_id, platform)
            ).fetchone()
        return self._post_dict(row) if row else None

    def scheduled_posts(self) -> list[dict]:
        with self._conn() as db:
            rows = db.execute(
                "SELECT posts.*, runs.source FROM posts JOIN runs ON runs.id = posts.run_id "
                "WHERE posts.schedule_at IS NOT NULL OR posts.status IN ('published','scheduled') "
                "ORDER BY COALESCE(posts.schedule_at, posts.updated_at)"
            ).fetchall()
        return [{**self._post_dict(r), "source": r["source"]} for r in rows]

    # --- settings ---------------------------------------------------------
    def get_setting(self, key: str, default=None):
        with self._conn() as db:
            row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value) -> None:
        with self._conn() as db:
            db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )

    # --- autopilot topics -------------------------------------------------
    def add_topic(self, source: str) -> str:
        topic_id = uuid.uuid4().hex[:10]
        with self._conn() as db:
            db.execute(
                "INSERT INTO topics (id, created_at, source, status) VALUES (?,?,?,?)",
                (topic_id, now(), source, "waiting"),
            )
        return topic_id

    def list_topics(self) -> list[dict]:
        with self._conn() as db:
            rows = db.execute("SELECT * FROM topics ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def next_topic(self) -> dict | None:
        with self._conn() as db:
            row = db.execute(
                "SELECT * FROM topics WHERE status = 'waiting' ORDER BY created_at LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def update_topic(self, topic_id: str, **fields) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as db:
            db.execute(f"UPDATE topics SET {sets} WHERE id = ?", (*fields.values(), topic_id))

    def delete_topic(self, topic_id: str) -> None:
        with self._conn() as db:
            db.execute("DELETE FROM topics WHERE id = ?", (topic_id,))

    # --- helpers ----------------------------------------------------------
    @staticmethod
    def _run_dict(row, posts=None, tally=None) -> dict:
        run = dict(row)
        run["platforms"] = json.loads(run["platforms"])
        run["brief"] = json.loads(run["brief"]) if run["brief"] else None
        if posts is not None:
            run["posts"] = posts
            tally = {}
            for p in posts:
                tally[p["status"]] = tally.get(p["status"], 0) + 1
        run["counts"] = tally or {}
        return run

    @staticmethod
    def _post_dict(row) -> dict:
        post = dict(row)
        post["data"] = json.loads(post["data"]) if post["data"] else None
        post["errors"] = json.loads(post["errors"]) if post["errors"] else []
        post["result"] = json.loads(post["result"]) if post["result"] else None
        post["needs_review"] = bool(post["needs_review"])
        return post
