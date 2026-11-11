"""Artist mode fetches a release whole, or only its new songs when most of it is held elsewhere."""

import pytest

from discogseek.core.text import normalize_text
from tests.helpers.artist_mode import (
    ARTIST,
    ARTIST_ID,
    FakeArtistClient,
    add_dir,
    build_index,
    history_file,
    local_track,
    prescan,
    raw_catalog,
    release,
    response,
    run_scraper,
    scraper_for,
)

ALBUM = ["Opening", "Miracle Mile", "Triton", "Scattergun"]
ALBUM_RECORDINGS = ["r-open", "r-mile", "r-triton", "r-scatter"]


def numbered(titles):
    return [f"{number:02d} {title}" for number, title in enumerate(titles, 1)]


def album(index=0):
    return release("Hyperphantasia", ALBUM, index, recordings=ALBUM_RECORDINGS)


def compilation(title, tracks, index, recordings):
    """One of the artist's own compilations (a best-of, a singles collection)."""
    metadata = release(title, tracks, index, recordings=recordings)
    metadata["release-group"]["secondary-type-list"] = ["Compilation"]
    return metadata


def compilation_copy(recording, title):
    """A library file of a song on another release: a various-artists compilation."""
    filename = f"07 {ARTIST} - {title}.flac"
    return {
        "path": f"/music/Various Artists/Night Shift Vol. 2/{filename}", "filename": filename,
        "title": title, "norm_title": normalize_text(title),
        "album": "Night Shift Vol. 2", "norm_album": normalize_text("Night Shift Vol. 2"),
        "album_artist": "Various Artists", "artists": [ARTIST], "track_number": "7", "disc_number": 1,
        "mb_track_ids": {"trk-comp-7"}, "mb_rec_ids": {recording}, "mb_artist_ids": {ARTIST_ID},
        "mb_release_ids": {"rel-comp"}, "source": "local",
    }


def queue(scraper):
    build_index(scraper)
    scraper._reconcile_primary_releases()
    scraper._queue_downloads()


def basenames(client):
    return sorted(name.rsplit("\\", 1)[1] for name in client.enqueued_names())


def folders(client):
    return [(user, sorted(file["filename"].rsplit("\\", 1)[1] for file in files)) for user, files in client.calls]


def test_album_song_held_only_on_a_compilation_is_fetched_with_the_album(monkeypatch):
    raw = raw_catalog([album()])
    client = FakeArtistClient()
    scraper = scraper_for(raw, client)
    prescan(scraper, monkeypatch, [compilation_copy("r-mile", "Miracle Mile")])
    files = add_dir(scraper, "p1", "Music\\Target Artist\\Hyperphantasia", numbered(ALBUM))

    queue(scraper)

    assert client.calls == [("p1", files)]


def test_album_song_held_in_the_album_itself_is_not_fetched_again(monkeypatch):
    raw = raw_catalog([album()])
    client = FakeArtistClient()
    scraper = scraper_for(raw, client)
    prescan(scraper, monkeypatch, [local_track(raw, 0, 2)])
    files = add_dir(scraper, "p1", "Music\\Target Artist\\Hyperphantasia", numbered(ALBUM))

    queue(scraper)

    assert client.calls == [("p1", [files[0], files[2], files[3]])]


def test_album_with_a_hole_is_completed_with_its_own_copy_of_a_song_held_elsewhere(monkeypatch):
    raw = raw_catalog([album()])
    client = FakeArtistClient()
    scraper = scraper_for(raw, client)
    library = [local_track(raw, 0, number) for number in (1, 3, 4)] + [compilation_copy("r-mile", "Miracle Mile")]
    prescan(scraper, monkeypatch, library)
    files = add_dir(scraper, "p1", "Music\\Target Artist\\Hyperphantasia", numbered(ALBUM))

    queue(scraper)

    assert client.calls == [("p1", [files[1]])]


def test_untagged_song_file_named_like_the_album_is_not_the_album(monkeypatch):
    titles = ["Opening", "Hyperphantasia", "Triton"]
    raw = raw_catalog([release("Hyperphantasia", titles, 0, recordings=["r-open", "r-title", "r-triton"])])
    client = FakeArtistClient()
    scraper = scraper_for(raw, client)
    loose = dict(compilation_copy("r-title", "Hyperphantasia"), album="", norm_album="", album_artist="",
                 mb_release_ids=set(), path=f"/music/{ARTIST}/07 {ARTIST} - Hyperphantasia.flac")
    prescan(scraper, monkeypatch, [loose])
    files = add_dir(scraper, "p1", "Music\\Target Artist\\Hyperphantasia", numbered(titles))

    queue(scraper)

    assert client.calls == [("p1", files)]


