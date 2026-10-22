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
        "id": "failed", "filename": "Album\\Song.flac", "state": "Completed, TimedOut",
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

    assert not history.excludes("peer", "Album\\Song.flac")
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


@pytest.mark.parametrize("failure", ["Rejected", "Cancelled"])
def test_refusing_or_stopped_peer_is_excluded_for_other_files(failure):
    history = download_history({
        "id": "failed", "filename": "Album\\Song.flac", "state": f"Completed, {failure}",
    })

    assert history.is_peer_blocked("PEER (FLAC)")
    assert history.excludes("PEER (FLAC)", "Different/Other Song.mp3")
    assert not history.excludes("other-peer", "Album/Song.flac")
    assert history.failure_summary() == f"PEER: {failure}"


def test_peer_block_survives_empty_slskd_history():
    download_history({"filename": "Album/Song.flac", "state": "Completed, Rejected"})
    history = DownloadHistory([])
    client = MagicMock()

    assert history.excludes("peer", "Other/Unseen.flac")
    history.prepare_recovery(client)
    client.cancel_download.assert_not_called()
    assert history.failure_summary() == "peer: Rejected"


def test_blocked_peer_healthy_transfer_remains_protected():
    history = download_history(
        {"id": "failed", "filename": "Album/Bad.flac", "state": "Completed, Rejected"},
        {"id": "healthy", "filename": "Album/Good.flac", "state": "Queued, Remotely"},
    )
    client = MagicMock()

    assert history.is_peer_blocked("peer")
    assert not history.excludes("peer", "Album/Good.flac")
    assert history.excludes("peer", "Album/Another.flac")
    history.prepare_recovery(client)
    client.cancel_download.assert_not_called()


def test_healthy_copy_overrides_failed_source_across_username_casing():
    filename = "Album/Song.flac"
    history = DownloadHistory([
        {"username": "PEER", "directories": [{"files": [
            {"id": "old", "filename": filename, "state": "Completed, Rejected"},
        ]}]},
        {"username": "peer (FLAC)", "directories": [{"files": [
            {"id": "live", "filename": filename, "state": "InProgress"},
        ]}]},
    ])

    assert not history.is_peer_blocked("peer")
    assert history.failed == {}
    assert history.is_protected("PEER", filename)
    assert not history.excludes("PEER", filename)


def test_download_history_uses_explicit_policy_and_one_shot_iterable(tmp_path):
    from discogseek.core.peer_policy import PeerPolicy

    policy = PeerPolicy(tmp_path / "custom-policy.db")
    files = [{"filename": "Album/Song.flac", "state": "Completed, Rejected"}]
    downloads = iter([{"username": "peer", "directories": [{"files": files}]}])
    history = DownloadHistory(downloads, peer_policy=policy)

    assert history.excludes("peer", "Other/Song.flac")
    assert policy.blocked_peers() == {"peer": "Rejected"}
    assert PeerPolicy().blocked_peers() == {}


# Live slskd records (2026-10-21): stuck remote queues and moved-out successes.
NOW = "2026-10-21T20:51:00Z"
GHOST_STORY = (
    "@@hwvfp\\Soulseek Downloads\\complete\\### Filed SLSK DLs 20-4-26\\In Order To Care\\"
    "1.24 - Lone - In Order To Care - Ghost Story.flac"
)
CODY = (
    "Musics\\Music\\Lone (UK electronic artist Matt Cutler)\\2020 - The Light Year Wave Tape [Album]\\"
    "07 - Cody [FLAC] [889 kbps].flac"
)
SHAPEN = (
    "@@hwvfp\\Soulseek Downloads\\complete\\### Filed SLSK DLs 20-4-26\\In Order To Care\\"
    "1.01 - 96 Back - In Order To Care - Shapen1.flac"
)


def stuck_queue(**fields):
    return {
        "id": "66189263-3443-47f5-8841-0087d7485e35", "filename": GHOST_STORY, "size": 31689889,
        "state": "Queued, Remotely", "requestedAt": "2026-10-09T16:24:27.5500323",
        "enqueuedAt": "2026-10-09T16:24:29.2622551", "bytesTransferred": 0, "attempts": 1, **fields,
    }


