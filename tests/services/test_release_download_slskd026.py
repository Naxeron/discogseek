"""download_missing_tracks end to end: the real SlskdClient against slskd 0.26 (tests/helpers/slskd026.py).

The release downloader, the slskd client, transfer history and peer policy meet here:
searches stopped at their time limit, reruns over existing transfers, stale queue
replacement, peer failures, search errors, blocked peers and search reuse.
"""

import os
import time
from datetime import timedelta, timezone
from unittest.mock import MagicMock
from urllib.parse import unquote, urlsplit

import pytest

from discogseek.clients.slskd import SlskdAPIError, SlskdClient, SlskdEnqueueError, SlskdEnqueueRefusedError
from discogseek.core.peer_policy import PeerPolicy
from discogseek.services.library import LibraryReleaseService
from discogseek.services.library_download import download_missing_tracks
from tests.helpers.slskd026 import FakeResponse, FakeSlskd026, iso, peer

ARTIST = "Lone"
RELEASE = "Hyperphantasia"
ALBUM_QUERY = "Lone Hyperphantasia"
FOLDER = r"Music\Lone\Hyperphantasia (2019) [FLAC]"
MIRACLE = FOLDER + r"\05 - Miracle Mile.flac"
TRITON = FOLDER + r"\06 - Triton.flac"
TRACKS = [
    {"title": "Miracle Mile", "artist": ARTIST, "track_number": 5, "disc_number": 1, "status": "missing"},
    {"title": "Triton", "artist": ARTIST, "track_number": 6, "disc_number": 1, "status": "missing"},
]
HOUR = 3600.0
DAY = 24 * HOUR


