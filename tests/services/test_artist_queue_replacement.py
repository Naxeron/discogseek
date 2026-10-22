"""Artist queueing replaces stale transfers only once the replacement is queued, and
retries files slskd refused from other sources."""

import pytest

from discogseek.clients.slskd import (
    SlskdAPIError,
    SlskdEnqueueError,
    SlskdEnqueueRefusedError,
    SlskdPeerUnavailableError,
)
from discogseek.core.transfers import source_key
from discogseek.services.library import LibraryReleaseService
from tests.helpers.artist_mode import (
    FakeArtistClient,
    add_dir,
    build_index,
    history_file,
    raw_catalog,
    release,
    scraper_for,
)

LONG_AGO = "2020-01-01T00:00:00.0000000Z"
FOLDER = "Music\\Target Artist\\Night Signals"


class OrderedClient(FakeArtistClient):
    """Records submissions and cancellations in order; a callable failure sees the submitted files."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.events = []

    def enqueue_download(self, user, files):
        self.calls.append((user, list(files)))
        self.events.append(("enqueue", user))
        failure = self.failures.get(user)
        if callable(failure):
            failure = failure(files)
        if failure is not None:
            self.accepted.extend(failure.queued_files)
            raise failure
        self.accepted.extend(files)
        return {"status": "enqueued", "files_count": len(files)}

    def cancel_download(self, user, transfer_id):
        self.events.append(("cancel", transfer_id))
        super().cancel_download(user, transfer_id)


def stale(user, filename, transfer_id):
    return history_file(user, filename, "Queued, Remotely", transfer_id=transfer_id,
                        requestedAt=LONG_AGO, enqueuedAt=LONG_AGO)


def refused(user, reason="File not shared", queued=()):
    return SlskdEnqueueRefusedError(f"slskd did not queue 1 of 2 files from {user}: {reason}", list(queued))


# ----------------------------------------------------------------------
# Stale transfers wait for their replacement
# ----------------------------------------------------------------------


@pytest.mark.parametrize("failure", [
    SlskdPeerUnavailableError("new-peer", "offline"),
    SlskdPeerUnavailableError("new-peer", "unreachable"),
    refused("new-peer"),
    SlskdEnqueueError("Failed to enqueue download from new-peer (HTTP 500): boom"),
], ids=["offline", "unreachable", "refused", "unknown"])
def test_stale_transfer_survives_a_replacement_that_was_not_queued(failure):
    client = OrderedClient(
        history=[stale("seiko", f"{FOLDER}\\01 Neon Memory.mp3", "stale-1")],
        failures={"new-peer": failure},
    )
    scraper = scraper_for(raw_catalog([release("Night Signals", ["Neon Memory"])]), client)
    files = add_dir(scraper, "new-peer", FOLDER, ["01 Neon Memory"])
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert client.calls == [("new-peer", files)]
    # Nothing replaced seiko's copy: it may still be served, so it keeps its place.
    assert client.cancelled == []
    assert scraper.enqueued_count == 0


def test_stale_transfer_is_cancelled_after_its_replacement_is_queued():
    client = OrderedClient(history=[stale("seiko", f"{FOLDER}\\01 Neon Memory.mp3", "stale-1")])
    scraper = scraper_for(raw_catalog([release("Night Signals", ["Neon Memory"])]), client)
    files = add_dir(scraper, "new-peer", FOLDER, ["01 Neon Memory"])
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert client.calls == [("new-peer", files)]
    assert client.events == [("enqueue", "new-peer"), ("cancel", "stale-1")]
    assert scraper.queue_errors == []


@pytest.mark.parametrize("make_failure", [
    lambda files: refused("new-peer", queued=files[:1]),
    lambda files: SlskdPeerUnavailableError("new-peer", "unreachable", files[:1]),
], ids=["refused", "unreachable"])
def test_partial_submission_cancels_only_the_stale_transfers_it_replaced(make_failure):
    client = OrderedClient(
        history=[
            stale("seiko", f"{FOLDER}\\01 Neon Memory.mp3", "stale-1"),
            stale("seiko", f"{FOLDER}\\02 Midnight Echo.mp3", "stale-2"),
        ],
        failures={"new-peer": make_failure},
    )
    scraper = scraper_for(raw_catalog([release("Night Signals", ["Neon Memory", "Midnight Echo"])]), client)
    files = add_dir(scraper, "new-peer", FOLDER, ["01 Neon Memory", "02 Midnight Echo"])
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert client.calls == [("new-peer", files)]
    assert client.accepted == files[:1]
    assert client.cancelled == [("seiko", "stale-1")]


@pytest.mark.parametrize("fallback_available", [True, False])
def test_single_track_stale_transfer_waits_for_an_accepted_fallback(fallback_available):
    client = OrderedClient(
        history=[stale("seiko", "Music\\Target Artist\\Singles\\Target Artist - Neon Memory.mp3", "stale-1")],
        failures={"best": SlskdPeerUnavailableError("best", "offline")},
    )
    scraper = scraper_for(raw_catalog([]), client)
    best = add_dir(scraper, "best", "Music\\Target Artist\\Singles", ["Target Artist - Neon Memory"])
    if fallback_available:
        fallback = add_dir(scraper, "fallback", "Music\\Target Artist\\Loose", ["Target Artist - Neon Memory"], "mp3")
    build_index(scraper)
    scraper.verified_standalone_tracks = [{"user": "best", "track": "Neon Memory", "file": best[0]}]

    scraper._queue_downloads()

    if fallback_available:
        assert client.calls == [("best", best), ("fallback", fallback)]
        assert client.events == [("enqueue", "best"), ("enqueue", "fallback"), ("cancel", "stale-1")]
    else:
        assert client.calls == [("best", best)]
        assert client.cancelled == []


def test_failed_stale_cancellation_after_replacement_is_reported():
    client = OrderedClient(history=[stale("seiko", f"{FOLDER}\\01 Neon Memory.mp3", "stale-1")])
    scraper = scraper_for(raw_catalog([release("Night Signals", ["Neon Memory"])]), client)
    files = add_dir(scraper, "new-peer", FOLDER, ["01 Neon Memory"])
    build_index(scraper)

    def fail(user, transfer_id):
        raise SlskdAPIError("Could not stop retries (HTTP 500)")

    client.cancel_download = fail
    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    # The replacement stays queued; the run reports that the old copy may still start.
    assert client.calls == [("new-peer", files)]
    assert scraper.enqueued_count == 1
    assert len(scraper.queue_errors) == 1
    assert "stale transfer it replaces may still start" in scraper.queue_errors[0]
    assert "Could not stop retries" in scraper.queue_errors[0]


def test_stale_and_failed_copies_of_a_song_are_stopped_around_its_replacement():
    client = OrderedClient(history=[
        stale("seiko", f"{FOLDER}\\01 Neon Memory.mp3", "stale-1"),
        history_file("kaz", f"{FOLDER}\\01 Neon Memory.ogg", "Completed, TimedOut",
                     transfer_id="failed-1", attempts=100),
    ])
    scraper = scraper_for(raw_catalog([release("Night Signals", ["Neon Memory"])]), client)
    add_dir(scraper, "new-peer", FOLDER, ["01 Neon Memory"])
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    # The failed copy is stopped before the submission, the stale one after slskd accepted it.
    assert client.events == [("cancel", "failed-1"), ("enqueue", "new-peer"), ("cancel", "stale-1")]
    assert scraper.queue_errors == []


def test_failed_transfer_is_still_stopped_before_its_replacement_is_submitted():
    client = OrderedClient(
        history=[history_file("seiko", f"{FOLDER}\\01 Neon Memory.mp3", "Completed, TimedOut",
                              transfer_id="failed-1", attempts=100)],
        failures={"new-peer": SlskdPeerUnavailableError("new-peer", "offline")},
    )
    scraper = scraper_for(raw_catalog([release("Night Signals", ["Neon Memory"])]), client)
    add_dir(scraper, "new-peer", FOLDER, ["01 Neon Memory"])
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert client.events == [("cancel", "failed-1"), ("enqueue", "new-peer")]


# ----------------------------------------------------------------------
# Files slskd refused come from the next source
# ----------------------------------------------------------------------


def test_refused_folder_falls_back_without_blocking_the_peer():
    client = OrderedClient(failures={
        "best": lambda files: refused("best") if "Night Signals" in files[0]["filename"] else None,
    })
    scraper = scraper_for(raw_catalog([
        release("Night Signals", ["Neon Memory"], 0), release("Second Wind", ["Paper Comets"], 1),
    ]), client)
    best = add_dir(scraper, "best", FOLDER, ["01 Neon Memory"])
    fallback = add_dir(scraper, "fallback", FOLDER, ["01 Neon Memory"], "mp3")
    second = add_dir(scraper, "best", "Music\\Target Artist\\Second Wind", ["01 Paper Comets"])
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    # The refusal names files, not the peer: best still supplies the other album.
    assert client.calls == [("best", best), ("fallback", fallback), ("best", second)]
    assert client.accepted == fallback + second
    assert scraper.enqueued_count == 2
    assert scraper.queue_errors == []


def test_refused_folder_retires_the_stale_copy_once_the_next_source_accepted_it():
    client = OrderedClient(
        history=[stale("seiko", f"{FOLDER}\\01 Neon Memory.ogg", "stale-1")],
        failures={"best": refused("best")},
    )
    scraper = scraper_for(raw_catalog([release("Night Signals", ["Neon Memory"])]), client)
    best = add_dir(scraper, "best", FOLDER, ["01 Neon Memory"])
    fallback = add_dir(scraper, "fallback", FOLDER, ["01 Neon Memory"], "mp3")
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert client.calls == [("best", best), ("fallback", fallback)]
    assert client.events == [("enqueue", "best"), ("enqueue", "fallback"), ("cancel", "stale-1")]
    assert scraper.queue_errors == []


def test_refused_folder_without_other_source_is_searched_again_without_it(monkeypatch):
    client = OrderedClient(failures={"best": refused("best")})
    scraper = scraper_for(raw_catalog([release("Night Signals", ["Neon Memory"])]), client)
    best = add_dir(scraper, "best", FOLDER, ["01 Neon Memory"])
    build_index(scraper)
    requests = []

    def download_missing_tracks(self, **kwargs):
        requests.append(kwargs)
        return {"queued_tracks": [], "matched_tracks": [], "enqueued_files": [], "queue_errors": []}

    monkeypatch.setattr(LibraryReleaseService, "download_missing_tracks", download_missing_tracks)
    scraper._reconcile_primary_releases()
    scraper._queue_downloads()
    scraper._search_unfilled_tracks()

    assert client.calls == [("best", best)]
    assert len(scraper.queue_errors) == 1
    # slskd's reason ends its own sentence before the summary.
    assert "File not shared. 1 matched files could not be queued from the available sources." in (
        scraper.queue_errors[0]
    )
    # The track search may find another copy, but not the refused one.
    assert [row["title"] for row in requests[0]["missing_tracks"]] == ["Neon Memory"]
    assert source_key("best", best[0]["filename"]) in requests[0]["excluded_sources"]


@pytest.mark.parametrize("kind", ["standalone", "compilation"])
def test_refused_single_track_falls_back_to_the_next_candidate(kind):
    neon = "Music\\Target Artist\\Singles\\Target Artist - Neon Memory.flac"
    client = OrderedClient(failures={
        "best": lambda files: refused("best", queued=[file for file in files if file["filename"] != neon]),
    })
    scraper = scraper_for(raw_catalog([]), client)
    best = add_dir(scraper, "best", "Music\\Target Artist\\Singles",
                   ["Target Artist - Neon Memory", "Target Artist - Paper Comets"])
    fallback = add_dir(scraper, "fallback", "Music\\Target Artist\\Loose", ["Target Artist - Neon Memory"], "mp3")
    build_index(scraper)
    items = [{"user": "best", "track": title, "file": file}
             for title, file in zip(["Neon Memory", "Paper Comets"], best)]
    if kind == "compilation":
        for item in items:
            item["release"] = "Singles"
    setattr(scraper, f"verified_{kind}_tracks", items)

    scraper._queue_downloads()

    assert client.calls == [("best", best), ("fallback", fallback)]
    assert client.accepted == best[1:] + fallback
    assert items[0]["user"] == "fallback"
    assert scraper.enqueued_count == 2
    assert scraper.queue_errors == []


def test_refused_fallback_source_moves_on_to_the_next_one():
    client = OrderedClient(failures={
        "best": SlskdPeerUnavailableError("best", "offline"),
        "second": refused("second"),
    })
    scraper = scraper_for(raw_catalog([]), client)
    best = add_dir(scraper, "best", "Music\\Target Artist\\Singles", ["Target Artist - Neon Memory"])
    second = add_dir(scraper, "second", "Music\\Target Artist\\Loose", ["Target Artist - Neon Memory"])
    third = add_dir(scraper, "third", "Music\\Target Artist\\Other", ["Target Artist - Neon Memory"], "mp3")
    build_index(scraper)
    item = {"user": "best", "track": "Neon Memory", "file": best[0]}
    scraper.verified_standalone_tracks = [item]

    scraper._queue_downloads()

    # A refusal by one fallback source is no reason to stop: the next one supplies the song.
    assert client.calls == [("best", best), ("second", second), ("third", third)]
    assert client.accepted == third
    assert item["user"] == "third"
    assert scraper.queue_errors == []


def test_unknown_single_track_error_is_still_not_replayed():
    client = OrderedClient(failures={"best": SlskdEnqueueError("Failed to enqueue download from best (HTTP 502)")})
    scraper = scraper_for(raw_catalog([]), client)
    best = add_dir(scraper, "best", "Music\\Target Artist\\Singles", ["Target Artist - Neon Memory"])
    add_dir(scraper, "fallback", "Music\\Target Artist\\Loose", ["Target Artist - Neon Memory"], "mp3")
    build_index(scraper)
    scraper.verified_standalone_tracks = [{"user": "best", "track": "Neon Memory", "file": best[0]}]

    scraper._queue_downloads()

    assert client.calls == [("best", best)]
    assert len(scraper.queue_errors) == 1
    assert "HTTP 502" in scraper.queue_errors[0]
