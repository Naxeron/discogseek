"""Incremental release audits over one complete local and Navidrome inventory."""

import hashlib
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

from discogseek.clients.navidrome import NavidromeScanner
from discogseek.config import Config
from discogseek.core.release_metadata import is_various_artists, parse_disc_and_track_number
from discogseek.core.text import artist_credit_matches, normalize_text
from discogseek.services.library import LibraryReleaseService
from discogseek.services.library_audit import is_numeric_track_item
from discogseek.services.library_browser_cache import BrowserAuditCache
from discogseek.services.reconciler import deduplicate_candidate_tracks


ProgressCallback = Optional[Callable[[int, int, str], None]]
# Lengths of an untagged, credit-less copy conflict beyond this (as for peer files).
_CREDITLESS_DURATION_SECONDS = 10.0
_CREDITLESS_DURATION_RATIO = 0.05


def _natural_key(release: Dict[str, Any]) -> Tuple[str, str]:
    artist = release.get("album_artist") or release.get("artist") or ""
    if release.get("is_va") or is_various_artists(artist):
        artist = "Various Artists"
    return normalize_text(artist), normalize_text(release.get("title") or "")


def _track_position(track: Dict[str, Any]) -> Tuple[int, str]:
    try:
        disc_number = int(track.get("disc_number") or 1)
    except (TypeError, ValueError):
        disc_number = None
    disc, number, _ = parse_disc_and_track_number(
        track.get("track_number"), filename=track.get("filename"),
        meta_disc=disc_number,
    )
    return disc or 1, str(number) if number is not None else str(track.get("track_number") or "")


def _matches_verified_tracklist(local: Dict[str, Any], verified: Dict[str, Any]) -> bool:
    """Require independent track evidence before correcting an untagged album credit."""
    if not verified.get("is_audited") or not verified.get("mb_release_id"):
        return False
    official = {
        _track_position(track): track for track in verified.get("tracks", [])
        if track.get("mb_track_id") or track.get("mb_recording_id")
    }
    # Files without any artist credit (untagged peer files that slskd saved as
    # 'downloads/<Album>/01 Title.flac') contradict no credit of the edition:
    # their positions, full titles and lengths are the evidence, even for one file.
    credit_less = not _credit_names(local)
    positions = set()
    edition_track_match = False
    for track in local.get("tracks", []):
        if track.get("status") != "found":
            continue
        position = _track_position(track)
        expected = official.get(position)
        if expected is None or not position[1]:
            return False
        track_ids = set(track.get("mb_track_ids") or [])
        recording_ids = set(track.get("mb_rec_ids") or [])
        if ((track_ids and expected.get("mb_track_id") not in track_ids)
                or (recording_ids and expected.get("mb_recording_id") not in recording_ids)):
            return False
        if track_ids:
            edition_track_match = True
        else:
            # A shared recording alone does not identify an edition. Untagged
            # copies need matching positions, full titles (including versions;
            # the printed or the recording title), artists, and lengths for every
            # file, across at least two tracks (see credit_less above).
            titles = {normalize_text(title)
                      for title in [expected.get("title")] + list(expected.get("alt_titles") or [])}
            if (is_numeric_track_item(track)
                    or not normalize_text(track.get("title"))
                    or normalize_text(track.get("title")) not in titles
                    or not (credit_less or (
                        normalize_text(track.get("artist"))
                        and not is_various_artists(track.get("artist"))
                        and normalize_text(track.get("artist")) == normalize_text(expected.get("artist"))
                    ))):
                return False
            try:
                duration, expected_duration = float(track["duration"]), float(expected["duration"])
                # A credit-less copy may come from another master (every track a few
                # seconds shorter); its lengths only must not conflict.
                tolerance = max(_CREDITLESS_DURATION_SECONDS, _CREDITLESS_DURATION_RATIO * expected_duration) \
                    if credit_less else 2
                if not (duration > 0 and expected_duration > 0 and abs(duration - expected_duration) <= tolerance):
                    return False
            except (KeyError, TypeError, ValueError):
                return False
        positions.add(position)
    return bool(positions) and (edition_track_match or len(positions) >= 2 or credit_less)


