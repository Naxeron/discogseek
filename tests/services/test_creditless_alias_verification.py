"""A same-title local copy without any artist credit is verified by positions, titles and lengths.

Untagged peer files that slskd saved as 'downloads/<Album>/02 Title.flac' have no artist;
the scan files them under 'Unknown Artist'. They must not block the selected edition's
refresh (and so every later download) when they are exactly its missing tracks.
"""

from unittest.mock import MagicMock

import pytest

from discogseek.services.library import LibraryReleaseService
from discogseek.services.library_browser import LibraryBrowserService


def track(title, number, artist="Artist", duration=None, recording=None, folder="Album"):
    return {
        "title": title, "artist": artist, "track_number": str(number), "disc_number": 1,
        "source": "local", "status": "found", "duration": duration,
        "path": f"/music/{folder}/{number:02d} {title}.flac", "filename": f"{number:02d} {title}.flac",
        "mb_rec_ids": [recording] if recording else [],
    }


def release(tracks, artist="Artist", release_id="edition-1"):
    return {
        "id": f"local-{artist}", "artist": artist, "album_artist": artist, "title": "Album",
        "mb_release_id": release_id, "source": "local", "tracks": tracks,
        "status": "complete", "is_audited": False, "found_count": len(tracks), "missing_count": 0,
    }


def browser_with(copy):
    mb = MagicMock()
    mb.search_release.return_value = []
    mb.get_release_by_id.return_value = {
        "id": "edition-1", "title": "Album", "artist-credit": [{"name": "Artist"}],
        "medium-list": [{"position": "1", "track-list": [
            {"id": "track-1", "number": "1", "length": "180000", "recording": {"id": "rec-1", "title": "First"}},
            {"id": "track-2", "number": "2", "length": "317000",
             "recording": {"id": "rec-2", "title": "Rene Thomas"}, "title": "René Thomas"},
            {"id": "track-3", "number": "3", "length": "137000", "recording": {"id": "rec-3", "title": "Fine"}},
        ]}],
    }
    service = LibraryReleaseService(mb_client=mb, slskd_client=MagicMock())
    service.scan_library_releases = MagicMock(return_value=[
        release([track("First", 1, duration=180.0, recording="rec-1")]), copy,
    ])
    return LibraryBrowserService(service)


def downloaded(*tracks):
    """The scan's release for untagged files in 'downloads/Album/'."""
    return release(list(tracks), artist="Unknown Artist", release_id=None)


def refreshed(copy):
    browser = browser_with(copy)
    selected = next(row for row in browser.iter_releases() if row.get("mb_release_id") == "edition-1")
    assert selected["missing_count"] == 2
    return browser, selected


def test_untagged_copy_of_another_master_verifies_its_rows():
    # Brownswood Bubblers Four: every peer file is three to four seconds shorter.
    copy = downloaded(track("René Thomas", 2, "Unknown Artist", 313.0, folder="downloads/Album"),
                      track("Fine", 3, "Unknown Artist", 134.0, folder="downloads/Album"))
    browser, selected = refreshed(copy)

    fresh = browser.refresh_release(selected)

    assert [(t["title"], t["status"]) for t in fresh["tracks"]] == [
        ("First", "found"), ("René Thomas", "found"), ("Fine", "found"),
    ]
    assert ("unknown artist", "album") in fresh["browser_alias_keys"]


def test_one_untagged_file_named_by_the_recording_title_verifies_its_row():
    browser, selected = refreshed(downloaded(track("Rene Thomas", 2, "Unknown Artist", 316.0)))

    fresh = browser.refresh_release(selected)

    assert [t["status"] for t in fresh["tracks"]] == ["found", "found", "missing"]


@pytest.mark.parametrize("change", [
    {"track_number": "3"},                 # another position
    {"duration": 280.0},                   # a conflicting length: another version
    {"duration": None},
    {"title": "René Thomas (Live)"},
])
def test_untagged_copy_that_contradicts_the_edition_still_blocks(change):
    copy = downloaded(dict(track("René Thomas", 2, "Unknown Artist", 316.0), **change))
    browser, selected = refreshed(copy)

    with pytest.raises(ValueError, match="same-title local release"):
        browser.refresh_release(selected)


def test_credited_copy_still_needs_lengths_within_two_seconds():
    copy = release([track("René Thomas", 2, duration=313.0), track("Fine", 3, duration=134.0)],
                   artist="ZZ Label (Artist)", release_id=None)
    browser, selected = refreshed(copy)

    with pytest.raises(ValueError, match="same-title local release"):
        browser.refresh_release(selected)
