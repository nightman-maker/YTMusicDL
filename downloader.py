"""
Audio downloader using YouTube Music API + yt-dlp for URL extraction only.

Architecture:
  - ytmusicapi: metadata extraction (title, artist, duration) — FREE, no quota
  - yt-dlp:     URL extraction only (handles YouTube signature encryption)
  - requests:   actual file download from the extracted URLs
  - ffmpeg:     audio transcoding (MP3/FLAC conversion)
  - mutagen:    metadata tag embedding

No yt-dlp downloading — we extract URLs and handle everything else ourselves.
"""

import asyncio
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests
import mutagen
import mutagen.mp3
import mutagen.flac
import mutagen.oggopus
import yt_dlp
import ytmusicapi
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)


logger = logging.getLogger(__name__)

# Find ffmpeg: check PATH first, then project directory
_PROJECT_DIR = Path(__file__).parent
_FFMPEG_PATH: str | None = None


def _find_ffmpeg() -> str | None:
    """Find ffmpeg executable — checks PATH first, then project directory."""
    import shutil
    path = shutil.which("ffmpeg")
    if path:
        return path
    local = _PROJECT_DIR / "ffmpeg.exe"
    if local.exists():
        return str(local)
    return None


def _check_ffmpeg() -> bool:
    """Check if ffmpeg is available on the system."""
    global _FFMPEG_PATH
    _FFMPEG_PATH = _find_ffmpeg()
    return _FFMPEG_PATH is not None


# ─── ytmusicapi client singleton ──────────────────────────────────────

_ytmusic_instance: ytmusicapi.YTMusic | None = None


def get_ytmusic() -> ytmusicapi.YTMusic:
    """Get or create the shared YTMusic instance."""
    global _ytmusic_instance
    if _ytmusic_instance is None:
        _ytmusic_instance = ytmusicapi.YTMusic(language="en")
    return _ytmusic_instance


# ─── Metadata extraction via YouTube Music API ────────────────────────

def extract_playlist_ids_from_ytmusic(playlist_url_or_id: str) -> list[str]:
    """
    Extract all video IDs from a YouTube Music playlist.

    Uses ytmusicapi's get_playlist() which talks to YouTube Music's internal API.
    Returns video IDs in order, skipping unavailable videos gracefully.
    """
    try:
        ytm = get_ytmusic()
        # Normalize: if it looks like a URL, extract the ID
        playlist_id = playlist_url_or_id
        if "playlist" in playlist_url_or_id:
            match = re.search(r'list=([a-zA-Z0-9_-]{13,})', playlist_url_or_id)
            if match:
                playlist_id = match.group(1)

        data = ytm.get_playlist(playlist_id, limit=None)  # type: ignore[arg-type]
        tracks = data.get("tracks", [])

        video_ids = []
        for track in tracks:
            vid = track.get("videoId")
            if vid and not track.get("isAvailable", True):
                continue  # Skip unavailable videos
            if vid:
                video_ids.append(vid)

        return video_ids

    except Exception as e:
        logger.warning(f"Could not extract playlist IDs via ytmusicapi: {e}")
        return []


def extract_metadata_from_ytmusic(video_id: str) -> dict[str, Any]:
    """
    Extract metadata from YouTube Music using ytmusicapi.

    Uses get_song() which returns full metadata + streaming data.
    This is FREE — no API quota needed.
    """
    try:
        ytm = get_ytmusic()
        song_data = ytm.get_song(video_id)

        if not song_data:
            return {}

        # Extract basic info from videoDetails
        video_details = song_data.get("videoDetails", {})

        title = video_details.get("title", "")
        artist = video_details.get("author", "").replace(" - Topic", "").strip()
        duration = int(float(video_details.get("lengthSeconds", 0)))

        # Try to get year from microformat if available
        year = 0
        try:
            microformat = song_data.get("microformat", {})
            vmr = microformat.get("microformatDataRenderer", {})
            publish_date = vmr.get("publishDate", "")
            if publish_date and len(publish_date) >= 4:
                year = int(publish_date[:4])
        except (ValueError, IndexError):
            pass

        return {
            "title": title,
            "uploader": artist,
            "description": video_details.get("shortDescription", ""),
            "duration": duration,
            "year": year,
            "channel_id": "",
            "view_count": int(video_details.get("viewCount", 0)),
        }

    except Exception as e:
        logger.warning(f"Could not extract metadata for {video_id}: {e}")
        return {}


