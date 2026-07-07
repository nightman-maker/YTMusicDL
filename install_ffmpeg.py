#!/usr/bin/env python3
"""Auto-download ffmpeg into the project directory (Windows).

Usage:
    python install_ffmpeg.py

Downloads the latest shared build of ffmpeg from gyan.dev and places
ffmpeg.exe, ffprobe.exe, and related binaries in this folder.
"""

import os
import sys
import urllib.request
import zipfile
import tempfile
import shutil

FFMPEG_URL = (
    "https://www.gyan.dev/ffmpeg/builds/"
    "ffmpeg-release-essentials.zip"
)

DEST_DIR = os.path.dirname(os.path.abspath(__file__))


def download_file(url: str, dest_path: str) -> None:
    """Download a file from *url* to *dest_path*, showing progress."""
    print(f"Downloading {url} ...")
    urllib.request.urlretrieve(url, dest_path, reporthook=_progress)
    print()


def _progress(block_num: int, block_size: int, total_size: int) -> None:
    downloaded = block_num * block_size
    if total_size > 0:
        pct = min(downloaded / total_size * 100, 100)
        mb = downloaded / (1024 * 1024)
        print(f"\r  {mb:.1f} MB ({pct:.0f}%)", end="", flush=True)


def extract_ffmpeg(zip_path: str) -> None:
    """Extract ffmpeg binaries from the zip into DEST_DIR."""
    print("Extracting ffmpeg ...")
    with zipfile.ZipFile(zip_path, "r") as zf:
        # Find files that are directly in the top-level folder (not nested)
        for info in zf.infolist():
            name = info.filename
            # Skip directories and non-binary files
            if name.endswith("/") or not name.endswith(".exe"):
                continue
            # The zip usually has a prefix like ffmpeg-7.1.1-essentials_build/
            parts = name.split("/")
            if len(parts) >= 2:
                basename = parts[-1]  # e.g. ffmpeg.exe, ffprobe.exe
                dest = os.path.join(DEST_DIR, basename)
                with zf.open(info) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                print(f"  Extracted {basename}")


def main() -> None:
    if sys.platform != "win32":
        print("This script is intended for Windows only.")
        print(
            "On macOS run: brew install ffmpeg\n"
            "On Linux run: sudo apt install ffmpeg (Debian/Ubuntu)"
        )
        sys.exit(1)

    # Check if ffmpeg already exists
    existing = os.path.join(DEST_DIR, "ffmpeg.exe")
    if os.path.exists(existing):
        print(f"ffmpeg.exe already exists at {existing}")
        resp = input("Overwrite? [y/N]: ").strip().lower()
        if resp != "y":
            print("Aborted.")
            sys.exit(0)

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        download_file(FFMPEG_URL, tmp_path)
        extract_ffmpeg(tmp_path)
        print("\nDone! ffmpeg is now available in this directory.")
        print("Make sure this folder is on your PATH, or run:")
        print(f'  setx PATH "%PATH%;{DEST_DIR}"')
    except Exception as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        os.unlink(tmp_path)


if __name__ == "__main__":
    main()
