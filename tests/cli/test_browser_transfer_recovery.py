"""Browser recovery of stale, unimported, retried and failed transfers without real services."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
import requests

from discogseek.cli import browser
from discogseek.cli.browser import ReleaseBrowser, release_key, track_key
from discogseek.cli.main import build_parser
from discogseek.clients.slskd import SlskdAPIError
from discogseek.core import transfers as transfer_history
from discogseek.core.peer_policy import PeerPolicy
from discogseek.services.library import LibraryReleaseService


PATH = "Music/Album/{} song.flac"


def release(identity="edition-1", missing=2):
    tracks = [{"disc_number": 1, "track_number": str(index), "title": f"Track {index}",
               "mb_recording_id": f"rec-{index}", "status": "found" if index == 1 else "missing"}
              for index in range(1, missing + 2)]
    return {"id": identity, "mb_release_id": identity, "artist": "Artist", "title": "Album",
            "tracks": tracks, "is_audited": True, "found_count": 1, "missing_count": missing}


def imported(row, *positions):
    fresh = deepcopy(row)
    for position in positions:
        fresh["tracks"][position]["status"] = "found"
    fresh["found_count"] = sum(track["status"] == "found" for track in fresh["tracks"])
    fresh["missing_count"] = len(fresh["tracks"]) - fresh["found_count"]
    return fresh


def queued_result(row, positions=(1,), user="peer"):
    tracks = [row["tracks"][position] for position in positions]
    return {"dry_run": False, "total_missing": len(tracks), "resolved_count": len(tracks),
            "queued_count": len(tracks), "matched_tracks": tracks, "queued_tracks": tracks,
            "queued_files": [dict(track, user=user, filename=PATH.format(track["track_number"]).replace("/", "\\"))
                             for track in tracks]}


def nothing_queued(count=1):
    return {"total_missing": count, "resolved_count": 0, "queued_count": 0,
            "queued_tracks": [], "matched_tracks": []}


def transfers(*files, user="peer"):
    return [{"username": user, "directories": [{"directory": "Music\\Album", "files": list(files)}]}]


def transfer(number=2, state="Completed, Succeeded", **extra):
    return {"filename": f"Music\\Album\\{number} song.flac", "state": state, **extra}


def ago(seconds):
    """A slskd timestamp (UTC without a zone) relative to the real clock."""
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%f")


def stale(number=2, **extra):
    return transfer(number, "Queued, Remotely", requestedAt=ago(2 * 86400), enqueuedAt=ago(2 * 86400),
                    attempts=2, bytesTransferred=8378324, **extra)


def remember(tui, row, result=None):
    result = queued_result(row) if result is None else result
    tui.model.update(row)
    tui.model.record_result(row, result)
    tui._remember_downloads(row, result)


def check(tui, service):
    tui._check_downloads(service)
    tui._drain_events()


def run_jobs(tui, service):
    """Run the already scheduled FIFO jobs, then stop the worker without real waiting."""
    tui.service_factory.return_value = service
    tui.jobs.put(None)
    tui._worker()
    tui._drain_events()


@pytest.fixture(autouse=True)
def policy_settings(monkeypatch):
    monkeypatch.delenv("DISCOGSEEK_SLSKD_DOWNLOADS_DIR", raising=False)
    monkeypatch.setattr(transfer_history, "STALE_QUEUE_SECONDS", 6 * 3600)
    monkeypatch.setattr(transfer_history, "IMPORT_WINDOW_SECONDS", 24 * 3600)


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
            download_missing_tracks=Mock(return_value=nothing_queued()),
        ),
        refresh_release=Mock(side_effect=lambda row, **kwargs: deepcopy(row)),
    )


def status(tui, row, position=1):
    current = tui.model.releases[release_key(row)]
    return tui.model.track_status(current, current["tracks"][position])


# --- Stale remote queues ---------------------------------------------------


def test_stale_queued_transfer_searches_again_without_cancelling_it(tui, service):
    row = release(missing=1)
    remember(tui, row)
    client = service.release_service.slskd_client
    client.get_downloads.return_value = transfers(stale(id="old-id"))

    check(tui, service)

    client.cancel_download.assert_not_called()
    assert status(tui, row) == "stale"
    assert "waiting for 2 days without starting" in tui.last_result
    assert "still queued in slskd" in tui.last_result and "1/2" in tui.last_result
    assert tui.active_download.recovery
    assert tui.active_download.only_track == track_key(row["tracks"][1])
    kind, (_, queued, only_track, _) = tui.jobs.queue[0]
    assert kind == "recover" and queued == set() and only_track == track_key(row["tracks"][1])

    service.release_service.download_missing_tracks.return_value = queued_result(row, user="fresh-peer")
    run_jobs(tui, service)

    call = service.release_service.download_missing_tracks.call_args.kwargs
    assert call["missing_tracks"] == [row["tracks"][1]]
    assert call["search_scope"] == "track"
    # Excluded for the search; the downloader cancels it only for a replacement.
    assert call["excluded_sources"] == {("peer", PATH.format(2))}
    client.cancel_download.assert_not_called()
    assert status(tui, row) == "queued"
    watch = tui._downloads[release_key(row)]
    assert watch["files"] == {("fresh-peer", PATH.format(2)): track_key(row["tracks"][1])}
    assert tui._superseded == {("peer", PATH.format(2)): release_key(row)}

    # A replaced transfer still waiting is stopped so it cannot arrive late.
    fresh = transfer(state="Queued, Remotely", id="new-id", enqueuedAt=ago(5))
    client.get_downloads.return_value = transfers(stale(id="old-id")) + transfers(fresh, user="fresh-peer")
    check(tui, service)

    client.cancel_download.assert_called_once_with("peer", "old-id")
    assert not tui._superseded
    client.get_downloads.return_value = (transfers(transfer(state="Completed, Cancelled", id="old-id"))
                                         + transfers(fresh, user="fresh-peer"))
    check(tui, service)

    assert not tui._peer_policy.blocked_reason("peer")
    client.cancel_download.assert_called_once()
    assert status(tui, row) == "queued"
    assert tui.jobs.empty() and not tui.pending_downloads


def test_stale_track_replaced_through_an_equivalent_edition_is_handed_over(tui, service, clock):
    first, second = release("edition-1", missing=1), release("edition-2", missing=1)
    assert browser.equivalent_release_key(first) == browser.equivalent_release_key(second)
    remember(tui, first)
    tui.model.update(second)
    identity = track_key(first["tracks"][1])
    tui._recovery_attempts[release_key(first)] = {identity: browser.DOWNLOAD_RECOVERY_LIMIT}
    client = service.release_service.slskd_client
    client.get_downloads.return_value = transfers(stale(id="old-id"))
    check(tui, service)
    assert status(tui, second) == "stale"

    # The user asks for the other edition; its search replaces the stale source.
    service.release_service.download_missing_tracks.return_value = queued_result(second, user="fresh-peer")
    tui.jobs.put(("download", (deepcopy(second), set(), None, False)))
    run_jobs(tui, service)

    assert service.release_service.download_missing_tracks.call_args.kwargs["excluded_sources"] == {
        ("peer", PATH.format(2)),
    }
    assert release_key(first) not in tui._downloads
    assert tui._downloads[release_key(second)]["files"] == {("fresh-peer", PATH.format(2)): identity}
    assert status(tui, first) == status(tui, second) == "queued"
    assert tui.model.pending(tui.model.releases[release_key(second)]) == []

    fresh = transfer(state="Queued, Remotely", id="new-id", enqueuedAt=ago(5))
    client.get_downloads.return_value = transfers(stale(id="old-id")) + transfers(fresh, user="fresh-peer")
    clock.now += browser.DOWNLOAD_POLL_INTERVAL
    check(tui, service)

    client.cancel_download.assert_called_once_with("peer", "old-id")

    # If the replacement fails, its track is missing again in both editions.
    client.get_downloads.return_value = transfers(transfer(state="Completed, Rejected", id="new-id"),
                                                  user="fresh-peer")
    clock.now += browser.DOWNLOAD_POLL_INTERVAL
    check(tui, service)

    assert status(tui, first) == status(tui, second) == "missing"
    assert tui.active_download.recovery


def test_replaced_transfer_already_cancelled_by_the_downloader_is_left_alone(tui, service):
    row = release(missing=1)
    remember(tui, row)
    client = service.release_service.slskd_client
    client.get_downloads.return_value = transfers(stale(id="old-id"))
    check(tui, service)
    service.release_service.download_missing_tracks.return_value = queued_result(row, user="fresh-peer")
    run_jobs(tui, service)
    tui._peer_policy.record_internal_cancel("peer", "old-id")
    client.get_downloads.return_value = (
        transfers(transfer(state="Completed, Cancelled", id="old-id"))
        + transfers(transfer(state="InProgress", id="new-id"), user="fresh-peer")
    )

    check(tui, service)

    client.cancel_download.assert_not_called()
    assert not tui._superseded
    assert not tui._peer_policy.blocked_reason("peer")
    assert status(tui, row) == "queued"


def test_stale_transfer_without_alternative_keeps_waiting_and_is_searched_once(tui, service, clock):
    row = release(missing=1)
    remember(tui, row)
    client = service.release_service.slskd_client
    client.get_downloads.return_value = transfers(stale(id="old-id"))
    check(tui, service)

    run_jobs(tui, service)
    for _ in range(50):
        clock.now += browser.DOWNLOAD_POLL_INTERVAL
        check(tui, service)

    assert service.release_service.download_missing_tracks.call_count == 1
    assert "No alternative queued" in tui.last_result
    assert tui.jobs.empty() and not tui.pending_downloads
    client.cancel_download.assert_not_called()
    assert status(tui, row) == "stale"
    assert ("peer", PATH.format(2)) in tui._downloads[release_key(row)]["files"]

    # The user can still ask for another search; the stale source stays excluded.
    tui.model.selected_key = release_key(row)
    tui.model.track_index = 1
    tui._download(single=True)

    kind, (_, queued, only_track, _) = tui.jobs.queue[0]
    assert kind == "download" and queued == set()
    assert only_track == track_key(row["tracks"][1])
    run_jobs(tui, service)
    call = service.release_service.download_missing_tracks.call_args.kwargs
    assert call["missing_tracks"] == [row["tracks"][1]]
    assert call["excluded_sources"] == {("peer", PATH.format(2))}


def test_stale_transfer_after_recovery_limit_keeps_watching_without_loop(tui, service, clock):
    row = release(missing=1)
    remember(tui, row)
    tui._recovery_attempts[release_key(row)] = {track_key(row["tracks"][1]): browser.DOWNLOAD_RECOVERY_LIMIT}
    client = service.release_service.slskd_client
    client.get_downloads.return_value = transfers(stale(id="old-id"))

    for _ in range(20):
        check(tui, service)
        clock.now += browser.DOWNLOAD_POLL_INTERVAL

    assert "automatic recovery stopped after 2 searches" in tui.last_result
    assert tui.jobs.empty() and not tui.pending_downloads
    client.cancel_download.assert_not_called()
    service.release_service.download_missing_tracks.assert_not_called()
    assert status(tui, row) == "stale"
    assert release_key(row) in tui._downloads


@pytest.mark.parametrize("later", ["InProgress", "Completed, Succeeded"])
def test_stale_transfer_that_starts_is_watched_normally_again(tui, service, later):
    row = release(missing=2)
    result = queued_result(row, positions=(1,))
    other = queued_result(row, positions=(2,), user="other-peer")
    for field in ("queued_tracks", "matched_tracks", "queued_files"):
        result[field] += other[field]
    remember(tui, row, result)
    tui._recovery_attempts[release_key(row)] = {
        track_key(track): browser.DOWNLOAD_RECOVERY_LIMIT for track in row["tracks"][1:]
    }
    client = service.release_service.slskd_client
    client.get_downloads.return_value = transfers(stale(2, id="a")) + transfers(stale(3, id="b"), user="other-peer")
    check(tui, service)
    assert status(tui, row, 1) == status(tui, row, 2) == "stale"
    current = tui.model.releases[release_key(row)]
    assert tui.model.pending(current) == current["tracks"][1:]

    client.get_downloads.return_value = (transfers(transfer(2, later, id="a"))
                                         + transfers(stale(3, id="b"), user="other-peer"))
    check(tui, service)

    current = tui.model.releases[release_key(row)]
    assert status(tui, row, 1) == ("queued" if later == "InProgress" else "downloaded")
    assert status(tui, row, 2) == "stale"
    assert tui.model.pending(current) == [current["tracks"][2]]


@pytest.mark.parametrize("serving", [
    transfer(9, "InProgress", id="other"),
    transfer(9, "Completed, Succeeded", id="other", endedAt=ago(1800) + "Z"),
], ids=["sending-now", "sent-recently"])
def test_long_queue_of_a_peer_that_is_still_serving_is_not_stale(tui, service, serving):
    row = release(missing=1)
    remember(tui, row)
    service.release_service.slskd_client.get_downloads.return_value = transfers(stale(id="old-id"), serving)

    check(tui, service)

    assert status(tui, row) == "queued"
    assert tui.jobs.empty() and not tui.pending_downloads


def test_recently_queued_transfer_is_not_stale(tui, service):
    row = release(missing=1)
    remember(tui, row)
    service.release_service.slskd_client.get_downloads.return_value = transfers(
        transfer(state="Queued, Remotely", id="id", enqueuedAt=ago(3600), requestedAt=ago(3600)),
    )

    check(tui, service)

    assert status(tui, row) == "queued"
    assert tui.jobs.empty() and not tui.pending_downloads


def test_waiting_release_is_checked_occasionally_so_tracks_found_elsewhere_stop_the_watch(tui, service, clock):
    row = release(missing=1)
    remember(tui, row)
    service.release_service.slskd_client.get_downloads.return_value = transfers(
        transfer(state="Queued, Remotely", id="id", enqueuedAt=ago(60)),
    )

    check(tui, service)
    clock.now += browser.DOWNLOAD_QUEUED_REFRESH_INTERVAL - 1
    check(tui, service)

    service.refresh_release.assert_not_called()
    service.refresh_release.side_effect = None
    service.refresh_release.return_value = imported(row, 1)
    clock.now += 1
    check(tui, service)

    service.refresh_release.assert_called_once()
    assert status(tui, row) == "found"
    assert not tui._downloads


def test_stale_transfer_of_a_track_found_elsewhere_is_stopped(tui, service, clock):
    row = release(missing=1)
    remember(tui, row)
    tui._recovery_attempts[release_key(row)] = {track_key(row["tracks"][1]): browser.DOWNLOAD_RECOVERY_LIMIT}
    client = service.release_service.slskd_client
    client.get_downloads.return_value = transfers(stale(id="old-id"))
    check(tui, service)
    assert status(tui, row) == "stale"

    service.refresh_release.side_effect = None
    service.refresh_release.return_value = imported(row, 1)
    clock.now += browser.DOWNLOAD_QUEUED_REFRESH_INTERVAL
    check(tui, service)

    assert status(tui, row) == "found" and not tui._downloads
    client.cancel_download.assert_not_called()
    clock.now += browser.DOWNLOAD_POLL_INTERVAL
    check(tui, service)

    client.cancel_download.assert_called_once_with("peer", "old-id")
    assert not tui._superseded and not tui._monitoring()
    client.get_downloads.return_value = transfers(transfer(state="Completed, Cancelled", id="old-id"))
    tui._peer_policy.observe(client.get_downloads())
    assert not tui._peer_policy.blocked_reason("peer")


def test_stale_status_is_drawn(tui, service):
    rows = {}
    size = (24, 100)

    def addstr(y, x, text, attr):
        row = rows.get(y, " " * size[1])
        rows[y] = row[:x] + text + row[x + len(text):]

    row = release(missing=1)
    remember(tui, row)
    tui._recovery_attempts[release_key(row)] = {track_key(row["tracks"][1]): browser.DOWNLOAD_RECOVERY_LIMIT}
    service.release_service.slskd_client.get_downloads.return_value = transfers(stale(id="old-id"))
    check(tui, service)
    screen = SimpleNamespace(getmaxyx=lambda: size, erase=rows.clear, addstr=addstr, refresh=lambda: None)

    tui._draw(screen, SimpleNamespace(A_BOLD=1, A_REVERSE=2, A_DIM=4, error=RuntimeError))

    assert any("STALE" in line and "Track 2" in line for line in rows.values())


class StatefulSlskd:
    """Transfers that change when cancelled or enqueued, like slskd's history."""

    def __init__(self, responses):
        self.responses = responses
        self.transfers = {}
        self.cancelled, self.enqueued = [], []
        self.policy = PeerPolicy()

    def add(self, user, filename, **file):
        self.transfers[(user, filename)] = dict(file, filename=filename)

    def add_record(self, user, file):
        self.transfers[(user, file["filename"])] = dict(file)

    def get_downloads(self):
        users = {}
        for (user, _), file in self.transfers.items():
            users.setdefault(user, []).append(dict(file))
        return [{"username": user, "directories": [{"directory": "Music", "files": files}]}
                for user, files in users.items()]

    def cancel_download(self, user, transfer_id):
        # Like SlskdClient: mark the cancellation as discogseek's own first.
        self.policy.record_internal_cancel(user, transfer_id)
        self.cancelled.append((user, transfer_id))
        for (owner, _), file in self.transfers.items():
            if owner == user and file["id"] == transfer_id:
                file.update(state="Completed, Cancelled", endedAt=ago(0) + "Z")

    def enqueue_download(self, user, files):
        self.enqueued.append((user, [file["filename"] for file in files]))
        for file in files:
            self.add(user, file["filename"], id=f"new-{len(self.transfers)}", state="Queued, Remotely",
                     requestedAt=ago(1), enqueuedAt=ago(1))
        return True

    def search(self, query, **kwargs):
        return {"searchText": query, "responses": self.responses}

    def batch_search(self, queries, **kwargs):
        return {query: self.search(query) for query in queries}


