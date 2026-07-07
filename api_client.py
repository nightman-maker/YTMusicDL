"""
YouTube Data API v3 client with rate limiting and quota management.

IMPORTANT: We use the YouTube API sparingly as a fallback only.
yt-dlp extracts most metadata (title, tags, upload_date) for free.
API is used only when yt-dlp data is insufficient.

Quota costs:
  - videos.list (up to 50 IDs): 1 unit TOTAL per batch
  - search.list:               100 units per call
  - playlistItems.list:        1 unit per page
"""

import time
from dataclasses import dataclass, field
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


@dataclass
class QuotaTracker:
    """Tracks API quota usage to stay within safe limits."""

    requests_per_minute: int = 30
    _timestamps: list[float] = field(default_factory=list, repr=False)
    _total_units_used: int = 0
    _max_daily_units: int = 10_000  # YouTube default quota

    def can_make_request(self) -> bool:
        """Check if we can make another API request without exceeding rate limits."""
        now = time.monotonic()
        self._timestamps = [t for t in self._timestamps if now - t < 60]

        if len(self._timestamps) >= self.requests_per_minute:
            return False

        # Stay at 80% of daily quota to be safe
        if self._total_units_used >= int(self._max_daily_units * 0.8):
            return False

        return True

    def record_request(self, units: int = 1) -> None:
        """Record that an API request was made."""
        self._timestamps.append(time.monotonic())
        self._total_units_used += units

    def wait_for_slot(self) -> None:
        """Block until an API request slot is available (sync version)."""
        while not self.can_make_request():
            time.sleep(1.0)

    @property
    def remaining_daily_quota(self) -> int:
        return max(0, int(self._max_daily_units * 0.8) - self._total_units_used)


