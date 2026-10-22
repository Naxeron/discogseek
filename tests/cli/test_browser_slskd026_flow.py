"""The browser's download flow end to end: worker, downloader, audits and a slskd 0.26 fake."""

import shutil
import threading
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import unquote, urlsplit

import pytest

from discogseek.cli import browser
from discogseek.cli.browser import ReleaseBrowser, release_key, track_key
from discogseek.cli.main import build_parser
from discogseek.clients.slskd import SlskdClient
from discogseek.core import transfers as transfer_history
from discogseek.core.audio import AudioMetadata, AudioQualityAnalyzer
from discogseek.services.library import LibraryReleaseService
from discogseek.services.library_browser import LibraryBrowserService
from tests.helpers.slskd026 import FakeResponse, FakeSlskd026, iso, peer


TITLES = ["First", "Second", "Third"]
FOLDER = "Music\\Artist - Album (2020) [FLAC]"
OTHER_FOLDER = "Shares\\Artist\\Album"
RETRY = {"attempts": 10, "delay": 2000, "maxDelay": 30000, "partial": "resume"}


class _AnyQuery(dict):
    """Search plans that answer every query with the same peers."""

    def __init__(self, responses):
        super().__init__()
        self.responses = responses

    def get(self, key, default=None):
        return (list(self.responses), None)


class Slskd(FakeSlskd026):
    """FakeSlskd026 plus the options endpoint the browser uses."""

    def __init__(self, downloads_dir):
        super().__init__()
        self.options = {"directories": {"downloads": str(downloads_dir)},
                        "transfers": {"download": {"retry": dict(RETRY)}}}
        self.plans = _AnyQuery([])

    def offer(self, *responses):
        self.plans.responses = list(responses)

    def request(self, method, path, **kwargs):
        segments = [unquote(part) for part in urlsplit(path).path.split("/") if part][2:]
        if method == "GET" and segments == ["options"]:
            with self._lock:
                self.requests.append((method, path, None))
            if self.options is None:
                return FakeResponse(403, text="Forbidden")
            return FakeResponse(200, self.options)
        return super().request(method, path, **kwargs)

    def transfer(self, user, number):
        """The newest transfer record of a track's file."""
        return next(t for t in reversed(self.transfers)
                    if t["username"] == user and f"0{number} - " in t["filename"])

    def set_state(self, user, number, state, **extra):
        file = self.transfer(user, number)
        file.update(state=state, **extra)
        if "Completed" in state:
            file.setdefault("endedAt", iso(self.wall))
        return file

    def posted(self, user):
        """The filenames of each enqueue request sent for a peer."""
        return [[file["filename"] for file in body]
                for _, _, body in self.calls("POST", f"/api/v0/transfers/downloads/{user}")]

    def cancels(self):
        return [tuple(path.split("/")[-2:]) for method, path, _ in self.requests if method == "DELETE"]


def files(folder, *numbers):
    return [f"{folder}\\0{number} - {TITLES[number - 1]}.flac" for number in numbers]


