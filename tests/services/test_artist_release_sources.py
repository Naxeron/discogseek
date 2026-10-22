"""Artist mode fills every missing track of a release from folders, loose files or track searches."""

import random

import pytest

from discogseek.clients.slskd import SlskdClient
from discogseek.services.library import LibraryReleaseService
from tests.helpers.artist_mode import (
    FakeArtistClient,
    add_dir,
    build_index,
    local_track,
    prescan,
    raw_catalog,
    release,
    response,
    run_scraper,
    scraper_for,
)
from tests.helpers.slskd026 import FakeSlskd026, peer

SIGNALS = [f"Signal {word}" for word in (
    "Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel", "India", "Juliet",
)]


def numbered(titles, start=1):
    return [f"{number:02d} {title}" for number, title in enumerate(titles, start)]


def queued_titles(client):
    return sorted(next(title for title in SIGNALS if title in name) for name in client.enqueued_names())


def test_accepted_partial_folder_leaves_its_missing_tracks_to_other_folders():
    client = FakeArtistClient()
    scraper = scraper_for(raw_catalog([release("Phonetic Dreams", SIGNALS)]), client)
    flac = add_dir(scraper, "flacpeer", "Music\\Target Artist - Phonetic Dreams [FLAC]", numbered(SIGNALS[:8]))
    mp3 = add_dir(scraper, "mp3peer", "Music\\Target Artist - Phonetic Dreams", numbered(SIGNALS), "mp3")
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert client.calls == [("flacpeer", flac), ("mp3peer", mp3[8:])]
    assert scraper.queue_errors == []
    assert scraper.unresolved_releases == []


def test_tracks_no_source_supplies_are_reported_per_track():
    client = FakeArtistClient()
    scraper = scraper_for(raw_catalog([release("Phonetic Dreams", SIGNALS)]), client)
    flac = add_dir(scraper, "flacpeer", "Music\\Target Artist - Phonetic Dreams [FLAC]", numbered(SIGNALS[:8]))
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert client.calls == [("flacpeer", flac)]
    assert [(entry["release"], entry["title"], entry["disc"], entry["position"]) for entry in scraper.unresolved_tracks] == [
        ("Phonetic Dreams", "Signal India", 1, "9"), ("Phonetic Dreams", "Signal Juliet", 1, "10"),
    ]
    assert [item["title"] for item in scraper.unresolved_releases] == ["Phonetic Dreams"]


def test_missing_bonus_tracks_come_from_partial_folders_and_loose_files(monkeypatch):
    raw = raw_catalog([release("Phonetic Dreams", SIGNALS)])
    client = FakeArtistClient()
    scraper = scraper_for(raw, client)
    prescan(scraper, monkeypatch, [local_track(raw, 0, number) for number in range(1, 9)])
    add_dir(scraper, "b", "@@b\\Music\\Target Artist - Phonetic Dreams (bonus)", numbered(SIGNALS[8:], 9))
    add_dir(scraper, "a", "@@a\\Music\\Target Artist\\Loose Tracks",
            ["Target Artist - Signal India.flac", "Target Artist - Signal Juliet.flac"])
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert queued_titles(client) == ["Signal India", "Signal Juliet"]
    assert scraper.unresolved_tracks == []
    assert scraper.unresolved_releases == []


def test_multi_disc_release_in_disc_subfolders_is_verified_as_one_copy():
    discs = [SIGNALS[:5], SIGNALS[5:]]
    client = FakeArtistClient()
    scraper = scraper_for(raw_catalog([release("Twin Horizons", discs)]), client)
    root = "Music\\Target Artist - Twin Horizons (2019) [FLAC]"
    first = add_dir(scraper, "peer", f"{root}\\CD1", numbered(discs[0]))
    second = add_dir(scraper, "peer", f"{root}\\CD2", numbered(discs[1]))
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert [(item["matched_count"], item["total_count"]) for item in scraper.verified_releases] == [(10, 10)]
    assert sorted(client.enqueued_names()) == sorted(file["filename"] for file in first + second)
    assert scraper.unresolved_releases == []


def test_multi_disc_repeated_titles_keep_their_disc():
    discs = [["Intro", "Neon Memory"], ["Intro", "Morning Light"]]
    client = FakeArtistClient()
    scraper = scraper_for(raw_catalog([release("Twin Horizons", discs)]), client)
    root = "Music\\Target Artist\\Twin Horizons"
    first = add_dir(scraper, "peer", f"{root}\\Disc 1", numbered(discs[0]))
    second = add_dir(scraper, "peer", f"{root}\\Disc 2", numbered(discs[1]))
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert sorted(client.enqueued_names()) == sorted(file["filename"] for file in first + second)


