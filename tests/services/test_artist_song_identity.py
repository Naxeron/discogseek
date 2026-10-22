"""Artist mode tells songs apart by recording, not by title, when it checks the library and slskd."""

import pytest

from discogseek.clients.slskd import SlskdEnqueueError, SlskdPeerUnavailableError
from discogseek.core.transfers import source_key
from discogseek.services.library import LibraryReleaseService
from discogseek.services.soulseek import _copy_key
from tests.helpers.artist_mode import (
    FakeArtistClient,
    add_dir,
    build_index,
    history_file,
    local_track,
    prescan,
    raw_catalog,
    release,
    scraper_for,
)

FIRST = ["Intro", "Dawn Patrol", "Glass Harbor"]
SECOND = ["Intro", "Paper Comets", "Iron Lullaby"]
LONG_AGO = "2020-01-01T00:00:00.0000000Z"


def two_albums():
    return raw_catalog([release("First Light", FIRST, 0), release("Second Wind", SECOND, 1)])


def numbered(titles):
    return [f"{number:02d} {title}" for number, title in enumerate(titles, 1)]


def test_same_titled_songs_of_two_albums_are_both_queued():
    client = FakeArtistClient()
    scraper = scraper_for(two_albums(), client)
    first = add_dir(scraper, "p1", "Music\\Target Artist\\First Light", numbered(FIRST))
    second = add_dir(scraper, "p2", "Music\\Target Artist\\Second Wind", numbered(SECOND))
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert client.calls == [("p1", first), ("p2", second)]


def test_local_song_of_one_album_does_not_cover_the_same_title_on_another(monkeypatch):
    raw = two_albums()
    client = FakeArtistClient()
    scraper = scraper_for(raw, client)
    prescan(scraper, monkeypatch, [local_track(raw, 0, 1)])
    second = add_dir(scraper, "p2", "Music\\Target Artist\\Second Wind", numbered(SECOND))
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert client.calls == [("p2", second)]


@pytest.mark.parametrize("catalogued", [True, False])
def test_downloaded_song_from_another_album_folder_does_not_cover_this_album(catalogued):
    raw = two_albums() if catalogued else raw_catalog([release("Second Wind", SECOND, 1)])
    client = FakeArtistClient(history=[history_file(
        "old", "@@o\\Music\\Target Artist\\2015 - First Light\\Target Artist - 01 - Intro.mp3", "Completed, Succeeded",
    )])
    scraper = scraper_for(raw, client)
    second = add_dir(scraper, "p2", "Music\\Target Artist\\Second Wind", numbered(SECOND))
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert client.calls == [("p2", second)]


@pytest.mark.parametrize("folder", [
    "@@o\\Music\\Target Artist\\2015 - Second Wind [FLAC]",
    "@@o\\Music\\Target Artist\\Singles",
    "@@o\\complete",
])
def test_downloaded_song_from_this_album_or_a_loose_folder_still_counts_as_queued(folder):
    client = FakeArtistClient(history=[history_file(
        "old", f"{folder}\\Target Artist - 02 - Paper Comets.mp3", "Completed, Succeeded",
    )])
    scraper = scraper_for(raw_catalog([release("Second Wind", SECOND, 1)]), client)
    second = add_dir(scraper, "p2", "Music\\Target Artist\\Second Wind", numbered(SECOND))
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert client.calls == [("p2", [second[0], second[2]])]


def test_remastered_edition_in_the_same_release_group_needs_each_song_once():
    titles = [f"Song {word}" for word in ("One", "Two", "Three", "Four", "Five", "Six")]
    raw = raw_catalog([
        release("Go Plastic", titles, 0, group="rg-go-plastic"),
        release("Go Plastic", titles, 1, group="rg-go-plastic",
                recordings=[f"remaster-{number}" for number in range(len(titles))]),
    ])
    client = FakeArtistClient()
    scraper = scraper_for(raw, client)
    files = add_dir(scraper, "peer", "@@p\\Music\\Target Artist - Go Plastic (FLAC)", numbered(titles))
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert client.calls == [("peer", files)]
    assert scraper.unresolved_tracks == []
    assert scraper.unresolved_releases == []


