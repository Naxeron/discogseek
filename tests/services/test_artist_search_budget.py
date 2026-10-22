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