def test_stale_transfer_is_replaced_through_the_real_downloader_exactly_once(tui, clock):
    row = {"id": "lone", "mb_release_id": "lone", "artist": "Lone", "title": "The Light Year Wave Tape",
           "is_audited": True, "found_count": 1, "missing_count": 1, "tracks": [
               {"disc_number": 1, "track_number": "1", "title": "Intro", "mb_recording_id": "rec-1",
                "status": "found"},
               {"disc_number": 1, "track_number": "7", "title": "Cody", "mb_recording_id": "rec-7",
                "status": "missing"},
           ]}
    old = "Musics\\Music\\Lone\\2020 - The Light Year Wave Tape [Album]\\07 - Cody [FLAC] [889 kbps].flac"
    new = "Music\\Lone - The Light Year Wave Tape (2020)\\07 - Cody.flac"
    client = StatefulSlskd([
        {"username": "gliczide", "files": [{"filename": old, "size": 19493883}]},
        {"username": "fresh-peer", "files": [{"filename": new, "size": 19493883}]},
    ])
    client.add_record("gliczide", dict(stale(id="stale-id"), filename=old))
    service = SimpleNamespace(
        release_service=LibraryReleaseService(mb_client=MagicMock(), slskd_client=client),
        refresh_release=Mock(side_effect=lambda release, **kwargs: deepcopy(release)),
    )
    cody = row["tracks"][1]
    remember(tui, row, {"queued_tracks": [cody], "matched_tracks": [cody],
                        "queued_files": [dict(cody, user="gliczide", filename=old)]})

    check(tui, service)
    assert status(tui, row) == "stale" and not client.cancelled
    run_jobs(tui, service)
    for _ in range(3):
        clock.now += browser.DOWNLOAD_POLL_INTERVAL
        check(tui, service)

    assert client.enqueued == [("fresh-peer", [new])]
    assert client.cancelled == [("gliczide", "stale-id")]
    assert not tui._superseded
    assert not tui._peer_policy.blocked_reason("gliczide")
    assert status(tui, row) == "queued"
    assert tui._downloads[release_key(row)]["files"] == {
        ("fresh-peer", new.replace("\\", "/")): track_key(cody),
    }
    assert tui.jobs.empty() and not tui.pending_downloads


