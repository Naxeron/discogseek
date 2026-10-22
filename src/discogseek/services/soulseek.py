"""Coordinate Soulseek artist discovery, reconciliation, and download queueing."""

import logging
import re
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Dict, FrozenSet, Hashable, List, Set, Tuple, Optional, Any

from unidecode import unidecode

from discogseek.config import Config
from discogseek.core.audio import DISC_DIR_PATTERN
from discogseek.core.constants import (
    DIR_STOP_WORDS,
    GENERIC_OR_COMMON_WORDS,
)
from discogseek.core.text import (
    normalize_text,
    clean_tokens,
    _tokenize_words_cached,
    artist_credit_matches,
    calculate_similarity,
    clean_search_phrase,
    extract_dir_and_filename,
    is_sublist,
)
from discogseek.core.release_metadata import (
    disc_number_from_folder,
    is_various_artists,
    parse_disc_and_track_number,
)
from discogseek.clients.slskd import (
    SlskdAPIError, SlskdClient, SlskdEnqueueError, SlskdEnqueueRefusedError, SlskdPeerBlockedError,
    SlskdPeerUnavailableError, SlskdTransferFailedError,
)
from discogseek.core.peer_policy import peer_key
from discogseek.core.transfers import DownloadHistory, resolve_downloads_dir, source_key
from discogseek.clients.musicbrainz import MusicBrainzClient, ArtistCatalog
from discogseek.services.auditor import AuditorService
from discogseek.services.library import LibraryReleaseService
from discogseek.services.library_download import _search_key
from discogseek.services.candidates import (
    CandidateDir,
    CandidateFile,
    PeerCandidateIndex,
    alias_word_lists,
    directory_has_artist_context,
    disc_folder_group,
    evaluate_directory,
    file_has_artist_context,
    find_best_track_candidate,
    find_ranked_track_candidates,
    find_track_candidates,
    is_dir_name_match_fast,
    is_track_title_match_fast,
    names_other_artist,
    path_mentions_artist,
    pre_parse_expected_tracks,
    pre_parse_single_track,
    release_context,
    track_match_tier,
)
from discogseek.services.reconciler import DiscographyReconciler


# Keep candidate helpers available at their original import path.
__all__ = [
    "CandidateDir",
    "CandidateFile",
    "PeerCandidateIndex",
    "SlskdArtistScraper",
    "is_dir_name_match_fast",
    "is_track_title_match_fast",
    "pre_parse_expected_tracks",
    "pre_parse_single_track",
]

# The same forms MusicBrainzClient.resolve_artist_mbid accepts as identifiers.
_ARTIST_MBID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_ARTIST_URL_RE = re.compile(r"musicbrainz\.org/artist/[0-9a-f]{8}-[0-9a-f]{4}-", re.IGNORECASE)

# Folder names too generic to identify a copy of one release.
_GENERIC_FOLDERS = {
    "music", "mp3", "mp3s", "flac", "audio", "downloads", "download", "complete", "incomplete",
    "shared", "share", "singles", "tracks", "misc", "new folder", "various", "unsorted",
}

# Folder words that say nothing about which release a folder holds.
_FOLDER_NOISE_WORDS = _GENERIC_FOLDERS | {
    "singles", "single", "tracks", "track", "songs", "misc", "various", "artists", "collection",
    "discography", "kbps", "bit", "khz", "aac", "ogg", "opus", "m4a", "wav", "alac", "aiff",
    "hires", "hi", "res", "lossy", "unknown", "artist", "new", "folder", "files", "the",
}

_PEER_ERRORS = (SlskdPeerUnavailableError, SlskdPeerBlockedError, SlskdTransferFailedError)
# Submissions slskd definitely did not queue (apart from the error's queued_files):
# another source may supply those files. Any other error leaves it unknown.
_NOT_QUEUED_ERRORS = _PEER_ERRORS + (SlskdEnqueueRefusedError,)


def is_artist_identifier(query: str) -> bool:
    """Whether an artist query is a MusicBrainz ID or artist URL rather than a name."""
    text = (query or "").strip()
    return bool(_ARTIST_MBID_RE.match(text) or _ARTIST_URL_RE.search(text))


def _release_folder_name(path: str) -> str:
    """The release-level folder of a remote path: a disc folder gives its parent."""
    parts = [part for part in (path or "").replace("/", "\\").split("\\") if part.strip()]
    if not parts:
        return ""
    folder = parts[-1]
    if len(parts) >= 2 and (DISC_DIR_PATTERN.match(folder.strip()) or disc_number_from_folder(folder) is not None):
        group = disc_folder_group("\\".join(parts))
        folder = extract_dir_and_filename(group)[1] if group else parts[-2]
    return folder


def _is_release_folder(path: str, release: str, aliases: FrozenSet[str] = frozenset()) -> bool:
    """The release title (with or without its bracketed edition notes) names the folder path.

    A self-titled release ('Lone' by Lone) cannot be told apart from the
    artist's own folder, so no path names it.
    """
    if not release:
        return False
    core = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", " ", release).strip()
    if aliases and tuple(normalize_text(core or release).split()) in set(alias_word_lists(aliases)):
        return False
    if release_context(path, release) or bool(core and core != release and release_context(path, core)):
        return True
    # A double A-side ('Airglow Fires / Begin to Begin') is often named after one side.
    sides = [side for side in re.split(r"\s+/\s+", core or release) if side.strip()]
    return len(sides) > 1 and any(
        len("".join(_title_words(side, aliases))) >= 6 and release_context(path, side) for side in sides
    )


def _is_generic_title(title: str) -> bool:
    """Titles many different songs share ('Intro', 'Untitled 2', 'Home')."""
    words = normalize_text(title).split()
    return not words or len("".join(words)) <= 3 or all(
        word in GENERIC_OR_COMMON_WORDS or word.isdigit() for word in words
    )


def _folder_title_words(path: str, aliases: FrozenSet[str]) -> List[str]:
    """Words of a release folder's name besides the artist, years, catalog codes and format tags.

    '2015 - First Light [FLAC]' -> ['first', 'light']; 'Target Artist - Singles' -> [].
    """
    return _title_words(_release_folder_name(path), aliases)


def _title_words(name: str, aliases: FrozenSet[str]) -> List[str]:
    """Significant words of a release or folder name, without the artist's names."""
    words = normalize_text(name).split()
    for run in sorted(alias_word_lists(aliases), key=len, reverse=True):
        size = len(run)
        index = 0
        while index + size <= len(words):
            if tuple(words[index:index + size]) == run:
                del words[index:index + size]
            else:
                index += 1
    return [
        word for word in words
        if len(word) >= 3 and not any(char.isdigit() for char in word)
        and word not in DIR_STOP_WORDS and word not in _FOLDER_NOISE_WORDS
    ]


_DISC_NUMBER_RE = re.compile(r"(?i)((?:^|[\s_\-.(\[])(?:cd|disc|disk)\s*[-_.]?\s*)(\d{1,2})(?!\d)")


def _sibling_disc_folders(path: str, disc_count: int) -> List[str]:
    """The other disc folders a per-disc folder implies: 'Album\\CD1' -> ['Album\\CD2', ...]."""
    parent, base = extract_dir_and_filename(path or "")
    if disc_count < 2 or disc_number_from_folder(base) is None:
        return []
    matches = list(_DISC_NUMBER_RE.finditer(base))
    if not matches:
        return []
    number = matches[-1]
    own, width = int(number.group(2)), len(number.group(2))
    siblings = []
    for disc in range(1, disc_count + 1):
        if disc == own:
            continue
        name = base[:number.start(2)] + str(disc).zfill(width) + base[number.end(2):]
        siblings.append(f"{parent}\\{name}" if parent else name)
    return siblings


class _RunSearchCache(OrderedDict):
    """Finished searches of one run by normalized text (library_download's search_cache).

    slskd never reuses a search that found nothing, so those (small) entries are
    all kept, as are the discovery searches (pinned; their files stay indexed
    for the whole run anyway). Of the other searches that found files, only the
    most recent max_found are kept: an artist run makes hundreds of searches,
    and slskd's own recent search history still answers an older repeat
    without a new network search.
    """

    def __init__(self, max_found: int = 64):
        super().__init__()
        self.max_found = max_found
        self._pinned: Set[str] = set()

    def pin(self, key: str, value: Dict[str, Any]) -> None:
        self._pinned.add(key)
        self[key] = value

    def get(self, key, default=None):
        if key in self:
            self.move_to_end(key)
        return super().get(key, default)

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.move_to_end(key)
        found = [
            name for name, entry in self.items()
            if name not in self._pinned and (entry or {}).get("responses")
        ]
        for name in found[:max(0, len(found) - self.max_found)]:
            del self[name]


def track_failures(result: Dict[str, Any]) -> Dict[int, Tuple[str, str]]:
    """(reason, error) by id() of each requested row a release download reported an error for.

    A refused or failed submission ('could not be queued') outweighs a failed
    search, which outweighs finding only earlier failed sources (stage 'recovery').
    """
    ranks = {"recovery": 0, "search": 1}
    reasons: Dict[int, Tuple[int, str, str]] = {}
    for error in result.get("queue_errors") or []:
        if not isinstance(error, dict):
            continue
        stage = error.get("stage")
        rank = ranks.get(stage, 2)
        reason = "search failed" if stage == "search" else "could not be queued"
        for row in error.get("tracks") or []:
            if rank > reasons.get(id(row), (-1,))[0]:
                reasons[id(row)] = (rank, reason, str(error.get("error") or ""))
    return {key: (reason, message) for key, (_, reason, message) in reasons.items()}


def _copy_key(filename: str) -> Optional[Tuple[str, str]]:
    """(release folder, basename) naming one copy of a file on any peer.

    'Music\\Target Artist\\Prisms\\05 Glass.mp3' -> ('prisms', '05 glass.mp3'). A disc
    folder includes its release folder, and generic folders ('Music') name no copy.
    """
    parts = [part for part in filename.replace("/", "\\").split("\\") if part]
    if len(parts) < 2:
        return None
    folder = parts[-2]
    if DISC_DIR_PATTERN.match(folder.strip()) and len(parts) >= 3:
        folder = f"{parts[-3]} {folder}"
    norm = normalize_text(folder)
    if not norm or norm in _GENERIC_FOLDERS or len(norm.replace(" ", "")) < 4:
        return None
    # A year or format folder ('2019', 'MP3 320', 'Unknown Album') names no release.
    if all(word.isdigit() or word in DIR_STOP_WORDS or word in _FOLDER_NOISE_WORDS for word in norm.split()):
        return None
    return norm, parts[-1].lower()


