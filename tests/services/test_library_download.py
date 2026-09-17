"""Missing release tracks, compilation downloads, and placeholder resolution."""

from unittest.mock import MagicMock

import pytest

from discogseek.services.library import LibraryReleaseService
from discogseek.services.library_download import download_missing_tracks


def test_download_missing_tracks_orchestration():
    """Tests Soulseek missing track downloading via folder matching and individual search."""
    mb_mock = MagicMock()
    slsk_mock = MagicMock()
    service = LibraryReleaseService(mb_client=mb_mock, slskd_client=slsk_mock)

    # Peer directory search result containing the missing tracks
    slsk_mock.search.return_value = {
        "responses": [
            {
                "username": "AmbientFan",
                "files": [
                    {"filename": "Music\\Aphex Twin - SAW\\03 Pulsewidth.flac", "size": 25000000},
                    {"filename": "Music\\Aphex Twin - SAW\\04 Ageispolis.flac", "size": 30000000},
                ]
            }
        ]
    }

    missing_tracks = [
        {"title": "Pulsewidth", "track_number": "3"},
        {"title": "Ageispolis", "track_number": "4"}
    ]

    res = service.download_missing_tracks(
        artist="Aphex Twin",
        release_title="Selected Ambient Works 85-92",
        missing_tracks=missing_tracks,
        preferred_format="flac",
        dry_run=False
    )

    assert res["total_missing"] == 2
    assert res["queued_count"] == 2
    assert res["resolved_count"] == 2
    slsk_mock.enqueue_download.assert_called_once()
    args = slsk_mock.enqueue_download.call_args[0]
    assert args[0] == "AmbientFan"
    assert len(args[1]) == 2

def test_download_single_missing_track():
    """Tests downloading a single missing track from Soulseek."""
    mb_mock = MagicMock()
    slsk_mock = MagicMock()
    service = LibraryReleaseService(mb_client=mb_mock, slskd_client=slsk_mock)

    slsk_mock.search.return_value = {
        "responses": [
            {
                "username": "IDM_Collector",
                "files": [
                    {"filename": "\\Shared\\Aphex Twin - Pulsewidth.flac", "size": 24000000, "bitRate": 1411}
                ]
            }
        ]
    }

    res = service.download_single_missing_track(
        artist="Aphex Twin",
        release_title="Selected Ambient Works 85-92",
        track_title="Pulsewidth",
        dry_run=False
    )

    assert res["success"] is True
    assert res["user"] == "IDM_Collector"
    assert "Pulsewidth" in res["filename"]
    slsk_mock.enqueue_download.assert_called_once()

def test_download_missing_tracks_various_artists_orchestration():
    """Verifies that downloading missing tracks for a compilation searches for each track's actual artist rather than the release artist."""
    mb_mock = MagicMock()
    slsk_mock = MagicMock()
    service = LibraryReleaseService(mb_client=mb_mock, slskd_client=slsk_mock)

    # Soulseek batch search responses for individual missing tracks
    slsk_mock.search.return_value = {"responses": []}  # No peer directory match, so goes to Stage 2
    slsk_mock.batch_search.return_value = {
        "Venetian Snares - Amen Terror": {
            "responses": [{
                "username": "BreakcoreHead",
                "files": [{"filename": "Music\\Venetian Snares - Amen Terror.flac", "size": 30000000}]
            }]
        },
        "Bong-Ra - Mashup Core": {
            "responses": [{
                "username": "JungleJunkie",
                "files": [{"filename": "Music\\Bong-Ra - Mashup Core.flac", "size": 28000000}]
            }]
        }
    }

    missing_tracks = [
        {"title": "Amen Terror", "artist": "Venetian Snares", "track_number": "2"},
        {"title": "Mashup Core", "artist": "Bong-Ra", "track_number": "3"},
    ]

    res = service.download_missing_tracks(
        artist="Various Artists",
        release_title="Amen Destroyer",
        missing_tracks=missing_tracks,
        preferred_format="flac",
        dry_run=False
    )

    assert res["total_missing"] == 2
    assert res["queued_count"] == 2
    assert res["resolved_count"] == 2

    # Verify that Soulseek batch_search was called with the individual track artists, NOT "Various Artists" or "Exnoiz"
    slsk_mock.batch_search.assert_called_once()
    queries = slsk_mock.batch_search.call_args[0][0]
    assert "Venetian Snares - Amen Terror" in queries
    assert "Bong-Ra - Mashup Core" in queries
    assert not any("Exnoiz" in q for q in queries)
    assert not any("Various Artists" in q for q in queries)

