"""Background task adapters: validate inputs, report progress, and call services."""

from pathlib import Path
from typing import Any, Dict, List

from musicscraper.config import Config
from musicscraper.web.library import cache_library_releases
from musicscraper.web.tasks import BackgroundTask


def run_audit_task(task: BackgroundTask) -> Dict[str, Any]:
    """Executes artist discography audit against local library."""
    task.check_cancelled()
    from musicscraper.services.auditor import AuditorService
    artist = task.params.get("artist", "").strip()
    music_dir = Path(task.params.get("music_dir", str(Config.DEFAULT_LIBRARY_DIR)))
    full_scan = bool(task.params.get("full_scan", False))
    force_refresh = bool(task.params.get("force_refresh", False))

    if not artist:
        raise ValueError("Artist name or MBID is required for audit.")

    task.update_progress(10, f"Resolving MusicBrainz discography for '{artist}'...")
    task.check_cancelled()
    auditor = AuditorService()
    catalog, found_items, missing_items = auditor.audit_artist(
        artist_query=artist,
        music_dir=music_dir,
        full_scan=full_scan,
        force_refresh=force_refresh
    )

    task.check_cancelled()
    task.update_progress(80, "Aggregating audit metrics...")

    # Group releases with found/missing breakdowns
    release_groups: Dict[str, Dict[str, Any]] = {}
    for rel in catalog.releases:
        rid = rel.get("id") or rel.get("title")
        release_groups[rid] = {
            "id": rid,
            "title": rel.get("title", "Unknown"),
            "year": (rel.get("date") or "")[:4],
            "type": rel.get("type", "Album"),
            "total_tracks": 0,
            "found_count": 0,
            "missing_count": 0,
            "tracks": []
        }

    groups_by_title = {}
    for group in release_groups.values():
        groups_by_title.setdefault(group["title"].lower(), group)

    def find_group(track):
        release_key = track.get("release_id") or track.get("release_title")
        return release_groups.get(release_key) or groups_by_title.get(
            (track.get("release_title") or "").lower()
        )

    for trk in catalog.tracks:
        group = find_group(trk)
        if group is not None:
            group["total_tracks"] += 1

    # Populate found track details
    for f in found_items:
        mb = f.get("mb_track", {})
        matched_rg = find_group(mb)
        if matched_rg:
            matched_rg["found_count"] += 1
            matched_rg["tracks"].append({
                "title": mb.get("title"),
                "status": "found",
                "local_file": f.get("local_track", {}).get("filename"),
                "format": f.get("local_track", {}).get("format", ""),
                "bitrate": f.get("local_track", {}).get("bitrate", "")
            })

    # Populate missing track details
    for m in missing_items:
        matched_rg = find_group(m)
        if matched_rg:
            matched_rg["missing_count"] += 1
            matched_rg["tracks"].append({
                "title": m.get("title"),
                "status": "missing",
                "local_file": None
            })

    # Filter out empty release groups if any
    filtered_releases = [rg for rg in release_groups.values() if rg["total_tracks"] > 0 or rg["found_count"] > 0 or rg["missing_count"] > 0]

    total_tracks = len(catalog.tracks)
    found_count = len(found_items)
    missing_count = len(missing_items)
    completion_pct = (found_count / total_tracks * 100.0) if total_tracks > 0 else 100.0

    task.update_progress(100, f"Audit complete: {found_count}/{total_tracks} tracks ({completion_pct:.1f}%)")

    return {
        "artist": catalog.name,
        "sort_name": catalog.sort_name,
        "mbid": catalog.mbid,
        "type": catalog.artist_info.get("type", "Artist"),
        "country": catalog.artist_info.get("country", ""),
        "tags": [t.get("name") for t in catalog.artist_info.get("tag-list", []) if isinstance(t, dict)],
        "bandcamp_urls": catalog.bandcamp_urls,
        "total_tracks": total_tracks,
        "found_count": found_count,
        "missing_count": missing_count,
        "completion_pct": round(completion_pct, 1),
        "releases": filtered_releases,
        "missing_items": [{
            "title": m.get("title"),
            "release": m.get("release_title"),
            "year": m.get("release_year")
        } for m in missing_items],
    }


