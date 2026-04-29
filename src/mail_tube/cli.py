from __future__ import annotations

import argparse
import html
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .db import Database
from .config import get_youtube_api_key, load_local_env
from .refresh import refresh_profile
from .web import run_server
from .youtube import build_embed_url, extract_video_id


def create_single_video_html(video_id: str) -> str:
    embed_url = build_embed_url(video_id, autoplay=True)
    escaped_url = html.escape(embed_url, quote=True)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>mail-tube</title>
  <style>
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: #0f1115;
      color: #f3f4f6;
      font-family: Arial, sans-serif;
    }}
    main {{
      width: min(96vw, 1200px);
      display: grid;
      gap: 12px;
    }}
    iframe {{
      width: 100%;
      aspect-ratio: 16 / 9;
      border: 0;
      border-radius: 12px;
      box-shadow: 0 12px 40px rgba(0, 0, 0, 0.35);
    }}
  </style>
</head>
<body>
  <main>
    <h1>mail-tube</h1>
    <iframe
      src="{escaped_url}"
      allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
      referrerpolicy="strict-origin-when-cross-origin"
      allowfullscreen
      title="YouTube video player"></iframe>
  </main>
</body>
</html>"""


def parse_length_mode(value: str | None) -> tuple[str | None, bool]:
    mode = (value or "no-shorts").strip().lower().replace("_", "-")
    if mode == "any":
        return None, False
    if mode in {"short", "medium", "long"}:
        return mode, True
    return None, True


def filter_length_label(duration_bucket: str | None, exclude_shorts: bool) -> str:
    if duration_bucket == "short":
        return "short regular (3-5m)" if exclude_shorts else "short (<5m)"
    if duration_bucket == "medium":
        return "medium (5-20m)"
    if duration_bucket == "long":
        return "long (>20m)"
    return "no shorts (>3m)" if exclude_shorts else "any"


def make_single_video_handler(page: str) -> type[BaseHTTPRequestHandler]:
    class VideoHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path not in {"/", "/index.html"}:
                self.send_error(404, "Not Found")
                return

            encoded = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return VideoHandler


def _snapshot_source_tree(paths: list[Path]) -> dict[str, int]:
    state: dict[str, int] = {}
    for base in paths:
        if not base.exists():
            continue
        for entry in base.rglob("*"):
            if not entry.is_file():
                continue
            if "__pycache__" in entry.parts:
                continue
            if entry.suffix in {".pyc", ".pyo"}:
                continue
            try:
                state[str(entry)] = entry.stat().st_mtime_ns
            except OSError:
                continue
    return state


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def run_dev_server(args: argparse.Namespace) -> None:
    watch_paths = [Path("src")]
    command = [
        sys.executable,
        "-m",
        "mail_tube.cli",
        "--db",
        args.db,
        "start",
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    if not args.startup_refresh:
        command.append("--no-startup-refresh")

    print("Starting dev server with auto-reload...")
    process: subprocess.Popen[bytes] = subprocess.Popen(command)
    snapshot = _snapshot_source_tree(watch_paths)

    try:
        while True:
            time.sleep(args.reload_interval)
            current = _snapshot_source_tree(watch_paths)
            if current != snapshot:
                snapshot = current
                print("Source change detected. Reloading...")
                _terminate_process(process)
                process = subprocess.Popen(command)
                continue
            if process.poll() is not None:
                print("Server exited. Restarting...")
                process = subprocess.Popen(command)
    except KeyboardInterrupt:
        pass
    finally:
        _terminate_process(process)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mail-tube")
    parser.add_argument(
        "--db",
        default="mail_tube.db",
        help="Path to local SQLite database (default: ./mail_tube.db)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="Start the inbox web application.")
    start.add_argument("--host", default="127.0.0.1", help="Host to bind the local server.")
    start.add_argument("--port", default=8000, type=int, help="Port to bind the local server.")
    start.add_argument(
        "--no-startup-refresh",
        action="store_true",
        help="Skip inbox refresh on startup.",
    )

    dev = subparsers.add_parser("dev", help="Start web app with automatic reload on source changes.")
    dev.add_argument("--host", default="127.0.0.1", help="Host to bind the local server.")
    dev.add_argument("--port", default=8000, type=int, help="Port to bind the local server.")
    dev.add_argument(
        "--startup-refresh",
        action="store_true",
        help="Run startup refresh on each reload cycle.",
    )
    dev.add_argument(
        "--reload-interval",
        default=0.8,
        type=float,
        help="Seconds between source checks (default: 0.8).",
    )

    watch = subparsers.add_parser("watch", help="Watch a YouTube video in a local web app.")
    watch.add_argument("youtube_link", help="YouTube URL or video id.")
    watch.add_argument("--host", default="127.0.0.1", help="Host to bind the local server.")
    watch.add_argument("--port", default=8000, type=int, help="Port to bind the local server.")

    profile = subparsers.add_parser("profile", help="Manage inbox profiles.")
    profile_subcommands = profile.add_subparsers(dest="profile_command", required=True)

    profile_subcommands.add_parser("list", help="List profiles.")

    profile_create = profile_subcommands.add_parser("create", help="Create a profile.")
    profile_create.add_argument("name", help="Profile name.")

    profile_set_active = profile_subcommands.add_parser("set-active", help="Set active profile.")
    profile_set_active.add_argument("name", help="Profile name.")

    profile_delete = profile_subcommands.add_parser("delete", help="Delete a profile.")
    profile_delete.add_argument("name", help="Profile name.")

    profile_refresh = profile_subcommands.add_parser("refresh", help="Refresh inbox for a profile.")
    profile_refresh.add_argument("--name", help="Profile name. Defaults to active profile.")

    filter_parser = subparsers.add_parser("filter", help="Manage channel+keyword+length filter rows.")
    filter_subcommands = filter_parser.add_subparsers(dest="filter_command", required=True)

    filter_add = filter_subcommands.add_parser("add", help="Add a filter row to a profile.")
    filter_add.add_argument("--profile", required=True, help="Profile name.")
    filter_add.add_argument("--channel", required=True, help="Channel URL, ID, or @handle.")
    filter_add.add_argument("--keyword", default="", help="Optional title keyword.")
    filter_add.add_argument(
        "--duration",
        choices=("no-shorts", "any", "short", "medium", "long"),
        default="no-shorts",
        help="Length mode (default: no-shorts; short means 3-5m).",
    )
    filter_add.add_argument(
        "--since",
        choices=("anytime", "from_now", "from-now"),
        default="anytime",
        help="Optional publish-time mode: anytime or from_now.",
    )

    filter_list = filter_subcommands.add_parser("list", help="List filter rows for a profile.")
    filter_list.add_argument("--profile", required=True, help="Profile name.")

    filter_remove = filter_subcommands.add_parser("remove", help="Remove a filter row by ID.")
    filter_remove.add_argument("--profile", required=True, help="Profile name.")
    filter_remove.add_argument("--filter-id", required=True, type=int, help="Filter row ID.")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    load_local_env()

    if args.command == "dev":
        run_dev_server(args)
        return

    if args.command == "start":
        db = Database(args.db)
        db.init()
        run_server(
            db,
            host=args.host,
            port=args.port,
            startup_refresh=not args.no_startup_refresh,
            page_size=20,
        )
        return

    if args.command == "watch":
        video_id = extract_video_id(args.youtube_link)
        if not video_id:
            parser.error("Could not parse a valid YouTube video ID from the provided link.")

        page = create_single_video_html(video_id)
        handler = make_single_video_handler(page)
        server = ThreadingHTTPServer((args.host, args.port), handler)
        print(f"Serving video {video_id} at http://{args.host}:{args.port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return

    db = Database(args.db)
    db.init()

    def get_profile_or_error(name: str) -> sqlite3.Row:
        profile = db.get_profile_by_name(name)
        if not profile:
            parser.error(f"Profile '{name}' does not exist.")
        return profile

    if args.command == "profile":
        if args.profile_command == "list":
            profiles = db.list_profiles()
            if not profiles:
                print("No profiles configured.")
                return
            for profile in profiles:
                marker = "*" if profile["is_active"] else " "
                print(f"{marker} {profile['name']} (id={profile['id']})")
            return

        if args.profile_command == "create":
            try:
                profile_id = db.create_profile(args.name)
            except sqlite3.IntegrityError:
                parser.error(f"Profile '{args.name}' already exists.")
            print(f"Created profile '{args.name}' (id={profile_id}).")
            return

        if args.profile_command == "set-active":
            profile = get_profile_or_error(args.name)
            db.set_active_profile(int(profile["id"]))
            print(f"Active profile set to '{profile['name']}'.")
            return

        if args.profile_command == "delete":
            profile = get_profile_or_error(args.name)
            db.delete_profile(int(profile["id"]))
            print(f"Deleted profile '{profile['name']}'.")
            return

        if args.profile_command == "refresh":
            if args.name:
                profile = get_profile_or_error(args.name)
            else:
                profile = db.get_active_profile()
                if not profile:
                    parser.error("No profiles configured.")
            outcome = refresh_profile(
                db,
                int(profile["id"]),
                api_key=get_youtube_api_key(),
            )
            print(
                f"Refresh {outcome.status}: fetched={outcome.fetched_count} "
                f"matched={outcome.matched_count} added={outcome.added_count}"
            )
            if outcome.error_message:
                print(outcome.error_message)
            return

    if args.command == "filter":
        if args.filter_command == "add":
            profile = get_profile_or_error(args.profile)
            duration_bucket, exclude_shorts = parse_length_mode(args.duration)
            filter_id = db.add_filter(
                int(profile["id"]),
                args.channel,
                args.keyword,
                duration_bucket=duration_bucket,
                since_mode=args.since,
                exclude_shorts=exclude_shorts,
            )
            print(f"Added filter row {filter_id} to '{profile['name']}'.")
            return

        if args.filter_command == "list":
            profile = get_profile_or_error(args.profile)
            filters = db.list_filters(int(profile["id"]))
            if not filters:
                print(f"No filters for '{profile['name']}'.")
                return
            print(f"Filters for '{profile['name']}':")
            for row in filters:
                keyword = row["keyword"] or "-"
                duration = filter_length_label(row["duration_bucket"], bool(row["exclude_shorts"]))
                since_mode = "from now" if row["since_mode"] == "from_now" else "anytime"
                resolved = row["channel_title"] or row["channel_id"] or "(unresolved)"
                valid = "valid" if row["is_valid"] else "invalid"
                print(
                    f"id={row['id']} channel={row['channel_input']} "
                    f"resolved={resolved} keyword={keyword} duration={duration} since={since_mode} state={valid}"
                )
            return

        if args.filter_command == "remove":
            profile = get_profile_or_error(args.profile)
            db.remove_filter(int(profile["id"]), args.filter_id)
            print(f"Removed filter row {args.filter_id} from '{profile['name']}'.")
            return


if __name__ == "__main__":
    main()
