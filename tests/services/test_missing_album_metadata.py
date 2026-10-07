"""Loose tracks must not become incomplete releases through placeholder titles."""

from unittest.mock import MagicMock

import pytest

from discogseek.clients.navidrome import NavidromeScanner
from discogseek.core.audio import AudioMetadata
from discogseek.services.library import LibraryReleaseService
from discogseek.services.library_browser import LibraryBrowserService


@pytest.mark.parametrize("album,release_id,expected", [
    (None, None, False),
    ("", None, False),
    (" \t ", None, False),
    ("[Unknown Album]", None, False),
    (" unknown ALBUM ", None, False),
    ("Untitled", None, True),
    ("Unknown Pleasures", None, True),
    ("[Unknown Album]", "edition", True),
    (None, "edition", True),
])
def test_release_inventory_requires_album_identity(tmp_path, album, release_id, expected):
    music = tmp_path / "singles"
    music.mkdir()
    path = music / "01 Loose track.flac"
    path.write_bytes(b"audio")
    mb = MagicMock()
    service = LibraryReleaseService(mb_client=mb, slskd_client=MagicMock())
    service.cache.store_audio_metadata(AudioMetadata(
        path=path, title="Loose track", artist="Artist", album=album or "",
        track_number="1/3", mb_release_ids={release_id} if release_id else set(),
    ))
    assert bool(service.scan_library_releases(library_dir=music)) is expected

    remote = NavidromeScanner(base_url="http://navidrome.invalid", username="user", password="pass")
    remote.test_connection = MagicMock(return_value=True)
    responses = [
        {"albumList2": {"album": [{"id": "a", "name": album}]}},
        {"album": {"id": "a", "name": album, "artist": "Artist", "musicBrainzId": release_id,
                   "song": [{"id": "one", "title": "Loose track", "track": 1}]}},
    ]
    for selected in (None, {"navidrome_id": "a"}):
        remote._scan_request = MagicMock(side_effect=responses)
        assert bool(remote.scan_library_releases(selected_release=selected)) is expected

    mb.search_release.return_value = [{"id": "edition", "title": album}]
    mb.get_release_by_id.return_value = {
        "id": "edition", "title": album,
        "medium-list": [{"position": 1, "track-list": [
            {"number": "A", "recording": {"title": "That Side"}},
            {"number": "B", "recording": {"title": "This Side"}},
        ]}],
    }
    remote._scan_request = MagicMock(side_effect=responses)
    browser = LibraryBrowserService(service, remote)
    assert bool(list(browser.iter_releases(library_dir=music))) is expected

    if not expected:
        # The shared auditor also rejects placeholder searches from direct callers.
        audited = service.audit_release({"artist": "Artist", "title": album, "tracks": []})
        assert audited["is_audited"] is False
        assert audited["tracks"] == []
        mb.search_release.assert_not_called()
        mb.get_release_by_id.assert_not_called()
    elif release_id:
        mb.get_release_by_id.assert_called_with(release_id, force_refresh=False)
        mb.search_release.assert_not_called()
    else:
        mb.search_release.assert_called_with(release_title=album, artist_name="Artist", limit=5)
