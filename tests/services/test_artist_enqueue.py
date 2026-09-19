"""Artist queue recovery with unavailable peers and partial submissions."""

import pytest

from discogseek.clients.musicbrainz import ArtistCatalog
from discogseek.clients.slskd import SlskdAPIError, SlskdEnqueueError, SlskdPeerUnavailableError
from discogseek.services.soulseek import SlskdArtistScraper


class FakeSlskdClient:
    def __init__(self, failures=None):
        self.failures = failures or {}
        self.calls = []
        self.accepted = []
        self.history = []
        self.cancelled = []

    def get_queued_filenames(self):
        return {file["filename"] for file in self.accepted}

    def get_downloads(self):
        return self.history + [{"username": "peer", "directories": [{"files": [
            dict(file, state="InProgress") for file in self.accepted
        ]}]}]

    def cancel_download(self, user, transfer_id):
        self.cancelled.append((user, transfer_id))

    def get_queued_track_fingerprints(self):
        return {
            "base_filenames": {
                filename.replace("/", "\\").split("\\")[-1].lower()
                for filename in self.get_queued_filenames()
            },
            "clean_titles": set(),
            "full_paths": self.get_queued_filenames(),
        }

    def enqueue_download(self, user, files):
        self.calls.append((user, list(files)))
        failure = self.failures.get(user)
        if callable(failure):
            failure = failure(files)
        if failure is not None:
            self.accepted.extend(failure.queued_files)
            raise failure
        self.accepted.extend(files)
        return {"status": "enqueued", "files_count": len(files)}


def artist_scraper(client, releases=None):
    scraper = SlskdArtistScraper("Target Artist", slskd_client=client)
    scraper.all_artist_aliases = {"target artist"}
    scraper.catalog = ArtistCatalog({
        "artist": {"id": "artist", "name": "Target Artist"},
        "releases_artist": [
            {
                "id": f"release-{release_index}",
                "title": title,
                "medium-list": [{"track-list": [
                    {"id": f"track-{release_index}-{track_index}", "title": track}
                    for track_index, track in enumerate(tracks)
                ]}],
            }
            for release_index, (title, tracks) in enumerate((releases or {}).items())
        ],
        "releases_track_artist": [],
        "recordings": [],
    })
    return scraper


def add_peer_files(scraper, user, release, titles, extension="flac"):
    directory = f"{user}\\Target Artist\\{release}"
    files = [
        {"filename": f"{directory}\\{index:02d} {title}.{extension}", "size": 1000}
        for index, title in enumerate(titles, 1)
    ]
    scraper.peer_directories[(user, directory)] = {
        "user": user,
        "directory": directory,
        "matched_search_files": files,
        "full_directory_files": files,
        "queue": 0,
        "speed": 5000,
        "has_slot": True,
    }
    return files


def queue_directory(user, directory, files):
    return {"user": user, "directory": directory, "all_dir_files": files}


def test_unavailable_peer_is_attempted_once_across_directories_and_singles():
    client = FakeSlskdClient({"offline": SlskdPeerUnavailableError("offline", "offline")})
    scraper = artist_scraper(client)
    scraper.queued_directories = [
        queue_directory("offline", "First Album", [{"filename": "First Album\\First.flac"}]),
        queue_directory("offline", "Second Album", [{"filename": "Second Album\\Second.flac"}]),
    ]
    scraper.verified_compilation_tracks = [{
        "user": "offline", "release": "Compilation", "track": "Third Song",
        "file": {"filename": "Compilation\\Third Song.flac"},
    }]
    scraper.verified_standalone_tracks = [{
        "user": "offline", "track": "Fourth Song",
        "file": {"filename": "Singles\\Fourth Song.flac"},
    }]

    scraper._queue_downloads()

    assert [user for user, _ in client.calls] == ["offline"]
    assert scraper.enqueued_count == 0
    assert scraper.already_downloading_files == set()
    assert len(scraper.queue_errors) == 1
    assert "Soulseek user offline is offline." in scraper.queue_errors[0]
    assert "4 matched files" in scraper.queue_errors[0]
    assert "again later" in scraper.queue_errors[0]
    assert "HTTP 500" not in scraper.queue_errors[0]