@pytest.fixture
def world(tmp_path, monkeypatch):
    library = tmp_path / "music" / "Library"
    album = library / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01 First.flac").write_bytes(b"audio")
    downloads = tmp_path / "slskd" / "downloads"
    downloads.mkdir(parents=True)
    monkeypatch.delenv("DISCOGSEEK_SLSKD_DOWNLOADS_DIR", raising=False)

    def metadata(path):
        number, title = path.stem.split(" ", 1)
        return AudioMetadata(path=path, title=title, artist="Artist", album="Album", track_number=number,
                             format_label="FLAC", mb_release_ids={"edition-1"},
                             mb_rec_ids={f"rec-{int(number)}"})

    monkeypatch.setattr(AudioQualityAnalyzer, "analyze_file", Mock(side_effect=metadata))
    mb = Mock()
    mb.get_release_by_id.return_value = {
        "id": "edition-1", "title": "Album", "artist-credit": [{"name": "Artist"}],
        "medium-list": [{"position": "1", "track-list": [
            {"number": str(number), "recording": {"id": f"rec-{number}", "title": title}}
            for number, title in enumerate(TITLES, 1)
        ]}],
    }
    client = SlskdClient(base_url="http://slskd.test", api_key="key")
    slskd = Slskd(downloads).install(monkeypatch, client)
    service = LibraryBrowserService(LibraryReleaseService(mb_client=mb, slskd_client=client))
    refresh = Mock(wraps=service.refresh_release)
    monkeypatch.setattr(service, "refresh_release", refresh)
    options = Mock(wraps=client.get_options)
    monkeypatch.setattr(client, "get_options", options)
    searches = Mock(wraps=service.release_service.download_missing_tracks)
    monkeypatch.setattr(service.release_service, "download_missing_tracks", searches)
    tui = ReleaseBrowser(build_parser().parse_args(["browse"]), service_factory=Mock(return_value=service))
    tui.args.music_dir = library
    row = next(service.iter_releases(library_dir=library))
    assert row["is_audited"] and row["missing_count"] == 2
    tui.model.update(row)
    tui.model.selected()
    refresh.reset_mock()

    def run():
        """Run the scheduled jobs in FIFO order, then stop the worker."""
        tui.jobs.put(None)
        tui._worker()
        tui._drain_events()

    def poll(seconds=browser.DOWNLOAD_POLL_INTERVAL):
        slskd.sleep(seconds)
        tui._check_downloads(service)
        tui._drain_events()

    def status(number):
        current = tui.model.releases[release_key(row)]
        return tui.model.track_status(current, current["tracks"][number - 1])

    def deliver(user, number):
        """slskd finished the file: it waits in the downloads folder for an importer."""
        name = slskd.transfer(user, number)["filename"].split("\\")
        target = downloads.joinpath(*name[-2:])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"audio")
        slskd.set_state(user, number, "Completed, Succeeded", bytesTransferred=1000)
        return target

    def import_file(path, number):
        shutil.move(str(path), str(album / f"0{number} {TITLES[number - 1]}.flac"))

    return SimpleNamespace(tui=tui, slskd=slskd, client=client, service=service, refresh=refresh,
                           options=options, searches=searches, row=row, run=run, poll=poll, status=status,
                           deliver=deliver, import_file=import_file, downloads=downloads, album=album)


def queue_release(world, *responses):
    world.slskd.offer(*responses)
    world.tui._download()
    world.run()


# --- Successful download --------------------------------------------------


def test_queued_release_finishes_and_is_found_after_import(world):
    queue_release(world, peer("peer", *files(FOLDER, 1, 2, 3)))
    # The worker already polled once after queueing; it read slskd's options then.
    assert world.tui._slskd_settings_loaded and world.tui._retry_attempts == 10
    assert world.tui._downloads_dir == str(world.downloads)
    # The downloader gets the browser's downloads folder instead of reading the options again.
    assert world.searches.call_args.kwargs["downloads_dir"] == str(world.downloads)
    assert world.options.call_count == 1
    options_reads = world.options.call_count

    assert world.status(2) == world.status(3) == "queued"
    assert world.slskd.posted("peer") == [files(FOLDER, 2, 3)]
    watch = world.tui._downloads[release_key(world.row)]
    assert set(watch["files"].values()) == {track_key(t) for t in world.row["tracks"][1:]}

    world.slskd.set_state("peer", 2, "InProgress", startedAt=iso(world.slskd.wall))
    world.poll()
    assert world.status(2) == "queued"

    second = world.deliver("peer", 2)
    world.poll()
    assert world.status(2) == "downloaded" and world.status(3) == "queued"
    world.import_file(second, 2)
    third = world.deliver("peer", 3)
    world.poll()
    assert world.status(3) == "downloaded"
    world.poll(browser.DOWNLOAD_IMPORT_RETRY_INTERVAL)
    assert world.status(2) == "found" and world.status(3) == "downloaded"
    world.import_file(third, 3)
    world.poll(browser.DOWNLOAD_IMPORT_RETRY_INTERVAL)

    assert world.status(2) == world.status(3) == "found"
    assert not world.tui._downloads and not world.tui._monitoring()
    assert world.tui.model.visible() == []
    # slskd's options are read once for monitoring, not at every poll.
    assert world.options.call_count == options_reads
    assert world.refresh.call_count == 5
    assert not world.slskd.cancels()