def slskd_moment(moment):
    """requestedAt/enqueuedAt as slskd 0.26 stores them: UTC, 7 fractional digits, no zone."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "1"


class Slskd(FakeSlskd026):
    """FakeSlskd026 plus GET /options and DELETE /transfers/downloads/{user}/{id}."""

    def __init__(self):
        super().__init__()
        self.downloads_dir = None
        # Users whose enqueue slskd carries out while the client's request times out.
        self.lost_replies = set()

    def request(self, method, path, **kwargs):
        route = [unquote(part) for part in urlsplit(path).path.split("/") if part][2:]
        if method == "GET" and route == ["options"]:
            self.requests.append((method, path, None))
            return FakeResponse(200, {"directories": {"downloads": self.downloads_dir}})
        if method == "DELETE" and route[:2] == ["transfers", "downloads"] and len(route) == 4:
            self.requests.append((method, path, kwargs.get("params")))
            for transfer in self.transfers:
                if transfer["username"] == route[2] and transfer["id"] == route[3]:
                    transfer.update(state="Completed, Cancelled", endedAt=iso(self.wall))
                    return FakeResponse(204)
            return FakeResponse(404, text="Not found")
        reply = super().request(method, path, **kwargs)
        if method == "POST" and route[:2] == ["transfers", "downloads"] and route[2:] and route[2] in self.lost_replies:
            raise SlskdAPIError(f"slskd request error [POST {path}]: Read timed out. (read timeout=30.0)")
        return reply

    def seed(self, user, filename, state, age, transfer_id=None, **fields):
        """An existing transfer that slskd was asked for age seconds ago."""
        requested = slskd_moment(self.wall - timedelta(seconds=age))
        self.transfers.append({
            "id": transfer_id or f"seed-{len(self.transfers)}", "username": user, "direction": "Download",
            "filename": filename, "size": 1000, "state": state, "requestedAt": requested,
            "enqueuedAt": requested, **fields,
        })

    def enqueued(self):
        """(user, filename) of every file POSTed to /transfers/downloads, in order."""
        return [(path.rsplit("/", 1)[-1], file["filename"])
                for _, path, body in self.calls("POST", "/api/v0/transfers/downloads/") for file in body]

    def cancels(self):
        return [path for _, path, _ in self.calls("DELETE", "/api/v0/transfers/downloads/")]

    def search_posts(self, text=None):
        return [body["searchText"] for _, _, body in self.calls("POST", "/api/v0/searches")
                if text is None or body["searchText"].casefold() == text.casefold()]

    def state_of(self, transfer_id):
        return next(transfer["state"] for transfer in self.transfers if transfer["id"] == transfer_id)


@pytest.fixture
def slskd(monkeypatch):
    client = SlskdClient(base_url="http://slskd.test", api_key="key")
    fake = Slskd().install(monkeypatch, client)
    # The client's own block check reads the simulated clock, like DownloadHistory.
    client.peer_policy = PeerPolicy()
    return client, fake


def run(client, tracks=TRACKS, **kwargs):
    return download_missing_tracks(client, MagicMock(), ARTIST, RELEASE, [dict(track) for track in tracks], **kwargs)


def titles(rows):
    return sorted(row["title"] for row in rows)


# (a) An album search that keeps receiving responses is stopped at its time limit (PUT);
# the responses slskd stores then still resolve the release.
@pytest.mark.parametrize("complete", [False, True])
def test_album_search_stopped_at_its_time_limit_still_queues_the_release(slskd, complete):
    client, fake = slskd
    noise = [peer(f"noise{index}", rf"Share\Other\{index}.mp3") for index in range(30)]
    responses = noise[:12] + [peer("collector", MIRACLE, TRITON)] + noise[12:]
    # A response every 5 s keeps slskd's 15 s inactivity timeout from ever ending the search.
    fake.plan_search(ALBUM_QUERY, responses, arrivals=[5.0 * index for index in range(1, len(responses) + 1)])

    result = run(client, complete_release_tracks=TRACKS if complete else None)

    assert len(fake.calls("PUT", "/api/v0/searches/")) == 1
    assert fake.search_posts() == [ALBUM_QUERY]
    assert fake.enqueued() == [("collector", MIRACLE), ("collector", TRITON)]
    assert titles(result["queued_tracks"]) == ["Miracle Mile", "Triton"]
    assert result["queue_errors"] == []
    assert result["complete_release_available"] is complete


def test_track_search_stopped_at_its_time_limit_still_queues_the_row(slskd):
    client, fake = slskd
    single = r"Share\Lone\Singles\Lone - Miracle Mile.flac"
    responses = [peer(f"noise{index}", rf"Share\Other\{index}.mp3") for index in range(20)]
    responses.insert(8, peer("single", single))
    fake.plan_search("Lone - Miracle Mile", responses, arrivals=[5.0 * index for index in range(1, 22)])
    fake.plan_search("Lone - Triton", [peer("single", r"Share\Lone\Singles\Lone - Triton.flac")])

    result = run(client)

    stopped = {path for _, path, _ in fake.calls("PUT", "/api/v0/searches/")}
    assert len(stopped) == 1 and fake.peak_running <= 2
    assert fake.enqueued() == [("single", single), ("single", r"Share\Lone\Singles\Lone - Triton.flac")]
    assert titles(result["queued_tracks"]) == ["Miracle Mile", "Triton"] and result["queue_errors"] == []


# (b) Rerunning a request never enqueues a file twice while its transfer is healthy.
@pytest.mark.parametrize("state", [
    "Queued, Locally", "Queued, Remotely", "Requested", "Initializing", "InProgress",
])
def test_rerun_after_enqueue_queues_nothing_twice(slskd, state):
    client, fake = slskd
    fake.plan_search(ALBUM_QUERY, [peer("collector", MIRACLE, TRITON)])
    service = LibraryReleaseService(mb_client=MagicMock(), slskd_client=client)

    first = service.download_missing_tracks(ARTIST, RELEASE, [dict(track) for track in TRACKS])
    for transfer in fake.transfers:
        transfer["state"] = state
    fake.sleep(30 * 60)
    second = service.download_missing_tracks(ARTIST, RELEASE, [dict(track) for track in TRACKS])

    assert fake.enqueued() == [("collector", MIRACLE), ("collector", TRITON)]
    assert titles(first["queued_tracks"]) == titles(second["queued_tracks"]) == ["Miracle Mile", "Triton"]
    assert second["enqueued_files"] == [] and second["queue_errors"] == []
    assert {(file["user"], file["filename"]) for file in second["queued_files"]} == {
        ("collector", MIRACLE), ("collector", TRITON),
    }


def test_rerun_keeps_queued_transfers_whose_peer_no_longer_answers(slskd):
    client, fake = slskd
    fake.plan_search(ALBUM_QUERY, [peer("collector", MIRACLE, TRITON)])
    run(client)
    # The collector went offline; another peer now offers the same release.
    fake.searches.clear()
    fake.plan_search(ALBUM_QUERY, [peer("other", r"Share\Lone - Hyperphantasia\05 - Miracle Mile.flac",
                                        r"Share\Lone - Hyperphantasia\06 - Triton.flac")])

    second = run(client)

    assert [user for user, _ in fake.enqueued()] == ["collector", "collector"]
    assert titles(second["queued_tracks"]) == ["Miracle Mile", "Triton"]
    assert fake.search_posts() == [ALBUM_QUERY]


def test_rerun_keeps_transfers_chosen_from_a_folder_named_only_after_the_release(slskd):
    """The release folder and the position chose the file; they also recognise its transfer."""
    client, fake = slskd
    folder = r"Downloads\Hyperphantasia (2019) [FLAC]"
    fake.plan_search(ALBUM_QUERY, [peer("collector", folder + r"\05 - Miracle Mile.flac",
                                        folder + r"\06 - Triton.flac")])
    first = run(client)
    fake.searches.clear()
    fake.plan_search(ALBUM_QUERY, [peer("other", r"Share\Lone - Hyperphantasia\05 - Miracle Mile.flac",
                                        r"Share\Lone - Hyperphantasia\06 - Triton.flac")])

    second = run(client)

    assert titles(first["queued_tracks"]) == titles(second["queued_tracks"]) == ["Miracle Mile", "Triton"]
    assert {user for user, _ in fake.enqueued()} == {"collector"}
    assert second["enqueued_files"] == []


@pytest.mark.parametrize("state", ["Queued, Remotely", "Completed, Succeeded"])
def test_history_without_the_artist_needs_the_release_position_of_a_live_transfer(slskd, state):
    client, fake = slskd
    other_position = r"Downloads\Hyperphantasia\09 - Miracle Mile.flac"
    same_position = r"Unsorted\Hyperphantasia\05 - Miracle Mile.flac"
    fake.seed("elsewhere", other_position if state.startswith("Queued") else same_position, state, HOUR,
              endedAt=iso(fake.wall))
    fake.plan_search(ALBUM_QUERY, [peer("collector", MIRACLE)])

    result = run(client, tracks=TRACKS[:1])

    # Another position, or a finished download from a folder that may be another
    # artist's album of that name, does not stand for the row.
    assert fake.enqueued() == [("collector", MIRACLE)]
    assert result["queued_tracks"] == TRACKS[:1]


def test_succeeded_download_awaiting_import_is_not_downloaded_again(slskd, monkeypatch, tmp_path):
    client, fake = slskd
    monkeypatch.delenv("DISCOGSEEK_SLSKD_DOWNLOADS_DIR")
    fake.downloads_dir = str(tmp_path / "downloads")
    fake.plan_search(ALBUM_QUERY, [peer("collector", MIRACLE, TRITON)])
    run(client)
    for transfer in fake.transfers:
        transfer.update(state="Completed, Succeeded", endedAt=iso(fake.wall))
    # Miracle Mile still waits in slskd's downloads folder; Triton was imported elsewhere.
    local = tmp_path / "downloads" / "Hyperphantasia (2019) [FLAC]"
    local.mkdir(parents=True)
    (local / "05 - Miracle Mile.flac").write_bytes(b"")
    options_before = len(fake.calls("GET", "/api/v0/options"))

    second = run(client, tracks=TRACKS[:1])

    assert fake.enqueued() == [("collector", MIRACLE), ("collector", TRITON)]
    assert second["queued_tracks"] == TRACKS[:1] and second["enqueued_files"] == []
    # The downloads folder is resolved once per request, not per row or per file.
    assert len(fake.calls("GET", "/api/v0/options")) - options_before == 1


def test_known_downloads_folder_is_used_without_reading_slskd_options(slskd, monkeypatch, tmp_path):
    """The browser already knows slskd's downloads folder; a download request need not read it again."""
    client, fake = slskd
    monkeypatch.delenv("DISCOGSEEK_SLSKD_DOWNLOADS_DIR")
    fake.downloads_dir = str(tmp_path / "elsewhere")
    fake.seed("collector", MIRACLE, "Completed, Succeeded", age=3 * DAY, endedAt=iso(fake.wall - timedelta(days=3)))
    other = r"Share\Lone\Hyperphantasia\05 - Miracle Mile.flac"
    fake.plan_search(ALBUM_QUERY, [peer("other", other)])
    local = tmp_path / "downloads" / "Hyperphantasia (2019) [FLAC]"
    local.mkdir(parents=True)
    (local / "05 - Miracle Mile.flac").write_bytes(b"")

    kept = run(client, tracks=TRACKS[:1], downloads_dir=str(tmp_path / "downloads"))

    assert fake.calls("GET", "/api/v0/options") == []
    assert kept["queued_tracks"] == TRACKS[:1] and fake.enqueued() == []

    # slskd's own setting names a folder without the file: three days on, it is fetched again.
    again = run(client, tracks=TRACKS[:1])

    assert len(fake.calls("GET", "/api/v0/options")) == 1
    assert fake.enqueued() == [("other", other)] and again["queued_tracks"] == TRACKS[:1]