def old_success(**fields):
    return {
        "id": "e8c461cf-5e25-4b86-a67d-c540499ef499", "filename": SHAPEN, "size": 45953478,
        "state": "Completed, Succeeded", "requestedAt": "2026-10-09T16:24:27.380854",
        "enqueuedAt": "2026-10-09T16:24:27.7895267", "startedAt": "2026-10-09T16:24:28.8098622Z",
        "endedAt": "2026-10-09T16:25:20.2045001Z", "bytesTransferred": 45953478, "attempts": 1, **fields,
    }


def history_at(*files, username="godfather", now=NOW, **kwargs):
    return DownloadHistory(
        [{"username": username, "directories": [{"files": list(files)}]}], now=now, **kwargs,
    )


def test_parse_timestamp_reads_slskd_precision_and_treats_naive_values_as_utc():
    from datetime import datetime, timezone

    from discogseek.core.transfers import parse_timestamp

    expected = datetime(2026, 10, 9, 16, 24, 29, 262255, tzinfo=timezone.utc).timestamp()
    assert parse_timestamp("2026-10-09T16:24:29.2622551") == expected
    assert parse_timestamp("2026-10-09T16:24:29.2622551Z") == expected
    assert parse_timestamp("2026-10-09T21:24:29.2622551+05:00") == expected
    assert parse_timestamp(datetime(2026, 10, 9, 16, 24, 29, 262255)) == expected
    assert parse_timestamp(expected) == expected
    for unset in (None, "", "soon", "0001-01-01T00:00:00", True):
        assert parse_timestamp(unset) is None


def test_stale_remote_queue_is_not_protected_and_is_replaced():
    stale = stuck_queue()
    history = history_at(stale)
    client = MagicMock()

    assert history.stale == {("godfather", GHOST_STORY.replace("\\", "/")): stale}
    assert history.protected == {} and history.failed == {}
    assert history.queued_filenames == set() and history.queued_base_filenames == set()
    assert not history.is_protected("godfather", GHOST_STORY)
    assert not history.is_peer_blocked("godfather")
    assert not history.excludes("other-peer", GHOST_STORY)
    history.prepare_recovery(client)
    client.cancel_download.assert_not_called()

    assert history.excludes("godfather", GHOST_STORY)
    assert history.failure_summary() == (
        "godfather: Queued, Remotely (retries: 1): waiting for 12 days without starting"
    )
    history.prepare_recovery(client)
    history.prepare_recovery(client)

    client.cancel_download.assert_called_once_with("godfather", "66189263-3443-47f5-8841-0087d7485e35")


def test_stale_queue_is_cancelled_only_for_the_row_being_replaced():
    history = history_at(stuck_queue())
    client = MagicMock()
    assert history.excludes("godfather", GHOST_STORY)

    history.prepare_recovery(client, {("godfather", "Other/Track.flac")})
    client.cancel_download.assert_not_called()

    history.prepare_recovery(client, {("godfather", GHOST_STORY.replace("\\", "/"))})
    client.cancel_download.assert_called_once_with("godfather", "66189263-3443-47f5-8841-0087d7485e35")


@pytest.mark.parametrize("hours,stale", [(1, False), (5.9, False), (6.1, True), (24 * 12, True)])
def test_queue_becomes_stale_after_the_threshold(hours, stale):
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 10, 21, 20, 51, tzinfo=timezone.utc)
    queued = stuck_queue(requestedAt=None, enqueuedAt=(now - timedelta(hours=hours)).isoformat())
    history = history_at(queued, now=now)
    client = MagicMock()

    assert bool(history.stale) is stale
    assert history.is_protected("godfather", GHOST_STORY) is not stale
    assert history.excludes("godfather", GHOST_STORY) is stale
    history.prepare_recovery(client)
    assert client.cancel_download.called is stale