# ─── URL extraction via yt-dlp (signature decryption only) ────────────

def _get_audio_url(video_id: str, audio_codec: str = "mp3") -> str | None:
    """
    Extract a direct audio stream URL using yt-dlp.

    yt-dlp handles YouTube's signature encryption and returns usable URLs.
    We only use it for URL extraction — no downloading or transcoding.
    """
    try:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            # Only request audio formats
            "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
            "js_runtimes": {"node": {}},
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}",
                download=False,
            )

        if not info:
            return None

        # Get the URL from the selected format
        url = info.get("url") or info.get("format_url")
        if url:
            return str(url)

        # Try adaptive formats
        fmts = info.get("formats", [])
        for fmt in reversed(fmts):  # Prefer higher quality (later in list)
            mime = fmt.get("format_note", "") or fmt.get("quality", "")
            if "audio" in fmt.get("acodec", "") and fmt.get("url"):
                return str(fmt["url"])

        return None

    except Exception as e:
        logger.warning(f"Could not extract URL for {video_id}: {e}")
        return None


# ─── Audio download via requests + ffmpeg ─────────────────────────────

_FFMPEG_PROGRESS_RE = re.compile(
    r"time=(\d+):(\d{2}):(\d{2})\.?(\d*)"
)


def _transcode_audio(
    input_path: Path,
    output_path: Path,
    codec: str,
    progress_callback: Callable[[int, int | None], None] | None = None,
) -> bool:
    """Transcode audio file to target format using ffmpeg.

    Parses ffmpeg's stderr for time-based progress and reports it via callback.
    """
    global _FFMPEG_PATH
    if _FFMPEG_PATH is None:
        _check_ffmpeg()

    ffmpeg_exe = _FFMPEG_PATH or "ffmpeg"

    if codec == "mp3":
        cmd = [
            ffmpeg_exe, "-y", "-i", str(input_path),
            "-codec:a", "libmp3lame", "-b:a", "320k",
            "-map_metadata", "0",
            str(output_path),
        ]
    elif codec == "flac":
        cmd = [
            ffmpeg_exe, "-y", "-i", str(input_path),
            "-codec:a", "flac",
            "-compression_level", "8",
            "-map_metadata", "0",
            str(output_path),
        ]
    elif codec == "opus":
        cmd = [
            ffmpeg_exe, "-y", "-i", str(input_path),
            "-codec:a", "libopus", "-b:a", "160k",
            "-map_metadata", "0",
            str(output_path),
        ]
    else:
        logger.error(f"Unknown codec: {codec}")
        return False

    try:
        # Get input duration for progress calculation
        input_duration = 0
        if progress_callback and output_path.exists():
            # Probe the input file to get its duration
            probe_cmd = [
                ffmpeg_exe, "-i", str(input_path),
                "-f", "null", "-"
            ]
            try:
                probe = subprocess.run(
                    probe_cmd,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                # Parse duration from stderr: "Duration: 00:03:45.12"
                dur_match = re.search(r"Duration:\s*(\d+):(\d{2}):(\d{2})\.?(\d*)", probe.stderr)
                if dur_match:
                    h, m, s, _ = dur_match.groups()
                    input_duration = int(h) * 3600 + int(m) * 60 + int(s)
            except Exception:
                pass

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
        )

        last_progress_time = 0.0
        while True:
            line = proc.stderr.readline()
            if not line and proc.poll() is not None:
                break

            if progress_callback and input_duration > 0:
                now = time.time()
                # Throttle progress updates to ~1 per second
                if now - last_progress_time < 1.0:
                    continue
                last_progress_time = now

                match = _FFMPEG_PROGRESS_RE.search(line)
                if match:
                    h, m, s, _ = match.groups()
                    elapsed = int(h) * 3600 + int(m) * 60 + int(s)
                    progress_callback(elapsed, input_duration)

        proc.wait()

        if proc.returncode != 0:
            stderr_output = proc.stderr.read() if not line else ""
            logger.error(f"ffmpeg error: {stderr_output[:200]}")
            return False
        return True

    except FileNotFoundError:
        logger.error(
            "ffmpeg not found. Install it from https://ffmpeg.org/download.html\n"
            "Required for audio transcoding (MP3/FLAC conversion)."
        )
        return False