def test_browsing_a_folder_with_the_real_slskd_reply_verifies_the_release(monkeypatch):
    client = SlskdClient(base_url="http://slskd.test", api_key="key")
    slskd = FakeSlskd026().install(monkeypatch, client)
    folder = "@@abc\\Music\\Target Artist\\Night Signals"
    titles = ["Neon Memory", "Midnight Echo", "Morning Light"]
    for query in ("Target Artist", "Target Artist Night Signals", "Night Signals"):
        slskd.plan_search(query, [peer("peer", f"{folder}\\01 Neon Memory.flac")])
    slskd.add_directory("peer", folder, *(f"{name}.flac" for name in numbered(titles)))
    raw = raw_catalog([release("Night Signals", titles)])
    scraper = scraper_for(raw, client)
    monkeypatch.setattr(client, "get_application", lambda: {"user": {}, "server": {}})
    scraper.mb_client.resolve_artist_mbid.return_value = ("target-artist", "Target Artist")
    scraper.mb_client.fetch_full_discography.return_value = raw
    monkeypatch.setattr("discogseek.services.auditor.AuditorService.scan_library", lambda *a, **k: [])

    result = scraper.run()

    assert [(item["matched_count"], item["total_count"]) for item in result["verified_releases"]] == [(3, 3)]
    assert sorted(file["filename"] for file in result["queued_files"]) == [
        f"{folder}\\{name}.flac" for name in numbered(titles)
    ]
    assert result["unresolved_releases"] == []
    assert slskd.calls("POST", "/api/v0/users/peer/directory")


def test_browse_skips_a_best_folder_that_holds_only_local_tracks(monkeypatch):
    raw = raw_catalog([release("Phonetic Dreams", SIGNALS)])
    flac_dir = "Music\\Target Artist - Phonetic Dreams [FLAC]"
    mp3_dir = "Music\\Target Artist - Phonetic Dreams"
    client = FakeArtistClient(browse={
        ("flacpeer", flac_dir): [{"filename": f"{flac_dir}\\{name}.flac", "size": 1} for name in numbered(SIGNALS[:8])],
        ("mp3peer", mp3_dir): [{"filename": f"{mp3_dir}\\{name}.mp3", "size": 1} for name in numbered(SIGNALS)],
    })
    scraper = scraper_for(raw, client)
    prescan(scraper, monkeypatch, [local_track(raw, 0, number) for number in range(1, 9)])
    add_dir(scraper, "flacpeer", flac_dir, numbered(SIGNALS[:1]), full=False)
    add_dir(scraper, "mp3peer", mp3_dir, numbered(SIGNALS[:1]), "mp3", full=False)
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert [item["user"] for item in scraper.verified_releases] == ["mp3peer"]
    assert client.enqueued_names() == [f"{mp3_dir}\\09 Signal India.mp3", f"{mp3_dir}\\10 Signal Juliet.mp3"]
    assert scraper.unresolved_releases == []


def test_browse_starts_with_the_folder_that_matched_the_most_tracks():
    titles = SIGNALS[:6]
    real = "@@r\\Music\\Target Artist - Phonetic Dreams (FLAC)"
    client = FakeArtistClient(browse={
        ("real", real): [{"filename": f"{real}\\{name}.flac", "size": 1} for name in numbered(titles)],
    })
    scraper = scraper_for(raw_catalog([release("Phonetic Dreams", titles)]), client)
    noise = list(range(40))
    random.Random(7).shuffle(noise)
    for number in noise:
        add_dir(scraper, f"noise{number}", f"@@n{number}\\Mixes\\Phonetic Dreams Tribute {number}",
                ["01 Other Track"], full=False)
    add_dir(scraper, "real", real, numbered(titles[:3]), full=False)
    build_index(scraper)

    scraper._reconcile_primary_releases()

    assert client.browsed[0] == ("real", real)
    assert [item["user"] for item in scraper.verified_releases] == ["real"]


