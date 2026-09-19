"""slskd search polling and download payload deduplication."""

from unittest.mock import MagicMock, call

import pytest

from discogseek.clients.slskd import (
    SlskdAPIError,
    SlskdClient,
    SlskdEnqueueError,
    SlskdPeerUnavailableError,
)


def enqueue_response(status_code, text=""):
    return MagicMock(status_code=status_code, text=text)


@pytest.fixture
def enqueue_client(monkeypatch):
    client = SlskdClient(base_url="http://mock:5030", api_key="dummy_key")
    request = MagicMock()
    sleep = MagicMock()
    monkeypatch.setattr(client, "_request", request)
    monkeypatch.setattr("discogseek.clients.slskd.time.sleep", sleep)
    return client, request, sleep


def test_slskd_enqueue_offline_peer_fails_without_retry(enqueue_client):
    client, request, sleep = enqueue_client
    request.return_value = enqueue_response(500, '"User quobol appears to be offline"')

    with pytest.raises(SlskdPeerUnavailableError) as raised:
        client.enqueue_download("quobol (FLAC)", [{"filename": "Album\\track.flac"}])

    assert raised.value.username == "quobol"
    assert raised.value.reason == "offline"
    assert raised.value.queued_files == []
    assert str(raised.value) == "Soulseek user quobol is offline."
    request.assert_called_once()
    sleep.assert_not_called()


@pytest.mark.parametrize("message", [
    '"Failed to connect to user rhapsodyarchive: Failed to establish a direct or indirect message connection to rhapsodyarchive (88.90.183.22:50300)"',
    "Failed to establish a direct or indirect message connection to rhapsodyarchive",
])
def test_slskd_enqueue_unreachable_peer_retries_then_succeeds(enqueue_client, message):
    client, request, sleep = enqueue_client
    request.side_effect = [
        enqueue_response(500, message),
        enqueue_response(503, message),
        enqueue_response(202),
    ]
    files = [{"filename": "Album\\track.flac", "size": 1000}]

    result = client.enqueue_download("rhapsodyarchive", files)

    assert result == {"status": "enqueued", "username": "rhapsodyarchive", "files_count": 1}
    assert request.call_count == 3
    assert [entry.kwargs["json"] for entry in request.call_args_list] == [files] * 3
    assert sleep.call_args_list == [call(1), call(2)]


def test_slskd_enqueue_unreachable_peer_stops_after_three_attempts(enqueue_client):
    client, request, sleep = enqueue_client
    request.return_value = enqueue_response(500, "Failed to connect to user peer")

    with pytest.raises(SlskdPeerUnavailableError) as raised:
        client.enqueue_download("peer", [{"filename": "Album\\track.flac"}])

    assert raised.value.reason == "unreachable"
    assert raised.value.queued_files == []
    assert str(raised.value) == "Soulseek user peer could not be reached after 3 attempts."
    assert request.call_count == 3
    assert sleep.call_args_list == [call(1), call(2)]


@pytest.mark.parametrize("status_code,message", [
    (500, "Internal database error"),
    (400, "User peer appears to be offline"),
    (404, "Failed to connect to user peer"),
])
def test_slskd_enqueue_other_http_errors_are_not_retried(enqueue_client, status_code, message):
    client, request, sleep = enqueue_client
    request.return_value = enqueue_response(status_code, message)

    with pytest.raises(SlskdEnqueueError) as raised:
        client.enqueue_download("peer", [{"filename": "Album\\track.flac"}])

    assert not isinstance(raised.value, SlskdPeerUnavailableError)
    assert f"HTTP {status_code}" in str(raised.value)
    assert message in str(raised.value)
    assert raised.value.queued_files == []
    request.assert_called_once()
    sleep.assert_not_called()


def test_slskd_enqueue_uncertain_transport_error_is_not_retried(enqueue_client):
    client, request, sleep = enqueue_client
    error = SlskdAPIError("slskd request error: Read timed out")
    request.side_effect = error

    with pytest.raises(SlskdEnqueueError) as raised:
        client.enqueue_download("peer", [{"filename": "Album\\track.flac"}])

    assert not isinstance(raised.value, SlskdPeerUnavailableError)
    assert raised.value.__cause__ is error
    assert str(raised.value) == str(error)
    assert raised.value.queued_files == []
    request.assert_called_once()
    sleep.assert_not_called()


