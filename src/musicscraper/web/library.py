"""Library browsing and the release snapshot shared by HTTP requests and tasks."""

import hashlib
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from musicscraper.config import Config


def browse_library(subpath: str = "") -> Dict[str, Any]:
    """Lists folders and audio files in the music library."""
    from musicscraper.core.constants import AUDIO_EXTENSIONS
    base_dir = Config.DEFAULT_LIBRARY_DIR.resolve()
    clean_subpath = (subpath or "").lstrip("/\\")
    target = (base_dir / clean_subpath).resolve()

    if target != base_dir and base_dir not in target.parents:
        target = base_dir

    if not target.exists() or not target.is_dir():
        return {"current_path": str(target), "exists": False, "directories": [], "files": []}

    directories = []
    files = []

    try:
        for entry in sorted(os.scandir(target), key=lambda e: e.name.lower()):
            if entry.name.startswith("."):
                continue
            entry_path = Path(entry.path).resolve()
            if entry.is_dir():
                directories.append({
                    "name": entry.name,
                    "rel_path": str(entry_path.relative_to(base_dir))
                })
            elif entry.is_file():
                ext = Path(entry.name).suffix.lower()
                if ext in AUDIO_EXTENSIONS:
                    stat = entry.stat()
                    files.append({
                        "name": entry.name,
                        "size": stat.st_size,
                        "rel_path": str(entry_path.relative_to(base_dir))
                    })
    except Exception as e:
        return {"current_path": str(target), "error": str(e), "directories": [], "files": []}

    return {
        "current_path": str(target),
        "rel_path": str(target.relative_to(base_dir)) if target != base_dir else "",
        "exists": True,
        "directories": directories,
        "files": files
    }


_library_releases_cache: Dict[str, Any] = {"timestamp": 0, "releases": []}


def get_library_releases(
    refresh: bool = False,
    force_disk_rescan: bool = False,
    search: str = "",
    filter_mode: str = "all"
) -> Dict[str, Any]:
    """Scans and retrieves all releases present in the music library."""
    global _library_releases_cache
    from musicscraper.services.library import LibraryReleaseService

    now = time.time()
    if refresh or not _library_releases_cache["releases"] or (now - _library_releases_cache["timestamp"] > 300):
        service = LibraryReleaseService()
        releases = service.scan_library_releases(force_rescan=force_disk_rescan)
        _library_releases_cache = {"timestamp": now, "releases": releases}

    all_releases = _library_releases_cache["releases"]

    # Compute global summary metrics
    total_releases = len(all_releases)
    complete_releases = sum(1 for r in all_releases if r.get("status") == "complete")
    has_missing_releases = sum(1 for r in all_releases if r.get("status") == "has_missing")
    total_local_tracks = sum(r.get("found_count", len(r.get("tracks", []))) for r in all_releases)
    total_missing_tracks = sum(r.get("missing_count", 0) for r in all_releases)

    # Filter releases
    filtered = list(all_releases)
    if search:
        s_lower = search.lower().strip()
        filtered = [
            r for r in filtered
            if s_lower in r.get("artist", "").lower()
            or s_lower in r.get("title", "").lower()
            or s_lower in r.get("folder_path", "").lower()
        ]

    if filter_mode == "missing":
        filtered = [r for r in filtered if r.get("status") == "has_missing" or r.get("missing_count", 0) > 0]
    elif filter_mode == "complete":
        filtered = [r for r in filtered if r.get("status") == "complete" and r.get("missing_count", 0) == 0]

    return {
        "summary": {
            "total_releases": total_releases,
            "complete_releases": complete_releases,
            "has_missing_releases": has_missing_releases,
            "total_local_tracks": total_local_tracks,
            "total_missing_tracks": total_missing_tracks,
        },
        "count": len(filtered),
        "releases": filtered
    }


def get_library_release_details(release_id: str, audit: bool = True, force_refresh: bool = False) -> Dict[str, Any]:
    """Retrieves full tracklist and metadata for a specific release, with optional MusicBrainz audit."""
    global _library_releases_cache
    from musicscraper.services.library import LibraryReleaseService

    if force_refresh or not _library_releases_cache["releases"]:
        get_library_releases(refresh=True, force_disk_rescan=False)

    matched_rel = _find_release(release_id)
    if not matched_rel and not force_refresh:
        # Fallback to fresh scan
        get_library_releases(refresh=True, force_disk_rescan=False)
        matched_rel = _find_release(release_id)

    if not matched_rel:
        raise ValueError(f"Release ID '{release_id}' not found in library.")

    if audit:
        service = LibraryReleaseService()
        audited = service.audit_release(matched_rel, force_refresh=force_refresh)
        # Update cache in place
        for idx, r in enumerate(_library_releases_cache["releases"]):
            if r.get("id") == matched_rel.get("id") or r.get("id") == release_id:
                _library_releases_cache["releases"][idx] = audited
                break
        return audited

    return matched_rel


def _find_release(release_id: str) -> Optional[Dict[str, Any]]:
    """Accept local IDs, MusicBrainz IDs, and IDs from older library snapshots."""
    for release in _library_releases_cache["releases"]:
        mbid = release.get("mb_release_id")
        legacy_id = hashlib.md5(f"mb_{mbid}".encode()).hexdigest()[:16] if mbid else None
        if release_id in (release.get("id"), mbid, legacy_id):
            return release
    return None


def cache_library_releases(releases: list) -> None:
    """Publish the latest completed scan or audit to the library endpoints."""
    global _library_releases_cache
    _library_releases_cache = {"timestamp": time.time(), "releases": releases}
