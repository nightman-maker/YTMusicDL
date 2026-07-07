"""
Utility functions and helpers.
"""

import asyncio
import logging
import re
from pathlib import Path


def setup_logging(level: str = "INFO") -> None:
    """Configure application logging."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def extract_video_id(input_str: str) -> str | None:
    """Extract YouTube video ID from various URL formats or raw ID."""

    # Raw 11-char video ID
    if re.match(r'^[a-zA-Z0-9_-]{11}$', input_str.strip()):
        return input_str.strip()

    # Various YouTube URL patterns
    patterns = [
        r'(?:youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]{11})',
        r'music\.youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})',
        r'youtube\.com/playlist\?list=([a-zA-Z0-9_-]+)',
        r'youtube\.com/embed/([a-zA-Z0-9_-]{11})',
    ]

    for pattern in patterns:
        match = re.search(pattern, input_str)
        if match:
            return match.group(1)

    return None


def extract_playlist_id(input_str: str) -> str | None:
    """Extract playlist ID from URL."""
    match = re.search(r'playlist\?list=([a-zA-Z0-9_-]+)', input_str)
    if match:
        return match.group(1)

    # Also accept raw playlist IDs (starts with PL, ULP, etc.)
    if re.match(r'^[a-zA-Z0-9_-]{12,}$', input_str.strip()):
        return input_str.strip()

    return None


def is_playlist_url(input_str: str) -> bool:
    """Check if the input string contains a playlist URL."""
    return "playlist" in input_str.lower() or "list=" in input_str.lower()


async def async_retry(
    func, *args, max_retries: int = 3, base_delay: float = 1.0, **kwargs
):
    """Execute an async function with exponential backoff retry."""
    last_error = None

    for attempt in range(max_retries):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                logging.warning(
                    f"Attempt {attempt + 1}/{max_retries} failed: {e}. "
                    f"Retrying in {delay:.1f}s..."
                )
                await asyncio.sleep(delay)

    raise last_error


def format_duration(seconds: int) -> str:
    """Format seconds into HH:MM:SS or MM:SS."""
    if seconds < 3600:
        mins, secs = divmod(seconds, 60)
        return f"{mins:02d}:{secs:02d}"
    hours, remainder = divmod(seconds, 3600)
    mins, secs = divmod(remainder, 60)
    return f"{hours:02d}:{mins:02d}:{secs:02d}"


def count_files_recursive(directory: Path) -> int:
    """Count total files in a directory tree."""
    if not directory.exists():
        return 0
    return sum(1 for _ in directory.rglob("*") if _.is_file())


def get_directory_size(directory: Path) -> str:
    """Get human-readable directory size."""
    import os

    total = 0
    if directory.exists():
        for path in directory.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    pass

    # Convert to human readable
    for unit in ["B", "KB", "MB", "GB"]:
        if total < 1024:
            return f"{total:.1f} {unit}"
        total /= 1024
    return f"{total:.1f} TB"