# (c) A transfer waiting in a remote queue for more than 6 h is replaced, and cancelled once.
def test_stale_queued_transfer_is_replaced_and_cancelled_exactly_once(slskd):
    client, fake = slskd
    stale_miracle = r"Music\Lone\Hyperphantasia\05 Miracle Mile.flac"
    stale_triton = r"Music\Lone\Hyperphantasia\06 Triton.flac"
    fake.seed("seiko", stale_miracle, "Queued, Remotely", 12 * DAY, "stale-miracle", attempts=6)
    fake.seed("seiko", stale_triton, "Queued, Remotely", 12 * DAY, "stale-triton", attempts=6)
    fake.plan_search(ALBUM_QUERY, [peer("seiko", stale_miracle, stale_triton), peer("healthy", MIRACLE)])

    first = run(client)
    second = run(client)

    assert fake.enqueued() == [("healthy", MIRACLE)]
    assert fake.cancels() == ["/api/v0/transfers/downloads/seiko/stale-miracle"]
    assert fake.state_of("stale-miracle") == "Completed, Cancelled"
    # Only the replaced row's transfer is stopped: Triton has no other source.
    assert fake.state_of("stale-triton") == "Queued, Remotely"
    for result in (first, second):
        assert titles(result["queued_tracks"]) == ["Miracle Mile"]
        recovery = [error for error in result["queue_errors"] if error.get("stage") == "recovery"]
        assert [titles(error["tracks"]) for error in recovery] == [["Triton"]]
        assert "No alternative sources" in recovery[0]["error"]
        assert "waiting for 12 days without starting" in recovery[0]["error"]
    # Our own cancellation never blocks the peer.
    assert PeerPolicy().blocked_peers() == {}