class SlskdArtistScraper:
    """Orchestrates parallel Soulseek discovery, directory expansion, reconciliation, and queueing."""

    def __init__(
        self,
        artist_query: str,
        slskd_client: Optional[SlskdClient] = None,
        music_dir: Optional[Path] = None,
        preferred_format: str = "flac",
        min_match_ratio: float = 0.70,
        search_timeout: float = 30.0,
        dry_run: bool = False,
        threads: int = 6,
        full_scan: bool = False,
        force_refresh: bool = False,
        refresh_searches: bool = False,
        search_missing_tracks: bool = True,
    ):
        self.artist_query = artist_query.strip()
        # An MBID or MusicBrainz URL identifies the artist but is never a Soulseek search phrase.
        self.search_artist: Optional[str] = None if is_artist_identifier(self.artist_query) else self.artist_query
        self.client = slskd_client or SlskdClient()
        self.music_dir = Path(music_dir or Config.DEFAULT_LIBRARY_DIR).resolve() if (music_dir or Config.DEFAULT_LIBRARY_DIR) else None
        self.preferred_format = preferred_format.lower()
        self.min_match_ratio = min_match_ratio
        self.search_timeout = search_timeout
        self.dry_run = dry_run
        self.threads = threads
        self.full_scan = full_scan
        self.force_refresh = force_refresh
        # Fresh Soulseek searches instead of slskd's recent results for the same text.
        self.refresh_searches = refresh_searches
        # Search track by track for release tracks no discovered folder or file supplies.
        self.search_missing_tracks = search_missing_tracks
        self.enqueued_count = 0
        self.enqueued_files: List[Dict[str, Any]] = []
        self.queue_errors: List[str] = []
        self.search_errors: List[Dict[str, str]] = []
        # Finished searches of this run by normalized text, shared by every
        # release and track search so a query reaches Soulseek once per run.
        self.search_cache = _RunSearchCache()

        self.mb_client = MusicBrainzClient()
        self.catalog: Optional[ArtistCatalog] = None
        self.raw_mb_data: Dict[str, Any] = {}
        self.all_artist_aliases: Set[str] = set()

        # Titles of local tracks (informational); coverage uses song identities.
        self.local_found_map: Dict[str, Dict[str, Any]] = {}
        self.local_found_releases: Set[str] = set()
        self.local_song_keys: Set[Hashable] = set()
        self.local_release_ids: Set[str] = set()

        self.peer_directories: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.candidate_index: Optional[PeerCandidateIndex] = None
        self.searches_performed: Set[str] = set()
        self._browse_attempted: Set[Tuple[str, str]] = set()

        self.reconciled_release_keys: Set[str] = set()
        self.covered_track_titles: Set[str] = set()
        self.covered_song_keys: Set[Hashable] = set()
        self.queued_song_keys: Set[Hashable] = set()

        self.queued_directories: List[Dict[str, Any]] = []
        self._release_candidates: Dict[str, List[Dict[str, Any]]] = {}
        self._release_plans: Dict[str, Dict[str, Any]] = {}
        # Matching details of verified single-track items, by item identity.
        self._item_meta: Dict[int, Tuple[Dict[str, Any], Dict[str, Any]]] = {}
        self.already_downloading_files: Set[str] = set()
        # Sources whose peer refused or was unreachable during this run's queue pass.
        self._unavailable_sources: Set[Tuple[str, str]] = set()
        self.verified_releases: List[Dict[str, Any]] = []
        self.verified_release_tracks: List[Dict[str, Any]] = []
        self.unresolved_releases: List[Dict[str, Any]] = []
        self.unresolved_tracks: List[Dict[str, Any]] = []
        # Tracks of splits and appearances that their release download left missing.
        self._full_release_unresolved: List[Dict[str, Any]] = []
        self.verified_compilation_tracks: List[Dict[str, Any]] = []
        self.unresolved_compilation_tracks: List[Dict[str, Any]] = []
        self.verified_standalone_tracks: List[Dict[str, Any]] = []
        self.unresolved_standalone_tracks: List[Dict[str, Any]] = []
        self.full_release_audits: List[Dict[str, Any]] = []
        self.release_downloads: List[Dict[str, Any]] = []
        self._full_release_ids: Set[str] = set()

        self._peer_failures: List[Tuple[Exception, List[Tuple[str, FrozenSet[Hashable]]]]] = []
        self._peer_failure_messages: List[str] = []

        self._song_key_cache: Dict[int, Tuple[Dict[str, Any], FrozenSet[Hashable]]] = {}
        self._appearances: Optional[Dict[str, Set[Hashable]]] = None
        self._title_index: Optional[Dict[str, List[Dict[str, Any]]]] = None
        self._release_song_index: Optional[Dict[Tuple[str, ...], Set[Hashable]]] = None

    def run(self) -> Dict[str, Any]:
        """Runs the complete Soulseek discography discovery and queueing pipeline."""
        # 1. Check slskd Connection
        logging.getLogger(__name__).info("Connecting to slskd...")
        app_info = self.client.get_application()
        slsk_user = app_info.get("user", {}).get("username", "Unknown")
        server_state = app_info.get("server", {}).get("state", "Unknown")
        logging.getLogger(__name__).info(f"✔ Connected to slskd (User: {slsk_user} | Server: {server_state})")

        logging.getLogger(__name__).info("Checking existing slskd downloads...")
        self.already_downloading_files = self.client.get_queued_filenames()

        # 2. Resolve Artist & Catalog
        logging.getLogger(__name__).info("Resolving artist on MusicBrainz: %s...", self.artist_query)
        mbid, canonical_name = self.mb_client.resolve_artist_mbid(self.artist_query)
        logging.getLogger(__name__).info("Loading MusicBrainz discography for %s...", canonical_name)
        self.raw_mb_data = self.mb_client.fetch_full_discography(mbid, force_refresh=self.force_refresh)
        self.catalog = ArtistCatalog(self.raw_mb_data)

        self.all_artist_aliases = set(self.catalog.aliases)
        self.all_artist_aliases.add(self.catalog.name)
        for a in list(self.all_artist_aliases):
            self.all_artist_aliases.add(unidecode(a))

        logging.getLogger(__name__).info(f"✔ Canonical Name: {self.catalog.name} (MBID: {self.catalog.mbid})")
        logging.getLogger(__name__).info(f"Catalog: {len(self.catalog.tracks)} total tracks | {len(self.catalog.primary_releases)} primary releases")

        # 3. Pre-scan local library
        self._prescan_library()

        # 4. Search Soulseek
        self._discover_soulseek_candidates()

        # 5. Reconcile primary releases
        self._reconcile_primary_releases()

        # 6. Reconcile standalone recordings; appearances keep their full release scope.
        self._reconcile_standalone_tracks()

        # 7. Queue downloads
        if not self.dry_run:
            self._queue_downloads()
        else:
            logging.getLogger(__name__).info("\n--dry-run enabled: Showing matched directories without enqueuing transfers.")

        self._download_full_releases()
        self._search_unfilled_tracks()
        for entry in self.unresolved_tracks:
            position = f"{entry['disc'] or 1}.{entry['position']} " if entry.get("position") else ""
            logging.getLogger(__name__).info(
                "Unresolved track: %s | %s%s (%s)", entry["release"], position, entry["title"], entry["reason"],
            )

        return {
            "artist": self.catalog.name,
            "mbid": self.catalog.mbid,
            "queued_directories": self.queued_directories,
            "matched_files": self._matched_files(),
            "verified_releases": self.verified_releases,
            "verified_release_tracks": self.verified_release_tracks,
            "verified_compilation_tracks": self.verified_compilation_tracks,
            "verified_standalone_tracks": self.verified_standalone_tracks,
            "release_downloads": self.release_downloads,
            "unresolved_releases": self.unresolved_releases,
            "unresolved_tracks": self.unresolved_tracks,
            "unresolved_compilation_tracks": self.unresolved_compilation_tracks,
            "unresolved_standalone_tracks": self.unresolved_standalone_tracks,
            "enqueued_count": self.enqueued_count,
            "queued_files": self.enqueued_files,
            "queue_errors": self.queue_errors,
            "search_errors": self.search_errors,
            "dry_run": self.dry_run,
        }

    def _matched_files(self) -> List[Dict[str, Any]]:
        """Every file matching chose for a missing song, as a queue pass would try them first.

        Folders in score order supply one file per song (a partial folder leaves
        its other songs to the next folder), then loose files, then compilation
        and standalone tracks. Release and track searches report their own files.
        """
        files: List[Dict[str, Any]] = []
        for plan in self._release_plans.values():
            taken: Set[int] = set()
            for candidate in plan["candidates"]:
                targets = self._slot_targets(plan, candidate)
                for file in candidate["all_dir_files"]:
                    slot = targets.get(file.get("filename"))
                    if slot is not None and slot not in taken:
                        taken.add(slot)
                        files.append({**file, "user": candidate["user"]})
            for candidates in plan["track_slots"].values():
                files.append({**candidates[0].raw_file, "filename": candidates[0].full_filename,
                              "user": candidates[0].user})
        for item in self.verified_compilation_tracks + self.verified_standalone_tracks:
            files.append({**item["file"], "user": item["user"]})
        return files

    # ------------------------------------------------------------------
    # Song identity
    # ------------------------------------------------------------------

    def _catalog_appearances(self) -> Dict[str, Set[Hashable]]:
        """Per recording ID, each (release group, title, occurrence) place it fills.

        Two recordings that fill the same place in one release group (an album
        and its remaster) are one song; the same title in another release group
        is a different song unless the recording is the same.
        """
        if self._appearances is None:
            appearances: Dict[str, Set[Hashable]] = {}
            raw = (self.catalog.raw_data if self.catalog else None) or {}
            for release in raw.get("releases_artist", []) + raw.get("releases_track_artist", []):
                group = (release.get("release-group") or {}).get("id") or release.get("id")
                seen: Counter = Counter()
                for medium in release.get("medium-list", []):
                    for track in medium.get("track-list", []):
                        recording = track.get("recording") or {}
                        title = normalize_text(track.get("title") or recording.get("title"))
                        occurrence = seen[title]
                        seen[title] += 1
                        if recording.get("id") and title and group:
                            appearances.setdefault(recording["id"], set()).add(("app", group, title, occurrence))
            self._appearances = appearances
        return self._appearances

    def _song_keys(self, track: Dict[str, Any]) -> FrozenSet[Hashable]:
        """Identity keys of a catalog track; two tracks sharing a key are the same song."""
        owner, cached = self._song_key_cache.get(id(track), (None, None))
        if owner is not track or cached is None:
            recordings = set(track.get("recording_ids") or ())
            keys: Set[Hashable] = {("rec", recording) for recording in recordings}
            appearances = self._catalog_appearances()
            for recording in recordings:
                keys |= appearances.get(recording, set())
            if not recordings:
                # Without a recording ID nothing distinguishes same-titled songs.
                keys.add(("title", track.get("norm_title") or normalize_text(track.get("title"))))
            cached = frozenset(keys)
            self._song_key_cache[id(track)] = (track, cached)
        return cached

    def _title_is_unique(self, title: str, keys: FrozenSet[Hashable]) -> bool:
        """Whether every catalog track with this title is the same song."""
        if self._title_index is None:
            self._title_index = {}
            for track in self.catalog.tracks if self.catalog else []:
                self._title_index.setdefault(track.get("norm_title") or normalize_text(track.get("title")), []).append(track)
        return all(self._song_keys(track) & keys for track in self._title_index.get(normalize_text(title), []))

    def _release_family(self, rel: Dict[str, Any]) -> List[Dict[str, Any]]:
        """The artist's editions of a release: the same title, or the same release group.

        'Album (Expanded)' and 'Album' are one release group; the catalog lists
        only one of them, so the other's bonus tracks belong to the same release.
        """
        raw = (self.catalog.raw_data if self.catalog else None) or {}
        releases = raw.get("releases_artist", [])
        norm_title = normalize_text(rel.get("title"))
        groups = {rel.get("release_group_id")} | {
            (metadata.get("release-group") or {}).get("id") for metadata in releases
            if metadata.get("id") == rel.get("id") or normalize_text(metadata.get("title")) == norm_title
        }
        groups.discard(None)
        return [
            metadata for metadata in releases
            if (metadata.get("id") == rel.get("id") or normalize_text(metadata.get("title")) == norm_title
                or (metadata.get("release-group") or {}).get("id") in groups)
            and (metadata.get("id") == rel.get("id") or metadata.get("id") not in self._full_release_ids)
        ]

    def _release_expected_tracks(self, rel_title: str, rel: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Catalog tracks of a release, including every edition in its family when rel is given."""
        titles = {normalize_text(rel_title)}
        if rel is not None:
            titles.update(normalize_text(metadata.get("title")) for metadata in self._release_family(rel))
        return [
            t for t in self.catalog.tracks
            if titles.intersection(normalize_text(r) for r in t.get("all_releases", set()))
            or t.get("norm_release") in titles
        ]

    def _release_editions(self, rel: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Every edition of the release, with position details by track and recording ID."""
        editions = []
        for metadata in self._release_family(rel):
            positions: Dict[str, Dict[str, Any]] = {}
            absolute = 0
            for medium_index, medium in enumerate(metadata.get("medium-list", []), 1):
                try:
                    disc = int(medium.get("position") or medium_index)
                except (TypeError, ValueError):
                    disc = medium_index
                tracks = medium.get("track-list", [])
                for track_index, track in enumerate(tracks, 1):
                    absolute += 1
                    recording = track.get("recording") or {}
                    try:
                        position = int(track.get("position") or track_index)
                    except (TypeError, ValueError):
                        position = track_index
                    length = track.get("length") or recording.get("length")
                    try:
                        duration = round(int(length) / 1000.0, 1) if length else None
                    except (TypeError, ValueError):
                        duration = None
                    info = {
                        "title": track.get("title") or recording.get("title"),
                        "recording_title": recording.get("title"),
                        "disc_number": disc,
                        "position_token": str(track.get("number") or position),
                        "track_position": position,
                        "absolute_position": absolute,
                        "medium_track_count": len(tracks),
                        "mb_track_id": track.get("id"),
                        "mb_recording_id": recording.get("id"),
                        "duration": duration,
                    }
                    for key in (track.get("id"), recording.get("id")):
                        if key:
                            positions.setdefault(key, info)
            editions.append({"id": metadata.get("id"), "positions": positions, "track_count": absolute})
        return editions

    def _chosen_edition(self, editions: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """The edition whose numbering applies: the one in the library, else the fullest."""
        local = [edition for edition in editions if edition["id"] in self.local_release_ids]
        return max(local or editions, key=lambda edition: edition["track_count"], default=None)

    def _plan_release(self, rel: Dict[str, Any], expected_tracks: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Group a release's expected tracks into songs and find those missing locally."""
        editions = self._release_editions(rel)
        chosen = self._chosen_edition(editions)
        positions = chosen["positions"] if chosen else {}
        keys = [self._song_keys(track) for track in expected_tracks]
        parent = list(range(len(expected_tracks)))

        def root(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        for index in range(len(expected_tracks)):
            for other in range(index):
                if keys[index] & keys[other]:
                    first, second = root(other), root(index)
                    if first != second:
                        parent[max(first, second)] = min(first, second)
        slot_of = [root(index) for index in range(len(expected_tracks))]

        slots: Dict[int, Dict[str, Any]] = {}
        track_positions: List[Optional[Tuple[Optional[int], Optional[int]]]] = []
        for index, track in enumerate(expected_tracks):
            info = next((positions[key] for key in sorted(track.get("track_ids") or ()) if key in positions), None)
            if info is None:
                info = next((positions[key] for key in sorted(track.get("recording_ids") or ()) if key in positions), None)
            track_positions.append((info["disc_number"], info["track_position"]) if info else None)
            slot = slots.setdefault(slot_of[index], {
                "id": slot_of[index], "indices": [], "tracks": [], "keys": frozenset(), "info": None,
            })
            slot["indices"].append(index)
            slot["tracks"].append(track)
            slot["keys"] = slot["keys"] | keys[index]
            if slot["info"] is None and info is not None:
                slot["info"] = info
        title_counts = Counter(
            normalize_text((slot["info"] or {}).get("title") or slot["tracks"][0].get("title"))
            for slot in slots.values()
        )
        for slot in slots.values():
            info = slot["info"] or {}
            slot["track"] = slot["tracks"][0]
            slot["title"] = info.get("title") or slot["track"].get("title", "")
            slot["position"] = (info["disc_number"], info["track_position"]) if info else None
            slot["repeated"] = title_counts[normalize_text(slot["title"])] > 1
            slot["unique"] = not slot["repeated"] and self._title_is_unique(slot["track"].get("title", ""), slot["keys"])
        needed = [slot_id for slot_id, slot in slots.items() if not (slot["keys"] & self.local_song_keys)]

        def on_edition(track: Dict[str, Any], edition: Dict[str, Any]) -> bool:
            ids = set(track.get("track_ids") or ()) | set(track.get("recording_ids") or ())
            return bool(ids.intersection(edition["positions"]))

        edition_slots = [
            {slot_of[index] for index, track in enumerate(expected_tracks) if on_edition(track, edition)}
            for edition in editions
        ]
        return {
            "release": rel.get("title", ""),
            "release_id": rel.get("id"),
            "rel": rel,
            "expected_tracks": expected_tracks,
            "slot_of": slot_of,
            "slots": slots,
            "positions": track_positions,
            "editions": [songs for songs in edition_slots if songs],
            "needed": needed,
            "has_local": len(needed) < len(slots),
            "candidates": [],
            "dir_slots": set(),
            "track_slots": {},
            "unfilled": set(),
            "unqueued": set(),
            "queue_failed": set(),
            "resolved": set(),
            # Error of the track search, by slot, when it failed rather than found nothing.
            "search_failed": {},
            # Folder of the only copy track searches found, by slot, when it is another release's.
            "other_release": {},
        }

    # ------------------------------------------------------------------
    # Library prescan and full releases
    # ------------------------------------------------------------------

    def _prescan_library(self) -> None:
        logging.getLogger(__name__).info("Scanning local and configured remote libraries...")
        local_tracks = AuditorService(mb_client=self.mb_client).scan_library(
            self.catalog, self.music_dir, full_scan=self.full_scan, threads=self.threads,
        )
        self._audit_full_releases(local_tracks)

        logging.getLogger(__name__).info("Comparing %s library tracks with the discography...", len(local_tracks))
        reconciler = DiscographyReconciler(catalog=self.catalog, local_tracks=local_tracks)
        found_items, _ = reconciler.reconcile()

        for item in found_items:
            mb = item["mb_track"]
            norm_t = mb.get("norm_title", "")
            if norm_t:
                self.local_found_map[norm_t] = item["local_track"]
            # A local file accounts for its own recording only, never for a
            # same-titled song on another release.
            self.local_song_keys |= self._song_keys(mb)
            self.local_release_ids.update(item["local_track"].get("mb_release_ids") or ())

        self.covered_track_titles.update(self.local_found_map)

        for rel in self.catalog.releases:
            if rel.get("is_va") or rel.get("id") in self._full_release_ids:
                continue
            rel_title = rel.get("title", "")
            norm_rel = normalize_text(rel_title)
            rel_tracks = self._release_expected_tracks(rel_title, rel)
            if not rel_tracks:
                continue

            if all(self._song_keys(t) & self.local_song_keys for t in rel_tracks):
                self.local_found_releases.add(norm_rel)
                self.reconciled_release_keys.add(norm_rel)

        logging.getLogger(__name__).info(f"✔ Library Status: {len(found_items)} artist tracks / {len(self.local_found_releases)} releases already in library.")

    def _credits_other_artist(self, credit_list: Any, aliases: Set[str]) -> bool:
        """Whether a track's MusicBrainz credit leaves out this artist."""
        if not credit_list:
            return False  # The track inherits the release's credit.
        ids = [
            credit.get("artist", {}).get("id") for credit in credit_list
            if isinstance(credit, dict) and credit.get("artist", {}).get("id")
        ]
        if ids:
            return self.catalog.mbid not in ids
        display = ArtistCatalog.format_credit(credit_list)
        return bool(display) and not artist_credit_matches(self.catalog.name, display, aliases)

    def _audit_full_releases(self, library_tracks: List[Dict[str, Any]]) -> None:
        """Audit complete appearances and splits using the shared library inventory."""
        raw = self.catalog.raw_data
        primary_ids = {release.get("id") for release in self.catalog.primary_releases}
        primary_groups = {
            release["release_group_id"] for release in self.catalog.primary_releases
            if release.get("release_group_id")
        }
        aliases = {normalize_text(name) for name in self.catalog.aliases | {self.catalog.name}}
        service = LibraryReleaseService(mb_client=self.mb_client, slskd_client=self.client)
        self.full_release_audits = []
        self._full_release_ids = set()
        seen = set()
        releases = [(False, release) for release in raw.get("releases_artist", [])]
        releases += [(True, release) for release in raw.get("releases_track_artist", [])]
        for appearance, metadata in releases:
            release_id = metadata.get("id")
            group_id = metadata.get("release-group", {}).get("id")
            identity = group_id or release_id
            if identity in seen:
                continue
            credit_lists = [
                track.get("artist-credit") or track.get("recording", {}).get("artist-credit")
                for medium in metadata.get("medium-list", [])
                for track in medium.get("track-list", [])
            ]
            credits = [ArtistCatalog.format_credit(credit_list) for credit_list in credit_lists]
            if appearance:
                # MusicBrainz often returns the same album in both artist searches.
                if release_id in primary_ids or group_id in primary_groups:
                    continue
            elif not any(self._credits_other_artist(credit_list, aliases) for credit_list in credit_lists):
                # A 'feat.' credit still names this artist: an ordinary album.
                continue

            seen.add(identity)
            self._full_release_ids.add(release_id)
            if group_id:
                self._full_release_ids.update(
                    release.get("id") for release in self.catalog.primary_releases
                    if release.get("release_group_id") == group_id
                )
            title = metadata.get("title", "")
            norm_title = normalize_text(title)
            artist = ArtistCatalog.format_credit(
                metadata.get("artist-credit"),
                default="Various Artists" if appearance else self.catalog.name,
            )
            credited_artists = {normalize_text(credit) for credit in credits if credit}
            credited_names = {
                name for credit in metadata.get("artist-credit") or [] if isinstance(credit, dict)
                for name in (credit.get("name"), credit.get("artist", {}).get("name")) if name
            }
            for medium in metadata.get("medium-list", []):
                for track in medium.get("track-list", []):
                    for credit in (track.get("artist-credit")
                                   or track.get("recording", {}).get("artist-credit") or []):
                        if isinstance(credit, dict):
                            credited_artists.update(normalize_text(name) for name in (
                                credit.get("name"), credit.get("artist", {}).get("name"),
                            ) if name)
            release_names = credited_names | set(self.catalog.aliases) | {self.catalog.name}
            tracks, recording_candidates = [], []
            for track in library_tracks:
                release_ids = track.get("mb_release_ids") or set()
                if release_id in release_ids:
                    tracks.append(dict(track))
                    continue
                album = normalize_text(track.get("album") or track.get("norm_album", ""))
                if album != norm_title:
                    # Untagged audio can still be grouped by its release folder.
                    path = normalize_text(str(track.get("path", "")))
                    if album or not norm_title or not re.search(
                        r"(?<!\w)" + re.escape(norm_title) + r"(?!\w)", path,
                    ):
                        continue
                album_artist = track.get("album_artist")
                # Tags often name one credited artist, another joiner ('x', 'vs')
                # or another credited spelling of the same release credit.
                if (album_artist and not is_various_artists(album_artist)
                        and (is_various_artists(artist)
                             or not artist_credit_matches(artist, album_artist, release_names))):
                    track_artists = {normalize_text(name) for name in track.get("artists", [])}
                    if not is_various_artists(artist) or not track_artists.intersection(credited_artists):
                        continue
                # Other tagged editions can only supply exact recordings at the
                # same position, as in the incomplete-release browser.
                (recording_candidates if release_ids else tracks).append(dict(track))
            audit = service.audit_release({
                "artist": artist, "title": title, "mb_release_id": release_id,
                "is_va": appearance, "tracks": tracks,
                "recording_candidates": recording_candidates,
            }, mb_release=metadata)
            audit["artist_track_ids"] = {
                track.get("id")
                for medium in metadata.get("medium-list", [])
                for track in medium.get("track-list", [])
                if track.get("id") and any(
                    isinstance(credit, dict) and credit.get("artist", {}).get("id") == self.catalog.mbid
                    for credit in (track.get("artist-credit")
                                   or track.get("recording", {}).get("artist-credit")
                                   or metadata.get("artist-credit", []))
                )
            }
            self.full_release_audits.append(audit)
            logging.getLogger(__name__).info(
                "Full release: %s — %s found, %s missing", title,
                audit.get("found_count", 0), audit.get("missing_count", 0),
            )

    def _download_full_releases(self) -> None:
        """Prefer a verified complete release; otherwise fetch only this artist's tracks."""
        service = LibraryReleaseService(mb_client=self.mb_client, slskd_client=self.client)
        logger = logging.getLogger(__name__)
        # A dry run queues nothing: the songs found folders and files would queue
        # count as queued, so it plans the same searches as a real run.
        planned = self.covered_song_keys if self.dry_run else set()
        for audit in self.full_release_audits:
            missing = [track for track in audit.get("tracks", []) if track.get("status") == "missing"]
            if not missing:
                continue
            title = audit.get("mb_release_title") or audit["title"]
            progress = lambda done, total, message: logger.info(
                "%s [%s/%s tracks matched]", message, done, total,
            )
            result = service.download_missing_tracks(
                artist=audit["artist"], release_title=title, missing_tracks=missing,
                preferred_format=self.preferred_format, search_timeout=self.search_timeout,
                dry_run=self.dry_run,
                on_progress=progress,
                complete_release_tracks=[track for track in audit["tracks"] if track.get("mb_track_id")],
                search_cache=self.search_cache,
                use_existing=self._reuse_searches(),
            )
            scope = "release"
            if not result.get("complete_release_available"):
                missing = [track for track in missing if track.get("mb_track_id") in audit["artist_track_ids"]]
                # Only this artist's songs, and only once: a recording already in
                # the library or queued for another release (a compilation or DJ
                # mix repeating an album track) is not fetched again.
                missing = [
                    track for track in missing
                    if not (track.get("mb_recording_id") and ("rec", track["mb_recording_id"]) in (
                        self.local_song_keys | self.queued_song_keys | planned))
                ]
                logger.info(
                    "No complete source verified for %s; searching only %s's %s missing tracks.",
                    title, self.catalog.name, len(missing),
                )
                if not missing:
                    continue
                result = service.download_missing_tracks(
                    artist=audit["artist"], release_title=title, missing_tracks=missing,
                    preferred_format=self.preferred_format, search_timeout=self.search_timeout,
                    dry_run=self.dry_run, on_progress=progress, search_scope="track",
                    # The whole tracklist, so titles and positions of found tracks still count as evidence.
                    official_tracks=[track for track in audit["tracks"] if not track.get("is_extra")],
                    search_cache=self.search_cache,
                    use_existing=self._reuse_searches(),
                )
                scope = "artist_tracks"
            result["download_scope"] = scope
            self.release_downloads.append(dict(result, release_id=audit.get("mb_release_id")))
            if not self.dry_run:
                enqueued = result.get("enqueued_files", [])
                self.enqueued_count += len(enqueued)
                self.enqueued_files.extend(enqueued)
            self.queue_errors.extend(
                f"{title}: {error.get('error', str(error))}" for error in result.get("queue_errors", [])
            )
            # A recording queued for a split or appearance is not needed again
            # for one of the artist's own releases.
            resolved_rows = result.get("matched_tracks" if self.dry_run else "queued_tracks") or []
            for row in resolved_rows:
                if row.get("mb_recording_id"):
                    self.queued_song_keys.add(("rec", row["mb_recording_id"]))
            # Each requested track that was neither queued (matched in a dry run) nor
            # protected by history, so a partly completed release shows what is left.
            reasons = track_failures(result)
            unresolved = []
            for row in missing:
                if any(row is item for item in resolved_rows):
                    continue
                reason, error = reasons.get(id(row), ("no matching file found", ""))
                entry = {
                    "release": title, "release_id": audit.get("mb_release_id"), "title": row.get("title", ""),
                    "disc": row.get("disc_number"), "position": row.get("position_token") or row.get("track_number"),
                    "reason": reason,
                }
                unresolved.append(dict(entry, error=error) if error else entry)
            self._full_release_unresolved.extend(unresolved)
            if unresolved:
                self.unresolved_releases.append({"id": audit.get("mb_release_id"), "title": title})
        self._refresh_unresolved()

    # ------------------------------------------------------------------
    # Soulseek discovery
    # ------------------------------------------------------------------

    def _generate_all_search_queries(self) -> List[str]:
        queries: List[str] = []
        search_artist = self.search_artist or ""

        # 1. Primary Artist Names (User query + Canonical MB Name)
        if search_artist:
            q_user = clean_search_phrase(search_artist)
            if q_user and q_user not in queries:
                queries.append(q_user)

        if self.catalog and self.catalog.name:
            q_name = clean_search_phrase(self.catalog.name)
            if q_name and q_name not in queries:
                queries.append(q_name)

        # 2. Targeted Primary Release Queries (User Query + Release, Canonical Name + Release, Release Title)
        if self.catalog:
            for rel in self.catalog.primary_releases:
                if rel.get("id") in self._full_release_ids:
                    continue
                rel_title = rel.get("title", "")
                norm_rel = normalize_text(rel_title)
                if not rel_title or norm_rel in self.local_found_releases:
                    continue

                clean_rel = clean_search_phrase(rel_title)

                # Query with user artist query (e.g. "Stellabee Breakcore Forever")
                if search_artist:
                    q_user_rel = clean_search_phrase(f"{search_artist} {rel_title}")
                    if q_user_rel and q_user_rel not in queries:
                        queries.append(q_user_rel)

                # Query with canonical name (e.g. "すてらべえ Breakcore Forever")
                if self.catalog.name and self.catalog.name.lower() != search_artist.lower():
                    q_canon_rel = clean_search_phrase(f"{self.catalog.name} {rel_title}")
                    if q_canon_rel and q_canon_rel not in queries:
                        queries.append(q_canon_rel)

                # If the release title is distinctive (len >= 4 and not generic noise), add release title query
                if clean_rel and len(clean_rel) >= 4 and clean_rel.lower() not in DIR_STOP_WORDS and clean_rel.lower() not in GENERIC_OR_COMMON_WORDS:
                    if clean_rel not in queries:
                        queries.append(clean_rel)

        # 3. Artist Aliases (Ordered by relevance, skipping overly short/symbolic noise)
        if self.catalog:
            for alias in self.catalog.aliases:
                alias_clean = clean_search_phrase(alias)
                if alias_clean and len(alias_clean) >= 3 and alias_clean.lower() != search_artist.lower() and alias_clean.lower() != self.catalog.name.lower():
                    if alias_clean not in queries:
                        queries.append(alias_clean)

        return list(dict.fromkeys(q for q in queries if q and len(q) >= 3 and not is_artist_identifier(q)))

    def _reuse_searches(self) -> bool:
        """Whether every search of this run may reuse slskd's recent results; a refresh asks the network again."""
        return not (self.force_refresh or self.refresh_searches)

    def _discover_soulseek_candidates(self) -> None:
        logger = logging.getLogger(__name__)
        all_queries = self._generate_all_search_queries()
        logger.info(f"\nExecuting parallel Soulseek searches ({len(all_queries)} targeted queries)...")

        batch_results = self.client.batch_search(
            all_queries, timeout=self.search_timeout, poll_interval=1.0, use_existing=self._reuse_searches(),
        )
        total_files = 0
        for query_str, s_data in batch_results.items():
            if s_data.get("error"):
                logger.warning("Soulseek search '%s' failed: %s", query_str, s_data["error"])
                self.search_errors.append({"query": query_str, "error": str(s_data["error"])})
            else:
                # Release and track searches of this run reuse it; a failed search stays retryable.
                self.search_cache.pin(_search_key(query_str), s_data)
            self.searches_performed.add(query_str)
            for resp in s_data.get("responses", []):
                user = resp.get("username")
                speed = resp.get("uploadSpeed", 0)
                queue = resp.get("queueLength", 0)
                has_slot = resp.get("hasFreeUploadSlot", True)
                for f in resp.get("files", []):
                    fn = f.get("filename", "")
                    if not fn or f.get("isLocked", False):
                        continue
                    dir_name, _ = extract_dir_and_filename(fn)
                    if not dir_name:
                        continue
                    key = (user, dir_name)
                    if key not in self.peer_directories:
                        self.peer_directories[key] = {
                            "user": user,
                            "directory": dir_name,
                            "speed": speed,
                            "queue": queue,
                            "has_slot": has_slot,
                            "matched_search_files": [],
                            "full_directory_files": None,
                            "_seen_files": set()
                        }
                    fn_norm = fn.strip().lower()
                    if fn_norm not in self.peer_directories[key]["_seen_files"]:
                        self.peer_directories[key]["_seen_files"].add(fn_norm)
                        self.peer_directories[key]["matched_search_files"].append(f)
                        total_files += 1

        logger.info(f"✔ Soulseek Discovery: Found {len(self.peer_directories)} candidate directories ({total_files} candidate files).")
        self.candidate_index = PeerCandidateIndex(self.peer_directories)

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    def _evaluate_indexed_directory(
        self,
        cd: CandidateDir,
        parsed_expected: List[Dict[str, Any]],
        expected_tracks: List[Dict[str, Any]],
        rel_title: str,
        track_keys: Optional[List[Hashable]] = None,
        positions: Optional[List[Optional[Tuple[Optional[int], Optional[int]]]]] = None,
    ) -> Optional[Dict[str, Any]]:
        return evaluate_directory(
            cd,
            parsed_expected,
            expected_tracks,
            rel_title,
            self.all_artist_aliases,
            self.preferred_format,
            self.min_match_ratio,
            track_keys=track_keys,
            positions=positions,
        )

    def _is_unbrowsed(self, cd: CandidateDir) -> bool:
        return any(self._needs_browse(cd.user, folder) for folder in cd.disc_dirs)

    def _needs_browse(self, user: str, folder: str) -> bool:
        # A folder is asked for once per run, even when the peer did not answer.
        return (
            (user, folder) not in self._browse_attempted
            and self.peer_directories.get((user, folder), {}).get("full_directory_files") is None
        )

    def _tracklist_evidence_dirs(self, plan: Dict[str, Any], parsed_expected: List[Dict[str, Any]]) -> List[CandidateDir]:
        """Folders named by catalog number or year that hold the release's exact tracklist.

        The release needs four songs or more; nine in ten of its edition's songs
        must match exactly, and the folder or its files must name the artist.
        """
        edition = [index for index, position in enumerate(plan["positions"]) if position is not None]
        edition = edition or list(range(len(parsed_expected)))
        songs = {plan["slot_of"][index] for index in edition}
        if len(songs) < 4:
            return []
        threshold = 0.9 * len(songs)
        hits: Dict[Tuple[str, str], Set[int]] = {}
        for index in edition:
            for cf in self.candidate_index.files_with_base(parsed_expected[index]["p_struct"]["base_norm"]):
                hits.setdefault((cf.user, cf.dir_name), set()).add(plan["slot_of"][index])
                group = self.candidate_index.group_for(cf.user, cf.dir_name)
                if group is not None:
                    hits.setdefault((group.user, group.dir_name), set()).add(plan["slot_of"][index])
        directories = []
        for (user, directory), matched in sorted(hits.items()):
            if len(matched) < threshold:
                continue
            cd = self.candidate_index.disc_groups.get((user, directory)) or self.candidate_index.dirs_map.get((user, directory))
            if cd is not None and directory_has_artist_context(cd, self.all_artist_aliases):
                directories.append(cd)
        return directories

    def _browse_rank(self, rel_title: str, cd: CandidateDir, evaluation: Optional[Dict[str, Any]]):
        """Browse folders with the most matched songs and the artist's name first."""
        folder = disc_folder_group(cd.dir_name) or cd.dir_name
        basename = extract_dir_and_filename(folder)[1]
        return (
            -(evaluation["matched_song_count"] if evaluation else 0),
            not directory_has_artist_context(cd, self.all_artist_aliases),
            -calculate_similarity(normalize_text(rel_title), normalize_text(basename)),
            not cd.dir_info.get("has_slot", True),
            cd.dir_info.get("queue", 0),
            cd.user.casefold(),
            cd.dir_name.casefold(),
        )

    def _slot_targets(self, plan: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, int]:
        slot_of = plan["slot_of"]
        return {
            match["full_filename"]: slot_of[match["expected_index"]]
            for match in candidate.get("matched_tracks", [])
            if isinstance(match.get("expected_index"), int) and match["expected_index"] < len(slot_of)
        }

    def _unresolved_entry(self, plan: Dict[str, Any], slot_id: int, reason: str) -> Dict[str, Any]:
        slot = plan["slots"][slot_id]
        info = slot["info"] or {}
        return {
            "release": plan["release"],
            "release_id": plan["release_id"],
            "title": slot["title"],
            "disc": info.get("disc_number"),
            "position": info.get("position_token"),
            "reason": reason,
        }

    def _refresh_unresolved(self) -> None:
        """Report every needed release track that no source supplied or could queue."""
        entries = []
        for plan in self._release_plans.values():
            unresolved = []
            for slot_id in plan["needed"]:
                slot = plan["slots"][slot_id]
                if slot_id in plan["resolved"] or slot["keys"] & self.queued_song_keys:
                    continue
                if slot_id in plan["unqueued"] or slot_id in plan["queue_failed"]:
                    unresolved.append(self._unresolved_entry(plan, slot_id, "could not be queued"))
                elif slot_id in plan["search_failed"]:
                    # The track search failed: nothing says the track is unavailable.
                    entry = self._unresolved_entry(plan, slot_id, "search failed")
                    unresolved.append(dict(entry, error=plan["search_failed"][slot_id]))
                elif slot_id in plan["other_release"]:
                    entry = self._unresolved_entry(plan, slot_id, "only another release's copy found")
                    unresolved.append(dict(entry, folder=plan["other_release"][slot_id]))
                elif slot_id in plan["unfilled"]:
                    unresolved.append(self._unresolved_entry(plan, slot_id, "no matching file found"))
            entries.extend(unresolved)
            rel = plan["rel"]
            listed = any(item is rel for item in self.unresolved_releases)
            if unresolved and not listed:
                self.unresolved_releases.append(rel)
            elif not unresolved and listed and (plan["resolved"] or plan["candidates"] or plan["track_slots"]):
                self.unresolved_releases = [item for item in self.unresolved_releases if item is not rel]
        self.unresolved_tracks = entries + self._full_release_unresolved

    def _reconcile_primary_releases(self) -> None:
        logger = logging.getLogger(__name__)
        primary_rels = self.catalog.primary_releases
        logger.info(f"\nVerifying Primary Releases ({len(primary_rels)} releases)...")

        if self.candidate_index is None:
            self.candidate_index = PeerCandidateIndex(self.peer_directories)

        for index, rel in enumerate(primary_rels, 1):
            if rel.get("id") in self._full_release_ids:
                continue
            rel_title = rel.get("title", "")
            norm_rel = normalize_text(rel_title)
            if norm_rel in self.local_found_releases or norm_rel in self.reconciled_release_keys:
                continue
            self.reconciled_release_keys.add(norm_rel)
            logger.info("Checking release %s/%s: %s...", index, len(primary_rels), rel_title)

            expected_tracks = self._release_expected_tracks(rel_title, rel)
            if not expected_tracks:
                continue
            plan = self._plan_release(rel, expected_tracks)
            if not plan["needed"]:
                continue
            self._release_plans[norm_rel] = plan
            needed = set(plan["needed"])

            parsed_expected = pre_parse_expected_tracks(expected_tracks)
            clean_rel = clean_tokens(rel_title)
            rel_words = [w for w in _tokenize_words_cached(rel_title) if w not in DIR_STOP_WORDS]
            rel_sig_words = {w for w in rel_words if len(w) >= 3}

            def evaluate(cd: CandidateDir) -> Optional[Dict[str, Any]]:
                return self._evaluate_indexed_directory(
                    cd, parsed_expected, expected_tracks, rel_title,
                    track_keys=plan["slot_of"], positions=plan["positions"],
                )

            def accepted(evaluation: Optional[Dict[str, Any]]) -> bool:
                if not evaluation:
                    return False
                matched = set(self._slot_targets(plan, evaluation).values())
                # A folder is a copy of one edition: the union of every edition's
                # bonus tracks must not hide a complete standard edition. A small
                # edition only counts with the artist's name on the folder or files.
                edition_ratio = max((
                    len(matched & songs) / len(songs) for songs in plan["editions"]
                    if len(songs) > 3 or evaluation["artist_context"]
                ), default=0.0)
                ratio = max(evaluation["match_ratio"], edition_ratio)
                evaluation["edition_ratio"] = edition_ratio
                evaluation["total_score"] += (ratio - evaluation["match_ratio"]) * 100
                # A source must supply at least one track that is still missing.
                return ratio >= self.min_match_ratio and bool(needed & matched)

            cand_dirs = self.candidate_index.get_candidate_dirs_for_release(rel_title, parsed_expected, len(expected_tracks))
            evidence_dirs = self._tracklist_evidence_dirs(plan, parsed_expected)
            known = {id(cd) for cd in cand_dirs}
            cand_dirs += [cd for cd in evidence_dirs if id(cd) not in known]
            evidence_ids = {id(cd) for cd in evidence_dirs}
            # Keyed by folder; a merged multi-disc copy is kept apart from its parent folder's own files.
            candidate_matches: Dict[Tuple[str, str, bool], Dict[str, Any]] = {}

            def match_key(cd: CandidateDir) -> Tuple[str, str, bool]:
                return cd.user, cd.dir_name, bool(cd.dir_info.get("merged"))

            # Phase 1: Evaluate candidate directories with already indexed search files
            browse_pool: List[Tuple[CandidateDir, Optional[Dict[str, Any]]]] = []
            for cd in cand_dirs:
                if not is_dir_name_match_fast(clean_rel, rel_sig_words, cd) and id(cd) not in evidence_ids:
                    continue
                evaluation = evaluate(cd) if cd.audio_files else None
                if accepted(evaluation):
                    candidate_matches[match_key(cd)] = evaluation
                if (not cd.audio_files or len(cd.audio_files) < len(plan["slots"])) and self._is_unbrowsed(cd):
                    browse_pool.append((cd, evaluation))

            # Phase 2: browse the most promising folders when missing songs remain uncovered.
            covered = {slot for candidate in candidate_matches.values() for slot in self._slot_targets(plan, candidate).values()}
            if needed - covered and browse_pool:
                browse_pool.sort(key=lambda item: self._browse_rank(rel_title, *item))
                to_browse = browse_pool[:6]
                requests = list(dict.fromkeys(
                    (cd.user, folder) for cd, _ in to_browse for folder in cd.disc_dirs
                    if self._needs_browse(cd.user, folder)
                ))
                # Search results may show only some discs of a multi-disc copy:
                # also ask for the disc folders its naming implies ('CD1' -> 'CD2').
                disc_count = max(((slot["info"] or {}).get("disc_number") or 1 for slot in plan["slots"].values()), default=1)
                guessed: Dict[Tuple[str, str], Dict[str, Any]] = {}
                for cd, _ in to_browse:
                    if len(cd.disc_dirs) >= disc_count:
                        continue
                    for folder in cd.disc_dirs:
                        for sibling in _sibling_disc_folders(folder, disc_count):
                            if (cd.user, sibling) not in self.peer_directories and (cd.user, sibling) not in self._browse_attempted:
                                guessed.setdefault((cd.user, sibling), cd.dir_info)
                requests += [key for key in guessed if key not in requests]
                self._browse_attempted.update(requests)
                logger.info("Browsing %s peer directories for %s...", len(requests), rel_title)
                try:
                    browse_res = self.client.browse_directories_batch(requests, max_workers=6)
                except Exception as error:
                    logger.warning("Could not browse peer directories for %s: %s", rel_title, error)
                    browse_res = {}
                for (user, directory), remote_files in browse_res.items():
                    if remote_files and (user, directory) in guessed:
                        source = guessed[(user, directory)]
                        self.peer_directories[(user, directory)] = {
                            "user": user, "directory": directory, "speed": source.get("speed", 0),
                            "queue": source.get("queue", 0), "has_slot": source.get("has_slot", True),
                            "matched_search_files": [], "full_directory_files": None, "_seen_files": set(),
                        }
                    if remote_files and (user, directory) in self.peer_directories:
                        self.peer_directories[(user, directory)]["full_directory_files"] = remote_files
                        self.candidate_index.update_directory(user, directory, self.peer_directories[(user, directory)])
                browsed: Dict[int, CandidateDir] = {}
                for cd, _ in to_browse:
                    updated = (
                        self.candidate_index.disc_groups.get((cd.user, cd.dir_name))
                        if cd.dir_info.get("merged") else self.candidate_index.dirs_map.get((cd.user, cd.dir_name))
                    )
                    # A browsed disc folder also completes the release folder it belongs to.
                    for item in (updated, self.candidate_index.group_for(cd.user, cd.dir_name)):
                        if item is not None and item is not cd and item.audio_files:
                            browsed[id(item)] = item
                for updated in browsed.values():
                    evaluation = evaluate(updated)
                    if accepted(evaluation):
                        candidate_matches[match_key(updated)] = evaluation

            ranked = sorted(candidate_matches.values(), key=lambda item: item["total_score"], reverse=True)
            for candidate in ranked:
                targets = self._slot_targets(plan, candidate)
                missing_files = {filename for filename, slot in targets.items() if slot in needed}
                candidate["all_dir_files"] = [
                    f for f in candidate["all_dir_files"] if f.get("filename") in missing_files
                ]
                candidate["release"] = rel_title
            # A candidate holding only local tracks supplies nothing.
            ranked = [candidate for candidate in ranked if candidate["all_dir_files"]]
            plan["candidates"] = ranked
            plan["dir_slots"] = {
                slot for candidate in ranked for slot in self._slot_targets(plan, candidate).values() if slot in needed
            }

            # Missing songs no folder supplies: loose files with the artist's name.
            taken = {
                (candidate["user"], filename) for candidate in ranked
                for filename in self._slot_targets(plan, candidate)
            }
            for slot_id in plan["needed"]:
                if slot_id in plan["dir_slots"]:
                    continue
                slot = plan["slots"][slot_id]
                files = [
                    cf for cf in find_ranked_track_candidates(
                        self.candidate_index, slot["title"], self.all_artist_aliases, self.preferred_format,
                        rel_title, require_context=True, exact=True, position=slot["position"],
                    )
                    if (cf.user, cf.full_filename) not in taken
                    and (not slot["repeated"] or (slot["position"] and cf.position()[1] == slot["position"][1]))
                    # Another song of the artist has this title: only a copy in
                    # this release's folder is this one.
                    and (slot["unique"] or _is_release_folder(cf.folder, rel_title, frozenset(self.all_artist_aliases)))
                ]
                if files:
                    plan["track_slots"][slot_id] = files
                    taken.add((files[0].user, files[0].full_filename))
                else:
                    plan["unfilled"].add(slot_id)

            if ranked:
                best = ranked[0]
                self._release_candidates[norm_rel] = ranked
                self.verified_releases.append({
                    "release": rel_title,
                    "user": best["user"],
                    "directory": best["directory"],
                    "match_ratio": best["match_ratio"],
                    "matched_count": len(best["matched_tracks"]),
                    "total_count": len(expected_tracks),
                    "format_label": best["format_label"],
                    "queue": best["queue"],
                    "speed": best["speed"],
                    "files": best["all_dir_files"]
                })
                self.queued_directories.append(best)
                logger.info(f"✔ Matched Album: {rel_title} ({best['format_label']}) from {best['user']} ({len(best['matched_tracks'])}/{len(expected_tracks)} tracks)")
            for slot_id, files in plan["track_slots"].items():
                cf = files[0]
                self.verified_release_tracks.append({
                    "release": rel_title,
                    "track": plan["slots"][slot_id]["title"],
                    "user": cf.user,
                    "file": {**cf.raw_file, "filename": cf.full_filename},
                    "format_label": cf.fmt_label,
                })
                logger.info(f"✔ Matched Track: {rel_title} / {plan['slots'][slot_id]['title']} ({cf.fmt_label}) from {cf.user}")
            for slot_id in plan["dir_slots"] | set(plan["track_slots"]):
                slot = plan["slots"][slot_id]
                self.covered_song_keys |= slot["keys"]
                self.covered_track_titles.add(normalize_text(slot["title"]))
            if not ranked and not plan["track_slots"]:
                self.unresolved_releases.append(rel)
        self._refresh_unresolved()

    def _reconcile_standalone_tracks(self) -> None:
        standalone = [t for t in self.catalog.tracks if t.get("release_type") == "Standalone / Single"]
        if not standalone:
            return
        logging.getLogger(__name__).info(f"\nVerifying Standalone Tracks ({len(standalone)} tracks)...")

        if self.candidate_index is None:
            self.candidate_index = PeerCandidateIndex(self.peer_directories)

        for t in standalone:
            t_title = t.get("title", "")
            norm_t = normalize_text(t_title)
            keys = self._song_keys(t)
            # Without a release folder a title cannot tell a version apart from
            # the same-titled track already found, so the title covers it.
            if keys & (self.local_song_keys | self.covered_song_keys) or norm_t in self.covered_track_titles:
                continue

            candidates = find_ranked_track_candidates(
                self.candidate_index, t_title, self.all_artist_aliases, self.preferred_format,
                require_context=True, exact=True,
            )
            if candidates:
                best_cf = candidates[0]
                self.covered_track_titles.add(norm_t)
                self.covered_song_keys |= keys
                item = {
                    "track": t_title,
                    "user": best_cf.user,
                    "file": {**best_cf.raw_file, "filename": best_cf.full_filename},
                    "format_label": best_cf.fmt_label,
                }
                self._set_item_meta(item, {
                    "keys": keys, "candidates": candidates, "unique": self._title_is_unique(t_title, keys),
                })
                self.verified_standalone_tracks.append(item)
                logging.getLogger(__name__).info(f"✔ Matched Single: {t_title} ({best_cf.fmt_label}) from {best_cf.user}")
            else:
                self.unresolved_standalone_tracks.append(t)

    # ------------------------------------------------------------------
    # Queueing
    # ------------------------------------------------------------------

    def _release_songs(self) -> Dict[Tuple[str, ...], Set[Hashable]]:
        """Songs of each catalogued release, by the release title's significant words."""
        if self._release_song_index is None:
            aliases = frozenset(alias for alias in self.all_artist_aliases if alias)
            release_songs: Dict[Tuple[str, ...], Set[Hashable]] = {}
            for track in (self.catalog.tracks if self.catalog else []):
                keys = self._song_keys(track)
                for title in set(track.get("all_releases") or ()) | {track.get("release_title") or ""}:
                    words = tuple(_title_words(title, aliases))
                    if words:
                        release_songs.setdefault(words, set()).update(keys)
            self._release_song_index = release_songs
        return self._release_song_index

    def _folder_place(self, folder: str, release: str, keys: FrozenSet[Hashable]) -> str:
        """What a copy's release folder says about the song.

        'loose': no release named ('Target Artist', 'Singles');
        'shares': a catalogued release that has this song too;
        'other': a catalogued release without it;
        'unknown': a name the catalog does not know ('2015 - First Light').
        """
        aliases = frozenset(alias for alias in self.all_artist_aliases if alias)
        words = _folder_title_words(folder, aliases)
        if not words:
            return "loose"
        release_songs = self._release_songs()
        own = tuple(_title_words(release, aliases))
        named = [title for title in release_songs if title != own and is_sublist(list(title), words)]
        # 'First Light' names First Light, not also a release called 'Light'.
        named = [
            title for title in named
            if not any(len(other) > len(title) and is_sublist(list(title), list(other)) for other in named)
        ]
        if not named:
            return "unknown"
        return "shares" if any(keys & release_songs[title] for title in named) else "other"

    def _names_other_release(self, folder: str, release: str, slot: Dict[str, Any]) -> bool:
        """Whether a copy's folder shows it is another release's song, as history checks judge it.

        A catalogued release without this song, or an unknown release for a
        generic title ('Intro'). This release's own folder, an artist or
        singles folder, and a release sharing the song are no such evidence.
        """
        aliases = frozenset(alias for alias in self.all_artist_aliases if alias)
        if _is_release_folder(folder, release, aliases):
            return False
        place = self._folder_place(folder, release, slot["keys"])
        return place == "other" or (place == "unknown" and _is_generic_title(slot["title"]))

    def _set_item_meta(self, item: Dict[str, Any], meta: Dict[str, Any]) -> None:
        self._item_meta[id(item)] = (item, meta)

    def _get_item_meta(self, item: Dict[str, Any]) -> Dict[str, Any]:
        owner, meta = self._item_meta.get(id(item), (None, {}))
        return meta if owner is item else {}

    def _queue_downloads(self) -> None:
        track_slot_count = sum(len(plan["track_slots"]) for plan in self._release_plans.values())
        total_items = (
            len(self.queued_directories) + len(self.verified_compilation_tracks)
            + len(self.verified_standalone_tracks) + track_slot_count
        )
        if total_items == 0:
            logging.getLogger(__name__).info("No releases or tracks to enqueue.")
            if not any(plan["unfilled"] for plan in self._release_plans.values()):
                return

        history = DownloadHistory(self.client.get_downloads(), downloads_dir=resolve_downloads_dir(self.client))
        self.already_downloading_files = set(history.queued_filenames)
        queued_copies = {key for key in map(_copy_key, history.queued_filenames) if key}

        # An existing peer may be offline or absent from fresh search results.
        # Index its healthy transfers independently so a renamed/re-encoded copy
        # cannot bypass the exact-path check below.
        protected_directories: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for (user, _), file in history.protected.items():
            directory, _ = extract_dir_and_filename(file["filename"])
            protected_directories.setdefault((user, directory), {"matched_search_files": []})[
                "matched_search_files"
            ].append(file)
        protected_index = PeerCandidateIndex(protected_directories)
        # Stale and failed transfers of any peer, including peers missing from
        # this run's search results: a replacement cancels them (prepare_recovery).
        recovery_directories: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for (user, _), file in list(history.stale.items()) + list(history.failed.items()):
            directory, _ = extract_dir_and_filename(file["filename"])
            recovery_directories.setdefault((user, directory), {"matched_search_files": []})[
                "matched_search_files"
            ].append(file)
        recovery_index = PeerCandidateIndex(recovery_directories)
        aliases = frozenset(alias for alias in self.all_artist_aliases if alias)
        alias_words = [normalize_text(alias).split() for alias in aliases]
        folder_place = self._folder_place

        def history_position(filename: str, file: Optional[Dict[str, Any]] = None) -> Tuple[Optional[int], Optional[int]]:
            file = file or {}
            try:
                disc = int(file.get("disc_number"))
            except (TypeError, ValueError):
                disc = None
            parts = filename.replace("\\", "/").split("/")
            if disc is None:
                for folder in reversed(parts[:-1]):
                    disc = disc_number_from_folder(folder)
                    if disc is not None:
                        break
            return parse_disc_and_track_number(
                file.get("track_number"), filename=parts[-1], meta_disc=disc,
            )[:2]

        def history_matches(index: PeerCandidateIndex, track: str, release: str = "", reference: str = "",
                            repeated: bool = False, compilation: bool = False, unique: bool = True,
                            keys: FrozenSet[Hashable] = frozenset(),
                            position: Optional[Tuple[Optional[int], Optional[int]]] = None):
            parsed = pre_parse_single_track(track)
            expected_base = parsed["p_struct"]["base_norm"]
            generic = _is_generic_title(track)
            for candidate in find_track_candidates(index, track, aliases, release):
                # Search matching deliberately tolerates partial titles. History
                # suppression needs the complete title, including version checks
                # already enforced by find_track_candidates. A copy in this
                # release's own folder may match as closely as folder matching
                # does ('Machine Drum (Riveurs ...)' for 'Riveurs ...').
                if not expected_base:
                    continue
                if candidate.p_struct["base_norm"] != expected_base and not (
                    release and _is_release_folder(candidate.folder, release, aliases)
                    and track_match_tier(
                        parsed, parsed["words"], parsed["clean_words"], parsed["sig_words"], parsed["concat"],
                        candidate, aliases, release,
                    ) >= 2
                ):
                    continue
                # Transfer history contains every artist, unlike artist search
                # results. Require identifying context outside the track title.
                credited_words = normalize_text(candidate.raw_file.get("artist", "")).split()
                if credited_words and not any(is_sublist(words, credited_words) for words in alias_words):
                    continue
                if names_other_artist(candidate, aliases, track, release):
                    continue
                own_release = bool(release) and _is_release_folder(candidate.folder, release, aliases)
                # This release's own folder only needs the artist's name somewhere in the path.
                artist_context = file_has_artist_context(candidate, aliases) or (
                    own_release and path_mentions_artist(candidate.folder, aliases)
                )
                if not artist_context and not (compilation and own_release):
                    continue
                place = "own" if own_release else (
                    "loose" if not release or compilation else folder_place(candidate.folder, release, keys)
                )
                # A folder of another release holds this song only when that
                # release has it too. A folder the catalog does not know
                # ('2015 - First Light') may hold another song with a generic
                # title ('Intro'); a distinctive title is the same song.
                if place == "other" or (place == "unknown" and generic):
                    continue
                # Another release has a song with this title: only a copy from
                # this release's folder (or one that has this song) accounts for it.
                if not unique and place not in ("own", "shares"):
                    continue
                if repeated:
                    # Two positions titled "Echo" remain distinct. A title-only
                    # history match cannot account for both of them.
                    existing_position = history_position(candidate.full_filename, candidate.raw_file)
                    if reference:
                        expected_position = history_position(reference)
                        if expected_position[1] is None or existing_position != expected_position:
                            continue
                    elif not position or position[1] is None or existing_position[1] != position[1] or (
                        position[0] is not None and candidate.position()[0] not in (None, position[0])
                    ):
                        continue
                yield candidate

        def protected_track(track: str, release: str = "", reference: str = "",
                            repeated: bool = False, compilation: bool = False,
                            unique: bool = True, keys: FrozenSet[Hashable] = frozenset(),
                            position: Optional[Tuple[Optional[int], Optional[int]]] = None) -> Optional[CandidateFile]:
            return next(history_matches(
                protected_index, track, release, reference, repeated, compilation, unique, keys, position,
            ), None)

        def superseded(track: str, release: str = "", reference: str = "", repeated: bool = False,
                       compilation: bool = False, unique: bool = True,
                       keys: FrozenSet[Hashable] = frozenset()) -> Set[Tuple[str, str]]:
            """Stale or failed transfers of this song that a new submission replaces."""
            return {
                source_key(candidate.user, candidate.full_filename)
                for candidate in history_matches(
                    recovery_index, track, release, reference, repeated, compilation, unique, keys,
                )
                if history.excludes(candidate.user, candidate.full_filename)
            }

        logger = logging.getLogger(__name__)
        self.queue_errors.clear()
        self.queued_song_keys = set()
        unavailable: Dict[str, SlskdEnqueueError] = {}
        unresolved_peers: Dict[str, Tuple[Exception, List[Tuple[Dict[str, Any], FrozenSet[Hashable]]]]] = {}
        queued_titles: Set[str] = set()
        self._unavailable_sources = set()

        def is_queued(filename: str) -> bool:
            # The exact remote path, or the same file of the same release folder
            # from another peer. A shared basename alone ('01 - Intro.flac') is no evidence.
            return filename in self.already_downloading_files or _copy_key(filename) in queued_copies

        def remember(user: str, files: List[Dict[str, Any]]) -> None:
            for file in files:
                filename = file["filename"]
                self.already_downloading_files.add(filename)
                copy = _copy_key(filename)
                if copy:
                    queued_copies.add(copy)
            self.enqueued_count += len(files)
            self.enqueued_files.extend({**file, "user": user} for file in files)

        def sources_replaced(files: List[Dict[str, Any]], replaced_sources: Dict[str, Set[Tuple[str, str]]],
                             stale: bool) -> Set[Tuple[str, str]]:
            """The stale (or else the failed) transfers that these files replace."""
            return {
                source for file in files for source in replaced_sources.get(file.get("filename", ""), ())
                if (source in history.stale) == stale
            }

        def retire_stale(files: List[Dict[str, Any]], replaced_sources: Dict[str, Set[Tuple[str, str]]]) -> None:
            """A stale transfer may still start, so it is stopped only once its replacement is queued."""
            sources = sources_replaced(files, replaced_sources, stale=True)
            if not sources:
                return
            try:
                history.prepare_recovery(self.client, sources)
            except SlskdAPIError as error:
                self.queue_errors.append(
                    f"Queued a replacement, but the stale transfer it replaces may still start: {error}"
                )

        def queue_files(user: str, files: List[Dict[str, Any]],
                        replaced_sources: Optional[Dict[str, Set[Tuple[str, str]]]] = None,
                        ) -> Tuple[Set[str], Optional[Exception]]:
            """Submit files; replaced_sources maps a filename to the failed or stale transfers it replaces."""
            replaced_sources = replaced_sources or {}
            pending = []
            seen = set()
            excluded = set()
            for file in files:
                filename = file.get("filename", "")
                if filename and not is_queued(filename) and filename not in seen:
                    if history.excludes(user, filename):
                        excluded.add(source_key(user, filename))
                        continue
                    pending.append(file)
                    seen.add(filename)
            error = SlskdTransferFailedError(history.failure_summary(excluded)) if excluded else None
            if pending:
                if peer_key(user) in unavailable:
                    error = unavailable[peer_key(user)]
                else:
                    try:
                        # Stop only the failed transfers these files replace; their
                        # stale ones wait until the replacement is queued.
                        history.prepare_recovery(self.client, sources_replaced(pending, replaced_sources, stale=False))
                        self.client.enqueue_download(user, pending)
                    except Exception as exc:
                        error = exc
                        if isinstance(exc, SlskdEnqueueError):
                            remember(user, exc.queued_files)
                            retire_stale(exc.queued_files, replaced_sources)
                        if isinstance(exc, (SlskdPeerUnavailableError, SlskdPeerBlockedError)):
                            unavailable[peer_key(user)] = exc
                            logger.info("%s Skipping this peer for the rest of this queue pass; trying other matches.", exc)
                        if isinstance(exc, _NOT_QUEUED_ERRORS):
                            # This run's track searches skip the files slskd did not queue.
                            self._unavailable_sources.update(
                                source_key(user, file["filename"]) for file in pending
                                if not is_queued(file["filename"])
                            )
                    else:
                        remember(user, pending)
                        retire_stale(pending, replaced_sources)
                        logger.info("✔ Enqueued %s files from %s", len(pending), user)
            return {f["filename"] for f in files if f.get("filename") and is_queued(f["filename"])}, error

        def record_unavailable(error: Exception, files: List[Dict[str, Any]],
                               keys: Optional[List[FrozenSet[Hashable]]] = None) -> None:
            identity = getattr(error, "username", str(error))
            _, pending = unresolved_peers.setdefault(identity, (error, []))
            pending.extend(zip(files, keys or [frozenset()] * len(files)))

        def legacy_targets(directory: Dict[str, Any]) -> Dict[str, Any]:
            return {
                match["full_filename"]: match.get("expected_index", normalize_text(match["expected"]))
                for match in directory.get("matched_tracks", [])
            }

        for directory in self.queued_directories:
            release = directory.get("release", "")
            release_key = normalize_text(release)
            plan = self._release_plans.get(release_key) if release else None
            candidates = self._release_candidates.get(release_key, [directory])
            if plan is not None:
                file_targets = lambda candidate, plan=plan: self._slot_targets(plan, candidate)
                remaining = set(plan["dir_slots"])
            else:
                file_targets = legacy_targets
                targets = file_targets(directory)
                remaining = {
                    targets.get(file["filename"], file["filename"])
                    for file in directory["all_dir_files"] if file.get("filename")
                }
            protected_targets = set()
            title_counts: Dict[str, int] = {}
            if plan is not None:
                references = {}
                for candidate in candidates:
                    for filename, slot_id in file_targets(candidate).items():
                        references.setdefault(slot_id, filename)
                for slot_id in remaining:
                    slot = plan["slots"][slot_id]
                    if slot["keys"] & self.queued_song_keys or protected_track(
                        slot["title"], release, references.get(slot_id, ""),
                        repeated=slot["repeated"], unique=slot["unique"], keys=slot["keys"],
                    ):
                        protected_targets.add(slot_id)
            else:
                for match in directory.get("matched_tracks", []):
                    title = normalize_text(match["expected"])
                    title_counts[title] = title_counts.get(title, 0) + 1
                targets = file_targets(directory)
                for match in directory.get("matched_tracks", []):
                    title = normalize_text(match["expected"])
                    if (title_counts[title] == 1 and title in queued_titles) or protected_track(
                        match["expected"], release, match["full_filename"],
                        repeated=title_counts[title] > 1,
                    ):
                        protected_targets.add(targets[match["full_filename"]])
            # Failed or stale copies of a target, cancelled only when it is replaced.
            replaced: Dict[Any, Set[Tuple[str, str]]] = {}
            if plan is not None:
                for slot_id in remaining - protected_targets:
                    slot = plan["slots"][slot_id]
                    sources = superseded(
                        slot["title"], release, references.get(slot_id, ""),
                        repeated=slot["repeated"], unique=slot["unique"], keys=slot["keys"],
                    )
                    if sources:
                        replaced.setdefault(slot_id, set()).update(sources)
            for candidate in candidates:
                targets = file_targets(candidate)
                for file in candidate["all_dir_files"]:
                    filename = file.get("filename", "")
                    target = targets.get(filename, filename)
                    if not filename or target not in remaining:
                        continue
                    if is_queued(filename):
                        protected_targets.add(target)
                    elif history.excludes(candidate["user"], filename):
                        replaced.setdefault(target, set()).add(source_key(candidate["user"], filename))
            remaining.difference_update(protected_targets)
            peer_error = None
            # Songs in a submission that failed for an unknown reason: slskd may
            # have accepted them, so no other source is tried.
            uncertain: Set[Any] = set()
            for candidate in candidates:
                if not remaining:
                    break
                targets = file_targets(candidate)
                files, chosen = [], set()
                for file in candidate["all_dir_files"]:
                    target = targets.get(file.get("filename"), file.get("filename"))
                    if file.get("filename") and target in remaining and target not in chosen:
                        files.append(file)
                        chosen.add(target)
                if not files:
                    continue
                sources = {
                    file["filename"]: replaced.get(targets.get(file["filename"], file["filename"]), set())
                    for file in files
                }
                done, error = queue_files(candidate["user"], files, sources)
                remaining.difference_update(targets.get(filename, filename) for filename in done)
                # Files slskd refused or could not reach the peer for come from the next source.
                if isinstance(error, _NOT_QUEUED_ERRORS):
                    peer_error = error
                elif error is not None:
                    self.queue_errors.append(f"Failed to enqueue {candidate['directory']}: {error}")
                    peer_error = None
                    uncertain = chosen & remaining
                    break
            # Cover a song only after slskd accepted it (or history protected
            # it), so a later edition can still supply a failed first attempt.
            if plan is not None:
                for slot_id in plan["dir_slots"]:
                    if slot_id not in remaining:
                        slot = plan["slots"][slot_id]
                        self.queued_song_keys |= slot["keys"]
                        queued_titles.add(normalize_text(slot["title"]))
                plan["queue_failed"] |= remaining & uncertain
                plan["unqueued"] |= remaining - uncertain
            else:
                targets = file_targets(directory)
                for match in directory.get("matched_tracks", []):
                    title = normalize_text(match["expected"])
                    if title_counts[title] == 1 and targets[match["full_filename"]] not in remaining:
                        queued_titles.add(title)
            if remaining and peer_error is not None:
                unqueued_files: Dict[Any, Dict[str, Any]] = {}
                for candidate in candidates:
                    targets = file_targets(candidate)
                    for file in candidate["all_dir_files"]:
                        target = targets.get(file.get("filename"), file.get("filename"))
                        if file.get("filename") and target in remaining:
                            unqueued_files.setdefault(target, file)
                record_unavailable(peer_error, list(unqueued_files.values()), [
                    plan["slots"][target]["keys"] if plan is not None else frozenset()
                    for target in unqueued_files
                ])

        # Songs no discovered source supplies may already be downloading from
        # an earlier run (a peer that has since left): never search for them again.
        for plan in self._release_plans.values():
            for slot_id in sorted(plan["unfilled"]):
                slot = plan["slots"][slot_id]
                if not (slot["keys"] & self.queued_song_keys) and protected_track(
                    slot["title"], plan["release"], repeated=slot["repeated"], unique=slot["unique"],
                    keys=slot["keys"], position=slot["position"],
                ):
                    self.queued_song_keys |= slot["keys"]

        # Single files: release songs no folder supplies, then compilation and
        # standalone tracks. Keep each user's initial submission batched, then
        # try other sources only for tracks slskd did not queue (the peer was
        # unavailable, or slskd refused the files).
        single_items: List[Dict[str, Any]] = []
        for plan in self._release_plans.values():
            for slot_id, files in plan["track_slots"].items():
                slot = plan["slots"][slot_id]
                cf = files[0]
                item = {
                    "user": cf.user, "track": slot["title"], "release": plan["release"],
                    "file": {**cf.raw_file, "filename": cf.full_filename}, "format_label": cf.fmt_label,
                }
                self._set_item_meta(item, {
                    "keys": slot["keys"], "candidates": files, "unique": slot["unique"],
                    "plan": plan, "slot": slot_id, "release_track": True,
                })
                single_items.append(item)
        single_items += self.verified_compilation_tracks + self.verified_standalone_tracks

        def item_keys(item: Dict[str, Any]) -> FrozenSet[Hashable]:
            meta = self._get_item_meta(item)
            return meta.get("keys") or frozenset({("title", normalize_text(item["track"]))})

        single_tracks_by_user: Dict[str, List[Dict[str, Any]]] = {}
        replaced_by_item: Dict[int, Set[Tuple[str, str]]] = {}
        uncertain_items: Set[int] = set()
        for item in single_items:
            meta = self._get_item_meta(item)
            keys = item_keys(item)
            if keys & self.queued_song_keys:
                continue
            if not meta.get("release_track") and normalize_text(item["track"]) in queued_titles:
                continue
            protected = protected_track(
                item["track"], item.get("release", ""), item["file"].get("filename", ""),
                compilation=bool(item.get("release")) and not meta.get("release_track"),
                unique=meta.get("unique", True), keys=keys,
            )
            if protected is not None:
                item.update(user=protected.user, format_label=protected.fmt_label,
                            file={**protected.raw_file, "filename": protected.full_filename})
                self.queued_song_keys |= keys
                continue
            if self.candidate_index is None:
                self.candidate_index = PeerCandidateIndex(self.peer_directories)
            candidates = meta.get("candidates")
            if candidates is None:
                # A track without a release folder needs the artist's name around it.
                candidates = find_track_candidates(
                    self.candidate_index, item["track"], self.all_artist_aliases, item.get("release", ""),
                    require_context=True, exact=True,
                )
            replaced = replaced_by_item.setdefault(id(item), set())
            replaced |= superseded(
                item["track"], item.get("release", ""), item["file"].get("filename", ""),
                compilation=bool(item.get("release")) and not meta.get("release_track"),
                unique=meta.get("unique", True), keys=keys,
            )
            for candidate in candidates:
                if history.is_protected(candidate.user, candidate.full_filename):
                    item.update(user=candidate.user, format_label=candidate.fmt_label,
                                file={**candidate.raw_file, "filename": candidate.full_filename})
                    break
                if history.excludes(candidate.user, candidate.full_filename):
                    replaced.add(source_key(candidate.user, candidate.full_filename))
            single_tracks_by_user.setdefault(item["user"], []).append(item)

        def item_done(item: Dict[str, Any]) -> None:
            self.queued_song_keys |= item_keys(item)
            queued_titles.add(normalize_text(item["track"]))

        def next_candidate(item: Dict[str, Any], tried: Set[Tuple[str, str]]) -> Optional[CandidateFile]:
            meta = self._get_item_meta(item)
            excluded_users = set(unavailable) | set(history.blocked_peers)
            if meta.get("candidates") is not None:
                return next((
                    candidate for candidate in meta["candidates"]
                    if peer_key(candidate.user) not in {peer_key(user) for user in excluded_users}
                    and source_key(candidate.user, candidate.full_filename) not in tried
                    and source_key(candidate.user, candidate.full_filename) not in history.excluded_sources
                ), None)
            return find_best_track_candidate(
                self.candidate_index, item["track"], self.all_artist_aliases,
                self.preferred_format, item.get("release", ""),
                excluded_users=excluded_users,
                excluded_sources=set(history.excluded_sources) | tried,
                require_context=True, exact=True,
            )

        for user, items in single_tracks_by_user.items():
            sources: Dict[str, Set[Tuple[str, str]]] = {}
            for item in items:
                sources.setdefault(item["file"].get("filename", ""), set()).update(
                    replaced_by_item.get(id(item), set()),
                )
            done, error = queue_files(user, [item["file"] for item in items], sources)
            for item in items:
                if item["file"].get("filename") in done:
                    item_done(item)
            if error is None:
                continue
            # slskd may have queued files after an unknown error: never replay them elsewhere.
            if not isinstance(error, _NOT_QUEUED_ERRORS):
                self.queue_errors.append(f"Failed to enqueue tracks from {user}: {error}")
                uncertain_items.update(id(item) for item in items if item["file"].get("filename") not in done)
                continue
            if self.candidate_index is None:
                self.candidate_index = PeerCandidateIndex(self.peer_directories)
            for item in items:
                if item["file"].get("filename") in done:
                    continue
                peer_error = error
                tried = {source_key(item["user"], item["file"].get("filename", ""))}
                while True:
                    candidate = next_candidate(item, tried)
                    if candidate is None:
                        record_unavailable(peer_error, [item["file"]], [item_keys(item)])
                        break
                    tried.add(source_key(candidate.user, candidate.full_filename))
                    file = {**candidate.raw_file, "filename": candidate.full_filename}
                    accepted, fallback_error = queue_files(
                        candidate.user, [file], {candidate.full_filename: replaced_by_item.get(id(item), set())},
                    )
                    if candidate.full_filename in accepted:
                        item.update(user=candidate.user, file=file, format_label=candidate.fmt_label)
                        item_done(item)
                        break
                    if isinstance(fallback_error, _NOT_QUEUED_ERRORS):
                        peer_error = fallback_error
                        continue
                    self.queue_errors.append(f"Failed to enqueue {item['track']}: {fallback_error}")
                    uncertain_items.add(id(item))
                    break

        for item in single_items:
            meta = self._get_item_meta(item)
            if meta.get("release_track") and not (item_keys(item) & self.queued_song_keys):
                meta["plan"]["queue_failed" if id(item) in uncertain_items else "unqueued"].add(meta["slot"])

        self._peer_failures = [
            (error, [(file["filename"], keys) for file, keys in files
                     if file.get("filename") and not is_queued(file["filename"])])
            for error, files in unresolved_peers.values()
        ]
        self._peer_failure_messages = []
        self._report_peer_failures()
        self._refresh_unresolved()

    def _report_peer_failures(self) -> None:
        """Report files no available peer accepted, leaving out songs queued since."""
        for message in self._peer_failure_messages:
            if message in self.queue_errors:
                self.queue_errors.remove(message)
        self._peer_failure_messages = []
        for error, files in self._peer_failures:
            count = len({
                filename.replace("/", "\\").lower() for filename, keys in files
                if not (keys and keys & self.queued_song_keys)
            })
            if count:
                # A peer error ends its sentence; slskd's refusals ('...: File not shared') do not.
                reason = str(error).rstrip()
                if reason and reason[-1] not in ".!?":
                    reason += "."
                message = (
                    f"{reason} {count} matched files could not be queued from the available sources. "
                    "Run the download again later to retry."
                )
                self._peer_failure_messages.append(message)
                self.queue_errors.append(message)

    # ------------------------------------------------------------------
    # Track searches for songs no discovered source supplies
    # ------------------------------------------------------------------

    def _release_artist(self, plan: Dict[str, Any]) -> str:
        raw = (self.catalog.raw_data if self.catalog else None) or {}
        metadata = next((item for item in raw.get("releases_artist", []) if item.get("id") == plan["release_id"]), {})
        credit = ArtistCatalog.format_credit(metadata.get("artist-credit"))
        aliases = set(self.catalog.aliases) | {self.catalog.name}
        # Peers name folders after the credited spelling ('Machine Drum').
        return credit if credit and artist_credit_matches(self.catalog.name, credit, aliases) else self.catalog.name

    def _missing_row(self, plan: Dict[str, Any], slot_id: int, release_artist: str) -> Dict[str, Any]:
        """A missing-track row in the shape library audits give the release downloader."""
        slot = plan["slots"][slot_id]
        track = slot["track"]
        info = slot["info"] or {}
        title = slot["title"]
        aliases = set(self.catalog.aliases) | {self.catalog.name}
        credit = track.get("artist_credit") or release_artist
        artist = release_artist if artist_credit_matches(self.catalog.name, credit, aliases) else credit
        alt_titles = [
            name for name in dict.fromkeys((info.get("recording_title"), track.get("title")))
            if name and normalize_text(name) != normalize_text(title)
        ]
        row = {
            "title": title,
            "alt_titles": alt_titles,
            "artist": artist,
            "status": "missing",
            "disc_number": info.get("disc_number"),
            "track_number": info.get("position_token") or track.get("track_number") or None,
            "position_token": info.get("position_token"),
            "track_position": info.get("track_position"),
            "absolute_position": info.get("absolute_position"),
            "medium_track_count": info.get("medium_track_count"),
            "mb_track_id": info.get("mb_track_id") or next(iter(sorted(track.get("track_ids") or ())), None),
            "mb_recording_id": info.get("mb_recording_id") or next(iter(sorted(track.get("recording_ids") or ())), None),
            "duration": info.get("duration"),
        }
        return {key: value for key, value in row.items() if value is not None}

    def _search_unfilled_tracks(self) -> None:
        """Search track by track for missing songs that no discovered folder or file supplied."""
        if not self.search_missing_tracks or not self._release_plans:
            return
        logger = logging.getLogger(__name__)
        service = LibraryReleaseService(mb_client=self.mb_client, slskd_client=self.client)
        progress = lambda done, total, message: logger.info("%s [%s/%s tracks matched]", message, done, total)
        for plan in self._release_plans.values():
            # Songs no discovered source supplied, and songs whose sources all
            # refused or were offline. A submission that failed for an unknown
            # reason may have been accepted, so it is never repeated elsewhere.
            slots = [
                slot_id for slot_id in plan["needed"]
                if (slot_id in plan["unfilled"] or slot_id in plan["unqueued"])
                and slot_id not in plan["queue_failed"] and slot_id not in plan["resolved"]
                and not (plan["slots"][slot_id]["keys"] & self.queued_song_keys)
            ]
            if not slots:
                continue
            release_artist = self._release_artist(plan)
            rows = [self._missing_row(plan, slot_id, release_artist) for slot_id in slots]
            # Search the release first only when nothing of it was found yet.
            scope = "track" if plan["has_local"] or plan["candidates"] or plan["track_slots"] else "release"
            logger.info("Searching %s missing tracks of %s individually...", len(rows), plan["release"])
            try:
                result = service.download_missing_tracks(
                    artist=release_artist, release_title=plan["release"], missing_tracks=rows,
                    preferred_format=self.preferred_format, search_timeout=self.search_timeout,
                    dry_run=self.dry_run, on_progress=progress, search_scope=scope,
                    excluded_sources=set(self._unavailable_sources) or None,
                    search_cache=self.search_cache,
                    use_existing=self._reuse_searches(),
                )
            except (SlskdAPIError, ValueError) as error:
                self.queue_errors.append(f"{plan['release']}: {error}")
                plan["search_failed"].update((slot_id, str(error)) for slot_id in slots)
                continue
            result["download_scope"] = "missing_tracks"
            self.release_downloads.append(dict(result, release_id=plan["release_id"]))
            if not self.dry_run:
                enqueued = result.get("enqueued_files", [])
                self.enqueued_count += len(enqueued)
                self.enqueued_files.extend(enqueued)
            self.queue_errors.extend(
                f"{plan['release']}: {error.get('error', str(error)) if isinstance(error, dict) else error}"
                for error in result.get("queue_errors", [])
            )
            resolved_rows = result.get("matched_tracks" if self.dry_run else "queued_tracks") or []
            resolved_ids = {
                row.get("mb_track_id") or (normalize_text(row.get("title")), row.get("disc_number"), row.get("track_number"))
                for row in resolved_rows if isinstance(row, dict)
            }
            folders: Dict[Tuple[str, int], List[str]] = {}
            for file in result.get("queued_files") or []:
                folders.setdefault((normalize_text(file.get("title")), file.get("disc_number") or 1), []).append(
                    extract_dir_and_filename(file.get("filename") or "")[0],
                )
            for slot_id, row in zip(slots, rows):
                identity = row.get("mb_track_id") or (normalize_text(row.get("title")), row.get("disc_number"), row.get("track_number"))
                if not (any(row is item for item in resolved_rows) or identity in resolved_ids):
                    continue
                slot = plan["slots"][slot_id]
                # A copy in another release's folder ('First Light\\01 Intro') is that
                # release's song, even when it is already downloading: the track is still missing.
                sources = folders.get((normalize_text(row.get("title")), row.get("disc_number") or 1), [])
                if sources and all(self._names_other_release(folder, plan["release"], slot) for folder in sources):
                    plan["other_release"][slot_id] = sources[0]
                    continue
                plan["resolved"].add(slot_id)
                self.queued_song_keys |= slot["keys"]
            # Tell a failed search or submission apart from a track nobody shares.
            reasons = track_failures(result)
            for slot_id, row in zip(slots, rows):
                reason, error = reasons.get(id(row), ("", ""))
                if slot_id in plan["resolved"] or not reason:
                    continue
                if reason == "search failed":
                    plan["search_failed"][slot_id] = error
                else:
                    plan["unqueued"].add(slot_id)
        self._report_peer_failures()
        self._refresh_unresolved()
