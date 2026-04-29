from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from mail_tube.db import Database
from mail_tube.importer import FALLBACK_TITLE_TEMPLATE, import_video_link
from mail_tube.refresh import refresh_profile
from mail_tube.youtube import (
    ChannelInfo,
    VideoInfo,
    build_embed_url,
    duration_matches_filter,
    duration_matches_bucket,
    extract_video_id,
    fetch_video,
    published_at_on_or_after,
    title_matches_keyword,
)


class YouTubeHelpersTest(unittest.TestCase):
    def test_extract_video_id_from_common_urls(self) -> None:
        self.assertEqual(extract_video_id("dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(
            extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
            "dQw4w9WgXcQ",
        )
        self.assertEqual(extract_video_id("https://youtu.be/dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(
            extract_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ"),
            "dQw4w9WgXcQ",
        )
        self.assertIsNone(extract_video_id("https://example.com/not-youtube"))

    def test_build_embed_url_has_required_parameters(self) -> None:
        url = build_embed_url("dQw4w9WgXcQ", autoplay=True)
        self.assertIn("autoplay=1", url)
        self.assertIn("controls=1", url)
        self.assertIn("rel=0", url)
        self.assertIn("iv_load_policy=3", url)

    def test_fetch_video_reads_snippet_metadata(self) -> None:
        payload = {
            "items": [
                {
                    "id": "dQw4w9WgXcQ",
                    "snippet": {
                        "title": "Single import",
                        "channelId": "UCsingle",
                        "channelTitle": "Singles",
                        "publishedAt": "2026-01-05T00:00:00Z",
                        "thumbnails": {"medium": {"url": "https://example.com/thumb.jpg"}},
                    },
                }
            ]
        }
        with patch("mail_tube.youtube._api_get", return_value=payload) as api_get:
            video = fetch_video("dQw4w9WgXcQ", api_key="fake-key")

        api_get.assert_called_once()
        self.assertEqual(video.youtube_video_id, "dQw4w9WgXcQ")
        self.assertEqual(video.title, "Single import")
        self.assertEqual(video.channel_title, "Singles")
        self.assertEqual(video.thumbnail_url, "https://example.com/thumb.jpg")

    def test_keyword_match_is_case_insensitive_substring(self) -> None:
        self.assertTrue(title_matches_keyword("Great Cat Video", "cat"))
        self.assertTrue(title_matches_keyword("Studio Session", "studio"))
        self.assertTrue(title_matches_keyword("Luca&#39;s Studio Session", "luca's"))
        self.assertTrue(title_matches_keyword("Luca\u2019s Studio Session", "luca's"))
        self.assertFalse(title_matches_keyword("Dog clip", "cat"))
        self.assertTrue(title_matches_keyword("Anything", None))
        self.assertTrue(title_matches_keyword("Anything", ""))

    def test_duration_bucket_matching(self) -> None:
        self.assertTrue(duration_matches_bucket(120, "short"))
        self.assertFalse(duration_matches_bucket(300, "short"))
        self.assertTrue(duration_matches_bucket(300, "medium"))
        self.assertTrue(duration_matches_bucket(1200, "medium"))
        self.assertFalse(duration_matches_bucket(1201, "medium"))
        self.assertTrue(duration_matches_bucket(1201, "long"))
        self.assertTrue(duration_matches_bucket(None, None))
        self.assertFalse(duration_matches_bucket(None, "short"))

    def test_duration_filter_can_exclude_shorts(self) -> None:
        self.assertFalse(duration_matches_filter(60, None, exclude_shorts=True))
        self.assertFalse(duration_matches_filter(180, None, exclude_shorts=True))
        self.assertTrue(duration_matches_filter(181, None, exclude_shorts=True))
        self.assertTrue(duration_matches_filter(60, None, exclude_shorts=False))
        self.assertFalse(duration_matches_filter(60, "short", exclude_shorts=True))
        self.assertTrue(duration_matches_filter(240, "short", exclude_shorts=True))
        self.assertFalse(duration_matches_filter(360, "short", exclude_shorts=True))

    def test_published_at_cutoff_matching(self) -> None:
        self.assertTrue(published_at_on_or_after("2026-01-02T00:00:00Z", None))
        self.assertTrue(published_at_on_or_after("2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z"))
        self.assertFalse(published_at_on_or_after("2026-01-01T23:59:59Z", "2026-01-02T00:00:00Z"))
        self.assertFalse(published_at_on_or_after(None, "2026-01-02T00:00:00Z"))


class RefreshFlowTest(unittest.TestCase):
    def test_init_migrates_legacy_inbox_table_without_is_starred(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            with db.connect() as conn:
                conn.executescript(
                    """
                    PRAGMA foreign_keys = ON;
                    CREATE TABLE profiles (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        is_active INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE profile_filters (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
                        channel_input TEXT NOT NULL,
                        channel_id TEXT,
                        channel_title TEXT,
                        keyword TEXT,
                        is_valid INTEGER NOT NULL DEFAULT 1,
                        validation_error TEXT,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE videos (
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
                    CREATE TABLE inbox_items (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
                        video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                        status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new', 'watched', 'dismissed')),
                        first_inboxed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        watched_at TEXT,
                        opened_count INTEGER NOT NULL DEFAULT 0,
                        UNIQUE(profile_id, video_id)
                    );
                    INSERT INTO profiles(name, is_active) VALUES ('legacy', 1);
                    INSERT INTO profile_filters(profile_id, channel_input, keyword) VALUES (1, '@legacy', NULL);
                    """
                )
                conn.commit()

            db.init()
            with db.connect() as conn:
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(inbox_items)")}
                self.assertIn("is_starred", columns)
                self.assertIn("starred_at", columns)
                self.assertIn("dismissed_at", columns)
                filter_columns = {row["name"] for row in conn.execute("PRAGMA table_info(profile_filters)")}
                self.assertIn("duration_bucket", filter_columns)
                self.assertIn("exclude_shorts", filter_columns)
                self.assertIn("since_mode", filter_columns)
                self.assertIn("since_published_after", filter_columns)
                migrated_filter = conn.execute("SELECT exclude_shorts FROM profile_filters WHERE id = 1").fetchone()
                self.assertIsNotNone(migrated_filter)
                self.assertEqual(int(migrated_filter["exclude_shorts"]), 1)
                index_names = {row["name"] for row in conn.execute("PRAGMA index_list(inbox_items)")}
                self.assertIn("idx_inbox_profile_starred", index_names)
                self.assertIn("idx_inbox_profile_starred_at", index_names)
                self.assertIn("idx_inbox_profile_dismissed_at", index_names)

    def test_refresh_uses_channel_and_optional_keyword(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            db.init()
            profile_id = db.create_profile("default")
            db.add_filter(profile_id, "@abc", "cat")
            db.add_filter(profile_id, "@bbc", None)

            def fake_resolve(channel_input: str, *, api_key: str) -> ChannelInfo:
                if channel_input == "@abc":
                    return ChannelInfo(channel_id="UCaaaaaaaaaaaaaaaaaaaaaa", channel_title="ABC")
                if channel_input == "@bbc":
                    return ChannelInfo(channel_id="UCbbbbbbbbbbbbbbbbbbbbbb", channel_title="BBC")
                raise AssertionError("Unexpected input")

            def fake_videos(
                channel_id: str,
                *,
                api_key: str,
                max_results: int = 50,
                include_duration: bool = False,
            ) -> list[VideoInfo]:
                self.assertTrue(include_duration)
                if channel_id == "UCaaaaaaaaaaaaaaaaaaaaaa":
                    return [
                        VideoInfo(
                            youtube_video_id="AAAAAAAAAAA",
                            title="cat news",
                            channel_id=channel_id,
                            channel_title="ABC",
                            published_at="2026-01-01T00:00:00Z",
                            thumbnail_url="",
                            video_url="https://www.youtube.com/watch?v=AAAAAAAAAAA",
                            duration_seconds=240,
                        ),
                        VideoInfo(
                            youtube_video_id="BBBBBBBBBBB",
                            title="dog news",
                            channel_id=channel_id,
                            channel_title="ABC",
                            published_at="2026-01-02T00:00:00Z",
                            thumbnail_url="",
                            video_url="https://www.youtube.com/watch?v=BBBBBBBBBBB",
                            duration_seconds=240,
                        ),
                    ]
                return [
                    VideoInfo(
                        youtube_video_id="CCCCCCCCCCC",
                        title="world update",
                        channel_id=channel_id,
                        channel_title="BBC",
                        published_at="2026-01-03T00:00:00Z",
                        thumbnail_url="",
                        video_url="https://www.youtube.com/watch?v=CCCCCCCCCCC",
                        duration_seconds=240,
                    )
                ]

            with (
                patch("mail_tube.refresh.resolve_channel_input", side_effect=fake_resolve),
                patch("mail_tube.refresh.fetch_channel_videos", side_effect=fake_videos),
            ):
                outcome = refresh_profile(db, profile_id, api_key="fake-key")

            self.assertEqual(outcome.status, "ok")
            self.assertEqual(outcome.matched_count, 2)
            self.assertEqual(outcome.added_count, 2)

            items = db.list_inbox_items(profile_id, limit=20, offset=0)
            video_ids = {row["youtube_video_id"] for row in items}
            self.assertSetEqual(video_ids, {"AAAAAAAAAAA", "CCCCCCCCCCC"})

    def test_refresh_applies_duration_bucket(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            db.init()
            profile_id = db.create_profile("default")
            db.add_filter(profile_id, "@abc", None, "short")

            def fake_resolve(channel_input: str, *, api_key: str) -> ChannelInfo:
                if channel_input == "@abc":
                    return ChannelInfo(channel_id="UCaaaaaaaaaaaaaaaaaaaaaa", channel_title="ABC")
                raise AssertionError("Unexpected input")

            def fake_videos(
                channel_id: str,
                *,
                api_key: str,
                max_results: int = 50,
                include_duration: bool = False,
            ) -> list[VideoInfo]:
                self.assertTrue(include_duration)
                return [
                    VideoInfo(
                        youtube_video_id="HHHHHHHHHHH",
                        title="shorts clip",
                        channel_id=channel_id,
                        channel_title="ABC",
                        published_at="2026-01-01T00:00:00Z",
                        thumbnail_url="",
                        video_url="https://www.youtube.com/watch?v=HHHHHHHHHHH",
                        duration_seconds=60,
                    ),
                    VideoInfo(
                        youtube_video_id="SSSSSSSSSSS",
                        title="short regular clip",
                        channel_id=channel_id,
                        channel_title="ABC",
                        published_at="2026-01-01T12:00:00Z",
                        thumbnail_url="",
                        video_url="https://www.youtube.com/watch?v=SSSSSSSSSSS",
                        duration_seconds=240,
                    ),
                    VideoInfo(
                        youtube_video_id="IIIIIIIIIII",
                        title="long clip",
                        channel_id=channel_id,
                        channel_title="ABC",
                        published_at="2026-01-02T00:00:00Z",
                        thumbnail_url="",
                        video_url="https://www.youtube.com/watch?v=IIIIIIIIIII",
                        duration_seconds=1800,
                    ),
                ]

            with (
                patch("mail_tube.refresh.resolve_channel_input", side_effect=fake_resolve),
                patch("mail_tube.refresh.fetch_channel_videos", side_effect=fake_videos),
            ):
                outcome = refresh_profile(db, profile_id, api_key="fake-key")

            self.assertEqual(outcome.status, "ok")
            self.assertEqual(outcome.matched_count, 1)
            self.assertEqual(outcome.added_count, 1)
            items = db.list_inbox_items(profile_id, limit=20, offset=0)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["youtube_video_id"], "SSSSSSSSSSS")

    def test_refresh_excludes_shorts_by_default(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            db.init()
            profile_id = db.create_profile("default-no-shorts")
            db.add_filter(profile_id, "@abc", None)

            def fake_resolve(channel_input: str, *, api_key: str) -> ChannelInfo:
                if channel_input == "@abc":
                    return ChannelInfo(channel_id="UCaaaaaaaaaaaaaaaaaaaaaa", channel_title="ABC")
                raise AssertionError("Unexpected input")

            def fake_videos(
                channel_id: str,
                *,
                api_key: str,
                max_results: int = 50,
                include_duration: bool = False,
            ) -> list[VideoInfo]:
                self.assertTrue(include_duration)
                return [
                    VideoInfo(
                        youtube_video_id="TTTTTTTTTTT",
                        title="shorts update",
                        channel_id=channel_id,
                        channel_title="ABC",
                        published_at="2026-01-01T00:00:00Z",
                        thumbnail_url="",
                        video_url="https://www.youtube.com/watch?v=TTTTTTTTTTT",
                        duration_seconds=60,
                    ),
                    VideoInfo(
                        youtube_video_id="UUUUUUUUUUU",
                        title="regular update",
                        channel_id=channel_id,
                        channel_title="ABC",
                        published_at="2026-01-02T00:00:00Z",
                        thumbnail_url="",
                        video_url="https://www.youtube.com/watch?v=UUUUUUUUUUU",
                        duration_seconds=181,
                    ),
                ]

            with (
                patch("mail_tube.refresh.resolve_channel_input", side_effect=fake_resolve),
                patch("mail_tube.refresh.fetch_channel_videos", side_effect=fake_videos),
            ):
                outcome = refresh_profile(db, profile_id, api_key="fake-key")

            self.assertEqual(outcome.status, "ok")
            self.assertEqual(outcome.matched_count, 1)
            items = db.list_inbox_items(profile_id, limit=20, offset=0)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["youtube_video_id"], "UUUUUUUUUUU")

    def test_refresh_any_length_includes_shorts(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            db.init()
            profile_id = db.create_profile("any-length")
            db.add_filter(profile_id, "@abc", None, exclude_shorts=False)

            def fake_resolve(channel_input: str, *, api_key: str) -> ChannelInfo:
                if channel_input == "@abc":
                    return ChannelInfo(channel_id="UCaaaaaaaaaaaaaaaaaaaaaa", channel_title="ABC")
                raise AssertionError("Unexpected input")

            def fake_videos(
                channel_id: str,
                *,
                api_key: str,
                max_results: int = 50,
                include_duration: bool = False,
            ) -> list[VideoInfo]:
                self.assertFalse(include_duration)
                return [
                    VideoInfo(
                        youtube_video_id="VVVVVVVVVVV",
                        title="shorts update",
                        channel_id=channel_id,
                        channel_title="ABC",
                        published_at="2026-01-01T00:00:00Z",
                        thumbnail_url="",
                        video_url="https://www.youtube.com/watch?v=VVVVVVVVVVV",
                    )
                ]

            with (
                patch("mail_tube.refresh.resolve_channel_input", side_effect=fake_resolve),
                patch("mail_tube.refresh.fetch_channel_videos", side_effect=fake_videos),
            ):
                outcome = refresh_profile(db, profile_id, api_key="fake-key")

            self.assertEqual(outcome.status, "ok")
            self.assertEqual(outcome.matched_count, 1)
            items = db.list_inbox_items(profile_id, limit=20, offset=0)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["youtube_video_id"], "VVVVVVVVVVV")

    def test_refresh_respects_from_now_cutoff(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            db.init()
            profile_id = db.create_profile("default")
            filter_id = db.add_filter(profile_id, "@abc", None, None, "from_now")
            with db.connect() as conn:
                conn.execute(
                    "UPDATE profile_filters SET since_published_after = ? WHERE id = ?",
                    ("2026-01-02T00:00:00Z", filter_id),
                )
                conn.commit()

            def fake_resolve(channel_input: str, *, api_key: str) -> ChannelInfo:
                if channel_input == "@abc":
                    return ChannelInfo(channel_id="UCaaaaaaaaaaaaaaaaaaaaaa", channel_title="ABC")
                raise AssertionError("Unexpected input")

            def fake_videos(
                channel_id: str,
                *,
                api_key: str,
                max_results: int = 50,
                include_duration: bool = False,
            ) -> list[VideoInfo]:
                self.assertTrue(include_duration)
                return [
                    VideoInfo(
                        youtube_video_id="JJJJJJJJJJJ",
                        title="older",
                        channel_id=channel_id,
                        channel_title="ABC",
                        published_at="2026-01-01T00:00:00Z",
                        thumbnail_url="",
                        video_url="https://www.youtube.com/watch?v=JJJJJJJJJJJ",
                        duration_seconds=240,
                    ),
                    VideoInfo(
                        youtube_video_id="KKKKKKKKKKK",
                        title="newer",
                        channel_id=channel_id,
                        channel_title="ABC",
                        published_at="2026-01-03T00:00:00Z",
                        thumbnail_url="",
                        video_url="https://www.youtube.com/watch?v=KKKKKKKKKKK",
                        duration_seconds=240,
                    ),
                ]

            with (
                patch("mail_tube.refresh.resolve_channel_input", side_effect=fake_resolve),
                patch("mail_tube.refresh.fetch_channel_videos", side_effect=fake_videos),
            ):
                outcome = refresh_profile(db, profile_id, api_key="fake-key")

            self.assertEqual(outcome.status, "ok")
            self.assertEqual(outcome.matched_count, 1)
            self.assertEqual(outcome.added_count, 1)
            items = db.list_inbox_items(profile_id, limit=20, offset=0)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["youtube_video_id"], "KKKKKKKKKKK")

    def test_update_filter_preserves_resolution_when_channel_is_unchanged(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            db.init()
            profile_id = db.create_profile("edit")
            filter_id = db.add_filter(profile_id, "@old", "cats", "medium", "from_now")
            db.update_filter_resolution(
                filter_id,
                channel_id="UCold",
                channel_title="Old Channel",
                is_valid=True,
                validation_error=None,
            )
            original = db.list_filters(profile_id)[0]

            db.update_filter(
                profile_id,
                filter_id,
                "@old",
                "dogs",
                duration_bucket=None,
                since_mode="from_now",
                exclude_shorts=False,
            )

            row = db.list_filters(profile_id)[0]
            self.assertEqual(row["channel_input"], "@old")
            self.assertEqual(row["channel_id"], "UCold")
            self.assertEqual(row["channel_title"], "Old Channel")
            self.assertEqual(row["keyword"], "dogs")
            self.assertIsNone(row["duration_bucket"])
            self.assertEqual(int(row["exclude_shorts"]), 0)
            self.assertEqual(row["since_mode"], "from_now")
            self.assertEqual(row["since_published_after"], original["since_published_after"])

    def test_update_filter_clears_resolution_when_channel_changes(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            db.init()
            profile_id = db.create_profile("edit-channel")
            filter_id = db.add_filter(profile_id, "@old", "cats", "medium", "anytime")
            db.update_filter_resolution(
                filter_id,
                channel_id="UCold",
                channel_title="Old Channel",
                is_valid=True,
                validation_error=None,
            )

            db.update_filter(
                profile_id,
                filter_id,
                "@new",
                "dogs",
                duration_bucket=None,
                since_mode="from_now",
                exclude_shorts=False,
            )

            row = db.list_filters(profile_id)[0]
            self.assertEqual(row["channel_input"], "@new")
            self.assertIsNone(row["channel_id"])
            self.assertIsNone(row["channel_title"])
            self.assertEqual(row["keyword"], "dogs")
            self.assertIsNone(row["duration_bucket"])
            self.assertEqual(int(row["exclude_shorts"]), 0)
            self.assertEqual(row["since_mode"], "from_now")
            self.assertIsNotNone(row["since_published_after"])

    def test_inbox_dedupe_is_per_profile(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            db.init()
            p1 = db.create_profile("p1")
            p2 = db.create_profile("p2")
            video_db_id = db.upsert_video(
                VideoInfo(
                    youtube_video_id="DDDDDDDDDDD",
                    title="same video",
                    channel_id="UC1",
                    channel_title="Channel",
                    published_at="2026-01-01T00:00:00Z",
                    thumbnail_url="",
                    video_url="https://www.youtube.com/watch?v=DDDDDDDDDDD",
                )
            )
            self.assertTrue(db.insert_inbox_item(p1, video_db_id))
            self.assertFalse(db.insert_inbox_item(p1, video_db_id))
            self.assertTrue(db.insert_inbox_item(p2, video_db_id))

    def test_import_link_can_use_fallback_metadata_without_api_key(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            db.init()
            profile_id = db.create_profile("manual")

            result = import_video_link(
                db,
                profile_id,
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                action="inbox",
                api_key=None,
            )

            self.assertTrue(result.inserted)
            self.assertTrue(result.used_fallback_metadata)
            items = db.list_inbox_items(profile_id, limit=20, offset=0, statuses=("new",))
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["youtube_video_id"], "dQw4w9WgXcQ")
            self.assertEqual(items[0]["title"], FALLBACK_TITLE_TEMPLATE.format(video_id="dQw4w9WgXcQ"))

    def test_import_fallback_does_not_overwrite_existing_video_metadata(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            db.init()
            profile_id = db.create_profile("existing")
            db.upsert_video(
                VideoInfo(
                    youtube_video_id="dQw4w9WgXcQ",
                    title="Known title",
                    channel_id="UCknown",
                    channel_title="Known channel",
                    published_at="2026-01-01T00:00:00Z",
                    thumbnail_url="https://example.com/known.jpg",
                    video_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                )
            )

            import_video_link(db, profile_id, "dQw4w9WgXcQ", action="inbox", api_key=None)

            items = db.list_inbox_items(profile_id, limit=20, offset=0, statuses=("new",))
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["title"], "Known title")
            self.assertEqual(items[0]["channel_title"], "Known channel")

    def test_import_link_watch_action_saves_watched_history(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            db.init()
            profile_id = db.create_profile("watch")

            result = import_video_link(db, profile_id, "dQw4w9WgXcQ", action="watch", api_key=None)

            watched_items = db.list_inbox_items(profile_id, limit=20, offset=0, statuses=("watched",))
            inbox_items = db.list_inbox_items(profile_id, limit=20, offset=0, statuses=("new",))
            self.assertEqual(len(watched_items), 1)
            self.assertEqual(len(inbox_items), 0)
            self.assertEqual(int(watched_items[0]["inbox_item_id"]), result.inbox_item_id)

    def test_import_link_starred_action_adds_to_starred_list(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            db.init()
            profile_id = db.create_profile("star-import")

            import_video_link(db, profile_id, "dQw4w9WgXcQ", action="starred", api_key=None)

            starred_items = db.list_inbox_items(
                profile_id,
                limit=20,
                offset=0,
                statuses=("new", "watched"),
                starred_only=True,
            )
            self.assertEqual(len(starred_items), 1)
            self.assertEqual(starred_items[0]["youtube_video_id"], "dQw4w9WgXcQ")
            self.assertEqual(int(starred_items[0]["is_starred"]), 1)

    def test_import_link_rejects_invalid_link_without_db_changes(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            db.init()
            profile_id = db.create_profile("invalid")

            with self.assertRaises(ValueError):
                import_video_link(db, profile_id, "https://example.com/watch?v=nope", action="inbox", api_key=None)

            self.assertEqual(db.count_inbox_items(profile_id, statuses=("new", "watched", "dismissed")), 0)

    def test_trash_status_moves_item_out_of_inbox(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            db.init()
            profile_id = db.create_profile("main")
            video_db_id = db.upsert_video(
                VideoInfo(
                    youtube_video_id="EEEEEEEEEEE",
                    title="trash me",
                    channel_id="UCtrash",
                    channel_title="Trash",
                    published_at="2026-01-01T00:00:00Z",
                    thumbnail_url="",
                    video_url="https://www.youtube.com/watch?v=EEEEEEEEEEE",
                )
            )
            db.insert_inbox_item(profile_id, video_db_id)
            inbox_items = db.list_inbox_items(profile_id, limit=20, offset=0, statuses=("new",))
            self.assertEqual(len(inbox_items), 1)

            inbox_item_id = int(inbox_items[0]["inbox_item_id"])
            db.mark_inbox_trashed(inbox_item_id)

            inbox_items_after = db.list_inbox_items(profile_id, limit=20, offset=0, statuses=("new",))
            trash_items = db.list_inbox_items(profile_id, limit=20, offset=0, statuses=("dismissed",))
            self.assertEqual(len(inbox_items_after), 0)
            self.assertEqual(len(trash_items), 1)

    def test_mark_inbox_opened_can_skip_watched_transition(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            db.init()
            profile_id = db.create_profile("viewer")
            video_db_id = db.upsert_video(
                VideoInfo(
                    youtube_video_id="LLLLLLLLLLL",
                    title="open only",
                    channel_id="UCopen",
                    channel_title="Open",
                    published_at="2026-01-01T00:00:00Z",
                    thumbnail_url="",
                    video_url="https://www.youtube.com/watch?v=LLLLLLLLLLL",
                )
            )
            db.insert_inbox_item(profile_id, video_db_id)
            inbox_item = db.list_inbox_items(profile_id, limit=20, offset=0, statuses=("new",))[0]
            inbox_item_id = int(inbox_item["inbox_item_id"])

            db.mark_inbox_opened(inbox_item_id, mark_watched=False)

            with db.connect() as conn:
                row = conn.execute(
                    "SELECT status, opened_count, watched_at FROM inbox_items WHERE id = ?",
                    (inbox_item_id,),
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "new")
            self.assertEqual(int(row["opened_count"]), 1)
            self.assertIsNone(row["watched_at"])

            db.mark_inbox_watched(inbox_item_id, watched=True)
            with db.connect() as conn:
                watched_row = conn.execute(
                    "SELECT status, opened_count, watched_at FROM inbox_items WHERE id = ?",
                    (inbox_item_id,),
                ).fetchone()
            self.assertIsNotNone(watched_row)
            self.assertEqual(watched_row["status"], "watched")
            self.assertEqual(int(watched_row["opened_count"]), 1)
            self.assertIsNotNone(watched_row["watched_at"])

    def test_watched_list_can_sort_by_watched_timestamp(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            db.init()
            profile_id = db.create_profile("watched-order")
            older_published = db.upsert_video(
                VideoInfo(
                    youtube_video_id="MMMMMMMMMMM",
                    title="older upload",
                    channel_id="UCwatch",
                    channel_title="Watch",
                    published_at="2026-01-01T00:00:00Z",
                    thumbnail_url="",
                    video_url="https://www.youtube.com/watch?v=MMMMMMMMMMM",
                )
            )
            newer_published = db.upsert_video(
                VideoInfo(
                    youtube_video_id="NNNNNNNNNNN",
                    title="newer upload",
                    channel_id="UCwatch",
                    channel_title="Watch",
                    published_at="2026-01-02T00:00:00Z",
                    thumbnail_url="",
                    video_url="https://www.youtube.com/watch?v=NNNNNNNNNNN",
                )
            )
            db.insert_inbox_item(profile_id, older_published)
            db.insert_inbox_item(profile_id, newer_published)
            initial = db.list_inbox_items(profile_id, limit=20, offset=0, statuses=("new",))
            by_video_id = {row["youtube_video_id"]: int(row["inbox_item_id"]) for row in initial}
            older_item = by_video_id["MMMMMMMMMMM"]
            newer_item = by_video_id["NNNNNNNNNNN"]
            db.mark_inbox_watched(older_item, watched=True)
            db.mark_inbox_watched(newer_item, watched=True)

            with db.connect() as conn:
                conn.execute("UPDATE inbox_items SET watched_at = '2026-01-03T00:00:00Z' WHERE id = ?", (older_item,))
                conn.execute("UPDATE inbox_items SET watched_at = '2026-01-04T00:00:00Z' WHERE id = ?", (newer_item,))
                conn.commit()

            watched_items = db.list_inbox_items(
                profile_id,
                limit=20,
                offset=0,
                statuses=("watched",),
                sort_by_watched_at=True,
            )
            watched_video_ids = [row["youtube_video_id"] for row in watched_items]
            self.assertEqual(watched_video_ids, ["NNNNNNNNNNN", "MMMMMMMMMMM"])

    def test_starred_list_can_sort_by_starred_timestamp(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            db.init()
            profile_id = db.create_profile("starred-order")
            older_published = db.upsert_video(
                VideoInfo(
                    youtube_video_id="OOOOOOOOOOO",
                    title="older upload",
                    channel_id="UCstar",
                    channel_title="Star",
                    published_at="2026-01-01T00:00:00Z",
                    thumbnail_url="",
                    video_url="https://www.youtube.com/watch?v=OOOOOOOOOOO",
                )
            )
            newer_published = db.upsert_video(
                VideoInfo(
                    youtube_video_id="PPPPPPPPPPP",
                    title="newer upload",
                    channel_id="UCstar",
                    channel_title="Star",
                    published_at="2026-01-02T00:00:00Z",
                    thumbnail_url="",
                    video_url="https://www.youtube.com/watch?v=PPPPPPPPPPP",
                )
            )
            db.insert_inbox_item(profile_id, older_published)
            db.insert_inbox_item(profile_id, newer_published)
            initial = db.list_inbox_items(profile_id, limit=20, offset=0, statuses=("new",))
            by_video_id = {row["youtube_video_id"]: int(row["inbox_item_id"]) for row in initial}
            older_item = by_video_id["OOOOOOOOOOO"]
            newer_item = by_video_id["PPPPPPPPPPP"]
            db.mark_inbox_starred(older_item, starred=True)
            db.mark_inbox_starred(newer_item, starred=True)

            with db.connect() as conn:
                conn.execute("UPDATE inbox_items SET starred_at = '2026-01-03T00:00:00Z' WHERE id = ?", (older_item,))
                conn.execute("UPDATE inbox_items SET starred_at = '2026-01-04T00:00:00Z' WHERE id = ?", (newer_item,))
                conn.commit()

            starred_items = db.list_inbox_items(
                profile_id,
                limit=20,
                offset=0,
                statuses=("new", "watched"),
                starred_only=True,
                sort_mode="starred",
            )
            starred_video_ids = [row["youtube_video_id"] for row in starred_items]
            self.assertEqual(starred_video_ids, ["PPPPPPPPPPP", "OOOOOOOOOOO"])

    def test_trash_list_can_sort_by_dismissed_timestamp(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            db.init()
            profile_id = db.create_profile("trash-order")
            older_published = db.upsert_video(
                VideoInfo(
                    youtube_video_id="QQQQQQQQQQ1",
                    title="older upload",
                    channel_id="UCtrash",
                    channel_title="Trash",
                    published_at="2026-01-01T00:00:00Z",
                    thumbnail_url="",
                    video_url="https://www.youtube.com/watch?v=QQQQQQQQQQ1",
                )
            )
            newer_published = db.upsert_video(
                VideoInfo(
                    youtube_video_id="QQQQQQQQQQ2",
                    title="newer upload",
                    channel_id="UCtrash",
                    channel_title="Trash",
                    published_at="2026-01-02T00:00:00Z",
                    thumbnail_url="",
                    video_url="https://www.youtube.com/watch?v=QQQQQQQQQQ2",
                )
            )
            db.insert_inbox_item(profile_id, older_published)
            db.insert_inbox_item(profile_id, newer_published)
            initial = db.list_inbox_items(profile_id, limit=20, offset=0, statuses=("new",))
            by_video_id = {row["youtube_video_id"]: int(row["inbox_item_id"]) for row in initial}
            older_item = by_video_id["QQQQQQQQQQ1"]
            newer_item = by_video_id["QQQQQQQQQQ2"]
            db.mark_inbox_trashed(older_item)
            db.mark_inbox_trashed(newer_item)

            with db.connect() as conn:
                conn.execute("UPDATE inbox_items SET dismissed_at = '2026-01-03T00:00:00Z' WHERE id = ?", (older_item,))
                conn.execute("UPDATE inbox_items SET dismissed_at = '2026-01-04T00:00:00Z' WHERE id = ?", (newer_item,))
                conn.commit()

            trashed_items = db.list_inbox_items(
                profile_id,
                limit=20,
                offset=0,
                statuses=("dismissed",),
                sort_mode="dismissed",
            )
            trashed_video_ids = [row["youtube_video_id"] for row in trashed_items]
            self.assertEqual(trashed_video_ids, ["QQQQQQQQQQ2", "QQQQQQQQQQ1"])

    def test_starred_filter_and_trash_clears_star(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            db.init()
            profile_id = db.create_profile("starred")
            first_video_id = db.upsert_video(
                VideoInfo(
                    youtube_video_id="FFFFFFFFFFF",
                    title="save me",
                    channel_id="UCsave",
                    channel_title="Save",
                    published_at="2026-01-01T00:00:00Z",
                    thumbnail_url="",
                    video_url="https://www.youtube.com/watch?v=FFFFFFFFFFF",
                )
            )
            second_video_id = db.upsert_video(
                VideoInfo(
                    youtube_video_id="GGGGGGGGGGG",
                    title="normal",
                    channel_id="UCnormal",
                    channel_title="Normal",
                    published_at="2026-01-02T00:00:00Z",
                    thumbnail_url="",
                    video_url="https://www.youtube.com/watch?v=GGGGGGGGGGG",
                )
            )
            db.insert_inbox_item(profile_id, first_video_id)
            db.insert_inbox_item(profile_id, second_video_id)
            items = db.list_inbox_items(profile_id, limit=20, offset=0, statuses=("new",))
            by_video_id = {row["youtube_video_id"]: row for row in items}

            starred_item_id = int(by_video_id["FFFFFFFFFFF"]["inbox_item_id"])
            unstarred_item_id = int(by_video_id["GGGGGGGGGGG"]["inbox_item_id"])
            db.mark_inbox_starred(starred_item_id, starred=True)
            db.mark_inbox_starred(unstarred_item_id, starred=False)

            starred_items = db.list_inbox_items(
                profile_id,
                limit=20,
                offset=0,
                statuses=("new", "watched"),
                starred_only=True,
            )
            self.assertEqual(len(starred_items), 1)
            self.assertEqual(starred_items[0]["youtube_video_id"], "FFFFFFFFFFF")

            db.mark_inbox_trashed(starred_item_id)
            starred_items_after_trash = db.list_inbox_items(
                profile_id,
                limit=20,
                offset=0,
                statuses=("new", "watched"),
                starred_only=True,
            )
            self.assertEqual(len(starred_items_after_trash), 0)

            item_after_trash = db.get_inbox_item_with_video(starred_item_id)
            self.assertIsNotNone(item_after_trash)
            self.assertEqual(item_after_trash["status"], "dismissed")
            self.assertEqual(int(item_after_trash["is_starred"]), 0)


if __name__ == "__main__":
    unittest.main()