def test_stale_transfer_absent_from_search_results_is_still_replaced(slskd):
    client, fake = slskd
    stale = r"Music\Lone\Hyperphantasia\05 Miracle Mile.flac"
    fake.seed("seiko", stale, "Queued, Remotely", 3 * DAY, "stale-id")
    fake.plan_search(ALBUM_QUERY, [peer("healthy", MIRACLE)])

    result = run(client, tracks=TRACKS[:1])

    assert fake.enqueued() == [("healthy", MIRACLE)]
    assert fake.cancels() == ["/api/v0/transfers/downloads/seiko/stale-id"]
    assert result["queue_errors"] == []


@pytest.mark.parametrize("failure", [
    (500, "User healthy appears to be offline"),
    (500, "Failed to retrieve endpoint for user healthy"),
])
def test_stale_transfer_stays_queued_when_its_replacement_cannot_be_queued(slskd, failure):
    client, fake = slskd
    stale = r"Music\Lone\Hyperphantasia\05 Miracle Mile.flac"
    fake.seed("seiko", stale, "Queued, Remotely", 3 * DAY, "stale-id")
    fake.plan_search(ALBUM_QUERY, [peer("healthy", MIRACLE)])
    fake.enqueue_user_errors["healthy"] = [failure] * 3

    result = run(client, tracks=TRACKS[:1])

    assert fake.cancels() == []
    assert fake.state_of("stale-id") == "Queued, Remotely"
    assert result["queued_tracks"] == []
    messages = " ".join(error["error"] for error in result["queue_errors"])
    assert "healthy" in messages
    assert "No alternative sources" in messages and "waiting for 3 days" in messages