@pytest.mark.parametrize("state", ["Queued, Remotely", "Queued, Locally", "Requested", "Initializing"])
def test_waiting_states_go_stale(state):
    from discogseek.core.transfers import is_stale_queued

    assert is_stale_queued(stuck_queue(state=state), now=NOW)


@pytest.mark.parametrize("state", [
    "InProgress", "Completed, Succeeded", "Completed, TimedOut", "Completed", "", None,
])
def test_only_waiting_transfers_go_stale(state):
    from discogseek.core.transfers import is_stale_queued

    assert not is_stale_queued(stuck_queue(state=state), now=NOW)


def test_queue_without_timestamps_or_with_recent_activity_is_not_stale():
    from discogseek.core.transfers import is_stale_queued

    assert not is_stale_queued(stuck_queue(requestedAt=None, enqueuedAt=None), now=NOW)
    # A start or end after the original enqueue shows the transfer is moving.
    assert not is_stale_queued(stuck_queue(startedAt="2026-10-21T20:00:00Z"), now=NOW)
    assert is_stale_queued(stuck_queue(), now="2026-10-09T22:25:00Z")
    assert not is_stale_queued(stuck_queue(), now="2026-10-09T22:20:00Z")


def test_bytes_from_an_earlier_attempt_do_not_keep_a_requeued_retry_alive():
    # gliczide: Cody was retried and has sat in the remote queue since 10-09.
    retry = stuck_queue(id="2b6c484d-9f25-4101-8abd-5a558554f648", filename=CODY,
                        requestedAt="2026-10-09T15:30:10.1797834", enqueuedAt="2026-10-09T15:30:10.6767721",
                        bytesTransferred=8378324, attempts=2)
    history = history_at(retry, username="gliczide")
    client = MagicMock()

    assert not history.is_protected("gliczide", CODY)
    assert history.excludes("gliczide", CODY)
    history.prepare_recovery(client)
    client.cancel_download.assert_called_once_with("gliczide", "2b6c484d-9f25-4101-8abd-5a558554f648")


def test_stale_queue_threshold_reads_environment(monkeypatch):
    from discogseek.core import transfers

    monkeypatch.setenv("DISCOGSEEK_STALE_QUEUE_HOURS", "48")
    assert transfers._hours_setting("DISCOGSEEK_STALE_QUEUE_HOURS", 6) == 48 * 3600
    monkeypatch.setenv("DISCOGSEEK_STALE_QUEUE_HOURS", "soon")
    assert transfers._hours_setting("DISCOGSEEK_STALE_QUEUE_HOURS", 6) == 6 * 3600
    monkeypatch.setattr(transfers, "STALE_QUEUE_SECONDS", 13 * 86400)
    assert history_at(stuck_queue()).is_protected("godfather", GHOST_STORY)


def test_fresh_copy_of_the_same_source_overrides_its_stale_copy():
    history = history_at(stuck_queue(), stuck_queue(id="fresh", enqueuedAt="2026-10-21T20:00:00Z"))
    client = MagicMock()

    assert history.stale == {}
    assert history.is_protected("godfather", GHOST_STORY)
    assert not history.excludes("godfather", GHOST_STORY)
    history.prepare_recovery(client)
    client.cancel_download.assert_not_called()


def test_stale_queue_replaces_old_failure_of_the_same_source_for_recovery():
    failed = {"id": "old-attempt", "filename": GHOST_STORY, "state": "Completed, Errored",
              "endedAt": "2026-10-09T16:20:00Z"}
    history = history_at(failed, stuck_queue())
    client = MagicMock()

    assert history.failed == {}
    assert history.excludes("godfather", GHOST_STORY)
    history.prepare_recovery(client)
    client.cancel_download.assert_called_once_with("godfather", "66189263-3443-47f5-8841-0087d7485e35")


def test_stale_queue_without_id_cannot_be_replaced_safely():
    history = history_at(stuck_queue(id=None))
    assert history.excludes("godfather", GHOST_STORY)

    with pytest.raises(SlskdAPIError, match="did not provide its ID"):
        history.prepare_recovery(MagicMock())


