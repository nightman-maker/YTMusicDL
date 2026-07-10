"""
ID3 Tag Resolver — Fetches fresh metadata from YouTube Music API and updates existing audio files.

Workflow for new downloads:
  After transcoding, the resolver searches YouTube Music using the song's title (and artist)
  to find matching metadata, then overwrites all ID3 tags with the latest data.

Workflow for previously downloaded files (--resolve-all):
  Reads whatever tag/filename data is available, searches YouTube Music by name,
  and updates all tags with fresh metadata from the matched result.
"""

import logging
import re
from pathlib import Path
from typing import Any

import mutagen
import ytmusicapi

from downloader import sanitize_filename


logger = logging.getLogger(__name__)

# Supported audio extensions
_AUDIO_EXTENSIONS = {".mp3", ".flac", ".opus"}


# ─── Tag Reading ──────────────────────────────────────────────────────

def _read_tag(tags, key: str) -> str:
    """Safely get the first value of a tag key."""
    vals = tags.get(key, [])
    return vals[0] if vals else ""


def read_tags(file_path: Path) -> dict[str, Any]:
    """Read all readable tags from an audio file.

    Returns a dict with keys: title, artist, album, year, genre, tracknumber.
    Always returns at least the filename-based title if no tags exist.
    Uses mutagen.File() to avoid Windows file-handle conflicts.
    """
    result: dict[str, Any] = {
        "title": "",
        "artist": "",
        "album": "",
        "year": 0,
        "genre": "",
        "tracknumber": 0,
    }

    try:
        ext = file_path.suffix.lower()

        if ext == ".mp3":
            tags = mutagen.easyid3.EasyID3(str(file_path))
            result["title"] = _read_tag(tags, "title")
            result["artist"] = _read_tag(tags, "artist")
            result["album"] = _read_tag(tags, "album")
            date_val = _read_tag(tags, "date")
            if date_val:
                try:
                    result["year"] = int(date_val[:4])
                except (ValueError, IndexError):
                    pass
            result["genre"] = _read_tag(tags, "genre")
            track_val = _read_tag(tags, "tracknumber")
            if track_val:
                try:
                    result["tracknumber"] = int(track_val.split("/")[0])
                except ValueError:
                    pass
            del tags

        elif ext == ".flac":
            # Use mutagen.File() instead of FLAC directly to avoid
            # Windows file-handle conflicts (FLAC keeps handle open)
            tags = mutagen.File(str(file_path), easy=False)
            if tags:
                result["title"] = _read_tag(tags, "TITLE")
                result["artist"] = _read_tag(tags, "ARTIST")
                result["album"] = _read_tag(tags, "ALBUM")
                date_val = _read_tag(tags, "DATE")
                if date_val:
                    try:
                        result["year"] = int(date_val[:4])
                    except (ValueError, IndexError):
                        pass
                result["genre"] = _read_tag(tags, "GENRE")
                track_val = _read_tag(tags, "TRACKNUMBER")
                if track_val:
                    try:
                        result["tracknumber"] = int(track_val)
                    except ValueError:
                        pass
            del tags

        elif ext == ".opus":
            # Use mutagen.File() for the same reason as FLAC
            tags = mutagen.File(str(file_path), easy=False)
            if tags:
                result["title"] = _read_tag(tags, "TITLE")
                result["artist"] = _read_tag(tags, "ARTIST")
                result["album"] = _read_tag(tags, "ALBUM")
                date_val = _read_tag(tags, "DATE")
                if date_val:
                    try:
                        result["year"] = int(date_val[:4])
                    except (ValueError, IndexError):
                        pass
                result["genre"] = _read_tag(tags, "GENRE")
                track_val = _read_tag(tags, "TRACKNUMBER")
                if track_val:
                    try:
                        result["tracknumber"] = int(track_val)
                    except ValueError:
                        pass
            del tags

    except Exception as e:
        logger.debug(f"Could not read tags from {file_path}: {e}")

    # Fallback to filename for title if no tag exists
    if not result["title"]:
        stem = file_path.stem
        parts = [p.strip() for p in stem.split(" - ", 1)]
        result["title"] = parts[-1] if len(parts) > 1 else stem

    return result


# ─── Tag Writing (Full Overwrite) ─────────────────────────────────────