@pytest.mark.parametrize("failure,attempts", [
    (enqueue_response(500, "User peer appears to be offline"), 1),
    (enqueue_response(500, "Failed to connect to user peer"), 3),
    (enqueue_response(500, "Internal database error"), 1),
    (SlskdAPIError("slskd request error: Read timed out"), 1),
])
def test_slskd_enqueue_failure_retains_accepted_chunks(enqueue_client, failure, attempts):
    client, request, sleep = enqueue_client
    files = [{"filename": f"Album\\{track:02d}.flac", "size": 1000} for track in range(51)]
    request.side_effect = [enqueue_response(201)] + [failure] * attempts

    with pytest.raises(SlskdEnqueueError) as raised:
        client.enqueue_download("peer", files)

    assert raised.value.queued_files == files[:50]
    assert [entry.kwargs["json"] for entry in request.call_args_list] == [files[:50]] + [files[50:]] * attempts
    assert sleep.call_args_list == ([call(1), call(2)] if attempts == 3 else [])


def test_slskd_enqueue_retry_does_not_replay_successful_chunk(enqueue_client):
    client, request, sleep = enqueue_client
    files = [{"filename": f"Album\\{track:02d}.flac", "size": 1000} for track in range(51)]
    request.side_effect = [
        enqueue_response(200),
        enqueue_response(500, "Failed to connect to user peer"),
        enqueue_response(202),
    ]

    result = client.enqueue_download("peer", files)

    assert result["files_count"] == 51
    assert [entry.kwargs["json"] for entry in request.call_args_list] == [files[:50], files[50:], files[50:]]
    sleep.assert_called_once_with(1)


def test_slskd_enqueue_deduplication(monkeypatch):
    client = SlskdClient(base_url="http://mock:5030", api_key="dummy_key")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "OK"

    posted_payloads = []

    def mock_request(method, path, **kwargs):
        if "json" in kwargs:
            posted_payloads.append(kwargs["json"])
        return mock_resp

    monkeypatch.setattr(client, "_request", mock_request)

    files = [
        {"filename": "Album\\01 track.flac", "size": 1000},
        {"filename": "Album\\01 track.flac", "size": 1000},
        {"filename": "Album\\02 track.flac", "size": 2000},
    ]

    res = client.enqueue_download("peer_user", files)
    assert res["status"] == "enqueued"
    assert res["files_count"] == 2
    assert len(posted_payloads) == 1
    assert len(posted_payloads[0]) == 2
    assert posted_payloads[0][0]["filename"] == "Album\\01 track.flac"
    assert posted_payloads[0][1]["filename"] == "Album\\02 track.flac"


@pytest.mark.parametrize("status_code", [200, 204])
def test_slskd_cancel_download_encodes_path_and_preserves_history(enqueue_client, status_code):
    client, request, _ = enqueue_client
    request.return_value = enqueue_response(status_code)

    client.cancel_download("peer/name &音", "id/with ?#")

    request.assert_called_once_with(
        "DELETE", "/api/v0/transfers/downloads/peer%2Fname%20%26%E9%9F%B3/id%2Fwith%20%3F%23",
        params={"remove": "false"},
    )


@pytest.mark.parametrize("status_code", [400, 401, 404, 500])
def test_slskd_cancel_download_http_error_blocks_replacement(enqueue_client, status_code):
    client, request, _ = enqueue_client
    request.return_value = enqueue_response(status_code, "Cancellation refused")

    with pytest.raises(SlskdAPIError) as raised:
        client.cancel_download("peer", "failed-id")

    assert f"HTTP {status_code}" in str(raised.value)
    assert "Cancellation refused" in str(raised.value)
    assert "No replacement was submitted" in str(raised.value)
    request.assert_called_once()


def test_slskd_cancel_download_transport_error_propagates(enqueue_client):
    client, request, _ = enqueue_client
    error = SlskdAPIError("slskd request timed out")
    request.side_effect = error

    with pytest.raises(SlskdAPIError) as raised:
        client.cancel_download("peer", "failed-id")

    assert raised.value is error
    request.assert_called_once()


