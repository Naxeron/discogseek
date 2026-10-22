"""Soulseek search and queueing for missing release tracks."""

import logging
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple, Union

from discogseek.clients.slskd import (
    SlskdAPIError, SlskdClient, SlskdEnqueueRefusedError, SlskdPeerBlockedError, SlskdPeerUnavailableError,
)
from discogseek.core.audio import AudioQualityAnalyzer, DISC_DIR_PATTERN
from discogseek.core.constants import AUDIO_EXTENSIONS, DIR_STOP_WORDS
from discogseek.core.peer_policy import peer_key
from discogseek.core.release_metadata import (
    MISSING_TRACK_PATTERN,
    disc_number_from_folder,
    is_missing_track_placeholder,
    is_various_artists,
    parse_disc_and_track_number,
    vinyl_position,
)
from discogseek.core.text import (
    artist_credit_matches,
    clean_search_phrase,
    fold_search_phrase,
    normalize_text,
    parse_track_title_structure,
    primary_artist,
    split_artist_credit,
    title_match_score,
)
from discogseek.core.submissions import SubmissionLog, row_identity, submission_source
from discogseek.core.transfers import DownloadHistory, resolve_downloads_dir, source_key, transfer_states
from discogseek.services.candidates import PeerCandidateIndex


# slskd reports a file's 'length' in whole seconds; MusicBrainz durations are exact.
DURATION_TOLERANCE = 3.0
# Outside the release's folder a clearly different length is another recording (an edit,
# a DJ-mix cut, another album's 'Intro'); encodes of one recording differ by a few seconds.
DURATION_CONFLICT_SECONDS = 10.0
DURATION_CONFLICT_RATIO = 0.05
# Searches qualified by a credited artist for a compilation whose bare title is generic.
MAX_QUALIFIED_QUERIES = 4
# Peer folders browsed per request to finish a release from the peer that has it.
MAX_FOLDER_BROWSES = 2
_LIMIT_STATES = ("ResponseLimitReached", "FileLimitReached")
_EXACT = 0.999

# One leading position token: '05 ', '05. ', '1-05 ', 'B1 - ', '(05) ', '[05]-'.
_LEADING_POSITION_RE = re.compile(
    r"^(?:[\(\[]\s*(\d{1,3})\s*[\)\]]|((?:\d{1,2}[-.])?\d{1,4}|[A-Ha-h]\d{1,2}))(?:\s*[-._]+\s*|\s+)(?=\S)"
)
_POSITION_SEGMENT_RE = re.compile(r"^[\(\[]?((?:\d{1,2}[-.])?\d{1,4}|[A-Ha-h]\d{1,2})[\)\]]?\.?$")
_LEADING_VINYL_RE = re.compile(r"^([A-Ha-h]\d{1,2})(?=[\s._\-])")
_DISC_TRACK_PREFIX_RE = re.compile(
    r"^(?:(?:disc|disk|cd)\s*[-_.]?\s*\d{1,2}\s*[-_.:\s]|\d{1,2}[-_.]\d{1,3}(?:[\s._\-]|$))", re.IGNORECASE,
)
_DISC_SEGMENT_RE = re.compile(r"^(?:disc|disk|cd|side|lp)\s*[-_.]?\s*(?:\d{1,2}|[a-h])$", re.IGNORECASE)
_YEAR_SEGMENT_RE = re.compile(r"^[\(\[]?(?:19|20)\d{2}(?:[.\-]\d{2}){0,2}[\)\]]?$")
_BRACKETED_RE = re.compile(r"^[\(\[\{][^\)\]\}]*[\)\]\}]$")
_BRACKET_GROUP_RE = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_ARTICLES = frozenset({"the", "a", "an"})
# (medium, track number or vinyl side token, title) of a requested row.
_TrackKey = Tuple[int, Union[int, str, None], str]

# Radio captures, DJ mixes, podcasts, live sets and playlist dumps: a file there is cut
# from a continuous recording (talk-overs, crossfades, edits) or taken from any release.
# Any folder of a path can say so: 'zLONDON BURNING WEB RADIO\Playlist Auto DJ - 2012'.
_MIXED_SOURCE_RE = re.compile(
    r"(?<![a-z0-9])(?:"
    r"soma\s?fm|podcasts?|auto\s?-?\s?dj|essential\s+mix(?:es)?|radios|stream\s?(?:writer|ripper)|"
    # 'BBC Radio Orchestra' and 'BBC Radio Big Band' are performers.
    r"(?:web|net|internet|online|bbc)\s?-?\s?radio"
    r"(?!\s+(?:\w+\s+)?(?:orchestra|big\s+band|symphony|philharmonic|choir|chorus|singers|ensemble))|"
    # Not 'radio' alone: 'Radio Slave', 'All India Radio' and 'Live At Radio City' are
    # artists and venues.
    r"radio\s?-?\s?(?:shows?|rips?|streams?|stations?|broadcasts?|captures?|recordings?|sessions?|"
    r"specials?|channels?|programm?e?s?|episodes?|archives?)"
    r")(?![a-z0-9])",
    re.IGNORECASE,
)
# Only the release folder itself: a 'Playlist' or 'DJ Set' folder higher up is often a
# collector's library of whole albums. Legacy's 'Playlist: The Very Best of Journey' is an
# artist's own best-of.
_MIXED_RELEASE_FOLDER_RE = re.compile(
    r"(?<![a-z0-9])(?:"
    r"playlists?(?![\s:\-–]*the\s+very\s+best)|"
    r"dj\s?-?\s?(?:mix(?:es|ed)?|sets?)|mixed\s+by|[(\[{,]\s*mixed\s*(?:cd)?\s*[)\]},]|"
    r"(?:continuous|guest|mini|live)\s?-?\s?mix|live\s?-?\s?sets?|mix\s?-?\s?tapes?|boiler\s+room|"
    r"(?:bbc|peel|maida\s+vale)\s+sessions?|live\s+(?:at|from)|live\s*@|in\s+concert"
    r")(?![a-z0-9])",
    re.IGNORECASE,
)
# A requested release or track that is itself a mix, a live recording or a radio session
# comes from such folders.
_MIXED_TITLE_RE = re.compile(
    r"(?<![a-z0-9])(?:mix(?:es|ed|tapes?)?|radio|live|dj|sessions?|podcasts?|concert|broadcast|somafm|playlists?|"
    r"boiler\s+room)"
    r"(?![a-z0-9])",
    re.IGNORECASE,
)
_MIXED_TRACK_TITLE_RE = re.compile(r"(?<![a-z0-9])(?:live|sessions?|mixed|dj\s?-?\s?mix)(?![a-z0-9])", re.IGNORECASE)
# The unmixed edition of a mix compilation ('Balance 020 (Mixed By Deetron) [Un-Mixed Version]')
# holds the full tracks.
_UNMIXED_RE = re.compile(r"(?<![a-z0-9])(?:un|non)\s?-?\s?mixed(?![a-z0-9])", re.IGNORECASE)
# Titles that name a part of an album, not a song: another release's 'Intro' is another recording.
_GENERIC_TITLE_RE = re.compile(
    r"^(?:intro|outro|interlude|prelude|untitled|skit|intermission|segue|hidden track|bonus track|silence)"
    r"(?: (?:no |number |part |pt )?(?:\d{1,3}|[ivx]{1,4}))?$"
)


def _search_key(query: str) -> str:
    return " ".join(str(query or "").split()).casefold()


def _variant_key(query: str) -> str:
    """Queries that differ only in punctuation ('Bleep: The Top 100', 'Bleep The Top 100') find the same files."""
    return _search_key(clean_search_phrase(query) or query)


def _mixed_source_folder(folder: str, release_folder: Optional[str] = None) -> bool:
    """
    Whether a peer folder holds a radio capture, a DJ mix, a podcast, a live set or a
    playlist dump. release_folder is the folder a disc or format subfolder belongs to;
    by default a disc folder ('CD2') belongs to its parent.
    """
    parts = [part.replace("_", " ") for part in folder.replace("\\", "/").split("/") if part.strip()]
    if any(_MIXED_SOURCE_RE.search(part) for part in parts):
        return True
    leaves = set(parts[-1:])
    if release_folder:
        leaves.add(os.path.basename(release_folder.replace("\\", "/").rstrip("/")).replace("_", " "))
    elif len(parts) > 1 and DISC_DIR_PATTERN.match(parts[-1].strip()):
        leaves.add(parts[-2])
    return any(_MIXED_RELEASE_FOLDER_RE.search(leaf) and not _UNMIXED_RE.search(leaf) for leaf in leaves if leaf)


def _as_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _audio_stem(basename: str) -> str:
    stem, extension = os.path.splitext(basename)
    return stem if extension.lower() in AUDIO_EXTENSIONS else basename


