from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    is_active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS profile_filters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    channel_input TEXT NOT NULL,
    channel_id TEXT,
    channel_title TEXT,
    keyword TEXT,
    duration_bucket TEXT CHECK(duration_bucket IN ('short', 'medium', 'long') OR duration_bucket IS NULL),
    since_mode TEXT NOT NULL DEFAULT 'anytime' CHECK(since_mode IN ('anytime', 'from_now')),
    since_published_after TEXT,
    is_valid INTEGER NOT NULL DEFAULT 1,
    validation_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    youtube_video_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    channel_id TEXT,
    channel_title TEXT,
    published_at TEXT,
    thumbnail_url TEXT,
    video_url TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS inbox_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new', 'watched', 'dismissed')),
    is_starred INTEGER NOT NULL DEFAULT 0 CHECK(is_starred IN (0, 1)),
    first_inboxed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    watched_at TEXT,
    starred_at TEXT,
    dismissed_at TEXT,
    opened_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(profile_id, video_id)
);

CREATE TABLE IF NOT EXISTS refresh_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('ok', 'error', 'partial')),
    error_message TEXT,
    added_count INTEGER NOT NULL DEFAULT 0,
    matched_count INTEGER NOT NULL DEFAULT 0,
    fetched_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_profile_filters_profile_id ON profile_filters(profile_id);