def test_old_success_whose_file_left_the_downloads_folder_is_not_protected(tmp_path):
    (tmp_path / "In Order To Care").mkdir()
    history = history_at(old_success(), downloads_dir=str(tmp_path))
    client = MagicMock()

    assert history.protected == {} and history.failed == {} and history.stale == {}
    assert history.completed == {("godfather", SHAPEN.replace("\\", "/")): old_success()}
    assert history.queued_filenames == set()
    assert "1.01 - 96 back - in order to care - shapen1.flac" not in history.queued_base_filenames
    assert not history.is_protected("godfather", SHAPEN)
    # The peer delivered this file before: it remains a usable source.
    assert not history.excludes("godfather", SHAPEN)
    history.prepare_recovery(client)
    client.cancel_download.assert_not_called()


@pytest.mark.parametrize("local_name", [
    "1.01 - 96 Back - In Order To Care - Shapen1.flac",
    "1.01 - 96 Back - In Order To Care - Shapen1_639345678901234567.flac",
])
def test_success_awaiting_import_still_prevents_a_duplicate(tmp_path, local_name):
    folder = tmp_path / "In Order To Care"
    folder.mkdir()
    (folder / local_name).write_bytes(b"flac")

    history = history_at(old_success(), downloads_dir=str(tmp_path))

    assert history.is_protected("godfather", SHAPEN)
    assert history.queued_base_filenames == {"1.01 - 96 back - in order to care - shapen1.flac"}
    assert history.completed == {}


@pytest.mark.parametrize("ended,protected", [
    ("2026-10-09T16:25:20.2045001Z", False), ("2026-10-21T20:41:00Z", True), (None, True),
])
def test_without_downloads_folder_success_protects_only_within_import_window(ended, protected):
    history = history_at(old_success(endedAt=ended))

    assert history.is_protected("godfather", SHAPEN) is protected
    assert bool(history.queued_base_filenames) is protected
    assert not history.excludes("godfather", SHAPEN)


def test_success_overrides_only_older_failures_of_the_same_source():
    earlier = {"id": "earlier", "filename": SHAPEN, "state": "Completed, TimedOut",
               "endedAt": "2026-10-09T16:20:00Z"}
    later = {"id": "later", "filename": SHAPEN, "state": "Completed, Rejected",
             "exception": "Transfer rejected: File not shared.", "endedAt": "2026-10-20T10:00:00Z"}

    assert not history_at(earlier, old_success()).excludes("godfather", SHAPEN)
    history = history_at(old_success(), later)
    assert history.excludes("godfather", SHAPEN)
    assert "File not shared" in history.failure_summary()


def test_local_download_path_uses_the_remote_folder_name():
    from discogseek.core.transfers import local_download_path

    assert local_download_path("/mnt/music/downloads", SHAPEN) == (
        "/mnt/music/downloads/In Order To Care/1.01 - 96 Back - In Order To Care - Shapen1.flac"
    )
    assert local_download_path("/downloads", "Song.flac") == "/downloads/Song.flac"


def test_local_download_exists_accepts_slskd_renames_only(tmp_path):
    from discogseek.core.transfers import local_download_exists

    folder = tmp_path / "Want to 1 2_ (2009)"
    folder.mkdir()
    (folder / "03 - Brighty_638123.flac").write_bytes(b"")
    (folder / "04 - Other Song.flac").write_bytes(b"")

    assert local_download_exists(str(tmp_path), "ssk4\\Want to 1 2? (2009)\\03 - Brighty.flac")
    assert local_download_exists(str(tmp_path), "ssk4\\Want to 1 2_ (2009)\\04 - Other Song.flac")
    assert not local_download_exists(str(tmp_path), "ssk4\\Want to 1 2_ (2009)\\03 - Bright.flac")
    assert not local_download_exists(str(tmp_path), "ssk4\\Want to 1 2_ (2009)\\03 - Brighty.mp3")
    assert not local_download_exists(str(tmp_path), "Elsewhere\\03 - Brighty.flac")
    assert not local_download_exists(str(tmp_path), "")