@pytest.mark.parametrize("serving_state, ended_hours, replaced", [
    ("InProgress", None, False),
    ("Completed, Succeeded", 2, False),
    ("Completed, Succeeded", 7, True),
])
def test_waiting_transfer_of_a_peer_still_serving_us_is_not_stale(slskd, serving_state, ended_hours, replaced):
    """A peer working through a long album queue keeps it, as the browser's stale check does."""
    client, fake = slskd
    album = r"Music\Lone\Hyperphantasia"
    ended = {"endedAt": iso(fake.wall - timedelta(hours=ended_hours))} if ended_hours else {}
    fake.seed("slow", album + r"\04 Lucid.flac", serving_state, 8 * HOUR, "serving-id", **ended)
    fake.seed("slow", album + r"\05 Miracle Mile.flac", "Queued, Remotely", 8 * HOUR, "waiting-id")
    fake.plan_search(ALBUM_QUERY, [peer("healthy", MIRACLE)])

    result = run(client, tracks=TRACKS[:1])

    assert fake.enqueued() == ([("healthy", MIRACLE)] if replaced else [])
    assert fake.cancels() == (["/api/v0/transfers/downloads/slow/waiting-id"] if replaced else [])
    assert [file["user"] for file in result["queued_files"]] == ["healthy" if replaced else "slow"]
    assert result["queue_errors"] == []


@pytest.fixture
def local_utc_plus_5():
    """A local clock five hours ahead of UTC, as on the machine that runs discogseek."""
    original = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Yekaterinburg"
    time.tzset()
    yield
    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    time.tzset()


def test_staleness_reads_slskd_zoneless_timestamps_as_utc(slskd, local_utc_plus_5):
    """slskd stores requestedAt/enqueuedAt in UTC without a zone; a local UTC+5 clock must not age them."""
    client, fake = slskd
    fake.seed("waiting", r"Music\Lone\Hyperphantasia\05 Miracle Mile.flac", "Queued, Remotely",
              5.5 * HOUR, "recent-id")
    fake.seed("waiting", r"Music\Lone\Hyperphantasia\06 Triton.flac", "Queued, Remotely",
              6.5 * HOUR, "stale-id")
    fake.plan_search(ALBUM_QUERY, [peer("healthy", MIRACLE, TRITON)])

    result = run(client)

    assert fake.enqueued() == [("healthy", TRITON)]
    assert fake.cancels() == ["/api/v0/transfers/downloads/waiting/stale-id"]
    assert {(file["user"], file["title"]) for file in result["queued_files"]} == {
        ("waiting", "Miracle Mile"), ("healthy", "Triton"),
    }


# (d) Peer failures.
def test_offline_peer_falls_back_to_the_next_candidate(slskd):
    client, fake = slskd
    mp3_folder = r"Share\Lone - Hyperphantasia (2019) [MP3]"
    fake.plan_search(ALBUM_QUERY, [
        peer("offline", MIRACLE, TRITON),
        peer("online", mp3_folder + r"\05 - Miracle Mile.mp3", mp3_folder + r"\06 - Triton.mp3"),
    ])
    fake.enqueue_user_errors["offline"] = [(500, "User offline appears to be offline")]

    result = run(client)

    assert [user for user, _ in fake.enqueued()] == ["offline", "offline", "online", "online"]
    assert len(fake.calls("POST", "/api/v0/transfers/downloads/offline")) == 1
    assert {transfer["username"] for transfer in fake.transfers} == {"online"}
    assert titles(result["queued_tracks"]) == ["Miracle Mile", "Triton"]
    assert result["queue_errors"] == []


@pytest.mark.parametrize("reason", ["User offline appears to be offline", "Failed to retrieve endpoint for user offline"])
def test_selected_track_falls_back_when_its_best_peer_is_unavailable(slskd, reason):
    client, fake = slskd
    mp3 = r"Share\Lone - Hyperphantasia (2019) [MP3]\05 - Miracle Mile.mp3"
    fake.plan_search("Lone Miracle Mile", [peer("offline", MIRACLE), peer("online", mp3)])
    fake.enqueue_user_errors["offline"] = [(500, reason)] * 3

    result = run(client, tracks=TRACKS[:1], search_scope="track")

    assert fake.search_posts() == ["Lone Miracle Mile"]
    assert {transfer["username"] for transfer in fake.transfers} == {"online"}
    assert result["queued_tracks"] == TRACKS[:1] and result["queue_errors"] == []