class YouTubeAPIClient:
    """Client for YouTube Data API v3 with rate limiting."""

    def __init__(self, api_key: str, requests_per_minute: int = 30):
        self.api_key = api_key
        self.quota = QuotaTracker(requests_per_minute=requests_per_minute)
        self._service = build("youtube", "v3", developerKey=api_key)

    def get_video_metadata(self, video_ids: list[str]) -> dict[str, dict[str, Any]]:
        """
        Get detailed metadata for multiple videos in batch.
        Costs only 1 unit per batch (up to 50 IDs).

        Returns a dict mapping video_id -> metadata.
        """
        if not video_ids:
            return {}

        results = {}
        batch_size = 50

        for i in range(0, len(video_ids), batch_size):
            chunk = video_ids[i : i + batch_size]
            self.quota.wait_for_slot()
            self.quota.record_request(1)  # 1 unit per batch of up to 50 IDs

            try:
                response = (
                    self._service.videos()
                    .list(part="snippet,contentDetails,status", id=",".join(chunk))
                    .execute()
                )

                for item in response.get("items", []):
                    video_id = item["id"]
                    snippet = item.get("snippet", {})
                    content_details = item.get("contentDetails", {})
                    status_info = item.get("status", {})

                    results[video_id] = {
                        "title": snippet.get("title", ""),
                        "channel": snippet.get("channelTitle", ""),
                        "description": snippet.get("description", ""),
                        "published_at": snippet.get("publishedAt", ""),
                        "thumbnail": snippet.get("thumbnails", {}).get(
                            "high", {}
                        ).get("url", ""),
                        "duration_iso": content_details.get("duration", ""),
                        "is_private": status_info.get("privacyStatus") == "private",
                    }

            except HttpError as e:
                if e.resp.status in (403, 429):
                    # Rate limited or quota exceeded — wait and retry once
                    self.quota.wait_for_slot()
                    time.sleep(5)
                    response = (
                        self._service.videos()
                        .list(part="snippet,contentDetails,status", id=",".join(chunk))
                        .execute()
                    )
                    for item in response.get("items", []):
                        video_id = item["id"]
                        snippet = item.get("snippet", {})
                        content_details = item.get("contentDetails", {})
                        results[video_id] = {
                            "title": snippet.get("title", ""),
                            "channel": snippet.get("channelTitle", ""),
                            "description": snippet.get("description", ""),
                            "published_at": snippet.get("publishedAt", ""),
                            "thumbnail": snippet.get("thumbnails", {}).get(
                                "high", {}
                            ).get("url", ""),
                            "duration_iso": content_details.get("duration", ""),
                            "is_private": False,
                        }
                else:
                    raise

        return results

    def get_playlist_videos(self, playlist_id: str) -> list[dict[str, Any]]:
        """
        Get all videos from a YouTube playlist.
        Costs 1 unit per page (typically ~50 videos per page).
        """
        self.quota.wait_for_slot()
        self.quota.record_request(1)

        items = []
        next_page_token = None

        while True:
            response = (
                self._service.playlistItems()
                .list(
                    part="snippet,contentDetails",
                    playlistId=playlist_id,
                    maxResults=50,
                    pageToken=next_page_token,
                )
                .execute()
            )

            for item in response.get("items", []):
                content = item["contentDetails"]
                snippet = item["snippet"]
                video_id = content.get("videoId")
                if not video_id:
                    continue

                items.append({
                    "video_id": video_id,
                    "title": snippet.get("title", ""),
                    "channel": snippet.get("channelTitle", ""),
                    "position": snippet.get("position", 0),
                    "published_at": snippet.get("publishedAt", ""),
                })

            next_page_token = response.get("nextPageToken")
            if not next_page_token:
                break

        return items

    def search_by_query(self, query: str, max_results: int = 25) -> list[dict[str, Any]]:
        """Search YouTube for videos matching a query. Costs ~100 quota units."""
        self.quota.wait_for_slot()
        self.quota.record_request(100)

        results = (
            self._service.search()
            .list(
                part="snippet",
                q=query,
                type="video",
                maxResults=max_results,
                videoEmbeddable="true",
                relevanceLanguage="en",
            )
            .execute()
        )

        items = []
        for item in results.get("items", []):
            snippet = item["snippet"]
            items.append({
                "video_id": item["id"]["videoId"],
                "title": snippet.get("title", ""),
                "channel": snippet.get("channelTitle", ""),
                "thumbnail": snippet.get("thumbnails", {}).get(
                    "high", {}
                ).get("url", ""),
                "published_at": snippet.get("publishedAt", ""),
            })

        return items

    @staticmethod
    def extract_year_from_published(published_at: str) -> int:
        """Extract year from ISO 8601 date string."""
        if not published_at:
            return 0
        try:
            return int(published_at[:4])
        except (ValueError, IndexError):
            return 0

    @staticmethod
    def infer_genre_from_tags(
        tags: list[str] | None, channel_name: str = ""
    ) -> str:
        """Infer genre from YouTube video tags and/or channel name."""
        if not tags and not channel_name:
            return "Unknown"

        combined = f"{' '.join(tags or [])} {channel_name}".lower()

        genre_keywords = {
            "Pop": ["pop", "dance pop", "electropop", "indie pop"],
            "Rock": ["rock", "alternative rock", "indie rock", "classic rock",
                     "soft rock", "progressive rock", "psychedelic rock"],
            "Hip-Hop": ["hip hop", "rap", "trap", "hiphop", "drill",
                        "conscious rap", "mumble rap"],
            "R&B": ["r&b", "rnb", "soul", "funk", "neo soul", "contemporary r&b"],
            "Electronic": ["edm", "electronic", "house", "techno", "trance",
                           "dubstep", "drum and bass", "ambient", "chill",
                           "lo-fi", "synthwave", "future bass", "garage"],
            "Jazz": ["jazz", "smooth jazz", "bebop", "fusion", "latin jazz"],
            "Classical": ["classical", "orchestra", "symphony", "baroque",
                          "contemporary classical"],
            "Country": ["country", "folk", "americana", "bluegrass", "alt country"],
            "Latin": ["latin", "reggaeton", "salsa", "bachata", "cumbia",
                      "latin pop", "latin trap"],
            "Metal": ["metal", "heavy metal", "death metal", "black metal",
                      "nu metal", "metalcore", "djent"],
            "Punk": ["punk", "hardcore punk", "ska", "pop punk", "emo"],
            "Blues": ["blues", "delta blues", "electric blues", "blues rock"],
            "Reggae": ["reggae", "dub", "dancehall", "roots reggae"],
            "K-Pop": ["k-pop", "kpop", "korean pop"],
            "Indie": ["indie", "indie folk", "indie electronic", "bedroom pop"],
        }

        for genre, keywords in genre_keywords.items():
            if any(kw in combined for kw in keywords):
                return genre

        return "Unknown"