def test_same_basename_from_an_unrelated_album_in_history_does_not_block_queueing():
    titles = ["Intro", "Neon Memory", "Midnight Echo"]
    client = FakeArtistClient(history=[history_file(
        "unrelated", "@@q\\Music\\Other Band\\Other Album\\01 - Intro.flac", "Completed, Succeeded",
    )])
    scraper = scraper_for(raw_catalog([release("Night Signals", titles)]), client)
    files = add_dir(scraper, "peer", "Music\\Target Artist\\Night Signals",
                    [f"{number:02d} - {title}" for number, title in enumerate(titles, 1)])
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert client.calls == [("peer", files)]
    assert scraper.queue_errors == []


@pytest.mark.parametrize("old_filename,blocks", [
    ("Other Artist\\Unrelated Album\\05 Glass.mp3", False),
    ("AnyPeer\\Prisms\\05 Glass.mp3", True),
])
def test_history_copy_counts_only_for_the_same_release_folder(old_filename, blocks):
    client = FakeArtistClient(history=[history_file("other-peer", old_filename, "InProgress")])
    scraper = scraper_for(raw_catalog([release("Prisms", ["Thunder", "Glass"])]), client)
    files = add_dir(scraper, "peer", "Music\\Target Artist\\Prisms", ["04 Thunder", "05 Glass"], "mp3")
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert client.calls == [("peer", files[:1] if blocks else files)]


def test_stale_transfer_from_a_peer_missing_from_search_is_cancelled_when_replaced():
    client = FakeArtistClient(history=[history_file(
        "old-peer", "Music\\Target Artist\\Night Signals\\01 Neon Memory.mp3", "Queued, Remotely",
        transfer_id="stale-1", requestedAt=LONG_AGO, enqueuedAt=LONG_AGO,
    )])
    scraper = scraper_for(raw_catalog([release("Night Signals", ["Neon Memory"])]), client)
    files = add_dir(scraper, "new-peer", "Music\\Target Artist\\Night Signals", ["01 Neon Memory"])
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert client.calls == [("new-peer", files)]
    assert client.cancelled == [("old-peer", "stale-1")]


def test_stale_transfer_is_kept_when_nothing_replaces_it():
    stale_file = "Music\\Target Artist\\Night Signals\\01 Neon Memory.flac"
    client = FakeArtistClient(history=[history_file(
        "stale-peer", stale_file, "Queued, Remotely",
        transfer_id="stale-1", requestedAt=LONG_AGO, enqueuedAt=LONG_AGO,
    )])
    scraper = scraper_for(raw_catalog([
        release("Night Signals", ["Neon Memory"], 0), release("Second Wind", ["Paper Comets"], 1),
    ]), client)
    add_dir(scraper, "stale-peer", "Music\\Target Artist\\Night Signals", ["01 Neon Memory"])
    second = add_dir(scraper, "good-peer", "Music\\Target Artist\\Second Wind", ["01 Paper Comets"])
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert client.calls == [("good-peer", second)]
    assert client.cancelled == []
    assert [entry["title"] for entry in scraper.unresolved_tracks] == ["Neon Memory"]


def test_tracks_refused_by_offline_peers_are_searched_again_without_those_sources(monkeypatch):
    client = FakeArtistClient(failures={"offline": SlskdPeerUnavailableError("offline", "offline")})
    scraper = scraper_for(raw_catalog([release("Night Signals", ["Neon Memory", "Midnight Echo"])]), client)
    files = add_dir(scraper, "offline", "Music\\Target Artist\\Night Signals", ["01 Neon Memory", "02 Midnight Echo"])
    build_index(scraper)
    requests = []

    def download_missing_tracks(self, **kwargs):
        requests.append(kwargs)
        return {"queued_tracks": kwargs["missing_tracks"], "matched_tracks": kwargs["missing_tracks"],
                "enqueued_files": [], "queue_errors": []}

    monkeypatch.setattr(LibraryReleaseService, "download_missing_tracks", download_missing_tracks)
    scraper._reconcile_primary_releases()
    scraper._queue_downloads()
    assert len(scraper.queue_errors) == 1
    assert "2 matched files could not be queued" in scraper.queue_errors[0]

    scraper._search_unfilled_tracks()

    assert [row["title"] for row in requests[0]["missing_tracks"]] == ["Neon Memory", "Midnight Echo"]
    assert requests[0]["excluded_sources"] == {source_key("offline", file["filename"]) for file in files}
    assert scraper.queue_errors == []
    assert scraper.unresolved_tracks == []
    assert scraper.unresolved_releases == []


