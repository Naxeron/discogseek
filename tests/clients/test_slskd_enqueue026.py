"""SlskdClient.enqueue_download against slskd 0.26 replies (tests/helpers/slskd026.py)."""

import threading

import pytest

from discogseek.clients.slskd import (
    SlskdClient,
    SlskdEnqueueError,
    SlskdPeerUnavailableError,
)
from tests.helpers.slskd026 import FakeSlskd026

ALBUM = "music\\Lone - Reality Testing (japanese edition) (2014) [CD FLAC]"
FILES = [
    {"filename": f"{ALBUM}\\01 First Born Seconds.flac", "size": 31_000_000},
    {"filename": f"{ALBUM}\\02 Restless City.flac", "size": 32_000_000},
]


@pytest.fixture
def slskd(monkeypatch):
    client = SlskdClient(base_url="http://mock:5030", api_key="dummy_key")
    fake = FakeSlskd026().install(monkeypatch, client)
    sleeps = []
    monkeypatch.setattr("discogseek.clients.slskd.time.sleep", sleeps.append)
    return client, fake, sleeps


def enqueue_posts(fake, user="estrogenpanther"):
    return fake.calls("POST", f"/api/v0/transfers/downloads/{user}")


@pytest.mark.parametrize("busy, delays", [(1, [1.0]), (3, [1.0, 2.0, 4.0])])
def test_busy_slskd_enqueue_is_retried(slskd, busy, delays):
    client, fake, sleeps = slskd
    fake.busy_enqueue_posts = busy

    result = client.enqueue_download("estrogenpanther", FILES)

    assert result == {"status": "enqueued", "username": "estrogenpanther", "files_count": 2}
    assert len(enqueue_posts(fake)) == busy + 1
    assert sleeps == delays
    assert {transfer["filename"] for transfer in fake.transfers} == {file["filename"] for file in FILES}


def test_enqueue_still_busy_after_retries_fails_without_queueing(slskd):
    client, fake, sleeps = slskd
    fake.busy_enqueue_posts = 4

    with pytest.raises(SlskdEnqueueError) as raised:
        client.enqueue_download("estrogenpanther", FILES)

    assert not isinstance(raised.value, SlskdPeerUnavailableError)
    assert "HTTP 429" in str(raised.value)
    assert raised.value.queued_files == []
    assert len(enqueue_posts(fake)) == 4
    assert sleeps == [1.0, 2.0, 4.0]


@pytest.mark.parametrize("style", ["tuple", "named", "opaque"])
def test_partially_failed_enqueue_reports_only_accepted_files(slskd, style):
    client, fake, _ = slskd
    fake.failed_entry_style = style
    fake.enqueue_errors[FILES[1]["filename"]] = "database is locked"

    with pytest.raises(SlskdEnqueueError) as raised:
        client.enqueue_download("estrogenpanther", FILES)

    assert not isinstance(raised.value, SlskdPeerUnavailableError)
    assert raised.value.queued_files == [FILES[0]]
    assert "did not queue 1 of 2 files from estrogenpanther" in str(raised.value)
    if style != "opaque":
        assert "database is locked" in str(raised.value)
    assert len(enqueue_posts(fake)) == 1


@pytest.mark.parametrize("style", ["opaque", "tuple", "named"])
def test_files_already_downloading_count_as_queued(slskd, style):
    client, fake, _ = slskd
    fake.failed_entry_style = style
    fake.transfers.append({"id": "live", "username": "estrogenpanther", "filename": FILES[0]["filename"],
                           "size": FILES[0]["size"], "state": "InProgress"})

    result = client.enqueue_download("estrogenpanther", FILES)

    assert result["files_count"] == 2
    assert [transfer["filename"] for transfer in fake.transfers] == [file["filename"] for file in FILES]


