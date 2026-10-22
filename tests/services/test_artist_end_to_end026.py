"""Artist mode end to end through the real SlskdClient and an in-memory slskd 0.26.

One artist with a partly shared album (folder plus track searches), a two-disc
album in per-disc folders, and a compilation appearance. slskd also holds a
stale queued transfer of one album track and of an unrelated file.
"""

import json
from collections import Counter
from datetime import timedelta
from unittest.mock import Mock

import pytest

from discogseek.cli.main import main
from discogseek.clients.musicbrainz import MusicBrainzClient
from discogseek.clients.slskd import SlskdClient
from discogseek.core.peer_policy import PeerPolicy
from discogseek.services.auditor import AuditorService
from discogseek.services.soulseek import SlskdArtistScraper
from tests.helpers.artist_mode import ARTIST, ARTIST_ID, release
from tests.helpers.slskd026 import FakeResponse, FakeSlskd026, iso, peer

SIGNALS = [f"Signal {word}" for word in (
    "Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel", "India", "Juliet",
)]
DISCS = [["Amber Coast", "Bright Harbor", "Copper Field", "Distant Shore", "Ember Valley"],
         ["Frost Meadow", "Golden Ridge", "Hollow Pine", "Ivory Tower", "Jade River"]]
ALBUM = "@@a\\Music\\Target Artist - Phonetic Dreams [FLAC]"
LOOSE = "@@l\\Music\\Target Artist\\Singles"
TWIN = "@@d\\Music\\Target Artist - Twin Horizons (2019) [FLAC]"
STALE_COPY = "@@s\\Music\\Target Artist - Phonetic Dreams\\03 Signal Charlie.mp3"
STALE_SINGLE = "@@t\\Music\\Target Artist\\Singles\\Target Artist - Signal India.mp3"
UNRELATED_STALE = "@@o\\Music\\Other Artist - Lights\\01 Signal Fire.mp3"
DISCOVERY = ["Target Artist", "Target Artist Phonetic Dreams", "Phonetic Dreams",
             "Target Artist Twin Horizons", "Twin Horizons"]
LOOSE_TITLES = ("Signal India", "Signal Juliet", "Neon Memory")


def numbered(titles, start=1):
    return [f"{number:02d} {title}" for number, title in enumerate(titles, start)]


ALBUM_FILES = [f"{ALBUM}\\{name}.flac" for name in numbered(SIGNALS[:8])]
DISC_FILES = {number: [f"{TWIN}\\CD{number}\\{name}.flac" for name in numbered(titles)]
              for number, titles in enumerate(DISCS, 1)}
LOOSE_FILES = [f"{LOOSE}\\Target Artist - {title}.flac" for title in LOOSE_TITLES]


class FakeSlskd(FakeSlskd026):
    """slskd 0.26 that also stops transfers (DELETE /transfers/downloads/{user}/{id})."""

    def __init__(self):
        super().__init__()
        self.cancelled = []

    def request(self, method, path, **kwargs):
        segments = [part for part in path.split("?")[0].split("/") if part][2:]
        if method == "DELETE" and segments[:2] == ["transfers", "downloads"] and len(segments) == 4:
            with self._lock:
                self.requests.append((method, path, None))
                for transfer in self.transfers:
                    if transfer["username"] == segments[2] and transfer["id"] == segments[3]:
                        transfer["state"] = "Completed, Cancelled"
                        self.cancelled.append((segments[2], segments[3]))
                        return FakeResponse(204)
            return FakeResponse(404, text="Not found")
        return super().request(method, path, **kwargs)

    def add_transfer(self, user, filename, state, age_hours, transfer_id):
        requested = self.wall - timedelta(hours=age_hours)
        self.transfers.append({
            "id": transfer_id, "username": user, "direction": "Download", "filename": filename,
            "size": 1000, "state": state, "requestedAt": iso(requested), "enqueuedAt": iso(requested),
        })

    def searches_started(self):
        return [body["searchText"] for _, _, body in self.calls("POST", "/api/v0/searches")]

    def enqueued(self):
        return [file["filename"] for _, _, body in self.calls("POST", "/api/v0/transfers/downloads/")
                for file in body]