@pytest.mark.parametrize("folder", [
    "@@a\\Music\\Squarepusher\\2004 - [WARPCD117] (FLAC)",
    "@@a\\Music\\Squarepusher\\Ultravisitor (2004)",
])
def test_folder_with_the_complete_tracklist_is_verified_whatever_its_name(folder):
    titles = ["Iambic 9 Poetry", "Steinbolt", "Menelec", "C-Town Smash", "Circlewave", "Tetra-Sync",
              "Andrei", "50 Cycles", "An Arched Pathway", "Telluric Piece"]
    client = FakeArtistClient()
    scraper = scraper_for(raw_catalog([release("Ultravisitor", titles)], aliases=["Squarepusher"]), client)
    add_dir(scraper, "peer", folder, numbered(titles))
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert [item["directory"] for item in scraper.verified_releases] == [folder]
    assert len(client.enqueued_names()) == 10


def test_complete_tracklist_in_another_artists_folder_is_not_verified_by_tracklist_alone():
    titles = ["Iambic 9 Poetry", "Steinbolt", "Menelec", "C-Town Smash", "Circlewave"]
    client = FakeArtistClient()
    scraper = scraper_for(raw_catalog([release("Ultravisitor", titles)]), client)
    add_dir(scraper, "peer", "@@a\\Music\\Someone Else\\2004 - [WARPCD117]", numbered(titles))
    build_index(scraper)

    scraper._reconcile_primary_releases()

    assert scraper.verified_releases == []


def test_track_searches_cover_the_songs_no_discovered_source_supplied(monkeypatch):
    raw = raw_catalog([release("Phonetic Dreams", SIGNALS)])
    client = FakeArtistClient()
    scraper = scraper_for(raw, client)
    prescan(scraper, monkeypatch, [local_track(raw, 0, number) for number in range(1, 9)])
    build_index(scraper)
    requests = []

    def download_missing_tracks(self, **kwargs):
        requests.append(kwargs)
        found = kwargs["missing_tracks"][:1]
        return {"queued_tracks": found, "matched_tracks": found, "resolved_count": 1,
                "enqueued_files": [{"filename": "x\\09 Signal India.flac", "user": "u"}], "queue_errors": []}

    monkeypatch.setattr(LibraryReleaseService, "download_missing_tracks", download_missing_tracks)
    scraper._reconcile_primary_releases()
    scraper._queue_downloads()
    scraper._search_unfilled_tracks()

    assert len(requests) == 1
    assert requests[0]["artist"] == "Target Artist"
    assert requests[0]["release_title"] == "Phonetic Dreams"
    assert requests[0]["search_scope"] == "track"
    assert [
        (row["title"], row["disc_number"], row["track_number"], row["track_position"], row["mb_track_id"])
        for row in requests[0]["missing_tracks"]
    ] == [("Signal India", 1, "9", 9, "trk-0-1-9"), ("Signal Juliet", 1, "10", 10, "trk-0-1-10")]
    assert [entry["title"] for entry in scraper.unresolved_tracks] == ["Signal Juliet"]
    assert scraper.enqueued_count == 1


def test_release_with_nothing_found_is_searched_as_a_release_first(monkeypatch):
    raw = raw_catalog([release("Phonetic Dreams", SIGNALS[:3])])
    scraper = scraper_for(raw, FakeArtistClient())
    build_index(scraper)
    requests = []
    monkeypatch.setattr(LibraryReleaseService, "download_missing_tracks",
                        lambda self, **kwargs: requests.append(kwargs) or {"queued_tracks": [], "queue_errors": []})

    scraper._reconcile_primary_releases()
    scraper._search_unfilled_tracks()

    assert [(request["search_scope"], len(request["missing_tracks"])) for request in requests] == [("release", 3)]
    assert [item["title"] for item in scraper.unresolved_releases] == ["Phonetic Dreams"]
    assert len(scraper.unresolved_tracks) == 3


def test_run_hands_missing_tracks_to_track_searches_and_reports_the_rest(monkeypatch):
    raw = raw_catalog([release("Phonetic Dreams", SIGNALS)])
    album = "@@f\\Music\\Target Artist - Phonetic Dreams [FLAC]"
    loose = "@@l\\Music\\Target Artist\\Loose"

    def responder(query):
        if "Signal India" in query:
            return [response("loose", f"{loose}\\Target Artist - Signal India.mp3")]
        if "Signal" in query:
            return []
        return [response("album", *(f"{album}\\{name}.flac" for name in numbered(SIGNALS[:8])))]

    client = FakeArtistClient(responder=responder)
    scraper = scraper_for(raw, client)

    result = run_scraper(scraper, monkeypatch, raw)

    assert sorted(file["filename"] for file in result["queued_files"]) == sorted(
        [f"{album}\\{name}.flac" for name in numbered(SIGNALS[:8])] + [f"{loose}\\Target Artist - Signal India.mp3"]
    )
    assert [(entry["title"], entry["reason"]) for entry in result["unresolved_tracks"]] == [
        ("Signal Juliet", "no matching file found"),
    ]
    assert [item["title"] for item in result["unresolved_releases"]] == ["Phonetic Dreams"]
    assert any("Signal Juliet" in query for query in client.queries)