# --- Finished downloads that never reach the library -----------------------


@pytest.fixture
def downloads_dir(tmp_path, monkeypatch):
    directory = tmp_path / "slskd-downloads"
    directory.mkdir()
    monkeypatch.setenv("DISCOGSEEK_SLSKD_DOWNLOADS_DIR", str(directory))
    return directory


def test_succeeded_but_unimported_track_is_demoted_after_grace(tui, service, clock, downloads_dir):
    row = release(missing=1)
    remember(tui, row)
    service.release_service.slskd_client.get_downloads.return_value = transfers(transfer(id="done"))

    check(tui, service)
    assert status(tui, row) == "downloaded"
    clock.now += browser.DOWNLOAD_IMPORT_GRACE - 1
    check(tui, service)

    assert status(tui, row) == "downloaded"
    # The next library refresh after the grace period still lacks the track.
    clock.now += browser.DOWNLOAD_IMPORT_RETRY_INTERVAL
    check(tui, service)

    assert service.refresh_release.call_count == 3
    assert status(tui, row) == "missing"
    current = tui.model.releases[release_key(row)]
    assert tui.model.pending(current) == [current["tracks"][1]]
    assert "not in the library" in tui.last_result and "press d or t" in tui.last_result
    assert release_key(row) not in tui._downloads
    assert ("peer", PATH.format(2)) in tui._failed_sources[release_key(row)]

    tui.model.selected_key = release_key(row)
    tui._download()

    kind, (_, queued, only_track, _) = tui.jobs.get_nowait()
    assert kind == "download" and queued == set() and only_track is None