def _filename_segments(stem: str) -> List[str]:
    """
    Splits a filename stem into its ' - ' separated parts. Scene names without spaces
    ('05-lone-big_world') split at hyphens, and '-[02]-' tokens become a position part.
    """
    text = re.sub(r"[‐-―−－]", "-", unicodedata.normalize("NFC", stem)).strip()
    if not re.search(r"\s", text) and "-" in text:
        parts = re.split(r"-+", text)
    else:
        text = re.sub(r"-\[(\d{1,3})\]-", r" - \1 - ", text)
        text = re.sub(r"\s*[~～〜]\s*", " - ", text)
        parts = re.split(r"\s+-+\s+|\s*_+-_+\s*", text)
    segments = []
    for part in parts:
        part = re.sub(r"\s+", " ", part.replace("_", " ")).strip(" .")
        if part:
            segments.append(part)
    return segments


def _strip_leading_position(text: str) -> Tuple[str, Optional[str]]:
    match = _LEADING_POSITION_RE.match(text)
    if not match:
        return text, None
    return text[match.end():], match.group(1) or match.group(2)


def _is_noise_segment(segment: str) -> bool:
    """Year, disc, catalog bracket and compilation markers are not artist names."""
    text = segment.strip()
    return bool(
        _YEAR_SEGMENT_RE.match(text) or _DISC_SEGMENT_RE.match(text) or _BRACKETED_RE.match(text)
        or is_various_artists(text) or not normalize_text(text)
    )


def _queue_bucket(value: Any) -> int:
    try:
        queue = int(value)
    except (TypeError, ValueError):
        return 1
    if queue <= 0:
        return 0
    return 1 if queue <= 10 else 2 if queue <= 100 else 3


def _speed_bucket(value: Any) -> int:
    try:
        speed = float(value)
    except (TypeError, ValueError):
        return 2
    if speed >= 5_000_000:
        return 0
    if speed >= 1_000_000:
        return 1
    return 2 if speed >= 200_000 or speed <= 0 else 3


def _peer_meta(response: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "hasFreeUploadSlot": response.get("hasFreeUploadSlot"),
        "queueLength": response.get("queueLength"),
        "uploadSpeed": response.get("uploadSpeed"),
    }


def _health_rank(meta: Optional[Dict[str, Any]]) -> Tuple[int, int, int]:
    """Free upload slot, then a short remote queue, then a fast upload."""
    meta = meta or {}
    return (
        -int(meta.get("hasFreeUploadSlot") is True),
        _queue_bucket(meta.get("queueLength")),
        _speed_bucket(meta.get("uploadSpeed")),
    )


class _Expected:
    """Where a missing row sits on its release, in every numbering peers use."""
    __slots__ = ("disc", "number", "token", "sequence", "absolute")

    def __init__(self, disc: int, number: Optional[int], token: Optional[Tuple[str, int]],
                 sequence: Optional[int], absolute: Optional[int]):
        self.disc = disc
        self.number = number
        self.token = token
        self.sequence = sequence
        self.absolute = absolute


class _Remote:
    """The position a peer file claims through its tags, folders and name."""
    __slots__ = ("disc", "explicit_disc", "number", "token", "alternate")

    def __init__(self) -> None:
        self.disc: Optional[int] = None
        self.explicit_disc = False
        self.number: Optional[int] = None
        self.token: Optional[Tuple[str, int]] = None
        self.alternate: Optional[Tuple[int, int]] = None