@pytest.mark.parametrize("reason", ["offline", "unreachable"])
def test_reconciled_release_falls_back_from_best_quality_unavailable_peer(reason):
    client = FakeSlskdClient({"best": SlskdPeerUnavailableError("best", reason)})
    scraper = artist_scraper(client, {"Night Signals": ["Neon Memory", "Midnight Echo"]})
    add_peer_files(scraper, "best", "Night Signals", ["Neon Memory", "Midnight Echo"])
    fallback_files = add_peer_files(
        scraper, "fallback", "Night Signals", ["Neon Memory", "Midnight Echo"], "mp3",
    )
    scraper._reconcile_primary_releases()
    assert scraper.queued_directories[0]["user"] == "best"
    assert scraper.verified_releases[0]["format_label"].startswith("FLAC")

    scraper._queue_downloads()

    assert [user for user, _ in client.calls] == ["best", "fallback"]
    assert client.accepted == fallback_files
    assert scraper.enqueued_count == 2
    assert scraper.enqueued_files == [{**file, "user": "fallback"} for file in fallback_files]
    assert scraper.queue_errors == []


def test_failed_enqueue_does_not_block_later_peer_with_same_filename():
    client = FakeSlskdClient({"offline": SlskdPeerUnavailableError("offline", "offline")})
    scraper = artist_scraper(client)
    files = [{"filename": "Album\\Neon Memory.flac", "size": 1000}]
    scraper.queued_directories = [
        queue_directory("offline", "Album", files),
        queue_directory("online", "Album", files),
    ]

    scraper._queue_downloads()

    assert [user for user, _ in client.calls] == ["offline", "online"]
    assert client.accepted == files
    assert scraper.already_downloading_files == {files[0]["filename"]}
    assert scraper.enqueued_count == 1
    assert scraper.queue_errors == []


def test_partial_album_fallback_only_submits_remaining_titles_with_other_basenames():
    client = FakeSlskdClient({
        "best": lambda files: SlskdPeerUnavailableError("best", "unreachable", files[:50]),
    })
    titles = [f"Melody {track:03d}" for track in range(1, 53)]
    scraper = artist_scraper(client, {"Collected Melodies": titles})
    primary_files = add_peer_files(scraper, "best", "Collected Melodies", titles)
    fallback_files = add_peer_files(scraper, "fallback", "Collected Melodies", titles, "mp3")
    scraper._reconcile_primary_releases()

    scraper._queue_downloads()

    assert client.calls == [("best", primary_files), ("fallback", fallback_files[50:])]
    assert client.accepted == primary_files[:50] + fallback_files[50:]
    assert scraper.enqueued_count == 52
    assert scraper.enqueued_files == (
        [{**file, "user": "best"} for file in primary_files[:50]]
        + [{**file, "user": "fallback"} for file in fallback_files[50:]]
    )
    assert scraper.already_downloading_files == {file["filename"] for file in client.accepted}
    assert scraper.queue_errors == []


def test_partial_fallback_keeps_distinct_positions_with_same_normalized_title():
    client = FakeSlskdClient({
        "best": lambda files: SlskdPeerUnavailableError("best", "offline", files[:1]),
    })
    scraper = artist_scraper(client)
    candidates = []
    # Supply verified distinct track matches directly: this exercises queue
    # recovery independently of the matcher's treatment of repeated titles.
    for user, extension in [("best", "flac"), ("fallback", "mp3")]:
        files = [
            {"filename": f"{user}\\Twin Memories\\{index + 1:02d} Echo.{extension}", "size": 1000}
            for index in range(2)
        ]
        candidate = queue_directory(user, "Twin Memories", files)
        candidate.update(
            release="Twin Memories",
            matched_tracks=[
                {"full_filename": file["filename"], "expected": "Echo", "expected_index": index}
                for index, file in enumerate(files)
            ],
        )
        candidates.append(candidate)
    scraper.queued_directories = [candidates[0]]
    scraper._release_candidates["twin memories"] = candidates

    scraper._queue_downloads()

    assert client.calls == [
        ("best", candidates[0]["all_dir_files"]),
        ("fallback", candidates[1]["all_dir_files"][1:]),
    ]
    assert scraper.enqueued_count == 2
    assert scraper.queue_errors == []


