"""slskd search polling and download payload deduplication."""

from threading import Barrier, Lock
from unittest.mock import MagicMock, call

import pytest

from discogseek.clients.slskd import (
    SlskdAPIError,
    SlskdClient,
    SlskdEnqueueError,
    SlskdPeerBlockedError,
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
    monkeypatch.setattr(client, "get_downloads", MagicMock(return_value=[]))
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


@pytest.mark.parametrize("state", ["Rejected", "Aborted", "Cancelled"])
def test_enqueue_observes_new_peer_block_before_post(enqueue_client, state):
    client, request, _ = enqueue_client
    client.get_downloads.return_value = [{"username": "Peer", "directories": [{"files": [{
        "id": "old", "filename": "Other Album/old.flac", "state": f"Completed, {state}",
    }]}]}]

    with pytest.raises(SlskdPeerBlockedError, match="blocked"):
        client.enqueue_download("peer (FLAC)", [{"filename": "New Album/new.flac"}])

    request.assert_not_called()
    # Clearing server history and creating a new client cannot clear the block.
    fresh = SlskdClient(base_url="http://mock:5030", api_key="dummy_key")
    fresh.get_downloads = MagicMock(return_value=[])
    fresh._request = request
    with pytest.raises(SlskdPeerBlockedError):
        fresh.enqueue_download("Peer", [{"filename": "Third Album/new.flac"}])
    request.assert_not_called()


def test_enqueue_retains_first_chunk_if_peer_is_canceled_between_chunks(enqueue_client):
    client, request, _ = enqueue_client
    request.return_value = enqueue_response(202)
    files = [{"filename": f"Album/{number}.flac"} for number in range(51)]
    client.get_downloads.side_effect = [[], [{"username": "peer", "directories": [{"files": [{
        "id": "manual", "filename": "Album/0.flac", "state": "Completed, Cancelled",
    }]}]}]]

    with pytest.raises(SlskdPeerBlockedError) as raised:
        client.enqueue_download("peer", files)

    assert [file["filename"] for file in raised.value.queued_files] == [file["filename"] for file in files[:50]]
    request.assert_called_once()


def test_enqueue_does_not_submit_when_fresh_history_cannot_be_checked(enqueue_client):
    client, request, _ = enqueue_client
    client.get_downloads.side_effect = SlskdAPIError("History unavailable")

    with pytest.raises(SlskdEnqueueError, match="Cannot verify blocked peers"):
        client.enqueue_download("peer", [{"filename": "Album/Song.flac"}])

    request.assert_not_called()


def test_internal_client_cancel_is_not_later_treated_as_a_peer_block(enqueue_client):
    client, request, _ = enqueue_client
    request.return_value = enqueue_response(204)
    client.cancel_download("peer", "automatic")
    client.get_downloads.return_value = [{"username": "peer", "directories": [{"files": [{
        "id": "automatic", "filename": "Old/song.flac", "state": "Completed, Cancelled",
    }]}]}]
    request.reset_mock()
    request.return_value = enqueue_response(202)

    client.enqueue_download("peer", [{"filename": "New/song.flac"}])

    request.assert_called_once()
    assert client.peer_policy.blocked_reason("peer") is None


@pytest.mark.parametrize("failure", [500, "transport"])
def test_failed_auto_cancel_does_not_exempt_later_manual_cancel(enqueue_client, failure):
    client, request, _ = enqueue_client
    if failure == "transport":
        request.side_effect = SlskdAPIError("Connection lost")
    else:
        request.return_value = enqueue_response(failure, "Cancellation refused")
    with pytest.raises(SlskdAPIError):
        client.cancel_download("peer", "transfer")

    client.peer_policy.observe([{"username": "peer", "directories": [{"files": [{
        "id": "transfer", "filename": "Old/song.flac", "state": "Completed, Cancelled",
    }]}]}])

    assert client.peer_policy.blocked_reason("peer")


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
    monkeypatch.setattr(client, "get_downloads", lambda: [])

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


@pytest.fixture
def search_client(monkeypatch):
    client = SlskdClient(base_url="http://mock:5030", api_key="dummy_key")
    clock = {"now": 0.0}

    def advance(seconds):
        clock["now"] += seconds

    monkeypatch.setattr("discogseek.clients.slskd.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr("discogseek.clients.slskd.time.sleep", advance)
    request = MagicMock()
    monkeypatch.setattr(client, "_request", request)
    monkeypatch.setattr(client, "list_searches", lambda: [])
    return client, request, clock


def search_response(data):
    return MagicMock(status_code=200, json=MagicMock(return_value=data))


def peer_response(filename="song.flac"):
    return {"username": "peer", "files": [{"filename": filename, "size": 1000}]}


def test_slskd_batch_search_waits_for_completion_and_fetches_live_responses(search_client):
    client, request, clock = search_client
    progress_updates = []

    def respond(method, path, **kwargs):
        if method == "POST":
            return search_response({"id": "search-123"})
        if path.endswith("/responses"):
            files = [peer_response()]
            if clock["now"] >= 8:
                files.append(peer_response("late.flac"))
            return search_response(files)
        return search_response({
            "id": "search-123", "isComplete": False,
            "state": "Completed, TimedOut" if clock["now"] >= 9 else "InProgress",
            "responseCount": 50, "fileCount": 200, "responses": [],
        })

    request.side_effect = respond
    results = client.batch_search(
        ["test artist"], timeout=10.0,
        on_progress=lambda *args: progress_updates.append(args),
    )

    assert clock["now"] == 9
    assert results["test artist"]["responses"] == [peer_response(), peer_response("late.flac")]
    assert progress_updates == [(1, 1, "test artist")]
    assert sum(entry.args[1].endswith("/responses") for entry in request.call_args_list) == 9


def test_slskd_batch_search_observes_cold_search_for_six_seconds(search_client):
    client, request, clock = search_client

    def respond(method, path, **kwargs):
        if method == "POST":
            return search_response({"id": "new-search"})
        return search_response({"isComplete": True, "responses": [peer_response()]})

    request.side_effect = respond
    results = client.batch_search(["query"], timeout=2)

    assert clock["now"] == 6
    assert results["query"]["responses"] == [peer_response()]


def test_slskd_batch_search_uses_in_progress_existing_search(search_client, monkeypatch):
    client, request, clock = search_client
    monkeypatch.setattr(client, "list_searches", lambda: [{
        "id": "existing-sid", "searchText": "existing query",
        "isComplete": False, "state": "InProgress", "fileCount": 10,
    }])
    request.return_value = search_response({"isComplete": True, "responses": [peer_response()]})

    results = client.batch_search(["existing query"], timeout=5.0)

    request.assert_called_once_with("GET", "/api/v0/searches/existing-sid?includeResponses=true")
    assert clock["now"] == 1
    assert results["existing query"]["responses"] == [peer_response()]


def test_slskd_batch_search_keeps_files_when_completed_snapshot_is_empty(search_client):
    client, request, clock = search_client

    def respond(method, path, **kwargs):
        if method == "POST":
            return search_response({"id": "search-id"})
        if path.endswith("/responses"):
            return search_response([peer_response()] if clock["now"] == 1 else [])
        return search_response({
            "isComplete": clock["now"] >= 8,
            "state": "Completed" if clock["now"] >= 8 else "InProgress",
            "fileCount": 1 if clock["now"] == 1 else 0, "responses": [],
        })

    request.side_effect = respond
    results = client.batch_search(["query"])

    assert clock["now"] == 8
    assert results["query"]["responses"] == [peer_response()]
    assert results["query"]["fileCount"] == 1
    assert results["query"]["isComplete"] is True


@pytest.mark.parametrize("final_failure", [False, True])
def test_slskd_batch_search_timeout_retains_streamed_files(search_client, final_failure):
    client, request, clock = search_client
    metadata_reads = 0
    progress_updates = []

    def respond(method, path, **kwargs):
        nonlocal metadata_reads
        if method == "POST":
            return search_response({"id": "search-id"})
        if path.endswith("/responses"):
            return search_response([peer_response()] if clock["now"] == 1 else [])
        metadata_reads += 1
        if final_failure and metadata_reads > 28:
            raise SlskdAPIError("Disconnected during final poll")
        return search_response({"isComplete": False, "state": "InProgress", "responses": []})

    request.side_effect = respond
    results = client.batch_search(
        ["query"], timeout=2, on_progress=lambda *args: progress_updates.append(args),
    )

    assert clock["now"] == 28
    assert metadata_reads == 29
    assert results["query"]["responses"] == [peer_response()]
    assert results["query"]["isComplete"] is False
    assert progress_updates == [(1, 1, "query")]


@pytest.mark.parametrize("already_complete", [False, True])
def test_slskd_batch_search_reads_concurrently_with_bounded_workers(search_client, monkeypatch, already_complete):
    client, request, clock = search_client
    queries = [f"query-{i}" for i in range(4)]
    monkeypatch.setattr(client, "list_searches", lambda: [
        {"id": query, "searchText": query, "isComplete": already_complete, "fileCount": 1}
        for query in queries
    ])
    barrier = Barrier(2, timeout=2)
    lock = Lock()
    concurrency = {"active": 0, "peak": 0}
    fetched = []

    def fetch(sid):
        with lock:
            concurrency["active"] += 1
            concurrency["peak"] = max(concurrency["peak"], concurrency["active"])
        try:
            # Serial polling cannot pass this rendezvous. No real sleep/network.
            barrier.wait()
            with lock:
                fetched.append(sid)
            return {"isComplete": True, "responses": [peer_response()]}
        finally:
            with lock:
                concurrency["active"] -= 1

    monkeypatch.setattr(client, "get_search_results", fetch)
    results = client.batch_search(queries, max_concurrent=2)

    assert sorted(fetched) == queries
    assert concurrency["peak"] == 2
    assert set(results) == set(queries)
    assert clock["now"] == (0 if already_complete else 2)
    request.assert_not_called()


def test_slskd_batch_search_purges_empty_completed_search_before_redispatch(search_client, monkeypatch):
    client, request, clock = search_client
    monkeypatch.setattr(client, "list_searches", lambda: [{
        "id": "stale", "searchText": "query", "isComplete": True, "fileCount": 0,
    }])

    def respond(method, path, **kwargs):
        if method == "POST":
            return search_response({"id": "new"})
        return search_response({"isComplete": True, "responses": [peer_response()]})

    request.side_effect = respond
    results = client.batch_search(["query"])

    assert request.call_args_list[:2] == [
        call("DELETE", "/api/v0/searches/stale"),
        call("POST", "/api/v0/searches", json={"searchText": "query"}),
    ]
    assert results["query"]["responses"] == [peer_response()]


@pytest.mark.parametrize("stale_file_count", [0, 10])
def test_slskd_batch_search_reuses_running_search_when_old_results_are_unavailable(
    search_client, monkeypatch, stale_file_count,
):
    client, request, clock = search_client
    monkeypatch.setattr(client, "list_searches", lambda: [
        {"id": "old", "searchText": "query", "isComplete": True, "fileCount": stale_file_count},
        {"id": "running", "searchText": "query", "isComplete": False, "fileCount": 0},
    ])

    def respond(method, path, **kwargs):
        if method == "DELETE":
            return search_response({})
        if path.startswith("/api/v0/searches/old"):
            raise SlskdAPIError("Old search no longer exists")
        assert method == "GET"
        assert path == "/api/v0/searches/running?includeResponses=true"
        return search_response({"isComplete": True, "responses": [peer_response()]})

    request.side_effect = respond
    results = client.batch_search(["query"])

    assert results["query"]["responses"] == [peer_response()]
    assert clock["now"] == 1
    assert all(entry.args[0] != "POST" for entry in request.call_args_list)