def test_unreadable_slskd_options_are_retried_every_few_minutes_not_every_poll(world):
    world.slskd.options = None
    queue_release(world, peer("peer", *files(FOLDER, 1, 2, 3)))
    assert not world.tui._slskd_settings_loaded and world.tui._retry_attempts is None
    reads = world.options.call_count
    # The browser read the options before its search; the next read is due
    # a retry interval after that failure.
    retry_at = world.tui._slskd_settings_retry_at

    polls = 0
    while world.slskd.now + browser.DOWNLOAD_POLL_INTERVAL < retry_at:
        world.poll()
        polls += 1
    assert polls > browser.DOWNLOAD_OPTIONS_RETRY_INTERVAL / browser.DOWNLOAD_POLL_INTERVAL / 2
    assert world.options.call_count == reads

    world.slskd.options = {"transfers": {"download": {"retry": dict(RETRY)}}}
    world.poll(browser.DOWNLOAD_POLL_INTERVAL + 1)
    world.poll()

    assert world.options.call_count == reads + 1
    assert world.tui._slskd_settings_loaded and world.tui._retry_attempts == 10


def test_worker_thread_queues_and_polls_while_the_ui_loop_drains_events(world):
    world.slskd.offer(peer("peer", *files(FOLDER, 1, 2, 3)))
    worker = threading.Thread(target=world.tui._worker, daemon=True, name="release-browser")
    worker.start()
    try:
        world.tui._download()
        deadline = time.perf_counter() + 10
        # The search advances slskd's clock, so the worker polls right after it.
        while (world.tui.busy or not world.tui._slskd_settings_loaded) and time.perf_counter() < deadline:
            world.tui._drain_events()
            threading.Event().wait(0.005)
    finally:
        world.tui._quit()
        world.tui.jobs.put(None)
        worker.join(10)
    world.tui._drain_events()

    assert not worker.is_alive()
    assert world.tui.error == "" and world.tui.download_error == ""
    assert world.status(2) == world.status(3) == "queued"
    assert world.slskd.calls("POST", "/api/v0/transfers/downloads/peer")


@pytest.mark.parametrize("single", [False, True], ids=["release", "track"])
def test_the_whole_tracklist_corroborates_a_folder_that_names_neither_artist_nor_album(world, single):
    # Without the official tracklist the downloader knows only the requested rows,
    # so the folder's other tracks of this release cannot vouch for the file.
    (world.album / "04 Bonus.flac").write_bytes(b"audio")  # a local extra, not official
    world.slskd.offer(peer("peer", *files("Shares\\Rips\\cd123", 1, 2, 3)))
    world.tui.model.track_index = 1
    world.tui._download(single=single)
    world.run()

    assert [track["title"] for track in world.tui.model.releases[release_key(world.row)]["tracks"]] \
        == TITLES + ["Bonus"]
    official = world.searches.call_args.kwargs["official_tracks"]
    assert [track["title"] for track in official] == TITLES
    assert world.slskd.posted("peer") == [files("Shares\\Rips\\cd123", *([2] if single else [2, 3]))]
    assert world.status(2) == "queued"


# --- Stale remote queues ---------------------------------------------------


def make_stale(world, user="peer", numbers=(2, 3)):
    for number in numbers:
        requested = world.slskd.transfer(user, number)["requestedAt"]
        world.slskd.set_state(user, number, "Queued, Remotely", enqueuedAt=requested)
    world.poll(6 * 3600 + 60)


@pytest.mark.parametrize("stale_peer_listed", [True, False], ids=["stale-peer-in-results", "stale-peer-offline"])
def test_stale_queue_is_replaced_and_the_old_transfer_cancelled_once(world, stale_peer_listed):
    queue_release(world, peer("peer", *files(FOLDER, 1, 2, 3)))
    old = {number: world.slskd.transfer("peer", number)["id"] for number in (2, 3)}
    world.slskd.offer(*([peer("peer", *files(FOLDER, 1, 2, 3))] if stale_peer_listed else []),
                      peer("fresh-peer", *files(OTHER_FOLDER, 1, 2, 3)))

    make_stale(world)

    assert world.status(2) == world.status(3) == "stale"
    assert not world.slskd.cancels()
    assert world.tui.active_download.recovery and world.tui.active_download.only_tracks is not None
    world.run()

    assert world.searches.call_count == 2
    recovery = world.searches.call_args.kwargs
    assert recovery["search_scope"] == "release" and len(recovery["missing_tracks"]) == 2
    assert world.slskd.posted("fresh-peer")[0] == files(OTHER_FOLDER, 2, 3)
    assert world.status(2) == world.status(3) == "queued"
    for _ in range(5):
        world.poll()

    assert sorted(world.slskd.cancels()) == sorted(("peer", old[number]) for number in (2, 3))
    assert not world.tui._superseded
    assert not world.tui._peer_policy.blocked_reason("peer")
    watch = world.tui._downloads[release_key(world.row)]
    assert {source[0] for source in watch["files"]} == {"fresh-peer"}
    assert world.tui.jobs.empty() and not world.tui.pending_downloads
    assert world.searches.call_count == 2