def test_succeeded_file_still_in_downloads_folder_stays_downloaded(tui, service, clock, downloads_dir):
    (downloads_dir / "Album").mkdir()
    (downloads_dir / "Album" / "2 song.flac").write_bytes(b"audio")
    row = release(missing=1)
    remember(tui, row)
    service.release_service.slskd_client.get_downloads.return_value = transfers(transfer(id="done"))

    check(tui, service)
    for _ in range(3):
        clock.now += browser.DOWNLOAD_IMPORT_GRACE
        check(tui, service)

    assert status(tui, row) == "downloaded"
    assert release_key(row) in tui._downloads
    assert not tui._failed_sources


def test_file_waiting_long_for_an_import_is_checked_less_often(tui, service, clock, downloads_dir):
    (downloads_dir / "Album").mkdir()
    (downloads_dir / "Album" / "2 song.flac").write_bytes(b"audio")
    row = release(missing=1)
    remember(tui, row)
    service.release_service.slskd_client.get_downloads.return_value = transfers(transfer(id="done"))

    check(tui, service)
    while clock.now < 100 + browser.DOWNLOAD_IMPORT_GRACE:
        clock.now += browser.DOWNLOAD_POLL_INTERVAL
        check(tui, service)
    early = service.refresh_release.call_count
    assert early >= browser.DOWNLOAD_IMPORT_GRACE // browser.DOWNLOAD_IMPORT_RETRY_INTERVAL

    for _ in range(int(3600 / browser.DOWNLOAD_POLL_INTERVAL)):
        clock.now += browser.DOWNLOAD_POLL_INTERVAL
        check(tui, service)

    assert service.refresh_release.call_count - early <= 3600 / browser.DOWNLOAD_QUEUED_REFRESH_INTERVAL + 1
    assert status(tui, row) == "downloaded"

    # Another release's file finishing is checked promptly again.
    row_two = release("edition-2", missing=1)
    row_two["title"] = "Other"
    remember(tui, row_two, queued_result(row_two, user="other-peer"))
    service.release_service.slskd_client.get_downloads.return_value = transfers(
        transfer(id="done-2"), user="other-peer",
    )
    before = service.refresh_release.call_count
    clock.now += browser.DOWNLOAD_POLL_INTERVAL
    check(tui, service)
    clock.now += browser.DOWNLOAD_IMPORT_RETRY_INTERVAL
    check(tui, service)

    assert service.refresh_release.call_count - before >= 2