def test_download_single_missing_track_with_track_artist():
    """Verifies that downloading a single track on a VA release searches for the track artist rather than 'Various Artists'."""
    mb_mock = MagicMock()
    slsk_mock = MagicMock()
    service = LibraryReleaseService(mb_client=mb_mock, slskd_client=slsk_mock)

    slsk_mock.search.return_value = {
        "responses": [{
            "username": "BreakcoreHead",
            "files": [{"filename": "Music\\Venetian Snares - Amen Terror.flac", "size": 30000000}]
        }]
    }

    res = service.download_single_missing_track(
        artist="Various Artists",
        release_title="Amen Destroyer",
        track_title="Amen Terror",
        track_artist="Venetian Snares",
        dry_run=False
    )

    assert res["success"] is True
    assert res["artist"] == "Venetian Snares"
    # Verify search query was for "Venetian Snares Amen Terror", NOT "Various Artists Amen Terror"
    slsk_mock.search.assert_called_once()
    query = slsk_mock.search.call_args[1]["query"]
    assert query == "Venetian Snares Amen Terror"

def test_download_single_missing_track_va_without_track_artist():
    """Verifies that downloading a single track on a VA release without a track artist queries 'Various Artists <Track Title>'."""
    mb_mock = MagicMock()
    slsk_mock = MagicMock()
    service = LibraryReleaseService(mb_client=mb_mock, slskd_client=slsk_mock)

    slsk_mock.search.return_value = {
        "responses": [{
            "username": "CompilationHoarder",
            "files": [{"filename": "Music\\VA - Amen Destroyer\\05 - Hard Track.flac", "size": 30000000}]
        }]
    }

    res = service.download_single_missing_track(
        artist="Various Artists",
        release_title="Amen Destroyer",
        track_title="Hard Track",
        track_artist=None,
        dry_run=False
    )

    assert res["success"] is True
    assert res["artist"] == "Various Artists"
    slsk_mock.search.assert_called_once()
    query = slsk_mock.search.call_args[1]["query"]
    assert query == "Various Artists Hard Track"

def test_download_single_missing_track_resolves_placeholder_title():
    """Verifies that downloading a placeholder 'Track 01 (Missing)' resolves the official title via MusicBrainz."""
    mb_mock = MagicMock()
    slsk_mock = MagicMock()
    service = LibraryReleaseService(mb_client=mb_mock, slskd_client=slsk_mock)

    # MusicBrainz search returns release
    mb_mock.search_release.return_value = [{"id": "mb-rel-1", "title": "propa bo! EP"}]
    mb_mock.get_release_by_id.return_value = {
        "id": "mb-rel-1",
        "title": "propa bo! EP",
        "artist-credit": [{"name": "goreshit"}],
        "medium-list": [{
            "position": 1,
            "track-list": [
                {"number": "1", "recording": {"title": "take you"}},
                {"number": "2", "recording": {"title": "propa bo!"}}
            ]
        }]
    }

    slsk_mock.search.return_value = {
        "responses": [{
            "username": "peer1",
            "files": [{"filename": "goreshit\\propa bo! EP\\01 take you.flac", "size": 20000000}]
        }]
    }

    res = service.download_single_missing_track(
        artist="goreshit",
        release_title="propa bo! EP",
        track_title="Track 01 (Missing)",
        dry_run=True
    )

    assert res["success"] is True
    assert res["track"] == "take you"
    slsk_mock.search.assert_called_once()
    assert slsk_mock.search.call_args[1]["query"] == "goreshit take you"