def test_browser_and_downloader_share_one_serving_peer_rule(world, monkeypatch):
    queue_release(world, peer("peer", *files(FOLDER, 1, 2, 3)))
    # Whatever the rule says about a peer's moving queue holds for both.
    monkeypatch.setattr(transfer_history, "serving_peers", lambda downloads, now=None: {"peer"})

    make_stale(world)

    assert world.status(2) == world.status(3) == "queued"
    assert world.searches.call_count == 1
    assert transfer_history.DownloadHistory(world.client.get_downloads()).stale == {}


def test_stale_queue_without_alternative_stays_stale_without_a_loop(world):
    queue_release(world, peer("peer", *files(FOLDER, 1, 2, 3)))

    make_stale(world)
    world.run()

    assert world.searches.call_count == 2
    assert "No alternative queued" in world.tui.last_result
    assert world.status(2) == world.status(3) == "stale"
    refreshes = world.refresh.call_count
    for _ in range(120):
        world.poll(60)

    assert world.searches.call_count == 2
    assert world.tui.jobs.empty() and not world.tui.pending_downloads
    assert not world.slskd.cancels()
    assert world.status(2) == world.status(3) == "stale"
    # Two hours of waiting refresh the library about every five minutes.
    assert world.refresh.call_count - refreshes <= 2 * 3600 / browser.DOWNLOAD_QUEUED_REFRESH_INTERVAL + 1

    # The transfer that never started is searched once more after it starts and stalls again.
    for number in (2, 3):
        world.slskd.set_state("peer", number, "Queued, Remotely", enqueuedAt=iso(world.slskd.wall))
    world.poll()
    assert world.status(2) == world.status(3) == "queued"
    make_stale(world)
    world.run()
    assert world.searches.call_count == 3
    for number in (2, 3):
        world.slskd.set_state("peer", number, "Queued, Remotely", enqueuedAt=iso(world.slskd.wall))
    world.poll()
    make_stale(world)

    assert "automatic recovery stopped after 2 searches" in world.tui.last_result
    assert world.tui.jobs.empty() and not world.tui.pending_downloads
    assert world.searches.call_count == 3
    assert not world.slskd.cancels()


# --- Failed transfers ------------------------------------------------------


def test_timed_out_attempt_with_retries_left_is_not_cancelled(world):
    queue_release(world, peer("peer", *files(FOLDER, 1, 2, 3)))
    world.slskd.set_state("peer", 2, "Completed, TimedOut", attempts=1, exception="Inactivity timeout")
    window = RETRY["maxDelay"] / 1000 + 2 * browser.DOWNLOAD_POLL_INTERVAL + browser.DOWNLOAD_RETRY_MARGIN

    world.poll()
    world.poll(window - browser.DOWNLOAD_POLL_INTERVAL - 1)

    assert not world.slskd.cancels() and world.searches.call_count == 1
    assert world.status(2) == "queued"
    file = world.slskd.set_state("peer", 2, "InProgress", attempts=2)
    file.pop("endedAt", None)
    world.poll()
    world.deliver("peer", 2)
    world.poll()

    assert world.status(2) == "downloaded"
    assert not world.slskd.cancels() and world.searches.call_count == 1
    assert world.tui.jobs.empty() and not world.tui.pending_downloads