def test_slskd_queue_fingerprints_exclude_failures_but_protect_live_and_unknown_transfers(monkeypatch):
    client = SlskdClient(base_url="http://mock:5030", api_key="dummy_key")
    failed_files = [
        {"filename": f"Failed\\{index:02d} Artist - {state}.flac", "state": f"Completed, {state}"}
        for index, state in enumerate(["Rejected", "TimedOut", "Errored", "Cancelled", "Aborted"], start=1)
    ]
    protected_files = [
        {"filename": "Album\\01 Artist - Queued.flac", "state": "Queued, Remotely", "attempts": 100},
        {"filename": "Album/02 Artist - Active.mp3", "state": "InProgress"},
        {"filename": "Album\\03 Artist - Unknown.flac", "state": "Completed"},
        {"filename": "Album\\04 Artist - Success.flac", "state": "Completed, Errored, Succeeded"},
        {"filename": "Album\\05 Artist - Missing State.flac"},
    ]
    monkeypatch.setattr(client, "get_downloads", lambda: [{
        "username": "peer", "directories": [{"files": failed_files + protected_files}],
    }])

    expected_paths = {file["filename"] for file in protected_files}
    assert client.get_queued_filenames() == expected_paths
    assert client.get_queued_track_fingerprints() == {
        "full_paths": expected_paths,
        "base_filenames": {
            "01 artist - queued.flac", "02 artist - active.mp3", "03 artist - unknown.flac",
            "04 artist - success.flac", "05 artist - missing state.flac",
        },
        "clean_titles": {"queued", "active", "unknown", "success", "missing state"},
    }


def test_slskd_batch_search_waits_for_completion(monkeypatch):
    client = SlskdClient(base_url="http://mock:5030", api_key="dummy_key")

    call_count = {"count": 0}
    progress_updates = []

    def mock_request(method, path, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if method == "GET" and path == "/api/v0/searches":
            resp.json.return_value = []
            return resp
        elif method == "POST" and path == "/api/v0/searches":
            resp.json.return_value = {"id": "search-123", "searchText": kwargs["json"]["searchText"]}
            return resp
        elif method == "GET" and "/api/v0/searches/search-123" in path:
            call_count["count"] += 1
            if call_count["count"] < 3:
                # Simulating in-progress search where slskd has responseCount > 0 but empty responses
                resp.json.return_value = {
                    "id": "search-123",
                    "searchText": "test artist",
                    "isComplete": False,
                    "state": "InProgress",
                    "responseCount": 50,
                    "fileCount": 200,
                    "responses": []
                }
            else:
                # Search finishes on 3rd poll
                resp.json.return_value = {
                    "id": "search-123",
                    "searchText": "test artist",
                    "isComplete": True,
                    "state": "Completed, TimedOut",
                    "responseCount": 50,
                    "fileCount": 200,
                    "responses": [{"username": "peer1", "files": [{"filename": "song.flac", "size": 1000}]}]
                }
            return resp
        return resp

    monkeypatch.setattr(client, "_request", mock_request)

    def on_prog(done, total, q):
        progress_updates.append((done, total, q))

    results = client.batch_search(["test artist"], timeout=10.0, poll_interval=0.01, on_progress=on_prog)

    assert "test artist" in results
    assert len(results["test artist"]["responses"]) == 1
    assert results["test artist"]["responses"][0]["username"] == "peer1"
    assert call_count["count"] >= 3
    assert len(progress_updates) == 1
    assert progress_updates[0] == (1, 1, "test artist")


def test_slskd_batch_search_uses_in_progress_existing_search(monkeypatch):
    client = SlskdClient(base_url="http://mock:5030", api_key="dummy_key")

    post_calls = []

    def mock_request(method, path, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if method == "GET" and path == "/api/v0/searches":
            # Search is already running in slskd
            resp.json.return_value = [
                {
                    "id": "existing-sid",
                    "searchText": "existing query",
                    "isComplete": False,
                    "state": "InProgress",
                    "fileCount": 10,
                    "responseCount": 2
                }
            ]
            return resp
        elif method == "POST" and path == "/api/v0/searches":
            post_calls.append(kwargs["json"])
            resp.json.return_value = {"id": "new-sid"}
            return resp
        elif method == "GET" and "/api/v0/searches/existing-sid" in path:
            resp.json.return_value = {
                "id": "existing-sid",
                "searchText": "existing query",
                "isComplete": True,
                "state": "Completed",
                "responses": [{"username": "peer2", "files": []}]
            }
            return resp
        return resp

    monkeypatch.setattr(client, "_request", mock_request)

    results = client.batch_search(["existing query"], timeout=5.0, poll_interval=0.01)

    assert len(post_calls) == 0  # Did not dispatch duplicate POST
    assert "existing query" in results
    assert len(results["existing query"]["responses"]) == 1