def test_resolve_downloads_dir_prefers_environment_then_local_slskd_option(tmp_path, monkeypatch):
    from discogseek.core.transfers import resolve_downloads_dir

    client = MagicMock()
    client.get_options.return_value = {"directories": {"downloads": str(tmp_path)}}
    monkeypatch.delenv("DISCOGSEEK_SLSKD_DOWNLOADS_DIR", raising=False)
    assert resolve_downloads_dir(client) == str(tmp_path)

    override = tmp_path / "mounted"
    override.mkdir()
    monkeypatch.setenv("DISCOGSEEK_SLSKD_DOWNLOADS_DIR", str(override))
    assert resolve_downloads_dir(client) == str(override)
    monkeypatch.setenv("DISCOGSEEK_SLSKD_DOWNLOADS_DIR", str(tmp_path / "missing"))
    assert resolve_downloads_dir(client) is None


@pytest.mark.parametrize("options", [
    {"directories": {"downloads": "/app/downloads-that-only-exist-in-a-container"}},
    {"directories": {"downloads": None}}, {"directories": {}}, {}, None,
])
def test_resolve_downloads_dir_ignores_unusable_slskd_options(monkeypatch, options):
    from discogseek.core.transfers import resolve_downloads_dir

    monkeypatch.delenv("DISCOGSEEK_SLSKD_DOWNLOADS_DIR", raising=False)
    client = MagicMock()
    client.get_options.return_value = options

    assert resolve_downloads_dir(client) is None


def test_resolve_downloads_dir_is_safe_with_test_doubles_and_errors(monkeypatch):
    from discogseek.core.transfers import resolve_downloads_dir

    monkeypatch.delenv("DISCOGSEEK_SLSKD_DOWNLOADS_DIR", raising=False)
    failing = MagicMock()
    failing.get_options.side_effect = SlskdAPIError("offline")

    assert resolve_downloads_dir(MagicMock()) is None
    assert resolve_downloads_dir(failing) is None
    assert resolve_downloads_dir(None) is None


@pytest.mark.parametrize("file,retry_attempts,final", [
    ({"state": "Completed, Rejected", "attempts": 1, "nextAttemptAt": "2099-01-01T00:00:00Z"}, 10, True),
    ({"state": "Completed, Cancelled"}, None, True),
    ({"state": "Completed, Aborted"}, None, True),
    ({"state": "Completed, TimedOut", "attempts": 1, "nextAttemptAt": "2026-10-21T20:52:00Z"}, 10, False),
    ({"state": "Completed, Errored", "attempts": 3, "nextAttemptAt": "2026-10-21T20:52:00Z"}, None, False),
    ({"state": "Completed, TimedOut", "attempts": 10, "nextAttemptAt": "2026-10-21T20:52:00Z"}, 10, True),
    ({"state": "Completed, TimedOut", "attempts": 1}, 10, True),
    ({"state": "Completed, Errored", "attempts": 1, "nextAttemptAt": None}, None, True),
    ({"state": "Completed, Errored", "attempts": 1, "nextAttemptAt": "0001-01-01T00:00:00"}, 10, True),
    ({"state": "Completed, TimedOut", "attempts": 1, "nextAttemptAt": "2026-10-09T16:00:00Z"}, 10, True),
    ({"state": "Queued, Remotely", "attempts": 10}, 10, False),
    ({"state": "Completed, Succeeded"}, 10, False),
])
def test_final_failure_respects_slskd_retry_budget(file, retry_attempts, final):
    from discogseek.core.transfers import is_final_failure

    assert is_final_failure(file, retry_attempts, now=NOW) is final


URBAN_BIOLOGY = "torrent\\archive\\Machine Drum - 2002 - Urban Biology [FLAC]\\0{0} - Cream Soda Part {0}.flac"


