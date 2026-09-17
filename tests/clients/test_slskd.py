"""slskd search polling and download payload deduplication."""

from unittest.mock import MagicMock

from discogseek.clients.slskd import SlskdClient


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
