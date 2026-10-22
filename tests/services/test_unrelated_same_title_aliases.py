"""A generic same-title album by another artist never blocks a verified release."""

from unittest.mock import MagicMock

import pytest

from discogseek.cli.browser import download_release
from discogseek.services.library import LibraryReleaseService
from discogseek.services.library_browser import LibraryBrowserService


def track(title, number, artist, recording=None):
    return {
        "title": title, "artist": artist, "track_number": str(number), "disc_number": 1,
        "source": "local", "status": "found",
        "path": f"/music/{artist}/Remixes/{number:02d} {title}.flac",
        "filename": f"{number:02d} {title}.flac",
        "mb_rec_ids": [recording] if recording else [],
    }


def release(tracks, artist, release_id=None):
    return {
        "id": f"local-{artist}", "artist": artist, "album_artist": artist, "title": "Remixes",
        "mb_release_id": release_id, "source": "local", "tracks": tracks,
        "status": "complete", "is_audited": False, "found_count": len(tracks), "missing_count": 0,
    }


def browser_with(other):
    mb = MagicMock()
    mb.get_release_by_id.return_value = {
        "id": "lone-remixes", "title": "Remixes", "artist-credit": [{"name": "Lone"}],
        "medium-list": [{"position": "1", "track-list": [
            {"id": "t1", "number": "1", "recording": {"id": "rec-1", "title": "Pineapple Crush (remix)"}},
            {"id": "t2", "number": "2", "recording": {"id": "rec-2", "title": "Coreshine (remix)"}},
        ]}],
    }
    mb.search_release.return_value = []
    service = LibraryReleaseService(mb_client=mb, slskd_client=MagicMock())
    service.scan_library_releases = MagicMock(return_value=[
        release([track("Pineapple Crush (remix)", 1, "Lone", recording="rec-1")], "Lone", "lone-remixes"),
        other,
    ])
    return LibraryBrowserService(service)


def test_unrelated_untagged_same_title_release_does_not_block_refresh_or_download():
    browser = browser_with(release([track("Something Else", 1, "Some Other Artist")], "Some Other Artist"))
    rows = {row["artist"]: row for row in browser.iter_releases()}
    selected = rows["Lone"]
    assert selected["missing_count"] == 1
    assert rows["Some Other Artist"]["is_audited"] is False
    mb = browser.release_service.mb_client
    mb.search_release.reset_mock()

    fresh = browser.refresh_release(selected)
    assert fresh["is_audited"] is True
    assert fresh["missing_count"] == 1
    # Repeated download-watch refreshes neither raise nor search MusicBrainz again.
    assert browser.refresh_release(fresh)["missing_count"] == 1
    mb.search_release.assert_not_called()

    browser.release_service.download_missing_tracks = MagicMock(return_value={"total_missing": 1})
    _, result = download_release(browser, selected, library_dir=None)
    assert result == {"total_missing": 1}
    pending = browser.release_service.download_missing_tracks.call_args.kwargs["missing_tracks"]
    assert [t["title"] for t in pending] == ["Coreshine (remix)"]


@pytest.mark.parametrize("artist", ["Lone feat. Guest", "Lone (UK electronic artist)"])
def test_unverifiable_same_title_copy_by_the_same_artist_still_blocks(artist):
    browser = browser_with(release([track("Coreshine (remix)", 2, artist)], artist))
    rows = list(browser.iter_releases())
    selected = next(row for row in rows if row.get("mb_release_id") == "lone-remixes")

    with pytest.raises(ValueError, match="same-title local release"):
        browser.refresh_release(selected)
