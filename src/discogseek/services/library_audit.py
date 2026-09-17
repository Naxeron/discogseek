"""MusicBrainz release lookup and reconciliation against local library tracks."""

import re
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

from discogseek.clients.musicbrainz import MusicBrainzClient
from discogseek.core.release_metadata import (
    format_artist_credit,
    is_various_artists,
    parse_disc_and_track_number,
)
from discogseek.core.text import (
    are_versions_compatible,
    calculate_similarity,
    normalize_text,
    parse_track_title_structure,
)
from discogseek.services.reconciler import (
    have_conflicting_numbers,
    have_conflicting_track_numbers,
    is_purely_numeric_track,
    is_track_number_match,
)


def is_numeric_track_item(lt: Dict[str, Any]) -> bool:
    """Checks if track is purely numeric or compound disc-track number with no title."""
    if is_purely_numeric_track(lt):
        return True
    raw_title = (lt.get("title") or "").strip()
    raw_fn = Path(lt.get("filename") or lt.get("path") or "").stem.strip()
    return bool(
        re.match(r"^\d{1,2}[-_.]\d{1,3}$", raw_fn)
        and (
            not raw_title
            or raw_title.isdigit()
            or raw_title == raw_fn
            or re.match(r"^(?:track|trk)[\s._\-]*\d{1,3}$", raw_title, re.IGNORECASE)
            or re.match(r"^\d{1,2}[-_.]\d{1,3}$", raw_title)
        )
    )


