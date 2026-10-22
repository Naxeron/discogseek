"""Fast, complete Navidrome track inventories include songs without albums."""

from unittest.mock import Mock

import pytest

from discogseek.clients.navidrome import NavidromeScanner


def scanner(*responses):
    client = NavidromeScanner(base_url="http://navidrome.invalid", username="user", password="pass")
    client._scan_request = Mock(side_effect=responses)
    return client


def test_track_scan_pages_to_empty_despite_server_cap_and_preserves_raw_songs():
    loose_song = {"id": "one", "artist": "Artist", "title": "Loose song", "duration": 123,
                  "artists": [{"id": "artist", "name": "Artist"}]}
    other_song = {"id": "two", "title": "Other song"}
    client = scanner(
        {"searchResult3": {"song": [loose_song]}},
        {"searchResult3": {"song": [other_song]}},
        {"searchResult3": {}},
    )
    progress = Mock()

    assert client.scan_library_tracks(on_progress=progress) == [loose_song, other_song]
    assert [call.args for call in client._scan_request.call_args_list] == [
        ("search3", {"query": "", "artistCount": 0, "albumCount": 0,
                     "songCount": 1000, "songOffset": offset})
        for offset in (0, 1, 2)
    ]
    assert [call.args[0] for call in progress.call_args_list] == [1, 2]


def test_track_scan_deduplicates_overlapping_pages():
    songs = [{"id": str(index)} for index in range(3)]
    client = scanner(
        {"searchResult3": {"song": songs[:2]}},
        {"searchResult3": {"song": songs[1:]}},
        {"searchResult3": {"song": []}},
    )
    assert client.scan_library_tracks(page_size=2) == songs
    assert [call.args[1]["songOffset"] for call in client._scan_request.call_args_list] == [0, 2, 4]


@pytest.mark.parametrize("response", [
    None, [], {}, {"searchResult3": None}, {"searchResult3": []},
    {"searchResult3": {"song": None}}, {"searchResult3": {"song": {}}},
    {"searchResult3": {"song": [None]}}, {"searchResult3": {"song": [{}]}},
    {"searchResult3": {"song": [{"id": ""}]}},
    {"searchResult3": {"song": [{"id": " "}]}},
    {"searchResult3": {"song": [{"id": []}]}},
])
def test_track_scan_rejects_malformed_pages(response):
    client = scanner(response)
    with pytest.raises(RuntimeError, match="Navidrome scan failed"):
        client.scan_library_tracks()


def test_track_scan_rejects_repeated_page():
    response = {"searchResult3": {"song": [{"id": "one"}]}}
    client = scanner(response, response)
    with pytest.raises(RuntimeError, match="no progress"):
        client.scan_library_tracks()


def test_track_scan_propagates_failure_without_returning_partial_inventory():
    client = scanner(
        {"searchResult3": {"song": [{"id": "one"}]}},
        RuntimeError("Remote unavailable"),
    )
    with pytest.raises(RuntimeError, match="Remote unavailable"):
        client.scan_library_tracks()


@pytest.mark.parametrize("page_size", [0, -1])
def test_track_scan_rejects_invalid_page_size(page_size):
    client = scanner()
    with pytest.raises(ValueError, match="positive"):
        client.scan_library_tracks(page_size=page_size)
    client._scan_request.assert_not_called()