def test_tracks_of_a_submission_with_an_unknown_outcome_are_not_searched_again(monkeypatch):
    client = FakeArtistClient(failures={"best": SlskdEnqueueError("HTTP 500: Internal database error")})
    scraper = scraper_for(raw_catalog([release("Night Signals", ["Neon Memory"])]), client)
    add_dir(scraper, "best", "Music\\Target Artist\\Night Signals", ["01 Neon Memory"])
    build_index(scraper)
    requests = []
    monkeypatch.setattr(LibraryReleaseService, "download_missing_tracks",
                        lambda self, **kwargs: requests.append(kwargs) or {"queued_tracks": [], "queue_errors": []})

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()
    scraper._search_unfilled_tracks()

    assert requests == []
    assert [(entry["title"], entry["reason"]) for entry in scraper.unresolved_tracks] == [
        ("Neon Memory", "could not be queued"),
    ]
    assert len(scraper.queue_errors) == 1


@pytest.mark.parametrize("folder", [
    "@@o\\Music\\Target Artist\\Loose Tracks",
    "@@o\\Music\\Target Artist\\Target Artist - Best Bits",
])
def test_downloading_song_with_a_distinctive_title_in_a_loose_folder_still_counts_as_queued(folder):
    client = FakeArtistClient(history=[history_file(
        "old", f"{folder}\\Target Artist - Paper Comets.mp3", "InProgress",
    )])
    scraper = scraper_for(raw_catalog([release("Second Wind", SECOND, 1)]), client)
    second = add_dir(scraper, "p2", "Music\\Target Artist\\Second Wind", numbered(SECOND))
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert client.calls == [("p2", [second[0], second[2]])]


@pytest.mark.parametrize("single_first", [True, False])
def test_downloading_album_copy_covers_the_same_recording_on_a_single(single_first):
    titles = ["Opening", "Miracle Mile", "Triton", "Scattergun"]
    album = release("Hyperphantasia", titles, 1, recordings=["r-open", "r-mile", "r-triton", "r-scatter"])
    single = release("Miracle Mile", ["Miracle Mile"], 2, recordings=["r-mile"], primary_type="Single")
    client = FakeArtistClient(history=[history_file(
        "old", "@@o\\Music\\Target Artist\\Hyperphantasia\\02 Miracle Mile.flac", "InProgress",
    )])
    scraper = scraper_for(raw_catalog([single, album] if single_first else [album, single]), client)
    add_dir(scraper, "p2", "Music\\Target Artist\\Hyperphantasia", numbered(titles))
    add_dir(scraper, "p3", "Music\\Target Artist\\Target Artist - Miracle Mile (Single)", ["01 Miracle Mile"])
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert sorted(name.rsplit("\\", 1)[1] for name in client.enqueued_names()) == [
        "01 Opening.flac", "03 Triton.flac", "04 Scattergun.flac",
    ]


def test_self_titled_album_is_not_covered_by_another_albums_copy_of_a_same_titled_song():
    raw = raw_catalog([release("Target Artist", FIRST, 0), release("Second Wind", SECOND, 1)])
    client = FakeArtistClient(history=[history_file(
        "old", "@@o\\Music\\Target Artist\\Second Wind\\01 Intro.flac", "InProgress",
    )])
    scraper = scraper_for(raw, client)
    files = add_dir(scraper, "p1", "Music\\Target Artist\\Target Artist (2010)", numbered(FIRST))
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert client.calls == [("p1", files)]


@pytest.mark.parametrize("filename,key", [
    ("AnyPeer\\Prisms\\05 Glass.mp3", ("prisms", "05 glass.mp3")),
    ("Music\\Other Band\\2019\\01 - Intro.flac", None),
    ("Music\\Other Band\\Unknown Album\\01 - Intro.mp3", None),
    ("Music\\Other Band\\MP3 320\\01 - Intro.mp3", None),
])
def test_only_a_release_folder_identifies_another_copy_of_a_file(filename, key):
    assert _copy_key(filename) == key