def test_without_downloads_folder_unimported_track_waits_for_import_window(tui, service, clock, monkeypatch):
    monkeypatch.setattr(transfer_history, "IMPORT_WINDOW_SECONDS", 7200)
    row = release(missing=1)
    remember(tui, row)
    service.release_service.slskd_client.get_downloads.return_value = transfers(transfer(id="done"))

    check(tui, service)
    clock.now += browser.DOWNLOAD_IMPORT_GRACE + browser.DOWNLOAD_IMPORT_RETRY_INTERVAL
    check(tui, service)

    assert status(tui, row) == "downloaded"
    clock.now = 100.0 + 7200
    check(tui, service)

    assert status(tui, row) == "missing"
    assert release_key(row) not in tui._downloads


def test_old_succeeded_history_without_local_file_is_demoted_at_first_refresh(tui, service, tmp_path):
    folder = tmp_path / "configured-downloads"
    folder.mkdir()
    client = service.release_service.slskd_client
    client.get_options = Mock(return_value={"directories": {"downloads": str(folder)}})
    row = release(missing=1)
    remember(tui, row)
    client.get_downloads.return_value = transfers(transfer(id="old", endedAt=ago(3 * 86400) + "Z"))

    check(tui, service)

    service.refresh_release.assert_called_once()
    assert status(tui, row) == "missing"
    assert not tui._downloads