def _prepare_tracks(release: Dict[str, Any]) -> None:
    """Use shared candidate deduplication without collapsing repeated disc positions."""
    positions: Dict[Tuple[int, str], List[Dict[str, Any]]] = {}
    placeholders = []
    for source_track in release.get("tracks", []):
        track = deepcopy(source_track)
        if not (track.get("path") or track.get("filename") or track.get("source") == "navidrome"):
            if track.get("status") == "missing":
                placeholders.append(track)
            continue
        disc, number = _track_position(track)
        track["disc_number"] = disc
        track["track_number"] = number
        track["norm_album"] = normalize_text(release.get("title") or "")
        track["norm_title"] = normalize_text(track.get("title") or "")
        track.setdefault("source", "local")
        track.setdefault("artist", ", ".join(track.get("artists", [])) or release.get("artist", ""))
        track["status"] = "found"
        for field in ("mb_rec_ids", "mb_track_ids", "mb_artist_ids", "mb_release_ids"):
            track[field] = set(track.get(field) or [])
        positions.setdefault((disc, number), []).append(track)

    tracks = []
    for candidates in positions.values():
        # The shared helper keeps the first candidate's metadata. Local quality,
        # tag data, and its real disk path should take precedence over the index.
        candidates.sort(key=lambda track: track.get("source") != "local")
        deduped = deduplicate_candidate_tracks(candidates)
        for track in deduped:
            if not (track.get("path") or track.get("filename")):
                track["source"] = "navidrome"
        tracks.extend(deduped)
    missing_positions = set()
    for track in placeholders:
        position = _track_position(track)
        if position not in positions and position not in missing_positions:
            tracks.append(track)
            missing_positions.add(position)
    tracks.sort(key=lambda track: (
        _track_position(track)[0],
        int(_track_position(track)[1]) if _track_position(track)[1].isdigit() else 9999,
        track.get("title") or "",
    ))
    release["tracks"] = tracks
    release["found_count"] = sum(track["status"] == "found" for track in tracks)
    release["missing_count"] = sum(track["status"] == "missing" for track in tracks)
    release["total_tracks_expected"] = max(release.get("total_tracks_expected") or 0, len(tracks))
    release["is_audited"] = False
    release["status"] = "unverified"