def run_soulseek_search_task(task: BackgroundTask) -> Dict[str, Any]:
    """Searches Soulseek via slskd for artist/album/track query."""
    task.check_cancelled()
    from musicscraper.clients.slskd import SlskdClient
    from musicscraper.core.audio import AudioQualityAnalyzer

    query = task.params.get("query", "").strip()
    timeout = float(task.params.get("timeout", 15.0))

    if not query:
        raise ValueError("Search query is required.")

    task.update_progress(20, f"Submitting Soulseek search for '{query}'...")
    task.check_cancelled()
    client = SlskdClient(timeout=timeout + 10)
    search_data = client.search(query=query, timeout=timeout)
    task.check_cancelled()

    task.update_progress(85, "Parsing discovered peer directories...")
    res = search_data.get("responses", [])

    parsed_dirs: List[Dict[str, Any]] = []
    if res and isinstance(res, list):
        for resp in res:
            task.check_cancelled()
            user = resp.get("username", "Unknown")
            files = resp.get("files", [])
            if not files:
                continue

            # Group files by folder
            folder_map: Dict[str, List[Dict[str, Any]]] = {}
            for f in files:
                full_fn = f.get("filename", "")
                parts = full_fn.replace("\\", "/").rsplit("/", 1)
                dir_name = parts[0] if len(parts) > 1 else "/"
                fn = parts[1] if len(parts) > 1 else full_fn
                f_copy = dict(f)
                f_copy["base_filename"] = fn
                f_copy["full_filename"] = full_fn
                fmt_label, fmt_score = AudioQualityAnalyzer.determine_stream_quality(f_copy)
                f_copy["fmt_label"] = fmt_label
                f_copy["fmt_score"] = fmt_score
                folder_map.setdefault(dir_name, []).append(f_copy)

            for d_name, d_files in folder_map.items():
                audio_files = [f for f in d_files if f.get("fmt_score", 0) > 0]
                if not audio_files:
                    continue
                parsed_dirs.append({
                    "user": user,
                    "dir_name": d_name,
                    "file_count": len(audio_files),
                    "total_size": sum(f.get("size", 0) for f in audio_files),
                    "files": audio_files
                })

    task.check_cancelled()
    task.update_progress(100, f"Found {len(parsed_dirs)} peer folders.")
    return {
        "query": query,
        "directories_count": len(parsed_dirs),
        "directories": parsed_dirs[:100]
    }


def run_soulseek_queue_task(task: BackgroundTask) -> Dict[str, Any]:
    """Enqueues files or directory transfers to slskd."""
    task.check_cancelled()
    from musicscraper.clients.slskd import SlskdClient
    username = task.params.get("username", "").strip()
    files = task.params.get("files", [])

    if not username or not files:
        raise ValueError("Username and files list are required to queue transfers.")

    task.update_progress(20, f"Queueing {len(files)} items from user '{username}'...")
    task.check_cancelled()
    client = SlskdClient()
    client.enqueue_download(username, files)
    task.update_progress(100, "Transfers successfully queued.")
    return {"queued_count": len(files), "username": username}


def run_artist_download_task(task: BackgroundTask) -> Dict[str, Any]:
    """Executes artist discography downloader (MusicBrainz + Soulseek)."""
    task.check_cancelled()
    from musicscraper.services.artist import ArtistDownloadOrchestrator
    artist = task.params.get("artist", "").strip()
    output_dir = Path(task.params.get("output_dir", str(Config.DEFAULT_OUTPUT_DIR)))
    library_dir = Path(task.params.get("library_dir", str(Config.DEFAULT_LIBRARY_DIR)))
    preferred_format = task.params.get("format", "flac")
    dry_run = bool(task.params.get("dry_run", False))
    use_soulseek = bool(task.params.get("use_soulseek", True))
    timeout = float(task.params.get("timeout", 25.0))

    if not artist:
        raise ValueError("Artist name is required.")

    task.update_progress(10, f"Initializing artist orchestrator for '{artist}'...")
    task.check_cancelled()
    orchestrator = ArtistDownloadOrchestrator(
        artist_query=artist,
        output_dir=output_dir,
        library_dir=library_dir,
        preferred_format=preferred_format,
        dry_run=dry_run,
        use_soulseek=use_soulseek,
        search_timeout=timeout
    )
    res = orchestrator.run()
    task.check_cancelled()
    task.update_progress(100, "Artist download orchestration completed.")
    return res

def run_library_scan_task(task: BackgroundTask) -> Dict[str, Any]:
    """Scans and caches all releases present in the music library."""
    task.check_cancelled()
    from musicscraper.services.library import LibraryReleaseService

    library_dir = Path(task.params.get("library_dir", str(Config.DEFAULT_LIBRARY_DIR)))
    force_rescan = bool(task.params.get("force_rescan", True))

    task.update_progress(10, f"Scanning audio files in library: {library_dir}...")
    task.check_cancelled()
    service = LibraryReleaseService()

    def on_prog(done, total, msg):
        task.check_cancelled()
        pct = 10 + int((done / max(1, total)) * 75)
        task.update_progress(pct, f"{msg} ({done}/{total})")

    releases = service.scan_library_releases(
        library_dir=library_dir,
        force_rescan=force_rescan,
        on_progress=on_prog
    )
    task.check_cancelled()

    cache_library_releases(releases)
    task.update_progress(100, f"Discovered and organized {len(releases)} releases in library.")
    return {
        "releases_count": len(releases),
        "complete_count": sum(1 for r in releases if r.get("status") == "complete"),
        "missing_count": sum(1 for r in releases if r.get("status") == "has_missing"),
    }