def test_final_timeouts_are_cancelled_and_recovered_in_one_search(world):
    queue_release(world, peer("peer", *files(FOLDER, 1, 2, 3)))
    ids = {number: world.slskd.transfer("peer", number)["id"] for number in (2, 3)}
    for number in (2, 3):
        world.slskd.set_state("peer", number, "Completed, TimedOut", attempts=10, exception="Inactivity timeout")
    world.slskd.offer(peer("peer", *files(FOLDER, 1, 2, 3)), peer("fresh-peer", *files(OTHER_FOLDER, 1, 2, 3)))

    world.poll()

    assert sorted(world.slskd.cancels()) == sorted(("peer", ids[number]) for number in (2, 3))
    assert world.status(2) == world.status(3) == "missing"
    assert world.tui.active_download.recovery and len(world.tui.active_download.scope()) == 2
    assert len(world.tui.pending_downloads) == 0
    world.run()

    assert world.searches.call_count == 2
    recovery = world.searches.call_args.kwargs
    assert recovery["search_scope"] == "release" and len(recovery["missing_tracks"]) == 2
    assert world.status(2) == world.status(3) == "queued"
    watch = world.tui._downloads[release_key(world.row)]
    assert {source[0] for source in watch["files"]} == {"fresh-peer"}
    # The recovery search lists the stopped files again; they are not stopped twice.
    assert sorted(world.slskd.cancels()) == sorted(("peer", ids[number]) for number in (2, 3))
    for _ in range(3):
        world.poll()
    assert world.searches.call_count == 2
    assert not world.tui._peer_policy.blocked_reason("peer")


def test_attempt_the_browser_stopped_between_retries_is_replaced_by_its_recovery(world):
    queue_release(world, peer("peer", *files(FOLDER, 1, 2, 3)))
    stopped = world.slskd.set_state("peer", 2, "Completed, TimedOut", attempts=1, exception="Inactivity timeout")
    world.slskd.offer(peer("peer", *files(FOLDER, 1, 2, 3)), peer("fresh-peer", *files(OTHER_FOLDER, 1, 2, 3)))

    while not world.slskd.cancels():
        world.poll()

    # The browser waited out slskd's retry delay from its first poll, so the
    # downloader's own retry window (from endedAt) has not passed yet: only
    # the cancellation marker tells it that this attempt will not be retried.
    ended = transfer_history.parse_timestamp(stopped["endedAt"])
    retry_window = RETRY["maxDelay"] / 1000 + transfer_history.RETRY_DELAY_MARGIN_SECONDS
    assert transfer_history.parse_timestamp(iso(world.slskd.wall)) - ended < retry_window
    assert world.slskd.cancels() == [("peer", stopped["id"])] and stopped["state"] == "Completed, TimedOut"
    world.run()

    assert world.searches.call_count == 2
    assert [name.rsplit("\\", 1)[1] for names in world.slskd.posted("fresh-peer") for name in names] == [
        "02 - Second.flac"]
    assert world.slskd.cancels() == [("peer", stopped["id"])]
    assert world.status(2) == world.status(3) == "queued"


def test_attempt_between_slskd_retries_is_neither_stopped_nor_replaced_by_a_request(world):
    # An earlier session queued track 2; its first attempt timed out 10 s ago
    # and slskd retries it within maxDelay (slskd 0.26 sends no nextAttemptAt).
    ended = world.slskd.wall - timedelta(seconds=10)
    world.slskd.transfers.append(dict(
        id="earlier-2", username="peer", direction="Download", filename=files(FOLDER, 2)[0], size=1000,
        state="Completed, TimedOut", attempts=1, exception="Inactivity timeout",
        requestedAt=iso(ended - timedelta(minutes=5)), endedAt=iso(ended),
    ))

    queue_release(world, peer("peer", *files(FOLDER, 1, 2, 3)), peer("fresh-peer", *files(OTHER_FOLDER, 1, 2, 3)))

    assert not world.slskd.cancels()
    posted = [name for names in world.slskd.posted("peer") + world.slskd.posted("fresh-peer") for name in names]
    assert [name.rsplit("\\", 1)[1] for name in posted] == ["03 - Third.flac"]
    assert world.status(2) == world.status(3) == "queued"


def test_aborted_size_mismatch_is_searched_again_without_blocking_the_peer(world):
    queue_release(world, peer("peer", *files(FOLDER, 1, 2, 3)))
    aborted = world.slskd.transfer("peer", 2)["filename"]
    world.slskd.set_state("peer", 2, "Completed, Aborted",
                          exception="Transfer aborted: remote file size 2000 does not match expected size 1000")
    # The same peer shares another copy; the aborted file itself is excluded.
    world.slskd.offer(peer("peer", *files(FOLDER, 1, 2, 3), *files(OTHER_FOLDER, 2)))

    world.poll()

    assert not world.slskd.cancels()
    assert world.status(2) == "missing" and world.tui.active_download.recovery
    world.run()

    assert world.searches.call_count == 2
    assert world.searches.call_args.kwargs["excluded_sources"] == {("peer", aborted.replace("\\", "/"))}
    assert world.slskd.posted("peer")[-1] == files(OTHER_FOLDER, 2)
    assert world.status(2) == "queued"
    assert not world.tui._peer_policy.blocked_reason("peer")
    assert not world.slskd.cancels()