# --- slskd's own retries ----------------------------------------------------


SLSKD_RETRY = {"transfers": {"download": {"retry": {"attempts": 10, "delay": 2000, "maxDelay": 30000,
                                                    "partial": "resume"}}}}


def test_slskd026_failed_attempt_waits_out_the_retry_delay_before_switching_sources(tui, service, clock):
    # slskd 0.26 records carry attempts but no nextAttemptAt; one record is
    # reused for every retry (live history: Succeeded after 2-10 attempts).
    row = release(missing=1)
    remember(tui, row)
    client = service.release_service.slskd_client
    client.get_options = Mock(return_value=SLSKD_RETRY)
    attempt = dict(id="id-2", endedAt=ago(1) + "Z", exception="Inactivity timeout")
    client.get_downloads.return_value = transfers(transfer(state="Completed, TimedOut", attempts=1, **attempt))
    window = 30 + 2 * browser.DOWNLOAD_POLL_INTERVAL + browser.DOWNLOAD_RETRY_MARGIN

    check(tui, service)
    clock.now += window - 1
    check(tui, service)

    client.cancel_download.assert_not_called()
    assert tui.jobs.empty() and not tui.pending_downloads
    assert status(tui, row) == "queued" and not tui._failed_sources

    # slskd retried: the next failed attempt waits for its own delay again.
    client.get_downloads.return_value = transfers(transfer(state="Queued, Remotely", attempts=2, **attempt))
    clock.now += browser.DOWNLOAD_POLL_INTERVAL
    check(tui, service)
    client.get_downloads.return_value = transfers(transfer(state="Completed, Errored", attempts=2, **attempt))
    clock.now += browser.DOWNLOAD_POLL_INTERVAL
    check(tui, service)
    clock.now += window - 1
    check(tui, service)

    client.cancel_download.assert_not_called()
    assert status(tui, row) == "queued"

    # No retry came within slskd's longest delay: the source has failed.
    clock.now += 1
    check(tui, service)

    client.cancel_download.assert_called_once_with("peer", "id-2")
    assert status(tui, row) == "missing"
    assert tui.active_download.recovery
    assert not tui._downloads


def test_slskd026_failure_with_exhausted_attempts_is_recovered_at_once(tui, service):
    row = release(missing=1)
    remember(tui, row)
    client = service.release_service.slskd_client
    client.get_options = Mock(return_value=SLSKD_RETRY)
    client.get_downloads.return_value = transfers(transfer(
        state="Completed, TimedOut", id="id-2", attempts=10, endedAt=ago(1) + "Z",
    ))

    check(tui, service)

    client.cancel_download.assert_called_once_with("peer", "id-2")
    assert tui.active_download.recovery


def test_failed_attempt_with_retries_remaining_is_not_cancelled(tui, service):
    row = release(missing=1)
    remember(tui, row)
    client = service.release_service.slskd_client
    client.get_options = Mock(return_value={"transfers": {"download": {"retry": {"attempts": 10}}}})
    retrying = dict(id="id-2", attempts=1, nextAttemptAt=ago(-30) + "Z", exception="Inactivity timeout")
    client.get_downloads.return_value = transfers(transfer(state="Completed, TimedOut", **retrying))

    check(tui, service)
    check(tui, service)

    client.cancel_download.assert_not_called()
    assert tui.jobs.empty() and not tui.pending_downloads
    assert status(tui, row) == "queued"
    assert not tui._failed_sources
    client.get_options.assert_called_once()

    client.get_downloads.return_value = transfers(transfer(state="Completed, TimedOut", **dict(retrying, attempts=10)))
    check(tui, service)

    client.cancel_download.assert_called_once_with("peer", "id-2")
    assert tui.active_download.recovery
    assert status(tui, row) == "missing"


@pytest.mark.parametrize("retry_in,final", [(30, False), (None, True), (-3600, True)],
                         ids=["scheduled", "unscheduled", "lost-schedule"])
def test_retry_schedule_decides_with_a_magicmock_client(tui, retry_in, final):
    row = release(missing=1)
    remember(tui, row)
    client = MagicMock()
    extra = {"nextAttemptAt": ago(-retry_in) + "Z"} if retry_in is not None else {}
    client.get_downloads.return_value = transfers(transfer(state="Completed, Errored", id="id-2", attempts=1, **extra))
    service = SimpleNamespace(release_service=SimpleNamespace(slskd_client=client), refresh_release=Mock())

    check(tui, service)

    assert client.cancel_download.called is final
    assert status(tui, row) == ("missing" if final else "queued")
    assert (tui.active_download is not None) is final


