from __future__ import annotations

from dataclasses import dataclass

from .db import Database, VideoRecord
from .youtube import YouTubeAPIError, extract_video_id, fetch_video


FALLBACK_TITLE_TEMPLATE = "Imported YouTube video ({video_id})"
IMPORT_ACTIONS = {"watch", "inbox", "starred"}


@dataclass(frozen=True)
class ImportResult:
    inbox_item_id: int
    youtube_video_id: str
    action: str
    inserted: bool
    used_fallback_metadata: bool


def import_video_link(
    db: Database,
    profile_id: int,
    youtube_link: str,
    *,
    action: str,
    api_key: str | None,
) -> ImportResult:
    clean_action = (action or "").strip().lower()
    if clean_action not in IMPORT_ACTIONS:
        raise ValueError("Import action must be watch, inbox, or starred.")
    if profile_id <= 0 or not db.get_profile_by_id(profile_id):
        raise ValueError("Profile does not exist.")

    video_id = extract_video_id(youtube_link or "")
    if not video_id:
        raise ValueError("Enter a YouTube video link or video ID.")

    used_fallback = False
    if api_key:
        try:
            video = fetch_video(video_id, api_key=api_key)
        except YouTubeAPIError:
            used_fallback = True
        else:
            video_db_id = db.upsert_video(
                VideoRecord(
                    youtube_video_id=video.youtube_video_id,
                    title=video.title,
                    channel_id=video.channel_id,
                    channel_title=video.channel_title,
                    published_at=video.published_at,
                    thumbnail_url=video.thumbnail_url,
                    video_url=video.video_url,
                )
            )
    else:
        used_fallback = True

    if used_fallback:
        existing_video = db.get_video_by_youtube_id(video_id)
        if existing_video:
            video_db_id = int(existing_video["id"])
        else:
            video_db_id = db.upsert_video(
                VideoRecord(
                    youtube_video_id=video_id,
                    title=FALLBACK_TITLE_TEMPLATE.format(video_id=video_id),
                    channel_id="",
                    channel_title="",
                    published_at="",
                    thumbnail_url="",
                    video_url=f"https://www.youtube.com/watch?v={video_id}",
                )
            )

    inbox_item_id, inserted = db.ensure_inbox_item(profile_id, video_db_id)

    if clean_action == "watch":
        db.mark_inbox_watched(inbox_item_id, watched=True)
    elif clean_action == "starred":
        db.mark_inbox_watched(inbox_item_id, watched=False)
        db.mark_inbox_starred(inbox_item_id, starred=True)
    else:
        db.mark_inbox_watched(inbox_item_id, watched=False)

    return ImportResult(
        inbox_item_id=inbox_item_id,
        youtube_video_id=video_id,
        action=clean_action,
        inserted=inserted,
        used_fallback_metadata=used_fallback,
    )