def test_unnamed_failures_are_told_apart_by_live_transfers(slskd):
    # slskd 0.26 writes failed (Filename, Message) tuples as {}: a file skipped
    # as already in progress has a live transfer, a file that errored does not.
    client, fake, _ = slskd
    files = FILES + [{"filename": f"{ALBUM}\\03 Meeker Warm Energy.flac", "size": 33_000_000}]
    fake.transfers.append({"id": "live", "username": "estrogenpanther", "filename": files[0]["filename"],
                           "size": files[0]["size"], "state": "Queued, Remotely"})
    fake.transfers.append({"id": "old", "username": "estrogenpanther", "filename": files[2]["filename"],
                           "size": files[2]["size"], "state": "Completed, Errored"})
    fake.enqueue_errors[files[2]["filename"]] = "database is locked"

    with pytest.raises(SlskdEnqueueError) as raised:
        client.enqueue_download("estrogenpanther", files)

    assert raised.value.queued_files == files[:2]
    assert f"did not queue 1 of 3 files from estrogenpanther: {files[2]['filename']}: not queued" in str(raised.value)


def test_failure_naming_a_file_outside_the_request_is_not_success(monkeypatch):
    client = SlskdClient(base_url="http://mock:5030", api_key="dummy_key")
    monkeypatch.setattr(client, "get_downloads", lambda: [])
    body = {"enqueued": [{"filename": FILES[0]["filename"]}],
            "failed": [{"item1": FILES[1]["filename"].replace("\\", "/"), "item2": "Error: database is locked"}]}

    class Reply:
        status_code = 201
        text = ""

        def json(self):
            return body

    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: Reply())

    with pytest.raises(SlskdEnqueueError) as raised:
        client.enqueue_download("estrogenpanther", FILES)

    assert raised.value.queued_files == [FILES[0]]


def test_partial_failure_keeps_earlier_chunks_and_stops_before_later_ones(slskd):
    client, fake, _ = slskd
    files = [{"filename": f"{ALBUM}\\{n:02d} Track.flac", "size": 1000} for n in range(1, 52)]
    fake.enqueue_errors[files[10]["filename"]] = "Transfer record could not be saved"

    with pytest.raises(SlskdEnqueueError) as raised:
        client.enqueue_download("estrogenpanther", files)

    assert raised.value.queued_files == files[:10] + files[11:50]
    assert [len(body) for _, _, body in enqueue_posts(fake)] == [50]


def test_peer_endpoint_lookup_failure_is_an_unreachable_peer(slskd):
    client, fake, sleeps = slskd
    message = "Failed to retrieve endpoint for user estrogenpanther: Operation timed out"
    fake.enqueue_user_errors["estrogenpanther"] = [(500, message)] * 3

    with pytest.raises(SlskdPeerUnavailableError) as raised:
        client.enqueue_download("estrogenpanther", FILES)

    assert raised.value.reason == "unreachable"
    assert raised.value.queued_files == []
    assert len(enqueue_posts(fake)) == 3
    assert sleeps == [1, 2]


def test_busy_and_unreachable_retries_are_counted_separately(slskd):
    client, fake, sleeps = slskd
    fake.busy_enqueue_posts = 1
    fake.enqueue_user_errors["estrogenpanther"] = [(500, "Failed to connect to user estrogenpanther")] * 2

    client.enqueue_download("estrogenpanther", FILES)

    assert len(enqueue_posts(fake)) == 4
    assert sleeps == [1.0, 1, 2]


def test_overlapping_enqueue_requests_are_both_accepted(monkeypatch):
    client = SlskdClient(base_url="http://mock:5030", api_key="dummy_key")
    fake = FakeSlskd026().install(monkeypatch, client)
    # A real pause outlasting the first request's hold of slskd's limiter.
    monkeypatch.setattr("discogseek.clients.slskd.time.sleep", lambda seconds: threading.Event().wait(0.3))
    fake.post_hold = 0.2
    results = {}
    first = threading.Thread(target=lambda: results.update(first=client.enqueue_download("first", FILES[:1])))
    first.start()
    assert fake.enqueue_entered.wait(2)
    # The browser and the CLI can enqueue at the same moment.
    results["second"] = client.enqueue_download("second", FILES[1:])
    first.join(2)

    assert results["first"]["status"] == results["second"]["status"] == "enqueued"
    assert [path for _, path, _ in fake.calls("POST")] == [
        "/api/v0/transfers/downloads/first",
        "/api/v0/transfers/downloads/second",
        "/api/v0/transfers/downloads/second",
    ]