# --- Aborted and cancelled transfers -----------------------------------------


@pytest.mark.parametrize("with_id", [True, False])
def test_aborted_transfer_is_searched_again_without_cancel_or_peer_block(tui, service, with_id):
    row = release(missing=1)
    remember(tui, row)
    client = service.release_service.slskd_client
    client.get_downloads.return_value = transfers(transfer(
        state="Completed, Aborted", exception="Remote size 123 does not match expected size 456",
        **({"id": "aborted-id"} if with_id else {}),
    ))

    check(tui, service)

    client.cancel_download.assert_not_called()
    assert not tui._peer_policy.blocked_reason("peer")
    assert status(tui, row) == "missing"
    assert "Aborted" in tui.last_result and "1/2" in tui.last_result
    assert tui.active_download.recovery
    service.release_service.download_missing_tracks.return_value = queued_result(row, user="other-peer")
    run_jobs(tui, service)

    call = service.release_service.download_missing_tracks.call_args.kwargs
    assert call["excluded_sources"] == {("peer", PATH.format(2))}
    assert status(tui, row) == "queued"


def test_bulk_cancellation_by_slskd_does_not_exclude_its_sources(tui, service):
    row = release(missing=3)
    remember(tui, row, queued_result(row, positions=(1, 2, 3)))
    ended = ago(10) + "Z"
    client = service.release_service.slskd_client
    client.get_downloads.return_value = transfers(*[
        transfer(number, "Completed, Cancelled", id=f"id-{number}", endedAt=ended) for number in (2, 3, 4)
    ])

    check(tui, service)

    assert not tui._failed_sources.get(release_key(row))
    assert not tui._peer_policy.blocked_reason("peer")
    assert "stopped together with other transfers" in tui.last_result
    assert tui.jobs.empty() and not tui.pending_downloads
    assert [status(tui, row, position) for position in (1, 2, 3)] == ["missing"] * 3


# --- One recovery search per release ----------------------------------------


def test_album_wide_failure_coalesces_into_one_recovery_search(tui, service):
    row = release(missing=8)
    remember(tui, row, queued_result(row, positions=range(1, 9)))
    service.release_service.slskd_client.get_downloads.return_value = transfers(*[
        transfer(number, "Completed, Errored", id=f"id-{number}", attempts=10,
                 exception="User peer appears to be offline")
        for number in range(2, 10)
    ])

    check(tui, service)

    assert tui.active_download.recovery and not tui.pending_downloads
    assert tui.active_download.only_tracks == {track_key(track) for track in row["tracks"][1:]}
    service.release_service.download_missing_tracks.return_value = nothing_queued(8)
    service.refresh_release.reset_mock()
    run_jobs(tui, service)

    download = service.release_service.download_missing_tracks
    download.assert_called_once()
    assert download.call_args.kwargs["missing_tracks"] == row["tracks"][1:]
    assert download.call_args.kwargs["search_scope"] == "release"
    service.refresh_release.assert_called_once()
    assert tui._recovery_attempts[release_key(row)] == {track_key(track): 1 for track in row["tracks"][1:]}
    assert "Recovery 1/2" in tui.last_result


def test_later_failures_join_the_waiting_recovery_request(tui, service):
    row = release(missing=3)
    other = release("edition-2", missing=1)
    other["title"] = "Other"
    remember(tui, row, queued_result(row, positions=(1, 2, 3)))
    tui.model.update(other)
    tui.model.selected_key = release_key(other)
    tui._download()
    assert tui.jobs.get_nowait()[0] == "download"
    client = service.release_service.slskd_client
    healthy = [transfer(3, "InProgress", id="b"), transfer(4, "InProgress", id="c")]
    client.get_downloads.return_value = transfers(transfer(2, "Completed, Rejected", id="a"), *healthy)

    check(tui, service)
    client.get_downloads.return_value = transfers(
        transfer(3, "Completed, TimedOut", id="b"), transfer(4, "Completed, Errored", id="c"),
    )
    check(tui, service)

    assert len(tui.pending_downloads) == 1
    waiting = tui.pending_downloads[0]
    assert waiting.recovery and waiting.only_track is None
    assert waiting.only_tracks == {track_key(track) for track in row["tracks"][1:]}
    tui.events.put(("done", "download"))
    tui._drain_events()
    run_jobs(tui, service)

    download = service.release_service.download_missing_tracks
    download.assert_called_once()
    assert download.call_args.kwargs["missing_tracks"] == row["tracks"][1:]
    assert download.call_args.kwargs["search_scope"] == "release"


def test_recovery_skips_tracks_at_their_limit_and_searches_the_rest(tui, service):
    row = release(missing=2)
    tui.model.update(row)
    key = release_key(row)
    tui._recovery_attempts[key] = {track_key(row["tracks"][1]): browser.DOWNLOAD_RECOVERY_LIMIT}
    tui.jobs.put(("recover", (deepcopy(row), set(), None, False,
                              frozenset(track_key(track) for track in row["tracks"][1:]))))

    run_jobs(tui, service)

    call = service.release_service.download_missing_tracks.call_args.kwargs
    assert call["missing_tracks"] == [row["tracks"][2]]
    assert call["search_scope"] == "track"
    assert tui._recovery_attempts[key][track_key(row["tracks"][2])] == 1
    assert tui._recovery_attempts[key][track_key(row["tracks"][1])] == browser.DOWNLOAD_RECOVERY_LIMIT