def _embed_metadata(
    file_path: Path,
    title: str,
    artist: str = "",
    album: str = "",
    year: int = 0,
    genre: str = "Unknown",
    track_number: int = 0,
) -> None:
    """Embed metadata tags into an audio file using mutagen."""
    try:
        if not title and not artist:
            return

        ext = file_path.suffix.lower()

        if ext == ".mp3":
            # Use EasyID3 for MP3 — accepts plain string values
            import mutagen.easyid3
            tags = mutagen.easyid3.EasyID3(str(file_path))
            # Clear existing tags
            for key in list(tags.keys()):
                del tags[key]
            tags["title"] = title
            if artist:
                tags["artist"] = artist
            if album:
                tags["album"] = album
            if year > 0:
                tags["date"] = str(year)
            if genre and genre != "Unknown":
                tags["genre"] = genre
            if track_number > 0:
                tags["tracknumber"] = str(track_number)
            tags.save()

        elif ext == ".flac":
            tags = mutagen.flac.FLAC(str(file_path))
            tags["title"] = title
            if artist:
                tags["artist"] = artist
            if album:
                tags["album"] = album
            if year > 0:
                tags["date"] = str(year)
            if genre and genre != "Unknown":
                tags["genre"] = genre
            if track_number > 0:
                tags["tracknumber"] = str(track_number)
            tags.save()

        elif ext == ".opus":
            tags = mutagen.oggopus.OggOpus(str(file_path))
            tags["title"] = title
            if artist:
                tags["artist"] = artist
            if album:
                tags["album"] = album
            if year > 0:
                tags["date"] = str(year)
            if genre and genre != "Unknown":
                tags["genre"] = genre
            if track_number > 0:
                tags["tracknumber"] = str(track_number)
            tags.save()

    except Exception as e:
        logger.debug(f"Could not embed metadata into {file_path}: {e}")


_YTMUSIC_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Referer": "https://music.youtube.com/",
}


def _format_bytes(size: int) -> str:
    """Format bytes into human-readable string."""
    for unit in ["B", "KB", "MB", "GB"]:
        if abs(size) < 1024.0:
            return f"{size:.1f}{unit}"
        size /= 1024.0
    return f"{size:.1f}TB"


_MULTI_CONNECTIONS = 4  # Parallel connections per file (tune for speed)
_CHUNK_SIZE = 1024 * 64  # 64 KB read chunks