@pytest.mark.parametrize("individual", [False, True])
def test_download_missing_tracks_preserves_repeated_title_positions(individual):
    client = MagicMock()
    tracks = [
        {"title": "Introduction", "disc_number": 1, "track_number": "1"},
        {"title": "Introduction", "disc_number": 2, "track_number": "1"},
        {"title": "Introduction", "disc_number": 2, "track_number": "3"},
    ]
    responses = [{"username": "peer", "files": [
        {"filename": "Album/CD 2/03 Introduction.flac", "size": 30},
        {"filename": "Album/CD 1/01 Introduction.flac", "size": 10},
        {"filename": "Album/CD 2/01 Introduction.flac", "size": 20},
    ]}]
    client.search.return_value = {"responses": [] if individual else responses}
    client.batch_search.return_value = {"Artist - Introduction": {"responses": responses}}

    result = download_missing_tracks(client, MagicMock(), "Artist", "Album", tracks)

    assert result["matched_count"] == result["queued_count"] == result["resolved_count"] == 3
    assert sorted((t["disc_number"], t["track_number"]) for t in result["queued_files"]) == [
        (1, 1), (2, 1), (2, 3),
    ]
    assert all(any(queued is requested for queued in result["queued_tracks"]) for requested in tracks)
    assert len({file["filename"] for file in result["queued_files"]}) == 3
    if individual:
        client.batch_search.assert_called_once_with(["Artist - Introduction"], timeout=30.0)


@pytest.mark.parametrize("individual", [False, True])
def test_download_missing_tracks_does_not_reuse_unnumbered_file(individual):
    client = MagicMock()
    tracks = [
        {"title": "Introduction", "disc_number": 1, "track_number": 1},
        {"title": "Introduction", "disc_number": 2, "track_number": 1},
    ]
    responses = [{"username": "peer", "files": [{"filename": "Album/Introduction.flac"}]}]
    client.search.return_value = {"responses": [] if individual else responses}
    client.batch_search.return_value = {"Artist - Introduction": {"responses": responses}}

    result = download_missing_tracks(client, MagicMock(), "Artist", "Album", tracks)

    assert result["queued_count"] == 1
    assert result["queued_tracks"] == [tracks[0]]
    client.enqueue_download.assert_called_once()


def test_download_missing_tracks_does_not_treat_numeric_title_as_track_position():
    client = MagicMock()
    tracks = [{"title": "94", "track_number": 3}]
    client.search.return_value = {"responses": [{
        "username": "peer", "files": [{"filename": "Album/94.flac"}],
    }]}

    result = download_missing_tracks(client, MagicMock(), "Artist", "Album", tracks)

    assert result["queued_tracks"] == tracks
    assert result["queued_count"] == 1


@pytest.mark.parametrize("filename, metadata", [
    ("Album/04 Pulsewidth.flac", {}),
    ("Album/CD 2/03 Pulsewidth.flac", {}),
    ("Album/2-03 Pulsewidth.flac", {}),
    ("Album/Pulsewidth.flac", {"track_number": 4}),
    ("Album/Pulsewidth.flac", {"disc_number": 2, "track_number": 3}),
])
def test_download_missing_tracks_rejects_numbering_conflicts(filename, metadata):
    client = MagicMock()
    tracks = [{"title": "Pulsewidth", "disc_number": 1, "track_number": "3/12"}]
    responses = [{"username": "peer", "files": [{"filename": filename, **metadata}]}]
    client.search.return_value = {"responses": responses}
    client.batch_search.return_value = {"Artist - Pulsewidth": {"responses": responses}}

    result = download_missing_tracks(client, MagicMock(), "Artist", "Album", tracks)

    assert result["matched_tracks"] == result["queued_tracks"] == []
    client.enqueue_download.assert_not_called()


