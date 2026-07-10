#!/usr/bin/env python3
"""
YouTube Music Downloader — Production Edition

Downloads songs and playlists from YouTube Music at maximum audio quality,
organized into Year/Genre folder structure.

Key design decisions:
  - yt-dlp extracts metadata (title, tags, upload_date) for FREE — no API calls
  - YouTube Data API is used ONLY as a fallback when yt-dlp data is insufficient
  - This keeps API quota usage extremely low (~1 unit per 50 videos in batch)

Usage:
    python main.py <url_or_video_id>          # Download a single track
    python main.py --playlist <playlist_url>  # Download an entire playlist
    python main.py --search "artist song"     # Search and pick from results
"""

import argparse
import asyncio
import datetime
import logging
import os
import re
import sys
import traceback
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

# Use ASCII color system on Windows to avoid cp1252 encoding errors with emojis
if sys.platform == "win32":
    console = Console(force_terminal=True, color_system="standard")
else:
    console = Console()
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
)
from rich.table import Table

from api_client import YouTubeAPIClient
from config import Config
from downloader import AudioDownloader, DownloadQueueItem, DownloadResult
from organizer import organize_batch, print_organization_summary
from utils import (
    count_files_recursive,
    extract_playlist_id,
    extract_video_id,
    get_directory_size,
    is_playlist_url,
)

console = Console()
logger = logging.getLogger(__name__)


def _error_log_path() -> str:
    """Return the path to the current error log file."""
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), f"error_{ts}.log")


def _write_error_log(exc: BaseException) -> None:
    """Write full traceback of *exc* to a dated error log file."""
    path = _error_log_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"Error logged at {datetime.datetime.now().isoformat()}\n")
            f.write("=" * 72 + "\n\n")
            f.writelines(traceback.format_exception(type(exc), exc, exc.__traceback__))
        logger.info("Full error details written to %s", path)
    except OSError:
        # If we can't write the file, fall back to stderr
        traceback.print_exc()


# ─── CLI Argument Parsing ──────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yt-music-downloader",
        description="Download YouTube Music songs & playlists at max quality.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s https://music.youtube.com/watch?v=dQw4w9WgXcQ
  %(prog)s --playlist PLxxxxxxxxxxxxxxx
  %(prog)s --search "Daft Punk Get Lucky"
  %(prog)s dQw4w9WgXcQ

