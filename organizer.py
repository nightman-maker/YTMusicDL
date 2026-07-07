"""
File organizer module.
Moves downloaded files into Year/Genre folder structure.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def organize_file(
    source_path: Path,
    year: int,
    genre: str,
    artist: str,
    title: str,
    output_base: Path,
) -> Path | None:
    """
    Move a downloaded file into the organized folder structure.

    Structure: output_base/Year/Genre/Artist - Title.ext

    Returns the final path if successful, None on failure.
    """

    safe_year = year if year > 0 else "Various"
    safe_genre = genre.replace("/", "-").replace("\\", "-") or "Unknown"
    safe_artist = _sanitize_path_part(artist)
    safe_title = _sanitize_path_part(title)

    # Build target path: Year/Genre/Artist - Title.ext
    year_dir = output_base / str(safe_year)
    genre_dir = year_dir / safe_genre
    genre_dir.mkdir(parents=True, exist_ok=True)

    stem = source_path.stem
    suffix = source_path.suffix
    final_name = f"{safe_artist} - {safe_title}{suffix}" if safe_artist != "Unknown" else f"{safe_title}{suffix}"

    target_path = genre_dir / final_name

    # Handle filename collisions
    counter = 1
    while target_path.exists():
        alt_name = f"{safe_artist} - {safe_title} ({counter}){suffix}" if safe_artist != "Unknown" else f"{safe_title} ({counter}){suffix}"
        target_path = genre_dir / alt_name
        counter += 1

    try:
        source_path.rename(target_path)
        logger.info(
            f"Organized: {source_path.name} → {target_path.relative_to(output_base)}"
        )
        return target_path
    except Exception as e:
        logger.error(f"Failed to organize '{source_path.name}': {e}")
        return None


def _sanitize_path_part(name: str, max_len: int = 100) -> str:
    """Sanitize a string for use in a file/folder path."""
    import re

    # Remove invalid characters for filesystem paths
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = re.sub(r'\s+', " ", name).strip()

    if len(name) > max_len:
        name = name[:max_len]

    return name or "Unknown"


def organize_batch(
    results: list,  # DownloadResult objects with metadata
    output_base: Path,
) -> dict[str, int]:
    """
    Organize all downloaded files into the Year/Genre structure.

    Returns a summary dict of {genre: count} per year.
    """
    from downloader import DownloadResult

    summary = {}

    for result in results:
        if not result.success or not result.file_path:
            continue

        metadata = getattr(result, "_metadata", {})
        year = metadata.get("year", 0)
        genre = metadata.get("genre", "Unknown")
        artist = metadata.get("artist", "")
        title = metadata.get("title", "")

        final_path = organize_file(
            source_path=result.file_path,
            year=year,
            genre=genre,
            artist=artist,
            title=title,
            output_base=output_base,
        )

        if final_path:
            genre_key = f"{year}/{genre}" if year > 0 else f"Various/{genre}"
            summary[genre_key] = summary.get(genre_key, 0) + 1

    return summary


def print_organization_summary(summary: dict[str, int]) -> None:
    """Print a formatted summary of the organization results."""
    if not summary:
        print("No files were organized.")
        return

    print("\nOrganization Summary:")
    print("-" * 40)

    # Group by year
    years = {}
    for key, count in sorted(summary.items()):
        parts = key.split("/", 1)
        year = parts[0]
        genre = parts[1] if len(parts) > 1 else "Unknown"
        if year not in years:
            years[year] = {}
        years[year][genre] = count

    for year, genres in sorted(years.items()):
        total = sum(genres.values())
        print(f"\n  {year} ({total} tracks):")
        for genre, count in sorted(genres.items()):
            print(f"     - {genre}: {count}")
