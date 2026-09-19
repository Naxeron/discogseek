"""Interpret failed transfer history without disturbing healthy downloads."""

from unittest.mock import MagicMock, call

import pytest

from discogseek.clients.slskd import SlskdAPIError
from discogseek.core.transfers import DownloadHistory, failure_description, terminal_failure


def download_history(*files, username="peer", excluded_sources=None):
    return DownloadHistory(
        [{"username": username, "directories": [{"files": list(files)}]}],
        excluded_sources=excluded_sources,
    )


@pytest.mark.parametrize("failure", ["Rejected", "TimedOut", "Errored", "Cancelled", "Aborted"])
def test_terminal_failure_does_not_suppress_later_download(failure):
    file = {"filename": "Album\\01 Song.flac", "state": f"Completed, {failure}"}

    history = download_history(file)

    assert terminal_failure(file) == failure
    assert history.queued_filenames == set()
    assert history.queued_base_filenames == set()
    assert history.excludes("peer", file["filename"])


@pytest.mark.parametrize("state", [
    "InProgress", "Requested", "Queued, Locally", "Queued, Remotely", "Completed",
    "Completed, Unknown", "TimedOut", "", None,
])
def test_active_or_unknown_state_remains_protected_despite_retry_count(state):
    file = {
        "id": "active", "filename": "Album\\01 Song.flac", "state": state,
        "attempts": 100, "exception": "Failure from an earlier attempt",
    }
    history = download_history(file)
    client = MagicMock()

    assert terminal_failure(file) is None
    assert history.queued_filenames == {file["filename"]}
    assert history.queued_base_filenames == {"01 song.flac"}
    assert not history.excludes("peer", file["filename"])
    history.prepare_recovery(client)
    client.cancel_download.assert_not_called()


@pytest.mark.parametrize("failure", ["Rejected", "TimedOut", "Errored", "Cancelled", "Aborted"])
def test_succeeded_takes_precedence_over_failure_flags(failure):
    file = {
        "id": "done", "filename": "Album\\Song.flac",
        "state": f"Completed, {failure}, Succeeded",
    }
    history = download_history(file)
    client = MagicMock()

    assert terminal_failure(file) is None
    assert history.queued_filenames == {file["filename"]}
    assert not history.excludes("peer", file["filename"])
    history.prepare_recovery(client)
    client.cancel_download.assert_not_called()


@pytest.mark.parametrize("stop", ["Cancelled", "Aborted"])
def test_explicit_stop_takes_precedence_over_recoverable_failure(stop):
    file = {"id": "stopped", "filename": "Album/Song.flac", "state": f"Completed, Errored, {stop}"}
    history = download_history(file)
    client = MagicMock()

    assert terminal_failure(file) == stop
    assert history.excludes("peer", file["filename"])
    history.prepare_recovery(client)
    client.cancel_download.assert_not_called()


def test_failed_source_exclusion_is_exact_not_peerwide_or_filenamewide():
    history = download_history({
        "id": "failed", "filename": "Album\\Song.flac", "state": "Completed, Rejected",
    })

    assert history.excludes("peer (FLAC)", "Album/Song.flac")
    assert not history.excludes("other-peer", "Album\\Song.flac")
    assert not history.excludes("peer", "Album\\Another Song.flac")
    assert not history.excludes("peer", "Other Album\\Song.flac")


@pytest.mark.parametrize("failure", ["Rejected", "TimedOut", "Errored"])
def test_recovery_cancels_only_encountered_failed_source_once(failure):
    history = download_history(
        {
            "id": "requested", "filename": "Wanted\\Song.flac", "state": f"Completed, {failure}",
            "nextAttemptAt": "2099-01-01T00:00:00Z",
        },
        {"id": "unrelated", "filename": "Other\\Song.flac", "state": "Completed, Errored"},
    )
    client = MagicMock()

    history.prepare_recovery(client)
    client.cancel_download.assert_not_called()
    assert history.excludes("peer", "Wanted/Song.flac")
    assert history.excludes("peer", "Wanted/Song.flac")
    client.cancel_download.assert_not_called()

    history.prepare_recovery(client)
    history.prepare_recovery(client)

    client.cancel_download.assert_called_once_with("peer", "requested")