@pytest.mark.parametrize("old_listed", ["first", "last"])
def test_new_attempt_of_a_file_downloaded_before_is_judged_by_its_own_state(world, old_listed):
    # Downloaded days ago but never imported: slskd keeps that record, so the
    # same source now has two records.
    old = dict(id="old-2", username="peer", direction="Download", filename=files(FOLDER, 2)[0], size=1000,
               state="Completed, Succeeded", requestedAt=iso(world.slskd.wall - timedelta(days=3)),
               endedAt=iso(world.slskd.wall - timedelta(days=3)))
    world.slskd.transfers.append(old)
    queue_release(world, peer("peer", *files(FOLDER, 1, 2, 3)))
    retry = next(t for t in world.slskd.transfers if t["filename"] == old["filename"] and t is not old)
    if old_listed == "last":
        world.slskd.transfers.remove(old)
        world.slskd.transfers.append(old)
    assert world.status(2) == "queued"

    world.poll()
    assert world.status(2) == "queued"
    retry.update(state="Completed, Rejected", exception="File not shared.", endedAt=iso(world.slskd.wall))
    world.slskd.offer(peer("fresh-peer", *files(OTHER_FOLDER, 1, 2, 3)))
    world.poll()

    # The new attempt failed: the old success must not hide that.
    assert world.slskd.cancels() == [("peer", retry["id"])]
    assert world.status(2) == "missing" and world.tui.active_download.recovery
    world.run()
    assert world.status(2) == "queued"
    assert world.slskd.calls("POST", "/api/v0/transfers/downloads/fresh-peer")
    assert not world.tui._peer_policy.blocked_reason("peer")


# --- Finished files that never reach the library ---------------------------


def test_finished_file_moved_elsewhere_is_demoted_after_the_grace_period(world):
    queue_release(world, peer("peer", *files(FOLDER, 1, 2, 3)))
    finished = world.deliver("peer", 2)
    world.poll()
    assert world.status(2) == "downloaded"
    finished.unlink()  # an importer put it somewhere outside the library
    refreshes = world.refresh.call_count

    world.poll(browser.DOWNLOAD_IMPORT_GRACE - 30)
    assert world.status(2) == "downloaded"
    world.poll(30 + browser.DOWNLOAD_IMPORT_RETRY_INTERVAL)

    assert world.status(2) == "missing" and world.status(3) == "queued"
    assert "not in the library" in world.tui.last_result
    assert world.refresh.call_count - refreshes == 2
    assert not world.tui.jobs.qsize() and not world.tui.pending_downloads

    world.slskd.offer(peer("peer", *files(FOLDER, 1, 2, 3)), peer("fresh-peer", *files(OTHER_FOLDER, 1, 2, 3)))
    world.tui.model.track_index = 1
    world.tui._download(single=True)
    world.run()

    assert world.searches.call_args.kwargs["excluded_sources"] == {
        ("peer", files(FOLDER, 2)[0].replace("\\", "/")),
    }
    assert world.slskd.posted("fresh-peer")[0] == files(OTHER_FOLDER, 2)
    assert world.status(2) == "queued"


# --- Transfers removed from slskd's list ----------------------------------