@pytest.mark.parametrize("preferred, expected_peer", [("flac", "lossless"), ("mp3-320", "lossy")])
@pytest.mark.parametrize("individual", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_download_missing_tracks_honors_format_independent_of_response_order(preferred, expected_peer, individual, reverse):
    client = MagicMock()
    tracks = [{"title": "Pulsewidth", "track_number": 3}]
    responses = [
        {"username": "lossless", "files": [{"filename": "Album/03 Pulsewidth.flac", "bitRate": 1411}]},
        {"username": "lossy", "files": [{"filename": "Album/03 Pulsewidth.mp3", "bitRate": 320}]},
        {"username": "low-bitrate", "files": [{"filename": "Album/03 Pulsewidth.mp3", "bitRate": 128}]},
    ]
    if reverse:
        responses.reverse()
    client.search.return_value = {"responses": [] if individual else responses}
    client.batch_search.return_value = {"Artist - Pulsewidth": {"responses": responses}}

    result = download_missing_tracks(client, MagicMock(), "Artist", "Album", tracks, preferred_format=preferred)

    assert result["queued_count"] == 1
    assert result["queued_files"][0]["user"] == expected_peer
    assert client.enqueue_download.call_args.args[0] == expected_peer


def test_download_missing_tracks_reports_successful_rows_after_enqueue_failure():
    client = MagicMock()
    tracks = [
        {"title": "Pulsewidth", "track_number": 3},
        {"title": "Ageispolis", "track_number": 4},
    ]
    client.search.return_value = {"responses": [{
        "username": "album-peer", "files": [{"filename": "Album/03 Pulsewidth.flac"}],
    }]}
    client.batch_search.return_value = {"Artist - Ageispolis": {"responses": [{
        "username": "track-peer", "files": [{"filename": "Album/04 Ageispolis.flac"}],
    }]}}
    client.enqueue_download.side_effect = [{"status": "enqueued"}, RuntimeError("peer refused queue")]

    result = download_missing_tracks(client, MagicMock(), "Artist", "Album", tracks)

    assert result["queued_count"] == 1
    assert result["matched_count"] == 2
    assert result["matched_tracks"] == tracks
    assert result["queued_tracks"] == [tracks[0]]
    assert [file["title"] for file in result["queued_files"]] == ["Pulsewidth"]
    assert result["queue_errors"] == [{"user": "track-peer", "error": "peer refused queue", "tracks": [tracks[1]]}]
    # Retrying only failed rows never resubmits the successfully queued item.
    client.enqueue_download.side_effect = None
    client.enqueue_download.reset_mock()
    retried = download_missing_tracks(client, MagicMock(), "Artist", "Album", result["queue_errors"][0]["tracks"])
    assert retried["queued_tracks"] == [tracks[1]]
    client.enqueue_download.assert_called_once()
    assert client.enqueue_download.call_args.args[1][0]["title"] == "Ageispolis"


def test_download_missing_tracks_retains_successes_when_individual_search_fails():
    client = MagicMock()
    tracks = [{"title": "Pulsewidth", "track_number": 3}, {"title": "Ageispolis", "track_number": 4}]
    client.search.return_value = {"responses": [{
        "username": "peer", "files": [{"filename": "Album/03 Pulsewidth.flac"}],
    }]}
    client.batch_search.side_effect = RuntimeError("search disconnected")

    result = download_missing_tracks(client, MagicMock(), "Artist", "Album", tracks)

    assert result["queued_tracks"] == [tracks[0]]
    assert result["queued_count"] == 1
    assert result["queue_errors"] == [{"stage": "search", "error": "search disconnected", "tracks": [tracks[1]]}]


def test_download_missing_tracks_keeps_successful_chunks_when_later_chunk_fails():
    client = MagicMock()
    tracks = [{"title": "Introduction", "track_number": number} for number in range(1, 52)]
    client.search.return_value = {"responses": [{
        "username": "peer", "files": [
            {"filename": f"Album/{number:02d} Introduction.flac"} for number in range(1, 52)
        ],
    }]}
    client.enqueue_download.side_effect = [{"status": "enqueued"}, RuntimeError("second chunk refused")]

    result = download_missing_tracks(client, MagicMock(), "Artist", "Album", tracks)

    assert result["matched_count"] == 51
    assert result["queued_count"] == 50
    assert result["queued_tracks"] == tracks[:50]
    assert result["queue_errors"][0]["tracks"] == tracks[50:]
    assert [len(call.args[1]) for call in client.enqueue_download.call_args_list] == [50, 1]


def test_download_missing_tracks_dry_run_attributes_matches_without_queueing():
    client = MagicMock()
    tracks = [{"title": "Pulsewidth", "track_number": 3}]
    client.search.return_value = {"responses": [{
        "username": "peer", "files": [{"filename": "Album/03 Pulsewidth.flac"}],
    }]}

    result = download_missing_tracks(client, MagicMock(), "Artist", "Album", tracks, dry_run=True)

    assert result["matched_tracks"][0] is tracks[0]
    assert result["matched_count"] == 1
    assert result["queued_tracks"] == []
    assert result["queued_count"] == 0
    assert result["queued_files"][0]["track_number"] == 3
    assert result["queue_errors"] == []
    client.enqueue_download.assert_not_called()


def test_download_missing_tracks_resolves_only_requested_placeholders_and_attributes_original_rows():
    client = MagicMock()
    tracks = [{"title": "Disc 2 Track 01 (Missing)", "status": "missing"}]
    audit = MagicMock(return_value={"tracks": [
        {"title": "Pulsewidth", "track_number": 1, "disc_number": 1, "status": "missing"},
        {"title": "Ageispolis", "track_number": 1, "disc_number": 2, "status": "missing"},
    ]})
    client.search.return_value = {"responses": [{
        "username": "peer", "files": [
            {"filename": "Album/CD1/01 Pulsewidth.flac"},
            {"filename": "Album/CD2/01 Ageispolis.flac"},
        ],
    }]}

    result = download_missing_tracks(client, audit, "Artist", "Album", tracks)

    audit.assert_called_once()
    assert result["total_missing"] == 1
    assert result["queued_count"] == 1
    assert result["queued_tracks"][0] is tracks[0]
    assert result["matched_tracks"][0] is tracks[0]
    assert result["queued_files"][0]["title"] == "Ageispolis"
    assert result["queued_files"][0]["disc_number"] == 2
    assert tracks[0]["title"] == "Disc 2 Track 01 (Missing)"


def test_download_missing_tracks_unresolved_placeholders_never_search():
    client = MagicMock()
    tracks = [{"title": "Track 01 (Missing)", "track_number": 1}]
    audit = MagicMock(return_value={"tracks": tracks})

    with pytest.raises(ValueError, match="audit the release with MusicBrainz first"):
        download_missing_tracks(client, audit, "Artist", "Album", tracks)

    client.search.assert_not_called()
    client.batch_search.assert_not_called()


def test_selected_track_searches_artist_title_first_and_queues_only_that_row():
    client = MagicMock()
    track = {"title": "Pulsewidth", "artist": "Aphex Twin", "disc_number": 2, "track_number": 3, "mb_track_id": "selected-id"}
    client.search.return_value = {"responses": [{"username": "peer", "files": [
        {"filename": "Aphex Twin/Album/CD2/03 Pulsewidth.flac"},
        {"filename": "Aphex Twin/Album/CD2/04 Ageispolis.flac"},
    ]}]}

    result = download_missing_tracks(client, MagicMock(), "Aphex Twin", "Album", [track], search_scope="track")

    client.search.assert_called_once_with(query="Aphex Twin Pulsewidth", timeout=30.0)
    client.batch_search.assert_not_called()
    assert result["queued_tracks"][0] is track
    assert len(client.enqueue_download.call_args.args[1]) == 1
    assert result["queued_files"][0]["disc_number"] == 2


def test_selected_track_falls_back_to_release_after_unusable_track_responses():
    client = MagicMock()
    tracks = [{"title": "Pulsewidth", "track_number": 3}]
    searches = {
        "Artist Pulsewidth": {"responses": [{"username": "locked", "files": [
            {"filename": "Artist/Album/03 Pulsewidth.flac", "isLocked": True},
        ]}]},
        "Artist Album": {"responses": [{"username": "album-peer", "files": [
            {"filename": "Artist/Album/03 Pulsewidth.flac"},
            {"filename": "Artist/Album/04 Ageispolis.flac"},
        ]}]},
    }
    client.search.side_effect = lambda query, **kwargs: searches.get(query, {"responses": []})

    result = download_missing_tracks(client, MagicMock(), "Artist", "Album", tracks, search_scope="track")

    assert client.search.call_args_list[0].kwargs["query"] == "Artist Pulsewidth"
    assert client.search.call_args_list[-1].kwargs["query"] == "Artist Album"
    assert result["queued_tracks"] == tracks
    assert result["queued_count"] == 1


def test_release_search_tries_fallback_after_responses_without_usable_tracks():
    client = MagicMock()
    tracks = [{"title": "Pulsewidth", "track_number": 3}, {"title": "Ageispolis", "track_number": 4}]
    client.search.side_effect = [
        {"responses": [{"username": "wrong-version", "files": [{"filename": "Artist/Album/03 Pulsewidth (Live).flac"}]}]},
        {"responses": [{"username": "album-peer", "files": [
            {"filename": "Artist/Album/03 Pulsewidth.flac"},
            {"filename": "Artist/Album/04 Ageispolis.flac"},
        ]}]},
    ]

    result = download_missing_tracks(client, MagicMock(), "Artist", "Album", tracks)

    assert [call.kwargs["query"] for call in client.search.call_args_list] == ["Artist Album", "Album"]
    assert result["queued_tracks"] == tracks
    client.batch_search.assert_not_called()


def test_release_falls_back_to_individual_queries_when_album_search_fails():
    client = MagicMock()
    tracks = [{"title": "Pulsewidth", "track_number": 3}]
    client.search.side_effect = RuntimeError("album search failed")
    client.batch_search.return_value = {"Artist - Pulsewidth": {"responses": [{
        "username": "track-peer", "files": [{"filename": "Artist/03 Pulsewidth.flac"}],
    }]}}

    result = download_missing_tracks(client, MagicMock(), "Artist", "Album", tracks)

    assert result["queued_tracks"] == tracks
    assert all(error["stage"] == "search" for error in result["queue_errors"])


@pytest.mark.parametrize("scope", ["track", "release"])
def test_track_search_retries_without_separator_after_unusable_responses(scope):
    client = MagicMock()
    tracks = [{"title": "propa bo!", "track_number": 2}]
    good = {"responses": [{"username": "peer", "files": [{"filename": "goreshit/02 propa bo.flac"}]}]}
    def search_result(query):
        if query == "goreshit propa bo":
            return good
        return {"responses": [{"username": "peer", "files": [{"filename": "goreshit/02 propa bo (Live).flac"}]}]}
    client.search.side_effect = lambda query, **kwargs: search_result(query)
    client.batch_search.side_effect = lambda queries, **kwargs: {query: search_result(query) for query in queries}

    result = download_missing_tracks(client, MagicMock(), "goreshit", "Album", tracks, search_scope=scope)

    assert result["queued_tracks"] == tracks
    assert result["queued_files"][0]["filename"].endswith("02 propa bo.flac")


@pytest.mark.parametrize("scope", ["track", "release"])
@pytest.mark.parametrize("title, filename", [
    ("Symphony No. 5", "01 Symphony No. 6.flac"),
    ("Come Together", "01 Other Artist - Come Together.flac"),
    ("Come Together", "01 Come Together (Live).flac"),
])
def test_download_scopes_reject_conflicting_recordings(scope, title, filename):
    client = MagicMock()
    tracks = [{"title": title, "track_number": 1}]
    response = {"responses": [{"username": "peer", "files": [{"filename": "Artist/Album/" + filename}]}]}
    client.search.return_value = response
    client.batch_search.side_effect = lambda queries, **kwargs: {query: response for query in queries}

    result = download_missing_tracks(client, MagicMock(), "Artist", "Album", tracks, search_scope=scope)

    assert result["matched_count"] == result["queued_count"] == 0
    client.enqueue_download.assert_not_called()


@pytest.mark.parametrize("scope", ["track", "release"])
def test_compilation_without_track_artist_uses_release_context_fallback(scope):
    client = MagicMock()
    tracks = [{"title": "Hard Track", "track_number": 5}]
    good = {"responses": [{"username": "peer", "files": [{"filename": "Amen Destroyer/05 Hard Track.flac"}]}]}
    client.search.side_effect = lambda query, **kwargs: good if query == "Amen Destroyer Hard Track" else {"responses": []}
    client.batch_search.side_effect = lambda queries, **kwargs: {
        query: good if query == "Amen Destroyer Hard Track" else {"responses": []} for query in queries
    }

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "Amen Destroyer", tracks, search_scope=scope)

    assert result["queued_tracks"] == tracks


