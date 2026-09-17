"""Live transfer completion and eventual library import without real services."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from discogseek.cli import browser
from discogseek.cli.browser import ReleaseBrowser, release_key
from discogseek.cli.main import build_parser


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
        release_service=SimpleNamespace(slskd_client=SimpleNamespace(get_downloads=Mock(return_value=[]))),
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


@pytest.mark.parametrize("state", ["Queued, Remotely", "InProgress", "Completed",
                                   "Completed, Cancelled", "Completed, TimedOut",
                                   "Completed, Errored", "Completed, Rejected",
                                   "Completed, Aborted"])
def test_active_and_failed_transfers_do_not_refresh_or_claim_success(tui, service, state):
    row = release()
    remember(tui, row)
    service.release_service.slskd_client.get_downloads.return_value = transfers(transfer(state=state))

    check(tui, service)

    service.refresh_release.assert_not_called()
    assert tui.model.track_status(row, row["tracks"][1]) == "queued"


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
