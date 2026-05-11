from __future__ import annotations

from datetime import datetime, timezone
import html
import mimetypes
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import ParseResult, parse_qs, urlencode, urlparse

from .config import get_youtube_api_key
from .db import Database
from .importer import import_video_link
from .refresh import refresh_all_profiles, refresh_profile
from .youtube import build_embed_url

RESOURCES_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "resources"))
ALLOWED_RESOURCE_FILES = {"Email-logo.png", "Youtube-logo.png", "MailTube-logo_v0.png", "htmx.min.js"}


def _to_int(value: str | None, *, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _safe_return_to(value: str | None, *, default: str = "/inbox") -> str:
    if not value:
        return default
    candidate = value.strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return default
    return candidate


def _append_query_flag(url: str, key: str, value: str) -> str:
    joiner = "&" if "?" in url else "?"
    return f"{url}{joiner}{urlencode({key: value})}"


def _without_query_keys(url: str, keys: set[str]) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    for key in keys:
        query.pop(key, None)
    encoded = urlencode([(k, v) for k, vals in query.items() for v in vals])
    rebuilt = parsed.path if not encoded else f"{parsed.path}?{encoded}"
    if parsed.fragment:
        rebuilt = f"{rebuilt}#{parsed.fragment}"
    return rebuilt


def _drop_open_from_return_to_if_matches(value: str | None, *, inbox_item_id: int) -> str | None:
    if not value:
        return value
    parsed = urlparse(value)
    if parsed.path != "/inbox":
        return value
    query = parse_qs(parsed.query, keep_blank_values=True)
    open_id = _to_int((query.get("open") or [None])[0], default=-1)
    if open_id != inbox_item_id:
        return value
    query.pop("open", None)
    encoded = urlencode([(k, v) for k, vals in query.items() for v in vals])
    rebuilt = parsed.path if not encoded else f"{parsed.path}?{encoded}"
    if parsed.fragment:
        rebuilt = f"{rebuilt}#{parsed.fragment}"
    return rebuilt


def _relative_published_label(value: str | None) -> str:
    if not value:
        return "unknown time"

    try:
        raw = value.replace("Z", "+00:00")
        published_at = datetime.fromisoformat(raw)
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        published_day = published_at.astimezone(timezone.utc).date()
        now_day = datetime.now(timezone.utc).date()
        day_delta = (now_day - published_day).days
    except ValueError:
        return "unknown time"

    if day_delta <= 0:
        return "today"
    if day_delta == 1:
        return "yesterday"
    if day_delta < 30:
        return f"{day_delta} days ago"
    if day_delta < 365:
        months = day_delta // 30
        unit = "month" if months == 1 else "months"
        return f"{months} {unit} ago"
    years = day_delta // 365
    unit = "year" if years == 1 else "years"
    return f"{years} {unit} ago"


def _safe_display_text(value: str | None) -> str:
    if value is None:
        return ""
    return html.escape(html.unescape(value))


def _parse_length_mode(value: str | None) -> tuple[str | None, bool]:
    mode = (value or "no_shorts").strip().lower().replace("-", "_")
    if mode == "any":
        return None, False
    if mode in {"short", "medium", "long"}:
        return mode, True
    return None, True


def _filter_length_label(duration_bucket: str | None, exclude_shorts: bool) -> str:
    if duration_bucket == "short":
        return "short regular (3-5m)" if exclude_shorts else "short (<5m)"
    if duration_bucket == "medium":
        return "medium (5-20m)"
    if duration_bucket == "long":
        return "long (>20m)"
    return "no shorts (>3m)" if exclude_shorts else "any"


def _filter_length_mode_value(duration_bucket: str | None, exclude_shorts: bool) -> str:
    if duration_bucket in {"short", "medium", "long"}:
        return duration_bucket
    return "no_shorts" if exclude_shorts else "any"


def _selected_attr(value: str, selected: str) -> str:
    return ' selected' if value == selected else ""


EYE_ICON_SVG = (
    '<svg viewBox="0 0 24 24" aria-hidden="true">'
    '<path d="M2 12s3.8-6 10-6 10 6 10 6-3.8 6-10 6-10-6-10-6"></path>'
    '<circle cx="12" cy="12" r="3"></circle>'
    "</svg>"
)

TRASH_ICON_SVG = (
    '<svg viewBox="0 0 24 24" aria-hidden="true">'
    '<path d="M3 6h18"></path>'
    '<path d="M8 6V4h8v2"></path>'
    '<path d="M19 6l-1 14H6L5 6"></path>'
    '<path d="M10 11v6"></path>'
    '<path d="M14 11v6"></path>'
    "</svg>"
)

PEN_ICON_SVG = (
    '<svg viewBox="0 0 24 24" aria-hidden="true">'
    '<path d="M12 20h9"></path>'
    '<path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z"></path>'
    "</svg>"
)

STAR_ICON_SVG = (
    '<svg viewBox="0 0 24 24" aria-hidden="true">'
    '<path d="M12 3.8l2.5 5.1 5.6.8-4.1 4 1 5.7L12 16.8 7 19.4l1-5.7-4.1-4 5.6-.8L12 3.8"></path>'
    "</svg>"
)

STAR_FILLED_ICON_SVG = (
    '<svg viewBox="0 0 24 24" aria-hidden="true">'
    '<path fill="currentColor" stroke="none" d="M12 3.8l2.5 5.1 5.6.8-4.1 4 1 5.7L12 16.8 7 19.4l1-5.7-4.1-4 5.6-.8L12 3.8"></path>'
    "</svg>"
)

BACK_ICON_SVG = (
    '<svg viewBox="0 0 24 24" aria-hidden="true">'
    '<path d="M15 18l-6-6 6-6"></path>'
    "</svg>"
)


def _inbox_location(
    profile_id: int,
    active_list: str,
    *,
    page: int | None = None,
    open_item_id: int | None = None,
) -> str:
    payload: dict[str, int | str] = {"profile": profile_id, "list": active_list}
    if page is not None:
        payload["page"] = max(1, page)
    if open_item_id is not None and open_item_id > 0:
        payload["open"] = open_item_id
    return f"/inbox?{urlencode(payload)}"


def _base_layout(title: str, body: str) -> str:
    safe_title = html.escape(title)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title}</title>
  <style>
    @import url("https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=Saira+Semi+Condensed:wght@500;600;700&display=swap");
    :root {{
      --bg: #07090d;
      --bg-elev: #0d1118;
      --bg-elev-2: #111724;
      --line: #253247;
      --line-soft: #1b2638;
      --text: #d8e3f4;
      --muted: #8499b7;
      --accent: #1ec4ff;
      --accent-soft: rgba(30, 196, 255, 0.17);
      --danger: #ff6464;
      --ok: #5ff7ae;
      --radius: 14px;
      --radius-tight: 10px;
      --shadow: 0 24px 48px rgba(0, 0, 0, 0.32);
      --workspace-max-width: 1760px;
      --rail-col-width: 220px;
      --center-col-min: 620px;
      --dock-col-min: 320px;
      --dock-col-max: 500px;
    }}
    * {{
      box-sizing: border-box;
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      color: var(--text);
      font-family: "IBM Plex Mono", "Consolas", monospace;
      background:
        radial-gradient(circle at 10% 10%, rgba(30, 196, 255, 0.1), transparent 30%),
        radial-gradient(circle at 80% 5%, rgba(95, 247, 174, 0.08), transparent 28%),
        linear-gradient(120deg, rgba(255, 255, 255, 0.03) 1px, transparent 1px),
        linear-gradient(0deg, var(--bg), #05070a 58%);
      background-size: auto, auto, 28px 28px, auto;
      background-repeat: no-repeat, no-repeat, repeat, repeat;
      background-attachment: fixed, fixed, fixed, fixed;
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background: linear-gradient(rgba(255, 255, 255, 0.015), rgba(0, 0, 0, 0.08));
      mix-blend-mode: soft-light;
    }}
    .app-frame {{
      width: min(var(--workspace-max-width), calc(100vw - 28px));
      margin: 14px auto 24px;
      position: relative;
      z-index: 1;
    }}
    .workspace {{
      display: grid;
      grid-template-columns:
        var(--rail-col-width)
        minmax(var(--center-col-min), 1fr)
        minmax(var(--dock-col-min), var(--dock-col-max));
      gap: 12px;
      align-items: start;
    }}
    .panel {{
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: linear-gradient(180deg, var(--bg-elev-2), var(--bg-elev));
      box-shadow: var(--shadow);
      backdrop-filter: blur(3px);
    }}
    .rail {{
      padding: 16px 14px;
      position: sticky;
      top: 10px;
      max-height: calc(100vh - 24px);
      overflow: auto;
      animation: rise-in 280ms ease-out both;
    }}
    .rail-brand {{
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 16px;
      font-family: "Saira Semi Condensed", "Arial Narrow", sans-serif;
      font-size: 1.3rem;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }}
    .rail-brand img {{
      width: 28px;
      height: 28px;
      object-fit: contain;
      filter: saturate(1.25);
    }}
    .rail-group {{
      margin-bottom: 18px;
      border-top: 1px solid var(--line-soft);
      padding-top: 12px;
    }}
    .rail-label {{
      color: var(--muted);
      font-size: 0.7rem;
      letter-spacing: 0.11em;
      text-transform: uppercase;
      margin-bottom: 8px;
      display: block;
    }}
    .rail-link {{
      display: block;
      color: var(--text);
      text-decoration: none;
      border: 1px solid var(--line-soft);
      border-radius: var(--radius-tight);
      padding: 9px 10px;
      margin-bottom: 7px;
      transition: border-color 140ms ease, transform 140ms ease, background-color 140ms ease;
      background: rgba(255, 255, 255, 0.01);
    }}
    .rail-link:hover {{
      border-color: #386096;
      background: rgba(255, 255, 255, 0.035);
      transform: translateX(2px);
    }}
    .rail-link.active {{
      border-color: var(--accent);
      box-shadow: inset 2px 0 0 var(--accent), 0 0 0 1px rgba(30, 196, 255, 0.15);
      background: linear-gradient(90deg, rgba(30, 196, 255, 0.12), rgba(30, 196, 255, 0.03));
    }}
    .rail-tool-link {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      color: var(--text);
      text-decoration: none;
      border: 1px solid #3f526f;
      border-radius: var(--radius-tight);
      padding: 9px 10px;
      margin-bottom: 7px;
      transition: border-color 140ms ease, background-color 140ms ease, transform 140ms ease;
      background: linear-gradient(90deg, rgba(30, 196, 255, 0.11), rgba(30, 196, 255, 0.03));
      font-size: 0.82rem;
      letter-spacing: 0.03em;
      text-transform: capitalize;
      font-weight: 500;
    }}
    .rail-tool-link::after {{
      content: "↗";
      color: var(--accent);
      font-size: 0.85rem;
    }}
    .rail-tool-link:hover {{
      border-color: #63bfff;
      background: linear-gradient(90deg, rgba(30, 196, 255, 0.2), rgba(30, 196, 255, 0.06));
      transform: translateX(1px);
    }}
    .rail-future {{
      color: var(--muted);
      border: 1px dashed #31445f;
      border-radius: var(--radius-tight);
      padding: 9px 10px;
      margin-bottom: 7px;
      font-size: 0.87rem;
      opacity: 0.88;
    }}
    .workspace-main {{
      padding: 12px;
      display: grid;
      gap: 12px;
      animation: rise-in 320ms ease-out both;
      animation-delay: 80ms;
    }}
    .workspace-head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      border: 1px solid var(--line-soft);
      border-radius: var(--radius-tight);
      padding: 10px 12px;
      background: rgba(255, 255, 255, 0.015);
    }}
    .workspace-title {{
      margin: 0;
      font-family: "Saira Semi Condensed", "Arial Narrow", sans-serif;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-size: 1rem;
    }}
    .workspace-subtitle {{
      margin: 4px 0 0;
      color: var(--muted);
      font-size: 0.76rem;
    }}
    .head-actions {{
      display: flex;
      gap: 8px;
      align-items: center;
    }}
    .banner {{
      border: 1px solid #6b2a2a;
      background: rgba(130, 34, 34, 0.15);
      border-radius: var(--radius-tight);
      padding: 9px 10px;
      color: #ffc7c7;
    }}
    .banner.info {{
      border-color: #315b7c;
      background: rgba(30, 196, 255, 0.08);
      color: #bfefff;
    }}
    .import-panel {{
      position: relative;
    }}
    .import-trigger {{
      list-style: none;
      border: 1px solid #2b3a52;
      border-radius: 999px;
      padding: 8px 11px;
      background: #0d1626;
      color: var(--text);
      font-size: 0.8rem;
      min-height: 34px;
      cursor: pointer;
      user-select: none;
    }}
    .import-trigger::-webkit-details-marker {{
      display: none;
    }}
    .import-panel[open] .import-trigger {{
      border-color: var(--accent);
      box-shadow: 0 0 0 2px var(--accent-soft);
    }}
    .import-popover {{
      position: absolute;
      top: calc(100% + 8px);
      right: 0;
      z-index: 80;
      width: min(520px, calc(100vw - 40px));
      border: 1px solid #31506f;
      border-radius: var(--radius-tight);
      padding: 10px;
      background: linear-gradient(180deg, #101827, #0b111d);
      box-shadow: 0 18px 36px rgba(0, 0, 0, 0.38);
    }}
    .import-form {{
      display: grid;
      gap: 9px;
    }}
    .import-form input[name="youtube_link"] {{
      width: 100%;
    }}
    .import-actions {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }}
    .workspace.is-import-dragging::after {{
      content: "Drop YouTube link to import";
      position: fixed;
      inset: 14px;
      z-index: 90;
      display: grid;
      place-items: center;
      border: 1px dashed var(--accent);
      border-radius: var(--radius);
      background: rgba(7, 9, 13, 0.78);
      color: var(--text);
      font-family: "Saira Semi Condensed", "Arial Narrow", sans-serif;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      pointer-events: none;
    }}
    .message-list {{
      display: grid;
      gap: 8px;
    }}
    .message-row {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 10px;
      border: 1px solid var(--line-soft);
      border-radius: var(--radius-tight);
      padding: 11px 10px;
      background: linear-gradient(160deg, rgba(255, 255, 255, 0.015), rgba(255, 255, 255, 0));
      transition: border-color 130ms ease, transform 130ms ease;
    }}
    .message-row:hover {{
      border-color: #406997;
      transform: translateY(-1px);
    }}
    .message-row.active {{
      border-color: var(--accent);
      box-shadow: inset 2px 0 0 var(--accent);
      background: linear-gradient(90deg, rgba(30, 196, 255, 0.09), rgba(30, 196, 255, 0.02));
    }}
    .message-main a {{
      color: var(--text);
      text-decoration: none;
      display: inline-block;
      line-height: 1.4;
      font-weight: 500;
    }}
    .message-main a:hover {{
      text-decoration: underline;
      text-decoration-thickness: 1px;
    }}
    .message-meta {{
      color: var(--muted);
      font-size: 0.77rem;
      margin-top: 6px;
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .message-actions {{
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }}
    .item-picker {{
      display: none;
      align-items: center;
      justify-content: center;
      width: 34px;
      height: 34px;
      border-radius: 999px;
      border: 1px solid #2b3a52;
      background: #0d1626;
      cursor: pointer;
    }}
    .item-picker input {{
      width: 14px;
      height: 14px;
      margin: 0;
      accent-color: #1ec4ff;
      cursor: pointer;
    }}
    .workspace.select-mode .item-picker {{
      display: inline-flex;
    }}
    .workspace.select-mode .item-action-form {{
      display: none;
    }}
    .selection-toggle {{
      min-width: 98px;
    }}
    .bulk-toolbar {{
      display: none;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      border: 1px dashed #3f526f;
      border-radius: var(--radius-tight);
      padding: 9px 10px;
      background: rgba(30, 196, 255, 0.06);
    }}
    .workspace.select-mode .bulk-toolbar {{
      display: flex;
    }}
    .bulk-count {{
      margin: 0;
      color: var(--muted);
      font-size: 0.78rem;
      white-space: nowrap;
    }}
    .bulk-actions {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
      margin-left: auto;
    }}
    .watch-dock {{
      padding: 12px;
      position: sticky;
      top: 10px;
      animation: rise-in 340ms ease-out both;
      animation-delay: 140ms;
      overflow: auto;
      max-height: calc(100vh - 24px);
    }}
    .watch-dock-head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      margin-bottom: 10px;
    }}
    .watch-title {{
      margin: 0;
      font-size: 0.9rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
    }}
    .watch-dock-empty {{
      border: 1px dashed #355173;
      border-radius: var(--radius-tight);
      padding: 16px 12px;
      color: var(--muted);
      line-height: 1.55;
      background: rgba(255, 255, 255, 0.015);
      min-height: 220px;
      display: grid;
      place-items: center;
      text-align: center;
    }}
    .watch-item-title {{
      margin: 0 0 8px;
      line-height: 1.4;
      font-size: 1rem;
    }}
    .watch-meta {{
      color: var(--muted);
      font-size: 0.78rem;
      margin: 0 0 10px;
    }}
    iframe {{
      width: 100%;
      aspect-ratio: 16 / 9;
      border: 1px solid #213149;
      border-radius: var(--radius-tight);
      background: #000;
    }}
    .watch-actions {{
      margin-top: 10px;
      display: flex;
      gap: 7px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }}
    .pagination {{
      border-top: 1px solid var(--line-soft);
      margin-top: 2px;
      padding-top: 10px;
      display: grid;
      grid-template-columns: 1fr auto 1fr;
      align-items: center;
      gap: 10px;
    }}
    .pagination .next {{
      text-align: right;
    }}
    .pagination p {{
      margin: 0;
      color: var(--muted);
      text-align: center;
      font-size: 0.75rem;
      letter-spacing: 0.04em;
    }}
    .drawer-title {{
      margin: 0;
      font-family: "Saira Semi Condensed", "Arial Narrow", sans-serif;
      text-transform: uppercase;
      letter-spacing: 0.09em;
      font-size: 0.75rem;
      color: var(--muted);
    }}
    .drawer-row {{
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
    }}
    .drawer-link {{
      display: inline-flex;
      border: 1px solid var(--line-soft);
      border-radius: 999px;
      padding: 8px 11px;
      color: var(--text);
      text-decoration: none;
      font-size: 0.78rem;
      background: rgba(255, 255, 255, 0.015);
    }}
    .settings-shell {{
      max-width: 1100px;
      margin: 0 auto;
      display: grid;
      gap: 12px;
    }}
    .settings-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      padding: 12px 14px;
    }}
    .settings-title {{
      margin: 0;
      font-family: "Saira Semi Condensed", "Arial Narrow", sans-serif;
      text-transform: uppercase;
      letter-spacing: 0.09em;
      font-size: 1rem;
    }}
    .settings-subtitle {{
      margin: 4px 0 0;
      color: var(--muted);
      font-size: 0.75rem;
    }}
    .settings-panel {{
      padding: 12px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.85rem;
    }}
    th, td {{
      text-align: left;
      border-top: 1px solid var(--line-soft);
      padding: 9px 8px;
      vertical-align: top;
    }}
    th {{
      color: var(--muted);
      font-size: 0.73rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    .filter-actions {{
      display: flex;
      justify-content: flex-end;
      gap: 6px;
    }}
    .filter-row-editing {{
      background: rgba(30, 196, 255, 0.06);
      box-shadow: inset 2px 0 0 var(--accent);
    }}
    .edit-context {{
      border: 1px solid #315b7c;
      border-radius: var(--radius-tight);
      padding: 9px 10px;
      background: rgba(30, 196, 255, 0.08);
      color: #bfefff;
      font-size: 0.8rem;
      line-height: 1.45;
      margin-bottom: 10px;
    }}
    .edit-context strong {{
      color: var(--text);
    }}
    .row {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }}
    input, select, button {{
      border: 1px solid #2b3a52;
      border-radius: 999px;
      padding: 8px 11px;
      background: #0d1626;
      color: var(--text);
      font-family: "IBM Plex Mono", "Consolas", monospace;
      font-size: 0.8rem;
      min-height: 34px;
    }}
    input, select {{
      min-width: 0;
    }}
    button {{
      cursor: pointer;
      transition: border-color 130ms ease, background-color 130ms ease;
    }}
    button:hover {{
      border-color: #4f82c3;
      background: #132038;
    }}
    button:focus-visible,
    a:focus-visible,
    summary:focus-visible,
    input:focus-visible,
    select:focus-visible {{
      outline: none;
      box-shadow: 0 0 0 2px var(--accent-soft);
      border-color: var(--accent);
    }}
    .icon-button {{
      min-width: 34px;
      width: 34px;
      height: 34px;
      padding: 0;
      display: grid;
      place-items: center;
      border-radius: 999px;
    }}
    .icon-button svg,
    .icon-link-button svg {{
      width: 15px;
      height: 15px;
      stroke: currentColor;
      fill: none;
      stroke-width: 1.8;
      stroke-linecap: round;
      stroke-linejoin: round;
    }}
    .icon-button.starred {{
      color: #f5d56b;
      border-color: #f5d56b;
    }}
    .icon-link-button {{
      width: 34px;
      height: 34px;
      border: 1px solid #2b3a52;
      border-radius: 999px;
      color: var(--text);
      text-decoration: none;
      display: inline-grid;
      place-items: center;
      background: #0d1626;
      flex-shrink: 0;
    }}
    .muted {{
      color: var(--muted);
    }}
    .danger {{
      color: var(--danger);
    }}
    .ok {{
      color: var(--ok);
    }}
    .empty-state {{
      padding: 26px 20px;
      border: 1px dashed #31445f;
      border-radius: var(--radius-tight);
      text-align: center;
      color: var(--muted);
      line-height: 1.6;
      background: rgba(255, 255, 255, 0.012);
    }}
    .mobile-close {{
      display: none;
    }}
    form.is-pending {{
      opacity: 0.62;
      pointer-events: none;
    }}
    @media (max-width: 1180px) {{
      .workspace {{
        grid-template-columns: 190px minmax(420px, 1fr) minmax(280px, 390px);
      }}
    }}
    @media (max-width: 980px) {{
      .workspace {{
        grid-template-columns: 1fr;
      }}
      .rail {{
        position: static;
        max-height: none;
      }}
      .watch-dock {{
        position: static;
        max-height: none;
      }}
      .watch-dock.has-item {{
        position: fixed;
        inset: 10px;
        margin: 0;
        z-index: 60;
        overflow: auto;
      }}
      .mobile-close {{
        display: inline-flex;
      }}
    }}
    @keyframes rise-in {{
      from {{
        opacity: 0;
        transform: translateY(8px);
      }}
      to {{
        opacity: 1;
        transform: translateY(0);
      }}
    }}
  </style>
</head>
<body>
  <main class="app-frame">
    {body}
  </main>
  <script src="/resources/htmx.min.js"></script>
  <script>
    (() => {{
      const workspaceSelector = "#inbox-workspace";
      let pendingWorkspaceTopReset = false;

      function getWorkspace() {{
        return document.querySelector(workspaceSelector);
      }}

      function isEditableTarget(target) {{
        if (!(target instanceof Element)) {{
          return false;
        }}
        return Boolean(target.closest("input, textarea, select, [contenteditable='true']"));
      }}

      function firstImportCandidate(text) {{
        const value = (text || "").trim();
        if (!value) {{
          return "";
        }}
        const urlMatch = value.match(/https?:\/\/(?:www\.)?(?:youtube\.com|youtube-nocookie\.com|youtu\.be)\/[^\s<>"']+/i);
        if (urlMatch) {{
          return urlMatch[0].replace(/[),.;]+$/, "");
        }}
        return /^[A-Za-z0-9_-]{{11}}$/.test(value) ? value : "";
      }}

      function transferText(dataTransfer) {{
        if (!dataTransfer) {{
          return "";
        }}
        return dataTransfer.getData("text/uri-list") || dataTransfer.getData("text/plain") || "";
      }}

      function openImportPrompt(candidate) {{
        const workspace = getWorkspace();
        if (!workspace) {{
          return false;
        }}
        const panel = workspace.querySelector("[data-import-panel]");
        const input = workspace.querySelector("[data-import-link]");
        if (!(panel instanceof HTMLDetailsElement) || !(input instanceof HTMLInputElement)) {{
          return false;
        }}
        panel.open = true;
        input.value = candidate;
        input.focus();
        input.select();
        return true;
      }}

      function resetWindowScrollTop() {{
        window.scrollTo(0, 0);
        document.documentElement.scrollTop = 0;
        document.body.scrollTop = 0;
      }}

      function requestWorkspaceTopReset() {{
        pendingWorkspaceTopReset = true;
      }}

      function selectedInputs(workspace) {{
        return Array.from(workspace.querySelectorAll(".item-select:checked"));
      }}

      function selectedIds(workspace) {{
        return selectedInputs(workspace).map((input) => input.value);
      }}

      function updateSelectionUi(workspace) {{
        const ids = selectedIds(workspace);
        const countEl = workspace.querySelector("[data-bulk-count]");
        if (countEl) {{
          countEl.textContent = `${{ids.length}} selected`;
        }}
        workspace.querySelectorAll("[data-bulk-submit]").forEach((button) => {{
          if (button instanceof HTMLButtonElement) {{
            button.disabled = ids.length === 0;
          }}
        }});
        const itemIdsField = workspace.querySelector('input[name="item_ids"]');
        if (itemIdsField instanceof HTMLInputElement) {{
          itemIdsField.value = ids.join(",");
        }}
        const returnToField = workspace.querySelector('[data-bulk-form] input[name="return_to"]');
        const returnTo = workspace.dataset.returnTo;
        if (returnToField instanceof HTMLInputElement && typeof returnTo === "string" && returnTo.startsWith("/")) {{
          returnToField.value = returnTo;
        }}
      }}

      function htmxIsAvailable() {{
        return Boolean(window.htmx);
      }}

      function hxTargetElement(source) {{
        const targetSpec = source.getAttribute("hx-target");
        if (!targetSpec) {{
          return null;
        }}
        if (targetSpec === "this") {{
          return source;
        }}
        if (targetSpec.startsWith("closest ")) {{
          return source.closest(targetSpec.slice("closest ".length).trim());
        }}
        return document.querySelector(targetSpec);
      }}

      function fragmentHtml(fragment) {{
        return Array.from(fragment.childNodes).map((node) => {{
          if (node.nodeType === Node.ELEMENT_NODE) {{
            return node.outerHTML;
          }}
          return node.textContent || "";
        }}).join("");
      }}

      function selectedHxFragment(source, target, fragment) {{
        const selectSpec = source.getAttribute("hx-select");
        if (selectSpec && selectSpec !== "unset") {{
          const selected = fragment.querySelectorAll(selectSpec);
          if (selected.length > 0) {{
            const selectedFragment = document.createDocumentFragment();
            selected.forEach((node) => selectedFragment.appendChild(node));
            return selectedFragment;
          }}
        }}
        if (target.id) {{
          const matchingTarget = fragment.getElementById(target.id);
          if (matchingTarget) {{
            const selectedFragment = document.createDocumentFragment();
            selectedFragment.appendChild(matchingTarget);
            return selectedFragment;
          }}
        }}
        return fragment;
      }}

      function applyHxResponse(source, target, htmlText, pushUrl) {{
        const template = document.createElement("template");
        template.innerHTML = htmlText;

        template.content.querySelectorAll("[hx-swap-oob]").forEach((incoming) => {{
          const id = incoming.getAttribute("id");
          const existing = id ? document.getElementById(id) : null;
          if (!existing) {{
            incoming.remove();
            return;
          }}
          incoming.removeAttribute("hx-swap-oob");
          existing.replaceWith(incoming);
        }});

        const swap = source.getAttribute("hx-swap") || "innerHTML";
        const selectedFragment = selectedHxFragment(source, target, template.content);
        const replacement = fragmentHtml(selectedFragment);
        let swapTarget = target;
        if (swap.startsWith("outerHTML")) {{
          const wrapper = document.createElement("template");
          wrapper.innerHTML = replacement;
          const firstElement = wrapper.content.firstElementChild;
          target.replaceWith(wrapper.content);
          if (firstElement?.id) {{
            swapTarget = document.getElementById(firstElement.id) || firstElement;
          }} else {{
            swapTarget = firstElement || target;
          }}
        }} else {{
          target.innerHTML = replacement;
        }}

        if (pushUrl && pushUrl !== "false" && pushUrl !== window.location.pathname + window.location.search) {{
          history.pushState({{}}, "", pushUrl);
        }}

        const workspace = getWorkspace();
        if (workspace) {{
          updateSelectionUi(workspace);
        }}
        swapTarget.dispatchEvent(new CustomEvent("htmx:afterSwap", {{ bubbles: true }}));
        swapTarget.dispatchEvent(new CustomEvent("htmx:afterSettle", {{ bubbles: true }}));
        return swapTarget;
      }}

      async function submitHxForm(form) {{
        const target = hxTargetElement(form);
        if (!target) {{
          return false;
        }}
        const response = await fetch(form.action, {{
          method: (form.method || "post").toUpperCase(),
          body: new FormData(form),
          credentials: "same-origin",
          headers: {{
            "HX-Request": "true",
            "HX-Current-URL": window.location.href,
          }},
        }});
        const htmlText = await response.text();
        const pushUrl = response.headers.get("HX-Push-Url") || form.querySelector('input[name="return_to"]')?.value || "";
        applyHxResponse(form, target, htmlText, pushUrl);
        return true;
      }}

      async function followHxLink(link) {{
        const target = hxTargetElement(link);
        const url = link.getAttribute("hx-get");
        if (!target || !url) {{
          return false;
        }}
        const response = await fetch(url, {{
          method: "GET",
          credentials: "same-origin",
          headers: {{
            "HX-Request": "true",
            "HX-Current-URL": window.location.href,
          }},
        }});
        const htmlText = await response.text();
        const pushUrl = link.getAttribute("hx-push-url") || response.headers.get("HX-Push-Url") || link.href;
        applyHxResponse(link, target, htmlText, pushUrl);
        return true;
      }}

      function setSelectMode(workspace, enabled) {{
        workspace.classList.toggle("select-mode", enabled);
        const toggle = workspace.querySelector("[data-select-toggle]");
        if (toggle instanceof HTMLButtonElement) {{
          toggle.textContent = enabled ? "Exit Select" : "Select Mode";
          toggle.setAttribute("aria-pressed", enabled ? "true" : "false");
        }}
        if (!enabled) {{
          workspace.querySelectorAll(".item-select:checked").forEach((input) => {{
            if (input instanceof HTMLInputElement) {{
              input.checked = false;
            }}
          }});
        }}
        updateSelectionUi(workspace);
      }}

      document.addEventListener(
        "submit",
        (event) => {{
          const target = event.target;
          if (!(target instanceof HTMLFormElement)) {{
            return;
          }}
          if (target.getAttribute("action") === "/inbox/refresh") {{
            requestWorkspaceTopReset();
          }}
          if (!target.matches("[data-bulk-form]")) {{
            return;
          }}
          const workspace = getWorkspace();
          if (!workspace) {{
            return;
          }}
          updateSelectionUi(workspace);
          const selected = selectedIds(workspace);
          if (selected.length === 0) {{
            event.preventDefault();
          }}
        }},
        true,
      );

      document.addEventListener(
        "submit",
        (event) => {{
          if (htmxIsAvailable()) {{
            return;
          }}
          const target = event.target;
          if (!(target instanceof HTMLFormElement) || !target.matches("form[hx-target]")) {{
            return;
          }}
          event.preventDefault();
          submitHxForm(target).catch(() => {{
            target.submit();
          }});
        }},
        true,
      );

      document.addEventListener(
        "click",
        (event) => {{
          const target = event.target;
          if (!(target instanceof Element)) {{
            return;
          }}
          const workspace = getWorkspace();
          if (!workspace) {{
            return;
          }}

          const toggle = target.closest("[data-select-toggle]");
          if (toggle) {{
            event.preventDefault();
            setSelectMode(workspace, !workspace.classList.contains("select-mode"));
            return;
          }}

          const cancel = target.closest("[data-bulk-cancel]");
          if (cancel) {{
            event.preventDefault();
            setSelectMode(workspace, false);
            return;
          }}

          const listNavLink = target.closest(".rail-link, #pagination a");
          if (listNavLink instanceof HTMLAnchorElement) {{
            requestWorkspaceTopReset();
          }}

          const rowLink = target.closest(".message-main a");
          if (rowLink instanceof HTMLAnchorElement) {{
            if (workspace.classList.contains("select-mode")) {{
              event.preventDefault();
              const row = rowLink.closest(".message-row");
              const checkbox = row?.querySelector(".item-select");
              if (checkbox instanceof HTMLInputElement) {{
                checkbox.checked = !checkbox.checked;
                updateSelectionUi(workspace);
              }}
              return;
            }}

            updateSelectionUi(workspace);
          }}
        }},
        true,
      );

      document.addEventListener(
        "click",
        (event) => {{
          if (htmxIsAvailable()) {{
            return;
          }}
          const target = event.target;
          if (!(target instanceof Element)) {{
            return;
          }}
          const link = target.closest("a[hx-get][hx-target]");
          if (!(link instanceof HTMLAnchorElement)) {{
            return;
          }}
          event.preventDefault();
          followHxLink(link).catch(() => {{
            window.location.href = link.href;
          }});
        }},
        true,
      );

      document.addEventListener("dragover", (event) => {{
        const workspace = getWorkspace();
        if (!workspace || !event.dataTransfer) {{
          return;
        }}
        const types = Array.from(event.dataTransfer.types || []);
        if (!types.includes("text/uri-list") && !types.includes("text/plain")) {{
          return;
        }}
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
        workspace.classList.add("is-import-dragging");
      }});

      document.addEventListener("dragleave", (event) => {{
        const workspace = getWorkspace();
        if (!workspace) {{
          return;
        }}
        if (event.target === document || event.clientX <= 0 || event.clientY <= 0) {{
          workspace.classList.remove("is-import-dragging");
        }}
      }});

      document.addEventListener("dragend", () => {{
        const workspace = getWorkspace();
        if (workspace) {{
          workspace.classList.remove("is-import-dragging");
        }}
      }});

      document.addEventListener("drop", (event) => {{
        const workspace = getWorkspace();
        if (!workspace) {{
          return;
        }}
        workspace.classList.remove("is-import-dragging");
        const candidate = firstImportCandidate(transferText(event.dataTransfer));
        if (!candidate) {{
          return;
        }}
        event.preventDefault();
        openImportPrompt(candidate);
      }});

      document.addEventListener("paste", (event) => {{
        if (isEditableTarget(event.target)) {{
          return;
        }}
        const candidate = firstImportCandidate(event.clipboardData?.getData("text/plain") || "");
        if (!candidate) {{
          return;
        }}
        event.preventDefault();
        openImportPrompt(candidate);
      }});

      document.body.addEventListener("htmx:afterSwap", (event) => {{
        const workspace = getWorkspace();
        if (!workspace) {{
          return;
        }}
        const swapTarget = event.target;
        if (!(swapTarget instanceof Element)) {{
          return;
        }}
        if (swapTarget !== workspace && !workspace.contains(swapTarget)) {{
          return;
        }}
        updateSelectionUi(workspace);
      }});

      document.body.addEventListener("htmx:afterSettle", () => {{
        if (!pendingWorkspaceTopReset) {{
          return;
        }}
        pendingWorkspaceTopReset = false;
        requestAnimationFrame(() => {{
          resetWindowScrollTop();
        }});
      }});

      document.addEventListener(
        "change",
        (event) => {{
          const target = event.target;
          if (!(target instanceof HTMLInputElement) || !target.classList.contains("item-select")) {{
            return;
          }}
          const workspace = getWorkspace();
          if (!workspace) {{
            return;
          }}
          updateSelectionUi(workspace);
        }},
        true,
      );

      const initialWorkspace = getWorkspace();
      if (initialWorkspace) {{
        updateSelectionUi(initialWorkspace);
      }}
    }})();
  </script>
</body>
</html>"""


class MailTubeHandler(BaseHTTPRequestHandler):
    db: Database
    page_size: int

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/":
            self._redirect("/inbox")
            return
        if path == "/inbox":
            self._render_inbox(query)
            return
        if path == "/filters":
            self._render_filters(query)
            return
        if path.startswith("/resources/"):
            self._serve_resource(path)
            return
        if path == "/profiles":
            self._render_profiles(query)
            return
        if path.startswith("/watch/"):
            self._render_watch(parsed)
            return
        self.send_error(404, "Not Found")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        form = self._parse_form_data()

        if path == "/inbox/refresh":
            self._post_refresh(form)
            return
        if path == "/inbox/import":
            self._post_import(form)
            return
        if path.startswith("/inbox/item/") and path.endswith("/watch"):
            self._post_mark_watch(path, form, watched=True)
            return
        if path.startswith("/inbox/item/") and path.endswith("/unwatch"):
            self._post_mark_watch(path, form, watched=False)
            return
        if path.startswith("/inbox/item/") and path.endswith("/trash"):
            self._post_mark_trash(path, form)
            return
        if path.startswith("/inbox/item/") and path.endswith("/star"):
            self._post_mark_star(path, form, starred=True)
            return
        if path.startswith("/inbox/item/") and path.endswith("/unstar"):
            self._post_mark_star(path, form, starred=False)
            return
        if path.startswith("/inbox/item/") and path.endswith("/close"):
            self._post_close_viewer(path, form)
            return
        if path == "/inbox/bulk":
            self._post_bulk_action(form)
            return
        if path == "/filters/add":
            self._post_add_filter(form)
            return
        if path == "/filters/update":
            self._post_update_filter(form)
            return
        if path == "/filters/delete":
            self._post_delete_filter(form)
            return
        if path == "/profiles/create":
            self._post_create_profile(form)
            return
        if path == "/profiles/set-active":
            self._post_set_active(form)
            return

        self.send_error(404, "Not Found")

    def _parse_form_data(self) -> dict[str, str]:
        length = _to_int(self.headers.get("Content-Length"), default=0)
        body = self.rfile.read(max(0, length)).decode("utf-8")
        parsed = parse_qs(body, keep_blank_values=True)
        return {key: values[0] for key, values in parsed.items()}

    def _send_html(self, body: str, *, status: int = 200, headers: dict[str, str] | None = None) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if headers:
            for name, value in headers.items():
                self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_bytes(self, payload: bytes, *, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _serve_resource(self, path: str) -> None:
        filename = path.removeprefix("/resources/")
        if "/" in filename or filename not in ALLOWED_RESOURCE_FILES:
            self.send_error(404, "Not Found")
            return
        resource_path = os.path.join(RESOURCES_DIR, filename)
        try:
            with open(resource_path, "rb") as handle:
                payload = handle.read()
        except OSError:
            self.send_error(404, "Not Found")
            return
        content_type = mimetypes.guess_type(resource_path)[0] or "application/octet-stream"
        self._send_bytes(payload, content_type=content_type)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def _is_htmx_request(self) -> bool:
        return self.headers.get("HX-Request", "").strip().lower() == "true"

    def _htmx_current_open_id(self) -> int | None:
        current_url = self.headers.get("HX-Current-URL")
        if not current_url:
            return None
        parsed = urlparse(current_url)
        if parsed.path != "/inbox":
            return None
        open_id = _to_int((parse_qs(parsed.query).get("open") or [None])[0], default=-1)
        return open_id if open_id > 0 else None

    def _respond_inbox_action(
        self,
        form: dict[str, str],
        *,
        default: str = "/inbox",
        item_id: int | None = None,
    ) -> None:
        target = _safe_return_to(form.get("return_to"), default=default)
        parsed = urlparse(target)
        if self._is_htmx_request() and parsed.path == "/inbox":
            swap_mode = (form.get("swap_mode") or "workspace").strip().lower()
            fragment_mode = swap_mode if swap_mode in {"row", "list"} else None
            self._render_inbox(
                parse_qs(parsed.query),
                response_headers={"HX-Push-Url": target},
                fragment_mode=fragment_mode,
                fragment_item_id=item_id,
            )
            return
        self._redirect(target)

    def _selected_profile(self, requested: int | None) -> sqlite3.Row | None:
        if requested is not None:
            profile = self.db.get_profile_by_id(requested)
            if profile:
                return profile
        return self.db.get_active_profile()

    def _render_no_profiles(self, *, section: str) -> None:
        target = "/inbox" if section == "inbox" else "/filters"
        body = _base_layout(
            "mail-tube",
            f"""
            <section class="panel settings-shell">
              <header class="settings-header">
                <div>
                  <h1 class="settings-title">Mailbox Bootstrap</h1>
                  <p class="settings-subtitle">Create your first profile to start syncing channels.</p>
                </div>
                <img src="/resources/MailTube-logo_v0.png" alt="" aria-hidden="true" width="36" height="36">
              </header>
              <div class="settings-panel">
                <div class="empty-state">
                  No profiles yet. Once a profile is created, MailTube can refresh your inbox stream.
                </div>
                <form method="post" action="/profiles/create" class="row" style="margin-top:12px;">
                  <input type="hidden" name="return_to" value="{target}">
                  <input type="text" name="name" placeholder="Profile name" required>
                  <button type="submit">Create profile</button>
                </form>
              </div>
            </section>
            """,
        )
        self._send_html(body)

    def _render_inbox(
        self,
        query: dict[str, list[str]],
        *,
        response_headers: dict[str, str] | None = None,
        fragment_mode: str | None = None,
        fragment_item_id: int | None = None,
    ) -> None:
        profiles = self.db.list_profiles()
        if not profiles:
            self._render_no_profiles(section="inbox")
            return

        requested_profile = _to_int((query.get("profile") or [None])[0], default=-1)
        profile = self._selected_profile(None if requested_profile < 0 else requested_profile)
        if not profile:
            self._render_no_profiles(section="inbox")
            return

        profile_id = int(profile["id"])
        active_list = (query.get("list") or ["inbox"])[0]
        if active_list not in {"inbox", "watched", "trash", "starred"}:
            active_list = "inbox"
        requested_fragment = fragment_mode
        if requested_fragment is None:
            candidate_fragment = (query.get("fragment") or [None])[0]
            if candidate_fragment in {"row", "list", "viewer"}:
                requested_fragment = candidate_fragment

        starred_only = False
        if active_list == "watched":
            statuses = ("watched",)
        elif active_list == "trash":
            statuses = ("dismissed",)
        elif active_list == "starred":
            statuses = ("new", "watched")
            starred_only = True
        else:
            statuses = ("new",)

        page = max(1, _to_int((query.get("page") or [None])[0], default=1))
        selected_open_id = _to_int((query.get("open") or [None])[0], default=-1)
        selected_item: sqlite3.Row | None = None
        previous_open_id = self._htmx_current_open_id() if requested_fragment == "viewer" else None
        if selected_open_id > 0:
            candidate = self.db.get_inbox_item_with_video(selected_open_id)
            if candidate and int(candidate["profile_id"]) == profile_id:
                self.db.mark_inbox_opened(selected_open_id, mark_watched=False)
                selected_item = self.db.get_inbox_item_with_video(selected_open_id)
            else:
                selected_open_id = -1

        offset = (page - 1) * self.page_size
        total = self.db.count_inbox_items(profile_id, statuses=statuses, starred_only=starred_only)
        items = self.db.list_inbox_items(
            profile_id,
            limit=self.page_size,
            offset=offset,
            statuses=statuses,
            starred_only=starred_only,
            sort_mode=(
                "watched"
                if active_list == "watched"
                else "starred"
                if active_list == "starred"
                else "dismissed"
                if active_list == "trash"
                else "published"
            ),
        )
        latest_run = self.db.latest_refresh_run(profile_id)
        return_to = _inbox_location(profile_id, active_list, page=page, open_item_id=selected_open_id)
        safe_return_to = html.escape(return_to, quote=True)
        profile_switch_return = f"/inbox?{urlencode({'list': active_list, 'page': page})}"
        list_labels = {
            "inbox": "Inbox",
            "watched": "Watched",
            "starred": "Starred",
            "trash": "Trash",
        }

        rows: list[str] = []
        rows_by_id: dict[int, str] = {}
        list_swap_attrs = 'hx-select="#message-list" hx-target="#message-list" hx-swap="outerHTML show:none"'
        for item in items:
            inbox_item_id = int(item["inbox_item_id"])
            title = _safe_display_text(item["title"])
            channel = _safe_display_text(item["channel_title"] or "Unknown channel")
            published = _relative_published_label(item["published_at"])
            status = str(item["status"])
            is_starred = bool(item["is_starred"])
            open_link = _inbox_location(profile_id, active_list, page=page, open_item_id=inbox_item_id)
            open_request_link = _append_query_flag(open_link, "fragment", "viewer")
            item_return = _inbox_location(
                profile_id,
                active_list,
                page=page,
                open_item_id=selected_open_id if selected_open_id > 0 else None,
            )
            actions: list[str] = []
            if status == "new":
                actions.append(
                    f"""
                    <form class="item-action-form" method="post" action="/inbox/item/{inbox_item_id}/watch" {list_swap_attrs}>
                      <input type="hidden" name="return_to" value="{item_return}">
                      <input type="hidden" name="swap_mode" value="list">
                      <button class="icon-button" type="submit" title="Move to watched" aria-label="Move to watched">{EYE_ICON_SVG}</button>
                    </form>
                    """
                )
            else:
                restore_label = "Move to inbox" if status == "watched" else "Restore to inbox"
                actions.append(
                    f"""
                    <form class="item-action-form" method="post" action="/inbox/item/{inbox_item_id}/unwatch" {list_swap_attrs}>
                      <input type="hidden" name="return_to" value="{item_return}">
                      <input type="hidden" name="swap_mode" value="list">
                      <button class="icon-button" type="submit" title="{restore_label}" aria-label="{restore_label}">↺</button>
                    </form>
                    """
                )

            if status in {"new", "watched"}:
                star_action = "unstar" if is_starred else "star"
                star_title = "Remove star" if is_starred else "Save item"
                star_icon = STAR_FILLED_ICON_SVG if is_starred else STAR_ICON_SVG
                starred_class = " starred" if is_starred else ""
                star_swap_mode = "row" if active_list in {"inbox", "watched"} else "list"
                star_swap_attrs = (
                    f'hx-select="#inbox-item-{inbox_item_id}" hx-target="closest .message-row" hx-swap="outerHTML show:none"'
                    if star_swap_mode == "row"
                    else list_swap_attrs
                )
                actions.append(
                    f"""
                    <form class="item-action-form" method="post" action="/inbox/item/{inbox_item_id}/{star_action}" {star_swap_attrs}>
                      <input type="hidden" name="return_to" value="{item_return}">
                      <input type="hidden" name="swap_mode" value="{star_swap_mode}">
                      <button class="icon-button{starred_class}" type="submit" title="{star_title}" aria-label="{star_title}">{star_icon}</button>
                    </form>
                    """
                )

            if status != "dismissed":
                actions.append(
                    f"""
                    <form class="item-action-form" method="post" action="/inbox/item/{inbox_item_id}/trash" {list_swap_attrs}>
                      <input type="hidden" name="return_to" value="{item_return}">
                      <input type="hidden" name="swap_mode" value="list">
                      <button class="icon-button" type="submit" title="Move to trash" aria-label="Move to trash">{TRASH_ICON_SVG}</button>
                    </form>
                    """
                )
            active_class = " active" if inbox_item_id == selected_open_id else ""
            row_markup = (
                f"""
                <article id="inbox-item-{inbox_item_id}" class="message-row{active_class}">
                  <div class="message-main">
                    <a href="{open_link}" hx-get="{open_request_link}" hx-push-url="{open_link}" hx-select="#watch-dock" hx-target="#watch-dock" hx-swap="outerHTML show:none">{title}</a>
                    <div class="message-meta">
                      <span>{channel} | {html.escape(published)}</span>
                    </div>
                  </div>
                  <div class="message-actions">
                    <label class="item-picker" title="Select item">
                      <input class="item-select" type="checkbox" value="{inbox_item_id}" aria-label="Select item {inbox_item_id}">
                    </label>
                    {''.join(actions)}
                  </div>
                </article>
                """
            )
            rows.append(row_markup)
            rows_by_id[inbox_item_id] = row_markup

        if not rows:
            empty_labels = {
                "inbox": "No inbox items yet. Add filters and run refresh.",
                "watched": "No watched items yet.",
                "trash": "Trash is empty.",
                "starred": "No starred items yet.",
            }
            empty_text = html.escape(empty_labels.get(active_list, "No items."))
            rows.append(
                f"""
                <div class="empty-state">{empty_text}</div>
                    """
                )

        message_list_html = f"""
                <div id="message-list" class="message-list">
                  {''.join(rows)}
                </div>
        """

        prev_link = ""
        if page > 1:
            prev_link = f'<a class="drawer-link" href="{_inbox_location(profile_id, active_list, page=page - 1)}">Previous</a>'
        next_link = ""
        if offset + len(items) < total:
            next_link = f'<a class="drawer-link" href="{_inbox_location(profile_id, active_list, page=page + 1)}">Next</a>'

        banners: list[str] = []
        if latest_run and latest_run["status"] != "ok":
            error_text = html.escape(latest_run["error_message"] or "Refresh failed.")
            banners.append(f'<div class="banner"><strong>Refresh issue:</strong> {error_text}</div>')
        import_error = (query.get("import_error") or [None])[0]
        import_notice = (query.get("import_notice") or [None])[0]
        if import_error:
            banners.append('<div class="banner"><strong>Import issue:</strong> Enter a YouTube video link or video ID.</div>')
        elif import_notice == "fallback":
            banners.append(
                '<div class="banner info"><strong>Imported with minimal metadata:</strong> '
                "MailTube used a fallback title because YouTube metadata was unavailable.</div>"
            )
        banner = "".join(banners)

        selected_dock = """
            <div class="watch-dock-empty">
              Select an item to start the player.
            </div>
        """
        dock_class = ""
        if selected_item:
            selected_title = _safe_display_text(selected_item["title"])
            selected_channel = _safe_display_text(selected_item["channel_title"] or "Unknown channel")
            selected_status = str(selected_item["status"])
            embed_url = html.escape(build_embed_url(selected_item["youtube_video_id"], autoplay=True), quote=True)
            close_link = _inbox_location(profile_id, active_list, page=page)
            close_action = f"/inbox/item/{selected_open_id}/close"
            dock_actions: list[str] = []

            if selected_status != "new":
                dock_actions.append(
                    f"""
                    <form method="post" action="/inbox/item/{selected_open_id}/unwatch" {list_swap_attrs}>
                      <input type="hidden" name="return_to" value="{return_to}">
                      <input type="hidden" name="swap_mode" value="list">
                      <button type="submit">Move to inbox</button>
                    </form>
                    """
                )

            if selected_status in {"new", "watched"}:
                selected_is_starred = bool(selected_item["is_starred"])
                star_action = "unstar" if selected_is_starred else "star"
                star_title = "Unstar" if selected_is_starred else "Star"
                star_icon = STAR_FILLED_ICON_SVG if selected_is_starred else STAR_ICON_SVG
                star_class = " starred" if selected_is_starred else ""
                dock_actions.append(
                    f"""
                    <form method="post" action="/inbox/item/{selected_open_id}/{star_action}" {list_swap_attrs}>
                      <input type="hidden" name="return_to" value="{return_to}">
                      <input type="hidden" name="swap_mode" value="list">
                      <button class="icon-button{star_class}" type="submit" title="{star_title}" aria-label="{star_title}">{star_icon}</button>
                    </form>
                    """
                )

            if selected_status != "dismissed":
                dock_actions.append(
                    f"""
                    <form method="post" action="/inbox/item/{selected_open_id}/trash" {list_swap_attrs}>
                      <input type="hidden" name="return_to" value="{return_to}">
                      <input type="hidden" name="swap_mode" value="list">
                      <button class="icon-button" type="submit" title="Move to trash" aria-label="Move to trash">{TRASH_ICON_SVG}</button>
                    </form>
                    """
                )

            selected_dock = f"""
                <div class="watch-dock-head">
                  <h3 class="watch-title">Watch Dock</h3>
                  <form method="post" action="{close_action}" {list_swap_attrs}>
                    <input type="hidden" name="return_to" value="{close_link}">
                    <input type="hidden" name="swap_mode" value="list">
                    <button class="icon-link-button mobile-close" type="submit" title="Close watch dock" aria-label="Close watch dock">{BACK_ICON_SVG}</button>
                  </form>
                </div>
                <p class="watch-item-title">{selected_title}</p>
                <p class="watch-meta">{selected_channel} · {html.escape(selected_status)}</p>
                <iframe
                  src="{embed_url}"
                  sandbox="allow-scripts allow-same-origin allow-presentation allow-forms"
                  allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
                  referrerpolicy="strict-origin-when-cross-origin"
                  allowfullscreen
                  title="YouTube video player"></iframe>
                <div class="watch-actions">
                  <form method="post" action="{close_action}" {list_swap_attrs}>
                    <input type="hidden" name="return_to" value="{close_link}">
                    <input type="hidden" name="swap_mode" value="list">
                    <button class="drawer-link" type="submit" title="Close watch dock" aria-label="Close watch dock">Close</button>
                  </form>
                  {''.join(dock_actions)}
                </div>
            """
            dock_class = " has-item"

        subtitle_html = (
            f'<p id="workspace-subtitle" class="workspace-subtitle">'
            f'profile {html.escape(profile["name"])} · showing {len(items)} of {total}'
            f"</p>"
        )
        pagination_html = f"""
                <div id="pagination" class="pagination">
                  <div>{prev_link}</div>
                  <p>Page {page}</p>
                  <div class="next">{next_link}</div>
                </div>
        """
        watch_dock_html = f"""
              <aside id="watch-dock" class="panel watch-dock{dock_class}" data-region="watch-dock">
                {selected_dock}
              </aside>
        """

        if requested_fragment:
            if requested_fragment == "viewer":
                row_oob_fragments: list[str] = []
                row_ids: list[int] = []
                if selected_open_id > 0:
                    row_ids.append(selected_open_id)
                if previous_open_id and previous_open_id not in row_ids:
                    row_ids.append(previous_open_id)
                for row_id in row_ids:
                    row_markup = rows_by_id.get(row_id)
                    if not row_markup:
                        continue
                    row_oob_fragments.append(
                        row_markup.replace(
                            f'id="inbox-item-{row_id}"',
                            f'id="inbox-item-{row_id}" hx-swap-oob="outerHTML"',
                            1,
                        )
                    )
                self._send_html("\n".join([watch_dock_html, *row_oob_fragments]), headers=response_headers)
                return

            primary_fragment = message_list_html
            if requested_fragment == "row" and fragment_item_id is not None and fragment_item_id in rows_by_id:
                primary_fragment = rows_by_id[fragment_item_id]

            subtitle_oob = subtitle_html.replace(
                'id="workspace-subtitle"',
                'id="workspace-subtitle" hx-swap-oob="outerHTML"',
                1,
            )
            pagination_oob = pagination_html.replace(
                'id="pagination"',
                'id="pagination" hx-swap-oob="outerHTML"',
                1,
            )
            watch_dock_oob = watch_dock_html.replace(
                'id="watch-dock"',
                'id="watch-dock" hx-swap-oob="outerHTML"',
                1,
            )
            self._send_html(
                "\n".join([primary_fragment, subtitle_oob, pagination_oob, watch_dock_oob]),
                headers=response_headers,
            )
            return

        body = _base_layout(
            "mail-tube inbox",
            f"""
            <div id="inbox-workspace" class="workspace" data-return-to="{safe_return_to}" hx-boost="true" hx-target="this" hx-select="#inbox-workspace" hx-swap="outerHTML show:window:top" hx-push-url="true">
              <aside class="panel rail" data-region="workspace-rail">
                <div class="rail-brand">
                  <img src="/resources/MailTube-logo_v0.png" alt="" aria-hidden="true">
                  <span>MailTube</span>
                </div>
                <div class="rail-group">
                  <span class="rail-label">System Lists</span>
                  <a class="rail-link {'active' if active_list == 'inbox' else ''}" href="{_inbox_location(profile_id, 'inbox')}">Inbox</a>
                  <a class="rail-link {'active' if active_list == 'starred' else ''}" href="{_inbox_location(profile_id, 'starred')}">Starred</a>
                  <a class="rail-link {'active' if active_list == 'watched' else ''}" href="{_inbox_location(profile_id, 'watched')}">Watched</a>
                  <a class="rail-link {'active' if active_list == 'trash' else ''}" href="{_inbox_location(profile_id, 'trash')}">Trash</a>
                </div>
                <div class="rail-group">
                  <span class="rail-label">Custom Lists (Soon)</span>
                  <div class="rail-future">Priority Queue</div>
                  <div class="rail-future">Research Sprint</div>
                  <div class="rail-future">Longform Weekend</div>
                </div>
                <div class="rail-group">
                  <span class="rail-label">Workspace Tools</span>
                  <a class="rail-tool-link" hx-boost="false" href="/filters?{urlencode({'profile': profile_id})}">Edit Filters</a>
                  <a class="rail-tool-link" hx-boost="false" href="/profiles?{urlencode({'return_to': profile_switch_return})}">Manage Profiles</a>
                </div>
              </aside>
              <section class="panel workspace-main" data-region="workspace-main">
                <header class="workspace-head">
                  <div>
                    <h1 class="workspace-title">{html.escape(list_labels.get(active_list, "Inbox"))} Stream</h1>
                    {subtitle_html}
                  </div>
                  <div class="head-actions">
                    <details class="import-panel" data-import-panel>
                      <summary class="import-trigger">Import Link</summary>
                      <div class="import-popover">
                        <form class="import-form" method="post" action="/inbox/import">
                          <input type="hidden" name="profile_id" value="{profile_id}">
                          <input type="hidden" name="return_to" value="{safe_return_to}">
                          <input data-import-link type="text" name="youtube_link" placeholder="YouTube link or video ID" required>
                          <div class="import-actions">
                            <button type="submit" name="action" value="watch">Watch Now</button>
                            <button type="submit" name="action" value="inbox">Add to Inbox</button>
                            <button type="submit" name="action" value="starred">Star</button>
                          </div>
                        </form>
                      </div>
                    </details>
                    <form method="post" action="/inbox/refresh">
                      <input type="hidden" name="profile_id" value="{profile_id}">
                      <input type="hidden" name="return_to" value="{return_to}">
                      <button type="submit" title="Refresh now" aria-label="Refresh now">Refresh</button>
                    </form>
                    <button type="button" class="selection-toggle" data-select-toggle aria-pressed="false">Select Mode</button>
                  </div>
                </header>
                <form class="bulk-toolbar" data-bulk-form method="post" action="/inbox/bulk" hx-select="#message-list" hx-target="#message-list" hx-swap="outerHTML show:none">
                  <p class="bulk-count" data-bulk-count>0 selected</p>
                  <input type="hidden" name="item_ids" value="">
                  <input type="hidden" name="return_to" value="{safe_return_to}">
                  <input type="hidden" name="swap_mode" value="list">
                  <div class="bulk-actions">
                    <button type="submit" name="action" value="star" data-bulk-submit disabled>Star</button>
                    <button type="submit" name="action" value="unstar" data-bulk-submit disabled>Unstar</button>
                    <button type="submit" name="action" value="trash" data-bulk-submit disabled>Trash</button>
                    <button type="submit" name="action" value="watch" data-bulk-submit disabled>Watched</button>
                    <button type="submit" name="action" value="inbox" data-bulk-submit disabled>Inbox</button>
                    <button type="button" data-bulk-cancel>Done</button>
                  </div>
                </form>
                {banner}
                {message_list_html}
                {pagination_html}
              </section>
              {watch_dock_html}
            </div>
            """,
        )
        self._send_html(body, headers=response_headers)

    def _render_filters(self, query: dict[str, list[str]]) -> None:
        profiles = self.db.list_profiles()
        if not profiles:
            self._render_no_profiles(section="filters")
            return

        requested_profile = _to_int((query.get("profile") or [None])[0], default=-1)
        profile = self._selected_profile(None if requested_profile < 0 else requested_profile)
        if not profile:
            self._render_no_profiles(section="filters")
            return

        profile_id = int(profile["id"])
        profile_switch_link = f"/profiles?{urlencode({'return_to': '/filters'})}"
        filters = self.db.list_filters(profile_id)
        edit_filter_id = _to_int((query.get("edit") or [None])[0], default=-1)
        edit_filter = next((row for row in filters if int(row["id"]) == edit_filter_id), None)
        rows = []
        for row in filters:
            filter_id = int(row["id"])
            channel_input = html.escape(row["channel_input"])
            channel_title = html.escape(row["channel_title"] or "")
            keyword = html.escape(row["keyword"] or "")
            duration = html.escape(_filter_length_label(row["duration_bucket"], bool(row["exclude_shorts"])))
            since_mode = row["since_mode"] or "anytime"
            since_label = "from now" if since_mode == "from_now" else "anytime"
            validity = "ok" if row["is_valid"] else "invalid"
            edit_link = f"/filters?{urlencode({'profile': profile_id, 'edit': filter_id})}"
            editing_class = ' class="filter-row-editing"' if edit_filter and filter_id == int(edit_filter["id"]) else ""
            rows.append(
                f"""
                <tr{editing_class}>
                  <td>{channel_input}</td>
                  <td>{channel_title}</td>
                  <td>{keyword}</td>
                  <td>{duration}</td>
                  <td>{html.escape(since_label)}</td>
                  <td>{validity}</td>
                  <td>
                    <div class="filter-actions">
                      <a class="icon-link-button" href="{edit_link}" title="Edit filter" aria-label="Edit filter">{PEN_ICON_SVG}</a>
                      <form method="post" action="/filters/delete">
                        <input type="hidden" name="profile_id" value="{profile_id}">
                        <input type="hidden" name="filter_id" value="{filter_id}">
                        <button class="icon-button" type="submit" title="Delete filter" aria-label="Delete filter">{TRASH_ICON_SVG}</button>
                      </form>
                    </div>
                  </td>
                </tr>
                """
            )

        if not rows:
            rows.append('<tr><td colspan="7" class="muted">No filters yet.</td></tr>')

        back_link = _inbox_location(profile_id, "inbox")
        form_title = "Editing Filter" if edit_filter else "Add Filter"
        form_action = "/filters/update" if edit_filter else "/filters/add"
        form_channel = html.escape(edit_filter["channel_input"] if edit_filter else "", quote=True)
        form_keyword = html.escape((edit_filter["keyword"] or "") if edit_filter else "", quote=True)
        form_length_mode = (
            _filter_length_mode_value(edit_filter["duration_bucket"], bool(edit_filter["exclude_shorts"]))
            if edit_filter
            else "no_shorts"
        )
        form_since_mode = (edit_filter["since_mode"] or "anytime") if edit_filter else "from_now"
        filter_id_input = (
            f'<input type="hidden" name="filter_id" value="{int(edit_filter["id"])}">' if edit_filter else ""
        )
        cancel_edit_link = (
            f'<a class="drawer-link" href="/filters?{urlencode({"profile": profile_id})}">Cancel</a>' if edit_filter else ""
        )
        submit_label = "Save" if edit_filter else "Add"
        edit_context = ""
        if edit_filter:
            editing_channel = _safe_display_text(edit_filter["channel_input"])
            resolved_channel = _safe_display_text(edit_filter["channel_title"] or edit_filter["channel_id"] or "unresolved")
            edit_context = (
                f'<div class="edit-context"><strong>Editing:</strong> {editing_channel}'
                f' <span class="muted">| resolved: {resolved_channel}</span></div>'
            )

        body = _base_layout(
            "mail-tube filters",
            f"""
            <section class="settings-shell">
              <header class="panel settings-header">
                <div>
                  <h1 class="settings-title">Channel Filters</h1>
                  <p class="settings-subtitle">Profile: {html.escape(profile["name"])}</p>
                </div>
                <div class="row">
                  <a class="icon-link-button" href="{back_link}" title="Back to inbox" aria-label="Back to inbox">{BACK_ICON_SVG}</a>
                  <a class="drawer-link" href="{profile_switch_link}">Switch profile</a>
                </div>
              </header>
              <section class="panel settings-panel">
                <h2 class="drawer-title">{form_title}</h2>
                {edit_context}
                <form method="post" action="{form_action}" class="row">
                  <input type="hidden" name="profile_id" value="{profile_id}">
                  {filter_id_input}
                  <input type="text" name="channel_input" placeholder="Channel URL or @handle" value="{form_channel}" required>
                  <input type="text" name="keyword" placeholder="Optional keyword" value="{form_keyword}">
                  <select name="length_mode">
                    <option value="no_shorts"{_selected_attr("no_shorts", form_length_mode)}>No Shorts (&gt;3m)</option>
                    <option value="any"{_selected_attr("any", form_length_mode)}>Any length</option>
                    <option value="short"{_selected_attr("short", form_length_mode)}>Short regular (3-5m)</option>
                    <option value="medium"{_selected_attr("medium", form_length_mode)}>Medium (5-20m)</option>
                    <option value="long"{_selected_attr("long", form_length_mode)}>Long (&gt;20m)</option>
                  </select>
                  <select name="since_mode">
                    <option value="from_now"{_selected_attr("from_now", form_since_mode)}>From now</option>
                    <option value="anytime"{_selected_attr("anytime", form_since_mode)}>Anytime</option>
                  </select>
                  <button type="submit">{submit_label}</button>
                  {cancel_edit_link}
                </form>
              </section>
              <section class="panel settings-panel">
                <h2 class="drawer-title">Current Filters</h2>
                <table>
                  <thead>
                    <tr>
                      <th>Channel Input</th>
                      <th>Resolved Channel</th>
                      <th>Keyword</th>
                      <th>Length</th>
                      <th>Time</th>
                      <th>State</th>
                      <th>Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {''.join(rows)}
                  </tbody>
                </table>
              </section>
            </section>
            """,
        )
        self._send_html(body)

    def _render_profiles(self, query: dict[str, list[str]]) -> None:
        profiles = self.db.list_profiles()
        active_profile = self.db.get_active_profile()
        active_name = active_profile["name"] if active_profile else None
        return_to = _safe_return_to((query.get("return_to") or [None])[0], default="/inbox")

        rows = []
        for row in profiles:
            profile_id = int(row["id"])
            profile_name = _safe_display_text(row["name"])
            if int(row["is_active"]) == 1:
                action_html = '<span class="muted">Active</span>'
            else:
                action_html = f"""
                    <form method="post" action="/profiles/set-active">
                      <input type="hidden" name="profile_id" value="{profile_id}">
                      <input type="hidden" name="return_to" value="{html.escape(return_to, quote=True)}">
                      <button type="submit">Switch</button>
                    </form>
                """
            rows.append(
                f"""
                <tr>
                  <td>{profile_name}</td>
                  <td>{action_html}</td>
                </tr>
                """
            )
        if not rows:
            rows.append('<tr><td colspan="2" class="muted">No profiles yet.</td></tr>')

        body = _base_layout(
            "mail-tube profiles",
            f"""
            <section class="settings-shell">
              <header class="panel settings-header">
                <div>
                  <h1 class="settings-title">Profiles</h1>
                  <p class="settings-subtitle">Active: {html.escape(active_name or "None")}</p>
                </div>
                <a class="icon-link-button" href="{html.escape(return_to, quote=True)}" title="Back" aria-label="Back">{BACK_ICON_SVG}</a>
              </header>
              <section class="panel settings-panel">
                <h2 class="drawer-title">Switch Profile</h2>
                <table>
                  <thead>
                    <tr>
                      <th>Profile</th>
                      <th>Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {''.join(rows)}
                  </tbody>
                </table>
              </section>
              <section class="panel settings-panel">
                <h2 class="drawer-title">Create Profile</h2>
                <form method="post" action="/profiles/create" class="row">
                  <input type="hidden" name="return_to" value="{html.escape(return_to, quote=True)}">
                  <input type="text" name="name" placeholder="Profile name" required>
                  <button type="submit">Create</button>
                </form>
              </section>
            </section>
            """,
        )
        self._send_html(body)

    def _render_watch(self, parsed: ParseResult) -> None:
        path = parsed.path
        query = parse_qs(parsed.query)
        parts = [part for part in path.split("/") if part]
        if len(parts) != 2:
            self.send_error(404, "Not Found")
            return
        inbox_item_id = _to_int(parts[1], default=-1)
        if inbox_item_id <= 0:
            self.send_error(404, "Not Found")
            return

        item = self.db.get_inbox_item_with_video(inbox_item_id)
        if not item:
            self.send_error(404, "Not Found")
            return

        profile_id = int(item["profile_id"])
        active_list = (query.get("list") or ["inbox"])[0]
        if active_list not in {"inbox", "watched", "trash", "starred"}:
            active_list = "inbox"
        page = max(1, _to_int((query.get("page") or [None])[0], default=1))
        self._redirect(_inbox_location(profile_id, active_list, page=page, open_item_id=inbox_item_id))

    def _post_refresh(self, form: dict[str, str]) -> None:
        profile_id = _to_int(form.get("profile_id"), default=-1)
        if profile_id > 0:
            refresh_profile(self.db, profile_id, api_key=get_youtube_api_key())
            self._respond_inbox_action(form, default=f"/inbox?{urlencode({'profile': profile_id})}")
            return
        self._respond_inbox_action(form, default="/inbox")

    def _post_import(self, form: dict[str, str]) -> None:
        profile_id = _to_int(form.get("profile_id"), default=-1)
        return_to = _without_query_keys(
            _safe_return_to(form.get("return_to"), default=f"/inbox?{urlencode({'profile': profile_id})}"),
            {"import_error", "import_notice"},
        )
        if profile_id <= 0:
            self._redirect(_append_query_flag(return_to, "import_error", "1"))
            return

        try:
            result = import_video_link(
                self.db,
                profile_id,
                form.get("youtube_link") or "",
                action=form.get("action") or "",
                api_key=get_youtube_api_key(),
            )
        except ValueError:
            self._redirect(_append_query_flag(return_to, "import_error", "1"))
            return

        parsed_return = urlparse(return_to)
        return_query = parse_qs(parsed_return.query)
        active_list = (return_query.get("list") or ["inbox"])[0]
        if active_list not in {"inbox", "watched", "trash", "starred"}:
            active_list = "inbox"
        page = max(1, _to_int((return_query.get("page") or [None])[0], default=1))

        if result.action == "inbox":
            target = _inbox_location(profile_id, "inbox")
        elif result.action == "starred":
            target = _inbox_location(profile_id, "starred")
        else:
            target = _inbox_location(profile_id, active_list, page=page, open_item_id=result.inbox_item_id)

        if result.used_fallback_metadata:
            target = _append_query_flag(target, "import_notice", "fallback")
        self._redirect(target)

    def _post_mark_watch(self, path: str, form: dict[str, str], *, watched: bool) -> None:
        parts = [part for part in path.split("/") if part]
        if len(parts) != 4:
            self.send_error(404, "Not Found")
            return
        inbox_item_id = _to_int(parts[2], default=-1)
        if inbox_item_id <= 0:
            self.send_error(404, "Not Found")
            return
        self.db.mark_inbox_watched(inbox_item_id, watched=watched)
        self._respond_inbox_action(form, default="/inbox", item_id=inbox_item_id)

    def _post_mark_trash(self, path: str, form: dict[str, str]) -> None:
        parts = [part for part in path.split("/") if part]
        if len(parts) != 4:
            self.send_error(404, "Not Found")
            return
        inbox_item_id = _to_int(parts[2], default=-1)
        if inbox_item_id <= 0:
            self.send_error(404, "Not Found")
            return
        self.db.mark_inbox_trashed(inbox_item_id)
        next_form = dict(form)
        next_form["return_to"] = _drop_open_from_return_to_if_matches(form.get("return_to"), inbox_item_id=inbox_item_id) or "/inbox"
        self._respond_inbox_action(next_form, default="/inbox", item_id=inbox_item_id)

    def _post_mark_star(self, path: str, form: dict[str, str], *, starred: bool) -> None:
        parts = [part for part in path.split("/") if part]
        if len(parts) != 4:
            self.send_error(404, "Not Found")
            return
        inbox_item_id = _to_int(parts[2], default=-1)
        if inbox_item_id <= 0:
            self.send_error(404, "Not Found")
            return
        self.db.mark_inbox_starred(inbox_item_id, starred=starred)
        self._respond_inbox_action(form, default="/inbox", item_id=inbox_item_id)

    def _post_close_viewer(self, path: str, form: dict[str, str]) -> None:
        parts = [part for part in path.split("/") if part]
        if len(parts) != 4:
            self.send_error(404, "Not Found")
            return
        inbox_item_id = _to_int(parts[2], default=-1)
        if inbox_item_id <= 0:
            self.send_error(404, "Not Found")
            return
        self.db.mark_inbox_watched(inbox_item_id, watched=True)
        self._respond_inbox_action(form, default="/inbox", item_id=inbox_item_id)

    def _post_bulk_action(self, form: dict[str, str]) -> None:
        action = (form.get("action") or "").strip().lower()
        raw_ids = form.get("item_ids") or ""
        item_ids: list[int] = []
        for part in raw_ids.split(","):
            item_id = _to_int(part.strip(), default=-1)
            if item_id > 0 and item_id not in item_ids:
                item_ids.append(item_id)

        if action and item_ids:
            for item_id in item_ids:
                if action == "trash":
                    self.db.mark_inbox_trashed(item_id)
                elif action == "star":
                    self.db.mark_inbox_starred(item_id, starred=True)
                elif action == "unstar":
                    self.db.mark_inbox_starred(item_id, starred=False)
                elif action == "watch":
                    self.db.mark_inbox_watched(item_id, watched=True)
                elif action == "inbox":
                    self.db.mark_inbox_watched(item_id, watched=False)

        next_form = dict(form)
        return_to = form.get("return_to")
        if action == "trash" and return_to:
            for item_id in item_ids:
                return_to = _drop_open_from_return_to_if_matches(return_to, inbox_item_id=item_id)
            if return_to:
                next_form["return_to"] = return_to
        self._respond_inbox_action(next_form, default="/inbox")

    def _post_add_filter(self, form: dict[str, str]) -> None:
        profile_id = _to_int(form.get("profile_id"), default=-1)
        channel_input = (form.get("channel_input") or "").strip()
        keyword = (form.get("keyword") or "").strip() or None
        duration_bucket, exclude_shorts = _parse_length_mode(form.get("length_mode") or form.get("duration_bucket"))
        since_mode = (form.get("since_mode") or "anytime").strip().lower()
        if profile_id <= 0:
            self._redirect("/filters")
            return
        if channel_input:
            try:
                self.db.add_filter(
                    profile_id,
                    channel_input,
                    keyword,
                    duration_bucket=duration_bucket,
                    since_mode=since_mode,
                    exclude_shorts=exclude_shorts,
                )
            except ValueError:
                pass
        self._redirect(f"/filters?{urlencode({'profile': profile_id})}")

    def _post_update_filter(self, form: dict[str, str]) -> None:
        profile_id = _to_int(form.get("profile_id"), default=-1)
        filter_id = _to_int(form.get("filter_id"), default=-1)
        channel_input = (form.get("channel_input") or "").strip()
        keyword = (form.get("keyword") or "").strip() or None
        duration_bucket, exclude_shorts = _parse_length_mode(form.get("length_mode") or form.get("duration_bucket"))
        since_mode = (form.get("since_mode") or "anytime").strip().lower()
        if profile_id <= 0:
            self._redirect("/filters")
            return
        if filter_id > 0 and channel_input:
            try:
                self.db.update_filter(
                    profile_id,
                    filter_id,
                    channel_input,
                    keyword,
                    duration_bucket=duration_bucket,
                    since_mode=since_mode,
                    exclude_shorts=exclude_shorts,
                )
            except ValueError:
                pass
        self._redirect(f"/filters?{urlencode({'profile': profile_id})}")

    def _post_delete_filter(self, form: dict[str, str]) -> None:
        profile_id = _to_int(form.get("profile_id"), default=-1)
        filter_id = _to_int(form.get("filter_id"), default=-1)
        if profile_id > 0 and filter_id > 0:
            self.db.remove_filter(profile_id, filter_id)
        self._redirect(f"/filters?{urlencode({'profile': profile_id})}")

    def _post_create_profile(self, form: dict[str, str]) -> None:
        name = (form.get("name") or "").strip()
        return_to = _safe_return_to(form.get("return_to"), default="/inbox")
        if not name:
            self._redirect(return_to)
            return
        try:
            profile_id = self.db.create_profile(name)
        except (ValueError, sqlite3.IntegrityError):
            self._redirect(return_to)
            return
        self.db.set_active_profile(profile_id)
        self._redirect(return_to)

    def _post_set_active(self, form: dict[str, str]) -> None:
        profile_id = _to_int(form.get("profile_id"), default=-1)
        return_to = _safe_return_to(form.get("return_to"), default="/inbox")
        if profile_id > 0:
            try:
                self.db.set_active_profile(profile_id)
            except ValueError:
                pass
        self._redirect(return_to)


def run_server(
    db: Database,
    *,
    host: str,
    port: int,
    startup_refresh: bool,
    page_size: int = 20,
) -> None:
    if startup_refresh:
        refresh_all_profiles(db, api_key=get_youtube_api_key())

    class BoundHandler(MailTubeHandler):
        pass

    BoundHandler.db = db
    BoundHandler.page_size = page_size

    server = ThreadingHTTPServer((host, port), BoundHandler)
    print(f"Serving mail-tube at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
