"""An artist run sends each Soulseek search once, and a dry run plans the searches a real run makes."""

from collections import Counter

import pytest

from discogseek.services.soulseek import _RunSearchCache
from tests.helpers.artist_mode import (
    ARTIST,
    ARTIST_ID,
    FakeArtistClient,
    raw_catalog,
    release,
    response,
    run_scraper,
    scraper_for,
)

SIGNALS = [f"Signal {word}" for word in ("Alpha", "Bravo", "Charlie", "Delta", "Echo")]
ALBUM = "@@a\\Music\\Target Artist - Phonetic Dreams [FLAC]"


def numbered(titles, start=1):
    return [f"{number:02d} {title}" for number, title in enumerate(titles, start)]


def found(count):
    return {"responses": [response("peer", f"Music\\{count}.flac")]}


def test_run_search_cache_keeps_empty_and_pinned_searches_and_the_latest_others():
    cache = _RunSearchCache(max_found=2)
    cache.pin("artist", found(0))
    cache["nothing"] = {"responses": []}
    for name in ("one", "two", "three"):
        cache[name] = found(name)
    assert list(cache) == ["artist", "nothing", "two", "three"]

    cache.get("two")
    cache["four"] = found(4)

    assert list(cache) == ["artist", "nothing", "two", "four"]


def test_release_search_reuses_the_discovery_search_of_the_same_text(monkeypatch):
    raw = raw_catalog([release("Phonetic Dreams", SIGNALS)])
    # Discovery finds nothing usable, so the release is searched again as a release.
    client = FakeArtistClient(responses=[response("peer", "@@p\\Music\\Unrelated\\01 Other Song.flac")])

    result = run_scraper(scraper_for(raw, client), monkeypatch, raw)

    assert [(download["release"], download["download_scope"]) for download in result["release_downloads"]] == [
        ("Phonetic Dreams", "missing_tracks"),
    ]
    repeated = [query for query, count in Counter(query.casefold() for query in client.queries).items() if count > 1]
    assert repeated == []
    assert len(result["unresolved_tracks"]) == len(SIGNALS)


def compilation_with(recording):
    """A Various Artists compilation repeating one album recording."""
    other = [{"artist": {"id": "other", "name": "Other Artist"}, "name": "Other Artist"}]
    own = [{"artist": {"id": ARTIST_ID, "name": ARTIST}, "name": ARTIST}]
    return {
        "id": "va-1", "title": "Shared Night Signals",
        "artist-credit": [{"artist": {"id": "va", "name": "Various Artists"}, "name": "Various Artists"}],
        "release-group": {"id": "va-group", "primary-type": "Album"},
        "medium-list": [{"position": "1", "track-list": [
            {"id": "va-track-1", "number": "1", "position": "1", "title": "Signal Alpha",
             "artist-credit": own, "recording": {"id": recording, "title": "Signal Alpha"}},
            {"id": "va-track-2", "number": "2", "position": "2", "title": "Midnight Echo",
             "artist-credit": other, "recording": {"id": "va-rec-2", "title": "Midnight Echo"}},
        ]}],
    }


@pytest.mark.parametrize("dry_run", [False, True])
def test_album_recording_on_a_compilation_is_not_searched_again(monkeypatch, dry_run):
    raw = raw_catalog([release("Phonetic Dreams", SIGNALS)])
    raw["releases_track_artist"] = [compilation_with("rec-0-1-1")]
    album = [f"{ALBUM}\\{name}.flac" for name in numbered(SIGNALS)]
    client = FakeArtistClient(responder=lambda query: (
        [response("albumpeer", *album)] if "Phonetic Dreams" in query or query == ARTIST else []
    ))

    result = run_scraper(scraper_for(raw, client, dry_run=dry_run), monkeypatch, raw)

    # The album folder supplies Signal Alpha, so the compilation adds no search for it.
    assert not any("Signal Alpha" in query for query in client.queries)
    assert [download["release"] for download in result["release_downloads"]] == []
    assert sorted(file["filename"] for file in result["matched_files"]) == sorted(album)
    assert result["unresolved_tracks"] == []
    if dry_run:
        assert client.calls == []
    else:
        assert sorted(client.enqueued_names()) == sorted(album)