@pytest.mark.parametrize("single_first", [True, False])
def test_single_taken_from_an_album_adds_nothing_to_it(single_first):
    single = release("Miracle Mile", ["Miracle Mile"], 1, recordings=["r-mile"], primary_type="Single")
    client = FakeArtistClient()
    scraper = scraper_for(raw_catalog([single, album()] if single_first else [album(), single]), client)
    files = add_dir(scraper, "p1", "Music\\Target Artist\\Hyperphantasia", numbered(ALBUM))
    add_dir(scraper, "p2", "Music\\Target Artist\\Target Artist - Miracle Mile (Single)", ["01 Miracle Mile"])

    queue(scraper)

    assert client.calls == [("p1", files)]
    assert scraper.unresolved_tracks == []


def test_singles_collected_on_a_compilation_come_with_the_compilation():
    singles = [
        release(title, [title], index, recordings=[recording], primary_type="Single")
        for index, (title, recording) in enumerate([("Killer Bee", "r-bee"), ("Nexus Town", "r-nexus")], 1)
    ]
    titles = ["Killer Bee", "Nexus Town", "Dust Angel"]
    collection = compilation("The Singles vol.3", titles, 3, ["r-bee", "r-nexus", "r-dust"])
    client = FakeArtistClient()
    scraper = scraper_for(raw_catalog(singles + [collection]), client)
    add_dir(scraper, "p1", "Music\\Target Artist\\2025 - Killer Bee", ["01 Killer Bee"])
    add_dir(scraper, "p2", "Music\\Target Artist\\2025 - Nexus Town", ["01 Nexus Town"])
    files = add_dir(scraper, "p3", "Music\\Target Artist\\The Singles vol.3", numbered(titles))

    queue(scraper)

    assert client.calls == [("p3", files)]


def test_albums_repackaged_in_a_compilation_stay_whole_and_the_compilation_adds_nothing():
    first = ["Dawn Patrol", "Glass Harbor", "Low Tide", "Salt Lines"]
    second = ["Paper Comets", "Iron Lullaby", "Tin Stars", "Ember Road"]
    recordings = [f"r-{number}" for number in range(8)]
    archives = compilation("Archives", first + second, 0, recordings)
    raw = raw_catalog([
        archives, release("First Light", first, 1, recordings=recordings[:4]),
        release("Second Wind", second, 2, recordings=recordings[4:]),
    ])
    client = FakeArtistClient()
    scraper = scraper_for(raw, client)
    add_dir(scraper, "p0", "Music\\Target Artist\\Archives", numbered(first + second))
    one = add_dir(scraper, "p1", "Music\\Target Artist\\First Light", numbered(first))
    two = add_dir(scraper, "p2", "Music\\Target Artist\\Second Wind", numbered(second))

    queue(scraper)

    assert client.calls == [("p1", one), ("p2", two)]


def test_two_song_ep_collected_on_a_compilation_comes_with_the_compilation():
    titles = ["Jumble Cook", "Jumble Cook (Dub)", "Crazy Crabber", "High and Low"]
    recordings = ["r-jumble", "r-dub", "r-crab", "r-high"]
    ep = release("The Jumble Cook", titles[:2], 0, recordings=recordings[:2], primary_type="EP")
    collection = compilation("The Singles vol.5", titles, 1, recordings)
    client = FakeArtistClient()
    scraper = scraper_for(raw_catalog([ep, collection]), client)
    add_dir(scraper, "p1", "Music\\Target Artist\\The Jumble Cook", numbered(titles[:2]))
    files = add_dir(scraper, "p2", "Music\\Target Artist\\The Singles vol.5", numbered(titles))

    queue(scraper)

    assert client.calls == [("p2", files)]


