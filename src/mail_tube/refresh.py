from __future__ import annotations

from dataclasses import dataclass

from .db import Database, VideoRecord
from .youtube import (
    YouTubeAPIError,
    duration_matches_filter,
    fetch_channel_videos,
    published_at_on_or_after,
    resolve_channel_input,
    title_matches_keyword,
)


@dataclass(frozen=True)
class RefreshOutcome:
    status: str
    error_message: str | None
    fetched_count: int
    matched_count: int
    added_count: int


def refresh_profile(
    db: Database,
    profile_id: int,
    *,
    api_key: str | None,
    max_results_per_filter: int = 50,
) -> RefreshOutcome:
    run_id = db.start_refresh_run(profile_id)
    fetched_count = 0
    matched_count = 0
    added_count = 0
    errors: list[str] = []

    if not api_key:
        message = "Missing YOUTUBE_API_KEY. Set it in .mailtube.env or your environment."
        db.finish_refresh_run(
            run_id,
            status="error",
            error_message=message,
            added_count=0,
            matched_count=0,
            fetched_count=0,
        )
        return RefreshOutcome("error", message, 0, 0, 0)

    profile_filters = db.list_filters(profile_id)
    if not profile_filters:
        db.finish_refresh_run(
            run_id,
            status="ok",
            error_message=None,
            added_count=0,
            matched_count=0,
            fetched_count=0,
        )
        return RefreshOutcome("ok", None, 0, 0, 0)

    for row in profile_filters:
        filter_id = int(row["id"])
        channel_input = row["channel_input"]
        keyword = (row["keyword"] or "").strip()
        duration_bucket = (row["duration_bucket"] or "").strip().lower() or None
        exclude_shorts = bool(row["exclude_shorts"])
        since_mode = (row["since_mode"] or "anytime").strip().lower()
        since_published_after = row["since_published_after"] if since_mode == "from_now" else None

        try:
            channel = resolve_channel_input(channel_input, api_key=api_key)
            db.update_filter_resolution(
                filter_id,
                channel_id=channel.channel_id,
                channel_title=channel.channel_title,
                is_valid=True,
                validation_error=None,
            )
        except YouTubeAPIError as exc:
            message = str(exc)
            errors.append(f"Filter {filter_id}: {message}")
            db.update_filter_resolution(
                filter_id,
                channel_id=None,
                channel_title=None,
                is_valid=False,
                validation_error=message,
            )
            continue

        try:
            fetch_kwargs = {"max_results": max_results_per_filter}
            if duration_bucket or exclude_shorts:
                fetch_kwargs["include_duration"] = True
            videos = fetch_channel_videos(channel.channel_id, api_key=api_key, **fetch_kwargs)
        except YouTubeAPIError as exc:
            errors.append(f"Filter {filter_id}: {exc}")
            continue

        fetched_count += len(videos)
        for video in videos:
            if not title_matches_keyword(video.title, keyword):
                continue
            if not duration_matches_filter(video.duration_seconds, duration_bucket, exclude_shorts=exclude_shorts):
                continue
            if not published_at_on_or_after(video.published_at, since_published_after):
                continue

            matched_count += 1
            video_id = db.upsert_video(
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
            if db.insert_inbox_item(profile_id, video_id):
                added_count += 1

    if errors and (fetched_count or matched_count or added_count):
        status = "partial"
    elif errors:
        status = "error"
    else:
        status = "ok"
    error_message = " | ".join(errors)[:1200] if errors else None

    db.finish_refresh_run(
        run_id,
        status=status,
        error_message=error_message,
        added_count=added_count,
        matched_count=matched_count,
        fetched_count=fetched_count,
    )
    return RefreshOutcome(status, error_message, fetched_count, matched_count, added_count)


def refresh_all_profiles(db: Database, *, api_key: str | None) -> dict[int, RefreshOutcome]:
    outcomes: dict[int, RefreshOutcome] = {}
    for profile_id in db.profile_ids():
        outcomes[profile_id] = refresh_profile(db, profile_id, api_key=api_key)
    return outcomes
