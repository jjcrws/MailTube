from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
YOUTUBE_HANDLE_RE = re.compile(r"^@[A-Za-z0-9._-]{3,}$")
YOUTUBE_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)
API_BASE = "https://www.googleapis.com/youtube/v3"


class YouTubeAPIError(Exception):
    """Raised when a YouTube API call fails or returns invalid data."""


@dataclass(frozen=True)
class ChannelInfo:
    channel_id: str
    channel_title: str


@dataclass(frozen=True)
class VideoInfo:
    youtube_video_id: str
    title: str
    channel_id: str
    channel_title: str
    published_at: str
    thumbnail_url: str
    video_url: str
    duration_seconds: int | None = None


def extract_video_id(link: str) -> str | None:
    value = link.strip()
    if YOUTUBE_ID_RE.fullmatch(value):
        return value

    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    path_parts = [part for part in parsed.path.split("/") if part]

    if host in {"youtu.be", "www.youtu.be"} and path_parts:
        candidate = path_parts[0]
        return candidate if YOUTUBE_ID_RE.fullmatch(candidate) else None

    if host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        if parsed.path == "/watch":
            query = parse_qs(parsed.query)
            candidate = (query.get("v") or [None])[0]
            return candidate if candidate and YOUTUBE_ID_RE.fullmatch(candidate) else None
        if path_parts and path_parts[0] in {"shorts", "embed", "live"}:
            candidate = path_parts[1] if len(path_parts) > 1 else None
            return candidate if candidate and YOUTUBE_ID_RE.fullmatch(candidate) else None

    return None


def build_embed_url(video_id: str, *, autoplay: bool = True) -> str:
    params = {
        "autoplay": "1" if autoplay else "0",
        "controls": "1",
        "rel": "0",
        "iv_load_policy": "3",
    }
    return f"https://www.youtube.com/embed/{video_id}?{urlencode(params)}"


def _best_thumbnail_url(snippet: dict) -> str:
    thumbnails = snippet.get("thumbnails", {})
    return (
        thumbnails.get("high", {}).get("url")
        or thumbnails.get("medium", {}).get("url")
        or thumbnails.get("default", {}).get("url")
        or ""
    )


def fetch_video(video_id: str, *, api_key: str) -> VideoInfo:
    clean_video_id = video_id.strip()
    if not YOUTUBE_ID_RE.fullmatch(clean_video_id):
        raise YouTubeAPIError("Invalid YouTube video ID.")

    payload = _api_get(
        "videos",
        params={
            "part": "snippet",
            "id": clean_video_id,
            "maxResults": "1",
            "key": api_key,
        },
    )
    items = payload.get("items", [])
    if not items:
        raise YouTubeAPIError(f"Unknown YouTube video ID: {clean_video_id}")

    item = items[0]
    snippet = item.get("snippet", {})
    return VideoInfo(
        youtube_video_id=clean_video_id,
        title=snippet.get("title") or f"Imported YouTube video ({clean_video_id})",
        channel_id=snippet.get("channelId") or "",
        channel_title=snippet.get("channelTitle") or "",
        published_at=snippet.get("publishedAt") or "",
        thumbnail_url=_best_thumbnail_url(snippet),
        video_url=f"https://www.youtube.com/watch?v={clean_video_id}",
    )


def title_matches_keyword(title: str, keyword: str | None) -> bool:
    if not keyword:
        return True
    normalized_keyword = (
        html.unescape(keyword).casefold().strip().replace("\u2019", "'").replace("\u2018", "'")
    )
    if not normalized_keyword:
        return True
    normalized_title = html.unescape(title).casefold().replace("\u2019", "'").replace("\u2018", "'")
    return normalized_keyword in normalized_title


def duration_matches_bucket(duration_seconds: int | None, bucket: str | None) -> bool:
    clean_bucket = (bucket or "").strip().lower()
    if not clean_bucket:
        return True
    if duration_seconds is None:
        return False
    if clean_bucket == "short":
        return duration_seconds < 5 * 60
    if clean_bucket == "medium":
        return 5 * 60 <= duration_seconds <= 20 * 60
    if clean_bucket == "long":
        return duration_seconds > 20 * 60
    return False


def duration_matches_filter(
    duration_seconds: int | None,
    bucket: str | None,
    *,
    exclude_shorts: bool,
) -> bool:
    if exclude_shorts:
        if duration_seconds is None:
            return False
        if duration_seconds <= 3 * 60:
            return False
    return duration_matches_bucket(duration_seconds, bucket)


def published_at_on_or_after(published_at: str | None, threshold: str | None) -> bool:
    if not threshold:
        return True
    if not published_at:
        return False
    try:
        published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        cutoff = datetime.fromisoformat(threshold.replace("Z", "+00:00"))
    except ValueError:
        return False
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    return published >= cutoff


def _api_get(endpoint: str, *, params: dict[str, str]) -> dict:
    query = urlencode(params)
    url = f"{API_BASE}/{endpoint}?{query}"
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=20) as response:
            payload = response.read().decode("utf-8")
    except HTTPError as exc:
        message = exc.reason
        try:
            body = exc.read().decode("utf-8")
            data = json.loads(body)
            message = data.get("error", {}).get("message") or message
        except Exception:
            pass
        raise YouTubeAPIError(f"YouTube API error: {message}") from exc
    except URLError as exc:
        raise YouTubeAPIError(f"Failed to contact YouTube API: {exc.reason}") from exc

    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise YouTubeAPIError("YouTube API returned invalid JSON.") from exc