def test_user_request_for_whole_release_is_not_deduplicated_by_a_partial_recovery(tui, service):
    row = release(missing=3)
    remember(tui, row, queued_result(row, positions=(1, 2)))
    other = release("edition-2", missing=1)
    other["title"] = "Other"
    tui.model.update(other)
    tui.model.selected_key = release_key(other)
    tui._download()
    service.release_service.slskd_client.get_downloads.return_value = transfers(
        transfer(2, "Completed, Rejected", id="a"), transfer(3, "Completed, Rejected", id="b"),
    )
    check(tui, service)
    tui.model.selected_key = release_key(row)

    tui._download()

    assert len(tui.pending_downloads) == 2
    assert tui.pending_downloads[1].only_track is None and tui.pending_downloads[1].only_tracks is None


# --- Recovery searches interrupted by an outage -------------------------------


def failed_once(tui, service):
    row = release(missing=1)
    remember(tui, row)
    service.release_service.slskd_client.get_downloads.return_value = transfers(
        transfer(state="Completed, Errored", id="failed-id", attempts=10),
    )
    check(tui, service)
    assert tui.active_download.recovery
    return row


@pytest.mark.parametrize("stage,error", [
    ("refresh", RuntimeError("Navidrome connection failed: timed out")),
    ("search", SlskdAPIError("slskd request error [GET /api/v0/transfers/downloads]: timed out")),
    ("search", requests.ConnectionError("Connection refused")),
])
def test_transient_recovery_error_is_retried_with_backoff(tui, service, clock, stage, error):
    row = failed_once(tui, service)
    identity = track_key(row["tracks"][1])
    failing = service.refresh_release if stage == "refresh" else service.release_service.download_missing_tracks
    failing.side_effect = error

    run_jobs(tui, service)

    assert "retrying in 1 min" in tui.error
    assert tui._recovery_attempts[release_key(row)][identity] == 0
    assert tui.jobs.empty() and not tui.pending_downloads and tui.active_download is None
    clock.now += browser.DOWNLOAD_RECOVERY_BACKOFF - 1
    run_jobs(tui, service)

    assert tui.jobs.empty() and not tui.pending_downloads
    failing.side_effect = (lambda row, **kwargs: deepcopy(row)) if stage == "refresh" else None
    service.release_service.download_missing_tracks.return_value = queued_result(row, user="other-peer")
    clock.now += 1
    tui._release_due_recoveries()
    tui._drain_events()

    assert tui.active_download.recovery
    run_jobs(tui, service)

    assert tui._recovery_attempts[release_key(row)][identity] == 1
    assert status(tui, row) == "queued"
    assert tui.error == ""
    assert "Recovery 1/2" in tui.last_result
    assert not tui._delayed_recoveries


def test_transient_recovery_errors_back_off_and_stop_after_the_limit(tui, service, clock):
    row = failed_once(tui, service)
    service.release_service.download_missing_tracks.side_effect = SlskdAPIError("slskd unavailable")
    delays = []

    for _ in range(browser.DOWNLOAD_RECOVERY_TRANSIENT_LIMIT):
        run_jobs(tui, service)
        (due, _, _), = tui._delayed_recoveries
        delays.append(due - clock.now)
        clock.now = due
        tui._release_due_recoveries()
        tui._drain_events()
    run_jobs(tui, service)

    assert delays == [60, 120, 240]
    assert service.release_service.download_missing_tracks.call_count == 4
    assert "Automatic recovery stopped after 3 retries" in tui.error
    assert not tui._delayed_recoveries
    assert tui._recovery_attempts[release_key(row)][track_key(row["tracks"][1])] == 0
    assert tui.jobs.empty() and not tui.pending_downloads


def test_worker_wakes_for_a_due_recovery_without_watched_transfers(tui, service, clock):
    row = release(missing=1)
    tui.model.update(row)
    tui._delayed_recoveries.append((clock.now, deepcopy(row), frozenset([track_key(row["tracks"][1])])))

    assert tui._next_job(None, clock.now + 1000) == ("wake", None)

    tui.jobs.put(None)
    tui._worker()
    tui._drain_events()

    assert tui.active_download.recovery
    assert tui.active_download.only_track == track_key(row["tracks"][1])
    assert not tui._delayed_recoveries


def test_quit_discards_delayed_recoveries(tui, service, clock):
    row = release(missing=1)
    tui.model.update(row)
    tui._delayed_recoveries.append((clock.now, deepcopy(row), frozenset([track_key(row["tracks"][1])])))
    tui._quit()

    tui._release_due_recoveries()
    tui._drain_events()

    assert tui.events.empty() and not tui.pending_downloads
