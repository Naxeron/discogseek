"""Local audio discovery and release aggregation, independent of remote services."""

import hashlib
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from discogseek.config import Config
from discogseek.core.audio import (
    AUDIO_EXTRACTOR_VERSION,
    AudioMetadata,
    AudioQualityAnalyzer,
    DISC_DIR_PATTERN,
)
from discogseek.core.cache import UnifiedCacheManager
from discogseek.core.constants import (
    AUDIO_EXTENSIONS,
    IGNORED_SCAN_DIR_NAMES,
    IGNORED_SCAN_DIR_PREFIXES,
)
from discogseek.core.release_metadata import (
    is_unknown_album,
    is_various_artists,
    is_various_artists_directory,
    parse_disc_and_track_number,
)
from discogseek.core.text import normalize_text, primary_artist


def scan_library_tracks(
    cache: UnifiedCacheManager,
    library_dir: Optional[Path] = None,
    force_rescan: bool = False,
    threads: int = 16,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> List[AudioMetadata]:
    """Read cached or fresh metadata for every audio file, including loose tracks."""
    lib_path = Path(library_dir or Config.DEFAULT_LIBRARY_DIR).resolve()
    if not lib_path.exists():
        return []

    # 1. Discover all candidate audio files. Network filesystems list entries in
    # server order; sort them so grouping never depends on directory order.
    audio_paths: List[Path] = []
    for root, dirs, files in os.walk(lib_path):
        dirs[:] = sorted(
            d
            for d in dirs
            if not d.startswith(IGNORED_SCAN_DIR_PREFIXES)
            and d.lower() not in IGNORED_SCAN_DIR_NAMES
        )
        for f in sorted(files):
            ext = os.path.splitext(f)[1].lower()
            if ext in AUDIO_EXTENSIONS:
                audio_paths.append(Path(root) / f)

    total_files = len(audio_paths)
    if total_files == 0:
        return []

    # 2. Inspect metadata (Cache or Mutagen)
    all_tracks: List[AudioMetadata] = []
    uncached_paths: List[Path] = []

    cached_metadata = cache.get_audio_metadata_batch(audio_paths) if not force_rescan else {}
    for p in audio_paths:
        cached = cached_metadata.get(p)
        if cached:
            all_tracks.append(cached)
        else:
            uncached_paths.append(p)
    del cached_metadata

    progress = {"completed": len(all_tracks)}

    def analyze(paths: List[Path], report_progress: bool = True) -> List[AudioMetadata]:
        def analyze_with_fingerprint(path):
            # Batch writes may happen well after analysis. Never save old tags
            # under a new fingerprint if a download or retag changes the file.
            try:
                stat = path.stat()
            except OSError:
                return None, None
            return AudioQualityAnalyzer.analyze_file(path), (stat.st_mtime, stat.st_size)

        analyzed: List[AudioMetadata] = []
        pending_cache: List[AudioMetadata] = []
        pending_fingerprints: Dict[Path, Tuple[float, int]] = {}
        with ThreadPoolExecutor(max_workers=threads) as pool:
            for meta, fingerprint in pool.map(analyze_with_fingerprint, paths):
                # A failed read keeps its folder/filename fallback for this scan
                # only; the next scan reads the file again instead of trusting it.
                if meta and not meta.read_error:
                    pending_cache.append(meta)
                    pending_fingerprints[meta.path] = fingerprint
                    if len(pending_cache) >= 500:
                        cache.store_audio_metadata_batch(pending_cache, pending_fingerprints)
                        pending_cache.clear()
                        pending_fingerprints.clear()
                if meta:
                    analyzed.append(meta)
                if not report_progress:
                    continue
                progress["completed"] += 1
                if on_progress and progress["completed"] % 25 == 0:
                    on_progress(progress["completed"], total_files, "Extracting audio metadata...")
        if pending_cache:
            cache.store_audio_metadata_batch(pending_cache, pending_fingerprints)
        return analyzed

    if uncached_paths:
        all_tracks.extend(analyze(uncached_paths))
    reread = _legacy_rows_with_inconsistent_album_artists(all_tracks)
    if reread:
        fresh = {meta.path: meta for meta in analyze(sorted(reread), report_progress=False)}
        # A file that cannot be read (or stat'ed) right now keeps its cached tags for
        # this scan; the cache lookup found it moments ago.
        all_tracks = [
            fresh[meta.path] if meta.path in fresh and not fresh[meta.path].read_error else meta
            for meta in all_tracks
        ]

    return all_tracks


def _legacy_rows_with_inconsistent_album_artists(tracks: List[AudioMetadata]) -> Set[Path]:
    """
    Cached rows from an older extractor in folders whose album artists disagree.

    Before extractor version 2 a FLAC file carrying both ALBUMARTIST and
    'ALBUM ARTIST' kept whichever key mutagen listed first, which changed with each
    process's hash seed, so one album could be split between two album artists (and
    a file read now may disagree with its cached neighbours). Re-reading only these
    folders avoids reading the whole library again over a network mount.
    """
    by_folder: Dict[Path, List[AudioMetadata]] = {}
    for meta in tracks:
        by_folder.setdefault(meta.path.parent, []).append(meta)
    reread: Set[Path] = set()
    for metas in by_folder.values():
        legacy = [meta for meta in metas if meta.extractor_version < AUDIO_EXTRACTOR_VERSION]
        if legacy and len({normalize_text(meta.album_artist) for meta in metas}) > 1:
            reread.update(meta.path for meta in legacy)
    return reread


def scan_library_releases(
    cache: UnifiedCacheManager,
    library_dir: Optional[Path] = None,
    force_rescan: bool = False,
    threads: int = 16,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> List[Dict[str, Any]]:
    """Scan local audio files and aggregate them into grouped releases."""
    lib_path = Path(library_dir or Config.DEFAULT_LIBRARY_DIR).resolve()
    all_tracks = scan_library_tracks(cache, lib_path, force_rescan, threads, on_progress)
    releases_map = _group_releases(all_tracks, lib_path)
    release_list = [_finalize_release(rel) for rel in releases_map.values()]

    # Sort releases alphabetically by Artist, then Title
    release_list.sort(key=lambda r: (r["artist"].lower(), r["title"].lower()))
    return release_list


# A featured guest: 'Lone feat. Guest', 'Lone (ft. Guest)'. Collaborations ('A & B', 'A x B') stay.
_FEATURED_CREDIT_RE = re.compile(
    r"\s*(?:[\(\[\{]\s*(?:feat\b\.?|ft\b\.?|featuring\b|with\b)[^\)\]\}]*[\)\]\}]"
    r"|(?<=\S)\s+(?:feat\b\.?|ft\b\.?|featuring\b).*)$",
    re.IGNORECASE,
)


def _main_credit(artist: str) -> str:
    """The artist credit without its featured guests."""
    return _FEATURED_CREDIT_RE.sub("", artist).strip(" -,;") or artist.strip()


def _dominant_credit(counts: Counter, main_credits: Dict[str, Counter]) -> str:
    """
    The credit naming an album without an album-artist tag: the most frequent main
    credit of its most frequent primary artist (ties: the smallest name). When no main
    credit covers most of that artist's tracks ('Lone feat. A', 'Lone x B'), the
    primary artist's own name.
    """
    if not counts:
        return ""
    dominant = min(counts, key=lambda name: (-counts[name], name))
    credits = main_credits.get(dominant) or Counter()
    if not credits:
        return ""
    credit = min(credits, key=lambda name: (-credits[name], name))
    if credits[credit] * 2 > sum(credits.values()):
        return credit
    return primary_artist(credit) or credit


def _group_releases(all_tracks: List[AudioMetadata], lib_path: Path) -> Dict[str, Dict[str, Any]]:
    """Group tagged files into releases, retaining track details for gap analysis."""
    releases_map: Dict[str, Dict[str, Any]] = {}
    # Loose tracks must not become albums or affect compilation inference.
    all_tracks = [meta for meta in all_tracks if meta.mb_release_ids or not is_unknown_album(meta.album)]

    # Pre-analyze each folder's albums to detect multi-artist compilations. Albums
    # are counted separately because slskd puts every remote '.../CD2' folder into one
    # local 'CD2' folder, and featured guests do not make a compilation.
    album_artists: Dict[Tuple[str, str], Counter] = {}
    # The credits (without featured guests) of each primary artist's tracks, so the
    # release is named 'Simon & Garfunkel' rather than its first name, 'Simon'.
    main_credits: Dict[Tuple[str, str], Dict[str, Counter]] = {}
    for meta in all_tracks:
        art = meta.artist.strip() if meta.artist else ""
        if art and not is_various_artists(art) and art.lower() != "unknown artist":
            primary = normalize_text(primary_artist(art) or art)
            album_key = (str(meta.path.parent), normalize_text(meta.album))
            album_artists.setdefault(album_key, Counter())[primary] += 1
            main_credits.setdefault(album_key, {}).setdefault(primary, Counter())[_main_credit(art)] += 1

    mbid_counts: Dict[str, Counter] = {}
    for meta in all_tracks:
        album_key = (str(meta.path.parent), normalize_text(meta.album))
        counts = album_artists.get(album_key) or Counter()
        # A compilation needs two or more primary artists, none credited on more
        # than half of the album's tracks ('Lone', 'Lone feat. Guest' is one artist).
        is_folder_multi_artist = len(counts) > 1 and max(counts.values()) * 2 <= sum(counts.values())
        # Guest/remix credits and a VA folder are only hints when no album
        # artist is tagged. Preserve an explicit album credit across its tracks.
        tagged_album_artist = (meta.album_artist or "").strip()
        is_va = is_various_artists(tagged_album_artist) if tagged_album_artist else (
            is_various_artists(meta.artist)
            or is_folder_multi_artist
            or is_various_artists_directory(meta.path.parent)
        )

        if is_va:
            artist = "Various Artists"
            album_artist = "Various Artists"
        else:
            # Without an album-artist tag, the album's main primary artist names the
            # release, so 'Lone feat. Guest' joins the rest of the album.
            artist = tagged_album_artist or _dominant_credit(counts, main_credits.get(album_key, {})) \
                or meta.artist or "Unknown Artist"
            album_artist = tagged_album_artist or artist

        album = meta.album or "Unknown Album"

        norm_artist = normalize_text(artist)
        norm_album = normalize_text(album)

        # Check if MB release ID is present
        mb_rel_id = min(meta.mb_release_ids) if meta.mb_release_ids else None

        # Detect if file is in a disc subfolder
        is_disc_folder = bool(DISC_DIR_PATTERN.match(meta.path.parent.name))
        effective_folder_path = meta.path.parent.parent if is_disc_folder else meta.path.parent
        effective_folder_str = str(effective_folder_path)

        # Generate unique release key
        if is_va:
            if norm_album and norm_album not in (
                "",
                "unknown album",
                "unknown",
                "untitled",
                "album",
            ):
                rel_key = f"va_{norm_album}"
            elif mb_rel_id:
                rel_key = f"mb_{mb_rel_id}"
            else:
                rel_key = f"dir_{hashlib.md5(effective_folder_str.encode()).hexdigest()[:12]}"
        elif norm_artist and norm_album:
            rel_key = f"art_alb_{norm_artist}::{norm_album}"
        elif mb_rel_id:
            rel_key = f"mb_{mb_rel_id}"
        else:
            rel_key = f"dir_{hashlib.md5(effective_folder_str.encode()).hexdigest()[:12]}"

        if rel_key not in releases_map:
            rel_id = hashlib.md5(rel_key.encode()).hexdigest()[:16]
            try:
                rel_folder = str(effective_folder_path.relative_to(lib_path))
            except Exception:
                rel_folder = effective_folder_str

            releases_map[rel_key] = {
                "id": rel_id,
                "rel_key": rel_key,
                "title": album,
                "artist": artist,
                "album_artist": album_artist,
                "is_va": is_va,
                "year": meta.year or "",
                "folder_path": rel_folder,
                "full_path": effective_folder_str,
                "mb_release_id": None,
                "formats": set(),
                "is_lossless_all": True,
                "tracks": [],
                "total_tracks_expected": 0,
                "status": "unverified",
                "is_audited": False,
                "found_count": 0,
                "missing_count": 0,
                "completion_pct": 100.0,
            }
        else:
            rel = releases_map[rel_key]
            try:
                curr_folder = str(effective_folder_path.relative_to(lib_path))
            except Exception:
                curr_folder = effective_folder_str
            # If existing folder is in 'downloads' but this track is in a proper library folder, prefer library folder
            if "download" in rel["folder_path"].lower() and "download" not in curr_folder.lower():
                rel["folder_path"] = curr_folder
                rel["full_path"] = effective_folder_str

        rel = releases_map[rel_key]
        if meta.mb_release_ids:
            mbid_counts.setdefault(rel_key, Counter()).update(meta.mb_release_ids)
        fmt_label = meta.format_label or meta.file_type.lstrip(".").upper()
        if fmt_label:
            rel["formats"].add(fmt_label)
        if not meta.is_lossless:
            rel["is_lossless_all"] = False
        if meta.year and not rel["year"]:
            rel["year"] = meta.year

        # Track number and disc number parsing
        parsed_disc, trk_num, total_in_tag = parse_disc_and_track_number(
            meta.track_number, filename=meta.path.name, meta_disc=getattr(meta, "disc_number", None)
        )
        if total_in_tag is None and getattr(meta, "track_total", None):
            # TRACKTOTAL/TOTALTRACKS or an MP4 trkn total: trailing gaps are visible too.
            total_in_tag = meta.track_total
        effective_disc = parsed_disc or getattr(meta, "disc_number", 1) or 1
        effective_total_discs = getattr(meta, "total_discs", 1) or 1
        if effective_total_discs < effective_disc:
            effective_total_discs = effective_disc

        if total_in_tag and total_in_tag > rel["total_tracks_expected"]:
            rel["total_tracks_expected"] = total_in_tag

        rel["tracks"].append(
            {
                "path": str(meta.path),
                "filename": meta.path.name,
                "title": meta.title or meta.path.stem,
                "artist": meta.artist or artist,
                "disc_number": effective_disc,
                "total_discs": effective_total_discs,
                "track_number": str(trk_num) if trk_num is not None else None,
                "track_num_int": trk_num,
                # The tag's own position ('B2'): the audit compares vinyl sides directly.
                "position_token": (meta.track_number or "").strip() or None,
                "total_in_tag": total_in_tag,
                "format": fmt_label,
                "bitrate": meta.bitrate_kbps,
                "is_lossless": meta.is_lossless,
                "quality_score": meta.quality_score,
                "duration": meta.duration,
                "mb_track_ids": list(meta.mb_track_ids),
                "mb_rec_ids": list(meta.mb_rec_ids),
                "status": "found",
            }
        )

    # A folder can mix files tagged with several editions. Audit the edition most of
    # its files carry, independent of scan order (ties: the smallest ID).
    for rel_key, counts in mbid_counts.items():
        releases_map[rel_key]["mb_release_id"] = min(counts, key=lambda mbid: (-counts[mbid], mbid))

    return releases_map


def _finalize_release(rel: Dict[str, Any]) -> Dict[str, Any]:
    """Deduplicate files, identify per-disc gaps, and compute display metrics."""
    # Deduplicate multiple files for the same track (e.g. file in Library/ and file in downloads/)
    deduped_found: Dict[Any, Dict[str, Any]] = {}
    for t in rel["tracks"]:
        d_num = t.get("disc_number", 1) or 1
        d_key = (
            (d_num, t["track_num_int"], normalize_text(t["title"]))
            if t.get("track_num_int") is not None
            else (d_num, normalize_text(t["title"]))
        )
        if d_key not in deduped_found:
            deduped_found[d_key] = t
        else:
            existing = deduped_found[d_key]
            existing_is_lib = "download" not in (existing.get("path") or "").lower()
            new_is_lib = "download" not in (t.get("path") or "").lower()
            if (new_is_lib and not existing_is_lib) or (
                new_is_lib == existing_is_lib
                and (t.get("quality_score", 0) > existing.get("quality_score", 0))
            ):
                deduped_found[d_key] = t
    found_tracks = list(deduped_found.values())

    # Group found tracks by disc
    discs: Dict[int, List[Dict[str, Any]]] = {}
    disc_expected_tags: Dict[int, int] = {}
    for t in found_tracks:
        d = t.get("disc_number", 1) or 1
        discs.setdefault(d, []).append(t)
        tot = t.get("total_in_tag")
        if tot and tot > disc_expected_tags.get(d, 0):
            disc_expected_tags[d] = tot

    max_disc_num = max(
        max((t.get("disc_number", 1) or 1 for t in found_tracks), default=1),
        max((t.get("total_discs", 1) or 1 for t in found_tracks), default=1),
    )
    if max_disc_num > 1:
        for d in range(1, max_disc_num + 1):
            discs.setdefault(d, [])

    all_tracks_list: List[Dict[str, Any]] = list(found_tracks)
    is_multi_disc = len(discs) > 1

    for d_num, d_tracks in sorted(discs.items()):
        found_nums = {
            t["track_num_int"]
            for t in d_tracks
            if t.get("track_num_int") is not None and t["track_num_int"] > 0
        }
        max_trk_num = max(found_nums) if found_nums else 0
        expected_from_tag = disc_expected_tags.get(d_num, 0)
        disc_expected = max(expected_from_tag, max_trk_num)

        if 0 < disc_expected <= 60:
            for k in range(1, disc_expected + 1):
                if k not in found_nums:
                    missing_title = (
                        f"Disc {d_num} Track {k:02d} (Missing)"
                        if is_multi_disc
                        else f"Track {k:02d} (Missing)"
                    )
                    all_tracks_list.append(
                        {
                            "disc_number": d_num,
                            "track_number": str(k),
                            "track_num_int": k,
                            "title": missing_title,
                            "status": "missing",
                            "filename": None,
                            "path": None,
                            "format": None,
                            "bitrate": None,
                            "is_lossless": None,
                            "quality_score": 0,
                            "duration": None,
                            "mb_recording_id": None,
                        }
                    )

    # Sort tracks by disc_number, track_num_int, and title/filename
    def _sort_key(t: Dict[str, Any]) -> Tuple[int, int, str]:
        d = t.get("disc_number", 1) or 1
        tn = t.get("track_num_int")
        order = tn if (tn is not None and tn > 0) else 9999
        return d, order, t.get("filename") or t.get("title", "")

    all_tracks_list.sort(key=_sort_key)
    rel["tracks"] = all_tracks_list
    rel["formats"] = sorted(list(rel["formats"]))

    found_count = sum(1 for t in all_tracks_list if t["status"] == "found")
    missing_count = sum(1 for t in all_tracks_list if t["status"] == "missing")
    total_count = len(all_tracks_list)

    rel["found_count"] = found_count
    rel["missing_count"] = missing_count
    rel["total_tracks_expected"] = max(rel["total_tracks_expected"], total_count)

    if missing_count > 0:
        rel["status"] = "has_missing"
        rel["completion_pct"] = (
            round((found_count / total_count) * 100.0, 1) if total_count > 0 else 100.0
        )
    else:
        rel["status"] = "complete"
        rel["completion_pct"] = 100.0
    return rel
