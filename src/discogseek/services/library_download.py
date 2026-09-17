"""Soulseek search and queueing for missing release tracks."""

import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from discogseek.clients.slskd import SlskdClient
from discogseek.core.audio import AudioQualityAnalyzer
from discogseek.core.constants import AUDIO_EXTENSIONS
from discogseek.core.release_metadata import (
    MISSING_TRACK_PATTERN,
    is_missing_track_placeholder,
    is_various_artists,
    parse_disc_and_track_number,
)
from discogseek.core.text import (
    are_versions_compatible,
    calculate_similarity,
    clean_search_phrase,
    normalize_text,
    parse_track_title_structure,
)
from discogseek.services.candidates import PeerCandidateIndex, pre_parse_single_track
from discogseek.services.reconciler import have_conflicting_numbers


def download_missing_tracks(
    slskd_client: SlskdClient,
    audit_release: Callable[[Dict[str, Any]], Dict[str, Any]],
    artist: str,
    release_title: str,
    missing_tracks: List[Dict[str, Any]],
    preferred_format: str = "flac",
    search_timeout: float = 30.0,
    dry_run: bool = False,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    search_scope: str = "release",
) -> Dict[str, Any]:
    """
    Orchestrates Soulseek discovery and queueing for missing tracks of a release.
    Release searches start with album peers; selected-track searches start with its artist/title.
    Both modes fall back to the other search strategy for unresolved requested rows.

    matched_tracks and queued_tracks refer to requested rows; only confirmed enqueue
    successes enter queued_tracks. Queue/search errors retain their affected rows so
    callers can keep earlier successes and retry the remaining positions.
    Progress counts matched requested tracks, not a percentage or transfer progress.
    """
    if search_scope not in {"release", "track"}:
        raise ValueError("search_scope must be 'release' or 'track'")
    if not missing_tracks:
        return {
            "status": "skipped", "message": "No missing tracks to download.",
            "queued_count": 0, "matched_count": 0, "resolved_count": 0,
            "queued_files": [], "matched_tracks": [], "queued_tracks": [], "queue_errors": [],
        }

    def track_position(track: Dict[str, Any]) -> Tuple[int, Optional[int]]:
        try:
            disc = int(track.get("disc_number") or 1)
        except (TypeError, ValueError):
            disc = 1
        parsed_disc, number, _ = parse_disc_and_track_number(
            track.get("track_num_int") or track.get("track_number"), meta_disc=disc,
        )
        placeholder = MISSING_TRACK_PATTERN.match(track.get("title", ""))
        if placeholder:
            parsed_disc = int(placeholder.group(1) or parsed_disc or 1)
            number = number or int(placeholder.group(2))
        return parsed_disc or 1, number

    def track_key(track: Dict[str, Any]) -> Tuple[int, Optional[int], str]:
        return (*track_position(track), track.get("title", ""))

    # Retain the requested rows for UI attribution even when an audit resolves titles.
    requested_tracks = list(missing_tracks)
    missing_tracks = [dict(track) for track in requested_tracks]

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
            official_by_position = {track_position(t): t for t in audited_missing}
            for index, track in enumerate(missing_tracks):
                if is_missing_track_placeholder(track.get("title", "")):
                    official = official_by_position.get(track_position(track))
                    if official and not is_missing_track_placeholder(official.get("title", "")):
                        missing_tracks[index] = {**track, **official}
        except Exception as e:
            logging.getLogger(__name__).info(f"Could not auto-resolve placeholder track titles: {e}")

    if any(is_missing_track_placeholder(t.get("title", "")) for t in missing_tracks):
        raise ValueError("Cannot download unresolved track placeholders; audit the release with MusicBrainz first")

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

    effective_artist = "Various Artists" if is_va else artist
    logging.getLogger(__name__).info(
        f"Initiating Soulseek download for {len(missing_tracks)} missing tracks of '{effective_artist} - {release_title}'..."
    )

    queued_files: List[Dict[str, Any]] = []
    matched_tracks: List[Dict[str, Any]] = []
    queued_tracks: List[Dict[str, Any]] = []
    queue_errors: List[Dict[str, Any]] = []
    resolved_missing: Set[Tuple[int, Optional[int], str]] = set()
    used_files: Set[Tuple[str, str]] = set()
    requested_by_key = {
        track_key(resolved): requested
        for requested, resolved in zip(requested_tracks, missing_tracks)
    }
    preferred = preferred_format.lower()

    def report_progress(message: str) -> None:
        if on_progress:
            on_progress(len(matched_tracks), len(requested_tracks), message)

    def candidate_rank(user: str, file: Dict[str, Any]) -> Tuple[Any, ...]:
        filename = file.get("filename", "")
        label, quality = AudioQualityAnalyzer.determine_stream_quality(file)
        preferred_match = (
            (preferred == "flac" and "FLAC" in label)
            or (preferred == "mp3-320" and Path(filename).suffix.lower() == ".mp3"
                and (file.get("bitRate") or 0) >= 320)
        )
        return (-int(preferred_match), -quality, user.casefold(), filename.casefold(), filename)

    def file_key(user: str, file: Dict[str, Any]) -> Tuple[str, str]:
        return user, file.get("filename", "").replace("\\", "/")

    def candidate_matches(track: Dict[str, Any], file: Dict[str, Any], individual: bool) -> bool:
        filename = file.get("filename", "").replace("\\", "/")
        basename = os.path.basename(filename)
        if Path(filename).suffix.lower() not in AUDIO_EXTENSIONS or file.get("isLocked"):
            return False
        expected_disc, expected_number = track_position(track)
        remote_disc = file.get("disc_number")
        if remote_disc is not None:
            try:
                remote_disc = int(remote_disc)
            except (TypeError, ValueError):
                remote_disc = None
        if remote_disc is None:
            for folder in reversed(filename.split("/")[:-1]):
                disc_match = re.search(
                    r"(?:^|[\s._\-\[(])(?:disc|disk|cd)[\s._\-]*(\d+)(?:$|[\s._\-\])])",
                    folder, re.IGNORECASE,
                )
                if disc_match:
                    remote_disc = int(disc_match.group(1))
                    break
        raw_number = file.get("track_num_int") or file.get("track_number")
        parsed_disc, remote_number, _ = parse_disc_and_track_number(
            raw_number, filename=basename, meta_disc=remote_disc,
        )
        # A purely numeric title is not evidence of the file's track position.
        if raw_number is None and Path(basename).stem.isdecimal():
            if normalize_text(Path(basename).stem) == normalize_text(track.get("title", "")):
                remote_number = None
        # The shared parser defaults an unknown disc to 1; only reject explicit conflicts.
        numbered_disc = re.match(
            r"^(?:(?:disc|disk|cd|side)\b|[A-Za-z]\d|\d{1,2}[-_.]\d{1,3}(?:[\s._\-]|$))",
            basename, re.IGNORECASE,
        ) or re.match(r"^\d{1,2}[-_.]\d{1,3}$", str(raw_number or ""))
        if remote_disc is not None or numbered_disc:
            if expected_disc != parsed_disc:
                return False
        if expected_number is not None and remote_number is not None and expected_number != remote_number:
            return False

        p_missing = parse_track_title_structure(track.get("title", ""))
        p_remote = parse_track_title_structure(basename)
        if have_conflicting_numbers(p_missing["base_norm"], p_remote["base_norm"]):
            return False
        sim = calculate_similarity(p_missing["base_norm"], p_remote["base_norm"])
        if not p_missing["base_norm"] or not are_versions_compatible(
            p_missing["version_type"], p_missing["version_text"],
            p_remote["version_type"], p_remote["version_text"],
        ):
            return False
        artist_name = (track.get("artist") or ("" if is_va else artist)).strip()
        if artist_name and not is_various_artists(artist_name) and artist_name.lower() != "unknown artist":
            # An exact title alone must not admit another artist's recording.
            remote_artist = (file.get("artist") or "").strip()
            unnumbered = re.sub(
                r"^(?:(?:disc|disk|cd)\s*\d+\s*[-_. ]+)?(?:\d{1,2}[-_.])?\d{1,3}(?:\s*[-_.]\s*|\s+)",
                "", Path(basename).stem, flags=re.IGNORECASE,
            )
            parts = re.split(r"\s+[-_]\s+", unnumbered)
            if not remote_artist and len(parts) > 1:
                prefix = normalize_text(parts[0])
                full_title = normalize_text(track.get("title", ""))
                if (
                    prefix not in {p_missing["base_norm"], normalize_text(release_title)}
                    and not full_title.startswith(prefix + " ")
                ):
                    remote_artist = parts[0]
            if remote_artist and not is_various_artists(remote_artist) and calculate_similarity(
                normalize_text(artist_name), normalize_text(remote_artist),
            ) < 0.90:
                return False
            artist_tokens = set(normalize_text(artist_name).split())
            filename_tokens = set(normalize_text(filename).split())
            if individual and not artist_tokens.issubset(filename_tokens) and sim < 0.90:
                return False
        return p_missing["base_norm"] == p_remote["base_norm"] or sim >= (0.80 if individual else 0.85)

    def queue_matches(user: str, matches: List[Tuple[Dict[str, Any], Dict[str, Any]]]) -> None:
        items = []
        originals = []
        for track, file in matches:
            key = track_key(track)
            if key in resolved_missing or file_key(user, file) in used_files:
                continue
            disc, number = track_position(track)
            items.append({
                "filename": file.get("filename"), "size": file.get("size", 0),
                "title": track.get("title", ""), "artist": track.get("artist") or artist, "disc_number": disc,
                "track_number": number, "user": user,
            })
            originals.append(requested_by_key[key])
            resolved_missing.add(key)
            used_files.add(file_key(user, file))
        matched_tracks.extend(originals)
        # Keep each API call to one chunk, so a later failure cannot hide earlier successes.
        for start in range(0, len(items), 50):
            chunk = items[start:start + 50]
            tracks = originals[start:start + 50]
            if not dry_run:
                report_progress(f"Queueing {len(chunk)} matched tracks from {user} in slskd…")
                try:
                    slskd_client.enqueue_download(user, chunk)
                except Exception as error:
                    queue_errors.append({"user": user, "error": str(error), "tracks": tracks})
                    continue
                queued_tracks.extend(tracks)
            queued_files.extend(chunk)

    def remaining_tracks() -> List[Dict[str, Any]]:
        return [track for track in missing_tracks if track_key(track) not in resolved_missing]

    def record_search_error(error: Exception, tracks: List[Dict[str, Any]]) -> None:
        queue_errors.append({
            "stage": "search", "error": str(error),
            "tracks": [requested_by_key[track_key(track)] for track in tracks],
        })

    def unique_queries(queries: List[str]) -> List[str]:
        return list(dict.fromkeys(query.strip() for query in queries if query.strip()))

    def evaluate_album(responses: List[Dict[str, Any]], require_artist: bool = False) -> None:
        # Compare all peers before choosing, so response ordering cannot override format preference.
        peer_directories: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for response in responses:
            user = response.get("username")
            if not user:
                continue
            for file in response.get("files", []):
                filename = file.get("filename", "")
                if Path(filename).suffix.lower() in AUDIO_EXTENSIONS and not file.get("isLocked"):
                    folder = os.path.dirname(filename.replace("\\", "/"))
                    info = peer_directories.setdefault((user, folder), {"matched_search_files": []})
                    info["matched_search_files"].append(file)

        candidate_index = PeerCandidateIndex(peer_directories)
        matches_by_directory: Dict[Tuple[str, str], Dict[Tuple[int, Optional[int], str], Any]] = {}
        selected_by_directory: Dict[Tuple[str, str], Set[Tuple[str, str]]] = {}
        for track in remaining_tracks():
            candidates = candidate_index.get_candidate_files_for_track(pre_parse_single_track(track.get("title", "")))
            for candidate in sorted(candidates, key=lambda item: candidate_rank(item.user, item.raw_file)):
                if require_artist:
                    expected_artist = normalize_text(track.get("artist") or artist).split()
                    remote_words = set(normalize_text(candidate.full_filename).split())
                    if expected_artist and not set(expected_artist).issubset(remote_words):
                        continue
                directory = (candidate.user, candidate.dir_name)
                matches = matches_by_directory.setdefault(directory, {})
                selected_files = selected_by_directory.setdefault(directory, set())
                identity = file_key(candidate.user, candidate.raw_file)
                if track_key(track) not in matches and identity not in selected_files:
                    if candidate_matches(track, candidate.raw_file, False):
                        matches[track_key(track)] = (track, candidate.raw_file)
                        selected_files.add(identity)

        directory_matches = []
        for (user, folder), matches in matches_by_directory.items():
            if matches:
                ranks = [candidate_rank(user, file) for _, file in matches.values()]
                rank = (
                    sum(item[0] for item in ranks), -len(matches),
                    sum(item[1] for item in ranks), user.casefold(), folder.casefold(),
                )
                directory_matches.append((rank, user, list(matches.values())))
        for _, user, matches in sorted(directory_matches, key=lambda item: item[0]):
            queue_matches(user, matches)

    def search_album() -> None:
        if not release_title or not remaining_tracks():
            return
        if is_va:
            queries = [f"Various Artists {release_title}", release_title, f"VA {release_title}"]
        else:
            queries = [f"{artist} {release_title}", clean_search_phrase(f"{artist} {release_title}"), release_title]
        queries = unique_queries(queries)
        for attempt, query in enumerate(queries, 1):
            remaining = remaining_tracks()
            if not remaining:
                break
            report_progress(f"Release search {attempt}/{len(queries)}: {query}…")
            try:
                result = slskd_client.search(query=query, timeout=search_timeout)
            except Exception as error:
                record_search_error(error, remaining)
                continue
            # A response may contain only artwork, locked files or wrong versions.
            # Continue the fallback queries until actual requested tracks match.
            evaluate_album(result.get("responses", []), require_artist=not is_va and query == release_title)

    def search_tracks() -> None:
        remaining = remaining_tracks()
        if not remaining:
            return
        queries_by_track = {}
        for track in remaining:
            title = track.get("title", "")
            track_artist = (track.get("artist") or "").strip()
            known_artist = track_artist and not is_various_artists(track_artist) and track_artist.lower() != "unknown artist"
            query_artist = track_artist if known_artist else ("Various Artists" if is_va else artist)
            separator = " " if search_scope == "track" else " - "
            primary = f"{query_artist}{separator}{title}".strip()
            # The release context finds compilation files that omit the track artist;
            # cleaned variants handle punctuation omitted in peer filenames.
            queries_by_track[track_key(track)] = unique_queries([
                primary, clean_search_phrase(f"{query_artist} {title}"),
                f"{release_title} {title}" if release_title else "",
            ])

        for round_index in range(max((len(queries) for queries in queries_by_track.values()), default=0)):
            query_tracks: Dict[str, List[Dict[str, Any]]] = {}
            for track in remaining_tracks():
                queries = queries_by_track[track_key(track)]
                if round_index < len(queries):
                    query_tracks.setdefault(queries[round_index], []).append(track)
            if not query_tracks:
                continue
            detail = (next(iter(query_tracks)) if len(query_tracks) == 1
                      else f"{len(query_tracks)} queries for {len(remaining_tracks())} remaining tracks")
            report_progress(f"Track search round {round_index + 1}: {detail}…")
            try:
                if search_scope == "track" and len(query_tracks) == 1:
                    query = next(iter(query_tracks))
                    results = {query: slskd_client.search(query=query, timeout=search_timeout)}
                else:
                    results = slskd_client.batch_search(list(query_tracks), timeout=search_timeout)
            except Exception as error:
                record_search_error(error, [track for tracks in query_tracks.values() for track in tracks])
                # Transport failure affects all query variants, unlike a successful
                # search with no usable files. The other strategy may still work.
                break

            for query, tracks in query_tracks.items():
                candidates = [
                    (response["username"], file)
                    for response in results.get(query, {}).get("responses", [])
                    if response.get("username")
                    for file in response.get("files", [])
                ]
                candidates.sort(key=lambda item: candidate_rank(*item))
                for track in tracks:
                    for user, file in candidates:
                        if file_key(user, file) not in used_files and candidate_matches(track, file, True):
                            queue_matches(user, [(track, file)])
                            break

    if search_scope == "track":
        search_tracks()
        search_album()
    else:
        search_album()
        search_tracks()

    verb, count = ("Matched", len(matched_tracks)) if dry_run else ("Queued", len(queued_tracks))
    report_progress(f"Completed: {verb} {count} missing track files.")

    return {
        "artist": effective_artist,
        "release": release_title,
        "total_missing": len(missing_tracks),
        "queued_count": len(queued_tracks),
        "matched_count": len(matched_tracks),
        "resolved_count": len(resolved_missing),
        "dry_run": dry_run,
        "queued_files": queued_files,
        "matched_tracks": matched_tracks,
        "queued_tracks": queued_tracks,
        "queue_errors": queue_errors,
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
    """Download one track using the same matching and fallback rules as release downloads."""
    track = {"title": track_title, "artist": track_artist, "track_number": track_number, "status": "missing"}
    display_artist = track_artist if track_artist and not is_various_artists(track_artist) else artist
    try:
        result = download_missing_tracks(
            slskd_client=slskd_client, audit_release=audit_release,
            artist=artist, release_title=release_title, missing_tracks=[track],
            preferred_format=preferred_format, search_timeout=search_timeout,
            dry_run=dry_run, search_scope="track",
        )
    except ValueError as error:
        return {"success": False, "message": str(error), "artist": display_artist, "track": track_title}

    if not result["queued_files"]:
        message = (
            result["queue_errors"][-1]["error"] if result["queue_errors"]
            else f"No compatible peer matches found for '{display_artist} - {track_title}'."
        )
        return {"success": False, "message": message, "artist": display_artist, "track": track_title}

    file = result["queued_files"][0]
    return {
        "success": True, "artist": file["artist"], "track": file["title"],
        "user": file["user"], "filename": file["filename"], "size": file["size"],
        "dry_run": dry_run,
    }
