# 🎵 YouTube Music Downloader

Download songs and playlists from YouTube Music at **maximum audio quality**, organized into a clean `Year/Genre` folder structure.

## Features

- **Max Quality Audio**: Downloads best available audio streams and transcodes to your choice of format:
  - **MP3** (320kbps CBR) — default, universal compatibility
  - **FLAC** (lossless) — bit-perfect archival quality
  - **Opus** (160kbps) — efficient open-source codec
- **Smart Metadata Extraction**: Uses YouTube Music API for metadata — **FREE, no API quota needed** for title, artist, duration, year
- **Minimal API Usage**: Google Data API is used only as a fallback. Batch lookups cost just 1 unit per 50 videos
- **Year/Genre Organization**: Files are automatically organized into `downloads/{Year}/{Genre}/Artist - Title.ext`
- **Playlist Support**: Download entire playlists with concurrent downloads (configurable concurrency)
- **Search & Pick**: Search for songs and pick from results interactively
- **Rate Limiting**: Built-in retry logic and connection throttling to avoid YouTube rate limits
- **Metadata Embedding**: ID3 tags embedded in audio files (title, artist, album, year, genre, track number)
- **Deduplication**: Automatically skips songs that already exist in the output directory

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                    Input Layer                        │
│  URLs / Playlist IDs / Text search queries            │
└──────────────────┬───────────────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────────────┐
│    Metadata Extraction (PRIMARY: ytmusicapi)         │
│                                                      │
│  ✓ Title, Artist, Duration                           │
│  ✓ Year from publish date                            │
│  ✓ Genre inference                                   │
│  ✗ No API quota — FREE                              │
└──────────────────┬───────────────────────────────────┘
                   │ (only if incomplete)
                   ▼
┌──────────────────────────────────────────────────────┐
│     Metadata Enrichment (FALLBACK: YouTube API)      │
│                                                      │
│  • videos.list batch: 50 IDs = 1 unit               │
│  • playlistItems.list: 1 unit per page              │
│  • search.list: 100 units per call                  │
└──────────────────┬───────────────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────────────┐
│       URL Extraction (yt-dlp — signature only)       │
│                                                      │
│  yt-dlp handles YouTube's signature encryption        │
│  Returns direct audio stream URLs                    │
│  No downloading or transcoding by yt-dlp             │
└──────────────────┬───────────────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────────────┐
│           Audio Download (requests + ffmpeg)         │
│                                                      │
│  • Direct HTTP download from extracted URLs          │
│  • Transcode to MP3/FLAC/Opus via ffmpeg             │
│  • Retry logic with exponential backoff              │
│  • Concurrent downloads with semaphore limiting      │
└──────────────────┬───────────────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────────────┐
│           Tagging & Organization                     │
│                                                      │
│  ID3 tags: title, artist, album, year, genre         │
│  File structure:                                     │
│    downloads/2024/Rock/Artist - Title.mp3            │
│    downloads/2024/Pop/Artist - Title.flac            │
│    downloads/Various/Unknown/Artist - Title.opus     │
└──────────────────────────────────────────────────────┘
```

## Prerequisites

### 1. Python 3.10+

Ensure Python is installed and accessible from the command line:

```bash
python --version   # Should show 3.10 or higher
```

### 2. ffmpeg (Required)

ffmpeg is required for audio transcoding (MP3/FLAC conversion). Download it from [https://ffmpeg.org/download.html](https://ffmpeg.org/download.html):

**Windows:**
- Download the "Shared" build from [GitHub releases](https://www.gyan.dev/ffmpeg/builds/)
- Extract `ffmpeg.exe` and place it in a folder on your PATH (e.g., `C:\Windows\`)
- Or run: `python install_ffmpeg.py` to auto-download into the project directory

**macOS:**
```bash
brew install ffmpeg
```

**Linux:**
```bash
sudo apt install ffmpeg    # Debian/Ubuntu
sudo dnf install ffmpeg    # Fedora
sudo pacman -S ffmpeg      # Arch
```

Verify installation:
```bash
ffmpeg -version
```

### 3. Node.js (for yt-dlp URL extraction)

yt-dlp needs a JavaScript runtime to decrypt YouTube's signature encryption. Node.js is required:

- Download from [https://nodejs.org](https://nodejs.org) (LTS version recommended)
- Verify installation:
```bash
node --version   # Should show v18+ or higher
```

### 4. Google API Key (Optional but Recommended)

A Google Cloud API key enables playlist extraction and metadata enrichment as a fallback. It's **not required** for basic single-track downloads, which work entirely through the free YouTube Music API.

To get an API key:
1. Go to [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
2. Create a project or select an existing one
3. Enable "YouTube Data API v3"
4. Create an API key
5. (Optional) Set up daily quota limits

## Installation

```bash
# Clone or copy the project
cd YTMusicDL

# Install dependencies
pip install -r requirements.txt

# Configure environment variables (optional — only needed for playlist downloads)
cp .env.example .env
# Edit .env and add your GOOGLE_API_KEY if you have one
```

## Usage

### Download a Single Track

```bash
# By URL
python main.py https://music.youtube.com/watch?v=dQw4w9WgXcQ

# By video ID
python main.py dQw4w9WgXcQ

# With custom output directory and codec
python main.py dQw4w9WgXcQ -o ./my-music --codec opus
```

### Download a Playlist

```bash
# By playlist URL
python main.py --playlist PLxxxxxxxxxxxxxxx

# By playlist ID (raw)
python main.py --playlist PLxxxxxxxxxxxxxxx

# With concurrency control
python main.py --playlist PLxxxxxxxxxxxxxxx -c 4
```

> **Note:** Playlist downloads require a `GOOGLE_API_KEY` in your `.env` file. Single-track downloads work without it.

### Search and Pick

```bash
# Search for a song, pick from results
python main.py --search "Daft Punk Get Lucky"

