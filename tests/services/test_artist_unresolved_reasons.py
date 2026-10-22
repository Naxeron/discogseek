"""Artist mode says why each track is still missing: nothing found, a failed search, or a refused submission."""

from discogseek.clients.slskd import SlskdPeerUnavailableError
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

SIGNALS = ["Signal Alpha", "Signal Bravo", "Signal Charlie"]


def appearance():
    """A Various Artists compilation with one Target Artist track."""
    def credit(name, artist_id):
        return [{"artist": {"id": artist_id, "name": name}, "name": name}]

    return {
        "id": "va-1", "title": "Shared Night Signals", "artist-credit": credit("Various Artists", "va"),
        "release-group": {"id": "va-group", "primary-type": "Album"},
        "medium-list": [{"position": "1", "track-list": [
            {"id": "va-track-1", "number": "A1", "position": "1", "title": "Neon Memory",
             "artist-credit": credit(ARTIST, ARTIST_ID), "recording": {"id": "va-rec-1", "title": "Neon Memory"}},
            {"id": "va-track-2", "number": "A2", "position": "2", "title": "Midnight Echo",
             "artist-credit": credit("Other Artist", "other"),
             "recording": {"id": "va-rec-2", "title": "Midnight Echo"}},
        ]}],
    }


def test_a_track_search_that_could_not_run_is_reported_as_failed(monkeypatch):
    raw = raw_catalog([release("Phonetic Dreams", SIGNALS)])
    client = FakeArtistClient()

    def unavailable():
        raise ConnectionError("slskd went away")

    # Discovery works; the track searches cannot check slskd's transfers first.
    monkeypatch.setattr(client, "get_downloads", unavailable)
    result = run_scraper(scraper_for(raw, client, dry_run=True), monkeypatch, raw)

    assert [(entry["title"], entry["reason"]) for entry in result["unresolved_tracks"]] == [
        (title, "search failed") for title in SIGNALS
    ]
    assert all("slskd went away" in entry["error"] for entry in result["unresolved_tracks"])
    assert any("slskd went away" in error for error in result["queue_errors"])


def test_appearance_track_a_peer_refused_is_reported_per_track(monkeypatch):
    raw = raw_catalog([])
    raw["releases_track_artist"] = [appearance()]
    loose = "@@l\\Music\\Target Artist\\Target Artist - Neon Memory.flac"
    client = FakeArtistClient(
        responder=lambda query: [response("loosepeer", loose)] if "Neon Memory" in query else [],
        failures={"loosepeer": SlskdPeerUnavailableError("loosepeer", "offline")},
    )

    result = run_scraper(scraper_for(raw, client), monkeypatch, raw)

    assert client.enqueued_names() == [loose]
    assert [(entry["release"], entry["title"], entry["disc"], entry["position"], entry["reason"])
            for entry in result["unresolved_tracks"]] == [
        ("Shared Night Signals", "Neon Memory", 1, "A1", "could not be queued"),
    ]
    assert "loosepeer is offline" in result["unresolved_tracks"][0]["error"]
    assert result["unresolved_releases"] == [{"id": "va-1", "title": "Shared Night Signals"}]


def test_another_releases_same_titled_song_does_not_resolve_a_track(monkeypatch):
    first_light = ["Intro", "Neon Memory", "Midnight Echo", "Morning Light"]
    phonetic = ["Intro", "Signal Alpha", "Signal Bravo", "Signal Charlie"]
    raw = raw_catalog([release("First Light", first_light, 0), release("Phonetic Dreams", phonetic, 1)])
    light = [f"@@a\\Music\\Target Artist - First Light\\{number:02d} {title}.flac"
             for number, title in enumerate(first_light, 1)]
    dreams = [f"@@b\\Music\\Target Artist - Phonetic Dreams\\{number:02d} {title}.flac"
              for number, title in enumerate(phonetic, 1)][1:]
    client = FakeArtistClient(responses=[response("apeer", *light), response("bpeer", *dreams)])

    result = run_scraper(scraper_for(raw, client), monkeypatch, raw)

    assert sorted(client.enqueued_names()) == sorted(light + dreams)
    # First Light's Intro, queued a moment ago, is a different song than Phonetic Dreams' Intro:
    # the track search no longer takes another release's 'Intro' without a matching length.
    assert [(entry["release"], entry["title"], entry["reason"])
            for entry in result["unresolved_tracks"]] == [
        ("Phonetic Dreams", "Intro", "no matching file found"),
    ]
    assert [item["title"] for item in result["unresolved_releases"]] == ["Phonetic Dreams"]