Metadata is extracted from yt-dlp (FREE). YouTube API is used only as fallback.
        """,
    )

    parser.add_argument(
        "input", nargs="?", help="YouTube Music URL, video ID, or playlist link"
    )
    parser.add_argument(
        "--playlist", "-p", help="Download all tracks from a YouTube playlist"
    )
    parser.add_argument(
        "--search", "-s", help='Search for a song and pick from results'
    )
    parser.add_argument(
        "--output-dir", "-o", type=Path, help="Output directory (default: ./downloads)"
    )
    parser.add_argument(
        "--codec",
        choices=["mp3", "flac", "opus"],
        default=None,
        help="Audio codec for output (default: mp3, 320kbps)",
    )
    parser.add_argument(
        "--max-results", "-n", type=int, default=5, help="Max search results (default: 5)"
    )
    parser.add_argument(
        "--concurrent", "-c", type=int, default=None, help="Max concurrent downloads"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be downloaded without downloading"
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true", help="Suppress progress output"
    )
    parser.add_argument(
        "--resolve-tags", "-r", action="store_true",
        help="Resolve and update tags with fresh metadata from YouTube Music (default: on)",
    )
    parser.add_argument(
        "--no-resolve", action="store_true",
        help="Disable automatic tag resolution after download",
    )
    parser.add_argument(
        "--resolve-all", action="store_true",
        help="Resolve tags for all previously downloaded songs in the output directory",
    )

    return parser


# ─── Metadata Extraction (Primary: yt-dlp, Fallback: YouTube API) ─────

def extract_metadata_primary(video_id: str, downloader: AudioDownloader) -> dict | None:
    """
    PRIMARY metadata source: YouTube Music API (ytmusicapi).
    Free — no API quota needed. Provides title, artist, duration.
    """
    return downloader.extract_metadata_from_ytmusic(video_id)


def extract_metadata_fallback(
    video_id: str, api_client: YouTubeAPIClient
) -> dict | None:
    """
    FALLBACK metadata source: YouTube Data API v3.
    Used only when ytmusicapi extraction fails or is incomplete.
    Costs 1 unit per batch of up to 50 videos.
    """
    metadata = api_client.get_video_metadata([video_id])
    return metadata.get(video_id) if metadata else None


def merge_metadata(
    primary: dict | None, fallback: dict | None
) -> dict[str, Any]:
    """Merge yt-dlp (primary) and API (fallback) metadata. Primary takes precedence."""

    result = {}

    # Title — prefer yt-dlp (usually cleaner for YouTube Music)
    if primary and primary.get("title"):
        result["title"] = primary["title"]
    elif fallback and fallback.get("title"):
        result["title"] = fallback["title"]

    # Artist/Channel — prefer uploader from yt-dlp
    if primary and primary.get("uploader"):
        result["channel"] = primary["uploader"]
    elif fallback and fallback.get("channel"):
        result["channel"] = fallback["channel"]

    # Description (for album extraction)
    if primary and primary.get("description"):
        result["description"] = primary["description"]
    elif fallback and fallback.get("description"):
        result["description"] = fallback["description"]

    # Year — prefer upload_date from yt-dlp (YYYYMMDD), then publishedAt from API
    year = 0
    if primary:
        upload_date = primary.get("upload_date", "")
        if upload_date and len(upload_date) == 8:
            try:
                year = int(upload_date[:4])
            except ValueError:
                pass

    if year == 0 and fallback:
        published_at = fallback.get("published_at", "")
        if published_at:
            try:
                year = int(published_at[:4])
            except (ValueError, IndexError):
                pass

    result["year"] = year

    # Genre — infer from tags (yt-dlp) or channel name (API)
    genre = "Unknown"
    if primary and primary.get("tags"):
        genre = YouTubeAPIClient.infer_genre_from_tags(
            tags=primary["tags"],
            channel_name=result.get("channel", ""),
        )

    result["genre"] = genre

    # Duration
    duration = 0
    if primary:
        duration = int(primary.get("duration", 0))
    elif fallback:
        iso_duration = fallback.get("duration_iso", "")
        if iso_duration:
            import re as _re
            match = _re.search(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', iso_duration)
            if match:
                h, m, s = match.groups() or (0, 0, 0)
                duration = int(h or 0) * 3600 + int(m or 0) * 60 + int(s or 0)

    result["duration"] = duration

    return result


def extract_album_from_description(description: str | None) -> str:
    """Try to extract album name from video description."""
    if not description:
        return ""

    # Common patterns in YouTube Music descriptions
    patterns = [
        r'(?:from\s+(?:the\s+)?album[:\s]+)([^\n]{3,60})',
        r'(?:album[:\s]+)([^\n]{3,60})',
        r'(?:album\s*:\s*)([^\n]{3,60})',
    ]

    for pattern in patterns:
        match = re.search(pattern, description, re.IGNORECASE)
        if match:
            album = match.group(1).strip()
            # Filter out false positives
            skip_words = {"official", "video", "lyrics", "audio", "music"}
            words = album.lower().split()
            if not any(w in skip_words for w in words) and len(words) <= 8:
                return album

    return ""


# ─── Download Logic ────────────────────────────────────────────────────

async def download_single_track(
    config: Config,
    api_client: YouTubeAPIClient,
    downloader: AudioDownloader,
    video_id: str,
) -> None:
    """Download a single track with metadata enrichment."""

    print(f"\nDownloading: {video_id}")

    # Step 1: Extract metadata from YouTube Music API (FREE — no API quota)
    primary_meta = extract_metadata_primary(video_id, downloader)

    if not primary_meta or not primary_meta.get("title"):
        print("(info) YouTube Music API metadata incomplete. Falling back to YouTube Data API...")
        fallback_meta = extract_metadata_fallback(video_id, api_client)
        meta = merge_metadata(None, fallback_meta)
    else:
        # Step 2: Merge with API data only if needed (fallback)
        fallback_meta = None
        if not primary_meta.get("uploader"):
            print("  Missing artist info. Querying YouTube API...")
            fallback_meta = extract_metadata_fallback(video_id, api_client)

        meta = merge_metadata(primary_meta, fallback_meta)

    title = meta.get("title", "Unknown Title")
    channel = meta.get("channel", config.default_artist)
    year = meta.get("year", 0)
    genre = meta.get("genre", "Unknown")
    duration = meta.get("duration", 0)
    description = meta.get("description", "")

    # Try to extract album from description
    album = extract_album_from_description(description)

    print(f"  Title:     {title}")
    print(f"  Artist:   {channel}")
    if year:
        print(f"  Year:     {year}")
    print(f"  Genre:    {genre}")
    if duration:
        mins, secs = divmod(duration, 60)
        print(f"  Duration: {mins}:{secs:02d}")

    if config.dry_run:
        print("(dry-run) Skipping download.")
        return

    # Step 3: Download the track
    result = await downloader.download_track(
        video_id=video_id,
        title=title,
        artist=channel,
        album=album,
        year=year,
        genre=genre,
    )

    if result.success:
        print("(OK) Successfully downloaded!")
    else:
        print(f"(FAIL) Download failed: {result.error}")


async def download_playlist(
    config: Config,
    api_client: YouTubeAPIClient,
    downloader: AudioDownloader,
    playlist_url_or_id: str,
) -> None:
    """Download all tracks from a YouTube playlist."""

    print(f"\nDownloading Playlist: {playlist_url_or_id}")

    # Step 1: Extract video IDs using YouTube Music API (FREE — no API quota)
    with Progress(
        SpinnerColumn(),
        TextColumn("[dim]Extracting playlist contents via YouTube Music API...[/dim]"),
        console=console,
    ) as progress:
        task = progress.add_task("Scanning playlist...", total=None)

        # Try ytmusicapi first (works for public playlists, no quota needed)
        video_ids = downloader.extract_playlist_ids_from_ytmusic(playlist_url_or_id)

        if not video_ids:
            print("  YouTube Music API extraction failed. Falling back to YouTube Data API...")
            try:
                pid = extract_playlist_id(playlist_url_or_id) or playlist_url_or_id
                api_items = api_client.get_playlist_videos(pid)
                video_ids = [item["video_id"] for item in api_items]
            except Exception as e:
                print(f"  (FAIL) Could not extract playlist: {e}")
                return

        progress.update(task, completed=len(video_ids))

    total = len(video_ids)
    print(f"  Found {total} tracks in playlist\n")

    # Step 2: Extract metadata from YouTube Music API for ALL videos
    print("Extracting metadata via YouTube Music API (no API quota used)...")

    items = []
    all_results = {}
    skipped_videos = []  # Videos that are unavailable or have no metadata

    with Progress(
        SpinnerColumn(),
        TextColumn("[dim]{task.description}[/dim]"),
        console=console,
    ) as progress:
        task = progress.add_task("Extracting metadata...", total=total)

        for idx, vid in enumerate(video_ids):

            # Extract from ytmusicapi (FREE — no quota needed)
            primary_meta = extract_metadata_primary(vid, downloader)

            if not primary_meta or not primary_meta.get("title"):
                # Fallback to API only for this video
                fallback_meta = extract_metadata_fallback(vid, api_client)
                meta = merge_metadata(None, fallback_meta)
            else:
                fallback_meta = None
                if not primary_meta.get("uploader"):
                    fallback_meta = extract_metadata_fallback(vid, api_client)
                meta = merge_metadata(primary_meta, fallback_meta)

            # Skip videos that are unavailable (no title from either source)
            if not meta.get("title"):
                reason = "unavailable on YouTube"
                skipped_videos.append((vid, reason))
                progress.update(task, advance=1)
                continue

            year = meta.get("year", 0)
            genre = meta.get("genre", "Unknown")
            description = meta.get("description", "")
            album = extract_album_from_description(description)

            items.append(DownloadQueueItem(
                video_id=vid,
                title=meta.get("title", ""),
                channel=meta.get("channel", ""),
                metadata={
                    "channel": meta.get("channel", ""),
                    "year": year,
                    "genre": genre,
                    "album": album,
                    "description": description,
                },
            ))

            all_results[vid] = {
                "title": meta.get("title", ""),
                "artist": meta.get("channel", ""),
                "year": year,
                "genre": genre,
                "album": album,
            }

            progress.update(task, advance=1)

    # Step 3: Download all tracks concurrently (limited by semaphore)
    if config.dry_run:
        print("\n(dry-run) Preview of what would be downloaded:\n")
        for idx, item in enumerate(items):
            meta = item.metadata or {}
            year = meta.get("year", 0)
            genre = meta.get("genre", "Unknown")
            album = meta.get("album", "")
            album_str = f" ({album})" if album else ""
            print(
                f"  {idx + 1}. {item.title} "
                f"({meta.get('channel', '')}) - {year}/{genre}{album_str}"
            )
        return

    print(f"\nDownloading up to {config.max_concurrent_downloads} tracks at a time...\n")

    results = await downloader.download_batch(items)

    # Attach metadata to results for organization and track status
    failed_downloads = []
    already_exists = []  # Songs that were already downloaded
    for idx, result in enumerate(results):
        if result.success and items[idx].metadata:
            meta = items[idx].metadata
            result._metadata = {  # type: ignore[attr-defined]
                "title": all_results.get(result.video_id, {}).get("title", ""),
                "artist": all_results.get(result.video_id, {}).get("artist", ""),
                "year": meta.get("year", 0),
                "genre": meta.get("genre", "Unknown"),
                "album": meta.get("album", ""),
            }

            # Check if this was an existing file (not newly downloaded)
            if result.file_path and items[idx].title:
                from downloader import _find_existing_file
                existing = _find_existing_file(
                    config.output_dir, items[idx].title, meta.get("channel", ""),
                    config.preferred_audio_codec
                )
                # If file exists but wasn't just created (check modification time)
                if existing and result.file_path == existing:
                    try:
                        # Simple heuristic: if file is older than 1 minute, it's pre-existing
                        import os as _os
                        mtime = _os.path.getmtime(result.file_path)
                        if (_os.time() - mtime) > 60:
                            already_exists.append((items[idx].title or items[idx].video_id))
                    except Exception:
                        pass
        elif not result.success:
            failed_downloads.append((items[idx].title or items[idx].video_id, result.error))

    # Step 4: Organize into Year/Genre structure
    print()
    summary = organize_batch(results, config.output_dir)
    print_organization_summary(summary)

    # Report skipped, existing, and failed videos
    total_skipped = len(skipped_videos)
    total_existing = len(already_exists)
    total_failed = len(failed_downloads)
    total_issues = total_skipped + total_existing + total_failed

    if total_issues > 0:
        print("\n" + "=" * 40)
        print(f"Summary: {total_existing} existing, {total_skipped} skipped, {total_failed} failed")
        print("=" * 40)

        for vid, reason in skipped_videos:
            title = all_results.get(vid, {}).get("title", "Unknown Title")
            print(f"  ⏭ Skipped:   [{vid}] {title} — {reason}")

        if already_exists:
            for title_or_id in already_exists:
                print(f"  ✓ Existing:  [{title_or_id}]")

        for title_or_id, error in failed_downloads:
            clean_error = error.replace("ERROR: [youtube] ", "").split(":")[0]
            print(f"  ✗ Failed:    [{title_or_id}] — {clean_error}")


async def search_and_download(
    config: Config,
    api_client: YouTubeAPIClient,
    downloader: AudioDownloader,
    query: str,
    max_results: int = 5,
) -> None:
    """Search for a song and let user pick from results."""

    print(f"\nSearching: {query}\n")

    # Search costs 100 units — use sparingly
    with Progress(
        SpinnerColumn(),
        TextColumn("[dim]Searching YouTube...[/dim]"),
        console=console,
    ) as progress:
        results = api_client.search_by_query(query, max_results=max_results)

    if not results:
        print("(FAIL) No results found.")
        return

    # Display search results in a table
    table = Table(title="Search Results")
    table.add_column("#", style="cyan", width=3)
    table.add_column("Title", style="white")
    table.add_column("Channel", style="dim")
    table.add_column("Published", style="dim")

    for idx, item in enumerate(results):
        pub = item["published_at"][:10] if item.get("published_at") else ""
        table.add_row(
            str(idx + 1),
            f"[bold]{item['title']}[/bold]",
            item["channel"],
            pub,
        )

    console.print(table)

    # Let user pick (or auto-pick first result in non-interactive mode)
    if sys.stdin.isatty():
        try:
            choice = input("\nSelect track number (1-{}) or 'a' for all: ".format(len(results)))
        except (EOFError, KeyboardInterrupt):
            print("(cancelled)")
            return

        choice = choice.strip().lower()
    else:
        choice = "1"  # Default to first result in non-interactive mode

    if choice == "a":
        for idx, item in enumerate(results):
            await download_single_track(config, api_client, downloader, item["video_id"])
            # Space out single downloads to avoid rate limiting
            if idx < len(results) - 1:
                await asyncio.sleep(3.0)
    else:
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(results):
                await download_single_track(
                    config, api_client, downloader, results[idx]["video_id"]
                )
            else:
                print("(error) Invalid selection.")
        except ValueError:
            print("(error) Invalid input. Enter a number or 'a'.")


# ─── Resolve Tags Batch Command ──────────────────────────────────────

async def _cmd_resolve_all(config: Config) -> None:
    """Resolve tags for all previously downloaded songs in the output directory."""

    console.print(Panel.fit(
        "[bold cyan]Tag Resolver[/bold cyan]\n"
        f"[dim]Scanning: {config.output_dir}[/dim]",
        subtitle="Fetching fresh metadata from YouTube Music API",
    ))

    # Check ffmpeg is available (needed for reading duration)
    from downloader import _check_ffmpeg
    if not _check_ffmpeg():
        print("(ERROR) ffmpeg is required but was not found on PATH.")
        return

    from tag_resolver import resolve_all_files, find_audio_files

    files = find_audio_files(config.output_dir)
    total = len(files)

    if total == 0:
        console.print(f"\n[dim]No audio files found in {config.output_dir}[/dim]")
        return

    print(f"\nFound {total} audio file(s) to resolve.\n")

    # Show what we're about to process
    for idx, f in enumerate(files[:20], 1):  # Limit preview
        rel = f.relative_to(config.output_dir)
        console.print(f"  {idx}. [dim]{rel}[/dim]")
    if total > 20:
        print(f"  ... and {total - 20} more")

    confirm = input("\nProceed? (y/N): ").strip().lower()
    if confirm != "y":
        print("Cancelled.")
        return

    # Run resolution
    summary = resolve_all_files(config.output_dir)

    # Print results
    console.print(f"\n[bold]Results:[/bold]")
    console.print(f"  Total:   {summary['total']}")
    console.print(f"  Updated: [green]{summary['success']}[/green]")
    if summary['failed']:
        console.print(f"  Failed:  [red]{summary['failed']}[/red]")

    # Show details for updated files
    updated = [r for r in summary['results'] if r['success']]
    if updated:
        table = Table(title="Updated Files")
        table.add_column("File", style="cyan", no_wrap=True)
        table.add_column("Changes", style="white")

        for entry in updated[:50]:  # Limit display
            changes_str = ", ".join(entry['changes']) if entry['changes'] else "none"
            console.print(
                f"  ✓ {entry['file']} — changed: {changes_str}"
            )

    failed = [r for r in summary['results'] if not r['success']]
    if failed:
        print(f"\n[dim]Files that could not be resolved ({len(failed)}):[/dim]")
        for entry in failed[:20]:
            console.print(f"  ✗ {entry['file']}")
        if len(failed) > 20:
            print(f"  ... and {len(failed) - 20} more")

    # Print summary stats
    total_changes = sum(len(r['changes']) for r in updated)
    if total_changes > 0:
        console.print(
            f"\n[dim]Total fields updated: {total_changes}[/dim]"
        )


# ─── Main Entry Point ─────────────────────────────────────────────────

async def main() -> int:
    """Main entry point."""

    parser = build_parser()
    args = parser.parse_args()

    # Load config from environment
    try:
        config = Config.from_env()
    except ValueError as e:
        print(f"(ERROR) {e}")
        return 1

    # Override config with CLI args
    if args.output_dir:
        config.output_dir = args.output_dir.resolve()
    if args.concurrent:
        config.max_concurrent_downloads = args.concurrent
    if args.codec:
        config.preferred_audio_codec = args.codec

    config.dry_run = args.dry_run

    # Setup logging
    log_level = "WARNING" if args.quiet else "INFO"
    from utils import setup_logging
    setup_logging(log_level)

    # Ensure output directory exists
    config.ensure_dirs()

    # Check for ffmpeg (required for audio transcoding)
    from downloader import _check_ffmpeg
    if not _check_ffmpeg():
        print(
            "(ERROR) ffmpeg is required but was not found on PATH.\n"
            "Install it and add to PATH, or download from:\n"
            "  https://ffmpeg.org/download.html (Windows: build-shared.zip)\n"
            "After extracting, place ffmpeg.exe in a folder on your PATH\n"
            "(e.g., C:\\Windows\\) or set the PATH environment variable."
        )
        return 1

    # Print welcome banner
    codec = config.preferred_audio_codec
    conc = config.max_concurrent_downloads
    rpm = config.api_requests_per_minute
    console.print(Panel.fit(
        "[bold cyan]YouTube Music Downloader[/bold cyan]\n"
        f"[dim]Output: {config.output_dir} | "
        f"Codec: {codec} | "
        f"Concurrency: {conc} | "
        f"API Rate Limit: {rpm}/min[/dim]",
        subtitle="Production Edition - ytmusicapi primary, API fallback",
    ))

    # Determine tag resolution setting
    resolve_tags = not args.no_resolve

    # Initialize clients
    api_client = YouTubeAPIClient(
        api_key=config.api_key,
        requests_per_minute=config.api_requests_per_minute,
    )
    downloader = AudioDownloader(
        output_dir=config.output_dir,
        audio_codec=config.preferred_audio_codec,
        max_concurrent=config.max_concurrent_downloads,
        resolve_tags=resolve_tags,
    )

    # Handle --resolve-all batch command
    if args.resolve_all:
        await _cmd_resolve_all(config)
        return 0

    # Determine mode of operation
    try:
        if args.search:
            await search_and_download(config, api_client, downloader, args.search, args.max_results)

        elif args.playlist or (args.input and is_playlist_url(args.input)):
            playlist_id = args.playlist or extract_playlist_id(args.input)
            if not playlist_id:
                print("(error) Could not extract playlist ID from URL.")
                return 1
            await download_playlist(config, api_client, downloader, playlist_id)

        elif args.input:
            video_id = extract_video_id(args.input)
            if not video_id:
                print(
                    "(error) Could not extract video ID from input.\n"
                    "Provide a YouTube URL or 11-character video ID."
                )
                return 1
            await download_single_track(config, api_client, downloader, video_id)

        else:
            parser.print_help()
            return 0

    except KeyboardInterrupt:
        print("(!) Interrupted by user.")
        return 130
    except Exception as e:
        # Always log full traceback to a dated error file
        _write_error_log(e)

        err_str = str(e).lower()

        if "api key not valid" in err_str or "invalid api key" in err_str:
            print(
                "(FAIL) Invalid Google API Key.\n"
                "  Get a free key at: https://console.cloud.google.com/apis/credentials\n"
                "  Then add it to .env as GOOGLE_API_KEY=your_key_here"
            )
        elif "quota exceeded" in err_str or "403" in err_str:
            print(
                "(FAIL) API quota exceeded or key not authorized. Check:\n"
                "  - YouTube Data API v3 is enabled in Google Cloud Console\n"
                "  - You haven't exceeded your daily quota (10,000 units/day)"
            )
        elif "playlistnotfound" in err_str or ("404" in err_str and "playlistid" in err_str):
            print(
                "(FAIL) Playlist not found. This can happen if:\n"
                "  - The playlist is private or unlisted\n"
                "  - The playlist was deleted\n"
                "  - The playlist ID is incorrect\n"
                "Try a public playlist URL instead."
            )
        elif "videonotfound" in err_str or ("404" in err_str and "video" in err_str):
            print("(FAIL) Video not found. Check the URL or video ID.")
        else:
            # Short message for unknown errors — full details are in the log file
            short = str(e).splitlines()[0][:120]
            print(f"(FAIL) {short}")
            print("  Full error logged to: " + _error_log_path())
        return 1

    # Print summary
    if config.output_dir.exists():
        from utils import count_files_recursive, get_directory_size
        file_count = count_files_recursive(config.output_dir)
        size = get_directory_size(config.output_dir)
        print(f"\nTotal files: {file_count} | Size: {size}")

    return 0


def main_sync() -> None:
    """Synchronous wrapper for async main."""
    exit_code = asyncio.run(main())
    sys.exit(exit_code)


if __name__ == "__main__":
    main_sync()
