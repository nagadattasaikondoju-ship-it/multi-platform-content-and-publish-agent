"""Persistence for runs, posts, settings and the autopilot queue.

SQLite locally; Postgres (e.g. Neon on Vercel) when given a postgres:// URL.
The SQL is written once with ? placeholders and translated for Postgres.
"""

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
    position INTEGER NOT NULL DEFAULT 0,
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


def is_postgres_url(value: str) -> bool:
    return value.startswith(("postgres://", "postgresql://"))


class _DB:
    """Tiny adapter so the same SQL runs on sqlite3 and psycopg."""

    def __init__(self, conn, postgres: bool):
        self.conn = conn
        self.postgres = postgres

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.postgres else sql

    def execute(self, sql: str, params=()):
        return self.conn.execute(self._sql(sql), params)

    def executemany(self, sql: str, rows):
        if self.postgres:
            cur = self.conn.cursor()
            cur.executemany(self._sql(sql), rows)
            return cur
        return self.conn.executemany(sql, rows)


class Store:
    def __init__(self, target: str | Path):
        self.path = str(target)
        self.postgres = is_postgres_url(self.path)
        self._memory = None
        if self.postgres:
            self.kind = "postgres"
        else:
            self.kind = "memory" if self.path == ":memory:" else "sqlite"
            if self.path == ":memory:":
                self._memory = sqlite3.connect(":memory:", check_same_thread=False)
            else:
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as db:
            if self.postgres:
                db.conn.execute(SCHEMA)
                db.conn.execute("ALTER TABLE posts ADD COLUMN IF NOT EXISTS position INTEGER NOT NULL DEFAULT 0")
            else:
                db.conn.executescript(SCHEMA)
                try:  # databases created before the position column existed
                    db.conn.execute("ALTER TABLE posts ADD COLUMN position INTEGER NOT NULL DEFAULT 0")
                except sqlite3.OperationalError:
                    pass

    @contextmanager
    def _conn(self):
        if self.postgres:
            import psycopg
            from psycopg.rows import dict_row

            conn = psycopg.connect(self.path, row_factory=dict_row, connect_timeout=10)
        else:
            conn = self._memory or sqlite3.connect(self.path, timeout=10)
            conn.row_factory = sqlite3.Row
        try:
            yield _DB(conn, self.postgres)
            conn.commit()
        finally:
            if not self._memory:
                conn.close()

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
                "INSERT INTO posts (run_id, platform, status, updated_at, position) VALUES (?,?,?,?,?)",
                [(run_id, p, "queued", stamp, i) for i, p in enumerate(platforms)],
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
                "SELECT * FROM posts WHERE run_id = ? ORDER BY position", (run_id,)
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

    def claim_posts(self, run_id: str, limit: int, stale_after_seconds: int = 240) -> list[str]:
        """Mark up to `limit` waiting posts as generating and return their platforms.

        A post stuck in 'generating' longer than `stale_after_seconds` is taken
        to belong to a request that died, and is claimed again.
        """
        cutoff = datetime.now(timezone.utc).timestamp() - stale_after_seconds
        with self._conn() as db:
            rows = db.execute(
                "SELECT platform, status, updated_at FROM posts WHERE run_id = ? "
                "AND status IN ('queued', 'generating') ORDER BY position",
                (run_id,),
            ).fetchall()
            claimable = [
                r["platform"] for r in rows
                if r["status"] == "queued" or datetime.fromisoformat(r["updated_at"]).timestamp() < cutoff
            ][:limit]
            stamp = now()
            for platform in claimable:
                db.execute(
                    "UPDATE posts SET status = 'generating', updated_at = ? WHERE run_id = ? AND platform = ?",
                    (stamp, run_id, platform),
                )
        return claimable

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