@pytest.mark.parametrize("track_group", ["verified_compilation_tracks", "verified_standalone_tracks"])
@pytest.mark.parametrize("filename_field", ["filename", "full_filename"])
def test_single_track_falls_back_using_existing_search_candidates(track_group, filename_field):
    client = FakeSlskdClient({"best": SlskdPeerUnavailableError("best", "offline")})
    scraper = artist_scraper(client)
    initial_file = add_peer_files(scraper, "best", "Night Signals", ["Neon Memory"])[0]
    fallback_file = add_peer_files(scraper, "fallback", "Night Signals", ["Neon Memory"], "mp3")[0]
    fallback_path = fallback_file.pop("filename")
    fallback_file[filename_field] = fallback_path
    submitted_file = {**fallback_file, "filename": fallback_path}
    item = {"user": "best", "track": "Neon Memory", "file": initial_file, "format_label": "FLAC"}
    if track_group == "verified_compilation_tracks":
        item["release"] = "Night Signals"
    setattr(scraper, track_group, [item])

    scraper._queue_downloads()

    assert client.calls == [("best", [initial_file]), ("fallback", [submitted_file])]
    assert client.accepted == [submitted_file]
    assert scraper.enqueued_count == 1
    assert item["user"] == "fallback"
    assert item["file"] == submitted_file
    assert scraper.queue_errors == []


@pytest.mark.parametrize("as_single", [False, True])
def test_unknown_enqueue_error_does_not_replay_from_another_source(as_single):
    client = FakeSlskdClient({"best": SlskdEnqueueError("HTTP 500: Internal database error")})
    scraper = artist_scraper(client, {"Night Signals": ["Neon Memory"]})
    initial_file = add_peer_files(scraper, "best", "Night Signals", ["Neon Memory"])[0]
    add_peer_files(scraper, "fallback", "Night Signals", ["Neon Memory"], "mp3")
    if as_single:
        scraper.verified_standalone_tracks = [{
            "user": "best", "track": "Neon Memory", "file": initial_file,
        }]
    else:
        scraper._reconcile_primary_releases()

    scraper._queue_downloads()

    assert client.calls == [("best", [initial_file])]
    assert client.accepted == []
    assert scraper.enqueued_count == 0
    assert scraper.already_downloading_files == set()
    assert len(scraper.queue_errors) == 1
    assert "Internal database error" in scraper.queue_errors[0]


def test_next_queue_pass_can_retry_previously_offline_peer():
    client = FakeSlskdClient({"peer": SlskdPeerUnavailableError("peer", "offline")})
    scraper = artist_scraper(client)
    files = [{"filename": "Album\\Neon Memory.flac", "size": 1000}]
    scraper.queued_directories = [queue_directory("peer", "Album", files)]
    scraper._queue_downloads()
    assert len(scraper.queue_errors) == 1
    client.failures.clear()

    scraper._queue_downloads()

    assert client.calls == [("peer", files), ("peer", files)]
    assert client.accepted == files
    assert scraper.enqueued_count == 1
    assert scraper.queue_errors == []


@pytest.mark.parametrize("state", ["Rejected", "TimedOut", "Errored"])
def test_failed_history_uses_alternative_and_keeps_healthy_sibling(state):
    client = FakeSlskdClient()
    titles = ["Neon Memory", "Midnight Echo"]
    scraper = artist_scraper(client, {"Night Signals": titles})
    primary = add_peer_files(scraper, "best", "Night Signals", titles)
    fallback = add_peer_files(scraper, "fallback", "Night Signals", titles, "mp3")
    client.history = [{"username": "best", "directories": [{"files": [
        dict(primary[0], id="failed-transfer", state=f"Completed, {state}", attempts=100,
             exception="Peer refused the transfer"),
        dict(primary[1], id="live-transfer", state="Queued, Remotely", attempts=100),
    ]}]}]
    scraper._reconcile_primary_releases()

    scraper._queue_downloads()

    assert client.cancelled == [("best", "failed-transfer")]
    assert client.calls == [("fallback", fallback[:1])]
    assert scraper.enqueued_count == 1
    assert scraper.queue_errors == []


def test_failed_history_without_alternative_reports_reason_and_retries():
    client = FakeSlskdClient()
    scraper = artist_scraper(client, {"Night Signals": ["Neon Memory"]})
    primary = add_peer_files(scraper, "best", "Night Signals", ["Neon Memory"])
    client.history = [{"username": "best", "directories": [{"files": [
        dict(primary[0], id="failed", state="Completed, Errored", attempts=100,
             exception="Access to the path is denied"),
    ]}]}]
    scraper._reconcile_primary_releases()

    scraper._queue_downloads()

    assert not client.calls
    assert not client.cancelled  # No replacement to submit.
    assert scraper.enqueued_count == 0
    assert len(scraper.queue_errors) == 1
    assert "retries: 100" in scraper.queue_errors[0]
    assert "Access to the path is denied" in scraper.queue_errors[0]