@pytest.mark.parametrize("style", ["opaque", "tuple", "named"])
def test_files_slskd_refuses_are_taken_from_another_candidate(slskd, style):
    client, fake = slskd
    mp3 = r"Share\Lone - Hyperphantasia [MP3]\05 - Miracle Mile.mp3"
    fake.plan_search(ALBUM_QUERY, [peer("refusing", MIRACLE, TRITON), peer("serving", mp3)])
    fake.failed_entry_style = style
    fake.enqueue_errors[MIRACLE] = "File not shared."

    result = run(client)

    assert {(transfer["username"], transfer["filename"]) for transfer in fake.transfers} == {
        ("refusing", TRITON), ("serving", mp3),
    }
    assert titles(result["queued_tracks"]) == ["Miracle Mile", "Triton"]
    assert result["queue_errors"] == []


def test_busy_enqueue_is_retried_without_duplicates(slskd):
    client, fake = slskd
    fake.plan_search(ALBUM_QUERY, [peer("collector", MIRACLE, TRITON)])
    fake.busy_enqueue_posts = 2

    result = run(client)

    assert len(fake.calls("POST", "/api/v0/transfers/downloads/collector")) == 3
    assert [transfer["filename"] for transfer in fake.transfers] == [MIRACLE, TRITON]
    assert titles(result["queued_tracks"]) == ["Miracle Mile", "Triton"]
    assert result["queue_errors"] == []


def test_enqueue_still_busy_after_retries_is_taken_from_the_next_candidate(slskd):
    client, fake = slskd
    mp3_folder = r"Share\Lone - Hyperphantasia (2019) [MP3]"
    fake.plan_search(ALBUM_QUERY, [
        peer("collector", MIRACLE, TRITON),
        peer("other", mp3_folder + r"\05 - Miracle Mile.mp3", mp3_folder + r"\06 - Triton.mp3"),
    ])
    fake.busy_enqueue_posts = 4

    result = run(client)

    assert len(fake.calls("POST", "/api/v0/transfers/downloads/collector")) == 4
    assert {transfer["username"] for transfer in fake.transfers} == {"other"}
    assert titles(result["queued_tracks"]) == ["Miracle Mile", "Triton"]
    assert result["queue_errors"] == []


@pytest.mark.parametrize("failure", ["lost reply", "server error"])
def test_uncertain_enqueue_failure_never_queues_a_second_copy(slskd, failure):
    """slskd may have queued the files: another peer's copy would download them twice."""
    client, fake = slskd
    mp3_folder = r"Share\Lone - Hyperphantasia (2019) [MP3]"
    fake.plan_search(ALBUM_QUERY, [
        peer("collector", MIRACLE, TRITON),
        peer("other", mp3_folder + r"\05 - Miracle Mile.mp3", mp3_folder + r"\06 - Triton.mp3"),
    ])
    if failure == "lost reply":
        fake.lost_replies.add("collector")
    else:
        fake.enqueue_user_errors["collector"] = [(500, "An unexpected error occurred")]

    first = run(client)
    fake.lost_replies.clear()
    second = run(client)

    assert {user for user, _ in fake.enqueued()} == {"collector"}
    assert first["queued_tracks"] == [] and titles(first["matched_tracks"]) == ["Miracle Mile", "Triton"]
    assert [(error["user"], titles(error["tracks"])) for error in first["queue_errors"]] == [
        ("collector", ["Miracle Mile", "Triton"]),
    ]
    if failure == "lost reply":
        # The next request finds slskd's transfers and keeps them.
        assert len(fake.transfers) == 2
        assert titles(second["queued_tracks"]) == ["Miracle Mile", "Triton"] and second["enqueued_files"] == []
    else:
        assert {transfer["username"] for transfer in fake.transfers} == {"collector"}
        assert titles(second["queued_tracks"]) == ["Miracle Mile", "Triton"]


def test_failed_file_with_an_earlier_finished_download_is_not_taken_as_queued(slskd):
    """slskd skips only transfers that have not completed; an old success says nothing about this one."""
    client, fake = slskd
    fake.seed("collector", TRITON, "Completed, Succeeded", 2 * DAY, "old-download",
              endedAt=iso(fake.wall - timedelta(days=2)))
    fake.enqueue_errors[TRITON] = "database is locked"
    mp3 = r"Share\Lone - Hyperphantasia [MP3]\06 - Triton.mp3"
    fake.plan_search(ALBUM_QUERY, [peer("collector", MIRACLE, TRITON), peer("other", mp3)])

    result = run(client)

    assert {(transfer["username"], transfer["filename"]) for transfer in fake.transfers if
            transfer["id"] != "old-download"} == {("collector", MIRACLE), ("other", mp3)}
    assert titles(result["queued_tracks"]) == ["Miracle Mile", "Triton"]
    assert result["queue_errors"] == []