def test_browse_finds_the_disc_folders_search_results_did_not_show():
    discs = [SIGNALS[:5], SIGNALS[5:]]
    root = "Music\\Target Artist - Twin Horizons (2019) [FLAC]"
    listings = {
        f"{root}\\CD{number}": [{"filename": f"{root}\\CD{number}\\{name}.flac", "size": 1} for name in numbered(titles)]
        for number, titles in enumerate(discs, 1)
    }
    client = FakeArtistClient(browse={("peer", folder): files for folder, files in listings.items()})
    scraper = scraper_for(raw_catalog([release("Twin Horizons", discs)]), client)
    add_dir(scraper, "peer", f"{root}\\CD1", numbered(discs[0]), full=False)
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert ("peer", f"{root}\\CD2") in client.browsed
    assert [(item["matched_count"], item["total_count"]) for item in scraper.verified_releases] == [(10, 10)]
    assert sorted(client.enqueued_names()) == sorted(
        file["filename"] for files in listings.values() for file in files
    )
    assert scraper.unresolved_tracks == []


def test_bonus_tracks_of_a_differently_titled_edition_belong_to_the_release(monkeypatch):
    standard = [f"Season {number}" for number in range(1, 9)]
    bonus = ["Glass Demo", "Night Version", "Coda"]
    shared = [f"rec-std-{number}" for number in range(len(standard))]
    raw = raw_catalog([
        release("Glass Season", standard, 0, group="rg-glass", recordings=shared),
        release("Glass Season (Expanded)", standard + bonus, 1, group="rg-glass",
                recordings=shared + [f"rec-bonus-{number}" for number in range(len(bonus))]),
    ])
    client = FakeArtistClient()
    scraper = scraper_for(raw, client)
    prescan(scraper, monkeypatch, [local_track(raw, 1, number) for number in range(1, len(standard) + 1)])
    folder = "Music\\Target Artist - Glass Season (Expanded) [FLAC]"
    files = add_dir(scraper, "peer", folder, numbered(standard + bonus))
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert client.calls == [("peer", files[len(standard):])]
    assert scraper.unresolved_tracks == []


def test_standard_edition_folder_leaves_the_expanded_bonus_tracks_unresolved():
    standard = [f"Season {number}" for number in range(1, 9)]
    bonus = ["Glass Demo", "Night Version"]
    shared = [f"rec-std-{number}" for number in range(len(standard))]
    raw = raw_catalog([
        release("Glass Season", standard, 0, group="rg-glass", recordings=shared),
        release("Glass Season (Expanded)", standard + bonus, 1, group="rg-glass",
                recordings=shared + ["rec-bonus-0", "rec-bonus-1"]),
    ])
    client = FakeArtistClient()
    scraper = scraper_for(raw, client)
    files = add_dir(scraper, "peer", "Music\\Target Artist - Glass Season", numbered(standard))
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert client.calls == [("peer", files)]
    assert [(entry["title"], entry["position"]) for entry in scraper.unresolved_tracks] == [
        ("Glass Demo", "9"), ("Night Version", "10"),
    ]


def test_a_folder_the_peer_did_not_list_is_not_browsed_again_for_the_next_release():
    client = FakeArtistClient()
    scraper = scraper_for(raw_catalog([
        release("Night Signals", ["Neon Memory", "Morning Light", "Midnight Echo"], 0),
        release("Night Signals Remixes", ["Neon Memory (Dub)", "Morning Light (Dub)", "Midnight Echo (Dub)"], 1),
    ]), client)
    folder = "Music\\Target Artist\\Night Signals"
    add_dir(scraper, "peer", folder, ["01 Neon Memory"], full=False)
    build_index(scraper)

    scraper._reconcile_primary_releases()

    assert client.browsed == [("peer", folder)]