def test_releases_that_share_a_song_each_get_their_own_copy():
    snakepit = release("The Snakepit", ["Snakepit", "Red Terror", "Wired", "Blue Screen"], 0,
                       recordings=["r-snake", "r-red", "r-wired", "r-blue"], primary_type="EP")
    psycho = release("Psychobiotic", ["Blue Screen", "Extra Zone", "Time Leap"], 1,
                     recordings=["r-blue", "r-zone", "r-leap"], primary_type="EP")
    client = FakeArtistClient()
    scraper = scraper_for(raw_catalog([snakepit, psycho]), client)
    one = add_dir(scraper, "p1", "Music\\Target Artist\\The Snakepit",
                  numbered(["Snakepit", "Red Terror", "Wired", "Blue Screen"]))
    two = add_dir(scraper, "p2", "Music\\Target Artist\\Psychobiotic", numbered(["Blue Screen", "Extra Zone", "Time Leap"]))

    queue(scraper)

    assert client.calls == [("p1", one), ("p2", two)]


def test_a_shared_song_is_fetched_again_only_from_the_releases_own_folder():
    snakepit = release("The Snakepit", ["Snakepit", "Red Terror", "Wired", "Blue Screen"], 0,
                       recordings=["r-snake", "r-red", "r-wired", "r-blue"], primary_type="EP")
    psycho = release("Psychobiotic", ["Blue Screen", "Extra Zone", "Time Leap", "Amino Acid"], 1,
                     recordings=["r-blue", "r-zone", "r-leap", "r-amino"], primary_type="EP")
    client = FakeArtistClient()
    scraper = scraper_for(raw_catalog([snakepit, psycho]), client)
    one = add_dir(scraper, "p1", "Music\\Target Artist\\The Snakepit",
                  numbered(["Snakepit", "Red Terror", "Wired", "Blue Screen"]))
    # This peer's copy of the second EP lacks the shared song; a loose copy of it exists.
    two = add_dir(scraper, "p2", "Music\\Target Artist\\Psychobiotic",
                  ["02 Extra Zone", "03 Time Leap", "04 Amino Acid"])
    add_dir(scraper, "p3", "Music\\Target Artist", ["Target Artist - Blue Screen"])

    queue(scraper)

    assert client.calls == [("p1", one), ("p2", two)]
    # The song is held with the other EP: nothing is missing, and no search is owed.
    assert scraper.unresolved_tracks == []
    assert scraper._release_plans[normalize_text("Psychobiotic")]["unfilled"] == set()


def test_release_mostly_held_on_other_releases_only_adds_its_new_songs():
    best_of = compilation("Greatest Phantasms", ALBUM + ["Afterglow"], 0, ALBUM_RECORDINGS + ["r-after"])
    client = FakeArtistClient()
    scraper = scraper_for(raw_catalog([best_of, album(1)]), client)
    files = add_dir(scraper, "p1", "Music\\Target Artist\\Hyperphantasia", numbered(ALBUM))
    best = add_dir(scraper, "p2", "Music\\Target Artist\\Greatest Phantasms", numbered(ALBUM + ["Afterglow"]))

    queue(scraper)

    assert client.calls == [("p1", files), ("p2", [best[4]])]


def test_a_release_that_only_added_its_new_song_is_complete_on_the_next_run(monkeypatch):
    best_of = compilation("Greatest Phantasms", ALBUM + ["Afterglow"], 0, ALBUM_RECORDINGS + ["r-after"])
    raw = raw_catalog([best_of, album(1)])
    client = FakeArtistClient()
    scraper = scraper_for(raw, client)
    library = [local_track(raw, 1, number) for number in range(1, 5)] + [local_track(raw, 0, 5)]
    prescan(scraper, monkeypatch, library)
    add_dir(scraper, "p1", "Music\\Target Artist\\Hyperphantasia", numbered(ALBUM))
    add_dir(scraper, "p2", "Music\\Target Artist\\Greatest Phantasms", numbered(ALBUM + ["Afterglow"]))

    queue(scraper)

    assert client.calls == []
    assert scraper.local_found_releases == {normalize_text("Greatest Phantasms"), normalize_text("Hyperphantasia")}