def test_transfer_removed_from_slskd_is_released_after_grace_without_refreshing_forever(world):
    queue_release(world, peer("peer", *files(FOLDER, 1, 2, 3)))
    removed = world.slskd.transfer("peer", 2)
    world.slskd.transfers.remove(removed)
    refreshes = world.refresh.call_count

    for _ in range(int(3600 / browser.DOWNLOAD_POLL_INTERVAL)):
        world.poll()

    assert world.status(2) == "missing" and world.status(3) == "queued"
    assert "no longer listed in slskd" in world.tui.last_result
    assert world.tui.jobs.empty() and not world.tui.pending_downloads
    assert not world.slskd.cancels()
    # About one refresh per 15 s until the grace period ends, then every five minutes.
    assert world.refresh.call_count - refreshes <= (
        browser.DOWNLOAD_IMPORT_GRACE / browser.DOWNLOAD_IMPORT_RETRY_INTERVAL
        + 3600 / browser.DOWNLOAD_QUEUED_REFRESH_INTERVAL + 2
    )
    current = world.tui.model.releases[release_key(world.row)]
    assert world.tui.model.pending(current) == [current["tracks"][1]]
    watch = world.tui._downloads[release_key(world.row)]
    assert set(watch["files"].values()) == {track_key(world.row["tracks"][2])}
    # The removed source is not offered again; the user's retry searches elsewhere.
    world.slskd.offer(peer("peer", *files(FOLDER, 1, 2, 3)), peer("fresh-peer", *files(OTHER_FOLDER, 1, 2, 3)))
    world.tui.model.track_index = 1
    world.tui._download(single=True)
    world.run()
    assert world.slskd.posted("fresh-peer")[0] == files(OTHER_FOLDER, 2)
    assert world.status(2) == "queued"


def test_without_the_downloads_folder_a_removed_transfer_waits_for_the_import_window(world, monkeypatch):
    monkeypatch.setattr(transfer_history, "IMPORT_WINDOW_SECONDS", 4 * 3600)
    world.slskd.options["directories"]["downloads"] = "/nonexistent/slskd/downloads"
    queue_release(world, peer("peer", *files(FOLDER, 1, 2, 3)))
    assert world.tui._downloads_dir is None
    world.slskd.transfers.remove(world.slskd.transfer("peer", 2))
    refreshes = world.refresh.call_count

    for _ in range(int(3600 / 15)):
        world.poll(15)

    # It may have finished and await an importer: keep it, checking now and then.
    assert world.status(2) == "queued"
    assert world.refresh.call_count - refreshes <= (
        browser.DOWNLOAD_IMPORT_GRACE / browser.DOWNLOAD_IMPORT_RETRY_INTERVAL
        + 3600 / browser.DOWNLOAD_QUEUED_REFRESH_INTERVAL + 2
    )
    world.poll(3 * 3600 + browser.DOWNLOAD_QUEUED_REFRESH_INTERVAL)

    assert world.status(2) == "missing" and world.status(3) == "queued"


def test_finished_transfer_cleared_from_slskd_waits_for_import_as_downloaded(world):
    queue_release(world, peer("peer", *files(FOLDER, 1, 2, 3)))
    finished = world.deliver("peer", 2)
    world.slskd.transfers.remove(world.slskd.transfer("peer", 2))

    world.poll()

    assert world.status(2) == "downloaded"
    for _ in range(3):
        world.poll(browser.DOWNLOAD_IMPORT_GRACE)
    assert world.status(2) == "downloaded"
    world.import_file(finished, 2)
    world.poll(browser.DOWNLOAD_QUEUED_REFRESH_INTERVAL)

    assert world.status(2) == "found" and world.status(3) == "queued"
    assert not world.tui._failed_sources


def test_removed_transfer_with_failing_audits_slows_down_its_refreshes(world):
    queue_release(world, peer("peer", *files(FOLDER, 1, 2, 3)))
    world.slskd.transfers.remove(world.slskd.transfer("peer", 2))
    world.refresh.side_effect = RuntimeError("Navidrome connection failed: timeout")
    refreshes = world.refresh.call_count

    for _ in range(int(3600 / browser.DOWNLOAD_POLL_INTERVAL)):
        world.poll()

    # The failure stays visible between checks instead of flashing for one poll.
    assert "Navidrome connection failed" in world.tui.download_error
    assert world.status(2) == "queued"
    assert world.refresh.call_count - refreshes <= (
        browser.DOWNLOAD_IMPORT_GRACE / browser.DOWNLOAD_IMPORT_RETRY_INTERVAL
        + 3600 / browser.DOWNLOAD_QUEUED_REFRESH_INTERVAL + 2
    )

    world.refresh.side_effect = None
    world.poll(browser.DOWNLOAD_QUEUED_REFRESH_INTERVAL)

    assert world.tui.download_error == ""
    assert world.status(2) == "missing" and world.status(3) == "queued"
