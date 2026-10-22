"""Builders and an in-memory slskd for artist-mode (SlskdArtistScraper) tests."""

from typing import Any, Dict, Iterable, List, Optional, Sequence
from unittest.mock import Mock

from discogseek.clients.musicbrainz import ArtistCatalog
from discogseek.core.text import normalize_text
from discogseek.services.auditor import AuditorService
from discogseek.services.soulseek import PeerCandidateIndex, SlskdArtistScraper

ARTIST_ID = "target-artist"
ARTIST = "Target Artist"


class FakeArtistClient:
    """The slskd calls SlskdArtistScraper makes, answered from memory."""

    def __init__(self, responses=None, browse=None, history=None, failures=None, responder=None):
        self.responses = responses or []
        # responder(query) -> responses, for answers that depend on the search text.
        self.responder = responder
        self.browse = browse or {}
        self.history = history or []
        self.failures = failures or {}
        self.calls: List[Any] = []
        self.accepted: List[Dict[str, Any]] = []
        self.cancelled: List[Any] = []
        self.browsed: List[Any] = []
        self.search_kwargs: List[Dict[str, Any]] = []
        self.queries: List[str] = []

    def get_application(self):
        return {"user": {"username": "me"}, "server": {"state": "Connected"}}

    def get_queued_filenames(self):
        return {file["filename"] for file in self.accepted}

    def get_downloads(self):
        return self.history + [{"username": "peer", "directories": [{"files": [
            dict(file, state="InProgress") for file in self.accepted
        ]}]}]

    def _answer(self, query):
        return {"responses": self.responder(query) if self.responder else self.responses}

    def batch_search(self, queries, **kwargs):
        self.search_kwargs.append(kwargs)
        self.queries.extend(queries)
        return {query: self._answer(query) for query in queries}

    def search(self, query, **kwargs):
        self.queries.append(query)
        return self._answer(query)

    def browse_directories_batch(self, directories, **kwargs):
        self.browsed.extend(directories)
        return {key: self.browse.get(key, []) for key in directories}

    def enqueue_download(self, user, files):
        self.calls.append((user, list(files)))
        failure = self.failures.get(user)
        if failure is not None:
            self.accepted.extend(getattr(failure, "queued_files", []) or [])
            raise failure
        self.accepted.extend(files)
        return {"status": "enqueued", "files_count": len(files)}

    def cancel_download(self, user, transfer_id):
        self.cancelled.append((user, transfer_id))

    def enqueued_names(self) -> List[str]:
        return [file["filename"] for _, files in self.calls for file in files]


def release(title: str, tracks: Sequence[Any], index: int = 0, group: Optional[str] = None,
            release_id: Optional[str] = None, recordings: Optional[Sequence[Any]] = None,
            artist_credit: Optional[List[Dict[str, Any]]] = None, primary_type: str = "Album") -> Dict[str, Any]:
    """A MusicBrainz release; tracks is a list of titles or a list of discs of titles."""
    discs = tracks if tracks and isinstance(tracks[0], (list, tuple)) else [tracks]
    flat_recordings = list(recordings or [])
    media, absolute = [], 0
    for disc_index, titles in enumerate(discs, 1):
        track_list = []
        for number, track_title in enumerate(titles, 1):
            recording = flat_recordings[absolute] if absolute < len(flat_recordings) else f"rec-{index}-{disc_index}-{number}"
            absolute += 1
            track_list.append({
                "id": f"trk-{index}-{disc_index}-{number}", "number": str(number), "position": str(number),
                "title": track_title, "recording": {"id": recording, "title": track_title},
            })
        media.append({"position": str(disc_index), "track-list": track_list})
    return {
        "id": release_id or f"rel-{index}",
        "title": title,
        "artist-credit": artist_credit or [{"artist": {"id": ARTIST_ID, "name": ARTIST}, "name": ARTIST}],
        "release-group": {"id": group or f"rg-{index}", "primary-type": primary_type},
        "medium-list": media,
    }


