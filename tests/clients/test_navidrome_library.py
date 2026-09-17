"""Complete, paginated Navidrome album inventories for library browsing."""

from unittest.mock import MagicMock

import pytest

from discogseek.clients.navidrome import NavidromeScanner


def scanner():
    return NavidromeScanner(base_url="http://navidrome.invalid", username="user", password="pass")


def test_pages_all_albums_then_reads_full_tracks_and_metadata():
    client = scanner()
    client._scan_request = MagicMock(side_effect=[
        {"albumList2": {"album": [{"id": "a", "name": "Alpha"}, {"id": "b", "name": "Beta"}]}},
        {"albumList2": {"album": [{"id": "c", "name": "Gamma"}]}},
        {"album": {
            "id": "a", "name": "Alpha", "artist": "Artist", "musicBrainzId": "release-id",
            "songCount": 2, "song": [
                {"id": "one", "title": "First", "artist": "Performer", "track": 1,
                 "discNumber": 2, "musicBrainzId": "recording-id", "suffix": "flac", "duration": 120},
                {"id": "two", "title": "Second", "track": 2, "discNumber": 2,
                 "path": "Artist/Alpha/CD2/02 Second.mp3", "bitRate": 320},
            ],
        }},
        {"album": {"id": "b", "name": "Beta", "artist": "B", "songCount": 0, "song": []}},
        {"album": {"id": "c", "name": "Gamma", "artist": "C", "songCount": 0, "song": []}},
    ])
    releases = client.scan_library_releases(page_size=2)
    assert len(releases) == 3
    calls = client._scan_request.call_args_list
    assert [call.args[1]["offset"] for call in calls if call.args[0] == "getAlbumList2"] == [0, 2]
    assert [call.args[1]["id"] for call in calls if call.args[0] == "getAlbum"] == ["a", "b", "c"]
    assert releases[0]["mb_release_id"] == "release-id"
    first, second = releases[0]["tracks"]
    assert first["source"] == "navidrome" and first["path"] == ""
    assert first["disc_number"] == 2 and first["track_number"] == "1"
    assert first["mb_rec_ids"] == {"recording-id"}
    assert first["mb_release_ids"] == {"release-id"}
    assert first["artist"] == "Performer" and first["is_lossless"] is True
    assert second["filename"] == "02 Second.mp3" and second["bitrate"] == 320


def test_full_page_requests_next_offset_until_empty():
    client = scanner()
    client._scan_request = MagicMock(side_effect=[
        {"albumList2": {"album": [{"id": "a"}]}},
        {"albumList2": {}},
        {"album": {"id": "a", "name": "Album", "songCount": 0}},
    ])
    assert len(client.scan_library_releases(page_size=1)) == 1
    assert client._scan_request.call_args_list[1].args[1]["offset"] == 1


def test_duplicate_album_ids_on_overlapping_pages_are_read_once():
    client = scanner()
    client._scan_request = MagicMock(side_effect=[
        {"albumList2": {"album": [{"id": "a"}, {"id": "b"}]}},
        {"albumList2": {"album": [{"id": "b"}, {"id": "c"}]}},
        {"albumList2": {}},
        *[{"album": {"id": album_id, "name": album_id, "songCount": 0}} for album_id in ("a", "b", "c")],
    ])
    assert len(client.scan_library_releases(page_size=2)) == 3
    assert sum(call.args[0] == "getAlbum" for call in client._scan_request.call_args_list) == 3


@pytest.mark.parametrize("failure", [
    RuntimeError("Remote timed out"),
    {},
    {"album": {"songCount": 2, "song": [{"id": "one", "title": "First"}]}},
])
def test_album_failures_abort_inventory(failure):
    client = scanner()
    client._scan_request = MagicMock(side_effect=[
        {"albumList2": {"album": [{"id": "a"}]}}, failure,
    ])
    with pytest.raises(RuntimeError):
        client.scan_library_releases()


def test_repeated_page_raises_instead_of_accepting_truncated_inventory():
    client = scanner()
    client._scan_request = MagicMock(return_value={"albumList2": {"album": [{"id": "a"}]}})
    with pytest.raises(RuntimeError, match="no progress"):
        client.scan_library_releases(page_size=1)


def test_compilation_preserves_individual_track_credits():
    client = scanner()
    client._scan_request = MagicMock(side_effect=[
        {"albumList2": {"album": [{"id": "a"}]}},
        {"album": {"name": "Compilation", "artist": "VA", "isCompilation": True, "song": [
            {"id": "one", "title": "First", "artist": "Actual Artist", "track": 1},
        ]}},
    ])
    result = client.scan_library_releases()[0]
    assert result["artist"] == "Various Artists"
    assert result["is_va"] is True
    assert result["tracks"][0]["artist"] == "Actual Artist"


def test_selected_refresh_pages_inventory_but_reads_only_matching_album_details():
    client = scanner()
    summaries = [
        {"id": "unrelated", "name": "Other Album", "artist": "Other Artist"},
        {"id": "existing", "name": "Album", "artist": "Artist", "musicBrainzId": "edition"},
        {"id": "wrong-artist", "name": "Album", "artist": "Someone Else"},
        {"id": "new-copy", "name": "Album", "artist": "Artist"},
        {"id": "wrong-edition", "name": "Album", "artist": "Artist", "musicBrainzId": "other-edition"},
    ]
    client._scan_request = MagicMock(side_effect=[
        {"albumList2": {"album": summaries[:2]}},
        {"albumList2": {"album": summaries[2:4]}},
        {"albumList2": {"album": summaries[4:]}},
        {"album": {"id": "existing", "songCount": 1, "song": [{"id": "one", "title": "First", "track": 1}]}},
        {"album": {"id": "new-copy", "songCount": 1, "song": [{"id": "two", "title": "Second", "track": 2}]}},
    ])
    result = client.scan_library_releases(page_size=2, selected_release={
        "title": "Album", "artist": "Artist", "mb_release_id": "edition", "navidrome_id": "existing",
    })
    calls = client._scan_request.call_args_list
    assert [call.args[1]["offset"] for call in calls if call.args[0] == "getAlbumList2"] == [0, 2, 4]
    assert [call.args[1]["id"] for call in calls if call.args[0] == "getAlbum"] == ["existing", "new-copy"]
    assert [album["navidrome_id"] for album in result] == ["existing", "new-copy"]


def test_selected_refresh_uses_exact_id_remote_id_and_verified_artist_aliases():
    client = scanner()
    summaries = [
        {"id": "by-mbid", "name": "Different title", "artist": "Different credit", "musicBrainzId": "edition"},
        {"id": "by-remote-id", "name": "Renamed", "artist": "Credit"},
        {"id": "by-alias", "name": "Album", "artist": "Contributor"},
    ]
    client._scan_request = MagicMock(side_effect=[
        {"albumList2": {"album": summaries}},
        *[{"album": {**album, "songCount": 0}} for album in summaries],
    ])
    result = client.scan_library_releases(selected_release={
        "title": "Album", "artist": "Various Artists", "mb_release_id": "edition",
        "navidrome_ids": ["by-remote-id"], "browser_alias_keys": [("contributor", "album")],
    })
    assert len(result) == 3


def test_selected_refresh_resolves_missing_summary_credit_before_accepting_album():
    client = scanner()
    client._scan_request = MagicMock(side_effect=[
        {"albumList2": {"album": [{"id": "same-title", "name": "Album"}]}},
        {"album": {"id": "same-title", "name": "Album", "artist": "Another Artist", "songCount": 0}},
    ])
    assert client.scan_library_releases(selected_release={"title": "Album", "artist": "Artist"}) == []