def test_first_searches_of_several_appearances_run_together(monkeypatch):
    raw = raw_catalog([])
    second = compilation_with("va-rec-b")
    second.update(id="va-2", title="Second Night Signals")
    second["release-group"] = {"id": "va-group-2", "primary-type": "Album"}
    for number, track in enumerate(second["medium-list"][0]["track-list"], 1):
        track["id"] = f"va-b-track-{number}"
    raw["releases_track_artist"] = [compilation_with("va-rec-a"), second]
    client = FakeArtistClient()
    batches = []
    batch_search = client.batch_search
    client.batch_search = lambda queries, **kwargs: batches.append(list(queries)) or batch_search(queries, **kwargs)

    run_scraper(scraper_for(raw, client, dry_run=True), monkeypatch, raw)

    # slskd runs two searches at a time: the releases' first searches share them.
    assert ["Shared Night Signals", "Second Night Signals"] in batches
    assert Counter(client.queries)["Shared Night Signals"] == 1
    assert Counter(client.queries)["Second Night Signals"] == 1


def test_appearance_track_the_artist_search_showed_in_its_release_folder_needs_no_search(monkeypatch):
    raw = raw_catalog([])
    raw["releases_track_artist"] = [compilation_with("va-rec-a")]
    copy = "@@c\\Music\\VA - Shared Night Signals (2020)\\01 Target Artist - Signal Alpha.flac"
    client = FakeArtistClient(responder=lambda query: [response("peer", copy)] if query == ARTIST else [])

    result = run_scraper(scraper_for(raw, client, dry_run=True), monkeypatch, raw)

    # The artist's name and the compilation as a whole; nothing for the track itself.
    assert client.queries == [ARTIST, "Shared Night Signals"]
    assert [file["filename"] for download in result["release_downloads"] for file in download["queued_files"]] == [copy]


def test_loose_copy_from_the_artist_search_waits_for_the_tracks_own_search(monkeypatch):
    raw = raw_catalog([])
    raw["releases_track_artist"] = [compilation_with("va-rec-a")]
    loose = "@@l\\Music\\Target Artist\\Target Artist - Signal Alpha.mp3"
    copy = "@@c\\Music\\VA - Shared Night Signals (2020)\\01 Target Artist - Signal Alpha.flac"
    client = FakeArtistClient(responder=lambda query: (
        [response("loose", loose)] if query == ARTIST
        else [response("peer", copy)] if query == "Target Artist Signal Alpha" else []
    ))

    result = run_scraper(scraper_for(raw, client, dry_run=True), monkeypatch, raw)

    assert "Target Artist Signal Alpha" in client.queries
    assert [file["filename"] for download in result["release_downloads"] for file in download["queued_files"]] == [copy]


def test_joint_credit_track_the_artist_search_showed_in_its_release_folder_needs_no_search(monkeypatch):
    # 'Other Artist with Target Artist' is searched as Other Artist; the artist search already
    # showed the file in the album's folder.
    album = compilation_with("va-rec-a")
    album["artist-credit"] = [{"artist": {"id": "other", "name": "Other Artist"}, "name": "Other Artist"}]
    album["medium-list"][0]["track-list"][0]["artist-credit"] = [
        {"artist": {"id": "other", "name": "Other Artist"}, "name": "Other Artist", "joinphrase": " with "},
        {"artist": {"id": ARTIST_ID, "name": ARTIST}, "name": ARTIST},
    ]
    raw = raw_catalog([])
    raw["releases_track_artist"] = [album]
    copy = "@@c\\Music\\Other Artist - Shared Night Signals\\01 - Signal Alpha (with Target Artist).mp3"
    client = FakeArtistClient(responder=lambda query: [response("peer", copy)] if query == ARTIST else [])

    result = run_scraper(scraper_for(raw, client, dry_run=True), monkeypatch, raw)

    # The artist's name and the album as a whole; nothing for the track itself.
    assert client.queries == [ARTIST, "Other Artist Shared Night Signals", "Shared Night Signals"]
    assert [file["filename"] for download in result["release_downloads"] for file in download["queued_files"]] == [copy]