def test_single_track_api_honors_format_preference_and_position():
    client = MagicMock()
    service = LibraryReleaseService(mb_client=MagicMock(), slskd_client=client)
    client.search.return_value = {"responses": [{"username": "peer", "files": [
        {"filename": "Artist/Album/CD1/03 Pulsewidth.flac", "bitRate": 1411},
        {"filename": "Artist/Album/CD2/03 Pulsewidth.flac", "bitRate": 1411},
        {"filename": "Artist/Album/CD2/03 Pulsewidth.mp3", "bitRate": 320},
    ]}]}

    result = service.download_single_missing_track(
        "Artist", "Album", "Pulsewidth", track_number="2-03", preferred_format="mp3-320",
    )

    assert result["success"] is True
    assert result["filename"] == "Artist/Album/CD2/03 Pulsewidth.mp3"


@pytest.mark.parametrize("scope", ["track", "release"])
@pytest.mark.parametrize("filename", ["01 Love - Part One.flac", "01 Artist - Love - Part One.flac"])
def test_download_scopes_preserve_hyphens_in_real_track_title(scope, filename):
    client = MagicMock()
    tracks = [{"title": "Love - Part One", "track_number": 1}]
    client.search.return_value = {"responses": [{"username": "peer", "files": [{"filename": "Album/" + filename}]}]}

    result = download_missing_tracks(client, MagicMock(), "Artist", "Album", tracks, search_scope=scope)

    assert result["queued_tracks"] == tracks


