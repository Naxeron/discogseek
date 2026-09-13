"""Soulseek search and queueing for missing release tracks."""

import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from musicscraper.clients.slskd import SlskdClient
from musicscraper.core.audio import AudioQualityAnalyzer
from musicscraper.core.constants import AUDIO_EXTENSIONS
from musicscraper.core.release_metadata import (
    MISSING_TRACK_PATTERN,
    is_missing_track_placeholder,
    is_various_artists,
)
from musicscraper.core.report import console
from musicscraper.core.text import (
    are_versions_compatible,
    calculate_similarity,
    normalize_text,
    parse_track_title_structure,
)


def download_missing_tracks(
    slskd_client: SlskdClient,
    audit_release: Callable[[Dict[str, Any]], Dict[str, Any]],
    artist: str,
    release_title: str,
    missing_tracks: List[Dict[str, Any]],
    preferred_format: str = "flac",
    search_timeout: float = 25.0,
    dry_run: bool = False,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> Dict[str, Any]:
    """
    Orchestrates Soulseek discovery and queueing for missing tracks of a release.
    Attempts smart whole-album peer matching first, followed by individual track searches.
    """
    if not missing_tracks:
        return {"status": "skipped", "message": "No missing tracks to download.", "queued_count": 0}

    # Check if release is compilation / Various Artists
    is_va = is_various_artists(artist)
    track_artists = {
        m.get("artist").strip()
        for m in missing_tracks
        if m.get("artist")
        and not is_various_artists(m.get("artist"))
        and m.get("artist").strip().lower() != "unknown artist"
    }
    if len(track_artists) > 1 or (
        len(track_artists) == 1
        and not is_va
        and next(iter(track_artists)).lower() != artist.strip().lower()
    ):
        is_va = True

    # Check if any missing tracks have placeholder names like "Track 01 (Missing)" or "Disc 1 Track 02 (Missing)"
    has_generic_placeholders = any(
        is_missing_track_placeholder(m.get("title", "")) for m in missing_tracks
    )
    if has_generic_placeholders and release_title:
        try:
            stub_rel = {
                "artist": artist,
                "title": release_title,
                "tracks": missing_tracks,
                "total_tracks_expected": len(missing_tracks),
            }
            audited = audit_release(stub_rel)
            audited_missing = [t for t in audited.get("tracks", []) if t.get("status") == "missing"]
            if audited_missing and not any(
                is_missing_track_placeholder(t.get("title", "")) for t in audited_missing
            ):
                missing_tracks = audited_missing
        except Exception as e:
            console.print(f"[yellow]Could not auto-resolve placeholder track titles: {e}[/yellow]")

    effective_artist = "Various Artists" if is_va else artist
    console.print(
        f"[cyan]Initiating Soulseek download for {len(missing_tracks)} missing tracks of '{effective_artist} - {release_title}'...[/cyan]"
    )
    if on_progress:
        on_progress(10, 100, f"Searching Soulseek for album: {effective_artist} {release_title}...")

    queued_files: List[Dict[str, Any]] = []
    resolved_missing: Set[str] = set()

    # -------------------------------------------------------------
    # STAGE 1: Peer Directory Album Match
    # -------------------------------------------------------------
    if is_va:
        album_queries = [
            f"Various Artists {release_title}".strip(),
            f"{release_title}".strip(),
            f"VA {release_title}".strip(),
        ]
    else:
        album_queries = [f"{artist} {release_title}".strip()]

    search_res = None
    for q in album_queries:
        search_res = slskd_client.search(query=q, timeout=search_timeout)
        if search_res.get("responses"):
            break

    # Fallback album query if specific query yielded 0 responses
    if (
        (not search_res or not search_res.get("responses"))
        and not is_va
        and len(missing_tracks) >= 2
        and release_title
    ):
        for fallback_q in (f"Various Artists {release_title}".strip(), release_title.strip()):
            fallback_res = slskd_client.search(query=fallback_q, timeout=search_timeout)
            if fallback_res.get("responses"):
                search_res = fallback_res
                break

    responses = search_res.get("responses", []) if search_res else []

    if responses:
        if on_progress:
            on_progress(40, 100, "Evaluating peer directory candidates...")

        # Group files by user and folder
        for resp in responses:
            user = resp.get("username")
            files = resp.get("files", [])
            if not user or not files:
                continue

            folder_files: Dict[str, List[Dict[str, Any]]] = {}
            for f in files:
                fn = f.get("filename", "")
                ext = Path(fn).suffix.lower()
                if ext in AUDIO_EXTENSIONS and not f.get("isLocked"):
                    folder_name = os.path.dirname(fn.replace("\\", "/"))
                    folder_files.setdefault(folder_name, []).append(f)

            # Check if this folder has files matching the missing tracks
            for d_name, d_files in folder_files.items():
                to_queue_for_peer: List[Dict[str, Any]] = []

                for m_trk in missing_tracks:
                    m_title = m_trk.get("title", "")
                    if m_title in resolved_missing:
                        continue

                    is_placeholder = bool(
                        re.match(r"^Track\s+\d+\s*\(Missing\)$", m_title, re.IGNORECASE)
                    )
                    m_trk_num = m_trk.get("track_num_int")
                    p_missing = parse_track_title_structure(m_title)

                    for remote_f in d_files:
                        r_fn = remote_f.get("filename", "")
                        base_fn = os.path.basename(r_fn.replace("\\", "/"))
                        p_remote = parse_track_title_structure(base_fn)

                        # If placeholder, match by track number prefix
                        if is_placeholder and m_trk_num:
                            trk_prefix_match = re.match(
                                r"^0*" + str(m_trk_num) + r"[\s._\-]", base_fn
                            )
                            if trk_prefix_match:
                                to_queue_for_peer.append(
                                    {
                                        "filename": r_fn,
                                        "size": remote_f.get("size", 0),
                                        "title": m_title,
                                    }
                                )
                                resolved_missing.add(m_title)
                                break
                            continue

                        sim = calculate_similarity(p_missing["base_norm"], p_remote["base_norm"])
                        ver_compat = are_versions_compatible(
                            p_missing["version_type"],
                            p_missing["version_text"],
                            p_remote["version_type"],
                            p_remote["version_text"],
                        )

                        if (
                            p_missing["base_norm"] == p_remote["base_norm"] or sim >= 0.85
                        ) and ver_compat:
                            to_queue_for_peer.append(
                                {
                                    "filename": r_fn,
                                    "size": remote_f.get("size", 0),
                                    "title": m_title,
                                }
                            )
                            resolved_missing.add(m_title)
                            break

                if to_queue_for_peer:
                    if not dry_run:
                        try:
                            slskd_client.enqueue_download(user, to_queue_for_peer)
                        except Exception as e:
                            console.print(f"[yellow]Failed to queue from peer {user}: {e}[/yellow]")
                    queued_files.extend(to_queue_for_peer)

    # -------------------------------------------------------------
    # STAGE 2: Individual Missing Track Search
    # -------------------------------------------------------------
    remaining_missing = [m for m in missing_tracks if m.get("title") not in resolved_missing]
    if remaining_missing:
        if on_progress:
            on_progress(
                60,
                100,
                f"Searching individual Soulseek tracks for {len(remaining_missing)} remaining items...",
            )

        track_queries = []
        trk_query_map: Dict[str, Dict[str, Any]] = {}

        for m in remaining_missing:
            m_title = m.get("title", "")
            m_artist = (m.get("artist") or "").strip()
            if is_missing_track_placeholder(m_title):
                # Cannot search Soulseek for generic "Track 02 (Missing)"
                continue

            if (
                m_artist
                and not is_various_artists(m_artist)
                and m_artist.lower() != "unknown artist"
            ):
                q = f"{m_artist} - {m_title}".strip()
            elif is_va:
                q = f"Various Artists {m_title}".strip()
            else:
                q = f"{artist} - {m_title}".strip()

            track_queries.append(q)
            trk_query_map[q] = m

        batch_results = (
            slskd_client.batch_search(track_queries, timeout=search_timeout)
            if track_queries
            else {}
        )

        for q_key, m_trk in trk_query_map.items():
            m_title = m_trk.get("title", "")
            m_artist = (m_trk.get("artist") or "").strip()
            s_data = batch_results.get(q_key, {})
            s_responses = s_data.get("responses", [])

            best_candidate: Optional[Tuple[str, Dict[str, Any]]] = None
            best_score = -1

            for resp in s_responses:
                user = resp.get("username")
                for f in resp.get("files", []):
                    fn = f.get("filename", "")
                    ext = Path(fn).suffix.lower()
                    if ext not in AUDIO_EXTENSIONS or f.get("isLocked"):
                        continue

                    base_fn = os.path.basename(fn.replace("\\", "/"))
                    p_missing = parse_track_title_structure(m_title)
                    p_remote = parse_track_title_structure(base_fn)

                    sim = calculate_similarity(p_missing["base_norm"], p_remote["base_norm"])
                    ver_compat = are_versions_compatible(
                        p_missing["version_type"],
                        p_missing["version_text"],
                        p_remote["version_type"],
                        p_remote["version_text"],
                    )

                    artist_matched = True
                    if (
                        m_artist
                        and not is_various_artists(m_artist)
                        and m_artist.lower() != "unknown artist"
                    ):
                        norm_ma = normalize_text(m_artist)
                        norm_fn = normalize_text(base_fn)
                        if norm_ma not in norm_fn and sim < 0.90:
                            artist_matched = False

                    if (
                        artist_matched
                        and (p_missing["base_norm"] == p_remote["base_norm"] or sim >= 0.80)
                        and ver_compat
                    ):
                        fmt_label, score = AudioQualityAnalyzer.determine_stream_quality(f)
                        if score > best_score:
                            best_score = score
                            best_candidate = (user, f)

            if best_candidate:
                u, cand_f = best_candidate
                item = {
                    "filename": cand_f.get("filename"),
                    "size": cand_f.get("size", 0),
                    "title": m_title,
                    "user": u,
                }
                if not dry_run:
                    try:
                        slskd_client.enqueue_download(u, [item])
                    except Exception as e:
                        console.print(f"[yellow]Failed to queue track from peer {u}: {e}[/yellow]")
                queued_files.append(item)
                resolved_missing.add(m_title)

    if on_progress:
        on_progress(100, 100, f"Completed: Queued {len(queued_files)} missing track files.")

    return {
        "artist": effective_artist,
        "release": release_title,
        "total_missing": len(missing_tracks),
        "queued_count": len(queued_files),
        "resolved_count": len(resolved_missing),
        "dry_run": dry_run,
        "queued_files": queued_files,
    }


def download_single_missing_track(
    slskd_client: SlskdClient,
    audit_release: Callable[[Dict[str, Any]], Dict[str, Any]],
    artist: str,
    release_title: str,
    track_title: str,
    track_artist: Optional[str] = None,
    track_number: Optional[Union[int, str]] = None,
    preferred_format: str = "flac",
    search_timeout: float = 28.0,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Searches Soulseek and downloads a single missing track.
    """
    # Auto-resolve placeholder titles like "Track 01 (Missing)" or "Disc 1 Track 02 (Missing)" via MusicBrainz audit
    m_match = MISSING_TRACK_PATTERN.match(track_title)
    disc_from_m = int(m_match.group(1)) if (m_match and m_match.group(1)) else None
    track_from_m = int(m_match.group(2)) if (m_match and m_match.group(2)) else None
    t_num = track_number or track_from_m

    if (m_match or not track_title or "missing" in track_title.lower()) and release_title:
        try:
            stub_rel = {
                "artist": artist,
                "title": release_title,
                "tracks": [],
                "total_tracks_expected": 0,
            }
            audited = audit_release(stub_rel)
            for trk in audited.get("tracks", []):
                trk_idx = trk.get("track_num_int") or trk.get("track_number")
                trk_d = trk.get("disc_number", 1) or 1
                disc_matches = (disc_from_m is None) or (trk_d == disc_from_m)

                if t_num and str(trk_idx) == str(t_num) and disc_matches:
                    if trk.get("title") and not is_missing_track_placeholder(trk["title"]):
                        resolved_title = trk["title"]
                        console.print(
                            f"[green]Resolved placeholder '{track_title}' to '{resolved_title}' via MusicBrainz[/green]"
                        )
                        track_title = resolved_title
                        if not track_artist and trk.get("artist"):
                            track_artist = trk.get("artist")
                        break
        except Exception as e:
            console.print(
                f"[yellow]Could not auto-resolve placeholder track '{track_title}': {e}[/yellow]"
            )

    if is_missing_track_placeholder(track_title):
        return {
            "success": False,
            "message": f"Cannot download generic placeholder '{track_title}'. Please audit the release with MusicBrainz to resolve track titles.",
            "artist": artist,
            "track": track_title,
        }

    eff_artist = (track_artist or "").strip()
    is_va = is_various_artists(artist)

    if eff_artist and not is_various_artists(eff_artist) and eff_artist.lower() != "unknown artist":
        query = f"{eff_artist} {track_title}".strip()
        display_artist = eff_artist
    elif is_va:
        query = f"Various Artists {track_title}".strip()
        display_artist = "Various Artists"
    else:
        query = f"{artist} {track_title}".strip()
        display_artist = artist

    search_res = slskd_client.search(query=query, timeout=search_timeout)
    responses = search_res.get("responses", [])

    # Fallback if "Various Artists {track_title}" yielded 0 responses
    if not responses and is_va and not eff_artist:
        fallback_res = slskd_client.search(query=track_title.strip(), timeout=search_timeout)
        if fallback_res.get("responses"):
            responses = fallback_res.get("responses", [])

    best_candidate: Optional[Tuple[str, Dict[str, Any]]] = None
    best_score = -1

    p_missing = parse_track_title_structure(track_title)

    for resp in responses:
        user = resp.get("username")
        for f in resp.get("files", []):
            fn = f.get("filename", "")
            ext = Path(fn).suffix.lower()
            if ext not in AUDIO_EXTENSIONS or f.get("isLocked"):
                continue

            base_fn = os.path.basename(fn.replace("\\", "/"))
            p_remote = parse_track_title_structure(base_fn)

            sim = calculate_similarity(p_missing["base_norm"], p_remote["base_norm"])
            ver_compat = are_versions_compatible(
                p_missing["version_type"],
                p_missing["version_text"],
                p_remote["version_type"],
                p_remote["version_text"],
            )

            if (p_missing["base_norm"] == p_remote["base_norm"] or sim >= 0.75) and ver_compat:
                fmt_label, score = AudioQualityAnalyzer.determine_stream_quality(f)
                if score > best_score:
                    best_score = score
                    best_candidate = (user, f)

    if not best_candidate:
        return {
            "success": False,
            "message": f"No compatible peer matches found for '{display_artist} - {track_title}'.",
            "artist": display_artist,
            "track": track_title,
        }

    user, file_obj = best_candidate
    payload = [{"filename": file_obj.get("filename"), "size": file_obj.get("size", 0)}]

    if not dry_run:
        slskd_client.enqueue_download(user, payload)

    return {
        "success": True,
        "artist": display_artist,
        "track": track_title,
        "user": user,
        "filename": file_obj.get("filename"),
        "size": file_obj.get("size", 0),
        "dry_run": dry_run,
    }
