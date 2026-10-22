"""A refresh reaches every search of an artist run; release fallbacks see the whole tracklist."""

import pytest

from discogseek.services.library import LibraryReleaseService
from tests.helpers.artist_mode import (
    FakeArtistClient,
    add_dir,
    build_index,
    raw_catalog,
    release,
    scraper_for,
)

REFRESH_OPTIONS = [({}, True), ({"force_refresh": True}, False), ({"refresh_searches": True}, False)]


def record_requests(monkeypatch, *results):
    """Answer LibraryReleaseService.download_missing_tracks from results, in order, recording each call."""
    requests = []
    pending = list(results)

    def download_missing_tracks(self, **kwargs):
        requests.append(kwargs)
        return pending.pop(0) if pending else {"queued_tracks": [], "matched_tracks": [], "queue_errors": []}

    monkeypatch.setattr(LibraryReleaseService, "download_missing_tracks", download_missing_tracks)
    return requests


def shared_release_audit():
    found = {"title": "Morning Light", "status": "found", "mb_track_id": "t1", "mb_recording_id": "r1",
             "disc_number": 1, "track_number": "1"}
    own = {"title": "Neon Memory", "status": "missing", "mb_track_id": "t2", "mb_recording_id": "r2",
           "disc_number": 1, "track_number": "2"}
    other = {"title": "Midnight Echo", "status": "missing", "mb_track_id": "t3", "mb_recording_id": "r3",
             "disc_number": 1, "track_number": "3"}
    extra = {"title": "Bonus Rip", "status": "found", "is_extra": True, "disc_number": 1, "track_number": "4"}
    return {
        "artist": "Various Artists", "title": "Shared Night Signals", "mb_release_id": "shared",
        "tracks": [found, own, other, extra], "artist_track_ids": {"t2"},
    }


@pytest.mark.parametrize("options,use_existing", REFRESH_OPTIONS)
def test_full_release_searches_follow_the_refresh_options(monkeypatch, options, use_existing):
    scraper = scraper_for(raw_catalog([]), FakeArtistClient(), **options)
    audit = shared_release_audit()
    scraper.full_release_audits = [audit]
    requests = record_requests(monkeypatch, {"complete_release_available": False, "queued_tracks": []})

    scraper._download_full_releases()

    assert [request["use_existing"] for request in requests] == [use_existing, use_existing]


def test_artist_tracks_fallback_receives_the_official_tracklist(monkeypatch):
    scraper = scraper_for(raw_catalog([]), FakeArtistClient())
    audit = shared_release_audit()
    found, own, other, extra = audit["tracks"]
    scraper.full_release_audits = [audit]
    requests = record_requests(monkeypatch, {"complete_release_available": False, "queued_tracks": []})

    scraper._download_full_releases()

    fallback = requests[1]
    assert fallback["search_scope"] == "track"
    assert fallback["missing_tracks"] == [own]
    # Found and other artists' tracks are evidence for repeated titles and positions; local extras are not.
    assert fallback["official_tracks"] == [found, own, other]


@pytest.mark.parametrize("options,use_existing", REFRESH_OPTIONS)
def test_track_searches_follow_the_refresh_options(monkeypatch, options, use_existing):
    client = FakeArtistClient()
    scraper = scraper_for(raw_catalog([release("Night Signals", ["Neon Memory", "Midnight Echo"])]), client, **options)
    add_dir(scraper, "peer", "Music\\Target Artist\\Night Signals", ["01 Neon Memory"])
    build_index(scraper)
    requests = record_requests(monkeypatch)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()
    scraper._search_unfilled_tracks()

    assert [row["title"] for row in requests[0]["missing_tracks"]] == ["Midnight Echo"]
    assert requests[0]["use_existing"] is use_existing


def test_track_searches_can_be_turned_off(monkeypatch):
    client = FakeArtistClient()
    scraper = scraper_for(raw_catalog([release("Night Signals", ["Neon Memory", "Midnight Echo"])]), client,
                          search_missing_tracks=False)
    add_dir(scraper, "peer", "Music\\Target Artist\\Night Signals", ["01 Neon Memory"])
    build_index(scraper)
    requests = record_requests(monkeypatch)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()
    scraper._search_unfilled_tracks()

    assert requests == []
    assert [entry["title"] for entry in scraper.unresolved_tracks] == ["Midnight Echo"]
