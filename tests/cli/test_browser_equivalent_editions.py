"""Equivalent recording lists share one browser row and download history."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from discogseek.cli.browser import (
    BrowserModel, ReleaseBrowser, download_release, equivalent_release_key, release_key, track_key,
)
from discogseek.cli.main import build_parser
from tests.services.test_browser_recording_coverage import recording_browser
from tests.services.test_library_browser import release, track


def equivalent_editions(titles=("First", "Second", "Third"), missing=2):
    available = [
        track(title, position, recording=f"rec-{position}")
        for position, title in enumerate(titles, 1) if position != missing
    ]
    local = [release(available)]
    remote = [release([
        track(item["title"], item["track_number"], source="navidrome",
              recording=item["mb_rec_ids"][0]) for item in available
    ], release_id="edition-2", source="navidrome")]
    return list(recording_browser(local, remote, titles=titles).iter_releases())


def make_tui():
    return ReleaseBrowser(build_parser().parse_args(["browse"]), service_factory=Mock())


def test_identical_twenty_track_editions_show_one_row_with_shared_queue_state():
    rows = equivalent_editions(tuple(f"Song {index}" for index in range(1, 21)), missing=18)
    assert len(rows) == 2
    assert {row["mb_release_id"] for row in rows} == {"edition-1", "edition-2"}
    assert rows[0]["tracks"][17]["mb_track_id"] != rows[1]["tracks"][17]["mb_track_id"]
    assert all(row["found_count"] == 19 and row["missing_count"] == 1 for row in rows)
    original = deepcopy(rows)
    model = BrowserModel()
    model.update(rows[0])
    model.record_result(rows[0], {"queued_tracks": [rows[0]["tracks"][17]]})
    model.update(rows[1])

    assert len(model.visible()) == 1
    assert model.track_status(model.selected(), model.selected()["tracks"][17]) == "queued"
    assert model.pending(rows[1]) == []
    assert rows == original


def test_single_track_selection_and_submission_survive_equivalent_edition_refresh(tmp_path):
    first, second = equivalent_editions()
    service = SimpleNamespace(refresh_release=Mock(return_value=second), release_service=SimpleNamespace(
        download_missing_tracks=Mock(return_value={"queued_count": 1}),
    ))
    selected = track_key(first["tracks"][1])

    fresh, _ = download_release(service, first, tmp_path, only_track=selected)

    assert fresh["mb_release_id"] == "edition-2"
    assert service.release_service.download_missing_tracks.call_args.kwargs["missing_tracks"] == [second["tracks"][1]]
    service.release_service.download_missing_tracks.reset_mock()
    _, result = download_release(service, second, tmp_path, queued={selected}, only_track=selected)
    assert result["queued_count"] == 0
    service.release_service.download_missing_tracks.assert_not_called()


@pytest.mark.parametrize("action", ["queued", "imported"])
def test_late_sibling_scan_cannot_replace_a_download_refresh(action):
    first, sibling = equivalent_editions()
    tui = make_tui()
    tui.model.update(first)
    tui._start("scan")
    tui.jobs.get_nowait()
    fresh = deepcopy(first)
    result = {"total_missing": 1, "queued_count": 1, "queued_tracks": [first["tracks"][1]]}
    if action == "imported":
        fresh["tracks"][1]["status"] = "found"
        fresh.update(found_count=3, missing_count=0)
        result = {"total_missing": 0, "queued_count": 0, "queued_tracks": []}
    tui.events.put(("result", (release_key(first), fresh, result)))
    tui.events.put(("release", (None, sibling)))
    tui.events.put(("done", "scan"))

    tui._drain_events()

    assert tui.model.releases[release_key(fresh)] is fresh
    if action == "queued":
        assert len(tui.model.visible()) == 1
        assert tui.model.pending(fresh) == []
        assert tui.model.track_status(fresh, fresh["tracks"][1]) == "queued"
    else:
        assert tui.model.visible() == []


@pytest.mark.parametrize("difference", [
    "recording", "position", "disc", "bonus", "artist", "title", "unverified",
    "unknown-recording", "duplicate-position", "unknown-position",
])
def test_distinct_or_unproven_editions_remain_separate(difference):
    first, second = equivalent_editions()
    if difference == "recording":
        second["tracks"][1]["mb_recording_id"] = "other-recording"
    elif difference == "position":
        second["tracks"][1]["track_number"] = "4"
    elif difference == "disc":
        second["tracks"][1]["disc_number"] = 2
    elif difference == "bonus":
        bonus = deepcopy(second["tracks"][1])
        bonus.update(track_number="4", title="Bonus", mb_recording_id="bonus-recording")
        second["tracks"].append(bonus)
        second["missing_count"] += 1
    elif difference == "artist":
        second["artist"] = second["album_artist"] = "Other Artist"
    elif difference == "title":
        second["title"] = second["mb_release_title"] = "Other Album"
    elif difference == "unverified":
        second["is_audited"] = False
    elif difference == "unknown-recording":
        second["tracks"][1]["mb_recording_id"] = None
    elif difference == "duplicate-position":
        second["tracks"][1]["track_number"] = second["tracks"][0]["track_number"]
    else:
        second["tracks"][1]["track_number"] = ""
    model = BrowserModel()
    model.show_unverified = True
    model.update(first)
    model.update(second)

    assert len(model.visible()) == 2
    assert equivalent_release_key(first) != equivalent_release_key(second)


def test_position_normalization_preserves_equivalence_and_repeated_recordings():
    first, second = equivalent_editions()
    for item in second["tracks"]:
        item["disc_number"] = str(item["disc_number"])
        item["track_number"] = str(item["track_number"]).zfill(2)
    second["tracks"].reverse()
    assert equivalent_release_key(first) == equivalent_release_key(second)
    assert release_key(first) == "edition-1"
    assert release_key(second) == "edition-2"
    repeated = deepcopy(first["tracks"][0])
    repeated["track_number"] = "2"
    assert track_key(first["tracks"][0]) != track_key(repeated)


def test_imported_extra_local_track_preserves_history_and_blocks_stale_scan():
    old, _ = equivalent_editions()
    tui = make_tui()
    tui.model.update(old)
    tui.model.selected()
    tui.model.track_index = 1
    missing = old["tracks"][1]
    queued = {"queued_tracks": [missing], "queued_files": [
        {"user": "peer", "filename": "Album/Second.flac"},
    ]}
    tui.model.record_result(old, queued)
    tui._remember_downloads(old, queued)
    old_key = release_key(old)
    fresh = deepcopy(old)
    fresh["tracks"].append({"disc_number": 1, "track_number": "4", "title": "Local bonus",
                            "path": "/music/Album/Local bonus.flac", "status": "found"})
    fresh["found_count"] += 1
    assert release_key(fresh) == old_key
    assert equivalent_release_key(fresh) != equivalent_release_key(old)
    tui._start("scan")
    tui.jobs.get_nowait()
    tui._update_download_watch(fresh, old_key)
    tui.events.put(("release", (old_key, fresh)))
    tui.events.put(("release", (None, deepcopy(old))))
    tui.events.put(("done", "scan"))

    tui._drain_events()

    assert len(tui.model.releases) == 1
    assert tui.model.selected() is fresh
    assert tui.model.track_index == 1
    assert tui.model.track_status(fresh, missing) == "queued"
    assert tui.model.pending(fresh) == []
    assert old_key in tui.model.queued
    assert old_key in tui._downloads
    assert tui._download_key(old_key) == release_key(fresh)
    assert tui._submitted_tracks[release_key(fresh)] == {track_key(missing)}


def test_equivalent_edition_rescan_keeps_queue_history():
    first, second = equivalent_editions()
    model = BrowserModel()
    model.update(first)
    model.record_result(first, {"queued_tracks": [first["tracks"][1]]})
    model.releases.clear()
    model.update(second)

    assert model.pending(second) == []
    assert model.track_status(second, second["tracks"][1]) == "queued"


def test_later_scan_audit_replaces_same_edition_after_recording_list_changes():
    old, _ = equivalent_editions()
    tui = make_tui()
    tui.model.update(old)
    tui.model.selected()
    tui.model.track_index = 1
    tui.model.record_result(old, {"queued_tracks": [old["tracks"][1]]})
    fresh = deepcopy(old)
    bonus = deepcopy(fresh["tracks"][1])
    bonus.update(track_number="4", title="Official bonus", mb_recording_id="bonus-recording")
    fresh["tracks"].append(bonus)
    fresh["missing_count"] += 1
    assert release_key(fresh) == release_key(old)
    assert equivalent_release_key(fresh) != equivalent_release_key(old)
    tui.events.put(("release", (None, fresh)))

    tui._drain_events()

    assert len(tui.model.releases) == 1
    assert tui.model.selected() is fresh
    assert tui.model.track_index == 1
    assert tui.model.track_status(fresh, fresh["tracks"][1]) == "queued"
    assert tui.model.pending(fresh) == [bonus]


def test_sibling_scan_during_active_request_cannot_dispatch_duplicate_download():
    first, second = equivalent_editions()
    tui = make_tui()
    tui.model.update(first)
    tui.model.selected()
    tui.model.track_index = 1
    tui._download(single=True)
    tui.events.put(("release", (None, second)))
    tui._drain_events()
    tui._download(single=True)
    assert tui.jobs.qsize() == 1
    assert not tui.pending_downloads
    service = SimpleNamespace(refresh_release=Mock(return_value=second), release_service=SimpleNamespace(
        download_missing_tracks=Mock(return_value={
            "total_missing": 1, "resolved_count": 1, "queued_count": 1,
            "queued_tracks": [second["tracks"][1]],
        }),
    ))
    tui.service_factory.return_value = service
    tui.jobs.put(None)

    tui._worker()
    tui._drain_events()
    tui._download(single=True)

    service.release_service.download_missing_tracks.assert_called_once()
    assert len(tui.model.visible()) == 1
    assert tui.model.track_status(second, second["tracks"][1]) == "queued"
    assert tui.jobs.empty()
    assert not tui.pending_downloads


def test_corrected_sibling_tracklist_splits_rows_without_losing_original_edition():
    first, second = equivalent_editions()
    model = BrowserModel()
    model.update(first)
    model.update(second)
    model.record_result(first, {"queued_tracks": [first["tracks"][1]]})
    assert len(model.visible()) == 1
    corrected = deepcopy(second)
    corrected["tracks"][1]["mb_recording_id"] = "different-version"

    model.update(corrected)

    assert len(model.visible()) == 2
    assert model.releases["edition-1"] is first
    assert model.releases["edition-2"] is corrected
    assert model.track_status(first, first["tracks"][1]) == "queued"
    assert model.track_status(corrected, corrected["tracks"][1]) == "missing"
    assert model.pending(corrected) == [corrected["tracks"][1]]


def test_sibling_request_does_not_swallow_failed_track_recovery(monkeypatch):
    monkeypatch.setattr("discogseek.cli.browser.time.monotonic", lambda: 100.0)
    first, second = equivalent_editions()
    for row in (first, second):
        row["tracks"][2]["status"] = "missing"
        row.update(found_count=1, missing_count=2)
    tui = make_tui()
    failed_track = first["tracks"][1]
    queued = {"queued_tracks": [failed_track], "queued_files": [
        {"user": "peer", "filename": "Album/Second.flac"},
    ]}
    tui.model.update(first)
    tui.model.record_result(first, queued)
    tui._remember_downloads(first, queued)
    # A rescan can choose the sibling while the first edition still downloads.
    tui.model.releases.clear()
    tui.model.update(second)
    tui._download()
    assert tui.active_download.release["mb_release_id"] == "edition-2"
    client = Mock()
    client.get_downloads.return_value = [{"username": "peer", "directories": [{"files": [
        {"filename": "Album/Second.flac", "state": "Completed, TimedOut", "id": "failed"},
    ]}]}]
    service = SimpleNamespace(refresh_release=Mock(return_value=second), release_service=SimpleNamespace(
        slskd_client=client,
        download_missing_tracks=Mock(return_value={
            "total_missing": 1, "resolved_count": 1, "queued_count": 1,
            "queued_tracks": [second["tracks"][2]],
        }),
    ))
    tui.service_factory.return_value = service
    # The poll invalidates the queued snapshot already dispatched for the sibling.
    tui._check_downloads(service)
    tui.jobs.put(None)
    tui._worker()
    tui._drain_events()

    assert service.release_service.download_missing_tracks.call_args.kwargs["missing_tracks"] == [second["tracks"][2]]
    client.cancel_download.assert_called_once_with("peer", "failed")
    assert tui.active_download is not None
    assert tui.active_download.recovery
    assert tui.active_download.release["mb_release_id"] == "edition-1"
    assert tui.active_download.only_track == track_key(failed_track)

    service.refresh_release.return_value = first
    service.release_service.download_missing_tracks.return_value = {
        "total_missing": 1, "resolved_count": 1, "queued_count": 1,
        "queued_tracks": [failed_track],
    }
    tui.jobs.put(None)
    tui._worker()
    tui._drain_events()

    retry = service.release_service.download_missing_tracks.call_args.kwargs
    assert retry["missing_tracks"] == [failed_track]
    assert retry["excluded_sources"] == {("peer", "Album/Second.flac")}
    assert tui.model.track_status(second, second["tracks"][1]) == "queued"
    assert not tui.pending_downloads
    assert tui.active_download is None