def test_selected_placeholder_uses_resolved_track_artist_and_disc():
    client = MagicMock()
    track = {"title": "Disc 2 Track 01 (Missing)", "status": "missing"}
    audit = MagicMock(return_value={"tracks": [
        {"title": "First Song", "artist": "First Artist", "disc_number": 1, "track_number": 1, "status": "missing"},
        {"title": "Second Song", "artist": "Second Artist", "disc_number": 2, "track_number": 1, "status": "missing"},
    ]})
    client.search.return_value = {"responses": [{"username": "peer", "files": [
        {"filename": "Compilation/CD2/01 Second Artist - Second Song.flac"},
    ]}]}

    result = download_missing_tracks(client, audit, "Various Artists", "Compilation", [track], search_scope="track")

    client.search.assert_called_once_with(query="Second Artist Second Song", timeout=30.0)
    assert result["queued_tracks"][0] is track
    assert result["queued_files"][0]["artist"] == "Second Artist"
    assert result["queued_files"][0]["disc_number"] == 2


@pytest.mark.parametrize("dry_run", [False, True])
def test_progress_counts_actual_matches_across_release_and_track_fallbacks(dry_run):
    client = MagicMock()
    tracks = [{"title": "First Song", "track_number": 1},
              {"title": "Second Song", "track_number": 2},
              {"title": "Last Song", "track_number": 3}]
    client.search.side_effect = [
        {"responses": [{"username": "peer", "files": [{"filename": "Artist/Album/01 First Song.flac"}]}]},
        {"responses": []},
    ]
    client.batch_search.side_effect = lambda queries, **kw: {
        query: {"responses": [{"username": "peer", "files": [{"filename": "Artist/Album/02 Second Song.flac"}]}]}
        for query in queries
    }
    events = []

    result = download_missing_tracks(client, MagicMock(), "Artist", "Album", tracks,
                                     dry_run=dry_run, on_progress=lambda *args: events.append(args))

    searches = [event for event in events if "search" in event[2]]
    assert searches[0] == (0, 3, "Release search 1/2: Artist Album…")
    assert searches[1] == (1, 3, "Release search 2/2: Album…")
    assert searches[2] == (1, 3, "Track search round 1: 2 queries for 2 remaining tracks…")
    assert any(done == 2 and "Track search round 2:" in message for done, _, message in searches)
    assert result["matched_count"] == 2
    assert events[-1] == (2, 3, f"Completed: {'Matched' if dry_run else 'Queued'} 2 missing track files.")
    assert all(total == 3 and 0 <= done <= 2 for done, total, _ in events)


def test_selected_track_progress_reports_match_even_when_queue_fails():
    client = MagicMock()
    client.search.return_value = {"responses": [{"username": "peer", "files": [
        {"filename": "Artist/Album/01 First Song.flac"},
    ]}]}
    client.enqueue_download.side_effect = RuntimeError("Peer disconnected")
    events = []

    result = download_missing_tracks(client, MagicMock(), "Artist", "Album",
                                     [{"title": "First Song", "track_number": 1}],
                                     search_scope="track", on_progress=lambda *args: events.append(args))

    assert events[0] == (0, 1, "Track search round 1: Artist First Song…")
    assert events[-1] == (1, 1, "Completed: Queued 0 missing track files.")
    assert result["matched_count"] == 1 and result["queued_count"] == 0