@pytest.mark.parametrize("reply, refused", [
    ("partial", True), ((429, "busy"), True), ((400, "Bad request"), True),
    ((500, "An unexpected error occurred"), False), ("lost reply", False),
])
def test_client_tells_refused_enqueues_from_uncertain_ones(slskd, reply, refused):
    client, fake = slskd
    files = [{"filename": MIRACLE, "size": 1000}, {"filename": TRITON, "size": 1000}]
    if reply == "partial":
        fake.enqueue_errors[TRITON] = "File not shared."
    elif reply == "lost reply":
        fake.lost_replies.add("collector")
    else:
        fake.enqueue_user_errors["collector"] = [reply] * 4

    with pytest.raises(SlskdEnqueueError) as raised:
        client.enqueue_download("collector", files)

    assert isinstance(raised.value, SlskdEnqueueRefusedError) is refused
    assert raised.value.queued_files == ([files[0]] if reply == "partial" else [])


# (e) Searches that fail are retryable errors, dropped once the row is queued by another search.
def test_failed_searches_are_retryable_and_dropped_for_rows_queued_later(slskd):
    client, fake = slskd
    fake.search_post_errors[ALBUM_QUERY.casefold()] = [(409, "Conflict")]
    fake.errored_searches["lone - triton"] = 2.0
    fake.plan_search("Lone - Miracle Mile", [peer("single", r"Share\Lone\Singles\Lone - Miracle Mile.flac")])

    result = run(client)

    assert titles(result["queued_tracks"]) == ["Miracle Mile"]
    errors = result["queue_errors"]
    assert errors and all(error["stage"] == "search" for error in errors)
    assert all(titles(error["tracks"]) == ["Triton"] for error in errors)
    assert any("HTTP 409" in error["error"] for error in errors)
    assert any("errored" in error["error"] for error in errors)


def test_failed_track_search_is_dropped_when_a_later_variant_queues_the_row(slskd):
    client, fake = slskd
    fake.search_post_errors["lone - triton"] = [(409, "Conflict")]
    fake.plan_search("Lone - Miracle Mile", [peer("single", r"Share\Lone\Singles\Lone - Miracle Mile.flac")])
    fake.plan_search("Lone Triton", [peer("single", r"Share\Lone\Singles\Lone - Triton.flac")])

    result = run(client)

    assert titles(result["queued_tracks"]) == ["Miracle Mile", "Triton"]
    assert result["queue_errors"] == []


# (f) Peer policy: banned peers are never used; a transient refusal cools down and expires.
def test_banned_peer_is_never_used(slskd):
    client, fake = slskd
    fake.seed("banned", r"Other\Album\01 Song.flac", "Completed, Rejected", DAY, "banned-id",
              exception="Banned", endedAt=iso(fake.wall - timedelta(days=1)))
    mp3 = r"Share\Lone - Hyperphantasia [MP3]\05 - Miracle Mile.mp3"
    fake.plan_search(ALBUM_QUERY, [peer("banned", MIRACLE), peer("other", mp3)])

    result = run(client, tracks=TRACKS[:1])

    assert fake.enqueued() == [("other", mp3)]
    assert result["queue_errors"] == []


def test_transient_refusal_blocks_the_peer_only_until_its_cooldown_expires(slskd):
    client, fake = slskd
    fake.seed("cooling", MIRACLE, "Completed, Rejected", HOUR, "refused-id",
              exception="Too many files", endedAt=iso(fake.wall - timedelta(hours=1)))
    fake.plan_search(ALBUM_QUERY, [peer("cooling", MIRACLE)])

    during = run(client, tracks=TRACKS[:1])
    fake.sleep(5.5 * HOUR)
    after = run(client, tracks=TRACKS[:1])

    assert during["queued_tracks"] == [] and "Too many files" in during["queue_errors"][0]["error"]
    assert fake.enqueued() == [("cooling", MIRACLE)]
    assert after["queued_tracks"] == TRACKS[:1] and after["queue_errors"] == []