def audit_release(
    mb_client: MusicBrainzClient, release_data: Dict[str, Any], force_refresh: bool = False
) -> Dict[str, Any]:
    """
    Reconciles a local release against MusicBrainz to identify full tracklist,
    found tracks, and missing tracks with real official song titles.
    """
    artist = release_data.get("artist", "")
    title = release_data.get("title", "")
    mb_rel_id = release_data.get("mb_release_id")

    # Only consider actual local audio files on disk (ignore any prior missing placeholders)
    local_tracks = [t for t in release_data.get("tracks", []) if t.get("filename") or t.get("path") or t.get("source") == "navidrome"]

    # Defensive normalization: ensure disc_number and track_number are accurately parsed from filenames
    for lt in local_tracks:
        curr_disc = lt.get("disc_number")
        if not curr_disc or curr_disc == 1:
            p_disc, p_trk, _ = parse_disc_and_track_number(
                lt.get("track_number"),
                filename=lt.get("filename") or lt.get("path"),
                meta_disc=curr_disc,
            )
            if p_disc and p_disc > 1:
                lt["disc_number"] = p_disc
            if p_trk is not None:
                if not lt.get("track_number") or lt.get("track_number") == "-":
                    lt["track_number"] = str(p_trk)
                if lt.get("track_num_int") is None:
                    lt["track_num_int"] = p_trk

    mb_release = None

    # 1. Look up by MBID if available
    if mb_rel_id:
        mb_release = mb_client.get_release_by_id(mb_rel_id, force_refresh=force_refresh)

    # 2. Search MusicBrainz by release title + artist if not found by ID
    if not mb_release and title:
        search_results = mb_client.search_release(release_title=title, artist_name=artist, limit=5)
        if search_results:
            best_cand = None
            norm_target_title = normalize_text(title)
            for cand in search_results:
                cand_title = normalize_text(cand.get("title", ""))
                if (
                    cand_title == norm_target_title
                    or calculate_similarity(cand_title, norm_target_title) >= 0.70
                ):
                    best_cand = cand
                    break
            if best_cand:
                best_id = best_cand.get("id")
                if best_id:
                    mb_release = mb_client.get_release_by_id(best_id, force_refresh=force_refresh)

    # 3. If MB release found, reconcile official tracks against local files
    if mb_release:
        mb_artist = format_artist_credit(mb_release.get("artist-credit")) or mb_release.get(
            "artist-credit-phrase", ""
        )
        rg = mb_release.get("release-group", {})
        rg_type = (rg.get("primary-type") or "").lower()
        rg_sec_types = [t.lower() for t in rg.get("secondary-type-list", [])]
        is_mb_compilation = (
            rg_type == "compilation"
            or "compilation" in rg_sec_types
            or is_various_artists(mb_artist)
            or release_data.get("is_va", False)
        )
        rel_artist = "Various Artists" if is_mb_compilation else (mb_artist or artist)

        official_tracks: List[Dict[str, Any]] = []
        media_list = mb_release.get("medium-list", [])

        for medium in media_list:
            disc_num = medium.get("position", 1)
            for trk in medium.get("track-list", []):
                rec = trk.get("recording", {})
                trk_num = trk.get("number") or str(trk.get("position", ""))
                trk_title = rec.get("title") or trk.get("title") or "Unknown Track"
                trk_len = trk.get("length") or rec.get("length")
                duration_sec = round(int(trk_len) / 1000.0, 1) if trk_len else None
                trk_artist = format_artist_credit(
                    trk.get("artist-credit") or rec.get("artist-credit")
                )
                if not trk_artist:
                    trk_artist = rel_artist

                official_tracks.append(
                    {
                        "disc_number": disc_num,
                        "track_number": trk_num,
                        "title": trk_title,
                        "artist": trk_artist,
                        "norm_title": normalize_text(trk_title),
                        "mb_recording_id": rec.get("id"),
                        "mb_track_id": trk.get("id"),
                        "duration": duration_sec,
                    }
                )

        # Reconcile local tracks against official tracks
        matched_local_indices: Set[int] = set()
        matched_candidate_indices: Set[int] = set()
        recording_candidates = release_data.get("recording_candidates", [])
        reconciled_tracklist: List[Dict[str, Any]] = []
        positions_by_recording: Dict[str, Set[Tuple[int, str]]] = {}
        for track in official_tracks:
            if track["mb_recording_id"]:
                positions_by_recording.setdefault(track["mb_recording_id"], set()).add(
                    (int(track["disc_number"] or 1), track["track_number"])
                )

        for off_trk in official_tracks:
            matched_local = None
            reserved_local_indices: Set[int] = set()
            p_off = parse_track_title_structure(off_trk["title"])

            # Pass 1: MBID match
            for idx, lt in enumerate(local_tracks):
                if idx in matched_local_indices:
                    continue
                if off_trk["mb_recording_id"] and off_trk["mb_recording_id"] in lt.get(
                    "mb_rec_ids", []
                ):
                    # Reserve a repeated recording for its actual position when
                    # that position also appears on this edition's tracklist.
                    own_disc = int(lt.get("disc_number") or 1)
                    if not (own_disc == int(off_trk["disc_number"] or 1)
                            and is_track_number_match(lt.get("track_number"), off_trk["track_number"])):
                        if any(own_disc == disc and is_track_number_match(lt.get("track_number"), number)
                               for disc, number in positions_by_recording[off_trk["mb_recording_id"]]):
                            reserved_local_indices.add(idx)
                            continue
                    matched_local = lt
                    matched_local_indices.add(idx)
                    break
                if off_trk["mb_track_id"] and off_trk["mb_track_id"] in lt.get("mb_track_ids", []):
                    matched_local = lt
                    matched_local_indices.add(idx)
                    break

            # Pass 2: Track number + Title match (with numeric filename support)
            if not matched_local:
                for idx, lt in enumerate(local_tracks):
                    if idx in matched_local_indices or idx in reserved_local_indices:
                        continue

                    # Check disc number alignment if both are specified
                    lt_disc = lt.get("disc_number")
                    off_disc = off_trk.get("disc_number")
                    if (
                        lt_disc is not None
                        and off_disc is not None
                        and int(lt_disc) != int(off_disc)
                    ):
                        continue

                    lt_num = lt.get("track_number")
                    off_num = off_trk.get("track_number")

                    if is_track_number_match(lt_num, off_num):
                        p_lt = parse_track_title_structure(lt.get("title", ""))

                        # Fast-path: local file is purely numeric (e.g. 01.flac, 1-01.flac) in confirmed album
                        if is_numeric_track_item(lt):
                            matched_local = lt
                            matched_local_indices.add(idx)
                            break

                        # Title match with numeric conflict guardrail
                        has_num_conflict = have_conflicting_numbers(
                            p_off["base_norm"], p_lt["base_norm"]
                        )
                        if not has_num_conflict:
                            sim = calculate_similarity(p_off["base_norm"], p_lt["base_norm"])
                            ver_compat = are_versions_compatible(
                                p_off["version_type"],
                                p_off["version_text"],
                                p_lt["version_type"],
                                p_lt["version_text"],
                            )
                            if (
                                (p_off["base_norm"] == p_lt["base_norm"]) or sim >= 0.70
                            ) and ver_compat:
                                matched_local = lt
                                matched_local_indices.add(idx)
                                break

            # Pass 3: High title similarity
            if not matched_local:
                for idx, lt in enumerate(local_tracks):
                    if idx in matched_local_indices or idx in reserved_local_indices:
                        continue

                    if is_numeric_track_item(lt):
                        continue

                    # Check disc number alignment if both are specified
                    lt_disc = lt.get("disc_number")
                    off_disc = off_trk.get("disc_number")
                    if lt_disc is not None and off_disc is not None:
                        try:
                            if int(lt_disc) != int(off_disc):
                                continue
                        except (ValueError, TypeError):
                            pass

                    p_lt = parse_track_title_structure(lt.get("title", ""))

                    if have_conflicting_numbers(p_off["base_norm"], p_lt["base_norm"]):
                        continue

                    if p_off["base_norm"] != p_lt["base_norm"] and have_conflicting_track_numbers(
                        off_trk.get("track_number"), lt.get("track_number")
                    ):
                        continue

                    sim = calculate_similarity(p_off["base_norm"], p_lt["base_norm"])
                    ver_compat = are_versions_compatible(
                        p_off["version_type"],
                        p_off["version_text"],
                        p_lt["version_type"],
                        p_lt["version_text"],
                    )
                    if (p_off["base_norm"] == p_lt["base_norm"] or sim >= 0.85) and ver_compat:
                        matched_local = lt
                        matched_local_indices.add(idx)
                        break

            # A library album can be split across edition tags (e.g. CD and
            # digital). Only an exact recording at the same position can supply
            # a missing track from another edition; title similarity is not proof.
            if not matched_local and off_trk["mb_recording_id"]:
                off_position = parse_disc_and_track_number(
                    off_trk["track_number"], meta_disc=int(off_trk["disc_number"] or 1),
                )[:2]
                for idx, candidate in enumerate(recording_candidates):
                    if idx in matched_candidate_indices:
                        continue
                    if not (candidate.get("path") or candidate.get("filename")
                            or candidate.get("source") == "navidrome"):
                        continue
                    if off_trk["mb_recording_id"] not in candidate.get("mb_rec_ids", []):
                        continue
                    position = parse_disc_and_track_number(
                        candidate.get("track_number"), filename=candidate.get("filename"),
                        meta_disc=candidate.get("disc_number"),
                    )[:2]
                    if off_position[1] is not None and position == off_position:
                        matched_local = candidate
                        matched_candidate_indices.add(idx)
                        break

            eff_trk_artist = (
                off_trk.get("artist")
                or (matched_local.get("artist") if matched_local else None)
                or rel_artist
            )

            if matched_local:
                reconciled_tracklist.append(
                    {
                        "disc_number": off_trk["disc_number"],
                        "track_number": off_trk["track_number"],
                        "title": off_trk["title"],
                        "artist": eff_trk_artist,
                        "status": "found",
                        "source": matched_local.get("source"),
                        "remote_id": matched_local.get("remote_id"),
                        "mb_track_ids": matched_local.get("mb_track_ids", []),
                        "mb_rec_ids": matched_local.get("mb_rec_ids", []),
                        "mb_track_id": off_trk["mb_track_id"],
                        "filename": matched_local.get("filename"),
                        "path": matched_local.get("path"),
                        "format": matched_local.get("format"),
                        "bitrate": matched_local.get("bitrate"),
                        "is_lossless": matched_local.get("is_lossless"),
                        "quality_score": matched_local.get("quality_score"),
                        "duration": matched_local.get("duration") or off_trk["duration"],
                        "mb_recording_id": off_trk["mb_recording_id"],
                    }
                )
            else:
                reconciled_tracklist.append(
                    {
                        "disc_number": off_trk["disc_number"],
                        "track_number": off_trk["track_number"],
                        "title": off_trk["title"],
                        "artist": eff_trk_artist,
                        "status": "missing",
                        "mb_track_id": off_trk["mb_track_id"],
                        "filename": None,
                        "path": None,
                        "format": None,
                        "bitrate": None,
                        "is_lossless": None,
                        "quality_score": 0,
                        "duration": off_trk["duration"],
                        "mb_recording_id": off_trk["mb_recording_id"],
                    }
                )

        # Append any unmatched local tracks (e.g. bonus tracks or non-standard mixes)
        for idx, lt in enumerate(local_tracks):
            if idx not in matched_local_indices and (lt.get("filename") or lt.get("path")):
                reconciled_tracklist.append(
                    {
                        "disc_number": lt.get("disc_number", 1) or 1,
                        "track_number": lt.get("track_number") or "-",
                        "title": lt.get("title") or lt.get("filename"),
                        "artist": lt.get("artist") or rel_artist,
                        "status": "found",
                        "filename": lt.get("filename"),
                        "path": lt.get("path"),
                        "format": lt.get("format"),
                        "bitrate": lt.get("bitrate"),
                        "is_lossless": lt.get("is_lossless"),
                        "quality_score": lt.get("quality_score"),
                        "duration": lt.get("duration"),
                        "mb_recording_id": None,
                    }
                )

        # Sort tracks cleanly by (disc_number, track_number)
        def _audit_sort_key(t: Dict[str, Any]) -> Tuple[int, int, str]:
            dn = t.get("disc_number") or 1
            tn_val = t.get("track_number")
            try:
                tn_int = int(re.sub(r"[^\d]", "", str(tn_val))) if tn_val else 9999
            except Exception:
                tn_int = 9999
            return dn, tn_int, t.get("title", "")

        reconciled_tracklist.sort(key=_audit_sort_key)

        total_tracks = len(reconciled_tracklist)
        found_count = sum(1 for t in reconciled_tracklist if t["status"] == "found")
        missing_count = sum(1 for t in reconciled_tracklist if t["status"] == "missing")
        completion_pct = (
            round((found_count / total_tracks * 100.0), 1) if total_tracks > 0 else 100.0
        )

        result = dict(release_data)
        result["artist"] = rel_artist
        result["album_artist"] = rel_artist
        result["is_va"] = is_mb_compilation
        result["mb_release_id"] = mb_release.get("id")
        result["mb_release_title"] = mb_release.get("title")
        result["total_tracks_expected"] = total_tracks
        result["found_count"] = found_count
        result["missing_count"] = missing_count
        result["completion_pct"] = completion_pct
        result["status"] = "has_missing" if missing_count > 0 else "complete"
        result["tracks"] = reconciled_tracklist
        result["is_audited"] = bool(official_tracks)
        return result

    # Sequence gaps alone cannot establish MusicBrainz completeness.
    result = dict(release_data)
    result["is_audited"] = False
    result["status"] = "unverified"
    return result