def merge_library_releases(releases: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Unify naturally matching releases and exact MBIDs, retaining edition conflicts."""
    ids_by_name: Dict[Tuple[str, str], set] = {}
    for release in releases:
        if release.get("mb_release_id"):
            ids_by_name.setdefault(_natural_key(release), set()).add(release["mb_release_id"])

    merged: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for original in sorted(releases, key=lambda release: (
        (release.get("source") or "local") != "local", _natural_key(release),
        release.get("mb_release_id") or "", release.get("full_path") or "",
        release.get("navidrome_id") or "",
    )):
        release = deepcopy(original)
        name_key = _natural_key(release)
        exact_ids = ids_by_name.get(name_key, set())
        release_id = release.get("mb_release_id")
        if not release_id and len(exact_ids) == 1:
            release_id = next(iter(exact_ids))
        key = ("mb", release_id) if release_id else ("name",) + name_key
        if key not in merged:
            release["mb_release_id"] = release_id
            release["id"] = "browser_" + hashlib.sha256(repr(key).encode()).hexdigest()[:16]
            release["browser_name_key"] = name_key
            release["sources"] = sorted(set(release.get("sources") or [release.get("source") or "local"]))
            merged[key] = release
        else:
            current = merged[key]
            current.setdefault("tracks", []).extend(release.get("tracks", []))
            if release.get("recording_candidates"):
                current.setdefault("recording_candidates", []).extend(release["recording_candidates"])
            current["formats"] = sorted(set(current.get("formats") or []) | set(release.get("formats") or []))
            current["sources"] = sorted(set(current["sources"]) | set(release.get("sources") or [release.get("source") or "local"]))
            if release.get("navidrome_id"):
                current["navidrome_id"] = release["navidrome_id"]
            if not current.get("full_path") and release.get("full_path"):
                current["full_path"] = release["full_path"]
                current["folder_path"] = release.get("folder_path")
            current["total_tracks_expected"] = max(
                current.get("total_tracks_expected") or 0, release.get("total_tracks_expected") or 0,
            )

    result = list(merged.values())
    for release in result:
        _prepare_tracks(release)
    result.sort(key=lambda release: (_natural_key(release), release.get("mb_release_id") or ""))
    return result


def _compact_key(release: Dict[str, Any]) -> Tuple[str, str]:
    """The natural key without spaces: 'Machine Drum' and 'Machinedrum' name one artist."""
    artist, title = _natural_key(release)
    return re.sub(r"[\W_]+", "", artist), re.sub(r"[\W_]+", "", title)


def _sibling_keys(release: Dict[str, Any], release_groups: Dict[str, str]) -> List[Tuple[str, ...]]:
    keys = [("name",) + _natural_key(release)]
    compact = _compact_key(release)
    if all(compact):
        keys.append(("compact",) + compact)
    group = release_groups.get(release.get("mb_release_id") or "")
    if group:
        keys.append(("group", group))
    return keys


def _sibling_index(
    releases: List[Dict[str, Any]], release_groups: Dict[str, str],
) -> Dict[Tuple[str, ...], List[Dict[str, Any]]]:
    groups: Dict[Tuple[str, ...], List[Dict[str, Any]]] = {}
    for release in releases:
        if release.get("mb_release_id"):
            for key in _sibling_keys(release, release_groups):
                groups.setdefault(key, []).append(release)
    return groups


def _sibling_candidates(
    release: Dict[str, Any], index: Dict[Tuple[str, ...], List[Dict[str, Any]]],
    release_groups: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Tagged found tracks of the release's sibling editions, marked with their edition."""
    siblings: Dict[int, Dict[str, Any]] = {}
    for key in _sibling_keys(release, release_groups):
        for sibling in index.get(key, []):
            if sibling["mb_release_id"] != release.get("mb_release_id"):
                siblings.setdefault(id(sibling), sibling)
    candidates = []
    for sibling in sorted(siblings.values(), key=lambda item: (
        item["mb_release_id"], _natural_key(item), item.get("source") or "",
    )):
        for track in sibling.get("tracks", []):
            if track.get("status") == "found" and track.get("mb_rec_ids"):
                candidate = deepcopy(track)
                candidate["sibling_release_id"] = sibling["mb_release_id"]
                candidates.append(candidate)
    return candidates


def _add_recording_candidates(
    releases: List[Dict[str, Any]], release_groups: Optional[Dict[str, str]] = None,
) -> None:
    """
    Share tagged recordings across sibling editions without merging identities.

    Siblings are other release MBIDs with the same album name (ignoring spaces and
    punctuation in the artist credit) or, once audited, the same MusicBrainz release
    group: a folder of mixed edition tags makes Navidrome list each edition as an album.
    """
    release_groups = release_groups or {}
    index = _sibling_index(releases, release_groups)
    for release in releases:
        if not release.get("mb_release_id"):
            continue
        candidates = _sibling_candidates(release, index, release_groups)
        if candidates:
            release["recording_candidates"] = candidates
        else:
            release.pop("recording_candidates", None)


def _credit_names(release: Dict[str, Any]) -> List[str]:
    """Album and track artist credits, without Various Artists placeholders."""
    names = [release.get("artist"), release.get("album_artist")]
    for track in release.get("tracks", []):
        names.append(track.get("artist"))
        names.extend(track.get("artists") or [])
    return [name for name in dict.fromkeys(names) if name and not is_various_artists(name)
            and normalize_text(name) not in ("", "unknown artist")]


def _is_va_release(release: Dict[str, Any]) -> bool:
    return bool(release.get("is_va") or is_various_artists(release.get("artist"))
                or is_various_artists(release.get("album_artist")))


def _plausible_alias(local: Dict[str, Any], selected: Dict[str, Any]) -> bool:
    """
    Whether an untagged same-title release could be a copy of the selected edition:
    a shared album or track artist credit (a compilation may be filed under one of
    its contributors), two compilations, missing credits, or a shared recording.
    Generic titles ('Remixes', 'EP') by other artists are unrelated.
    """
    if _is_va_release(local) and _is_va_release(selected):
        return True
    local_names = _credit_names(local)
    expected = _credit_names(selected)
    if not local_names or not expected:
        # Without credits on either side (a compilation listing no track artists)
        # the copy cannot be ruled out.
        return True
    if any(artist_credit_matches(name, credit) for name in expected for credit in local_names):
        return True
    official = {track.get("mb_recording_id") for track in selected.get("tracks", []) if track.get("mb_recording_id")}
    return any(official & set(track.get("mb_rec_ids") or []) for track in local.get("tracks", []))


class LibraryBrowserService:
    """Provide the browser fresh, auditable release snapshots without queueing files."""

    def __init__(
        self,
        release_service: Optional[LibraryReleaseService] = None,
        navidrome_scanner: Optional[NavidromeScanner] = None,
    ):
        self.release_service = release_service or LibraryReleaseService()
        self.navidrome_scanner = navidrome_scanner
        self._audit_cache = BrowserAuditCache(self.release_service.cache)
        self._release_aliases: Dict[str, Set[Tuple[str, str]]] = {}
        self._navidrome_ids: Dict[str, Set[str]] = {}
        # Release group of each audited edition, kept across launches so a targeted
        # refresh can use sibling editions whose album names differ.
        self._release_groups: Dict[str, str] = self._audit_cache.load_release_groups()
        self._release_groups_changed = False
        # Same-title untagged releases already found unrelated to an edition.
        self._unrelated_aliases: Dict[str, Set[str]] = {}

    def _inventory(
        self, library_dir: Optional[Path], on_progress: ProgressCallback = None,
        selected_release: Optional[Dict[str, Any]] = None,
        combine_known_identities: bool = True,
        force_refresh: bool = False,
        invalidated_editions: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        scanner = self.navidrome_scanner
        if scanner is None and Config.NAVIDROME_URL:
            if not Config.NAVIDROME_USER or not Config.NAVIDROME_TOKEN:
                raise ValueError("Navidrome requires a username and password")
            scanner = NavidromeScanner()
        if on_progress:
            on_progress(0, 0, "Scanning local library...")
        releases = self.release_service.scan_library_releases(
            library_dir=library_dir, force_rescan=False, on_progress=on_progress,
        )
        if selected_release and selected_release.get("mb_release_id"):
            self._resolve_local_aliases(releases, selected_release, force_refresh, invalidated_editions)
        if scanner is not None:
            if not scanner.test_connection():
                raise RuntimeError(f"Navidrome connection failed: {scanner.last_error}")
            if selected_release is None:
                remote = scanner.scan_library_releases(on_progress=on_progress)
            else:
                remote = scanner.scan_library_releases(
                    on_progress=on_progress, selected_release=selected_release,
                    include_sibling_editions=True,
                )
            releases = releases + remote
        # MusicBrainz can correct an album's artist credit (especially a partial
        # compilation). Remember that verified identity across subsequent scans.
        identities_by_name: Dict[Tuple[str, str], Set[str]] = {}
        for release_id, names in self._release_aliases.items():
            for name in names:
                identities_by_name.setdefault(name, set()).add(release_id)
        releases = deepcopy(releases)
        if not combine_known_identities:
            # A forced full scan refreshes each source snapshot as well as their
            # union, so a later process can reuse them before learning aliases.
            releases = merge_library_releases(releases)
        for release in releases:
            if not release.get("mb_release_id"):
                identities = identities_by_name.get(_natural_key(release), set())
                if len(identities) == 1:
                    release["mb_release_id"] = next(iter(identities))
        inventory = merge_library_releases(releases) if combine_known_identities else releases
        _add_recording_candidates(inventory, self._release_groups)
        return inventory

    def _resolve_local_aliases(
        self, releases: List[Dict[str, Any]], selected: Dict[str, Any], force_refresh: bool = False,
        invalidated_editions: Optional[Set[str]] = None,
    ) -> None:
        """Resolve possible local copies before a prioritized download interrupts the audit."""
        exact_id = selected["mb_release_id"]
        known_names = self._release_aliases.get(exact_id, set())
        titles = {title for _, title in known_names}
        titles.update(normalize_text(title) for title in (selected.get("title"), selected.get("mb_release_title")) if title)
        unrelated = self._unrelated_aliases.setdefault(exact_id, set())
        for release in releases:
            name = _natural_key(release)
            if release.get("mb_release_id") or name in known_names or name[1] not in titles:
                continue
            # The album may have been filed under one contributing artist, whose
            # initial background audit has not run yet. Resolve its own credit;
            # never assign the selected MBID just because the titles agree.
            prepared = merge_library_releases([release])[0]
            inventory_key = self._audit_cache.key(prepared)
            if inventory_key in unrelated:
                continue
            if not _plausible_alias(prepared, selected):
                # Another artist's 'Remixes' or 'EP' must not block this download.
                unrelated.add(inventory_key)
                continue
            audited = self._audit(prepared, force_refresh, invalidated_editions)
            if not audited.get("is_audited") or not audited.get("mb_release_id"):
                if _matches_verified_tracklist(prepared, selected):
                    candidate = dict(prepared, mb_release_id=exact_id)
                    try:
                        # Empty inventory preserves the official lengths; a
                        # reconciled found track carries the local file's length.
                        verified = self.release_service.audit_release(
                            dict(candidate, tracks=[], recording_candidates=[]), force_refresh=force_refresh,
                        )
                    except Exception:
                        verified = {}
                    # Recheck against the fetched edition before remembering or
                    # caching the alias; the selected UI snapshot may be stale.
                    if (verified.get("mb_release_id") == exact_id
                            and _matches_verified_tracklist(prepared, verified)):
                        if force_refresh:
                            self._audit_cache.invalidate(
                                candidate, None, invalidated_editions if invalidated_editions is not None else set(),
                            )
                        audited = self._audit(candidate, False)
                        self._audit_cache.store(self._audit_cache.key(prepared), prepared, audited, None)
            if not audited.get("is_audited") or not audited.get("mb_release_id"):
                raise ValueError(
                    f"Cannot verify the same-title local release '{release.get('artist', '')} — "
                    f"{release.get('title', '')}'; refresh its MusicBrainz audit before downloading"
                )
            self._remember_identity(release, audited)
        selected["browser_alias_keys"] = sorted(self._release_aliases.get(exact_id, set()))

    def _remember_identity(self, original: Dict[str, Any], audited: Dict[str, Any]) -> None:
        release_id = audited.get("mb_release_id")
        if not audited.get("is_audited") or not release_id:
            return
        group = audited.get("mb_release_group_id")
        if group and self._release_groups.get(release_id) != group:
            self._release_groups[release_id] = group
            self._release_groups_changed = True
        aliases = self._release_aliases.setdefault(release_id, set())
        aliases.update((_natural_key(original), _natural_key(audited)))
        aliases.update(tuple(pair) for pair in original.get("browser_alias_keys") or [])
        remote_ids = self._navidrome_ids.setdefault(release_id, set())
        remote_ids.update(original.get("navidrome_ids") or [])
        if original.get("navidrome_id"):
            remote_ids.add(original["navidrome_id"])
        audited["browser_alias_keys"] = sorted(aliases)
        audited["navidrome_ids"] = sorted(remote_ids)

    def _audit(
        self, release: Dict[str, Any], force_refresh: bool,
        invalidated_editions: Optional[Set[str]] = None,
    ) -> Dict[str, Any]:
        cache_key = self._audit_cache.key(release)
        cached = self._audit_cache.load(cache_key)
        if cached is not None and not force_refresh:
            audited = cached["audit"]
            # Browser row IDs can change after an initially untagged source has
            # acquired its MusicBrainz identity. Keep this scan's row identity.
            for field in ("id", "browser_name_key"):
                if field in release:
                    audited[field] = deepcopy(release[field])
            return audited
        if force_refresh:
            invalidated_editions = invalidated_editions if invalidated_editions is not None else set()
            self._audit_cache.invalidate(release, cached, invalidated_editions)
        try:
            audited = self.release_service.audit_release(deepcopy(release), force_refresh=force_refresh)
            if release.get("mb_release_id") and audited.get("mb_release_id") != release["mb_release_id"]:
                raise ValueError("MusicBrainz returned a different release edition; the selected release is unverified")
        except Exception as exc:
            audited = deepcopy(release)
            audited["audit_error"] = str(exc)
            audited["is_audited"] = False
        if not audited.get("is_audited"):
            audited["status"] = "unverified"
            audited.setdefault("audit_error", "MusicBrainz could not verify this release's tracklist")
        if force_refresh and audited.get("is_audited"):
            self._audit_cache.invalidate(audited, cached, invalidated_editions)
        self._audit_cache.store(cache_key, release, audited, cached)
        return audited

    def iter_releases(
        self,
        library_dir: Optional[Path] = None,
        artist_filter: str = "",
        force_refresh: bool = False,
        on_progress: ProgressCallback = None,
    ) -> Iterator[Dict[str, Any]]:
        """Yield each audit, including complete and unverified releases.

        The inventory is complete before the first audit is yielded. Artist
        filtering is case-insensitive text filtering of release/track credits.
        Every selected release has an audit, even if its local numbers have no
        gaps. Verified audits persist across launches while inventory is unchanged.
        An incomplete edition is yielded again at the end when a release group
        learned during this scan connects it to a sibling edition holding its
        missing recordings; the later audit of an edition replaces the earlier one.
        """
        inventory = self._inventory(library_dir, on_progress, combine_known_identities=not force_refresh)
        # The release groups this inventory's sibling candidates were built from. A
        # refresh between scan steps may save (and clear the flag of) groups learned
        # by this scan, so compare contents rather than relying on that flag.
        inventory_groups = dict(self._release_groups)
        releases = inventory
        needle = normalize_text(artist_filter)
        if needle:
            releases = [release for release in releases if any(
                needle in normalize_text(credit)
                for credit in [release.get("artist") or "", release.get("album_artist") or ""]
                + [track.get("artist") or "" for track in release.get("tracks", [])]
            )]
        try:
            audited_sources: Dict[str, List[Dict[str, Any]]] = {}
            audit_inputs: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]] = {}
            invalidated_editions: Set[str] = set()
            for index, release in enumerate(releases, 1):
                if on_progress:
                    on_progress(index - 1, len(releases), f"Checking {release.get('artist', '')} — {release.get('title', '')}")
                audited = self._audit(release, force_refresh, invalidated_editions)
                self._remember_identity(release, audited)
                audit_input = release
                if audited.get("is_audited") and audited.get("mb_release_id"):
                    release_id = audited["mb_release_id"]
                    source = deepcopy(release)
                    source["mb_release_id"] = release_id
                    sources = audited_sources.setdefault(release_id, [])
                    sources.append(source)
                    if len(sources) > 1:
                        # Two differently tagged raw entries can resolve to the same
                        # official album. Reconcile their combined files before the
                        # browser replaces the earlier result for that MusicBrainz ID.
                        combined = merge_library_releases(sources)[0]
                        audited = self._audit(combined, False)
                        self._remember_identity(combined, audited)
                        audit_input = combined
                    if audited.get("is_audited"):
                        audit_inputs[release_id] = (audit_input, audited)
                yield audited
                if on_progress:
                    on_progress(index, len(releases), f"Checked {index} of {len(releases)} releases")
            # Release groups learned by this scan's audits connect editions whose album
            # names differ. Re-audit an incomplete edition when a newly found sibling
            # holds one of its missing recordings; the browser replaces the earlier row.
            if self._release_groups != inventory_groups:
                self._save_release_groups()
                siblings = _sibling_index(inventory, self._release_groups)
                for release_id, (audit_input, audited) in audit_inputs.items():
                    missing = {track.get("mb_recording_id") for track in audited.get("tracks", [])
                               if track.get("status") == "missing" and track.get("mb_recording_id")}
                    if not missing or not audit_input.get("mb_release_id"):
                        continue
                    candidates = _sibling_candidates(audit_input, siblings, self._release_groups)
                    if candidates == (audit_input.get("recording_candidates") or []) or not any(
                        missing & set(candidate.get("mb_rec_ids") or []) for candidate in candidates
                    ):
                        continue
                    updated = dict(audit_input, recording_candidates=candidates)
                    reaudited = self._audit(updated, False)
                    self._remember_identity(updated, reaudited)
                    if reaudited.get("is_audited") and reaudited.get("mb_release_id") == release_id:
                        yield reaudited
        finally:
            # Also when the browser stops the scan early: release groups learned so
            # far connect editions on the next launch.
            self._save_release_groups()

    def refresh_release(
        self,
        release: Dict[str, Any],
        library_dir: Optional[Path] = None,
        force_refresh: bool = False,
        on_progress: ProgressCallback = None,
    ) -> Dict[str, Any]:
        """Rescan both sources and audit the selected edition before downloading."""
        exact_id = release.get("mb_release_id")
        selected = deepcopy(release)
        self._remember_identity(release, selected)
        if exact_id:
            selected["browser_alias_keys"] = sorted(
                self._release_aliases.get(exact_id, set())
                | {tuple(pair) for pair in release.get("browser_alias_keys") or []}
            )
            selected["navidrome_ids"] = sorted(
                self._navidrome_ids.get(exact_id, set()) | set(release.get("navidrome_ids") or [])
            )
        invalidated_editions: Set[str] = set()
        inventory = self._inventory(
            library_dir, on_progress, selected, force_refresh=force_refresh,
            invalidated_editions=invalidated_editions,
        )
        original_name = tuple(release.get("browser_name_key") or _natural_key(release))
        matches = [candidate for candidate in inventory if (
            exact_id and candidate.get("mb_release_id") == exact_id
        ) or (
            (not exact_id or not candidate.get("mb_release_id"))
            and (candidate.get("id") == release.get("id") or _natural_key(candidate) == original_name)
        )]
        if not matches:
            raise ValueError("The selected release is no longer in the library; reload the release list")
        if exact_id:
            for candidate in matches:
                candidate["mb_release_id"] = exact_id
        fresh = merge_library_releases(matches)[0]
        # A previously untagged release acquired its exact edition during the
        # initial audit. Keep that edition on refresh instead of searching again.
        if exact_id:
            fresh["mb_release_id"] = exact_id
        fresh["id"] = release.get("id") or fresh["id"]
        audited = self._audit(fresh, force_refresh, invalidated_editions)
        self._remember_identity(fresh, audited)
        self._save_release_groups()
        return audited

    def _save_release_groups(self) -> None:
        if self._release_groups_changed:
            self._audit_cache.store_release_groups(self._release_groups)
            self._release_groups_changed = False