@pytest.mark.parametrize("state", ["Queued, Remotely", "Queued, Locally", "InProgress", "Completed, Succeeded", "Completed"])
def test_high_attempt_count_alone_never_replaces_healthy_or_unknown_transfer(state):
    client = FakeSlskdClient()
    scraper = artist_scraper(client, {"Night Signals": ["Neon Memory"]})
    primary = add_peer_files(scraper, "best", "Night Signals", ["Neon Memory"])
    add_peer_files(scraper, "fallback", "Night Signals", ["Neon Memory"], "mp3")
    client.history = [{"username": "best", "directories": [{"files": [
        dict(primary[0], id="existing", state=state, attempts=100),
    ]}]}]
    scraper._reconcile_primary_releases()

    scraper._queue_downloads()

    assert not client.calls
    assert not client.cancelled
    assert scraper.enqueued_count == 0
    assert scraper.queue_errors == []


def test_artist_does_not_replace_failed_transfer_if_cancellation_fails(monkeypatch):
    client = FakeSlskdClient()
    scraper = artist_scraper(client, {"Night Signals": ["Neon Memory"]})
    primary = add_peer_files(scraper, "best", "Night Signals", ["Neon Memory"])
    add_peer_files(scraper, "fallback", "Night Signals", ["Neon Memory"], "mp3")
    client.history = [{"username": "best", "directories": [{"files": [
        dict(primary[0], id="failed", state="Completed, TimedOut"),
    ]}]}]
    scraper._reconcile_primary_releases()

    def fail(*args):
        raise SlskdAPIError("Could not stop retries")

    monkeypatch.setattr(client, "cancel_download", fail)
    scraper._queue_downloads()

    assert not client.calls
    assert scraper.enqueued_count == 0
    assert len(scraper.queue_errors) == 1
    assert "Could not stop retries" in scraper.queue_errors[0]


@pytest.mark.parametrize("kind", ["album", "standalone", "compilation"])
def test_artist_preserves_lower_ranked_healthy_copy_with_different_extension(kind):
    client = FakeSlskdClient()
    scraper = artist_scraper(client, {"Night Signals": ["Neon Memory"]})
    primary = add_peer_files(scraper, "best", "Night Signals", ["Neon Memory"])
    fallback = add_peer_files(scraper, "fallback", "Night Signals", ["Neon Memory"], "mp3")
    client.history = [{"username": "fallback", "directories": [{"files": [
        dict(fallback[0], id="active", state="Queued, Remotely", attempts=100),
    ]}]}]
    if kind == "album":
        scraper._reconcile_primary_releases()
    else:
        item = {"user": "best", "track": "Neon Memory", "file": primary[0]}
        if kind == "compilation":
            item["release"] = "Night Signals"
        setattr(scraper, f"verified_{kind}_tracks", [item])

    scraper._queue_downloads()

    assert client.calls == []
    assert client.cancelled == []
    assert scraper.enqueued_count == 0
    assert scraper.queue_errors == []


@pytest.mark.parametrize("kind", ["album", "standalone", "compilation"])
def test_artist_stops_lower_ranked_failed_copy_before_new_submission(kind, monkeypatch):
    client = FakeSlskdClient()
    scraper = artist_scraper(client, {"Night Signals": ["Neon Memory"]})
    primary = add_peer_files(scraper, "best", "Night Signals", ["Neon Memory"])
    fallback = add_peer_files(scraper, "fallback", "Night Signals", ["Neon Memory"], "mp3")
    client.history = [{"username": "fallback", "directories": [{"files": [
        dict(fallback[0], id="old-failure", state="Completed, TimedOut", attempts=100),
    ]}]}]
    if kind == "album":
        scraper._reconcile_primary_releases()
    else:
        item = {"user": "best", "track": "Neon Memory", "file": primary[0]}
        if kind == "compilation":
            item["release"] = "Night Signals"
        setattr(scraper, f"verified_{kind}_tracks", [item])
    enqueue = client.enqueue_download

    def enqueue_after_cancellation(user, files):
        assert client.cancelled == [("fallback", "old-failure")]
        return enqueue(user, files)

    monkeypatch.setattr(client, "enqueue_download", enqueue_after_cancellation)
    scraper._queue_downloads()

    assert client.calls == [("best", primary)]
    assert scraper.enqueued_count == 1
    assert scraper.queue_errors == []
