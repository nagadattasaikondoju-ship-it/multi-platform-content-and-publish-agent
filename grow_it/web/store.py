"""Persistence for runs, posts, settings and the autopilot queue.

SQLite locally; Postgres (e.g. Neon on Vercel) when given a postgres:// URL.
The SQL is written once with ? placeholders and translated for Postgres.
"""

import json
import sqlite3
import threading
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
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    email TEXT,
    name TEXT,
    picture TEXT,
    google_sub TEXT UNIQUE,
    zernio_profile_id TEXT
);
CREATE TABLE IF NOT EXISTS topics (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    run_id TEXT
);
"""

MIGRATIONS = (
    ("posts", "position", "INTEGER NOT NULL DEFAULT 0"),
    ("runs", "user_id", "TEXT"),
    ("topics", "user_id", "TEXT"),
    ("runs", "meta", "TEXT"),
    ("runs", "analysis", "TEXT"),
    ("runs", "evidence", "TEXT"),
    ("runs", "source_text", "TEXT"),
)

JSON_RUN_FIELDS = ("brief", "meta", "analysis", "evidence")

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
        self._memory_lock = threading.RLock()
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
            else:
                db.conn.executescript(SCHEMA)
        # Columns added after the first release; adding them is a no-op once present.
        for table, column, ddl in MIGRATIONS:
            self._add_column(table, column, ddl)

    def _add_column(self, table: str, column: str, ddl: str) -> None:
        with self._conn() as db:
            if self.postgres:
                db.conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}")
            else:
                try:
                    db.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                except sqlite3.OperationalError:
                    pass

    @contextmanager
    def _conn(self):
        # The shared in-memory connection (tests) must not be used by two threads at once;
        # file and Postgres stores open a fresh connection per call instead.
        if self._memory:
            with self._memory_lock:
                with self._open() as db:
                    yield db
            return
        with self._open() as db:
            yield db

    @contextmanager
    def _open(self):
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
    def create_run(self, source: str, platforms: list[str], origin: str = "manual",
                   user_id: str | None = None, status: str = "briefing", meta: dict | None = None) -> str:
        run_id = uuid.uuid4().hex[:10]
        stamp = now()
        with self._conn() as db:
            db.execute(
                "INSERT INTO runs (id, created_at, source, platforms, status, origin, user_id, meta) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (run_id, stamp, source, json.dumps(platforms), status, origin, user_id,
                 json.dumps(meta or {})),
            )
            db.executemany(
                "INSERT INTO posts (run_id, platform, status, updated_at, position) VALUES (?,?,?,?,?)",
                [(run_id, p, "queued", stamp, i) for i, p in enumerate(platforms)],
            )
        return run_id

    def update_run(self, run_id: str, **fields) -> None:
        for key in JSON_RUN_FIELDS:
            if fields.get(key) is not None:
                fields[key] = json.dumps(fields[key])
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as db:
            db.execute(f"UPDATE runs SET {sets} WHERE id = ?", (*fields.values(), run_id))

    def set_platforms(self, run_id: str, platforms: list[str]) -> None:
        """Replace a run's platforms before any post is written (review stage)."""
        stamp = now()
        with self._conn() as db:
            db.execute("UPDATE runs SET platforms = ? WHERE id = ?", (json.dumps(platforms), run_id))
            db.execute("DELETE FROM posts WHERE run_id = ?", (run_id,))
            db.executemany(
                "INSERT INTO posts (run_id, platform, status, updated_at, position) VALUES (?,?,?,?,?)",
                [(run_id, p, "queued", stamp, i) for i, p in enumerate(platforms)],
            )

    def get_run(self, run_id: str) -> dict | None:
        with self._conn() as db:
            row = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if not row:
                return None
            posts = db.execute(
                "SELECT * FROM posts WHERE run_id = ? ORDER BY position", (run_id,)
            ).fetchall()
        return self._run_dict(row, [self._post_dict(p) for p in posts])

    def list_runs(self, user_id: str, limit: int = 50) -> list[dict]:
        with self._conn() as db:
            rows = db.execute(
                "SELECT * FROM runs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit)
            ).fetchall()
            counts = db.execute(
                "SELECT posts.run_id, posts.status, COUNT(*) AS n FROM posts JOIN runs ON runs.id = posts.run_id "
                "WHERE runs.user_id = ? GROUP BY posts.run_id, posts.status",
                (user_id,),
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
    def get_source_text(self, run_id: str) -> str:
        with self._conn() as db:
            row = db.execute("SELECT source_text FROM runs WHERE id = ?", (run_id,)).fetchone()
        return (row["source_text"] if row else None) or ""

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

    def count_runs_since(self, user_id: str, since_iso: str) -> int:
        with self._conn() as db:
            row = db.execute(
                "SELECT COUNT(*) AS n FROM runs WHERE user_id = ? AND created_at >= ?", (user_id, since_iso)
            ).fetchone()
        return int(row["n"])

    def scheduled_posts(self, user_id: str) -> list[dict]:
        with self._conn() as db:
            rows = db.execute(
                "SELECT posts.*, runs.source FROM posts JOIN runs ON runs.id = posts.run_id "
                "WHERE runs.user_id = ? AND "
                "(posts.schedule_at IS NOT NULL OR posts.status IN ('published','scheduled')) "
                "ORDER BY COALESCE(posts.schedule_at, posts.updated_at)",
                (user_id,),
            ).fetchall()
        return [{**self._post_dict(r), "source": r["source"]} for r in rows]

    def count_waiting(self, user_id: str) -> int:
        """Posts written and waiting for approval, plus loops waiting for a brief review."""
        with self._conn() as db:
            posts = db.execute(
                "SELECT COUNT(*) AS n FROM posts JOIN runs ON runs.id = posts.run_id "
                "WHERE runs.user_id = ? AND posts.status = 'draft'", (user_id,)
            ).fetchone()["n"]
            briefs = db.execute(
                "SELECT COUNT(*) AS n FROM runs WHERE user_id = ? AND status = 'review'", (user_id,)
            ).fetchone()["n"]
        return posts + briefs

    def user_posts(self, user_id: str, limit_runs: int = 200) -> list[dict]:
        """Every post in a user's recent loops, with enough of its loop to link and label it."""
        with self._conn() as db:
            rows = db.execute(
                "SELECT posts.*, runs.source, runs.meta, runs.created_at AS run_created_at "
                "FROM posts JOIN runs ON runs.id = posts.run_id "
                "WHERE runs.id IN (SELECT id FROM runs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?) "
                "ORDER BY posts.updated_at DESC",
                (user_id, limit_runs),
            ).fetchall()
        return [{**self._post_dict(r), "source": r["source"], "meta": json.loads(r["meta"]) if r["meta"] else {},
                 "run_created_at": r["run_created_at"]} for r in rows]

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
    def add_topic(self, source: str, user_id: str) -> str:
        topic_id = uuid.uuid4().hex[:10]
        with self._conn() as db:
            db.execute(
                "INSERT INTO topics (id, created_at, source, status, user_id) VALUES (?,?,?,?,?)",
                (topic_id, now(), source, "waiting", user_id),
            )
        return topic_id

    def list_topics(self, user_id: str) -> list[dict]:
        with self._conn() as db:
            rows = db.execute(
                "SELECT * FROM topics WHERE user_id = ? ORDER BY created_at", (user_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def next_topic(self, user_id: str) -> dict | None:
        with self._conn() as db:
            row = db.execute(
                "SELECT * FROM topics WHERE user_id = ? AND status = 'waiting' ORDER BY created_at LIMIT 1",
                (user_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_topic(self, topic_id: str, **fields) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as db:
            db.execute(f"UPDATE topics SET {sets} WHERE id = ?", (*fields.values(), topic_id))

    def delete_topic(self, topic_id: str, user_id: str) -> None:
        with self._conn() as db:
            db.execute("DELETE FROM topics WHERE id = ? AND user_id = ?", (topic_id, user_id))

    # --- users --------------------------------------------------------------
    def upsert_google_user(self, sub: str, email: str, name: str = "", picture: str = "") -> dict:
        with self._conn() as db:
            row = db.execute("SELECT id FROM users WHERE google_sub = ?", (sub,)).fetchone()
            if row:
                db.execute(
                    "UPDATE users SET email = ?, name = ?, picture = ? WHERE id = ?",
                    (email, name, picture, row["id"]),
                )
                user_id = row["id"]
            else:
                user_id = "u_" + uuid.uuid4().hex[:12]
                db.execute(
                    "INSERT INTO users (id, created_at, email, name, picture, google_sub) VALUES (?,?,?,?,?,?)",
                    (user_id, now(), email, name, picture, sub),
                )
        return self.get_user(user_id)

    def ensure_user(self, user_id: str, name: str = "") -> dict:
        """Create a fixed-id user (the local or password owner) if missing."""
        if not self.get_user(user_id):
            with self._conn() as db:
                db.execute(
                    "INSERT INTO users (id, created_at, name) VALUES (?,?,?)", (user_id, now(), name)
                )
        return self.get_user(user_id)

    def adopt_unowned(self, user_id: str) -> None:
        """Hand loops, ideas and settings saved before sign-in existed to this user.
        Whoever adopts first keeps them; afterwards there is nothing left to adopt."""
        with self._conn() as db:
            db.execute("UPDATE runs SET user_id = ? WHERE user_id IS NULL", (user_id,))
            db.execute("UPDATE topics SET user_id = ? WHERE user_id IS NULL", (user_id,))
            for key in ("voice", "autopilot"):
                row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
                if not row:
                    continue
                if not db.execute("SELECT 1 FROM settings WHERE key = ?", (f"{key}:{user_id}",)).fetchone():
                    db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (f"{key}:{user_id}", row["value"]))
                db.execute("DELETE FROM settings WHERE key = ?", (key,))

    def get_user(self, user_id: str) -> dict | None:
        with self._conn() as db:
            row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def set_user_profile(self, user_id: str, profile_id: str) -> None:
        with self._conn() as db:
            db.execute("UPDATE users SET zernio_profile_id = ? WHERE id = ?", (profile_id, user_id))

    def settings_with_prefix(self, prefix: str) -> dict:
        with self._conn() as db:
            rows = db.execute(
                "SELECT key, value FROM settings WHERE key LIKE ?", (prefix + "%",)
            ).fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}

    # --- helpers ----------------------------------------------------------
    @staticmethod
    def _run_dict(row, posts=None, tally=None) -> dict:
        run = dict(row)
        run["platforms"] = json.loads(run["platforms"])
        run["brief"] = json.loads(run["brief"]) if run["brief"] else None
        run["meta"] = json.loads(run["meta"]) if run.get("meta") else {}
        run["analysis"] = json.loads(run["analysis"]) if run.get("analysis") else None
        run["evidence"] = json.loads(run["evidence"]) if run.get("evidence") else []
        run.pop("source_text", None)  # large; read it with get_source_text
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
