"""Multiple release requests, overlapping scopes, and worker lifecycle without services."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from discogseek.cli import browser
from discogseek.cli.browser import ReleaseBrowser, release_key, track_key
from discogseek.cli.main import build_parser


def release(identity):
    return {"id": identity, "title": identity, "artist": "Artist", "is_audited": True,
            "missing_count": 2, "tracks": [
                {"disc_number": 1, "track_number": str(index), "title": f"Track {index}", "status": "missing"}
                for index in (1, 2)
            ]}


@pytest.fixture
def tui():
    app = ReleaseBrowser(build_parser().parse_args(["browse"]), service_factory=Mock())
    for identity in ("A", "B", "C"):
        app.model.update(release(identity))
    app.model.selected()
    return app


def finish(tui, row, queued=(), *, dry_run=False, old_key=None):
    tui.events.put(("result", (old_key or release_key(row), row, {
        "total_missing": len(row["tracks"]), "resolved_count": len(queued),
        "queued_count": len(queued), "queued_tracks": list(queued), "dry_run": dry_run,
    })))
    tui.events.put(("done", "download"))


def test_d_on_multiple_releases_runs_fifo_and_repeated_presses_do_not_duplicate(tui):
    import curses

    for key in ("d", "d", "j", "d", "d", "j", "d"):
        tui._handle_key(key, curses, page_size=10)
    assert tui.jobs.qsize() == 1
    assert len(tui.pending_downloads) == 2
    assert "2 waiting" in tui.message
    tui.model.artist_filter = "Nothing matches"

    for identity in ("A", "B", "C"):
        kind, (row, queued, only_track, dry_run) = tui.jobs.get_nowait()
        assert (kind, row["id"], queued, only_track, dry_run) == ("download", identity, set(), None, False)
        finish(tui, row, row["tracks"])
        tui._drain_events()
        assert tui.busy is (identity != "C")

    assert tui.jobs.empty()
    assert not tui.pending_downloads
    assert tui.active_download is None
    assert all(len(tracks) == 2 for tracks in tui.model.queued.values())


@pytest.mark.parametrize("preview,global_dry_run", [(False, False), (True, False), (False, True)])
def test_request_dedup_covers_whole_release_and_effective_preview_mode(tui, preview, global_dry_run):
    tui.args.dry_run = global_dry_run
    tui._download(preview=preview)
    tui._download(single=True, preview=preview)
    tui.model.move(1)
    tui._download(preview=preview)
    tui._download(single=True, preview=preview)
    assert tui.jobs.qsize() == 1
    assert len(tui.pending_downloads) == 1


def test_preview_does_not_suppress_real_download_and_single_tracks_stay_distinct(tui):
    row = tui.model.selected()
    tui._download(preview=True)
    tui._download(single=True)
    tui.model.move(1, tracks=True)
    tui._download(single=True)
    tui._download(single=True)
    assert len(tui.pending_downloads) == 2
    assert tui.jobs.get_nowait()[1][3] is True

    finish(tui, row, dry_run=True)
    tui._drain_events()
    _, (_, queued, only_track, dry_run) = tui.jobs.get_nowait()
    assert queued == set()
    assert only_track == track_key(row["tracks"][0])
    assert not dry_run

    finish(tui, row, row["tracks"][:1])
    tui._drain_events()
    _, (_, queued, only_track, _) = tui.jobs.get_nowait()
    assert queued == {track_key(row["tracks"][0])}
    assert only_track == track_key(row["tracks"][1])


def test_overlapping_single_and_all_requests_follow_identity_changes_and_latest_events(tui):
    row = tui.model.selected()
    tui._download(single=True)
    tui._download()
    tui.jobs.get_nowait()
    fresh = dict(deepcopy(row), mb_release_id="official-id")
    finish(tui, fresh, fresh["tracks"][:1], old_key="A")
    newest = dict(deepcopy(fresh), title="Updated title")
    tui.events.put(("release", ("official-id", newest)))

    tui._drain_events()

    _, (submitted, queued, only_track, _) = tui.jobs.get_nowait()
    assert submitted["title"] == "Updated title"
    assert release_key(submitted) == "official-id"
    assert queued == {track_key(fresh["tracks"][0])}
    assert only_track is None
    tui._download()
    assert not tui.pending_downloads
    assert tui.jobs.empty()


def test_waiting_release_that_becomes_complete_is_skipped(tui):
    a, b, c = [tui.model.releases[key] for key in ("A", "B", "C")]
    for row in (a, b, c):
        tui.model.selected_key = row["id"]
        tui._download()
    tui.jobs.get_nowait()
    finish(tui, a)
    fresh = deepcopy(b)
    for track in fresh["tracks"]:
        track["status"] = "found"
    fresh["missing_count"] = 0
    tui.events.put(("release", ("B", fresh)))

    tui._drain_events()

    assert tui.jobs.get_nowait()[1][0]["id"] == "C"
    assert not tui.pending_downloads


def test_request_during_refresh_waits_and_uses_refreshed_release(tui):
    row = tui.model.selected()
    tui._start("refresh", row)
    tui._download()
    assert tui.jobs.get_nowait()[0] == "refresh"
    assert tui.jobs.empty()
    fresh = dict(deepcopy(row), mb_release_id="resolved")
    tui.events.put(("release", ("A", fresh)))
    tui.events.put(("done", "refresh"))

    tui._drain_events()

    assert release_key(tui.jobs.get_nowait()[1][0]) == "resolved"
    assert tui.busy and tui.operation == "download"


def test_worker_continues_queue_after_error_and_preserves_error_message(tui, monkeypatch):
    calls = []

    def refresh(row, **kwargs):
        calls.append(row["id"])
        if row["id"] == "A":
            raise RuntimeError("A: library temporarily unavailable")
        return deepcopy(row)

    service = SimpleNamespace(refresh_release=Mock(side_effect=refresh), release_service=SimpleNamespace(
        download_missing_tracks=Mock(side_effect=lambda **kwargs: {"queued_tracks": kwargs["missing_tracks"]}),
    ))
    tui.service_factory.return_value = service
    for identity in ("A", "B", "C"):
        tui.model.selected_key = identity
        tui._download()
    original_put = tui.events.put

    def consume(event):
        original_put(event)
        tui._drain_events()
        if event == ("done", "download") and not tui.busy:
            tui.jobs.put(None)

    monkeypatch.setattr(tui.events, "put", consume)
    tui._worker()

    assert calls == ["A", "B", "C"]
    assert service.release_service.download_missing_tracks.call_count == 2
    assert tui.error == "A: library temporarily unavailable"
    assert not tui.busy and not tui.pending_downloads


def test_quit_discards_waiting_requests_and_finishes_active_operation(tui, monkeypatch):
    tui._download()
    tui.model.move(1)
    tui._download()
    tui.model.move(1)
    tui._download()
    tui._quit()
    assert not tui.pending_downloads
    assert "Discarded 2" in tui.message
    tui._download()
    assert not tui.pending_downloads
    service = SimpleNamespace(refresh_release=Mock(side_effect=lambda row, **kwargs: row),
                              release_service=SimpleNamespace(download_missing_tracks=Mock(return_value={})))
    tui.service_factory.return_value = service
    original_put = tui.events.put

    def consume(event):
        original_put(event)
        tui._drain_events()
        if event == ("done", "download"):
            tui.jobs.put(None)

    monkeypatch.setattr(tui.events, "put", consume)
    tui._worker()

    assert service.refresh_release.call_args.args[0]["id"] == "A"
    assert service.refresh_release.call_count == 1
    assert service.release_service.download_missing_tracks.call_count == 1
    assert not tui.busy


def test_failure_poll_before_next_request_overrides_stale_queued_snapshot(tui, monkeypatch):
    row = tui.model.selected()
    tui._download(single=True)
    tui._download()
    clock = [0]
    monkeypatch.setattr(browser.time, "monotonic", lambda: clock[0])
    submitted = []

    def download(**kwargs):
        tracks = kwargs["missing_tracks"]
        submitted.append(tracks)
        if len(submitted) == 2:
            tui.jobs.put(None)
        clock[0] = browser.DOWNLOAD_POLL_INTERVAL
        return {"queued_tracks": tracks, "queued_files": [
            {"user": "peer", "filename": f"{track['title']}.flac"} for track in tracks
        ]}

    client = SimpleNamespace(get_downloads=Mock(return_value=[{"username": "peer", "directories": [{"files": [
        {"filename": "Track 1.flac", "state": "Completed, Errored"},
    ]}]}]))
    service = SimpleNamespace(refresh_release=Mock(side_effect=lambda row, **kwargs: deepcopy(row)),
                              release_service=SimpleNamespace(slskd_client=client,
                                  download_missing_tracks=Mock(side_effect=download)))
    tui.service_factory.return_value = service
    original_put = tui.events.put

    def consume(event):
        original_put(event)
        tui._drain_events()

    monkeypatch.setattr(tui.events, "put", consume)
    tui._worker()

    assert submitted == [row["tracks"][:1], row["tracks"]]
    client.get_downloads.assert_called_once()


@pytest.mark.parametrize("size", [(14, 72), (24, 100)])
def test_queue_status_is_visible_while_browsing(tui, size):
    rows = {}

    def addstr(y, x, text, attr):
        assert 0 <= y < size[0] and x + len(text) < size[1]
        row = rows.get(y, " " * size[1])
        rows[y] = row[:x] + text + row[x + len(text):]

    screen = SimpleNamespace(getmaxyx=lambda: size, erase=rows.clear, addstr=addstr, refresh=lambda: None)
    curses = SimpleNamespace(A_BOLD=1, A_REVERSE=2, A_DIM=4, error=RuntimeError)
    tui._download()
    tui.model.move(1)
    tui._download()
    tui._draw(screen, curses)

    assert "1 waiting" in rows[2]
    assert any("[searching] A" in row for row in rows.values())
    assert any("[waiting] B" in row for row in rows.values())