def _fetch_channel_by_id(channel_id: str, *, api_key: str) -> ChannelInfo:
    payload = _api_get(
        "channels",
        params={
            "part": "snippet",
            "id": channel_id,
            "maxResults": "1",
            "key": api_key,
        },
    )
    items = payload.get("items", [])
    if not items:
        raise YouTubeAPIError(f"Unknown channel ID: {channel_id}")
    item = items[0]
    return ChannelInfo(channel_id=item["id"], channel_title=item["snippet"]["title"])


def _fetch_channel_by_handle(handle: str, *, api_key: str) -> ChannelInfo:
    payload = _api_get(
        "channels",
        params={
            "part": "snippet",
            "forHandle": handle.removeprefix("@"),
            "maxResults": "1",
            "key": api_key,
        },
    )
    items = payload.get("items", [])
    if not items:
        raise YouTubeAPIError(f"Unknown channel handle: {handle}")
    item = items[0]
    return ChannelInfo(channel_id=item["id"], channel_title=item["snippet"]["title"])


def resolve_channel_input(channel_input: str, *, api_key: str) -> ChannelInfo:
    value = channel_input.strip()
    if not value:
        raise YouTubeAPIError("Channel input is required.")

    if YOUTUBE_CHANNEL_ID_RE.fullmatch(value):
        return _fetch_channel_by_id(value, api_key=api_key)

    if YOUTUBE_HANDLE_RE.fullmatch(value):
        return _fetch_channel_by_handle(value, api_key=api_key)

    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host != "youtube.com":
        raise YouTubeAPIError("Unsupported channel format. Use a YouTube channel URL or @handle.")

    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        raise YouTubeAPIError("Unsupported channel URL.")

    if parts[0] == "channel" and len(parts) >= 2:
        channel_id = parts[1]
        if not YOUTUBE_CHANNEL_ID_RE.fullmatch(channel_id):
            raise YouTubeAPIError("Invalid channel ID in URL.")
        return _fetch_channel_by_id(channel_id, api_key=api_key)

    if parts[0].startswith("@"):
        return _fetch_channel_by_handle(parts[0], api_key=api_key)

    raise YouTubeAPIError("Unsupported channel URL. Use /channel/<id> or /@handle.")


def fetch_channel_videos(
    channel_id: str,
    *,
    api_key: str,
    max_results: int = 50,
    include_duration: bool = False,
) -> list[VideoInfo]:
    def parse_duration_seconds(raw_duration: str | None) -> int | None:
        if not raw_duration:
            return None
        match = YOUTUBE_DURATION_RE.fullmatch(raw_duration)
        if not match:
            return None
        days = int(match.group("days") or 0)
        hours = int(match.group("hours") or 0)
        minutes = int(match.group("minutes") or 0)
        seconds = int(match.group("seconds") or 0)
        return (((days * 24) + hours) * 60 + minutes) * 60 + seconds

    def fetch_duration_map(video_ids: list[str]) -> dict[str, int]:
        if not video_ids:
            return {}
        payload = _api_get(
            "videos",
            params={
                "part": "contentDetails",
                "id": ",".join(video_ids),
                "maxResults": str(min(50, len(video_ids))),
                "key": api_key,
            },
        )
        duration_by_id: dict[str, int] = {}
        for item in payload.get("items", []):
            video_id = item.get("id")
            if not video_id or not YOUTUBE_ID_RE.fullmatch(video_id):
                continue
            raw_duration = item.get("contentDetails", {}).get("duration")
            duration_seconds = parse_duration_seconds(raw_duration)
            if duration_seconds is not None:
                duration_by_id[video_id] = duration_seconds
        return duration_by_id

    videos: list[VideoInfo] = []
    page_token: str | None = None
    remaining = max(1, max_results)

    while remaining > 0:
        batch_size = min(50, remaining)
        params = {
            "part": "snippet",
            "channelId": channel_id,
            "type": "video",
            "order": "date",
            "maxResults": str(batch_size),
            "key": api_key,
        }
        if page_token:
            params["pageToken"] = page_token

        payload = _api_get("search", params=params)
        items = payload.get("items", [])
        parsed_items: list[tuple[str, str, str, str, str]] = []
        for item in items:
            video_id = item.get("id", {}).get("videoId")
            snippet = item.get("snippet", {})
            if not video_id or not YOUTUBE_ID_RE.fullmatch(video_id):
                continue

            thumb = _best_thumbnail_url(snippet)
            title = snippet.get("title") or "(untitled)"
            published_at = snippet.get("publishedAt") or ""
            channel_title = snippet.get("channelTitle") or ""
            parsed_items.append((video_id, title, published_at, channel_title, thumb))

        duration_by_id: dict[str, int] = {}
        if include_duration:
            duration_by_id = fetch_duration_map([video_id for video_id, _, _, _, _ in parsed_items])

        for video_id, title, published_at, channel_title, thumb in parsed_items:
            videos.append(
                VideoInfo(
                    youtube_video_id=video_id,
                    title=title,
                    channel_id=channel_id,
                    channel_title=channel_title,
                    published_at=published_at,
                    thumbnail_url=thumb,
                    video_url=f"https://www.youtube.com/watch?v={video_id}",
                    duration_seconds=duration_by_id.get(video_id),
                )
            )

        remaining = max_results - len(videos)
        if remaining <= 0:
            break
        page_token = payload.get("nextPageToken")
        if not page_token:
            break

    return videos