# (g) Search reuse: one slskd search per query within an hour; dead searches are ignored.
def test_consecutive_requests_reuse_the_recent_search_and_ignore_zombies(slskd):
    client, fake = slskd
    zombie = fake.add_history(ALBUM_QUERY, state="InProgress", age=2 * HOUR)
    fake.plan_search(ALBUM_QUERY, [peer("collector", MIRACLE, TRITON)])

    first = run(client, dry_run=True)
    fake.sleep(20 * 60)
    second = run(client, dry_run=True)
    fake.sleep(45 * 60)
    third = run(client, dry_run=True)

    assert f"/api/v0/searches/{zombie}" in [path for _, path, _ in fake.calls("DELETE", "/api/v0/searches/")]
    assert fake.search_posts() == [ALBUM_QUERY, ALBUM_QUERY]
    for result in (first, second, third):
        assert titles(result["matched_tracks"]) == ["Miracle Mile", "Triton"]
        assert {file["user"] for file in result["queued_files"]} == {"collector"}


@pytest.mark.parametrize("use_existing", [True, False])
def test_fresh_searches_skip_slskds_recent_history(slskd, use_existing):
    """A refreshing caller asks the network again instead of reusing a search slskd finished ten minutes ago."""
    client, fake = slskd
    fake.add_history(ALBUM_QUERY, age=10 * 60, responses=[peer("earlier", FOLDER + r"\05 - Miracle Mile.mp3")])
    fake.plan_search(ALBUM_QUERY, [peer("now", MIRACLE)])

    result = run(client, tracks=TRACKS[:1], dry_run=True, use_existing=use_existing)

    assert fake.search_posts() == ([] if use_existing else [ALBUM_QUERY])
    assert [file["user"] for file in result["queued_files"]] == ["earlier" if use_existing else "now"]


def test_search_stopped_at_its_time_limit_is_reused_by_the_next_request(slskd):
    """A preview and then the download: the capped album search is not run (and stopped) twice."""
    client, fake = slskd
    responses = [peer(f"noise{index}", rf"Share\Other\{index}.mp3") for index in range(30)]
    responses.insert(12, peer("collector", MIRACLE, TRITON))
    fake.plan_search(ALBUM_QUERY, responses, arrivals=[5.0 * index for index in range(1, len(responses) + 1)])

    preview = run(client, dry_run=True)
    fake.sleep(10 * 60)
    download = run(client)

    assert fake.search_posts() == [ALBUM_QUERY]
    assert len(fake.calls("PUT", "/api/v0/searches/")) == 1
    assert titles(preview["matched_tracks"]) == titles(download["queued_tracks"]) == ["Miracle Mile", "Triton"]


@pytest.mark.parametrize("method", ["search", "batch_search"])
@pytest.mark.parametrize("duration, reused", [(80.0, True), (30.0, False)])
def test_client_reuses_a_cancelled_search_only_when_stopped_at_the_time_limit(slskd, method, duration, reused):
    client, fake = slskd
    fake.add_history("Hyperphantasia", state="Completed, Cancelled", age=10 * 60, duration=duration,
                     responses=[peer("old", r"Music\Hyperphantasia\01.flac")])
    fake.plan_search("Hyperphantasia", [peer("new", r"Music\Hyperphantasia\01.flac")])

    if method == "search":
        result = client.search("Hyperphantasia", timeout=28)
    else:
        result = client.batch_search(["Hyperphantasia"], timeout=28)["Hyperphantasia"]

    assert fake.search_posts() == ([] if reused else ["Hyperphantasia"])
    assert [response["username"] for response in result["responses"]] == ["old" if reused else "new"]


def test_search_stopped_early_by_someone_is_searched_again(slskd):
    client, fake = slskd
    fake.add_history(ALBUM_QUERY, state="Completed, Cancelled", age=5 * 60, duration=8.0,
                     responses=[peer("early", r"Share\Other\1.mp3")])
    fake.plan_search(ALBUM_QUERY, [peer("collector", MIRACLE, TRITON)])

    result = run(client, dry_run=True)

    assert fake.search_posts() == [ALBUM_QUERY]
    assert titles(result["matched_tracks"]) == ["Miracle Mile", "Triton"]


def test_track_and_album_phases_search_a_shared_query_once(slskd):
    client, fake = slskd
    single = {"title": RELEASE, "artist": ARTIST, "track_number": 1, "disc_number": 1}

    run(client, tracks=[single], search_scope="track", dry_run=True)

    assert fake.search_posts(ALBUM_QUERY) == [ALBUM_QUERY]
    assert len(fake.search_posts()) == len({query.casefold() for query in fake.search_posts()})