def run_release_missing_download_task(task: BackgroundTask) -> Dict[str, Any]:
    """Searches Soulseek and downloads all missing tracks for a specific release."""
    task.check_cancelled()
    from musicscraper.services.library import LibraryReleaseService

    artist = task.params.get("artist", "").strip()
    release_title = task.params.get("release_title", "").strip()
    missing_tracks = task.params.get("missing_tracks", [])
    format_pref = task.params.get("format", "flac")
    dry_run = bool(task.params.get("dry_run", False))
    timeout = float(task.params.get("timeout", 25.0))

    if not artist or not release_title:
        raise ValueError("Artist and release title are required to download missing tracks.")

    if not missing_tracks:
        task.update_progress(100, "No missing tracks specified for download.")
        return {"status": "skipped", "queued_count": 0}

    task.update_progress(10, f"Searching Soulseek for {len(missing_tracks)} missing tracks of '{artist} - {release_title}'...")
    task.check_cancelled()
    service = LibraryReleaseService()

    def on_prog(done, total, msg):
        task.check_cancelled()
        task.update_progress(done, msg)

    res = service.download_missing_tracks(
        artist=artist,
        release_title=release_title,
        missing_tracks=missing_tracks,
        preferred_format=format_pref,
        search_timeout=timeout,
        dry_run=dry_run,
        on_progress=on_prog
    )
    task.check_cancelled()

    task.update_progress(100, f"Queued {res.get('queued_count', 0)} of {len(missing_tracks)} missing tracks.")
    return res


def run_track_soulseek_download_task(task: BackgroundTask) -> Dict[str, Any]:
    """Searches Soulseek and downloads a single missing track."""
    task.check_cancelled()
    from musicscraper.services.library import LibraryReleaseService

    artist = task.params.get("artist", "").strip()
    release_title = task.params.get("release_title", "").strip()
    track_title = task.params.get("track_title", "").strip()
    track_artist = task.params.get("track_artist", "").strip()
    track_number = task.params.get("track_number")
    format_pref = task.params.get("format", "flac")
    dry_run = bool(task.params.get("dry_run", False))
    timeout = float(task.params.get("timeout", 28.0))

    if not artist or not track_title:
        raise ValueError("Artist and track title are required.")

    service = LibraryReleaseService()
    display_name = f"{track_artist} - {track_title}" if track_artist else f"{artist} - {track_title}"
    task.update_progress(15, f"Searching Soulseek for single track '{display_name}'...")
    task.check_cancelled()
    res = service.download_single_missing_track(
        artist=artist,
        release_title=release_title,
        track_title=track_title,
        track_artist=track_artist if track_artist else None,
        track_number=track_number,
        preferred_format=format_pref,
        search_timeout=timeout,
        dry_run=dry_run
    )
    task.check_cancelled()

    actual_track = res.get("track", track_title)
    if res.get("success"):
        task.update_progress(100, f"Queued '{actual_track}' from peer '{res.get('user')}'.")
    else:
        task.update_progress(100, res.get("message") or f"No peer candidates found for '{actual_track}'.")

    return res


def run_library_audit_all_task(task: BackgroundTask) -> Dict[str, Any]:
    """Audits all library releases against MusicBrainz to find missing tracks across the entire library."""
    task.check_cancelled()
    from musicscraper.services.library import LibraryReleaseService

    library_dir = Path(task.params.get("library_dir", str(Config.DEFAULT_LIBRARY_DIR)))
    force_refresh = bool(task.params.get("force_refresh", True))

    task.update_progress(10, "Starting MusicBrainz audit for all library releases...")
    task.check_cancelled()
    service = LibraryReleaseService()

    def on_prog(done, total, msg):
        task.check_cancelled()
        pct = 10 + int((done / max(1, total)) * 85)
        task.update_progress(pct, f"[{done}/{total}] {msg}")

    audited = service.audit_all_releases(
        library_dir=library_dir,
        force_refresh=force_refresh,
        on_progress=on_prog
    )
    task.check_cancelled()

    cache_library_releases(audited)

    has_missing = sum(1 for r in audited if r.get("status") == "has_missing")
    total_missing_trks = sum(r.get("missing_count", 0) for r in audited)

    task.update_progress(100, f"Library audit complete: {has_missing} releases have missing tracks ({total_missing_trks} missing tracks total).")
    return {
        "total_releases": len(audited),
        "has_missing_releases": has_missing,
        "total_missing_tracks": total_missing_trks,
    }