def write_tags(
    file_path: Path,
    title: str,
    artist: str = "",
    album: str = "",
    year: int = 0,
    genre: str = "Unknown",
    track_number: int = 0,
) -> bool:
    """Overwrite all ID3 tags on an audio file with the provided metadata.

    Directly overwrites existing tag fields without deleting them first
    (deleting-all-then-setting causes mutagen FLAC save bugs).
    Returns True on success, False on failure.
    """
    try:
        ext = file_path.suffix.lower()

        if ext == ".mp3":
            tags = mutagen.easyid3.EasyID3(str(file_path))
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
            # Use mutagen.File() instead of FLAC directly to avoid
            # Windows file-handle conflicts (FLAC keeps handle open after save())
            tags = mutagen.File(str(file_path), easy=False)
            if not tags:
                raise ValueError(f"Could not open {file_path} as FLAC")
            tags["TITLE"] = title
            if artist:
                tags["ARTIST"] = artist
            if album:
                tags["ALBUM"] = album
            if year > 0:
                tags["DATE"] = str(year)
            if genre and genre != "Unknown":
                tags["GENRE"] = genre
            if track_number > 0:
                tags["TRACKNUMBER"] = str(track_number)
            tags.save()

        elif ext == ".opus":
            tags = mutagen.oggopus.OggOpus(str(file_path))
            tags["TITLE"] = title
            if artist:
                tags["ARTIST"] = artist
            if album:
                tags["ALBUM"] = album
            if year > 0:
                tags["DATE"] = str(year)
            if genre and genre != "Unknown":
                tags["GENRE"] = genre
            if track_number > 0:
                tags["TRACKNUMBER"] = str(track_number)
            tags.save()

        return True

    except Exception as e:
        logger.debug(f"Could not write tags to {file_path}: {e}")
        return False


# ─── Metadata Resolution by Song Name ─────────────────────────────────

def _get_ytmusic() -> ytmusicapi.YTMusic:
    """Get or create the shared YTMusic instance."""
    import downloader
    return downloader.get_ytmusic()


def resolve_by_name(artist: str, title: str) -> dict[str, Any] | None:
    """Search YouTube Music by artist + song name and fetch metadata from the first match.

    Builds a search query like "Artist - Title" or just "Title", searches YouTube Music,
    takes the first song result, and returns its full metadata (title, artist, year, genre).

    Returns a dict with keys: title, artist, year, genre — or None if no match found.
    """
    try:
        ytm = _get_ytmusic()

        # Build search query from available info
        clean_artist = artist.strip() if artist else ""
        clean_title = title.strip() if title else ""

        if clean_artist and clean_title:
            query = f"{clean_artist} {clean_title}"
        elif clean_title:
            query = clean_title
        else:
            return None

        # Search YouTube Music
        results = ytm.search(query, limit=10)  # type: ignore[arg-type]
        if not results:
            logger.debug(f"No search results for '{query}'")
            return None

        # Take the first song result
        for r in results:
            if r.get("resultType") != "song":
                continue

            video_id = r.get("videoId", "")
            if not video_id:
                continue

            logger.info(f"Matched by name '{query}' → {r.get('title', '')} (ID: {video_id})")

            # Fetch full metadata from the matched song
            return _fetch_metadata(video_id)

        logger.debug(f"No song results in search for '{query}'")
        return None

    except Exception as e:
        logger.debug(f"Search failed for '{artist} - {title}': {e}")
        return None


def _fetch_metadata(video_id: str) -> dict[str, Any] | None:
    """Fetch full metadata from YouTube Music API using a video ID.

    Returns a dict with keys: title, artist, year, genre — or None on failure.
    """
    try:
        ytm = _get_ytmusic()
        song_data = ytm.get_song(video_id)

        if not song_data:
            return None

        video_details = song_data.get("videoDetails", {})
        title = video_details.get("title", "")
        artist = video_details.get("author", "").replace(" - Topic", "").strip()

        # Extract year from publishDate in microformat
        year = 0
        try:
            microformat = song_data.get("microformat", {})
            vmr = microformat.get("microformatDataRenderer", {})
            publish_date = vmr.get("publishDate", "")
            if publish_date and len(publish_date) >= 4:
                year = int(publish_date[:4])
        except (ValueError, IndexError):
            pass

        # Infer genre from video tags
        genre = "Unknown"
        video_tags = video_details.get("tags", [])
        if video_tags:
            try:
                from api_client import YouTubeAPIClient
                genre = YouTubeAPIClient.infer_genre_from_tags(
                    tags=video_tags, channel_name=artist
                )
            except Exception:
                pass

        return {
            "title": title,
            "artist": artist,
            "year": year,
            "genre": genre,
        }

    except Exception as e:
        logger.debug(f"Could not fetch metadata for video {video_id}: {e}")
        return None


