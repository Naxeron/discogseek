"""Offline coverage of release selection, refreshes, and safe download dispatch."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from discogseek.cli import browser
from discogseek.cli.browser import BrowserModel, ReleaseBrowser, download_release, release_key, track_key
from discogseek.cli.main import build_parser, main
from discogseek.clients.musicbrainz import MusicBrainzClient
from discogseek.clients.navidrome import NavidromeScanner
from discogseek.clients.slskd import SlskdClient
from discogseek.config import Config
from discogseek.core.audio import AudioMetadata, AudioQualityAnalyzer
from discogseek.services import library
from discogseek.services.library_browser import LibraryBrowserService


def track(number, title, status="missing", disc=1):
    return {"disc_number": disc, "track_number": str(number), "title": title, "status": status}


def release(identity="release-1", artist="Artist", title="Album", tracks=None, audited=True):
    tracks = tracks if tracks is not None else [track(1, "One", "found"), track(2, "Two")]
    return {"id": identity, "artist": artist, "title": title, "tracks": tracks,
            "is_audited": audited, "missing_count": sum(t["status"] == "missing" for t in tracks)}


@pytest.fixture
def fake_service():
    service = SimpleNamespace(refresh_release=Mock(), release_service=SimpleNamespace(
        download_missing_tracks=Mock(return_value={"queued_count": 1}),
    ))
    service.refresh_release.return_value = release()
    return service


@pytest.fixture
def tui():
    return ReleaseBrowser(build_parser().parse_args(["browse"]), service_factory=Mock())


@pytest.fixture
def terminal_keys():
    return SimpleNamespace(**{name: index for index, name in enumerate([
        "KEY_ENTER", "KEY_BACKSPACE", "KEY_BTAB", "KEY_LEFT", "KEY_RIGHT", "KEY_UP",
        "KEY_DOWN", "KEY_NPAGE", "KEY_PPAGE", "KEY_HOME", "KEY_END",
    ], 1000)})


def test_filter_shows_only_incomplete_releases_across_artists():
    model = BrowserModel()
    incomplete = release("z", artist="Zebra")
    accented = release("b", artist="Björk")
    earlier_album = release("a", artist="Björk", title="A")
    complete = release("complete", tracks=[track(1, "One", "found")])
    unverified = release("unknown", artist="Björk", audited=False)
    for row in (incomplete, accented, complete, earlier_album, unverified):
        model.update(row)

    assert model.visible() == [earlier_album, accented, incomplete]
    model.artist_filter = "BJORK"
    assert model.visible() == [earlier_album, accented]
    model.show_unverified = True
    assert {release_key(row) for row in model.visible()} == {"a", "b", "unknown"}
    model.artist_filter = "absent"
    assert model.visible() == []
    assert model.selected() is None


@pytest.mark.parametrize("credit_source", ["album_artist", "track_artist"])
def test_artist_filter_includes_compilation_and_album_credits(credit_source):
    model = BrowserModel("BJORK")
    row = release(artist="Various Artists")
    if credit_source == "album_artist":
        row["album_artist"] = "Björk"
    else:
        row["tracks"][1]["artist"] = "Björk"
    model.update(row)
    model.update(release("other", artist="Someone Else"))

    assert model.visible() == [row]


def test_navigation_clamps_and_filter_changes_rebind_selection():
    model = BrowserModel()
    first = release("first", artist="A", tracks=[track(i, str(i)) for i in range(1, 4)])
    second = release("second", artist="B")
    model.update(second)
    model.update(first)

    assert model.selected() is first
    model.move(100, tracks=True)
    assert model.track_index == 2
    model.move(1)
    assert model.selected() is second
    assert model.track_index == 0
    model.move(100)
    assert model.selected() is second
    model.move(-100)
    assert model.selected() is first
    model.move(-100, tracks=True)
    assert model.track_index == 0
    model.artist_filter = "B"
    assert model.selected() is second
    model.artist_filter = "absent"
    model.move(1, tracks=True)
    assert model.selected() is None
    assert model.track_index == 0


def test_refresh_rebinds_selection_and_submission_history_when_release_key_changes():
    model = BrowserModel()
    old = release(identity=None)
    old_key = release_key(old)
    model.update(old)
    assert model.selected() is old
    model.track_index = 1
    model.record_result(old, {"queued_tracks": [old["tracks"][1]], "matched_tracks": old["tracks"]})
    fresh = deepcopy(old)
    fresh.update(id="resolved-release", mb_release_id="mb-release")

    model.update(fresh, old_key=old_key)

    assert old_key not in model.releases
    assert old_key not in model.queued
    assert old_key not in model.matched
    assert model.selected() is fresh
    assert model.track_index == 1
    assert model.track_status(fresh, fresh["tracks"][1]) == "queued"
    assert model.pending(fresh) == []


def test_queued_is_distinct_from_found_and_partial_failures_remain_retryable():
    model = BrowserModel()
    row = release(tracks=[track(1, "One", "found"), track(2, "Two"), track(3, "Three")])
    model.update(row)
    model.record_result(row, {"matched_tracks": row["tracks"][1:], "queued_tracks": [row["tracks"][1]]})

    assert [model.track_status(row, item) for item in row["tracks"]] == ["found", "queued", "matched"]
    assert model.pending(row) == [row["tracks"][2]]
    assert row["tracks"][1]["status"] == "missing"
    fresh = deepcopy(row)
    fresh["tracks"][1]["status"] = "found"
    model.update(fresh)
    assert model.track_status(fresh, fresh["tracks"][1]) == "found"


def test_track_identity_preserves_same_title_on_different_discs():
    model = BrowserModel()
    first, second = track(1, "Theme", disc=1), track(1, "Theme", disc=2)
    row = release(tracks=[first, second])
    model.update(row)
    model.record_result(row, {"queued_tracks": [first]})

    assert model.pending(row, only_track=track_key(first)) == []
    assert model.pending(row, only_track=track_key(second)) == [second]
    assert model.track_status(row, second) == "missing"


def test_full_rescan_keeps_queue_history_when_browser_id_changes():
    model = BrowserModel()
    old = dict(release("original-browser-id"), mb_release_id="edition-1")
    model.update(old)
    model.record_result(old, {"queued_tracks": [old["tracks"][1]]})
    model.releases.clear()
    refreshed = dict(deepcopy(old), id="new-browser-id")
    other_edition = dict(deepcopy(old), id="other-browser-id", mb_release_id="edition-2")
    model.update(refreshed)
    model.update(other_edition)

    assert len(model.visible()) == 2
    assert model.pending(refreshed) == []
    assert model.track_status(refreshed, refreshed["tracks"][1]) == "queued"
    assert model.pending(other_edition) == [other_edition["tracks"][1]]


@pytest.mark.parametrize("dry_run", [False, True])
def test_download_refreshes_inventory_then_selects_only_missing_unqueued_tracks(fake_service, tmp_path, dry_run):
    stale = release(tracks=[track(i, str(i)) for i in range(1, 4)])
    fresh = deepcopy(stale)
    fresh["tracks"][0]["status"] = "found"
    fresh["mb_release_title"] = "Verified Edition"
    fake_service.refresh_release.return_value = fresh
    calls = []
    fake_service.refresh_release.side_effect = lambda *a, **k: (calls.append("refresh"), fresh)[1]
    fake_service.release_service.download_missing_tracks.side_effect = lambda **k: (calls.append("download"), {"dry_run": k["dry_run"]})[1]
    progress = Mock()

    actual, result = download_release(
        fake_service, stale, tmp_path, queued={track_key(fresh["tracks"][1])},
        preferred_format="mp3-320", search_timeout=12.5, dry_run=dry_run, on_progress=progress,
    )

    assert calls == ["refresh", "download"]
    assert actual is fresh
    assert result["dry_run"] is dry_run
    fake_service.refresh_release.assert_called_once_with(stale, library_dir=tmp_path)
    fake_service.release_service.download_missing_tracks.assert_called_once_with(
        artist="Artist", release_title="Verified Edition", missing_tracks=[fresh["tracks"][2]],
        preferred_format="mp3-320", search_timeout=12.5, dry_run=dry_run, on_progress=progress,
        search_scope="release",
    )


def test_single_track_download_uses_disc_and_position_after_refresh(fake_service, tmp_path):
    row = release(tracks=[track(1, "Theme", disc=1), track(1, "Theme", disc=2)])
    fake_service.refresh_release.return_value = deepcopy(row)

    download_release(fake_service, row, tmp_path, only_track=track_key(row["tracks"][1]))

    submitted = fake_service.release_service.download_missing_tracks.call_args.kwargs["missing_tracks"]
    assert submitted == [row["tracks"][1]]
    assert fake_service.release_service.download_missing_tracks.call_args.kwargs["search_scope"] == "track"


@pytest.mark.parametrize("dry_run", [False, True])
def test_download_skips_search_when_refresh_finds_selected_track(fake_service, tmp_path, dry_run):
    old = release()
    fresh = deepcopy(old)
    fresh["tracks"][1]["status"] = "found"
    fake_service.refresh_release.return_value = fresh

    actual, result = download_release(fake_service, old, tmp_path, dry_run=dry_run)

    assert actual is fresh
    assert result["total_missing"] == result["queued_count"] == result["matched_count"] == 0
    assert result["queued_tracks"] == result["matched_tracks"] == []
    assert result["dry_run"] is dry_run
    fake_service.release_service.download_missing_tracks.assert_not_called()


def test_download_does_not_search_unverified_release(fake_service, tmp_path):
    row = release()
    fake_service.refresh_release.return_value = dict(row, is_audited=False, audit_error="MusicBrainz unavailable")

    with pytest.raises(ValueError, match="MusicBrainz unavailable"):
        download_release(fake_service, row, tmp_path)

    fake_service.release_service.download_missing_tracks.assert_not_called()


def test_download_does_not_search_when_remote_inventory_fails(fake_service, tmp_path):
    fake_service.refresh_release.side_effect = RuntimeError("Navidrome scan failed")

    with pytest.raises(RuntimeError, match="Navidrome scan failed"):
        download_release(fake_service, release(), tmp_path)

    fake_service.release_service.download_missing_tracks.assert_not_called()


@pytest.mark.parametrize("single,preview,global_dry_run", [(True, False, False), (False, True, False), (False, False, True)])
def test_download_action_captures_selection_and_preview_mode(tui, single, preview, global_dry_run):
    row = release(tracks=[track(1, "Theme", disc=1), track(1, "Theme", disc=2)])
    tui.model.update(row)
    tui.model.selected()
    tui.model.track_index = 1
    tui.args.dry_run = global_dry_run

    tui._download(single=single, preview=preview)
    kind, (submitted, queued, only_track, dry_run) = tui.jobs.get_nowait()
    row["tracks"][1]["title"] = "Changed after dispatch"

    assert kind == "download"
    assert submitted["tracks"][1]["title"] == "Theme"
    assert queued == set()
    assert only_track == (track_key(submitted["tracks"][1]) if single else None)
    assert dry_run is (preview or global_dry_run)
    assert tui.busy


def test_busy_browser_rejects_duplicate_download_jobs(tui):
    row = release()
    tui.model.update(row)

    tui._download()
    tui._download()

    assert tui.jobs.qsize() == 1
    assert "in progress" in tui.message


@pytest.mark.parametrize("key,single,preview", [
    ("d", False, False), ("t", True, False), ("p", False, True), ("P", True, True),
])
def test_download_menu_dispatches_both_scopes_and_previews(tui, terminal_keys, key, single, preview):
    row = release(tracks=[track(1, "One"), track(2, "Two")])
    tui.model.update(row)
    tui.model.selected()
    tui.model.track_index = 1

    tui._handle_key("\n", terminal_keys, page_size=10)

    assert tui.overlay == "downloads"
    assert tui.jobs.empty()
    tui._handle_key(key, terminal_keys, page_size=10)
    kind, (_, _, only_track, dry_run) = tui.jobs.get_nowait()
    assert kind == "download"
    assert only_track == (track_key(row["tracks"][1]) if single else None)
    assert dry_run is preview
    assert tui.overlay is None


def test_download_menu_keyboard_choice_and_cancel(tui, terminal_keys):
    row = release(tracks=[track(1, "One"), track(2, "Two")])
    tui.model.update(row)
    tui._handle_key("\n", terminal_keys, page_size=10)
    tui._handle_key("\x1b", terminal_keys, page_size=10)
    assert tui.jobs.empty()
    assert tui.overlay is None

    tui._handle_key("\t", terminal_keys, page_size=10)
    tui._handle_key("j", terminal_keys, page_size=10)
    tui._handle_key("\n", terminal_keys, page_size=10)
    assert tui.action_index == 1
    tui._handle_key("j", terminal_keys, page_size=10)
    tui._handle_key("j", terminal_keys, page_size=10)
    tui._handle_key("\n", terminal_keys, page_size=10)
    _, (_, _, only_track, dry_run) = tui.jobs.get_nowait()
    assert only_track == track_key(row["tracks"][1])
    assert dry_run is True


def test_selected_found_track_explains_how_to_select_a_missing_track(tui, terminal_keys):
    tui.model.update(release())
    tui._handle_key("t", terminal_keys, page_size=10)
    assert tui.jobs.empty()
    assert "already in your library" in tui.message
    assert "Tab" in tui.message


@pytest.mark.parametrize("size", [(14, 72), (24, 100)])
def test_download_actions_and_menu_are_visible_at_supported_terminal_sizes(tui, terminal_keys, size):
    class Screen:
        def __init__(self):
            self.rows = {}

        def getmaxyx(self):
            return size

        def erase(self):
            self.rows.clear()

        def addstr(self, y, x, text, attr):
            assert 0 <= y < size[0]
            assert x + len(text) < size[1]
            row = self.rows.get(y, " " * size[1])
            self.rows[y] = row[:x] + text + row[x + len(text):]

        def refresh(self):
            pass

    curses = SimpleNamespace(**vars(terminal_keys), A_BOLD=1, A_REVERSE=2, A_DIM=4, error=RuntimeError)
    screen = Screen()
    tui.model.update(release())
    tui._draw(screen, curses)
    footer = screen.rows[size[0] - 3]
    assert "d Download all missing" in footer
    assert "t Download selected track" in footer
    assert "Enter options" in footer

    tui._handle_key("\n", terminal_keys, page_size=10)
    tui._draw(screen, curses)
    drawn = "\n".join(screen.rows.values())
    assert "DOWNLOAD OPTIONS" in drawn
    assert "Download all missing tracks (1 remaining)" in drawn
    assert "Download selected track only" in drawn
    assert "Preview all missing tracks" in drawn
    assert "Preview selected track only" in drawn
    tui._handle_key("\x1b", terminal_keys, page_size=10)
    tui._handle_key("?", terminal_keys, page_size=10)
    tui._draw(screen, curses)
    assert "KEYBOARD HELP" in "\n".join(screen.rows.values())
    tui._handle_key("\n", terminal_keys, page_size=10)
    assert tui.overlay is None


def test_keyboard_artist_filter_and_track_selection_need_no_new_scan(tui, terminal_keys):
    keys = terminal_keys
    target = release("target", artist="Björk")
    tui.model.update(release("other", artist="Other"))
    tui.model.update(target)
    for key in ["/", *"BJORKx", keys.KEY_BACKSPACE, "\n", "\t", "j"]:
        tui._handle_key(key, keys, page_size=10)

    assert tui.model.selected() is target
    assert tui.model.artist_filter == "BJORK"
    assert tui.focus_tracks
    assert tui.model.track_index == 1
    assert tui.jobs.empty()
    tui._handle_key("t", keys, page_size=10)
    kind, payload = tui.jobs.get_nowait()
    assert kind == "download"
    assert payload[2] == track_key(target["tracks"][1])
    tui._handle_key("\x1b", keys, page_size=10)
    assert len(tui.model.visible()) == 2


def test_worker_preserves_incremental_results_after_failure_and_accepts_next_job(tui):
    initial = release()
    fresh = dict(initial, title="Refreshed album")

    def interrupted_scan(**kwargs):
        yield initial
        tui.jobs.put(("refresh", initial))
        raise RuntimeError("Connection lost during audit")

    def refresh_after_failure(*args, **kwargs):
        tui.jobs.put(None)
        return fresh

    service = SimpleNamespace(iter_releases=Mock(side_effect=interrupted_scan),
                              refresh_release=Mock(side_effect=refresh_after_failure))
    tui.service_factory.return_value = service
    tui._start("scan")

    tui._worker()
    tui._drain_events()

    assert tui.model.selected() == fresh
    assert tui.error == "Connection lost during audit"
    assert not tui.busy
    tui.service_factory.assert_called_once_with()
    service.refresh_release.assert_called_once_with(initial, library_dir=tui.args.music_dir, force_refresh=True)


def test_worker_prioritizes_download_between_audits_then_resumes_scan(tui, monkeypatch):
    first, second = release("first", title="First album"), release("second", title="Second album")
    actions = []

    def incremental_scan(**kwargs):
        actions.append("audit first")
        yield first
        actions.append("audit second")
        yield second
        tui.jobs.put(None)

    def refresh_selected(row, **kwargs):
        actions.append("refresh first")
        return deepcopy(row)

    def download_selected(**kwargs):
        actions.append("download first")
        return {"total_missing": 1, "resolved_count": 1, "queued_count": 1,
                "queued_tracks": kwargs["missing_tracks"]}

    service = SimpleNamespace(iter_releases=Mock(side_effect=incremental_scan),
                              refresh_release=Mock(side_effect=refresh_selected),
                              release_service=SimpleNamespace(download_missing_tracks=Mock(side_effect=download_selected)))
    tui.service_factory.return_value = service
    put_event = tui.events.put

    def consume_event(event):
        put_event(event)
        tui._drain_events()
        if event[0] == "release" and event[1][1] is first:
            assert tui.scanning and tui.busy
            tui._download()
            tui._download()
            assert tui.jobs.qsize() == 1
        elif event == ("done", "download"):
            assert tui.scanning and tui.busy
            assert tui.operation == "scan"

    monkeypatch.setattr(tui.events, "put", consume_event)
    tui._start("scan")

    tui._worker()

    assert actions == ["audit first", "refresh first", "download first", "audit second"]
    assert len(tui.model.visible()) == 2
    assert tui.model.pending(first) == []
    assert not tui.scanning and not tui.busy


def test_worker_checks_due_downloads_before_waiting_jobs(tui, monkeypatch):
    row = release()
    tui._remember_downloads(row, {
        "queued_files": [{"user": "peer", "filename": "Album/02 Two.flac"}],
        "queued_tracks": [row["tracks"][1]],
    })
    actions = []
    clock = [0]
    monkeypatch.setattr(browser.time, "monotonic", lambda: clock[0])
    service = SimpleNamespace(refresh_release=Mock(side_effect=lambda *a, **k: (
        actions.append("user refresh"), row,
    )[1]))
    def create_service():
        clock[0] = browser.DOWNLOAD_POLL_INTERVAL
        return service

    tui.service_factory.side_effect = create_service

    def check_downloads(actual_service):
        assert actual_service is service
        actions.append("download check")
        tui.jobs.put(None)

    monkeypatch.setattr(tui, "_check_downloads", check_downloads)
    # Initialize the service before the refresh; it makes the poll overdue.
    service.iter_releases = Mock(return_value=iter(()))
    tui.jobs.put(("scan", None))
    tui._start("refresh", row)

    tui._worker()
    tui._drain_events()

    assert actions == ["download check", "user refresh"]
    assert not tui.busy
    assert tui.error == ""


def test_worker_monitors_new_submissions_until_library_confirms_completion(tui, monkeypatch):
    row = release()
    clock = [0]
    monkeypatch.setattr(browser.time, "monotonic", lambda: clock[0])
    filename = "Album/02 Two.flac"

    def queue_download(**kwargs):
        clock[0] = browser.DOWNLOAD_POLL_INTERVAL
        return {"total_missing": 1, "resolved_count": 1, "queued_count": 1,
                "queued_tracks": kwargs["missing_tracks"],
                "queued_files": [{"user": "peer", "filename": filename}]}

    def refresh_release(release, **kwargs):
        if clock[0] == 0:
            return deepcopy(release)
        fresh = deepcopy(release)
        fresh["tracks"][1]["status"] = "found"
        fresh["missing_count"] = 0
        tui.jobs.put(None)
        return fresh

    client = SimpleNamespace(get_downloads=Mock(return_value=[{
        "username": "peer", "directories": [{"files": [
            {"filename": filename, "state": "Completed, Succeeded"},
        ]}],
    }]))
    service = SimpleNamespace(refresh_release=Mock(side_effect=refresh_release),
                              release_service=SimpleNamespace(
                                  download_missing_tracks=Mock(side_effect=queue_download), slskd_client=client))
    tui.service_factory.return_value = service
    tui.model.update(row)
    tui._download()

    tui._worker()
    tui._drain_events()

    assert service.refresh_release.call_count == 2
    client.get_downloads.assert_called_once_with()
    assert tui.model.track_status(row, row["tracks"][1]) == "downloaded"
    fresh = tui.model.releases[release_key(row)]
    assert tui.model.track_status(fresh, fresh["tracks"][1]) == "found"
    assert tui.model.visible() == []
    assert tui._downloads == {}
    assert not tui.busy and not tui.error


def test_download_completion_between_audits_survives_stale_scan(tui, monkeypatch):
    old = release()
    fresh = deepcopy(old)
    fresh["tracks"][1]["status"] = "found"
    fresh["missing_count"] = 0
    tui.model.update(old)
    result = {"queued_files": [{"user": "peer", "filename": "Album/02 Two.flac"}],
              "queued_tracks": [old["tracks"][1]]}
    tui.model.record_result(old, result)
    tui._remember_downloads(old, result)
    clock = [0]
    monkeypatch.setattr(browser.time, "monotonic", lambda: clock[0])

    def scan(**kwargs):
        clock[0] = 3
        yield old
        # This inventory was captured before the download finished.
        yield deepcopy(old)
        tui.jobs.put(None)

    client = SimpleNamespace(get_downloads=Mock(return_value=[{
        "username": "peer", "directories": [{"files": [
            {"filename": "Album/02 Two.flac", "state": "Completed, Succeeded"},
        ]}],
    }]))
    service = SimpleNamespace(iter_releases=scan, refresh_release=Mock(return_value=fresh),
                              release_service=SimpleNamespace(slskd_client=client))
    tui.service_factory.return_value = service
    put = tui.events.put

    def consume(event):
        put(event)
        tui._drain_events()
        if event[0] == "release":
            assert tui.busy and tui.scanning

    monkeypatch.setattr(tui.events, "put", consume)
    tui._start("scan")

    tui._worker()

    client.get_downloads.assert_called_once_with()
    service.refresh_release.assert_called_once_with(old, library_dir=tui.args.music_dir)
    assert tui.model.releases[release_key(old)] == fresh
    assert tui.model.visible() == []
    assert not tui.busy and not tui.scanning
    assert tui._downloads == {}


def test_worker_quits_without_polling_pending_downloads(tui, monkeypatch):
    row = release()
    tui._remember_downloads(row, {
        "queued_files": [{"user": "peer", "filename": "Album/02 Two.flac"}],
        "queued_tracks": [row["tracks"][1]],
    })
    check = Mock()
    monkeypatch.setattr(tui, "_check_downloads", check)
    tui._quit()
    tui.jobs.put(None)

    tui._worker()

    check.assert_not_called()
    tui.service_factory.assert_not_called()


@pytest.mark.parametrize("action", ["refresh", "download"])
def test_explicit_refresh_survives_stale_scan_but_next_rescan_can_update_it(tui, terminal_keys, action):
    old = dict(release("original-browser-id"), mb_release_id="edition-1")
    fresh = dict(deepcopy(old), id="refreshed-browser-id", missing_count=0)
    fresh["tracks"][1]["status"] = "found"
    stale = dict(deepcopy(old), id="stale-browser-id")
    tui.model.update(old)
    tui._start("scan")
    tui.jobs.get_nowait()
    tui._start(action, old)
    tui.jobs.get_nowait()
    if action == "refresh":
        tui.events.put(("release", (release_key(old), fresh)))
    else:
        tui.events.put(("result", (release_key(old), fresh, {
            "total_missing": 0, "queued_count": 0, "queued_tracks": [],
        })))
    tui.events.put(("done", action))
    tui.events.put(("release", (None, stale)))
    tui.events.put(("done", "scan"))

    tui._drain_events()

    assert tui.model.releases["edition-1"] is fresh
    assert tui.model.visible() == []
    assert not tui.busy and not tui.scanning
    assert "edition-1" in tui.refreshed_during_scan
    tui._handle_key("R", terminal_keys, page_size=10)
    assert tui.jobs.get_nowait() == ("scan", None)
    assert tui.refreshed_during_scan == set()
    tui.events.put(("release", (None, stale)))
    tui.events.put(("done", "scan"))
    tui._drain_events()
    assert tui.model.selected() is stale
    assert not tui.busy and not tui.scanning


def test_quit_during_scan_progress_stops_cleanly_without_downloads(tui, monkeypatch):
    initial = release()
    actions = []

    def cancellable_scan(on_progress, **kwargs):
        try:
            yield initial
            actions.append("quit")
            tui._quit()
            on_progress(1, 2, "Starting next audit")
            actions.append("unexpected audit")
            yield release("should-not-appear")
        finally:
            actions.append("scan closed")

    service = SimpleNamespace(iter_releases=Mock(side_effect=cancellable_scan),
                              refresh_release=Mock(),
                              release_service=SimpleNamespace(download_missing_tracks=Mock()))
    tui.service_factory.return_value = service
    put_event = tui.events.put

    def stop_worker_after_scan(event):
        put_event(event)
        if event == ("done", "scan"):
            tui.jobs.put(None)

    monkeypatch.setattr(tui.events, "put", stop_worker_after_scan)
    tui._start("scan")

    tui._worker()
    tui._drain_events()

    assert actions == ["quit", "scan closed"]
    assert tui.model.visible() == [initial]
    assert tui.quit_requested and tui.stopping.is_set()
    assert not tui.busy and not tui.scanning
    assert tui.error == ""
    assert tui.jobs.empty()
    service.refresh_release.assert_not_called()
    service.release_service.download_missing_tracks.assert_not_called()


def test_progress_labels_real_track_counts_without_mislabeling_background_scan(tui):
    tui.operation = "download"
    tui._progress(2, 7, "Checking library releases")
    tui._progress(0, 0, "Scanning local library")
    # The worker can emit download events before the UI changes operation.
    tui.operation = "scan"
    tui._download_progress(1, 3, "Release search 2/3: Album")

    assert tui.events.get_nowait() == ("progress", "Checking library releases (2/7)")
    assert tui.events.get_nowait() == ("progress", "Scanning local library")
    assert tui.events.get_nowait() == ("progress", "1/3 tracks matched · Release search 2/3: Album")


def test_partial_queue_result_marks_only_successful_tracks_and_shows_failure(tui):
    row = release(tracks=[track(1, "One"), track(2, "Two"), track(3, "Three")])
    tui.model.update(row)
    tui.busy = True
    result = {"total_missing": 3, "resolved_count": 2, "matched_count": 2, "queued_count": 1,
              "matched_tracks": row["tracks"][:2], "queued_tracks": row["tracks"][:1],
              "queue_errors": [{"error": "Peer disconnected", "tracks": [row["tracks"][1]]}]}
    tui.events.put(("result", (release_key(row), row, result)))
    tui.events.put(("done", "download"))

    tui._drain_events()

    assert [tui.model.track_status(row, item) for item in row["tracks"]] == ["queued", "matched", "missing"]
    assert tui.model.pending(row) == row["tracks"][1:]
    assert "1 queued in slskd" in tui.last_result
    assert "1 unmatched" in tui.last_result
    assert tui.error == "Peer disconnected"
    assert not tui.busy


def test_preview_result_keeps_all_missing_tracks_available_to_download(tui):
    row = release()
    result = {"dry_run": True, "total_missing": 1, "resolved_count": 1, "matched_count": 1,
              "queued_count": 0, "matched_tracks": [row["tracks"][1]], "queued_tracks": []}
    tui.events.put(("result", (release_key(row), row, result)))

    tui._drain_events()

    assert tui.model.pending(row) == [row["tracks"][1]]
    assert tui.model.track_status(row, row["tracks"][1]) == "matched"
    assert "preview; nothing queued" in tui.last_result


def test_browse_command_accepts_library_filter_and_download_options(tmp_path):
    args = build_parser().parse_args([
        "browse", "--artist", "Björk", "--library-dir", str(tmp_path), "--format", "mp3-320",
        "--timeout", "4.5", "--force-refresh", "--dry-run",
    ])

    assert args.command == "browse"
    assert args.artist == "Björk"
    assert args.music_dir == Path(tmp_path)
    assert args.format == "mp3-320"
    assert args.timeout == 4.5
    assert args.force_refresh and args.dry_run
    assert build_parser().parse_args(["browse"]).artist is None


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf"])
def test_browse_rejects_invalid_search_timeout(timeout):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["browse", "--timeout", timeout])


@pytest.mark.parametrize("input_tty,output_tty", [(False, True), (True, False), (False, False)])
def test_browse_non_tty_returns_clear_error_before_creating_service(monkeypatch, capsys, input_tty, output_tty):
    monkeypatch.setattr(browser.sys.stdin, "isatty", lambda: input_tty)
    monkeypatch.setattr(browser.sys.stdout, "isatty", lambda: output_tty)
    unexpected_browser = Mock(side_effect=AssertionError("must not start the browser"))
    monkeypatch.setattr(browser, "ReleaseBrowser", unexpected_browser)

    assert main(["browse"]) == 1

    output = capsys.readouterr()
    assert "browse needs an interactive terminal" in output.err
    assert "discogseek browse directly in your terminal" in output.err
    assert output.out == ""
    unexpected_browser.assert_not_called()


def test_terminal_clipping_handles_wide_text_accents_and_control_characters():
    assert browser.clipped("日本語", 5) == "日本"
    assert browser.clipped("e\u0301!", 1) == "e\u0301"
    assert browser.clipped("a\x1b\nb", 4) == "a  b"


def test_title_corrections_preserve_queued_state_and_single_track_selection(fake_service, tmp_path):
    original = dict(track("02", "Old title"), mb_track_id="official-position")
    corrected = dict(track(2, "Corrected title"), mb_track_id="official-position")
    row = release(tracks=[original])
    fresh = release(tracks=[corrected])
    fake_service.refresh_release.return_value = fresh

    download_release(fake_service, row, tmp_path, only_track=track_key(original))
    assert fake_service.release_service.download_missing_tracks.call_args.kwargs["missing_tracks"] == [corrected]

    model = BrowserModel()
    model.record_result(row, {"queued_tracks": [original]})
    model.update(fresh)
    assert model.track_status(fresh, corrected) == "queued"
    assert model.pending(fresh) == []
    assert track_key(dict(corrected, disc_number=2)) != track_key(corrected)


@pytest.mark.parametrize("single", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_whole_library_scan_to_download_uses_local_and_remote_tracks(tmp_path, monkeypatch, single, dry_run):
    """Exercise real inventory, audit, search, and queue code with only external I/O faked."""
    artist, title = "Target Artist", "Four Track EP"
    titles = ["Track One", "Track Two", "Track Three", "Track Four"]
    official = {"id": "release-1", "title": title, "artist-credit": [{"name": artist}],
                "medium-list": [{"position": 1, "track-list": [
                    {"number": str(i), "recording": {"id": f"recording-{i}", "title": name}}
                    for i, name in enumerate(titles, 1)
                ]}]}
    music = tmp_path / artist / title
    music.mkdir(parents=True)
    local = music / "01 Track One.flac"
    local.touch()
    monkeypatch.setattr(AudioQualityAnalyzer, "analyze_file", lambda path: AudioMetadata(
        path=Path(path), title=titles[0], artist=artist, album=title, track_number="1",
        mb_rec_ids={"recording-1"}, file_type="flac", is_lossless=True,
    ))
    monkeypatch.setattr(Config, "NAVIDROME_URL", "http://navidrome.invalid")
    monkeypatch.setattr(Config, "NAVIDROME_USER", "tester")
    monkeypatch.setattr(Config, "NAVIDROME_TOKEN", "test-password")

    def remote_request(self, endpoint, params):
        if endpoint == "ping":
            return {}
        if endpoint == "getAlbumList2":
            return {"albumList2": {"album": [{"id": "album-1", "name": title, "artist": artist}]}}
        if endpoint == "getAlbum":
            return {"album": {"id": "album-1", "name": title, "artist": artist,
                              "musicBrainzId": "release-1", "songCount": 1, "song": [
                                  {"id": "song-2", "title": titles[1], "artist": artist,
                                   "album": title, "track": 2, "musicBrainzId": "recording-2"},
                              ]}}
        pytest.fail(f"Unexpected Navidrome endpoint: {endpoint}")

    monkeypatch.setattr(NavidromeScanner, "_api_request", remote_request)
    monkeypatch.setattr(MusicBrainzClient, "get_release_by_id", lambda *a, **k: official)
    monkeypatch.setattr(MusicBrainzClient, "search_release", lambda *a, **k: [official])
    files = [{"filename": f"Music\\{artist} - {title}\\{i:02d} {name}.flac", "size": i * 1000}
             for i, name in enumerate(titles, 1)]
    search_queries = []
    response = {"responses": [{"username": "peer", "files": files}]}
    monkeypatch.setattr(SlskdClient, "search", lambda *a, **k: (search_queries.append(k["query"]), response)[1])
    monkeypatch.setattr(SlskdClient, "batch_search", lambda self, queries, **k: (
        search_queries.extend(queries), {query: response for query in queries})[1])
    enqueued = []
    monkeypatch.setattr(SlskdClient, "enqueue_download", lambda self, username, files: enqueued.extend(files))
    service = LibraryBrowserService()

    rows = list(service.iter_releases(library_dir=tmp_path))
    assert len(rows) == 1
    assert rows[0]["found_count"] == 2
    assert rows[0]["missing_count"] == 2
    assert enqueued == []
    fresh, result = download_release(service, rows[0], tmp_path, dry_run=dry_run,
                                     only_track=track_key(rows[0]["tracks"][3]) if single else None)

    assert [item["title"] for item in fresh["tracks"]] == titles
    expected = ["Track Four"] if single else ["Track Three", "Track Four"]
    assert [item["title"] for item in result["matched_tracks"]] == expected
    assert [item["title"] for item in result["queued_tracks"]] == ([] if dry_run else expected)
    assert [item["title"] for item in enqueued] == ([] if dry_run else expected)
    assert search_queries == [f"{artist} {'Track Four' if single else title}"]


def test_browsing_and_auditing_never_constructs_slskd_client(monkeypatch):
    unexpected_slskd = Mock(side_effect=AssertionError("Browsing must not contact slskd"))
    monkeypatch.setattr(library, "SlskdClient", unexpected_slskd)
    metadata = Mock()
    metadata.get_release_by_id.return_value = {
        "id": "edition-1", "title": "Album", "artist-credit": [{"name": "Artist"}],
        "medium-list": [{"position": 1, "track-list": [
            {"number": "1", "recording": {"title": "One"}},
            {"number": "2", "recording": {"title": "Two"}},
        ]}],
    }
    release_service = library.LibraryReleaseService(mb_client=metadata)
    local_track = dict(track(1, "One", "found"), path="/music/Album/01 One.flac", source="local")
    release_service.scan_library_releases = Mock(return_value=[
        dict(release(tracks=[local_track]), mb_release_id="edition-1"),
    ])

    rows = list(LibraryBrowserService(release_service).iter_releases())

    assert len(rows) == 1
    assert rows[0]["is_audited"]
    assert rows[0]["missing_count"] == 1
    unexpected_slskd.assert_not_called()
