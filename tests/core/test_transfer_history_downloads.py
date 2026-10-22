"""Release downloads re-source stuck queues and moved-out successes without banning peers."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from discogseek.core.peer_policy import PeerPolicy
from discogseek.services.library_download import download_missing_tracks


GHOST_STORY = (
    "@@hwvfp\\Soulseek Downloads\\complete\\### Filed SLSK DLs 20-4-26\\In Order To Care\\"
    "1.24 - Lone - In Order To Care - Ghost Story.flac"
)
REPLACEMENT = "Music\\Lone\\In Order To Care\\1.24 - Lone - In Order To Care - Ghost Story.flac"
TRACK = {"title": "Ghost Story", "artist": "Lone", "disc_number": 1, "track_number": 24}


def ago(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%S.%f")


def slskd(history, responses):
    client = MagicMock()
    client.get_downloads.return_value = history
    client.search.return_value = {"responses": responses}
    client.batch_search.side_effect = lambda queries, **kwargs: {
        query: {"responses": responses} for query in queries
    }
    operations = []
    client.cancel_download.side_effect = lambda user, transfer_id: (
        # The real client records provenance before stopping a transfer.
        PeerPolicy().record_internal_cancel(user, transfer_id), operations.append(("cancel", user, transfer_id)),
    )
    client.enqueue_download.side_effect = lambda user, files: operations.append(
        ("enqueue", user, [file["filename"] for file in files]),
    )
    return client, operations


def godfather(**fields):
    return [{"username": "godfather", "directories": [{"files": [{
        "id": "t1", "filename": GHOST_STORY, "size": 31689889, "bytesTransferred": 0, "attempts": 1, **fields,
    }]}]}]


@pytest.mark.parametrize("search_scope", ["release", "track"])
def test_stale_remote_queue_is_cancelled_and_replaced(search_scope):
    history = godfather(state="Queued, Remotely", requestedAt=ago(days=3), enqueuedAt=ago(days=3))
    responses = [
        {"username": "godfather", "files": [{"filename": GHOST_STORY, "size": 31689889}]},
        {"username": "peerB", "files": [{"filename": REPLACEMENT, "size": 31689889}]},
    ]
    client, operations = slskd(history, responses)

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "In Order To Care", [TRACK],
                                     search_scope=search_scope)

    # The stale transfer may still start, so it is stopped once its replacement is queued.
    assert operations == [("enqueue", "peerB", [REPLACEMENT]), ("cancel", "godfather", "t1")]
    assert result["queued_tracks"] == [TRACK]
    assert result["queue_errors"] == []
    # slskd then reports the stopped transfer as cancelled: not a peer judgment.
    cancelled = godfather(state="Completed, Cancelled", requestedAt=ago(days=3), enqueuedAt=ago(days=3),
                          endedAt=ago(seconds=1), exception="A task was canceled.")
    assert PeerPolicy().observe(cancelled) == {}
    assert PeerPolicy().blocked_peers() == {}


def test_stale_remote_queue_without_alternative_is_left_queued():
    history = godfather(state="Queued, Remotely", requestedAt=ago(days=12), enqueuedAt=ago(days=12))
    client, operations = slskd(history, [{"username": "godfather", "files": [{"filename": GHOST_STORY}]}])

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "In Order To Care", [TRACK])

    assert operations == []
    assert result["queued_tracks"] == []
    assert "godfather: Queued, Remotely" in result["queue_errors"][0]["error"]
    assert "waiting for 12 days" in result["queue_errors"][0]["error"]


def test_fresh_remote_queue_remains_protected():
    history = godfather(state="Queued, Remotely", requestedAt=ago(hours=1), enqueuedAt=ago(hours=1))
    client, operations = slskd(history, [{"username": "peerB", "files": [{"filename": REPLACEMENT}]}])

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "In Order To Care", [TRACK])

    assert operations == []
    assert result["queued_tracks"] == [TRACK]
    assert result["queued_files"][0]["user"] == "godfather"
    client.search.assert_not_called()


def test_old_success_whose_file_was_imported_or_deleted_is_downloaded_again():
    history = godfather(state="Completed, Succeeded", endedAt=ago(days=12) + "Z", bytesTransferred=31689889)
    client, operations = slskd(history, [{"username": "peerB", "files": [{"filename": REPLACEMENT}]}])

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "In Order To Care", [TRACK])

    assert operations == [("enqueue", "peerB", [REPLACEMENT])]
    assert result["queued_count"] == 1
    assert result["enqueued_files"][0]["user"] == "peerB"


def test_recent_success_awaiting_import_is_not_downloaded_twice():
    history = godfather(state="Completed, Succeeded", endedAt=ago(minutes=10) + "Z", bytesTransferred=31689889)
    client, operations = slskd(history, [{"username": "peerB", "files": [{"filename": REPLACEMENT}]}])

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "In Order To Care", [TRACK])

    assert operations == []
    assert result["queued_files"][0]["user"] == "godfather"
    client.search.assert_not_called()


@pytest.mark.parametrize("state,exception", [
    ("Completed, Rejected", "Transfer rejected: File not shared."),
    ("Completed, Rejected", "Too many files"),
    ("Completed, Aborted", "Transfer aborted: the remote size of 9299071 does not match expected size 9292132"),
])
def test_peer_failing_one_file_still_serves_another(state, exception):
    other = "Music\\Lone\\In Order To Care\\1.23 - Lone - In Order To Care - Other Track.flac"
    history = [{"username": "pavlosh", "directories": [{"files": [{
        "id": "old", "filename": other, "state": state, "exception": exception,
        "endedAt": ago(hours=12) + "Z",
    }]}]}]
    client, operations = slskd(history, [{"username": "pavlosh", "files": [{"filename": REPLACEMENT}]}])

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "In Order To Care", [TRACK])

    assert operations == [("enqueue", "pavlosh", [REPLACEMENT])]
    assert result["queued_tracks"] == [TRACK]


def test_bulk_cancelled_source_is_usable_again():
    # gepa held the complete FLAC album when one slskd event cancelled 52 transfers.
    urban_biology = "torrent\\archive\\Machine Drum - 2002 - Urban Biology [FLAC]\\0{0} - Cream Soda Part {0}.flac"
    files = [{
        "id": f"bulk-{number}", "filename": urban_biology.format(number), "state": "Completed, Cancelled",
        "requestedAt": "2026-10-08T13:24:03.203966", "enqueuedAt": "2026-10-08T13:24:05.1642881",
        "startedAt": f"2026-10-09T14:58:16.4{number}Z", "endedAt": f"2026-10-09T14:58:16.4{number}Z",
        "bytesTransferred": 0, "attempts": 1, "exception": "A task was canceled.",
    } for number in (1, 2, 3)]
    tracks = [{"title": f"Cream Soda Part {number}", "track_number": number} for number in (1, 2)]
    client, operations = slskd([{"username": "gepa", "directories": [{"files": files}]}], [
        {"username": "gepa", "files": [{"filename": file["filename"]} for file in files]},
    ])

    result = download_missing_tracks(client, MagicMock(), "Machine Drum", "Urban Biology", tracks)

    assert operations == [("enqueue", "gepa", [urban_biology.format(1), urban_biology.format(2)])]
    assert result["queued_tracks"] == tracks
    assert PeerPolicy().blocked_peers() == {}