def bulk_cancelled(count=3):
    # gepa's transfers were all cancelled by one slskd event on 2026-10-09.
    return [{
        "id": f"bulk-{number}", "filename": URBAN_BIOLOGY.format(number), "state": "Completed, Cancelled",
        "requestedAt": "2026-10-08T13:24:03.203966", "enqueuedAt": "2026-10-08T13:24:05.1642881",
        "startedAt": f"2026-10-09T14:58:16.4{number}Z", "endedAt": f"2026-10-09T14:58:16.4{number}Z",
        "bytesTransferred": 0, "attempts": 1, "exception": "A task was canceled.",
    } for number in range(1, count + 1)]


def test_bulk_cancellation_neither_blocks_the_peer_nor_excludes_its_files():
    history = history_at(*bulk_cancelled(), username="gepa")

    assert history.blocked_peers == {}
    assert history.failed == {}
    assert not history.excludes("gepa", URBAN_BIOLOGY.format(1))
    assert not history.excludes("gepa", "Other Album/01 Song.flac")


def test_own_bulk_cancellation_still_excludes_the_cancelled_sources(tmp_path):
    from discogseek.core.peer_policy import PeerPolicy

    policy = PeerPolicy(tmp_path / "policy.db")
    for number in range(1, 4):
        policy.record_internal_cancel("gepa", f"bulk-{number}")
    history = history_at(*bulk_cancelled(), username="gepa", peer_policy=policy)

    assert history.blocked_peers == {}
    assert history.excludes("gepa", URBAN_BIOLOGY.format(1))
    # Reading the same history again keeps the provenance of the cancellation.
    again = history_at(*bulk_cancelled(), username="gepa", peer_policy=policy)
    assert again.excludes("gepa", URBAN_BIOLOGY.format(2))
    assert not again.excludes("gepa", "Other Album/01 Song.flac")


@pytest.mark.parametrize("now,blocked", [("2026-10-21T21:00:00Z", True), ("2026-10-22T03:00:00Z", False)])
def test_queue_limit_rejection_blocks_the_peer_only_during_its_cooldown(now, blocked):
    refusal = {"id": "refused", "filename": "Album/11 Song.flac", "state": "Completed, Rejected",
               "exception": "Too many files", "endedAt": "2026-10-21T20:50:00Z"}
    history = history_at(refusal, username="pavlosh", now=now)

    assert history.is_peer_blocked("pavlosh") is blocked
    assert history.excludes("pavlosh", "Album/12 Other Song.flac") is blocked
    # The refused file itself is offered again once the cooldown has passed.
    assert history.excludes("pavlosh", "Album/11 Song.flac") is blocked


def test_transient_refusal_without_end_time_keeps_its_file_excluded():
    refusal = {"id": "refused", "filename": "Album/11 Song.flac", "state": "Completed, Rejected",
               "exception": "Too many files"}

    assert history_at(refusal, username="pavlosh", now="2026-10-30T00:00:00Z").excludes(
        "pavlosh", "Album/11 Song.flac")


@pytest.mark.parametrize("retry_attempts", [MagicMock(), "ten", object(), True])
def test_final_failure_ignores_an_unusable_retry_budget(retry_attempts):
    from discogseek.core.transfers import is_final_failure

    pending = {"state": "Completed, TimedOut", "attempts": 10, "nextAttemptAt": "2026-10-21T20:52:00Z"}
    assert is_final_failure(pending, retry_attempts, now=NOW) is False
    assert is_final_failure(pending, "10", now=NOW) is True


@pytest.mark.parametrize("state,exception", [
    ("Completed, Rejected", "Transfer rejected: File not shared."),
    ("Completed, Rejected", "File read error."),
    ("Completed, Aborted", "Transfer aborted: the remote size of 9299071 does not match expected size 9292132"),
])
def test_file_level_failure_excludes_only_that_file(state, exception):
    history = history_at({"id": "failed", "filename": "Album/01 Song.flac", "state": state,
                          "exception": exception, "endedAt": "2026-10-21T20:00:00Z"}, username="oxym103")

    assert history.blocked_peers == {}
    assert history.excludes("oxym103", "Album/01 Song.flac")
    assert not history.excludes("oxym103", "Album/02 Other Song.flac")