def compilation():
    """'Shared Night Signals' by Various Artists: one Target Artist track among others."""
    def credit(name):
        artist_id = ARTIST_ID if name == ARTIST else name.lower().replace(" ", "-")
        return [{"artist": {"id": artist_id, "name": name}, "name": name}]

    tracks = [("Neon Memory", ARTIST), ("Midnight Echo", "Other Artist"), ("Morning Light", "Third Artist")]
    return {
        "id": "va-1", "title": "Shared Night Signals", "artist-credit": credit("Various Artists"),
        "release-group": {"id": "va-group", "primary-type": "Album", "secondary-types": ["Compilation"]},
        "medium-list": [{"position": "1", "track-list": [
            {"id": f"va-track-{number}", "number": str(number), "position": str(number), "title": title,
             "artist-credit": credit(artist), "recording": {"id": f"va-rec-{number}", "title": title}}
            for number, (title, artist) in enumerate(tracks, 1)
        ]}],
    }


RAW = {
    "artist": {"id": ARTIST_ID, "name": ARTIST},
    "releases_artist": [release("Phonetic Dreams", SIGNALS, 0), release("Twin Horizons", DISCS, 1)],
    "releases_track_artist": [compilation()],
    "recordings": [],
}


@pytest.fixture
def world(monkeypatch):
    client = SlskdClient(base_url="http://slskd.test", api_key="key")
    slskd = FakeSlskd().install(monkeypatch, client)
    monkeypatch.setattr(client, "get_application",
                        lambda: {"user": {"username": "me"}, "server": {"state": "Connected"}})
    # The album folder holds eight of its ten tracks; search results show only some files of each disc.
    for text in DISCOVERY[:3]:
        slskd.plan_search(text, [peer("albumpeer", *ALBUM_FILES)])
    for text in DISCOVERY[3:]:
        slskd.plan_search(text, [peer("discpeer", *DISC_FILES[1][:2], DISC_FILES[2][0])])
    for number, files in DISC_FILES.items():
        slskd.add_directory("discpeer", f"{TWIN}\\CD{number}", *(name.rsplit("\\", 1)[1] for name in files))
    # Loose singles only answer their own track searches.
    for title, filename in zip(LOOSE_TITLES, LOOSE_FILES):
        slskd.plan_search(f"Target Artist {title}", [peer("loosepeer", filename)])
    slskd.add_transfer("stalepeer", STALE_COPY, "Queued, Remotely", 10, "stale-album")
    slskd.add_transfer("stalesingle", STALE_SINGLE, "Queued, Remotely", 10, "stale-single")
    slskd.add_transfer("otherpeer", UNRELATED_STALE, "Queued, Remotely", 10, "stale-other")
    monkeypatch.setattr(AuditorService, "scan_library", Mock(return_value=[]))
    monkeypatch.setattr(MusicBrainzClient, "resolve_artist_mbid", lambda *a, **k: (ARTIST_ID, ARTIST))
    monkeypatch.setattr(MusicBrainzClient, "fetch_full_discography", lambda *a, **k: RAW)
    # The CLI builds its own scraper and client.
    monkeypatch.setattr("discogseek.services.soulseek.SlskdClient", lambda: client)
    return slskd


def run(dry_run=False):
    return SlskdArtistScraper(ARTIST, dry_run=dry_run).run()


def test_artist_run_queues_each_missing_track_once_and_a_rerun_reuses_everything(world):
    first = run()

    assert sorted(world.enqueued()) == sorted(ALBUM_FILES + DISC_FILES[1] + DISC_FILES[2] + LOOSE_FILES)
    # Each replacement stops only the stale transfer of its own song.
    assert sorted(world.cancelled) == [("stalepeer", "stale-album"), ("stalesingle", "stale-single")]
    assert [(item["release"], item["matched_count"], item["total_count"]) for item in first["verified_releases"]] == [
        ("Phonetic Dreams", 8, 10), ("Twin Horizons", 10, 10),
    ]
    assert sorted((download["release"], download["download_scope"], download["queued_count"])
                  for download in first["release_downloads"]) == [
        ("Phonetic Dreams", "missing_tracks", 2), ("Shared Night Signals", "artist_tracks", 1),
    ]
    assert first["enqueued_count"] == len(world.enqueued()) == 21
    assert (first["unresolved_tracks"], first["unresolved_releases"]) == ([], [])
    assert (first["queue_errors"], first["search_errors"]) == ([], [])
    # Search budget: the discovery queries, one compilation search, one search per loose track.
    searches = world.searches_started()
    assert sorted(searches) == sorted(DISCOVERY + ["Shared Night Signals"]
                                      + [f"Target Artist {title}" for title in LOOSE_TITLES])

    second = run()

    # Everything is queued already: nothing is submitted or stopped again, and
    # every search is read back from slskd instead of repeated, including the
    # compilation search that found nothing a moment ago.
    assert len(world.enqueued()) == 21
    assert len(world.cancelled) == 2
    assert world.searches_started()[len(searches):] == []
    assert second["enqueued_count"] == 0
    assert (second["unresolved_tracks"], second["unresolved_releases"], second["queue_errors"]) == ([], [], [])
    # Stopping a stale transfer to replace it is no judgment of its peer.
    assert not {"stalepeer", "stalesingle"} & set(PeerPolicy().blocked_peers())
    assert "stale-other" in {transfer["id"] for transfer in world.transfers if transfer["state"] == "Queued, Remotely"}