def test_another_releases_running_download_of_a_shared_song_does_not_stand_for_a_whole_release():
    snakepit = release("The Snakepit", ["Snakepit", "Red Terror", "Wired", "Blue Screen"], 0,
                       recordings=["r-snake", "r-red", "r-wired", "r-blue"], primary_type="EP")
    psycho = release("Psychobiotic", ["Blue Screen", "Extra Zone", "Time Leap"], 1,
                     recordings=["r-blue", "r-zone", "r-leap"], primary_type="EP")
    client = FakeArtistClient(history=[history_file(
        "old", "@@o\\Music\\Target Artist\\The Snakepit\\04 Blue Screen.flac", "InProgress",
    )])
    scraper = scraper_for(raw_catalog([snakepit, psycho]), client)
    one = add_dir(scraper, "p1", "Music\\Target Artist\\The Snakepit",
                  numbered(["Snakepit", "Red Terror", "Wired", "Blue Screen"]))
    two = add_dir(scraper, "p2", "Music\\Target Artist\\Psychobiotic", numbered(["Blue Screen", "Extra Zone", "Time Leap"]))

    queue(scraper)

    # The running transfer is The Snakepit's own copy; Psychobiotic takes its own.
    assert client.calls == [("p1", one[:3]), ("p2", two)]


def test_a_folder_that_names_the_release_supplies_it_before_one_that_only_holds_its_tracks():
    titles = ["Dawn Patrol", "Glass Harbor", "Paper Comets", "Iron Lullaby"]
    raw = raw_catalog([release("First Light", titles, 0)])
    client = FakeArtistClient()
    scraper = scraper_for(raw, client)
    # A catalog-number folder with the whole tracklist and a shorter queue.
    add_dir(scraper, "p1", "Music\\Target Artist\\[TA-001]", numbered(titles), queue=0)
    files = add_dir(scraper, "p2", "Music\\Target Artist\\First Light", numbered(titles), queue=30)

    queue(scraper)

    assert client.calls == [("p2", files)]


def test_short_album_claims_its_songs_before_a_compilation_that_repeats_them():
    titles = ["Drift", "Undertow", "Stillwater"]
    recordings = ["r-drift", "r-under", "r-still"]
    short = release("Tidal", titles, 0, recordings=recordings)
    collection = compilation("Collected Tides", titles[:2] + ["Low Sun", "High Sun"], 1,
                             recordings[:2] + ["r-low", "r-high"])
    client = FakeArtistClient()
    scraper = scraper_for(raw_catalog([collection, short]), client)
    whole = add_dir(scraper, "p1", "Music\\Target Artist\\Tidal", numbered(titles))
    files = add_dir(scraper, "p2", "Music\\Target Artist\\Collected Tides",
                    numbered(titles[:2] + ["Low Sun", "High Sun"]))

    queue(scraper)

    # The album is fetched whole; the compilation, half new, is fetched whole as well.
    assert client.calls == [("p1", whole), ("p2", files)]


@pytest.mark.parametrize("recording", ["r-mile", "va-rec-mile"])
def test_compilation_verified_complete_takes_its_own_copy_of_a_song_just_queued_for_the_album(monkeypatch, recording):
    """Whether or not MusicBrainz lists the compilation's track as the album's recording."""
    credit = lambda name, artist_id: [{"artist": {"id": artist_id, "name": name}, "name": name}]
    raw = raw_catalog([album()])
    raw["releases_track_artist"] = [{
        "id": "va-1", "title": "Night Shift Vol. 2", "artist-credit": credit("Various Artists", "va"),
        "release-group": {"id": "va-group", "primary-type": "Album", "secondary-type-list": ["Compilation"]},
        "medium-list": [{"position": "1", "track-list": [
            {"id": "va-t1", "number": "1", "position": "1", "title": "Miracle Mile",
             "artist-credit": credit(ARTIST, ARTIST_ID), "recording": {"id": recording, "title": "Miracle Mile"}},
            {"id": "va-t2", "number": "2", "position": "2", "title": "Midnight Echo",
             "artist-credit": credit("Other Artist", "other"), "recording": {"id": "va-rec-echo", "title": "Midnight Echo"}},
        ]}],
    }]
    own = [f"@@a\\Music\\Target Artist\\Hyperphantasia\\{name}.flac" for name in numbered(ALBUM)]
    shared = ["@@c\\Music\\VA - Night Shift Vol. 2\\01 Target Artist - Miracle Mile.flac",
              "@@c\\Music\\VA - Night Shift Vol. 2\\02 Other Artist - Midnight Echo.flac"]
    client = FakeArtistClient(responder=lambda query: (
        [response("albumpeer", *own)] if query == ARTIST else [response("vapeer", *shared)] if "Night Shift" in query else []
    ))

    result = run_scraper(scraper_for(raw, client), monkeypatch, raw)

    # The album's copy of the song is downloading; the compilation still comes whole.
    assert client.enqueued_names() == own + shared
    assert result["unresolved_tracks"] == []