# ─── Public Resolve Function ──────────────────────────────────────────

def _extract_artist_from_filename(file_path: Path) -> str:
    """Extract artist from filename using 'Artist - Title' pattern."""
    stem = file_path.stem
    parts = [p.strip() for p in stem.split(" - ", 1)]
    if len(parts) > 1 and parts[0]:
        return parts[0]
    return ""


def resolve_file_tags(file_path: Path) -> dict[str, Any]:
    """Resolve and update tags for a single audio file.

    Strategy:
      1. Extract title/artist from filename ('Artist - Title') — avoids Windows
         file-handle conflicts that occur when mutagen keeps files open.
      2. Search YouTube Music by song name to find matching metadata.
      3. Overwrite all ID3 tags with fresh data from the match.

    Returns a dict with: success (bool), changes (list of field names changed).
    """
    result = {"success": False, "changes": []}

    if not file_path.exists():
        logger.warning(f"File not found: {file_path}")
        return result

    # Extract title and artist from filename (e.g. 'Artist - Title.flac')
    stem = file_path.stem
    parts = [p.strip() for p in stem.split(" - ", 1)]
    old_artist = parts[0] if len(parts) > 1 and parts[0] else ""
    old_title = parts[-1] if len(parts) > 1 else stem

    # Search YouTube Music by song name (artist + title for accuracy)
    metadata = resolve_by_name(
        artist=old_artist,
        title=old_title,
    )

    if not (metadata and metadata.get("title")):
        logger.warning(f"No matching metadata found for {file_path.name}")
        return result

    # Write updated tags
    new_title = metadata.get("title", "")
    new_artist = metadata.get("artist", "") or ""
    new_year = metadata.get("year", 0)
    new_genre = metadata.get("genre", "Unknown") or "Unknown"

    write_tags(
        file_path=file_path,
        title=sanitize_filename(new_title),
        artist=sanitize_filename(new_artist),
        year=new_year,
        genre=new_genre,
    )

    # Track which fields changed
    if new_title != old_title:
        result["changes"].append("title")
    if new_artist and new_artist != old_artist:
        result["changes"].append("artist")
    if new_year:
        result["changes"].append("year")
    if new_genre and new_genre != "Unknown":
        result["changes"].append("genre")

    result["success"] = True
    return result


# ─── Batch Resolution ─────────────────────────────────────────────────

def find_audio_files(directory: Path) -> list[Path]:
    """Recursively find all supported audio files in a directory."""
    files = []
    if not directory.exists():
        return files

    for ext in _AUDIO_EXTENSIONS:
        files.extend(directory.rglob(f"*{ext}"))

    # Sort by path for deterministic ordering
    files.sort()
    return files


def resolve_all_files(
    directory: Path,
) -> dict[str, Any]:
    """Resolve tags for all audio files in a directory tree.

    Processes files sequentially (to avoid overwhelming the API).
    Returns a summary dict with counts and per-file results.
    """
    files = find_audio_files(directory)
    total = len(files)

    if total == 0:
        return {"total": 0, "success": 0, "failed": 0, "results": []}

    logger.info(f"Found {total} audio file(s) in {directory}")

    results_list: list[dict[str, Any]] = []
    success_count = 0
    failed_count = 0

    for idx, file_path in enumerate(files, 1):
        rel_path = file_path.relative_to(directory)
        logger.info(f"[{idx}/{total}] Resolving: {rel_path}")

        file_result = resolve_file_tags(file_path)

        entry = {
            "file": str(rel_path),
            "success": file_result["success"],
            "changes": file_result.get("changes", []),
        }
        results_list.append(entry)

        if file_result["success"]:
            success_count += 1
        else:
            failed_count += 1

    return {
        "total": total,
        "success": success_count,
        "failed": failed_count,
        "results": results_list,
    }


# ─── Helpers ──────────────────────────────────────────────────────────

def _first(lst: list[str]) -> str:
    """Return the first element of a list, or empty string."""
    return lst[0] if lst else ""