class _Match:
    """Evidence that a peer file is a requested track."""
    __slots__ = ("score", "context", "named_context", "same_position", "duration_match")

    def __init__(self, score: float, context: int, named_context: bool, same_position: bool, duration_match: bool):
        self.score = score
        self.context = context
        self.named_context = named_context
        self.same_position = same_position
        self.duration_match = duration_match

    @property
    def evidence(self) -> int:
        """Release folder at the same position first, then a matching duration."""
        rank = 2 if self.context and self.same_position else 1 if self.context else 0
        return rank + int(self.duration_match)


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
    excluded_sources: Optional[Set[Tuple[str, str]]] = None,
    complete_release_tracks: Optional[List[Dict[str, Any]]] = None,
    official_tracks: Optional[List[Dict[str, Any]]] = None,
    search_cache: Optional[Dict[str, Dict[str, Any]]] = None,
    use_existing: bool = True,
    downloads_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Orchestrates Soulseek discovery and queueing for missing tracks of a release.
    Release searches start with album peers; selected-track searches start with its artist/title.
    Both modes fall back to the other search strategy for unresolved requested rows.

    matched_tracks and queued_tracks refer to requested rows; only confirmed enqueue
    successes enter queued_tracks. Queue/search errors retain their affected rows so
    callers can keep earlier successes and retry the remaining positions.
    enqueued_files contains only newly accepted submissions, while queued_files
    also contains protected transfers and dry-run matches.
    Progress counts matched requested tracks, not a percentage or transfer progress.
    complete_release_tracks requires one peer's release folder to match every
    official track before queueing missing rows; it disables individual searches.
    official_tracks (the full audited tracklist, optional) adds flat and vinyl
    numbering and the other titles of the release as evidence. search_cache shares
    query results between calls of one run, so identical queries are searched once.
    use_existing=False asks the Soulseek network again instead of reusing slskd's
    recent searches. downloads_dir is slskd's downloads folder when the caller already
    knows it; otherwise it is read from slskd's options.
    """
    if search_scope not in {"release", "track"}:
        raise ValueError("search_scope must be 'release' or 'track'")
    if not missing_tracks:
        return {
            "status": "skipped", "message": "No missing tracks to download.",
            "queued_count": 0, "matched_count": 0, "resolved_count": 0,
            "queued_files": [], "enqueued_files": [], "matched_tracks": [], "queued_tracks": [], "queue_errors": [],
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

    def row_disc(track: Dict[str, Any]) -> int:
        try:
            return int(track.get("disc_number") or 1)
        except (TypeError, ValueError):
            return 1

    def row_side(track: Dict[str, Any]) -> Optional[Tuple[str, int]]:
        """The vinyl side token of a row ('B2'), which no disc-and-number reading preserves."""
        raw = track.get("position_token") or track.get("track_number") or track.get("track_num_int")
        return vinyl_position(raw) if isinstance(raw, str) else None

    def track_slot(track: Dict[str, Any]) -> Tuple[int, Union[int, str, None]]:
        """
        (medium, position) of a row. A vinyl row keeps its own medium and side token:
        'B2' of a single LP is medium 1, and B1 and C1 of a double LP never collide.
        """
        side = row_side(track)
        if side and not MISSING_TRACK_PATTERN.match(track.get("title", "")):
            return row_disc(track), f"{side[0]}{side[1]}"
        return track_position(track)

    def track_key(track: Dict[str, Any]) -> _TrackKey:
        return (*track_slot(track), track.get("title", ""))

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

    try:
        downloads = slskd_client.get_downloads()
        history = DownloadHistory(
            downloads, excluded_sources=excluded_sources,
            downloads_dir=downloads_dir if downloads_dir is not None else resolve_downloads_dir(slskd_client),
        )
    except Exception as error:
        raise SlskdAPIError(f"Cannot check existing slskd downloads before searching: {error}") from error

    # Only the release credit decides compilation mode: a featured guest or a remix
    # credit on some rows never turns an artist's album into 'Various Artists'.
    is_va = is_various_artists(artist)
    effective_artist = "Various Artists" if is_va else artist
    release_artist = "" if is_va or not artist or artist.strip().lower() == "unknown artist" else artist.strip()
    logging.getLogger(__name__).info(
        f"Initiating Soulseek download for {len(missing_tracks)} missing tracks of '{effective_artist} - {release_title}'..."
    )

    queued_files: List[Dict[str, Any]] = []
    enqueued_files: List[Dict[str, Any]] = []
    matched_tracks: List[Dict[str, Any]] = []
    queued_tracks: List[Dict[str, Any]] = []
    queue_errors: List[Dict[str, Any]] = []
    peer_queue_errors: List[Dict[str, Any]] = []
    matched_missing: Set[_TrackKey] = set()
    resolved_missing: Set[_TrackKey] = set()
    used_files: Set[Tuple[str, str]] = set()
    unavailable_users: Set[str] = set()
    failed_source_tracks: Dict[_TrackKey, Set[Tuple[str, str]]] = {}
    requested_by_key = {
        track_key(resolved): requested
        for requested, resolved in zip(requested_tracks, missing_tracks)
    }
    if complete_release_tracks is not None and (
        not complete_release_tracks
        or any(is_missing_track_placeholder(track.get("title", "")) for track in complete_release_tracks)
        or not set(requested_by_key).issubset({track_key(track) for track in complete_release_tracks})
    ):
        raise ValueError("Complete release verification requires the official tracklist, including all missing tracks")
    complete_release_available = False
    preferred = preferred_format.lower()
    if search_cache is None:
        search_cache = {}
    peer_meta: Dict[str, Dict[str, Any]] = {}
    seen_directories: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = {}
    directory_hits_cache: Dict[Tuple[str, str], Tuple[int, Set[int]]] = {}
    browse_candidates: Dict[Tuple[str, str], Tuple[str, str]] = {}
    browsed_directories: Set[Tuple[str, str]] = set()
    generic_release = False

    # The release's full tracklist, when known, gives every numbering and title.
    known_tracks = [dict(track) for track in (official_tracks or complete_release_tracks or missing_tracks)]
    known_index = {track_key(track): index for index, track in enumerate(known_tracks)}

    def expected_titles(track: Dict[str, Any]) -> List[str]:
        titles = [track.get("title") or ""]
        alternates = track.get("alt_titles")
        if isinstance(alternates, (list, tuple)):
            titles.extend(str(title) for title in alternates if title)
        unique = []
        for title in titles:
            if title.strip() and normalize_text(title) not in {normalize_text(item) for item in unique}:
                unique.append(title.strip())
        return unique or [""]

    def title_sig_words(titles: Iterable[str]) -> Set[str]:
        words: Set[str] = set()
        for title in titles:
            base = parse_track_title_structure(title, is_filename=False)["base_norm"]
            words.update(w for w in base.split() if len(w) >= 3 and w not in DIR_STOP_WORDS)
        return words

    known_titles = [expected_titles(track) for track in known_tracks]
    known_sig_words = [title_sig_words(titles) for titles in known_titles]

    def title_identity(track: Dict[str, Any]) -> Tuple[str, str]:
        """A title and its primary artist: two 'Intro' rows by different artists are distinct."""
        title = expected_titles(track)[0]
        base = parse_track_title_structure(title, is_filename=False)["base_norm"] or normalize_text(title)
        credit = (track.get("artist") or "").strip()
        if not credit or is_various_artists(credit) or credit.lower() == "unknown artist":
            credit = release_artist
        return base, normalize_text(primary_artist(credit)) if credit else ""

    title_counts: Dict[Tuple[str, str], int] = {}
    for track in known_tracks:
        identity = title_identity(track)
        title_counts[identity] = title_counts.get(identity, 0) + 1

    def repeated_title(track: Dict[str, Any]) -> bool:
        return title_counts.get(title_identity(track), 0) > 1

    def generic_title(track: Dict[str, Any]) -> bool:
        """'Intro', 'Interlude 2', 'Untitled': titles of album parts that many releases share."""
        return bool(_GENERIC_TITLE_RE.match(title_identity(track)[0]))

    # A mix, live or radio release comes from such folders; so does a live or mixed track.
    release_is_mix = bool(_MIXED_TITLE_RE.search(release_title or ""))
    mixed_folders: Dict[str, bool] = {}

    def mixed_source(track: Dict[str, Any], folder: str) -> bool:
        """Whether the folder is a radio capture, DJ mix, podcast, live set or playlist dump that cannot supply the row."""
        if release_is_mix:
            return False
        if folder not in mixed_folders:
            mixed_folders[folder] = _mixed_source_folder(folder, release_folder(folder))
        return mixed_folders[folder] and not any(
            _MIXED_TRACK_TITLE_RE.search(title) or _MIXED_SOURCE_RE.search(title)
            or _MIXED_RELEASE_FOLDER_RE.search(title)
            for title in expected_titles(track)
        )

    # Flat numbering across media and sequential vinyl indexes, from the known tracklist.
    disc_counts: Dict[int, int] = {}
    official_positions: Dict[_TrackKey, Tuple[Optional[int], Optional[int]]] = {}
    if official_tracks or complete_release_tracks:
        by_disc: Dict[int, List[Dict[str, Any]]] = {}
        for track in known_tracks:
            try:
                disc = int(track.get("disc_number") or 1)
            except (TypeError, ValueError):
                disc = 1
            by_disc.setdefault(disc, []).append(track)

        def order(track: Dict[str, Any]) -> Tuple[int, str, int]:
            token = vinyl_position(track.get("position_token") or track.get("track_number"))
            if token:
                return 0, token[0], token[1]
            return 1, "", track_position(track)[1] or 0

        offset = 0
        for disc in sorted(by_disc):
            rows = sorted(by_disc[disc], key=order)
            disc_counts[disc] = len(rows)
            for sequence, track in enumerate(rows, 1):
                official_positions[track_key(track)] = (sequence, offset + sequence)
            offset += len(rows)
    disc_count = max(disc_counts) if disc_counts else None
    # '205' is disc 2 track 5 only while no medium reaches three-digit track numbers.
    dnn_disc_count = disc_count if disc_counts and max(disc_counts.values()) < 100 else None

    def expected_position(track: Dict[str, Any]) -> _Expected:
        token = row_side(track)
        if token:
            disc, number = row_disc(track), None
        else:
            disc, number = track_position(track)
        sequence, absolute = official_positions.get(track_key(track), (None, None))
        sequence = _as_int(track.get("track_position")) or sequence or (None if token else number)
        absolute = _as_int(track.get("absolute_position")) or absolute
        return _Expected(disc, number, token, sequence, absolute)

    def report_progress(message: str) -> None:
        if on_progress:
            on_progress(len(matched_tracks), len(requested_tracks), message)

    def is_preferred(file: Dict[str, Any]) -> bool:
        filename = file.get("filename", "")
        label, _ = AudioQualityAnalyzer.determine_stream_quality(file)
        return (
            (preferred == "flac" and "FLAC" in label)
            or (preferred == "mp3-320" and Path(filename).suffix.lower() == ".mp3"
                and (file.get("bitRate") or 0) >= 320)
        )

    def candidate_rank(user: str, file: Dict[str, Any]) -> Tuple[Any, ...]:
        """Preferred format, then a healthy peer (slot, queue, speed), then quality and name."""
        filename = file.get("filename", "")
        _, quality = AudioQualityAnalyzer.determine_stream_quality(file)
        return (-int(is_preferred(file)), *_health_rank(peer_meta.get(peer_key(user))), -quality,
                user.casefold(), filename.casefold(), filename)

    def file_key(user: str, file: Dict[str, Any]) -> Tuple[str, str]:
        return user, file.get("filename", "").replace("\\", "/")

    def user_key(user: str) -> str:
        return re.sub(r"\s*\(.*?\)$", "", user).strip().casefold()

    release_words = normalize_text(release_title).split()
    release_sig_words = {word for word in release_words if word not in DIR_STOP_WORDS} or set(release_words)
    release_core = (release_sig_words - _ARTICLES) or release_sig_words
    # Words a folder may add around a one-word title: the release artist, or 'VA'.
    release_artist_words = (
        {"va", "v", "various", "artists"} if is_va
        else set(normalize_text(release_artist).split()) if release_artist else set()
    )

    def names_release(text: str) -> bool:
        if not release_sig_words or not release_sig_words.issubset(normalize_text(text).split()):
            return False
        if len(release_core) > 1:
            return True
        # One significant word ('Memory', 'Cambio') must be a whole part of the name:
        # 'Lazer Sword - Memory (2012)', not 'Fond Memory - People' or the artist 'Memory Tapes'.
        for segment in _filename_segments(text):
            words = normalize_text(_BRACKET_GROUP_RE.sub(" ", segment)).split()
            core = {
                word for word in words
                if word in release_core or not (
                    word in DIR_STOP_WORDS or word in _ARTICLES or word.isdigit() or word in release_artist_words
                )
            }
            if core == release_core:
                return True
        return False

    def release_folder(folder: str) -> str:
        """A disc or format subfolder ('CD 2', 'FLAC') belongs to its release folder."""
        leaf = os.path.basename(folder)
        words = normalize_text(leaf).split()
        # A release titled 'Album' or 'EP' names its own folder with stop words only.
        if names_release(leaf):
            return folder
        if DISC_DIR_PATTERN.match(leaf.strip()) or (words and all(
            word in DIR_STOP_WORDS or word.isdigit() for word in words
        )):
            parent = os.path.dirname(folder)
            if parent:
                return parent
        return folder

    def directory_key(user: str, filename: str) -> Tuple[str, str]:
        folder = os.path.dirname(filename.replace("\\", "/"))
        return peer_key(user), release_folder(folder).casefold()

    def note_seen(user: str, file: Dict[str, Any]) -> None:
        filename = file.get("filename", "")
        if filename and Path(filename).suffix.lower() in AUDIO_EXTENSIONS:
            seen_directories.setdefault(directory_key(user, filename), {})[filename.replace("\\", "/")] = file

    def directory_hits(user: str, filename: str) -> Set[int]:
        """Indexes of known release tracks that some seen file of this release folder matches."""
        key = directory_key(user, filename)
        files = seen_directories.get(key, {})
        cached = directory_hits_cache.get(key)
        if cached is not None and cached[0] == len(files):
            return cached[1]
        hits: Set[int] = set()
        bases = [(os.path.basename(name), set(normalize_text(os.path.basename(name)).split())) for name in files]
        for index, titles in enumerate(known_titles):
            sig = known_sig_words[index]
            for basename, words in bases:
                if sig and not sig & words:
                    continue
                if max(title_match_score(title, basename) for title in titles) >= 0.85:
                    hits.add(index)
                    break
        directory_hits_cache[key] = (len(files), hits)
        return hits

    def corroborated(track: Dict[str, Any], user: str, filename: str) -> bool:
        """The peer folder also holds at least two other tracks of this release."""
        own = known_index.get(track_key(track))
        return len(directory_hits(user, filename) - {own}) >= 2

    def credited_names(track: Dict[str, Any]) -> List[str]:
        """Names whose presence is artist evidence for the row."""
        credit = (track.get("artist") or "").strip()
        if credit and not is_various_artists(credit) and credit.lower() != "unknown artist":
            names = [credit]
            if release_artist and artist_credit_matches(credit, release_artist):
                names.append(release_artist)
            return names
        return [release_artist] if release_artist else []

    def remote_structure(track: Dict[str, Any], stem: str) -> Tuple[List[str], List[str], Optional[str], bool]:
        """
        (album artist parts, track artist parts, position token, names the release) from the
        parts of a filename before the part that reads as the expected title.
        """
        segments = _filename_segments(stem)
        titles = expected_titles(track)

        def reads_as_title(text: str) -> bool:
            return max(title_match_score(title, text) for title in titles) >= 0.80

        split = None
        for start in range(len(segments) - 1, -1, -1):
            if reads_as_title(" - ".join(segments[start:])):
                split = start
                break
        if split is None:
            return [], [], None, False
        token = None
        parts = []
        for index, part in enumerate(segments[:split]):
            if index == 0:
                part, lead = _strip_leading_position(part)
                token = token or lead
            whole = _POSITION_SEGMENT_RE.match(part)
            if whole:
                token = token or whole.group(1)
                continue
            if not _is_noise_segment(part):
                parts.append(part)
        if token is None:
            title_part = " - ".join(segments[split:])
            rest, lead = _strip_leading_position(title_part)
            # '2 Is 8.flac' keeps its number; '05 2 Is 8.flac' is track 5.
            if lead and max(title_match_score(t, rest, is_filename=False) for t in titles) >= max(
                title_match_score(t, title_part, is_filename=False) for t in titles
            ):
                token = lead
        scene = not re.search(r"\s", stem.strip()) and "-" in stem

        def readings(names: List[str]) -> List[str]:
            # Alternative readings of the artist: a scene name splits 'jay-z' at its hyphen.
            options = (["-".join(names)] if scene and len(names) > 1 else []) + names[:1] + names[-1:]
            return list(dict.fromkeys(option for option in options if option))

        release_index = [index for index, part in enumerate(parts) if names_release(part)]
        if release_index:
            # 'Album Artist - Album - NN - Title' and 'Compiler - Compilation - Artist - Title'.
            first = release_index[0]
            return readings(parts[:first]), readings(parts[release_index[-1] + 1:]), token, True
        return [], readings(parts), token, False

    def remote_position(track: Dict[str, Any], file: Dict[str, Any], basename: str,
                        token: Optional[str]) -> _Remote:
        remote = _Remote()
        raw_number = file.get("track_num_int") or file.get("track_number")
        meta_disc = _as_int(file.get("disc_number"))
        folder = os.path.dirname(file.get("filename", "").replace("\\", "/"))
        folder_disc = disc_number_from_folder(os.path.basename(folder)) if folder else None
        if folder_disc is not None and disc_count is not None and folder_disc > disc_count:
            folder_disc = None
        explicit = meta_disc or folder_disc

        def side(value: Optional[str]) -> Optional[Tuple[str, int]]:
            position = vinyl_position(value) if value and value[:1].isalpha() else None
            return position if position and position[1] <= 30 else None

        if raw_number is None:
            lead = _LEADING_VINYL_RE.match(basename)
            vinyl = side(lead.group(1) if lead else None)
            if vinyl:
                # 'B1 - Title.flac' names a side, not a disc of a CD edition.
                remote.token = vinyl
                remote.disc = explicit
                remote.explicit_disc = explicit is not None
                return remote
        source = basename
        parsed_disc, number, _ = parse_disc_and_track_number(
            raw_number, filename=source, meta_disc=explicit, disc_count=dnn_disc_count,
        )
        if raw_number is None and number is not None:
            # A title that starts with a number is not a position: '94.flac', '2 Is 8.flac'.
            stem_norm = normalize_text(_audio_stem(basename))
            for title in expected_titles(track):
                title_norm = normalize_text(title)
                if title_norm[:1].isdigit() and (stem_norm == title_norm or stem_norm.startswith(title_norm + " ")):
                    number = None
                    parsed_disc = explicit or 1
                    source = ""
                    break
        if number is None and raw_number is None and token:
            # 'Artist - Album - 05 - Title', 'VA (Album)-[05]-Artist - Title'.
            vinyl = side(token)
            if vinyl:
                remote.token = vinyl
                remote.disc = explicit
                remote.explicit_disc = explicit is not None
                return remote
            source = f"{token} - x"
            parsed_disc, number, _ = parse_disc_and_track_number(
                None, filename=source, meta_disc=explicit, disc_count=dnn_disc_count,
            )
        remote.number = number
        prefix_disc = bool(source and _DISC_TRACK_PREFIX_RE.match(source)) or bool(
            re.match(r"^\s*\d{1,2}\s*[-_.]\s*\d{1,3}\s*$", str(raw_number or ""))
        )
        # With the medium count, '205 - Title' is disc 2 track 5.
        dnn = (raw_number is None and dnn_disc_count is not None and number is not None
               and parsed_disc is not None and parsed_disc != (explicit or 1))
        remote.explicit_disc = bool(explicit) or prefix_disc or dnn
        remote.disc = parsed_disc if remote.explicit_disc else None
        if number is not None and number >= 100 and not remote.explicit_disc and 1 <= number // 100 <= 9:
            remote.alternate = (number // 100, number % 100)
        return remote

    def compare_positions(expected: _Expected, remote: _Remote) -> Optional[str]:
        """'same', 'conflict', 'weak' (vinyl rows without a known index) or None (unknown)."""
        if remote.token:
            if expected.token:
                return "same" if remote.token == expected.token else "conflict"
            return None
        if remote.number is None:
            if remote.explicit_disc and remote.disc is not None and not expected.token and remote.disc != expected.disc:
                return "conflict"
            return None
        if expected.token:
            # A side token maps to digital numbering only through the official tracklist.
            candidates = {value for value in (expected.sequence, expected.absolute) if value}
            if remote.explicit_disc and remote.disc is not None and remote.disc != expected.disc:
                return "weak"
            if not candidates:
                return None
            return "same" if remote.number in candidates else "conflict"
        if remote.explicit_disc and remote.disc is not None:
            if remote.disc != expected.disc:
                return "conflict"
            if expected.number is None:
                return None
            return "same" if remote.number in {expected.number, expected.sequence} else "conflict"
        if expected.number is None:
            return None
        if remote.number == expected.number:
            return "same"
        if expected.absolute and remote.number == expected.absolute:
            return "same"
        if remote.alternate and remote.alternate == (expected.disc, expected.number):
            return "same"
        return "conflict"

    def path_names(track: Dict[str, Any], filename: str, names: List[str]) -> bool:
        """A credited name is a folder of the path, or whole words in it."""
        folders = [part for part in filename.replace("\\", "/").split("/")[:-1] if part.strip()]
        folder_words = " " + normalize_text(" / ".join(folders)) + " "
        for folder in folders:
            for part in _filename_segments(folder):
                part = re.sub(r"^[\(\[][^\)\]]*[\)\]]\s*", "", part).strip()
                if part and any(artist_credit_matches(name, part) for name in names):
                    return True
        # Inside a longer folder name only a name of two or more words is evidence:
        # 'Lone' also occurs in 'Lone Wolf', 'Azealia Banks' rarely in anything else.
        for name in names:
            for norm in {*split_artist_credit(name), normalize_text(name)}:
                if len(norm.split()) >= 2 and re.search(r"(?<!\w)" + re.escape(norm) + r"(?!\w)", folder_words):
                    return True
        return False

    def candidate_matches(track: Dict[str, Any], file: Dict[str, Any], individual: bool,
                          user: str = "", require_artist: bool = False, strict: bool = False) -> Optional[_Match]:
        """
        Whether a peer file is the requested track. Positions are enforced in this release's
        folder (or for repeated titles); elsewhere the title must be exact. Every match needs
        evidence beyond its title: the credited artist, this release's folder, two other tracks
        of the release in the folder, or the duration. Outside the release's folder a clearly
        different duration rules the file out, a radio capture, DJ mix, podcast, live set or
        playlist dump never qualifies (unless the release or the row is such a recording), and
        a generic title ('Intro') needs a matching duration. A row marked exclusive_title
        (another song shares its title) comes only from this release's folder.
        strict (history) needs the exact title and the credited artist itself, or the release
        folder for compilations and rows without a credit.
        """
        filename = file.get("filename", "").replace("\\", "/")
        basename = os.path.basename(filename)
        if Path(filename).suffix.lower() not in AUDIO_EXTENSIONS or file.get("isLocked"):
            return None
        titles = expected_titles(track)
        score = max(title_match_score(title, basename) for title in titles)
        if score < (0.80 if individual else 0.85):
            return None
        stem = _audio_stem(basename)
        album_parts, artist_parts, token, names_release_in_name = remote_structure(track, stem)

        # A filename or tag that names another artist rules the file out.
        credit = (track.get("artist") or "").strip()
        known_credit = credit and not is_various_artists(credit) and credit.lower() != "unknown artist"
        allowed = [name for name in ([credit] if known_credit else []) + [release_artist] if name]
        version_text = parse_track_title_structure(titles[0], is_filename=False)["version_text"] or ""
        version_text = version_text.replace(" ", "")
        tagged = (file.get("artist") or "").strip()

        def plausible_artist(remote_artist: str) -> bool:
            if is_various_artists(remote_artist) or names_release(remote_artist):
                return True
            if any(artist_credit_matches(name, remote_artist) for name in allowed):
                return True
            # 'Machinedrum - Wallis & Futuna (Machine Drum remix)': the remixer named in the title.
            compact = normalize_text(remote_artist).replace(" ", "")
            return bool(compact) and compact in version_text

        if allowed and (
            (artist_parts and not any(plausible_artist(reading) for reading in artist_parts))
            or (tagged and not plausible_artist(tagged))
        ):
            return None

        folder = os.path.dirname(filename)
        named_context = names_release(os.path.basename(release_folder(folder))) or names_release_in_name
        context = 2 if named_context else 1 if corroborated(track, user, filename) else 0
        repeated = repeated_title(track)
        exact = score >= _EXACT
        duration = track.get("duration")
        length = file.get("length")
        duration_match = duration_conflict = False
        try:
            if duration and length:
                expected_seconds, difference = float(duration), abs(float(duration) - float(length))
                duration_match = difference <= DURATION_TOLERANCE
                duration_conflict = difference > max(DURATION_CONFLICT_SECONDS, DURATION_CONFLICT_RATIO * expected_seconds)
        except (TypeError, ValueError):
            duration_match = duration_conflict = False
        if duration_conflict and not named_context:
            # Artist and title alone cannot tell an album version from an edit or another
            # 'Intro'; only a folder named after the release outweighs a misreported length.
            return None
        if not named_context and mixed_source(track, folder):
            # A radio capture, DJ-mix segment, live set or playlist dump carries the album
            # recording's title, but cut, crossfaded, talked over or re-encoded from a stream.
            return None
        if not context and not duration_match and generic_title(track):
            # Another release's 'Intro' (by the same artist, in an artist's folder) is
            # another recording; only a matching length outside this release says otherwise.
            return None
        if not context and track.get("exclusive_title"):
            # The caller knows another song with this title: only this release's folder has this one.
            return None
        comparison = compare_positions(expected_position(track), remote_position(track, file, basename, token))
        if (context or repeated) and comparison == "conflict":
            # Another edition of the release (a CD rip of a 2x12", a deluxe edition with an
            # inserted track) numbers the same recording differently. Its exact, unique title
            # and duration identify it; complete-release verification still needs positions.
            if repeated or not exact or not duration_match or complete_release_tracks is not None:
                return None
        # A repeated title ('Intro', 'Interlude') is identified only by its place in this
        # release; another release's copy may be any of them, or none.
        if repeated and (comparison == "weak" or not context):
            return None
        same_position = comparison == "same"
        # Outside the release's folder another edition's numbering says nothing, but only the
        # exact title of the recording qualifies.
        if not context and not repeated and not exact and not same_position:
            return None

        names = credited_names(track)
        if strict:
            if not exact:
                return None
            folder_words = " " + normalize_text(folder) + " "
            strict_names = {
                norm for name in names for norm in {*split_artist_credit(name), normalize_text(name)} if norm
            }
            # The credited artist. A compilation's own folder rarely names each track's
            # artist, so there the folder is enough; an artist's album title is not unique.
            # A live transfer in the release's folder at this position is what a search
            # chose on that evidence; a finished one may be another artist's album.
            live = "Succeeded" not in transfer_states(file)
            evidence = (named_context and (is_va or not strict_names or (live and same_position))) or any(
                re.search(r"(?<!\w)" + re.escape(norm) + r"(?!\w)", folder_words)
                or normalize_text(tagged) == norm
                or any(norm in split_artist_credit(part) or normalize_text(part) == norm
                       for part in [*artist_parts, *album_parts])
                for norm in strict_names
            )
            if not evidence:
                return None
            return _Match(score, context, named_context, same_position, duration_match)

        artist_evidence = bool(names) and (
            any(artist_credit_matches(name, part) for name in names for part in [*artist_parts, *album_parts])
            or bool(tagged and any(artist_credit_matches(name, tagged) for name in names))
            or path_names(track, filename, names)
        )
        title_is_release = normalize_text(titles[0]) == normalize_text(release_title)
        folder_evidence = (
            named_context and not require_artist and not title_is_release
            and (same_position or not generic_release)
        )
        if not (artist_evidence or duration_match or folder_evidence or corroborated(track, user, filename)):
            return None
        return _Match(score, context, named_context, same_position, duration_match)

    protected_records = {(peer_key(user), path): file for (user, path), file in history.protected.items()}
    # Rows that earlier requests submitted the protected transfers for (see core.submissions).
    submitted = SubmissionLog().identities(history.protected) if history.protected else {}
    submitted_transfers: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
    for (user, path), file in history.protected.items():
        for identity in submitted.get(submission_source(user, path), ()):
            submitted_transfers.setdefault(identity, []).append((user, file))
    identities = {track_key(track): row_identity(track, release_title, track_slot(track), artist)
                  for track in missing_tracks}
    # (user, filename, row identity) of files slskd accepted in this request.
    submissions: List[Tuple[str, str, Optional[str]]] = []

    def mixed_transfer(track: Dict[str, Any], file: Dict[str, Any]) -> bool:
        # As in matching, a folder that names this release may itself be a mix.
        folder = os.path.dirname(file.get("filename", "").replace("\\", "/"))
        return mixed_source(track, folder) and not names_release(os.path.basename(release_folder(folder)))

    def submitted_for(track: Dict[str, Any], user: str, file: Dict[str, Any]) -> bool:
        """Whether an earlier request submitted this transfer for this very row."""
        identity = identities.get(track_key(track))
        # An earlier request may have taken a radio capture or mix segment before
        # such folders were refused; its log entry must not keep standing for the row.
        return (bool(identity) and identity in submitted.get(submission_source(user, file.get("filename", "")), ())
                and not mixed_transfer(track, file))

    def history_satisfies(track: Dict[str, Any], user: str, file: Dict[str, Any], match: _Match) -> bool:
        """
        A transfer submitted for this row fulfils it. Otherwise a live transfer that
        matches the row fulfils it, and a finished one only when it came from this release.
        Matching already refused another release's 'Intro', a mix segment and an exclusive
        title outside this release, wherever a transfer is queued.
        """
        if submitted_for(track, user, file):
            return True
        record = protected_records.get((peer_key(user), source_key(user, file.get("filename", ""))[1]), {})
        if "Succeeded" in transfer_states(record):
            return match.named_context
        return True

    def stale_sources(key: _TrackKey) -> Set[Tuple[str, str]]:
        return {source for source in failed_source_tracks.get(key, set()) if source in history.stale}

    def retire_stale(key: _TrackKey, track: Dict[str, Any], kept: bool = False) -> None:
        """
        A stale transfer may still start, so it is stopped only once its replacement is
        queued, or once another live transfer (kept) is known to fulfil its row.
        """
        try:
            history.prepare_recovery(slskd_client, stale_sources(key))
        except SlskdAPIError as error:
            done = "Kept a queued transfer" if kept else "Queued a replacement"
            queue_errors.append({
                "stage": "recovery", "tracks": [track],
                "error": f"{done}, but the stale transfer it replaces may still start: {error}",
            })

    def queue_matches(user: str, matches: List[Tuple[Dict[str, Any], Dict[str, Any]]]) -> None:
        if user_key(user) in unavailable_users:
            return
        items = []
        originals = []
        keys = []
        for track, file in matches:
            key = track_key(track)
            if key in resolved_missing or file_key(user, file) in used_files:
                continue
            disc, number = track_slot(track)
            item = {
                "filename": file.get("filename"), "size": file.get("size", 0),
                "title": track.get("title", ""), "artist": track.get("artist") or artist, "disc_number": disc,
                "track_number": number, "user": user,
            }
            if key not in matched_missing:
                matched_tracks.append(requested_by_key[key])
                matched_missing.add(key)
            resolved_missing.add(key)
            used_files.add(file_key(user, file))
            if history.is_protected(user, file.get("filename", "")):
                # An existing live/successful transfer already fulfills this
                # request; retain its attribution without sending another POST.
                queued_files.append(item)
                if not dry_run:
                    queued_tracks.append(requested_by_key[key])
                    submissions.append((user, item["filename"], identities.get(key)))
                    # A second, stale copy of this row (a cancel that failed earlier,
                    # another tool's request) would download the track twice.
                    retire_stale(key, requested_by_key[key], kept=True)
                continue
            items.append(item)
            originals.append(requested_by_key[key])
            keys.append(key)
        if items and not dry_run:
            ready_items, ready_originals, ready_keys = [], [], []
            for item, original, key in zip(items, originals, keys):
                try:
                    # Stop only this row's failed sources before replacing it; its stale
                    # ones wait for the replacement. Another row's cancellation failure
                    # must not block the batch.
                    history.prepare_recovery(slskd_client, failed_source_tracks.get(key, set()) - stale_sources(key))
                except SlskdAPIError as error:
                    queue_errors.append({"stage": "recovery", "error": str(error), "tracks": [original]})
                    continue
                ready_items.append(item)
                ready_originals.append(original)
                ready_keys.append(key)
            items, originals, keys = ready_items, ready_originals, ready_keys
        # Keep each API call to one chunk, so a later failure cannot hide earlier successes.
        for start in range(0, len(items), 50):
            chunk = items[start:start + 50]
            tracks = originals[start:start + 50]
            if not dry_run:
                report_progress(f"Queueing {len(chunk)} matched tracks from {user} in slskd…")
                try:
                    slskd_client.enqueue_download(user, chunk)
                except (SlskdPeerUnavailableError, SlskdPeerBlockedError) as error:
                    unavailable_users.add(user_key(user))
                    # A definite peer connection failure is safe to recover from
                    # by choosing another peer. Keep any accepted files, and free
                    # both the rejected chunk and any chunks not yet submitted.
                    accepted = {file_key(user, file) for file in error.queued_files}
                    failed_tracks = []
                    for item, track, key in zip(items[start:], originals[start:], keys[start:]):
                        identity = file_key(user, item)
                        if identity in accepted:
                            queued_files.append(item)
                            enqueued_files.append(item)
                            queued_tracks.append(track)
                            submissions.append((user, item["filename"], identities.get(key)))
                            retire_stale(key, track)
                        else:
                            resolved_missing.discard(key)
                            used_files.discard(identity)
                            failed_tracks.append(track)
                    if failed_tracks:
                        detail = {"user": user, "error": str(error), "tracks": failed_tracks}
                        queue_errors.append(detail)
                        peer_queue_errors.append(detail)
                    report_progress(f"Peer {user} is unavailable; trying other matches…")
                    break
                except Exception as error:
                    # slskd did not queue the refused files; those rows may still come
                    # from another candidate. After a lost reply or a server error a file
                    # may be queued, and another copy would download it twice.
                    definite = isinstance(error, SlskdEnqueueRefusedError)
                    accepted = {file_key(user, file) for file in getattr(error, "queued_files", ())}
                    rejected_tracks = []
                    for item, track, key in zip(chunk, tracks, keys[start:start + 50]):
                        if file_key(user, item) in accepted:
                            queued_files.append(item)
                            enqueued_files.append(item)
                            queued_tracks.append(track)
                            submissions.append((user, item["filename"], identities.get(key)))
                            retire_stale(key, track)
                        else:
                            rejected_tracks.append(track)
                            if definite:
                                resolved_missing.discard(key)
                    if rejected_tracks:
                        detail = {"user": user, "error": str(error), "tracks": rejected_tracks}
                        queue_errors.append(detail)
                        if definite:
                            peer_queue_errors.append(detail)
                    continue
                queued_tracks.extend(tracks)
                enqueued_files.extend(chunk)
                for item, key, track in zip(chunk, keys[start:start + 50], tracks):
                    submissions.append((user, item["filename"], identities.get(key)))
                    retire_stale(key, track)
            queued_files.extend(chunk)

    def remaining_tracks() -> List[Dict[str, Any]]:
        return [track for track in missing_tracks if track_key(track) not in resolved_missing]

    def record_search_errors(errors: Dict[str, str], tracks_by_query: Dict[str, List[Dict[str, Any]]]) -> None:
        """One retryable 'search' error per distinct failure, listing the rows it left unsearched."""
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for query, message in errors.items():
            rows = grouped.setdefault(message, [])
            for track in tracks_by_query.get(query, []):
                row = requested_by_key[track_key(track)]
                if not any(row is existing for existing in rows):
                    rows.append(row)
        for message, rows in grouped.items():
            queue_errors.append({"stage": "search", "error": message, "tracks": rows})

    def unique_queries(queries: List[str], planned: Iterable[str] = ()) -> List[str]:
        """
        Each query once in its cleaned spelling, leaving out those already planned. Peers
        match words, so 'Bleep: The Top 100' and 'Bleep The Top 100' would cost two
        searches for the same files; the cleaned one is also what discovery searched.
        """
        seen = {_variant_key(query) for query in planned}
        result: Dict[str, str] = {}
        for query in queries:
            query = " ".join(str(query or "").split())
            if query and _variant_key(query) not in seen and _variant_key(query) not in result:
                result[_variant_key(query)] = clean_search_phrase(query) or query
        return list(result.values())

    def run_searches(queries: List[str], solo: bool = False) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str]]:
        """Search each query once per run; failed searches stay retryable and are not cached."""
        results: Dict[str, Dict[str, Any]] = {}
        errors: Dict[str, str] = {}
        pending = []
        for query in queries:
            cached = search_cache.get(_search_key(query))
            if cached is not None:
                results[query] = cached
            else:
                pending.append(query)
        if not pending:
            return results, errors
        # Only a refresh names the option, so clients without it keep working.
        refresh = {} if use_existing else {"use_existing": False}
        try:
            if solo and len(pending) == 1:
                batch = {pending[0]: slskd_client.search(query=pending[0], timeout=search_timeout, **refresh)}
            else:
                batch = slskd_client.batch_search(pending, timeout=search_timeout, **refresh)
        except Exception as error:
            return results, {query: str(error) for query in pending}
        for query in pending:
            entry = batch.get(query) if isinstance(batch, dict) else None
            if not isinstance(entry, dict):
                entry = {}
            if "error" in entry:
                errors[query] = str(entry.get("error") or "search failed")
                continue
            search_cache[_search_key(query)] = entry
            results[query] = entry
        return results, errors

    def truncated(result: Dict[str, Any]) -> bool:
        return any(state in str(result.get("state") or "") for state in _LIMIT_STATES)

    def track_candidates(index: PeerCandidateIndex, track: Dict[str, Any]) -> List[Any]:
        words = title_sig_words(expected_titles(track))
        if not words:
            return list(index.all_audio_files)
        found: Dict[int, Any] = {}
        for word in words:
            for candidate in index.word_to_audio_files.get(word, []):
                found[id(candidate)] = candidate
        return list(found.values()) if found else list(index.all_audio_files)

    def candidate_available(track: Dict[str, Any], user: str, file: Dict[str, Any], individual: bool,
                            require_artist: bool = False) -> Optional[_Match]:
        match = candidate_matches(track, file, individual, user=user, require_artist=require_artist)
        if match is None:
            return None
        if history.is_protected(user, file.get("filename", "")):
            # A finished download from another release would be imported there.
            return match if history_satisfies(track, user, file, match) else None
        if history.excludes(user, file.get("filename", "")):
            failed_source_tracks.setdefault(track_key(track), set()).add(source_key(user, file.get("filename", "")))
            return None
        return match

    def reuse_history() -> None:
        # History is independent of search results: a queued/offline peer or a
        # completed file may never appear in another search. Resolve these first,
        # and remember stale transfers so a replacement cancels them.
        directories: Dict[Tuple[str, str], Dict[str, Any]] = {}
        records = [(key, file, False) for key, file in history.protected.items()]
        records += [(key, file, True) for key, file in history.stale.items()]
        stale_records = set()
        for (user, _), file, stale in records:
            folder = os.path.dirname(file["filename"].replace("\\", "/"))
            directories.setdefault((user, folder), {"matched_search_files": []})["matched_search_files"].append(file)
            if stale:
                stale_records.add(source_key(user, file["filename"]))
        if not directories:
            return
        index = PeerCandidateIndex(directories)
        for track in remaining_tracks():
            # A transfer submitted for this very row stands for it wherever its folder is.
            satisfying = sorted(
                ((user, file) for user, file in submitted_transfers.get(identities.get(track_key(track)) or "", ())
                 if not mixed_transfer(track, file)),
                key=lambda item: candidate_rank(*item),
            )
            chosen = {source_key(user, file["filename"]) for user, file in satisfying}
            candidates = track_candidates(index, track)
            for candidate in sorted(candidates, key=lambda item: candidate_rank(item.user, item.raw_file)):
                file = candidate.raw_file
                # Unlike query results, unrelated history has no search context.
                # Require the exact title and artist evidence outside the title (or
                # the release folder for compilations with unknown track credits).
                match = candidate_matches(track, file, True, user=candidate.user, strict=True)
                if match is None:
                    continue
                source = source_key(candidate.user, file["filename"])
                if source in stale_records:
                    # Link every stale copy of the row, also one ranked after the live
                    # transfer that keeps it, so that copy is stopped too.
                    if history.excludes(candidate.user, file["filename"]):
                        failed_source_tracks.setdefault(track_key(track), set()).add(source)
                    continue
                if source not in chosen and history_satisfies(track, candidate.user, file, match):
                    satisfying.append((candidate.user, file))
                    chosen.add(source)
            for user, file in satisfying:
                queue_matches(user, [(track, file)])
                if track_key(track) in resolved_missing:
                    break

    def evaluate_album(responses: List[Dict[str, Any]], require_artist: bool = False,
                       remember_folders: bool = False) -> None:
        nonlocal complete_release_available
        # Compare all peers before choosing, so response ordering cannot override format preference.
        peer_directories: Dict[Tuple[str, str], Dict[str, Any]] = {}
        folder_names: Dict[Tuple[str, str], str] = {}
        for response in responses:
            user = response.get("username")
            if not user or user_key(user) in unavailable_users:
                continue
            peer_meta[peer_key(user)] = _peer_meta(response)
            for file in response.get("files", []):
                filename = file.get("filename", "")
                if Path(filename).suffix.lower() in AUDIO_EXTENSIONS and not file.get("isLocked"):
                    note_seen(user, file)
                    # Sibling disc folders belong to one release folder.
                    folder = release_folder(os.path.dirname(filename.replace("\\", "/")))
                    if complete_release_tracks is not None and not set(normalize_text(release_title).split()).issubset(
                        normalize_text(os.path.basename(folder)).split()
                    ):
                        # Other folders and peers do not verify the release.
                        continue
                    info = peer_directories.setdefault((user, folder), {"matched_search_files": []})
                    info["matched_search_files"].append(file)
                    folder_names.setdefault((user, folder), filename[:max(filename.rfind("\\"), filename.rfind("/"))])

        candidate_index = PeerCandidateIndex(peer_directories)
        matches_by_directory: Dict[Tuple[str, str], Dict[_TrackKey, Any]] = {}
        context_by_directory: Dict[Tuple[str, str], int] = {}
        placed_by_directory: Dict[Tuple[str, str], int] = {}
        selected_by_directory: Dict[Tuple[str, str], Set[Tuple[str, str]]] = {}
        protected_matches: Dict[_TrackKey, Tuple[str, Dict[str, Any], Dict[str, Any]]] = {}
        for track in complete_release_tracks if complete_release_tracks is not None else remaining_tracks():
            candidates = track_candidates(candidate_index, track)
            for candidate in sorted(candidates, key=lambda item: candidate_rank(item.user, item.raw_file)):
                if complete_release_tracks is not None and max(
                    title_match_score(title, candidate.base_filename) for title in expected_titles(track)
                ) < _EXACT:
                    continue
                directory = (candidate.user, candidate.dir_name)
                matches = matches_by_directory.setdefault(directory, {})
                selected_files = selected_by_directory.setdefault(directory, set())
                identity = file_key(candidate.user, candidate.raw_file)
                # Inspect even lower-ranked matches so old failed transfers are
                # stopped when a better file from this directory replaces them.
                match = candidate_available(track, candidate.user, candidate.raw_file, False, require_artist)
                if match is None:
                    continue
                if remember_folders and match.named_context and directory in folder_names:
                    browse_candidates.setdefault(directory_key(candidate.user, candidate.raw_file.get("filename", "")),
                                                 (candidate.user, folder_names[directory]))
                if complete_release_tracks is None and history.is_protected(candidate.user, candidate.raw_file.get("filename", "")):
                    protected_matches.setdefault(track_key(track), (candidate.user, track, candidate.raw_file))
                if track_key(track) not in matches and identity not in selected_files:
                    matches[track_key(track)] = (track, candidate.raw_file)
                    selected_files.add(identity)
                    context_by_directory[directory] = max(context_by_directory.get(directory, 0), match.context)
                    placed_by_directory[directory] = placed_by_directory.get(directory, 0) + int(match.same_position)

        # Keep a healthy queued transfer even when another peer offers a higher
        # ranked format; sending its alternative would download the track twice.
        for user, track, file in protected_matches.values():
            queue_matches(user, [(track, file)])

        directory_matches = []
        for (user, folder), matches in matches_by_directory.items():
            if complete_release_tracks is not None and len(matches) != len(complete_release_tracks):
                continue
            if matches:
                files = [file for _, file in matches.values()]
                qualities = [AudioQualityAnalyzer.determine_stream_quality(file)[1] for file in files]
                # This release's own folder first, then the most requested tracks in the
                # preferred format, numbered as on this release, from one healthy peer.
                rank = (
                    -int(context_by_directory.get((user, folder), 0) > 0),
                    -sum(is_preferred(file) for file in files), -len(matches),
                    -placed_by_directory.get((user, folder), 0),
                    *_health_rank(peer_meta.get(peer_key(user))),
                    -sum(qualities), user.casefold(), folder.casefold(),
                )
                directory_matches.append((rank, user, list(matches.values())))
        for _, user, matches in sorted(directory_matches, key=lambda item: item[0]):
            if complete_release_tracks is not None:
                complete_release_available = True
                report_progress(f"Verified complete release: {len(matches)}/{len(complete_release_tracks)} tracks from {user}…")
                matches = [(track, file) for track, file in matches if track_key(track) in requested_by_key]
            queue_matches(user, matches)
            if not remaining_tracks():
                break

    def browse_release_folders() -> None:
        """Read whole release folders that searches showed only in part, healthiest peer first."""
        pending = [
            (key, user, folder) for key, (user, folder) in browse_candidates.items()
            if key not in browsed_directories
        ]
        pending.sort(key=lambda item: (_health_rank(peer_meta.get(item[0][0])), item[1].casefold(), item[2]))
        for key, user, folder in pending:
            if not remaining_tracks() or len(browsed_directories) >= MAX_FOLDER_BROWSES:
                return
            if user_key(user) in unavailable_users or history.is_peer_blocked(user):
                continue
            # Searches return only matching files; a folder seen whole needs no browse.
            if len(seen_directories.get(key, {})) >= len(known_tracks):
                continue
            browsed_directories.add(key)
            report_progress(f"Reading {user}'s release folder for the remaining tracks…")
            try:
                listing = slskd_client.browse_directory(user, folder)
            except Exception as error:
                logging.getLogger(__name__).info(f"Could not browse {user}'s folder {folder}: {error}")
                continue
            if not isinstance(listing, list):
                continue
            files = [file for file in listing if isinstance(file, dict) and file.get("filename")]
            if files:
                evaluate_album([{**peer_meta.get(peer_key(user), {}), "username": user, "files": files}])

    def search_album() -> None:
        nonlocal generic_release
        if not release_title or not remaining_tracks():
            return
        title_query = clean_search_phrase(release_title) or release_title
        if is_va:
            # 'Various Artists <title>' finds a subset of the bare title's results.
            first = [title_query]
            rest = [release_title]
        else:
            album_query = f"{artist} {release_title}"
            first = [album_query]
            rest = [clean_search_phrase(album_query)]
            lead = primary_artist(artist)
            if lead and normalize_text(lead) != normalize_text(artist):
                rest.append(clean_search_phrase(f"{lead} {release_title}"))
            rest.append(fold_search_phrase(album_query))
            # Title-only results need artist evidence beyond the folder name.
            rest.append(title_query)
        first = unique_queries(first)
        rest = unique_queries(rest, planned=first)
        groups = [group for group in (first, rest) if group]
        title_keys = {_variant_key(title_query), _variant_key(release_title)}
        # Keep the likely album query as a cheap fast path. If it misses, wait
        # for the cleaned/title-only variants together instead of serially.
        for attempt, group in enumerate(groups, 1):
            remaining = remaining_tracks()
            if not remaining:
                break
            report_progress(f"Release search {attempt}/{len(groups)}: {' / '.join(group)}…")
            results, errors = run_searches(group, solo=len(group) == 1)
            record_search_errors(errors, {query: remaining for query in group})
            # A response may contain only artwork, locked files or wrong versions.
            # Continue the fallback queries until actual requested tracks match.
            for query in group:
                result = results.get(query)
                if result is None:
                    continue
                if _variant_key(query) in title_keys and truncated(result):
                    generic_release = True
                if not remaining_tracks():
                    continue
                evaluate_album(result.get("responses", []),
                               require_artist=not is_va and _variant_key(query) in title_keys)
        if is_va:
            search_compilation(title_query)

    def search_compilation(title_query: str) -> None:
        """Generic compilation titles: qualify the title by credited artists, then browse."""
        if not remaining_tracks():
            return
        single_word = len([word for word in normalize_text(release_title).split() if word not in DIR_STOP_WORDS]) <= 1
        if not (single_word or generic_release):
            return
        names: Dict[str, str] = {}
        present = [track for track in known_tracks if track_key(track) not in requested_by_key]
        for track in remaining_tracks() + present:
            credit = (track.get("artist") or "").strip()
            if credit and not is_various_artists(credit) and credit.lower() != "unknown artist":
                lead = primary_artist(credit)
                if normalize_text(lead):
                    names.setdefault(normalize_text(lead), lead)
        queries = unique_queries([clean_search_phrase(f"{title_query} {name}") for name in names.values()])
        queries = queries[:MAX_QUALIFIED_QUERIES]
        for start in range(0, len(queries), 2):
            remaining = remaining_tracks()
            if not remaining:
                return
            group = queries[start:start + 2]
            report_progress(f"Compilation search: {' / '.join(group)}…")
            results, errors = run_searches(group)
            record_search_errors(errors, {query: remaining for query in group})
            for query in group:
                if query in results and remaining_tracks():
                    evaluate_album(results[query].get("responses", []), remember_folders=True)
            browse_release_folders()
        if remaining_tracks() and generic_release:
            remaining = remaining_tracks()
            query = f"Various Artists {title_query}"
            report_progress(f"Compilation search: {query}…")
            results, errors = run_searches([query], solo=True)
            record_search_errors(errors, {query: remaining})
            if query in results:
                evaluate_album(results[query].get("responses", []))

    def track_queries(track: Dict[str, Any]) -> List[str]:
        title = track.get("title", "")
        if not clean_search_phrase(title):
            # A symbol-only title ('★') would search for the artist alone.
            return []
        credit = (track.get("artist") or "").strip()
        known_artist = credit and not is_various_artists(credit) and credit.lower() != "unknown artist"
        # Peers name the primary artist; 'feat.' guests and joint credits narrow results.
        if known_artist:
            query_artist = primary_artist(credit)
        else:
            query_artist = "Various Artists" if is_va else primary_artist(artist)
        separator = " " if search_scope == "track" else " - "
        queries = [
            f"{query_artist}{separator}{title}".strip(),
            clean_search_phrase(f"{query_artist} {title}"),
            fold_search_phrase(f"{query_artist} {title}"),
        ]
        core = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*", " ", title).strip()
        if core and normalize_text(core) != normalize_text(title) and clean_search_phrase(core):
            queries.append(clean_search_phrase(f"{query_artist} {core}"))
        for alternate in expected_titles(track)[1:2]:
            if clean_search_phrase(alternate):
                queries.append(clean_search_phrase(f"{query_artist} {alternate}"))
        # The release context finds compilation files that omit the track artist;
        # a single's title is its release title and adds nothing.
        if release_title and normalize_text(title) != normalize_text(release_title):
            queries.append(clean_search_phrase(f"{release_title} {title}"))
        return unique_queries(queries)

    def fallback_queries(track: Dict[str, Any]) -> List[str]:
        """
        Peers match whole words inconsistently ('Sepalcure Lose' finds "All You've Got
        To Lose.mp3" where the full title does not). For a compilation or guest track
        the title searches missed, try the artist with the title's most distinctive
        words, then the artist alone. An album's own artist was already searched with
        the album, and MusicBrainz placeholder credits ('[unknown]') name nobody.
        """
        credit = (track.get("artist") or "").strip()
        if (not credit or is_various_artists(credit) or credit.lower() == "unknown artist"
                or re.fullmatch(r"\[[^\]]+\]", credit)):
            return []
        query_artist = primary_artist(credit)
        if not is_va and normalize_text(query_artist) == normalize_text(primary_artist(artist)):
            return []
        if len(normalize_text(query_artist).replace(" ", "")) < 4 or not clean_search_phrase(query_artist):
            return []
        title = track.get("title", "")
        core = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*", " ", title).strip() or title
        words = [word for word in clean_search_phrase(core).split()
                 if len(word) >= 4 and word.isalpha() and normalize_text(word) not in DIR_STOP_WORDS]
        distinctive = sorted(words, key=len, reverse=True)[:2]
        queries = []
        if distinctive:
            queries.append(clean_search_phrase(f"{query_artist} {' '.join(w for w in words if w in distinctive)}"))
        queries.append(clean_search_phrase(query_artist))
        return unique_queries(queries)

    def search_tracks(fallback: bool = False) -> None:
        remaining = remaining_tracks()
        if not remaining:
            return
        plan = fallback_queries if fallback else track_queries
        queries_by_track = {track_key(track): plan(track) for track in remaining}

        for round_index in range(max((len(queries) for queries in queries_by_track.values()), default=0)):
            query_tracks: Dict[str, List[Dict[str, Any]]] = {}
            for track in remaining_tracks():
                queries = queries_by_track.get(track_key(track), [])
                if round_index < len(queries):
                    query_tracks.setdefault(queries[round_index], []).append(track)
            if not query_tracks:
                continue
            detail = (next(iter(query_tracks)) if len(query_tracks) == 1
                      else f"{len(query_tracks)} queries for {len(remaining_tracks())} remaining tracks")
            report_progress(f"Track search round {round_index + 1}: {detail}…")
            results, errors = run_searches(list(query_tracks), solo=search_scope == "track")
            record_search_errors(errors, query_tracks)
            if errors and len(errors) == len(query_tracks):
                # Transport failure affects all query variants, unlike a successful
                # search with no usable files. The other strategy may still work.
                break

            candidates_by_track = {}
            for query, tracks in query_tracks.items():
                if query not in results:
                    continue
                candidates = []
                for response in results[query].get("responses", []):
                    user = response.get("username")
                    if not user or user_key(user) in unavailable_users:
                        continue
                    peer_meta[peer_key(user)] = _peer_meta(response)
                    for file in response.get("files", []):
                        note_seen(user, file)
                        candidates.append((user, file))
                candidates.sort(key=lambda item: candidate_rank(*item))
                for track in tracks:
                    available = []
                    for user, file in candidates:
                        if file_key(user, file) in used_files:
                            continue
                        match = candidate_available(track, user, file, True)
                        if match is None:
                            continue
                        if fallback and not (match.score >= _EXACT or match.named_context):
                            # Artist-wide results hold the artist's other songs: only the
                            # exact title, or this release's own folder, can stand for the row.
                            continue
                        available.append((user, file, match))
                        if match.named_context:
                            filename = file.get("filename", "")
                            browse_candidates.setdefault(
                                directory_key(user, filename),
                                (user, filename[:max(filename.rfind("\\"), filename.rfind("/"))]),
                            )
                    # The release's own folder and a matching duration first, within
                    # the preferred format; a healthy existing transfer before all.
                    available.sort(key=lambda item: (
                        candidate_rank(item[0], item[1])[0], -item[2].evidence, candidate_rank(item[0], item[1])[1:],
                    ))
                    available.sort(key=lambda item: not history.is_protected(item[0], item[1].get("filename", "")))
                    candidates_by_track[track_key(track)] = [(user, file) for user, file, _ in available]

            # A peer can satisfy several separate track queries. Submit these
            # together (queue_matches caps requests at 50 files), retaining the
            # next-best candidates if that peer is definitely unavailable.
            while True:
                matches_by_user = {}
                selected_files = set(used_files)
                for track in remaining_tracks():
                    for user, file in candidates_by_track.get(track_key(track), []):
                        identity = file_key(user, file)
                        if user_key(user) in unavailable_users or identity in selected_files:
                            continue
                        matches_by_user.setdefault(user, []).append((track, file))
                        selected_files.add(identity)
                        break
                if not matches_by_user:
                    break
                for user, matches in matches_by_user.items():
                    queue_matches(user, matches)
            # A release folder that supplied a track may hold the remaining ones.
            browse_release_folders()

    try:
        reuse_history()
        if complete_release_tracks is not None:
            search_album()
        elif search_scope == "track":
            search_tracks()
            search_album()
            search_tracks(fallback=True)
        else:
            search_album()
            search_tracks()
            search_tracks(fallback=True)
    finally:
        # Whatever happens next, slskd has accepted these files for these rows.
        if submissions:
            SubmissionLog().record(submissions)

    # Successful fallback should not surface an obsolete failure to callers.
    queued_row_ids = {id(track) for track in queued_tracks}
    if dry_run:
        queued_row_ids |= {id(requested_by_key[key]) for key in resolved_missing if key in requested_by_key}
    peer_error_ids = {id(error) for error in peer_queue_errors}
    for error in queue_errors:
        if id(error) in peer_error_ids or error.get("stage") == "search":
            error["tracks"] = [track for track in error["tracks"] if id(track) not in queued_row_ids]
    queue_errors = [
        error for error in queue_errors
        if (id(error) not in peer_error_ids and error.get("stage") != "search") or error["tracks"]
    ]
    # Only a failed or stale transfer (not a blocked peer that merely shares the file)
    # means there is no alternative to an earlier download.
    caller_excluded = {source_key(*source) for source in (excluded_sources or ())}

    def real_failures(sources: Set[Tuple[str, str]]) -> Set[Tuple[str, str]]:
        return {
            source for source in sources
            if source in history.failed or source in history.stale or source in caller_excluded
        }

    unrecovered_tracks = [
        requested_by_key[track_key(track)] for track in missing_tracks
        if track_key(track) not in resolved_missing and real_failures(failed_source_tracks.get(track_key(track), set()))
    ]
    if unrecovered_tracks:
        unrecovered_sources = set().union(*(
            real_failures(sources) for key, sources in failed_source_tracks.items()
            if key in requested_by_key and key not in resolved_missing
        ))
        queue_errors.append({
            "stage": "recovery",
            "error": f"No alternative sources found. {history.failure_summary(unrecovered_sources)}",
            "tracks": unrecovered_tracks,
        })

    verb, count = ("Matched", len(matched_tracks)) if dry_run else ("Queued", len(queued_tracks))
    report_progress(f"Completed: {verb} {count} missing track files.")

    return {
        "artist": effective_artist,
        "release": release_title,
        "total_missing": len(missing_tracks),
        "queued_count": len(queued_tracks),
        "matched_count": len(matched_tracks),
        "resolved_count": len(matched_missing),
        "dry_run": dry_run,
        "queued_files": queued_files,
        "enqueued_files": enqueued_files,
        "matched_tracks": matched_tracks,
        "queued_tracks": queued_tracks,
        "queue_errors": queue_errors,
        "complete_release_available": complete_release_available,
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
    excluded_sources: Optional[Set[Tuple[str, str]]] = None,
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
            excluded_sources=excluded_sources,
        )
    except (ValueError, SlskdAPIError) as error:
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