# Show more search results to choose from
python main.py --search "Radiohead Creep" -n 10
```

### Dry Run (Preview)

```bash
# See what would be downloaded without actually downloading
python main.py dQw4w9WgXcQ --dry-run
python main.py --playlist PLxxxxxxxxxxxxxxx --dry-run
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_API_KEY` | *(optional)* | YouTube Data API v3 key (needed for playlist downloads) |
| `OUTPUT_DIR` | `./downloads` | Where downloaded music is stored |
| `MAX_CONCURRENT_DOWNLOADS` | `2` | Max simultaneous downloads |
| `API_REQUESTS_PER_MINUTE` | `30` | Rate limit for API calls |

### Environment Variables

Create a `.env` file:

```env
GOOGLE_API_KEY=AIzaSy...your_key_here
OUTPUT_DIR=./downloads
MAX_CONCURRENT_DOWNLOADS=2
API_REQUESTS_PER_MINUTE=30
```

Or set them directly in your shell:

```bash
# Windows (Command Prompt)
set GOOGLE_API_KEY=AIzaSy...

# Windows (PowerShell)
$env:GOOGLE_API_KEY="AIzaSy..."

# macOS/Linux
export GOOGLE_API_KEY="AIzaSy..."

python main.py <url>
```

## API Quota Management

YouTube Data API v3 has a default quota of **10,000 units/day**. This tool minimizes usage:

| Operation | Cost | When Used |
|-----------|------|-----------|
| ytmusicapi metadata extraction | **0** (FREE) | Always — primary source |
| yt-dlp URL extraction | **0** (FREE) | Always — no API calls |
| `videos.list` batch (50 IDs) | 1 unit | Only when metadata is incomplete |
| `playlistItems.list` per page | 1 unit | When downloading playlists |
| `search.list` | 100 units | Only for search queries |

### Example Quota Usage

- **Single track** (ytmusicapi metadata complete): **~0 units**
- **Single track** (API fallback needed): **~2 units**
- **Playlist of 50 tracks**: **~3 units** (1 for playlist + 1 batch API)
- **Search + download**: **~102 units**

At this rate, you can process **thousands of videos per day** without hitting quota limits.

## Folder Structure

Downloaded files are organized as:

```
downloads/
├── 2024/
│   ├── Rock/
│   │   ├── Queen - Bohemian Rhapsody.mp3
│   │   └── Led Zeppelin - Stairway to Heaven.flac
│   ├── Pop/
│   │   ├── Daft Punk - Get Lucky.mp3
│   │   └── The Weeknd - Blinding Lights.opus
│   └── Electronic/
│       └── Deadmau5 - Strobe.mp3
├── 2023/
│   └── Hip-Hop/
│       └── Kendrick Lamar - HUMBLE..mp3
└── Various/
    └── Unknown/
        └── Artist - Title.mp3
```

## CLI Reference

```
usage: yt-music-downloader [-h] [--playlist PLAYLIST] [--search SEARCH]
                           [--output-dir OUTPUT_DIR] [--codec {mp3,flac,opus}]
                           [--max-results MAX_RESULTS] [--concurrent CONCURRENT]
                           [--dry-run] [--quiet]
                           [input]

positional arguments:
  input                 YouTube Music URL, video ID, or playlist link

options:
  -h, --help            show this help message and exit
  -p, --playlist PLAYLIST
                        Download all tracks from a YouTube playlist
  -s, --search SEARCH   Search for a song and pick from results
  -o, --output-dir OUTPUT_DIR
                        Output directory (default: ./downloads)
  --codec {mp3,flac,opus}
                        Audio codec for output (default: mp3, 320kbps)
  -n, --max-results MAX_RESULTS
                        Max search results (default: 5)
  -c, --concurrent CONCURRENT
                        Max concurrent downloads
  --dry-run             Show what would be downloaded without downloading
  -q, --quiet           Suppress progress output
```

## Codec Comparison

| Codec | Quality | File Size | Compatibility | Use Case |
|-------|---------|-----------|---------------|----------|
| **MP3** (default) | 320kbps CBR | Medium | Universal | Everyday listening, all devices |
| **FLAC** | Lossless | Large | Most modern players | Archival quality, audiophiles |
| **Opus** | ~160kbps VBR | Small | Modern browsers/apps | Streaming, storage-constrained |

## Error Handling

- **Partial file cleanup**: Failed downloads automatically clean up partial files
- **Retry logic**: Downloads retry up to 3 times with exponential backoff (5s → 10s → max 30s)
- **Connection throttling**: 2-second delay between concurrent downloads to avoid YouTube rate limits
- **Unavailable videos**: Skipped gracefully during playlist downloads, reported in summary
- **Deduplication**: Automatically skips songs that already exist in the output directory
- **Graceful interruption**: Ctrl+C cleans up and saves progress

## Troubleshooting

### "ffmpeg not found" error
Install ffmpeg and ensure it's on your PATH. On Windows, place `ffmpeg.exe` in `C:\Windows\` or run `python install_ffmpeg.py`.

### "No supported JavaScript runtime could be found"
Install Node.js from [https://nodejs.org](https://nodejs.org). yt-dlp needs it to decrypt YouTube's signature encryption.

### "Video unavailable" errors
The video may have been removed or is region-restricted. The downloader will skip these and continue with other tracks in a playlist.

### "Connection broken: ConnectionResetError"
YouTube is rate-limiting your IP. The tool automatically retries with backoff, but you can also reduce `MAX_CONCURRENT_DOWNLOADS` to 1 for slower, more reliable downloads.

## License

MIT License — see [LICENSE](LICENSE) file for details.