def _download_stream(
    url: str,
    output_path: Path,
    progress_callback: Callable[[int, int | None], None] | None = None,
) -> bool:
    """Download an audio stream with retries and proper headers.

    Uses multi-connection (parallel chunk) downloads by default for speed.
    Falls back to single connection if the server doesn't support Range requests.

    Args:
        url: The URL to download from.
        output_path: Where to save the file.
        progress_callback: Called as callback(downloaded_bytes, total_bytes).
                           total_bytes may be None if Content-Length is unknown.
    """
    last_error = None

    for attempt in range(3):
        if attempt > 0:
            delay = min(5 * (2 ** (attempt - 1)), 30)  # 5s, 10s, max 30s
            logger.info(f"Retrying download in {delay}s (attempt {attempt + 1}/3)")
            time.sleep(delay)

        try:
            # Step 1: HEAD request to get file size and check Range support
            head_resp = requests.head(
                url,
                headers=_YTMUSIC_HEADERS,
                timeout=30,
            )
            if head_resp.status_code != 200:
                logger.error(f"HEAD request failed: HTTP {head_resp.status_code}")
                last_error = f"HTTP {head_resp.status_code}"
                continue

            total_size_header = head_resp.headers.get("Content-Length")
            accept_ranges = head_resp.headers.get("Accept-Ranges", "")
            file_size: int | None = (
                int(total_size_header) if total_size_header else None
            )

            # Step 2: Try multi-connection download if server supports it
            use_multi = (
                file_size is not None
                and ("bytes" in accept_ranges.lower() or "range" in url)
                and file_size > 1024 * 512  # Only for files > 512KB
            )

            if use_multi:
                downloaded = _download_stream_multi(
                    url, output_path, file_size,
                    progress_callback=progress_callback,
                )
            else:
                # Fallback: single connection with streaming
                downloaded = _download_stream_single(
                    url, output_path, file_size,
                    progress_callback=progress_callback,
                )

            if downloaded:
                return True

        except requests.exceptions.ConnectionError as e:
            last_error = str(e)
            logger.warning(f"Connection error (attempt {attempt + 1}/3): {e}")
        except Exception as e:
            last_error = str(e)
            logger.warning(f"Download failed (attempt {attempt + 1}/3): {e}")

    # Clean up partial file on final failure
    try:
        if output_path.exists():
            output_path.unlink()
    except Exception:
        pass

    logger.error(f"Stream download failed after retries: {last_error}")
    return False


def _download_stream_single(
    url: str,
    output_path: Path,
    total_bytes: int | None,
    progress_callback: Callable[[int, int | None], None] | None = None,
) -> bool:
    """Download using a single HTTP connection (fallback mode)."""
    try:
        with requests.get(
            url,
            headers=_YTMUSIC_HEADERS,
            timeout=600,  # 10 min max
            stream=True,
        ) as response:
            if response.status_code != 200:
                logger.error(f"HTTP {response.status_code} downloading stream")
                return False

            downloaded = 0

            with open(output_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)

                    if progress_callback and (downloaded % (1024 * 512) < _CHUNK_SIZE):
                        progress_callback(downloaded, total_bytes)

            return output_path.exists() and output_path.stat().st_size > 0

    except Exception as e:
        logger.warning(f"Single-connection download failed: {e}")
        return False


