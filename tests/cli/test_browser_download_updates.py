"""Live transfer completion and eventual library import without real services."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from discogseek.cli import browser
from discogseek.cli.browser import ReleaseBrowser, release_key, track_key
from discogseek.cli.main import build_parser
from discogseek.services.library import LibraryReleaseService
from discogseek.services.library_browser import LibraryBrowserService


def release(identity="edition-1", missing=2):
    tracks = [{"disc_number": 1, "track_number": str(index), "title": f"Track {index}",
               "status": "found" if index == 1 else "missing"}
              for index in range(1, missing + 2)]
    return {"id": identity, "mb_release_id": identity, "artist": "Artist", "title": "Album",
            "tracks": tracks, "is_audited": True, "found_count": 1,
            "missing_count": missing, "completion_pct": 100 / len(tracks)}


def imported(row, *positions):
    fresh = deepcopy(row)
    for position in positions:
        fresh["tracks"][position]["status"] = "found"
    fresh["found_count"] = sum(track["status"] == "found" for track in fresh["tracks"])
    fresh["missing_count"] = len(fresh["tracks"]) - fresh["found_count"]
    fresh["completion_pct"] = 100 * fresh["found_count"] / len(fresh["tracks"])
    return fresh


def queued_result(row, positions=(1,), *, dry_run=False, user="peer"):
    tracks = [row["tracks"][position] for position in positions]
    return {"dry_run": dry_run, "queued_count": 0 if dry_run else len(tracks),
            "matched_tracks": tracks, "queued_tracks": [] if dry_run else tracks,
            "queued_files": [dict(track, user=user,
                                  filename=f"Music\\Album\\{track['track_number']} song.flac")
                             for track in tracks]}


def transfers(*files, user="peer"):
    return [{"username": user, "directories": [{"directory": "Music\\Album", "files": files}]}]


def transfer(number=2, state="Completed, Succeeded", **extra):
    return {"filename": f"Music\\Album\\{number} song.flac", "state": state, **extra}


def remember(tui, row, result=None):
    result = queued_result(row) if result is None else result
    tui.model.update(row)
    tui.model.record_result(row, result)
    tui._remember_downloads(row, result)


def check(tui, service):
    tui._check_downloads(service)
    tui._drain_events()


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(browser.time, "monotonic", lambda: clock.now)
    return clock


@pytest.fixture
def tui(clock):
    return ReleaseBrowser(build_parser().parse_args(["browse"]), service_factory=Mock())


@pytest.fixture
def service():
    return SimpleNamespace(
        release_service=SimpleNamespace(
            slskd_client=SimpleNamespace(get_downloads=Mock(return_value=[]), cancel_download=Mock()),
            download_missing_tracks=Mock(return_value={}),
        ),
        refresh_release=Mock(),
    )


def test_finished_download_updates_status_then_retries_delayed_import(tui, service, clock):
    row = release(missing=1)
    remember(tui, row)
    service.release_service.slskd_client.get_downloads.return_value = transfers(transfer())
    service.refresh_release.side_effect = [deepcopy(row), imported(row, 1)]

    check(tui, service)

    waiting = tui.model.releases[release_key(row)]
    assert tui.model.track_status(waiting, waiting["tracks"][1]) == "downloaded"
    assert waiting["missing_count"] == 1
    assert tui.model.visible() == [waiting]
    assert tui.model.pending(waiting) == []

    clock.now += browser.DOWNLOAD_POLL_INTERVAL
    check(tui, service)

    service.refresh_release.assert_called_once()
    assert tui.model.releases[release_key(row)]["missing_count"] == 1
    clock.now += browser.DOWNLOAD_IMPORT_RETRY_INTERVAL - browser.DOWNLOAD_POLL_INTERVAL
    check(tui, service)

    fresh = tui.model.releases[release_key(row)]
    assert tui.model.track_status(fresh, fresh["tracks"][1]) == "found"
    assert fresh["missing_count"] == 0
    assert fresh["found_count"] == 2
    assert fresh["completion_pct"] == 100
    assert tui.model.visible() == []
    assert service.refresh_release.call_count == 2
    service.release_service.slskd_client.get_downloads.reset_mock()
    service.refresh_release.reset_mock()

    check(tui, service)

    service.release_service.slskd_client.get_downloads.assert_not_called()
    service.refresh_release.assert_not_called()


def test_finished_download_refreshes_real_audit_with_string_discs_and_local_bonus(tui, monkeypatch):
    mb = Mock()
    mb.get_release_by_id.return_value = {
        "id": "edition-1", "title": "Album", "artist-credit": [{"name": "Artist"}],
        "medium-list": [{"position": "1", "track-list": [
            {"number": str(number), "recording": {"id": f"rec-{number}", "title": title}}
            for number, title in enumerate(["First", "Second"], 1)
        ]}],
    }

    def local_track(number, title):
        return {"disc_number": 1, "track_number": str(number), "title": title,
                "artist": "Artist", "source": "local", "status": "found",
                "filename": f"{number} {title}.flac", "path": f"/music/Album/{number} {title}.flac"}

    local = {"id": "local-album", "mb_release_id": "edition-1", "title": "Album",
             "artist": "Artist", "tracks": [local_track(1, "First")]}
    slskd = SimpleNamespace(get_downloads=Mock(return_value=transfers(transfer())))
    release_service = LibraryReleaseService(mb_client=mb, slskd_client=slskd)
    release_service.scan_library_releases = Mock(return_value=[local])
    service = LibraryBrowserService(release_service)
    row = next(service.iter_releases())
    assert row["is_audited"] and row["missing_count"] == 1
    remember(tui, row)
    release_service.scan_library_releases.return_value = [dict(local, tracks=[
        local_track(1, "First"), local_track(2, "Second"), local_track(3, "Bonus"),
    ])]
    put_event = Mock(wraps=tui.events.put)
    monkeypatch.setattr(tui.events, "put", put_event)

    tui._check_downloads(service)

    events = [call.args[0] for call in put_event.call_args_list]
    assert not [payload for kind, payload in events if kind == "download_error" and payload]
    assert any(kind == "release" for kind, _ in events)
    tui._drain_events()
    fresh = tui.model.releases[release_key(row)]
    assert tui.download_error == ""
    assert fresh["found_count"] == 3 and fresh["missing_count"] == 0
    assert [track["title"] for track in fresh["tracks"]] == ["First", "Second", "Bonus"]
    assert tui.model.track_status(fresh, fresh["tracks"][1]) == "found"
    assert release_key(row) not in tui._downloads
    assert tui.model.visible() == []


@pytest.mark.parametrize("state", ["Queued, Remotely", "Queued, Locally", "InProgress", "Completed"])
def test_active_and_unknown_transfers_remain_queued_without_claiming_success(tui, service, state):
    row = release()
    remember(tui, row)
    service.release_service.slskd_client.get_downloads.return_value = transfers(
        transfer(state=state, id="healthy-id", attempts=100),
    )

    check(tui, service)

    service.refresh_release.assert_not_called()
    service.release_service.slskd_client.cancel_download.assert_not_called()
    assert tui.model.track_status(row, row["tracks"][1]) == "queued"
    assert tui.model.pending(row) == [row["tracks"][2]]


@pytest.mark.parametrize("state", ["Completed, Cancelled", "Completed, TimedOut",
                                   "Completed, Errored", "Completed, Rejected",
                                   "Completed, Aborted"])
def test_terminal_failure_restores_missing_track_and_stops_empty_watch(tui, service, state):
    row = release(missing=1)
    remember(tui, row)
    key, identity = release_key(row), track_key(row["tracks"][1])
    # Clear stale successful overlays as well as the queue submission history.
    tui.model.downloaded[key] = {identity}
    get_downloads = service.release_service.slskd_client.get_downloads
    get_downloads.return_value = transfers(transfer(state=state))

    check(tui, service)

    service.refresh_release.assert_not_called()
    assert tui.model.track_status(row, row["tracks"][1]) == "missing"
    assert tui.model.pending(row) == [row["tracks"][1]]
    for history in (tui.model.queued, tui.model.matched, tui.model.downloaded):
        assert identity not in history.get(key, set())
    assert key not in tui._downloads
    assert tui.jobs.empty()
    get_downloads.reset_mock()

    tui._check_downloads(service)

    get_downloads.assert_not_called()
    service.refresh_release.assert_not_called()
    assert tui.events.empty()
    assert tui.jobs.empty()


def test_pressing_d_retries_failed_transfer_without_automatic_resubmission(tui, service):
    row = release(missing=1)
    remember(tui, row)
    service.release_service.slskd_client.get_downloads.return_value = transfers(
        transfer(state="Completed, Errored"),
    )

    check(tui, service)

    assert tui.jobs.empty()
    keys = SimpleNamespace(**{name: index for index, name in enumerate([
        "KEY_ENTER", "KEY_BTAB", "KEY_LEFT", "KEY_RIGHT", "KEY_UP", "KEY_DOWN",
        "KEY_NPAGE", "KEY_PPAGE", "KEY_HOME", "KEY_END",
    ], 1000)})
    tui._handle_key("d", keys, page_size=10)

    kind, payload = tui.jobs.get_nowait()
    selected, queued, only_track, dry_run = payload
    assert kind == "download"
    assert release_key(selected) == release_key(row)
    assert queued == set()
    assert only_track is None and not dry_run
    assert tui.busy
    assert tui.jobs.empty()


def test_failure_only_releases_failed_track_with_other_downloads_in_progress(tui, service):
    row = release(missing=3)
    other = release("edition-2", missing=1)
    remember(tui, row, queued_result(row, positions=(1, 2, 3)))
    remember(tui, other, queued_result(other, user="other-peer"))
    service.release_service.slskd_client.get_downloads.return_value = (
        transfers(transfer(2, "Completed, TimedOut"), transfer(3), transfer(4, "InProgress"))
        + transfers(transfer(), user="other-peer")
    )
    service.refresh_release.side_effect = lambda release, **kwargs: deepcopy(release)

    check(tui, service)

    fresh = tui.model.releases[release_key(row)]
    other_fresh = tui.model.releases[release_key(other)]
    assert [tui.model.track_status(fresh, track) for track in fresh["tracks"]] == [
        "found", "missing", "downloaded", "queued",
    ]
    assert tui.model.track_status(other_fresh, other_fresh["tracks"][1]) == "downloaded"
    assert tui.model.pending(fresh) == [fresh["tracks"][1]]
    assert tui.model.pending(other_fresh) == []
    assert len(tui._downloads[release_key(row)]["files"]) == 2
    assert len(tui._downloads[release_key(other)]["files"]) == 1
    assert service.refresh_release.call_count == 2
    assert tui.jobs.empty()

    tui.model.selected_key = release_key(row)
    tui._download()

    kind, (_, queued, _, _) = tui.jobs.get_nowait()
    assert kind == "download"
    assert queued == {track_key(fresh["tracks"][2]), track_key(fresh["tracks"][3])}


@pytest.mark.parametrize("other", [transfers(transfer(), user="different-peer"),
                                    transfers(transfer(number=20))])
def test_other_users_and_files_cannot_complete_the_tracked_transfer(tui, service, other):
    row = release()
    remember(tui, row)
    service.release_service.slskd_client.get_downloads.return_value = (
        transfers(transfer(state="InProgress")) + other
    )

    check(tui, service)

    service.refresh_release.assert_not_called()
    assert tui.model.track_status(row, row["tracks"][1]) == "queued"


def test_same_release_completions_share_one_refresh_and_later_completion_refreshes_again(tui, service, clock):
    row = release(missing=3)
    remember(tui, row, queued_result(row, positions=(1, 2, 3)))
    get_downloads = service.release_service.slskd_client.get_downloads
    get_downloads.return_value = transfers(transfer(2), transfer(3), transfer(4, "InProgress"))
    service.refresh_release.return_value = imported(row, 1, 2)

    check(tui, service)

    service.refresh_release.assert_called_once()
    fresh = tui.model.releases[release_key(row)]
    assert fresh["missing_count"] == 1
    assert tui.model.track_status(fresh, fresh["tracks"][3]) == "queued"
    get_downloads.return_value = transfers(transfer(2), transfer(3), transfer(4))
    service.refresh_release.return_value = imported(row, 1, 2, 3)

    # A newly finished file refreshes promptly even during the import retry delay.
    clock.now += browser.DOWNLOAD_POLL_INTERVAL
    check(tui, service)

    assert service.refresh_release.call_count == 2
    assert tui.model.releases[release_key(row)]["missing_count"] == 0


def test_missing_transfer_checks_library_without_claiming_success(tui, service, clock):
    row = release(missing=1)
    remember(tui, row)
    service.release_service.slskd_client.get_downloads.return_value = []
    service.refresh_release.side_effect = [deepcopy(row), imported(row, 1)]

    check(tui, service)

    waiting = tui.model.releases[release_key(row)]
    assert tui.model.track_status(waiting, waiting["tracks"][1]) == "queued"
    service.refresh_release.assert_called_once()

    clock.now += browser.DOWNLOAD_POLL_INTERVAL
    check(tui, service)

    service.refresh_release.assert_called_once()
    clock.now += browser.DOWNLOAD_IMPORT_RETRY_INTERVAL - browser.DOWNLOAD_POLL_INTERVAL
    check(tui, service)

    fresh = tui.model.releases[release_key(row)]
    assert tui.model.track_status(fresh, fresh["tracks"][1]) == "found"


def test_normalized_peer_and_path_preserve_requested_track_identity(tui, service):
    row = release()
    row["tracks"][1]["title"] = "Disc 1 Track 02 (Missing)"
    result = queued_result(row, user="peer (fast)")
    result["queued_files"][0]["title"] = "Official title"
    remember(tui, row, result)
    service.release_service.slskd_client.get_downloads.return_value = transfers(
        transfer(filename="Music/Album/2 song.flac"),
    )
    service.refresh_release.return_value = deepcopy(row)

    check(tui, service)

    fresh = tui.model.releases[release_key(row)]
    service.refresh_release.assert_called_once()
    assert tui.model.track_status(fresh, fresh["tracks"][1]) == "downloaded"


def test_preview_matches_never_start_transfer_monitoring(tui, service):
    row = release()
    remember(tui, row, queued_result(row, dry_run=True))

    check(tui, service)

    service.release_service.slskd_client.get_downloads.assert_not_called()
    service.refresh_release.assert_not_called()
    assert tui.model.track_status(row, row["tracks"][1]) == "matched"


@pytest.mark.parametrize("failure_stage", ["poll", "refresh"])
def test_transient_monitor_errors_recover_without_replacing_user_errors(tui, service, failure_stage, clock):
    row = release(missing=1)
    remember(tui, row)
    tui.error = "Previous user operation failed"
    get_downloads = service.release_service.slskd_client.get_downloads
    get_downloads.return_value = transfers(transfer())
    service.refresh_release.return_value = imported(row, 1)
    failing = get_downloads if failure_stage == "poll" else service.refresh_release
    failing.side_effect = RuntimeError("Service temporarily unavailable")

    check(tui, service)

    assert "Service temporarily unavailable" in tui.download_error
    assert tui.error == "Previous user operation failed"
    assert tui.model.releases[release_key(row)]["missing_count"] == 1
    failing.side_effect = None

    if failure_stage == "refresh":
        clock.now += browser.DOWNLOAD_POLL_INTERVAL
        check(tui, service)
        service.refresh_release.assert_called_once()
        assert tui.model.releases[release_key(row)]["missing_count"] == 1
    clock.now += browser.DOWNLOAD_IMPORT_RETRY_INTERVAL
    check(tui, service)

    assert tui.download_error == ""
    assert tui.error == "Previous user operation failed"
    assert tui.model.releases[release_key(row)]["missing_count"] == 0


def test_live_refresh_survives_stale_release_from_background_scan(tui, service):
    row = release(missing=1)
    remember(tui, row)
    tui.scanning = tui.busy = True
    tui.operation = "scan"
    service.release_service.slskd_client.get_downloads.return_value = transfers(transfer())
    service.refresh_release.return_value = imported(row, 1)

    check(tui, service)
    tui.events.put(("release", (None, deepcopy(row))))
    tui._drain_events()

    assert tui.model.releases[release_key(row)]["missing_count"] == 0
    assert tui.scanning and tui.busy
    assert tui.operation == "scan"


def run_recovery(tui, service, row, result=None):
    """Run the already scheduled FIFO job, then stop the worker without real waiting."""
    tui.service_factory.return_value = service
    service.refresh_release.return_value = deepcopy(row)
    service.release_service.download_missing_tracks.return_value = result or {
        "total_missing": 1, "resolved_count": 0, "queued_count": 0, "queued_tracks": [],
    }
    tui.jobs.put(None)
    tui._worker()
    tui._drain_events()


@pytest.mark.parametrize("state", ["Rejected", "TimedOut", "Errored"])
def test_failed_transfer_is_stopped_then_only_its_track_tries_another_source(tui, service, state):
    row = release(missing=3)
    remember(tui, row, queued_result(row, positions=(1, 2)))
    client = service.release_service.slskd_client
    client.get_downloads.return_value = transfers(
        transfer(2, f"Completed, {state}", id="failed-id", attempts=100, exception="Upload unavailable"),
        transfer(3, "Queued, Remotely", id="healthy-id", attempts=100),
        transfer(4, "Completed, Errored", id="unrelated-id", attempts=100),
    )

    check(tui, service)

    client.cancel_download.assert_called_once_with("peer", "failed-id")
    assert state in tui.last_result and "100" in tui.last_result
    assert "Upload unavailable" in tui.last_result and "1/2" in tui.last_result
    assert tui.active_download.recovery
    result = queued_result(row, user="other-peer")
    run_recovery(tui, service, row, result)

    call = service.release_service.download_missing_tracks.call_args.kwargs
    assert call["missing_tracks"] == [row["tracks"][1]]
    assert call["search_scope"] == "track"
    assert call["excluded_sources"] == {("peer", "Music/Album/2 song.flac")}
    assert tui.model.track_status(row, row["tracks"][1]) == "queued"
    assert tui.model.track_status(row, row["tracks"][2]) == "queued"
    assert tui.model.track_status(row, row["tracks"][3]) == "missing"
    assert tui.jobs.empty() and not tui.pending_downloads


@pytest.mark.parametrize("state", ["Cancelled", "Aborted"])
def test_explicitly_stopped_transfer_is_never_cancelled_or_recovered(tui, service, state):
    row = release(missing=1)
    remember(tui, row)
    service.release_service.slskd_client.get_downloads.return_value = transfers(
        transfer(state=f"Completed, {state}", id="transfer-id", attempts=100),
    )

    check(tui, service)

    service.release_service.slskd_client.cancel_download.assert_not_called()
    assert tui.jobs.empty() and not tui.pending_downloads
    assert tui.model.track_status(row, row["tracks"][1]) == "missing"
    assert not tui._failed_sources


@pytest.mark.parametrize("with_id", [False, True])
def test_unsafe_to_stop_transfer_leaves_actionable_error_without_auto_fallback(tui, service, with_id):
    row = release(missing=1)
    remember(tui, row)
    client = service.release_service.slskd_client
    client.get_downloads.return_value = transfers(transfer(
        state="Completed, Rejected", **({"id": "failed-id"} if with_id else {}),
    ))
    if with_id:
        client.cancel_download.side_effect = RuntimeError("slskd unavailable")

    check(tui, service)
    check(tui, service)

    assert "slskd unavailable" in tui.error if with_id else "transfer ID unavailable" in tui.error
    assert "Press d or t" in tui.error
    assert client.cancel_download.call_count == int(with_id)
    assert tui.jobs.empty() and not tui.pending_downloads
    assert tui.model.track_status(row, row["tracks"][1]) == "missing"
    assert tui._failed_sources[release_key(row)] == {("peer", "Music/Album/2 song.flac")}


def test_recovery_budget_stops_after_two_alternative_searches_and_keeps_source_history(tui, service):
    row = release(missing=1)
    remember(tui, row, queued_result(row, user="peer-0"))
    client = service.release_service.slskd_client
    for number in range(3):
        client.get_downloads.return_value = transfers(
            transfer(state="Completed, TimedOut", id=f"failed-{number}"), user=f"peer-{number}",
        )
        check(tui, service)
        if number < 2:
            run_recovery(tui, service, row, queued_result(row, user=f"peer-{number + 1}"))

    assert service.release_service.download_missing_tracks.call_count == 2
    assert client.cancel_download.call_count == 3
    assert tui._recovery_attempts[release_key(row)][track_key(row["tracks"][1])] == 2
    assert len(tui._failed_sources[release_key(row)]) == 3
    assert "stopped after 2 searches" in tui.last_result
    assert tui.model.track_status(row, row["tracks"][1]) == "missing"
    assert tui.jobs.empty() and not tui._downloads


def test_no_alternative_stops_recovery_and_manual_retry_keeps_exclusions(tui, service):
    row = release(missing=1)
    remember(tui, row)
    service.release_service.slskd_client.get_downloads.return_value = transfers(
        transfer(state="Completed, Rejected", id="failed-id"),
    )
    check(tui, service)

    run_recovery(tui, service, row)
    check(tui, service)

    assert "No alternative queued" in tui.last_result
    assert tui.jobs.empty() and not tui._downloads
    assert service.release_service.download_missing_tracks.call_count == 1
    tui._download()
    run_recovery(tui, service, row, queued_result(row, user="other-peer"))

    assert service.release_service.download_missing_tracks.call_count == 2
    assert service.release_service.download_missing_tracks.call_args.kwargs["excluded_sources"] == {
        ("peer", "Music/Album/2 song.flac"),
    }
    assert tui._recovery_attempts[release_key(row)][track_key(row["tracks"][1])] == 1


def test_library_import_before_recovery_suppresses_search(tui, service):
    row = release(missing=1)
    remember(tui, row)
    service.release_service.slskd_client.get_downloads.return_value = transfers(
        transfer(state="Completed, Errored", id="failed-id"),
    )
    check(tui, service)

    run_recovery(tui, service, imported(row, 1))

    service.release_service.download_missing_tracks.assert_not_called()
    fresh = tui.model.releases[release_key(row)]
    assert tui.model.track_status(fresh, fresh["tracks"][1]) == "found"
    assert tui.jobs.empty()


def test_quit_after_failure_poll_discards_automatic_recovery(tui, service):
    row = release(missing=1)
    remember(tui, row)
    service.release_service.slskd_client.get_downloads.return_value = transfers(
        transfer(state="Completed, Errored", id="failed-id"),
    )
    tui._check_downloads(service)
    tui._quit()
    tui._drain_events()

    assert tui.jobs.empty() and not tui.pending_downloads
    service.release_service.download_missing_tracks.assert_not_called()


def test_quit_before_failure_poll_does_not_touch_transfers(tui, service):
    row = release(missing=1)
    remember(tui, row)
    tui._quit()

    check(tui, service)

    service.release_service.slskd_client.get_downloads.assert_not_called()
    service.release_service.slskd_client.cancel_download.assert_not_called()
    assert tui.jobs.empty()


def test_recovery_state_migrates_with_release_identity(tui, service):
    row = release(missing=1)
    remember(tui, row)
    old_key = release_key(row)
    identity = track_key(row["tracks"][1])
    tui._failed_sources[old_key] = {("old-peer", "old/file.flac")}
    tui._recovery_attempts[old_key] = {identity: 1}
    fresh = dict(deepcopy(row), mb_release_id="new-edition")
    service.release_service.slskd_client.get_downloads.return_value = transfers(
        transfer(state="Completed, Rejected", id="failed-id"),
    )
    check(tui, service)

    tui._update_download_watch(fresh, old_key)
    tui._update_release(fresh, old_key)

    assert old_key not in tui._recovery_attempts and old_key not in tui._failed_sources
    assert tui._failed_sources[release_key(fresh)] == {
        ("old-peer", "old/file.flac"), ("peer", "Music/Album/2 song.flac"),
    }
    assert tui._recovery_attempts[release_key(fresh)] == {identity: 1}
    run_recovery(tui, service, fresh)

    assert tui._recovery_attempts[release_key(fresh)] == {identity: 2}
    assert len(service.release_service.download_missing_tracks.call_args.kwargs["excluded_sources"]) == 2


@pytest.mark.parametrize("failure_first", [False, True])
def test_healthy_copy_of_tracked_source_wins_over_older_failure(tui, service, failure_first):
    row = release(missing=1)
    remember(tui, row)
    failed = transfer(state="Completed, TimedOut", id="old-id")
    healthy = transfer(state="Queued, Remotely", id="live-id", attempts=100)
    service.release_service.slskd_client.get_downloads.return_value = transfers(
        *([failed, healthy] if failure_first else [healthy, failed]),
    )

    check(tui, service)

    service.release_service.slskd_client.cancel_download.assert_not_called()
    assert tui.jobs.empty()
    assert tui.model.track_status(row, row["tracks"][1]) == "queued"


def test_recovery_search_error_stops_without_a_polling_loop(tui, service):
    row = release(missing=1)
    remember(tui, row)
    service.release_service.slskd_client.get_downloads.return_value = transfers(
        transfer(state="Completed, Errored", id="failed-id"),
    )
    check(tui, service)
    service.release_service.download_missing_tracks.side_effect = RuntimeError("Search unavailable")

    run_recovery(tui, service, row)
    check(tui, service)

    assert tui.error == "Search unavailable"
    assert tui.jobs.empty() and not tui.pending_downloads and not tui._downloads
    service.release_service.download_missing_tracks.assert_called_once()