def timed_out(attempts=1, ended="2026-10-21T20:50:40Z", **fields):
    # slskd 0.26 keeps one record across its retries and sends no nextAttemptAt.
    return {"id": "retrying", "filename": "Album/02 Song.flac", "state": "Completed, TimedOut",
            "attempts": attempts, "exception": "Inactivity timeout", "endedAt": ended, **fields}


@pytest.mark.parametrize("file,live", [
    (timed_out(), True),
    (timed_out(state="Completed, Errored"), True),
    # Ended longer ago than slskd's longest retry delay plus the margin.
    (timed_out(ended="2026-10-21T20:49:00Z"), False),
    (timed_out(attempts=10), False),
    (timed_out(ended=None), False),
    (timed_out(ended="2026-10-21T20:00:00Z", nextAttemptAt="2026-10-21T20:52:00Z"), True),
    (timed_out(state="Completed, Rejected"), False),
])
def test_attempt_that_slskd_still_retries_is_live_under_its_retry_budget(file, live):
    history = history_at(file, retry_attempts=10, retry_max_delay=30.0)
    client = MagicMock()

    assert history.is_protected("godfather", file["filename"]) is live
    assert history.excludes("godfather", file["filename"]) is not live
    assert (file["filename"] in history.queued_filenames) is live
    history.prepare_recovery(client)
    assert client.cancel_download.called is not live


def test_without_a_retry_budget_every_timed_out_record_is_a_failure():
    history = history_at(timed_out())

    assert history.excludes("godfather", "Album/02 Song.flac")
    assert set(history.failed) == {("godfather", "Album/02 Song.flac")}


def test_retry_budget_from_slskd_options_reaches_histories_built_in_its_context():
    from discogseek.core.transfers import retry_budget, slskd_retry_budget

    options = {"transfers": {"download": {"retry": {"attempts": 10, "delay": 2000, "maxDelay": 30000}}}}
    assert retry_budget(options) == (10, 30.0)
    assert retry_budget({"transfers": {"retry": {"attempts": "5"}}}) == (5, None)
    assert retry_budget(MagicMock()) == retry_budget(None) == (None, None)

    with slskd_retry_budget(*retry_budget(options)):
        assert history_at(timed_out()).is_protected("godfather", "Album/02 Song.flac")
        # An explicit budget takes precedence over the context.
        assert history_at(timed_out(), retry_attempts=1).excludes("godfather", "Album/02 Song.flac")
    assert history_at(timed_out()).excludes("godfather", "Album/02 Song.flac")


def test_retrying_attempt_already_stopped_by_discogseek_is_excluded_and_not_cancelled_again(tmp_path):
    from discogseek.core.peer_policy import PeerPolicy

    policy = PeerPolicy(tmp_path / "policy.db")
    # The browser stopped slskd's retries; slskd keeps the TimedOut record.
    policy.record_internal_cancel("godfather", "retrying")
    history = history_at(timed_out(), peer_policy=policy, retry_attempts=10, retry_max_delay=30.0)
    client = MagicMock()

    assert history.excludes("godfather", "Album/02 Song.flac")
    history.prepare_recovery(client)
    client.cancel_download.assert_not_called()


@pytest.mark.parametrize("failure", ["TimedOut", "Errored", "Rejected"])
def test_failure_cancelled_before_is_not_cancelled_again_but_others_are(tmp_path, failure):
    from discogseek.core.peer_policy import PeerPolicy

    policy = PeerPolicy(tmp_path / "policy.db")
    policy.record_internal_cancel("GodFather (FLAC)", "stopped")
    history = history_at(
        {"id": "stopped", "filename": "Album/02 Song.flac", "state": f"Completed, {failure}", "attempts": 10},
        {"id": "other", "filename": "Album/03 Song.flac", "state": f"Completed, {failure}", "attempts": 10},
        peer_policy=policy,
    )
    client = MagicMock()

    assert history.excludes("godfather", "Album/02 Song.flac")
    assert history.excludes("godfather", "Album/03 Song.flac")
    history.prepare_recovery(client)

    client.cancel_download.assert_called_once_with("godfather", "other")