def test_dry_run_matches_every_source_without_queueing_or_cancelling(world, capsys):
    assert main(["download", ARTIST, "--dry-run", "--quiet"]) == 0

    output = capsys.readouterr()
    assert world.calls("POST", "/api/v0/transfers/downloads/") == []
    assert world.calls("DELETE") == []
    matched = [line.split(" | ", 1)[1] for line in output.out.splitlines() if line.startswith("MATCH | ")]
    assert sorted(matched) == sorted(ALBUM_FILES + DISC_FILES[1] + DISC_FILES[2] + LOOSE_FILES)
    assert "21 matched files (dry run; no transfers queued)" in output.out
    assert "UNRESOLVED" not in output.out

    # A real run right after the dry run repeats none of its searches, not
    # even the one that found nothing.
    searches = len(world.searches_started())
    result = run()
    assert world.searches_started()[searches:] == []
    assert result["enqueued_count"] == 21


def test_each_query_is_searched_once_and_every_track_left_missing_is_reported(world, capsys):
    for title in LOOSE_TITLES:
        world.plans.pop(f"Target Artist {title}".casefold())

    # The stale single is the only copy of its track left: kept, and reported.
    assert main(["download", ARTIST, "--quiet"]) == 1

    output = capsys.readouterr()
    searches = world.searches_started()
    assert [query for query, count in Counter(query.casefold() for query in searches).items() if count > 1] == []
    assert searches.count("Shared Night Signals") == 1
    assert sorted(world.enqueued()) == sorted(ALBUM_FILES + DISC_FILES[1] + DISC_FILES[2])
    # Nothing replaced the stale single, so it is left alone.
    assert world.cancelled == [("stalepeer", "stale-album")]
    lines = [line for line in output.out.splitlines() if line.startswith("UNRESOLVED")]
    assert lines == [
        "UNRESOLVED | Phonetic Dreams | 1.9 | Signal India (could not be queued)",
        "UNRESOLVED | Phonetic Dreams | 1.10 | Signal Juliet (no matching file found)",
        "UNRESOLVED | Shared Night Signals | 1.1 | Neon Memory (no matching file found)",
    ]
    assert "3 tracks unresolved" in output.out
    assert "No alternative sources found. stalesingle: Queued, Remotely" in output.err


def test_failed_searches_are_reported_apart_from_tracks_nobody_shares(world, capsys):
    # One discovery query and every attempt at one track's search fail on slskd's side.
    world.search_post_errors["twin horizons"] = [(500, "search backend down")]
    world.search_post_errors["target artist signal india"] = [(500, "search backend down")] * 5

    assert main(["download", ARTIST, "--quiet", "--json", "-"]) == 1

    output = capsys.readouterr()
    result = json.loads(output.out)
    assert [error["query"] for error in result["search_errors"]] == ["Twin Horizons"]
    assert "Search failed: Twin Horizons: Failed to initiate search" in output.err
    # Another query still found the two-disc album.
    assert [item["release"] for item in result["verified_releases"]] == ["Phonetic Dreams", "Twin Horizons"]
    assert [(entry["title"], entry["reason"]) for entry in result["unresolved_tracks"]] == [
        ("Signal India", "search failed"),
    ]
    assert "search backend down" in result["unresolved_tracks"][0]["error"]