def test_song_no_source_supplies_is_not_searched_again_while_an_earlier_download_runs(monkeypatch):
    titles = ["Neon Memory", "Midnight Echo"]
    client = FakeArtistClient(history=[history_file(
        "gone", "@@g\\Music\\Target Artist\\Night Signals\\02 Midnight Echo.flac", "InProgress",
    )])
    scraper = scraper_for(raw_catalog([release("Night Signals", titles)]), client)
    neon = add_dir(scraper, "peer", "Music\\Target Artist\\Night Signals", ["01 Neon Memory"])
    build_index(scraper)
    requests = []
    monkeypatch.setattr(LibraryReleaseService, "download_missing_tracks",
                        lambda self, **kwargs: requests.append(kwargs) or {"queued_tracks": [], "queue_errors": []})

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()
    scraper._search_unfilled_tracks()

    assert client.enqueued_names() == [neon[0]["filename"]]
    assert requests == []
    assert scraper.unresolved_tracks == []


def test_downloading_copy_in_a_folder_named_after_one_side_of_a_double_a_side_counts():
    single = release("Airglow Fires / Begin to Begin", ["Airglow Fires", "Begin to Begin"], 0, primary_type="Single")
    album = release("Reality Testing", ["Airglow Fires", "Aurora Over Eastwood"], 1)
    client = FakeArtistClient(history=[history_file(
        "old", "@@o\\Music\\Target Artist\\Target Artist - Airglow Fires (2013) [FLAC]\\01 Airglow Fires.flac", "InProgress",
    )])
    scraper = scraper_for(raw_catalog([single, album]), client)
    files = add_dir(scraper, "p2", "Music\\Target Artist\\Airglow Fires", ["01 Airglow Fires", "02 Begin to Begin"])
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert client.calls == [("p2", files[1:])]
    # The album's own 'Airglow Fires' recording is another song: still missing.
    assert ("Reality Testing", "Airglow Fires") in {(entry["release"], entry["title"]) for entry in scraper.unresolved_tracks}


def test_downloading_copy_in_the_releases_own_folder_matches_as_closely_as_folders_do():
    titles = ["Riveurs Enjienrd by Brothomstates", "Izey Rael", "Pony Racquetball"]
    client = FakeArtistClient(history=[history_file(
        "old", "@@o\\Music\\Target Artist\\Half the Battle\\05 Target Artist (Riveurs Enjienrd by Brothomstates).flac",
        "InProgress",
    )])
    scraper = scraper_for(raw_catalog([release("Half the Battle", titles)]), client)
    files = add_dir(scraper, "p2", "Music\\Target Artist - Half the Battle", numbered(titles))
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert client.calls == [("p2", files[1:])]


def test_appearance_tracks_already_in_the_library_or_queued_are_not_fetched_again(monkeypatch):
    scraper = scraper_for(raw_catalog([]), FakeArtistClient())
    scraper.local_song_keys = {("rec", "r-local")}

    def audit(release_id, *recordings):
        tracks = [{"title": recording, "status": "missing", "mb_track_id": f"t-{recording}",
                   "mb_recording_id": recording} for recording in recordings]
        return {"title": release_id, "artist": "Various Artists", "mb_release_id": release_id, "tracks": tracks,
                "artist_track_ids": {track["mb_track_id"] for track in tracks}}

    scraper.full_release_audits = [audit("mix-1", "r-local", "r-new"), audit("mix-2", "r-new")]
    requests = []

    def download_missing_tracks(self, **kwargs):
        requests.append((kwargs["release_title"], [track["mb_recording_id"] for track in kwargs["missing_tracks"]],
                         kwargs.get("search_scope", "release")))
        queued = kwargs["missing_tracks"] if kwargs.get("search_scope") == "track" else []
        return {"queued_tracks": queued, "matched_tracks": queued, "enqueued_files": [], "queue_errors": [],
                "resolved_count": len(queued), "complete_release_available": False}

    monkeypatch.setattr(LibraryReleaseService, "download_missing_tracks", download_missing_tracks)
    scraper._download_full_releases()

    assert requests == [
        ("mix-1", ["r-local", "r-new"], "release"),
        ("mix-1", ["r-new"], "track"),
        ("mix-2", ["r-new"], "release"),
    ]