def _download_stream_multi(
    url: str,
    output_path: Path,
    total_bytes: int,
    progress_callback: Callable[[int, int | None], None] | None = None,
) -> bool:
    """Download using multiple parallel HTTP Range connections.

    Splits the file into N chunks and downloads them simultaneously,
    then merges them in order. This can significantly increase download speed
    by saturating the connection bandwidth.
    """
    num_connections = min(_MULTI_CONNECTIONS, max(2, total_bytes // (1024 * 512)))
    chunk_size = total_bytes // num_connections

    # Create temp files for each chunk
    chunk_files: list[Path] = []
    try:
        for i in range(num_connections):
            start = i * chunk_size
            end = (start + chunk_size - 1) if i < num_connections - 1 else total_bytes - 1
            chunk_path = Path(str(output_path) + f".part{i}")
            chunk_files.append(chunk_path)

        # Download each chunk in parallel using threads
        errors: list[str] = []
        lock = threading.Lock()

        def _download_chunk(idx: int, start: int, end: int) -> None:
            """Download a single range and write to temp file."""
            headers = dict(_YTMUSIC_HEADERS)
            headers["Range"] = f"bytes={start}-{end}"

            try:
                with requests.get(
                    url,
                    headers=headers,
                    timeout=600,
                    stream=True,
                ) as response:
                    if response.status_code not in (200, 206):
                        with lock:
                            errors.append(f"Chunk {idx}: HTTP {response.status_code}")
                        return

                    downloaded = 0
                    expected_size = end - start + 1

                    with open(chunk_files[idx], "wb") as f:
                        for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                            if not chunk:
                                continue
                            f.write(chunk)
                            downloaded += len(chunk)

            except Exception as e:
                with lock:
                    errors.append(f"Chunk {idx}: {e}")

        threads: list[threading.Thread] = []
        for i in range(num_connections):
            start = i * chunk_size
            end = (start + chunk_size - 1) if i < num_connections - 1 else total_bytes - 1
            t = threading.Thread(
                target=_download_chunk,
                args=(i, start, end),
                daemon=True,
            )
            threads.append(t)
            t.start()

        # Wait for all threads with progress reporting
        import time as _time
        last_report = 0.0
        while any(t.is_alive() for t in threads):
            now = _time.time()
            if now - last_report >= 1.0:
                last_report = now
                # Calculate total downloaded from completed chunks + current progress
                total_downloaded = 0
                for i, t in enumerate(threads):
                    if not t.is_alive() and chunk_files[i].exists():
                        total_downloaded += chunk_files[i].stat().st_size
                    elif t.is_alive() and chunk_files[i].exists():
                        # Estimate: thread is alive but file exists = partial write
                        try:
                            total_downloaded += chunk_files[i].stat().st_size
                        except Exception:
                            pass
                if progress_callback:
                    progress_callback(total_downloaded, total_bytes)
            _time.sleep(0.1)

        # Wait for all threads to finish
        for t in threads:
            t.join(timeout=610)

        if errors:
            logger.warning(f"Multi-connection download had errors: {errors}")
            return False

        # Merge chunks into final file (in order, no gaps expected)
        with open(output_path, "wb") as out:
            for chunk_file in chunk_files:
                if not chunk_file.exists():
                    logger.error(f"Missing chunk: {chunk_file}")
                    return False
                with open(chunk_file, "rb") as inp:
                    while True:
                        data = inp.read(1024 * 1024)  # 1 MB read for merge
                        if not data:
                            break
                        out.write(data)

        return output_path.exists() and output_path.stat().st_size == total_bytes

    finally:
        # Clean up chunk files on any failure path
        for cf in chunk_files:
            try:
                if cf.exists():
                    cf.unlink()
            except Exception:
                pass


# ─── Main Downloader Class ────────────────────────────────────────────

class AudioDownloader:
    """Downloads audio tracks using ytmusicapi + yt-dlp (URL only) + requests."""

    def __init__(
        self,
        output_dir: Path,
        audio_codec: str = "mp3",
        max_concurrent: int = 2,
    ):
        self.output_dir = output_dir
        self.audio_codec = audio_codec
        self.max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)

    def extract_playlist_ids_from_ytmusic(self, playlist_url_or_id: str) -> list[str]:
        """Extract video IDs from a YouTube Music playlist."""
        return extract_playlist_ids_from_ytmusic(playlist_url_or_id)

    def extract_metadata_from_ytmusic(self, video_id: str) -> dict[str, Any]:
        """Extract metadata for a single video via ytmusicapi."""
        return extract_metadata_from_ytmusic(video_id)

    async def download_track(
        self,
        video_id: str,
        title: str = "",
        artist: str = "",
        album: str = "",
        year: int = 0,
        genre: str = "Unknown",
        track_number: int = 0,
    ) -> DownloadResult:
        """Download a single track with metadata embedding."""

        async with self._semaphore:
            return await asyncio.to_thread(
                self._download_single,
                video_id=video_id,
                title=title,
                artist=artist,
                album=album,
                year=year,
                genre=genre,
                track_number=track_number,
            )

    def _download_single(
        self,
        video_id: str,
        title: str = "",
        artist: str = "",
        album: str = "",
        year: int = 0,
        genre: str = "Unknown",
        track_number: int = 0,
    ) -> DownloadResult:
        """Perform the actual download (runs in thread pool)."""

        # Build output filename template
        safe_title = sanitize_filename(title or video_id)
        if artist and artist != "Unknown Artist":
            safe_artist = sanitize_filename(artist)
            filename_template = str(
                self.output_dir / f"{safe_artist} - {safe_title}.{self.audio_codec}"
            )
        else:
            filename_template = str(
                self.output_dir / f"{safe_title}.{self.audio_codec}"
            )

        result = DownloadResult(
            success=False,
            video_id=video_id,
            title=title,
            artist=artist,
            album=album,
            year=year,
            genre=genre,
        )

        # Check if file already exists (skip download)
        existing_file = _find_existing_file(
            self.output_dir, title or video_id, artist, self.audio_codec
        )
        if existing_file:
            result.success = True
            result.file_path = existing_file
            result.title = title or sanitize_filename(video_id)
            logger.info(f"Already exists: {result.title} -> {existing_file}")
            return result

        # Prepare display name for progress bar
        track_display = f"{artist} - {title}" if artist and artist != "Unknown Artist" else title or video_id

        try:
            # Step 1: Get metadata from ytmusicapi (FREE — no quota)
            ytm = get_ytmusic()
            song_data = ytm.get_song(video_id)

            if not song_data:
                raise RuntimeError("No song data returned from YouTube Music API")

            video_title = song_data.get("videoDetails", {}).get("title", title)
            duration = int(float(song_data.get("videoDetails", {}).get("lengthSeconds", 0)))

            # Step 2: Extract audio URL using yt-dlp (handles signature encryption)
            audio_url = _get_audio_url(video_id, self.audio_codec)
            if not audio_url:
                raise RuntimeError("No audio stream URL found for this video")

            with Progress(
                TextColumn("[bold cyan]{task.fields[track]}[/bold cyan]"),
                SpinnerColumn(spinner_name="dots", style="blue"),
                BarColumn(bar_width=30),
                TextColumn("{task.percentage:>6.1f}%"),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                console=Console(file=sys.stderr, force_terminal=True, color_system="standard" if sys.platform == "win32" else None),
            ) as progress:

                # ── Step 3: Download with progress bar ───────────────────────
                download_task = progress.add_task(
                    f"[{track_display}]",
                    track=track_display,
                    total=None,  # Unknown total initially (YouTube streams often omit Content-Length)
                )

                temp_path = Path(filename_template + ".raw")
                downloaded_bytes = [0]
                total_size = [None]

                def _download_progress(downloaded: int, total: int | None) -> None:
                    """Update the Rich progress bar in real-time."""
                    downloaded_bytes[0] = downloaded
                    if total is not None:
                        total_size[0] = total
                        progress.update(download_task, total=total, completed=downloaded)
                    else:
                        # No Content-Length — show bytes without percentage
                        progress.update(download_task, completed=downloaded)

                downloaded = _download_stream(audio_url, temp_path, progress_callback=_download_progress)

                if not downloaded:
                    raise RuntimeError("Failed to download audio stream")

                # Finalize: set total to actual file size (in case Content-Length was wrong/missing)
                file_size = temp_path.stat().st_size
                progress.update(download_task, total=file_size, completed=file_size)

                # ── Step 4: Transcode with progress bar ───────────────────────
                output_path = Path(filename_template)
                transcode_elapsed = [0]
                transcode_total = [None]  # Duration in seconds

                def _transcode_progress(elapsed: int, total: int | None) -> None:
                    """Update the Rich progress bar with ffmpeg time-based progress."""
                    transcode_elapsed[0] = elapsed
                    if total is not None:
                        transcode_total[0] = total
                        # Convert seconds to a fraction for the progress bar
                        progress.update(transcode_task, total=total, completed=elapsed)
                    else:
                        progress.update(transcode_task, completed=elapsed)

                transcode_task = progress.add_task(
                    f"[{track_display}]",
                    track=track_display,
                    total=None,  # Duration unknown initially
                )

                transcoded = _transcode_audio(
                    temp_path, output_path, self.audio_codec,
                    progress_callback=_transcode_progress,
                )

                # Clean up temp file
                try:
                    if temp_path.exists():
                        temp_path.unlink()
                except Exception:
                    pass

                if not transcoded or not output_path.exists():
                    raise RuntimeError("Transcoding failed")

            # Step 5: Embed metadata tags (fast, no progress bar needed)
            _embed_metadata(
                file_path=output_path,
                title=sanitize_filename(video_title) if video_title else "",
                artist=sanitize_filename(artist) if artist and artist != "Unknown Artist" else "",
                album=sanitize_filename(album) if album else "",
                year=year,
                genre=genre if genre != "Unknown" else "",
                track_number=track_number,
            )

            result.success = True
            result.file_path = output_path
            result.title = video_title or title
            result.duration_seconds = duration
            logger.info(
                f"Downloaded: {result.title} "
                f"({result.duration_seconds}s) -> {output_path}"
            )

        except Exception as e:
            error_msg = str(e).replace("\n", " ")
            # Clean up partial files on failure
            try:
                for ext in [".flac", ".mp3", ".opus", ".raw"]:
                    candidate = Path(filename_template + ext)
                    if candidate.exists():
                        candidate.unlink()
                        logger.debug(f"Cleaned up partial file: {candidate}")
            except Exception:
                pass

            result.error = error_msg
            logger.error(
                f"Failed to download '{title}' (ID: {video_id}): "
                f"{error_msg[:200]}"
            )

        return result

    async def download_batch(
        self,
        items: list[DownloadQueueItem],
    ) -> list[DownloadResult]:
        """Download multiple tracks concurrently with rate limiting."""

        logger.info(f"Starting batch download of {len(items)} tracks...")

        tasks = []
        for idx, item in enumerate(items):
            metadata = item.metadata or {}
            task = asyncio.create_task(
                self.download_track(
                    video_id=item.video_id,
                    title=item.title,
                    artist=metadata.get("channel", ""),
                    album="",  # Not available from YouTube Music API directly
                    year=metadata.get("year", 0),
                    genre=metadata.get("genre", "Unknown"),
                    track_number=idx + 1,
                )
            )
            tasks.append(task)

            # Space out task creation to avoid triggering YouTube rate limits
            if idx < len(items) - 1:
                await asyncio.sleep(2.0)  # 2s between each download start

        results = await asyncio.gather(*tasks)
        return list(results)


def sanitize_filename(name: str, max_length: int = 150) -> str:
    """Sanitize a string to be used as a filename."""
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = re.sub(r'\s+', " ", name).strip()
    if len(name) > max_length:
        name = name[:max_length]
    return name


def _find_existing_file(
    output_dir: Path,
    title: str,
    artist: str = "",
    codec: str = "mp3",
) -> Path | None:
    """
    Check if a song already exists in the output directory.

    Looks for exact matches first, then fuzzy matches by title/artist.
    Returns the path if found, None otherwise.
    """
    safe_title = sanitize_filename(title)
    safe_artist = sanitize_filename(artist) if artist and artist != "Unknown Artist" else ""

    # Search all supported extensions in output directory
    for ext in [".mp3", ".flac", ".opus"]:
        target_dir = output_dir

        # Check exact filename match first
        if safe_artist:
            candidate = target_dir / f"{safe_artist} - {safe_title}{ext}"
            if candidate.exists():
                return candidate

        # Search recursively for matching files (handles Year/Genre folders)
        for root, _, files in os.walk(target_dir):
            for filename in files:
                if not filename.endswith(ext):
                    continue

                # Remove extension and split by " - "
                stem = Path(filename).stem
                parts = [p.strip() for p in stem.split(" - ", 1)]

                if len(parts) == 2:
                    file_artist, file_title = parts
                else:
                    file_artist = ""
                    file_title = parts[0]

                # Check artist match (if provided)
                if safe_artist and file_artist.lower() != safe_artist.lower():
                    continue

                # Check title match (case-insensitive, ignore punctuation/whitespace)
                clean_file_title = re.sub(r'[^a-z0-9]', '', file_title.lower())
                clean_target_title = re.sub(r'[^a-z0-9]', '', safe_title.lower())

                if clean_file_title == clean_target_title:
                    return Path(root) / filename

    return None


@dataclass
class DownloadResult:
    """Result of a download operation."""

    success: bool
    file_path: Path | None = None
    video_id: str = ""
    error: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    year: int = 0
    genre: str = "Unknown"
    duration_seconds: int = 0


@dataclass
class DownloadQueueItem:
    """An item in the download queue."""

    video_id: str
    title: str = ""
    channel: str = ""
    metadata: dict[str, Any] | None = None