CREATE INDEX IF NOT EXISTS idx_inbox_profile_status ON inbox_items(profile_id, status);
CREATE INDEX IF NOT EXISTS idx_refresh_runs_profile_id ON refresh_runs(profile_id, id DESC);
"""


@dataclass(frozen=True)
class VideoRecord:
    youtube_video_id: str
    title: str
    channel_id: str
    channel_title: str
    published_at: str
    thumbnail_url: str
    video_url: str


class Database:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA_SQL)
            self._apply_migrations(conn)
            conn.commit()

    def _apply_migrations(self, conn: sqlite3.Connection) -> None:
        filter_columns = {row["name"] for row in conn.execute("PRAGMA table_info(profile_filters)")}
        if "duration_bucket" not in filter_columns:
            conn.execute(
                """
                ALTER TABLE profile_filters
                ADD COLUMN duration_bucket TEXT CHECK(
                    duration_bucket IN ('short', 'medium', 'long')
                    OR duration_bucket IS NULL
                )
                """
            )
        if "since_mode" not in filter_columns:
            conn.execute(
                """
                ALTER TABLE profile_filters
                ADD COLUMN since_mode TEXT NOT NULL DEFAULT 'anytime' CHECK(
                    since_mode IN ('anytime', 'from_now')
                )
                """
            )
        if "since_published_after" not in filter_columns:
            conn.execute(
                """
                ALTER TABLE profile_filters
                ADD COLUMN since_published_after TEXT
                """
            )

        columns = {row["name"] for row in conn.execute("PRAGMA table_info(inbox_items)")}
        if "is_starred" not in columns:
            conn.execute(
                """
                ALTER TABLE inbox_items
                ADD COLUMN is_starred INTEGER NOT NULL DEFAULT 0 CHECK(is_starred IN (0, 1))
                """
            )
        if "starred_at" not in columns:
            conn.execute(
                """
                ALTER TABLE inbox_items
                ADD COLUMN starred_at TEXT
                """
            )
        if "dismissed_at" not in columns:
            conn.execute(
                """
                ALTER TABLE inbox_items
                ADD COLUMN dismissed_at TEXT
                """
            )
        conn.execute(
            """
            UPDATE inbox_items
            SET starred_at = COALESCE(starred_at, last_seen_at, first_inboxed_at, CURRENT_TIMESTAMP)
            WHERE is_starred = 1 AND starred_at IS NULL
            """
        )
        conn.execute(
            """
            UPDATE inbox_items
            SET dismissed_at = COALESCE(dismissed_at, watched_at, last_seen_at, first_inboxed_at, CURRENT_TIMESTAMP)
            WHERE status = 'dismissed' AND dismissed_at IS NULL
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_inbox_profile_starred
            ON inbox_items(profile_id, is_starred)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_inbox_profile_starred_at
            ON inbox_items(profile_id, is_starred, starred_at DESC, id DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_inbox_profile_dismissed_at
            ON inbox_items(profile_id, status, dismissed_at DESC, id DESC)
            """
        )

    def list_profiles(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute("SELECT * FROM profiles ORDER BY name"))

    def get_profile_by_id(self, profile_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()

    def get_profile_by_name(self, name: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM profiles WHERE name = ?", (name,)).fetchone()

    def get_active_profile(self) -> sqlite3.Row | None:
        with self.connect() as conn:
            active = conn.execute("SELECT * FROM profiles WHERE is_active = 1 LIMIT 1").fetchone()
            if active:
                return active
            return conn.execute("SELECT * FROM profiles ORDER BY id LIMIT 1").fetchone()

    def create_profile(self, name: str) -> int:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Profile name cannot be empty.")

        with self.connect() as conn:
            has_profiles = conn.execute("SELECT EXISTS(SELECT 1 FROM profiles)").fetchone()[0]
            cursor = conn.execute(
                "INSERT INTO profiles(name, is_active) VALUES (?, ?)",
                (clean_name, 0 if has_profiles else 1),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def set_active_profile(self, profile_id: int) -> None:
        with self.connect() as conn:
            exists = conn.execute("SELECT 1 FROM profiles WHERE id = ?", (profile_id,)).fetchone()
            if not exists:
                raise ValueError(f"Profile {profile_id} does not exist.")
            conn.execute("UPDATE profiles SET is_active = 0")
            conn.execute(
                "UPDATE profiles SET is_active = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (profile_id,),
            )
            conn.commit()

    def delete_profile(self, profile_id: int) -> None:
        with self.connect() as conn:
            target = conn.execute("SELECT id, is_active FROM profiles WHERE id = ?", (profile_id,)).fetchone()
            if not target:
                raise ValueError(f"Profile {profile_id} does not exist.")
            conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
            if target["is_active"]:
                next_profile = conn.execute("SELECT id FROM profiles ORDER BY id LIMIT 1").fetchone()
                if next_profile:
                    conn.execute("UPDATE profiles SET is_active = 1 WHERE id = ?", (next_profile["id"],))
            conn.commit()

    def list_filters(self, profile_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT *
                    FROM profile_filters
                    WHERE profile_id = ?
                    ORDER BY id
                    """,
                    (profile_id,),
                )
            )

    def add_filter(
        self,
        profile_id: int,
        channel_input: str,
        keyword: str | None,
        duration_bucket: str | None = None,
        since_mode: str = "anytime",
    ) -> int:
        clean_channel = channel_input.strip()
        if not clean_channel:
            raise ValueError("Channel is required.")
        clean_keyword = (keyword or "").strip() or None
        clean_duration_bucket = None
        if duration_bucket is not None:
            candidate_bucket = duration_bucket.strip().lower()
            if candidate_bucket:
                if candidate_bucket not in {"short", "medium", "long"}:
                    raise ValueError("Duration bucket must be one of short, medium, long.")
                clean_duration_bucket = candidate_bucket
        clean_since_mode = (since_mode or "anytime").strip().lower()
        if clean_since_mode in {"from-now", "fromnow"}:
            clean_since_mode = "from_now"
        if clean_since_mode not in {"anytime", "from_now"}:
            raise ValueError("Since mode must be either anytime or from_now.")
        since_published_after = None
        if clean_since_mode == "from_now":
            since_published_after = (
                datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            )
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO profile_filters(
                    profile_id,
                    channel_input,
                    keyword,
                    duration_bucket,
                    since_mode,
                    since_published_after
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    profile_id,
                    clean_channel,
                    clean_keyword,
                    clean_duration_bucket,
                    clean_since_mode,
                    since_published_after,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def remove_filter(self, profile_id: int, filter_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM profile_filters WHERE profile_id = ? AND id = ?",
                (profile_id, filter_id),
            )
            conn.commit()

    def update_filter_resolution(
        self,
        filter_id: int,
        *,
        channel_id: str | None,
        channel_title: str | None,
        is_valid: bool,
        validation_error: str | None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE profile_filters
                SET channel_id = ?,
                    channel_title = ?,
                    is_valid = ?,
                    validation_error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (channel_id, channel_title, 1 if is_valid else 0, validation_error, filter_id),
            )
            conn.commit()

    def start_refresh_run(self, profile_id: int) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO refresh_runs(profile_id, status) VALUES (?, 'ok')",
                (profile_id,),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def finish_refresh_run(
        self,
        run_id: int,
        *,
        status: str,
        error_message: str | None,
        added_count: int,
        matched_count: int,
        fetched_count: int,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE refresh_runs
                SET completed_at = CURRENT_TIMESTAMP,
                    status = ?,
                    error_message = ?,
                    added_count = ?,
                    matched_count = ?,
                    fetched_count = ?
                WHERE id = ?
                """,
                (status, error_message, added_count, matched_count, fetched_count, run_id),
            )
            conn.commit()

    def latest_refresh_run(self, profile_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT *
                FROM refresh_runs
                WHERE profile_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (profile_id,),
            ).fetchone()

    def upsert_video(self, record: VideoRecord) -> int:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO videos(
                    youtube_video_id,
                    title,
                    channel_id,
                    channel_title,
                    published_at,
                    thumbnail_url,
                    video_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(youtube_video_id) DO UPDATE SET
                    title = excluded.title,
                    channel_id = excluded.channel_id,
                    channel_title = excluded.channel_title,
                    published_at = excluded.published_at,
                    thumbnail_url = excluded.thumbnail_url,
                    video_url = excluded.video_url,
                    fetched_at = CURRENT_TIMESTAMP
                """,
                (
                    record.youtube_video_id,
                    record.title,
                    record.channel_id,
                    record.channel_title,
                    record.published_at,
                    record.thumbnail_url,
                    record.video_url,
                ),
            )
            row = conn.execute(
                "SELECT id FROM videos WHERE youtube_video_id = ?",
                (record.youtube_video_id,),
            ).fetchone()
            conn.commit()
            return int(row["id"])

    def insert_inbox_item(self, profile_id: int, video_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO inbox_items(profile_id, video_id)
                VALUES (?, ?)
                """,
                (profile_id, video_id),
            )
            inserted = cursor.rowcount == 1
            if not inserted:
                conn.execute(
                    """
                    UPDATE inbox_items
                    SET last_seen_at = CURRENT_TIMESTAMP
                    WHERE profile_id = ? AND video_id = ?
                    """,
                    (profile_id, video_id),
                )
            conn.commit()
            return inserted

    def count_inbox_items(
        self,
        profile_id: int,
        *,
        statuses: tuple[str, ...] = ("new", "watched"),
        starred_only: bool = False,
    ) -> int:
        if not statuses:
            return 0
        placeholders = ", ".join("?" for _ in statuses)
        starred_clause = " AND is_starred = 1" if starred_only else ""
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS c
                FROM inbox_items
                WHERE profile_id = ? AND status IN ({placeholders})
                {starred_clause}
                """,
                (profile_id, *statuses),
            ).fetchone()
            return int(row["c"])

    def list_inbox_items(
        self,
        profile_id: int,
        *,
        limit: int,
        offset: int,
        statuses: tuple[str, ...] = ("new", "watched"),
        starred_only: bool = False,
        sort_by_watched_at: bool = False,
        sort_mode: str = "published",
    ) -> list[sqlite3.Row]:
        if not statuses:
            return []
        placeholders = ", ".join("?" for _ in statuses)
        starred_clause = " AND inbox_items.is_starred = 1" if starred_only else ""
        resolved_sort_mode = (sort_mode or "published").strip().lower()
        if sort_by_watched_at and resolved_sort_mode == "published":
            resolved_sort_mode = "watched"
        order_map = {
            "published": "videos.published_at DESC, inbox_items.id DESC",
            "watched": "inbox_items.watched_at DESC, inbox_items.id DESC",
            "starred": "inbox_items.starred_at DESC, inbox_items.id DESC",
            "dismissed": "inbox_items.dismissed_at DESC, inbox_items.id DESC",
        }
        order_clause = order_map.get(resolved_sort_mode, order_map["published"])
        with self.connect() as conn:
            return list(
                conn.execute(
                    f"""
                    SELECT
                        inbox_items.id AS inbox_item_id,
                        inbox_items.profile_id,
                        inbox_items.status,
                        inbox_items.is_starred,
                        inbox_items.watched_at,
                        inbox_items.starred_at,
                        inbox_items.dismissed_at,
                        inbox_items.opened_count,
                        inbox_items.first_inboxed_at,
                        videos.youtube_video_id,
                        videos.title,
                        videos.channel_title,
                        videos.published_at
                    FROM inbox_items
                    JOIN videos ON videos.id = inbox_items.video_id
                    WHERE inbox_items.profile_id = ?
                      AND inbox_items.status IN ({placeholders})
                      {starred_clause}
                    ORDER BY {order_clause}
                    LIMIT ? OFFSET ?
                    """,
                    (profile_id, *statuses, limit, offset),
                )
            )

    def mark_inbox_watched(self, inbox_item_id: int, watched: bool) -> None:
        with self.connect() as conn:
            if watched:
                conn.execute(
                    """
                    UPDATE inbox_items
                    SET status = 'watched',
                        watched_at = COALESCE(watched_at, CURRENT_TIMESTAMP),
                        dismissed_at = NULL
                    WHERE id = ?
                    """,
                    (inbox_item_id,),
                )
            else:
                conn.execute(
                    """
                    UPDATE inbox_items
                    SET status = 'new',
                        watched_at = NULL,
                        dismissed_at = NULL
                    WHERE id = ?
                    """,
                    (inbox_item_id,),
                )
            conn.commit()

    def mark_inbox_trashed(self, inbox_item_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE inbox_items
                SET status = 'dismissed'
                    , is_starred = 0
                    , starred_at = NULL
                    , dismissed_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (inbox_item_id,),
            )
            conn.commit()

    def mark_inbox_starred(self, inbox_item_id: int, *, starred: bool) -> None:
        with self.connect() as conn:
            if starred:
                conn.execute(
                    """
                    UPDATE inbox_items
                    SET is_starred = 1,
                        starred_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (inbox_item_id,),
                )
            else:
                conn.execute(
                    """
                    UPDATE inbox_items
                    SET is_starred = 0,
                        starred_at = NULL
                    WHERE id = ?
                    """,
                    (inbox_item_id,),
                )
            conn.commit()

    def mark_inbox_opened(self, inbox_item_id: int, *, mark_watched: bool = True) -> None:
        with self.connect() as conn:
            if mark_watched:
                conn.execute(
                    """
                    UPDATE inbox_items
                    SET opened_count = opened_count + 1,
                        status = 'watched',
                        watched_at = COALESCE(watched_at, CURRENT_TIMESTAMP),
                        dismissed_at = NULL
                    WHERE id = ?
                    """,
                    (inbox_item_id,),
                )
            else:
                conn.execute(
                    """
                    UPDATE inbox_items
                    SET opened_count = opened_count + 1
                    WHERE id = ?
                    """,
                    (inbox_item_id,),
                )
            conn.commit()

    def get_inbox_item_with_video(self, inbox_item_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT
                    inbox_items.id AS inbox_item_id,
                    inbox_items.profile_id,
                    inbox_items.status,
                    inbox_items.is_starred,
                    videos.youtube_video_id,
                    videos.title,
                    videos.channel_title
                FROM inbox_items
                JOIN videos ON videos.id = inbox_items.video_id
                WHERE inbox_items.id = ?
                """,
                (inbox_item_id,),
            ).fetchone()

    def profile_ids(self) -> Iterable[int]:
        with self.connect() as conn:
            rows = conn.execute("SELECT id FROM profiles ORDER BY id").fetchall()
            return [int(row["id"]) for row in rows]