def test_missing_transfer_id_blocks_recovery():
    history = download_history({"filename": "Album\\Song.flac", "state": "Completed, TimedOut"})
    assert history.excludes("peer", "Album\\Song.flac")
    client = MagicMock()

    with pytest.raises(SlskdAPIError, match="did not provide its ID"):
        history.prepare_recovery(client)

    client.cancel_download.assert_not_called()


def test_cancel_failure_blocks_recovery_and_allows_another_attempt():
    history = download_history({
        "id": "failed", "filename": "Album\\Song.flac", "state": "Completed, Errored",
    })
    assert history.excludes("peer", "Album\\Song.flac")
    client = MagicMock()
    error = SlskdAPIError("Cancellation timed out")
    client.cancel_download.side_effect = [error, None]

    with pytest.raises(SlskdAPIError) as raised:
        history.prepare_recovery(client)
    assert raised.value is error

    history.prepare_recovery(client)
    history.prepare_recovery(client)
    assert client.cancel_download.call_args_list == [call("peer", "failed")] * 2


@pytest.mark.parametrize("failure", ["Cancelled", "Aborted"])
def test_canceled_or_aborted_download_is_not_canceled_again(failure):
    history = download_history({
        "id": "stopped", "filename": "Album\\Song.flac", "state": f"Completed, {failure}",
    })
    client = MagicMock()
    assert history.excludes("peer", "Album\\Song.flac")

    history.prepare_recovery(client)

    client.cancel_download.assert_not_called()


@pytest.mark.parametrize("healthy_state", ["Queued, Remotely", "InProgress", "Completed, Succeeded"])
@pytest.mark.parametrize("failed_first", [True, False])
def test_healthy_duplicate_source_overrides_failed_record(healthy_state, failed_first):
    failed = {"id": "old", "filename": "Album\\Song.flac", "state": "Completed, Errored"}
    healthy = {"id": "current", "filename": "Album/Song.flac", "state": healthy_state}
    files = [failed, healthy] if failed_first else [healthy, failed]
    history = download_history(*files)
    client = MagicMock()

    assert history.queued_filenames == {healthy["filename"]}
    assert not history.excludes("peer", failed["filename"])
    history.prepare_recovery(client)
    client.cancel_download.assert_not_called()


@pytest.mark.parametrize("healthy_state", ["Queued, Remotely", "InProgress", "Completed, Succeeded"])
def test_explicit_exclusion_never_cancels_current_healthy_source(healthy_state):
    history = download_history(
        {"id": "old", "filename": "Album\\Song.flac", "state": "Completed, Errored"},
        {"id": "current", "filename": "Album/Song.flac", "state": healthy_state},
        excluded_sources={("peer", "Album\\Song.flac")},
    )
    client = MagicMock()

    assert history.excludes("peer", "Album\\Song.flac")
    assert history.is_protected("peer", "Album\\Song.flac")
    assert history.queued_filenames == {"Album/Song.flac"}
    history.prepare_recovery(client)

    client.cancel_download.assert_not_called()


def test_explicit_source_exclusion_without_history_does_not_cancel():
    history = download_history(excluded_sources={("peer", "Album\\Song.flac")})
    client = MagicMock()

    assert history.excludes("peer", "Album/Song.flac")
    assert not history.excludes("peer", "Other\\Song.flac")
    history.prepare_recovery(client)

    client.cancel_download.assert_not_called()
    assert "previously failed source" in history.failure_summary()


def test_failure_description_preserves_reason_and_retry_count_without_line_breaks():
    file = {
        "state": "Completed, Rejected", "attempts": 100,
        "exception": "  Too many\n\tfailed transfers this week  ",
    }

    description = failure_description(file)

    assert description == "Rejected (retries: 100): Too many failed transfers this week"


def test_failure_summary_only_reports_encountered_failures():
    history = download_history(
        {"filename": "Wanted\\Song.flac", "state": "Completed, Rejected", "exception": "File not shared"},
        {"filename": "Other\\Song.flac", "state": "Completed, Errored", "exception": "Unrelated failure"},
    )

    assert history.failure_summary() == ""
    assert history.excludes("peer", "Wanted\\Song.flac")
    assert history.failure_summary() == "peer: Rejected: File not shared"