def raw_catalog(releases: Iterable[Dict[str, Any]], recordings=(), aliases=()) -> Dict[str, Any]:
    return {
        "artist": {"id": ARTIST_ID, "name": ARTIST,
                   "alias-list": [{"alias": alias, "name": alias} for alias in aliases]},
        "releases_artist": list(releases),
        "releases_track_artist": [],
        "recordings": list(recordings),
    }


def scraper_for(raw: Dict[str, Any], client: Optional[FakeArtistClient] = None, **kwargs) -> SlskdArtistScraper:
    scraper = SlskdArtistScraper(ARTIST, slskd_client=client or FakeArtistClient(), **kwargs)
    scraper.catalog = ArtistCatalog(raw)
    scraper.all_artist_aliases = {normalize_text(ARTIST)} | set(scraper.catalog.aliases)
    scraper.mb_client = Mock()
    return scraper


def add_dir(scraper: SlskdArtistScraper, user: str, directory: str, names: Sequence[str], ext: str = "flac",
            full: bool = True, size: int = 1000, queue: int = 0) -> List[Dict[str, Any]]:
    """A peer folder; full=False leaves it unbrowsed (only search hits known)."""
    files = [{"filename": f"{directory}\\{name}.{ext}" if "." not in name[-5:] else f"{directory}\\{name}",
              "size": size} for name in names]
    scraper.peer_directories[(user, directory)] = {
        "user": user, "directory": directory,
        "matched_search_files": files,
        "full_directory_files": files if full else None,
        "queue": queue, "speed": 5000, "has_slot": True,
    }
    return files


def build_index(scraper: SlskdArtistScraper) -> None:
    scraper.candidate_index = PeerCandidateIndex(scraper.peer_directories)


def local_track(raw: Dict[str, Any], release_index: int, number: int, disc: int = 1) -> Dict[str, Any]:
    """A tagged library file of one catalog track (recording and release IDs set)."""
    metadata = raw["releases_artist"][release_index]
    track = metadata["medium-list"][disc - 1]["track-list"][number - 1]
    filename = f"{number:02d} {track['title']}.flac"
    return {
        "path": f"/music/{ARTIST}/{metadata['title']}/{filename}",
        "filename": filename,
        "title": track["title"],
        "norm_title": normalize_text(track["title"]),
        "album": metadata["title"],
        "norm_album": normalize_text(metadata["title"]),
        "album_artist": ARTIST,
        "artists": [ARTIST],
        "track_number": str(number),
        "disc_number": disc,
        "mb_track_ids": {track["id"]},
        "mb_rec_ids": {track["recording"]["id"]},
        "mb_artist_ids": {ARTIST_ID},
        "mb_release_ids": {metadata["id"]},
        "source": "local",
    }


def prescan(scraper: SlskdArtistScraper, monkeypatch, tracks: Sequence[Dict[str, Any]]) -> None:
    """Run the library prescan against an in-memory inventory."""
    monkeypatch.setattr(AuditorService, "scan_library", Mock(return_value=list(tracks)))
    scraper._prescan_library()


def response(user: str, *filenames: str, size: int = 1000) -> Dict[str, Any]:
    """One peer's search response."""
    return {"username": user, "uploadSpeed": 5000, "queueLength": 0, "hasFreeUploadSlot": True,
            "files": [{"filename": name, "size": size} for name in filenames]}


def run_scraper(scraper: SlskdArtistScraper, monkeypatch, raw: Dict[str, Any],
                library: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
    """scraper.run() with MusicBrainz and the library scan answered from memory."""
    scraper.mb_client.resolve_artist_mbid.return_value = (ARTIST_ID, ARTIST)
    scraper.mb_client.fetch_full_discography.return_value = raw
    monkeypatch.setattr(AuditorService, "scan_library", Mock(return_value=list(library)))
    return scraper.run()


def history_file(user: str, filename: str, state: str, transfer_id: str = "old", **extra) -> Dict[str, Any]:
    return {"username": user, "directories": [{"files": [
        dict({"id": transfer_id, "filename": filename, "state": state, "size": 1000}, **extra),
    ]}]}
