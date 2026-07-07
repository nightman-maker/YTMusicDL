"""
Configuration module for YouTube Music Downloader.
Loads settings from .env file and environment variables with sensible defaults.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env file into environment (if it exists)
load_dotenv()


@dataclass
class Config:
    """Application configuration."""

    # Google API Key (required)
    api_key: str = ""

    # Output directory
    output_dir: Path = field(default_factory=lambda: Path("./downloads"))

    # yt-dlp settings for max quality audio
    preferred_audio_codec: str = "mp3"  # mp3 (320kbps), flac (lossless), or opus
    preferred_audio_quality: int = 0     # 0 = best (VBR), 9 = worst
    postprocessor_args: dict = field(default_factory=lambda: {
        "ffmpeg": [
            "-map", "0:v",
            "-map", "0:a",
            "-b:a", "320k",
            "-vn",
        ],
    })

    # Rate limiting for YouTube API (units per minute)
    api_requests_per_minute: int = 30

    # Max concurrent downloads
    max_concurrent_downloads: int = 2

    # Retry settings
    max_retries: int = 3
    retry_delay_base: float = 1.0  # seconds, exponential backoff base

    # Metadata fields to fetch from YouTube API (costs quota)
    api_fields: str = "snippet,contentDetails,status"

    # Dry run mode (preview without downloading)
    dry_run: bool = False

    # Default values when metadata is missing
    default_year: int = 1900
    default_genre: str = "Unknown"
    default_artist: str = "Unknown Artist"

    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration from .env file and OS environment variables.

        OS environment variables take precedence over .env values.
        """
        api_key = os.getenv("GOOGLE_API_KEY", "")
        if not api_key:
            raise ValueError(
                "GOOGLE_API_KEY is required. Set it in .env file or as an environment variable.\n"
                "Get one at: https://console.cloud.google.com/apis/credentials"
            )

        output_dir = Path(os.getenv("OUTPUT_DIR", "./downloads"))
        max_concurrent = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2"))
        api_rpm = int(os.getenv("API_REQUESTS_PER_MINUTE", "30"))

        return cls(
            api_key=api_key,
            output_dir=output_dir.resolve(),
            max_concurrent_downloads=max_concurrent,
            api_requests_per_minute=api_rpm,
        )

    def ensure_dirs(self) -> None:
        """Create output directory if it doesn't exist."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
